import pytest

from safety.action_execution import ActionRefused, ActionRequest, execute
from safety.trust_graduation import ActionTypeHistory, AutonomyMode


def _history(mode: AutonomyMode) -> ActionTypeHistory:
    return ActionTypeHistory(client_id="client_a1", action_type="pause_discount_code", current_mode=mode)


def test_refuses_when_client_is_still_in_shadow_mode():
    req = ActionRequest(client_id="client_a1", action_type="pause_discount_code", finding_id="disc-ord_1003", description="pause")
    with pytest.raises(ActionRefused, match="shadow"):
        execute(req, _history(AutonomyMode.SHADOW))


def test_refuses_when_client_is_in_human_approval_mode():
    req = ActionRequest(client_id="client_a1", action_type="pause_discount_code", finding_id="disc-ord_1003", description="pause")
    with pytest.raises(ActionRefused, match="human_approval"):
        execute(req, _history(AutonomyMode.HUMAN_APPROVAL))


def test_refuses_when_history_is_for_a_different_client():
    req = ActionRequest(client_id="client_a1", action_type="pause_discount_code", finding_id="disc-ord_1003", description="pause")
    mismatched = ActionTypeHistory(client_id="client_b2", action_type="pause_discount_code", current_mode=AutonomyMode.AUTONOMOUS)
    with pytest.raises(ActionRefused, match="does not match"):
        execute(req, mismatched)


def test_refuses_when_history_is_for_a_different_action_type():
    req = ActionRequest(client_id="client_a1", action_type="pause_discount_code", finding_id="disc-ord_1003", description="pause")
    mismatched = ActionTypeHistory(client_id="client_a1", action_type="re_enable_tracking_tag", current_mode=AutonomyMode.AUTONOMOUS)
    with pytest.raises(ActionRefused, match="does not match"):
        execute(req, mismatched)


def test_passes_the_gate_when_autonomous_but_reports_no_live_connector():
    req = ActionRequest(client_id="client_a1", action_type="pause_discount_code", finding_id="disc-ord_1003", description="pause")
    result = execute(req, _history(AutonomyMode.AUTONOMOUS))
    assert result.executed is False  # honest: gate passed, but nothing real to execute against yet
    assert "no live platform connector" in result.note
