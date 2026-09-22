"""
Calibration Drift Agent — sole job: watch whether an agent's confidence
scores stay honest over time, and flag when they don't (Failure Mode #4).

Real calibration checking requires a ground-truth outcome per finding
(did a HIGH-confidence finding actually turn out correct?), which only
exists once client feedback and reconciliation have happened — this
agent's INPUT is that resolved history, not raw findings. What it does
tonight is real: given a resolved history, it computes each confidence
band's observed accuracy and flags a band whose accuracy has fallen
below what that band promises. It does not yet have live client history
to run against (none exists — pre-revenue), so it is exercised here
against a synthetic-but-labeled-as-such resolved history in tests only.
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel

from zbm_schema import DecisionConfidence

# The accuracy a confidence band is supposed to deliver, at minimum, for
# calibration to be considered honest. These are the floors — an agent
# claiming VERY_HIGH confidence should be right at least 95% of the time
# it says so, or its calibration has drifted.
EXPECTED_MIN_ACCURACY = {
    DecisionConfidence.LOW: 0.50,
    DecisionConfidence.MEDIUM: 0.70,
    DecisionConfidence.HIGH: 0.85,
    DecisionConfidence.VERY_HIGH: 0.95,
}

MIN_SAMPLE_SIZE_FOR_DRIFT_CHECK = 10  # don't flag drift off a tiny sample


class ResolvedFindingOutcome(BaseModel):
    finding_id: str
    agent_id: str
    confidence: DecisionConfidence
    was_correct: bool  # resolved via client feedback + reconciliation, not a guess


class DriftFlag(BaseModel):
    agent_id: str
    confidence_band: DecisionConfidence
    sample_size: int
    observed_accuracy: float
    expected_min_accuracy: float


def check_drift(outcomes: list[ResolvedFindingOutcome]) -> list[DriftFlag]:
    by_agent_band: dict[tuple[str, DecisionConfidence], list[bool]] = defaultdict(list)
    for o in outcomes:
        by_agent_band[(o.agent_id, o.confidence)].append(o.was_correct)

    flags: list[DriftFlag] = []
    for (agent_id, band), results in by_agent_band.items():
        if len(results) < MIN_SAMPLE_SIZE_FOR_DRIFT_CHECK:
            continue  # Decision 4 discipline: don't call drift off a sample too small to trust
        accuracy = sum(results) / len(results)
        expected = EXPECTED_MIN_ACCURACY[band]
        if accuracy < expected:
            flags.append(
                DriftFlag(
                    agent_id=agent_id, confidence_band=band, sample_size=len(results),
                    observed_accuracy=round(accuracy, 4), expected_min_accuracy=expected,
                )
            )

    return flags
