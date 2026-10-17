"""Personal data in free text (AEGIS wave 2 F-2, OOH-3/4 security F3, re-verification N1/N3). This department keeps
no personal data: an e-mail address or a phone number in a free-text value is refused (attributes, campaign names)
or redacted (vendor text).

Text is NFKC-normalised, zero-width characters and %40 are unfolded, then matched case-insensitively:
* e-mail: ``@`` / ``[at]`` / ``(at)`` / ``{at}`` (spaces allowed around it and around the dots after it), or the
  word `` at `` followed by a dotted (or `` dot ``-spelled) domain;
* phone: a North American number shape (optional +1, 3-3-4 digits with optional separators or parentheses) or ten
  or more digits in a row. Dates, hours, ZIP+4 and comma-grouped counts are not phone shapes, so OOH text that carries
  them passes.
Names, spelled-out digits and letters used as separators cannot be detected reliably: preparers must not type them."""

from __future__ import annotations

import re
import unicodedata

_ZW = re.compile("[​‌‍⁠﻿­]")
_DOM = r"[\w-]+"
_EMAIL_STRONG = re.compile(r"[^\s@]+\s*(?:@|\[at\]|\(at\)|\{at\})\s*" + _DOM +
                           r"(?:\s*(?:\.|\[dot\]|\(dot\)|\bdot\b)\s*[a-z]{2,})+", re.I)
_EMAIL_WORD = re.compile(r"\b[\w.+-]+\s+at\s+" + _DOM + r"(?:(?:\.|\s*\[dot\]\s*|\s*\(dot\)\s*|\s+dot\s+)[a-z]{2,})+",
                         re.I)
_SEP = "[\\s./_\u2010-\u2015-]*"
_PHONE = re.compile(r"(?<!\d)(?:\+?1" + _SEP + r")?\(?\d{3}\)?" + _SEP + r"\d{3}" + _SEP + r"\d{4}(?!\d)|(?<!\d)\d{10,}(?!\d)")


def _norm(text: str) -> str:
    """NFKC; zero-width characters removed; every other control or format character read as a space (it is a space
    once vendor text is stored, red team OOH-3/4 F4); %40 read as @."""
    t = unicodedata.normalize("NFKC", text)
    t = _ZW.sub("", t)
    t = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in t)
    return t.replace("%40", "@")


def _spans(t: str) -> list:
    return [m.span() for rx in (_EMAIL_STRONG, _EMAIL_WORD, _PHONE) for m in rx.finditer(t)]


def has_personal_data(text: str) -> bool:
    return bool(_spans(_norm(text)))


def redact(text: str) -> str:
    """The normalised text with every e-mail- or phone-like span replaced by "[redacted]" (case is kept)."""
    t = _norm(text)
    for rx in (_EMAIL_STRONG, _EMAIL_WORD, _PHONE):
        t = rx.sub("[redacted]", t)
    return t
