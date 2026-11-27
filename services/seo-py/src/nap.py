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
# unit words by kind: suite, unit, apartment and "#" are styles of one kind (the pool's formatting variants); a floor,
# a building and a room are other kinds, so "Floor 2" is never "Suite 2" (AEGIS L5 round 1)
UNIT_KIND = {"suite": "unit", "ste": "unit", "unit": "unit", "apt": "unit", "apartment": "unit", "#": "unit",
             "no": "unit", "bldg": "building", "building": "building", "fl": "floor", "floor": "floor",
             "rm": "room", "room": "room"}
# legal suffixes, set aside only at the END of a name ("PC Repair Pros" keeps "pc"); a leading "the" is set aside too
LEGAL_SUFFIXES = {"llc", "inc", "co", "corp", "corporation", "company", "ltd", "pllc", "pc", "lp", "llp", "pa"}


def _fold(text: object) -> str:
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def name_key(name: object) -> str:
    """Case, accents, punctuation, "&" / "and", a leading "the" and trailing legal suffixes (LLC, Inc ...) do not
    change a name. Letters of every script are kept ("Sushi 東京" is not "Sushi 大阪"; AEGIS L5 round 1)."""
    words = re.findall(r"\w+", _fold(name).replace("&", " and "))
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    if len(words) > 1 and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def street_key(street: object) -> tuple[str, Optional[str]]:
    """(street line, unit) with abbreviations spelled out; unit styles of one kind ("Ste 4", "Ste #4", "#4", "Unit 4")
    read as one, while a floor, building or room keeps its kind ("floor:2" is not "unit:2")."""
    text = _fold(street).replace("#", " # ")
    unit = None
    m = re.search(r"(?:,\s*|\s)(suite|ste|unit|apt|apartment|bldg|building|fl|floor|rm|room|no|#)\.?\s*(?:#\s*)?"
                  r"([a-z0-9\-]+)\s*$", text)
    if m:
        unit, text = f"{UNIT_KIND[m.group(1)]}:{m.group(2)}", text[:m.start()]
    words = [STREET_WORDS.get(w, w) for w in re.findall(r"[a-z0-9]+", text)]
    return " ".join(words), unit


def phone_key(phone: object) -> Optional[str]:
    """Ten digits of a North American number, or None."""
    digits = re.sub(r"[^0-9]", "", _fold(phone))          # NFKC first, so other scripts' digits read as ASCII
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


STATES = {"alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca", "colorado": "co",
          "connecticut": "ct", "delaware": "de", "district of columbia": "dc", "florida": "fl", "georgia": "ga",
          "hawaii": "hi", "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
          "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md", "massachusetts": "ma",
          "michigan": "mi", "minnesota": "mn", "mississippi": "ms", "missouri": "mo", "montana": "mt",
          "nebraska": "ne", "nevada": "nv", "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm",
          "new york": "ny", "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
          "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
          "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt", "virginia": "va", "washington": "wa",
          "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy", "puerto rico": "pr"}
CITY_WORDS = {"st": "saint", "ste": "sainte", "ft": "fort", "mt": "mount", "pt": "point"}


def state_key(state: object) -> str:
    """A US state as its two-letter code ("Nevada", "NV", "nv." all read "nv"); anything else as written in full, never
    cut (AEGIS L5 round 1: cutting to two letters read Mississippi and Michigan as one state)."""
    text = " ".join(re.findall(r"[a-z]+", _fold(state)))
    return STATES.get(text, text)


def city_key(city: object, state: object, zip_code: object) -> tuple[str, str, str]:
    z = re.sub(r"[^0-9]", "", _fold(zip_code))[:5]
    words = [CITY_WORDS.get(w, w) for w in re.findall(r"\w+", _fold(city))]
    return " ".join(words), state_key(state), z
