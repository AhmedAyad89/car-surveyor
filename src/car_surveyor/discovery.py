"""Discovery: crawl the listing pages and record every document they link to.

Listing pages are kept as snapshots like any other fetch, so a second run
reads them from disk. Nothing here guesses an address: a candidate exists
because a fetched page linked to it, or because the registry names it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import duckdb
import httpx

from car_surveyor import document, source
from car_surveyor.config import Settings
from car_surveyor.fetcher import Fetcher
from car_surveyor.snapshot import fetch_snapshot
from car_surveyor.source import Candidate, Registry


@dataclass(slots=True)
class DiscoveryReport:
    vehicle_page_count: int = 0
    document_count: int = 0
    failures: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"{self.vehicle_page_count} vehicle pages read, "
            f"{self.document_count} documents recorded, {len(self.failures)} failures"
        )


def discover(
    connection: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
    fetcher: Fetcher,
    *,
    model_years: tuple[int, ...] = (),
    is_retrying_unavailable: bool = False,
) -> DiscoveryReport:
    """Record the registry's documents and everything its listings link to.

    A vehicle page that could not be read is not asked for again unless told
    to, so a second run makes no requests.
    """
    years = model_years or registry.model_years
    report = DiscoveryReport()
    for registered in registry.documents:
        registered_source = registry.find_source(registered.url)
        if registered_source is None:
            raise ValueError(f"no source in the registry owns {registered.url}")
        source.record_candidate(
            connection,
            Candidate(registered.url, registered_source.id, registered.kind, (), "registry"),
        )

    for listing in registry.listings:
        page = _read_listing(connection, registry, settings, fetcher, listing.url, listing.kind)
        if listing.kind == "vehicle_sitemap":
            for address in source.list_sitemap_addresses(page):
                parsed = source.parse_vehicle_page_address(address)
                page_source = registry.find_source(address)
                if parsed is None or page_source is None:
                    continue
                if parsed[0] in registry.model_years:
                    source.record_candidate(
                        connection,
                        Candidate(
                            address, page_source.id, source.VEHICLE_PAGE, (parsed[0],), listing.url
                        ),
                    )
        else:
            for candidate in source.list_document_candidates(page, listing.url, registry):
                source.record_candidate(connection, candidate)

    vehicle_pages = source.list_candidates(
        connection, kinds=(source.VEHICLE_PAGE,), model_years=years
    )
    for vehicle_page in vehicle_pages:
        if vehicle_page.status == "unavailable" and not is_retrying_unavailable:
            continue
        try:
            page = _read_listing(
                connection, registry, settings, fetcher, vehicle_page.url, source.VEHICLE_PAGE
            )
        except (LookupError, httpx.HTTPError) as error:
            detail = str(error) or type(error).__name__
            source.mark_candidate(connection, vehicle_page.url, "unavailable", detail)
            report.failures.append(f"{vehicle_page.url}: {detail}")
            continue
        source.mark_candidate(connection, vehicle_page.url, "fetched")
        report.vehicle_page_count += 1
        for candidate in source.list_document_candidates(
            page, vehicle_page.url, registry, page_model_years=vehicle_page.model_years
        ):
            source.record_candidate(connection, candidate)

    documents = source.list_candidates(connection, model_years=years)
    report.document_count = sum(
        1 for candidate in documents if candidate.kind != source.VEHICLE_PAGE
    )
    return report


def _read_listing(
    connection: duckdb.DuckDBPyConnection,
    registry: Registry,
    settings: Settings,
    fetcher: Fetcher,
    url: str,
    kind: str,
) -> str:
    listing_source = registry.find_source(url)
    if listing_source is None:
        raise ValueError(f"no source in the registry owns {url}")
    snapshot = fetch_snapshot(
        connection, settings, fetcher, url=url, source_id=listing_source.id, kind=kind
    )
    return document.decode_text(snapshot.read_raw())
