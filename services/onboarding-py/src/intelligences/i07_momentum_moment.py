"""
Intelligence 7 — Momentum Moment.  PHASE 2 (scoring).

Decides: the ONE first win to deliver — highest confidence and
visibility, fastest, lowest risk — and never a win it can't prove.

Rules:
- Provable = has a LabeledValue whose classification is ``observed`` or
  ``financially_verified`` AND whose confidence is high or very_high, a
  NAMED cause, and no double-count risk. Anything else is not eligible,
  however large.
- Score (explicit): 3 x confidence rank + 2 x visibility - days to deliver
  - 2 x risk. Per-category visibility / days / risk are dated knowledge
  in ``CATEGORY_TRAITS`` (a draft, to be tuned with real outcomes; changes
  go through the playbook approval path).
- No eligible candidate => no pick, with the reason. The agent does not
  invent a win to have something to show.

Not certified for real clients until phase 1 is certified and this module
passes all four certification types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from onboarding_schema import CONFIDENCE_RANK, ConsumedFinding, DecisionConfidence, ValueClassification

from ._status import PHASE2_STATUS

NUMBER = 7
NAME = "Momentum Moment"
PHASE = 2
STATUS = PHASE2_STATUS

TRAITS_VERSION = "2026-09-24.draft1"
# category: (visibility 1-3, days to deliver, risk 1-3)
CATEGORY_TRAITS = {
    "abandoned_cart_coverage": (3, 3, 1),
    "renewal_never_triggered": (3, 5, 1),
    "discount_misuse": (2, 5, 2),
    "affiliate_coupon_extension": (2, 7, 2),
    "server_side_attribution_gap": (1, 10, 2),
    "platform_integration_gap": (1, 7, 2),
    "contract_pricing_term_drift": (2, 14, 3),
    "cross_channel_misattribution_risk": (1, 21, 3),
}
PROVABLE_CLASSIFICATIONS = {ValueClassification.OBSERVED, ValueClassification.FINANCIALLY_VERIFIED}


def provable(f: ConsumedFinding) -> tuple[bool, str]:
    v = f.recoverable_value
    if v is None:
        return False, "no labeled value"
    if v.classification not in PROVABLE_CLASSIFICATIONS:
        return False, f"value is {v.classification.value}, not observed/financially verified"
    if CONFIDENCE_RANK[v.confidence] < CONFIDENCE_RANK[DecisionConfidence.HIGH]:
        return False, f"confidence {v.confidence.value} is below high"
    if f.cause_certainty != "named":
        return False, "cause is uncertain"
    if f.double_count_risk:
        return False, "double-count risk"
    return True, "provable"


@dataclass(frozen=True)
class MomentumPick:
    finding_id: Optional[str]
    score: Optional[int]
    reason: str
    rejected: dict


def pick(findings: list[ConsumedFinding]) -> MomentumPick:
    rejected: dict[str, str] = {}
    scored: list[tuple[int, str, ConsumedFinding]] = []
    for f in findings:
        ok, why = provable(f)
        if not ok:
            rejected[f.finding_id] = why
            continue
        vis, days, risk = CATEGORY_TRAITS.get(f.leak_category, (1, 30, 3))
        score = 3 * CONFIDENCE_RANK[f.recoverable_value.confidence] + 2 * vis - days - 2 * risk
        scored.append((score, f.finding_id, f))
    if not scored:
        return MomentumPick(None, None, "no provable win yet; we will not claim one", rejected)
    scored.sort(key=lambda t: (-t[0], t[1]))
    best_score, best_id, best = scored[0]
    return MomentumPick(best_id, best_score, f"highest-scoring provable win ({best.leak_category})", rejected)
