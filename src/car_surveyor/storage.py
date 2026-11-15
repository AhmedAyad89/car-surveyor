"""The DuckDB file: its connection and its schema.

Each table's reads and writes live in the module that owns the concept
(`snapshot.py` for `snapshots`, `source.py` for `candidates`). This module
only opens the file and creates the tables, so the schema is in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

# JSON is kept as text and parsed in Python, so the file needs no extension.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS candidates (
        url TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        model_years TEXT NOT NULL,
        discovered_from TEXT NOT NULL,
        link_text TEXT NOT NULL,
        status TEXT NOT NULL,
        detail TEXT NOT NULL,
        discovered_at TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        sha256 TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        fetch_url TEXT NOT NULL,
        route TEXT NOT NULL,
        kind TEXT NOT NULL,
        media_type TEXT NOT NULL,
        raw_path TEXT NOT NULL,
        text_path TEXT NOT NULL,
        tables_path TEXT NOT NULL,
        fetched_at TIMESTAMP NOT NULL,
        capture_date DATE,
        document_date DATE
    )
    """,
)


def connect(database_path: Path, *, is_read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the database file, creating it and its tables when writable."""
    if is_read_only:
        return duckdb.connect(str(database_path), read_only=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(database_path))
    create_schema(connection)
    return connection


def create_schema(connection: duckdb.DuckDBPyConnection) -> None:
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)


def fetch_records(
    connection: duckdb.DuckDBPyConnection, sql: str, parameters: list[Any] | None = None
) -> list[dict[str, Any]]:
    """Run a query and return its rows as dictionaries keyed by column name."""
    cursor = connection.execute(sql, parameters or [])
    columns = [column[0] for column in cursor.description or []]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
