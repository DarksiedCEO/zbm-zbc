"""Brand and product-line routing (ADR 0013 decision 8).

Decides: which brand (zbm, the full-service agency; zbc, Z Best Clips) and which product lines a lead belongs to,
and the work queue it lands in. A ZBC campaign inquiry is ZBC; a Revenue Recovery scan is ZBM Revenue Recovery;
otherwise the product lines asked about decide; a lead naming lines of both brands, or a brand that contradicts its
lines, is refused; a lead with neither is ``unrouted`` (a human routes it). Never: recruits clippers (Clipper
Network owns that) or guesses a brand."""

from __future__ import annotations

from typing import Optional

NUMBER = 4
NAME = "lead_routing"
DECIDES = "brand, product lines and queue"

PRODUCT_LINES = {
    "zbm": ("revenue_recovery", "social", "ooh_billboards", "tv", "radio", "digital_media_buys", "creative"),
    "zbc": ("clipping_campaign",),
}
ALL_LINES = tuple(x for v in PRODUCT_LINES.values() for x in v)
BRAND_OF = {line: b for b, lines in PRODUCT_LINES.items() for line in lines}


def route(brand_hint: Optional[str], interests: list[str], evidence_kind: str) -> tuple[Optional[dict], Optional[str]]:
    """Returns ({brand, product_lines, queue}, None) or (None, problem_code)."""
    lines = sorted(set(interests))
    forced = {"zbc_campaign_inquiry": ("zbc", "clipping_campaign"), "rr_scan": ("zbm", "revenue_recovery")}.get(
        evidence_kind)
    if forced:
        brand, line = forced
        if brand_hint not in (None, brand):
            return None, "BRAND_PRODUCT_MISMATCH"
        if line not in lines:
            lines = sorted(set(lines) | {line})
    brands = {BRAND_OF[x] for x in lines}
    if len(brands) > 1:
        return None, "BRAND_PRODUCT_MISMATCH"
    if brands:
        brand = brands.pop()
        if brand_hint not in (None, brand):
            return None, "BRAND_PRODUCT_MISMATCH"
    else:
        brand = brand_hint
    if brand is None:
        return {"brand": None, "product_lines": [], "queue": "unrouted"}, None
    return {"brand": brand, "product_lines": lines, "queue": f"{brand}_sales"}, None
