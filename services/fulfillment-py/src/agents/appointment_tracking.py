"""
Agent: Appointment-Completion Tracking
Single job: find appointments whose scheduled time has passed with no
completion signal, and emit a FollowUpTask to chase that signal down —
Vocca's core insight: a booking is not the finish line, a confirmed
outcome is.

Policy (encoded here, the agent's rule): a completion-check task fires
starting 2 hours after the scheduled time (gives a real job time to run
long); past 24 hours still unconfirmed, it's treated as needing
escalation-priority (call) follow-up rather than a routine (SMS) check.

Independent review finding (Sep 22 2026, CONFIRMED — docstring/comment
accuracy, not a logic bug): the previous wording here said "a second
pass finds the SAME appointment still unconfirmed," implying this
function tracks state across calls. It does not — find_overdue() is a
pure function with no memory of prior scans; "stale" is computed fresh
each call purely from elapsed time (now - scheduled_at >= 24h), not from
counting passes. Corrected the description to match the actual code.
Because there's no pass-tracking, this function also emits the SAME
task_id ("ct-{appointment_id}") every time it's called on the same
still-overdue appointment — a caller invoking this on a schedule without
deduplicating by task_id will see repeat, identically-IDed tasks, not
one task that escalates in place. That's consistent with every other
agent in this department (none of them persist state — see README
"Known gaps," no orchestrator/persistence layer this pass), but is
called out explicitly here since the old docstring implied otherwise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fulfillment_schema import Appointment, FollowUpTask, TaskChannel, TaskPurpose

AGENT_ID = "appointment-tracking-v1"

_GRACE_PERIOD = timedelta(hours=2)
_STALE_THRESHOLD = timedelta(hours=24)  # unconfirmed this long past scheduled_at -> higher urgency


def find_overdue(appointments: list[Appointment], *, now: datetime | None = None) -> list[FollowUpTask]:
    now = now or datetime.now(timezone.utc)
    tasks: list[FollowUpTask] = []

    for appt in appointments:
        if not appt.is_overdue_for_confirmation(now):
            continue
        elapsed = now - appt.scheduled_at
        if elapsed < _GRACE_PERIOD:
            continue  # still within the grace period — not overdue yet

        is_stale = elapsed >= _STALE_THRESHOLD
        tasks.append(
            FollowUpTask(
                task_id=f"ct-{appt.appointment_id}",
                purpose=TaskPurpose.COMPLETION_CHECK,
                channel=TaskChannel.SMS if not is_stale else TaskChannel.CALL,
                customer_id=appt.customer_id,
                source_appointment_id=appt.appointment_id,
                due_at=now,
                attempt_number=1,
                reason=(
                    f"appointment {appt.appointment_id} scheduled {appt.scheduled_at.isoformat()} "
                    f"for '{appt.service_type}' still status={appt.status.value} "
                    f"{elapsed} after scheduled time"
                    + (" — STALE, escalation-priority" if is_stale else "")
                ),
            )
        )

    return tasks
