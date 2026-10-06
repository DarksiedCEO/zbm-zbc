"""Inbound reply classification, copied from sales-py's i10 as fixed in its AEGIS rounds 1-3 (ADR 0015 decision
12). Deterministic keyword rules, checked in this order:

0. The text is normalised first (sales-py S1-H3): Unicode NFKC, accents removed, case folded, confusables folded,
   punctuation BETWEEN letters removed (``S.T.O.P`` is ``stop``), spaced letters joined (``S T O P``).
1. ``unsubscribe`` — any channel: STOP, UNSUBSCRIBE (any form), OPT OUT, REVOKE, "remove me", "take me off", "do not
   contact/email/message", declines ("not interested", "no thanks"), Spanish (alto, parar, cancelar, baja). On a
   platform DM the bar is lower (sales-py's SMS rule): cancel, end, quit, stop, unsubscribe, opt out, revoke, remove
   ANYWHERE in the message, "wrong person", "leave me alone", "no more messages". When in doubt, suppress.
2. ``out_of_office``, 3. ``interested``, 4. ``review`` — labels for Andre's review only.

The label is NOT what protects the creator (sales-py AEGIS S3-C1): ANY reply on ANY channel holds every further
automatic outreach to that influencer (email and DMs, both brands) until Andre decides, unless its raw body is exactly
one of ``AUTO_REPLIES`` on email. The label only adds, for opt-out wording, a permanent suppression.

The reply text is never stored: only its SHA-256 and the class. Never: answers the person."""

from __future__ import annotations

import re
import unicodedata

NUMBER = 7
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


def _fold_word(w: str) -> str:
    """Digits and @/$ inside a word that also has letters are read as letters (St0p -> stop; 310 stays 310)."""
    if any(c.isalpha() for c in w) and any(c in "013457@$" for c in w):
        return w.translate(_LEET)
    return w


def normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c)).casefold()
    t = t.translate(_CONFUSABLE).replace("_", " ")             # sweep A: "_" separates words (S_T_O_P, please_unsubscribe)
    t = " ".join(_fold_word(w) for w in t.split())
    t = re.sub(r"(?<=[^\W\d_])[^\w\s]+(?=[^\W\d_])", "", t)      # S.T.O.P -> stop, don't -> dont, opt-out -> optout
    t = re.sub(r"[^\w\s]+", " ", t)
    words, out, run = t.split(), [], []
    for w in words + [""]:                                     # S T O P -> stop (a run of 3+ single letters)
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        out += ["".join(run)] if len(run) >= 3 else run
        run = []
        if w:
            out.append(w)
    return " ".join(out)


def _collapse(t: str) -> str:
    """Repeated letters collapsed (service-py's channels._collapse): "stoooop" -> "stop"."""
    return re.sub(r"([^\W\d_])\1+", r"\1", t)


def classify(text: str, channel: str = "email") -> str:
    """``channel`` is ``email`` or a platform (instagram, tiktok, x, youtube): a DM gets the lower opt-out bar."""
    t = normalise(text)
    # sweep A: repeated letters collapsed too ("stoooop", "unsubbbscribe"); only for opt-out wording (over-suppressing
    # is the safe side), never for the other labels
    for v in (t, _collapse(t)):
        if v in _CARRIER or _UNSUB.search(v):
            return "unsubscribe"
        if channel != "email" and _UNSUB_PHONE.search(v):
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
