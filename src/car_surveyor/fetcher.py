"""The one place the pipeline touches the network.

Every request passes `check_address` first. It refuses the reference domain
outright, refuses any host the source registry does not name, and accepts a
Wayback address only when the archived address belongs to a Toyota source:
the Wayback Machine holds copies of the reference too, and a deny rule for
`visor.vin` does not match a copy served from `web.archive.org`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx

from car_surveyor.source import Registry, Source

REFERENCE_DOMAIN = "visor.vin"
MAX_REDIRECTS = 5
# docs/plan/sources.md: pause a minute after the archive answers 429.
RATE_LIMIT_PAUSE_SECONDS = 60.0


class FetchRefusedError(Exception):
    """The address is one the pipeline must not fetch."""


@dataclass(frozen=True, slots=True)
class Response:
    url: str
    status_code: int
    media_type: str
    body: bytes

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


def is_reference_address(url: str) -> bool:
    """Whether an address is on the reference's domain or any subdomain of it."""
    host = (urlsplit(_with_scheme(url)).hostname or "").lower().rstrip(".")
    return host == REFERENCE_DOMAIN or host.endswith("." + REFERENCE_DOMAIN)


def get_archived_address(url: str) -> str | None:
    """The address a Wayback address asks the archive for, or None if it asks for none."""
    parts = urlsplit(url)
    if parts.path.startswith("/web/"):
        # /web/<timestamp and flags>/<archived address>
        _, _, archived = parts.path.removeprefix("/web/").partition("/")
        if parts.query:
            archived = f"{archived}?{parts.query}"
        return _with_scheme(unquote(archived)) if archived else None
    if parts.path in ("/wayback/available", "/cdx/search/cdx"):
        values = parse_qs(parts.query).get("url")
        return _with_scheme(values[0]) if values else None
    return None


def check_address(url: str, registry: Registry) -> Source:
    """Return the source an address belongs to, or refuse the address."""
    if is_reference_address(url):
        raise FetchRefusedError("the reference domain is never fetched")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FetchRefusedError(f"only https addresses are fetched, not {parts.scheme!r}")
    source = registry.find_source(url)
    if source is None:
        raise FetchRefusedError(f"{parts.hostname} is not in the source registry")
    if source.archives:
        archived = get_archived_address(url)
        if archived is None:
            raise FetchRefusedError("not an archive address the pipeline uses")
        if is_reference_address(archived):
            raise FetchRefusedError("an archived copy of the reference is never fetched")
        archived_source = registry.find_source(archived)
        if archived_source is None or archived_source.id not in source.archives:
            raise FetchRefusedError("the archive is asked only for addresses of listed sources")
    elif source.route != "direct":
        raise FetchRefusedError(f"{source.id} refuses this client and is not fetched directly")
    return source


class Fetcher:
    """Fetches registry addresses politely and counts what it fetched."""

    def __init__(
        self,
        registry: Registry,
        user_agent: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.request_count = 0
        self._registry = registry
        self._user_agent = user_agent
        self._client = client or httpx.Client(timeout=180.0)
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: dict[str, float] = {}

    def fetch(self, url: str) -> Response:
        """Fetch an address, checking it and every address it redirects to."""
        has_paused_for_rate_limit = False
        redirect_count = 0
        while True:
            source = check_address(url, self._registry)
            self._wait_for_turn(source)
            response = self._client.get(
                url, headers={"User-Agent": self._user_agent}, follow_redirects=False
            )
            self.request_count += 1
            self._last_request_at[source.id] = self._clock()
            if response.is_redirect and "location" in response.headers:
                redirect_count += 1
                if redirect_count > MAX_REDIRECTS:
                    raise FetchRefusedError(f"more than {MAX_REDIRECTS} redirects")
                url = urljoin(url, response.headers["location"])
                continue
            if response.status_code == 429 and not has_paused_for_rate_limit:
                has_paused_for_rate_limit = True
                self._sleep(RATE_LIMIT_PAUSE_SECONDS)
                continue
            media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            return Response(url, response.status_code, media_type, response.content)

    def _wait_for_turn(self, source: Source) -> None:
        last_request_at = self._last_request_at.get(source.id)
        if last_request_at is None:
            return
        remaining = source.crawl_delay_seconds - (self._clock() - last_request_at)
        if remaining > 0:
            self._sleep(remaining)


def _with_scheme(address: str) -> str:
    return address if "://" in address else f"https://{address}"
