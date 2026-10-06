"""Partner commissions (ADR 0016 decisions 15-17). Decimal-exact; every amount is a canonical two-decimal string.

Decides: what a partner is owed on one won deal after each Finance client-money event, and how a refund or
chargeback claws it back. The commissionable base is what the client has ACTUALLY paid, net of refunds and
chargebacks, never below 0.00 and never above the won deal value. Commission accrued = ``money.commission_total(base,
rate)``: exact, ONE half-up quantize (finance-py's ``q``), always on the CUMULATIVE base so rounding never drifts.
Money is "settled" once it is moved into a payout request. A clawback (accrued falls) is taken first from the unpaid
balance, then from payout requests Finance has not yet taken (newest first, cancelled at 0.00); whatever is left is a
SHORTFALL on money already with Finance or paid, recorded for Andre and never recovered here. The shortfall is always
``max(0, settled - accrued)`` (AEGIS round 2 L3), so a later payment that restores the accrual absorbs it. Never: pays anything."""

from __future__ import annotations

from decimal import Decimal

import money

NUMBER = 11
NAME = "commission_calculator"
DECIDES = "commission accrued, unpaid balance, clawbacks and shortfall per won partner deal"

ZERO = "0.00"
CLIENT_EVENTS = ("payment", "refund", "chargeback")


def fresh() -> dict:
    return {"client_paid": ZERO, "client_reversed": ZERO, "base": ZERO, "accrued": ZERO, "settled": ZERO,
            "shortfall": ZERO}


def unpaid(state: dict) -> Decimal:
    return money.D(state["accrued"]) - money.D(state["settled"])


def apply(state: dict, kind: str, amount: str, rate_pct: str, deal_value: str, open_payouts: list) -> dict:
    """One client-money event. ``open_payouts``: [{"payout_id", "amount"}] still cancellable (status ``queued``),
    newest first. Returns {"state", "accrued_delta", "payout_cuts": [{"payout_id", "amount"}], "shortfall_delta"}.
    Amounts out are canonical strings; ``accrued_delta`` is signed."""
    if kind not in CLIENT_EVENTS:
        raise ValueError("unknown client event")
    amt, rate, cap = money.D(amount), Decimal(rate_pct), money.D(deal_value)
    paid, rev = money.D(state["client_paid"]), money.D(state["client_reversed"])
    if kind == "payment":
        paid = money.q(paid + amt)
    else:
        rev = money.q(rev + amt)
    net = money.q(paid - rev)
    base = min(cap, max(money.ZERO, net))
    old_acc = money.D(state["accrued"])
    new_acc = money.commission_total(base, rate)
    delta = money.q(new_acc - old_acc)
    settled = money.D(state["settled"])
    cuts = []
    if delta < 0:
        owed_back = -delta
        free = money.q(old_acc - settled)                   # unpaid before this event (may be negative: shortfall)
        take = min(owed_back, max(money.ZERO, free))
        owed_back = money.q(owed_back - take)
        for p in open_payouts:
            if owed_back <= 0:
                break
            cut = min(owed_back, money.D(p["amount"]))
            if cut > 0:
                cuts.append({"payout_id": p["payout_id"], "amount": money.fmt(cut)})
                settled = money.q(settled - cut)
                owed_back = money.q(owed_back - cut)
    old_short = money.D(state["shortfall"])
    new_short = shortfall_of(new_acc, settled)
    st = {"client_paid": money.fmt(paid), "client_reversed": money.fmt(rev), "base": money.fmt(base),
          "accrued": money.fmt(new_acc), "settled": money.fmt(settled), "shortfall": money.fmt(new_short)}
    grew = money.q(new_short - old_short) if new_short > old_short else money.ZERO
    return {"state": st, "accrued_delta": money.sfmt(delta), "payout_cuts": cuts, "shortfall_delta": money.fmt(grew)}


def shortfall_of(accrued, settled) -> "Decimal":
    """AEGIS round 2 L3: the shortfall is DERIVED, never accumulated: what has been settled (moved into payout
    requests) beyond what is accrued now, floored at 0.00. A later payment that restores the accrual absorbs it."""
    return max(money.ZERO, money.q(money.D(settled) - money.D(accrued)))
