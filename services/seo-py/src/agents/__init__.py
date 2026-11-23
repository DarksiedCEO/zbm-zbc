"""
Wave-1 agents, one task each, each returning the outcome envelope (envelope.py):

  selene        crawlability: robots.txt per bot family, status codes, redirect chains, canonical / noindex
                conflicts, bot-vs-browser access differential, fetch / render states
  delia         sitemap.xml discovery and validation; llms.txt presence and format (an emerging, non-standard
                convention)
  roman         content structure and machine readability; per-engine HEURISTIC checklists (never a score)
  probes        the AI-visibility probe framework for Naomi (prompt volume: NOT_CONNECTED) and Callum (citation
                Strength / Opportunity / Mentioned-Not-Cited from repeated samples)
  entity_check  the site's structured data / NAP against the canonical entity record (stage D)
  osei          quarantine, freshness, refresh tiers (Osei-lite)

The run context is passed in; no agent touches service state, the ledger or the log.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import SplitResult, urlsplit


def split_url(url) -> Optional[SplitResult]:
    """``urlsplit`` that answers None for a URL it cannot read (an unbalanced or non-ASCII IPv6 literal raises
    ValueError there, and from ``.hostname`` / ``.port``): a crawled URL is data and never stops a run (WR-F010)."""
    if not isinstance(url, str):
        return None
    try:
        p = urlsplit(url)
        p.hostname, p.port                                  # both parse lazily and may raise
    except ValueError:
        return None
    return p


GONE_STATUSES = (404, 410)
CRAWL_DELAY_CAP = 3.0          # seconds; a robots.txt Crawl-delay above it stops discovery (see agents/selene.py)
DEADLINE_REASON = "AUDIT_DEADLINE"


class DeadlineReached(Exception):
    """The audit's overall time limit leaves no room for another request to the site (nothing was sent)."""


def gone(pg: dict) -> bool:
    """A page the site answered "not found" / "gone" for: reported by Selene (a broken link), not content to judge,
    and not counted against the content areas' coverage."""
    f = pg.get("fetch")
    return bool(f is not None and getattr(f, "state", None) == "OK" and getattr(f, "status", None) in GONE_STATUSES)


def host_of(url) -> Optional[str]:
    p = split_url(url)
    return None if p is None else p.hostname


@dataclass
class RunContext:
    fetcher: object
    renderer: object
    guard: Callable
    clock: object
    osei: object
    domain: str
    scheme: str = "https"
    paths: list = field(default_factory=lambda: ["/"])
    # the most pages one audit reads (requested + discovered). 0 = no discovery: only the requested paths are read
    max_pages: int = 0
    # politeness: the least time between two of this audit's requests to the site (robots.txt Crawl-delay raises it,
    # up to a cap); ``sleep`` / ``monotonic`` are injectable so tests measure the waits without waiting
    crawl_delay_s: float = 0.0
    sleep: Callable = time.sleep
    monotonic: Callable = time.monotonic
    requests: int = 0                       # requests this audit made to the site (robots.txt included)
    last_request: Optional[float] = None
    # one overall deadline (``monotonic`` time) over EVERY request of the audit to the site: no request starts that
    # could end after it (its wait plus ``fetch_reserve_s``, the fetcher's own hard per-request bound)
    deadline: Optional[float] = None
    fetch_reserve_s: float = 0.0
    deadline_hit: bool = False
    max_latency: float = 0.0                # the slowest request of this audit so far (seconds, measured)
    robots_cache: dict = field(default_factory=dict)
    robots: Optional[dict] = None          # filled by Selene, read by Delia and Roman

    def crawl_delay(self) -> float:
        """Seconds between two requests: the configured floor, raised by robots.txt's Crawl-delay for our crawler
        (or ``*``) up to CRAWL_DELAY_CAP."""
        from primitives.fetch import PRODUCT_TOKEN
        delay = max(0.0, float(self.crawl_delay_s or 0))
        asked = self.robots_crawl_delay(PRODUCT_TOKEN)
        if asked is not None:
            delay = max(delay, min(asked, CRAWL_DELAY_CAP))
        return delay

    def robots_crawl_delay(self, token: str) -> Optional[float]:
        rb = self.robots or {}
        parsed = rb.get("parsed")
        return parsed.crawl_delay(token) if parsed is not None else None

    def per_request_estimate(self) -> float:
        """What one more request is expected to take: the politeness wait or the slowest response seen, whichever is
        longer (re-review of PR 47, item 3: a reserve in crawl-delay seconds alone ignores slow responses)."""
        return max(self.crawl_delay(), self.max_latency)

    def observe(self, started: float) -> None:
        self.max_latency = max(self.max_latency, max(0.0, self.monotonic() - started))

    def polite(self) -> None:
        """Called before every request to the site: waits until ``crawl_delay()`` has passed since the last one.
        Raises DeadlineReached (nothing is sent) when the request could not finish before the audit deadline."""
        delay = self.crawl_delay()
        wait = 0.0
        if self.last_request is not None and delay > 0:
            wait = max(0.0, self.last_request + delay - self.monotonic())
        if self.deadline is not None and self.monotonic() + wait + self.fetch_reserve_s > self.deadline:
            self.deadline_hit = True
            raise DeadlineReached()
        if wait > 0:
            self.sleep(wait)
        self.last_request = self.monotonic()
        self.requests += 1

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.domain}"

    def url(self, path: str) -> str:
        return self.origin + path

    def same_site(self, host: Optional[str]) -> bool:
        h = (host or "").lower().split(":")[0].rstrip(".")
        d = self.domain.split(":")[0]
        return h == d or h == "www." + d or d == "www." + h
