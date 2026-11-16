"""Tests for the verify stage. Each seeded error must be caught. No network."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from car_surveyor import claim, evidence, verification
from car_surveyor.claim import PageHint, StyleClaim, ValueClaim
from car_surveyor.document import PAGE_BREAK, Table
from car_surveyor.evidence import FileRow, TableCell, TextSpan

SNAPSHOT = "a" * 64
TABLES = (
    Table(
        1,
        0,
        (
            ("MODEL #", "MY", "MODEL", "MSRP"),
            ("9101", "2023", "Example LE 4-cyl", "$28,495"),
            ("9102", "2023", "Example XLE 4-cyl", "$31,250"),
        ),
    ),
    Table(
        2,
        0,
        (
            ("ENGINE", "FOUR-CYLINDER", "V6"),
            ("Horsepower", "203 hp @ 6,600 rpm (LE, SE) 206 hp @ 6,600 rpm (XSE)", "301 hp"),
            ("EPA Estimated Fuel Economy", "28/39/32 (LE, SE)", "22/33/26"),
        ),
    ),
)
TEXT = PAGE_BREAK.join(
    [
        "2023 EXAMPLE PRICING\n"
        "- $1,095 for Passenger Cars (Example, Example\n"
        "Hybrid, eX4)\n"
        "- $1,335 for SUVs (eXplorer)",
        "The Example LE makes up to 203 hp and an EPA-estimated 32 combined MPG.",
    ]
)
ROW_LE = (1, 0, "9101")


def _style(**changes: Any) -> StyleClaim:
    style = StyleClaim(
        claim_id="s1",
        style_key="2023-9101",
        model_year=2023,
        model="Example",
        model_code="9101",
        grade="LE",
        powertrain=None,
        body=None,
        engine="4-cyl",
        transmission=None,
        drive=None,
        description="Example LE 4-cyl",
        snapshot_sha256=SNAPSHOT,
        locator=TableCell(1, 0, "9101", "MODEL"),
        quote="Example LE 4-cyl",
        extractor_version="test",
    )
    return replace(style, **changes)


def _value(attribute: str, value_text: str, quote: str, **changes: Any) -> ValueClaim:
    number = evidence.parse_number(value_text)
    value = ValueClaim(
        claim_id="v1",
        style_key=None,
        applies_to={},
        attribute=attribute,
        value_text=value_text,
        value_num=float(number) if number is not None else None,
        unit="USD",
        snapshot_sha256=SNAPSHOT,
        locator=PageHint(None),
        quote=quote,
        extractor_version="test",
        source_class=claim.OEM_PRICING,
    )
    return replace(value, **changes)


def _price(value_text: str = "$28,495", **changes: Any) -> ValueClaim:
    changes.setdefault("locator", TableCell(1, 0, "9101", "MSRP"))
    return _value(claim.MSRP, value_text, changes.pop("quote", value_text), **changes)


# --- styles -------------------------------------------------------------------


def test_check_style_claim_admits_a_row_as_printed() -> None:
    assert verification.check_style_claim(_style(), TABLES).is_admitted


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"grade": "XLE"}, "the grade is not in the description as printed"),
        ({"powertrain": "Hybrid"}, "the powertrain is not in the description as printed"),
        ({"model": "Sample"}, "the model is not in the description as printed"),
        ({"model_code": "9109"}, "the model code is not in the located row"),
        ({"model_year": 2022}, "the model year is not in the located row"),
        (
            {"description": "Example XLE 4-cyl", "quote": "Example XLE 4-cyl"},
            "quote absent from the located cell",
        ),
        (
            {"description": "Example LE", "quote": "Example LE"},
            "the description is not the located cell",
        ),
        ({"locator": PageHint(1)}, "a style needs a table-cell locator"),
    ],
)
def test_check_style_claim_rejects_what_the_row_does_not_print(
    changes: dict[str, Any], reason: str
) -> None:
    """Covers two grade names exchanged between rows: XLE is not in the LE row."""
    decision = verification.check_style_claim(_style(**changes), TABLES)
    assert (decision.is_admitted, decision.reason) == (False, reason)


# --- prices -------------------------------------------------------------------


def test_check_price_claim_admits_the_price_in_its_style_row() -> None:
    assert verification.check_price_claim(_price(), TABLES, {ROW_LE}).is_admitted


@pytest.mark.parametrize(
    ("price", "reason"),
    [
        (_price("$28,945", quote="$28,945"), "quote absent from the located cell"),
        (_price("$28,945", quote="$28,495"), "value absent from its own quote"),
        (_price("$31,250"), "quote absent from the located cell"),
        (
            _price("2023", locator=TableCell(1, 0, "9101", "MY")),
            "the column header does not name a price",
        ),
        (_price(locator=PageHint(1)), "a price needs a table-cell locator"),
        (_price("about $28,495", value_num=None), "the value is not one number"),
    ],
)
def test_check_price_claim_catches_seeded_errors(price: ValueClaim, reason: str) -> None:
    """A swapped digit, the price of the row below, and a number from another column."""
    decision = verification.check_price_claim(price, TABLES, {ROW_LE})
    assert (decision.is_admitted, decision.reason) == (False, reason)


def test_check_price_claim_needs_the_style_of_its_row_admitted() -> None:
    decision = verification.check_price_claim(_price(), TABLES, set())
    assert decision.reason == "the style in the same row was not admitted"


# --- delivery fees ------------------------------------------------------------


def _fee(**changes: Any) -> ValueClaim:
    scope = {"vehicle_class": "Passenger Cars", "models": ["Example", "Example Hybrid", "eX4"]}
    changes.setdefault("applies_to", scope)
    quote = changes.pop("quote", "- $1,095 for Passenger Cars (Example, Example")
    return _value(claim.DELIVERY_FEE, changes.pop("value_text", "$1,095"), quote, **changes)


def test_check_fee_claim_admits_a_fee_whose_models_run_onto_the_next_line() -> None:
    decision = verification.check_fee_claim(_fee(locator=PageHint(1)), TEXT)
    assert decision.is_admitted
    assert isinstance(decision.locator, TextSpan)
    assert TEXT[decision.locator.start : decision.locator.end].startswith("- $1,095 for")


@pytest.mark.parametrize(
    ("fee", "reason"),
    [
        (
            _fee(applies_to={"vehicle_class": "Passenger Cars", "models": ["Example", "Sample"]}),
            "a listed model is not printed with the fee",
        ),
        (
            _fee(applies_to={"vehicle_class": "Large Trucks", "models": []}),
            "the vehicle class is not printed with the fee",
        ),
        (_fee(value_text="$1,335"), "value absent from its own quote"),
        (_fee(quote="- $1,095 for Minivans"), "quote absent from fetched source text"),
        (
            _fee(value_text="2023", quote="2023 EXAMPLE PRICING - $1,095 for Passenger Cars"),
            "the quote does not give the value in dollars",
        ),
    ],
)
def test_check_fee_claim_rejects_a_fee_the_document_does_not_state(
    fee: ValueClaim, reason: str
) -> None:
    decision = verification.check_fee_claim(fee, TEXT.replace("\n", " "))
    assert (decision.is_admitted, decision.reason) == (False, reason)


# --- quantities ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("attribute", "value", "unit", "quote"),
    [
        (claim.HORSEPOWER, "203", "hp", "203 hp @ 6,600 rpm (LE, SE)"),
        (claim.HORSEPOWER, "245", "horsepower", "245 combined net horsepower"),
        (claim.HORSEPOWER, "243", "hp", "Combined net hp 243"),
        (claim.HORSEPOWER, "169", "hp", "the 169-hp 2.0L engine"),
        (claim.HORSEPOWER, "118", "hp", "88 kW (118 hp)"),
        (claim.HORSEPOWER, "88", "kW", "88 kW (118 hp)"),
        (claim.TORQUE, "267", "lb.-ft.", "267 lb.-ft. @ 4,700 rpm"),
        (claim.TORQUE, "175", "lb.-ft", "175 lb.-ft @4400 RPM"),
        (claim.TORQUE, "149", "lb.-ft.", "149/89 lb.-ft."),
        (claim.TORQUE, "89", "lb.-ft.", "149/89 lb.-ft."),
        (claim.TORQUE, "202", "Nm", "202 Nm (149 lb.-ft.)"),
        (claim.TORQUE, "89", "lb. ft.", "121 Nm (89 lb. ft.)"),
        (claim.TORQUE, "310", "lb ft", "310 lb ft of torque"),
        (claim.MPG_CITY, "28", "MPG", "28/39/32 (LE, SE)"),
        (claim.MPG_HIGHWAY, "39", "MPG", "28/39/32 (LE, SE)"),
        (claim.MPG_COMBINED, "35", "MPG", "HV AWD Limited + Platinum = 35 /34/35"),
        (claim.MPG_COMBINED, "32", "MPG", "an EPA-estimated 32 combined MPG"),
        (claim.MPG_HIGHWAY, "41", "MPG", "32 city/41 hwy/35 EPA combined MPG"),
        (claim.MPG_CITY, "131", "MPGe", "131/107/119"),
    ],
)
def test_find_quantity_problem_accepts_a_figure_stated_in_its_own_unit(
    attribute: str, value: str, unit: str, quote: str
) -> None:
    """What the unit rule must still match."""
    assert verification.find_quantity_problem(attribute, value, unit, quote) is None


@pytest.mark.parametrize(
    ("attribute", "value", "unit", "quote", "problem"),
    [
        (claim.HORSEPOWER, "6,600", "hp", "203 hp @ 6,600 rpm", "in the claim's unit"),
        (claim.TORQUE, "4,400", "lb-ft", "182 lb-ft @ 4,400 rpm", "in the claim's unit"),
        (claim.TORQUE, "203", "lb-ft", "203 hp @ 6,600 rpm", "in the claim's unit"),
        (claim.HORSEPOWER, "88", "hp", "88 kW (118 hp)", "in the claim's unit"),
        (claim.HORSEPOWER, "203", "rpm", "203 hp @ 6,600 rpm", "unit does not fit"),
        (claim.MPG_CITY, "39", "MPG", "28/39/32 (LE, SE)", "not that member"),
        (claim.MPG_COMBINED, "28", "MPG", "28/39/32 (LE, SE)", "not that member"),
        (claim.MPG_CITY, "28", "hp", "28/39/32", "unit does not fit"),
        (claim.MPG_CITY, "28", "MPG", "rated at 28 MPG", "does not say which"),
        (claim.HORSEPOWER, "203/206", "hp", "203/206 hp", "not one number"),
    ],
)
def test_find_quantity_problem_rejects_a_number_that_is_some_other_quantity(
    attribute: str, value: str, unit: str, quote: str, problem: str
) -> None:
    found = verification.find_quantity_problem(attribute, value, unit, quote)
    assert found is not None and problem in found


# --- specification figures ----------------------------------------------------


def _figure(attribute: str, value_text: str, quote: str, unit: str, **changes: Any) -> ValueClaim:
    changes.setdefault("source_class", claim.OEM_SPECIFICATION)
    return _value(attribute, value_text, quote, unit=unit, **changes)


def test_check_specification_claim_admits_one_figure_of_a_cell() -> None:
    figure = _figure(
        claim.HORSEPOWER,
        "206",
        "206 hp @ 6,600 rpm (XSE)",
        "hp",
        locator=TableCell(2, 0, "Horsepower", "FOUR-CYLINDER"),
    )
    decision = verification.check_specification_claim(figure, TEXT, TABLES)
    assert decision.is_admitted
    assert decision.locator is None


def test_check_specification_claim_rejects_a_figure_from_the_other_engine_column() -> None:
    figure = _figure(
        claim.HORSEPOWER,
        "206",
        "206 hp @ 6,600 rpm (XSE)",
        "hp",
        locator=TableCell(2, 0, "Horsepower", "V6"),
    )
    decision = verification.check_specification_claim(figure, TEXT, TABLES)
    assert decision.reason == "quote absent from the located cell"


def test_check_specification_claim_gives_a_prose_figure_its_span() -> None:
    figure = _figure(
        claim.MPG_COMBINED,
        "32",
        "an EPA-estimated 32 combined MPG",
        "MPG",
        locator=PageHint(2),
        hedge_flags=("EPA-estimated",),
    )
    decision = verification.check_specification_claim(figure, TEXT, TABLES)
    assert decision.is_admitted
    assert isinstance(decision.locator, TextSpan)
    found = TEXT[decision.locator.start : decision.locator.end]
    assert found == "an EPA-estimated 32 combined MPG"


@pytest.mark.parametrize(
    ("hedge_flags", "is_admitted"),
    [((), False), (("up to",), True), (("Up to 203 hp, per the release",), True)],
)
def test_check_specification_claim_catches_a_hedge_that_was_removed(
    hedge_flags: tuple[str, ...], is_admitted: bool
) -> None:
    figure = _figure(
        claim.HORSEPOWER,
        "203",
        "makes up to 203 hp",
        "hp",
        locator=PageHint(2),
        hedge_flags=hedge_flags,
    )
    decision = verification.check_specification_claim(figure, TEXT, TABLES)
    assert decision.is_admitted is is_admitted
    if not is_admitted:
        assert decision.reason == "the claim leaves out the document's hedge 'up to'"


def test_check_specification_claim_reads_a_hedge_from_the_row_label() -> None:
    locator = TableCell(2, 0, "EPA Estimated Fuel Economy", "FOUR-CYLINDER")
    figure = _figure(claim.MPG_CITY, "28", "28/39/32 (LE, SE)", "MPG", locator=locator)
    bare = verification.check_specification_claim(figure, TEXT, TABLES)
    hedged = verification.check_specification_claim(
        replace(figure, hedge_flags=("Estimated",)), TEXT, TABLES
    )
    assert bare.reason == "the claim leaves out the document's hedge 'estimated'"
    assert hedged.is_admitted


def test_check_specification_claim_does_not_mistake_a_word_for_a_hedge() -> None:
    """`est.` is a hedge; the `est` inside `highest` is not."""
    text = "the highest output is 301 hp"
    figure = _figure(claim.HORSEPOWER, "301", "the highest output is 301 hp", "hp")
    assert verification.check_specification_claim(figure, text, ()).is_admitted


# --- fuel economy file --------------------------------------------------------

FILE_ROW = {"id": "45001", "make": "Toyota", "year": "2023", "city08": "28", "highway08": "39"}


def _economy(attribute: str = claim.MPG_CITY, column: str = "city08", **changes: Any) -> ValueClaim:
    changes.setdefault("applies_to", {"model_year": 2023})
    changes.setdefault("locator", FileRow("45001", column))
    return _value(attribute, changes.pop("value_text", "28"), "28", unit="MPG", **changes)


def test_check_fuel_economy_claim_admits_the_attributes_own_column() -> None:
    assert verification.check_fuel_economy_claim(_economy(), FILE_ROW, "Toyota").is_admitted


@pytest.mark.parametrize(
    ("economy", "row", "reason"),
    [
        (
            _economy(claim.MPG_HIGHWAY, "city08"),
            FILE_ROW,
            "the column is not the attribute's column",
        ),
        (_economy(value_text="39"), FILE_ROW, "value absent from the located field"),
        (_economy(), None, "no row has the key '45001'"),
        (_economy(), {**FILE_ROW, "make": "Honda"}, "the row is for another make"),
        (_economy(), {**FILE_ROW, "year": "2022"}, "the row is for another model year"),
        (_economy(locator=PageHint(1)), FILE_ROW, "a fuel economy figure needs a file-row locator"),
    ],
)
def test_check_fuel_economy_claim_rejects_a_figure_from_the_wrong_place(
    economy: ValueClaim, row: dict[str, str] | None, reason: str
) -> None:
    decision = verification.check_fuel_economy_claim(economy, row, "Toyota")
    assert (decision.is_admitted, decision.reason) == (False, reason)
