from datetime import datetime, timedelta, timezone

import pytest

from agents import callback_orchestration
from fulfillment_schema import FollowUpTask, TaskChannel, TaskPurpose, TaskStatus
from integrations.sip_dialer import InMemorySipDialer, NotWiredSipDialer

NOON_UTC = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
LATE_NIGHT_UTC = datetime(2026, 9, 22, 23, 0, 0, tzinfo=timezone.utc)


def _task(**overrides) -> FollowUpTask:
    defaults = dict(
        task_id="fu-t1",
        purpose=TaskPurpose.MISSED_CALL_CALLBACK,
        channel=TaskChannel.CALL,
        customer_id="cust_1",
        source_call_id="call_1",
        created_at=NOON_UTC - timedelta(minutes=1),  # fixed, never real wall-clock time
        due_at=NOON_UTC + timedelta(minutes=5),
        reason="test task",
    )
    defaults.update(overrides)
    return FollowUpTask(**defaults)


def test_pending_call_task_within_business_hours_is_dialed():
    dialer = InMemorySipDialer()
    task = _task()
    outcomes = callback_orchestration.orchestrate(
        [task], dialer,
        phone_by_call_id={"call_1": "+15550101"},
        line_by_call_id={"call_1": "line_main"},
        now=NOON_UTC,
    )
    assert len(outcomes) == 1
    assert outcomes[0].attempted is True
    assert outcomes[0].dial_result.placed is True
    assert len(dialer.calls_placed) == 1


def test_outside_business_hours_is_skipped_not_silently_dropped():
    dialer = InMemorySipDialer()
    task = _task()
    outcomes = callback_orchestration.orchestrate(
        [task], dialer,
        phone_by_call_id={"call_1": "+15550101"},
        line_by_call_id={},
        now=LATE_NIGHT_UTC,
    )
    assert outcomes[0].attempted is False
    assert "business hours" in outcomes[0].skip_reason
    assert len(dialer.calls_placed) == 0


def test_non_call_channel_task_is_ignored_by_this_agent():
    dialer = InMemorySipDialer()
    task = _task(channel=TaskChannel.SMS, purpose=TaskPurpose.ESCALATION)
    outcomes = callback_orchestration.orchestrate(
        [task], dialer, phone_by_call_id={}, line_by_call_id={}, now=NOON_UTC
    )
    assert outcomes == []  # not this agent's job at all — no outcome recorded


def test_missing_phone_number_is_skipped_with_reason():
    dialer = InMemorySipDialer()
    task = _task(source_call_id="unknown_call")
    outcomes = callback_orchestration.orchestrate(
        [task], dialer, phone_by_call_id={}, line_by_call_id={}, now=NOON_UTC
    )
    assert outcomes[0].attempted is False
    assert "no phone number" in outcomes[0].skip_reason


def test_not_wired_dialer_fails_loudly_and_is_surfaced_not_swallowed():
    """Proves the honest-gap seam actually behaves honestly: with no real
    dialer configured, the agent must record that the dialer isn't wired,
    never silently report a task as successfully called."""
    task = _task()
    outcomes = callback_orchestration.orchestrate(
        [task], NotWiredSipDialer(),
        phone_by_call_id={"call_1": "+15550101"},
        line_by_call_id={},
        now=NOON_UTC,
    )
    assert outcomes[0].attempted is False
    assert "dialer not wired" in outcomes[0].skip_reason


def test_already_completed_task_is_not_redialed():
    dialer = InMemorySipDialer()
    task = _task(status=TaskStatus.COMPLETED)
    outcomes = callback_orchestration.orchestrate(
        [task], dialer, phone_by_call_id={"call_1": "+15550101"}, line_by_call_id={}, now=NOON_UTC
    )
    assert outcomes == []


# --- regressions from the Sep 22 2026 independent review --------------------

def test_status_is_advanced_so_a_persisting_caller_wont_redial():
    """CONFIRMED finding: orchestrate() never advanced task.status, so the
    same still-PENDING task, submitted twice, was dialed twice. The fix:
    a successful attempt now comes back with status=SENT on the returned
    task. This proves the agent's OUTPUT is correct; it does not by itself
    prevent a caller who ignores that returned status from redialing —
    that's the CALLER CONTRACT documented in the module docstring, since
    this agent has no persistence layer of its own (known gap)."""
    dialer = InMemorySipDialer()
    task = _task()
    outcomes = callback_orchestration.orchestrate(
        [task], dialer, phone_by_call_id={"call_1": "+15550101"}, line_by_call_id={}, now=NOON_UTC
    )
    assert outcomes[0].task.status == TaskStatus.SENT

    # Simulate a caller that DOES persist the returned status, then rescans:
    persisted = outcomes[0].task
    outcomes2 = callback_orchestration.orchestrate(
        [persisted], dialer, phone_by_call_id={"call_1": "+15550101"}, line_by_call_id={}, now=NOON_UTC
    )
    assert outcomes2 == []  # no longer PENDING — correctly not redialed
    assert len(dialer.calls_placed) == 1  # only the first, real attempt happened


def test_a_near_term_missed_call_deadline_does_not_block_the_callback():
    """Regression guard for the review's first-pass fix, which was itself
    wrong: gating on `now < due_at` silently blocked every normal,
    on-time callback, because missed_call_detection always sets due_at a
    few minutes in the FUTURE (a deadline to call back BY, not an
    earliest-dial time). due_at must never prevent an otherwise-ready
    call-channel task from being dialed."""
    dialer = InMemorySipDialer()
    task = _task(due_at=NOON_UTC + timedelta(minutes=5))  # deadline is 5 min from "now"
    outcomes = callback_orchestration.orchestrate(
        [task], dialer, phone_by_call_id={"call_1": "+15550101"}, line_by_call_id={}, now=NOON_UTC
    )
    assert outcomes[0].attempted is True
    assert outcomes[0].sla_breached is False


def test_sla_breach_is_surfaced_not_silent():
    """A callback attempted after its due_at deadline has already passed
    is still attempted (late is better than never) but the breach is
    now visible on the outcome, not silently dropped."""
    dialer = InMemorySipDialer()
    task = _task(due_at=NOON_UTC - timedelta(minutes=30))  # deadline was 30 min ago
    outcomes = callback_orchestration.orchestrate(
        [task], dialer, phone_by_call_id={"call_1": "+15550101"}, line_by_call_id={}, now=NOON_UTC
    )
    assert outcomes[0].attempted is True
    assert outcomes[0].sla_breached is True


def test_outside_business_hours_still_reports_sla_breach():
    dialer = InMemorySipDialer()
    task = _task(due_at=NOON_UTC - timedelta(hours=1))
    outcomes = callback_orchestration.orchestrate(
        [task], dialer, phone_by_call_id={"call_1": "+15550101"}, line_by_call_id={}, now=LATE_NIGHT_UTC
    )
    assert outcomes[0].attempted is False
    assert outcomes[0].sla_breached is True
