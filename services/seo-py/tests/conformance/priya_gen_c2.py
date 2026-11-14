"""Round 21 (priya-spec/1.1 C-2): the FULL glue set of the list, every one of its 24 characters (fullwidth
U+FF01-U+FF0F and U+FF1A-U+FF20, the ideographic comma U+3001 and full stop U+3002) in every C-2 position, for every
class: glued with no space between the value and a context word of Latin, CJK, Arabic or Devanagari script (before,
after, both sides), and as the value's own separators (between the digit groups, the words of an address, the
spelled e-mail's words and keywords, an encoded e-mail's chunks, the vanity code and its letters).

A corpus of its own (``corpora.PERSONAL["c2glue"]``), so the frozen builder generators (``priya_gen``) are
unchanged. Every value is synthetic, made at run time from a seeded RNG: ``random.Random(f"{SEED}:{key}")``."""

from __future__ import annotations

import base64
import random

import privacy_r18_corpus as rc
import privacy_r19_corpus as r19

SEED = 210001
GLUE = tuple(chr(c) for c in (*range(0xFF01, 0xFF01 + 15), *range(0xFF1A, 0xFF1A + 7), 0x3001, 0x3002))
CONTEXT = {"latin": ("thanks", "gracias", "call", "merci"),
           "cjk": ("\u8c22\u8c22", "\u3067\u3059", "\u8054\u7cfb"),
           "arabic": ("\u0634\u0643\u0631\u0627", "\u0631\u0642\u0645\u064a"),
           "devanagari": ("\u0927\u0928\u094d\u092f\u0935\u093e\u0926", "\u0928\u0902\u092c\u0930")}
POSITIONS = ("before", "after", "both")
PER = 2                          # values per (class form, glue character, position, script)


def _groups(rng, cls: str, d: str) -> list:
    if cls == "card":
        return [d[:4], d[4:10], d[10:]] if len(d) == 15 else [d[i:i + 4] for i in range(0, len(d), 4)]
    if cls == "ssn":
        return [d[:3], d[3:5], d[5:]]
    if cls == "e164":
        return ["+" + d[:2], d[2:5], d[5:]]
    return [d[:3], d[3:6], d[6:]]


def _value(rng, form: str, g: str | None) -> str:
    """One value of ``form``; ``g`` (when given) is the value's own separator, a glue character."""
    sep = g if g is not None else " "
    if form in ("card", "nanp", "e164", "ssn"):
        d = {"card": rc.card, "nanp": rc.nanp, "e164": rc.e164, "ssn": rc.ssn}[form](rng)
        if g is None:
            return rc._plain_digits(rng, {"nanp": "phone", "e164": "phone_intl"}.get(form, form), d)
        return sep.join(_groups(rng, form, d))
    if form == "vanity":
        v = rc.vanity(rng)
        return ("1" + sep if rng.random() < 0.5 else "") + v[:3] + sep + v[3:]
    if form == "address":
        return sep.join(rc.address(rng).split())
    v = rc.email(rng) if rng.random() < 0.5 else r19.email_unlisted(rng)
    if form == "email":
        return v
    if form in ("email_spelled", "email_sep"):
        local, dom = v.split("@")
        at = rng.choice(("at", "arroba", "chiocciola", "kukac", "sobaka"))
        dot = rng.choice(("dot", "punto", "point", "punkt", "titik"))
        if form == "email_sep":
            sep = g if g is not None else rng.choice(("_", "-"))
        return sep.join([local, at] + [x for lab in dom.split(".") for x in (lab, dot)][:-1])
    raw = v.encode()
    enc = {"email_b64": lambda: base64.b64encode(raw).decode().rstrip("=" if rng.random() < 0.5 else ""),
           "email_b32": lambda: base64.b32encode(raw).decode().lower(),
           "email_hex": raw.hex, "email_pct": lambda: "".join(f"%{b:02x}" for b in raw)}[form]()
    if g is None:
        return enc
    cuts = sorted(rng.sample(range(2, len(enc) - 1), 2))
    return g.join((enc[:cuts[0]], enc[cuts[0]:cuts[1]], enc[cuts[1]:]))


FORMS = ("card", "nanp", "e164", "ssn", "vanity", "address", "email", "email_spelled", "email_sep", "email_b64",
         "email_b32", "email_hex", "email_pct")
INSIDE = tuple(f for f in FORMS if f != "email")      # a plain mailbox has no separator of its own


def cases():
    """Yield (key, text): every glue character x every class form x (before / after / both x four scripts, and
    inside the value), ``PER`` values each; every case holds a value of the list and must be dropped."""
    for form in FORMS:
        for gi, g in enumerate(GLUE):
            for script, words in CONTEXT.items():
                for pos in POSITIONS:
                    for k in range(PER):
                        rng = random.Random(f"{SEED}:{form}:{gi}:{script}:{pos}:{k}")
                        v, w = _value(rng, form, None), rng.choice(words)
                        t = (w + g + v) if pos == "before" else (v + g + w) if pos == "after" else (w + g + v + g + w)
                        yield f"{form}|U+{ord(g):04X}|{script}|{pos}", t
            if form in INSIDE:
                for k in range(PER * 2):
                    rng = random.Random(f"{SEED}:{form}:{gi}:inside:{k}")
                    v = _value(rng, form, g)
                    t = v if k % 2 == 0 else rng.choice(CONTEXT["latin"]) + " " + v
                    yield f"{form}|U+{ord(g):04X}|inside", t


VANITY_CODES = ("800", "833", "844", "855", "866", "877", "888")
WORDS = {"en": rc.EN, "es": rc.ES}


def glued_vanity():
    """Yield (key, text): round 21 (priya-spec/1.1 C-4 with D11 / D12 / D14): a vanity number whose code's LAST digit
    is a number word glued to the seven keypad characters, the usual vanity form ("1 8 0 zero" + "flowers"): the
    code in words (D11 English, D12 Spanish), in digits with one number word (D14), spaced or run together, with or
    without the leading 1, any case, alone or after / before a context word. Every case must be dropped."""
    for code in VANITY_CODES:
        for one in ("", "1"):
            for lang, w in WORDS.items():
                for form in ("words", "digits", "tight", "words_tight"):
                    for k in range(6):
                        rng = random.Random(f"{SEED}:vglue:{code}:{one}:{lang}:{form}:{k}")
                        seven = rc.vanity(rng)[3:]
                        seven = "".join(c.upper() if rng.random() < 0.3 else c for c in seven)
                        digits = one + code
                        last = w[int(digits[-1])]
                        head = {"words": " ".join(w[int(x)] for x in digits[:-1]) + " ",
                                "digits": " ".join(digits[:-1]) + " ", "tight": digits[:-1],
                                "words_tight": "".join(w[int(x)] for x in digits[:-1])}[form]
                        t = head + rng.choice((str.lower, str.upper, str.title))(last) + seven
                        x = k % 3
                        t = t if x == 0 else (rng.choice(("call", "llama al")) + " " + t) if x == 1 \
                            else (t + " " + rng.choice(("today", "gracias")))
                        yield f"{form}|{lang}|{one or '-'}", t
