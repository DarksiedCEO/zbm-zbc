"""
Intelligence I2 — the approved-answer matcher (ADR 0014 decision 14, AEGIS round 3 V3-H1). Deterministic, one job:
answer only a message that IS one of the example questions Andre approved with an article. No interpretation:
two rounds of review showed that word-level rules (blocklists, then allow-lists) let meaning through.

The message and each approved question are compared after ``exact_form``, a tiny fixed normalisation and nothing
else: lower case; whitespace trimmed and collapsed; one leading greeting (hi, hello, hey, with an optional comma or
exclamation mark) removed; one trailing "thanks", "thank you" or "please" removed; trailing ? . ! removed. Equal ->
that article (if exactly one article has it); anything else -> a human.

An article is usable only when its CURRENT version is approved and the approval names the SHA-256 of exactly that
content, example questions included (``content_sha256``, recomputed at match time: an edit, which makes a new
unapproved version, or a tampered record is never used). The text sent is the article's answer, byte for byte.
"""

from __future__ import annotations

import re
from typing import Optional

from ledger import payload_sha256

CONTENT_KEYS = ("brands", "channels", "title", "answer", "questions")
_GREETING = re.compile(r"^(hi|hello|hey)\s*[,!]?\s+")
_THANKS = re.compile(r"[\s,]+(thanks|thank you|please)$")


def exact_form(text: str) -> str:
    t = " ".join(text.lower().split())
    t = t.rstrip("?.! ").strip()
    t = _GREETING.sub("", t, count=1)
    t = _THANKS.sub("", t, count=1)
    return t.rstrip("?.! ").strip()


def content_sha(content: dict) -> str:
    """The same hash service.catalog_sha gives a "kb" item (the catalog name is part of what is hashed)."""
    return payload_sha256({"catalog": "kb", **{k: content[k] for k in CONTENT_KEYS}})


def usable(article: dict) -> bool:
    appr = article.get("approved")
    return bool(appr) and appr["version"] == article["version"] \
        and appr["content_sha256"] == article["content_sha256"] == content_sha(article)


def match(articles: list[dict], text: str, brand: str, channel: str) -> tuple[Optional[dict], str]:
    """(article, reason). reason: matched | no_match | ambiguous."""
    form = exact_form(text)
    if not form:
        return None, "no_match"
    hits = [a for a in articles if usable(a) and a["status"] == "active" and brand in a["brands"]
            and channel in a["channels"] and form in {exact_form(q) for q in a["questions"]}]
    if not hits:
        return None, "no_match"
    if len(hits) > 1:
        return None, "ambiguous"
    return hits[0], "matched"
