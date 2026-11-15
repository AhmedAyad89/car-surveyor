"""Tests for claim records and style keys. No network."""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from car_surveyor import claim
from car_surveyor.claim import ClaimLocator, PageHint, StyleClaim, ValueClaim
from car_surveyor.evidence import FileRow, TableCell, TextSpan

SNAPSHOT = "a" * 64


def _style(claim_id: str = "s1", style_key: str = "2023-9101") -> StyleClaim:
    return StyleClaim(
        claim_id=claim_id,
        style_key=style_key,
        model_year=2023,
        model="Example",
        model_code="9101",
        grade="LE",
        powertrain=None,
        body=None,
        engine="4-cyl",
        transmission="8ECTi",
        drive=None,
        description="Example LE 4-cyl 8ECTi",
        snapshot_sha256=SNAPSHOT,
        locator=TableCell(1, 0, "9101", "MODEL"),
        quote="Example LE 4-cyl 8ECTi",
        extractor_version="test+pricing-v1",
    )


def _value(claim_id: str = "v1") -> ValueClaim:
    return ValueClaim(
        claim_id=claim_id,
        style_key="2023-9101",
        applies_to={},
        attribute=claim.MSRP,
        value_text="$28,495",
        value_num=28495.0,
        unit="USD",
        snapshot_sha256=SNAPSHOT,
        locator=TableCell(1, 0, "9101", "MSRP"),
        quote="$28,495",
        extractor_version="test+pricing-v1",
        source_class=claim.OEM_PRICING,
        qualifiers={"delivery_fee": "excluded"},
        hedge_flags=("estimated",),
        effective_date=date(2022, 12, 22),
    )


def test_build_style_key_uses_the_model_code_where_the_document_prints_one() -> None:
    key = claim.build_style_key(2023, model_code="9101", is_code_unique=True, model="Example")
    assert key == "2023-9101"


def test_build_style_key_is_the_same_whatever_the_description_says_when_coded() -> None:
    """Two editions that word a row differently still describe one style."""
    first = claim.build_style_key(
        2023, model_code="9101", is_code_unique=True, model="Example", grade="LE", drive="4x2"
    )
    second = claim.build_style_key(
        2023, model_code="9101", is_code_unique=True, model="Example 4x2", grade="LE"
    )
    assert first == second


def test_build_style_key_describes_a_row_that_has_no_code() -> None:
    key = claim.build_style_key(
        2023, model_code=None, is_code_unique=False, model="eX4", grade="XLE", drive="FWD"
    )
    assert key == "2023-ex4-xle-fwd"


def test_build_style_key_keeps_two_rows_apart_when_they_share_a_code() -> None:
    """A document can print one code on two rows. Both styles exist."""
    keys = [
        claim.build_style_key(
            2023, model_code="9103", is_code_unique=False, model="Example", grade=grade, drive="4x2"
        )
        for grade in ("Limited", "Platinum")
    ]
    assert keys == ["2023-example-limited-4x2", "2023-example-platinum-4x2"]


def test_build_style_key_keeps_manual_and_automatic_apart() -> None:
    automatic = claim.build_style_key(
        2023, model_code=None, is_code_unique=False, model="Example", transmission="AT"
    )
    manual = claim.build_style_key(
        2023, model_code=None, is_code_unique=False, model="Example", transmission="MT"
    )
    assert automatic != manual


def test_build_claim_id_is_stable_and_distinct() -> None:
    first = claim.build_claim_id(SNAPSHOT, "v1", "msrp", 0)
    assert first == claim.build_claim_id(SNAPSHOT, "v1", "msrp", 0)
    assert first != claim.build_claim_id(SNAPSHOT, "v1", "msrp", 1)
    assert first != claim.build_claim_id(SNAPSHOT, "v2", "msrp", 0)


@pytest.mark.parametrize(
    "locator",
    [
        TextSpan(10, 42),
        TableCell(2, 1, "Horsepower", "FOUR-CYLINDER"),
        FileRow("45001", "city08"),
        PageHint(3),
        PageHint(None),
    ],
)
def test_a_locator_survives_serialization(locator: ClaimLocator) -> None:
    assert claim.parse_locator(claim.serialize_locator(locator)) == locator


def test_claims_come_back_from_the_database_as_they_went_in(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    claim.store_style_claims(connection, [_style()])
    claim.store_value_claims(connection, [_value()])
    assert claim.list_style_claims(connection) == [_style()]
    assert claim.list_value_claims(connection) == [_value()]


def test_settle_claim_records_the_decision_and_the_location_found(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    claim.store_value_claims(connection, [_value("kept"), _value("dropped")])
    claim.settle_claim(connection, "value_claims", "kept", "admitted", locator=TextSpan(5, 12))
    claim.settle_claim(
        connection, "value_claims", "dropped", "rejected", reject_reason="quote absent"
    )
    admitted = claim.list_value_claims(connection, status="admitted")
    rejected = claim.list_value_claims(connection, status="rejected")
    assert [(found.claim_id, found.locator) for found in admitted] == [("kept", TextSpan(5, 12))]
    assert [(found.claim_id, found.reject_reason) for found in rejected] == [
        ("dropped", "quote absent")
    ]


def test_delete_claims_drops_only_the_named_snapshot(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    claim.store_style_claims(connection, [_style("s1")])
    claim.store_value_claims(connection, [_value("v1")])
    claim.delete_claims(connection, "b" * 64)
    assert len(claim.list_style_claims(connection)) == 1
    claim.delete_claims(connection, SNAPSHOT)
    assert claim.list_style_claims(connection) == []
    assert claim.list_value_claims(connection) == []
