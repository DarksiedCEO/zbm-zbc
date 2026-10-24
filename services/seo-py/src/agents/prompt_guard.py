"""
Prompts sent to an answer engine carry no personal data (ADR 0017 W4-3). A prompt is a generic question about a market
("best SEO agency in Long Beach"); it is refused when it holds:

  - an e-mail address: with an at-sign in any spelling the shared lookalike fold reaches (full-width, small), "%40",
    "&#64;" / "&#x40;" / "&commat;", "(at)" / "[at]" / "[@]" / "(@)", or spelled out with words where that is cheap
    to tell from prose ("john at example dot com": " at " followed by a " dot " part; "listed at yelp.com" is prose,
    and so is "listed at yelp dot com": a word that ordinary prose puts before " at " is not taken as a mailbox);
  - a run of seven or more digits whatever separates them: spaces, ( ) . + /, commas, underscores and every Unicode
    dash or connector punctuation (Pd, Pc: en/em dashes, the minus sign, the full-width low line, ...) — phone, SSN,
    card and account numbers;
  - an IPv4-shaped dotted quad (every part 0..255), whatever word comes before it ("version 192.168.0.1" and
    "build 10.0.0.1" are refused too: a version prefix never hides a dotted quad).

Allowed on purpose (a pragmatic rule, AEGIS M3, tightened in AEGIS round 2 M-r2-1): a 4-digit year range
("2025-2026", "2025–2026", "2025 2026", "2025/2026"); a ROUND count, i.e. 1-3 digits followed only by groups of "000"
with one separator ("1 000 000", "1,000,000", "1.000.000") and no other digit within three characters on either side
("call 1 234 567", "acct 1,234,567,890" and "tel 0612 345 678" are numbers); and a version string of two or three
parts ("version 2.10.300", "v1.2.3"). A four-part version is a dotted quad and is refused when it could be an
address; write it another way.

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
_EMAIL_WORDS = re.compile(r"\b(?P<local>[a-z0-9._%+-]+)\s+at\s+[a-z0-9-]+(?:\s+dot\s+[a-z0-9-]+)+\b", re.I)
# words that prose puts before " at <place>" ("listed at yelp dot com", "reviews at google dot com"): such a word is
# not taken as the mailbox of a spelled-out address. A real mailbox is a name or an id ("john", "j.doe", "sales1").
_PROSE_BEFORE_AT = frozenset("""
listed listing listings list reviews review reviewed rated rating ratings found find located location based available
open opened posted post published sold sell sells buy bought book booked hosted live lives work works worked job jobs
office offices store stores shop shops event events meet meeting held here there us them it is are was were be been
look looking see seen online page profile account accounts appear appears appearing ranked rank ranking ranks
""".split())
_DIGITS = re.compile(r"(?:\d[\s().+/,_-]{0,3}){7,}")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
# two or three parts only: a version prefix never hides a dotted quad (AEGIS M-r2-1)
_VERSION = re.compile(r"(?:\bv|\bver\.?\s*|\bversion\s*|\brelease\s*|\bbuild\s*)(?:\d{1,4}\.){1,2}\d{1,4}(?!\.?\d)",
                      re.I)
_YEARS = re.compile(r"(?<!\d)(?:19|20)\d\d\s*(?:-|/|\s)\s*(?:19|20)\d\d(?!\d)")
# a ROUND count: 1-3 digits then only "000" groups, one separator; the caller also requires no other digit within
# three characters on either side (AEGIS M-r2-1)
_COUNT = re.compile(r"\d{1,3}(?:(?P<s>[ ,.\u00a0\u202f])000)(?:(?P=s)000)*")
_AT = {"＠": "@", "﹫": "@", "%40": "@", "&#64;": "@", "&#x40;": "@", "&commat;": "@", "[@]": "@", "(@)": "@",
       "{@}": "@"}


def _ip_shaped(t: str) -> bool:
    return any(all(int(p) <= 255 for p in m.group(0).split(".")) for m in _IPV4.finditer(t))


def _round_counts(t: str) -> str:
    """``t`` with each round count replaced by " n " when no other digit is within three characters of it."""
    out, last = [], 0
    for m in _COUNT.finditer(t):
        a, b = m.span()
        if any(c.isdigit() for c in t[max(0, a - 3):a] + t[b:b + 3]):
            continue
        out.append(t[last:a] + " n ")
        last = b
    return "".join(out) + t[last:]


def _email_words(t: str) -> bool:
    return any(m.group("local").lower() not in _PROSE_BEFORE_AT for m in _EMAIL_WORDS.finditer(t))


def personal_data(text: str) -> Optional[str]:
    """The kind of personal data found (``email``, ``number``, ``ip``) or None."""
    t = unicodedata.normalize("NFKC", lookalikes.strip_invisible(text or ""))
    for k, v in _AT.items():
        t = t.replace(k, v)
    t = re.sub(r"\(\s*@\s*\)|\[\s*@\s*\]", "@", t)
    folded = _TABLE.fold_cased(t)
    if _EMAIL.search(t) or _EMAIL.search(folded) or _email_words(t):
        return "email"
    t = t.translate(_DASHES)
    if _ip_shaped(t):
        return "ip"
    t = _round_counts(_YEARS.sub(" y ", _VERSION.sub(" v ", t)))            # the allowed numbers, set aside
    if _DIGITS.search(t):
        return "number"
    return None
