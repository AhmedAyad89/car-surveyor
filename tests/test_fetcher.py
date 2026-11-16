"""Tests for the fetcher's address rules and pacing. No network."""

from __future__ import annotations

import httpx
import pytest
import respx

from car_surveyor import fetcher as fetcher_module
from car_surveyor.fetcher import Fetcher, FetchRefusedError, check_address
from car_surveyor.source import Registry

PRICING_PDF = "https://toyota-cms-media.s3.amazonaws.com/wp-content/uploads/2022/09/Pricing.pdf"


@pytest.mark.parametrize(
    "url",
    [
        "https://visor.vin/",
        "https://openspec.visor.vin/data/us/toyota",
        "https://OPENSPEC.VISOR.VIN/",
        "https://deep.sub.visor.vin./x",
    ],
)
def test_check_address_refuses_the_reference_domain(registry: Registry, url: str) -> None:
    with pytest.raises(FetchRefusedError, match="reference domain"):
        check_address(url, registry)


def test_check_address_accepts_a_registry_host(registry: Registry) -> None:
    found = check_address("https://pressroom.toyota.com/vehicle/2023-toyota-example/", registry)
    assert found.id == "toyota_pressroom"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/toyota",
        "https://notvisor.vin.example.org/",
        "https://s3.amazonaws.com/another-owner/file.pdf",
    ],
)
def test_check_address_refuses_a_host_the_registry_does_not_name(
    registry: Registry, url: str
) -> None:
    with pytest.raises(FetchRefusedError):
        check_address(url, registry)


def test_check_address_refuses_plain_http(registry: Registry) -> None:
    with pytest.raises(FetchRefusedError, match="https"):
        check_address("http://pressroom.toyota.com/", registry)


def test_check_address_refuses_the_media_host_that_refuses_this_client(registry: Registry) -> None:
    with pytest.raises(FetchRefusedError, match="not fetched directly"):
        check_address(PRICING_PDF, registry)


@pytest.mark.parametrize(
    "url",
    [
        f"https://web.archive.org/web/20221224052141id_/{PRICING_PDF}",
        f"https://web.archive.org/web/2022/{PRICING_PDF.removeprefix('https://')}",
        "https://archive.org/wayback/available?url=https%3A%2F%2Fpressroom.toyota.com%2Fx%2F",
        "https://web.archive.org/web/2022/https://s3.amazonaws.com/toyota-cms-media/a.pdf",
        f"https://web.archive.org/cdx/search/cdx?url={PRICING_PDF.removeprefix('https://')}",
    ],
)
def test_check_address_accepts_an_archived_toyota_address(registry: Registry, url: str) -> None:
    assert check_address(url, registry).id == "wayback"


@pytest.mark.parametrize(
    "url",
    [
        "https://web.archive.org/web/2026/https://openspec.visor.vin/",
        "https://web.archive.org/web/2026id_/openspec.visor.vin/data",
        "https://web.archive.org/web/2026/https%3A%2F%2Fopenspec.visor.vin%2F",
        "https://archive.org/wayback/available?url=openspec.visor.vin",
        "https://archive.org/wayback/available?url=https%3A%2F%2Fvisor.vin%2F",
        "https://web.archive.org/cdx/search/cdx?url=openspec.visor.vin/data",
    ],
)
def test_check_address_refuses_an_archived_copy_of_the_reference(
    registry: Registry, url: str
) -> None:
    with pytest.raises(FetchRefusedError, match="reference"):
        check_address(url, registry)


@pytest.mark.parametrize(
    "url",
    [
        "https://web.archive.org/web/2026/https://example.com/prices",
        "https://web.archive.org/cdx/search/cdx?url=example.com/prices.pdf",
        "https://web.archive.org/cdx/search/cdx?matchType=domain",
        "https://web.archive.org/",
        f"https://web.archive.org/web/2026/https://web.archive.org/web/2026/{PRICING_PDF}",
    ],
)
def test_check_address_refuses_other_archive_addresses(registry: Registry, url: str) -> None:
    with pytest.raises(FetchRefusedError):
        check_address(url, registry)


@respx.mock
def test_fetch_identifies_itself_and_returns_the_body(fetcher: Fetcher) -> None:
    route = respx.get("https://pressroom.toyota.com/page/").mock(
        return_value=httpx.Response(
            200, content=b"<p>hello</p>", headers={"content-type": "text/html; charset=UTF-8"}
        )
    )
    response = fetcher.fetch("https://pressroom.toyota.com/page/")
    assert response.is_success
    assert response.media_type == "text/html"
    assert response.body == b"<p>hello</p>"
    assert route.calls.last.request.headers["user-agent"] == "car-surveyor-tests/0"
    assert fetcher.request_count == 1


@respx.mock
def test_fetch_waits_out_the_crawl_delay_between_requests_to_one_source(
    fetcher: Fetcher, pauses: list[float]
) -> None:
    respx.get(url__startswith="https://pressroom.toyota.com/").mock(
        return_value=httpx.Response(200)
    )
    fetcher.fetch("https://pressroom.toyota.com/a/")
    assert pauses == []
    fetcher.fetch("https://pressroom.toyota.com/b/")
    assert pauses == [10.0]


@respx.mock
def test_fetch_does_not_make_one_source_wait_for_another(
    fetcher: Fetcher, pauses: list[float]
) -> None:
    respx.get(url__startswith="https://").mock(return_value=httpx.Response(200))
    fetcher.fetch("https://pressroom.toyota.com/a/")
    fetcher.fetch("https://www.fueleconomy.gov/feg/epadata/vehicles.csv.zip")
    assert pauses == []


@respx.mock
def test_fetch_refuses_a_redirect_that_leaves_the_registry(fetcher: Fetcher) -> None:
    respx.get("https://pressroom.toyota.com/moved/").mock(
        return_value=httpx.Response(302, headers={"location": "https://openspec.visor.vin/"})
    )
    with pytest.raises(FetchRefusedError, match="reference domain"):
        fetcher.fetch("https://pressroom.toyota.com/moved/")
    assert fetcher.request_count == 1


@respx.mock
def test_fetch_follows_a_redirect_that_stays_in_the_registry(fetcher: Fetcher) -> None:
    respx.get("https://pressroom.toyota.com/old/").mock(
        return_value=httpx.Response(301, headers={"location": "/new/"})
    )
    respx.get("https://pressroom.toyota.com/new/").mock(
        return_value=httpx.Response(200, content=b"moved here")
    )
    response = fetcher.fetch("https://pressroom.toyota.com/old/")
    assert response.url == "https://pressroom.toyota.com/new/"
    assert response.body == b"moved here"


@respx.mock
def test_fetch_pauses_once_when_the_archive_answers_429(
    fetcher: Fetcher, pauses: list[float]
) -> None:
    address = "https://archive.org/wayback/available?url=pressroom.toyota.com%2Fx%2F"
    respx.get(address).mock(side_effect=[httpx.Response(429), httpx.Response(200, content=b"{}")])
    response = fetcher.fetch(address)
    assert response.is_success
    assert fetcher_module.RATE_LIMIT_PAUSE_SECONDS in pauses
