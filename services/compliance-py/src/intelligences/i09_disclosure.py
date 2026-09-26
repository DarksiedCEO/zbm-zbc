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
from typing import Optional

from textguard import normalize

NUMBER, NAME, ACTOR = 9, "Disclosure Check", "intel_09_disclosure"

# AEGIS N14-6: an accepted label counts only when it stands alone as a
# whitespace-delimited token or hashtag (surrounding punctuation stripped, so
# "(#ad)" and "Sponsored:" count, "ad-free" does not) and is not negated: a
# negation word within NEGATION_WINDOW tokens before it, or "free" right after
# it ("ad free"), makes it negated. If ANY accepted occurrence is negated the
# label fails ("Not an ad, just kidding #ad" is ambiguous: blocked).
NEGATION_WINDOW = 3
NEGATIONS = frozenset({
    "not", "no", "non", "never", "without", "nor", "neither", "none", "zero", "isn't", "isnt", "ain't", "aint",
    "don't", "dont", "doesn't", "doesnt", "wasn't", "wasnt", "aren't", "arent", "n't",
    # local-label languages (HR-06 local_labels: IT, DE, NL, ES)
    "senza", "nessun", "nessuna", "nicht", "kein", "keine", "keinerlei", "ohne", "geen", "niet", "zonder", "sin", "ningún",
    "ninguna",
})
AFTER_NEGATIONS = frozenset({"free", "frei", "libero", "libera", "vrij", "libre"})
_LEAD = re.compile(r"^[^\w#]+")
_TRAIL = re.compile(r"[^\w]+$")


def label_tokens(text: str) -> list[str]:
    """Normalized (NFKC + case-fold) whitespace tokens with edge punctuation stripped; inner punctuation kept."""
    t = normalize(text).replace("\u2019", "'").replace("\u2018", "'").replace("\u02bc", "'")
    out = []
    for raw in t.split(" "):
        tok = _TRAIL.sub("", _LEAD.sub("", raw))
        if tok:
            out.append(tok)
    return out


def _occurrences(toks: list[str], phrase: list[str]) -> list[int]:
    n = len(phrase)
    return [i for i in range(len(toks) - n + 1) if toks[i:i + n] == phrase] if n else []


def _negated(toks: list[str], start: int, length: int) -> bool:
    before = toks[max(0, start - NEGATION_WINDOW):start]
    after = toks[start + length:start + length + 1]
    return any(t in NEGATIONS for t in before) or any(t in AFTER_NEGATIONS for t in after)


def phrase_status(toks: list[str], phrase_text: str) -> Optional[bool]:
    """None: the phrase does not occur; True: every occurrence stands clean; False: some occurrence is negated."""
    phrase = label_tokens(phrase_text)
    occ = _occurrences(toks, phrase)
    if not occ:
        return None
    return not any(_negated(toks, i, len(phrase)) for i in occ)


def label_has_accepted_token(label: str, accepted: list[str]) -> bool:
    toks = label_tokens(label)
    found = [phrase_status(toks, a) for a in accepted if isinstance(a, str)]
    return any(f is True for f in found) and not any(f is False for f in found)


def label_problem(label: Optional[str], accepted: list[str], rejected: list[str]) -> Optional[str]:
    if not label or not label.strip():
        return "no in-video disclosure label"
    toks = label_tokens(label)
    found = [phrase_status(toks, a) for a in accepted if isinstance(a, str)]
    if any(f is False for f in found):
        return "disclosure label is negated or part of a compound (e.g. 'not sponsored', 'ad-free'): not a disclosure"
    if any(f is True for f in found):
        return None
    rej = {" ".join(label_tokens(r)) for r in rejected if isinstance(r, str)}
    if toks and set(toks) <= rej:
        return "label uses only a rejected word (e.g. 'collab', 'spon', 'sp', 'thanks')"
    return "label contains none of the accepted disclosure words (advertisement, ad, sponsored, #ad, #sponsored) as a standalone word"


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
    """A local label counts under the same rule as the accepted words (standalone, not negated)."""
    if not label or not labels:
        return False
    toks = label_tokens(label)
    found = [phrase_status(toks, lab) for lab in labels if isinstance(lab, str)]
    return any(f is True for f in found) and not any(f is False for f in found)
