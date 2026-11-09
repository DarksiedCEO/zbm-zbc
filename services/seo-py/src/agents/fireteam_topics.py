"""
The topic list of one conversation-fireteam run (ADR 0049). Shared plumbing, not an agent: it decides WHICH topics
Theo mines and Naomi sizes, so both members work on exactly the same keys and Zara can pair their outputs. It asks
nothing, measures nothing and estimates nothing.

Topics, in this order (deterministic for the same input):
  1. the prompt set's prompts, at most ``MAX_PROMPT_SET_TOPICS`` (prompt index order);
  2. Priya's EMERGING items of kind ``question`` or ``conversational`` (``priya-emerging-handoff/1``), at most
     ``MAX_PRIYA_TOPICS``, by item key; an item whose text equals a prompt-set topic (agents.priya_handoff.norm) is not
     a second topic: that topic records the item's key (``priya_key``) so Naomi can read its counts.

Every topic: ``topic_key`` (``tpc-`` + 16 hex of the SHA-256 of the normalised text), ``source`` (prompt_set /
priya), the prompt index or the Priya key, and the text IN MEMORY ONLY (``_text``: the service never records it).
A prompt-set prompt is already stored with its prompt set, so its text is reported (marked untrusted); a Priya
query's text is client query text, which ADR 0017 GSC-6 / ADR 0047 keep out of the log, so it is never reported
(``text: null``, ``text_withheld: PRIYA_QUERY_TEXT_NOT_RETAINED``). A topic whose text looks like personal data is
kept with ``refused`` set: no member sends or sizes it.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from agents import priya_handoff
from agents.prompt_guard import personal_data
from envelope import observed

MAX_PROMPT_SET_TOPICS = 10
MAX_PRIYA_TOPICS = 5
PRIYA_TOPIC_KINDS = ("question", "conversational")
WITHHELD_PRIYA = "PRIYA_QUERY_TEXT_NOT_RETAINED"


def topic_key(text: str) -> str:
    return "tpc-" + hashlib.sha256(priya_handoff.norm(text).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def plan(prompt_set: dict, handoff_items: list) -> list:
    out: list = []
    by_norm: dict = {}
    for pi, text in enumerate(prompt_set["prompts"][:MAX_PROMPT_SET_TOPICS]):
        n = priya_handoff.norm(text)
        if n in by_norm:                                   # the same prompt twice is one topic
            continue
        kind = personal_data(text)
        t = {"topic_key": topic_key(text), "source": "prompt_set", "prompt_index": pi, "priya_key": None,
             "text": None if kind else observed(text), "text_withheld": f"personal_data:{kind}" if kind else None,
             "refused": f"personal_data:{kind}" if kind else None, "emerging_kinds": None, "_text": text}
        by_norm[n] = t
        out.append(t)
    taken = 0
    for it in sorted(handoff_items or [], key=lambda i: i["key"]):
        text = it["query"]["text"]
        n = priya_handoff.norm(text)
        if n in by_norm:
            if by_norm[n]["priya_key"] is None:
                by_norm[n]["priya_key"] = it["key"]
            continue
        if taken >= MAX_PRIYA_TOPICS or not set(it["emerging_kinds"]) & set(PRIYA_TOPIC_KINDS):
            continue
        kind = personal_data(text)
        t = {"topic_key": topic_key(text), "source": "priya", "prompt_index": None, "priya_key": it["key"],
             "text": None, "text_withheld": WITHHELD_PRIYA, "refused": f"personal_data:{kind}" if kind else None,
             "emerging_kinds": list(it["emerging_kinds"]), "_text": text}
        by_norm[n] = t
        out.append(t)
        taken += 1
    return out


def public(t: dict) -> dict:
    """A topic as reported (never the in-memory ``_text``)."""
    return {k: v for k, v in t.items() if not k.startswith("_")}


def item_for(t: dict, handoff_items: list) -> Optional[dict]:
    if t.get("priya_key") is None:
        return None
    return next((i for i in handoff_items or [] if i["key"] == t["priya_key"]), None)
