"""
BCP 47 oracle: the RFC 5646 section 2.1 ABNF (langtag, privateuse, grandfathered) read by hand, plus subtag
validity from the CLDR validity data (``data/cldr/{language,script,region,variant}.xml``, Unicode-3.0, CLDR 48)
parsed here from the original XML (the service bundles its own derived list; this oracle reads the source files).

``stored(tag)`` is what SPEC B-RO-05 / D-01 L says is stored: ``language[-script][-region]`` with each subtag that is
not valid in CLDR dropped (an invalid LANGUAGE makes the tag invalid), normalised case (``en``, ``Latn``, ``US``).
"""

from __future__ import annotations

import os
import re

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cldr")
# ``reserved`` too (rv-wave1v2 #6): CLDR files part of the BCP 47 private-use ranges (qaa~qtz, Qaaa~Qabx, AA, QM~QZ,
# XA~XZ, ZZ) as reserved and part as private_use; RFC 5646 2.2.1-2.2.4 makes the whole ranges valid
ACCEPTED = frozenset({"regular", "special", "macrolanguage", "macroregion", "deprecated", "private_use", "reserved",
                      "unknown"})
GRANDFATHERED = frozenset(x.lower() for x in (
    "en-GB-oed", "i-ami", "i-bnn", "i-default", "i-enochian", "i-hak", "i-klingon", "i-lux", "i-mingo", "i-navajo",
    "i-pwn", "i-tao", "i-tay", "i-tsu", "sgn-BE-FR", "sgn-BE-NL", "sgn-CH-DE", "art-lojban", "cel-gaulish",
    "no-bok", "no-nyn", "zh-guoyu", "zh-hakka", "zh-min", "zh-min-nan", "zh-xiang"))
_ID = re.compile(r"<id type='(\w+)' idStatus='(\w+)'>([^<]*)</id>")
_CACHE: dict = {}


def _expand(item: str) -> list:
    if "~" not in item:
        return [item]
    a, b = item.split("~")
    stem, last = a[:len(a) - len(b)], a[len(a) - len(b):]
    out = []
    # a range on the last character(s): Brah~i = Brah Brai; 013~5 = 013 014 015
    lo, hi = last, b
    cur = lo
    while True:
        out.append(stem + cur)
        if cur == hi:
            break
        cur = cur[:-1] + chr(ord(cur[-1]) + 1)
        if len(out) > 1000:
            raise ValueError(item)
    return out


def codes(kind: str) -> frozenset:
    if kind not in _CACHE:
        got = set()
        with open(os.path.join(DATA, kind + ".xml"), encoding="utf-8") as fh:
            text = fh.read()
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        for typ, status, body in _ID.findall(text):
            if typ != kind or status not in ACCEPTED:
                continue
            for item in body.split():
                got.update(x.lower() for x in _expand(item))
        _CACHE[kind] = frozenset(got)
    return _CACHE[kind]


def _alpha(s: str, lo: int, hi: int) -> bool:
    return lo <= len(s) <= hi and s.isascii() and s.isalpha()


def _alnum(s: str, lo: int, hi: int) -> bool:
    return lo <= len(s) <= hi and s.isascii() and s.isalnum()


def well_formed(tag: str) -> bool:
    """RFC 5646 2.1, by hand."""
    if not isinstance(tag, str) or not tag or not tag.isascii():
        return False
    low = tag.lower()
    if low in GRANDFATHERED:
        return True
    parts = low.split("-")
    if any(not p for p in parts):
        return False
    if parts[0] == "x":
        return len(parts) > 1 and all(_alnum(p, 1, 8) for p in parts[1:])
    i = 0
    lang = parts[0]
    if not (_alpha(lang, 2, 3) or _alpha(lang, 5, 8)):
        return False
    i = 1
    if len(lang) <= 3:
        ext = 0
        while i < len(parts) and ext < 3 and _alpha(parts[i], 3, 3):
            i += 1
            ext += 1
    if i < len(parts) and _alpha(parts[i], 4, 4):
        i += 1
    if i < len(parts) and (_alpha(parts[i], 2, 2) or (len(parts[i]) == 3 and parts[i].isdigit())):
        i += 1
    seen_v = set()
    while i < len(parts) and (_alnum(parts[i], 5, 8) or (len(parts[i]) == 4 and parts[i][0].isdigit()
                                                          and parts[i].isalnum())):
        if parts[i] in seen_v:
            return False
        seen_v.add(parts[i])
        i += 1
    seen_s = set()
    while i < len(parts) and len(parts[i]) == 1 and parts[i] != "x":
        s = parts[i]
        if s in seen_s:
            return False
        seen_s.add(s)
        i += 1
        n = 0
        while i < len(parts) and _alnum(parts[i], 2, 8):
            i += 1
            n += 1
        if n == 0:
            return False
    if i < len(parts) and parts[i] == "x":
        i += 1
        n = 0
        while i < len(parts) and _alnum(parts[i], 1, 8):
            i += 1
            n += 1
        if n == 0:
            return False
    return i == len(parts)


def stored(tag):
    """None (absent / blank), "invalid", ("coded", tag lower-cased) for a valid tag with nothing to keep (private use
    only, grandfathered: RFC 5646 2.1 ``privateuse`` / ``grandfathered``), or language[-Script][-REGION]."""
    if tag is None or not str(tag).strip():
        return None
    t = str(tag).strip()
    if not well_formed(t):
        return "invalid"
    parts = t.lower().split("-")
    if parts[0] == "x" or t.lower() in GRANDFATHERED:
        return ("coded", t.lower())
    if len(parts[0]) > 3:
        return "invalid"
    lang = parts[0]
    if lang not in codes("language"):
        return "invalid"
    i = 1
    while i < len(parts) and len(parts[i]) == 3 and parts[i].isalpha():
        i += 1                                          # extlang: not stored
    out = lang
    if i < len(parts) and len(parts[i]) == 4 and parts[i].isalpha():
        if parts[i] in codes("script"):
            out += "-" + parts[i].title()
        i += 1
    if i < len(parts) and (len(parts[i]) == 2 and parts[i].isalpha() or len(parts[i]) == 3 and parts[i].isdigit()):
        if parts[i] in codes("region"):
            out += "-" + parts[i].upper()
    return out


def self_test() -> None:
    assert stored("en-US") == "en-US" and stored("zh-hant-tw") == "zh-Hant-TW"
    assert stored("123") == "invalid" and stored("") is None and stored("  ") is None
    assert stored("en-Qqqq-US") == "en-US"            # Qqqq: not a CLDR script (Qaaa-Qabx private use only)
    assert stored("xx-US") == "invalid"               # xx: no such language
    assert stored("de-CH-1996") == "de-CH" and stored("sr-Latn") == "sr-Latn"
    assert well_formed("x-private") and stored("x-private") == ("coded", "x-private")
    assert stored("i-klingon") == ("coded", "i-klingon") and stored("en-GB-oed") == ("coded", "en-gb-oed")
    assert stored("qaa-Qaaa-QM-x-southern") == "qaa-Qaaa-QM" and stored("qtz-Qabx-XZ") == "qtz-Qabx-XZ"
    assert stored("de-1996-1996") == "invalid" and stored("en-a-bbb-a-ccc") == "invalid"
    assert stored("en-a-bbb-b-ccc") == "en" and stored("de-1996-1901") == "de"
