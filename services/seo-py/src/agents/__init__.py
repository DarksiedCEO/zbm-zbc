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
    robots_cache: dict = field(default_factory=dict)
    robots: Optional[dict] = None          # filled by Selene, read by Delia and Roman

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.domain}"

    def url(self, path: str) -> str:
        return self.origin + path

    def same_site(self, host: Optional[str]) -> bool:
        h = (host or "").lower().split(":")[0].rstrip(".")
        d = self.domain.split(":")[0]
        return h == d or h == "www." + d or d == "www." + h
