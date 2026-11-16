"""Tests for the extract stage. No network and no model: extractors are stand-ins."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import duckdb
import pytest
from conftest import PdfBuilder

from car_surveyor import claim, extraction, snapshot, source
from car_surveyor.claim import PageHint
from car_surveyor.config import Settings
from car_surveyor.evidence import FileRow, TableCell
from car_surveyor.extraction import (
    AnthropicExtractor,
    DatedQuote,
    ExtractionError,
    ExtractionRequest,
    ExtractorResponse,
)
from car_surveyor.snapshot import Snapshot
from car_surveyor.source import Candidate, Registry

PRICING_TEXT = [
    "2023 EXAMPLE FULL-LINE PRICING",
    "Prices do not include the delivery, processing and handling fee.",
    "$1,095 for Passenger Cars (Example, eX4)",
    "Last updated 12/22/2022",
]
PRICING_TABLE = [
    ["MODEL #", "MY", "MODEL", "MSRP"],
    ["9101", "2023", "Example LE 4-cyl", "$28,495"],
    ["9102", "2023", "Example LE AWD 4-cyl", "$29,895"],
    ["9103", "2023", "Example 4x2 Limited", "$35,900"],
    ["9103", "2023", "Example 4x2 Platinum", "$38,400"],
    ["", "2023", "eX4 XLE FWD", "$42,000"],
]
SPECIFICATION_TABLE = [
    ["ENGINE", "FOUR-CYLINDER", "V6"],
    ["Horsepower", "203 hp (LE, SE) 206 hp (XSE)", "301 hp"],
]


def _row(
    code: str | None, description: str, msrp: str, *, by_description: bool = False, **parts: Any
) -> dict[str, Any]:
    split = {"powertrain": None, "grade": None, "drive": None, "engine": None, "transmission": None}
    return {
        "page": 2,
        "table": 0,
        "row_label": description if by_description or code is None else code,
        "model_code": code,
        "model_year": "2023",
        "description": description,
        "msrp": msrp,
        "model": "Example",
        "body": None,
        **split,
        **parts,
    }


PRICING_ANSWER: dict[str, Any] = {
    "document_date": {"date": "2022-12-22", "quote": "Last updated 12/22/2022", "page": 1},
    "price_basis": {
        "delivery_fee": "excluded",
        "quote": "Prices do not include the delivery, processing and handling fee.",
        "page": 1,
    },
    "price_effective": None,
    "delivery_fees": [
        {
            "amount": "$1,095",
            "vehicle_class": "Passenger Cars",
            "models": ["Example", "eX4"],
            "effective_date": None,
            "quote": "$1,095 for Passenger Cars (Example, eX4)",
            "page": 1,
        }
    ],
    "columns": {
        "model_code": "MODEL #",
        "model_year": "MY",
        "description": "MODEL",
        "msrp": "MSRP",
    },
    "rows": [
        _row("9101", "Example LE 4-cyl", "$28,495", grade="LE", engine="4-cyl"),
        _row("9102", "Example LE AWD 4-cyl", "$29,895", grade="LE", drive="AWD", engine="4-cyl"),
        _row("9103", "Example 4x2 Limited", "$35,900", by_description=True, grade="Limited"),
        _row("9103", "Example 4x2 Platinum", "$38,400", by_description=True, grade="Platinum"),
        _row(None, "eX4 XLE FWD", "$42,000", model="eX4", grade="XLE", drive="FWD"),
    ],
}
SPECIFICATION_ANSWER: dict[str, Any] = {
    "model_year": 2023,
    "model": "Example",
    "document_date": None,
    "values": [
        {
            "attribute": "horsepower",
            "value": "203",
            "unit": "hp",
            "describes": None,
            "quote": "203 hp (LE, SE)",
            "applies_to": {
                "grades": ["LE", "SE"],
                "drive": None,
                "engine": "FOUR-CYLINDER",
                "transmission": None,
                "body": None,
            },
            "hedges": [],
            "locator": {
                "kind": "table_cell",
                "page": 2,
                "table": 0,
                "row_label": "Horsepower",
                "column_header": "FOUR-CYLINDER",
            },
        },
        {
            "attribute": "mpg_combined",
            "value": "32",
            "unit": "MPG",
            "describes": None,
            "quote": "an EPA-estimated 32 combined MPG",
            "applies_to": {
                "grades": [],
                "drive": None,
                "engine": None,
                "transmission": None,
                "body": None,
            },
            "hedges": ["EPA-estimated"],
            "locator": {
                "kind": "text",
                "page": 1,
                "table": None,
                "row_label": None,
                "column_header": None,
            },
        },
    ],
}


class FakeExtractor:
    """Stands in for a model: answers every request with the text it was given."""

    label = "fake-model"

    def __init__(self, answer: dict[str, Any] | str | Exception) -> None:
        self.requests: list[ExtractionRequest] = []
        self._answer = answer

    def extract(self, request: ExtractionRequest) -> ExtractorResponse:
        self.requests.append(request)
        if isinstance(self._answer, Exception):
            raise self._answer
        text = self._answer if isinstance(self._answer, str) else json.dumps(self._answer)
        return ExtractorResponse(text, input_tokens=1000, output_tokens=500, cost_usd=0.014)


def _store(
    connection: duckdb.DuckDBPyConnection, settings: Settings, kind: str, raw: bytes
) -> Snapshot:
    url = f"https://toyota-cms-media.s3.amazonaws.com/{kind}.pdf"
    return snapshot.store_snapshot(
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


@pytest.fixture
def pricing_snapshot(
    connection: duckdb.DuckDBPyConnection, settings: Settings, build_pdf: PdfBuilder
) -> Snapshot:
    return _store(
        connection, settings, source.PRICING_PDF, build_pdf([PRICING_TEXT], table=PRICING_TABLE)
    )


@pytest.fixture
def specification_snapshot(
    connection: duckdb.DuckDBPyConnection, settings: Settings, build_pdf: PdfBuilder
) -> Snapshot:
    text = ["2023 Example Product Information", "an EPA-estimated 32 combined MPG"]
    raw = build_pdf([text], table=SPECIFICATION_TABLE)
    return _store(connection, settings, source.PRODUCT_INFORMATION_PDF, raw)


# --- the request -------------------------------------------------------------


def test_build_request_shows_each_table_with_its_page_and_index(
    pricing_snapshot: Snapshot,
) -> None:
    request = extraction.build_request(pricing_snapshot)
    assert request.prompt_name == "pricing-v1"
    assert "locate and copy" in request.system
    assert "===== PAGE 1 =====\n2023 EXAMPLE FULL-LINE PRICING" in request.document
    assert "--- TABLE (page 2, table 0) ---\nMODEL # | MY | MODEL | MSRP" in request.document
    assert "9101 | 2023 | Example LE 4-cyl | $28,495" in request.document
    assert " | 2023 | eX4 XLE FWD | $42,000" in request.document
    assert request.schema["additionalProperties"] is False
    assert "rows" in request.schema["required"]


# --- the stage ---------------------------------------------------------------


def test_extract_documents_turns_an_answer_into_located_claims(
    connection: duckdb.DuckDBPyConnection, settings: Settings, pricing_snapshot: Snapshot
) -> None:
    report = extraction.extract_documents(connection, settings, FakeExtractor(PRICING_ANSWER))

    assert report.extracted == [pricing_snapshot.url]
    assert (report.model_call_count, report.input_tokens, report.cost_usd) == (1, 1000, 0.014)
    styles = claim.list_style_claims(connection)
    values = claim.list_value_claims(connection)
    assert [style.style_key for style in styles] == [
        "2023-9101",
        "2023-9102",
        "2023-example-limited",
        "2023-example-platinum",
        "2023-ex4-xle-fwd",
    ]
    first = styles[0]
    assert (first.model, first.grade, first.engine, first.model_code) == (
        "Example",
        "LE",
        "4-cyl",
        "9101",
    )
    assert first.locator == TableCell(2, 0, "9101", "MODEL")
    assert first.extractor_version == "fake-model+pricing-v1"
    assert {style.status for style in styles} == {"extracted"}

    prices = [value for value in values if value.attribute == claim.MSRP]
    assert [price.value_num for price in prices] == [28495.0, 29895.0, 35900.0, 38400.0, 42000.0]
    assert prices[0].style_key == "2023-9101"
    assert prices[0].locator == TableCell(2, 0, "9101", "MSRP")
    assert prices[0].qualifiers == {"delivery_fee": "excluded"}
    assert prices[0].effective_date == date(2022, 12, 22)
    assert prices[2].locator == TableCell(2, 0, "Example 4x2 Limited", "MSRP")


def test_extract_documents_records_each_delivery_fee_as_a_scoped_claim(
    connection: duckdb.DuckDBPyConnection, settings: Settings, pricing_snapshot: Snapshot
) -> None:
    extraction.extract_documents(connection, settings, FakeExtractor(PRICING_ANSWER))
    fees = [
        value
        for value in claim.list_value_claims(connection)
        if value.attribute == claim.DELIVERY_FEE
    ]
    assert len(fees) == 1
    assert fees[0].style_key is None
    assert fees[0].applies_to == {"vehicle_class": "Passenger Cars", "models": ["Example", "eX4"]}
    assert (fees[0].value_num, fees[0].locator) == (1095.0, PageHint(1))


def test_extract_documents_sets_the_document_date_it_could_find(
    connection: duckdb.DuckDBPyConnection, settings: Settings, pricing_snapshot: Snapshot
) -> None:
    extraction.extract_documents(connection, settings, FakeExtractor(PRICING_ANSWER))
    held = snapshot.find_snapshot(connection, settings, pricing_snapshot.url)
    assert held is not None
    assert held.document_date == date(2022, 12, 22)


def test_a_second_run_makes_no_model_calls(
    connection: duckdb.DuckDBPyConnection, settings: Settings, pricing_snapshot: Snapshot
) -> None:
    """The S1 gate for the extract stage."""
    extractor = FakeExtractor(PRICING_ANSWER)
    extraction.extract_documents(connection, settings, extractor)
    claims_after_first_run = claim.list_value_claims(connection)

    second = extraction.extract_documents(connection, settings, extractor)

    assert len(extractor.requests) == 1
    assert (second.model_call_count, second.already_held) == (0, [pricing_snapshot.url])
    assert claim.list_value_claims(connection) == claims_after_first_run


def test_a_failed_extraction_is_recorded_and_not_repeated_until_asked(
    connection: duckdb.DuckDBPyConnection, settings: Settings, pricing_snapshot: Snapshot
) -> None:
    failing = FakeExtractor(ExtractionError("the answer was cut off at 64000 tokens"))
    first = extraction.extract_documents(connection, settings, failing)
    second = extraction.extract_documents(connection, settings, failing)
    assert (first.failed, second.failed) == ([pricing_snapshot.url], [pricing_snapshot.url])
    assert len(failing.requests) == 1
    assert claim.list_style_claims(connection) == []

    working = FakeExtractor(PRICING_ANSWER)
    retried = extraction.extract_documents(connection, settings, working, is_retrying_failed=True)
    assert retried.extracted == [pricing_snapshot.url]
    assert len(claim.list_style_claims(connection)) == 5


def test_an_answer_that_does_not_fit_the_schema_yields_no_claims(
    connection: duckdb.DuckDBPyConnection, settings: Settings, pricing_snapshot: Snapshot
) -> None:
    broken = {**PRICING_ANSWER, "rows": [{"model_code": "9101", "msrp": 28495}]}
    report = extraction.extract_documents(connection, settings, FakeExtractor(broken))
    assert report.failed == [pricing_snapshot.url]
    assert claim.list_style_claims(connection) == []
    status = extraction.find_extraction_status(
        connection, pricing_snapshot.sha256, "fake-model+pricing-v1"
    )
    assert status == "failed"


def test_a_row_whose_model_year_is_not_a_number_is_left_out(
    connection: duckdb.DuckDBPyConnection, settings: Settings, pricing_snapshot: Snapshot
) -> None:
    rows = [{**PRICING_ANSWER["rows"][0], "model_year": "TBD"}, PRICING_ANSWER["rows"][1]]
    extraction.extract_documents(
        connection, settings, FakeExtractor({**PRICING_ANSWER, "rows": rows})
    )
    assert [style.style_key for style in claim.list_style_claims(connection)] == ["2023-9102"]


def test_specification_values_keep_what_the_document_says_they_apply_to(
    connection: duckdb.DuckDBPyConnection, settings: Settings, specification_snapshot: Snapshot
) -> None:
    extraction.extract_documents(connection, settings, FakeExtractor(SPECIFICATION_ANSWER))

    assert claim.list_style_claims(connection) == []
    power, economy = claim.list_value_claims(connection)
    assert (power.attribute, power.value_num, power.unit) == (claim.HORSEPOWER, 203.0, "hp")
    assert power.style_key is None
    assert power.applies_to == {
        "model_year": 2023,
        "model": "Example",
        "grades": ["LE", "SE"],
        "drive": None,
        "engine": "FOUR-CYLINDER",
        "transmission": None,
        "body": None,
    }
    assert power.locator == TableCell(2, 0, "Horsepower", "FOUR-CYLINDER")
    assert power.source_class == claim.OEM_SPECIFICATION
    assert (economy.locator, economy.hedge_flags) == (PageHint(1), ("EPA-estimated",))
    assert economy.extractor_version == "fake-model+specification-v1"


# --- the document date -------------------------------------------------------

DATED_TEXT = "2023 Pricing\nLast updated 12/22/2022\fRevised December 5, 2022 for dealers"


@pytest.mark.parametrize(
    ("stated", "quote", "admitted"),
    [
        ("2022-12-22", "Last updated 12/22/2022", date(2022, 12, 22)),
        ("2022-12-05", "Revised December 5, 2022", date(2022, 12, 5)),
        ("2022-12-23", "Last updated 12/22/2022", None),
        ("2023-12-22", "Last updated 12/22/2022", None),
        ("2022-12-22", "Last updated 12/22/2022 by the pricing team", None),
        ("22 December 2022", "Last updated 12/22/2022", None),
    ],
)
def test_check_document_date_admits_only_a_date_its_quote_states(
    stated: str, quote: str, admitted: date | None
) -> None:
    dated = DatedQuote(date=stated, quote=quote, page=1)
    assert extraction.check_document_date(dated, DATED_TEXT) == admitted


def test_check_document_date_is_none_when_the_document_states_none() -> None:
    assert extraction.check_document_date(None, DATED_TEXT) is None


# --- the fuel economy file ---------------------------------------------------

_GASOLINE = "2.5,4,Regular Gasoline,,SIDI,Midsize Cars"
FUEL_ECONOMY_CSV = "\n".join(
    [
        "id,make,model,year,city08,highway08,comb08,trany,drive,"
        "displ,cylinders,fuelType1,atvType,eng_dscr,VClass,baseModel",
        "45001,Toyota,Example AWD LE/SE,2023,25,34,29,Automatic (S8),All-Wheel Drive,"
        f"{_GASOLINE},Example",
        "45002,Toyota,eX4,2023,131,107,119,Automatic (A1),Front-Wheel Drive,"
        ",,Electricity,EV,,Small SUV 2WD,eX4",
        f"45003,Toyota,Example,2019,28,39,32,Automatic (S8),Front-Wheel Drive,{_GASOLINE},Example",
        f"45004,Honda,Sample,2023,30,38,33,Automatic (AV),Front-Wheel Drive,{_GASOLINE},Sample",
        "",
    ]
)


@pytest.fixture
def fuel_economy_snapshot(connection: duckdb.DuckDBPyConnection, settings: Settings) -> Snapshot:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("vehicles.csv", FUEL_ECONOMY_CSV)
    url = "https://www.fueleconomy.gov/feg/epadata/vehicles.csv.zip"
    return snapshot.store_snapshot(
        connection,
        settings,
        url=url,
        source_id="fueleconomy",
        kind=source.FUEL_ECONOMY_FILE,
        raw=buffer.getvalue(),
        media_type="application/zip",
        fetch_url=url,
        route=snapshot.DIRECT,
    )


def test_extract_fuel_economy_reads_the_rows_of_the_make_and_years_in_scope(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    registry: Registry,
    fuel_economy_snapshot: Snapshot,
) -> None:
    report = extraction.extract_fuel_economy(connection, settings, registry)

    assert report.extracted == [fuel_economy_snapshot.url]
    assert report.model_call_count == 0
    values = claim.list_value_claims(connection)
    assert len(values) == 6
    city = values[0]
    assert (city.attribute, city.value_text, city.value_num, city.unit) == (
        claim.MPG_CITY,
        "25",
        25.0,
        "MPG",
    )
    assert city.locator == FileRow("45001", "city08")
    assert city.source_class == claim.EPA
    assert city.applies_to["model"] == "Example AWD LE/SE"
    assert city.applies_to["model_year"] == 2023
    assert city.applies_to["drive"] == "All-Wheel Drive"
    assert [value.attribute for value in values[:3]] == [
        claim.MPG_CITY,
        claim.MPG_HIGHWAY,
        claim.MPG_COMBINED,
    ]


def test_extract_fuel_economy_labels_electric_figures_as_gasoline_equivalent(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    registry: Registry,
    fuel_economy_snapshot: Snapshot,
) -> None:
    extraction.extract_fuel_economy(connection, settings, registry)
    electric = [
        value for value in claim.list_value_claims(connection) if value.applies_to["model"] == "eX4"
    ]
    assert {value.unit for value in electric} == {"MPGe"}
    assert [value.value_num for value in electric] == [131.0, 107.0, 119.0]


def test_extract_fuel_economy_parses_the_file_only_once(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    registry: Registry,
    fuel_economy_snapshot: Snapshot,
) -> None:
    extraction.extract_fuel_economy(connection, settings, registry)
    second = extraction.extract_fuel_economy(connection, settings, registry)
    assert second.already_held == [fuel_economy_snapshot.url]
    assert len(claim.list_value_claims(connection)) == 6


# --- the file exchange -------------------------------------------------------


def test_export_then_import_reads_a_document_without_the_api(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    pricing_snapshot: Snapshot,
    tmp_path: Path,
) -> None:
    exchange = tmp_path / "exchange"
    written = extraction.export_requests(connection, settings, "another-runner", exchange)

    assert [path.name for path in written] == [f"{pricing_snapshot.sha256}.request.json"]
    request = json.loads(written[0].read_text())
    assert request["extractor_version"] == "another-runner+pricing-v1"
    document_text = (exchange / request["document_file"]).read_text()
    assert "9101 | 2023 | Example LE 4-cyl | $28,495" in document_text
    assert json.loads((exchange / request["schema_file"]).read_text()) == request["schema"]

    waiting = extraction.import_answers(connection, settings, "another-runner", exchange)
    assert (waiting.extracted, waiting.failed) == ([], [])

    (exchange / request["answer_file"]).write_text(json.dumps(PRICING_ANSWER))
    report = extraction.import_answers(connection, settings, "another-runner", exchange)
    assert report.extracted == [pricing_snapshot.url]
    styles = claim.list_style_claims(connection)
    assert {style.extractor_version for style in styles} == {"another-runner+pricing-v1"}
    assert extraction.export_requests(connection, settings, "another-runner", exchange) == []


def test_export_requests_can_be_narrowed_to_model_years(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    pricing_snapshot: Snapshot,
    tmp_path: Path,
) -> None:
    """A document is exported for the model years discovery found it under."""
    found = Candidate(pricing_snapshot.url, "toyota_media", source.PRICING_PDF, (2022, 2023), "p")
    source.record_candidate(connection, found)
    other_years = extraction.export_requests(
        connection, settings, "another-runner", tmp_path, model_years=(2024, 2025)
    )
    its_years = extraction.export_requests(
        connection, settings, "another-runner", tmp_path, model_years=(2023,)
    )
    assert other_years == []
    assert [path.name for path in its_years] == [f"{pricing_snapshot.sha256}.request.json"]


def test_import_answers_reads_a_corrected_answer_file_again(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    pricing_snapshot: Snapshot,
    tmp_path: Path,
) -> None:
    answer_path = tmp_path / f"{pricing_snapshot.sha256}.answer.json"
    answer_path.write_text('{"rows": "not a list"}')
    broken = extraction.import_answers(connection, settings, "another-runner", tmp_path)
    assert broken.failed == [pricing_snapshot.url]

    answer_path.write_text(json.dumps(PRICING_ANSWER))
    fixed = extraction.import_answers(connection, settings, "another-runner", tmp_path)
    assert fixed.extracted == [pricing_snapshot.url]


def test_check_answer_file_counts_rows_against_the_document(
    connection: duckdb.DuckDBPyConnection,
    settings: Settings,
    pricing_snapshot: Snapshot,
    tmp_path: Path,
) -> None:
    request_path = extraction.export_requests(connection, settings, "another-runner", tmp_path)[0]
    answer_path = tmp_path / "answer.json"

    answer_path.write_text(json.dumps(PRICING_ANSWER))
    is_usable, summary = extraction.check_answer_file(request_path, answer_path)
    assert is_usable
    assert "5 rows and 1 delivery fees" in summary
    assert "5 rows that print a dollar amount" in summary

    answer_path.write_text(json.dumps({**PRICING_ANSWER, "extra": 1, "rows": [{"page": "two"}]}))
    is_usable, summary = extraction.check_answer_file(request_path, answer_path)
    assert not is_usable
    assert "does not fit the schema" in summary
    assert "rows.0.page" in summary


# --- the API extractor -------------------------------------------------------


class _Stream:
    def __init__(self, message: Any) -> None:
        self._message = message

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *details: object) -> None:
        return None

    def get_final_message(self) -> Any:
        return self._message


class _Messages:
    def __init__(self, message: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._message = message

    def stream(self, **arguments: Any) -> _Stream:
        self.calls.append(arguments)
        return _Stream(self._message)


def _stub_client(
    stop_reason: str = "end_turn", text: str = "{}"
) -> tuple[anthropic.Anthropic, _Messages]:
    """A stand-in for the SDK client that records the call and returns one message."""
    message = SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
    )
    messages = _Messages(message)
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return cast(anthropic.Anthropic, client), messages


def _request() -> ExtractionRequest:
    return ExtractionRequest(
        snapshot_sha256="a" * 64,
        url="https://example.test/pricing.pdf",
        kind=source.PRICING_PDF,
        prompt_name="pricing-v1",
        system="Locate and copy.",
        document="===== PAGE 1 =====\n9101 | 2023 | Example LE | $28,495",
        schema={"type": "object", "additionalProperties": False},
    )


def test_anthropic_extractor_sends_the_document_with_the_schema_and_prices_the_call() -> None:
    client, messages = _stub_client(text='{"rows": []}')
    extractor = AnthropicExtractor("claude-opus-5-5", "high", client=client)

    response = extractor.extract(_request())

    assert extractor.label == "claude-opus-5-5"
    assert response.text == '{"rows": []}'
    assert (response.input_tokens, response.output_tokens) == (1000, 500)
    assert response.cost_usd == pytest.approx(0.014)
    (call,) = messages.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["system"] == "Locate and copy."
    assert call["messages"] == [{"role": "user", "content": _request().document}]
    assert call["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": _request().schema},
    }
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]


@pytest.mark.parametrize(
    ("stop_reason", "message"), [("refusal", "declined"), ("max_tokens", "cut off")]
)
def test_anthropic_extractor_refuses_an_answer_that_is_not_whole(
    stop_reason: str, message: str
) -> None:
    client, _ = _stub_client(stop_reason=stop_reason)
    extractor = AnthropicExtractor("claude-opus-5-5", "high", client=client)
    with pytest.raises(ExtractionError, match=message):
        extractor.extract(_request())
