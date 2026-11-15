"""Tests for the source registry and candidate records. No network."""

from __future__ import annotations

import duckdb
import pytest

from car_surveyor import source
from car_surveyor.source import Candidate, Registry

MEDIA = "https://toyota-cms-media.s3.amazonaws.com/wp-content/uploads"
VEHICLE_PAGE = "https://pressroom.toyota.com/vehicle/2023-toyota-example/"
VEHICLE_PAGE_HTML = f"""
<html><body>
  <a href="/2023-example-release/">2023 Example Keeps Leading</a>
  <a href="{MEDIA}/2022/05/C_001.jpg">Download high-resolution</a>
  <a href="{MEDIA}/2022/05/2023-Toyota-Example-Product-Information.pdf">Download PDF</a>
  <a href="{MEDIA}/2022/09/2022-2023-Toyota-Pricing.pdf">Download   PDF</a>
  <a href="{MEDIA}/2022/09/2022-2023-Toyota-Pricing.pdf">Download PDF again</a>
  <a href="https://example.com/2023-Toyota-Example-Product-Information.pdf">Mirror</a>
</body></html>
"""


def test_load_registry_reads_the_scope_and_the_sources(registry: Registry) -> None:
    assert registry.make == "Toyota"
    assert registry.model_years == (2020, 2021, 2022, 2023, 2024, 2025)
    assert registry.development_year in registry.model_years
    assert registry.get_source("toyota_media").route == "archive"


def test_find_source_counts_a_shared_host_only_under_its_path_prefix(registry: Registry) -> None:
    owned = registry.find_source("https://s3.amazonaws.com/toyota-cms-media/toyota-pdfs/a.pdf")
    assert owned is not None and owned.id == "toyota_media"
    assert registry.find_source("https://s3.amazonaws.com/someone-else/a.pdf") is None


def test_parse_vehicle_page_address_reads_the_model_year_and_the_model() -> None:
    assert source.parse_vehicle_page_address(VEHICLE_PAGE) == (2023, "example")
    assert source.parse_vehicle_page_address("https://pressroom.toyota.com/2023-news/") is None


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("2022 - 2023 Full-Line Pricing", (2022, 2023)),
        ("2024-2025ToyotaPricing-03282025.pdf", (2024, 2025)),
        ("2023-2024-Toyota-Pricing-1.05.2024.pdf", (2023, 2024)),
        ("2026_and_2027_Toyota_Pricing_11052026.pdf", (2026, 2027)),
        ("Click here to Download", ()),
    ],
)
def test_parse_model_years_ignores_edition_dates(text: str, years: tuple[int, ...]) -> None:
    assert source.parse_model_years(text) == years


@pytest.mark.parametrize(
    ("file_name", "kind"),
    [
        ("2022-2023-Toyota-Pricing-4.6.2023.pdf", source.PRICING_PDF),
        ("2024-2025ToyotaPricing-03282025.pdf", source.PRICING_PDF),
        ("2023-Toyota-Example-Product-Information.pdf", source.PRODUCT_INFORMATION_PDF),
        ("2023_Example_Product_Information_FINAL.pdf", source.PRODUCT_INFORMATION_PDF),
        ("2023-Toyota-Example-Product-Info_FINAL-1.pdf", source.PRODUCT_INFORMATION_PDF),
        ("2023-Toyota-Example_Product-Info-Guide.pdf", source.PRODUCT_INFORMATION_PDF),
        ("2023-Example-Features.pdf", source.OTHER_PDF),
        ("C_001-1500x1000.jpg", None),
    ],
)
def test_classify_document_goes_by_the_file_name(file_name: str, kind: str | None) -> None:
    assert source.classify_document(f"{MEDIA}/2023/01/{file_name}") == kind


def test_list_document_candidates_keeps_documents_on_registry_hosts_once(
    registry: Registry,
) -> None:
    candidates = source.list_document_candidates(
        VEHICLE_PAGE_HTML, VEHICLE_PAGE, registry, page_model_years=(2023,)
    )
    assert [(candidate.kind, candidate.model_years) for candidate in candidates] == [
        (source.PRODUCT_INFORMATION_PDF, (2023,)),
        (source.PRICING_PDF, (2022, 2023)),
    ]
    assert all(candidate.discovered_from == VEHICLE_PAGE for candidate in candidates)
    assert candidates[1].link_text == "Download PDF"


def test_record_candidate_widens_model_years_and_keeps_the_status(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    url = f"{MEDIA}/2022/09/2022-2023-Toyota-Pricing.pdf"
    source.record_candidate(
        connection, Candidate(url, "toyota_media", source.PRICING_PDF, (2023,), "page-a")
    )
    source.mark_candidate(connection, url, "unavailable", "no Wayback capture")
    source.record_candidate(
        connection, Candidate(url, "toyota_media", source.PRICING_PDF, (2022,), "page-b")
    )
    recorded = source.find_candidate(connection, url)
    assert recorded is not None
    assert recorded.model_years == (2022, 2023)
    assert recorded.discovered_from == "page-a"
    assert (recorded.status, recorded.detail) == ("unavailable", "no Wayback capture")


def test_record_candidate_reclassifies_a_recorded_candidate(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """A fix to the classifier reaches candidates recorded before it."""
    url = f"{MEDIA}/2022/05/2023-Toyota-Example-Product-Info.pdf"
    source.record_candidate(connection, Candidate(url, "toyota_media", source.OTHER_PDF, (), "p"))
    source.record_candidate(
        connection, Candidate(url, "toyota_media", source.PRODUCT_INFORMATION_PDF, (), "p")
    )
    recorded = source.find_candidate(connection, url)
    assert recorded is not None
    assert recorded.kind == source.PRODUCT_INFORMATION_PDF


def test_list_candidates_keeps_a_candidate_whose_model_year_is_unstated(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    source.record_candidate(connection, Candidate("u-2023", "s", source.OTHER_PDF, (2023,), "p"))
    source.record_candidate(connection, Candidate("u-2021", "s", source.OTHER_PDF, (2021,), "p"))
    source.record_candidate(connection, Candidate("u-none", "s", source.OTHER_PDF, (), "p"))
    found = source.list_candidates(connection, model_years=(2023,))
    assert {candidate.url for candidate in found} == {"u-2023", "u-none"}
