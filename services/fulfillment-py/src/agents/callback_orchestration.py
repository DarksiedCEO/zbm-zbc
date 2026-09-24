"""
Agent: Callback Orchestration
Single job: given a pending CALL-channel FollowUpTask, decide whether it
is due, and if so attempt the callback through the injected SipDialerPort
(ADR 0002, Decision 1/3). This agent owns the DECISION (should we dial
now, on which line) — it never owns the transport. See
integrations/sip_dialer.py for why no live dialer ships in this repo.

Contact-window policy (Sep 24 2026 audit — replaces the old "business
hours" check): a callback is only placed when `now` is inside the
configured ContactWindow (default 08:00-21:00, never wider) in the
RECIPIENT's local time zone, supplied per call via timezone_by_call_id.
Unknown or invalid time zone => not dialed (fail closed). The previous
rule was `8 <= now.hour < 20` on a UTC clock, which dialed a Los Angeles
caller at 02:00 local time (reproduced; see
tests/test_audit_2026_09_24.py). See src/contact_window.py.

Fix wave 1, F3 (Sep 24 2026, High, CONFIRMED by AEGIS): the zone above
was never checked against the number (a Los Angeles number at 02:00 local
was dialed because the caller said "UTC" or "Asia/Tokyo"), and the only
redial protection was the caller-chosen task_id (five fresh ids -> five
calls). Both decisions now belong to OutboundContactGate
(src/outbound_gate.py): the window must hold in every zone plausible for
the NUMBER plus the claimed zone, attempts are limited per phone number
and per customer (not per task), and the dialer receives a single-use
ContactAuthorization instead of a number, re-checked on the gate's clock
at the moment of dialing.

Duplicate handling (Sep 24 2026 audit): the same task_id appearing twice
in one batch is dialed at most once. A dialer that RAISES is recorded as
an attempted, FAILED outcome (the call may or may not have gone out —
unknown), never propagated: propagating used to abort the batch with a
500 after earlier tasks had already been dialed, so the caller never
learned about those dials and a retry redialed them.

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
from outbound_gate import ContactRefused, OutboundContactGate

AGENT_ID = "callback-orchestration-v1"


@dataclass(frozen=True)
class OrchestrationOutcome:
    task: FollowUpTask
    attempted: bool
    dial_result: DialAttemptResult | None
    skip_reason: str | None
    sla_breached: bool = False  # now > task.due_at at decision time — visible, never silent


def orchestrate(
    tasks: list[FollowUpTask],
    dialer: SipDialerPort,
    *,
    phone_by_call_id: dict[str, str],
    line_by_call_id: dict[str, str],
    timezone_by_call_id: dict[str, str],
    gate: OutboundContactGate,
    now: datetime | None = None,
) -> list[OrchestrationOutcome]:
    """
    phone_by_call_id / line_by_call_id / timezone_by_call_id: this agent
    works purely off FollowUpTask + call metadata already resolved
    upstream (missed-call detection knows the phone number and line; the
    recipient's IANA time zone comes from whoever knows the customer) —
    keeps this agent single-purpose: decide + dial, not look up call
    records itself. timezone_by_call_id and gate are required keywords on
    purpose: there is no default that could silently dial. The claimed zone
    is only an input to the gate, which checks it against the number. `now`
    decides task activity and SLA breach only; whether contact is permitted
    is decided by the gate on its own clock, per task, at dial time.
    """
    now = now or datetime.now(timezone.utc)
    outcomes: list[OrchestrationOutcome] = []
    seen_task_ids: set[str] = set()

    for task in tasks:
        if task.channel != TaskChannel.CALL:
            continue  # not this agent's job — sequencing/other channels handled elsewhere
        if task.status != TaskStatus.PENDING:
            continue

        if task.task_id in seen_task_ids:
            outcomes.append(OrchestrationOutcome(task, False, None, "duplicate task_id in this batch — not dialed twice"))
            continue
        seen_task_ids.add(task.task_id)

        if task.source_call_id is None or task.source_call_id not in phone_by_call_id:
            outcomes.append(OrchestrationOutcome(task, False, None, "no phone number resolvable for task"))
            continue

        if now < task.created_at:
            outcomes.append(OrchestrationOutcome(task, False, None, "task not yet active"))
            continue

        sla_breached = now > task.due_at  # visible signal, not a gate — see due_at SEMANTICS above

        decision = gate.authorize(
            channel=TaskChannel.CALL,
            phone=phone_by_call_id[task.source_call_id],
            claimed_tz=timezone_by_call_id.get(task.source_call_id),
            customer_id=task.customer_id,
        )
        if not decision.allowed:
            outcomes.append(OrchestrationOutcome(task, False, None, decision.reason, sla_breached))
            continue

        line = line_by_call_id.get(task.source_call_id, "default")

        try:
            result = dialer.place_call(decision.authorization, line)
        except NotImplementedError as exc:
            outcomes.append(OrchestrationOutcome(task, False, None, f"dialer not wired: {exc}", sla_breached))
            continue
        except ContactRefused as exc:
            # Re-checked at dial time and refused (e.g. the window closed while
            # earlier calls in this batch were being placed). Nothing was dialed.
            outcomes.append(OrchestrationOutcome(task, False, None, str(exc), sla_breached))
            continue
        except Exception as exc:  # noqa: BLE001 — any transport error must become an outcome, not a 500
            # Outcome unknown: the call may have gone out. Mark FAILED (not
            # PENDING) so nothing redials it automatically; escalation takes
            # over from here. Only the exception TYPE is surfaced — a real
            # dialer's message may contain the dialed number.
            failed = task.model_copy(update={"status": TaskStatus.FAILED})
            outcomes.append(OrchestrationOutcome(
                failed, True, None, f"dialer raised {type(exc).__name__}; outcome unknown", sla_breached,
            ))
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
