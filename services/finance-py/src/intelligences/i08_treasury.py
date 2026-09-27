"""
Intelligence 8 — Treasury Segregation (Finance spec §C.8, FIN-18). Pure judgment on balances.

Invariant (journal side, and independently against bank/rail balances):
    restricted pool (1020 + 1040 + 1041) >= 2010 + 2020 + 2030 + 2040 + 2050 + 2070
This is stricter than "restricted cash >= unearned prepayments": creator money earned but not yet paid stays
segregated too (§0.1.2).

``posting_allowed``: a posting may not create a breach and may not deepen an existing one. The exceptions are the
FACT flows, which record what already happened: a V&I clawback after release (F5a) and a client deposit that landed
in the operating account (F1a). They post, the breach is recorded, and FC-03 turns red (hard stop on runs,
releases, sweeps, refunds) until Andre tops up restricted cash (F5c).
``sweepable`` = pool - liabilities - buffer (never below 0.00).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

import chart as C
import money as M
from intelligences import i01_journal as J

NUMBER, NAME, ACTOR = 8, "Treasury Segregation", "intel_08_treasury"
FACT_FLOWS = ("F5a", "F1a")


def position(balances: dict) -> dict:
    pool = M.total(J.account_balance(balances, "zbc", a) for a in C.RESTRICTED_POOL)
    liab = M.total(J.account_balance(balances, "zbc", a) for a in C.LIABILITIES)
    return {"pool": pool, "liabilities": liab, "gap": M.q(pool - liab)}


def projected(balances: dict, entry: dict) -> dict:
    b = dict(balances)
    J.apply_balances(b, entry)
    return position(b)


def posting_allowed(balances: dict, entry: dict) -> tuple[bool, dict, dict]:
    before = position(balances)
    after = projected(balances, entry) if entry["entity"] == "zbc" else before
    if entry["entity"] != "zbc" or entry["memo_code"] in FACT_FLOWS:
        return True, before, after
    ok = not (after["gap"] < 0 and after["gap"] < before["gap"])
    return ok, before, after


def sweepable(balances: dict, buffer: Decimal) -> Decimal:
    p = position(balances)
    return max(M.ZERO, M.q(p["gap"] - buffer))


def independent(bank_1020: Optional[Decimal], rail_balances: dict, balances: dict) -> dict:
    """The independent side: bank 1020 + every rail balance, against the JOURNAL liabilities."""
    liab = position(balances)["liabilities"]
    if bank_1020 is None or any(v is None for v in rail_balances.values()):
        return {"known": False, "pool": None, "liabilities": M.fmt(liab), "ok": False}
    pool = M.total([bank_1020, *rail_balances.values()])
    return {"known": True, "pool": M.sfmt(pool), "liabilities": M.fmt(liab), "ok": pool >= liab}


def view(p: dict) -> dict:
    return {"pool": M.sfmt(p["pool"]), "liabilities": M.sfmt(p["liabilities"]), "gap": M.sfmt(p["gap"]),
            "ok": p["gap"] >= 0}
