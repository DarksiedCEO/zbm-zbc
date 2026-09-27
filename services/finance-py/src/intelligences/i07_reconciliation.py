"""
Intelligence 7 — Reconciliation (Finance spec §B.10, §C.7; FIN-17). Pure judgment: legs, differences, breaks,
aging. Zero tolerance: a leg is matched iff its difference is exactly 0.00; one cent is a break. A leg whose
independent source cannot be read (a stand-in) is a break of kind ``source_unavailable``. It never plugs a
difference and never resolves a break (Andre does, with a reconciling entry or a documented timing item).

Legs (each against a source the payout system does not produce):
  L1 bank-restricted   zbc 1020            vs the bank's balance of the deposits account
  L2 bank-operating    zbc 1010, zbm 1010  vs the bank's balances
  L3 rail              zbc 1040 / 1041     vs the rail balance API; open 2030 items vs the rail's item statuses
  L4 sub-ledgers       2020 / 2030 / 1200 / 2010 control balances vs the operational records (payables, items,
                       clawback receivables, campaign deposits) — a mismatch means a posting and a record disagree
  L5 GL                at close only (QBO adapter: stand-in -> source_unavailable)
A rail with no journal activity ever and a stand-in source is ``not_in_use`` (Trolley is dormant, R8).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

import money as M
from intelligences.i06_tax import business_days_between

NUMBER, NAME, ACTOR = 7, "Reconciliation", "intel_07_reconciliation"
FC01_MAX_AGE_H = 26
EXPLANATIONS = ("timing_in_transit", "fee_unbooked", "misapplied_receipt", "rail_return", "unknown")


def leg(leg_id: str, subject: str, expected: Decimal, observed: Optional[Decimal], source_sha: Optional[str],
        note: str = "") -> dict:
    if observed is None:
        return {"leg": leg_id, "subject": subject, "expected": M.sfmt(expected), "observed": None,
                "observed_source_sha256": None, "difference": None, "status": "source_unavailable", "note": note[:200]}
    diff = M.q(observed - expected)
    return {"leg": leg_id, "subject": subject, "expected": M.sfmt(expected), "observed": M.sfmt(observed),
            "observed_source_sha256": source_sha, "difference": M.sfmt(diff),
            "status": "matched" if diff == 0 else "break", "note": note[:200]}


def not_in_use(leg_id: str, subject: str) -> dict:
    return {"leg": leg_id, "subject": subject, "expected": "0.00", "observed": None, "observed_source_sha256": None,
            "difference": None, "status": "not_in_use", "note": "no journal activity and no source (dormant rail)"}


def aging_bucket(opened_on: date, today: date) -> str:
    n = business_days_between(opened_on, today)
    return "0-1" if n <= 1 else ("2-5" if n <= 5 else ">5")


def all_matched(legs: list[dict]) -> bool:
    return all(l["status"] in ("matched", "not_in_use") for l in legs)
