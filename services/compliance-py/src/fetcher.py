"""
Change Watcher fetch port and its rules (spec C.4).

``FeedFetcher.fetch(url) -> FetchResult``. The default is ``NotWiredFetcher``
(every fetch fails; the watcher is off unless COMPLIANCE_WATCHER_ENABLED=1).
``HttpFeedFetcher`` is the real implementation. Before ANY request it
refuses (``FetchRefused``, no request made):
  - anything but https;
  - a host outside the allowlist (the hosts of the configured sources);
  - X / Meta hosts, excluded pending counsel question CQ-13;
  - login / sign-in / account paths;
  - a URL robots.txt disallows (robots.txt cached 24 h; a robots.txt that
    cannot be read because of a network error or 5xx counts as "disallow").
The request itself: GET only, no cookies, no auth, redirects NOT followed
(a redirect could leave the allowlist), 10 s timeout, 5 MB cap.
"""

from __future__ import annotations

import re
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Protocol
from urllib.parse import urlsplit

import httpx

from clock import Clock, iso

USER_AGENT = "zbm-compliance-watcher/0.1 (+deterministic change detection; no login)"
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_S = 10.0
ROBOTS_TTL = timedelta(hours=24)

# Excluded pending CQ-13 (report: X terms bar crawling "in any form"; Meta in
# the same counsel question). Matched on the host and every parent domain.
EXCLUDED_DOMAINS = ("x.com", "twitter.com", "facebook.com", "instagram.com", "meta.com", "fb.com", "threads.net")
_LOGIN_PATH = re.compile(r"(?i)/(login|log-in|signin|sign-in|signup|sign-up|auth|oauth|account|accounts|session)(/|$|\?|\.)")


class FetchRefused(Exception):
    """Refused by a fetcher rule; no request was made."""


class FetchFailed(Exception):
    """The fetch was attempted (or the watcher is off) and did not succeed."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    status_code: int
    body: bytes
    fetched_at: str


class FeedFetcher(Protocol):
    def fetch(self, url: str) -> FetchResult: ...


class NotWiredFetcher:
    def fetch(self, url: str) -> FetchResult:
        raise FetchFailed("Change Watcher fetcher is not enabled (COMPLIANCE_WATCHER_ENABLED != 1)")


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_excluded_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in EXCLUDED_DOMAINS)


def precheck(url: str, allowed_hosts: frozenset[str]) -> None:
    """Every rule that needs no network. Raises FetchRefused."""
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 0x21 or ord(c) == 0x7f for c in url):
        raise FetchRefused("malformed URL")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FetchRefused("only https is fetched")
    if parts.username or parts.password:
        raise FetchRefused("URLs with credentials are refused")
    host = (parts.hostname or "").lower()
    if is_excluded_host(host):
        raise FetchRefused(f"host {host} is excluded pending counsel question CQ-13")
    if host not in allowed_hosts:
        raise FetchRefused(f"host {host} is not on the watcher allowlist")
    if _LOGIN_PATH.search(parts.path or "/"):
        raise FetchRefused("login / account pages are never fetched")


class HttpFeedFetcher:
    def __init__(self, allowed_hosts: frozenset[str], clock: Clock, transport: httpx.BaseTransport | None = None):
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        self.clock = clock
        self._transport = transport
        self._robots: dict[str, tuple[datetime, Optional[urllib.robotparser.RobotFileParser]]] = {}
        self.requests_made = 0

    def _get(self, url: str) -> tuple[int, bytes]:
        self.requests_made += 1
        try:
            with httpx.Client(timeout=TIMEOUT_S, follow_redirects=False, transport=self._transport,
                              headers={"User-Agent": USER_AGENT, "Accept": "*/*"}) as client:
                with client.stream("GET", url) as resp:
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise FetchFailed("response larger than 5 MB")
                        chunks.append(chunk)
                    return resp.status_code, b"".join(chunks)
        except httpx.HTTPError as exc:
            raise FetchFailed(f"fetch failed: {type(exc).__name__}") from exc

    def _robots_allows(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.hostname.lower()
        now = self.clock.now()
        cached = self._robots.get(host)
        if cached is None or now - cached[0] > ROBOTS_TTL:
            try:
                status, body = self._get(f"https://{host}/robots.txt")
            except FetchFailed:
                status, body = 599, b""
            if 400 <= status < 500:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse([])  # no robots.txt: everything allowed
            elif status == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(body.decode("utf-8", errors="replace").splitlines()[:5000])
            else:
                parser = None  # unreadable robots.txt: fail closed
            self._robots[host] = (now, parser)
            cached = self._robots[host]
        parser = cached[1]
        return parser is not None and parser.can_fetch(USER_AGENT, url)

    def fetch(self, url: str) -> FetchResult:
        precheck(url, self.allowed_hosts)
        if not self._robots_allows(url):
            raise FetchRefused("robots.txt disallows this URL (or robots.txt could not be read)")
        status, body = self._get(url)
        if status != 200:
            raise FetchFailed(f"HTTP {status}")
        return FetchResult(url, status, body, iso(self.clock.now()))


# --- Watcher sources (spec C.4 table) ----------------------------------------------

_FR = "https://www.federalregister.gov/api/v1/documents.rss?conditions[agencies][]={a}&conditions[type][]={t}"


@dataclass(frozen=True)
class Source:
    source_id: str
    url: str
    method: str                                   # feed | page
    agency: Optional[str] = None
    doc_type: Optional[str] = None
    jurisdictions: tuple[str, ...] = ()           # rows a feed item may target (by row jurisdiction prefix)
    row_ids: tuple[str, ...] = ()                 # page sources: rows mapped explicitly (ADR 0006)


def seeded_sources() -> list[Source]:
    out: list[Source] = []
    for slug, short in (("federal-trade-commission", "ftc"), ("federal-communications-commission", "fcc"),
                        ("internal-revenue-service", "irs"), ("justice-department", "doj")):
        for t in ("RULE", "PRORULE"):
            out.append(Source(f"fr-{short}-{t.lower()}", _FR.format(a=slug, t=t), "feed", slug, t, ("US",)))
    for sid, url, typ in (("ftc-press-consumer", "https://www.ftc.gov/feeds/press-release-consumer-protection.xml", "press_release"),
                          ("ftc-blog-business", "https://www.ftc.gov/feeds/blog-business.xml", "blog"),
                          ("ftc-press", "https://www.ftc.gov/feeds/press-release.xml", "press_release")):
        out.append(Source(sid, url, "feed", "federal-trade-commission", typ, ("US",)))
    out.append(Source("uk-legislation-new", "https://www.legislation.gov.uk/new/data.feed", "feed", "legislation-gov-uk",
                      "legislation", ("GB",)))
    out.append(Source("ca-gazette-p1", "https://www.gazette.gc.ca/rss/p1-eng.xml", "feed", "canada-gazette", "part1", ("CA",)))
    out.append(Source("ca-gazette-p2", "https://www.gazette.gc.ca/rss/p2-eng.xml", "feed", "canada-gazette", "part2", ("CA",)))
    out.append(Source("cppa-updates", "https://cppa.ca.gov/regulations/ccpa_updates.html", "page",
                      row_ids=("US-CPPA-2025", "US-PRIV-CA")))
    out.append(Source("cppa-announcements", "https://cppa.ca.gov/announcements/", "page",
                      row_ids=("US-CPPA-2025", "US-PRIV-CA")))
    out.append(Source("youtube-changelog", "https://support.google.com/youtube/answer/9725604?hl=en", "page",
                      row_ids=("PLT-YT-01", "PLT-YT-02")))
    return out


# Watch terms per row domain (config ``watch_terms``; seeded from the spec's list).
WATCH_TERMS: dict[str, tuple[str, ...]] = {
    "advertising_disclosure": ("endorsement", "testimonial", "influencer", "branded content", "paid partnership"),
    "reviews_metrics": ("consumer review", "testimonial"),
    "consumer_contracts": ("negative option",),
    "children_age": ("coppa",),
    "email_sms": ("can-spam", "tcpa"),
    "sanctions": ("sanctions",),
    "tax": ("backup withholding", "1099"),
    "impersonation_ai": ("synthetic performer",),
    "accessibility": ("accessibility",),
    "privacy": ("privacy",),
    "platform_policy": ("branded content", "paid partnership", "influencer"),
}
