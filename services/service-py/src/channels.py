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
                              "plz", "more", "future", "and", "or", "nor", "plus", "also", "both", "either", "e", "as", "well",
                              "spamming", "bothering", "any"})
SCOPE_LOOKAHEAD = 8
# AEGIS re-review of b7cc067 (H-F): scope is decided only when clear. Outside a phrase's own reach, these ADD email
# ("same for email", "email too"), these ask to be reached by email ("email me instead", "you can call or email",
# "emails are fine", "my email is ..."); anything else with an email word is unclear: SMS only, and Andre decides.
_EMAIL_ADDERS = frozenset({"too", "also", "same", "either", "neither", "both", "spamming", "spam", "everything"})
_EMAIL_PREFERENCE = frozenset({"instead", "prefer", "rather", "only", "use", "reach", "contact", "can", "could", "if",
                               "fine", "ok", "okay", "works", "better", "anytime", "whenever", "best", "good",
                               "welcome"})
_NEG_EMAIL = re.compile(r"\b(not|dont|never|stop|quit|no more|no)(?: (?:me|sending|send|receiving|getting|any|more|your|"
                        r"the|with|to|me any|sending me)){0,3} (e ?mail|emails|emailing|inbox|newsletters?)\b")
# a phone word followed by these is a label for an address ("Cell: 310 ...", "my number is ..."), not a channel
_ADDRESS_LABEL_NEXT = frozenset({"is", "was", "changed", "here"})
_QUOTE_HEADER = re.compile(r"^\s*(on\b.{0,300}\bwrote\s*:|el\b.{0,300}\bescribi[oó]\s*:|le\b.{0,300}\ba [eé]crit\s*:|"
                           r"am\b.{0,300}\bschrieb.{0,100}:|em\b.{0,300}\bescreveu\s*:|"
                           r"-{2,}\s*(original message|forwarded message)\s*-{2,}|begin forwarded message\s*:|_{5,})\s*$",
                           re.IGNORECASE)
_FORWARD_HEADER = re.compile(r"^\s*(-{2,}\s*forwarded message\s*-{2,}|begin forwarded message\s*:)\s*$", re.IGNORECASE)
# AEGIS H-B: Outlook for Mac / new Outlook quote a message with a header block and no "Original Message" line
_HDR_FROM = re.compile(r"^\s*\**(from|de|von)\s*:\**\s+\S", re.IGNORECASE)
_HDR_THIRD = re.compile(r"^\s*\**(to|cc|subject|para|asunto|an|betreff|objet|[àa])\s*:\**\s*\S", re.IGNORECASE)
_HDR_DATE = re.compile(r"^\s*\**(sent|date|enviado|fecha|gesendet|datum|envoy[eé])\s*:\**\s+\S", re.IGNORECASE)
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
_STYLE_SCRIPT = re.compile(r"<(style|script)\b[^<>]{0,500}>[^<]{0,50000}(?:<(?!/\1)[^<]{0,50000}){0,200}</\1\s*>",
                           re.IGNORECASE)
_HTML_TAGS = ("a|abbr|b|big|blockquote|body|center|cite|code|col|colgroup|dd|del|dfn|dl|dt|em|font|footer|head|header|"
              "html|i|img|ins|kbd|label|link|main|mark|meta|nav|o:p|pre|q|s|section|small|span|strike|strong|style|sub|"
              "sup|tbody|td|tfoot|th|thead|title|tt|u|var|wbr|v:[a-z]+|w:[a-z]+")
_ANY_TAG = re.compile(r"<!--.{0,2000}?-->|<\s*/?\s*(?:" + _HTML_TAGS + r")\b[^<>]{0,500}>", re.IGNORECASE | re.DOTALL)
_SEGMENT_SPLIT = re.compile(r"[\n\r.!?;]+")
# Opt-out wording strong enough to honour even inside the unmarked part of a reply (an Outlook "Original Message"
# block, or "On ... wrote:" with no ">" lines) — it cannot be told apart from a reply typed below the quote. Our own
# outbound email carries none of these today. A future marketing footer must be listed in OWN_FOOTER_LINES, or every
# reply quoting it would opt the contact out.
OPT_OUT_STRONG = ("unsubscribe", "unsub", "stopall", "do not email", "dont email", "do not e mail", "dont e mail",
                  "stop emailing", "remove my email", "no more emails", "no more email", "any more emails", "anymore emails", "take me off your list",
                  "take me off your mailing list", "remove me from your list", "remove me from your mailing list")
TAIL_LAST_LINE_MAX_WORDS = 4
# AEGIS M-1: in the quoted tail only an opt-out PHRASE alerts Andre — single words ("end", "cancel", "stop") and
# loose pairs ("no more than one update") turn up in our own quoted mail and would wear the alert down
_TAIL_ALERT_EXCLUDED = frozenset({"no more", "who is this", "wrong person"})
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
    low = text.lower()
    if "</style" in low or "</script" in low:      # AEGIS M-7: never scan an unclosed flood
        text = _STYLE_SCRIPT.sub(" ", text)
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
    forwarded = False
    for idx, line in enumerate(lines):
        if in_tail:
            tail.append(line)
            continue
        if _HDR_FROM.match(line) and any(_HDR_DATE.match(nl) for nl in lines[idx + 1:idx + 4]) \
                and ("@" in line or any(_HDR_THIRD.match(nl) for nl in lines[idx + 1:idx + 5])):
            in_tail = True                    # AEGIS H-B: "From: ... / Date: ... / To: ... / Subject: ..." block
            continue
        if _FORWARD_HEADER.match(line):
            forwarded = True                  # AEGIS M-2: a forwarded message is someone else's words, never read
            break
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
    if unclosed_tail.strip() and not forwarded:
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
    for f in (f for f in OWN_FOOTER_LINES if f.split()):   # substring match survives re-wrapping; never blank
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
    for ln in kept[-3:]:                      # AEGIS M-4: "Stop!" above a name sign-off ("Jane Doe / CEO, Acme")
        lt = [_collapse(t) for t in normalise(ln).split()]
        if lt and len(lt) <= TAIL_LAST_LINE_MAX_WORDS and set(lt) <= (_TAIL_LAST_LINE_WORDS | {"quit"}) \
                and set(lt) & (_TAIL_LAST_LINE_CORE | {"quit"}):    # AEGIS L-6: "Stop by anytime!" never alerts
            return "alert"
    norms = {normalise(body)}
    norms |= {" " + " ".join(_collapse(w) for w in v.split()) + " " for v in list(norms)}
    return "alert" if any(f" {p} " in n for n in norms for p in _TAIL_ALERT_PHRASES) else None


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


# AEGIS M-9: call wording names a channel for the scope ("do not call, text or email me") but is not an SMS opt-out
# on its own ("Never call before 9 please"): it reaches a suspected level through negated_channel at most
SCOPE_ONLY_TERMS = ("do not call", "dont call", "never call", "stop calling")
_SCOPE_SEGMENT = re.compile(r"[\n\r.!?;,]+")
# "Do not call, text or email me": a comma inside a list of channels is not a clause end
_CH = r"(?:call|text|txt|message|email|e-mail|mail|sms)"
# AEGIS H-G: only a real list of three or more ("call, text or email") — "Don't text, email me instead" is two clauses
_OXFORD_COMMA = re.compile(r"\b(" + _CH[3:-1] + r")\s*,\s*(?=(?:or|and|nor)\s+" + _CH + r"\b)", re.IGNORECASE)
_CHANNEL_LIST_COMMA = re.compile(r"\b(" + _CH[3:-1] + r")\s*,\s*(?=" + _CH + r"(?:\s*,\s*" + _CH + r")*\s*,?\s+(?:or|and|nor)\s+"
                                 + _CH + r"\b)", re.IGNORECASE)
_AFTER_EMAIL_REQUEST = frozenset({"instead", "if", "only", "rather", "anytime", "whenever", "is", "are", "fine", "ok",
                                  "okay", "works", "address", "at"})


def _phone_hit(toks: list[str], k: int) -> bool:
    if toks[k] not in PHONE_SCOPE_WORDS:
        return False
    nxt = toks[k + 1] if k + 1 < len(toks) else ""
    return not (nxt.isdigit() or nxt in _ADDRESS_LABEL_NEXT)


def _in_reach_email(toks: list[str], k: int) -> Optional[bool]:
    """An email word met inside an opt-out phrase's reach (same clause, only connector / channel words between).
    True: it is part of the opt-out ("texts and emails", "text or email me please", "texting and emailing me").
    None: it is not clear (a request may follow — "stop texting me or email me if you must", "... and email me
    instead"); the caller then reads the whole text. Grammatical, not keyword-guessing: after "or / nor / and", a
    base-form "email" following a gerund ("texting") or followed by a request word starts a new, positive request."""
    word = toks[k]
    nxt = toks[k + 1] if k + 1 < len(toks) else ""
    nxt2 = toks[k + 2] if k + 2 < len(toks) else ""
    prev = toks[k - 1] if k > 0 else ""
    if word in ("emailing", "mailing", "emails", "newsletters", "newsletter", "inbox", "mails", "mailings"):
        return True if nxt not in _AFTER_EMAIL_REQUEST else None
    # base form "email" / "mail"
    request_after = nxt in _AFTER_EMAIL_REQUEST or (nxt == "me" and nxt2 in _AFTER_EMAIL_REQUEST)
    if prev in ("or", "nor"):
        phone = next((toks[i] for i in range(k - 1, -1, -1) if toks[i] in PHONE_SCOPE_WORDS), "")
        if phone.endswith("ing"):
            return None                       # "stop texting me or email me ..." — a request
        return True                           # "do not text or email me (please / if you can help it)"
    if prev == "and":
        return None if request_after or nxt == "me" else True
    return None if request_after else True


def _scan_phrase(toks: list[str], i: int, j: int) -> tuple[bool, Optional[bool]]:
    """(phone named, email named) for the phrase toks[i:j] and its reach (SCOPE_LOOKAHEAD connector / channel words
    after it). email: True part of the opt-out, None unclear, False not named."""
    phone, email = False, False
    for k in range(i, j):
        phone = phone or _phone_hit(toks, k)
        if toks[k] in EMAIL_SCOPE_WORDS:
            email = True
    k, skipped = j, 0
    while k < len(toks) and skipped < SCOPE_LOOKAHEAD:
        t = toks[k]
        if t in EMAIL_SCOPE_WORDS:
            got = _in_reach_email(toks, k)
            if got is None:
                return phone, (None if email is not True else True)
            email = True
        elif t in PHONE_SCOPE_WORDS:
            phone = phone or _phone_hit(toks, k)
        elif t not in SCOPE_CONNECTORS:
            break
        k, skipped = k + 1, skipped + 1
    return phone, email


def _phone_scoped(toks: list[str], i: int, j: int) -> bool:
    phone, email = _scan_phrase(toks, i, j)
    return phone and email is not True


def _outside_email(whole: str) -> str:
    """For an SMS-only opt-out whose text also has an email word outside the phrases' reach: ``"all"`` (an explicit
    negated email phrase in one clause, or an adding word: "same for email", "email too"), ``"sms"`` (a request to be
    reached by email: "email me instead", "you can call or email", "emails are fine", "my email is ..."), else
    ``"ask"`` (unclear: SMS only, Andre decides)."""
    clauses = [normalise(c).strip() for c in _SCOPE_SEGMENT.split(whole) if c.strip()]
    if any(_NEG_EMAIL.search(c) for c in clauses):
        return "all"
    email_clauses = [c.split() for c in clauses if any(t in EMAIL_SCOPE_WORDS for t in c.split())]
    if not email_clauses:
        return "sms"
    adds = any(set(c) & _EMAIL_ADDERS or " as well" in f" {' '.join(c)} " for c in email_clauses)
    asks = any(set(c) & _EMAIL_PREFERENCE or re.search(r"\b(e ?mail me|e ?mail (address )?is|e ?mails? (is|are))\b",
                                                       " ".join(c)) for c in email_clauses)
    if adds and not asks:
        return "all"
    if asks and not adds:
        return "sms"
    return "ask"


def opt_out_scope(text: Optional[str]) -> Optional[str]:
    """The scope of the opt-out phrases in ``text`` (the person's own words): ``"all"``, ``"sms"``, ``"ask"`` or None
    (no phrase). ``"all"``: a phrase that names no phone channel ("unsubscribe", "stop", "do not email me", a typo, a
    bare "no"), or one whose reach also names email ("unsubscribe me from your texts and emails", "do not text or
    email me please", "stop texting and emailing me"). ``"sms"``: every phrase names only the phone ("stop texting
    me", "remove my number") and nothing else names email, or the rest asks to be reached by email. ``"ask"``: an SMS
    opt-out with email mentioned in a way that is not clear — SMS only, Andre decides (AEGIS H-F: never guess silently).
    Each clause (line, sentence, comma) is read on its own."""
    if not text:
        return None
    found, generic = False, False
    terms = [normalise(t).split() for t in OPT_OUT_TERMS + SCOPE_ONLY_TERMS]
    whole = _CHANNEL_LIST_COMMA.sub(r"\1 or ", html_as_text(text))
    whole = _OXFORD_COMMA.sub(r"\1 ", whole)    # "call or text, or email" (AEGIS M-11)
    for single, seg in [(False, whole)] + [(True, x) for x in _SCOPE_SEGMENT.split(whole)]:
        if not seg.strip():
            continue
        variants = {normalise(seg), normalise(seg.translate(_LEET))}
        variants |= {" " + " ".join(_collapse(w) for w in v.split()) + " " for v in list(variants)}
        for norm in variants:
            toks = norm.split()
            if norm.strip() in OPT_OUT_SHORT:
                found = generic = True
            if not single:
                continue
            hits = [(i, i + len(tt)) for tt in terms for i in range(len(toks) - len(tt) + 1)
                    if toks[i:i + len(tt)] == tt] + [(i, i + 1) for i in _typo_tokens(norm)]
            for i, j in hits:
                found = True
                phone, email = _scan_phrase(toks, i, j)
                if not phone or email is True:
                    generic = True
    if not found:
        return None
    if generic:
        return "all"
    return _outside_email(whole)              # "unclear" phrases are settled by the whole text, or asked


def email_opt_out_decision(text: Optional[str], subject: Optional[str] = None) -> Optional[str]:
    """Sweep A (AEGIS H1, M1 and the re-reviews of 1e709a0 .. b7cc067): what an email does to the contact's EMAIL
    consent. ``"revoke"``: an exact opt-out or a typo of stop / unsubscribe in the person's own words (``split_reply``)
    or the subject whose scope is ``all``, or strong opt-out wording in the unmarked quoted tail. ``"ask"``: an SMS
    opt-out whose email scope is unclear — email consent is kept and Andre is asked (``EMAIL_OPT_OUT_UNCLEAR``).
    ``"keep"``: an SMS-only opt-out ("stop texting me", "... email me instead"). None: no opt-out. Other words
    elsewhere ("Cell: 310 ...", "Sent from my phone", a quoted "call our phone line", a subject "Re: Text us anytime",
    a phone word on the next line) never narrow it. An exact opt-out whose phrase is not found in the text (a symbol,
    a negation near a channel word) revokes email too: err toward honouring."""
    own = strip_quoted(text or "")
    if quoted_tail_opt_out(text) == "revoke":
        return "revoke"
    exact = any(t and opt_out_level(t) == "exact" for t in (own, subject))
    typo = typo_opt_out(own) or typo_opt_out(subject)
    scopes = {opt_out_scope(own), opt_out_scope(subject)} - {None}
    if not exact and not typo:
        # a scope-only phrase ("do not call, text or email me") decides only with an email channel in it
        return "revoke" if "all" in scopes else None      # AEGIS H-H: "do not call or email me"
    if not scopes or "all" in scopes:
        return "revoke"
    return "ask" if "ask" in scopes else "keep"


def email_opt_out(text: Optional[str], subject: Optional[str] = None) -> bool:
    """Does an email revoke EMAIL consent (``email_opt_out_decision`` is ``"revoke"``)?"""
    return email_opt_out_decision(text, subject) == "revoke"


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


# AEGIS M-1 (see _TAIL_ALERT_EXCLUDED): the multi-word opt-out phrases that alert Andre from a quoted tail
_TAIL_ALERT_PHRASES = tuple(sorted({normalise(t).strip() for t in OPT_OUT_TERMS if len(normalise(t).split()) >= 2}
                                   - _TAIL_ALERT_EXCLUDED) + ["opt me out", "stop sending", "take me off",
                                   "cancel my subscription", "cancel my account", "cancel my membership",
                                   "stop contacting", "quit it", "stop messaging"])
