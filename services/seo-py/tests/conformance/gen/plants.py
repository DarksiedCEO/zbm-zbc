"""
A-18 planted values (synthetic, built at run time; SUITE_PLAN 3). No literal in this file is shaped like a secret or
personal datum: prefixes are assembled from character codes, numbers are generated and completed, names come from a
syllable grammar. Every value carries a run-unique core (``uniq``) so that a hit in a scanned output is a leak and
never a coincidence with a service constant (checked by ``constants_free``).
"""

from __future__ import annotations

import random
import string

CONS = "bdfgklmnprstvz"
VOWS = "aeiou"
SEPS6 = ("@", " (at) ", "[at]", " at ", "%40", "&#64;")
QWERTY = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
# strong-key-shaped prefixes, as character codes (never a literal prefix in source)
_PREFIX_CODES = ((115, 107, 95, 108, 105, 118, 101, 95), (103, 104, 112, 95), (120, 111, 120, 98, 45),
                 (65, 75, 73, 65))


def _syl(r: random.Random, n: int) -> str:
    return "".join(r.choice(CONS) + r.choice(VOWS) for _ in range(n))


def name(r: random.Random) -> str:
    return _syl(r, r.randint(2, 3)).capitalize()


def _luhn_complete(digits: list) -> list:
    s = 0
    for i, d in enumerate(reversed(digits)):
        x = d * 2 if i % 2 == 0 else d
        s += x - 9 if x > 9 else x
    return digits + [(10 - s % 10) % 10]


def _hex(r: random.Random, n: int) -> str:
    return "".join(r.choice("0123456789abcdef") for _ in range(n))


def _b64(r: random.Random, n: int) -> str:
    return "".join(r.choice(string.ascii_letters + string.digits + "+/") for _ in range(n))


def uniq(r: random.Random) -> str:
    """A run-unique letters-only core of 10 letters (pronounceable; never a service word)."""
    return _syl(r, 5)


def shape_value(shape: str, r: random.Random) -> str:
    """One planted value of ``shape``; the core (uniq) is inside, so a finder can always recognise it."""
    u = uniq(r)
    if shape == "email":
        return u + r.choice(SEPS6) + _syl(r, 2) + "." + r.choice(("test", "example"))
    if shape == "phone_e164":
        return "+1" + "".join(r.choice("23456789") for _ in range(10))
    if shape == "phone_nanp":
        d = [r.choice("23456789") for _ in range(10)]
        a, b, c = "".join(d[:3]), "".join(d[3:6]), "".join(d[6:])
        return r.choice((f"({a}) {b}-{c}", f"{a}-{b}-{c}", f"{a}.{b}.{c}", f"{a}{b}{c}"))
    if shape == "ssn_shape":
        return f"{r.randint(100, 665)}-{r.randint(10, 99)}-{r.randint(1000, 9999)}"
    if shape == "luhn16":
        return "".join(map(str, _luhn_complete([4] + [r.randint(0, 9) for _ in range(14)])))
    if shape == "key_prefix":
        return "".join(chr(c) for c in r.choice(_PREFIX_CODES)) + u + _hex(r, 20)
    if shape == "jwt_shape":
        return "".join(map(chr, (101, 121, 74))) + _b64(r, 20).replace("+", "x").replace("/", "y") + "." + u + _b64(r, 12).replace("+", "z") \
            .replace("/", "w") + "." + _hex(r, 16)
    if shape == "uuid":
        h = _hex(r, 32)
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    if shape == "ipv4":
        return ".".join(str(x) for x in (203, 0, 113, r.randint(1, 254)))
    if shape == "ipv6":
        return ":".join(("%x" % 8193, "%x" % 3512, "")) + ":" + _hex(r, 4)
    if shape in ("hex16", "hex32", "hex64"):
        return _hex(r, int(shape[3:]))
    if shape == "base64_40":
        return u + _b64(r, 30)
    if shape == "given_name":
        return u.capitalize()
    if shape == "surname":
        return (u + _syl(r, 1)).capitalize()
    if shape == "first_last_slug":
        return u + "-" + _syl(r, 3)
    if shape == "unlisted_name":
        return u[:6].capitalize() + _syl(r, 2)
    if shape == "common_password":
        row = r.choice(QWERTY)
        k = r.randint(0, len(row) - 4)
        return u + str(r.randint(10, 9999)) + r.choice("!#$*") + row[k:k + 4]
    if shape == "random_letters":
        return u + "".join(r.choice(string.ascii_lowercase) for _ in range(6))
    if shape == "pronounceable":
        return u + _syl(r, 2)
    if shape == "glued_words":
        return u + "and" + _syl(r, 3)
    if shape == "upper_case":
        return (u + _syl(r, 2)).upper()
    if shape == "non_ascii_name":
        return u[:5] + "\u00e9" + u[5:] + "\u00f8"
    if shape == "dob_date":
        return f"{r.randint(1940, 2005)}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}"
    raise KeyError(shape)


SHAPES = ("email", "phone_e164", "phone_nanp", "ssn_shape", "luhn16", "key_prefix", "jwt_shape", "uuid", "ipv4",
          "ipv6", "hex16", "hex32", "hex64", "base64_40", "given_name", "surname", "first_last_slug", "unlisted_name",
          "common_password", "random_letters", "pronounceable", "glued_words", "upper_case", "non_ascii_name",
          "dob_date")


def needles(value: str) -> list:
    """The parts of a planted value whose presence in an output is a leak: the value itself and, for shapes made
    around a uniq core, that core (a 10-letter syllable run nothing in the service contains)."""
    out = {value}
    letters = "".join(c for c in value.lower() if c.isalpha())
    for k in range(0, max(0, len(letters) - 9)):
        piece = letters[k:k + 10]
        if all((piece[i] in CONS) == (i % 2 == 0) for i in range(10)):
            out.add(piece)
            break
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) >= 9 and len(digits) * 10 >= len(value) * 7:     # a numeric shape: its digits-only form
        out.add(digits)
    return sorted(out, key=len)


def constants_free(value: str, constants: str) -> bool:
    return all(n.lower() not in constants for n in needles(value))
