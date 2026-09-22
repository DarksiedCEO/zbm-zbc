from datetime import datetime, timezone

from agents import appointment_tracking
from fixtures_loader import load_appointments
from fulfillment_schema import TaskChannel, TaskPurpose

FIXED_NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def test_stale_overdue_appointment_gets_call_channel_escalation():
    tasks = appointment_tracking.find_overdue(load_appointments(), now=FIXED_NOW)
    task = next(t for t in tasks if t.source_appointment_id == "appt_2001")
    assert task.channel == TaskChannel.CALL
    assert task.purpose == TaskPurpose.COMPLETION_CHECK
    assert "STALE" in task.reason


def test_recently_overdue_appointment_gets_sms_channel():
    tasks = appointment_tracking.find_overdue(load_appointments(), now=FIXED_NOW)
    task = next(t for t in tasks if t.source_appointment_id == "appt_2002")
    assert task.channel == TaskChannel.SMS
    assert "STALE" not in task.reason


def test_within_grace_period_produces_no_task():
    tasks = appointment_tracking.find_overdue(load_appointments(), now=FIXED_NOW)
    ids = {t.source_appointment_id for t in tasks}
    assert "appt_2003" not in ids


def test_completed_appointment_produces_no_task():
    tasks = appointment_tracking.find_overdue(load_appointments(), now=FIXED_NOW)
    ids = {t.source_appointment_id for t in tasks}
    assert "appt_2004" not in ids


def test_cancelled_appointment_produces_no_task():
    tasks = appointment_tracking.find_overdue(load_appointments(), now=FIXED_NOW)
    ids = {t.source_appointment_id for t in tasks}
    assert "appt_2005" not in ids


def test_future_appointment_produces_no_task():
    tasks = appointment_tracking.find_overdue(load_appointments(), now=FIXED_NOW)
    ids = {t.source_appointment_id for t in tasks}
    assert "appt_2006" not in ids


def test_exact_count_of_overdue_appointments():
    tasks = appointment_tracking.find_overdue(load_appointments(), now=FIXED_NOW)
    assert len(tasks) == 2  # appt_2001 (stale) and appt_2002 (recent)
