"""
RR-F2 — missed-call text-back (MVP spec v0.1, front-desk team). One job: when a call to a client is missed (busy,
after hours, dropped, voicemail, no answer), decide whether to text the caller now, wait until 08:00 their time, or
not text at all, and word the one informational text. Deterministic; no LLM; nothing is sent from here.

Rules (spec v0.1, the Nov 26 spec-freeze rulings, TCPA practice):
- Never text a landline, a blocked or unreadable caller ID, or a number the consent keeper (RR-F5) blocks.
- One text per number per 5 minutes: a text within the last 300 s means no new one (exactly 300 s may text).
- Quiet hours are the RECIPIENT's: texting is allowed from 08:00 up to, not including, 21:00 in the caller's own time
  zone. Outside that the text waits until the next 08:00 there. An unknown time zone is never guessed: no text, and
  the reason says why. A deferred text is decided again at 08:00: consent and the 5-minute rule are read then.
- The text is informational only (a way to continue the call), never marketing, and always says how to opt out.
- A send-now decision must reach the caller within 30 s (the bar); the deadline travels with the decision.
- Anything this module cannot read (line type, consent state, local time) fails closed: no text, with the reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

import channels
from consent_keeper import normalise_number

REPEAT_WINDOW_SECONDS = 300
SEND_DEADLINE_SECONDS = 30
TEXTABLE_LINES = ("mobile", "voip")
_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


@dataclass(frozen=True)
class TextBackDecision:
    missed_call_id: str
    action: str                        # "send_now" | "defer_until_0800" | "do_not_send"
    reason: str                        # why (stable code)
    deadline_seconds: Optional[int]    # SEND_DEADLINE_SECONDS on send_now, else None
    message_class: str = "informational"
    text: Optional[str] = None         # the one text, when there is one to send (now or at 08:00)


def message(business_name: str, callback_number: str, spanish: bool = False) -> str:
    """The only text F2 ever sends: informational, a way to continue, and how to opt out. No offer, no link."""
    if spanish:
        return (f"{business_name}: perdimos su llamada. Responda a este mensaje o llamenos al {callback_number}. "
                "Responda ALTO para no recibir mas mensajes.")
    return (f"{business_name}: sorry we missed your call. Reply here or call us back at {callback_number}. "
            "Reply STOP to opt out.")


def _local_minutes(call: dict, now: Optional[datetime]) -> Optional[int]:
    raw = call.get("recipient_local_time")
    if raw is not None:
        m = _HHMM.fullmatch(raw) if isinstance(raw, str) else None
        return int(m.group(1)) * 60 + int(m.group(2)) if m else None
    z = channels.zone(call.get("recipient_time_zone"))
    if z is None or now is None or now.tzinfo is None:
        return None
    local = now.astimezone(z)
    return local.hour * 60 + local.minute


def decide(call: dict, now: Optional[datetime] = None,
           opted_out: Optional[Callable[[str], Optional[bool]]] = None,
           business_name: Optional[str] = None, callback_number: Optional[str] = None,
           spanish: bool = False) -> TextBackDecision:
    """Decide one missed call.

    ``opted_out(number)`` asks the consent keeper (True blocks, False allows, None = unknown, which blocks). Without
    it the record's own ``number_opted_out`` flag (the keeper's answer, carried on the record) is used; when neither
    gives a boolean, no text goes out.
    """
    mcid = call.get("missed_call_id")
    if not isinstance(mcid, str) or not mcid:
        raise ValueError("missed_call_id is required")

    def no(reason: str) -> TextBackDecision:
        return TextBackDecision(mcid, "do_not_send", reason, None)

    number = normalise_number(call.get("caller_id"))
    if call.get("caller_id") is None:
        return no("blocked_caller_id")
    if number is None:
        return no("unreadable_caller_id")
    line = call.get("line_type")
    if line == "landline":
        return no("landline")
    if line not in TEXTABLE_LINES:
        return no("unknown_line_type")
    blocked = opted_out(number) if opted_out is not None else call.get("number_opted_out")
    if blocked is not False:
        return no("opted_out" if blocked is True else "consent_unknown")
    since = call.get("seconds_since_last_text_to_number")
    if since is not None:
        if not isinstance(since, int) or isinstance(since, bool) or since < 0:
            return no("unreadable_last_text_time")
        if since < REPEAT_WINDOW_SECONDS:
            return no("texted_within_5_minutes")
    minutes = _local_minutes(call, now)
    if minutes is None:
        return no("unknown_local_time")
    text = message(business_name, callback_number, spanish) if business_name and callback_number else None
    if channels.QUIET_START_HOUR * 60 <= minutes < channels.QUIET_END_HOUR * 60:
        return TextBackDecision(mcid, "send_now", "ok", SEND_DEADLINE_SECONDS, text=text)
    return TextBackDecision(mcid, "defer_until_0800", "quiet_hours", None, text=text)
