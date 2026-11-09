"""
Google Ads Keyword Planner (``KeywordPlanIdeaService.GenerateKeywordIdeas``): an INTERFACE ONLY (ADR 0047, "Google
Ads Keyword Planner — future"). Nothing here makes a request, holds a credential or is wired to a setting; the only
implementation answers NOT_BUILT. It exists so Priya's ledger can say, per row, that market search volume was not
measured and why, and so a future adapter has a fixed contract to meet.

What Google documents (https://developers.google.com/google-ads/api/docs/keyword-planning/generate-keyword-ideas,
read Nov 8 2026): the request takes a ``customer_id`` (a Google Ads account), an optional ``language``,
``geo_target_constants`` (empty = all geographies), and a ``keyword_seed`` (1..20 keywords) or a ``url_seed``; the
answer carries ``avg_monthly_searches`` ("12-month average"), ``competition`` (LOW / MEDIUM / HIGH),
``competition_index`` (0..100) and top-of-page bids (``low_top_of_page_bid_micros``, the 20th percentile;
``high_top_of_page_bid_micros``, the 80th).

What is NOT verified: the widely repeated claim that accounts without ad spend see bucketed RANGES instead of point
volumes is about the Keyword Planner web interface, from secondary sources; Google's API page read does not say it.
The contract below therefore carries every volume as a range (``low``, ``high``) and a ``bucketed`` flag that a
future adapter must set from what Google actually returned, never assume. Using it needs a Google Ads account and a
developer token (Google's terms); it costs no data-vendor fee, but the owner rule "no paid keyword data vendors" and
the account question are a founder decision before any adapter is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

NOT_BUILT = "NOT_BUILT"
DOC_URL = "https://developers.google.com/google-ads/api/docs/keyword-planning/generate-keyword-ideas"


@dataclass(frozen=True)
class VolumeRange:
    """A monthly search volume as Google would give it: a range, kept as a range. ``bucketed`` says whether Google
    returned a bucket (low < high) or a point value (low == high); ``source`` names the call and its date range."""

    low: int
    high: int
    bucketed: bool
    source: str
    months: str

    def __post_init__(self):
        if not (isinstance(self.low, int) and isinstance(self.high, int) and 0 <= self.low <= self.high):
            raise ValueError("a volume range needs 0 <= low <= high")
        if self.bucketed != (self.low < self.high):
            raise ValueError("bucketed must say whether the range is a bucket")


@dataclass(frozen=True)
class KeywordIdea:
    text: str
    volume: VolumeRange | None
    competition: str | None           # LOW | MEDIUM | HIGH as Google labels it; never turned into a difficulty


class KeywordPlanIdeasPort(Protocol):
    connected: bool

    def generate_keyword_ideas(self, seeds: list, language: str | None, geo_targets: list) -> list:
        ...


class NotBuiltKeywordPlanner:
    """The only implementation: no request, no credential, no invented volume."""

    connected = False
    state = NOT_BUILT

    def generate_keyword_ideas(self, seeds: list, language: str | None, geo_targets: list) -> list:
        raise NotImplementedError("Google Ads KeywordPlanIdeaService: interface only (ADR 0047)")

    @staticmethod
    def describe() -> dict:
        return {"state": NOT_BUILT, "service": "KeywordPlanIdeaService.GenerateKeywordIdeas", "doc": DOC_URL,
                "returns": "avg_monthly_searches (12-month average) as a range, competition, top-of-page bids",
                "why_not_built": "needs a Google Ads account and developer token; a founder decision (ADR 0047)"}
