"""
Intelligence I2 — the approved-answer matcher (ADR 0014 decision 14). Deterministic, one job: pick at most ONE
Andre-approved knowledge-base article whose rules match a routine message. Never generates text: the answer sent is
the article's approved text, byte for byte.

An article is usable only when its CURRENT version is approved and the approval names the SHA-256 of exactly that
content (``content_sha256``, recomputed here at match time, so an edit — which makes a new, unapproved version — or a
tampered record is never used). Rules per article (all lower-case words or phrases, whole-word matched on the
normalised text, triage.normalise):

  exclude  none of these may appear
  phrases  any one present matches (score 1000 + its length in words)
  all      every one must appear                     \\  at least one of the two lists; score 10 per `all` term
  any      at least ``min_any`` of these must appear /   plus 1 per `any` hit

The highest score wins; a tie at the top is ambiguous and nothing is answered (a human is). No match: nothing.
"""

from __future__ import annotations

from typing import Optional

from ledger import payload_sha256
from triage import normalise

CONTENT_KEYS = ("brands", "channels", "title", "answer", "rules")


def content_of(article: dict) -> dict:
    return {k: article[k] for k in CONTENT_KEYS}


def content_sha(content: dict) -> str:
    """The same hash service.catalog_sha gives a "kb" item (the catalog name is part of what is hashed)."""
    return payload_sha256({"catalog": "kb", **{k: content[k] for k in CONTENT_KEYS}})


def usable(article: dict) -> bool:
    appr = article.get("approved")
    return bool(appr) and appr["version"] == article["version"] \
        and appr["content_sha256"] == article["content_sha256"] == content_sha(article)


def _present(norm: str, term: str) -> bool:
    t = normalise(term).strip()
    return bool(t) and f" {t} " in norm


def score(rules: dict, norm: str) -> int:
    if any(_present(norm, t) for t in rules.get("exclude", ())):
        return 0
    phrase_hits = [p for p in rules.get("phrases", ()) if _present(norm, p)]
    if phrase_hits:
        return 1000 + max(len(normalise(p).split()) for p in phrase_hits)
    all_terms, any_terms = rules.get("all", ()), rules.get("any", ())
    if not all_terms and not any_terms:
        return 0
    if not all(_present(norm, t) for t in all_terms):
        return 0
    any_hits = sum(1 for t in any_terms if _present(norm, t))
    if any_terms and any_hits < max(1, rules.get("min_any", 1)):
        return 0
    return 10 * len(all_terms) + any_hits


def match(articles: list[dict], text: str, brand: str, channel: str) -> tuple[Optional[dict], str]:
    """(article, reason). reason: matched | no_match | ambiguous."""
    norm = normalise(text)
    scored = []
    for a in articles:
        if not usable(a) or a["status"] != "active" or brand not in a["brands"] or channel not in a["channels"]:
            continue
        sc = score(a["rules"], norm)
        if sc > 0:
            scored.append((sc, a["article_id"], a))
    if not scored:
        return None, "no_match"
    scored.sort(key=lambda x: (-x[0], x[1]))
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None, "ambiguous"
    return scored[0][2], "matched"
