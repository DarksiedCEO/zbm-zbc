"""
Intelligence 9 — Controls & SoD (Finance spec §C.9; FIN-15, FIN-19, FIN-20). Pure judgment: the control catalog and
each control's status from the evidence the service holds. A control is green only with a passing, in-SLA result;
never run = red; a control whose owner is not built never gets a result, so it stays red.

Separation of duties is enforced where it can only be enforced — at the API, by caller identity (api.py): V&I can only
read payout identity; the scheduler proposes and releases and can approve nothing; Andre approves and cannot release;
rail_gateway / bank_feed push events only; the other departments reach their protocol routes only.
"""

from __future__ import annotations

NUMBER, NAME, ACTOR = 9, "Controls & SoD", "intel_09_controls"

# id -> (test, owner, SLA hours, blocks)
CATALOG = {
    "FC-01": ("Daily reconciliation, every leg matched", "i07", 26, ("run", "release", "sweep", "refund")),
    "FC-02": ("No open break", "i07", 24, ("run", "release", "sweep", "refund")),
    "FC-03": ("Treasury invariant (journal and independent)", "i08", 24, ("run", "release", "sweep", "refund")),
    "FC-04": ("Journal integrity (chain + ledger anchor + /ledger/verify)", "i10", 24, ("run", "release", "post")),
    "FC-05": ("Access review attested", "andre", 2160, ("run", "release")),
    "FC-06": ("Tax: B-notice timers met; 1099 row in force", "i06", 168, ("run",)),
    "FC-07": ("Sanctions list current (mirror of Compliance C-04)", "compliance_38", 24, ("release",)),
    "FC-08": ("IRIS / TCC / FTB readiness by 2026-12-01", "andre", 720, ()),
    "FC-09": ("Close complete by WD5", "i01", 168, ()),
    "FC-10": ("GL tie-out (L5)", "i07", 720, ("close",)),
    "FC-11": ("Rail SOC 1 / SOC 2 Type II reports on file, <= 12 months old", "andre", 8760, ()),
    "FC-12": ("Bank controls attested: positive pay, ACH debit filter", "andre", 8760, ()),
    "FC-13": ("Fraud-risk assessment reviewed", "andre", 8760, ()),
    "FC-14": ("Control matrix (COSO 17) and deficiency log reviewed", "andre", 2160, ()),
}
ANDRE_OWNED = tuple(k for k, v in CATALOG.items() if v[1] == "andre")
