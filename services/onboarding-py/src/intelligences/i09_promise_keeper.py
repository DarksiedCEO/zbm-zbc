"""
Intelligence 9 — Promise Keeper (P2 promise ledger, P7 quiet hours).

Decides: when to nudge Andre, when to warn a client BEFORE a deadline
slips, and when not to message (quiet hours, the noon cutoff).

Rules:
- Escalation commitments (locked): before the cutoff (12:00 noon
  America/Los_Angeles, configurable) the client hears "Andre will get back
  to you today"; at or after it, "first thing tomorrow". Never "shortly".
  Exactly 12:00:00 is NOT before the cutoff, so it gets "first thing
  tomorrow". "Today" is due at 17:00 local; "first thing tomorrow" is due
  at 09:00 local the next calendar day (ADR 0004 choices; weekends and
  holidays are not special-cased — open item). All wall-clock math is done
  in the cutoff time zone with zoneinfo, so DST changes are handled.
- Roll rule: a "today" escalation Andre has not engaged by the cutoff on
  its due day rolls to next day — the client is told the new real time
  ("first thing tomorrow") at that moment, which is before the old one passes.
- Nudge Andre ``andre_nudge_lead_hours`` (default 3h) before due if the
  commitment is still open and not confirmed on track.
- Warn the client ``client_warn_lead_hours`` (default 1h) before due, with a
  new real time, if it is still not confirmed on track. The warning time is
  moved to the latest moment OUTSIDE the client's quiet hours (client's own
  time zone) that is still before due. If no such moment exists, the
  warning goes out anyway, flagged ``quiet_hours_override`` — a promise
  silently passing is worse than a message in quiet hours (ADR 0004 choice,
  for Andre to confirm).
- Past due: breached — nudge Andre and tell the client the new real time
  at the next moment outside quiet hours.
- Nudges count only when delivered (fix wave 2): ``andre_nudged`` is set by
  the service only on a confirmed push. An undelivered nudge is retried on
  the next tick while ``andre_nudge_failures < max_nudge_attempts``; that
  includes the breach nudge (``breach_nudge_pending``) after the commitment
  is already breached. Nudge retries never gate the client warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from guardrails import check_outbound
from onboarding_schema import Commitment, CommitmentStatus

from ._status import PHASE1_STATUS

NUMBER = 9
NAME = "Promise Keeper"
PHASE = 1
STATUS = PHASE1_STATUS

TODAY_TEXT = "Andre will get back to you today."
TOMORROW_TEXT = "Andre will get back to you first thing tomorrow."


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed; send an explicit UTC offset")
    return dt


@dataclass(frozen=True)
class EscalationCommitment:
    text: str
    due_at: datetime  # UTC
    form: str  # "today" | "first_thing_tomorrow"


def escalation_commitment(now: datetime, cutoff: time, tz_name: str, today_due: time, first_thing: time) -> EscalationCommitment:
    tz = ZoneInfo(tz_name)
    local = _aware(now).astimezone(tz)
    cutoff_dt = datetime.combine(local.date(), cutoff, tzinfo=tz)
    if local < cutoff_dt:
        due = datetime.combine(local.date(), today_due, tzinfo=tz)
        return EscalationCommitment(check_outbound(TODAY_TEXT), due.astimezone(timezone.utc), "today")
    due = datetime.combine(local.date() + timedelta(days=1), first_thing, tzinfo=tz)
    return EscalationCommitment(check_outbound(TOMORROW_TEXT), due.astimezone(timezone.utc), "first_thing_tomorrow")


def in_quiet_hours(at: datetime, client_tz: str, quiet_start: time, quiet_end: time) -> bool:
    t = _aware(at).astimezone(ZoneInfo(client_tz)).time()
    if quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= t < quiet_end
    return t >= quiet_start or t < quiet_end


def _fmt(dt: datetime, tz_name: str) -> str:
    local = dt.astimezone(ZoneInfo(tz_name))
    return local.strftime("%A %b %d at %I:%M %p %Z").replace(" 0", " ")


def plan_warn_time(created: datetime, due: datetime, lead_hours: int, client_tz: str, qs: time, qe: time) -> tuple[datetime, bool]:
    """Returns (warn_at, quiet_override)."""
    target = due - timedelta(hours=lead_hours)
    step = timedelta(minutes=1)
    floor = max(created, target - timedelta(hours=48))
    t = target.replace(second=0, microsecond=0)
    while t >= floor:
        if not in_quiet_hours(t, client_tz, qs, qe):
            return t, False
        t -= step
    t = target
    while t < due:
        if not in_quiet_hours(t, client_tz, qs, qe):
            return t, False
        t += step
    return target, True


@dataclass(frozen=True)
class PromiseAction:
    action: str  # nudge_andre | warn_client | hold_quiet_hours | breached | none
    send_at: Optional[datetime]
    reason: str
    message: Optional[str] = None
    new_due_at: Optional[datetime] = None
    quiet_hours_override: bool = False


def _next_allowed(now: datetime, until: datetime, client_tz: str, qs: time, qe: time) -> Optional[datetime]:
    t = now
    while t < until:
        if not in_quiet_hours(t, client_tz, qs, qe):
            return t
        t += timedelta(minutes=1)
    return None


def decide(
    c: Commitment, now: datetime, client_tz: str, quiet_start: time, quiet_end: time,
    cutoff: time, cutoff_tz: str, today_due: time, first_thing: time,
    nudge_lead_hours: int = 3, warn_lead_hours: int = 1, max_nudge_attempts: int = 3,
) -> list[PromiseAction]:
    now = _aware(now)
    retry_left = c.andre_nudge_failures < max_nudge_attempts
    attempt_note = f" (attempt {c.andre_nudge_failures + 1} of {max_nudge_attempts})" if c.andre_nudge_failures else ""
    if c.status == CommitmentStatus.BREACHED and c.breach_nudge_pending and retry_left:
        return [PromiseAction("nudge_andre", now, "breached; the breach nudge to Andre was not delivered yet" + attempt_note)]
    if c.status in (CommitmentStatus.KEPT, CommitmentStatus.BREACHED):
        return [PromiseAction("none", None, f"commitment is {c.status.value}")]
    fallback = escalation_commitment(now, cutoff, cutoff_tz, today_due, first_thing)
    new_due = c.proposed_new_due_at or fallback.due_at
    actions: list[PromiseAction] = []

    def warn(reason: str, send_at: datetime, override: bool = False) -> PromiseAction:
        msg = check_outbound(
            f"An update on our commitment ({c.text.rstrip('.')}): that time is going to slip, and we'd rather tell "
            f"you now than let it pass. The new time is {_fmt(new_due, client_tz)}."
        )
        return PromiseAction("warn_client", send_at, reason, msg, new_due, override)

    if now >= c.due_at:
        actions.append(PromiseAction("nudge_andre", now, "commitment passed without being kept"))
        nxt = _next_allowed(now, now + timedelta(hours=24), client_tz, quiet_start, quiet_end) or now
        actions.append(PromiseAction("breached", nxt, "past due; tell the client the new real time at the next allowed moment",
                                     check_outbound(f"We missed the time we gave you, and we're sorry. The new time is {_fmt(new_due, client_tz)}."),
                                     new_due))
        return actions

    on_track = c.status == CommitmentStatus.ON_TRACK
    # Roll rule for "today" escalations not engaged by the cutoff.
    if c.kind == "escalation_callback" and c.category == "today" and not c.engaged:
        tz = ZoneInfo(cutoff_tz)
        cutoff_dt = datetime.combine(c.due_at.astimezone(tz).date(), cutoff, tzinfo=tz)
        if now >= cutoff_dt:
            if not c.andre_nudged and retry_left:
                actions.append(PromiseAction("nudge_andre", now, "not engaged by the noon cutoff; rolling to next day" + attempt_note))
            if not c.client_warned:
                rolled = escalation_commitment(now, cutoff, cutoff_tz, today_due, first_thing)
                new_due = c.proposed_new_due_at or rolled.due_at
                if in_quiet_hours(now, client_tz, quiet_start, quiet_end):
                    nxt = _next_allowed(now, c.due_at, client_tz, quiet_start, quiet_end)
                    if nxt is not None:
                        actions.append(PromiseAction("hold_quiet_hours", nxt, "client is in quiet hours; roll notice held", None, new_due))
                        return actions
                    actions.append(warn("rolled at noon cutoff; no allowed time before old due", now, True))
                else:
                    actions.append(warn("not engaged by the noon cutoff; rolled to first thing tomorrow", now))
            return actions or [PromiseAction("none", None, "roll already handled")]

    if not on_track and not c.engaged and not c.andre_nudged and retry_left and now >= c.due_at - timedelta(hours=nudge_lead_hours):
        actions.append(PromiseAction("nudge_andre", now, f"due within {nudge_lead_hours}h and not confirmed on track" + attempt_note))
    if not on_track and not c.client_warned:
        warn_at, override = plan_warn_time(c.created_at, c.due_at, warn_lead_hours, client_tz, quiet_start, quiet_end)
        if now >= warn_at:
            if not in_quiet_hours(now, client_tz, quiet_start, quiet_end):
                actions.append(warn("not confirmed on track; warning before the time passes", now))
            else:
                nxt = _next_allowed(now, c.due_at, client_tz, quiet_start, quiet_end)
                if nxt is not None:
                    actions.append(PromiseAction("hold_quiet_hours", nxt, "client quiet hours; warning held to next allowed moment before due", None, new_due))
                else:
                    actions.append(warn("no moment outside quiet hours remains before due", now, True))
        else:
            actions.append(PromiseAction("none", warn_at, "not yet; client warning scheduled", None, None, override))
    return actions or [PromiseAction("none", None, "on track")]
