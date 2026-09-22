"""
Agent: Callback Orchestration
Single job: given a pending CALL-channel FollowUpTask, decide whether it
is due, and if so attempt the callback through the injected SipDialerPort
(ADR 0002, Decision 1/3). This agent owns the DECISION (should we dial
now, on which line) — it never owns the transport. See
integrations/sip_dialer.py for why no live dialer ships in this repo.

Business hours policy (encoded here, the agent's rule): callbacks only
fire between 08:00–20:00 in the line's local time. This build assumes
UTC-normalized fixture data and does not implement per-line timezone
resolution yet — documented as a known gap, not silently assumed away.

due_at SEMANTICS (clarified after independent review, Sep 22 2026): for
every CALL-channel task this agent actually receives — the only producer
today is missed_call_detection — due_at is a "call back by" SLA
deadline (created_at + a few minutes), not a "don't call before this
time" schedule. The review's first-pass fix (skip while now < due_at)
was WRONG and broke real behavior: it silently prevented every normal,
on-time callback from ever firing, because due_at is always a few
minutes in the FUTURE relative to when the task should be worked. That
regression was caught by this agent's own existing test suite before it
shipped. The schema has no field today for a genuinely future-scheduled
callback ("call this customer back next Tuesday") — if one is ever
added, it needs its own field (e.g. `not_before`), not an overload of
`due_at`. What IS fixed here: due_at is now surfaced as an SLA-breach
signal (OrchestrationOutcome.sla_breached) when now > due_at, so a
callback that's already blown its deadline is visible, not silent.

CALLER CONTRACT (load-bearing, read before wiring this into anything
that persists tasks): this agent is a pure function with no storage of
its own. Each OrchestrationOutcome carries an UPDATED copy of the task
(status flipped to SENT/FAILED once a dial is attempted) — the caller
MUST persist that updated status before the next scan, or the same
still-PENDING task will be dialed again. An independent review (Sep 22
2026) confirmed this exact failure by calling orchestrate() twice on the
same in-memory task and getting two real dial attempts — see
test_status_is_advanced_so_a_persisting_caller_wont_redial in the test
suite. This agent cannot prevent the repeat by itself without a
datastore, which does not exist yet (README "Known gaps" — no
orchestrator/persistence layer this pass); it can only make the correct
next state available to whoever does persist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fulfillment_schema import FollowUpTask, TaskChannel, TaskStatus
from integrations.sip_dialer import DialAttemptResult, SipDialerPort

AGENT_ID = "callback-orchestration-v1"

_BUSINESS_HOURS_START = 8
_BUSINESS_HOURS_END = 20


@dataclass(frozen=True)
class OrchestrationOutcome:
    task: FollowUpTask
    attempted: bool
    dial_result: DialAttemptResult | None
    skip_reason: str | None
    sla_breached: bool = False  # now > task.due_at at decision time — visible, never silent


def _within_business_hours(ts: datetime) -> bool:
    return _BUSINESS_HOURS_START <= ts.hour < _BUSINESS_HOURS_END


def orchestrate(
    tasks: list[FollowUpTask],
    dialer: SipDialerPort,
    *,
    phone_by_call_id: dict[str, str],
    line_by_call_id: dict[str, str],
    now: datetime | None = None,
) -> list[OrchestrationOutcome]:
    """
    phone_by_call_id / line_by_call_id: this agent works purely off
    FollowUpTask + call metadata already resolved upstream (missed-call
    detection knows the phone number and line; this agent does not
    re-derive them) — keeps this agent single-purpose: decide + dial,
    not look up call records itself.
    """
    now = now or datetime.now(timezone.utc)
    outcomes: list[OrchestrationOutcome] = []

    for task in tasks:
        if task.channel != TaskChannel.CALL:
            continue  # not this agent's job — sequencing/other channels handled elsewhere
        if task.status != TaskStatus.PENDING:
            continue

        if task.source_call_id is None or task.source_call_id not in phone_by_call_id:
            outcomes.append(OrchestrationOutcome(task, False, None, "no phone number resolvable for task"))
            continue

        if now < task.created_at:
            outcomes.append(OrchestrationOutcome(task, False, None, "task not yet active"))
            continue

        sla_breached = now > task.due_at  # visible signal, not a gate — see due_at SEMANTICS above

        if not _within_business_hours(now):
            outcomes.append(OrchestrationOutcome(task, False, None, "outside business hours (08:00-20:00)", sla_breached))
            continue

        phone = phone_by_call_id[task.source_call_id]
        line = line_by_call_id.get(task.source_call_id, "default")

        try:
            result = dialer.place_call(phone, line)
        except NotImplementedError as exc:
            outcomes.append(OrchestrationOutcome(task, False, None, f"dialer not wired: {exc}", sla_breached))
            continue

        # Independent review finding (Sep 22 2026, CONFIRMED): the same
        # still-PENDING task, submitted twice, was dialed twice — nothing
        # here ever advanced task.status. Fixed by returning the task with
        # its status flipped; see the CALLER CONTRACT note in the module
        # docstring — persistence of this new status is the caller's job.
        updated_task = task.model_copy(
            update={"status": TaskStatus.SENT if result.placed else TaskStatus.FAILED}
        )
        outcomes.append(OrchestrationOutcome(updated_task, True, result, None, sla_breached))

    return outcomes
