"""Tests for deriving text and tables from raw bytes. No network."""

from __future__ import annotations

import io
import zipfile

import pytest
from conftest import PdfBuilder

from car_surveyor import document
from car_surveyor.document import Box, Table, Word


def test_merge_sparse_columns_joins_a_header_with_the_values_one_column_over() -> None:
    """The shape a merged-cell price table has when read from a PDF."""
    rows = [
        ["MODEL #", "", "", "MY", "", "MODEL", "", "MSRP", ""],
        ["", "9101", "", "2023", "", "Example LE", "", "$28,495", ""],
        ["", "9102", "", "2023", "", "Example XLE", "", "$31,250", ""],
    ]
    assert document.merge_sparse_columns(rows) == (
        ("MODEL #", "MY", "MODEL", "MSRP"),
        ("9101", "2023", "Example LE", "$28,495"),
        ("9102", "2023", "Example XLE", "$31,250"),
    )


def test_merge_sparse_columns_keeps_a_row_with_a_missing_value_aligned() -> None:
    rows = [
        ["MODEL #", "", "MY", "MODEL", "MSRP"],
        ["", "9101", "2023", "Example LE", "$28,495"],
        ["", "", "2023", "Example EV", "$42,000"],
    ]
    merged = document.merge_sparse_columns(rows)
    assert merged[2] == ("", "2023", "Example EV", "$42,000")


def test_merge_sparse_columns_drops_empty_rows_and_pads_short_ones() -> None:
    assert document.merge_sparse_columns([["a", "b"], ["", ""], ["c"]]) == (("a", "b"), ("c", ""))
    assert document.merge_sparse_columns([]) == ()


def test_read_grid_places_words_under_the_header_when_a_row_is_one_wide_cell() -> None:
    """A specification table rules its columns in the header row only."""
    header: list[Box] = [(72, 136, 218, 153), (218, 136, 388, 153), (389, 136, 540, 153)]
    body: list[Box] = [(72, 153, 540, 216)]
    words = [
        Word("ENGINE", 76, 110, 140, 150),
        Word("FOUR-CYLINDER", 222, 300, 140, 150),
        Word("V6", 392, 402, 140, 150),
        Word("Horsepower", 76, 130, 160, 170),
        Word("203", 222, 240, 160, 170),
        Word("hp", 244, 255, 160, 170),
        Word("301", 392, 410, 160, 170),
        Word("hp", 414, 425, 160, 170),
        Word("(LE)", 222, 240, 175, 185),
        Word("Footnote", 76, 120, 230, 240),
    ]
    boundaries = document.get_column_boundaries([header, body])
    assert boundaries == [72, 218, 388, 540]
    assert document.read_grid([header, body], words, boundaries) == [
        ["ENGINE", "FOUR-CYLINDER", "V6"],
        ["Horsepower", "203 hp (LE)", "301 hp"],
    ]


def test_read_grid_splits_a_continued_table_by_the_boundaries_it_is_given() -> None:
    """A table continued on the next page has no header row to give it columns."""
    body: list[Box] = [(72, 80, 540, 120)]
    words = [Word("Torque", 76, 110, 90, 100), Word("184", 222, 240, 90, 100)]
    assert document.get_column_boundaries([body]) == [72, 540]
    assert document.read_grid([body], words, [72, 218, 388, 540]) == [["Torque", "184", ""]]


def test_derive_pdf_reads_text_page_by_page(build_pdf: PdfBuilder) -> None:
    raw = build_pdf([["2023 EXAMPLE PRICING", "Last updated 12/22/2022"], ["Second page"]])
    derived = document.derive_document(raw, document.PDF)
    assert document.get_page_text(derived.text, 1).splitlines() == [
        "2023 EXAMPLE PRICING",
        "Last updated 12/22/2022",
    ]
    assert document.get_page_text(derived.text, 2) == "Second page"
    with pytest.raises(IndexError):
        document.get_page_text(derived.text, 3)


def test_derive_pdf_reads_a_ruled_table_as_a_table(build_pdf: PdfBuilder) -> None:
    rows = [["MODEL #", "MODEL", "MSRP"], ["9101", "Example LE", "$28,495"]]
    derived = document.derive_document(build_pdf(table=rows), document.PDF)
    assert derived.tables == (
        Table(1, 0, (("MODEL #", "MODEL", "MSRP"), ("9101", "Example LE", "$28,495"))),
    )


def test_derive_document_recognises_a_pdf_by_its_bytes(build_pdf: PdfBuilder) -> None:
    derived = document.derive_document(build_pdf([["Example"]]), "application/octet-stream")
    assert derived.text == "Example"


def test_derive_html_keeps_visible_text_and_skips_scripts() -> None:
    raw = (
        b"<html><head><style>p {color: red}</style><script>var price = 1;</script></head>"
        b"<body><h1>2023  Example</h1><p>The LE starts at&nbsp;$28,495.</p></body></html>"
    )
    derived = document.derive_document(raw, document.HTML)
    assert derived.text.splitlines() == ["2023 Example", "The LE starts at $28,495."]
    assert derived.tables == ()


def test_derive_html_reads_tables() -> None:
    raw = (
        b"<table><tr><th>Grade</th><th>MSRP</th></tr><tr><td>LE</td><td> $28,495 </td></tr></table>"
    )
    derived = document.derive_document(raw, document.HTML)
    assert derived.tables == (Table(1, 0, (("Grade", "MSRP"), ("LE", "$28,495"))),)
    assert "$28,495" in derived.text


def test_derive_document_reads_the_file_inside_an_archive() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("vehicles.csv", "id,make,model\n1,Toyota,Example\n")
    derived = document.derive_document(buffer.getvalue(), document.ZIP)
    assert derived.text.splitlines()[1] == "1,Toyota,Example"


def test_a_file_cut_short_is_unreadable_not_a_crash(build_pdf: PdfBuilder) -> None:
    whole = build_pdf([["2023 Example LE"]])
    with pytest.raises(document.UnreadableDocumentError, match="not a readable PDF"):
        document.derive_document(whole[:60], document.PDF)
    with pytest.raises(document.UnreadableDocumentError, match="not a readable archive"):
        document.derive_document(b"PK\x03\x04 cut short", document.ZIP)
    assert document.derive_document(whole, document.PDF).text == "2023 Example LE"


def test_tables_survive_serialization() -> None:
    tables = (Table(2, 1, (("MODEL #", "MSRP"), ("9101", "$28,495"))),)
    assert document.parse_tables(document.serialize_tables(tables)) == tables
