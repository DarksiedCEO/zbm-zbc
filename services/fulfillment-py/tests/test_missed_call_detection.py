from datetime import datetime, timezone

from agents import missed_call_detection
from fixtures_loader import load_call_events
from fulfillment_schema import CallDirection, CallEvent, CallStatus, TaskChannel, TaskPurpose


def test_answered_call_produces_no_task():
    events = load_call_events()
    tasks = missed_call_detection.detect(events)
    task_call_ids = {t.source_call_id for t in tasks}
    assert "call_1004" not in task_call_ids  # answered, control case


def test_every_unresolved_call_produces_exactly_one_task():
    events = load_call_events()
    tasks = missed_call_detection.detect(events)
    unresolved_ids = {e.call_id for e in events if e.is_unresolved}
    task_call_ids = {t.source_call_id for t in tasks}
    assert unresolved_ids == task_call_ids
    assert len(tasks) == len(unresolved_ids)


def test_tasks_are_missed_call_callback_purpose_and_call_channel():
    tasks = missed_call_detection.detect(load_call_events())
    assert all(t.purpose == TaskPurpose.MISSED_CALL_CALLBACK for t in tasks)
    assert all(t.channel == TaskChannel.CALL for t in tasks)


def test_repeat_miss_gets_tightened_callback_window():
    events = load_call_events()
    tasks = {t.source_call_id: t for t in missed_call_detection.detect(events)}

    first_call = next(e for e in events if e.call_id == "call_1001")
    second_call = next(e for e in events if e.call_id == "call_1002")

    first_window = tasks["call_1001"].due_at - first_call.started_at
    second_window = tasks["call_1002"].due_at - second_call.started_at

    assert first_window.total_seconds() == 5 * 60
    assert second_window.total_seconds() == 2 * 60  # tightened due to prior miss
    assert "prior missed call" in tasks["call_1002"].reason


def test_unmatched_caller_still_produces_a_task():
    # call_1005 has customer_id=None but is a real missed call — the
    # department must not drop it just because identity resolution hasn't
    # matched it to a customer yet (that's customer_dossier's known gap,
    # not missed_call_detection's).
    tasks = missed_call_detection.detect(load_call_events())
    task = next(t for t in tasks if t.source_call_id == "call_1005")
    assert task.customer_id is None


def test_no_events_produces_no_tasks():
    assert missed_call_detection.detect([]) == []


# --- regression from the Sep 22 2026 independent review --------------------

def test_outbound_missed_call_does_not_produce_a_callback_task():
    """CONFIRMED finding: this agent filtered on status only, never on
    direction, so an outbound call this business placed and failed to
    connect was treated as a customer contacting the business — a
    backwards, duplicate task. Only INBOUND calls are candidates."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    outbound_missed = CallEvent(
        call_id="call_out_1", customer_id="cust_y", phone_number="+15559999",
        direction=CallDirection.OUTBOUND, status=CallStatus.MISSED,
        started_at=now, line_id="line_main",
    )
    assert missed_call_detection.detect([outbound_missed], now=now) == []


def test_outbound_missed_calls_do_not_count_toward_repeat_miss_urgency():
    """An outbound miss on a number shouldn't tighten the callback window
    for a later, genuine inbound miss from that same number — the two
    are not the same kind of event."""
    now = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    outbound_missed = CallEvent(
        call_id="call_out_1", customer_id="cust_y", phone_number="+15559999",
        direction=CallDirection.OUTBOUND, status=CallStatus.MISSED,
        started_at=now, line_id="line_main",
    )
    inbound_missed = CallEvent(
        call_id="call_in_1", customer_id="cust_y", phone_number="+15559999",
        direction=CallDirection.INBOUND, status=CallStatus.MISSED,
        started_at=now, line_id="line_main",
    )
    tasks = missed_call_detection.detect([outbound_missed, inbound_missed], now=now)
    assert len(tasks) == 1
    assert "prior missed call" not in tasks[0].reason
