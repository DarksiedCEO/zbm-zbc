"""Discovery fit: how well a prospect fits a brand (ADR 0015 decision 6). A label for the agent's work queue only;
it never approves, contacts or pays anyone.

Decides: a score 0..100 per brand from named rules — N niche overlap with the brand's niches (up to 50: 25 for each
matching niche, at most two), F follower band (nano 5, micro 20, mid 25, macro 20, mega 10: mid-size creators are the
default target), E engagement band (low 0, medium 15, high 25) — and a tier: A >= 70, B >= 45, C >= 25, else D.
Never: reads a platform, a model or anything outside the record."""

from __future__ import annotations

NUMBER = 3
NAME = "discovery_fit"
DECIDES = "fit score and tier per brand"

NICHES = ("ecommerce", "small_business", "marketing", "retail", "beauty", "pets", "fitness", "food", "tech",
          "gaming", "streaming", "music", "comedy", "sports", "entertainment", "lifestyle", "fashion", "education")
BRAND_NICHES = {"zbm": frozenset({"ecommerce", "small_business", "marketing", "retail", "beauty", "pets", "tech"}),
                "zbc": frozenset({"gaming", "streaming", "music", "comedy", "sports", "entertainment"})}
FOLLOWER_BANDS = {"nano": 5, "micro": 20, "mid": 25, "macro": 20, "mega": 10}
ENGAGEMENT_BANDS = {"low": 0, "medium": 15, "high": 25}


def score(niches, follower_band, engagement_band) -> dict:
    out = {}
    for brand, wanted in BRAND_NICHES.items():
        n = min(2, len(set(niches or ()) & wanted)) * 25
        s = n + FOLLOWER_BANDS.get(follower_band or "", 0) + ENGAGEMENT_BANDS.get(engagement_band or "", 0)
        tier = "A" if s >= 70 else "B" if s >= 45 else "C" if s >= 25 else "D"
        out[brand] = {"score": s, "tier": tier}
    return out
