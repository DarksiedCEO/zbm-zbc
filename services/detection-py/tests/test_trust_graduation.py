from datetime import date, timedelta

from safety.trust_graduation import ActionTypeHistory, AutonomyMode, evaluate


def _shadow(decisions: int, agreed: int) -> ActionTypeHistory:
    return ActionTypeHistory(
        client_id="client_a1", action_type="pause_discount_code",
        current_mode=AutonomyMode.SHADOW,
        shadow_decisions_made=decisions, shadow_decisions_agreed_with_human=agreed,
    )


def test_shadow_mode_no_history_not_eligible():
    d = evaluate(_shadow(0, 0))
    assert d.eligible_for_promotion is False


def test_shadow_mode_below_threshold_not_eligible():
    d = evaluate(_shadow(19, 19))  # 100% agreement but only 19 decisions
    assert d.eligible_for_promotion is False


def test_shadow_mode_low_agreement_not_eligible():
    d = evaluate(_shadow(25, 20))  # 80% agreement, enough decisions but below 95%
    assert d.eligible_for_promotion is False


def test_shadow_mode_meets_exact_threshold_is_eligible():
    d = evaluate(_shadow(20, 19))  # 95.0% exactly, 20 decisions
    assert d.eligible_for_promotion is True


def _human_approval(executions: int, reversals: int, days: int) -> ActionTypeHistory:
    window_start = date(2026, 5, 1)
    return ActionTypeHistory(
        client_id="client_a1", action_type="pause_discount_code",
        current_mode=AutonomyMode.HUMAN_APPROVAL,
        human_approved_executions=executions,
        human_approved_reversals_or_complaints=reversals,
        human_approval_window_start=window_start,
        human_approval_window_end=window_start + timedelta(days=days),
    )


def test_human_approval_below_execution_count_not_eligible():
    d = evaluate(_human_approval(29, 0, 35))
    assert d.eligible_for_promotion is False


def test_human_approval_with_reversal_not_eligible_even_with_enough_executions():
    d = evaluate(_human_approval(40, 1, 35))
    assert d.eligible_for_promotion is False
    assert "reversal" in d.reason


def test_human_approval_window_too_short_not_eligible():
    d = evaluate(_human_approval(40, 0, 10))
    assert d.eligible_for_promotion is False


def test_human_approval_meets_all_three_conditions_is_eligible():
    d = evaluate(_human_approval(30, 0, 30))
    assert d.eligible_for_promotion is True


def test_already_autonomous_never_eligible_for_further_promotion():
    h = ActionTypeHistory(
        client_id="client_a1", action_type="pause_discount_code",
        current_mode=AutonomyMode.AUTONOMOUS,
    )
    d = evaluate(h)
    assert d.eligible_for_promotion is False


# Fix wave 3 (noticed during the N6 safety-module sweep): agreement counts
# larger than the decision count made the agreement rate exceed 100% and
# could graduate an action type on impossible history (20 decisions, 40
# "agreements" -> 200%). An inconsistent history is now rejected at
# construction instead of being judged.
def test_more_agreements_than_decisions_is_rejected_not_graduated():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ActionTypeHistory(client_id="c", action_type="a", current_mode=AutonomyMode.SHADOW,
                          shadow_decisions_made=20, shadow_decisions_agreed_with_human=40)


def test_approval_window_ending_before_it_starts_is_rejected():
    import pytest
    from datetime import date
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ActionTypeHistory(client_id="c", action_type="a", current_mode=AutonomyMode.HUMAN_APPROVAL,
                          human_approved_executions=30, human_approval_window_start=date(2026, 7, 1),
                          human_approval_window_end=date(2026, 6, 1))
