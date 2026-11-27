"""
RR-F4 — warm-transfer router (MVP spec v0.1, front-desk team). One job: when the client's pack says a live caller
needs a human, ring the right people, hand the one who answers a whisper summary, and connect; and when nobody can
take the call, end in a fallback (take a message and ask RR-F2 for a text-back). Deterministic; nothing is dialled
from here: ``plan`` says who to ring and for how long, ``resolve`` turns what happened into the outcome.

Rules (spec v0.1, the Nov 26 spec-freeze rulings):
- Never transfer outside the client's business hours, and never ring anyone who is not on call: both go straight
  to the fallback.
- Simultaneous ringing: every on-call person rings at once (Nov 26 ruling), so the whole transfer fits inside
  30 s for a list of any length. A ring timeout above 30 s, or one that cannot be read, is cut to 30 s.
- The first person (in the on-call order) who answers while the caller is still on the line is connected. When a
  whisper summary exists it is played to them first; when it is missing the router still connects (the caller is
  never left waiting for paperwork) and records ``whisper_missing``. If one person answers live and another only
  after the caller hung up, the live answer wins (the caller was still there for it).
- A staff member who answers after the caller hung up is logged, and the caller gets a text-back.
- Every path ends in a connected call or a fallback within 30 s. Nothing ends in a dropped caller: any outcome the
  router cannot read is treated as "no answer", which leads to the fallback, and ``resolve`` never raises: a case it
  cannot read at all (not an object, no usable id) also ends in the fallback, noted ``unreadable_case``.
- The on-call order may be a list or a tuple; ids are strings or whole numbers (read as text). Anything else is
  nobody on call, noted ``unreadable_on_call_order``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

MAX_SECONDS = 30
CONNECTED = "connected"
FALLBACK = "fallback_message_and_text_back"
HUNG_UP = "caller_hung_up_log_and_text_back"
ANSWERS = "answers"
ANSWERS_LATE = "answers_after_caller_hung_up"


@dataclass(frozen=True)
class RingPlan:
    case_id: str
    ring: tuple                  # on-call staff to ring at once (empty: go straight to the fallback)
    timeout_seconds: int         # 1..30
    reason: str                  # why this plan ("ring_on_call", "outside_hours", "nobody_on_call")


@dataclass(frozen=True)
class TransferOutcome:
    case_id: str
    path: str                    # CONNECTED | FALLBACK | HUNG_UP
    connect_to: Optional[str]
    seconds_to_outcome: int      # never above MAX_SECONDS
    whisper_required: bool
    whisper_played: bool
    text_back_requested: bool    # FALLBACK and HUNG_UP ask RR-F2 (which still applies consent and quiet hours)
    notes: tuple = field(default_factory=tuple)


def _timeout(raw: object) -> tuple[int, Optional[str]]:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        return MAX_SECONDS, "ring_timeout_unreadable_used_30"
    if raw > MAX_SECONDS:
        return MAX_SECONDS, "ring_timeout_cut_to_30"
    return raw, None


def _staff_id(p: object) -> Optional[str]:
    if isinstance(p, bool):
        return None
    if isinstance(p, int):
        return str(p)
    if isinstance(p, str) and p.strip():
        return p.strip()
    return None


def _case_id(case: object) -> Optional[str]:
    if not isinstance(case, dict):
        return None
    cid = case.get("case_id")
    if isinstance(cid, int) and not isinstance(cid, bool):
        return str(cid)
    return cid.strip() if isinstance(cid, str) and cid.strip() else None


def plan(case: dict) -> RingPlan:
    cid = _case_id(case)
    if cid is None:
        return RingPlan("unknown", (), MAX_SECONDS, "unreadable_case")
    timeout, _ = _timeout(case.get("ring_timeout_seconds"))
    if case.get("in_business_hours") is not True:
        return RingPlan(cid, (), timeout, "outside_hours")
    order = case.get("on_call_order")
    if order is not None and not isinstance(order, (list, tuple)):
        return RingPlan(cid, (), timeout, "unreadable_on_call_order")
    staff = tuple(dict.fromkeys(x for x in map(_staff_id, order or ()) if x))
    if not staff:
        return RingPlan(cid, (), timeout, "nobody_on_call")
    return RingPlan(cid, staff, timeout, "ring_on_call")


def resolve(case: dict, behaviour: Optional[dict] = None, whisper: Optional[str] = None) -> TransferOutcome:
    """Turn what each rung person did into the outcome. ``behaviour`` maps staff id -> "answers" |
    "answers_after_caller_hung_up" | anything else (no answer); it defaults to the case's ``staff_behavior``."""
    p = plan(case)
    if p.reason == "unreadable_case":
        return TransferOutcome(p.case_id, FALLBACK, None, 0, False, False, True, ("unreadable_case",))
    notes = []
    t_note = _timeout(case.get("ring_timeout_seconds"))[1]
    if t_note:
        notes.append(t_note)
    if case.get("ring_mode") not in (None, "simultaneous"):
        notes.append("rang_simultaneously_per_policy")
    if not p.ring:
        return TransferOutcome(p.case_id, FALLBACK, None, 0, False, False, True, tuple(notes + [p.reason]))
    seen = behaviour if behaviour is not None else case.get("staff_behavior")
    seen = {_staff_id(k): v for k, v in seen.items() if _staff_id(k)} if isinstance(seen, dict) else {}
    for person in p.ring:
        if seen.get(person) == ANSWERS:
            played = isinstance(whisper, str) and bool(whisper.strip())
            if not played:
                notes.append("whisper_missing")
            return TransferOutcome(p.case_id, CONNECTED, person, p.timeout_seconds, True, played, False,
                                   tuple(notes))
    if any(seen.get(person) == ANSWERS_LATE for person in p.ring):
        return TransferOutcome(p.case_id, HUNG_UP, None, p.timeout_seconds, False, False, True, tuple(notes))
    return TransferOutcome(p.case_id, FALLBACK, None, p.timeout_seconds, False, False, True,
                           tuple(notes + ["nobody_answered"]))


def _spoken(value: object, limit: int) -> Optional[str]:
    """Record text fit to be read aloud: printable only, one line, bounded; anything else is left out."""
    if not isinstance(value, str):
        return None
    value = " ".join("".join(c for c in value if c.isprintable()).split())[:limit].strip()
    return value or None


def whisper_line(caller_name: Optional[str], reason: Optional[str], urgent: bool = False) -> str:
    """What the answering person hears before the caller is joined. Facts only from the call record (RR-F3): the
    urgency is announced only when the record says exactly ``True``; name and reason are bounded to 60 characters
    and read as data (they never change what the person is asked to press)."""
    who = _spoken(caller_name, 60) or "a caller who did not give a name"
    why = _spoken(reason, 60) or "no reason given"
    return f"{'Urgent: ' if urgent is True else ''}Transferring {who}, about {why}. Press 1 to accept."
