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
    raw = text.replace("<", " ").replace(">", " ")   # AEGIS R6: "<STOP>", "<3 ... stop texting me >:(" on SMS
    text = html_as_text(text)                    # AEGIS re-review N4: "Unsubscribe<br>Sent ..." is two words
    variants = {normalise(text), normalise(text.translate(_LEET)), normalise(raw), normalise(raw.translate(_LEET))}
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
# AEGIS re-review of 1e709a0 (H1 residual): an email word anywhere in the phrase's reach makes it an email opt-out too
# ("unsubscribe me from your texts and emails", "remove my number and my email")
EMAIL_SCOPE_WORDS = frozenset({"email", "emails", "emailing", "emailed", "mail", "mails", "mailing", "mailings",
                               "newsletter", "newsletters", "inbox", "list", "lists"})
# words that may sit between an opt-out word and the channel it names ("unsubscribe me from your texts")
SCOPE_CONNECTORS = frozenset({"me", "us", "from", "your", "our", "the", "all", "any", "these", "those", "this", "my",
                              "of", "to", "with", "sending", "send", "receiving", "getting", "get", "please", "pls",
                              "plz", "more", "future", "and", "or", "nor", "plus", "also", "both", "either", "e"})
SCOPE_LOOKAHEAD = 8
# a phone word followed by these is a label for an address ("Cell: 310 ...", "my number is ..."), not a channel
_ADDRESS_LABEL_NEXT = frozenset({"is", "was", "changed", "here"})
_QUOTE_HEADER = re.compile(r"^\s*(on\b.{0,300}\bwrote\s*:|el\b.{0,300}\bescribi[oó]\s*:|le\b.{0,300}\ba [eé]crit\s*:|"
                           r"am\b.{0,300}\bschrieb.{0,100}:|em\b.{0,300}\bescreveu\s*:|"
                           r"-{2,}\s*(original message|forwarded message)\s*-{2,}|_{5,})\s*$", re.IGNORECASE)
_SIG_DELIM = re.compile(r"^\s*(--|__)\s*$")
_SENT_FROM = re.compile(r"^\s*sent from (my|mail for|outlook|yahoo|gmail)\b", re.IGNORECASE)
_SIGN_OFF = re.compile(r"^\s*(thanks|thank you|many thanks|thx|regards|best|best regards|kind regards|warm regards|"
                       r"cheers|sincerely|yours truly|respectfully)\s*[,.!]*\s*$", re.IGNORECASE)
# AEGIS re-review of 1e709a0 (N4): HTML is read as text before anything else — a block tag ends a line, any other tag
# is a space (deleting it merged "Unsubscribe<br>Sent" into one word), a <blockquote> is the quoted thread
_BLOCKQUOTE = re.compile(r"<\s*blockquote\b[^<>]{0,500}>(?:(?!<\s*blockquote\b).)*?<\s*/\s*blockquote\s*>",
                         re.IGNORECASE | re.DOTALL)
_BLOCKQUOTE_OPEN = re.compile(r"<\s*blockquote\b[^<>]{0,500}>", re.IGNORECASE)
_BLOCK_TAG = re.compile(r"<\s*/?\s*(br|p|div|li|tr|h[1-6]|table|ul|ol|hr)\b[^<>]{0,500}>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^<>]{0,500}>")
_SEGMENT_SPLIT = re.compile(r"[\n\r.!?;]+")
# Opt-out wording strong enough to honour even inside the unmarked part of a reply (an Outlook "Original Message"
# block, or "On ... wrote:" with no ">" lines) — it cannot be told apart from a reply typed below the quote. Our own
# outbound email carries none of these today. A future marketing footer must be listed in OWN_FOOTER_LINES, or every
# reply quoting it would opt the contact out.
OPT_OUT_STRONG = ("unsubscribe", "unsub", "stopall", "do not email", "dont email", "do not e mail", "dont e mail",
                  "stop emailing", "remove my email", "no more emails", "no more email", "any more emails", "anymore emails", "take me off your list",
                  "take me off your mailing list", "remove me from your list", "remove me from your mailing list")
TAIL_LAST_LINE_MAX_WORDS = 4
# a short last line below an unmarked quote revokes only when it is nothing but stop / unsubscribe (and courtesy words):
# "Cancel anytime." or "Offer ends soon." as the last line of our own quoted message must never opt anyone out
_TAIL_LAST_LINE_CORE = frozenset({"stop", "stopall", "unsubscribe", "unsub"})
_TAIL_LAST_LINE_WORDS = _TAIL_LAST_LINE_CORE | {"please", "pls", "plz", "thanks", "thank", "you", "thx", "now", "me",
                                                "it", "already", "ok", "okay"}
OWN_FOOTER_LINES: tuple[str, ...] = ()


def html_as_text(text: str) -> str:
    """Tags read as text: <br>, <p>, <div> ... end a line, any other tag is a space. Entities are left to
    ``normalise`` (triage.clean unescapes them). Plain text without a tag is returned unchanged."""
    if not text or "<" not in text or ">" not in text:
        return text or ""
    return _ANY_TAG.sub(" ", _BLOCK_TAG.sub("\n", text))


def split_reply(text: str) -> tuple[str, str]:
    """(the person's own text, the unmarked quoted tail). ``>`` lines and a ``<blockquote>`` are dropped. A reply
    header ("On ... wrote:") followed by ``>`` lines is dropped and the lines AFTER the quoted block stay the person's
    own (a reply typed below the quote — AEGIS re-review N1). A header followed by unmarked lines (Outlook's
    "-----Original Message-----", "On ... wrote:" without ``>``) starts the tail: what follows cannot be told apart
    from our own quoted message, and is read only for strong opt-out wording (``quoted_tail_opt_out``)."""
    t = text or ""
    unclosed_tail = ""
    if "<" in t and ">" in t:
        for _ in range(50):                   # innermost first: nested quotes, and text between two quotes, kept right
            u = _BLOCKQUOTE.sub("\n", t)
            if u == t:
                break
            t = u
        m = _BLOCKQUOTE_OPEN.search(t)        # an unclosed quote: the rest is the quoted tail
        if m:
            t, unclosed_tail = t[:m.start()], html_as_text(t[m.end():])
    lines = html_as_text(t).splitlines()
    own: list[str] = []
    tail: list[str] = []
    in_tail = False
    for idx, line in enumerate(lines):
        if in_tail:
            tail.append(line)
            continue
        joined = (line.rstrip() + " " + lines[idx + 1].strip()) if idx + 1 < len(lines) else ""
        if _QUOTE_HEADER.match(line) or (joined and not _QUOTE_HEADER.match(lines[idx + 1])
                                           and _QUOTE_HEADER.match(joined)):
            if not _QUOTE_HEADER.match(line):
                lines[idx + 1] = ""           # the header's second line (Gmail wraps "... <a@b>\nwrote:")
            nxt = next((nl for nl in lines[idx + 1:] if nl.strip()), "")
            if nxt.lstrip().startswith(">"):
                continue                       # a marked quote follows: drop the header, keep reading
            in_tail = True
            continue
        if line.lstrip().startswith(">"):
            continue
        own.append(line)
    if unclosed_tail.strip():
        tail.append(unclosed_tail)
    return "\n".join(own), "\n".join(tail)


def strip_quoted(text: str) -> str:
    """The person's own text of an email (``split_reply``): quoted lines and the unmarked quoted tail dropped."""
    return split_reply(text)[0]


def strip_signature(text: str) -> str:
    """``strip_quoted`` and then the signature: everything from a ``--`` delimiter or a sign-off line ("Thanks,",
    "Best regards") on, and "Sent from my ..." lines. Not used to decide an opt-out (AEGIS re-review: text after a
    sign-off line can be another opt-out)."""
    out = []
    for line in strip_quoted(text).splitlines():
        if _SIG_DELIM.match(line) or (_SIGN_OFF.match(line) and out):
            break
        if _SENT_FROM.match(line):
            continue
        out.append(line)
    return "\n".join(out)


def _tail_without_signature(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        if _SIG_DELIM.match(line) or (_SIGN_OFF.match(line) and any(o.strip() for o in out)):
            break
        if _SENT_FROM.match(line):
            continue
        out.append(line)
    return out


def quoted_tail_opt_out(text: Optional[str]) -> Optional[str]:
    """AEGIS re-reviews of 1e709a0 / 91f5b8b (N1, R2, R4): the unmarked quoted tail of an email (an Outlook "Original
    Message" block, "On ... wrote:" without ``>``) cannot be told apart from a reply typed below it. ``"revoke"``:
    strong unsubscribe wording (``OPT_OUT_STRONG``) anywhere in it, or a short last line (signature removed) that is
    an exact opt-out ("STOP", "STOP. Thanks", "stop please"). ``"alert"``: any other opt-out wording — Andre reads
    it (it may be a third party's quoted words). None: nothing. Lines of our own footers are ignored."""
    tail = split_reply(text or "")[1]
    if not tail.strip():
        return None
    body = tail
    for f in OWN_FOOTER_LINES:                  # substring match on the joined tail survives re-wrapping
        body = re.sub(r"\s+".join(map(re.escape, f.split())), " ", body, flags=re.IGNORECASE)
    variants = {normalise(body), normalise(body.translate(_LEET))}
    variants |= {" " + " ".join(_collapse(w) for w in v.split()) + " " for v in list(variants)}
    if any(f" {normalise(t).strip()} " in v for v in variants for t in OPT_OUT_STRONG):
        return "revoke"
    kept = [ln for ln in _tail_without_signature(body.splitlines()) if normalise(ln).strip()]
    last = kept[-1] if kept else ""
    toks = [_collapse(t) for t in normalise(last).split()]
    if toks and len(toks) <= TAIL_LAST_LINE_MAX_WORDS and set(toks) <= _TAIL_LAST_LINE_WORDS \
            and set(toks) & _TAIL_LAST_LINE_CORE:
        return "revoke"
    return "alert" if opt_out_level(body) is not None else None


def _typo_tokens(norm: str) -> list[int]:
    toks = norm.split()
    return [i for i, t in enumerate(toks) if len(t) >= 4 and t not in OPT_OUT_FUZZY
            and any(_one_edit(t, stem) for stem in OPT_OUT_FUZZY)]


def typo_opt_out(text: Optional[str]) -> bool:
    """A word one typo from stop / unsubscribe / stopall ("unsubcribe", "unsubscibe"): the ``suspected`` level."""
    if not text:
        return False
    t = html_as_text(text)
    raw = text.replace("<", " ").replace(">", " ")
    return any(_typo_tokens(v) for v in (normalise(t), normalise(t.translate(_LEET)), normalise(raw)))


def _phone_hit(toks: list[str], k: int) -> bool:
    if toks[k] not in PHONE_SCOPE_WORDS:
        return False
    nxt = toks[k + 1] if k + 1 < len(toks) else ""
    return not (nxt.isdigit() or nxt in _ADDRESS_LABEL_NEXT)


def _phone_scoped(toks: list[str], i: int, j: int) -> bool:
    """True only when the phrase toks[i:j] names the phone channel (in the phrase or within SCOPE_LOOKAHEAD
    connector / channel words after it) and names no email channel in that same reach. Segments never cross a line
    or a sentence end (``opt_out_scope``)."""
    phone = email = False
    for k in range(i, j):
        phone = phone or _phone_hit(toks, k)
        email = email or toks[k] in EMAIL_SCOPE_WORDS
    k, skipped = j, 0
    while k < len(toks) and skipped < SCOPE_LOOKAHEAD:
        t = toks[k]
        if t in EMAIL_SCOPE_WORDS:
            email = True
        elif t in PHONE_SCOPE_WORDS:
            phone = phone or _phone_hit(toks, k)
        elif t not in SCOPE_CONNECTORS:
            break
        k, skipped = k + 1, skipped + 1
    return phone and not email


def opt_out_scope(text: Optional[str]) -> Optional[str]:
    """The scope of the opt-out phrases in ``text`` (already the person's own words): ``"sms"`` when every phrase
    found names the phone channel itself and no email channel ("stop texting", "remove my number", "unsubscribe from
    texts"), ``"all"`` when at least one does not ("unsubscribe", "stop", "do not email me", "unsubscribe from texts and
    emails", a typo of either, a bare "no"), None when no phrase is found. Each line and each sentence is read on its
    own, so a phone word on the next line ("STOP / my number is ...") never narrows it."""
    if not text:
        return None
    found, generic = False, False
    terms = [normalise(t).split() for t in OPT_OUT_TERMS]
    whole = html_as_text(text)
    for single, seg in [(False, whole)] + [(True, s) for s in _SEGMENT_SPLIT.split(whole)]:
        if not seg.strip():
            continue
        variants = {normalise(seg), normalise(seg.translate(_LEET))}
        variants |= {" " + " ".join(_collapse(w) for w in v.split()) + " " for v in list(variants)}
        for norm in variants:
            toks = norm.split()
            if norm.strip() in OPT_OUT_SHORT:
                found = generic = True
            if not single:
                continue                    # the whole text only for a bare short reply; phrases are read per segment
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
    # AEGIS R3: an email word anywhere in the person's own words ("stop texting me, same for email") widens it
    if any(t in EMAIL_SCOPE_WORDS for t in normalise(whole).split()):
        return "all"
    return "all" if generic else "sms"


def email_opt_out(text: Optional[str], subject: Optional[str] = None) -> bool:
    """Sweep A (AEGIS H1, M1; re-review of 1e709a0): does an email revoke the contact's EMAIL consent? Yes when, in
    the person's own words (``split_reply``: ``>`` lines and the quoted tail out, text typed below a marked quote IN)
    or the subject, there is an exact opt-out or a typo of stop / unsubscribe, or the unmarked quoted tail carries
    strong opt-out wording — unless EVERY opt-out phrase found names the phone itself and no email channel ("stop
    texting me", "remove my number", "unsubscribe from texts"): that one stays an SMS opt-out. Other words elsewhere
    ("Cell: 310 ...", "Sent from my phone", a quoted "call our phone line", a subject "Re: Text us anytime", a phone
    word on the next line) never narrow it. An exact opt-out whose phrase is not found in the text (a symbol, a
    negation near a channel word) revokes email too: err toward honouring."""
    own = strip_quoted(text or "")
    exact = any(t and opt_out_level(t) == "exact" for t in (own, subject))
    typo = typo_opt_out(own) or typo_opt_out(subject)
    if quoted_tail_opt_out(text) == "revoke":
        return True
    if not exact and not typo:
        return False
    scopes = {opt_out_scope(own), opt_out_scope(subject)} - {None}
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
