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

Confidence (review rounds 3-4: confirmed by default, as at a4af277). Every term that is NOT a common word —
multi-word terms and single words not on the list (``Acme``, ``HubSpot``, ``eBay``, ``iPhone``, ``Ünïcorn``) — is
matched case-insensitively, whole word, and every match is CONFIRMED, whatever its case (``try acme``, ``the iphone
15``, ``buy it on ebay``). A match inside a URL or host written in the answer text (``acme.io/pricing``,
``github.com/acme``) is a MENTION as at a4af277: it is text, never a citation of the client (citations come only
from the engine's structured fields).

A COMMON-WORD term — a single word on the bundled list of common English words (``common_words.txt``,
self-authored: ``apple``, ``zoom``, ``notion``, ...) or one the tenant declares ``common_word: true`` — with no
disambiguation rule (``case_sensitive``, ``context_terms``, ``negative_terms``; ``common_word: false`` opts out) has
UNCONFIRMED matches unless one of these confirms them:

  - a match capitalised as the term is configured (``Apple``, ``Zoom``) that does NOT start a sentence. A capital
    is grammar, not a name (review rounds 4-5), when the word starts the text or a line, a markdown list item
    (``-``, ``*``, ``+``, ``•``, ``1.``, ``1)``, ``a)``), a heading (``#``), a blockquote (``>``), or follows ``.``,
    ``!``, ``?``, ``:`` or ``;`` (optionally through closing quotes / brackets), or directly follows an opening quote
    (``He said "Apple is good."`` is a quoted sentence: not confirmation). Line structure is read on the answer with
    its line breaks kept (whitespace runs are normalised, a run holding a line break stays one line break). So
    ``Zoom in on the map``, ``- Target the right audience``, ``## Zoom in``, ``1) Target buyers`` and ``Steps: Zoom
    in`` alone are not confirmation; a lower-case match (``apple pie``) never confirms;
  - the same answer names the client's domain (``acme.com``) or cites the client;
  - the same answer names another brand term of the set that is NOT itself a common word (two common words never
    confirm each other: ``the notion of apple pie`` with terms Apple and Notion stays unconfirmed).

An unconfirmed mention is LOW confidence: reported apart (``LOW_CONFIDENCE`` / ``MENTION_UNCONFIRMED``) and never
counted in a headline rate.

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
    # words of a term may be separated by a space or a line break in the answer (the text keeps its line breaks)
    body = r"[ \n]".join(re.escape(w) for w in _ws(term).split(" "))
    return re.compile(r"(?<!\w)" + body + r"(?!\w)", 0 if case_sensitive else re.IGNORECASE)


@functools.lru_cache(maxsize=256)
def _domain_pattern(domain: str) -> "re.Pattern":
    return re.compile(r"(?<![\w.-])(?:www\.)?" + re.escape(domain) + r"(?![\w-]|\.[\w-])", re.IGNORECASE)


def _ws(text: str) -> str:
    return " ".join((text or "").split())             # str.split() splits on every Unicode whitespace, NBSP included


_WS_RUN = re.compile(r"\s+")
_LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")


def _ws_lines(text: str) -> str:
    """Whitespace normalised but LINE STRUCTURE kept (round 5): a run of whitespace holding a line break becomes one
    ``\n``, any other run one space; so list items, headings and quotes can still be recognised."""
    return _WS_RUN.sub(lambda m: "\n" if any(c in _LINE_BREAKS for c in m.group(0)) else " ", text or "").strip()


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
    """The brand matches ``(text, start)`` that survive the context and negative rules."""
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
            out.append((m.group(0), m.start()))
    return out


# round 5: what may come before a word that starts a sentence. On its line: nothing, or markdown structure — bullets
# (- * + •), numbered items (1. / 1) / a)), headings (#), blockquotes (>) — optionally then opening quotes/brackets.
_LINE_START = re.compile(r"^[ \t]*(?:(?:[-*+\u2022\u2023\u25e6]|\d{1,3}[.)]|[A-Za-z][.)]|#{1,6}|>+)[ \t]*)*"
                         r"['\"\u2018\u201c(\[*_]*$")
# after sentence punctuation or a colon (optionally closing quotes / brackets), then optional opening quotes; or
# directly after an opening quote ('He said "Apple is good."': a quoted sentence starts there)
_AFTER_PUNCT = re.compile(r"(?:[.!?:;]['\"\u2019\u201d)\]*_]*[ \t]+['\"\u2018\u201c(\[*_]*"
                          r"|(?:^|[ \t(\[])['\"\u2018\u201c])$")
_LOOKBACK = 32


def _sentence_initial(text: str, start: int) -> bool:
    """The match at ``start`` begins a sentence, a line, a list item, a heading, a quote or follows a colon. Bounded
    look-back (``_LOOKBACK`` characters), so the check is constant work per match."""
    lo = max(0, start - _LOOKBACK)
    nl = text.rfind("\n", lo, start)
    if nl != -1 or lo == 0:
        line = text[nl + 1 if nl != -1 else 0:start]
        if _LINE_START.search(line):
            return True
    return _AFTER_PUNCT.search(text[lo:start]) is not None


def _common(rule: dict) -> bool:
    """A common-word term without disambiguation: its matches need confirmation."""
    return rule.get("common_word") is not False and not disambiguated(rule) and common_word(rule)


def _prone(rule: dict, found: list, text: str) -> bool:
    """The term's matches in this answer are unconfirmed on their own (see the module docstring)."""
    if not _common(rule):
        return False                                  # not a common word: confirmed whatever the case (a4af277)
    term = _ws(rule["term"])
    if term == term.lower():
        return True                                   # configured lower case: capitalisation tells nothing
    return not any(f != f.lower() and not _sentence_initial(text, at) for f, at in found)


def mention(text: str, terms: list, rules: Optional[list] = None, domain: Optional[str] = None,
            client_cited: bool = False, stats: Optional[dict] = None) -> dict:
    """``{"mentioned", "confidence" (HIGH / LOW / None), "terms"}``. LOW only when every term named is an
    unconfirmed common word and the answer neither names ``domain`` nor cites the client."""
    t = _ws_lines(text)
    named = []
    for r in normalise(terms, rules):
        found = _matches(t, r, stats)
        if found:
            named.append((r, found))
    if not named:
        return {"mentioned": False, "confidence": None, "terms": []}
    # another brand term confirms only when it is not itself a common word, which is already "not prone" here
    confirmed = any(not _prone(r, found, t) for r, found in named) or client_cited \
        or bool(domain and _domain_pattern(domain.lower()).search(t))
    return {"mentioned": True, "confidence": HIGH if confirmed else LOW, "terms": [r["term"] for r, _ in named]}


def mentioned(text: str, terms: list, rules: Optional[list] = None) -> bool:
    """Any brand term named (either confidence)."""
    return mention(text, terms, rules)["mentioned"]
