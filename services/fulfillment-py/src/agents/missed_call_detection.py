"""
Agent: Missed-Call Detection
Single job: given a batch of CallEvents, find the ones the business did
not actually connect on (missed, voicemail, no-answer, busy) and emit a
FollowUpTask so nothing goes unanswered. This is the entry point into the
whole department — every downstream agent (callback orchestration,
sequencing, resolution) exists to make sure a task created here reaches
a real resolution, not silence.

Urgency policy (encoded here, the agent's rule): the due_at window
shrinks with each missed attempt on the same phone number within a
lookback window, on the theory that a customer who has now missed twice
is more likely to give up and call a competitor next.

Independent review finding (Sep 22 2026, CONFIRMED): this agent filtered
on CallEvent.status only, never on CallEvent.direction — an OUTBOUND
call this business placed and failed to connect (e.g. a callback attempt
that itself went unanswered) was being treated as a customer contacting
the business and generating a duplicate, backwards "callback" task.
Fixed: only INBOUND calls are candidates for missed-call detection. An
outbound dial failure is the callback agent's problem (it already
reports dial_result.placed=False for that), not this agent's.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone

from fulfillment_schema import CallDirection, CallEvent, FollowUpTask, TaskChannel, TaskPurpose

AGENT_ID = "missed-call-detection-v1"

# First missed call: call back within 5 minutes. Each additional missed
# call from the same number in the last 24h tightens the window.
_BASE_CALLBACK_WINDOW = timedelta(minutes=5)
_TIGHTENED_CALLBACK_WINDOW = timedelta(minutes=2)
_REPEAT_LOOKBACK = timedelta(hours=24)


def _mask_phone(phone: str) -> str:
    """Last four digits only. `reason` is free text that ends up in logs,
    CRM notes and dashboards; the full number is already reachable
    structurally via source_call_id (Sep 24 2026 audit)."""
    return f"***{phone[-4:]}" if len(phone) > 4 else "***"


def detect(call_events: list[CallEvent], *, now: datetime | None = None) -> list[FollowUpTask]:
    now = now or datetime.now(timezone.utc)
    tasks: list[FollowUpTask] = []

    # Sep 24 2026 audit: the same CallEvent delivered twice (webhook retry,
    # overlapping scans) produced two identical "fu-{call_id}" tasks, which
    # a caller would then dial twice. First occurrence of a call_id wins.
    unique: dict[str, CallEvent] = {}
    for ev in call_events:
        unique.setdefault(ev.call_id, ev)
    call_events = list(unique.values())

    # Repeat-miss urgency: per number, the sorted start times of its inbound
    # unresolved calls. Fix wave 4: this used to rescan every sibling for
    # every event — quadratic, 3.47 s of CPU for a 1000-call batch from one
    # number. Now two binary searches per event.
    miss_times: dict[str, list[datetime]] = {}
    for ev in call_events:
        if ev.direction == CallDirection.INBOUND and ev.is_unresolved:
            miss_times.setdefault(ev.phone_number, []).append(ev.started_at)
    for times in miss_times.values():
        times.sort()

    for ev in call_events:
        if ev.direction != CallDirection.INBOUND:
            continue  # an outbound dial failure is not a customer missing us
        if not ev.is_unresolved:
            continue

        # Other inbound unresolved calls from this number with
        # ev.started_at - 24h <= started_at <= ev.started_at (call_ids are
        # unique here, so "- 1" removes exactly this event itself).
        times = miss_times[ev.phone_number]
        prior_misses = (
            bisect_right(times, ev.started_at) - bisect_left(times, ev.started_at - _REPEAT_LOOKBACK) - 1
        )
        window = _TIGHTENED_CALLBACK_WINDOW if prior_misses > 0 else _BASE_CALLBACK_WINDOW

        reason = (
            f"{ev.status.value} inbound call from {_mask_phone(ev.phone_number)} on line {ev.line_id}"
            + (f", {prior_misses} prior missed call(s) from this number in the last 24h"
               if prior_misses else "")
        )

        tasks.append(
            FollowUpTask(
                task_id=f"fu-{ev.call_id}",
                purpose=TaskPurpose.MISSED_CALL_CALLBACK,
                channel=TaskChannel.CALL,
                customer_id=ev.customer_id,
                source_call_id=ev.call_id,
                due_at=ev.started_at + window,
                attempt_number=1,
                reason=reason,
            )
        )

    return tasks
