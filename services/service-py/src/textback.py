"""
RR-F2 — missed-call text-back (MVP spec v0.1, front-desk team). One job: when a call to a client is missed (busy,
after hours, dropped, voicemail, no answer), decide whether to text the caller now, wait until 08:00 their time, or
not text at all, and word the one informational text. Deterministic; no LLM; nothing is sent from here.

Rules (spec v0.1, the Nov 26 spec-freeze rulings, TCPA practice):
- Never text a landline, a blocked or unreadable caller ID, or a number the consent keeper (RR-F5) blocks.
- One text per number per 5 minutes: a text within the last 300 s means no new one (exactly 300 s may text). The
  record's own ``seconds_since_last_text_to_number`` is one source; ``TextBackGate`` also remembers every text it
  approved, so repeated calls through one gate can never double-text even when the record says "never texted".
  A record without that field at all fails closed.
- Quiet hours are the RECIPIENT's: texting is allowed from 08:00 up to, not including, 21:00 in the caller's own
  zone. The zone must always be readable. With a real clock (``now``, an aware time) the clock decides, in that
  zone, to the second, and a send-now needs its whole 30 s deadline to land before 21:00. Without a clock the
  record's ``recipient_local_time`` (HH:MM) is used. An unknown zone, a zone that is not a place ("UTC", "Factory",
  "localtime", "Etc/..."), or a clock with no UTC offset or that cannot be converted is never guessed.
  Outside the window the decision is ``defer_until_0800`` with the next 08:00 when the clock is known; the caller
  must ask again then (consent and the 5-minute rule are read at that moment, not now).
- The text is informational only (a way to continue the call), never marketing, and always says how to opt out. It
  is one of four fixed wordings (English or Spanish, with or without a callback number) and never carries record
  text: no business name (AEGIS round 4), only a callback number that reads as a North American number and is not a
  premium-rate (900 area, 976 exchange), N11 or international-rate (Caribbean) number. The number's format is checked, not who owns it: the wiring passes the
  client's own number.
- A send-now decision must reach the caller within 30 s (the bar); the deadline travels with the decision.
- Anything this module cannot read (line type, consent state, zone, local time, last-text time) fails closed: no
  text, with the reason. Records are read as plain data: a dict subclass through ``dict``'s own methods, any text
  or number value that is a subclass makes the record unreadable (``unreadable_record``, AEGIS round 8), a last-text
  time only as a plain ``int``, a clock only as a real ``datetime`` whose offset is read once (AEGIS round 7). A
  consent keeper that raises an ``Exception`` reads as ``consent_unknown``; a ``KeyboardInterrupt`` or
  ``SystemExit`` is not caught, on purpose, and nothing is sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import channels
from consent_keeper import normalise_number

REPEAT_WINDOW_SECONDS = 300
SEND_DEADLINE_SECONDS = 30
TEXTABLE_LINES = ("mobile", "voip")
_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
# tzdata entries that are not a place: "zone not set" placeholders and the host's own zone
_PLACEHOLDER_ZONES = re.compile(r"(?:Factory|localtime|posixrules|posix/.*|right/.*|SystemV/.*|Etc/.*|"
                                r"[A-Z]{3,5}(?:\d[A-Z]{3,5})?)", re.IGNORECASE)


@dataclass(frozen=True)
class TextBackDecision:
    missed_call_id: str
    action: str                        # "send_now" | "defer_until_0800" | "do_not_send"
    reason: str                        # why (stable code)
    deadline_seconds: Optional[int]    # SEND_DEADLINE_SECONDS on send_now, else None
    message_class: str = "informational"
    text: Optional[str] = None         # the one text, whenever there is one to send (now or at 08:00)
    not_before: Optional[datetime] = None   # the next 08:00 in the recipient's zone, when deferring with a clock


_NOT_A_CALLBACK_AREA = frozenset({"900", "976"})
# +1 numbers billed at international rates (Caribbean and Atlantic NANP members; US territories stay offerable)
_INTERNATIONAL_RATE_AREAS = frozenset({"242", "246", "264", "268", "284", "345", "441", "473", "649", "658", "664",
                                       "721", "758", "767", "784", "809", "829", "849", "868", "869", "876"})


def _offerable(number: str) -> bool:
    """+1 NPA NXX XXXX: no premium-rate area (900) or exchange (976, the pay-per-call exchange; AEGIS round 6: the check
    read 976 only as an area code), no N11 area or exchange, no international-rate area."""
    area, exchange = number[2:5], number[5:8]
    return not (area in _NOT_A_CALLBACK_AREA or exchange == "976" or area[1:] == "11" or exchange[1:] == "11"
                or area in _INTERNATIONAL_RATE_AREAS)


def message(business_name: Optional[str] = None, callback_number: Optional[str] = None,
            spanish: bool = False) -> str:
    """The only text F2 ever sends: informational, a way to continue, and how to opt out. No offer, no link, no
    price, and no free text from any record: ``business_name`` is accepted and deliberately never used (AEGIS round
    4: a word check on names was beaten by "PRØMØ", "MegaSale", "Claim Your Gift"; no list can close that). The
    caller knows whom they called; a branded name needs an approved-name registry signed off by a person (not
    built). A callback number that is not a readable +1 number is left out."""
    number = normalise_number(callback_number) if callback_number is not None else None
    if number is not None and not _offerable(number):
        number = None                  # premium-rate, N11 or international-rate: never offered as a callback
    if spanish:
        who = "Perdimos su llamada."
        back = f"Responda a este mensaje o llamenos al {number}." if number else "Responda a este mensaje."
        return f"{who} {back} Responda ALTO para no recibir mas mensajes."
    who = "Sorry we missed your call."
    back = f"Reply here or call us back at {number}." if number else "Reply here and we will call you back."
    return f"{who} {back} Reply STOP to opt out."


def _plain(d: object) -> object:
    """A dict subclass is read through ``dict``'s own methods, so an overridden ``get`` cannot change what the record
    holds (AEGIS round 7); anything else is returned unchanged."""
    if isinstance(d, dict) and type(d) is not dict:
        return dict(dict.items(d))
    return d


def _utc(now: object) -> Optional[datetime]:
    """The clock as a plain UTC time, its offset read exactly once (AEGIS round 7: a tzinfo that answers an offset to
    one question and none to the next is never read again), or None. Only a real ``datetime`` is a clock."""
    if type(now) is not datetime:
        return None
    off = now.utcoffset()
    if type(off) is not timedelta:
        return None
    return (now.replace(tzinfo=None) - off).replace(tzinfo=timezone.utc)


def _local(call: dict, now: Optional[datetime]) -> tuple[Optional[int], Optional[datetime]]:
    """(seconds after local midnight, the local datetime when a clock is known) or (None, None)."""
    name = call.get("recipient_time_zone")
    if not isinstance(name, str) or "/" not in name or _PLACEHOLDER_ZONES.fullmatch(name):
        return None, None             # only a real place ("America/Phoenix"); never UTC, Factory or the host's zone
    z = channels.zone(name)
    if z is None:
        return None, None
    if now is not None:
        try:
            utc = _utc(now)
            if utc is None:
                return None, None
            local = utc.astimezone(z)
        except Exception:             # an unreadable clock (overflow, a tzinfo that raises) fails closed with a reason
            return None, None
        return local.hour * 3600 + local.minute * 60 + local.second, local
    raw = call.get("recipient_local_time")
    m = _HHMM.fullmatch(raw) if isinstance(raw, str) else None
    return (int(m.group(1)) * 3600 + int(m.group(2)) * 60, None) if m else (None, None)


def _next_0800(local: datetime) -> datetime:
    target = local.replace(hour=channels.QUIET_START_HOUR, minute=0, second=0, microsecond=0)
    return target if local < target else target + timedelta(days=1)


def decide(call: dict, now: Optional[datetime] = None,
           opted_out: Optional[Callable[[str], Optional[bool]]] = None,
           business_name: Optional[str] = None, callback_number: Optional[str] = None,
           spanish: bool = False, seconds_since_gate: Optional[int] = None) -> TextBackDecision:
    """Decide one missed call.

    ``opted_out(number)`` asks the consent keeper (True blocks, False allows, anything else blocks). Without it the
    record's own ``number_opted_out`` flag (the keeper's answer, carried on the record) is used. ``seconds_since_gate``
    is the gate's own memory of the last text to this number (``TextBackGate`` passes it).
    """
    call = _plain(call)
    mcid = call.get("missed_call_id") if isinstance(call, dict) else None
    if not isinstance(mcid, str) or not mcid:
        raise ValueError("missed_call_id is required")

    def no(reason: str) -> TextBackDecision:
        return TextBackDecision(mcid, "do_not_send", reason, None)

    # every value is read as plain data: a subclass of str or a number can answer == / in / < however it likes
    # (AEGIS round 8: a str subclass holding "landline" compared equal to "mobile"), so the record is unreadable
    if any(isinstance(v, (str, int, float)) and type(v) not in (str, int, float, bool) for v in call.values()):
        return no("unreadable_record")

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
    try:
        blocked = opted_out(number) if opted_out is not None else call.get("number_opted_out")
    except Exception:                 # the consent keeper could not answer: unknown consent never sends
        blocked = None
    if blocked is not False:
        return no("opted_out" if blocked is True else "consent_unknown")
    if "seconds_since_last_text_to_number" not in call:
        return no("unreadable_last_text_time")
    for since in (call.get("seconds_since_last_text_to_number"), seconds_since_gate):
        if since is None:
            continue
        if type(since) is not int or since < 0:      # a plain whole number only (AEGIS round 7: no subclasses)
            return no("unreadable_last_text_time")
        if since < REPEAT_WINDOW_SECONDS:
            return no("texted_within_5_minutes")
    seconds, local = _local(call, now)
    if seconds is None:
        return no("unknown_local_time")
    text = message(business_name, callback_number, spanish)
    start = channels.QUIET_START_HOUR * 3600
    # with a clock, the whole 30 s deadline must land before 21:00; the minute-only record time is decided as given
    # ("20:59" sends: the pool's spec ruling; AEGIS round 4 notes the real time may be up to 59 s later; open for Andre)
    end = channels.QUIET_END_HOUR * 3600 - (SEND_DEADLINE_SECONDS if local is not None else 0)
    if start <= seconds < end:
        return TextBackDecision(mcid, "send_now", "ok", SEND_DEADLINE_SECONDS, text=text)
    try:
        not_before = _next_0800(local) if local is not None else None
    except OverflowError:             # the next 08:00 is past the last date a clock can hold
        return no("unknown_local_time")
    return TextBackDecision(mcid, "defer_until_0800", "quiet_hours", None, text=text, not_before=not_before)


class TextBackGate:
    """Remembers every text it approved (number -> time) so the 5-minute rule holds across calls, even when the
    record says the number was never texted. In-memory, single process; the store that outlives a restart is the
    wiring's job (not built)."""

    def __init__(self) -> None:
        self._last: dict[str, datetime] = {}

    def decide(self, call: dict, now: datetime, **kw) -> TextBackDecision:
        call = _plain(call)
        try:
            utc = _utc(now)                      # real elapsed time: two times in one zone subtract on the wall
            if utc is None:                      # clock (AEGIS round 2: across a DST jump), so compare in UTC
                raise ValueError
        except Exception:
            mcid = call.get("missed_call_id") if isinstance(call, dict) else None
            if not isinstance(mcid, str) or not mcid:
                raise ValueError("missed_call_id is required") from None
            return TextBackDecision(mcid, "do_not_send", "unknown_local_time", None)   # nothing recorded
        number = normalise_number(call.get("caller_id")) if isinstance(call, dict) else None
        last = self._last.get(number) if number else None
        since = int((utc - last).total_seconds()) if last else None
        kw.pop("seconds_since_gate", None)    # the gate's own memory is the only one it passes on
        d = decide(call, now=utc, seconds_since_gate=since, **kw)   # decide reads the clock the gate read
        if d.action == "send_now" and number:
            self._last[number] = utc
        return d
