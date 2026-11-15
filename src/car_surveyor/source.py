"""The source registry and the candidate documents found through it.

`sources.yaml` names every host the pipeline may fetch. A candidate is an
address a listing page linked to. Discovery records every one, fetched or
not, so that what the pipeline did not read stays visible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urljoin, urlsplit

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict

from car_surveyor import storage

Route = Literal["direct", "archive"]
CandidateStatus = Literal["pending", "fetched", "unavailable"]

VEHICLE_PAGE = "vehicle_page"
PRICING_PDF = "pricing_pdf"
PRODUCT_INFORMATION_PDF = "product_information_pdf"
OTHER_PDF = "other_pdf"
FUEL_ECONOMY_FILE = "fuel_economy_file"

_VEHICLE_PAGE_PATTERN = re.compile(r"/vehicle/(?P<year>\d{4})-toyota-(?P<model>[a-z0-9-]+)/?$")
_SITEMAP_ADDRESS_PATTERN = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.DOTALL)
# A model year standing alone. An edition date such as 03282025 is not one.
_MODEL_YEAR_PATTERN = re.compile(r"(?<!\d)(20[0-4]\d)(?!\d)")


class Source(BaseModel):
    """One site the pipeline reads, and how its documents are reached."""

    model_config = ConfigDict(frozen=True)

    id: str
    hosts: tuple[str, ...]
    route: Route
    crawl_delay_seconds: float = 0.0
    # A host shared with other owners counts only under this path.
    path_prefixes: dict[str, str] = {}
    # For an archive: the sources whose addresses it may be asked for.
    archives: tuple[str, ...] = ()

    def has_address(self, url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in self.hosts:
            return False
        prefix = self.path_prefixes.get(host)
        return prefix is None or parts.path.startswith(prefix)


class Listing(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    kind: Literal["vehicle_sitemap", "pricing_listing"]


class RegisteredDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    kind: str


class Registry(BaseModel):
    """The contents of `sources.yaml`."""

    model_config = ConfigDict(frozen=True)

    make: str
    market: str
    model_years: tuple[int, ...]
    development_year: int
    sources: tuple[Source, ...]
    listings: tuple[Listing, ...] = ()
    documents: tuple[RegisteredDocument, ...] = ()

    def find_source(self, url: str) -> Source | None:
        """The source an address belongs to, or None when no source claims it."""
        return next((source for source in self.sources if source.has_address(url)), None)

    def get_source(self, source_id: str) -> Source:
        return next(source for source in self.sources if source.id == source_id)


@dataclass(frozen=True, slots=True)
class Link:
    url: str
    text: str


@dataclass(frozen=True, slots=True)
class Candidate:
    """An address discovery found, and what became of it."""

    url: str
    source_id: str
    kind: str
    model_years: tuple[int, ...]
    discovered_from: str
    link_text: str = ""
    status: CandidateStatus = "pending"
    detail: str = ""


def load_registry(registry_path: Path) -> Registry:
    return Registry.model_validate(yaml.safe_load(registry_path.read_text()))


def list_sitemap_addresses(sitemap_xml: str) -> list[str]:
    return _SITEMAP_ADDRESS_PATTERN.findall(sitemap_xml)


def parse_vehicle_page_address(url: str) -> tuple[int, str] | None:
    """The model year and model slug of a pressroom vehicle page address."""
    match = _VEHICLE_PAGE_PATTERN.search(urlsplit(url).path)
    if match is None:
        return None
    return int(match["year"]), match["model"]


def parse_model_years(text: str) -> tuple[int, ...]:
    """The model years a link text or file name states, in order, without repeats."""
    return tuple(dict.fromkeys(int(year) for year in _MODEL_YEAR_PATTERN.findall(text)))


def get_file_name(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def classify_document(url: str) -> str | None:
    """The kind of document a link points to, or None when it is not a document."""
    file_name = get_file_name(url).lower()
    if not file_name.endswith(".pdf"):
        return None
    words = re.sub(r"[^a-z0-9]+", " ", file_name)
    if "pricing" in words:
        return PRICING_PDF
    # File names say "Product Information" or just "Product Info".
    if "product info" in words or "productinfo" in words:
        return PRODUCT_INFORMATION_PDF
    return OTHER_PDF


class _LinkCollector(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self._base_url = base_url
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._text).split())
            self.links.append(Link(urljoin(self._base_url, self._href.strip()), text))
            self._href = None


def list_links(page_html: str, page_url: str) -> list[Link]:
    """Every link on a page, with its address made absolute."""
    collector = _LinkCollector(page_url)
    collector.feed(page_html)
    return collector.links


def list_document_candidates(
    page_html: str, page_url: str, registry: Registry, *, page_model_years: tuple[int, ...] = ()
) -> list[Candidate]:
    """The documents a page links to, one candidate per address.

    A pricing document's model years come from its link text or file name. Any
    other document belongs to the model years of the page that links it.
    """
    candidates: dict[str, Candidate] = {}
    for link in list_links(page_html, page_url):
        kind = classify_document(link.url)
        source = registry.find_source(link.url)
        if kind is None or source is None:
            continue
        if kind == PRICING_PDF:
            years = parse_model_years(link.text) or parse_model_years(get_file_name(link.url))
        else:
            years = page_model_years
        candidates.setdefault(
            link.url,
            Candidate(
                url=link.url,
                source_id=source.id,
                kind=kind,
                model_years=years,
                discovered_from=page_url,
                link_text=link.text,
            ),
        )
    return list(candidates.values())


def record_candidate(connection: duckdb.DuckDBPyConnection, candidate: Candidate) -> None:
    """Add a candidate, or bring the kind and model years of a recorded one up to date.

    The kind follows the current classifier and the model years only widen.
    The status is left alone: discovery never undoes what a later stage found
    out about an address.
    """
    existing = find_candidate(connection, candidate.url)
    if existing is None:
        connection.execute(
            "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                candidate.url,
                candidate.source_id,
                candidate.kind,
                json.dumps(list(candidate.model_years)),
                candidate.discovered_from,
                candidate.link_text,
                candidate.status,
                candidate.detail,
                datetime.now(UTC).replace(tzinfo=None),
            ],
        )
        return
    years = tuple(sorted({*existing.model_years, *candidate.model_years}))
    if years != existing.model_years or candidate.kind != existing.kind:
        connection.execute(
            "UPDATE candidates SET model_years = ?, kind = ? WHERE url = ?",
            [json.dumps(list(years)), candidate.kind, candidate.url],
        )


def mark_candidate(
    connection: duckdb.DuckDBPyConnection, url: str, status: CandidateStatus, detail: str = ""
) -> None:
    connection.execute(
        "UPDATE candidates SET status = ?, detail = ? WHERE url = ?", [status, detail, url]
    )


def find_candidate(connection: duckdb.DuckDBPyConnection, url: str) -> Candidate | None:
    candidates = list_candidates(connection, url=url)
    return candidates[0] if candidates else None


def list_candidates(
    connection: duckdb.DuckDBPyConnection,
    *,
    url: str | None = None,
    kinds: tuple[str, ...] = (),
    model_years: tuple[int, ...] = (),
) -> list[Candidate]:
    """Recorded candidates, oldest first, optionally narrowed.

    A candidate with no stated model year is kept by a model-year filter: its
    scope is unknown, and unknown is not out of scope.
    """
    records = storage.fetch_records(
        connection,
        "SELECT url, source_id, kind, model_years, discovered_from, link_text, status, detail "
        "FROM candidates ORDER BY discovered_at, url",
    )
    candidates = [
        Candidate(
            url=record["url"],
            source_id=record["source_id"],
            kind=record["kind"],
            model_years=tuple(json.loads(record["model_years"])),
            discovered_from=record["discovered_from"],
            link_text=record["link_text"],
            status=record["status"],
            detail=record["detail"],
        )
        for record in records
    ]
    return [
        candidate
        for candidate in candidates
        if (url is None or candidate.url == url)
        and (not kinds or candidate.kind in kinds)
        and (
            not model_years
            or not candidate.model_years
            or bool(set(model_years) & set(candidate.model_years))
        )
    ]
