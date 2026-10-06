"""Deal approval: who must approve an influencer deal (ADR 0015 decision 15; Andre, Oct 6 2026: any influencer deal
over $5,000 total goes to him).

Decides, in exact Decimal arithmetic (money.py; no float anywhere): a deal's total is its cash fee plus the value of
any product given. It may be approved without Andre only if ALL of these stay within ``INF_AUTO_APPROVE_MAX`` (default
and ceiling 5000.00):
  D1 the deal's own total;
  D2 the deal plus EVERY other deal of the same PERSON that was not rejected or cancelled — lifetime, across both
     brands and every campaign, completed deals included (AEGIS R1-H1: deals run one after another were each approved
     alone) — where the person is the record AND every record sharing its tax reference or Finance payee (AEGIS
     R1-M5: one creator under two records);
  D3 the same, within the deal's campaign (a subset of D2, named so Andre sees a campaign-level split).
Each rule that fails is named (``DEAL_OVER_LIMIT``, ``INFLUENCER_TOTAL_OVER_LIMIT``, ``CAMPAIGN_TOTAL_OVER_LIMIT``);
any one sends the deal to ``pending_andre``. A time window instead of lifetime is not built
(``INF_DEAL_AGGREGATE_WINDOW_DAYS`` refuses start). The rule is applied again at payout time keyed on the payee
(svc_payouts). Never: approves on Andre's behalf."""

from __future__ import annotations

from decimal import Decimal

import money

NUMBER = 9
NAME = "deal_approval"
DECIDES = "whether a deal needs Andre (D1 own total, D2 the person's lifetime deals, D3 their deals in the campaign)"

NOT_COUNTED = ("rejected", "cancelled")


def deal_total(fee: Decimal, product_value: Decimal) -> Decimal:
    return money.total([fee, product_value])


def needs_andre(total: Decimal, others: list[dict], campaign_id: str, limit: Decimal) -> list[str]:
    """``others``: the person's other deals (``status``, ``campaign_id``, ``total`` as canonical strings)."""
    reasons = []
    if total > limit:
        reasons.append("DEAL_OVER_LIMIT")
    person_sum = money.total([total] + [d["total"] for d in others if d["status"] not in NOT_COUNTED])
    if person_sum > limit:
        reasons.append("INFLUENCER_TOTAL_OVER_LIMIT")
    camp_sum = money.total([total] + [d["total"] for d in others
                                      if d["campaign_id"] == campaign_id and d["status"] not in NOT_COUNTED])
    if camp_sum > limit:
        reasons.append("CAMPAIGN_TOTAL_OVER_LIMIT")
    return reasons
