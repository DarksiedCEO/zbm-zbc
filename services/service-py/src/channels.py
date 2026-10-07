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
                 # sweep A: email wording ("do not email me" was not an opt-out at all)
                 "dont email", "do not email", "dont e mail", "do not e mail", "never email", "stop emailing",
                 "quit emailing", "stop sending emails", "remove my email", "take my email", "delete my email",
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


CHANNEL_WORDS = ("text", "texts", "texting", "txt", "txts", "sms", "message", "messages", "messaging", "msg", "msgs",
                 "phone", "cell", "mobile", "number")
NEGATORS = ("no", "not", "never", "dont", "do not", "stop", "quit", "without", "nothing", "none", "unwelcome",
            "instead", "prefer", "only")
NEAR = 4                            # tokens between a negator and a channel word (V3-C1 label)


def negated_channel(norm: str) -> bool:
    """A negation within NEAR tokens of text / sms / message / phone / cell ("please no texts", "I do not want text
    messages", "texts are not welcome"): a likely SMS opt-out, labelled for Andre (V3-C1)."""
    toks = norm.split()
    neg = [i for i, t in enumerate(toks) if t in NEGATORS or (t == "do" and i + 1 < len(toks) and toks[i + 1] == "not")]
    chan = [i for i, t in enumerate(toks) if t in CHANNEL_WORDS]
    return any(abs(i - j) <= NEAR for i in neg for j in chan)


def opt_out_level(text: str) -> Optional[str]:
    """``exact``: a listed opt-out word or phrase anywhere (also with repeated letters collapsed or digits read as
    letters, "S T O P", a stop-sign emoji) or a short ambiguous message on its own. ``suspected``: only a one-typo
    match of stop / unsubscribe, or a negation near a channel word. None otherwise. On SMS both revoke; on chat and
    email only ``exact`` revokes, ``suspected`` pauses proactive SMS and asks Andre (V3 Info)."""
    if any(sym in text for sym in OPT_OUT_SYMBOLS):
        return "exact"
    variants = {normalise(text), normalise(text.translate(_LEET))}
    variants |= {" " + " ".join(_collapse(w) for w in v.split()) + " " for v in list(variants)}
    suspected = False
    for norm in variants:
        if any(f" {normalise(t).strip()} " in norm for t in OPT_OUT_TERMS):
            return "exact"
        if norm.strip() in OPT_OUT_SHORT:
            return "exact"
        for tok in norm.split():
            if len(tok) >= 4 and any(_one_edit(tok, stem) for stem in OPT_OUT_FUZZY):
                suspected = True
        suspected = suspected or negated_channel(norm)
    return "suspected" if suspected else None


# Sweep A, AEGIS H1 / M1: what an opt-out received BY EMAIL revokes is decided from the opt-out phrase itself, in the
# person's own words (quoted lines and signatures stripped first), never from other words elsewhere in the message.
PHONE_SCOPE_WORDS = frozenset({"text", "texts", "texting", "texted", "txt", "txts", "txting", "sms", "mms", "number",
                               "numbers", "cell", "mobile", "phone", "call", "calls", "calling"})
# words that may sit between an opt-out word and the channel it names ("unsubscribe me from your texts")
SCOPE_CONNECTORS = frozenset({"me", "us", "from", "your", "our", "the", "all", "any", "these", "those", "this", "my",
                              "of", "to", "with", "sending", "send", "receiving", "getting", "get", "please", "pls",
                              "plz", "more", "future"})
SCOPE_LOOKAHEAD = 4
_QUOTE_HEADER = re.compile(r"^\s*(on\b.{0,300}\bwrote\s*:|-{2,}\s*original message\s*-{2,}|_{5,})\s*$",
                           re.IGNORECASE)
_SIG_DELIM = re.compile(r"^\s*(--|__)\s*$")
_SENT_FROM = re.compile(r"^\s*sent from (my|mail for|outlook|yahoo|gmail)\b", re.IGNORECASE)
_SIGN_OFF = re.compile(r"^\s*(thanks|thank you|many thanks|thx|regards|best|best regards|kind regards|warm regards|"
                       r"cheers|sincerely|yours truly|respectfully)\s*[,.!]*\s*$", re.IGNORECASE)


def strip_quoted(text: str) -> str:
    """The person's own text of an email: quoted lines (``>``) dropped, and everything from a reply header ("On ...
    wrote:", "-----Original Message-----") on."""
    out = []
    for line in (text or "").splitlines():
        if _QUOTE_HEADER.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out)


def strip_signature(text: str) -> str:
    """``strip_quoted`` and then the signature: everything from a ``--`` delimiter or a sign-off line ("Thanks,",
    "Best regards") on, and "Sent from my ..." lines."""
    out = []
    for line in strip_quoted(text).splitlines():
        if _SIG_DELIM.match(line) or (_SIGN_OFF.match(line) and out):
            break
        if _SENT_FROM.match(line):
            continue
        out.append(line)
    return "\n".join(out)


def _typo_tokens(norm: str) -> list[int]:
    toks = norm.split()
    return [i for i, t in enumerate(toks) if len(t) >= 4 and t not in OPT_OUT_FUZZY
            and any(_one_edit(t, stem) for stem in OPT_OUT_FUZZY)]


def typo_opt_out(text: Optional[str]) -> bool:
    """A word one typo from stop / unsubscribe / stopall ("unsubcribe", "unsubscibe"): the ``suspected`` level."""
    if not text:
        return False
    return any(_typo_tokens(v) for v in (normalise(text), normalise(text.translate(_LEET))))


def _phone_scoped(toks: list[str], i: int, j: int) -> bool:
    if any(t in PHONE_SCOPE_WORDS for t in toks[i:j]):
        return True
    k, skipped = j, 0
    while k < len(toks) and toks[k] in SCOPE_CONNECTORS and skipped < SCOPE_LOOKAHEAD:
        k, skipped = k + 1, skipped + 1
    return k < len(toks) and toks[k] in PHONE_SCOPE_WORDS


def opt_out_scope(text: Optional[str]) -> Optional[str]:
    """The scope of the opt-out phrases in ``text`` (already stripped to the person's own words): ``"sms"`` when
    every phrase found names the phone channel itself ("stop texting", "remove my number", "unsubscribe from
    texts"), ``"all"`` when at least one does not ("unsubscribe", "stop", "do not email me", a typo of either, a
    bare "no"), None when no phrase is found."""
    if not text:
        return None
    found, generic = False, False
    variants = {normalise(text), normalise(text.translate(_LEET))}
    variants |= {" " + " ".join(_collapse(w) for w in v.split()) + " " for v in list(variants)}
    terms = [normalise(t).split() for t in OPT_OUT_TERMS]
    for norm in variants:
        toks = norm.split()
        if norm.strip() in OPT_OUT_SHORT:
            found = generic = True
        for tt in terms:
            n = len(tt)
            for i in range(len(toks) - n + 1):
                if toks[i:i + n] == tt:
                    found = True
                    generic = generic or not _phone_scoped(toks, i, i + n)
        for i in _typo_tokens(norm):
            found = True
            generic = generic or not _phone_scoped(toks, i, i + 1)
    if not found:
        return None
    return "all" if generic else "sms"


def email_opt_out(text: Optional[str], subject: Optional[str] = None) -> bool:
    """Sweep A (AEGIS H1, M1): does an email revoke the contact's EMAIL consent? Yes when, in the person's own words
    (quoted lines and the signature stripped) or the subject, there is an exact opt-out or a typo of stop /
    unsubscribe, unless EVERY opt-out phrase found names the phone itself ("stop texting me", "remove my number",
    "unsubscribe from texts"): that one stays an SMS opt-out and the ticket can still be answered by email. Other
    words elsewhere ("Cell: 310 ...", "Sent from my phone", a quoted "call our phone line", a subject "Re: Text us
    anytime") never narrow it. An exact opt-out whose phrase is not found in the stripped text (only in the
    signature, a symbol, a negation near a channel word) revokes email too: err toward honouring."""
    own = strip_quoted(text or "")
    exact = any(t and opt_out_level(t) == "exact" for t in (own, subject))
    typo = typo_opt_out(own) or typo_opt_out(subject)
    if not exact and not typo:
        return False
    scopes = {opt_out_scope(strip_signature(own)), opt_out_scope(subject)} - {None}
    return scopes != {"sms"}


def is_opt_out(text: str) -> bool:
    return opt_out_level(text) is not None


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
