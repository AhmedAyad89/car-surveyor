"""Claims: what a document says, with where it says it.

A style claim says a priced configuration exists. A value claim states one
figure for one attribute, either for a single style or for every style its
scope describes (a horsepower figure given for four grades, a fuel economy row
that covers a group of grades). Attaching a scoped value to styles is the
resolve stage's work; a claim records only what its document printed.

A claim starts as `extracted`. The verify stage admits it or rejects it with
a reason, and nothing else changes a claim.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal

import duckdb

from car_surveyor import storage
from car_surveyor.evidence import FileRow, TableCell, TextSpan

MSRP = "msrp"
DELIVERY_FEE = "delivery_fee"
HORSEPOWER = "horsepower"
TORQUE = "torque"
MPG_CITY = "mpg_city"
MPG_HIGHWAY = "mpg_highway"
MPG_COMBINED = "mpg_combined"
ATTRIBUTES = (MSRP, DELIVERY_FEE, HORSEPOWER, TORQUE, MPG_CITY, MPG_HIGHWAY, MPG_COMBINED)

# Who states the value: Toyota's pricing documents, Toyota's specification
# documents, or the EPA's fuel economy file.
OEM_PRICING = "oem_pricing"
OEM_SPECIFICATION = "oem_specification"
EPA = "epa"
# A claim an adjudicator recorded for one cell after re-reading a primary
# document. It is verified like any claim, is never attached to a style by
# the resolve stage, and is what the diff sets both sides against.
ADJUDICATION = "adjudication"

ClaimStatus = Literal["extracted", "admitted", "rejected"]

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class PageHint:
    """Where an extractor says a prose quote is, before code has found the span."""

    page: int | None


type ClaimLocator = TextSpan | TableCell | FileRow | PageHint


@dataclass(frozen=True, slots=True)
class StyleClaim:
    claim_id: str
    style_key: str
    model_year: int
    model: str
    model_code: str | None
    grade: str | None
    powertrain: str | None
    body: str | None
    engine: str | None
    transmission: str | None
    drive: str | None
    description: str
    snapshot_sha256: str
    locator: ClaimLocator
    quote: str
    extractor_version: str
    status: ClaimStatus = "extracted"
    reject_reason: str = ""


@dataclass(frozen=True, slots=True)
class ValueClaim:
    claim_id: str
    # Set when the document states the value for one style, as a price row does.
    style_key: str | None
    # Otherwise: what the document says the value applies to, as printed.
    applies_to: dict[str, Any]
    attribute: str
    value_text: str
    value_num: float | None
    unit: str
    snapshot_sha256: str
    locator: ClaimLocator
    quote: str
    extractor_version: str
    source_class: str
    qualifiers: dict[str, str] = field(default_factory=dict)
    hedge_flags: tuple[str, ...] = ()
    effective_date: date | None = None
    status: ClaimStatus = "extracted"
    reject_reason: str = ""


def build_claim_id(snapshot_sha256: str, extractor_version: str, kind: str, ordinal: int) -> str:
    """A stable identifier: the same extraction of the same snapshot gives the same ids."""
    seed = f"{snapshot_sha256}|{extractor_version}|{kind}|{ordinal}"
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def slugify(text: str) -> str:
    return _SLUG_PATTERN.sub("-", text.lower()).strip("-")


def build_style_key(
    model_year: int,
    *,
    model_code: str | None,
    is_code_unique: bool,
    model: str,
    grade: str | None = None,
    powertrain: str | None = None,
    body: str | None = None,
    engine: str | None = None,
    transmission: str | None = None,
    drive: str | None = None,
) -> str:
    """The key that makes two documents' rows the same style.

    Toyota's model code identifies a style within a model year, so it is the
    key wherever a document prints one. A row with no code, or with a code the
    same document also prints on another row, is keyed on what it says about
    itself. Transmission is part of that, because manual and automatic
    versions are priced separately.
    """
    if model_code and is_code_unique:
        return f"{model_year}-{slugify(model_code)}"
    parts = [model, grade, powertrain, body, engine, transmission, drive]
    described = "-".join(slugify(part) for part in parts if part and slugify(part))
    return f"{model_year}-{described}"


def serialize_locator(locator: ClaimLocator) -> str:
    kinds: dict[type, str] = {
        TextSpan: "text_span",
        TableCell: "table_cell",
        FileRow: "file_row",
        PageHint: "page_hint",
    }
    return json.dumps({"kind": kinds[type(locator)], **asdict(locator)})


def parse_locator(serialized: str) -> ClaimLocator:
    fields = json.loads(serialized)
    kind = fields.pop("kind")
    if kind == "text_span":
        return TextSpan(**fields)
    if kind == "table_cell":
        return TableCell(**fields)
    if kind == "file_row":
        return FileRow(**fields)
    if kind == "page_hint":
        return PageHint(**fields)
    raise ValueError(f"unknown locator kind {kind!r}")


def store_style_claims(connection: duckdb.DuckDBPyConnection, claims: list[StyleClaim]) -> None:
    if not claims:
        return
    connection.executemany(
        "INSERT INTO style_claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            [
                claim.claim_id,
                claim.style_key,
                claim.model_year,
                claim.model,
                claim.model_code,
                claim.grade,
                claim.powertrain,
                claim.body,
                claim.engine,
                claim.transmission,
                claim.drive,
                claim.description,
                claim.snapshot_sha256,
                serialize_locator(claim.locator),
                claim.quote,
                claim.extractor_version,
                claim.status,
                claim.reject_reason,
            ]
            for claim in claims
        ],
    )


def store_value_claims(connection: duckdb.DuckDBPyConnection, claims: list[ValueClaim]) -> None:
    if not claims:
        return
    connection.executemany(
        "INSERT INTO value_claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            [
                claim.claim_id,
                claim.style_key,
                json.dumps(claim.applies_to, sort_keys=True),
                claim.attribute,
                claim.value_text,
                claim.value_num,
                claim.unit,
                json.dumps(claim.qualifiers, sort_keys=True),
                json.dumps(list(claim.hedge_flags)),
                claim.effective_date,
                claim.source_class,
                claim.snapshot_sha256,
                serialize_locator(claim.locator),
                claim.quote,
                claim.extractor_version,
                claim.status,
                claim.reject_reason,
            ]
            for claim in claims
        ],
    )


def list_style_claims(
    connection: duckdb.DuckDBPyConnection,
    *,
    status: ClaimStatus | None = None,
    snapshot_sha256: str | None = None,
) -> list[StyleClaim]:
    records = _select_claims(connection, "style_claims", status, snapshot_sha256)
    return [
        StyleClaim(
            claim_id=record["claim_id"],
            style_key=record["style_key"],
            model_year=record["model_year"],
            model=record["model"],
            model_code=record["model_code"],
            grade=record["grade"],
            powertrain=record["powertrain"],
            body=record["body"],
            engine=record["engine"],
            transmission=record["transmission"],
            drive=record["drive"],
            description=record["description"],
            snapshot_sha256=record["snapshot_sha256"],
            locator=parse_locator(record["locator"]),
            quote=record["quote"],
            extractor_version=record["extractor_version"],
            status=record["status"],
            reject_reason=record["reject_reason"],
        )
        for record in records
    ]


def list_value_claims(
    connection: duckdb.DuckDBPyConnection,
    *,
    status: ClaimStatus | None = None,
    snapshot_sha256: str | None = None,
) -> list[ValueClaim]:
    records = _select_claims(connection, "value_claims", status, snapshot_sha256)
    return [
        ValueClaim(
            claim_id=record["claim_id"],
            style_key=record["style_key"],
            applies_to=json.loads(record["applies_to"]),
            attribute=record["attribute"],
            value_text=record["value_text"],
            value_num=record["value_num"],
            unit=record["unit"],
            qualifiers=json.loads(record["qualifiers"]),
            hedge_flags=tuple(json.loads(record["hedge_flags"])),
            effective_date=record["effective_date"],
            source_class=record["source_class"],
            snapshot_sha256=record["snapshot_sha256"],
            locator=parse_locator(record["locator"]),
            quote=record["quote"],
            extractor_version=record["extractor_version"],
            status=record["status"],
            reject_reason=record["reject_reason"],
        )
        for record in records
    ]


def delete_claims(connection: duckdb.DuckDBPyConnection, snapshot_sha256: str) -> None:
    """Drop a snapshot's claims, before a newer extraction of it replaces them."""
    for table in ("style_claims", "value_claims"):
        connection.execute(f"DELETE FROM {table} WHERE snapshot_sha256 = ?", [snapshot_sha256])


def settle_claim(
    connection: duckdb.DuckDBPyConnection,
    table: Literal["style_claims", "value_claims"],
    claim_id: str,
    status: ClaimStatus,
    *,
    reject_reason: str = "",
    locator: ClaimLocator | None = None,
) -> None:
    """Record the verify stage's decision, and the location it found if any."""
    connection.execute(
        f"UPDATE {table} SET status = ?, reject_reason = ? WHERE claim_id = ?",
        [status, reject_reason, claim_id],
    )
    if locator is not None:
        connection.execute(
            f"UPDATE {table} SET locator = ? WHERE claim_id = ?",
            [serialize_locator(locator), claim_id],
        )


def _select_claims(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    status: ClaimStatus | None,
    snapshot_sha256: str | None,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    parameters: list[Any] = []
    if status is not None:
        conditions.append("status = ?")
        parameters.append(status)
    if snapshot_sha256 is not None:
        conditions.append("snapshot_sha256 = ?")
        parameters.append(snapshot_sha256)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return storage.fetch_records(
        connection, f"SELECT * FROM {table}{where} ORDER BY rowid", parameters
    )
