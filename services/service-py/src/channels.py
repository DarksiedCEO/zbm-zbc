"""
Intelligence I4 — the outbound channel rules (ADR 0014 decisions 5-7). Deterministic, one job: may this message go
out on this channel to this contact NOW, and if not, why (a reason code)?

- SMS: only to a contact with a recorded, unrevoked EXPRESS SMS consent given for the number we send to (consent
  registry; a changed number has no consent), and only between 08:00
  and 21:00 in the recipient's own time zone; an unknown or invalid time zone refuses (we never guess). Outside the
  window a message waits (``QUIET_HOURS``); it is never sent early.
- Email: a reply on a ticket the contact opened is allowed unless they revoked email; anything proactive (check-in,
  survey, offer, renewal) needs a recorded, unrevoked email consent.
- Chat: messages into the contact's own portal / site thread; no consent record needed.
- Phone: never an outbound channel here (no voice provider).
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import re

from triage import _one_edit, normalise

QUIET_START_HOUR = 8       # 08:00 local: first minute SMS may go
QUIET_END_HOUR = 21        # 21:00 local: first minute SMS may NOT go
# AEGIS round 1 (V1-H3): any of these ANYWHERE in an inbound SMS (normalised like triage: "S T O P", a zero-width
# space, full-width letters all count) revokes SMS consent; a short ambiguous message on its own revokes too.
# Over-revoking only ever stops texts, never sends one.
OPT_OUT_TERMS = ("stop", "stopall", "unsubscribe", "unsub", "cancel", "cancelled", "canceled", "end", "quit", "revoke",
                 "optout", "opt out", "halt", "desist", "stahp",
                 "remove me", "take me off", "leave me alone", "no more", "dont text", "do not text", "dont message",
                 "do not message", "dont contact", "do not contact", "dont txt", "do not txt", "dont send",
                 "do not send", "never text", "never message", "never contact", "stop texting", "quit texting",
                 "remove my number", "my number off", "lose my number", "take my number", "delete my number",
                 "didnt sign up", "did not sign up", "never signed up", "who is this", "wrong number", "wrong person",
                 "not interested",
                 "alto", "parar", "pare", "cancelar", "baja", "darme de baja", "no me escriban", "no mas mensajes",
                 "numero equivocado", "arrete", "arreter", "desabonner", "sair", "cancele", "descadastrar",
                 "parem", "nao quero")
# V2-H3: one typo away from these (on 4+ letter tokens, after repeated letters are collapsed and leet undone)
OPT_OUT_FUZZY = ("stop", "unsubscribe", "stopall")
OPT_OUT_SYMBOLS = ("\U0001F6D1", "\u26D4", "\U0001F6AB", "\u270B")      # stop sign, no entry, prohibited, raised hand
_LEET = str.maketrans({"0": "o", "5": "s", "1": "i", "3": "e", "4": "a", "@": "a", "$": "s", "7": "t"})
OPT_OUT_SHORT = ("no", "nope", "nah", "no thanks", "no thank you", "bye", "go away", "enough", "basta", "no gracias")
OPT_OUT_CONFIRMATION = ("You are unsubscribed from {brand_name} texts and will receive no further messages. "
                        "Contact {brand_name} support by email to change this.")


def zone(tz: Optional[str]) -> Optional[ZoneInfo]:
    if not tz or not isinstance(tz, str) or len(tz) > 64:
        return None
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def within_sms_hours(now: datetime, tz: Optional[str]) -> Optional[bool]:
    """True / False, or None when the time zone is unknown (the caller refuses)."""
    z = zone(tz)
    if z is None:
        return None
    local = now.astimezone(z)
    return QUIET_START_HOUR <= local.hour < QUIET_END_HOUR


def _collapse(word: str) -> str:
    """Repeated letters collapsed ("stoooop", "STOPPPP" -> "stop")."""
    return re.sub(r"(.)\1+", r"\1", word)


def is_opt_out(text: str) -> bool:
    """An opt-out anywhere in the message (V1-H3, V2-H3), on ANY inbound channel: a listed word or phrase, a word one
    typo away from stop / unsubscribe once repeated letters are collapsed and digits read as letters, a stop-sign
    emoji, or a short ambiguous message on its own. Over-matching only ever stops texts."""
    if any(sym in text for sym in OPT_OUT_SYMBOLS):
        return True
    variants = {normalise(text), normalise(text.translate(_LEET))}
    variants |= {" " + " ".join(_collapse(w) for w in v.split()) + " " for v in list(variants)}
    for norm in variants:
        if any(f" {normalise(t).strip()} " in norm for t in OPT_OUT_TERMS):
            return True
        if norm.strip() in OPT_OUT_SHORT:
            return True
        for tok in norm.split():
            if len(tok) >= 4 and any(_one_edit(tok, stem) for stem in OPT_OUT_FUZZY):
                return True
    return False


is_stop = is_opt_out


def consent_matches(consent: Optional[dict], address: Optional[str]) -> bool:
    """V1-C1: a consent counts only while it is active AND was given for the address we would send to now."""
    return bool(consent and consent.get("status") == "active" and address and consent.get("address") == address)


def check(channel: str, contact: dict, consent_for: Callable[[str], Optional[dict]], proactive: bool, now: datetime,
          opt_out_confirmation: bool = False, sms_paused: bool = False) -> Optional[str]:
    """None when allowed now; else a reason code. ``consent_for(channel) -> the consent record or None``.
    ``opt_out_confirmation``: the one confirmation of an opt-out (no consent needed, sent at once)."""
    if channel == "sms":
        if not contact.get("phone"):
            return "NO_ADDRESS"
        if opt_out_confirmation:
            return None
        if not consent_matches(consent_for("sms"), contact["phone"]):
            return "SMS_CONSENT_REQUIRED"
        if proactive and sms_paused:
            return "SMS_PAUSED"            # V2-H3: an unclear inbound text pauses proactive SMS until Andre clears it
        ok = within_sms_hours(now, contact.get("timezone"))
        if ok is None:
            return "TIMEZONE_UNKNOWN"
        return None if ok else "QUIET_HOURS"
    if channel == "email":
        if not contact.get("email"):
            return "NO_ADDRESS"
        ec = consent_for("email")
        if ec and ec.get("status") == "revoked" and ec.get("address") in (None, contact["email"]):
            return "EMAIL_CONSENT_REVOKED"
        if proactive and not consent_matches(ec, contact["email"]):
            return "EMAIL_CONSENT_REQUIRED"
        return None
    if channel == "chat":
        return None
    return "CHANNEL_NOT_OUTBOUND"
