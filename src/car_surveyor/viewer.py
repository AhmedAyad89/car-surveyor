"""The development viewer: one local page over the database, the snapshots and the diff's feedback.

It only reads. Each request opens the database read-only and closes it, so
the pipeline can run between two clicks; while a stage holds the file, the
page says so. Nothing comes from the network: the page, its script and its
style sheet are the files in `viewer_assets/`.

Isolation. The viewer reads the data directory's database, its snapshots,
and `feedback/`, whose files hold the diff's verdict on each cell and never a
reference value. A reference value can reach the page one way only: the user
names the diff's detail file with `--diff-detail`. That argument has no
default and is refused in an agent session. What it unlocks is served by one
endpoint, `/api/diff`, and only to a request that carries the token printed
in the user's terminal, so another process on the machine that asks the same
server gets the page without reference values.
"""

from __future__ import annotations

import csv
import json
import os
import re
import secrets
import traceback
from collections import Counter, defaultdict
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import duckdb

from car_surveyor import claim, consistency, document, evidence, source, storage
from car_surveyor.claim import ClaimLocator, PageHint
from car_surveyor.config import Settings
from car_surveyor.document import Table
from car_surveyor.evidence import FileRow, TableCell, TextSpan

ASSET_DIR = Path(__file__).resolve().parent / "viewer_assets"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# Claude Code sets this in every session it starts. Where it is set, the
# detail file is never opened.
AGENT_SESSION_VARIABLE = "CLAUDECODE"

# How much of a document is shown either side of a quoted span.
CONTEXT_CHARACTERS = 400
# The most rejected claims one answer lists. The counts are always complete.
MAX_LISTED_CLAIMS = 2000

REFUSAL = f"""\
view: --diff-detail was refused. {AGENT_SESSION_VARIABLE} is set, so this is an agent session,
  and the detail file holds reference values, which no agent session may read.
  Run it from your own terminal:
    just view --diff-detail <file>
  Without --diff-detail the viewer shows no reference value and runs anywhere."""

_STYLE_COLUMNS = (
    "style_key",
    "model_year",
    "model",
    "model_code",
    "grade",
    "powertrain",
    "body",
    "engine",
    "transmission",
    "drive",
    "description",
    "drive_class",
    "is_drive_inferred",
    "transmission_class",
    "is_transmission_inferred",
    "document_count",
)
# A claim names its document by content hash, and two addresses can hold the
# same bytes. Joining claims to this, and not to `snapshots`, keeps one row per
# claim: the address fetched first stands for the document, and
# `address_count` says how many hold it.
_SNAPSHOT_PER_HASH = """(
    SELECT *, count(*) OVER (PARTITION BY sha256) AS address_count
    FROM snapshots
    QUALIFY row_number() OVER (PARTITION BY sha256 ORDER BY fetched_at, url) = 1
)"""
# The snapshot a claim came from, joined as `s`.
_DOCUMENT_COLUMNS = """
    s.url, s.kind AS document_kind, s.route, s.media_type, s.capture_date, s.document_date,
    s.fetched_at, s.text_path, s.tables_path, s.address_count
"""
# The columns of the fuel economy file that say which vehicle a row is.
_ROW_IDENTITY_COLUMNS = ("id", "make", "year", "model")

# What a feedback file may hold, by the diff's contract. Anything else in one
# is dropped before it is served, and named so the page can say so.
_FEEDBACK_KEYS = (
    "run_id",
    "created_at",
    "split",
    "model_years",
    "git_sha",
    "tolerances",
    "row_matching",
    "by_reference_model",
    "by_pipeline_model",
    "metrics",
    "buckets",
    "cells",
    "extra_styles",
)
_REFERENCE_MODEL_KEYS = ("model_year", "model", "rows", "matched", "missed")
_PIPELINE_MODEL_KEYS = ("model_year", "model", "styles", "matched", "extra")
_METRIC_KEYS = ("model_year", "attribute", "agree", "disagree", "pipeline_empty", "gap_filled")
_CELL_KEYS = ("style_key", "attribute", "status", "bucket")
# What the detail file adds. These hold reference values.
_DETAIL_KEYS = ("matches", "missed", "extra")

_SNAPSHOT_PATH_PATTERN = re.compile(r"(?P<sha256>[0-9a-f]{64})/(?P<part>raw|text)")
_UNSAFE_FILE_NAME_PATTERN = re.compile(r"[^\w.\- ]")
_ASSET_MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}
# The page may load its own files and nothing else.
_PAGE_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'",
    ),
)

# Rows of data files already looked up, by file, its modification time and row key.
_FILE_ROWS: dict[tuple[str, int, str], dict[str, str] | None] = {}
_MAX_HELD_FILE_ROWS = 5000


@dataclass(frozen=True, slots=True)
class Viewer:
    """What one viewer serves: a data directory and, if the user named it, the detail file."""

    settings: Settings
    diff_detail_path: Path | None = None
    # Set with the detail file. A request must carry it to be shown a reference value.
    token: str = ""


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    media_type: str
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()


class Problem(Exception):
    """A state the page shows in place of data, with the HTTP status that goes with it."""

    def __init__(self, state: str, message: str, status: int = 500) -> None:
        super().__init__(message)
        self.state = state
        self.message = message
        self.status = status


class ReferenceRefusal(Exception):
    """The detail file was asked for where reference values must not be read."""


@dataclass(frozen=True, slots=True)
class DerivedFiles:
    """The text and tables derived from snapshots, each read at most once per request."""

    snapshot_dir: Path
    texts: dict[str, str]
    tables: dict[str, tuple[Table, ...]]

    def read_text(self, file_name: str) -> str:
        if file_name not in self.texts:
            self.texts[file_name] = (self.snapshot_dir / file_name).read_text(encoding="utf-8")
        return self.texts[file_name]

    def read_tables(self, file_name: str) -> tuple[Table, ...]:
        if file_name not in self.tables:
            serialized = (self.snapshot_dir / file_name).read_text(encoding="utf-8")
            self.tables[file_name] = document.parse_tables(serialized)
        return self.tables[file_name]


# --- starting and serving ----------------------------------------------------


def serve(
    settings: Settings, *, port: int = DEFAULT_PORT, diff_detail_path: Path | None = None
) -> int:
    """Run the viewer until it is interrupted. Returns the process's exit status."""
    try:
        viewer = build_viewer(settings, diff_detail_path)
    except ReferenceRefusal as refusal:
        print(refusal)
        return 2
    except FileNotFoundError as error:
        print(f"view: {error}")
        return 2
    try:
        server = ViewerServer(viewer, port)
    except OSError as error:
        print(f"view: port {port} cannot be used ({error.strerror}); choose another with --port")
        return 1
    address = f"http://{HOST}:{server.server_port}/"
    print(f"view: serving {settings.data_dir}/ at {address}")
    if viewer.diff_detail_path is not None:
        print(f"view: SHOWING REFERENCE VALUES from {viewer.diff_detail_path}")
        print(f"  open {address}?token={viewer.token}")
        print("  the address without the token shows the page without reference values")
    # Flushed, so the address shows at once when the output goes to a pipe.
    print("view: Ctrl-C stops it", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


def build_viewer(
    settings: Settings,
    diff_detail_path: Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> Viewer:
    """The viewer for a data directory. Naming a detail file is refused in an agent session."""
    if diff_detail_path is None:
        return Viewer(settings)
    check_reference_access(os.environ if environment is None else environment)
    if not diff_detail_path.is_file():
        raise FileNotFoundError(f"--diff-detail: there is no file at {diff_detail_path}")
    return Viewer(settings, diff_detail_path, secrets.token_urlsafe(16))


def check_reference_access(environment: Mapping[str, str]) -> None:
    """Raise unless this process may read reference values. An agent session may not."""
    if environment.get(AGENT_SESSION_VARIABLE):
        raise ReferenceRefusal(REFUSAL)


class ViewerServer(ThreadingHTTPServer):
    """The local HTTP server, holding what its requests are answered from."""

    def __init__(self, viewer: Viewer, port: int) -> None:
        super().__init__((HOST, port), _Exchange)
        self.viewer = viewer


class _Exchange(BaseHTTPRequestHandler):
    """One request to the viewer and its answer."""

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, ViewerServer)
        response = respond(server.viewer, self.path)
        self.send_response(response.status)
        self.send_header("Content-Type", response.media_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response.body)

    def log_message(self, format: str, *args: Any) -> None:
        """Say nothing about requests that worked: the terminal is for what went wrong."""
        status = str(args[1]) if len(args) > 1 else ""
        if not status.startswith(("2", "3")):
            super().log_message(format, *args)


def respond(viewer: Viewer, target: str) -> Response:
    """Answer one GET request. A failure is an answer too, in a form the page can show."""
    parts = urlsplit(target)
    query = {name: values[-1] for name, values in parse_qs(parts.query).items()}
    try:
        if parts.path == "/":
            return read_asset("index.html")
        if parts.path.startswith("/assets/"):
            return read_asset(parts.path.removeprefix("/assets/"))
        if parts.path.startswith("/snapshot/"):
            return read_snapshot_file(viewer, parts.path.removeprefix("/snapshot/"))
        if parts.path.startswith("/api/"):
            return build_json_response(answer_api(viewer, parts.path.removeprefix("/api/"), query))
        raise Problem("not_found", f"There is nothing at {parts.path}.", 404)
    except Problem as problem:
        payload = {"state": problem.state, "message": problem.message}
        return build_json_response(payload, problem.status)
    except ReferenceRefusal as refusal:
        return build_json_response({"state": "refused", "message": str(refusal)}, 403)
    except Exception as error:
        # The terminal gets the traceback; the page gets a sentence.
        traceback.print_exc()
        payload = {"state": "error", "message": f"{type(error).__name__}: {error}"}
        return build_json_response(payload, 500)


def answer_api(viewer: Viewer, name: str, query: Mapping[str, str]) -> dict[str, Any]:
    is_showing_reference = is_reference_unlocked(viewer, query.get("token", ""))
    if name == "meta":
        return build_meta(viewer, is_showing_reference=is_showing_reference)
    if name == "diff":
        return load_diff(viewer, query.get("run"), is_showing_reference=is_showing_reference)
    loader = _DATABASE_ROUTES.get(name)
    if loader is None:
        raise Problem("not_found", f"There is no endpoint /api/{name}.", 404)
    with open_database(viewer.settings) as connection:
        return loader(connection, viewer.settings, query)


def is_reference_unlocked(viewer: Viewer, token: str) -> bool:
    """Whether a request may be shown reference values: only with the detail file's token."""
    if viewer.diff_detail_path is None or not viewer.token:
        return False
    return secrets.compare_digest(token.encode(), viewer.token.encode())


def build_json_response(payload: dict[str, Any], status: int = 200) -> Response:
    body = json.dumps(payload, default=_encode_value, ensure_ascii=False).encode("utf-8")
    return Response(status, "application/json; charset=utf-8", body)


def read_asset(name: str) -> Response:
    """One of the page's own files. A name that leaves the asset directory is not one."""
    path = ASSET_DIR / name
    media_type = _ASSET_MEDIA_TYPES.get(path.suffix)
    if media_type is None or path.parent != ASSET_DIR or not path.is_file():
        raise Problem("not_found", f"There is no asset named {name!r}.", 404)
    return Response(200, media_type, path.read_bytes(), _PAGE_HEADERS)


def read_snapshot_file(viewer: Viewer, target: str) -> Response:
    """A snapshot's raw bytes or its derived text, by hash.

    A PDF is shown in the browser. Any other raw document is offered as a
    download: a saved web page opened as a page would fetch what it links to.
    """
    match = _SNAPSHOT_PATH_PATTERN.fullmatch(target)
    if match is None:
        raise Problem("not_found", "A snapshot is /snapshot/<sha256>/raw or /text.", 404)
    with open_database(viewer.settings) as connection:
        records = storage.fetch_records(
            connection,
            # Addresses that share a hash share its files: any one of them will do.
            "SELECT url, raw_path, text_path FROM snapshots WHERE sha256 = ? "
            "ORDER BY fetched_at, url LIMIT 1",
            [match["sha256"]],
        )
    if not records:
        raise Problem("not_found", "The pipeline holds no snapshot with that hash.", 404)
    record = records[0]
    snapshot_dir = viewer.settings.snapshot_dir
    path = snapshot_dir / record["text_path" if match["part"] == "text" else "raw_path"]
    if path.parent != snapshot_dir or not path.is_file():
        raise Problem("not_found", f"The snapshot's file {path.name} is not on disk.", 404)
    body = path.read_bytes()
    file_name = _UNSAFE_FILE_NAME_PATTERN.sub("_", source.get_file_name(record["url"]) or path.name)
    headers = [("X-Content-Type-Options", "nosniff")]
    if match["part"] == "text":
        media_type = "text/plain; charset=utf-8"
    elif body.startswith(b"%PDF"):
        media_type = document.PDF
        headers.append(("Content-Disposition", f'inline; filename="{file_name}"'))
    else:
        media_type = "application/octet-stream"
        headers.append(("Content-Disposition", f'attachment; filename="{file_name}"'))
    return Response(200, media_type, body, tuple(headers))


@contextmanager
def open_database(settings: Settings) -> Generator[duckdb.DuckDBPyConnection]:
    """A read-only connection for one request, or the reason there is none."""
    path = settings.database_path
    if not path.is_file():
        message = f"There is no database at {path} yet. Run the pipeline, or pass --data-dir."
        raise Problem("no_database", message, 404)
    try:
        connection = storage.connect(path, is_read_only=True)
    except duckdb.Error as error:
        if isinstance(error, (duckdb.IOException, duckdb.ConnectionException)) and (
            "lock" in str(error).lower() or "different configuration" in str(error)
        ):
            message = "The pipeline is running and holds the database. Try again when it finishes."
            raise Problem("locked", message, 503) from error
        raise Problem("error", f"The database could not be opened: {error}", 500) from error
    try:
        yield connection
    except duckdb.CatalogException as error:
        message = (
            "The database has no resolved styles yet, or its views are older than this viewer. "
            f"Run `just resolve`. ({str(error).splitlines()[0]})"
        )
        raise Problem("not_resolved", message, 409) from error
    finally:
        connection.close()


def build_meta(viewer: Viewer, *, is_showing_reference: bool) -> dict[str, Any]:
    """What the page needs before it asks for data. Reads no file."""
    if is_showing_reference and viewer.diff_detail_path is not None:
        reference, detail_file = "shown", viewer.diff_detail_path.name
    elif viewer.diff_detail_path is not None:
        reference, detail_file = "needs_token", None
    else:
        reference, detail_file = "off", None
    settings = viewer.settings
    return {
        "data_dir": str(settings.data_dir.resolve()),
        "inbox_dir": str(settings.inbox_dir.resolve()),
        "feedback_dir": str(settings.feedback_dir.resolve()),
        "has_database": settings.database_path.is_file(),
        "attributes": list(claim.ATTRIBUTES),
        "reference": reference,
        "diff_detail_file": detail_file,
    }


# --- the catalogue and one cell of it ----------------------------------------


def load_catalogue(
    connection: duckdb.DuckDBPyConnection, settings: Settings, query: Mapping[str, str]
) -> dict[str, Any]:
    """Every style with the state of each of its cells."""
    styles = load_styles(connection)
    return {
        "attributes": list(claim.ATTRIBUTES),
        "model_years": sorted({style["model_year"] for style in styles}),
        "styles": styles,
    }


def load_styles(
    connection: duckdb.DuckDBPyConnection, style_key: str | None = None
) -> list[dict[str, Any]]:
    """Rows of the `styles` view, each cell marked chosen, open or unstated.

    The view gives the chosen value. What was stated for a cell that has
    none, and the unit a value is in, come from `style_values`.
    """
    where, parameters = ("WHERE style_key = ?", [style_key]) if style_key else ("", [])
    rows = storage.fetch_records(
        connection,
        f"SELECT * FROM styles {where} ORDER BY model_year, model, description, style_key",
        parameters,
    )
    stated = storage.fetch_records(
        connection,
        f"""
        SELECT style_key, attribute,
            bool_or(is_conflicting) AS is_conflicting,
            list(DISTINCT value_num ORDER BY value_num) AS stated_values,
            list(DISTINCT value_num ORDER BY value_num) FILTER (WHERE is_conflicting)
                AS conflicting_values,
            max(unit) FILTER (WHERE is_chosen) AS chosen_unit,
            min(unit) AS unit,
            count(*) AS claim_count
        FROM style_values {where}
        GROUP BY style_key, attribute
        """,
        parameters,
    )
    stated_by_cell = {(record["style_key"], record["attribute"]): record for record in stated}
    finding_counts = Counter(
        record["style_key"]
        for record in storage.fetch_records(
            connection, f"SELECT style_key FROM check_results {where}", parameters
        )
    )
    styles: list[dict[str, Any]] = []
    for row in rows:
        style = {column: row.get(column) for column in _STYLE_COLUMNS}
        style["finding_count"] = finding_counts.get(row["style_key"], 0)
        style["cells"] = {
            attribute: build_cell_state(
                row, attribute, stated_by_cell.get((row["style_key"], attribute))
            )
            for attribute in claim.ATTRIBUTES
        }
        styles.append(style)
    return styles


def build_cell_state(
    row: Mapping[str, Any], attribute: str, stated: Mapping[str, Any] | None
) -> dict[str, Any]:
    """One cell: a chosen value, left open by disagreement, or unstated.

    A chosen cell can still be contested: another leading claim states a
    different value. That is said, because the other claim is worth a look.
    """
    if stated is None:
        return {"state": "unstated"}
    value = row.get(attribute)
    if value is None:
        return {
            "state": "open",
            "values": stated["conflicting_values"] or stated["stated_values"],
            "unit": stated["unit"],
            "claim_count": stated["claim_count"],
        }
    return {
        "state": "chosen",
        "value": value,
        "unit": stated["chosen_unit"] or stated["unit"],
        "date": row.get(f"{attribute}_date"),
        "claim_id": row.get(f"{attribute}_claim"),
        "is_contested": bool(stated["is_conflicting"]),
        "claim_count": stated["claim_count"],
    }


def load_cell(
    connection: duckdb.DuckDBPyConnection, settings: Settings, query: Mapping[str, str]
) -> dict[str, Any]:
    """One style and attribute: every value attached to it, each with its claim and evidence."""
    style_key = _require(query, "style_key")
    attribute = _require(query, "attribute")
    if attribute not in claim.ATTRIBUTES:
        raise Problem("bad_request", f"{attribute!r} is not an attribute.", 400)
    style = find_style(connection, style_key)
    records = storage.fetch_records(
        connection,
        f"""
        SELECT v.claim_id, v.value_num, v.value_text, v.unit, v.effective_date, v.source_class,
            v.snapshot_sha256, v.specificity, v.note, v.is_chosen, v.is_conflicting,
            'value' AS claim_type, c.attribute, c.quote, c.locator, c.extractor_version,
            c.status, c.reject_reason, c.applies_to, c.qualifiers, c.hedge_flags,
            {_DOCUMENT_COLUMNS}
        FROM style_values AS v
        LEFT JOIN value_claims AS c ON c.claim_id = v.claim_id
        LEFT JOIN {_SNAPSHOT_PER_HASH} AS s ON s.sha256 = v.snapshot_sha256
        WHERE v.style_key = ? AND v.attribute = ?
        ORDER BY v.is_chosen DESC, v.is_conflicting DESC, v.specificity DESC,
            v.effective_date DESC NULLS LAST, v.value_num, v.claim_id
        """,
        [style_key, attribute],
    )
    claims = build_claim_views(records, _open_derived_files(settings))
    values = [
        {
            "value_num": record["value_num"],
            "value_text": record["value_text"],
            "unit": record["unit"],
            "effective_date": record["effective_date"],
            "source_class": record["source_class"],
            "specificity": record["specificity"],
            "note": record["note"],
            "is_chosen": record["is_chosen"],
            "is_conflicting": record["is_conflicting"],
            "claim": view,
        }
        for record, view in zip(records, claims, strict=True)
    ]
    return {
        "style": style,
        "attribute": attribute,
        "cell": style["cells"][attribute],
        "values": values,
        "findings": list_findings(connection, style_key),
    }


def load_style(
    connection: duckdb.DuckDBPyConnection, settings: Settings, query: Mapping[str, str]
) -> dict[str, Any]:
    """One style: the rows of documents that say it exists, and what the checks found."""
    style_key = _require(query, "style_key")
    style = find_style(connection, style_key)
    records = storage.fetch_records(
        connection,
        f"""
        SELECT c.claim_id, 'style' AS claim_type, c.description AS value_text, c.quote,
            c.locator, c.extractor_version, c.status, c.reject_reason, c.snapshot_sha256,
            {_DOCUMENT_COLUMNS}
        FROM style_claims AS c
        LEFT JOIN {_SNAPSHOT_PER_HASH} AS s ON s.sha256 = c.snapshot_sha256
        WHERE c.style_key = ?
        ORDER BY c.status, s.document_date DESC NULLS LAST, c.claim_id
        """,
        [style_key],
    )
    return {
        "style": style,
        "claims": build_claim_views(records, _open_derived_files(settings)),
        "findings": list_findings(connection, style_key),
    }


def load_claim(
    connection: duckdb.DuckDBPyConnection, settings: Settings, query: Mapping[str, str]
) -> dict[str, Any]:
    """One claim, admitted or not, with its evidence and the cells it was attached to."""
    claim_id = _require(query, "claim_id")
    records = storage.fetch_records(
        connection,
        f"""
        SELECT c.claim_id, 'value' AS claim_type, c.attribute, c.value_text, c.value_num, c.unit,
            c.style_key, c.effective_date, c.source_class, c.quote, c.locator,
            c.extractor_version, c.status, c.reject_reason, c.applies_to, c.qualifiers,
            c.hedge_flags, c.snapshot_sha256, {_DOCUMENT_COLUMNS}
        FROM value_claims AS c
        LEFT JOIN {_SNAPSHOT_PER_HASH} AS s ON s.sha256 = c.snapshot_sha256
        WHERE c.claim_id = ?
        """,
        [claim_id],
    ) or storage.fetch_records(
        connection,
        f"""
        SELECT c.claim_id, 'style' AS claim_type, c.description AS value_text, c.style_key,
            c.quote, c.locator, c.extractor_version, c.status, c.reject_reason,
            c.snapshot_sha256, {_DOCUMENT_COLUMNS}
        FROM style_claims AS c
        LEFT JOIN {_SNAPSHOT_PER_HASH} AS s ON s.sha256 = c.snapshot_sha256
        WHERE c.claim_id = ?
        """,
        [claim_id],
    )
    if not records:
        raise Problem("not_found", f"There is no claim {claim_id!r}.", 404)
    record = records[0]
    (view,) = build_claim_views([record], _open_derived_files(settings))
    attached = storage.fetch_records(
        connection,
        """
        SELECT v.style_key, r.description, r.model_year, v.attribute, v.is_chosen, v.is_conflicting
        FROM style_values AS v
        JOIN style_records AS r ON r.style_key = v.style_key
        WHERE v.claim_id = ?
        ORDER BY r.model_year, r.description, v.style_key
        """,
        [claim_id],
    )
    return {
        "claim": view,
        "value_num": record.get("value_num"),
        "unit": record.get("unit"),
        "style_key": record.get("style_key"),
        "effective_date": record.get("effective_date"),
        "source_class": record.get("source_class"),
        "attached_to": attached,
    }


def find_style(connection: duckdb.DuckDBPyConnection, style_key: str) -> dict[str, Any]:
    styles = load_styles(connection, style_key)
    if not styles:
        raise Problem("not_found", f"No style has the key {style_key!r}.", 404)
    return styles[0]


def list_findings(connection: duckdb.DuckDBPyConnection, style_key: str) -> list[dict[str, Any]]:
    return storage.fetch_records(
        connection,
        "SELECT check_name, attribute, detail FROM check_results WHERE style_key = ? "
        "ORDER BY check_name, attribute, detail",
        [style_key],
    )


# --- a claim and where its document says it ----------------------------------


def build_claim_views(records: list[dict[str, Any]], derived: DerivedFiles) -> list[dict[str, Any]]:
    """Each claim with its document and its evidence in context.

    The rows of a data file that the claims point at are looked up together,
    so the file is read once however many claims name it.
    """
    wanted: dict[str, set[str]] = defaultdict(set)
    for record in records:
        locator = _parse_locator(record)
        if isinstance(locator, FileRow) and record.get("text_path"):
            wanted[record["text_path"]].add(locator.row_key)
    for file_name, row_keys in wanted.items():
        try:
            find_file_rows(derived.snapshot_dir / file_name, row_keys)
        except OSError:
            # Said per claim, by build_evidence.
            continue
    return [build_claim_view(record, derived) for record in records]


def build_claim_view(record: Mapping[str, Any], derived: DerivedFiles) -> dict[str, Any]:
    sha256 = record.get("snapshot_sha256")
    has_snapshot = record.get("url") is not None
    return {
        "claim_id": record["claim_id"],
        "claim_type": record["claim_type"],
        "attribute": record.get("attribute"),
        "value_text": record.get("value_text"),
        "quote": record.get("quote"),
        "extractor_version": record.get("extractor_version"),
        "status": record.get("status"),
        "reject_reason": record.get("reject_reason") or "",
        "applies_to": _parse_json(record.get("applies_to"), {}),
        "qualifiers": _parse_json(record.get("qualifiers"), {}),
        "hedge_flags": _parse_json(record.get("hedge_flags"), []),
        "document": {
            "sha256": sha256,
            "is_held": has_snapshot,
            "url": record.get("url"),
            "file_name": source.get_file_name(record["url"]) if has_snapshot else None,
            "kind": record.get("document_kind"),
            "route": record.get("route"),
            "is_pdf": record.get("media_type") == document.PDF,
            "capture_date": record.get("capture_date"),
            "document_date": record.get("document_date"),
            "fetched_at": record.get("fetched_at"),
            # More than one when other addresses hold the same bytes.
            "address_count": record.get("address_count") or 0,
        },
        "evidence": build_evidence(record, derived),
    }


def build_evidence(record: Mapping[str, Any], derived: DerivedFiles) -> dict[str, Any]:
    """Where a claim's quote sits in its document, with enough around it to read it there.

    Never raises: what could not be shown is said in `problem`, beside whatever
    could be.
    """
    if record.get("locator") is None:
        return {"kind": "unknown", "problem": "the claim is no longer in the claims table"}
    locator = _parse_locator(record)
    if locator is None:
        return {"kind": "unknown", "problem": f"the locator cannot be read: {record['locator']}"}
    if not record.get("text_path") or not record.get("tables_path"):
        problem = "the pipeline holds no snapshot of this claim's document"
        return {**_describe_locator(locator), "problem": problem}
    quote = record.get("quote") or ""
    try:
        if isinstance(locator, TableCell):
            return build_cell_context(locator, quote, derived.read_tables(record["tables_path"]))
        if isinstance(locator, FileRow):
            path = derived.snapshot_dir / record["text_path"]
            row = find_file_rows(path, {locator.row_key})[locator.row_key]
            scope_columns = tuple(_parse_json(record.get("applies_to"), {}))
            return build_row_context(locator, row, scope_columns)
        return build_text_context(locator, quote, derived.read_text(record["text_path"]))
    except (OSError, ValueError) as error:
        problem = f"the snapshot's derived files cannot be read: {error}"
        return {**_describe_locator(locator), "problem": problem}


def build_cell_context(locator: TableCell, quote: str, tables: tuple[Table, ...]) -> dict[str, Any]:
    """The table a claim points into, with the located row and cell.

    `header_row_index` is 0 when the header is the table's own first row, and
    None when it was taken from the table this one continues.
    """
    context: dict[str, Any] = {**_describe_locator(locator), "problem": None, "rows": None}
    table = next(
        (held for held in tables if (held.page, held.index) == (locator.page, locator.table)),
        None,
    )
    if table is not None:
        context["rows"] = [list(row) for row in table.rows]
    found = evidence.find_cell(tables, locator)
    if isinstance(found, str) or table is None:
        context["problem"] = found if isinstance(found, str) else "the table is not there"
        return context
    span = evidence.find_quote(quote, found.text)
    context.update(
        header=list(found.header),
        header_row_index=0 if table.rows[0] == found.header else None,
        row_index=found.row_index,
        column_index=found.column_index,
        quote_span=[span.start, span.end] if span is not None else None,
    )
    if span is None:
        context["problem"] = "the quote is not in the located cell"
    return context


def build_text_context(locator: TextSpan | PageHint, quote: str, text: str) -> dict[str, Any]:
    """The text either side of a quoted span.

    A claim that was never admitted has only the page its extractor named, so
    the quote is searched for, as the verify stage would.
    """
    context: dict[str, Any] = {**_describe_locator(locator), "problem": None}
    if isinstance(locator, PageHint):
        found = evidence.find_quote(quote, text, page=locator.page)
        if found is None:
            context["problem"] = "the quote is not in the document's text"
            return context
        span = found
    else:
        span = locator
    start, end = max(0, span.start), min(len(text), span.end)
    context_start = max(0, start - CONTEXT_CHARACTERS)
    context_end = min(len(text), end + CONTEXT_CHARACTERS)
    quoted = text[start:end]
    context.update(
        start=start,
        end=end,
        page=text.count(document.PAGE_BREAK, 0, start) + 1,
        before=text[context_start:start],
        quoted=quoted,
        after=text[end:context_end],
        is_cut_before=context_start > 0,
        is_cut_after=context_end < len(text),
    )
    folded_quote = evidence.normalize_for_matching(quote)
    if not quoted or folded_quote not in evidence.normalize_for_matching(quoted):
        context["problem"] = "the text at the recorded span is not the quote"
    return context


def build_row_context(
    locator: FileRow, row: Mapping[str, str] | None, scope_columns: tuple[str, ...] = ()
) -> dict[str, Any]:
    """A data file's row: the located field, the fields that say which vehicle it is, and the rest.

    `fields` leads with the located column and the columns the claim's scope
    was read from. `other_fields` is every remaining column, in file order.
    """
    context: dict[str, Any] = {**_describe_locator(locator), "problem": None}
    if row is None:
        context["problem"] = f"no row has the key {locator.row_key!r}"
        return context
    leading = dict.fromkeys(
        name for name in (locator.column, *_ROW_IDENTITY_COLUMNS, *scope_columns) if name in row
    )
    context["fields"] = [[name, row[name]] for name in leading]
    context["other_fields"] = [[name, value] for name, value in row.items() if name not in leading]
    if locator.column not in row:
        context["problem"] = f"the file has no column {locator.column!r}"
    return context


def find_file_rows(text_path: Path, row_keys: Iterable[str]) -> dict[str, dict[str, str] | None]:
    """Rows of a data file by their `id`, read in one pass that stops at the last one found.

    The fuel economy file has fifty thousand rows, so a row once found is
    kept, under the file's modification time: a file derived again is read
    again.
    """
    modified = text_path.stat().st_mtime_ns
    name = str(text_path)
    missing = {key for key in row_keys if (name, modified, key) not in _FILE_ROWS}
    if missing:
        found: dict[str, dict[str, str]] = {}
        with text_path.open(encoding="utf-8", newline="") as file:
            reader = csv.reader(file)
            header = next(reader, [])
            key_column = header.index("id") if "id" in header else None
            if key_column is not None:
                for fields in reader:
                    if len(fields) > key_column and fields[key_column] in missing:
                        found[fields[key_column]] = dict(zip(header, fields, strict=False))
                        if len(found) == len(missing):
                            break
        if len(_FILE_ROWS) + len(missing) > _MAX_HELD_FILE_ROWS:
            _FILE_ROWS.clear()
        for key in missing:
            _FILE_ROWS[(name, modified, key)] = found.get(key)
    return {key: _FILE_ROWS[(name, modified, key)] for key in row_keys}


# --- documents, claims and checks --------------------------------------------


def load_documents(
    connection: duckdb.DuckDBPyConnection, settings: Settings, query: Mapping[str, str]
) -> dict[str, Any]:
    """Every candidate and every snapshot, with what became of it and what it yielded."""
    records = storage.fetch_records(
        connection,
        """
        SELECT coalesce(c.url, s.url) AS url, coalesce(c.kind, s.kind) AS kind,
            coalesce(c.source_id, s.source_id) AS source_id,
            coalesce(c.status, 'fetched') AS status, coalesce(c.detail, '') AS detail,
            c.url IS NOT NULL AS is_candidate, c.model_years, c.discovered_from, c.link_text,
            s.sha256, s.route, s.media_type, s.fetch_url, s.fetched_at, s.capture_date,
            s.document_date
        FROM candidates AS c
        FULL JOIN snapshots AS s ON s.url = c.url
        """,
    )
    claim_counts: dict[str, dict[str, int]] = defaultdict(dict)
    for record in storage.fetch_records(
        connection,
        """
        SELECT snapshot_sha256, status, count(*) AS claim_count
        FROM (
            SELECT snapshot_sha256, status FROM style_claims
            UNION ALL
            SELECT snapshot_sha256, status FROM value_claims
        )
        GROUP BY snapshot_sha256, status
        """,
    ):
        claim_counts[record["snapshot_sha256"]][record["status"]] = record["claim_count"]
    extractions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in storage.fetch_records(
        connection,
        "SELECT snapshot_sha256, extractor_version, status, detail FROM extractions "
        "ORDER BY extracted_at",
    ):
        extractions[record.pop("snapshot_sha256")].append(record)

    documents: list[dict[str, Any]] = []
    for record in records:
        sha256 = record["sha256"]
        documents.append(
            {
                "url": record["url"],
                "file_name": source.get_file_name(record["url"]),
                "kind": record["kind"],
                "source_id": record["source_id"],
                "status": record["status"],
                "detail": record["detail"],
                "is_candidate": record["is_candidate"],
                "model_years": _parse_json(record["model_years"], []),
                "discovered_from": record["discovered_from"],
                "link_text": record["link_text"],
                "snapshot": None
                if sha256 is None
                else {
                    "sha256": sha256,
                    "route": record["route"],
                    "is_pdf": record["media_type"] == document.PDF,
                    "fetch_url": record["fetch_url"],
                    "fetched_at": record["fetched_at"],
                    "capture_date": record["capture_date"],
                    "document_date": record["document_date"],
                },
                "claims": claim_counts.get(sha256, {}),
                "extractions": extractions.get(sha256, []),
            }
        )
    documents.sort(key=lambda held: (held["kind"], held["status"], held["url"]))
    counts = Counter((held["kind"], held["status"]) for held in documents)
    return {
        "inbox_dir": str(settings.inbox_dir.resolve()),
        "counts": [
            {"kind": kind, "status": status, "count": count}
            for (kind, status), count in sorted(counts.items())
        ],
        "documents": documents,
    }


def load_claims(
    connection: duckdb.DuckDBPyConnection, settings: Settings, query: Mapping[str, str]
) -> dict[str, Any]:
    """Claim counts by status and by reject reason, and the rejected claims themselves."""
    counts = storage.fetch_records(
        connection,
        """
        SELECT 'style' AS claim_type, status, count(*) AS claim_count
        FROM style_claims GROUP BY status
        UNION ALL
        SELECT 'value' AS claim_type, status, count(*) AS claim_count
        FROM value_claims GROUP BY status
        ORDER BY claim_type, status
        """,
    )
    reasons = storage.fetch_records(
        connection,
        """
        SELECT claim_type, reject_reason, count(*) AS claim_count
        FROM (
            SELECT 'style' AS claim_type, reject_reason FROM style_claims WHERE status = 'rejected'
            UNION ALL
            SELECT 'value' AS claim_type, reject_reason FROM value_claims WHERE status = 'rejected'
        )
        GROUP BY claim_type, reject_reason
        ORDER BY claim_count DESC, reject_reason, claim_type
        """,
    )
    rejected = storage.fetch_records(
        connection,
        f"""
        SELECT c.claim_type, c.claim_id, c.attribute, c.value_text, c.style_key, c.reject_reason,
            c.quote, c.extractor_version, c.snapshot_sha256, s.url, s.kind AS document_kind
        FROM (
            SELECT 'style' AS claim_type, claim_id, NULL AS attribute, description AS value_text,
                style_key, reject_reason, quote, extractor_version, snapshot_sha256
            FROM style_claims WHERE status = 'rejected'
            UNION ALL
            SELECT 'value' AS claim_type, claim_id, attribute, value_text, style_key,
                reject_reason, quote, extractor_version, snapshot_sha256
            FROM value_claims WHERE status = 'rejected'
        ) AS c
        LEFT JOIN {_SNAPSHOT_PER_HASH} AS s ON s.sha256 = c.snapshot_sha256
        ORDER BY c.reject_reason, s.url, c.claim_type, c.claim_id
        LIMIT ?
        """,
        [MAX_LISTED_CLAIMS],
    )
    for record in rejected:
        url = record["url"]
        record["file_name"] = source.get_file_name(url) if url else None
    rejected_count = sum(reason["claim_count"] for reason in reasons)
    return {
        "counts": counts,
        "reject_reasons": reasons,
        "rejected": rejected,
        "rejected_count": rejected_count,
        "is_cut": rejected_count > len(rejected),
    }


def load_checks(
    connection: duckdb.DuckDBPyConnection, settings: Settings, query: Mapping[str, str]
) -> dict[str, Any]:
    """The consistency findings by check, the disagreements, and the coverage."""
    findings = storage.fetch_records(
        connection,
        """
        SELECT f.check_name, f.style_key, f.attribute, f.detail, r.model_year, r.description
        FROM check_results AS f
        LEFT JOIN style_records AS r ON r.style_key = f.style_key
        ORDER BY f.check_name, r.model_year, r.description, f.style_key, f.detail
        """,
    )
    names = dict.fromkeys([*consistency.CHECKS, *(finding["check_name"] for finding in findings)])
    return {
        "checks": [
            {
                "check_name": name,
                "findings": [finding for finding in findings if finding["check_name"] == name],
            }
            for name in names
        ],
        "disagreements": storage.fetch_records(
            connection,
            "SELECT * FROM disagreements ORDER BY model_year, description, style_key, attribute",
        ),
        "coverage": storage.fetch_records(
            connection, "SELECT * FROM coverage ORDER BY model_year, attribute"
        ),
        "attributes": list(claim.ATTRIBUTES),
    }


# --- the diff's output -------------------------------------------------------


def load_diff(
    viewer: Viewer, run_id: str | None = None, *, is_showing_reference: bool = False
) -> dict[str, Any]:
    """One run of the diff: its feedback and, where the user unlocked them, reference values.

    Without `run_id` the run is the detail file's, when reference values are
    being shown, and otherwise the latest under `feedback/`. Reference values
    are attached only to the run the detail file describes.
    """
    feedback_dir = viewer.settings.feedback_dir
    runs, paths, unreadable = list_feedback_runs(feedback_dir)
    detail: dict[str, Any] | None = None
    if is_showing_reference and viewer.diff_detail_path is not None:
        detail = load_diff_detail(viewer.diff_detail_path)
        if detail["feedback"]["run_id"] not in paths:
            runs.insert(0, _summarize_run(detail["feedback"], viewer.diff_detail_path.name))
    answer: dict[str, Any] = {
        "feedback_dir": str(feedback_dir.resolve()),
        "runs": runs,
        "unreadable": unreadable,
        "run_id": None,
        "feedback": None,
        "dropped_keys": [],
        "reference": None,
        "reference_run_id": detail["feedback"]["run_id"] if detail is not None else None,
    }
    selected = run_id or answer["reference_run_id"] or (runs[0]["run_id"] if runs else None)
    if selected is None:
        return answer
    answer["run_id"] = selected
    if detail is not None and selected == detail["feedback"]["run_id"]:
        answer["feedback"] = detail["feedback"]
        answer["dropped_keys"] = detail["dropped_keys"]
        answer["reference"] = {
            "file_name": viewer.diff_detail_path.name if viewer.diff_detail_path else None,
            **{key: detail[key] for key in _DETAIL_KEYS},
        }
    elif selected in paths:
        answer["feedback"], answer["dropped_keys"] = load_feedback(paths[selected])
    else:
        raise Problem("not_found", f"There is no diff run {selected!r}.", 404)
    return answer


def list_feedback_runs(
    feedback_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Path], list[str]]:
    """The runs under `feedback/`, latest first, their files by run, and the unreadable files."""
    held: list[tuple[dict[str, Any], Path]] = []
    unreadable: list[str] = []
    for path in sorted(feedback_dir.glob("*.json")) if feedback_dir.is_dir() else []:
        try:
            feedback, _ = load_feedback(path)
        except Problem:
            unreadable.append(path.name)
            continue
        held.append((_summarize_run(feedback, path.name), path))
    held.sort(key=lambda pair: (pair[0]["created_at"] or "", pair[0]["run_id"]), reverse=True)
    runs = [summary for summary, _ in held]
    # Two files claiming one run: the listing keeps both, the first is served.
    paths: dict[str, Path] = {}
    for summary, path in held:
        paths.setdefault(summary["run_id"], path)
    return runs, paths, unreadable


def load_feedback(path: Path) -> tuple[dict[str, Any], list[str]]:
    """A feedback file as the contract describes it, and the names of what was left out."""
    return clean_feedback(_read_json(path), path.stem)


def load_diff_detail(path: Path) -> dict[str, Any]:
    """The detail file: the feedback, and the reference's side of every matched cell.

    This is the only function that opens the file, and it refuses in an agent
    session whoever calls it.
    """
    check_reference_access(os.environ)
    raw = _read_json(path)
    feedback, dropped = clean_feedback(raw, path.stem)
    detail: dict[str, Any] = {
        "feedback": feedback,
        "dropped_keys": [key for key in dropped if key not in _DETAIL_KEYS],
    }
    for key in _DETAIL_KEYS:
        detail[key] = [item for item in _as_list(raw.get(key)) if isinstance(item, dict)]
    return detail


def clean_feedback(raw: Any, fallback_run_id: str) -> tuple[dict[str, Any], list[str]]:
    """Keep what a feedback file may hold, and name what was dropped.

    A missing key becomes an empty value, so a file from an older diff still
    shows. A key the contract does not name is not served: were a detail file
    ever saved under `feedback/`, its reference values would stop here.
    """
    if not isinstance(raw, dict):
        raise Problem("bad_feedback", "The diff's output is not a JSON object.", 422)
    dropped = {str(key) for key in raw if key not in _FEEDBACK_KEYS}

    def keep_rows(name: str, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in _as_list(raw.get(name)):
            if not isinstance(item, dict):
                continue
            rows.append({key: _as_scalar(item.get(key)) for key in keys})
            dropped.update(f"{name}[].{key}" for key in item if key not in keys)
        return rows

    def keep_counts(name: str) -> dict[str, int | float]:
        held = raw.get(name)
        return {
            str(key): value
            for key, value in (held.items() if isinstance(held, dict) else ())
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }

    feedback = {
        "run_id": str(_as_scalar(raw.get("run_id")) or fallback_run_id),
        "created_at": _as_text(raw.get("created_at")),
        "split": _as_text(raw.get("split")),
        "git_sha": _as_text(raw.get("git_sha")),
        "model_years": [year for year in _as_list(raw.get("model_years")) if isinstance(year, int)],
        "tolerances": keep_counts("tolerances"),
        "row_matching": keep_counts("row_matching"),
        "buckets": keep_counts("buckets"),
        "by_reference_model": keep_rows("by_reference_model", _REFERENCE_MODEL_KEYS),
        "by_pipeline_model": keep_rows("by_pipeline_model", _PIPELINE_MODEL_KEYS),
        "metrics": keep_rows("metrics", _METRIC_KEYS),
        "cells": keep_rows("cells", _CELL_KEYS),
        "extra_styles": [key for key in _as_list(raw.get("extra_styles")) if isinstance(key, str)],
    }
    return feedback, sorted(dropped)


def _summarize_run(feedback: Mapping[str, Any], file_name: str) -> dict[str, Any]:
    return {
        "run_id": feedback["run_id"],
        "created_at": feedback["created_at"],
        "split": feedback["split"],
        "model_years": feedback["model_years"],
        "git_sha": feedback["git_sha"],
        "file_name": file_name,
    }


# --- small helpers -----------------------------------------------------------


def _open_derived_files(settings: Settings) -> DerivedFiles:
    return DerivedFiles(settings.snapshot_dir, {}, {})


def _require(query: Mapping[str, str], name: str) -> str:
    value = query.get(name, "")
    if not value:
        raise Problem("bad_request", f"The request needs {name}=.", 400)
    return value


def _parse_locator(record: Mapping[str, Any]) -> ClaimLocator | None:
    try:
        return claim.parse_locator(record["locator"])
    except KeyError, TypeError, ValueError:
        return None


def _describe_locator(locator: ClaimLocator) -> dict[str, Any]:
    kinds: dict[type, str] = {
        TextSpan: "text_span",
        TableCell: "table_cell",
        FileRow: "file_row",
        PageHint: "page_hint",
    }
    return {"kind": kinds[type(locator)], **asdict(locator)}


def _parse_json(text: Any, default: Any) -> Any:
    if not isinstance(text, str):
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Problem("bad_feedback", f"{path.name} cannot be read: {error}", 422) from error


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_scalar(value: Any) -> str | int | float | bool | None:
    return value if isinstance(value, (str, int, float, bool)) else None


def _encode_value(value: Any) -> Any:
    """What `json` cannot write by itself: the dates DuckDB returns."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON")


_DATABASE_ROUTES: dict[
    str, Callable[[duckdb.DuckDBPyConnection, Settings, Mapping[str, str]], dict[str, Any]]
] = {
    "catalogue": load_catalogue,
    "style": load_style,
    "cell": load_cell,
    "claim": load_claim,
    "documents": load_documents,
    "claims": load_claims,
    "checks": load_checks,
}
