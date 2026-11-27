"""
Name / address / phone / website normalisation for the Local agents (SEO-L4 citation audit, SEO-L5 duplicate
listings). One job: turn a listing's fields into comparable keys so that FORMATTING differences ("St." / "Street",
"Ste 4" / "Suite 4", "(602) 555-0123" / "602.555.0123", "http://" / "https://www.", case, "&" / "and") never read as
a different business, while a real difference (another street number, another phone, another name) always does.
Deterministic, standard library only.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional
from urllib.parse import urlsplit

STREET_WORDS = {"st": "street", "str": "street", "ave": "avenue", "av": "avenue", "blvd": "boulevard",
                "rd": "road", "dr": "drive", "ln": "lane", "ct": "court", "pl": "place", "pkwy": "parkway",
                "hwy": "highway", "cir": "circle", "ter": "terrace", "trl": "trail", "sq": "square",
                "n": "north", "s": "south", "e": "east", "w": "west", "ne": "northeast", "nw": "northwest",
                "se": "southeast", "sw": "southwest"}
UNIT_WORDS = ("suite", "ste", "unit", "apt", "apartment", "bldg", "building", "fl", "floor", "rm", "room", "#")
NAME_NOISE = {"the", "llc", "inc", "co", "corp", "corporation", "company", "ltd", "pllc", "pc"}


def _fold(text: object) -> str:
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def name_key(name: object) -> str:
    """Case, accents, punctuation, "&" / "and" and legal suffixes (LLC, Inc ...) do not change a name."""
    words = re.findall(r"[a-z0-9]+", _fold(name).replace("&", " and "))
    return " ".join(w for w in words if w not in NAME_NOISE)


def street_key(street: object) -> tuple[str, Optional[str]]:
    """(street line, unit) with abbreviations spelled out and any unit style ("Ste 4", "#4", "Unit 4") read as one."""
    text = _fold(street).replace("#", " # ")
    unit = None
    m = re.search(r"(?:,\s*|\s)(suite|ste|unit|apt|apartment|bldg|building|fl|floor|rm|room|#)\.?\s*([a-z0-9\-]+)\s*$",
                  text)
    if m:
        unit, text = m.group(2), text[:m.start()]
    words = [STREET_WORDS.get(w, w) for w in re.findall(r"[a-z0-9]+", text)]
    return " ".join(words), unit


def phone_key(phone: object) -> Optional[str]:
    """Ten digits of a North American number, or None."""
    digits = re.sub(r"\D", "", _fold(phone))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 and digits[0] in "23456789" and digits[3] in "23456789" else None


def is_toll_free(phone_digits: Optional[str]) -> bool:
    return bool(phone_digits) and phone_digits[:3] in ("800", "833", "844", "855", "866", "877", "888")


def website_key(url: object) -> Optional[str]:
    """The site's host without "www." (scheme, trailing slash and case ignored), plus any path."""
    text = _fold(url).strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = (parts.hostname or "").removeprefix("www.")
    if not host:
        return None
    return host + parts.path.rstrip("/")


def city_key(city: object, state: object, zip_code: object) -> tuple[str, str, str]:
    z = re.sub(r"\D", "", _fold(zip_code))[:5]
    return " ".join(re.findall(r"[a-z]+", _fold(city))), _fold(state).strip()[:2], z
