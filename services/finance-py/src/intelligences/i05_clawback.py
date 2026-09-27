"""
Intelligence 5 — Clawback & Netting (Finance spec §C.5; FIN-16). Pure judgment on V&I clawback records (counts,
never money) and the payee's open clawback receivable.

- adjustment: new paid_views = min(max(0, certified_views + sum(deltas)), cap); payable_delta = old - new amount,
  revenue_delta likewise (both by the §B.4 formulas, one rounding each). A ``voided`` certification (upheld fraud,
  cause ``void_upheld_fraud``) takes the whole payable.
- not yet released -> F5 (reduce the payable and the earned revenue); released -> F5a (a receivable from the payee).
- netting at release: netted = min(open 1200[payee], gross of the item) (F4b).
- write-off: only by Andre, only after FIN_CLAWBACK_WRITEOFF_MIN_DAYS (F5b). No function here, and no port anywhere,
  debits a creator's external account.
"""

from __future__ import annotations

from decimal import Decimal

import money as M

NUMBER, NAME, ACTOR = 5, "Clawback & Netting", "intel_05_clawback"
VOID_CAUSES = ("void_upheld_fraud",)


def adjustment(payable: dict, deltas: list[int], voided: bool) -> dict:
    from intelligences.i03_payables import new_paid
    cap = payable["max_paid_views_per_clip"]
    if voided:
        paid = 0
    else:
        paid = new_paid(payable["certified_views"], deltas, cap)
    new_amount = M.payable_amount(paid, M.D(payable["rate_per_1000"]))
    new_rev = M.payable_amount(paid, M.D(payable["client_rate_per_1000"]))
    cur_amount = M.D(payable["current_amount"])
    cur_rev = M.D(payable["current_revenue"])
    return {"new_paid_views": paid, "payable_delta": M.q(cur_amount - new_amount), "revenue_delta": M.q(cur_rev - new_rev),
            "new_amount": new_amount, "new_revenue": new_rev}


def netted(open_receivable: Decimal, gross: Decimal) -> Decimal:
    return M.q(min(max(M.ZERO, open_receivable), gross))
