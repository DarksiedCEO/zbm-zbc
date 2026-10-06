"""FTC Endorsement Guides (16 CFR Part 255) disclosure guard (ADR 0015 decision 14). Fail closed: content that does
not carry the brief's disclosure, clearly and up front, is refused before Andre ever sees it.

Decides:
- the closed list of disclosures a brief may require: ``#ad`` and ``#sponsored`` for either brand, or the brand's own
  ``Paid partnership with Z Best Media`` / ``Sponsored by Z Best Media`` (``... Z Best Clips`` for ZBC); a phrase for
  the other brand is refused;
- the brief as issued: the approved brief text plus a fixed FTC section this module appends (the material connection,
  the exact disclosure, where it goes, the platform label, honest-opinion and actual-use rules) — a brief cannot leave
  it out, and Andre's approval binds the hash of the brief WITH that section;
- whether a caption carries the disclosure: the exact disclosure text (ASCII, case-insensitive, with nothing but a
  space, punctuation or the caption's edge touching it, so ``#adventure`` is not ``#ad``) must END within the first
  ``DISCLOSURE_WINDOW`` (100) characters and come BEFORE any other hashtag (not buried in a hashtag block);
- hidden characters: a caption, brief or DM holding a bidirectional control, a zero-width space or joiner, a word joiner,
  a byte-order mark, a tag or private-use character, or any other invisible format character is refused (a zero-width
  joiner is allowed only between two emoji, as in family emoji) — so a look-alike or split disclosure can never pass;
- the platform's paid-partnership label: on Instagram, TikTok and YouTube the submission must state the platform's
  label is ON (X has no such label).
Never: decides the content is honest or approves it — Andre approves the final content by its hash."""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

NUMBER = 8
NAME = "ftc_disclosure"
DECIDES = "required disclosure, brief FTC section, caption disclosure check, hidden characters"

BRAND_DISPLAY = {"zbm": "Z Best Media", "zbc": "Z Best Clips"}
COMMON = ("#ad", "#sponsored")
DISCLOSURES = {b: COMMON + (f"Paid partnership with {n}", f"Sponsored by {n}") for b, n in BRAND_DISPLAY.items()}
ALL_DISCLOSURES = tuple(sorted({d for v in DISCLOSURES.values() for d in v}))
DISCLOSURE_WINDOW = 100
LABEL_PLATFORMS = ("instagram", "tiktok", "youtube")
_ZWJ = "‍"
_ALLOWED_INVISIBLE = {"︎", "️"}           # emoji presentation selectors


def allowed(brand: str, disclosure: str) -> bool:
    return disclosure in DISCLOSURES.get(brand, ())


def hidden_characters(text: str) -> bool:
    for i, c in enumerate(text):
        if c in "\n\t":
            continue
        if c in _ALLOWED_INVISIBLE:
            continue
        cat = unicodedata.category(c)
        if c == _ZWJ:
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if prev and nxt and ord(prev) >= 0x2000 and ord(nxt) >= 0x2000 and not prev.isalnum() \
                    and not nxt.isalnum():
                continue
            return True
        if cat in ("Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"):
            return True
    return False


def _pattern(disclosure: str) -> re.Pattern:
    return re.compile(r"(?<![\w#])" + re.escape(disclosure) + r"(?![\w])", re.IGNORECASE | re.ASCII)


def caption_problem(caption: str, disclosure: str) -> Optional[str]:
    if hidden_characters(caption):
        return "CONTENT_HIDDEN_CHARACTERS"
    m = _pattern(disclosure).search(caption)
    if m is None:
        return "DISCLOSURE_MISSING"
    if m.end() > DISCLOSURE_WINDOW or re.search(r"#\w", caption[:m.start()]):
        return "DISCLOSURE_NOT_PROMINENT"
    return None


def label_problem(platform: str, label_on: bool) -> Optional[str]:
    if platform in LABEL_PLATFORMS and label_on is not True:
        return "PLATFORM_LABEL_REQUIRED"
    return None


def ftc_section(brand: str, disclosure: str) -> str:
    name = BRAND_DISPLAY[brand]
    return ("\n\n--\nRequired disclosure (FTC Endorsement Guides, 16 CFR Part 255)\n"
            f"This is a paid partnership with {name}: you are being paid or given something of value for this "
            "content.\n"
            f"Every post, story, short or video made under this brief must show \"{disclosure}\" at the start of the "
            f"caption, before any other hashtag and within the first {DISCLOSURE_WINDOW} characters.\n"
            "Turn on the platform's paid-partnership label wherever the platform has one. In a video, also say it out "
            "loud at the start.\n"
            "Say only what you honestly think, and claim only what you have actually experienced. Content without the "
            "disclosure will not be approved or paid.\n")


def rendered_brief(brand: str, title: str, text: str, disclosure: str) -> str:
    return f"{title}\n\n{text}{ftc_section(brand, disclosure)}"
