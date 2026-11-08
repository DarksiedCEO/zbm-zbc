"""
Is a brand term named in an answer? (ADR 0046, review round 1 L1). One rule set for the probe framework's audit
summary (agents/probes.py) and Callum's citation runs (agents/callum.py).

Matching, per brand term of the prompt set:

  - whole words only: the term must not be preceded or followed by a letter, digit or underscore;
  - case-insensitive by default; ``case_sensitive: true`` matches the term exactly as written;
  - POSSESSIVES match deliberately: ``Acme's`` and ``Acme’s`` name Acme (an apostrophe is not a word character);
  - PLURALS and other inflections do NOT match deliberately: ``Acmes`` / ``apples`` are not ``Acme`` / ``Apple``.
    A tenant whose brand is usually written in another form lists that form as its own brand term. Not matching
    plurals keeps a common-word brand from matching the ordinary plural noun (``apples``);
  - ``negative_terms``: a match is discarded when a WHOLE-WORD occurrence of one of these words (found on the full
    text, never on a slice, so a word cut at a window edge never looks whole: ``crispie`` is not ``pie``) lies within
    ``WINDOW`` characters of the brand span on either side (``Apple`` next to ``pie`` / ``orchard``);
  - whitespace is normalised first, in the answer and in every term: any run of Unicode whitespace (NBSP, tabs,
    newlines, repeated spaces) is one space, so ``Z\u00a0Best  Media`` names ``Z Best Media``;
  - ``context_terms``: when given, a match counts only if at least one of them appears anywhere in the answer
    (``Apple`` counts only in an answer that also says ``iPhone`` or ``Mac``).

Context and negative terms are matched whole-word and case-insensitive.

Confidence (review round 3: confirmed by default). A brand term's mentions are CONFIRMED unless the term may be an
ordinary word and nothing in the answer confirms it. A term is UNCONFIRMED-PRONE when:

  (a) it is a single word on the bundled list of common English words (``common_words.txt``, self-authored, small:
      ``apple``, ``square``, ``notion``, ...), or the tenant declares ``common_word: true``; or
  (b) it is a single word written with a capital letter (``HubSpot``, ``Acme``) and every match in the answer is
      all lower case (``hubspot``).

``common_word: false`` or any disambiguation rule (``case_sensitive``, ``context_terms``, ``negative_terms``) makes
the term confirmed. A single word that is NOT on the list and matched as written (``Acme``, ``HubSpot``, ``Ünïcorn``)
is confirmed, as is every multi-word term. Even an unconfirmed-prone match is confirmed when the same answer names
the client's domain (``acme.com``), cites the client, or names another of the prompt set's brand terms. Otherwise
the mention is LOW confidence: reported apart (``LOW_CONFIDENCE`` / ``MENTION_UNCONFIRMED``) and never counted in a
headline rate.

``warnings()`` lists the terms that are on the list (or declared common) with no disambiguation rule: the prompt-set
API and the audit methodology show them, so the effect is never silent.

Cost: matching is linear in the answer; the negative-term check looks each brand match up in the sorted negative
positions with ``bisect`` (logarithmic), never by scanning every negative.

Pure functions; the answer text is data and is only searched.
"""

from __future__ import annotations

import bisect
import functools
import re
from pathlib import Path
from typing import Optional

WINDOW = 40
HIGH, LOW = "HIGH", "LOW"
_SINGLE_WORD = re.compile(r"[^\W\d_]+")
_WORDS_FILE = Path(__file__).with_name("common_words.txt")


@functools.lru_cache(maxsize=1)
def common_words() -> frozenset:
    with open(_WORDS_FILE, encoding="utf-8") as fh:
        return frozenset(w for w in (line.strip() for line in fh) if w and not w.startswith("#"))


@functools.lru_cache(maxsize=512)
def _pattern(term: str, case_sensitive: bool) -> "re.Pattern":
    return re.compile(r"(?<!\w)" + re.escape(_ws(term)) + r"(?!\w)", 0 if case_sensitive else re.IGNORECASE)


@functools.lru_cache(maxsize=256)
def _domain_pattern(domain: str) -> "re.Pattern":
    return re.compile(r"(?<![\w.-])(?:www\.)?" + re.escape(domain) + r"(?![\w-]|\.[\w-])", re.IGNORECASE)


def _ws(text: str) -> str:
    return " ".join((text or "").split())             # str.split() splits on every Unicode whitespace, NBSP included


def _any(text: str, words) -> bool:
    return any(_pattern(w, False).search(text) for w in words)


def normalise(terms: list, rules: Optional[list] = None) -> list:
    """One rule per brand term: the tenant's rule for it (``brand_term_rules``) or the defaults."""
    by_term = {r["term"]: r for r in (rules or ())}
    out = []
    for t in terms:
        r = by_term.get(t) or {}
        out.append({"term": t, "case_sensitive": bool(r.get("case_sensitive", False)),
                    "context_terms": list(r.get("context_terms") or ()),
                    "negative_terms": list(r.get("negative_terms") or ()),
                    "common_word": r.get("common_word")})
    return out


def single_word(term: str) -> bool:
    return _SINGLE_WORD.fullmatch(_ws(term)) is not None


def common_word(rule: dict) -> bool:
    """(a): the tenant's declaration when given, else a single word on the bundled list."""
    if rule.get("common_word") is not None:
        return bool(rule["common_word"])
    return single_word(rule["term"]) and _ws(rule["term"]).lower() in common_words()


def disambiguated(rule: dict) -> bool:
    return bool(rule.get("case_sensitive") or rule.get("context_terms") or rule.get("negative_terms"))


def warnings(terms: list, rules: Optional[list] = None) -> list:
    """Terms that may be common words and have no disambiguation rule (their mentions are unconfirmed by default)."""
    return [r["term"] for r in normalise(terms, rules)
            if r.get("common_word") is not False and common_word(r) and not disambiguated(r)]


def _matches(text: str, rule: dict, stats: Optional[dict] = None) -> list:
    """The brand matches (strings) that survive the context and negative rules."""
    if rule["context_terms"] and not _any(text, rule["context_terms"]):
        return []
    negs = sorted(n.span() for w in rule["negative_terms"] for n in _pattern(w, False).finditer(text))
    starts = [ns for ns, _ in negs]
    longest = max((ne - ns for ns, ne in negs), default=0)
    out = []
    for m in _pattern(rule["term"], rule["case_sensitive"]).finditer(text):
        # a negative counts when its whole-word span is within WINDOW characters of the brand span: only negatives
        # starting in [m.start - WINDOW - longest, m.end + WINDOW) can, found by bisect
        lo = bisect.bisect_left(starts, m.start() - WINDOW - longest)
        hi = bisect.bisect_left(starts, m.end() + WINDOW)
        near = False
        for j in range(lo, hi):
            if stats is not None:
                stats["negative_checks"] = stats.get("negative_checks", 0) + 1
            if negs[j][1] > m.start() - WINDOW:
                near = True
                break
        if not near:
            out.append(m.group(0))
    return out


def _prone(rule: dict, found: list) -> bool:
    """The term's matches in this answer are unconfirmed on their own: (a) or (b) above."""
    if rule.get("common_word") is False or disambiguated(rule):
        return False
    if common_word(rule):
        return True
    term = _ws(rule["term"])
    return single_word(term) and term != term.lower() and all(f == f.lower() for f in found)


def mention(text: str, terms: list, rules: Optional[list] = None, domain: Optional[str] = None,
            client_cited: bool = False, stats: Optional[dict] = None) -> dict:
    """``{"mentioned", "confidence" (HIGH / LOW / None), "terms"}``. LOW only when every term named is unconfirmed
    on its own and the answer neither names ``domain``, nor cites the client, nor names two of the terms."""
    t = _ws(text)
    named = []
    for r in normalise(terms, rules):
        found = _matches(t, r, stats)
        if found:
            named.append((r, found))
    if not named:
        return {"mentioned": False, "confidence": None, "terms": []}
    confirmed = any(not _prone(r, found) for r, found in named) or client_cited or len(named) > 1 \
        or bool(domain and _domain_pattern(domain.lower()).search(t))
    return {"mentioned": True, "confidence": HIGH if confirmed else LOW, "terms": [r["term"] for r, _ in named]}


def mentioned(text: str, terms: list, rules: Optional[list] = None) -> bool:
    """Any brand term named (either confidence)."""
    return mention(text, terms, rules)["mentioned"]
