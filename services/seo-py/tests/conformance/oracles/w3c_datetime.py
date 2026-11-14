"""
W3C-DTF oracle, written from the W3C note "Date and Time Formats" (1997): the six levels

   YYYY | YYYY-MM | YYYY-MM-DD | YYYY-MM-DDThh:mmTZD | YYYY-MM-DDThh:mm:ssTZD | YYYY-MM-DDThh:mm:ss.sTZD

with ASCII digits only, MM 01-12, DD 01-31, hh 00-23, mm / ss 00-59, one or more fraction digits, and
TZD = ``Z`` | ``+hh:mm`` | ``-hh:mm``. A character-by-character reader (no regular expression).
"""

from __future__ import annotations

DIG = "0123456789"


def _num(s: str, i: int, n: int):
    part = s[i:i + n]
    if len(part) != n or any(c not in DIG for c in part):
        return None
    return int(part)


def _tzd(s: str, i: int) -> bool:
    if s[i:] == "Z":
        return True
    if len(s) - i != 6 or s[i] not in "+-" or s[i + 3] != ":":
        return False
    h, m = _num(s, i + 1, 2), _num(s, i + 4, 2)
    return h is not None and m is not None and h <= 23 and m <= 59


def valid(s: str) -> bool:
    if not isinstance(s, str):
        return False
    y = _num(s, 0, 4)
    if y is None:
        return False
    if len(s) == 4:
        return True
    if s[4:5] != "-":
        return False
    mo = _num(s, 5, 2)
    if mo is None or not 1 <= mo <= 12:
        return False
    if len(s) == 7:
        return True
    if s[7:8] != "-":
        return False
    d = _num(s, 8, 2)
    if d is None or not 1 <= d <= 31:
        return False
    if len(s) == 10:
        return True
    if s[10:11] != "T":
        return False
    hh, mm = _num(s, 11, 2), _num(s, 14, 2)
    if hh is None or mm is None or s[13:14] != ":" or hh > 23 or mm > 59:
        return False
    i = 16
    if s[i:i + 1] == ":":
        ss = _num(s, i + 1, 2)
        if ss is None or ss > 59:
            return False
        i += 3
        if s[i:i + 1] == ".":
            j = i + 1
            while j < len(s) and s[j] in DIG:
                j += 1
            if j == i + 1:
                return False
            i = j
    return _tzd(s, i)


def self_test() -> None:
    for ok in ("1997", "1997-07", "1997-07-16", "1997-07-16T19:20+01:00", "1997-07-16T19:20:30+01:00",
               "1997-07-16T19:20:30.45+01:00", "2026-11-13T10:00Z"):
        assert valid(ok), ok
    for bad in ("97", "1997-7", "1997-07-16T19:20", "1997-07-16 19:20Z", "1997-13", "1997-07-32",
                "\uff12\uff10\uff12\uff16", "1997-07-16T24:00Z", "1997-07-16T19:20:30.Z", "2026-11-13x"):
        assert not valid(bad), bad
