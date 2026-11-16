"""Snapshots: each document fetched once and kept.

A snapshot is the raw bytes under their SHA-256, the text and tables derived
from them, and three dates that are never merged: when the document says it
was written, when an archive captured it, and when the pipeline fetched it.
An address that already has a snapshot is never fetched again, except that a
price sheet Toyota updated in place may be asked for every capture the
archive holds of it: see `snapshot_captures`.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import duckdb
import httpx

from car_surveyor import document, source, storage
from car_surveyor.config import Settings
from car_surveyor.fetcher import Fetcher
from car_surveyor.source import Candidate, Registry

DIRECT = "direct"
WAYBACK = "wayback"
MANUAL = "manual"

WAYBACK_AVAILABILITY_ADDRESS = "https://archive.org/wayback/available?url={address}"
# The `id_` flag asks for the capture's bytes as archived, without the
# archive's banner or rewritten links.
WAYBACK_CAPTURE_ADDRESS = "https://web.archive.org/web/{timestamp}id_/{address}"
# Every capture that returned 200, one per distinct content digest, oldest first.
WAYBACK_INDEX_ADDRESS = (
    "https://web.archive.org/cdx/search/cdx?url={address}"
    "&fl=timestamp,digest&filter=statuscode:200&collapse=digest"
)
# Documents Toyota updates in place at one address, so each capture may differ.
DATED_KINDS = (source.PRICING_PDF,)

_EXTENSIONS = {document.PDF: "pdf", document.HTML: "html", document.ZIP: "zip", "text/xml": "xml"}
_MEDIA_TYPES_BY_SUFFIX = {".pdf": document.PDF, ".zip": document.ZIP, ".html": document.HTML}


@dataclass(frozen=True, slots=True)
class Snapshot:
    sha256: str
    source_id: str
    url: str
    fetch_url: str
    route: str
    kind: str
    media_type: str
    raw_path: Path
    text_path: Path
    tables_path: Path
    fetched_at: datetime
    capture_date: date | None
    document_date: date | None

    def read_raw(self) -> bytes:
        return self.raw_path.read_bytes()

    def read_text(self) -> str:
        return self.text_path.read_text(encoding="utf-8")

    def read_tables(self) -> tuple[document.Table, ...]:
        return document.parse_tables(self.tables_path.read_text(encoding="utf-8"))


@dataclass(slots=True)
class SnapshotReport:
    """What one run of the snapshot stage did, by candidate address."""

    fetched: list[str]
    already_held: list[str]
    unavailable: list[str]

    def describe(self) -> str:
        return (
            f"{len(self.fetched)} fetched, {len(self.already_held)} already held, "
            f"{len(self.unavailable)} unavailable"
        )


def find_snapshot(
    connection: duckdb.DuckDBPyConnection, settings: Settings, url: str
) -> Snapshot | None:
    """The snapshot of an address, if the pipeline holds one."""
    snapshots = list_snapshots(connection, settings, url=url)
    return snapshots[0] if snapshots else None


def list_snapshots(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    *,
    url: str | None = None,
    kinds: tuple[str, ...] = (),
) -> list[Snapshot]:
    records = storage.fetch_records(connection, "SELECT * FROM snapshots ORDER BY fetched_at, url")
    return [
        _build_snapshot(record, settings.snapshot_dir)
        for record in records
        if (url is None or record["url"] == url) and (not kinds or record["kind"] in kinds)
    ]


def store_snapshot(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    *,
    url: str,
    source_id: str,
    kind: str,
    raw: bytes,
    media_type: str,
    fetch_url: str,
    route: str,
    capture_date: date | None = None,
) -> Snapshot:
    """Keep a document's bytes and what is derived from them.

    The table holds file names, not paths, so the data directory can be moved.
    """
    sha256 = hashlib.sha256(raw).hexdigest()
    derived = document.derive_document(raw, media_type)
    record: dict[str, Any] = {
        "sha256": sha256,
        "source_id": source_id,
        "url": url,
        "fetch_url": fetch_url,
        "route": route,
        "kind": kind,
        "media_type": media_type,
        "raw_path": f"{sha256}.{_EXTENSIONS.get(media_type, 'bin')}",
        "text_path": f"{sha256}.txt",
        "tables_path": f"{sha256}.tables.json",
        "fetched_at": datetime.now(UTC).replace(tzinfo=None),
        "capture_date": capture_date,
        "document_date": None,
    }
    settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot = _build_snapshot(record, settings.snapshot_dir)
    snapshot.raw_path.write_bytes(raw)
    snapshot.text_path.write_text(derived.text, encoding="utf-8")
    snapshot.tables_path.write_text(document.serialize_tables(derived.tables), encoding="utf-8")
    placeholders = ", ".join("?" for _ in record)
    connection.execute(f"INSERT INTO snapshots VALUES ({placeholders})", list(record.values()))
    return snapshot


def rederive_snapshots(connection: duckdb.DuckDBPyConnection, settings: Settings) -> int:
    """Derive every held document's text and tables again from its raw bytes.

    Nothing is fetched. This is for after a change to how documents are read:
    the stored bytes are the record, and what is derived from them is not.
    Returns how many documents were derived.
    """
    held = {found.sha256: found for found in list_snapshots(connection, settings)}
    for found in held.values():
        derived = document.derive_document(found.read_raw(), found.media_type)
        found.text_path.write_text(derived.text, encoding="utf-8")
        found.tables_path.write_text(document.serialize_tables(derived.tables), encoding="utf-8")
    return len(held)


def fetch_snapshot(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    fetcher: Fetcher,
    *,
    url: str,
    source_id: str,
    kind: str,
) -> Snapshot:
    """Return the snapshot of a directly fetchable address, fetching it at most once."""
    held = find_snapshot(connection, settings, url)
    if held is not None:
        return held
    response = fetcher.fetch(url)
    if not response.is_success:
        raise LookupError(f"HTTP {response.status_code}")
    return store_snapshot(
        connection,
        settings,
        url=url,
        source_id=source_id,
        kind=kind,
        raw=response.body,
        media_type=response.media_type,
        fetch_url=response.url,
        route=DIRECT,
    )


def snapshot_documents(
    connection: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
    fetcher: Fetcher,
    *,
    model_years: tuple[int, ...] = (),
    is_retrying_unavailable: bool = False,
) -> SnapshotReport:
    """Fetch every candidate document that has no snapshot yet.

    A candidate found unavailable is not asked for again unless told to, so a
    second run makes no requests. The inbox is local and is always checked.
    """
    report = SnapshotReport([], [], [])
    for candidate in source.list_candidates(connection, model_years=model_years):
        if candidate.kind == source.VEHICLE_PAGE:
            continue
        if find_snapshot(connection, settings, candidate.url) is not None:
            source.mark_candidate(connection, candidate.url, "fetched")
            report.already_held.append(candidate.url)
            continue
        is_asking_network = candidate.status == "pending" or is_retrying_unavailable
        try:
            problem = _snapshot_candidate(
                connection, registry, settings, fetcher, candidate, is_asking_network
            )
        except (LookupError, httpx.HTTPError, document.UnreadableDocumentError) as error:
            problem = str(error) or type(error).__name__
        if problem is None:
            source.mark_candidate(connection, candidate.url, "fetched")
            report.fetched.append(candidate.url)
        else:
            source.mark_candidate(connection, candidate.url, "unavailable", problem)
            report.unavailable.append(candidate.url)
    return report


def snapshot_captures(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    fetcher: Fetcher,
    *,
    model_years: tuple[int, ...] = (),
) -> SnapshotReport:
    """Fetch each archived capture of a held price sheet whose content is not held yet.

    The archive's digest of a capture is compared with the held bytes, so a
    capture already held is never fetched again. A capture is kept as its own
    snapshot under the same address, with its own capture date, and its
    prices become dated values beside the others.
    """
    report = SnapshotReport([], [], [])
    for candidate in source.list_candidates(connection, model_years=model_years):
        held = list_snapshots(connection, settings, url=candidate.url)
        if candidate.kind not in DATED_KINDS or not held:
            continue
        digests = {get_wayback_digest(found.read_raw()) for found in held}
        held_bytes = {found.sha256 for found in held}
        try:
            captures = list_wayback_captures(fetcher, candidate.url)
        except (LookupError, httpx.HTTPError) as error:
            report.unavailable.append(f"{candidate.url}: {str(error) or type(error).__name__}")
            continue
        for capture_address, capture_date, digest in captures:
            label = f"{candidate.url} ({capture_date})"
            if digest in digests:
                report.already_held.append(label)
                continue
            response = fetcher.fetch(capture_address)
            if not response.is_success or not response.body.startswith(b"%PDF"):
                report.unavailable.append(f"{label}: not a PDF, HTTP {response.status_code}")
                continue
            if hashlib.sha256(response.body).hexdigest() in held_bytes:
                report.already_held.append(label)
                continue
            stored = store_snapshot(
                connection,
                settings,
                url=candidate.url,
                source_id=candidate.source_id,
                kind=candidate.kind,
                raw=response.body,
                media_type=document.PDF,
                fetch_url=response.url,
                route=WAYBACK,
                capture_date=capture_date,
            )
            held_bytes.add(stored.sha256)
            report.fetched.append(label)
    return report


def list_wayback_captures(fetcher: Fetcher, url: str) -> list[tuple[str, date, str]]:
    """Each distinct capture of an address: where to fetch it, its date, and its digest."""
    response = fetcher.fetch(WAYBACK_INDEX_ADDRESS.format(address=quote(url, safe="")))
    if not response.is_success:
        raise LookupError(f"HTTP {response.status_code} from the Wayback index")
    captures: list[tuple[str, date, str]] = []
    for line in response.body.decode("utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) != 2 or len(fields[0]) < 8 or not fields[0].isdigit():
            continue
        timestamp, digest = fields
        capture_date = datetime.strptime(timestamp[:8], "%Y%m%d").date()
        address = WAYBACK_CAPTURE_ADDRESS.format(timestamp=timestamp, address=url)
        captures.append((address, capture_date, digest))
    return captures


def get_wayback_digest(raw: bytes) -> str:
    """The digest the Wayback index gives a capture: the base32 SHA-1 of its bytes."""
    return base64.b32encode(hashlib.sha1(raw, usedforsecurity=False).digest()).decode("ascii")


def find_wayback_capture(fetcher: Fetcher, url: str) -> tuple[str, date] | None:
    """The address and date of the Wayback capture the archive offers, if any."""
    response = fetcher.fetch(WAYBACK_AVAILABILITY_ADDRESS.format(address=quote(url, safe="")))
    if not response.is_success:
        raise LookupError(f"HTTP {response.status_code} from the Wayback availability lookup")
    closest: dict[str, Any] = (
        json.loads(response.body).get("archived_snapshots", {}).get("closest", {})
    )
    timestamp = str(closest.get("timestamp", ""))
    if not closest.get("available") or closest.get("status") != "200" or len(timestamp) < 8:
        return None
    capture_date = datetime.strptime(timestamp[:8], "%Y%m%d").date()
    return WAYBACK_CAPTURE_ADDRESS.format(timestamp=timestamp, address=url), capture_date


def _snapshot_candidate(
    connection: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
    fetcher: Fetcher,
    candidate: Candidate,
    is_asking_network: bool,
) -> str | None:
    """Snapshot one candidate. Returns None on success, or why it is unavailable."""
    if registry.get_source(candidate.source_id).route == "direct":
        if not is_asking_network:
            return candidate.detail
        fetch_snapshot(
            connection,
            settings,
            fetcher,
            url=candidate.url,
            source_id=candidate.source_id,
            kind=candidate.kind,
        )
        return None

    # A host that refuses this client: a hand-downloaded file, else the archive.
    inbox_path = settings.inbox_dir / source.get_file_name(candidate.url)
    if inbox_path.is_file():
        store_snapshot(
            connection,
            settings,
            url=candidate.url,
            source_id=candidate.source_id,
            kind=candidate.kind,
            raw=inbox_path.read_bytes(),
            media_type=_MEDIA_TYPES_BY_SUFFIX.get(inbox_path.suffix.lower(), ""),
            fetch_url=inbox_path.resolve().as_uri(),
            route=MANUAL,
        )
        return None
    if not is_asking_network:
        return candidate.detail
    capture = find_wayback_capture(fetcher, candidate.url)
    if capture is None:
        return f"no Wayback capture; download it by hand into {settings.inbox_dir}/"
    capture_address, capture_date = capture
    response = fetcher.fetch(capture_address)
    if not response.is_success:
        return f"HTTP {response.status_code} from the Wayback capture"
    is_pdf = response.body.startswith(b"%PDF")
    if candidate.kind.endswith("_pdf") and not is_pdf:
        return "the Wayback capture is not a PDF"
    store_snapshot(
        connection,
        settings,
        url=candidate.url,
        source_id=candidate.source_id,
        kind=candidate.kind,
        raw=response.body,
        media_type=document.PDF if is_pdf else response.media_type,
        fetch_url=response.url,
        route=WAYBACK,
        capture_date=capture_date,
    )
    return None


def _build_snapshot(record: dict[str, Any], snapshot_dir: Path) -> Snapshot:
    return Snapshot(
        sha256=record["sha256"],
        source_id=record["source_id"],
        url=record["url"],
        fetch_url=record["fetch_url"],
        route=record["route"],
        kind=record["kind"],
        media_type=record["media_type"],
        raw_path=snapshot_dir / record["raw_path"],
        text_path=snapshot_dir / record["text_path"],
        tables_path=snapshot_dir / record["tables_path"],
        fetched_at=record["fetched_at"],
        capture_date=record["capture_date"],
        document_date=record["document_date"],
    )
