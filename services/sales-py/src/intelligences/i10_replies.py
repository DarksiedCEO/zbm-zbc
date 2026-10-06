"""Inbound reply classification (ADR 0013 decision 13). Deterministic keyword rules, checked in this order:

0. The text is normalised first (AEGIS S1-H3): Unicode NFKC (fullwidth ``ＳＴＯＰ`` is ``stop``), accents removed,
   case folded, and punctuation BETWEEN letters removed (``S.T.O.P`` is ``stop``, ``opt-out`` is ``optout``); other
   punctuation becomes a space.
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

The reply text is never stored: only its SHA-256 and the class. Never: answers the person."""

from __future__ import annotations

import re
import unicodedata

NUMBER = 10
NAME = "reply_classifier"
DECIDES = "unsubscribe / out_of_office / interested / review"

_CARRIER = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "revoke", "optout", "opt out", "stop all"}
_UNSUB = re.compile(r"\b(stop|stopall|unsubscrib\w*|opt ?out|revoke|remove me|take me off|do not (contact|email|"
                    r"text|call)|dont (contact|email|text|call)|stop (texting|emailing|calling|contacting)|"
                    r"not interested|no thanks|no thank you|leave me alone|wrong person|no more (emails?|messages?)|"
                    r"alto|parar|cancelar|baja|no mas mensajes)\b")
_UNSUB_PHONE = re.compile(r"\b(cancel\w*|end|quit|stop\w*|unsubscrib\w*|opt ?out|revoke\w*|wrong (number|person)|"
                          r"lose my number|leave me alone|remove\w*|no more (texts?|messages?|calls?))\b")
_OOO = re.compile(r"\b(out of (the )?office|ooo|automatic reply|auto ?reply|on vacation|away until|on leave|"
                  r"currently away|limited access to email)\b")
_INTERESTED = re.compile(r"\b(interested|lets talk|book|schedule|set up a call|call me|sounds good|tell me more|"
                         r"send (me )?(the )?details|send (me )?more info|pricing|what does it cost|yes)\b")


def normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c)).casefold()
    t = re.sub(r"(?<=[^\W\d_])[^\w\s]+(?=[^\W\d_])", "", t)      # S.T.O.P -> stop, don't -> dont, opt-out -> optout
    t = re.sub(r"[^\w\s]+", " ", t)
    return " ".join(t.split())


def classify(text: str, channel: str = "email") -> str:
    t = normalise(text)
    if t in _CARRIER or _UNSUB.search(t):
        return "unsubscribe"
    if channel in ("sms", "voice") and _UNSUB_PHONE.search(t):
        return "unsubscribe"
    if _OOO.search(t):
        return "out_of_office"
    if _INTERESTED.search(t):
        return "interested"
    return "review"
