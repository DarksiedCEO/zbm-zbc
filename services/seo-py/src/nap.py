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
# USPS Publication 28, Appendix C1: the common street suffix abbreviations (AEGIS L5 round 4: Plz, Ctr, Ext ...)
STREET_WORDS.update({
    "aly": "alley", "anx": "annex", "arc": "arcade", "byu": "bayou", "bch": "beach", "bnd": "bend", "blf": "bluff",
    "btm": "bottom", "br": "branch", "brg": "bridge", "brk": "brook", "byp": "bypass", "cp": "camp", "cyn": "canyon",
    "cpe": "cape", "cswy": "causeway", "ctr": "center", "cntr": "center", "clf": "cliff", "clb": "club",
    "cmn": "common", "cor": "corner", "crse": "course", "cv": "cove", "crk": "creek", "cres": "crescent",
    "crst": "crest", "xing": "crossing", "xrd": "crossroad", "curv": "curve", "dl": "dale", "dm": "dam",
    "dv": "divide", "est": "estate", "expy": "expressway", "ext": "extension", "fls": "falls", "fry": "ferry",
    "fld": "field", "flds": "fields", "flt": "flat", "frd": "ford", "frst": "forest", "frg": "forge", "frk": "fork",
    "fwy": "freeway", "gdn": "garden", "gdns": "gardens", "gtwy": "gateway", "gln": "glen", "grn": "green",
    "grv": "grove", "hbr": "harbor", "hvn": "haven", "hts": "heights", "hl": "hill", "hls": "hills",
    "holw": "hollow", "inlt": "inlet", "is": "island", "jct": "junction", "ky": "key", "knl": "knoll", "lk": "lake",
    "lks": "lakes", "lndg": "landing", "lgt": "light", "lck": "lock", "ldg": "lodge", "mnr": "manor",
    "mdw": "meadow", "mdws": "meadows", "ml": "mill", "msn": "mission", "mtwy": "motorway", "mtn": "mountain",
    "nck": "neck", "orch": "orchard", "opas": "overpass", "pkwy": "parkway", "psge": "passage", "pne": "pine",
    "pnes": "pines", "pln": "plain", "plz": "plaza", "pt": "point", "prt": "port", "pr": "prairie", "radl": "radial",
    "rnch": "ranch", "rpd": "rapid", "rst": "rest", "rdg": "ridge", "riv": "river", "rte": "route", "shl": "shoal",
    "shr": "shore", "skwy": "skyway", "spg": "spring", "spgs": "springs", "sta": "station", "stra": "stravenue",
    "strm": "stream", "smt": "summit", "trwy": "throughway", "trce": "trace", "trak": "track", "tunl": "tunnel",
    "tpke": "turnpike", "upas": "underpass", "un": "union", "vly": "valley", "via": "viaduct", "vw": "view",
    "vlg": "village", "vl": "ville", "vis": "vista", "wy": "way", "wl": "well", "pk": "park", "pky": "parkway",
    "bl": "boulevard", "blv": "boulevard", "crt": "court", "drv": "drive", "hwy": "highway", "ave": "avenue"})
SUFFIXES = set(STREET_WORDS.values()) - {"north", "south", "east", "west", "northeast", "northwest", "southeast",
                                         "southwest", "mount", "fort"} | {"street", "loop", "mall", "oval", "pass",
                                                                         "path", "pike", "row", "run", "walk", "wall"}
_ORDINALS = {"first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5", "sixth": "6", "seventh": "7",
             "eighth": "8", "ninth": "9", "tenth": "10", "eleventh": "11", "twelfth": "12"}
_DIRECTIONS = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west", "northeast", "northwest",
               "southeast", "southwest"}
# unit words by kind: suite, unit, apartment and "#" are styles of one kind (the pool's formatting variants); a floor,
# a building and a room are other kinds, so "Floor 2" is never "Suite 2" (AEGIS L5 round 1)
UNIT_KIND = {"suite": "unit", "ste": "unit", "unit": "unit", "apt": "unit", "apartment": "unit", "#": "unit",
             "no": "unit", "bldg": "building", "building": "building", "fl": "floor", "floor": "floor",
             "rm": "room", "room": "room",
             # USPS Publication 28, Appendix C2 secondary unit designators that take a number (AEGIS L5 round 4)
             "spc": "space", "space": "space", "lot": "lot", "trlr": "trailer", "trailer": "trailer",
             "ofc": "office", "office": "office", "dept": "department", "department": "department",
             "hngr": "hangar", "hangar": "hangar", "pier": "pier", "slip": "slip", "ph": "penthouse",
             "penthouse": "penthouse"}
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
    text = "".join("-" if unicodedata.category(c) == "Pd" else c for c in text)   # every dash is "-" (round 7)
    text = re.sub(rf"(?<=\w)[{_APOSTROPHE_LIKE}](?=\w)", "", text)               # "Joe's" (AEGIS L5 round 3)
    # a hyphen with a letter on at least one side ("A-1", "Desert-Sky"); "1-10" between digits stays (round 6)
    text = re.sub(r"(?<=[^\W\d_])[\-‐‑](?=\w)|(?<=\w)[\-‐‑](?=[^\W\d_])", "", text)
    return re.sub(r"(?<!\w)(?:[^\W\d_]\.){2,}", lambda m: m.group(0).replace(".", ""), text)


def name_key(name: object) -> str:
    """Case, accents, punctuation, "&" / "and", a leading "the" and trailing legal suffixes (LLC, Inc ...) do not
    change a name. Letters of every script are kept ("Sushi 東京" is not "Sushi 大阪"; AEGIS L5 round 1)."""
    words = re.findall(r"[^\W_]+", _inner_punct(_fold(name)).replace("&", " and "))
    joined: list[str] = []             # single letters in a row are one set of initials ("J. R." = "J.R." = "JR")
    in_initials = False                # (AEGIS L5 round 4)
    for w in words:
        single = len(w) == 1 and w.isalpha()
        if single and in_initials:
            joined[-1] += w
        else:
            joined.append(w)
        in_initials = single
    words = joined
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    if len(words) > 1 and words[0] == "the":
        words.pop(0)
    return " ".join(words)


_UNIT = re.compile(r"(?:,\s*|\s)((?:" + "|".join(sorted((k for k in UNIT_KIND if k != "#"), key=len, reverse=True)) +
                   r")(?![^\W\d_])|#)\.?\s*(?:#\s*)?([^\W_][\w\-]*)$")
_ORDINAL_FLOOR = re.compile(r"(?:,\s*|\s)(\d+)(?:st|nd|rd|th)?\s+(?:floor|fl)\.?$")


# a vulgar fraction keeps its house number apart: "100½" is "100 1/2", never NFKC's "1001⁄2" (AEGIS L5 round 6)
_FRACTIONS = str.maketrans({"½": " 1/2", "¼": " 1/4", "¾": " 3/4", "⅓": " 1/3", "⅔": " 2/3", "⅛": " 1/8",
                            "⁄": "/"})


def _fractions(text: str) -> str:
    """Every vulgar fraction character ("½", "⅜", "⅕" ...) is " n/d", so NFKC never glues it to the house number
    ("100⅜" is "100 3/8", never "1003⁄8"; AEGIS L5 rounds 6-7)."""
    out = []
    for c in text.translate(_FRACTIONS):
        if unicodedata.category(c) == "No" and "⁄" in unicodedata.normalize("NFKC", c):   # "↉" too (round 8)
            out.append(" " + unicodedata.normalize("NFKC", c).replace("⁄", "/"))
        elif unicodedata.category(c) == "No" and unicodedata.normalize("NFKC", c).isdigit():
            out.append(" " + unicodedata.normalize("NFKC", c))    # "100¹⁄₂" is "100 1/ 2", never "1001" (round 9)
        else:
            out.append(c)
    return "".join(out)


def _unit_value(v: str) -> bool:
    """A unit number: it holds a digit, or is one or two letters that are not a street suffix ("Suite A", "Ste B")."""
    if any(c.isdigit() for c in v):
        return True
    if len(v) == 1:
        return True                    # "Suite E", "Apt N": one letter after a unit word is a unit (AEGIS L5 round 9)
    return len(v) == 2 and v not in STREET_WORDS and v not in SUFFIXES


def street_key(street: object) -> tuple[str, Optional[str]]:
    """(street line, unit) with abbreviations spelled out; unit styles of one kind ("Ste 4", "Ste #4", "#4", "Unit 4")
    read as one, while a floor, building or room keeps its kind ("floor:2" is not "unit:2"). Trailing punctuation is
    formatting ("Ste 4."), several unit parts are all kept ("Suite 400, Fl 4"), "4th Floor" is "floor:4", and street
    names of every script are kept ("100 東京通り" is not "100 大阪通り") (AEGIS L5 round 3)."""
    raw_street = _fractions(street) if isinstance(street, str) else street
    text = _fold(raw_street).replace("#", " # ")
    text = re.sub(r"^\s*(\d+)\s*-\s*([a-z])\b", r"\1\2", text)    # "1200-B" is house number "1200b" (round 6)
    text = "".join("-" if unicodedata.category(c) == "Pd" else c for c in text)
    # a unit word joined to its value by a dash ("Suite-B", "Apt-C") keeps the unit (AEGIS L5 round 7)
    text = re.sub(r"(?<![^\W\d_])(" + "|".join(k for k in UNIT_KIND if k != "#") + r")-(?=\w)", r"\1 ", text)
    text = _inner_punct(text)
    text = re.sub(r"\b(" + "|".join(_ORDINALS) + r")\b", lambda m: _ORDINALS[m.group(1)], text)   # "Fourth Floor"
    units = []
    while True:
        text = re.sub(r"[\s.,;:]+$", "", text)
        m = _ORDINAL_FLOOR.search(text)
        if m:
            units.append(f"floor:{m.group(1)}")
            text = text[:m.start()]
            continue
        m = _UNIT.search(text)
        if not m or not _unit_value(m.group(2)):
            break                      # "1 Pier Ave", "Post Office Rd", "Lot Rd": a street name, not a unit (round 5)
        # "4-B" is "4b"; "1-10" stays "1-10" (never "110": AEGIS L5 round 5)
        units.append(f"{UNIT_KIND[m.group(1)]}:{re.sub(r'(?<=[0-9])-(?=[a-z])|(?<=[a-z])-(?=[0-9])', '', m.group(2))}")
        text = text[:m.start()]
    # units sorted by kind only, so "Suite 400, Fl 4" is "Fl 4 Suite 400" while two units of one kind keep their order
    # ("Unit 5, Ste 4" is not "Unit 4, Ste 5"; AEGIS L5 round 6)
    unit = "|".join(sorted(units, key=lambda u: u.split(":")[0])) or None
    raw = re.findall(r"[^\W_]+", _join_initials(text))
    words = []
    for i, w in enumerate(raw):
        rest = raw[i + 1:]
        tail_only = bool(rest) and all(x in _DIRECTIONS or STREET_WORDS.get(x, x) in SUFFIXES for x in rest)
        if w == "st" and rest and not tail_only:
            words.append("saint")      # "St. Charles Ave": St before a name is Saint (AEGIS L5 round 2); not "St Ext"
        elif w in ("n", "s", "e", "w") and tail_only and i >= 1 and any(c.isdigit() for c in raw[0]) and \
                all(re.fullmatch(r"\d+[a-z]?|[a-z]", x) for x in raw[:i]):
            # "100 S St", "100A S St", "100 1/2 S St", "1200-B N St": a lettered street (AEGIS L5 rounds 4-5)
            words.append(w)            # "100 S St" is a lettered street, never "South Street" (AEGIS L5 round 4)
        else:
            words.append(STREET_WORDS.get(w, w))
    return " ".join(words), unit


def _house(words: list[str]) -> list[str]:
    """The leading words of a street line that hold a digit: the house number with any fraction or range."""
    out = []
    for w in words:
        if not any(c.isdigit() for c in w):
            break
        out.append(w)
    return out


def same_number_other_words(a: tuple[str, Optional[str]], b: tuple[str, Optional[str]]) -> bool:
    """Two street keys with the same house number and unit whose street words differ: formatting this module does not
    know, or another street. The data cannot tell, so a person decides (AEGIS L5 round 4: the inverted default)."""
    wa, wb = a[0].split(), b[0].split()
    ha, hb = _house(wa), _house(wb)    # the whole house number ("100 1 2", "100 102"), not only its first word (round 9)
    return bool(ha) and a != b and a[1] == b[1] and ha == hb and wa[len(ha):] != wb[len(hb):]


def loose(key: str) -> str:
    """A key with its spaces taken out: "desert sky" and "desertsky" differ only in spacing (AEGIS L5 round 7)."""
    return key.replace(" ", "")


def same_zip_other_city_words(a: tuple[str, str, str], b: tuple[str, str, str]) -> bool:
    """Two city keys with the same state and ZIP whose city words differ: an abbreviation this module does not know,
    or another city sharing a ZIP. The data cannot tell, so a person decides (AEGIS L5 round 6)."""
    return bool(a[2]) and a[2] == b[2] and a[1] == b[1] and bool(a[1]) and a[0] != b[0]


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
CITY_WORDS = {"st": "saint", "ste": "sainte", "ft": "fort", "mt": "mount", "pt": "point", "n": "north", "s": "south",
              "e": "east", "w": "west", "bch": "beach", "hts": "heights", "spgs": "springs", "spg": "spring",
              "vly": "valley", "cyn": "canyon", "lk": "lake", "pk": "park", "hls": "hills", "crk": "creek",
              "ctr": "center", "vlg": "village", "is": "island", "pnes": "pines", "mtn": "mountain",
              "mtns": "mountains", "jct": "junction", "fls": "falls", "gdns": "gardens", "gdn": "garden",
              "hbr": "harbor", "hl": "hill", "lks": "lakes", "mdw": "meadow", "mdws": "meadows", "prt": "port",
              "sta": "station", "vly": "valley", "vw": "view", "xing": "crossing", "frk": "fork", "frst": "forest",
              "grv": "grove", "hvn": "haven", "is.": "island", "isl": "island", "brk": "brook", "cv": "cove",
              "rdg": "ridge", "riv": "river", "shr": "shore", "spr": "springs", "twp": "township",
              "hgts": "heights", "hghts": "heights", "jctn": "junction", "vill": "village", "cty": "city"}


def state_key(state: object) -> str:
    """A US state as its two-letter code ("Nevada", "NV", "nv.", "Nev.", "D.C." all read as the code); anything else as
    written in full, never cut (AEGIS L5 round 1: cutting to two letters read Mississippi and Michigan as one state)."""
    text = " ".join(re.findall(r"[a-z]+", _join_initials(_fold(state))))
    return STATES.get(text) or AP_STATES.get(text) or text


def city_key(city: object, state: object, zip_code: object) -> tuple[str, str, str]:
    if type(zip_code) is int and 0 <= zip_code <= 99999:
        zip_code = f"{zip_code:05d}"   # a ZIP stored as a number lost its leading zero (AEGIS L5 round 2)
    z = re.sub(r"[^0-9]", "", _fold(zip_code))[:5]
    folded = "".join(" " if unicodedata.category(c) == "Pd" else c for c in _fold(city))   # "Winston-Salem" (round 7)
    words = [CITY_WORDS.get(w, w) for w in re.findall(r"[^\W_]+", _inner_punct(folded))]
    return " ".join(words), state_key(state), z
