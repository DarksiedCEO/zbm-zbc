"""A table-driven attack corpus for the query classifier (review round 10): CLEARLY FAKE / documented example values
(public test card numbers, AWS's documentation example key, RFC 5737 / RFC 3849 documentation addresses,
example.com, 555-01xx phone numbers) under every obfuscation the reviewers asked for, generated programmatically so
nobody hand-writes a secret. Reviewers can run ``python -m attack_corpus`` from ``tests/`` (with ``src`` on the path)
to print every case the classifier keeps. Values are built from parts so no literal reaches a secret scanner.

``cases()`` yields (base_name, obfuscation_name, text). Not every obfuscation applies to every value; ``SKIPPED``
says which combinations are not generated and why (they are disclosed gaps or meaningless)."""

from __future__ import annotations

J = "".join

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
}

_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_SUPER = "⁰¹²³⁴⁵⁶⁷⁸⁹"
SEPARATORS = {"space": " ", "dot": ".", "dash": "-", "nbsp": " ", "figure_space": " ",
              "narrow_nbsp": " ", "nul": "\x00", "unit_sep": "\x1f", "private_use": "",
              "zero_width_space": "​", "word_joiner": "⁠"}


def _map_digits(t: str, f) -> str:
    return J(f(int(c)) if c.isdigit() else c for c in t)


def _fullwidth(t: str) -> str:
    return J(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in t)


def _math_bold(t: str) -> str:
    out = []
    for c in t:
        if c.isdigit():
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


def _homoglyph(t: str) -> str:
    table = {"a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х",
             "A": "А", "E": "Е", "O": "О", "K": "К", "M": "М", "I": "І"}
    return J(table.get(c, c) for c in t)


def _groups(t: str, n: int = 4) -> list:
    return [t[i:i + n] for i in range(0, len(t), n)]


OBFUSCATIONS = {
    "plain": lambda t, k: t,
    "combining_acute": lambda t, k: J(c + "́" for c in t),
    "combining_keycap": lambda t, k: J(c + "⃣" for c in t),
    "combining_underline": lambda t, k: J(c + "̲" for c in t),
    "zero_width_between": lambda t, k: "​".join(t),
    "bidi_override": lambda t, k: "‮" + t + "‬",
    "bidi_isolates_between": lambda t, k: "⁦".join(t) + "⁩",
    "fullwidth": lambda t, k: _fullwidth(t),
    "math_bold": lambda t, k: _math_bold(t),
    "homoglyph_cyrillic": lambda t, k: _homoglyph(t),
    "circled_digits": lambda t, k: _map_digits(t, _circled),
    "superscript_digits": lambda t, k: _map_digits(t, lambda d: _SUPER[d]),
    "arabic_indic_digits": lambda t, k: _map_digits(t, lambda d: chr(0x0660 + d)),
    "devanagari_digits": lambda t, k: _map_digits(t, lambda d: chr(0x0966 + d)),
    "spelled_out": lambda t, k: " ".join(_WORDS[int(c)] for c in t),
    "reversed": lambda t, k: t[::-1],
    "filler_letters": lambda t, k: " x ".join(_groups(t)),
    "filler_tokens": lambda t, k: "-ab-".join(_groups(t)),
    **{f"sep_{n}_each": (lambda s: lambda t, k: s.join(t))(s) for n, s in SEPARATORS.items()},
    **{f"sep_{n}_groups": (lambda s: lambda t, k: s.join(_groups(t)))(s) for n, s in SEPARATORS.items()},
}

# which obfuscations apply to which kind of value; the rest are not generated (reason in SKIPPED)
_DIGIT_ONLY = {"circled_digits", "superscript_digits", "arabic_indic_digits", "devanagari_digits", "spelled_out",
               "filler_letters", "filler_tokens"}
_SEPARATOR = {n for n in OBFUSCATIONS if n.startswith("sep_")}
APPLIES = {
    "digits": set(OBFUSCATIONS),
    "token": set(OBFUSCATIONS) - {"spelled_out", "filler_letters", "filler_tokens"},
    "email": set(OBFUSCATIONS) - _DIGIT_ONLY,
    "net": set(OBFUSCATIONS) - _SEPARATOR - {"spelled_out", "filler_letters", "filler_tokens"},
}
SKIPPED = {
    "token": "spelled-out words and filler tokens apply to digit-only payloads",
    "email": "digit transforms do not apply; the '@' survives every other obfuscation",
    "net": "separators inside an address destroy it (an address spaced out character by character is not caught "
           "as an address: a disclosed gap, ADR 0047); spelled-out / filler forms apply to digits only",
}
# phones and SSN-shaped values have too few digits for the Luhn card rule, so a filler letter between groups breaks
# the 7-digit run rule: a disclosed gap (ADR 0047)
KNOWN_GAPS = {("phone_555", "filler_letters"), ("ssn_shape", "filler_letters")}


def cases():
    for base, (kind, value) in BASES.items():
        for name, f in OBFUSCATIONS.items():
            if name in APPLIES[kind] and (base, name) not in KNOWN_GAPS:
                yield base, name, f(value, kind)


def gaps():
    for base, name in sorted(KNOWN_GAPS):
        kind, value = BASES[base]
        yield base, name, OBFUSCATIONS[name](value, kind)


if __name__ == "__main__":                                # pragma: no cover - a reviewer's tool
    from agents import search_performance as sp
    kept = [(b, n, t) for b, n, t in cases() if sp.classify_query(t, ["acme"]) != "pii_suspect"]
    print(f"{sum(1 for _ in cases())} cases, {len(kept)} kept")
    for b, n, t in kept:
        print("KEPT", b, n, repr(t))
