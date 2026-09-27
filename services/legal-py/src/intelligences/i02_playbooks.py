"""
Intelligence 2 — Playbook Engine (Legal spec §B.3, §C.2). Decides the deviation class of each counterparty
clause position and its escalation; never accepts past ``fallback_1`` alone and never reads counterparty paper
for meaning (no clause extraction without a model).

Playbooks are data (Ironclad's four parts: standard, fallbacks, walk-away triggers, escalation rules). A clause
position is compared by the SHA-256 of its normalized text (NFKC, case-fold, collapsed whitespace): equal to
``standard`` -> standard; to ``fallback_1``/``fallback_2`` -> that class; else ``unmatched``. A walk-away fact
that is true -> ``walk_away``; a walk-away fact that is missing or ``unknown`` escalates (fail closed). An agent
may accept a ``fallback_1`` only where the clause's escalation says ``agent`` and ``LEGAL_AGENT_MAX_FALLBACK`` >=
1; every ``fallback_2``, unmatched clause and walk-away goes to counsel (LG-04). Output is codes only and always
``unreviewed: true``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

import reasons as R
from textguard import normalize

NUMBER, NAME, ACTOR = 2, "Playbook Engine", "intel_02_playbooks"
CLAUSE_ID = re.compile(r"^[A-Z]{2,5}-[A-Z0-9]{2,12}-[0-9]{2}$")
FACT_KEY = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
POSITIONS = ("standard", "fallback_1", "fallback_2")
OBLIGATION_CODES = ("renewal_notice", "auto_renew_date", "payment_terms", "deliverable_due", "termination_notice",
                    "min_live_period", "clawback_window", "takedown_sla", "insurance_notice_of_claim",
                    "ccpa_service_provider_duty", "dsar_assist", "confidentiality_term", "data_return")
OWNERS = ("onboarding", "finance_31", "creative_production", "clipper_network", "compliance_38", "legal_37", "andre")
PARTIES = ("zbc", "zbm", "counterparty")
DUE_RULE = re.compile(r"^(offset\((?P<of>[a-z][a-z0-9_]{0,39}),(?P<sign>[+-])(?P<n>[0-9]{1,4})(?P<unit>d|bd)\)"
                      r"|fixed\((?P<ff>[a-z][a-z0-9_]{0,39})\)|per_clip)$")
TEXT_MAX = 20_000


def norm_sha(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8", "surrogatepass")).hexdigest()


def validate_descriptor(d) -> dict:
    if not isinstance(d, dict) or set(d) != {"code", "party", "owner", "due_rule", "lead_days"}:
        raise ValueError("an obligation descriptor is {code, party, owner, due_rule, lead_days}")
    if d["code"] not in OBLIGATION_CODES or d["party"] not in PARTIES or d["owner"] not in OWNERS:
        raise ValueError("obligation descriptor: unknown code, party or owner")
    if not isinstance(d["due_rule"], str) or not DUE_RULE.fullmatch(d["due_rule"]):
        raise ValueError("due_rule must be offset(<field>,±Nd|±Nbd), fixed(<field>) or per_clip")
    if isinstance(d["lead_days"], bool) or not isinstance(d["lead_days"], int) or not 0 <= d["lead_days"] <= 365:
        raise ValueError("lead_days 0..365")
    return dict(d)


def validate_clause(c) -> dict:
    """Structural check of one proposed clause (texts still inline). Raises ValueError."""
    keys = {"clause_id", "title", "standard_text", "fallback_1_text", "fallback_2_text", "walk_away", "escalation",
            "rationale_code", "obligations"}
    if not isinstance(c, dict) or set(c) != keys:
        raise ValueError(f"a clause has exactly the keys {sorted(keys)}")
    if not isinstance(c["clause_id"], str) or not CLAUSE_ID.fullmatch(c["clause_id"]):
        raise ValueError("clause_id format (e.g. MSA-CCPA-01)")
    if not isinstance(c["title"], str) or not 1 <= len(c["title"]) <= 120:
        raise ValueError("clause title 1..120 characters")
    for k in ("standard_text", "fallback_1_text", "fallback_2_text"):
        v = c[k]
        if (k == "standard_text" or v is not None) and (not isinstance(v, str) or not v.strip() or len(v) > TEXT_MAX):
            raise ValueError(f"{k}: 1..{TEXT_MAX} characters" + ("" if k == "standard_text" else " or null"))
    if c["fallback_2_text"] is not None and c["fallback_1_text"] is None:
        raise ValueError("fallback_2 needs a fallback_1")
    wa = c["walk_away"]
    if not isinstance(wa, list) or len(wa) > 20 or not all(
            isinstance(w, dict) and set(w) == {"fact", "when"} and isinstance(w["fact"], str)
            and FACT_KEY.fullmatch(w["fact"]) and w["when"] is True for w in wa):
        raise ValueError("walk_away: at most 20 {fact, when: true}")
    esc = c["escalation"]
    if not isinstance(esc, dict) or set(esc) != {"fallback_1", "fallback_2", "unmatched"} or \
            esc["fallback_1"] not in ("agent", "counsel") or esc["fallback_2"] != "counsel" or \
            esc["unmatched"] != "counsel":
        raise ValueError("escalation: fallback_1 agent|counsel; fallback_2 and unmatched are always counsel (LG-04)")
    if not isinstance(c["rationale_code"], str) or not re.fullmatch(r"^[a-z][a-z0-9_]{1,40}$", c["rationale_code"]):
        raise ValueError("rationale_code is a short code, not prose")
    obs = c["obligations"]
    if not isinstance(obs, list) or len(obs) > 20:
        raise ValueError("obligations: at most 20 descriptors")
    return {**c, "obligations": [validate_descriptor(o) for o in obs]}


def weakening(old: Optional[dict], new: dict) -> list[str]:
    """Reasons a new playbook version weakens the one in force for the same doc_type (empty for a first one)."""
    if old is None:
        return []
    out = set()
    oc = {c["clause_id"]: c for c in old["clauses"]}
    nc = {c["clause_id"]: c for c in new["clauses"]}
    if set(oc) - set(nc):
        out.add("clause_removed")
    for cid, o in oc.items():
        n = nc.get(cid)
        if n is None:
            continue
        if o["standard"]["normalized_sha256"] != n["standard"]["normalized_sha256"]:
            out.add("standard_changed")
        for pos in ("fallback_1", "fallback_2"):
            os_, ns = o.get(pos), n.get(pos)
            if ns is not None and (os_ is None or os_["normalized_sha256"] != ns["normalized_sha256"]):
                out.add("fallback_added_or_changed")
        if {w["fact"] for w in o["walk_away"]} - {w["fact"] for w in n["walk_away"]}:
            out.add("walk_away_removed")
        if o["escalation"]["fallback_1"] == "counsel" and n["escalation"]["fallback_1"] == "agent":
            out.add("escalation_loosened")
        key = lambda d: (d["code"], d["party"], d["owner"], d["due_rule"])  # noqa: E731
        if {key(d) for d in o["obligations"]} - {key(d) for d in n["obligations"]}:
            out.add("obligations_removed")
        elif any(d["lead_days"] < min((x["lead_days"] for x in o["obligations"] if key(x) == key(d)), default=0)
                 for d in n["obligations"]):
            out.add("lead_time_shortened")
    return sorted(out)


def review(playbook: Optional[dict], positions: list[dict], facts: dict, max_fallback: int,
           counterparty_paper: bool) -> dict:
    """Deviation classes and escalation for one review. ``positions``: [{clause_id, text}]."""
    per: list[dict] = []
    accepted, escalate, walk = [], [], []
    reasons: list[dict] = []
    if counterparty_paper:
        reasons.append(R.item("COUNTERPARTY_PAPER", "counterparty paper not based on our template: the whole "
                              "document is unmatched and goes to counsel"))
        return {"per_clause": [{"clause_id": p["clause_id"], "class": "unmatched"} for p in positions],
                "accepted_by_agent": [], "escalate_to_counsel": ["DOCUMENT"] + sorted({p["clause_id"] for p in positions}),
                "walk_away": [], "reasons": reasons, "unreviewed": True}
    if playbook is None:
        reasons.append(R.item("NO_APPROVED_PLAYBOOK", "no approved playbook for this document type"))
        ids = sorted({p["clause_id"] for p in positions})
        return {"per_clause": [{"clause_id": i, "class": "unmatched"} for i in ids], "accepted_by_agent": [],
                "escalate_to_counsel": ids or ["DOCUMENT"], "walk_away": [], "reasons": reasons, "unreviewed": True}
    clauses = {c["clause_id"]: c for c in playbook["clauses"]}
    seen = set()
    for p in positions:
        cid = p["clause_id"]
        if cid in seen:
            continue
        seen.add(cid)
        c = clauses.get(cid)
        h = norm_sha(p["text"])
        cls = "unmatched"
        if c is not None:
            for pos in POSITIONS:
                if c.get(pos) and c[pos]["normalized_sha256"] == h:
                    cls = pos
                    break
        per.append({"clause_id": cid, "class": cls})
        if cls == "standard":
            continue
        if cls == "fallback_1" and c["escalation"]["fallback_1"] == "agent" and max_fallback >= 1:
            accepted.append(cid)
            continue
        escalate.append(cid)
        why = {"unmatched": ("UNMATCHED_CLAUSE", f"{cid} matches no playbook position"),
               "fallback_1": ("DEVIATION_ESCALATED", f"{cid} at fallback_1 is beyond the agent's authority"),
               "fallback_2": ("DEVIATION_ESCALATED", f"{cid} at fallback_2 always goes to counsel")}[cls]
        reasons.append(R.item(*why))
    for cid, c in sorted(clauses.items()):
        for w in c["walk_away"]:
            v = facts.get(w["fact"], "unknown")
            if v is True:
                walk.append(cid)
                reasons.append(R.item("WALK_AWAY", f"{cid}: walk-away fact {w['fact']} is true"))
            elif v != False:  # noqa: E712 - missing or "unknown": escalate, never assume false
                if cid not in escalate:
                    escalate.append(cid)
                reasons.append(R.item("FACT_UNKNOWN", f"{cid}: walk-away fact {w['fact']} is unknown"))
    walk = sorted(set(walk))
    accepted = sorted(set(accepted) - set(walk))
    escalate = sorted((set(escalate) | set(walk)) - set(accepted))
    return {"per_clause": sorted(per, key=lambda x: x["clause_id"]), "accepted_by_agent": accepted,
            "escalate_to_counsel": escalate, "walk_away": walk, "reasons": reasons, "unreviewed": True}
