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
import unicodedata
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
# words that would steer the listener, keys and digits spelled out: a name holding one is not spoken at all
_STEER = {"press", "presione", "presiona", "oprima", "oprime", "pulse", "pulsa", "marque", "marca", "dial", "hit",
          "push", "tap", "key", "enter", "select", "choose", "urgent", "urgente", "emergency", "emergencia", "accept",
          "acepte", "decline", "reject", "hang", "transfer", "wire", "send", "pay", "zero", "one", "two", "three",
          "four", "five", "six", "seven", "eight", "nine", "ten", "cero", "uno", "dos", "tres", "cuatro", "cinco",
          "seis", "siete", "ocho", "nueve", "diez", "star", "pound", "hash", "asterisk", "button", "boton", "tecla"}
_NAME_WORD = re.compile(r"[A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff][A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff'-]{0,19}")
NO_NAME = "a caller who did not give a name"


def _spoken_name(value: object) -> Optional[str]:
    """A caller name fit to be read aloud: at most 3 words of Latin letters (apostrophes and hyphens inside), read
    after NFKC, none of them a steering word or a spelled digit (folded: "Préss", "PRESS" and "press" alike).
    Anything else is not spoken at all, rather than spoken in part."""
    if not isinstance(value, str):
        return None
    words = unicodedata.normalize("NFKC", value).split()
    if not 1 <= len(words) <= 3 or not all(_NAME_WORD.fullmatch(w) for w in words):
        return None
    for w in words:
        plain = "".join(c for c in unicodedata.normalize("NFKD", w) if not unicodedata.combining(c)).casefold()
        # exact for short words ("hit", "one"), a prefix for longer ones ("presses", "accepted", "Pressley": a real
        # name can be left out, which costs only the name)
        if any(part in _STEER or any(len(x) >= 4 and part.startswith(x) for x in _STEER)
               for part in re.split(r"['-]", plain)):
            return None
    return " ".join(words)


def whisper_line(caller_name: Optional[str], need: Optional[str], urgent: bool = False) -> str:
    """What the answering person hears before the caller is joined. Facts only from the call record (RR-F3): the
    urgency is announced only when the record says exactly ``True``; the name is spoken only when it passes
    ``_spoken_name``; the need is a fixed phrase for its label (``NEED_PHRASES``), never the record's own words. So
    the only instruction ever spoken is "Press 1 to accept"."""
    who = _spoken_name(caller_name) or NO_NAME
    why = NEED_PHRASES.get(need, DEFAULT_NEED) if isinstance(need, str) else DEFAULT_NEED
    return f"{'Urgent: ' if urgent is True else ''}Transferring {who}, about {why}. Press 1 to accept."


_WHISPER_FORM = re.compile(r"(?:Urgent: )?Transferring (.+), about (.+)\. Press 1 to accept\.")


def _is_whisper_line(text: object) -> bool:
    m = _WHISPER_FORM.fullmatch(text) if isinstance(text, str) else None
    if not m:
        return False
    who, why = m.groups()
    return (who == NO_NAME or _spoken_name(who) == who) and (why == DEFAULT_NEED or why in NEED_PHRASES.values())
