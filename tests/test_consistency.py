"""Tests for the consistency checks: each seeded error is found, each clean case is not."""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from car_surveyor import claim, consistency, resolution
from car_surveyor.claim import StyleClaim, ValueClaim
from car_surveyor.evidence import TableCell

SNAPSHOT = "a" * 64


@pytest.fixture
def database(connection: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    connection.execute(resolution.VIEWS_PATH.read_text(encoding="utf-8"))
    return connection


def _add_style(
    connection: duckdb.DuckDBPyConnection,
    style_key: str,
    description: str,
    msrp: float,
    *,
    model_year: int = 2023,
    model_code: str | None = None,
    grade: str = "LE",
    drive_class: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO style_records VALUES (?, ?, 'Example', ?, ?, NULL, NULL, NULL, NULL, NULL, ?, "
        "?, false, NULL, false, 1)",
        [style_key, model_year, model_code, grade, description, drive_class],
    )
    _add_value(connection, style_key, "msrp", msrp, claim.OEM_PRICING)


def _add_value(
    connection: duckdb.DuckDBPyConnection,
    style_key: str,
    attribute: str,
    number: float,
    source_class: str,
    *,
    specificity: int = 0,
    is_chosen: bool = True,
) -> None:
    connection.execute(
        "INSERT INTO style_values VALUES (?, ?, ?, ?, ?, '', NULL, ?, ?, ?, '', ?, false)",
        [
            style_key,
            attribute,
            f"{style_key}-{attribute}-{source_class}-{number:g}",
            number,
            f"{number:g}",
            source_class,
            SNAPSHOT,
            specificity,
            is_chosen,
        ],
    )


def _find(connection: duckdb.DuckDBPyConnection, check_name: str) -> list[tuple[str, str]]:
    return [
        (finding.style_key, finding.detail)
        for finding in consistency.run_checks(connection)
        if finding.check_name == check_name
    ]


def test_a_four_wheel_drive_style_priced_below_its_twin_is_found(
    database: duckdb.DuckDBPyConnection,
) -> None:
    _add_style(database, "fwd", "Example LE", 28495, drive_class="2wd")
    _add_style(database, "awd", "Example LE AWD", 27895, drive_class="4wd")
    _add_style(database, "xle-fwd", "Example XLE", 31250, grade="XLE", drive_class="2wd")
    _add_style(database, "xle-awd", "Example XLE AWD", 32650, grade="XLE", drive_class="4wd")
    assert _find(database, "drive_price_order") == [
        ("awd", "Example LE AWD at $27895 is priced below Example LE at $28495")
    ]


def test_a_price_that_jumps_between_model_years_is_found(
    database: duckdb.DuckDBPyConnection,
) -> None:
    _add_style(database, "2022-9101", "Example LE", 20000, model_year=2022, model_code="9101")
    _add_style(database, "2023-9101", "Example LE", 26000, model_code="9101")
    _add_style(database, "2022-9102", "Example XLE", 30000, model_year=2022, model_code="9102")
    _add_style(database, "2023-9102", "Example XLE", 30900, model_code="9102")
    _add_style(database, "2022-9103", "Example SE", 25000, model_year=2022, model_code="9103")
    _add_style(database, "2023-9103", "Example SE", 23000, model_code="9103")
    found = _find(database, "year_over_year_price")
    assert [style_key for style_key, _ in found] == ["2023-9101", "2023-9103"]
    assert found[0][1] == "Example LE: $26000 in 2023 after $20000 in 2022"


def test_a_stated_fuel_economy_that_differs_from_the_epa_file_is_found(
    database: duckdb.DuckDBPyConnection,
) -> None:
    _add_style(database, "differs", "Example LE Hybrid AWD", 40000)
    _add_value(database, "differs", "mpg_city", 36, claim.OEM_SPECIFICATION, is_chosen=False)
    _add_value(database, "differs", "mpg_city", 35, claim.EPA)
    _add_style(database, "agrees", "Example LE Hybrid", 39000)
    _add_value(database, "agrees", "mpg_city", 36, claim.OEM_SPECIFICATION, is_chosen=False)
    _add_value(database, "agrees", "mpg_city", 36, claim.EPA)
    _add_style(database, "open", "Example LE", 28000)
    _add_value(database, "open", "mpg_city", 32, claim.OEM_SPECIFICATION)
    _add_value(database, "open", "mpg_city", 32, claim.EPA, is_chosen=False)
    _add_value(database, "open", "mpg_city", 31, claim.EPA, is_chosen=False)
    _add_style(database, "outranked", "Example XLE", 31000)
    _add_value(database, "outranked", "mpg_city", 30, claim.OEM_SPECIFICATION, is_chosen=False)
    _add_value(database, "outranked", "mpg_city", 30, claim.EPA, specificity=3)
    _add_value(database, "outranked", "mpg_city", 28, claim.EPA, is_chosen=False)
    assert _find(database, "fuel_economy_agreement") == [
        ("differs", "Example LE Hybrid AWD: the document states 36 and the EPA file 35")
    ]


def _priced_row(description: str, code: str | None, price: float, **parts: Any) -> None:
    style_key = parts.pop("style_key", f"2023-{code or claim.slugify(description)}")
    connection: duckdb.DuckDBPyConnection = parts.pop("connection")
    row_label = f"{code}-{description}"
    claim.store_style_claims(
        connection,
        [
            StyleClaim(
                claim_id=f"style-{row_label}",
                style_key=style_key,
                model_year=2023,
                model="Example",
                model_code=code,
                grade=None,
                powertrain=None,
                body=None,
                engine=None,
                transmission=None,
                drive=None,
                description=description,
                snapshot_sha256=SNAPSHOT,
                locator=TableCell(1, 0, row_label, "MODEL"),
                quote=description,
                extractor_version="test",
                status=parts.pop("status", "admitted"),
            )
        ],
    )
    claim.store_value_claims(
        connection,
        [
            ValueClaim(
                claim_id=f"price-{row_label}",
                style_key=style_key,
                applies_to={},
                attribute=claim.MSRP,
                value_text=f"${price:,.0f}",
                value_num=price,
                unit="USD",
                snapshot_sha256=SNAPSHOT,
                locator=TableCell(1, 0, row_label, "MSRP"),
                quote=f"${price:,.0f}",
                extractor_version="test",
                source_class=claim.OEM_PRICING,
                status="admitted",
            )
        ],
    )


def test_a_model_code_printed_on_two_different_rows_is_found(
    database: duckdb.DuckDBPyConnection,
) -> None:
    _priced_row("Example 4x2 Limited", "9103", 35900, style_key="a", connection=database)
    _priced_row("Example 4x2 Platinum", "9103", 38400, style_key="b", connection=database)
    _priced_row("Example LE", "9101", 28495, connection=database)
    _priced_row("Example XLE", "9103", 1, style_key="c", status="rejected", connection=database)
    assert _find(database, "repeated_model_code") == [
        ("a", "model code 9103 is also printed on another row; this row is Example 4x2 Limited"),
        ("b", "model code 9103 is also printed on another row; this row is Example 4x2 Platinum"),
    ]


def test_a_description_priced_twice_at_different_prices_is_found(
    database: duckdb.DuckDBPyConnection,
) -> None:
    _priced_row("Example SR5 Access Cab", "7126", 32985, connection=database)
    _priced_row("Example SR5 Access Cab", "7170", 34630, connection=database)
    _priced_row("Example Base", "2372", 53000, connection=database)
    _priced_row("Example Base", "2383", 53000, connection=database)
    twice = "Example SR5 Access Cab is priced twice in one document"
    assert _find(database, "repeated_description") == [
        ("2023-7126", f"{twice}; this row is 7126 at $32985"),
        ("2023-7170", f"{twice}; this row is 7170 at $34630"),
    ]


def test_run_checks_replaces_the_findings_of_the_run_before(
    database: duckdb.DuckDBPyConnection,
) -> None:
    _add_style(database, "fwd", "Example LE", 28495, drive_class="2wd")
    _add_style(database, "awd", "Example LE AWD", 27895, drive_class="4wd")
    first = consistency.run_checks(database)
    second = consistency.run_checks(database)
    assert first == second
    assert database.execute("SELECT count(*) FROM check_results").fetchone() == (1,)
    assert "1  drive_price_order" in consistency.describe_findings(first)
