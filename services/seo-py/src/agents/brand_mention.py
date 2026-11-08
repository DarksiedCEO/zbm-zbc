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

Confidence. A term that is a single alphabetic word may be an ordinary word (``Apple``, ``Notion``, ``Square``). No
dictionary is bundled, so the rule is mechanical and fails closed: a single-word alphabetic term is treated as
POSSIBLY A COMMON WORD unless the tenant says otherwise (``common_word: false``). A common-word term with no
disambiguation configured (none of ``case_sensitive``, ``context_terms``, ``negative_terms``) yields LOW confidence:
its mentions are reported apart (``LOW_CONFIDENCE``) and never counted in a headline rate. A multi-word term
(``Z Best Media``) or one with a digit or punctuation is not treated as a common word. ``common_word: true`` marks
any term as one.

Pure functions; the answer text is data and is only searched.
"""

from __future__ import annotations

import functools
import re
from typing import Optional

WINDOW = 40
HIGH, LOW = "HIGH", "LOW"
_SINGLE_WORD = re.compile(r"[^\W\d_]+")


@functools.lru_cache(maxsize=512)
def _pattern(term: str, case_sensitive: bool) -> "re.Pattern":
    return re.compile(r"(?<!\w)" + re.escape(_ws(term)) + r"(?!\w)", 0 if case_sensitive else re.IGNORECASE)


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


def common_word(rule: dict) -> bool:
    """The tenant's declaration when given, else: a single alphabetic word (fail closed: no dictionary)."""
    if rule.get("common_word") is not None:
        return bool(rule["common_word"])
    return _SINGLE_WORD.fullmatch(rule["term"]) is not None


def disambiguated(rule: dict) -> bool:
    return bool(rule.get("case_sensitive") or rule.get("context_terms") or rule.get("negative_terms"))


def _term_named(text: str, rule: dict) -> bool:
    if rule["context_terms"] and not _any(text, rule["context_terms"]):
        return False
    negatives = [n.span() for w in rule["negative_terms"] for n in _pattern(w, False).finditer(text)]
    for m in _pattern(rule["term"], rule["case_sensitive"]).finditer(text):
        # a negative counts when its whole-word span is within WINDOW characters of the brand span
        if any(ne > m.start() - WINDOW and ns < m.end() + WINDOW for ns, ne in negatives):
            continue
        return True
    return False


def mention(text: str, terms: list, rules: Optional[list] = None) -> dict:
    """``{"mentioned", "confidence" (HIGH / LOW / None), "terms"}``: LOW only when every term named is a common word
    without disambiguation."""
    t = _ws(text)
    named = [r for r in normalise(terms, rules) if _term_named(t, r)]
    if not named:
        return {"mentioned": False, "confidence": None, "terms": []}
    high = any(not common_word(r) or disambiguated(r) for r in named)
    return {"mentioned": True, "confidence": HIGH if high else LOW, "terms": [r["term"] for r in named]}


def mentioned(text: str, terms: list, rules: Optional[list] = None) -> bool:
    """Any brand term named (either confidence): the audit summary's rule (agents/probes.py)."""
    return mention(text, terms, rules)["mentioned"]
