"""
Intelligence 8 — Treasury Segregation (Finance spec §C.8, FIN-18). Pure judgment on balances.

Invariant (journal side, and independently against bank/rail balances):
    restricted pool (1020 + 1040 + 1041) >= 2010 + 2020 + 2030 + 2040 + 2050 + 2070
This is stricter than "restricted cash >= unearned prepayments": creator money earned but not yet paid stays
segregated too (§0.1.2).

``posting_allowed``: a posting may not create a breach and may not deepen an existing one. The exceptions are the
FACT flows, which record what already happened: a V&I clawback after release (F5a), a client deposit that landed
in the operating account (F1a), a client deposit the bank returned (F1r, ACH return) and the recorded reversal of
a treasury posting whose bank instruction failed (``fact=True``). They post, the breach is recorded, and FC-03 turns
red (hard stop on runs, releases, sweeps, refunds) until Andre tops up restricted cash (F5c).
``sweepable`` = pool - liabilities - buffer - sweeps already proposed/approved and not yet executed (never below
0.00). Liabilities are ABSOLUTE per sub-ledger (``liabilities``): a debit-balance sub-ledger never offsets another
(AEGIS N17-9).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

import chart as C
import money as M
from intelligences import i01_journal as J

NUMBER, NAME, ACTOR = 8, "Treasury Segregation", "intel_08_treasury"
FACT_FLOWS = ("F5a", "F1a", "F1r")     # F1r: a client deposit returned by the bank (ACH return), N17-9


def liabilities(balances: dict) -> Decimal:
    """ABSOLUTE creator/client liabilities (AEGIS N17-9): each liability sub-ledger counts at its own credit
    balance, never below zero. A sub-ledger driven into a debit balance (one client's deposit reversed after
    creators were accrued against it) is NOT allowed to offset another client's or creator's liability -- a signed
    net would hide the shortfall. Accounts without sub-ledgers count at max(0, balance)."""
    per: dict[tuple[str, Optional[str]], Decimal] = {}
    for (e, a, sl), v in balances.items():
        if e == "zbc" and a in C.LIABILITIES:
            per[(a, sl)] = per.get((a, sl), M.ZERO) + v
    total = M.ZERO
    for (a, _sl), v in per.items():
        natural = M.q(-v) if C.normal_side("zbc", a) == "credit" else M.q(v)
        total += max(M.ZERO, natural)
    return M.q(total)


def position(balances: dict) -> dict:
    pool = M.total(J.account_balance(balances, "zbc", a) for a in C.RESTRICTED_POOL)
    liab = liabilities(balances)
    return {"pool": pool, "liabilities": liab, "gap": M.q(pool - liab)}


def projected(balances: dict, entry: dict) -> dict:
    b = dict(balances)
    J.apply_balances(b, entry)
    return position(b)


def posting_allowed(balances: dict, entry: dict, fact: bool = False) -> tuple[bool, dict, dict]:
    before = position(balances)
    after = projected(balances, entry) if entry["entity"] == "zbc" else before
    if entry["entity"] != "zbc" or entry["memo_code"] in FACT_FLOWS or fact:
        return True, before, after
    ok = not (after["gap"] < 0 and after["gap"] < before["gap"])
    return ok, before, after


def sweepable(balances: dict, buffer: Decimal, reserved: Decimal = M.ZERO) -> Decimal:
    """pool - liabilities - buffer - ``reserved`` (sweeps approved or proposed but not yet executed, AEGIS N17-2),
    never below 0.00."""
    p = position(balances)
    return max(M.ZERO, M.q(p["gap"] - buffer - reserved))


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
