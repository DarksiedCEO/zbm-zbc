"""
Prompts sent to an answer engine carry no personal data (ADR 0017 W4-3). A prompt is a generic question about a market
("best SEO agency in Long Beach"); it is refused when it holds:

  - an e-mail address: with an at-sign in any spelling the shared lookalike fold reaches (full-width, small), "%40",
    "&#64;" / "&#x40;" / "&commat;", "(at)" / "[at]" / "[@]" / "(@)", or spelled out with words where that is cheap
    to tell from prose ("john at example dot com": " at " followed by a " dot " part; "listed at yelp.com" is prose);
  - a run of seven or more digits whatever separates them: spaces, ( ) . + /, commas, underscores and every Unicode
    dash or connector punctuation (Pd, Pc: en/em dashes, the minus sign, the full-width low line, ...) — phone, SSN,
    card and account numbers;
  - an IPv4 address.

Allowed on purpose (a pragmatic rule, AEGIS M3): a 4-digit year range ("2025-2026", "2025–2026", "2025 2026",
"2025/2026"), a count grouped in thousands with one separator ("1 000 000", "1,000,000", "1.000.000"), and a
version string ("version 1.2.3.4", "v1.2.3.4", "release 10.0.0.1"); a real IP address ("ip 192.168.0.1") is still
refused.

NOT detected (a stated limit, not a guarantee): personal names ("Jane Smith") and street addresses ("12 Elm Street"),
which no pattern can tell from a business or a place; numbers written as words; separators wider than three
characters. Prompt sets are written by staff as generic market questions; this guard catches the identifiers that
slip in, it does not make arbitrary text safe.

Checked when a prompt set is created and again before each prompt is sent.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from typing import Optional

import lookalikes

_TABLE = lookalikes.Table()
# every Unicode dash (Pd) and connector punctuation (Pc, e.g. "_" and its full-width form), as "-"
_DASHES = {cp: "-" for cp in range(sys.maxunicode + 1) if unicodedata.category(chr(cp)) in ("Pd", "Pc")}
_DASHES[ord("−")] = "-"                                     # the minus sign (Sm) reads as a dash in numbers
# "@" in any spelling the shared fold reaches, "(at)" / "[at]"; a bare " at " is ordinary prose ("listed at yelp.com")
_EMAIL = re.compile(r"[a-z0-9._%+-]+\s*(?:@|\(at\)|\[at\])\s*[a-z0-9-]+(?:\s*(?:\.|\(dot\)|\[dot\])\s*[a-z0-9-]+)+",
                    re.I)
# spelled out: "john at example dot com" (needs at least one " dot " part, so prose like "listed at yelp.com" passes)
_EMAIL_WORDS = re.compile(r"\b[a-z0-9._%+-]+\s+at\s+[a-z0-9-]+(?:\s+dot\s+[a-z0-9-]+)+\b", re.I)
_DIGITS = re.compile(r"(?:\d[\s().+/,_-]{0,3}){7,}")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_VERSION = re.compile(r"(?:\bv|\bver\.?\s*|\bversion\s*|\brelease\s*|\bbuild\s*)(?:\d{1,3}\.){3}\d{1,3}(?![\d.])",
                      re.I)
_YEARS = re.compile(r"(?<!\d)(?:19|20)\d\d\s*(?:-|/|\s)\s*(?:19|20)\d\d(?!\d)")
_COUNT = re.compile(r"(?<![\d,.])\d{1,3}(?:(?P<s>[ ,.\u00a0\u202f])\d{3})(?:(?P=s)\d{3})*(?![\s,.\u00a0\u202f_-]?\d)")
_AT = {"＠": "@", "﹫": "@", "%40": "@", "&#64;": "@", "&#x40;": "@", "&commat;": "@", "[@]": "@", "(@)": "@",
       "{@}": "@"}


def personal_data(text: str) -> Optional[str]:
    """The kind of personal data found (``email``, ``number``, ``ip``) or None."""
    t = unicodedata.normalize("NFKC", lookalikes.strip_invisible(text or ""))
    for k, v in _AT.items():
        t = t.replace(k, v)
    t = re.sub(r"\(\s*@\s*\)|\[\s*@\s*\]", "@", t)
    folded = _TABLE.fold_cased(t)
    if _EMAIL.search(t) or _EMAIL.search(folded) or _EMAIL_WORDS.search(t):
        return "email"
    t = t.translate(_DASHES)
    if _IPV4.search(_VERSION.sub(" v ", t)):
        return "ip"
    t = _COUNT.sub(" n ", _YEARS.sub(" y ", _VERSION.sub(" v ", t)))       # the allowed numbers, set aside
    if _DIGITS.search(t):
        return "number"
    return None
