"""The stages run together on a small synthetic document set. No network, no model.

This is the S1 gate in miniature: one model from documents to a row per style,
and a second run that changes nothing and makes no model calls.
"""

from __future__ import annotations

from typing import Any

import duckdb
from conftest import PdfBuilder
from test_extraction import (
    PRICING_ANSWER,
    PRICING_TABLE,
    PRICING_TEXT,
    SPECIFICATION_ANSWER,
    SPECIFICATION_TABLE,
    FakeExtractor,
)

from car_surveyor import (
    claim,
    consistency,
    extraction,
    resolution,
    snapshot,
    source,
    storage,
    verification,
)
from car_surveyor.config import Settings
from car_surveyor.extraction import ExtractionRequest, ExtractorResponse
from car_surveyor.source import Registry


class ExtractorByKind(FakeExtractor):
    """Answers a pricing document with the pricing answer and any other with the other."""

    def __init__(self) -> None:
        super().__init__(PRICING_ANSWER)

    def extract(self, request: ExtractionRequest) -> ExtractorResponse:
        self._answer = PRICING_ANSWER if request.kind == source.PRICING_PDF else SPECIFICATION
        return super().extract(request)


# The synthetic specification document says its figure is for the LE and SE.
SPECIFICATION: dict[str, Any] = {
    **SPECIFICATION_ANSWER,
    "values": SPECIFICATION_ANSWER["values"][:1],
}


def _store(
    connection: duckdb.DuckDBPyConnection, settings: Settings, kind: str, raw: bytes
) -> None:
    url = f"https://toyota-cms-media.s3.amazonaws.com/{kind}.pdf"
    snapshot.store_snapshot(
        connection,
        settings,
        url=url,
        source_id="toyota_media",
        kind=kind,
        raw=raw,
        media_type="application/pdf",
        fetch_url=url,
        route=snapshot.WAYBACK,
    )


def _run_stages(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    registry: Registry,
    extractor: FakeExtractor,
) -> tuple[int, int]:
    extraction.extract_fuel_economy(connection, settings, registry)
    extraction.extract_documents(connection, settings, extractor)
    verified = verification.verify_claims(connection, settings, registry)
    resolution.resolve(connection)
    consistency.run_checks(connection)
    return verified.admitted, verified.rejected


def _dump(connection: duckdb.DuckDBPyConnection) -> dict[str, list[dict[str, Any]]]:
    tables = ("style_claims", "value_claims", "style_records", "style_values", "check_results")
    return {
        table: storage.fetch_records(connection, f"SELECT * FROM {table} ORDER BY ALL")
        for table in tables
    }


def test_documents_become_one_row_per_style_and_a_second_run_changes_nothing(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    registry: Registry,
    build_pdf: PdfBuilder,
) -> None:
    _store(connection, settings, source.PRICING_PDF, build_pdf([PRICING_TEXT], table=PRICING_TABLE))
    title = ["2023 Example Product Information"]
    specification = build_pdf([title], table=SPECIFICATION_TABLE)
    _store(connection, settings, source.PRODUCT_INFORMATION_PDF, specification)
    extractor = ExtractorByKind()

    admitted, rejected = _run_stages(connection, settings, registry, extractor)

    assert (admitted, rejected) == (12, 0)
    assert len(extractor.requests) == 2
    rows = storage.fetch_records(
        connection,
        "SELECT style_key, description, msrp, msrp_date, delivery_fee, horsepower, drive_class "
        "FROM styles ORDER BY msrp",
    )
    assert [(row["description"], row["msrp"], row["horsepower"]) for row in rows] == [
        ("Example LE 4-cyl", 28495, 203),
        ("Example LE AWD 4-cyl", 29895, 203),
        ("Example 4x2 Limited", 35900, None),
        ("Example 4x2 Platinum", 38400, None),
        ("eX4 XLE FWD", 42000, None),
    ]
    assert [row["delivery_fee"] for row in rows] == [1095, 1095, 1095, 1095, 1095]
    assert rows[0]["drive_class"] == "2wd"
    assert {finding["check_name"] for finding in _dump(connection)["check_results"]} == {
        "repeated_model_code"
    }
    every_cell = storage.fetch_records(
        connection,
        "SELECT v.claim_id FROM style_values AS v "
        "LEFT JOIN value_claims AS c USING (claim_id) WHERE c.claim_id IS NULL",
    )
    assert every_cell == []

    first = _dump(connection)
    assert _run_stages(connection, settings, registry, extractor) == (0, 0)
    assert len(extractor.requests) == 2
    assert _dump(connection) == first


def test_a_claim_the_document_does_not_support_never_reaches_a_style(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    registry: Registry,
    build_pdf: PdfBuilder,
) -> None:
    """A price with two digits swapped is rejected, and its style has no price."""
    _store(connection, settings, source.PRICING_PDF, build_pdf([PRICING_TEXT], table=PRICING_TABLE))
    rows = [dict(row) for row in PRICING_ANSWER["rows"]]
    rows[0]["msrp"] = "$28,945"
    extractor = FakeExtractor({**PRICING_ANSWER, "rows": rows})

    admitted, rejected = _run_stages(connection, settings, registry, extractor)

    assert rejected == 1
    (wrong,) = claim.list_value_claims(connection, status="rejected")
    assert (wrong.value_num, wrong.reject_reason) == (28945.0, "quote absent from the located cell")
    (row,) = storage.fetch_records(
        connection, "SELECT msrp, delivery_fee FROM styles WHERE style_key = '2023-9101'"
    )
    assert (row["msrp"], row["delivery_fee"]) == (None, 1095)
