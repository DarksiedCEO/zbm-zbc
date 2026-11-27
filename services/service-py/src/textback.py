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
  is always worded. A business name is used only when it is Latin-script letters, spaces and & ' . , - ( ) with at
  most 3 digits, and carries no link, domain, offer, price or instruction word ("reply", "join", "yes" ...);
  otherwise it is left out. A callback number that is not a North American number is left out.
- A send-now decision must reach the caller within 30 s (the bar); the deadline travels with the decision.
- Anything this module cannot read (line type, consent state, zone, local time, last-text time) fails closed: no
  text, with the reason.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import channels
from consent_keeper import normalise_number

REPEAT_WINDOW_SECONDS = 300
SEND_DEADLINE_SECONDS = 30
TEXTABLE_LINES = ("mobile", "voip")
_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
# A business name must be letters (Latin script only, so a look-alike Cyrillic or Greek letter cannot hide a word),
# spaces and & ' . , - ( ), with at most 3 digits, no domain-like "x.yz", and none of the offer or instruction words.
_PROMO = re.compile(r"https?|www|\w\.[a-z]{2,}\b|\b(?:off|free|deals?|sales?|offers?|discounts?|promos?|coupons?|win|"
                    r"wins|winner|prize|gratis|descuentos?|ofertas?|promoci[oó]n(?:es)?|cheap|save|savings|usd|"
                    r"dollars?|bucks|reply|text|join|yes|stop|click|visit|subscribe)\b", re.IGNORECASE)
_SAFE_NAME = re.compile(r"[A-Za-z\u00c0-\u024f&'.,\- ()0-9]{1,40}")
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


def _clean_name(name: object) -> Optional[str]:
    if not isinstance(name, str) or any(unicodedata.category(c)[0] == "C" for c in name):
        return None              # control or format characters (a line break, a zero-width space): left out
    name = " ".join(name.split())
    if not name or not _SAFE_NAME.fullmatch(name) or _PROMO.search(name) or sum(c.isdigit() for c in name) > 3:
        return None
    if any(c.isalpha() and not unicodedata.name(c, "").startswith("LATIN") for c in name):
        return None
    return name


def message(business_name: Optional[str] = None, callback_number: Optional[str] = None,
            spanish: bool = False) -> str:
    """The only text F2 ever sends: informational, a way to continue, and how to opt out. No offer, no link, no
    price. A name or number that fails its check is left out rather than sent."""
    name = _clean_name(business_name)
    number = normalise_number(callback_number) if callback_number is not None else None
    if spanish:
        who = f"{name}: perdimos su llamada." if name else "Perdimos su llamada."
        back = f"Responda a este mensaje o llamenos al {number}." if number else "Responda a este mensaje."
        return f"{who} {back} Responda ALTO para no recibir mas mensajes."
    who = f"{name}: sorry we missed your call." if name else "Sorry we missed your call."
    back = f"Reply here or call us back at {number}." if number else "Reply here and we will call you back."
    return f"{who} {back} Reply STOP to opt out."


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
            if not isinstance(now, datetime) or now.utcoffset() is None:
                return None, None
            local = now.astimezone(z)
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
    mcid = call.get("missed_call_id") if isinstance(call, dict) else None
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
    if "seconds_since_last_text_to_number" not in call:
        return no("unreadable_last_text_time")
    for since in (call.get("seconds_since_last_text_to_number"), seconds_since_gate):
        if since is None:
            continue
        if not isinstance(since, int) or isinstance(since, bool) or since < 0:
            return no("unreadable_last_text_time")
        if since < REPEAT_WINDOW_SECONDS:
            return no("texted_within_5_minutes")
    seconds, local = _local(call, now)
    if seconds is None:
        return no("unknown_local_time")
    text = message(business_name, callback_number, spanish)
    start = channels.QUIET_START_HOUR * 3600
    # with a clock, the whole 30 s deadline must land before 21:00; the minute-only record time is decided as given
    end = channels.QUIET_END_HOUR * 3600 - (SEND_DEADLINE_SECONDS if local is not None else 0)
    if start <= seconds < end:
        return TextBackDecision(mcid, "send_now", "ok", SEND_DEADLINE_SECONDS, text=text)
    return TextBackDecision(mcid, "defer_until_0800", "quiet_hours", None, text=text,
                            not_before=_next_0800(local) if local is not None else None)


class TextBackGate:
    """Remembers every text it approved (number -> time) so the 5-minute rule holds across calls, even when the
    record says the number was never texted. In-memory, single process; the store that outlives a restart is the
    wiring's job (not built)."""

    def __init__(self) -> None:
        self._last: dict[str, datetime] = {}

    def decide(self, call: dict, now: datetime, **kw) -> TextBackDecision:
        try:
            if not isinstance(now, datetime) or now.utcoffset() is None:
                raise ValueError
            utc = now.astimezone(timezone.utc)   # real elapsed time: two times in one zone subtract on the wall
        except Exception:                        # clock (AEGIS round 2: across a DST jump), so compare in UTC
            raise ValueError("the gate needs an aware, readable clock") from None
        number = normalise_number(call.get("caller_id")) if isinstance(call, dict) else None
        last = self._last.get(number) if number else None
        since = int((utc - last).total_seconds()) if last else None
        d = decide(call, now=now, seconds_since_gate=since, **kw)
        if d.action == "send_now" and number:
            self._last[number] = utc
        return d
