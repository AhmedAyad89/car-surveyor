"""Tests for the development viewer. A small synthetic database, no network.

Requests are answered in-process by `viewer.respond`. One test binds an
ephemeral port on localhost, to see the HTTP server itself answer. Every
model, figure and diff file here is made up: the "reference" values are
numbers chosen to be found again if one leaks, and come from no reference.
"""

from __future__ import annotations

import http.client
import io
import json
import re
import subprocess
import sys
import threading
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
from conftest import PdfBuilder
from test_extraction import (
    PRICING_ANSWER,
    PRICING_TABLE,
    PRICING_TEXT,
    SPECIFICATION_TABLE,
    FakeExtractor,
)
from test_pipeline import SPECIFICATION

from car_surveyor import (
    cli,
    consistency,
    diff,
    extraction,
    resolution,
    snapshot,
    source,
    storage,
    verification,
    viewer,
)
from car_surveyor.claim import PageHint
from car_surveyor.config import Settings
from car_surveyor.document import Table
from car_surveyor.evidence import TableCell
from car_surveyor.extraction import ExtractionRequest, ExtractorResponse
from car_surveyor.reference import ReferenceStyle
from car_surveyor.source import Candidate, Registry
from car_surveyor.viewer import Viewer

_GASOLINE = "2.5,4,Regular Gasoline,,SIDI,Midsize Cars"
# Two rows that name the front-drive Example and disagree, and one for the AWD grades.
FUEL_ECONOMY_CSV = "\n".join(
    [
        "id,make,model,year,city08,highway08,comb08,trany,drive,"
        "displ,cylinders,fuelType1,atvType,eng_dscr,VClass,baseModel",
        f"46001,Toyota,Example,2023,32,41,35,Automatic (S8),Front-Wheel Drive,{_GASOLINE},Example",
        f"46002,Toyota,Example,2023,31,40,34,Automatic (S8),Front-Wheel Drive,{_GASOLINE},Example",
        "46003,Toyota,Example AWD LE/SE,2023,25,34,29,Automatic (S8),All-Wheel Drive,"
        f"{_GASOLINE},Example",
        "",
    ]
)
UNAVAILABLE_ADDRESS = "https://toyota-cms-media.s3.amazonaws.com/2023-Example-Product-Info.pdf"

# A number no document in these tests prints, so a leak is found by searching for it.
REFERENCE_MSRP = 987654
FEEDBACK: dict[str, Any] = {
    "run_id": "20261116T101233Z-dev",
    "created_at": "2026-11-16T10:12:33Z",
    "split": "dev",
    "model_years": [2023],
    "git_sha": "abc1234",
    "tolerances": {"msrp": 0, "horsepower": 5},
    "row_matching": {
        "reference_rows": 4,
        "matched_by_code": 2,
        "matched_by_description": 1,
        "missed": 1,
        "pipeline_styles": 5,
        "extra": 2,
    },
    "by_reference_model": [
        {"model_year": 2023, "model": "example", "rows": 4, "matched": 3, "missed": 1}
    ],
    "by_pipeline_model": [
        {"model_year": 2023, "model": "Example", "styles": 4, "matched": 3, "extra": 1}
    ],
    "metrics": [
        {
            "model_year": 2023,
            "attribute": "msrp",
            "agree": 0,
            "disagree": 1,
            "pipeline_empty": 1,
            "gap_filled": 0,
        }
    ],
    "buckets": {"confirmed": 1, "open": 1, "pipeline_miss": 1, "gap_filled": 1},
    "cells": [
        {"style_key": "2023-9101", "attribute": "msrp", "status": "disagree", "bucket": "open"},
        {
            "style_key": "2023-9101",
            "attribute": "horsepower",
            "status": "agree",
            "bucket": "confirmed",
        },
        {
            "style_key": "2023-9102",
            "attribute": "msrp",
            "status": "pipeline_empty",
            "bucket": "pipeline_miss",
        },
        {"style_key": "2023-9102", "attribute": "mpg_city", "status": None, "bucket": "gap_filled"},
    ],
    "extra_styles": ["2023-ex4-xle-fwd"],
}
DETAIL: dict[str, Any] = {
    **FEEDBACK,
    # One style matched twice, as the diff does for a model year's file and its mid-year file.
    "matches": [
        {
            "style_key": "2023-9101",
            "description": "Example LE 4-cyl",
            "matched_by": "code",
            "score": 1.0,
            "has_other_code": False,
            "reference": {
                "file": "example/2023.yaml",
                "model": "example",
                "trim": "le",
                "trim_name": "LE",
                "configuration": "fwd",
                "codes": ["9101"],
            },
            "cells": [
                {
                    "attribute": "msrp",
                    "status": "disagree",
                    "bucket": "open",
                    "pipeline_value": 28495,
                    "pipeline_values": [28495],
                    "reference_value": REFERENCE_MSRP,
                }
            ],
        },
        {
            "style_key": "2023-9101",
            "description": "Example LE 4-cyl",
            "matched_by": "description",
            "score": 0.75,
            "has_other_code": True,
            "reference": {
                "file": "example/2023.5.yaml",
                "model": "example",
                "trim": "le",
                "trim_name": "LE",
                "configuration": "fwd",
                "codes": ["9111"],
            },
            "cells": [
                {
                    "attribute": "msrp",
                    "status": "disagree",
                    "bucket": "open",
                    "pipeline_value": 28495,
                    "pipeline_values": [28495],
                    "reference_value": REFERENCE_MSRP + 1,
                }
            ],
        },
    ],
    "missed": [
        {
            "file": "example/2023.yaml",
            "model_year": 2023,
            "model": "example",
            "trim": "sport",
            "trim_name": "Sport",
            "configuration": "awd",
            "codes": ["9199"],
            "labels": {"drivetrain": "AWD"},
            "msrp": REFERENCE_MSRP,
        }
    ],
    "extra": [{"style_key": "2023-ex4-xle-fwd", "description": "eX4 XLE FWD"}],
}
EVERY_ENDPOINT = (
    "/api/meta",
    "/api/catalogue",
    "/api/documents",
    "/api/claims",
    "/api/checks",
    "/api/diff",
    "/api/style?style_key=2023-9101",
    "/api/cell?style_key=2023-9101&attribute=msrp",
)
# Run by another interpreter, so the lock on the file is another process's.
_HOLD_DATABASE = (
    "import sys, duckdb\n"
    "writer = duckdb.connect(sys.argv[1])\n"
    "print('holding', flush=True)\n"
    "sys.stdin.read()\n"
)


class ExtractorByKind(FakeExtractor):
    """Answers a pricing document with the given answer and any other with the specification's."""

    def __init__(self, pricing_answer: dict[str, Any]) -> None:
        super().__init__(pricing_answer)
        self._pricing_answer = pricing_answer

    def extract(self, request: ExtractionRequest) -> ExtractorResponse:
        is_pricing = request.kind == source.PRICING_PDF
        self._answer = self._pricing_answer if is_pricing else SPECIFICATION
        return super().extract(request)


@pytest.fixture
def data_viewer(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    registry: Registry,
    build_pdf: PdfBuilder,
) -> Viewer:
    """A viewer over five styles from three documents, with every kind of cell and locator.

    The price of the AWD style has two digits swapped, so that claim is
    rejected and its cell is unstated. The connection is closed before the
    viewer is used: it opens the file itself, read-only.
    """
    documents = {
        source.PRICING_PDF: build_pdf([PRICING_TEXT], table=PRICING_TABLE),
        source.PRODUCT_INFORMATION_PDF: build_pdf(
            [["2023 Example Product Information"]], table=SPECIFICATION_TABLE
        ),
    }
    for kind, raw in documents.items():
        url = f"https://toyota-cms-media.s3.amazonaws.com/{kind}.pdf"
        discovered_from = "https://pressroom.toyota.com/vehicle/2023-toyota-example/"
        source.record_candidate(
            connection, Candidate(url, "toyota_media", kind, (2023,), discovered_from)
        )
        source.mark_candidate(connection, url, "fetched")
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
    source.record_candidate(
        connection,
        Candidate(
            UNAVAILABLE_ADDRESS,
            "toyota_media",
            source.PRODUCT_INFORMATION_PDF,
            (2023,),
            "https://pressroom.toyota.com/vehicle/2023-toyota-example/",
        ),
    )
    source.mark_candidate(connection, UNAVAILABLE_ADDRESS, "unavailable", "no Wayback capture")
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("vehicles.csv", FUEL_ECONOMY_CSV)
    url = "https://www.fueleconomy.gov/feg/epadata/vehicles.csv.zip"
    snapshot.store_snapshot(
        connection,
        settings,
        url=url,
        source_id="fueleconomy",
        kind=source.FUEL_ECONOMY_FILE,
        raw=archive_bytes.getvalue(),
        media_type="application/zip",
        fetch_url=url,
        route=snapshot.DIRECT,
    )
    rows = [dict(row) for row in PRICING_ANSWER["rows"]]
    rows[1]["msrp"] = "$29,985"
    extraction.extract_fuel_economy(connection, settings, registry)
    extraction.extract_documents(
        connection, settings, ExtractorByKind({**PRICING_ANSWER, "rows": rows})
    )
    verification.verify_claims(connection, settings, registry)
    resolution.resolve(connection)
    consistency.run_checks(connection)
    connection.close()
    return Viewer(settings)


@pytest.fixture
def detail_path(tmp_path: Path) -> Path:
    path = tmp_path / "elsewhere" / "detail.json"
    path.parent.mkdir()
    path.write_text(json.dumps(DETAIL), encoding="utf-8")
    return path


def _get(data_viewer: Viewer, target: str) -> tuple[int, Any]:
    response = viewer.respond(data_viewer, target)
    return response.status, json.loads(response.body)


def _get_cell(data_viewer: Viewer, style_key: str, attribute: str) -> dict[str, Any]:
    status, cell = _get(data_viewer, f"/api/cell?style_key={style_key}&attribute={attribute}")
    assert status == 200
    return cell


def _write_feedback(settings: Settings, feedback: dict[str, Any], name: str = "") -> Path:
    directory = settings.feedback_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name or feedback['run_id']}.json"
    path.write_text(json.dumps(feedback), encoding="utf-8")
    return path


# --- the catalogue -----------------------------------------------------------


def test_catalogue_marks_each_cell_chosen_open_or_unstated(data_viewer: Viewer) -> None:
    status, catalogue = _get(data_viewer, "/api/catalogue")

    assert status == 200
    assert catalogue["model_years"] == [2023]
    styles = {style["style_key"]: style for style in catalogue["styles"]}
    assert len(styles) == 5
    front, awd = styles["2023-9101"], styles["2023-9102"]
    assert front["cells"]["msrp"] == {
        "state": "chosen",
        "value": 28495.0,
        "unit": "USD",
        "date": "2022-12-22",
        "claim_id": front["cells"]["msrp"]["claim_id"],
        "is_contested": False,
        "claim_count": 1,
    }
    assert front["cells"]["mpg_city"] == {
        "state": "open",
        "values": [31.0, 32.0],
        "unit": "MPG",
        "claim_count": 2,
    }
    assert front["cells"]["torque"] == {"state": "unstated"}
    assert awd["cells"]["msrp"] == {"state": "unstated"}
    assert awd["cells"]["mpg_city"]["value"] == 25.0


def test_catalogue_says_which_drive_classes_are_inferred(data_viewer: Viewer) -> None:
    _, catalogue = _get(data_viewer, "/api/catalogue")
    styles = {style["style_key"]: style for style in catalogue["styles"]}

    assert (styles["2023-9101"]["drive_class"], styles["2023-9101"]["is_drive_inferred"]) == (
        "2wd",
        True,
    )
    assert (styles["2023-9102"]["drive_class"], styles["2023-9102"]["is_drive_inferred"]) == (
        "4wd",
        False,
    )
    assert styles["2023-example-limited"]["finding_count"] == 1


# --- one cell and its evidence -----------------------------------------------


def test_a_table_cell_claim_is_shown_in_its_table(data_viewer: Viewer) -> None:
    cell = _get_cell(data_viewer, "2023-9101", "msrp")

    assert cell["cell"]["state"] == "chosen"
    (value,) = cell["values"]
    assert (value["is_chosen"], value["source_class"], value["specificity"]) == (
        True,
        "oem_pricing",
        0,
    )
    assert (value["value_num"], value["value_text"], value["unit"]) == (28495.0, "$28,495", "USD")
    assert value["effective_date"] == "2022-12-22"
    held_claim = value["claim"]
    assert held_claim["quote"] == "$28,495"
    assert held_claim["extractor_version"] == "fake-model+pricing-v1"
    located = held_claim["evidence"]
    assert located["kind"] == "table_cell"
    assert located["problem"] is None
    assert located["header"] == ["MODEL #", "MY", "MODEL", "MSRP"]
    assert located["header_row_index"] == 0
    assert located["rows"][located["row_index"]] == ["9101", "2023", "Example LE 4-cyl", "$28,495"]
    assert located["column_index"] == 3
    assert located["quote_span"] == [0, 7]
    assert held_claim["document"]["file_name"] == "pricing_pdf.pdf"
    assert held_claim["document"]["is_pdf"] is True
    assert held_claim["document"]["document_date"] == "2022-12-22"


def test_a_text_span_claim_is_shown_in_the_text_around_it(data_viewer: Viewer) -> None:
    (value,) = _get_cell(data_viewer, "2023-9101", "delivery_fee")["values"]

    located = value["claim"]["evidence"]
    assert located["kind"] == "text_span"
    assert located["problem"] is None
    assert located["quoted"] == "$1,095 for Passenger Cars (Example, eX4)"
    assert located["before"].endswith("processing and handling fee.\n")
    assert located["after"].startswith("\nLast updated 12/22/2022")
    assert located["page"] == 1
    assert (located["is_cut_before"], located["is_cut_after"]) == (False, False)
    assert value["claim"]["applies_to"]["models"] == ["Example", "eX4"]


def test_a_file_row_claim_is_shown_with_the_fields_of_its_row(data_viewer: Viewer) -> None:
    (value,) = _get_cell(data_viewer, "2023-9102", "mpg_city")["values"]

    located = value["claim"]["evidence"]
    assert located["kind"] == "file_row"
    assert located["problem"] is None
    assert (located["row_key"], located["column"]) == ("46003", "city08")
    assert located["fields"][:5] == [
        ["city08", "25"],
        ["id", "46003"],
        ["make", "Toyota"],
        ["year", "2023"],
        ["model", "Example AWD LE/SE"],
    ]
    assert ["drive", "All-Wheel Drive"] in located["fields"]
    assert ["highway08", "34"] in located["other_fields"]
    assert value["claim"]["document"]["is_pdf"] is False


def test_an_open_cell_lists_the_claims_that_disagree(data_viewer: Viewer) -> None:
    cell = _get_cell(data_viewer, "2023-9101", "mpg_city")

    assert cell["cell"] == {
        "state": "open",
        "values": [31.0, 32.0],
        "unit": "MPG",
        "claim_count": 2,
    }
    assert [value["value_num"] for value in cell["values"]] == [31.0, 32.0]
    assert all(value["is_conflicting"] and not value["is_chosen"] for value in cell["values"])
    assert [value["claim"]["evidence"]["row_key"] for value in cell["values"]] == ["46002", "46001"]


def test_an_unstated_cell_has_no_values_and_is_not_filled_in(data_viewer: Viewer) -> None:
    cell = _get_cell(data_viewer, "2023-9102", "msrp")

    assert cell["cell"] == {"state": "unstated"}
    assert cell["values"] == []


def test_a_cell_lists_the_findings_for_its_style(data_viewer: Viewer) -> None:
    cell = _get_cell(data_viewer, "2023-example-limited", "msrp")

    assert [finding["check_name"] for finding in cell["findings"]] == ["repeated_model_code"]


def test_a_style_shows_the_row_that_says_it_exists(data_viewer: Viewer) -> None:
    status, style = _get(data_viewer, "/api/style?style_key=2023-9101")

    assert status == 200
    assert style["style"]["description"] == "Example LE 4-cyl"
    (row,) = style["claims"]
    assert (row["claim_type"], row["status"], row["quote"]) == (
        "style",
        "admitted",
        "Example LE 4-cyl",
    )
    assert row["evidence"]["rows"][row["evidence"]["row_index"]][2] == "Example LE 4-cyl"


def test_a_request_for_what_does_not_exist_is_answered_not_raised(data_viewer: Viewer) -> None:
    assert _get(data_viewer, "/api/cell?style_key=2023-nothing&attribute=msrp")[0] == 404
    assert _get(data_viewer, "/api/cell?style_key=2023-9101&attribute=colour")[0] == 400
    assert _get(data_viewer, "/api/cell?style_key=2023-9101")[0] == 400
    assert _get(data_viewer, "/api/claim?claim_id=nothing")[0] == 404
    assert _get(data_viewer, "/api/nothing")[0] == 404
    assert _get(data_viewer, "/nothing")[0] == 404


def test_a_continued_table_is_shown_under_the_header_it_continues() -> None:
    tables = (
        Table(1, 0, (("MODEL #", "MODEL", "MSRP"), ("9101", "Example LE", "$28,495"))),
        Table(2, 0, (("9105", "Example XLE", "$31,000"), ("9106", "Example XSE", "$32,500"))),
    )

    located = viewer.build_cell_context(TableCell(2, 0, "9106", "MSRP"), "$32,500", tables)

    assert located["problem"] is None
    assert located["header"] == ["MODEL #", "MODEL", "MSRP"]
    assert located["header_row_index"] is None
    assert (located["row_index"], located["column_index"]) == (1, 2)


def test_a_cell_that_cannot_be_located_says_why_and_still_shows_the_table() -> None:
    tables = (Table(1, 0, (("MODEL #", "MSRP"), ("9101", "$28,495"))),)

    located = viewer.build_cell_context(TableCell(1, 0, "9199", "MSRP"), "$1", tables)

    assert located["problem"] == "no row is labelled '9199'"
    assert located["rows"] == [["MODEL #", "MSRP"], ["9101", "$28,495"]]
    assert "row_index" not in located


def test_a_page_hint_is_shown_where_the_quote_is_found() -> None:
    text = "first page\fThe Example makes 203 hp at the crank.\nmore"

    located = viewer.build_text_context(PageHint(2), "203 hp", text)

    assert (located["quoted"], located["page"], located["problem"]) == ("203 hp", 2, None)
    assert located["before"].endswith("The Example makes ")
    missing = viewer.build_text_context(PageHint(2), "999 hp", text)
    assert missing["problem"] == "the quote is not in the document's text"
    assert "quoted" not in missing


# --- documents, claims and checks --------------------------------------------


def test_documents_list_what_is_unavailable_with_the_name_to_save_it_under(
    data_viewer: Viewer,
) -> None:
    status, answer = _get(data_viewer, "/api/documents")

    assert status == 200
    assert answer["inbox_dir"] == str(data_viewer.settings.inbox_dir.resolve())
    documents = {entry["url"]: entry for entry in answer["documents"]}
    unavailable = documents[UNAVAILABLE_ADDRESS]
    assert (unavailable["status"], unavailable["file_name"], unavailable["snapshot"]) == (
        "unavailable",
        "2023-Example-Product-Info.pdf",
        None,
    )
    assert unavailable["detail"] == "no Wayback capture"
    assert unavailable["model_years"] == [2023]
    pricing = documents["https://toyota-cms-media.s3.amazonaws.com/pricing_pdf.pdf"]
    assert (pricing["status"], pricing["kind"]) == ("fetched", source.PRICING_PDF)
    assert pricing["snapshot"]["route"] == snapshot.WAYBACK
    assert pricing["snapshot"]["document_date"] == "2022-12-22"
    assert pricing["claims"] == {"admitted": 10, "rejected": 1}
    assert [entry["status"] for entry in pricing["extractions"]] == ["done"]
    assert {"kind": "product_information_pdf", "status": "unavailable", "count": 1} in answer[
        "counts"
    ]


def test_a_snapshot_with_no_candidate_is_still_listed(data_viewer: Viewer) -> None:
    _, answer = _get(data_viewer, "/api/documents")
    documents = {entry["url"]: entry for entry in answer["documents"]}

    fuel_economy = documents["https://www.fueleconomy.gov/feg/epadata/vehicles.csv.zip"]
    assert (fuel_economy["is_candidate"], fuel_economy["status"]) == (False, "fetched")
    assert fuel_economy["claims"] == {"admitted": 9}


def test_claims_are_counted_and_the_rejected_ones_listed_with_their_reason(
    data_viewer: Viewer,
) -> None:
    status, answer = _get(data_viewer, "/api/claims")

    assert status == 200
    assert answer["counts"] == [
        {"claim_type": "style", "status": "admitted", "claim_count": 5},
        {"claim_type": "value", "status": "admitted", "claim_count": 15},
        {"claim_type": "value", "status": "rejected", "claim_count": 1},
    ]
    assert answer["reject_reasons"] == [
        {
            "claim_type": "value",
            "reject_reason": "quote absent from the located cell",
            "claim_count": 1,
        }
    ]
    (rejected,) = answer["rejected"]
    assert (rejected["attribute"], rejected["value_text"], rejected["quote"]) == (
        "msrp",
        "$29,985",
        "$29,985",
    )
    assert rejected["file_name"] == "pricing_pdf.pdf"
    assert (answer["rejected_count"], answer["is_cut"]) == (1, False)


def test_a_rejected_claim_is_shown_beside_what_the_document_prints(data_viewer: Viewer) -> None:
    _, claims = _get(data_viewer, "/api/claims")

    status, answer = _get(data_viewer, f"/api/claim?claim_id={claims['rejected'][0]['claim_id']}")

    assert status == 200
    assert answer["claim"]["status"] == "rejected"
    assert answer["claim"]["reject_reason"] == "quote absent from the located cell"
    located = answer["claim"]["evidence"]
    assert located["problem"] == "the quote is not in the located cell"
    assert located["rows"][located["row_index"]][located["column_index"]] == "$29,895"
    assert answer["attached_to"] == []


def test_checks_group_the_findings_and_give_the_disagreements_and_coverage(
    data_viewer: Viewer,
) -> None:
    status, answer = _get(data_viewer, "/api/checks")

    assert status == 200
    assert [check["check_name"] for check in answer["checks"]] == list(consistency.CHECKS)
    by_name = {check["check_name"]: check["findings"] for check in answer["checks"]}
    assert [finding["description"] for finding in by_name["repeated_model_code"]] == [
        "Example 4x2 Limited",
        "Example 4x2 Platinum",
    ]
    assert by_name["drive_price_order"] == []
    open_cells = {(cell["style_key"], cell["attribute"]) for cell in answer["disagreements"]}
    assert ("2023-9101", "mpg_city") in open_cells
    assert all(cell["chosen_value"] is None for cell in answer["disagreements"])
    coverage = {(row["model_year"], row["attribute"]): row for row in answer["coverage"]}
    assert coverage[(2023, "msrp")] == {
        "model_year": 2023,
        "attribute": "msrp",
        "style_count": 5,
        "filled_count": 4,
        "open_count": 0,
        "unstated_count": 1,
    }
    assert coverage[(2023, "mpg_city")]["open_count"] == 3


def test_a_snapshot_is_served_by_its_hash(data_viewer: Viewer) -> None:
    _, answer = _get(data_viewer, "/api/documents")
    documents = {entry["kind"]: entry for entry in answer["documents"] if entry["snapshot"]}
    pricing = documents[source.PRICING_PDF]["snapshot"]["sha256"]
    fuel_economy = documents[source.FUEL_ECONOMY_FILE]["snapshot"]["sha256"]

    raw = viewer.respond(data_viewer, f"/snapshot/{pricing}/raw")
    text = viewer.respond(data_viewer, f"/snapshot/{pricing}/text")
    archive = viewer.respond(data_viewer, f"/snapshot/{fuel_economy}/raw")

    assert (raw.status, raw.media_type) == (200, "application/pdf")
    assert raw.body.startswith(b"%PDF")
    assert (text.status, text.media_type) == (200, "text/plain; charset=utf-8")
    assert "2023 EXAMPLE FULL-LINE PRICING" in text.body.decode()
    assert archive.media_type == "application/octet-stream"
    assert ("Content-Disposition", 'attachment; filename="vehicles.csv.zip"') in archive.headers
    assert viewer.respond(data_viewer, f"/snapshot/{'0' * 64}/raw").status == 404
    assert viewer.respond(data_viewer, "/snapshot/../surveyor.duckdb/raw").status == 404


def test_two_addresses_that_hold_the_same_bytes_do_not_show_a_claim_twice(
    data_viewer: Viewer,
) -> None:
    """Once `snapshots` is keyed on the address, one hash can be held under two addresses."""
    first_address = "https://toyota-cms-media.s3.amazonaws.com/pricing_pdf.pdf"
    second_address = "https://toyota-cms-media.s3.amazonaws.com/the-same-bytes-again.pdf"
    writer = duckdb.connect(str(data_viewer.settings.database_path))
    try:
        # The table as it will be: no uniqueness on the hash.
        writer.execute("CREATE TABLE snapshots_by_url AS SELECT * FROM snapshots")
        writer.execute("DROP TABLE snapshots")
        writer.execute("ALTER TABLE snapshots_by_url RENAME TO snapshots")
        writer.execute(
            "INSERT INTO snapshots "
            "SELECT * REPLACE (? AS url, fetched_at + INTERVAL 1 HOUR AS fetched_at) "
            "FROM snapshots WHERE url = ?",
            [second_address, first_address],
        )
    finally:
        writer.close()

    (value,) = _get_cell(data_viewer, "2023-9101", "msrp")["values"]
    _, style = _get(data_viewer, "/api/style?style_key=2023-9101")
    _, claims = _get(data_viewer, "/api/claims")
    status, rejected = _get(data_viewer, f"/api/claim?claim_id={claims['rejected'][0]['claim_id']}")
    _, answer = _get(data_viewer, "/api/documents")

    assert (value["claim"]["document"]["url"], value["claim"]["document"]["address_count"]) == (
        first_address,
        2,
    )
    assert len(style["claims"]) == 1
    assert len(claims["rejected"]) == 1
    assert (status, rejected["claim"]["document"]["url"]) == (200, first_address)
    documents = {entry["url"]: entry for entry in answer["documents"]}
    assert documents[second_address]["claims"] == documents[first_address]["claims"]
    sha256 = documents[second_address]["snapshot"]["sha256"]
    assert viewer.respond(data_viewer, f"/snapshot/{sha256}/raw").status == 200


# --- a database that cannot be read ------------------------------------------


def test_a_database_another_process_is_writing_is_reported_as_locked(data_viewer: Viewer) -> None:
    command = [sys.executable, "-c", _HOLD_DATABASE, str(data_viewer.settings.database_path)]
    holder = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert holder.stdin is not None and holder.stdout is not None
    try:
        assert holder.stdout.readline().strip() == "holding"

        status, answer = _get(data_viewer, "/api/catalogue")
        snapshot_status = viewer.respond(data_viewer, f"/snapshot/{'0' * 64}/raw").status
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)

    assert (status, answer["state"]) == (503, "locked")
    assert "pipeline is running" in answer["message"]
    assert "Traceback" not in answer["message"]
    assert snapshot_status == 503
    assert _get(data_viewer, "/api/catalogue")[0] == 200


def test_a_database_open_for_writing_in_this_process_is_reported_as_locked(
    connection: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    status, answer = _get(Viewer(settings), "/api/claims")

    assert (status, answer["state"]) == (503, "locked")


def test_a_missing_database_is_reported(settings: Settings) -> None:
    status, answer = _get(Viewer(settings), "/api/catalogue")

    assert (status, answer["state"]) == (404, "no_database")
    assert str(settings.database_path) in answer["message"]
    assert not settings.database_path.exists()


def test_a_database_that_was_never_resolved_is_reported(
    connection: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    connection.close()

    status, answer = _get(Viewer(settings), "/api/catalogue")

    assert (status, answer["state"]) == (409, "not_resolved")
    assert "just resolve" in answer["message"]
    assert _get(Viewer(settings), "/api/claims")[0] == 200


def test_meta_needs_no_database(settings: Settings) -> None:
    status, answer = _get(Viewer(settings), "/api/meta")

    assert status == 200
    assert (answer["has_database"], answer["reference"]) == (False, "off")
    assert answer["diff_detail_file"] is None
    assert answer["inbox_dir"] == str(settings.inbox_dir.resolve())


# --- the diff's feedback -----------------------------------------------------


def test_diff_says_nothing_has_run_when_there_is_no_feedback(settings: Settings) -> None:
    status, answer = _get(Viewer(settings), "/api/diff")

    assert status == 200
    assert (answer["runs"], answer["run_id"], answer["feedback"]) == ([], None, None)
    assert answer["feedback_dir"] == str((settings.data_dir / "feedback").resolve())


def test_diff_serves_the_latest_feedback_and_any_run_asked_for(settings: Settings) -> None:
    earlier = {
        **FEEDBACK,
        "run_id": "20261115T090000Z-dev",
        "created_at": "2026-11-15T09:00:00Z",
        "cells": FEEDBACK["cells"][:1],
    }
    _write_feedback(settings, earlier)
    _write_feedback(settings, FEEDBACK)
    data_viewer = Viewer(settings)

    status, latest = _get(data_viewer, "/api/diff")
    _, asked_for = _get(data_viewer, "/api/diff?run=20261115T090000Z-dev")

    assert status == 200
    assert [run["run_id"] for run in latest["runs"]] == [
        "20261116T101233Z-dev",
        "20261115T090000Z-dev",
    ]
    assert latest["run_id"] == "20261116T101233Z-dev"
    assert latest["feedback"] == FEEDBACK
    assert latest["dropped_keys"] == []
    assert latest["reference"] is None
    assert asked_for["run_id"] == "20261115T090000Z-dev"
    assert len(asked_for["feedback"]["cells"]) == 1
    assert _get(data_viewer, "/api/diff?run=nothing")[0] == 404


def test_feedback_with_keys_missing_is_still_shown(settings: Settings) -> None:
    _write_feedback(settings, {"cells": [{"style_key": "2023-9101", "attribute": "msrp"}]}, "old")

    _, answer = _get(Viewer(settings), "/api/diff")

    feedback = answer["feedback"]
    assert (answer["run_id"], feedback["run_id"]) == ("old", "old")
    assert feedback["cells"] == [
        {"style_key": "2023-9101", "attribute": "msrp", "status": None, "bucket": None}
    ]
    assert (feedback["created_at"], feedback["model_years"]) == (None, [])
    assert (feedback["row_matching"], feedback["metrics"], feedback["buckets"]) == ({}, [], {})
    assert feedback["extra_styles"] == []


def test_feedback_serves_only_what_the_contract_names(settings: Settings) -> None:
    """A detail file saved under feedback/ by mistake gives up no reference value."""
    leaking_cells = [{**cell, "reference_value": REFERENCE_MSRP} for cell in FEEDBACK["cells"]]
    _write_feedback(settings, {**DETAIL, "cells": leaking_cells})

    response = viewer.respond(Viewer(settings), "/api/diff")

    answer = json.loads(response.body)
    assert str(REFERENCE_MSRP) not in response.body.decode()
    assert answer["feedback"] == FEEDBACK
    assert answer["reference"] is None
    assert answer["dropped_keys"] == ["cells[].reference_value", "extra", "matches", "missed"]


def test_a_feedback_file_that_cannot_be_read_is_named_and_skipped(settings: Settings) -> None:
    _write_feedback(settings, FEEDBACK)
    (settings.data_dir / "feedback" / "broken.json").write_text("{not json", encoding="utf-8")
    (settings.data_dir / "feedback" / "list.json").write_text("[1, 2]", encoding="utf-8")

    status, answer = _get(Viewer(settings), "/api/diff")

    assert status == 200
    assert answer["unreadable"] == ["broken.json", "list.json"]
    assert answer["run_id"] == FEEDBACK["run_id"]


# --- reference values --------------------------------------------------------


def test_the_detail_argument_has_no_default() -> None:
    options = cli.build_parser().parse_args(["view"])

    assert options.diff_detail is None
    assert viewer.build_viewer(Settings()).diff_detail_path is None


def test_no_endpoint_serves_a_reference_value_without_the_detail_file(
    data_viewer: Viewer, detail_path: Path
) -> None:
    """The detail file exists, and feedback names the run, but the viewer was not given it."""
    _write_feedback(data_viewer.settings, FEEDBACK)

    bodies = {
        target: viewer.respond(data_viewer, target).body.decode() for target in EVERY_ENDPOINT
    }
    with_a_token = viewer.respond(data_viewer, "/api/diff?token=anything").body.decode()

    assert all(str(REFERENCE_MSRP) not in body for body in bodies.values())
    assert all("reference_value" not in body for body in bodies.values())
    assert str(REFERENCE_MSRP) not in with_a_token
    assert json.loads(bodies["/api/diff"])["reference"] is None
    assert json.loads(bodies["/api/diff"])["feedback"] == FEEDBACK
    assert json.loads(bodies["/api/meta"])["reference"] == "off"


def test_the_detail_file_is_served_only_to_a_request_with_the_token(
    data_viewer: Viewer, detail_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(viewer.AGENT_SESSION_VARIABLE, raising=False)
    started = viewer.build_viewer(data_viewer.settings, detail_path)
    assert len(started.token) >= 16

    without = viewer.respond(started, "/api/diff").body.decode()
    wrong = viewer.respond(started, "/api/diff?token=wrong").body.decode()
    status, shown = _get(started, f"/api/diff?token={started.token}")

    assert str(REFERENCE_MSRP) not in without + wrong
    assert json.loads(without)["reference"] is None
    assert _get(started, "/api/meta")[1]["reference"] == "needs_token"
    assert _get(started, "/api/meta")[1]["diff_detail_file"] is None
    assert status == 200
    assert shown["run_id"] == FEEDBACK["run_id"]
    assert shown["feedback"] == FEEDBACK
    assert shown["dropped_keys"] == []
    assert shown["reference"] == {
        "file_name": "detail.json",
        "matches": DETAIL["matches"],
        "missed": DETAIL["missed"],
        "extra": DETAIL["extra"],
    }
    assert _get(started, f"/api/meta?token={started.token}")[1]["reference"] == "shown"


def test_with_the_token_only_the_diff_endpoint_serves_reference_values(
    data_viewer: Viewer, detail_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(viewer.AGENT_SESSION_VARIABLE, raising=False)
    started = viewer.build_viewer(data_viewer.settings, detail_path)

    for target in EVERY_ENDPOINT:
        joiner = "&" if "?" in target else "?"
        body = viewer.respond(started, f"{target}{joiner}token={started.token}").body.decode()
        assert (str(REFERENCE_MSRP) in body) == (target == "/api/diff"), target


def test_what_the_diff_writes_is_served_whole(
    data_viewer: Viewer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The diff's two files, written by the diff itself from a made-up reference.

    The viewer and the diff were written apart, against one description of
    these files. This holds them to each other: nothing the diff writes is
    dropped, and nothing the viewer needs is missing.
    """
    monkeypatch.delenv(viewer.AGENT_SESSION_VARIABLE, raising=False)
    settings = data_viewer.settings
    connection = storage.connect(settings.database_path, is_read_only=True)
    try:
        styles = diff.load_pipeline_styles(connection, (2023,))
        adjudications = diff.load_adjudications(connection)
    finally:
        connection.close()

    def describe_row(trim_name: str, code: str, msrp: int) -> ReferenceStyle:
        return ReferenceStyle(
            file="example/2023.yaml",
            model_year=2023,
            is_mid_year=False,
            model_slug="example",
            model="Example",
            trim=trim_name.lower(),
            trim_name=trim_name,
            configuration="standard",
            codes=(code,),
            labels={"drivetrain": "FWD"},
            msrp=msrp,
        )

    result = diff.build_diff(
        styles,
        [describe_row("LE", "9101", REFERENCE_MSRP), describe_row("Nowhere", "9199", 31000)],
        adjudications,
        split="dev",
        model_years=(2023,),
        created_at=datetime(2026, 11, 16, 10, 12, 33, tzinfo=UTC),
        git_sha="abc1234",
    )
    detail_file, feedback_file = diff.write_diff(result, tmp_path / "out", settings.feedback_dir)
    written = json.loads(feedback_file.read_text(encoding="utf-8"))

    _, plain = _get(data_viewer, "/api/diff")
    started = viewer.build_viewer(settings, detail_file)
    _, shown = _get(started, f"/api/diff?token={started.token}")

    assert plain["run_id"] == "20261116T101233Z-dev"
    assert (plain["feedback"], plain["dropped_keys"], plain["reference"]) == (written, [], None)
    assert str(REFERENCE_MSRP) not in json.dumps(plain)
    assert (shown["feedback"], shown["dropped_keys"]) == (written, [])
    (match,) = shown["reference"]["matches"]
    assert (match["style_key"], match["matched_by"], match["has_other_code"]) == (
        "2023-9101",
        "code",
        False,
    )
    assert match["description"] == "Example LE 4-cyl"
    price = next(cell for cell in match["cells"] if cell["attribute"] == "msrp")
    assert (price["status"], price["pipeline_value"], price["reference_value"]) == (
        "disagree",
        28495.0,
        REFERENCE_MSRP,
    )
    assert [row["trim_name"] for row in shown["reference"]["missed"]] == ["Nowhere"]
    assert len(shown["reference"]["extra"]) == len(written["extra_styles"]) == 4


def test_reference_values_are_shown_only_for_the_run_the_detail_file_describes(
    settings: Settings, detail_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(viewer.AGENT_SESSION_VARIABLE, raising=False)
    earlier = {**FEEDBACK, "run_id": "20261115T090000Z-dev", "created_at": "2026-11-15T09:00:00Z"}
    _write_feedback(settings, earlier)
    started = viewer.build_viewer(settings, detail_path)

    _, shown = _get(started, f"/api/diff?token={started.token}")
    other = viewer.respond(started, f"/api/diff?token={started.token}&run=20261115T090000Z-dev")

    assert [run["run_id"] for run in shown["runs"]] == [
        "20261116T101233Z-dev",
        "20261115T090000Z-dev",
    ]
    assert shown["run_id"] == "20261116T101233Z-dev"
    assert shown["reference"] is not None
    assert str(REFERENCE_MSRP) not in other.body.decode()
    assert json.loads(other.body)["reference_run_id"] == "20261116T101233Z-dev"


def test_build_viewer_refuses_the_detail_file_in_an_agent_session(
    settings: Settings, tmp_path: Path
) -> None:
    """Refused before the path is looked at: the file here does not even exist."""
    with pytest.raises(viewer.ReferenceRefusal, match="own terminal"):
        viewer.build_viewer(
            settings, tmp_path / "no-such-file.json", environment={"CLAUDECODE": "1"}
        )


def test_the_view_command_refuses_the_detail_file_in_an_agent_session(
    tmp_path: Path,
    detail_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")

    def fail_to_start(*arguments: object) -> None:
        raise AssertionError("the server was started")

    monkeypatch.setattr(viewer, "ViewerServer", fail_to_start)

    status = cli.main(
        ["--data-dir", str(tmp_path / "data"), "view", "--diff-detail", str(detail_path)]
    )

    printed = capsys.readouterr().out
    assert status == 2
    assert "--diff-detail was refused" in printed
    assert "Run it from your own terminal" in printed
    assert str(REFERENCE_MSRP) not in printed


def test_the_view_command_runs_in_an_agent_session_without_the_detail_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(viewer.ViewerServer, "serve_forever", lambda self: None)

    status = cli.main(["--data-dir", str(tmp_path / "data"), "view", "--port", "0"])

    printed = capsys.readouterr().out
    assert status == 0
    assert "serving" in printed and "http://127.0.0.1:" in printed
    assert "REFERENCE" not in printed
    assert not (tmp_path / "data").exists()


def test_the_detail_file_is_not_opened_in_an_agent_session_whoever_asks(
    settings: Settings, detail_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A viewer built around `build_viewer` still cannot read the file."""
    monkeypatch.setenv("CLAUDECODE", "1")
    started = Viewer(settings, detail_path, "a-token")

    response = viewer.respond(started, "/api/diff?token=a-token")

    assert response.status == 403
    assert json.loads(response.body)["state"] == "refused"
    assert str(REFERENCE_MSRP) not in response.body.decode()
    with pytest.raises(viewer.ReferenceRefusal):
        viewer.load_diff_detail(detail_path)


def test_a_detail_file_that_is_not_there_is_said_in_the_users_terminal(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(viewer.AGENT_SESSION_VARIABLE, raising=False)

    status = viewer.serve(settings, port=0, diff_detail_path=tmp_path / "no-such-file.json")

    assert status == 2
    assert "there is no file at" in capsys.readouterr().out


# --- the page and the server -------------------------------------------------


def test_the_page_asks_the_network_for_nothing() -> None:
    page = (viewer.ASSET_DIR / "index.html").read_text(encoding="utf-8")
    style_sheet = (viewer.ASSET_DIR / "viewer.css").read_text(encoding="utf-8")
    script = (viewer.ASSET_DIR / "viewer.js").read_text(encoding="utf-8")

    assert re.findall(r'(?:src|href)="([^"]*)"', page) == [
        "data:,",
        "/assets/viewer.css",
        "/assets/viewer.js",
    ]
    assert "@import" not in style_sheet and "url(" not in style_sheet
    assert not re.search(r"""["'`](?:https?:)?//""", script)
    assert "innerHTML" not in script


def test_an_asset_outside_the_asset_directory_is_not_served() -> None:
    assert viewer.respond(Viewer(Settings()), "/assets/viewer.js").status == 200
    assert viewer.respond(Viewer(Settings()), "/assets/../viewer.py").status == 404
    assert viewer.respond(Viewer(Settings()), "/assets/../../../pyproject.toml").status == 404
    assert viewer.respond(Viewer(Settings()), "/assets/").status == 404


@pytest.fixture
def server_port(data_viewer: Viewer) -> Iterator[int]:
    """The viewer answering on an ephemeral port of localhost, on its own thread."""
    server = viewer.ViewerServer(data_viewer, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port
    server.shutdown()
    server.server_close()
    thread.join(timeout=10)


def test_the_server_answers_over_http(server_port: int) -> None:
    def fetch(target: str) -> tuple[int, str, bytes]:
        link = http.client.HTTPConnection(viewer.HOST, server_port, timeout=10)
        try:
            link.request("GET", target)
            response = link.getresponse()
            return response.status, response.headers["Content-Type"], response.read()
        finally:
            link.close()

    status, media_type, page = fetch("/")
    assert (status, media_type) == (200, "text/html; charset=utf-8")
    assert b"/assets/viewer.js" in page
    assert fetch("/assets/viewer.js")[:2] == (200, "text/javascript; charset=utf-8")
    assert fetch("/assets/viewer.css")[:2] == (200, "text/css; charset=utf-8")
    status, media_type, body = fetch("/api/catalogue")
    assert (status, media_type) == (200, "application/json; charset=utf-8")
    assert len(json.loads(body)["styles"]) == 5
    assert fetch("/assets/../viewer.py")[0] == 404
    assert fetch("/api/nothing")[0] == 404
