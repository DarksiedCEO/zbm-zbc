"""Inbound reply classification (ADR 0013 decision 13). Deterministic keyword rules, checked in this order:

0. The text is normalised first (AEGIS S1-H3): Unicode NFKC (fullwidth ``ＳＴＯＰ`` is ``stop``), accents removed,
   case folded, and punctuation BETWEEN letters removed (``S.T.O.P`` is ``stop``, ``opt-out`` is ``optout``); other
   punctuation becomes a space. War room (ADR 0018): lookalike letters are folded with the repo's shared lookalike
   fold (``src/lookalikes.py``, the same file service-py and three other services carry: invisible characters out,
   Cyrillic / Greek / Armenian / Cherokee / small-capital / IPA lookalikes and the Unicode confusables skeleton, this
   module's ``_CONFUSABLE`` on top so nothing it read changes), digits inside a word and lone digits in a spelled-out
   run are read as letters (``s 7 o p``), and an HTML body is also read with its tags as spaces and its entities
   decoded. A word written wholly in ONE non-Latin script is a word of that language, not a disguise (service-py's
   WR-F006: Russian ``по`` is "by", not "no"): it is read with ``_CONFUSABLE`` alone, as before, unless the shared
   fold makes it one of ``OPT_OUT_DISGUISE_WORDS`` (an all-Cherokee ``ᏚᎢᎾᏢ``).
1. ``unsubscribe`` — any channel: STOP, STOPALL, UNSUBSCRIBE (any form), CANCEL, END, QUIT, REVOKE, OPT OUT, "remove
   me", "take me off", "do not contact/email/text/call", "stop texting/emailing/calling", declines ("not interested",
   "no thanks", "no thank you"), and Spanish (alto, parar, cancelar, baja, "no mas mensajes"). On SMS and voice the
   bar is lower (TCPA: revocation "by any reasonable means"): cancel, end, quit, stop, unsubscribe, opt out or revoke
   ANYWHERE in the message, "wrong number", "lose my number", "leave me alone", "remove", "no more texts / messages
   / calls". When in doubt, suppress. An opt-out suppresses every channel for both brands (svc_outreach.reply).
2. ``out_of_office`` — auto-replies ("out of office", "OOO", "automatic reply", "auto-reply", "on vacation",
   "away until", "on leave", "currently away"): a reschedule task.
3. ``interested`` — "interested", "let's talk", "book", "schedule", "set up a call", "call me", "sounds good",
   "tell me more", "send details", "send more info", "pricing", "what does it cost", "yes": a task to book a call.
4. anything else — the human review queue.

The label is NOT what protects the person (AEGIS S3-C1): ANY reply on ANY channel holds phone outreach to the
contact's numbers for both brands unless its raw body is exactly one of ``AUTO_REPLIES``; only Andre lifts a hold, by
deciding the review task is "not an opt-out". The label only names the task and, for opt-out wording, adds a
permanent suppression.

The reply text is never stored: only its SHA-256 and the class. Never: answers the person."""

from __future__ import annotations

import functools
import html
import re
import unicodedata

import lookalikes

NUMBER = 10
NAME = "reply_classifier"
DECIDES = "unsubscribe / out_of_office / interested / review"

_CARRIER = {"stop", "stopall", "unsubscribe", "unsub", "cancel", "end", "quit", "revoke", "optout", "opt out", "stop all"}
_UNSUB = re.compile(r"\b(stop|stopall|unsub\w*|opt ?out|revoke|remove me|take me off|do not (contact|email|"
                    r"text|call)|dont (contact|email|text|call)|stop (texting|emailing|calling|contacting)|"
                    r"not interested|no thanks|no thank you|leave me alone|wrong person|no more (emails?|messages?)|"
                    r"alto|parar|cancelar|baja|no mas mensajes)\b")
_UNSUB_PHONE = re.compile(r"\b(cancel\w*|end|quit|stop\w*|unsub\w*|opt ?out|revoke\w*|wrong (number|person)|"
                          r"lose my number|leave me alone|remove\w*|no more (texts?|messages?|calls?))\b")
_OOO = re.compile(r"\b(out of (the )?office|ooo|automatic reply|auto ?reply|on vacation|away until|on leave|"
                  r"currently away|limited access to email)\b")
_INTERESTED = re.compile(r"\b(interested|lets talk|book|schedule|set up a call|call me|sounds good|tell me more|"
                         r"send (me )?(the )?details|send (me )?more info|pricing|what does it cost|yes)\b")


# Confusables that render like Latin letters (Cyrillic, Greek) and digits used as letters inside a word (AEGIS S2-C1).
# This only improves the LABEL; the safety is the fail-closed hold in svc_outreach.reply (AEGIS S3-C1).
_CONFUSABLE = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t", "у": "y",
    "х": "x", "ѕ": "s", "і": "i", "ј": "j", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ɡ": "g",
    "α": "a", "β": "b", "ε": "e", "ζ": "z", "η": "n", "ι": "i", "κ": "k", "μ": "m", "ν": "v", "ο": "o", "ρ": "p",
    "τ": "t", "υ": "u", "χ": "x"})
_LEET = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_LEET_I = {**_LEET, ord("1"): "i"}          # "1" stands for "i" as often as for "l" (1nterested); a second reading
_LEET_DIGITS = frozenset("013457")

# War room (ADR 0018): the shared lookalike fold with this module's table on top (ADR 0013 "War room fixes").
_OWN = {chr(k): v for k, v in _CONFUSABLE.items()}
_TABLE = lookalikes.Table(_OWN)
# The letters this table reads otherwise than the shared fold (Greek mu: "m" here, "u" there, as a "μ" for "u" in
# "μnsubscribe"). Opt-out wording is read a second time with the shared reading of those letters (an opt-out in either
# reading suppresses: over-suppressing is the safe side); the other labels keep this table's reading.
_SHARED_READS = frozenset(c for c, v in _OWN.items() if lookalikes.Table().mapping.get(c, v) != v)
_OWN_SHARED = {c: v for c, v in _OWN.items() if c not in _SHARED_READS}
_TABLE_SHARED = lookalikes.Table(_OWN_SHARED)
_CONFUSABLE_SHARED = str.maketrans(_OWN_SHARED)
# A word wholly in one non-Latin script gets the shared layers only when they make it one of these: the English single
# opt-out words of four letters or more that the rules below read (tests/test_warroom_fixes.py checks the list against
# _CARRIER, _UNSUB and _UNSUB_PHONE). Shorter words ("end", "no") and the Spanish words are left out: a real word of
# another script can read as one (Russian "по" is "no"; Serbian "Баја" is "baja"), as in service-py (WR-F006).
OPT_OUT_DISGUISE_WORDS = frozenset({"stop", "stopall", "unsubscribe", "unsub", "cancel", "cancelled", "canceled",
                                    "quit", "revoke", "optout", "remove"})


def _squeeze(w: str) -> str:
    return re.sub(r"(.)\1+", r"\1", w)


_DISGUISE_SQUEEZED = frozenset(_squeeze(w) for w in OPT_OUT_DISGUISE_WORDS)


# brackets, quotation marks and mathematical symbols (Ps Pe Pi Pf Sm: "(", "«", "≪", "＜" ...), for the reading in which
# they separate words (``split_brackets``)
_BRACKETS = re.compile("[" + lookalikes.char_class([cp for cp in range(0x110000) if unicodedata.category(chr(cp))
                                                    in ("Ps", "Pe", "Pi", "Pf", "Sm")]) + "]+")
# every character with a non-zero canonical combining class, as one regex class (the whole code space scanned once;
# one C-level deletion instead of a Python loop per character, as service-py's triage._COMBINING_CHARS)
_COMBINING = re.compile("[" + lookalikes.char_class([cp for cp in range(0x110000)
                                                    if unicodedata.combining(chr(cp))]) + "]+")


def _strip_accents(t: str) -> str:
    return t if t.isascii() else _COMBINING.sub("", unicodedata.normalize("NFKD", t))


def _opt_out_disguise(word: str) -> bool:
    """A word's full lookalike fold reads as an opt-out word (accents and repeated letters ignored)."""
    w = re.sub(r"[^a-z]", "", _strip_accents(word).casefold())
    return w in OPT_OUT_DISGUISE_WORDS or _squeeze(w) in _DISGUISE_SQUEEZED


# HTML read as text (the tags service-py's channels.html_as_text knows): a block tag ends a line, any other tag is a
# space. Deleting nothing between tags: "<p>StoP</p>" was read as "pstopp" (the rule below joins letters across
# punctuation), "<p>not interested, thanks</p>" as "pnot interested thanksp".
_BLOCK_TAG = re.compile(r"<\s*/?\s*(?:br|p|div|li|tr|h[1-6]|table|ul|ol|hr)\b[^<>]{0,500}>", re.IGNORECASE)
_HTML_TAGS = ("a|abbr|b|big|blockquote|body|center|cite|code|col|colgroup|dd|del|dfn|dl|dt|em|font|footer|head|header|"
              "html|i|img|ins|kbd|label|link|main|mark|meta|nav|o:p|pre|q|s|section|small|span|strike|strong|style|sub|"
              "sup|tbody|td|tfoot|th|thead|title|tt|u|var|wbr|v:[a-z]+|w:[a-z]+")
_ANY_TAG = re.compile(r"<!--.{0,2000}?-->|<\s*/?\s*(?:" + _HTML_TAGS + r")\b[^<>]{0,500}>", re.IGNORECASE | re.DOTALL)


def html_as_text(text: str) -> str:
    """Entities decoded (a double-escaped one too: service-py's triage.clean order), then tags read as spaces (a block
    tag as a line end). Text without an entity or a tag is returned unchanged."""
    t = text
    for _ in range(3):
        if "&" not in t:
            break
        u = html.unescape(t)
        if u == t:
            break
        t = u
    if "<" in t and ">" in t:
        t = _ANY_TAG.sub(" ", _BLOCK_TAG.sub("\n", t))
    return t


_LEET_CHAR = re.compile(r"[013457@$]")
_LETTER = re.compile(r"[^\W\d_]")


def _fold_word(w: str, leet: dict = _LEET) -> str:
    """Digits and @/$ inside a word that also has letters are read as letters (St0p -> stop; 310 stays 310)."""
    if _LEET_CHAR.search(w) and _LETTER.search(w):
        return w.translate(leet)
    return w


def normalise(text: str, shared: bool = False, casefold_first: bool = False, split_brackets: bool = False,
              one_as_i: bool = False) -> str:
    """The text as the rules read it (step 0 above). ``shared``: the letters in ``_SHARED_READS`` read as the shared
    fold reads them. ``casefold_first``: case folded before the lookalike fold, as this module read text before the
    war room (a capital Greek eta "Η" is then "η", "n"; folded as written it is the "H" it looks like).
    ``split_brackets``: brackets, quotation marks and mathematical symbols separate words instead of being dropped
    between letters ("≪b≫unsubscribe" -> "b unsubscribe"). ``one_as_i``: a "1" read as a letter is "i", not "l"."""
    return _finish(_prepare(text, shared, casefold_first), split_brackets, one_as_i)


@functools.lru_cache(maxsize=16)     # a pure function; classify reads one reply in several readings
def _prepare(text: str, shared: bool, casefold_first: bool) -> str:
    t = unicodedata.normalize("NFKC", lookalikes.strip_invisible(text))
    if not t.isascii():
        if casefold_first:
            t = t.casefold()
        t = (_TABLE_SHARED if shared else _TABLE).fold(t, single_script=_opt_out_disguise)
    t = _strip_accents(t).casefold()
    # sweep A: "_" separates words (S_T_O_P, please_unsubscribe)
    return t.translate(_CONFUSABLE_SHARED if shared else _CONFUSABLE).replace("_", " ")


def _finish(t: str, split_brackets: bool, one_as_i: bool) -> str:
    leet = _LEET_I if one_as_i else _LEET
    if _LEET_CHAR.search(t):
        t = " ".join(_fold_word(w, leet) for w in t.split())
    if split_brackets:
        t = _BRACKETS.sub(" ", t)
    # S.T.O.P -> stop, don't -> dont, opt-out -> optout; "<" and ">" always separate words (a tag the HTML reading does
    # not know, "<b>" disguised as "<в>", must not glue "b>unsubscribe</b" into one word)
    t = re.sub(r"(?<=[^\W\d_])[^\w\s<>]+(?=[^\W\d_])", "", t)
    t = re.sub(r"[^\w\s]+", " ", t)
    words, out, run = t.split(), [], []
    for w in words + [""]:
        # S T O P -> stop: a run of 3+ single letters; a lone leet digit in it counts as a letter when the run holds a
        # real letter (s 7 o p -> stop; 2 0 2 4 stays a number), as clipper-network-py does (WR-F002)
        if len(w) == 1 and (w.isalpha() or w in _LEET_DIGITS):
            run.append(w)
            continue
        out += _join_run(run, leet)
        run = []
        if w:
            out.append(w)
    return " ".join(out)


def _join_run(run: list[str], leet: dict) -> list[str]:
    if len(run) >= 3 and any(c.isalpha() for c in run):
        return ["".join(run).translate(leet)]
    return run


def _collapse(t: str) -> str:
    """Repeated letters collapsed (service-py's channels._collapse): "stoooop" -> "stop"."""
    return re.sub(r"([^\W\d_])\1+", r"\1", t)


def classify(text: str, channel: str = "email") -> str:
    as_text = html_as_text(text)
    t = normalise(as_text)
    # sweep A: repeated letters collapsed too ("stoooop", "unsubbbscribe"); only for opt-out wording (over-suppressing
    # is the safe side), never for the other labels. War room: opt-out wording is read in every reading below, and an
    # opt-out in any of them suppresses: the text as HTML read as text and as written; capitals as written and case
    # folded first (the reading before the war room); when the text holds one of _SHARED_READS, with the shared fold's
    # reading of it; with brackets / quotation marks / math symbols as word separators (an entity disguised by its
    # case, "&Lt;b&Gt;", decodes to "≪b≫"); and with a "1" as "i". The other labels read the HTML as text, capitals as
    # written, with this module's table.
    readings = []
    for source in dict.fromkeys((as_text, text)):
        options = [(False, False)]
        nfkc = source if source.isascii() else unicodedata.normalize("NFKC", source)
        if not nfkc.isascii():
            cased = nfkc.casefold() != nfkc        # case folding first changes nothing in a caseless text
            options += [(False, True)] if cased else []
            if not _SHARED_READS.isdisjoint(nfkc.casefold()):
                options += [(True, False)] + ([(True, True)] if cased else [])
        for shared, cf in options:
            p = _prepare(source, shared, cf)
            readings.append(t if (source, shared, cf) == (as_text, False, False) else _finish(p, False, False))
            if _BRACKETS.search(p):
                readings.append(_finish(p, True, False))
            if "1" in p:
                readings.append(_finish(p, False, True))
    for v in dict.fromkeys(x for r in readings for x in (r, _collapse(r))):
        if v in _CARRIER or _UNSUB.search(v):
            return "unsubscribe"
        if channel != "email" and _UNSUB_PHONE.search(v):         # sms, voice, or a channel we do not know
            return "unsubscribe"
    if _OOO.search(t):
        return "out_of_office"
    if _INTERESTED.search(t):
        return "interested"
    return "review"


# AEGIS S3-C1 — the ONLY replies that do not hold phone outreach: these exact bodies, compared after trimming
# whitespace and lower-casing and nothing else (no regex, no normalising, no tail). They are machine texts that carry
# no person's words. Everything else, on every channel, holds the contact's numbers until Andre releases them.
AUTO_REPLIES = frozenset({
    "i'm driving with do not disturb while driving turned on. i'll see your message when i get where i'm going.",
    "i\u2019m driving with do not disturb while driving turned on. i\u2019ll see your message when i get where "
    "i\u2019m going.",
    "i am currently out of the office.",
    "i am out of the office.",
    "out of office",
})


def exact_auto_reply(text: str) -> bool:
    return text.strip().lower() in AUTO_REPLIES
