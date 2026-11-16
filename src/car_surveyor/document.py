"""Text and tables derived from a document's raw bytes.

Evidence locations are computed on this text, so the derivation is
deterministic and does only what turning bytes into text requires. Nothing is
removed as boilerplate and nothing is reordered: a cleaned text would move
every location and could drop the sentence a quote came from.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from itertools import pairwise

import pdfplumber
from pdfminer.psexceptions import PSException
from pdfplumber.utils.exceptions import PdfminerException

PDF = "application/pdf"
HTML = "text/html"
ZIP = "application/zip"

# Pages of a PDF are joined with a form feed, so a page's text can be found again.
PAGE_BREAK = "\f"

# Left, top, right, bottom, in PDF points from the page's top-left corner.
type Box = tuple[float, float, float, float]

# Two vertical rules closer than this are one rule drawn twice.
_SAME_EDGE_TOLERANCE = 2.0

_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "ul"}
)
_SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template"})


class UnreadableDocumentError(ValueError):
    """Raised when a file's bytes are not the kind of document they claim to be."""


@dataclass(frozen=True, slots=True)
class Table:
    """One table: where it sits in the document, and its cells as text."""

    page: int  # 1-based; an HTML document is one page
    index: int  # 0-based position among the tables on its page
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class DerivedDocument:
    text: str
    tables: tuple[Table, ...]


@dataclass(frozen=True, slots=True)
class Word:
    """A word on a PDF page and the box it occupies."""

    text: str
    x0: float
    x1: float
    top: float
    bottom: float


def derive_document(raw: bytes, media_type: str) -> DerivedDocument:
    """Derive text and tables from raw bytes, by media type."""
    if media_type == PDF or raw.startswith(b"%PDF"):
        return derive_pdf(raw)
    if media_type == ZIP or raw.startswith(b"PK\x03\x04"):
        return DerivedDocument(read_zip_member(raw), ())
    if media_type == HTML:
        return derive_html(raw)
    return DerivedDocument(decode_text(raw), ())


def derive_pdf(raw: bytes) -> DerivedDocument:
    """Derive a PDF's text and tables. An archive can hold a capture that was cut short."""
    try:
        return _read_pdf(raw)
    except (PdfminerException, PSException) as error:
        raise UnreadableDocumentError(f"the file is not a readable PDF ({error})") from error


def _read_pdf(raw: bytes) -> DerivedDocument:
    page_texts: list[str] = []
    tables: list[Table] = []
    # The column boundaries of the last table that had columns of its own.
    continued_boundaries: list[float] | None = None
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            page_texts.append(page.extract_text() or "")
            words = [
                Word(word["text"], word["x0"], word["x1"], word["top"], word["bottom"])
                for word in page.extract_words()
            ]
            for index, found in enumerate(page.find_tables()):
                row_cells = [[cell for cell in row.cells if cell is not None] for row in found.rows]
                row_cells = [cells for cells in row_cells if cells]
                boundaries = get_column_boundaries(row_cells)
                if len(boundaries) > 2:
                    continued_boundaries = boundaries
                elif continued_boundaries is not None and _has_same_edges(
                    boundaries, continued_boundaries
                ):
                    boundaries = continued_boundaries
                rows = read_grid(row_cells, words, boundaries)
                spans = get_column_spans(row_cells[0], boundaries) if row_cells else []
                tables.append(Table(page_number, index, merge_sparse_columns(rows, spans)))
    return DerivedDocument(PAGE_BREAK.join(page_texts), tuple(tables))


def get_column_spans(cells: list[Box], boundaries: list[float]) -> list[tuple[int, int]]:
    """The first and last column each drawn cell of a row lies across."""
    spans: list[tuple[int, int]] = []
    for box in cells:
        covered = [
            column
            for column, (left, right) in enumerate(pairwise(boundaries))
            if left >= box[0] - _SAME_EDGE_TOLERANCE and right <= box[2] + _SAME_EDGE_TOLERANCE
        ]
        if covered:
            spans.append((covered[0], covered[-1]))
    return spans


def get_column_boundaries(row_cells: list[list[Box]]) -> list[float]:
    """Every vertical boundary any cell of a table has, left to right."""
    edges = sorted({edge for cells in row_cells for box in cells for edge in (box[0], box[2])})
    return [
        edge
        for position, edge in enumerate(edges)
        if position == 0 or edge - edges[position - 1] > _SAME_EDGE_TOLERANCE
    ]


def read_grid(
    row_cells: list[list[Box]], words: list[Word], boundaries: list[float]
) -> list[list[str]]:
    """Read a ruled table as a full grid of text, one string per row and column.

    A row whose rules are drawn only at the table's edges is one wide cell on
    the page, yet its text still sits under the header's columns. Each word is
    therefore placed by where its centre falls, not by the cell that was drawn.
    A table continued on a later page is drawn the same way and has no header
    row, so the caller gives it the boundaries of the table it continues.
    """
    grid: list[list[str]] = []
    for cells in row_cells:
        top = min(box[1] for box in cells)
        bottom = max(box[3] for box in cells)
        columns: list[list[str]] = [[] for _ in boundaries[:-1]]
        for word in words:
            if not top <= (word.top + word.bottom) / 2 < bottom:
                continue
            centre = (word.x0 + word.x1) / 2
            for column, (left, right) in enumerate(pairwise(boundaries)):
                if left <= centre < right:
                    columns[column].append(word.text)
                    break
        grid.append([" ".join(column) for column in columns])
    return grid


def derive_html(raw: bytes) -> DerivedDocument:
    reader = _HtmlReader()
    reader.feed(decode_text(raw))
    lines = (" ".join(line.split()) for line in "".join(reader.text).splitlines())
    text = "\n".join(line for line in lines if line)
    tables = tuple(
        Table(1, index, merge_sparse_columns(rows)) for index, rows in enumerate(reader.tables)
    )
    return DerivedDocument(text, tables)


def _has_same_edges(boundaries: list[float], other: list[float]) -> bool:
    return (
        abs(boundaries[0] - other[0]) <= _SAME_EDGE_TOLERANCE
        and abs(boundaries[-1] - other[-1]) <= _SAME_EDGE_TOLERANCE
    )


def read_zip_member(raw: bytes) -> str:
    """The text of the one file an archive holds."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if len(names) != 1:
                raise UnreadableDocumentError(
                    f"expected an archive of one file, found {len(names)}"
                )
            return decode_text(archive.read(names[0]))
    except zipfile.BadZipFile as error:
        raise UnreadableDocumentError(f"the file is not a readable archive ({error})") from error


def decode_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def clean_cell(cell: str | None) -> str:
    return " ".join((cell or "").split())


def merge_sparse_columns(
    rows: list[list[str]], header_spans: list[tuple[int, int]] | None = None
) -> tuple[tuple[str, ...], ...]:
    """Merge neighbouring columns that never both hold text in the same row.

    A table drawn with merged cells comes out of a PDF with more columns than
    it shows, and a header can land one column to the left of the values under
    it. Two neighbours that are never both filled in one row are one column on
    the page, so they are joined. Columns that are both filled somewhere stay
    apart, which keeps a row with a missing value aligned with its header.

    `header_spans` gives, for each cell drawn in the first row, the first and
    last column it lies across. A header cell drawn across several of the
    columns that are left heads each of them: `i-FORCE` over three columns of
    figures is given to all three, though its letters sit over the middle one.
    A cell drawn across the whole table is a title, and stays where it is.
    """
    if not rows:
        return ()
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    groups: list[list[int]] = []
    for column in range(width):
        is_mergeable = bool(groups) and not any(
            row[column] and any(row[member] for member in groups[-1]) for row in padded
        )
        if is_mergeable:
            groups[-1].append(column)
        else:
            groups.append([column])
    merged = [
        [next((row[column] for column in group if row[column]), "") for group in groups]
        for row in padded
    ]
    for first, last in header_spans or []:
        headed = [
            position
            for position, group in enumerate(groups)
            if first <= group[0] and group[-1] <= last
        ]
        texts = [merged[0][position] for position in headed if merged[0][position]]
        if len(texts) == 1 and 1 < len(headed) < len(groups):
            for position in headed:
                merged[0][position] = texts[0]
    return tuple(tuple(row) for row in merged if any(row))


def get_page_text(text: str, page: int) -> str:
    """The text of one page, 1-based. An unpaged document is its own first page."""
    pages = text.split(PAGE_BREAK)
    if not 1 <= page <= len(pages):
        raise IndexError(f"page {page} of {len(pages)}")
    return pages[page - 1]


def serialize_tables(tables: tuple[Table, ...]) -> str:
    return json.dumps(
        [{"page": table.page, "index": table.index, "rows": table.rows} for table in tables],
        ensure_ascii=False,
    )


def parse_tables(serialized: str) -> tuple[Table, ...]:
    return tuple(
        Table(item["page"], item["index"], tuple(tuple(row) for row in item["rows"]))
        for item in json.loads(serialized)
    )


class _HtmlReader(HTMLParser):
    """Collects visible text and tables in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.tables: list[list[list[str]]] = []
        self._skip_depth = 0
        self._open_tables: list[list[list[str]]] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.text.append("\n")
        if tag == "table":
            self._open_tables.append([])
        elif tag == "tr" and self._open_tables:
            self._open_tables[-1].append([])
        elif tag in ("td", "th") and self._open_tables and self._open_tables[-1]:
            self._cell = []
            self.text.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.text.append("\n")
        if tag in ("td", "th") and self._cell is not None and self._open_tables:
            self._open_tables[-1][-1].append(clean_cell("".join(self._cell)))
            self._cell = None
        elif tag == "table" and self._open_tables:
            self.tables.append(self._open_tables.pop())

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)
