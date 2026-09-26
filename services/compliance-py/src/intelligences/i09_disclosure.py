"""
Intelligence 9 — Disclosure Check (spec C.7).

Pure functions over the ``disclosure`` facts of a clip or paid asset:
platform toggle evidenced, in-video label in the accepted vocabulary
(US-FTC-D101-02, after NFKC + case-fold, standalone and not negated), label early enough
(<= COMPLIANCE_DISCLOSURE_MAX_OFFSET_S), audio disclosure when there is
voice, the local label for each audience jurisdiction that has one (HR-06
``local_labels``). Never judges creative quality (Clip Review does).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

NUMBER, NAME, ACTOR = 9, "Disclosure Check", "intel_09_disclosure"

# AEGIS N14-6 / N15-4: the sentence-level rule. The label text is NFKC-normalised and case-folded, then
# split into clauses on . ! ? ; newline (and U+2028/U+2029) and on parentheses. An occurrence of an accepted
# label (a standalone token; its hashtag form "#ad", "#ADV", "#publicidad" counts the same, case-insensitively)
# counts only when:
#   - it is not inside parentheses,
#   - its clause is not a question (the clause does not end with "?"),
#   - no negation token precedes it in its clause, and none follows it in its comma/dash segment
#     ("Sponsored, not affiliated with YouTube" is a disclosure; "not in any way sponsored" is not),
#   - it is not followed by "free" ("ad free").
# The whole text fails (fail closed, ambiguous) when it contains a retraction ("just kidding", "jk", "lol",
# "not really"), a segment made only of negations ("#ad, not."), or ANY negated or questioned occurrence of
# a label (e.g. "Not an ad, #ad"). The reason is returned in words.
NEGATIONS = frozenset({
    "not", "no", "n0t", "never", "without", "nor", "neither", "none", "zero", "isn't", "isnt", "ain't", "aint",
    "don't", "dont", "doesn't", "doesnt", "wasn't", "wasnt", "aren't", "arent", "n't", "nope", "nah",
    # local-label languages (HR-06 local_labels: IT, DE, NL, ES)
    "non", "senza", "nessun", "nessuna", "nicht", "kein", "keine", "keinerlei", "ohne", "nein", "geen", "niet",
    "zonder", "nee", "sin", "ningún", "ningun", "ninguna",
})
RETRACTIONS = (("just", "kidding"), ("jk",), ("j/k",), ("lol",), ("lmao",), ("not", "really"), ("kidding",))
AFTER_NEGATIONS = frozenset({"free", "frei", "libero", "libera", "vrij", "libre"})
_CLAUSE = re.compile(r"([.!?;\n\u2028\u2029()])")
_SEGMENT = re.compile(r"[,\u2014\u2013]|\s-\s")
_LEAD = re.compile(r"^[^\w#]+")
_TRAIL = re.compile(r"[^\w]+$")


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).casefold()
    return t.replace("\u2019", "'").replace("\u2018", "'").replace("\u02bc", "'")


def _tokens(text: str) -> list[str]:
    out = []
    for raw in text.split():
        tok = _TRAIL.sub("", _LEAD.sub("", raw))
        if tok:
            out.append(tok)
    return out


def label_tokens(text: str) -> list[str]:
    """Normalized (NFKC + case-fold) whitespace tokens with edge punctuation stripped; inner punctuation kept."""
    return _tokens(_norm(text))


def _bare(tok: str) -> str:
    return tok[1:] if tok.startswith("#") and len(tok) > 1 else tok


def _clauses(text: str) -> list[tuple[list[list[str]], bool, bool]]:
    """[(comma/dash segments of tokens, in_parentheses, is_question)] in order."""
    parts = _CLAUSE.split(_norm(text))
    out, depth = [], 0
    for i in range(0, len(parts), 2):
        body = parts[i]
        term = parts[i + 1] if i + 1 < len(parts) else ""
        segs = [_tokens(seg) for seg in _SEGMENT.split(body)]
        if any(segs):
            out.append((segs, depth > 0, term == "?"))
        if term == "(":
            depth += 1
        elif term == ")":
            depth = max(0, depth - 1)
    return out


def _find(flat: list[str], phrase: list[str]) -> list[int]:
    n = len(phrase)
    bare = [_bare(t) for t in phrase]
    return [i for i in range(len(flat) - n + 1) if [_bare(t) for t in flat[i:i + n]] == bare] if n else []


def assess_label(label: Optional[str], labels: list[str]) -> tuple[bool, Optional[str]]:
    """(counts, why-not). ``counts`` is True only when at least one occurrence of one of ``labels`` counts
    and nothing in the text makes it ambiguous."""
    if not label or not label.strip():
        return False, "no in-video disclosure label"
    toks = label_tokens(label)
    for r in RETRACTIONS:
        if any(toks[i:i + len(r)] == list(r) for i in range(len(toks) - len(r) + 1)):
            return False, f"label contains a retraction ('{' '.join(r)}'): ambiguous, not a disclosure"
    phrases = [label_tokens(x) for x in labels if isinstance(x, str) and label_tokens(x)]
    clean = 0
    problems: list[str] = []
    for segs, in_paren, question in _clauses(label):
        if any(seg and all(t in NEGATIONS for t in seg) for seg in segs):
            problems.append("label contains a bare negation ('no', 'not', 'nein'...)")
        flat, seg_of = [], []
        for si, seg in enumerate(segs):
            flat += seg
            seg_of += [si] * len(seg)
        for ph in phrases:
            for i in _find(flat, ph):
                j = i + len(ph)
                before = flat[:i]
                after_same_seg = [t for k, t in enumerate(flat[j:], start=j) if seg_of[k] == seg_of[j - 1]]
                if any(t in NEGATIONS for t in before) or any(t in NEGATIONS for t in after_same_seg) \
                        or (after_same_seg[:1] and after_same_seg[0] in AFTER_NEGATIONS):
                    problems.append("label is negated or part of a compound (e.g. 'not sponsored', 'ad free')")
                elif question:
                    problems.append("label is in a question (e.g. 'Sponsored?', 'Is this an ad?')")
                elif in_paren:
                    continue  # a parenthesised label does not count (and does not poison a real one)
                else:
                    clean += 1
    if problems:
        return False, problems[0] + ": ambiguous, not a disclosure"
    if clean:
        return True, None
    return False, None


def label_has_accepted_token(label: str, accepted: list[str]) -> bool:
    return assess_label(label, accepted)[0]


def label_problem(label: Optional[str], accepted: list[str], rejected: list[str]) -> Optional[str]:
    ok, why = assess_label(label, accepted)
    if ok:
        return None
    if why:
        return why
    toks = label_tokens(label or "")
    rej = {" ".join(label_tokens(r)) for r in rejected if isinstance(r, str)}
    if toks and set(toks) <= rej:
        return "label uses only a rejected word (e.g. 'collab', 'spon', 'sp', 'thanks')"
    return ("label contains none of the accepted disclosure words (advertisement, ad, sponsored, #ad, #sponsored) "
            "as a standalone word outside parentheses")


def timing_problem(disclosure: dict, max_offset_s: float) -> Optional[str]:
    start = disclosure.get("in_video_label_start_s")
    if start is None:
        return None  # reported as fact_missing by the gate
    if start > max_offset_s:
        return f"label starts at {start:g}s; must start within the first {max_offset_s:g}s"
    return None


def audio_problem(disclosure: dict) -> Optional[str]:
    if disclosure.get("voice_present") is True and disclosure.get("audio_disclosure_present") is not True:
        return "voice present but no audio disclosure"
    return None


def local_label_present(label: Optional[str], labels: list[str]) -> bool:
    """A local label counts under the same sentence-level rule as the accepted words (AEGIS N15-4)."""
    if not label or not labels:
        return False
    return assess_label(label, labels)[0]
