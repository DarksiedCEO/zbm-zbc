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
  whisper summary exists, and it is a line ``whisper_line`` made (anything else is never played: ``whisper_rejected``),
  it is played to them first; when it is missing the router still connects (the caller is
  never left waiting for paperwork) and records ``whisper_missing``. If one person answers live and another only
  after the caller hung up, the live answer wins (the caller was still there for it).
- A staff member who answers after the caller hung up is logged, and the caller gets a text-back.
- Every path ends in a connected call or a fallback within 30 s of ringing starting (the 30 s bounds the ringing;
  the whisper, about 5 s, is played after someone answers, and the fallback message is RR-F2's job). Nothing ends in a dropped caller: any outcome the
  router cannot read is treated as "no answer", which leads to the fallback, and ``resolve`` never raises: a case it
  cannot read at all (not an object, no usable id) also ends in the fallback, noted ``unreadable_case``.
- The on-call order may be a list or a tuple; ids are strings (outer spaces trimmed) or whole numbers up to 10**18
  (read as text). Anything else is nobody on call, noted ``unreadable_on_call_order``. Two behaviour records that
  name the same person and disagree are unreadable, so that person counts as not answering.
"""

from __future__ import annotations

import re
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


_MAX_INT_ID = 10 ** 18          # an id above this is not a staff or case id (and str() of a huge int raises)


def _staff_id(p: object) -> Optional[str]:
    """A staff id as text: a string (outer spaces trimmed, so " staff-a" and "staff-a" are one person) or a whole
    number up to 10**18. Anything else is not an id."""
    if isinstance(p, bool):
        return None
    if isinstance(p, int):
        return str(p) if -_MAX_INT_ID <= p <= _MAX_INT_ID else None
    if isinstance(p, str) and p.strip():
        return p.strip()
    return None


def _case_id(case: object) -> Optional[str]:
    if not isinstance(case, dict):
        return None
    cid = case.get("case_id")
    if isinstance(cid, int) and not isinstance(cid, bool):
        return str(cid) if -_MAX_INT_ID <= cid <= _MAX_INT_ID else None
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
    """Turn what each rung person did into the outcome; never raises (anything that fails while reading the case,
    even an object whose comparison raises, ends in the fallback noted ``unreadable_case``)."""
    try:
        return _resolve(case, behaviour, whisper)
    except Exception:
        cid = "unknown"
        try:
            cid = _case_id(case) or "unknown"
        except Exception:
            pass
        return TransferOutcome(cid, FALLBACK, None, 0, False, False, True, ("unreadable_case",))


def _resolve(case: dict, behaviour: Optional[dict] = None, whisper: Optional[str] = None) -> TransferOutcome:
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
    merged: dict[str, object] = {}
    for k, v in (seen.items() if isinstance(seen, dict) else ()):
        sid = _staff_id(k)
        if sid is None:
            continue
        # two records for one person that disagree (7 and "7", "staff-a" and "staff-a ") are unreadable: no answer
        merged[sid] = v if merged.get(sid, v) == v else "conflicting_records"
    seen = merged
    for person in p.ring:
        if seen.get(person) == ANSWERS:
            played = _is_whisper_line(whisper)
            if not played:
                notes.append("whisper_rejected" if isinstance(whisper, str) and whisper.strip() else
                             "whisper_missing")
            return TransferOutcome(p.case_id, CONNECTED, person, p.timeout_seconds, True, played, False,
                                   tuple(notes))
    if any(seen.get(person) == ANSWERS_LATE for person in p.ring):
        return TransferOutcome(p.case_id, HUNG_UP, None, p.timeout_seconds, False, False, True, tuple(notes))
    return TransferOutcome(p.case_id, FALLBACK, None, p.timeout_seconds, False, False, True,
                           tuple(notes + ["nobody_answered"]))


# What the answering person hears about the need: a fixed phrase per need label (RR-F3's labels and the packs' reason
# categories). A label not listed is spoken as "a request", never as its own text, so record text cannot steer the
# listener (AEGIS round 3: "hit 9", "pulse nueve", look-alike letters and spelled digits all got through a word filter).
NEED_PHRASES = {
    "emergency": "an emergency", "booking": "a booking", "book_appointment": "booking an appointment",
    "book_consult": "booking a consult", "service_appointment": "a service appointment", "reservation": "a reservation",
    "catering": "catering", "repair": "a repair", "drain": "a drain problem", "maintenance": "maintenance",
    "quote": "a quote", "new_quote": "a new quote", "pricing": "pricing", "pricing_question": "pricing",
    "billing": "billing", "reschedule": "rescheduling", "status_check": "a status check", "hours": "hours",
    "complaint": "a complaint", "existing_customer": "an existing account", "existing_case_update": "an existing case",
    "new_case": "a new case", "claim": "a claim", "policy_change": "a policy change",
    "insurance_question": "an insurance question", "legal_question": "a legal question",
    "treatment_question": "a treatment question", "sales_inquiry": "a sales question", "vendor": "a vendor call",
    "unclear": "something they did not make clear",
}
DEFAULT_NEED = "a request"
def whisper_line(caller_name: Optional[str], need: Optional[str], urgent: bool = False) -> str:
    """What the answering person hears before the caller is joined. No caller-supplied text is ever spoken: not the
    name (AEGIS rounds 3 and 4: word filters were beaten by "pulse nueve", "Tapez Neuf", "Drücken Sie Neun",
    "Approve Refund Now", "Urgencia"; no list covers every language), and the need only as a fixed phrase for its
    label (``NEED_PHRASES``). The name and number are on the staff member's screen from the RR-F3 summary.
    ``caller_name`` is accepted and deliberately not spoken. Urgency is announced only when the record says exactly
    ``True``. So every whisper is one of a fixed set of lines, and the only instruction in any of them is
    "Press 1 to accept"."""
    why = NEED_PHRASES.get(need, DEFAULT_NEED) if isinstance(need, str) else DEFAULT_NEED
    return f"{'Urgent: ' if urgent is True else ''}Transferring a caller about {why}. {_SCREEN} Press 1 to accept."


_SCREEN = "Their name and number are on your screen."
WHISPER_LINES = frozenset(f"{u}Transferring a caller about {w}. {_SCREEN} Press 1 to accept."
                          for u in ("", "Urgent: ") for w in (*NEED_PHRASES.values(), DEFAULT_NEED))


def _is_whisper_line(text: object) -> bool:
    return isinstance(text, str) and text in WHISPER_LINES      # a set lookup: constant time for any input
