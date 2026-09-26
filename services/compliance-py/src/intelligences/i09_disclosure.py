"""
Intelligence 9 — Disclosure Check (spec C.7).

Pure functions over the ``disclosure`` facts of a clip or paid asset:
platform toggle evidenced, in-video label in the accepted vocabulary
(US-FTC-D101-02, after NFKC + case-fold, whole tokens), label early enough
(<= COMPLIANCE_DISCLOSURE_MAX_OFFSET_S), audio disclosure when there is
voice, the local label for each audience jurisdiction that has one (HR-06
``local_labels``). Never judges creative quality (Clip Review does).
"""

from __future__ import annotations

from typing import Optional

from textguard import normalize, tokens

NUMBER, NAME, ACTOR = 9, "Disclosure Check", "intel_09_disclosure"


def label_has_accepted_token(label: str, accepted: list[str]) -> bool:
    toks = set(tokens(label))
    acc = {normalize(a) for a in accepted if isinstance(a, str)}
    return bool(toks & acc)


def label_problem(label: Optional[str], accepted: list[str], rejected: list[str]) -> Optional[str]:
    if not label or not label.strip():
        return "no in-video disclosure label"
    if label_has_accepted_token(label, accepted):
        return None
    toks = set(tokens(label))
    rej = {normalize(r) for r in rejected if isinstance(r, str)}
    if toks and toks <= rej:
        return "label uses only a rejected word (e.g. 'collab', 'spon', 'sp', 'thanks')"
    return "label contains none of the accepted disclosure words (advertisement, ad, sponsored, #ad, #sponsored)"


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
    if not label or not labels:
        return False
    text = normalize(label)
    toks = set(tokens(label))
    for lab in labels:
        n = normalize(lab)
        if n in toks or (" " in n and n in text):
            return True
    return False
