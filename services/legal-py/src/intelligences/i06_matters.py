"""
Intelligence 6 — Matter Triage & Holds (Legal spec §B.7, §C.6). Decides severity x likelihood, the route and
whether a hold issues; never decides an ambiguous trigger (it escalates, and a hold is issued: preserve first,
CQ-25) and never releases a hold (Andre, with a filed counsel memo).

Severity (LR design): S1 agency letter or subpoena; S2 litigation threat or demand letter; S3 IP claim; S4
contract dispute >= LEGAL_DISPUTE_THRESHOLD; S5 privacy request or data incident; S6 routine contract (and a
dispute below the threshold); S7 question. Likelihood from typed facts (counterparty_represented,
deadline_stated, prior_dispute; an unknown counts as true): 0 -> L1, 1 -> L2, 2+ -> L3.
Routes (LG-08): S1, S2, a data incident or any class-action flag -> counsel_same_day + hold now; S3, S4, S5
(non-incident) -> counsel_standard, hold at L2+; S6 -> playbook_lane; S7 -> template_lane (a routing notice, never
an answer). Any typed fact ``unknown`` -> counsel_same_day + hold (AMBIGUOUS_ESCALATED).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import reasons as R
from bizdays import BusinessCalendar

NUMBER, NAME, ACTOR = 6, "Matter Triage & Holds", "intel_06_matters"
KINDS = ("agency_letter", "subpoena", "litigation_threat", "demand_letter", "ip_claim", "contract_dispute",
         "privacy_request", "data_incident", "routine_contract", "question")
INTERNAL_KINDS = ("filing_lapsed",)
LIKELIHOOD_FACTS = ("counterparty_represented", "deadline_stated", "prior_dispute")
TRI_FACTS = LIKELIHOOD_FACTS + ("class_action_threat",)
SYSTEMS = ("email", "chat", "drive", "legal_store", "finance_log", "vi_evidence", "cn_records", "creative_store")
SYSTEM_OWNER = {"email": "cybersecurity_22", "chat": "cybersecurity_22", "drive": "cybersecurity_22",
                "legal_store": "legal_37", "finance_log": "finance_31", "vi_evidence": "verification_integrity",
                "cn_records": "clipper_network", "creative_store": "creative_production"}
SEVERITY = {"agency_letter": "S1", "subpoena": "S1", "litigation_threat": "S2", "demand_letter": "S2",
            "ip_claim": "S3", "privacy_request": "S5", "data_incident": "S5", "routine_contract": "S6",
            "question": "S7", "filing_lapsed": "S6"}


def triage(kind: str, facts: dict, amount: Optional[Decimal], threshold: Decimal) -> dict:
    reasons = []
    ambiguous = [k for k in TRI_FACTS if facts.get(k, False) == "unknown"]
    if kind == "contract_dispute" and amount is None:
        ambiguous.append("amount_usd")
    truthy = sum(1 for k in LIKELIHOOD_FACTS if facts.get(k, False) in (True, "unknown"))
    likelihood = "L1" if truthy == 0 else "L2" if truthy == 1 else "L3"
    if kind == "contract_dispute":
        severity = "S4" if amount is None or amount >= threshold else "S6"
    else:
        severity = SEVERITY[kind]
    class_action = facts.get("class_action_threat") is True
    if ambiguous:
        route, hold = "counsel_same_day", True
        reasons.append(R.item("AMBIGUOUS_ESCALATED", f"typed fact(s) unknown ({', '.join(sorted(ambiguous))}): "
                              "escalated to counsel and a hold issued", cq_id="CQ-25"))
    elif severity in ("S1", "S2") or kind == "data_incident" or class_action:
        route, hold = "counsel_same_day", True
        reasons.append(R.item("COUNSEL_SAME_DAY", f"{kind} ({severity}): counsel the same day and a hold issued"))
    elif kind == "filing_lapsed":
        route, hold = "counsel_standard", False
        reasons.append(R.item("COUNSEL_STANDARD", "a filing lapsed: routed to counsel"))
    elif severity in ("S3", "S4", "S5"):
        route, hold = "counsel_standard", likelihood in ("L2", "L3")
        reasons.append(R.item("COUNSEL_STANDARD", f"{kind} ({severity}, {likelihood}): routed to counsel"
                              + ("; hold issued" if hold else "")))
    elif severity == "S6":
        route, hold = "playbook_lane", False
    else:
        route, hold = "template_lane", False
        reasons.append(R.item("ROUTE_ONLY", "a question is routed with the routing notice; Legal answers no "
                              "legal question", cq_id="CQ-15"))
    return {"severity": severity, "likelihood": likelihood, "route": route, "hold_required": hold,
            "ambiguous": sorted(ambiguous), "reasons": reasons}


def dsar_deadlines(received: date, cal: BusinessCalendar, params: dict) -> dict:
    """LG-16 clocks (secondary source, UNVERIFIED): confirm, respond, extended respond, hard maximum."""
    return {"dsar_confirm_by": cal.add(received, int(params.get("confirm_business_days", 10))).isoformat(),
            "dsar_respond_by": (received + timedelta(days=int(params.get("respond_days", 45)))).isoformat(),
            "dsar_extended_by": (received + timedelta(days=int(params.get("respond_days", 45))
                                                      + int(params.get("extension_days", 45)))).isoformat(),
            "dsar_max": (received + timedelta(days=int(params.get("max_days", 90)))).isoformat()}
