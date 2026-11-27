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
                "se": "southeast", "sw": "southwest", "mt": "mount", "ft": "fort"}
_DIRECTIONS = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west", "northeast", "northwest",
               "southeast", "southwest"}
# unit words by kind: suite, unit, apartment and "#" are styles of one kind (the pool's formatting variants); a floor,
# a building and a room are other kinds, so "Floor 2" is never "Suite 2" (AEGIS L5 round 1)
UNIT_KIND = {"suite": "unit", "ste": "unit", "unit": "unit", "apt": "unit", "apartment": "unit", "#": "unit",
             "no": "unit", "bldg": "building", "building": "building", "fl": "floor", "floor": "floor",
             "rm": "room", "room": "room"}
# legal suffixes, set aside only at the END of a name ("PC Repair Pros" keeps "pc"); a leading "the" is set aside too
LEGAL_SUFFIXES = {"llc", "inc", "co", "corp", "corporation", "company", "ltd", "pllc", "pc", "lp", "llp", "pa"}


def _fold(text: object) -> str:
    """NFKC, case-folded, accents set aside on LATIN letters only ("Café" is "cafe"), then NFC. A mark on any other
    script stays: Japanese voicing marks make another word ("ガ" is not "カ"; AEGIS L5 round 2)."""
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFKC", text).casefold()
    out, base_latin = [], False
    for c in unicodedata.normalize("NFD", text):
        if unicodedata.combining(c):
            if not base_latin:
                out.append(c)
            continue
        base_latin = "LATIN" in unicodedata.name(c, "")
        out.append(c)
    return unicodedata.normalize("NFC", "".join(out))


def _join_initials(text: str) -> str:
    """"n.w." / "n. w." / "d.c." read as one word ("nw", "dc"), never as two letters."""
    return re.sub(r"\b([a-z])\.\s?([a-z])\b\.?", r"\1\2", text)


_APOSTROPHE_LIKE = "'\u2018\u2019\u201b\u0060\u00b4\u02b9\u02bb\u02bc\u02bd\u2032"


def _inner_punct(text: str) -> str:
    """Punctuation inside a word is formatting: "Joe's" is "joes", "A.A.A." is "aaa" (AEGIS L5 round 3)."""
    text = re.sub(rf"(?<=\w)[{_APOSTROPHE_LIKE}](?=\w)", "", text)
    return re.sub(r"(?<!\w)(?:[^\W\d_]\.){2,}", lambda m: m.group(0).replace(".", ""), text)


def name_key(name: object) -> str:
    """Case, accents, punctuation, "&" / "and", a leading "the" and trailing legal suffixes (LLC, Inc ...) do not
    change a name. Letters of every script are kept ("Sushi 東京" is not "Sushi 大阪"; AEGIS L5 round 1)."""
    words = re.findall(r"[^\W_]+", _inner_punct(_fold(name)).replace("&", " and "))
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    if len(words) > 1 and words[0] == "the":
        words.pop(0)
    return " ".join(words)


_UNIT = re.compile(r"(?:,\s*|\s)((?:suite|ste|unit|apt|apartment|bldg|building|fl|floor|rm|room|no)(?![^\W\d_])|#)\.?\s*"
                   r"(?:#\s*)?([^\W_][\w\-]*)$")
_ORDINAL_FLOOR = re.compile(r"(?:,\s*|\s)(\d+)(?:st|nd|rd|th)?\s+(?:floor|fl)\.?$")


def street_key(street: object) -> tuple[str, Optional[str]]:
    """(street line, unit) with abbreviations spelled out; unit styles of one kind ("Ste 4", "Ste #4", "#4", "Unit 4")
    read as one, while a floor, building or room keeps its kind ("floor:2" is not "unit:2"). Trailing punctuation is
    formatting ("Ste 4."), several unit parts are all kept ("Suite 400, Fl 4"), "4th Floor" is "floor:4", and street
    names of every script are kept ("100 東京通り" is not "100 大阪通り") (AEGIS L5 round 3)."""
    text = _inner_punct(_fold(street)).replace("#", " # ")
    units = []
    while True:
        text = re.sub(r"[\s.,;:]+$", "", text)
        m = _ORDINAL_FLOOR.search(text)
        if m:
            units.append(f"floor:{m.group(1)}")
            text = text[:m.start()]
            continue
        m = _UNIT.search(text)
        if not m:
            break
        units.append(f"{UNIT_KIND[m.group(1)]}:{m.group(2)}")
        text = text[:m.start()]
    unit = "|".join(sorted(units)) or None
    raw = re.findall(r"[^\W_]+", _join_initials(text))
    words = []
    for i, w in enumerate(raw):
        rest = raw[i + 1:]
        if w == "st" and rest and not all(x in _DIRECTIONS for x in rest):
            words.append("saint")      # "St. Charles Ave": St before a name is Saint (AEGIS L5 round 2)
        else:
            words.append(STREET_WORDS.get(w, w))
    return " ".join(words), unit


def phone_key(phone: object) -> Optional[str]:
    """Ten digits of a North American number, or None. Digits of any script count ("٦٠٢" is "602")."""
    digits = "".join(str(unicodedata.decimal(c)) for c in _fold(phone) if unicodedata.decimal(c, None) is not None)
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
# AP-style abbreviations ("Nev.", "Calif.", "Wash."); the two-letter codes read as themselves
AP_STATES = {"ala": "al", "ariz": "az", "ark": "ar", "calif": "ca", "cal": "ca", "colo": "co", "conn": "ct",
             "del": "de", "fla": "fl", "ill": "il", "ind": "in", "kan": "ks", "kans": "ks", "mass": "ma",
             "mich": "mi", "minn": "mn", "miss": "ms", "mont": "mt", "neb": "ne", "nebr": "ne", "nev": "nv",
             "okla": "ok", "ore": "or", "oreg": "or", "penn": "pa", "penna": "pa", "tenn": "tn", "tex": "tx",
             "wash": "wa", "wis": "wi", "wisc": "wi", "wyo": "wy", "wva": "wv", "w va": "wv", "n mex": "nm", "n dak": "nd", "s dak": "sd",
             "n car": "nc", "s car": "sc", "d c": "dc", "washington dc": "dc",
             "washington d c": "dc"}
CITY_WORDS = {"st": "saint", "ste": "sainte", "ft": "fort", "mt": "mount", "pt": "point"}


def state_key(state: object) -> str:
    """A US state as its two-letter code ("Nevada", "NV", "nv.", "Nev.", "D.C." all read as the code); anything else as
    written in full, never cut (AEGIS L5 round 1: cutting to two letters read Mississippi and Michigan as one state)."""
    text = " ".join(re.findall(r"[a-z]+", _join_initials(_fold(state))))
    return STATES.get(text) or AP_STATES.get(text) or text


def city_key(city: object, state: object, zip_code: object) -> tuple[str, str, str]:
    if type(zip_code) is int and 0 <= zip_code <= 99999:
        zip_code = f"{zip_code:05d}"   # a ZIP stored as a number lost its leading zero (AEGIS L5 round 2)
    z = re.sub(r"[^0-9]", "", _fold(zip_code))[:5]
    words = [CITY_WORDS.get(w, w) for w in re.findall(r"\w+", _fold(city))]
    return " ".join(words), state_key(state), z
