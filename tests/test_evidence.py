"""Tests for grounding. No network.

The fixtures are synthetic text in the style of a Toyota pricing release. The
model name and the figures are made up; nothing here is a source for a value.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from car_surveyor import evidence
from car_surveyor.document import PAGE_BREAK, Table
from car_surveyor.evidence import FileRow, FoundCell, TableCell, TextSpan

SOURCE = (
    "The 2023 Toyota Example LE has a Manufacturer’s Suggested Retail Price of $28,495. "
    "Pricing excludes the Delivery, Processing and Handling fee of $1,095."
)
QUOTE = "The 2023 Toyota Example LE has a Manufacturer’s Suggested Retail Price of $28,495."


def test_check_grounding_accepts_a_quote_that_is_in_the_source() -> None:
    result = evidence.check_grounding(QUOTE, "$28,495", SOURCE)
    assert result.is_grounded
    assert bool(result)


def test_check_grounding_rejects_a_hallucinated_price() -> None:
    """The failure mode this exists for: a plausible number, no source for it."""
    result = evidence.check_grounding(QUOTE, "$26,495", SOURCE)
    assert not result.is_grounded
    assert result.reason == "value absent from its own quote"


def test_check_grounding_rejects_a_fabricated_quote() -> None:
    fabricated = QUOTE.replace("$28,495", "$26,495")
    result = evidence.check_grounding(fabricated, "$26,495", SOURCE)
    assert not result.is_grounded
    assert result.reason == "quote absent from fetched source text"


def test_check_grounding_rejects_an_empty_quote() -> None:
    assert not evidence.check_grounding("", "$28,495", SOURCE).is_grounded


def test_check_grounding_rejects_an_empty_value() -> None:
    assert not evidence.check_grounding(QUOTE, "  ", SOURCE).is_grounded


def test_check_grounding_tolerates_whitespace_and_typographic_differences() -> None:
    """A model re-typing a quote changes spacing and quote glyphs, not content."""
    retyped = "The 2023 Toyota Example LE   has a Manufacturer's Suggested Retail Price of $28,495."
    assert evidence.check_grounding(retyped, "$28,495", SOURCE).is_grounded


def test_normalize_for_matching_folds_dashes_and_soft_hyphens() -> None:
    folded = evidence.normalize_for_matching("Sug­gested Retail Price – 2023")
    assert folded == "suggested retail price - 2023"


def test_is_quote_present_is_false_for_a_quote_from_another_document() -> None:
    assert not evidence.is_quote_present("The 2023 Toyota Sample XLE starts at $41,250.", SOURCE)


# --- numbers are compared whole ---------------------------------------------


def test_check_grounding_rejects_a_number_that_is_only_part_of_a_larger_one() -> None:
    """The substring bug: 495 used to pass because it is inside $28,495."""
    result = evidence.check_grounding(QUOTE, "495", SOURCE)
    assert not result.is_grounded
    assert result.reason == "value absent from its own quote"


@pytest.mark.parametrize("value", ["$28,495", "28,495", "28495", "$28,495.00", "USD 28,495"])
def test_check_grounding_still_accepts_a_price_however_it_is_written(value: str) -> None:
    """What the tightened comparison must still match."""
    assert evidence.check_grounding(QUOTE, value, SOURCE).is_grounded


@pytest.mark.parametrize("value", ["28", "8,495", "28,49", "284950", "2023.5"])
def test_check_grounding_rejects_fragments_and_near_misses(value: str) -> None:
    assert not evidence.check_grounding(QUOTE, value, SOURCE).is_grounded


@pytest.mark.parametrize(
    ("text", "numbers"),
    [
        ("$28,495", ["28495"]),
        ("203 hp @ 6,600 rpm", ["203", "6600"]),
        ("28/39/32 (LE, SE)", ["28", "39", "32"]),
        ("2.5-liter, 4-cylinder", ["2.5", "4"]),
        ("169-HP engine", ["169"]),
        ("priced at $28,495, and $1,095.", ["28495", "1095"]),
        ("2023,2024", ["2023", "2024"]),
        ("no figures here", []),
    ],
)
def test_list_numbers_reads_each_number_whole(text: str, numbers: list[str]) -> None:
    assert evidence.list_numbers(text) == [Decimal(number) for number in numbers]


def test_parse_number_is_none_when_a_value_states_several_numbers() -> None:
    assert evidence.parse_number("$28,495") == Decimal("28495")
    assert evidence.parse_number("28/39/32") is None
    assert evidence.parse_number("LE") is None


def test_is_value_present_matches_words_whole() -> None:
    assert evidence.is_value_present("XLE", "the XLE grade")
    assert not evidence.is_value_present("LE", "the XLE grade")
    assert evidence.is_value_present("28/39/32", "rated 28/39/32 (LE, SE)")


# --- the location is returned -----------------------------------------------


def test_check_grounding_returns_where_the_quote_was_found() -> None:
    retyped = "has a  Manufacturer's Suggested Retail Price of $28,495"
    result = evidence.check_grounding(retyped, "$28,495", SOURCE)
    assert result.span is not None
    found = SOURCE[result.span.start : result.span.end]
    assert found == "has a Manufacturer’s Suggested Retail Price of $28,495"


def test_find_quote_maps_back_through_folded_whitespace_and_soft_hyphens() -> None:
    source_text = "MSRP\n\n  for the  Ex­ample   LE:\t$28,495"
    span = evidence.find_quote("for the example le: $28,495", source_text)
    assert span is not None
    assert source_text[span.start : span.end] == "for the  Ex­ample   LE:\t$28,495"


def test_find_quote_looks_on_the_named_page_first() -> None:
    pages = ["Example LE $28,495", "Example LE $28,495 effective January"]
    source_text = PAGE_BREAK.join(pages)
    first = evidence.find_quote("Example LE $28,495", source_text)
    second = evidence.find_quote("Example LE $28,495", source_text, page=2)
    assert first == TextSpan(0, 18)
    assert second == TextSpan(19, 37)


def test_find_quote_still_finds_a_quote_whose_page_number_is_wrong() -> None:
    source_text = PAGE_BREAK.join(["first page", "Example LE $28,495"])
    assert evidence.find_quote("Example LE $28,495", source_text, page=1) == TextSpan(11, 29)
    assert evidence.find_quote("Example XLE $31,250", source_text, page=1) is None


# --- table cells -------------------------------------------------------------

PRICE_TABLES = (
    Table(
        1,
        0,
        (
            ("MODEL #", "MY", "MODEL", "MSRP"),
            ("9101", "2023", "Example LE", "$28,495"),
            ("9102", "2023", "Example XLE", "$31,250"),
            ("9103", "2023", "Example Limited", "$35,900"),
            ("9103", "2023", "Example Platinum", "$38,400"),
        ),
    ),
    # Continued on the next page, where the header is not repeated.
    Table(2, 0, (("9110", "2023", "Example Hybrid LE", "$29,875"),)),
    Table(2, 1, (("Class", "Fee"), ("Passenger Cars", "$1,095"))),
)
SPECIFICATION_TABLES = (
    Table(
        1,
        0,
        (
            ("ENGINE", "FOUR-CYLINDER", "V6"),
            ("Horsepower", "203 hp @ 6,600 rpm (LE, SE) 206 hp @ 6,600 rpm (XSE)", "301 hp"),
        ),
    ),
)


def test_check_cell_grounding_accepts_a_value_in_the_cell_the_locator_names() -> None:
    locator = TableCell(page=1, table=0, row_label="9101", column_header="MSRP")
    result = evidence.check_cell_grounding("$28,495", "28495", PRICE_TABLES, locator)
    assert result.is_grounded
    assert result.cell is not None
    assert result.cell.row == ("9101", "2023", "Example LE", "$28,495")
    assert result.cell.header == ("MODEL #", "MY", "MODEL", "MSRP")
    assert (result.cell.row_index, result.cell.column_index) == (1, 3)


def test_check_cell_grounding_rejects_a_price_quoted_from_the_row_below() -> None:
    """The shifted-row error: a real number from the document, in the wrong row."""
    locator = TableCell(page=1, table=0, row_label="9101", column_header="MSRP")
    result = evidence.check_cell_grounding("$31,250", "$31,250", PRICE_TABLES, locator)
    assert not result.is_grounded
    assert result.reason == "quote absent from the located cell"


def test_check_cell_grounding_rejects_a_value_from_the_wrong_column() -> None:
    locator = TableCell(page=1, table=0, row_label="9101", column_header="MY")
    result = evidence.check_cell_grounding("$28,495", "$28,495", PRICE_TABLES, locator)
    assert not result.is_grounded


def test_find_cell_takes_the_header_of_the_table_a_continued_table_continues() -> None:
    locator = TableCell(page=2, table=0, row_label="9110", column_header="MSRP")
    found = evidence.find_cell(PRICE_TABLES, locator)
    assert isinstance(found, FoundCell)
    assert found.text == "$29,875"
    assert found.header == ("MODEL #", "MY", "MODEL", "MSRP")


def test_find_cell_does_not_borrow_a_header_from_a_table_of_another_width() -> None:
    locator = TableCell(page=2, table=1, row_label="Passenger Cars", column_header="MSRP")
    assert evidence.find_cell(PRICE_TABLES, locator) == "no column is headed 'MSRP'"


def test_find_cell_refuses_a_label_that_fits_two_rows() -> None:
    """A document can print one model code on two rows; the code then names neither."""
    by_code = TableCell(page=1, table=0, row_label="9103", column_header="MSRP")
    assert evidence.find_cell(PRICE_TABLES, by_code) == "2 rows are labelled '9103'"
    by_description = TableCell(1, 0, "Example Platinum", "MSRP")
    found = evidence.find_cell(PRICE_TABLES, by_description)
    assert isinstance(found, FoundCell)
    assert found.text == "$38,400"


@pytest.mark.parametrize(
    ("locator", "reason"),
    [
        (TableCell(3, 0, "9101", "MSRP"), "no table 0 on page 3"),
        (TableCell(1, 0, "9199", "MSRP"), "no row is labelled '9199'"),
        (TableCell(1, 0, "9101", "Price"), "no column is headed 'Price'"),
        (TableCell(1, 0, "MSRP", "MSRP"), "no row is labelled 'MSRP'"),
    ],
)
def test_find_cell_says_why_a_cell_was_not_found(locator: TableCell, reason: str) -> None:
    assert evidence.find_cell(PRICE_TABLES, locator) == reason


def test_check_cell_grounding_finds_one_of_several_values_in_a_cell() -> None:
    """A specification cell holds a value per group of grades."""
    locator = TableCell(1, 0, "Horsepower", "FOUR-CYLINDER")
    quote = "206 hp @ 6,600 rpm (XSE)"
    assert evidence.check_cell_grounding(quote, "206", SPECIFICATION_TABLES, locator).is_grounded
    wrong_engine = TableCell(1, 0, "Horsepower", "V6")
    assert not evidence.check_cell_grounding(quote, "206", SPECIFICATION_TABLES, wrong_engine)


# --- file rows ---------------------------------------------------------------

FUEL_ECONOMY_ROW = {"id": "45001", "make": "Toyota", "city08": "28", "highway08": "39"}


def test_check_row_grounding_accepts_the_value_of_the_named_field() -> None:
    locator = FileRow(row_key="45001", column="city08")
    assert evidence.check_row_grounding("28", FUEL_ECONOMY_ROW, locator).is_grounded


def test_check_row_grounding_rejects_a_value_from_another_field() -> None:
    locator = FileRow(row_key="45001", column="city08")
    result = evidence.check_row_grounding("39", FUEL_ECONOMY_ROW, locator)
    assert result.reason == "value absent from the located field"


def test_check_row_grounding_rejects_a_missing_row_or_column() -> None:
    assert not evidence.check_row_grounding("28", None, FileRow("99999", "city08"))
    assert not evidence.check_row_grounding("28", FUEL_ECONOMY_ROW, FileRow("45001", "mpg"))
