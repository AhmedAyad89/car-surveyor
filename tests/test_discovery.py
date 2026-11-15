"""Tests for the discover stage. No network."""

from __future__ import annotations

import duckdb
import httpx
import respx

from car_surveyor import discovery, source
from car_surveyor.config import Settings
from car_surveyor.fetcher import Fetcher
from car_surveyor.source import Registry

PRESSROOM = "https://pressroom.toyota.com"
MEDIA = "https://toyota-cms-media.s3.amazonaws.com/wp-content/uploads"
SITEMAP = f"""<?xml version="1.0"?>
<urlset>
  <url><loc>{PRESSROOM}/vehicle/2023-toyota-example/</loc></url>
  <url><loc>{PRESSROOM}/vehicle/2021-toyota-example/</loc></url>
  <url><loc>{PRESSROOM}/vehicle/2018-toyota-example/</loc></url>
  <url><loc>{PRESSROOM}/vehicle/</loc></url>
</urlset>"""
PRICING_LISTING = f"""
<a href="{MEDIA}/2023/10/2022-2023-Toyota-Pricing-4.6.2023.pdf">2022 - 2023 Full-Line Pricing</a>
<a href="https://s3.amazonaws.com/toyota-cms-media/toyota-pdfs/2026_and_2027_Pricing.pdf">
  Click here to Download</a>
"""
VEHICLE_PAGE = f"""
<a href="{MEDIA}/2022/05/2023-Toyota-Example-Product-Information.pdf">Download PDF</a>
<a href="{MEDIA}/2022/09/2022-2023-Toyota-Pricing.pdf">Download PDF</a>
"""


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/html"})


def _mock_pressroom() -> None:
    respx.get(f"{PRESSROOM}/vehicle-sitemap.xml").mock(return_value=_html(SITEMAP))
    respx.get(f"{PRESSROOM}/toyota-full-line-pricing/").mock(return_value=_html(PRICING_LISTING))
    respx.get(f"{PRESSROOM}/vehicle/2023-toyota-example/").mock(return_value=_html(VEHICLE_PAGE))
    respx.get(f"{PRESSROOM}/vehicle/2021-toyota-example/").mock(return_value=httpx.Response(404))


@respx.mock
def test_discover_records_every_linked_document(
    connection: duckdb.DuckDBPyConnection, registry: Registry, settings: Settings, fetcher: Fetcher
) -> None:
    _mock_pressroom()

    report = discovery.discover(connection, registry, settings, fetcher)

    found = {candidate.url: candidate for candidate in source.list_candidates(connection)}
    by_kind = sorted((candidate.kind, candidate.model_years) for candidate in found.values())
    assert by_kind == [
        (source.FUEL_ECONOMY_FILE, ()),
        (source.PRICING_PDF, (2022, 2023)),
        (source.PRICING_PDF, (2022, 2023)),
        (source.PRICING_PDF, (2026, 2027)),
        (source.PRODUCT_INFORMATION_PDF, (2023,)),
        (source.VEHICLE_PAGE, (2021,)),
        (source.VEHICLE_PAGE, (2023,)),
    ]
    assert found[f"{PRESSROOM}/vehicle/2023-toyota-example/"].status == "fetched"
    failed = found[f"{PRESSROOM}/vehicle/2021-toyota-example/"]
    assert (failed.status, failed.detail) == ("unavailable", "HTTP 404")
    assert report.vehicle_page_count == 1
    assert len(report.failures) == 1


@respx.mock
def test_discover_reads_only_the_vehicle_pages_of_the_years_asked_for(
    connection: duckdb.DuckDBPyConnection, registry: Registry, settings: Settings, fetcher: Fetcher
) -> None:
    _mock_pressroom()

    report = discovery.discover(connection, registry, settings, fetcher, model_years=(2023,))

    assert report.failures == []
    assert fetcher.request_count == 3
    skipped = source.find_candidate(connection, f"{PRESSROOM}/vehicle/2021-toyota-example/")
    assert skipped is not None
    assert skipped.status == "pending"


@respx.mock
def test_a_second_discovery_makes_no_requests_for_pages_already_read(
    connection: duckdb.DuckDBPyConnection, registry: Registry, settings: Settings, fetcher: Fetcher
) -> None:
    """The S1 gate, for a page that was read and for a page that failed."""
    _mock_pressroom()
    discovery.discover(connection, registry, settings, fetcher)
    candidates_after_first_run = source.list_candidates(connection)

    discovery.discover(connection, registry, settings, fetcher)

    assert fetcher.request_count == 4
    assert source.list_candidates(connection) == candidates_after_first_run


@respx.mock
def test_discover_asks_again_for_a_failed_page_only_when_told_to(
    connection: duckdb.DuckDBPyConnection, registry: Registry, settings: Settings, fetcher: Fetcher
) -> None:
    _mock_pressroom()
    discovery.discover(connection, registry, settings, fetcher)

    discovery.discover(connection, registry, settings, fetcher, is_retrying_unavailable=True)

    assert fetcher.request_count == 5
