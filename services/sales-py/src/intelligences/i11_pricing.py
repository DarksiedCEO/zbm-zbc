"""Price books and proposal arithmetic (ADR 0013 decisions 14-15). Money is Decimal, two places, never a float.

Decides: the two price books' service lines (every line exists with NO price until Andre approves one), a proposal's
line amounts, subtotal, discount and total; whether a proposal may go out without Andre (total <= the auto-approve
maximum, and with the opportunity's other approved, sent or won proposals still <= it; no media-buy line, no
discount, no custom term — all of them); and the payment methods it must state
(Finance's rule: any media buy -> ACH only; card only for Revenue Recovery up to $5,000; otherwise ACH).

A media-buy line is cost + markup: the markup Andre approved for the line is the standard (he is expected to approve
15.00); a per-deal markup is allowed but a media-buy proposal always needs Andre anyway. Never: invents a price,
rounds per unit, or quotes a line whose approved price changed after the proposal was built."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Optional

import money

NUMBER = 11
NAME = "pricing"
DECIDES = "line amounts, totals, auto-approval and payment methods"

CARD_MAX = Decimal("5000.00")
MAX_QUANTITY = 100_000

# line_id: (brand, title, kind, product_line, unit)
CATALOG = {
    "zbm.revenue_recovery_engagement": ("zbm", "Revenue Recovery engagement", "service", "revenue_recovery",
                                        "engagement"),
    "zbm.revenue_recovery_monthly": ("zbm", "Revenue Recovery monthly retainer", "service", "revenue_recovery",
                                     "month"),
    "zbm.social_management_monthly": ("zbm", "Social media management", "service", "social", "month"),
    "zbm.creative_production": ("zbm", "Creative production package", "service", "creative", "package"),
    "zbm.media_planning_fee": ("zbm", "Media planning and buying fee", "service", "media_planning", "campaign"),
    "zbm.media_buy_ooh": ("zbm", "Billboard and out-of-home media buy", "media_buy", "ooh_billboards", "buy"),
    "zbm.media_buy_tv": ("zbm", "TV media buy", "media_buy", "tv", "buy"),
    "zbm.media_buy_radio": ("zbm", "Radio media buy", "media_buy", "radio", "buy"),
    "zbm.media_buy_digital": ("zbm", "Digital media buy", "media_buy", "digital_media_buys", "buy"),
    "zbc.clipping_campaign_setup": ("zbc", "Clipping campaign setup", "service", "clipping_campaign", "campaign"),
    "zbc.clipping_campaign_monthly": ("zbc", "Clipping campaign management", "service", "clipping_campaign",
                                      "month"),
    "zbc.clipping_views_per_1000": ("zbc", "Clipping paid views", "service", "clipping_campaign", "1000_views"),
}


def lines_of(brand: str) -> list[str]:
    return sorted(k for k, v in CATALOG.items() if v[0] == brand)


def approval_binding(line_id: str, version: int, price: Optional[str], markup_pct: Optional[str]) -> str:
    doc = {"line_id": line_id, "version": version, "price": price, "markup_pct": markup_pct}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class QuoteProblem(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def compute(brand: str, requested: list[dict], book: dict, discount: str, custom_terms: Optional[str],
            auto_max: Decimal, committed: Decimal = Decimal("0.00")) -> dict:
    """``committed``: the total of the opportunity's other proposals that are approved, sent or won (AEGIS S1-H2: a
    deal split into proposals under the maximum is judged as the sum)."""
    """``book``: line_id -> the line's state (``approved`` = {version, price, markup_pct} or None)."""
    out, has_media, custom_markup = [], False, False
    seen = set()
    for r in requested:
        lid = r["line_id"]
        meta = CATALOG.get(lid)
        if meta is None or meta[0] != brand:
            raise QuoteProblem("LINE_UNKNOWN")
        if lid in seen:
            raise QuoteProblem("LINE_REPEATED")
        seen.add(lid)
        appr = (book.get(lid) or {}).get("approved")
        if appr is None:
            raise QuoteProblem("PRICE_NOT_APPROVED")
        qty = r.get("quantity", 1)
        if meta[2] == "media_buy":
            has_media = True
            if r.get("media_cost") is None or qty != 1:
                raise QuoteProblem("MEDIA_COST_REQUIRED")
            cost = money.parse(r["media_cost"], positive=True)
            std = money.parse_pct(appr["markup_pct"])
            markup = money.parse_pct(r["markup_pct"]) if r.get("markup_pct") is not None else std
            custom_markup |= markup != std
            amount = money.q(cost + money.percent_of(cost, markup))
            out.append({"line_id": lid, "title": meta[1], "kind": "media_buy", "product_line": meta[3],
                        "price_version": appr["version"], "media_cost": money.fmt(cost),
                        "markup_pct": f"{markup:f}", "standard_markup_pct": appr["markup_pct"], "quantity": 1,
                        "unit": meta[4], "amount": money.fmt(amount)})
        else:
            if r.get("media_cost") is not None or r.get("markup_pct") is not None:
                raise QuoteProblem("MEDIA_FIELDS_ON_SERVICE_LINE")
            if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_QUANTITY:
                raise QuoteProblem("QUANTITY_INVALID")
            price = money.D(appr["price"])
            out.append({"line_id": lid, "title": meta[1], "kind": "service", "product_line": meta[3],
                        "price_version": appr["version"], "unit_price": money.fmt(price), "quantity": qty,
                        "unit": meta[4], "amount": money.fmt(money.times(price, qty))})
    if not out:
        raise QuoteProblem("NO_LINES")
    subtotal = money.total(x["amount"] for x in out)
    disc = money.parse(discount)
    if disc > subtotal:
        raise QuoteProblem("DISCOUNT_TOO_LARGE")
    total = money.q(subtotal - disc)
    reasons = []
    if total > auto_max:
        reasons.append("OVER_AUTO_APPROVE_MAX")
    elif money.q(total + money.D(committed)) > auto_max:
        reasons.append("OPPORTUNITY_TOTAL_OVER_MAX")
    if has_media:
        reasons.append("MEDIA_BUY_LINE")
    if disc > 0:
        reasons.append("DISCOUNT")
    if custom_terms:
        reasons.append("CUSTOM_TERMS")
    if custom_markup:
        reasons.append("PER_DEAL_MARKUP")
    if has_media:
        methods = ["ach"]
    elif all(x["product_line"] == "revenue_recovery" for x in out) and total <= CARD_MAX:
        methods = ["ach", "card"]
    else:
        methods = ["ach"]
    return {"lines": out, "subtotal": money.fmt(subtotal), "discount": money.fmt(disc), "total": money.fmt(total),
            "needs_andre": reasons, "payment_methods": methods}


def proposal_sha256(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
