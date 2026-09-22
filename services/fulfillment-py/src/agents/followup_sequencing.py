"""
Agent: Follow-Up Sequencing
Single job: given a FollowUpTask that failed or went unanswered, produce
the NEXT task in the escalation sequence — never let a failed attempt be
the end of the story. This is the mechanism that turns "we tried once"
into "we resolved it or a human took over," the same completeness
commitment Numa makes.

Sequence policy (encoded here, the agent's rule):
  attempt 1 (call, missed_call_callback) -> fails ->
  attempt 2 (sms, escalation, +10 min) -> fails ->
  attempt 3 (email, escalation, +1 hour) -> fails ->
  attempt 4 (human_handoff, escalation, immediate) -> terminal, no further escalation

Independent review finding (Sep 22 2026, CONFIRMED): when attempt 4
(human_handoff) itself comes back FAILED, escalate() returns None with
nothing else — no incident, no record, no queue entry. A human dropping
the ball on the LAST resort is a worse failure than any prior step, not
one that should be quieter than they were. This agent still does not
invent an attempt 5 (that would be inventing false progress, not
resolving anything) — but it now gives callers `is_sequence_exhausted()`
so this specific case can be told apart from "task not actually failed
yet" and routed to a real incident/resolution record instead of being
silently dropped. The API layer (api.py) uses this to automatically
produce a ResolutionRecord when a sequence exhausts, so it's visible in
the write-back trail rather than vanishing at the last agent boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fulfillment_schema import FollowUpTask, TaskChannel, TaskPurpose, TaskStatus

AGENT_ID = "followup-sequencing-v1"

_MAX_ATTEMPTS_BEFORE_HANDOFF = 4

_CHANNEL_SEQUENCE: list[tuple[TaskChannel, timedelta]] = [
    (TaskChannel.CALL, timedelta(minutes=0)),       # attempt 1 (already happened upstream)
    (TaskChannel.SMS, timedelta(minutes=10)),        # attempt 2
    (TaskChannel.EMAIL, timedelta(hours=1)),         # attempt 3
    (TaskChannel.HUMAN_HANDOFF, timedelta(minutes=0)),  # attempt 4 — terminal
]


def is_sequence_exhausted(failed_task: FollowUpTask) -> bool:
    """True when this failed task is the terminal step (a failed
    human_handoff) and there is nowhere left in the sequence to escalate
    to. Callers should treat this as an incident requiring a resolution
    record and human acknowledgement, not as "nothing more to do"."""
    return (
        failed_task.status == TaskStatus.FAILED
        and failed_task.attempt_number >= _MAX_ATTEMPTS_BEFORE_HANDOFF
    )


def escalate(failed_task: FollowUpTask, *, now: datetime | None = None) -> FollowUpTask | None:
    """Returns the next task in the sequence, or None if the sequence is
    already exhausted (attempt 4 / human_handoff itself failing means a
    human dropped the ball — this agent does not invent attempt 5; that
    is an operational failure to surface, not paper over)."""
    now = now or datetime.now(timezone.utc)

    if failed_task.status not in (TaskStatus.FAILED,):
        raise ValueError(
            f"escalate() called on task {failed_task.task_id} with status "
            f"{failed_task.status.value!r} — only FAILED tasks escalate. "
            "A caller marking a task FAILED without actually having attempted "
            "it would defeat the whole completeness guarantee."
        )

    next_attempt = failed_task.attempt_number + 1
    if next_attempt > _MAX_ATTEMPTS_BEFORE_HANDOFF:
        return None

    channel, delay = _CHANNEL_SEQUENCE[next_attempt - 1]
    purpose = TaskPurpose.ESCALATION if channel != TaskChannel.CALL else failed_task.purpose

    return FollowUpTask(
        task_id=f"{failed_task.task_id}-esc{next_attempt}",
        purpose=purpose,
        channel=channel,
        customer_id=failed_task.customer_id,
        source_call_id=failed_task.source_call_id,
        source_appointment_id=failed_task.source_appointment_id,
        due_at=now + delay,
        attempt_number=next_attempt,
        reason=(
            f"escalation from {failed_task.task_id} (attempt {failed_task.attempt_number} "
            f"via {failed_task.channel.value} failed) — attempt {next_attempt} via {channel.value}"
        ),
    )
