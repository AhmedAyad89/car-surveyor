"""Tests for the snapshot store and the snapshot stage. No network."""

from __future__ import annotations

import hashlib
import json
from datetime import date

import duckdb
import httpx
import pytest
import respx
from conftest import PdfBuilder

from car_surveyor import snapshot, source
from car_surveyor.config import Settings
from car_surveyor.fetcher import Fetcher
from car_surveyor.source import Candidate, Registry

RELEASE = "https://pressroom.toyota.com/2023-example-release/"
PRICING_PDF = (
    "https://toyota-cms-media.s3.amazonaws.com/wp-content/uploads/2022/09/2022-2023-Pricing.pdf"
)
AVAILABILITY = "https://archive.org/wayback/available"
CAPTURE = f"https://web.archive.org/web/20221224052141id_/{PRICING_PDF}"


def _availability(timestamp: str | None) -> httpx.Response:
    closest = {"available": True, "status": "200", "timestamp": timestamp} if timestamp else {}
    return httpx.Response(200, content=json.dumps({"archived_snapshots": {"closest": closest}}))


def _record_pricing_candidate(connection: duckdb.DuckDBPyConnection) -> None:
    source.record_candidate(
        connection, Candidate(PRICING_PDF, "toyota_media", source.PRICING_PDF, (2022, 2023), "page")
    )


def test_store_snapshot_keeps_the_bytes_under_their_hash_with_derived_text(
    connection: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    raw = b"<p>The 2023 Example LE starts at $28,495.</p>"
    stored = snapshot.store_snapshot(
        connection,
        settings,
        url=RELEASE,
        source_id="toyota_pressroom",
        kind="release",
        raw=raw,
        media_type="text/html",
        fetch_url=RELEASE,
        route=snapshot.DIRECT,
    )
    assert stored.sha256 == hashlib.sha256(raw).hexdigest()
    assert stored.raw_path.name == f"{stored.sha256}.html"
    assert stored.read_raw() == raw
    assert stored.read_text() == "The 2023 Example LE starts at $28,495."
    assert stored.read_tables() == ()
    assert snapshot.find_snapshot(connection, settings, RELEASE) == stored


def test_a_snapshot_is_found_again_after_the_data_directory_moves(
    connection: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    snapshot.store_snapshot(
        connection,
        settings,
        url=RELEASE,
        source_id="toyota_pressroom",
        kind="release",
        raw=b"<p>moved</p>",
        media_type="text/html",
        fetch_url=RELEASE,
        route=snapshot.DIRECT,
    )
    moved_dir = settings.data_dir.parent / "elsewhere"
    settings.data_dir.rename(moved_dir)
    moved_settings = settings.model_copy(update={"data_dir": moved_dir})
    moved = snapshot.find_snapshot(connection, moved_settings, RELEASE)
    assert moved is not None
    assert moved.read_text() == "moved"


@respx.mock
def test_fetch_snapshot_fetches_an_address_only_once(
    connection: duckdb.DuckDBPyConnection, settings: Settings, fetcher: Fetcher
) -> None:
    respx.get(RELEASE).mock(
        return_value=httpx.Response(
            200, content=b"<p>once</p>", headers={"content-type": "text/html"}
        )
    )
    arguments = {"url": RELEASE, "source_id": "toyota_pressroom", "kind": "release"}
    first = snapshot.fetch_snapshot(connection, settings, fetcher, **arguments)
    second = snapshot.fetch_snapshot(connection, settings, fetcher, **arguments)
    assert first == second
    assert fetcher.request_count == 1


@respx.mock
def test_fetch_snapshot_keeps_nothing_when_the_site_refuses(
    connection: duckdb.DuckDBPyConnection, settings: Settings, fetcher: Fetcher
) -> None:
    respx.get(RELEASE).mock(return_value=httpx.Response(403))
    with pytest.raises(LookupError, match="403"):
        snapshot.fetch_snapshot(
            connection, settings, fetcher, url=RELEASE, source_id="toyota_pressroom", kind="release"
        )
    assert snapshot.list_snapshots(connection, settings) == []


@respx.mock
def test_snapshot_documents_takes_a_refused_document_from_its_wayback_capture(
    connection: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
    fetcher: Fetcher,
    build_pdf: PdfBuilder,
) -> None:
    _record_pricing_candidate(connection)
    respx.get(AVAILABILITY).mock(return_value=_availability("20221224052141"))
    respx.get(CAPTURE).mock(return_value=httpx.Response(200, content=build_pdf([["Pricing"]])))

    report = snapshot.snapshot_documents(connection, registry, settings, fetcher)

    assert report.fetched == [PRICING_PDF]
    stored = snapshot.find_snapshot(connection, settings, PRICING_PDF)
    assert stored is not None
    assert (stored.route, stored.fetch_url) == (snapshot.WAYBACK, CAPTURE)
    assert stored.capture_date == date(2022, 12, 24)
    assert stored.media_type == "application/pdf"
    assert stored.read_text() == "Pricing"


@respx.mock
def test_a_second_run_makes_no_requests(
    connection: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
    fetcher: Fetcher,
    build_pdf: PdfBuilder,
) -> None:
    """The S1 gate, for both outcomes: a document held and a document unavailable."""
    _record_pricing_candidate(connection)
    missing = PRICING_PDF.replace("2022-2023", "2023-2024")
    source.record_candidate(
        connection, Candidate(missing, "toyota_media", source.PRICING_PDF, (2023,), "page")
    )
    respx.get(AVAILABILITY, params={"url": PRICING_PDF}).mock(
        return_value=_availability("20221224052141")
    )
    respx.get(AVAILABILITY, params={"url": missing}).mock(return_value=_availability(None))
    respx.get(CAPTURE).mock(return_value=httpx.Response(200, content=build_pdf([["Pricing"]])))

    first = snapshot.snapshot_documents(connection, registry, settings, fetcher)
    requests_after_first_run = fetcher.request_count
    second = snapshot.snapshot_documents(connection, registry, settings, fetcher)

    assert (first.fetched, first.unavailable) == ([PRICING_PDF], [missing])
    assert (second.already_held, second.unavailable) == ([PRICING_PDF], [missing])
    assert fetcher.request_count == requests_after_first_run == 3
    unavailable = source.find_candidate(connection, missing)
    assert unavailable is not None
    assert unavailable.status == "unavailable"
    assert "download it by hand" in unavailable.detail


@respx.mock
def test_snapshot_documents_takes_a_hand_downloaded_file_from_the_inbox(
    connection: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
    fetcher: Fetcher,
    build_pdf: PdfBuilder,
) -> None:
    _record_pricing_candidate(connection)
    source.mark_candidate(connection, PRICING_PDF, "unavailable", "no Wayback capture")
    settings.inbox_dir.mkdir(parents=True)
    (settings.inbox_dir / "2022-2023-Pricing.pdf").write_bytes(build_pdf([["By hand"]]))

    report = snapshot.snapshot_documents(connection, registry, settings, fetcher)

    assert report.fetched == [PRICING_PDF]
    assert fetcher.request_count == 0
    stored = snapshot.find_snapshot(connection, settings, PRICING_PDF)
    assert stored is not None
    assert stored.route == snapshot.MANUAL
    assert stored.capture_date is None
    assert stored.read_text() == "By hand"


@respx.mock
def test_snapshot_documents_rejects_a_capture_that_is_not_a_pdf(
    connection: duckdb.DuckDBPyConnection, registry: Registry, settings: Settings, fetcher: Fetcher
) -> None:
    _record_pricing_candidate(connection)
    respx.get(AVAILABILITY).mock(return_value=_availability("20221224052141"))
    respx.get(CAPTURE).mock(return_value=httpx.Response(200, content=b"<html>Access Denied</html>"))

    report = snapshot.snapshot_documents(connection, registry, settings, fetcher)

    assert report.unavailable == [PRICING_PDF]
    assert snapshot.list_snapshots(connection, settings) == []


@respx.mock
def test_a_capture_that_was_cut_short_is_recorded_and_the_run_goes_on(
    connection: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
    fetcher: Fetcher,
    build_pdf: PdfBuilder,
) -> None:
    """The archive holds captures that start as a PDF and stop partway."""
    _record_pricing_candidate(connection)
    later = PRICING_PDF.replace("2022-2023", "2023-2024")
    source.record_candidate(
        connection, Candidate(later, "toyota_media", source.PRICING_PDF, (2023, 2024), "page")
    )
    whole = build_pdf([["Pricing"]])
    respx.get(AVAILABILITY).mock(return_value=_availability("20221224052141"))
    respx.get(CAPTURE).mock(return_value=httpx.Response(200, content=whole[:60]))
    respx.get(CAPTURE.replace("2022-2023", "2023-2024")).mock(
        return_value=httpx.Response(200, content=whole)
    )

    report = snapshot.snapshot_documents(connection, registry, settings, fetcher)

    assert report.unavailable == [PRICING_PDF]
    assert report.fetched == [later]
    failed = source.find_candidate(connection, PRICING_PDF)
    assert failed is not None
    assert failed.detail.startswith("the file is not a readable PDF")
    assert [held.url for held in snapshot.list_snapshots(connection, settings)] == [later]
    assert len(list(settings.snapshot_dir.glob("*.pdf"))) == 1


@respx.mock
def test_snapshot_documents_asks_again_only_when_told_to(
    connection: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
    fetcher: Fetcher,
    build_pdf: PdfBuilder,
) -> None:
    _record_pricing_candidate(connection)
    source.mark_candidate(connection, PRICING_PDF, "unavailable", "HTTP 503")
    respx.get(AVAILABILITY).mock(return_value=_availability("20221224052141"))
    respx.get(CAPTURE).mock(return_value=httpx.Response(200, content=build_pdf([["Pricing"]])))

    quiet = snapshot.snapshot_documents(connection, registry, settings, fetcher)
    assert (quiet.unavailable, fetcher.request_count) == ([PRICING_PDF], 0)

    retried = snapshot.snapshot_documents(
        connection, registry, settings, fetcher, is_retrying_unavailable=True
    )
    assert retried.fetched == [PRICING_PDF]


@respx.mock
def test_snapshot_documents_fetches_a_direct_source_directly(
    connection: duckdb.DuckDBPyConnection, registry: Registry, settings: Settings, fetcher: Fetcher
) -> None:
    address = "https://www.fueleconomy.gov/feg/epadata/vehicles.csv.zip"
    source.record_candidate(
        connection, Candidate(address, "fueleconomy", source.FUEL_ECONOMY_FILE, (), "registry")
    )
    respx.get(address).mock(return_value=httpx.Response(500))

    report = snapshot.snapshot_documents(connection, registry, settings, fetcher)

    assert report.unavailable == [address]
    failed = source.find_candidate(connection, address)
    assert failed is not None
    assert failed.detail == "HTTP 500"


def test_rederive_snapshots_rebuilds_what_is_derived_from_the_stored_bytes(
    connection: duckdb.DuckDBPyConnection, settings: Settings, build_pdf: PdfBuilder
) -> None:
    table = [["MODEL #", "MSRP"], ["9101", "$28,495"]]
    stored = snapshot.store_snapshot(
        connection,
        settings,
        url=PRICING_PDF,
        source_id="toyota_media",
        kind=source.PRICING_PDF,
        raw=build_pdf(table=table),
        media_type="application/pdf",
        fetch_url=CAPTURE,
        route=snapshot.WAYBACK,
    )
    derived = stored.read_tables()
    stored.tables_path.write_text("[]", encoding="utf-8")
    stored.text_path.write_text("stale", encoding="utf-8")

    assert snapshot.rederive_snapshots(connection, settings) == 1

    assert stored.read_tables() == derived
    assert "$28,495" in stored.read_text()


def test_two_addresses_that_serve_the_same_bytes_are_both_kept(
    connection: duckdb.DuckDBPyConnection, settings: Settings
) -> None:
    """Toyota links one pricing PDF at two addresses. Both are documents; the bytes are one."""
    raw = b"<p>The same bytes at two addresses.</p>"
    copy = f"{RELEASE}copy/"
    stored = [
        snapshot.store_snapshot(
            connection,
            settings,
            url=url,
            source_id="toyota_pressroom",
            kind="release",
            raw=raw,
            media_type="text/html",
            fetch_url=url,
            route=snapshot.DIRECT,
        )
        for url in (RELEASE, copy)
    ]
    assert stored[0].sha256 == stored[1].sha256
    assert stored[0].raw_path == stored[1].raw_path
    held = snapshot.list_snapshots(connection, settings)
    assert sorted(found.url for found in held) == [RELEASE, copy]
    assert snapshot.find_snapshot(connection, settings, copy) == stored[1]
