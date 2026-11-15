"""Admissibility rule for extracted claims: grounding.

Language models are fluent at producing plausible car prices, which is exactly
the failure mode that matters here — a hallucinated `$28,495` is
indistinguishable from a real one by inspection. The defence is procedural
rather than statistical: **never accept a model-stated number, only a
model-located one.**

The extractor returns a quote and a locator alongside every value. This module
verifies, in plain code and without a model in the loop, that

1. the quote occurs in the fetched document at the place the locator names, and
2. the value occurs inside the quote, compared as a number where it is one.

A record failing either check is rejected, not down-weighted. A locator is one
of three kinds:

- a **text span** for prose: the quote is searched for in the snapshot's text
  and the span it was found at is returned;
- a **table cell** for tables: a page, a table, a row label and a column
  header. A bare number quoted from a price table proves only that the number
  is somewhere in the document, so the cell is looked up by its row and column
  and the quote must sit inside that cell;
- a **file row** for data files: a row key and a column.

This module says where a value is. Whether that row and column are the right
ones for the style and the attribute is decided in `verification.py`, which
knows what the attributes are.

Nothing here does I/O.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from car_surveyor.document import PAGE_BREAK, Table

_WHITESPACE_PATTERN = re.compile(r"\s+")
# A run of digits with its grouping commas and decimals: 28,495 and 2.5 are one
# number each, so 495 is never found inside $28,495.
_NUMBER_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?")
_GROUPED_NUMBER_PATTERN = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")

# Characters that differ between the rendered page and a model's transcription
# of it without changing the text: typographic quotes, dashes, soft hyphens.
# The invisible ones are written as escapes so an editor cannot lose them.
_LOOKALIKES = str.maketrans(
    {
        "\N{NO-BREAK SPACE}": " ",
        "\N{FIGURE SPACE}": " ",
        "\N{NARROW NO-BREAK SPACE}": " ",
        "‘": "'",
        "’": "'",
        "‚": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        "\N{SOFT HYPHEN}": "",
    }
)


@dataclass(frozen=True, slots=True)
class TextSpan:
    """Where a quote sits in a snapshot's text: `text[start:end]`."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class TableCell:
    """A cell named the way a reader would: by its row's label and its column's header."""

    page: int
    table: int
    row_label: str
    column_header: str


@dataclass(frozen=True, slots=True)
class FileRow:
    """A field of a data file: the row's key and the column's name."""

    row_key: str
    column: str


type Locator = TextSpan | TableCell | FileRow


@dataclass(frozen=True, slots=True)
class FoundCell:
    """A table cell that was looked up, with the row and header it sits in."""

    text: str
    row: tuple[str, ...]
    header: tuple[str, ...]
    row_index: int
    column_index: int


@dataclass(frozen=True, slots=True)
class Grounding:
    """Whether one extracted claim is anchored in the document, and where."""

    is_grounded: bool
    reason: str
    span: TextSpan | None = None
    cell: FoundCell | None = None

    def __bool__(self) -> bool:
        return self.is_grounded


def normalize_for_matching(text: str) -> str:
    """Fold a string to the form both sides of a quote comparison are compared in.

    Unicode-normalises, maps typographic lookalikes to ASCII, collapses runs of
    whitespace, and lowercases. This is deliberately lossy: the check is "did the
    model read this sentence", not "did it transcribe every glyph".
    """
    folded = unicodedata.normalize("NFKC", text).translate(_LOOKALIKES)
    return _WHITESPACE_PATTERN.sub(" ", folded).strip().lower()


def list_numbers(text: str) -> list[Decimal]:
    """Every number a text prints, each read whole.

    Commas group thousands only in the pattern `1,234`; any other comma
    separates two numbers.
    """
    numbers: list[Decimal] = []
    for match in _NUMBER_PATTERN.finditer(text):
        token = match.group().rstrip(",")
        if "," in token and not _GROUPED_NUMBER_PATTERN.fullmatch(token):
            parts = token.split(",")
        else:
            parts = [token.replace(",", "")]
        numbers.extend(Decimal(part) for part in parts if part)
    return numbers


def parse_number(text: str) -> Decimal | None:
    """The number a value states, or None when it states none or several."""
    numbers = list_numbers(text)
    return numbers[0] if len(numbers) == 1 else None


def is_value_present(value: str, text: str) -> bool:
    """Whether a text states a value: as a whole number, or else as whole words."""
    number = parse_number(value)
    if number is not None:
        return number in list_numbers(text)
    needle = normalize_for_matching(value)
    if not needle:
        return False
    pattern = rf"(?<!\w){re.escape(needle)}(?!\w)"
    return re.search(pattern, normalize_for_matching(text)) is not None


def is_quote_present(quote: str, source_text: str) -> bool:
    """Whether the verbatim quote occurs in the fetched source text."""
    return find_quote(quote, source_text) is not None


def find_quote(quote: str, source_text: str, *, page: int | None = None) -> TextSpan | None:
    """Where a quote occurs in the source text, as offsets into that text.

    The comparison is made on folded text and the result is mapped back, so
    the span selects the quote as the document printed it. A page narrows the
    search to that page first; a quote found only on another page is still
    found, because a wrong page number does not make a quote fabricated.
    """
    needle = "".join(_fold_with_offsets(quote)[0]).strip()
    if not needle:
        return None
    if page is not None:
        page_start = 0
        for number, page_text in enumerate(source_text.split(PAGE_BREAK), start=1):
            if number == page:
                found = _find_folded(needle, page_text)
                if found is not None:
                    return TextSpan(page_start + found.start, page_start + found.end)
                break
            page_start += len(page_text) + len(PAGE_BREAK)
    return _find_folded(needle, source_text)


def find_cell(tables: Sequence[Table], locator: TableCell) -> FoundCell | str:
    """Look a cell up by row label and column header. Returns the cell, or why not.

    The header is the table's first row. A table continued from an earlier
    page has no header of its own, so the header is then taken from the
    nearest earlier table with the same number of columns. The row label must
    equal one cell of exactly one row: a label that fits two rows names
    neither.
    """
    position = next(
        (
            index
            for index, table in enumerate(tables)
            if (table.page, table.index) == (locator.page, locator.table)
        ),
        None,
    )
    if position is None:
        return f"no table {locator.table} on page {locator.page}"
    table = tables[position]
    if not table.rows:
        return "the table is empty"

    wanted_header = normalize_for_matching(locator.column_header)
    header: tuple[str, ...] | None = None
    for candidate in reversed(tables[: position + 1]):
        if not candidate.rows or len(candidate.rows[0]) != len(table.rows[0]):
            continue
        if wanted_header in [normalize_for_matching(cell) for cell in candidate.rows[0]]:
            header = candidate.rows[0]
            break
    if header is None:
        return f"no column is headed {locator.column_header!r}"
    column_index = [normalize_for_matching(cell) for cell in header].index(wanted_header)

    wanted_label = normalize_for_matching(locator.row_label)
    matches = [
        (row_index, row)
        for row_index, row in enumerate(table.rows)
        if row is not header and wanted_label in [normalize_for_matching(cell) for cell in row]
    ]
    if not matches:
        return f"no row is labelled {locator.row_label!r}"
    if len(matches) > 1:
        return f"{len(matches)} rows are labelled {locator.row_label!r}"
    row_index, row = matches[0]
    return FoundCell(row[column_index], row, header, row_index, column_index)


def check_grounding(
    quote: str, value: str, source_text: str, *, page: int | None = None
) -> Grounding:
    """Verify a value stated in prose against its quote, and the quote against the source.

    Both directions are required. A quote that appears in the source but omits
    the value proves only that the model read *something*; a value inside a quote
    that never appears in the source is a fabrication with a citation attached.
    """
    problem = _check_quote_holds_value(quote, value)
    if problem is not None:
        return Grounding(False, problem)
    span = find_quote(quote, source_text, page=page)
    if span is None:
        return Grounding(False, "quote absent from fetched source text")
    return Grounding(True, "quote and value both present in source", span=span)


def check_cell_grounding(
    quote: str, value: str, tables: Sequence[Table], locator: TableCell
) -> Grounding:
    """Verify a value read from a table: the quote must sit inside the located cell."""
    problem = _check_quote_holds_value(quote, value)
    if problem is not None:
        return Grounding(False, problem)
    found = find_cell(tables, locator)
    if isinstance(found, str):
        return Grounding(False, found)
    if normalize_for_matching(quote) not in normalize_for_matching(found.text):
        return Grounding(False, "quote absent from the located cell", cell=found)
    return Grounding(True, "quote and value both present in the located cell", cell=found)


def check_row_grounding(value: str, row: Mapping[str, str] | None, locator: FileRow) -> Grounding:
    """Verify a value read from a data file against the field the locator names."""
    if not value.strip():
        return Grounding(False, "empty value")
    if row is None:
        return Grounding(False, f"no row has the key {locator.row_key!r}")
    if locator.column not in row:
        return Grounding(False, f"the file has no column {locator.column!r}")
    if not is_value_present(value, row[locator.column]):
        return Grounding(False, "value absent from the located field")
    return Grounding(True, "value present in the located field")


def _check_quote_holds_value(quote: str, value: str) -> str | None:
    if not quote.strip():
        return "empty quote"
    if not value.strip():
        return "empty value"
    if not is_value_present(value, quote):
        return "value absent from its own quote"
    return None


def _find_folded(needle: str, text: str) -> TextSpan | None:
    folded, offsets = _fold_with_offsets(text)
    index = "".join(folded).find(needle)
    if index < 0:
        return None
    return TextSpan(offsets[index], offsets[index + len(needle) - 1] + 1)


def _fold_with_offsets(text: str) -> tuple[list[str], list[int]]:
    """Fold a text as `normalize_for_matching` does, keeping each character's origin.

    Returns the folded characters and, for each, the offset in `text` of the
    character it came from.
    """
    folded: list[str] = []
    offsets: list[int] = []
    for offset, character in enumerate(text):
        for piece in unicodedata.normalize("NFKC", character).translate(_LOOKALIKES).lower():
            if piece.isspace():
                if folded and folded[-1] != " ":
                    folded.append(" ")
                    offsets.append(offset)
            else:
                folded.append(piece)
                offsets.append(offset)
    return folded, offsets
