"""Deal approval: who must approve an influencer deal (ADR 0015 decision 15; Andre, Oct 6 2026: any influencer deal
over $5,000 total goes to him).

Decides, in exact Decimal arithmetic (money.py; no float anywhere): a deal's total is its cash fee plus the value of
any product given. It may be approved without Andre only if ALL of these stay within ``INF_AUTO_APPROVE_MAX`` (default
and ceiling 5000.00):
  D1 the deal's own total;
  D2 the deal plus every OPEN deal of the same influencer, across both brands and every campaign (open = pending
     Andre, approved, contract sent, contracted);
  D3 the deal plus every other deal of the same influencer in the same campaign that was not rejected or cancelled
     (completed deals count here), so a deal split into pieces, at once or one after another, still reaches Andre
     (sales-py's S1-H2 fix).
Each rule that fails is named (``DEAL_OVER_LIMIT``, ``INFLUENCER_OPEN_TOTAL_OVER_LIMIT``,
``CAMPAIGN_TOTAL_OVER_LIMIT``); any one sends the deal to ``pending_andre``. Never: approves on Andre's behalf."""

from __future__ import annotations

from decimal import Decimal

import money

NUMBER = 9
NAME = "deal_approval"
DECIDES = "whether a deal needs Andre (D1 own total, D2 influencer's open deals, D3 influencer's campaign deals)"

OPEN = ("pending_andre", "approved", "contract_sent", "contracted")
NOT_COUNTED = ("rejected", "cancelled")


def deal_total(fee: Decimal, product_value: Decimal) -> Decimal:
    return money.total([fee, product_value])


def needs_andre(total: Decimal, others: list[dict], campaign_id: str, limit: Decimal) -> list[str]:
    """``others``: the influencer's other deals (``status``, ``campaign_id``, ``total`` as canonical strings)."""
    reasons = []
    if total > limit:
        reasons.append("DEAL_OVER_LIMIT")
    open_sum = money.total([total] + [d["total"] for d in others if d["status"] in OPEN])
    if open_sum > limit:
        reasons.append("INFLUENCER_OPEN_TOTAL_OVER_LIMIT")
    camp_sum = money.total([total] + [d["total"] for d in others
                                      if d["campaign_id"] == campaign_id and d["status"] not in NOT_COUNTED])
    if camp_sum > limit:
        reasons.append("CAMPAIGN_TOTAL_OVER_LIMIT")
    return reasons
