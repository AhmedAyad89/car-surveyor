"""Shared fixtures. No test touches the network or the real data directory.

Every document in the tests is synthetic: model names, codes and figures are
made up in the style of Toyota's documents, and nothing here is a source for
a value.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import duckdb
import pytest

from car_surveyor import source, storage
from car_surveyor.config import Settings
from car_surveyor.fetcher import Fetcher
from car_surveyor.source import Registry

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

PdfBuilder = Callable[..., bytes]


@pytest.fixture
def registry() -> Registry:
    return source.load_registry(REPOSITORY_ROOT / "sources.yaml")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", registry_path=REPOSITORY_ROOT / "sources.yaml")


@pytest.fixture
def connection(settings: Settings) -> Iterator[duckdb.DuckDBPyConnection]:
    database = storage.connect(settings.database_path)
    yield database
    database.close()


@pytest.fixture
def pauses() -> list[float]:
    """The pauses the fetcher asked for, recorded instead of slept."""
    return []


@pytest.fixture
def fetcher(registry: Registry, pauses: list[float]) -> Fetcher:
    return Fetcher(registry, "car-surveyor-tests/0", sleep=pauses.append, clock=lambda: 0.0)


@pytest.fixture
def build_pdf() -> PdfBuilder:
    return _build_pdf


def _build_pdf(
    pages: list[list[str]] | None = None, *, table: list[list[str]] | None = None
) -> bytes:
    """A small valid PDF: pages of text lines, then an optional ruled table on its own page."""
    streams = [_text_stream(lines) for lines in pages or []]
    if table is not None:
        streams.append(_table_stream(table))
    page_numbers = [4 + 2 * index for index in range(len(streams))]
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(streams)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for number, stream in zip(page_numbers, streams, strict=True):
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {number + 1} 0 R >>"
            ).encode()
        )
        data = stream.encode("latin-1")
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(data), data))

    output = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    cross_reference_offset = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode()
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{cross_reference_offset}\n%%EOF\n"
    ).encode()
    return bytes(output)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text_stream(lines: list[str]) -> str:
    shown = " T* ".join(f"({_escape(line)}) Tj" for line in lines)
    return f"BT /F1 11 Tf 14 TL 72 740 Td {shown} ET"


def _table_stream(rows: list[list[str]]) -> str:
    row_height, left, top = 20, 60, 720
    # As wide as fits the page, so a four-column table stays on it.
    column_width = min(150, (612 - 2 * left) // max(len(row) for row in rows))
    parts: list[str] = []
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            x = left + column_index * column_width
            y = top - (row_index + 1) * row_height
            parts.append(f"{x} {y} {column_width} {row_height} re S")
            if cell:
                parts.append(f"BT /F1 8 Tf 1 0 0 1 {x + 4} {y + 6} Tm ({_escape(cell)}) Tj ET")
    return "\n".join(parts)
