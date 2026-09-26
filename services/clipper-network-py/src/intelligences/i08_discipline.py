"""
Intelligence 8 — Discipline (spec §C.8, CN-19, CN-20).

Decides warning / suspension per the CN-19 table from V&I strikes ONLY, and
proposes a ban to Andre; never creates a strike and never bans without
Andre. A strike is acted on only when it carries finding ids AND evidence
ids that resolve at V&I: every finding exists, belongs to the same clipper
and is upheld, and every evidence id of the strike appears in a resolved
finding (else ``STRIKE_EVIDENCE_MISSING``, nothing applied). An overturned
strike lifts its consequence.
"""

from __future__ import annotations

from typing import Optional

from ports import FindingAnswer, Strike

NUMBER, NAME, ACTOR = 8, "Discipline", "intel_08_discipline"


def evidence_problem(s: Strike, findings: dict[str, FindingAnswer]) -> Optional[str]:
    if not s.finding_ids or not s.evidence_ids:
        return "the strike carries no V&I finding ids or evidence ids"
    resolved: set[str] = set()
    for fid in s.finding_ids:
        a = findings.get(fid)
        if a is None or not a.available:
            return f"V&I could not resolve finding {fid} (unavailable)"
        if a.finding is None:
            return f"V&I does not know finding {fid}"
        f = a.finding
        if f.clipper_id != s.clipper_id:
            return f"finding {fid} belongs to another clipper"
        if s.status != "overturned" and f.status != "upheld":
            return f"finding {fid} is {f.status}, not upheld"
        resolved.update(f.evidence_ids)
    missing = [e for e in s.evidence_ids if e not in resolved]
    if missing:
        return f"{len(missing)} evidence id(s) of the strike do not resolve to its findings at V&I"
    return None


def consequence(strike_class: str, table: dict) -> dict:
    return dict(table[strike_class])
