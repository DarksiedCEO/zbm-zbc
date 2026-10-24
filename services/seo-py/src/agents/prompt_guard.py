"""
Prompts sent to an answer engine carry no personal data (ADR 0017 W4-3). A prompt is a generic question about a market
("best SEO agency in Long Beach"); it is refused when it holds an e-mail address (with an at-sign, a full-width or small
at-sign, "%40", "(at)" or "[at]", also after the shared lookalike fold), a run of seven or more digits whatever
separates them (phone, SSN, card, account numbers), or an IPv4 address. Checked when a prompt set is created and again before each prompt is sent.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

import lookalikes

_TABLE = lookalikes.Table()
# "@" in any spelling the shared fold reaches, "(at)" / "[at]"; a bare " at " is ordinary prose ("listed at yelp.com")
_EMAIL = re.compile(r"[a-z0-9._%+-]+\s*(?:@|\(at\)|\[at\])\s*[a-z0-9-]+(?:\s*(?:\.|\(dot\)|\[dot\])\s*[a-z0-9-]+)+",
                    re.I)
_DIGITS = re.compile(r"(?:\d[\s().+/-]{0,3}){7,}")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_AT = {"＠": "@", "﹫": "@", "%40": "@"}


def personal_data(text: str) -> Optional[str]:
    """The kind of personal data found (``email``, ``number``, ``ip``) or None."""
    t = unicodedata.normalize("NFKC", lookalikes.strip_invisible(text or ""))
    for k, v in _AT.items():
        t = t.replace(k, v)
    folded = _TABLE.fold_cased(t)
    if _EMAIL.search(t) or _EMAIL.search(folded):
        return "email"
    if _IPV4.search(t):
        return "ip"
    if _DIGITS.search(t):
        return "number"
    return None
