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
    # Keyed on the address: two addresses can serve the same bytes, and then
    # share one hash and one set of files.
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        sha256 TEXT NOT NULL,
        source_id TEXT NOT NULL,
        url TEXT PRIMARY KEY,
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
    # One row per snapshot and extractor version: the extractor's answer as it
    # was returned. A stage that finds a row here makes no model call.
    """
    CREATE TABLE IF NOT EXISTS extractions (
        snapshot_sha256 TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        status TEXT NOT NULL,
        response TEXT NOT NULL,
        detail TEXT NOT NULL,
        input_tokens BIGINT NOT NULL,
        output_tokens BIGINT NOT NULL,
        cost_usd DOUBLE NOT NULL,
        extracted_at TIMESTAMP NOT NULL,
        PRIMARY KEY (snapshot_sha256, extractor_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS style_claims (
        claim_id TEXT PRIMARY KEY,
        style_key TEXT NOT NULL,
        model_year INTEGER NOT NULL,
        model TEXT NOT NULL,
        model_code TEXT,
        grade TEXT,
        powertrain TEXT,
        body TEXT,
        engine TEXT,
        transmission TEXT,
        drive TEXT,
        description TEXT NOT NULL,
        snapshot_sha256 TEXT NOT NULL,
        locator TEXT NOT NULL,
        quote TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        status TEXT NOT NULL,
        reject_reason TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS value_claims (
        claim_id TEXT PRIMARY KEY,
        style_key TEXT,
        applies_to TEXT NOT NULL,
        attribute TEXT NOT NULL,
        value_text TEXT NOT NULL,
        value_num DOUBLE,
        unit TEXT NOT NULL,
        qualifiers TEXT NOT NULL,
        hedge_flags TEXT NOT NULL,
        effective_date DATE,
        source_class TEXT NOT NULL,
        snapshot_sha256 TEXT NOT NULL,
        locator TEXT NOT NULL,
        quote TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        status TEXT NOT NULL,
        reject_reason TEXT NOT NULL
    )
    """,
    # The resolve stage rebuilds the two tables below from the admitted
    # claims on every run. The views over them are in sql/views.sql.
    """
    CREATE TABLE IF NOT EXISTS style_records (
        style_key TEXT PRIMARY KEY,
        model_year INTEGER NOT NULL,
        model TEXT NOT NULL,
        model_code TEXT,
        grade TEXT,
        powertrain TEXT,
        body TEXT,
        engine TEXT,
        transmission TEXT,
        drive TEXT,
        description TEXT NOT NULL,
        drive_class TEXT,
        is_drive_inferred BOOLEAN NOT NULL,
        transmission_class TEXT,
        is_transmission_inferred BOOLEAN NOT NULL,
        document_count INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS style_values (
        style_key TEXT NOT NULL,
        attribute TEXT NOT NULL,
        claim_id TEXT NOT NULL,
        value_num DOUBLE NOT NULL,
        value_text TEXT NOT NULL,
        unit TEXT NOT NULL,
        effective_date DATE,
        source_class TEXT NOT NULL,
        snapshot_sha256 TEXT NOT NULL,
        specificity INTEGER NOT NULL,
        note TEXT NOT NULL,
        is_chosen BOOLEAN NOT NULL,
        is_conflicting BOOLEAN NOT NULL
    )
    """,
    # What the consistency checks found, rebuilt with the two tables above.
    """
    CREATE TABLE IF NOT EXISTS check_results (
        check_name TEXT NOT NULL,
        style_key TEXT NOT NULL,
        attribute TEXT,
        detail TEXT NOT NULL
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
    rekey_snapshots(connection)


def rekey_snapshots(connection: duckdb.DuckDBPyConnection) -> None:
    """Bring a file whose `snapshots` table is keyed on the hash up to date.

    The table was first keyed on the content hash, which broke when two
    addresses served identical bytes. Its rows are copied into a table keyed
    on the address.
    """
    key = connection.execute(
        "SELECT constraint_column_names FROM duckdb_constraints() "
        "WHERE table_name = 'snapshots' AND constraint_type = 'PRIMARY KEY'"
    ).fetchone()
    if key is None or list(key[0]) == ["url"]:
        return
    statement = next(text for text in SCHEMA_STATEMENTS if "EXISTS snapshots (" in text)
    connection.execute(statement.replace("EXISTS snapshots (", "EXISTS snapshots_by_url ("))
    connection.execute("INSERT INTO snapshots_by_url SELECT * FROM snapshots")
    connection.execute("DROP TABLE snapshots")
    connection.execute("ALTER TABLE snapshots_by_url RENAME TO snapshots")


def fetch_records(
    connection: duckdb.DuckDBPyConnection, sql: str, parameters: list[Any] | None = None
) -> list[dict[str, Any]]:
    """Run a query and return its rows as dictionaries keyed by column name."""
    cursor = connection.execute(sql, parameters or [])
    columns = [column[0] for column in cursor.description or []]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
