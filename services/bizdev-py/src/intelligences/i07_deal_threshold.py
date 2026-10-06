"""Deal approval threshold, aggregated (ADR 0016 decision 18).

Decides: whether a pursuit or partner deal needs Andre's deal approval. Andre (Oct 6): any deal value over $10,000
goes to him, and splitting a deal must not get around it. So the test is never the deal's own value: it is the SUM
of the values of every live deal (pursuits and partner deals, both brands) in the same counterparty group whose
record was opened within the aggregation window, the deal itself included. Two deals are in one group when they
share ANY counterparty key — the caller's counterparty ref, the registrable domain, or the normalised organisation
name — and grouping is transitive. Over the threshold (strictly greater) needs Andre. Lost, withdrawn and no-bid deals
without a delivered submission do not count. An aggregate too large for money (``Overflow``) needs Andre and can
never be approved. Never: approves anything. The service re-runs this at every gate (submission, win), so a
sibling deal opened later re-blocks an earlier one that has no approval binding its value."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import money
from clock import parse_iso

NUMBER = 7
NAME = "deal_threshold"
DECIDES = "the counterparty group's aggregate value and whether it needs Andre"

OPEN = frozenset({"identified", "qualifying", "responding", "submitted", "registered"})
# closed but still counted (inside the window): won, and a pursuit whose submission was delivered (or may have been:
# ``sending``) whatever its stage (AEGIS round 1 H2: a delivered bid marked lost still went to that buyer)
CLOSED_COUNTED = frozenset({"won", "delivered"})
LIVE = OPEN | CLOSED_COUNTED


class Overflow(Exception):
    """The group's aggregate does not fit in money (an absurd value somewhere): the gate fails closed."""


def keys(counterparty_ref: str, domain_registrable: str, org: str) -> list[str]:
    return sorted({f"ref:{counterparty_ref}", f"domain:{domain_registrable}", f"org:{org}"})


def group(target_id: str, deals: dict, now: datetime, window_days: int) -> list[str]:
    """``deals``: id -> {"keys", "value", "opened_at", "status"}. The target's transitive group, among live deals
    opened within the window (the target is always in it)."""
    since = now - timedelta(days=window_days)

    def counts(d: dict) -> bool:
        if d["status"] in OPEN:
            return True                         # a live deal always counts, whatever its age (AEGIS round 1 Low)
        if d["status"] not in CLOSED_COUNTED:
            return False
        try:
            return parse_iso(d["opened_at"]) >= since      # the window applies to closed deals only
        except (TypeError, ValueError):
            return True                         # an unreadable date counts (fail closed)

    pool = {i: d for i, d in deals.items() if i == target_id or counts(d)}
    members, frontier = {target_id}, set(pool[target_id]["keys"])
    changed = True
    while changed:
        changed = False
        for i, d in pool.items():
            if i not in members and frontier & set(d["keys"]):
                members.add(i)
                frontier |= set(d["keys"])
                changed = True
    return sorted(members)


def aggregate(target_id: str, deals: dict, now: datetime, window_days: int) -> tuple[Decimal, list[str]]:
    members = group(target_id, deals, now, window_days)
    try:
        return money.total(deals[i]["value"] for i in members), members
    except money.MoneyError:
        raise Overflow() from None


def needs_andre(total: Decimal, threshold: Decimal) -> bool:
    return total > threshold
