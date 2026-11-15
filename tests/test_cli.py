"""Tests for the command line's own output. No network."""

from __future__ import annotations

import duckdb
import pytest

from car_surveyor import cli, source
from car_surveyor.config import Settings
from car_surveyor.source import Candidate

MEDIA = "https://toyota-cms-media.s3.amazonaws.com/wp-content/uploads"


def test_status_missing_lists_what_to_download_and_where(
    settings: Settings,
    connection: duckdb.DuckDBPyConnection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = f"{MEDIA}/2022/05/2023-Toyota-Example-Product-Information.pdf"
    held = f"{MEDIA}/2022/09/2022-2023-Toyota-Pricing.pdf"
    page = "https://pressroom.toyota.com/vehicle/2023-toyota-example/"
    for url, kind in (
        (missing, source.PRODUCT_INFORMATION_PDF),
        (held, source.PRICING_PDF),
        (page, source.VEHICLE_PAGE),
    ):
        source.record_candidate(connection, Candidate(url, "toyota_media", kind, (2023,), "page"))
    source.mark_candidate(connection, missing, "unavailable", "no Wayback capture")
    source.mark_candidate(connection, held, "fetched")
    source.mark_candidate(connection, page, "unavailable", "HTTP 404")
    connection.close()

    assert cli.main(["--data-dir", str(settings.data_dir), "status", "--missing"]) == 0

    printed = capsys.readouterr().out
    assert "product_information_pdf    unavailable  1" in printed
    assert f"1 documents to download by hand into {settings.inbox_dir}/:" in printed
    assert f"[2023] product_information_pdf: {missing}" in printed
    assert "no Wayback capture" in printed
    assert held not in printed
    assert f"vehicle_page: {page}" not in printed


def test_status_without_the_flag_prints_counts_only(
    settings: Settings,
    connection: duckdb.DuckDBPyConnection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection.close()
    assert cli.main(["--data-dir", str(settings.data_dir), "status"]) == 0
    assert "download by hand" not in capsys.readouterr().out
