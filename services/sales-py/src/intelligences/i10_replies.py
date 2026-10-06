"""Inbound reply classification (ADR 0013 decision 13). Deterministic keyword rules, checked in this order:

1. ``unsubscribe`` — STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT, REVOKE, OPT OUT / OPTOUT, "remove me",
   "take me off", "do not contact", "don't contact", "stop texting/emailing/calling", and declines ("not
   interested", "no thanks", "no thank you"): suppressed at once, everywhere — every address and number tied to
   the sender, whatever channel the reply came on (a decline is honoured as an opt-out, the conservative reading). For a text, a message that is ONLY one of the carrier keywords counts too.
2. ``out_of_office`` — auto-replies ("out of office", "OOO", "automatic reply", "auto-reply", "on vacation",
   "away until", "on leave", "currently away"): a reschedule task.
3. ``interested`` — "interested", "let's talk", "lets talk", "book", "schedule", "set up a call", "call me",
   "sounds good", "tell me more", "send details", "send more info", "pricing", "what does it cost", "yes": a task to
   book a call.
4. anything else — the human review queue.

The reply text is never stored: only its SHA-256 and the class. Never: answers the person."""

from __future__ import annotations

import re

NUMBER = 10
NAME = "reply_classifier"
DECIDES = "unsubscribe / out_of_office / interested / review"

_CARRIER = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "revoke", "optout", "opt out", "stop all"}
_UNSUB = re.compile(r"\b(stop|stopall|unsubscribe|opt[- ]?out|revoke|remove me|take me off|do not (contact|email|"
                    r"text|call)|don'?t (contact|email|text|call)|stop (texting|emailing|calling|contacting)|"
                    r"not interested|no thanks|no thank you)\b", re.I)
_OOO = re.compile(r"\b(out of (the )?office|ooo|automatic reply|auto[- ]?reply|on vacation|away until|on leave|"
                  r"currently away|limited access to email)\b", re.I)
_INTERESTED = re.compile(r"\b(interested|let'?s talk|book|schedule|set up a call|call me|sounds good|tell me more|"
                         r"send (me )?(the )?details|send (me )?more info|pricing|what does it cost|yes)\b", re.I)


def classify(text: str) -> str:
    t = " ".join(text.split())
    if t.strip(" .!").lower() in _CARRIER or _UNSUB.search(t):
        return "unsubscribe"
    if _OOO.search(t):
        return "out_of_office"
    if _INTERESTED.search(t):
        return "interested"
    return "review"
