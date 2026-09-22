from safety.calibration_drift import ResolvedFindingOutcome, check_drift
from zbm_schema import DecisionConfidence


def _outcomes(agent_id: str, band: DecisionConfidence, correct_count: int, wrong_count: int):
    out = []
    for i in range(correct_count):
        out.append(ResolvedFindingOutcome(finding_id=f"{agent_id}-c{i}", agent_id=agent_id, confidence=band, was_correct=True))
    for i in range(wrong_count):
        out.append(ResolvedFindingOutcome(finding_id=f"{agent_id}-w{i}", agent_id=agent_id, confidence=band, was_correct=False))
    return out


def test_no_drift_when_accuracy_meets_the_bands_promise():
    # VERY_HIGH requires >=95% — 19/20 correct = 95.0%, right at the floor
    outcomes = _outcomes("affiliate-coupon-extension-v1", DecisionConfidence.VERY_HIGH, 19, 1)
    flags = check_drift(outcomes)
    assert flags == []


def test_drift_flagged_when_high_confidence_underperforms():
    # HIGH requires >=85% — 15/20 correct = 75%, below the floor
    outcomes = _outcomes("discount-misuse-v1", DecisionConfidence.HIGH, 15, 5)
    flags = check_drift(outcomes)
    assert len(flags) == 1
    f = flags[0]
    assert f.agent_id == "discount-misuse-v1"
    assert f.confidence_band == DecisionConfidence.HIGH
    assert f.observed_accuracy == 0.75


def test_small_sample_does_not_trigger_a_drift_flag():
    # Only 5 samples, all wrong — but below MIN_SAMPLE_SIZE_FOR_DRIFT_CHECK (10),
    # so this must NOT be flagged. Small samples aren't trustworthy evidence.
    outcomes = _outcomes("renewal-never-triggered-v1", DecisionConfidence.VERY_HIGH, 0, 5)
    flags = check_drift(outcomes)
    assert flags == []


def test_multiple_agents_and_bands_tracked_independently():
    outcomes = (
        _outcomes("agent-a", DecisionConfidence.HIGH, 18, 2)  # 90% — fine
        + _outcomes("agent-b", DecisionConfidence.HIGH, 12, 8)  # 60% — drifted
    )
    flags = check_drift(outcomes)
    assert len(flags) == 1
    assert flags[0].agent_id == "agent-b"
