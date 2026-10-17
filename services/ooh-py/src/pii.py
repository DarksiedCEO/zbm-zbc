"""Personal data in free text (AEGIS wave 2 F-2 and OOH-3/4 security F3). This department keeps no personal data:
an e-mail address or a phone number in a free-text value is refused (attributes) or redacted (vendor text). The text
is NFKC-normalised and case-folded first, so full-width or look-alike characters, "[at]" spellings and any
separators between digits are caught. Names cannot be detected reliably; preparers must not type them."""

from __future__ import annotations

import re
import unicodedata

_EMAIL = re.compile(r"[^\s@]+\s*(?:@|\[at\]|\(at\)|\{at\}|\sat\s)\s*[a-z0-9-]+"
                    r"(?:(?:\.|\s*\[dot\]\s*|\s*\(dot\)\s*)[a-z]{2,})+")   # a plain dot takes no spaces
_PHONE = re.compile(r"(?:\d[^a-z\d]{0,3}){6,}\d")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def has_personal_data(text: str) -> bool:
    t = _norm(text)
    return bool(_EMAIL.search(t) or _PHONE.search(t))


def redact(text: str) -> str:
    """The text with every e-mail- or phone-like span replaced by "[redacted]" (spans found on the normalised text
    are removed from it; the result is the normalised text)."""
    t = _norm(text)
    t = _EMAIL.sub("[redacted]", t)
    return _PHONE.sub("[redacted]", t)
