from datetime import datetime, timezone

import pytest

from agents import followup_sequencing
from fulfillment_schema import FollowUpTask, TaskChannel, TaskPurpose, TaskStatus

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)


def _failed_task(attempt_number: int, channel: TaskChannel, task_id: str = "fu-1") -> FollowUpTask:
    return FollowUpTask(
        task_id=task_id,
        purpose=TaskPurpose.MISSED_CALL_CALLBACK,
        channel=channel,
        customer_id="cust_1",
        source_call_id="call_1",
        due_at=NOW,
        attempt_number=attempt_number,
        status=TaskStatus.FAILED,
        reason="test",
    )


def test_attempt_1_call_escalates_to_attempt_2_sms():
    nxt = followup_sequencing.escalate(_failed_task(1, TaskChannel.CALL), now=NOW)
    assert nxt is not None
    assert nxt.channel == TaskChannel.SMS
    assert nxt.attempt_number == 2
    assert nxt.purpose == TaskPurpose.ESCALATION


def test_attempt_2_sms_escalates_to_attempt_3_email():
    nxt = followup_sequencing.escalate(_failed_task(2, TaskChannel.SMS), now=NOW)
    assert nxt.channel == TaskChannel.EMAIL
    assert nxt.attempt_number == 3


def test_attempt_3_email_escalates_to_attempt_4_human_handoff():
    nxt = followup_sequencing.escalate(_failed_task(3, TaskChannel.EMAIL), now=NOW)
    assert nxt.channel == TaskChannel.HUMAN_HANDOFF
    assert nxt.attempt_number == 4


def test_attempt_4_human_handoff_failing_does_not_invent_attempt_5():
    nxt = followup_sequencing.escalate(_failed_task(4, TaskChannel.HUMAN_HANDOFF), now=NOW)
    assert nxt is None  # sequence exhausted — a human dropped the ball, not this agent's job to paper over


def test_escalating_a_non_failed_task_raises():
    task = _failed_task(1, TaskChannel.CALL)
    task = task.model_copy(update={"status": TaskStatus.PENDING})
    with pytest.raises(ValueError, match="only FAILED tasks escalate"):
        followup_sequencing.escalate(task, now=NOW)


def test_next_task_preserves_customer_and_source_linkage():
    original = _failed_task(1, TaskChannel.CALL)
    nxt = followup_sequencing.escalate(original, now=NOW)
    assert nxt.customer_id == original.customer_id
    assert nxt.source_call_id == original.source_call_id
    assert original.task_id in nxt.task_id
