"""
Intelligence 6 — Audit and Baseline.

Decides: the client's baseline and every finding labeled with evidence and
confidence — via Revenue Recovery. It never re-implements a detection
agent: the service layer calls detection-py's real HTTP routes through
``integrations.revenue_recovery`` and hands the raw Finding JSON here.

Rules:
- Each finding is parsed into ``ConsumedFinding``; ``amount_usd`` becomes a
  Decimal money value per BUILD_CONTRACTS.md section 1. A finding that does
  not parse (e.g. a dollar figure without both labels) is REJECTED and
  listed, never guessed at or shown to a client.
- Entities claimed by 2+ agents (Revenue Recovery's own /correlation/overlaps
  result) are flagged ``double_count_risk`` and excluded from totals.
- Totals are kept per value classification; observed and attributed
  dollars are never added into one number.
- Findings with cause_certainty "uncertain" are listed by id, and any value
  they carry is left out of the totals.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import ValidationError

from onboarding_schema import Baseline, ConsumedFinding

from ._status import PHASE1_STATUS

NUMBER = 6
NAME = "Audit and Baseline"
PHASE = 1
STATUS = PHASE1_STATUS


def consume(raw_findings: list[dict], overlaps: dict[str, list[dict]]) -> tuple[list[ConsumedFinding], list[str]]:
    overlap_entities = set(overlaps or {})
    parsed: list[ConsumedFinding] = []
    rejected: list[str] = []
    for raw in raw_findings:
        try:
            f = ConsumedFinding.model_validate(raw)
        except ValidationError:
            rejected.append(str(raw.get("finding_id", "<no id>"))[:128] if isinstance(raw, dict) else "<not an object>")
            continue
        if f.entity_id in overlap_entities:
            f = f.model_copy(update={"double_count_risk": True})
        parsed.append(f)
    return parsed, rejected


def baseline(findings: list[ConsumedFinding]) -> Baseline:
    by_cat: dict[str, int] = {}
    totals: dict[str, Decimal] = {}
    uncertain, double, excluded = [], set(), []
    for f in findings:
        by_cat[f.leak_category] = by_cat.get(f.leak_category, 0) + 1
        if f.cause_certainty != "named":
            uncertain.append(f.finding_id)
        if f.double_count_risk:
            double.add(f.entity_id)
        if f.recoverable_value is None:
            continue
        if f.double_count_risk or f.cause_certainty != "named":
            excluded.append(f.finding_id)
            continue
        k = f.recoverable_value.classification.value
        totals[k] = totals.get(k, Decimal("0.00")) + f.recoverable_value.amount_usd
    return Baseline(
        finding_count=len(findings), by_category=by_cat, totals_by_classification=totals,
        uncertain_findings=uncertain, double_count_entities=sorted(double), excluded_from_totals=excluded,
    )
