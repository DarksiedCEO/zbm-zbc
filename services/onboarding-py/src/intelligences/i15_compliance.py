"""
Intelligence 15 — Compliance.  PHASE 1 (activation gate).

Decides: activation is BLOCKED until every requirement is met, and names
exactly which requirement is missing. One of the two activation gates
(with 14). Risk (8) spots the unexpected; 14 and 15 enforce the known.

Requirements are explicit lists per lane. Each requirement is evaluated
from facts the service supplies; a fact that is absent counts as NOT met
(fail closed). The Compliance DEPARTMENT (38) is on the roster but not
built; its stand-in rules "not allowed yet", so today this gate cannot
pass for any real subject — which is the correct answer.
"""

from __future__ import annotations

from onboarding_schema import GateResult, Lane

from ._status import PHASE1_STATUS

NUMBER = 15
NAME = "Compliance"
PHASE = 1
STATUS = PHASE1_STATUS
GATE = "compliance_15"

# requirement id -> (fact key, message when unmet)
CLIENT_REQUIREMENTS: list[tuple[str, str, str]] = [
    ("p1_ai_disclosure_sent", "p1_disclosure_sent", "first message disclosing AI identity and offering a human has not been sent"),
    ("p1_wording_counsel_approved", "p1_wording_counsel_approved", "P1 disclosure wording not yet approved by counsel (Cal. B&P Code 17941)"),
    ("p23_clause_counsel_approved", "p23_clause_counsel_approved", "P23 CCPA/CPRA clause not yet drafted/approved by counsel"),
    ("audit_baseline_complete", "baseline_done", "Audit and Baseline (6) has not produced a baseline"),
    ("merged_plan_agreed", "plan_agreed", "no merged plan, or the client has not chosen on every disagreement"),
    ("access_verified_p21", "access_verified", "no platform access verified as actually working (P21)"),
    ("permission_receipt_sent_p3", "receipts_sent", "a permission receipt (P3) has not been sent for every verified grant"),
    ("no_hard_stop", "no_hard_stop", "Risk and Anomaly (8) has an active hard stop"),
    ("no_unacknowledged_hard_escalation", "no_open_hard_escalation", "a hard escalation is open and not yet resolved by Andre"),
    ("billing_setup_p16", "billing_ready", "billing setup (P16) not confirmed by the Billing department"),
    ("compliance_department_38_ruling", "compliance_dept_allowed", "Compliance department (38) is not built — not allowed yet"),
]
CREATOR_REQUIREMENTS: list[tuple[str, str, str]] = [
    ("p1_ai_disclosure_sent", "p1_disclosure_sent", "first message disclosing AI identity and offering a human has not been sent"),
    ("p1_wording_counsel_approved", "p1_wording_counsel_approved", "P1 disclosure wording not yet approved by counsel (Cal. B&P Code 17941)"),
    ("vetting_approved_11", "vetting_approved", "Creator Vetting (11) has not approved this applicant"),
    ("age_verified_18_plus", "age_verified", "age 18+ not verified by Verification and Integrity (not built — not allowed yet)"),
    ("w9_on_file_p8", "w9_on_file", "W-9 not on file (P8): required before payout activation"),
    ("ad_disclosure_training_p9", "disclosure_training", "ad-disclosure training (P9) not completed"),
    ("compliance_department_38_ruling", "compliance_dept_allowed", "Compliance department (38) is not built — not allowed yet"),
]


def requirements_for(lane: Lane) -> list[tuple[str, str, str]]:
    return CREATOR_REQUIREMENTS if lane == Lane.ZBC_CREATOR else CLIENT_REQUIREMENTS


def gate(lane: Lane, facts: dict, extra_unmet: tuple[str, ...] = ()) -> GateResult:
    unmet = [f"{rid}: {msg}" for rid, key, msg in requirements_for(lane) if facts.get(key) is not True]
    # Detail from other departments' stand-ins (e.g. the exact "not allowed yet" text).
    for u in extra_unmet:
        if u not in unmet:
            unmet.append(u)
    return GateResult(gate=GATE, passed=not unmet, unmet=unmet)
