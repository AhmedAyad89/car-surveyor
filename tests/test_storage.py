"""Tests for the database file's schema and its one migration."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb

from car_surveyor import storage

# The `snapshots` table as the first pipeline PR created it.
KEYED_ON_HASH = """
    CREATE TABLE snapshots (
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
"""
INSERT = (
    "INSERT INTO snapshots VALUES (?, 's', ?, ?, 'direct', 'k', 'm', 'r', 't', 'j', ?, NULL, NULL)"
)
FETCHED_AT = datetime(2026, 11, 15, 12)


def _get_key(connection: duckdb.DuckDBPyConnection) -> list[str]:
    found = connection.execute(
        "SELECT constraint_column_names FROM duckdb_constraints() "
        "WHERE table_name = 'snapshots' AND constraint_type = 'PRIMARY KEY'"
    ).fetchone()
    assert found is not None
    return list(found[0])


def test_a_new_file_keys_snapshots_on_the_address(tmp_path: Path) -> None:
    connection = storage.connect(tmp_path / "new.duckdb")
    assert _get_key(connection) == ["url"]
    connection.close()


def test_a_file_keyed_on_the_hash_is_rekeyed_and_keeps_its_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "old.duckdb"
    old = duckdb.connect(str(database_path))
    old.execute(KEYED_ON_HASH)
    old.execute(INSERT, ["hash-1", "https://example.test/a.pdf", "fetch-a", FETCHED_AT])
    old.close()

    connection = storage.connect(database_path)

    assert _get_key(connection) == ["url"]
    rows = storage.fetch_records(connection, "SELECT sha256, url, fetch_url FROM snapshots")
    assert rows == [
        {"sha256": "hash-1", "url": "https://example.test/a.pdf", "fetch_url": "fetch-a"}
    ]
    connection.execute(INSERT, ["hash-1", "https://example.test/b.pdf", "fetch-b", FETCHED_AT])
    assert connection.execute("SELECT count(*) FROM snapshots").fetchone() == (2,)
    connection.close()


def test_opening_a_file_twice_rekeys_it_only_once(tmp_path: Path) -> None:
    database_path = tmp_path / "twice.duckdb"
    first = storage.connect(database_path)
    first.execute(INSERT, ["hash-1", "https://example.test/a.pdf", "fetch-a", FETCHED_AT])
    first.close()
    second = storage.connect(database_path)
    assert second.execute("SELECT count(*) FROM snapshots").fetchone() == (1,)
    assert _get_key(second) == ["url"]
    second.close()
