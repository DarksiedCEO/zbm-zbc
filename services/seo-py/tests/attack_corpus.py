"""A table-driven attack corpus for the query classifier (review round 10): CLEARLY FAKE / documented example values
(public test card numbers, AWS's documentation example key, RFC 5737 / RFC 3849 documentation addresses,
example.com, 555-01xx phone numbers) under every obfuscation the reviewers asked for, generated programmatically so
nobody hand-writes a secret. Reviewers can run ``python -m attack_corpus`` from ``tests/`` (with ``src`` on the path)
to print every case the classifier keeps. Values are built from parts so no literal reaches a secret scanner.

``cases()`` yields (base_name, obfuscation_name, text). Not every obfuscation applies to every value; ``SKIPPED``
says which combinations are not generated and why (they are disclosed gaps or meaningless)."""

# ruff: noqa: FLY002 — values are joined from parts on purpose: no secret-shaped literal may reach a scanner
from __future__ import annotations

import base64
import hashlib
import json

J = "".join


def _b64url(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")


def _pct(t: str) -> str:
    return J(f"%{ord(c):02x}" for c in t)

BASES = {
    # kind "digits": digit-only payloads (cards, SSN-shaped, phone)
    "visa_test": ("digits", J(("4111", "1111", "1111", "1111"))),
    "mastercard_test": ("digits", J(("5555", "5555", "5555", "4444"))),
    "amex_test": ("digits", J(("3782", "822463", "10005"))),
    "discover_test": ("digits", J(("6011", "1111", "1111", "1117"))),
    "ssn_shape": ("digits", J(("123", "45", "6789"))),
    "phone_555": ("digits", J(("555", "010", "0199"))),
    # kind "token": secret-shaped words
    "aws_doc_example_key": ("token", J(("AKIA", "IOSFODNN7", "EXAMPLE"))),
    "hex_digest": ("token", J(("9f86d081", "884c7d65", "9a2feaa0", "c55ad015"))),
    # kind "email" / "net": structure-bearing values
    "email_example": ("email", J(("jane.doe", "@", "example.com"))),
    "ipv4_rfc5737": ("net", ".".join(("192", "0", "2", "1"))),
    "ipv6_rfc3849": ("net", J(("2001:db8", "::1"))),
    "url_example": ("net", J(("example.com", "/login"))),
    # round 11: encoded payloads (case-sensitive: only value-preserving obfuscations apply)
    "jwt_example": ("b64", ".".join((_b64url({"alg": "HS256", "typ": "JWT"}),
                                     _b64url({"sub": J(("1234", "567890")), "name": "Jane Doe"}),
                                     J(("SflKxwRJ", "SMeKKF2Q", "T4fwpMeJ", "f36POk6y", "JV_adQssw5c"))))),
    "base64_email": ("b64", base64.b64encode(J(("jane.doe", "@", "example.com")).encode()).decode()),
    "percent_email": ("pct", _pct(J(("jane.doe", "@", "example.com")))),
    "percent_aws_key": ("pct", _pct(J(("AKIA", "IOSFODNN7", "EXAMPLE")))),
}

_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_SUPER = "⁰¹²³⁴⁵⁶⁷⁸⁹"
SEPARATORS = {"space": " ", "dot": ".", "dash": "-", "nbsp": "\u00a0", "figure_space": "\u2007",
              "narrow_nbsp": "\u202f", "nul": "\x00", "unit_sep": "\x1f", "private_use": "\ue000",
              "zero_width_space": "\u200b", "word_joiner": "\u2060",
              # round 16: punctuation / symbol / dash separators the detectors once missed (by category, not a list)
              "bullet": "\u2022", "en_dash": "\u2013", "em_dash": "\u2014", "minus_sign": "\u2212",
              "middle_dot": "\u00b7", "katakana_middle_dot": "\u30fb", "fraction_slash": "\u2044", "tilde": "~",
              "asterisk": "*", "quadruple_prime": "\u2057", "musical_half_note": "\U0001d15e"}


def _map_digits(t: str, f) -> str:
    return J(f(int(c)) if "0" <= c <= "9" else c for c in t)


def _spell(t: str, words, every: int = 1) -> str:
    """Digits spelled out (every ``every``-th one; the rest kept), other characters kept: tolerant of composed input."""
    out, n = [], 0
    for c in t:
        if "0" <= c <= "9":
            out.append(words[int(c)] if n % every == every - 1 or every == 1 else c)
            n += 1
        else:
            out.append(c)
    return " ".join(out)


def _fullwidth(t: str) -> str:
    return J(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in t)


def _math_bold(t: str) -> str:
    out = []
    for c in t:
        if "0" <= c <= "9":
            out.append(chr(0x1D7CE + int(c)))
        elif "A" <= c <= "Z":
            out.append(chr(0x1D400 + ord(c) - 65))
        elif "a" <= c <= "z":
            out.append(chr(0x1D41A + ord(c) - 97))
        else:
            out.append(c)
    return J(out)


def _circled(d: int) -> str:
    return "⓪" if d == 0 else chr(0x2460 + d - 1)


_DIGIT_HOMOGLYPH = {"0": "O", "1": "l", "2": "Z", "5": "S", "8": "B"}
_SPANISH = ("cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve")


def _enclosed(base: int, zero: str):
    return lambda d: zero if d == 0 else chr(base + d - 1)


def _homoglyph(t: str) -> str:
    table = {"a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х",
             "A": "А", "E": "Е", "O": "О", "K": "К", "M": "М", "I": "І"}
    return J(table.get(c, c) for c in t)


def _groups(t: str, n: int = 4) -> list:
    return [t[i:i + n] for i in range(0, len(t), n)]


def _sep_each(sep: str):
    return lambda t, k: sep.join(t)


def _sep_groups(sep: str):
    return lambda t, k: sep.join(_groups(t))


OBFUSCATIONS = {
    "plain": lambda t, k: t,
    "combining_acute": lambda t, k: J(c + "\u0301" for c in t),
    "combining_keycap": lambda t, k: J(c + "\u20e3" for c in t),
    "combining_underline": lambda t, k: J(c + "\u0332" for c in t),
    "zero_width_between": lambda t, k: "\u200b".join(t),
    "bidi_override": lambda t, k: "\u202e" + t + "\u202c",
    "bidi_isolates_between": lambda t, k: "\u2066".join(t) + "\u2069",
    "fullwidth": lambda t, k: _fullwidth(t),
    "math_bold": lambda t, k: _math_bold(t),
    "homoglyph_cyrillic": lambda t, k: _homoglyph(t),
    "circled_digits": lambda t, k: _map_digits(t, _circled),
    "superscript_digits": lambda t, k: _map_digits(t, lambda d: _SUPER[d]),
    "arabic_indic_digits": lambda t, k: _map_digits(t, lambda d: chr(0x0660 + d)),
    "devanagari_digits": lambda t, k: _map_digits(t, lambda d: chr(0x0966 + d)),
    "spelled_out": lambda t, k: _spell(t, _WORDS),
    "reversed": lambda t, k: t[::-1],
    "filler_letters": lambda t, k: " x ".join(_groups(t)),
    # round 11
    "digit_homoglyphs": lambda t, k: J(_DIGIT_HOMOGLYPH.get(c, c) for c in t),
    "dingbat_negative_circled": lambda t, k: _map_digits(t, _enclosed(0x2776, "\u24ff")),
    "dingbat_circled_sans": lambda t, k: _map_digits(t, _enclosed(0x2780, "\u24ea")),
    "dingbat_negative_sans": lambda t, k: _map_digits(t, _enclosed(0x278A, "\u24ff")),
    "double_circled": lambda t, k: _map_digits(t, _enclosed(0x24F5, "\u24ea")),
    "parenthesized": lambda t, k: _map_digits(t, _enclosed(0x2474, "0")),
    "digit_full_stop": lambda t, k: _map_digits(t, _enclosed(0x2488, "0")),
    "spelled_spanish": lambda t, k: _spell(t, _SPANISH),
    "spelled_mixed": lambda t, k: _spell(t, _WORDS, every=2),
    "interleave_char": lambda t, k: "x".join(t),
    "case_change": lambda t, k: J(c.upper() if i % 2 else c.lower() for i, c in enumerate(t)),
    "filler_tokens": lambda t, k: "-ab-".join(_groups(t)),
    **{f"sep_{n}_each": _sep_each(s) for n, s in SEPARATORS.items()},
    **{f"sep_{n}_groups": _sep_groups(s) for n, s in SEPARATORS.items()},
}

# which obfuscations apply to which kind of value; the rest are not generated (reason in SKIPPED)
_DIGIT_ONLY = {"circled_digits", "superscript_digits", "arabic_indic_digits", "devanagari_digits", "spelled_out",
               "filler_letters", "filler_tokens", "digit_homoglyphs", "dingbat_negative_circled",
               "dingbat_circled_sans", "dingbat_negative_sans", "double_circled", "parenthesized", "digit_full_stop",
               "spelled_spanish", "spelled_mixed", "interleave_char"}
_SEPARATOR = {n for n in OBFUSCATIONS if n.startswith("sep_")}
_CASE_OR_LETTERS = {"case_change", "homoglyph_cyrillic", "reversed"}       # change the value of an encoded string
APPLIES = {
    "digits": set(OBFUSCATIONS) - {"case_change"},
    "token": set(OBFUSCATIONS) - _DIGIT_ONLY,
    "email": set(OBFUSCATIONS) - _DIGIT_ONLY,
    "net": set(OBFUSCATIONS) - _SEPARATOR - _DIGIT_ONLY,
    "b64": set(OBFUSCATIONS) - _DIGIT_ONLY - _CASE_OR_LETTERS - _SEPARATOR,
    "pct": set(OBFUSCATIONS) - _DIGIT_ONLY - _SEPARATOR - {"reversed"},
}
SKIPPED = {
    "token": "spelled-out words and filler tokens apply to digit-only payloads",
    "email": "digit transforms do not apply; the '@' survives every other obfuscation",
    "net": "separators inside an address destroy it (an address spaced out character by character is not caught "
           "as an address: a disclosed gap, ADR 0047); spelled-out / filler forms apply to digits only",
    "b64": "case changes, Cyrillic letters, reversal and separators change an encoded value into another one",
    "pct": "separators and reversal destroy the escapes; digit transforms do not apply",
}
# round 10 pinned phone / SSN with filler letters here; round 11 closed them (9+ digits across filler tokens).
KNOWN_GAPS: set = set()


def cases():
    for base, (kind, value) in BASES.items():
        for name, f in OBFUSCATIONS.items():
            if name in APPLIES[kind] and (base, name) not in KNOWN_GAPS:
                yield base, name, f(value, kind)


COMPOSED_EVERY = 11            # the fast sample: a deterministic one-in-11 subset of the ordered pairs


def composed_cases(every: int = 1):
    """Two obfuscations, the second applied to the first's output. ``every=1`` (the default since round 12) is EVERY
    ordered pair of different obfuscations that both apply to the value's kind (18,826 cases, about 10-20 s);
    ``every=COMPOSED_EVERY`` is round 11's deterministic SHA-256-selected sample (about one in 11)."""
    for base, (kind, value) in BASES.items():
        names = sorted(APPLIES[kind])
        for a in names:
            for b in names:
                if a == b:
                    continue
                h = hashlib.sha256(f"{base}|{a}|{b}".encode()).digest()
                if every == 1 or h[0] % every == 0:
                    yield base, f"{a}+{b}", OBFUSCATIONS[b](OBFUSCATIONS[a](value, kind), kind)


def gaps():
    for base, name in sorted(KNOWN_GAPS):
        kind, value = BASES[base]
        yield base, name, OBFUSCATIONS[name](value, kind)


if __name__ == "__main__":                                # pragma: no cover - a reviewer's tool
    from agents import search_performance as sp
    for label, gen in (("single", cases), ("composed", composed_cases)):
        allc = list(gen())
        kept = [(b, n, t) for b, n, t in allc if sp.classify_query(t, ["acme"]) != "pii_suspect"]
        print(f"{label}: {len(allc)} cases, {len(kept)} kept")
        for b, n, t in kept:
            print("KEPT", label, b, n, repr(t))
