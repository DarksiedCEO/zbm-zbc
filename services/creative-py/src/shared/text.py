"""
Deterministic text normalisation and phrase matching.

Submitted text (captions, bios, transcripts) is DATA. It is only ever
normalised and searched for configured phrases; it is never interpreted,
executed, or allowed to change which rules apply. Normalisation removes
the cheap evasions (case, Unicode compatibility forms, zero-width
characters, punctuation between letters of a phrase, repeated spaces).
"""

from __future__ import annotations

import re
import unicodedata

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_NON_WORD = re.compile(r"[^\w#@]+", re.UNICODE)


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").translate(_ZERO_WIDTH).casefold()
    t = _NON_WORD.sub(" ", t)
    return " ".join(t.split())


def contains_phrase(haystack: str, phrase: str) -> bool:
    """Whole-word(s) phrase match on normalised text."""
    h, p = normalize(haystack), normalize(phrase)
    if not p:
        return False
    return f" {p} " in f" {h} "


def word_count(text: str) -> int:
    return len(normalize(text).split())
