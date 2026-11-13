"""Seeded generators of priya-spec/1: the matrix class x disguise x context x language (section 2, M-1), the
boundary set (M-2), structural composed pairs (section 3) and the ordinary-query corpus (section 5). Every value is
synthetic, made at run time (valid Luhn / issuer prefix, SSN parts, NANP rules, made-up mailboxes); nothing personal-
data shaped is written here. Each generator is a pure function of (seed, index): ``random.Random(f"{seed}:{key}")``.

Built by the round-19 builder from the spec text and the builder's own round-18 / round-19 generators
(``privacy_r18_corpus``, ``privacy_r19_corpus``); the reviewer's harnesses were not used, so the boundary set's size is
this module's own (``len(boundary())``), not the 70,346 of the spec's reference harness."""

from __future__ import annotations

import base64
import codecs
import random
import string
import unicodedata

import privacy_r18_corpus as rc
import privacy_r19_corpus as r19

J = "".join
SEED = 190001

# ------------------------------------------------------------------ classes (C1-C7)
CLASSES = ("C1", "C2", "C3", "C4", "C5", "C6", "C7")
DIGIT_GEN = {"C1": rc.card, "C2": rc.nanp, "C3": rc.e164, "C5": rc.ssn}
CLS_NAME = {"C1": "card", "C2": "phone", "C3": "phone_intl", "C5": "ssn"}

# ------------------------------------------------------------------ context X0-X12 (12 languages)
LANGS = ("en", "es", "fr", "de", "pt", "it", "ja", "zh", "ko", "ru", "ar", "hi")
_BEFORE = {
    "en": ("call me", "my number is", "rug cleaner", "contact"), "es": ("llamame al", "mi numero", "contacto"),
    "fr": ("appelez", "mon numero", "contact"), "de": ("ruf mich an", "meine nummer", "kontakt bitte"),
    "pt": ("ligue para", "meu numero", "contato"), "it": ("chiamami", "il mio numero", "contatto"),
    "ja": ("\u96fb\u8a71", "\u9023\u7d61\u5148", "\u30e1\u30fc\u30eb"), "zh": ("\u7535\u8bdd", "\u8054\u7cfb"),
    "ko": ("\uc804\ud654", "\uc5f0\ub77d\ucc98"), "ru": ("\u043f\u043e\u0437\u0432\u043e\u043d\u0438",
                                                           "\u043d\u043e\u043c\u0435\u0440"),
    "ar": ("\u0627\u062a\u0635\u0644", "\u0631\u0642\u0645\u064a"), "hi": ("\u0915\u0949\u0932", "\u0928\u0902\u092c\u0930")}
_AFTER = {
    "en": ("tonight", "thanks", "asap", "near me"), "es": ("gracias", "hoy", "por favor"), "fr": ("merci", "ce soir"),
    "de": ("danke", "heute"), "pt": ("obrigado", "hoje"), "it": ("grazie", "stasera"),
    "ja": ("\u3067\u3059", "\u304a\u9858\u3044\u3057\u307e\u3059"), "zh": ("\u8c22\u8c22", "\u4eca\u5929"),
    "ko": ("\uac10\uc0ac\ud569\ub2c8\ub2e4",), "ru": ("\u0441\u043f\u0430\u0441\u0438\u0431\u043e",),
    "ar": ("\u0634\u0643\u0631\u0627",), "hi": ("\u0927\u0928\u094d\u092f\u0935\u093e\u0926",)}
_JOIN = (" ", " ", "\t", ": ", " - ", ", ", "\u3001", " \U0001f600 ", "\u300c", " (")
CONTEXTS = ("X0",) + tuple(f"X{i + 1}" for i in range(12))


def in_context(rng, x: str, t: str) -> str:
    if x == "X0":
        return t
    lang = LANGS[int(x[1:]) - 1]
    b, a = rng.choice(_BEFORE[lang]), rng.choice(_AFTER[lang])
    j1, j2 = rng.choice(_JOIN), rng.choice(_JOIN)
    m = rng.randrange(3)
    return (b + j1 + t) if m == 0 else (t + j2 + a) if m == 1 else (b + j1 + t + j2 + a)


# ------------------------------------------------------------------ digit disguises (C1-C3, C5)
OTHER_SCRIPT_LETTERS = ("\u0430", "\u0431", "\u0436", "\u03b1", "\u03bb", "\u05d0", "\u05d1", "\u0627", "\u0628",
                        "\u0645")
NFKC_LETTERS = tuple(c for c in rc.NFKC_LETTERS if unicodedata.category(c).startswith("L"))


def _digits_form(rng, cls: str, d: str, pool, every: bool = False, exact: int = 0) -> str:
    """The value's digits (the '+' of E.164 first) with gaps from ``pool`` at some / every digit boundary."""
    out = ["+"] if cls == "C3" else []
    for i, c in enumerate(d):
        out.append(c)
        if i + 1 < len(d) and (every or rng.random() < 0.5):
            out.append(J(rng.choice(pool) for _ in range(exact or rng.randint(1, 3))))
    return J(out)


def _spell(rng, t: str, words, sep: str, keep: float = 0.0) -> str:
    out = []
    for c in t:
        if "0" <= c <= "9":
            out.append(c if rng.random() < keep else words[int(c)])
        elif c.strip():
            out.append(c)
    return sep.join(out)


def digit_disguise(rng, cls: str, dname: str, y: str, d: str) -> str:
    plain = rc._plain_digits(rng, CLS_NAME[cls], d)
    words = rc.ES if y == "es" else rc.EN
    if dname == "D01":
        return plain
    if dname == "D02":
        return _digits_form(rng, cls, d, string.ascii_letters, every=rng.random() < 0.5)
    if dname == "D03":
        return _digits_form(rng, cls, d, string.ascii_letters + "-./ *", every=True, exact=3)
    if dname == "D04":
        return _digits_form(rng, cls, d, NFKC_LETTERS)
    if dname == "D05":
        return _digits_form(rng, cls, d, OTHER_SCRIPT_LETTERS)
    if dname == "D06":
        return _digits_form(rng, cls, d, rc.SEPARATORS)
    if dname == "D07":
        pool = string.ascii_letters + J(rc.SEPARATORS[::9])
        return _digits_form(rng, cls, d, pool, every=rng.random() < 0.5, exact=3 if rng.random() < 0.5 else 0)
    if dname == "D08":
        z = rng.choice(rc.ND_ZEROS)
        return J(chr(z + int(c)) if c.isdigit() else c for c in plain)
    if dname == "D09":
        return rc._fullwidth(plain)
    if dname == "D10":
        return rc._invisible(rng, plain)
    if dname == "D11":
        return _spell(rng, plain, rc.EN, rng.choice((" ", ", ", " ")))
    if dname == "D12":
        return _spell(rng, plain, rc.ES, rng.choice((" ", ", ", " ")))
    if dname == "D13":
        mode = rng.randrange(3)
        t = _spell(rng, plain, words, "-" if mode == 0 else "" if mode == 1 else " ")
        return t.upper() if mode == 2 or rng.random() < 0.3 else t
    if dname == "D14":
        if rng.random() < 0.5:
            return _spell(rng, plain, words, rng.choice((" ", "")), keep=0.5)
        return r19.one_word(rng, CLS_NAME[cls], d)
    raise KeyError(dname)


DIGIT_D = ("D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09", "D10", "D11", "D12", "D13", "D14")


# ------------------------------------------------------------------ vanity (C4)
def _vanity_plain(rng) -> str:
    return rc.vanity_form(rng, rc.vanity(rng))


def vanity_disguise(rng, dname: str, y: str) -> str:
    words = rc.ES if y == "es" else rc.EN
    if dname == "D01":
        return _vanity_plain(rng)
    if dname == "D06":
        return r19.vanity_sep_in_code(rng) if rng.random() < 0.5 else _vanity_plain(rng)
    if dname == "D08":
        z = rng.choice(rc.ND_ZEROS)
        return J(chr(z + int(c)) if c.isdigit() else c for c in _vanity_plain(rng))
    if dname == "D09":
        return rc._fullwidth(_vanity_plain(rng))
    if dname == "D10":
        return rc._invisible(rng, _vanity_plain(rng))
    if dname in ("D11", "D12"):
        return r19.vanity_code_words(rng, rc.EN if dname == "D11" else rc.ES)
    if dname == "D14":                                   # one number word in the code
        code = rng.choice(r19.VANITY_CODES)
        i = rng.randrange(3)
        parts = [c for c in code]
        parts[i] = " " + words[int(code[i])] + " "
        return " ".join(J(("1 " if rng.random() < 0.5 else "",) + tuple(parts)).split()) + " " + r19._vanity_tail(rng)
    if dname == "D15":
        return r19.vanity_keypad_spaced(rng) if rng.random() < 0.5 else r19.vanity_letter_gap_code(rng)
    raise KeyError(dname)


VANITY_D = ("D01", "D06", "D08", "D09", "D10", "D11", "D12", "D14", "D15")

# ------------------------------------------------------------------ e-mail (C6)
EMAIL_Y = {"en": ("at", "dot"), "es": ("arroba", "punto"), "pt": ("arroba", "ponto"), "fr": ("arobase", "dot"),
           "it": ("chiocciola", "punto"), "de": ("klammeraffe", "punkt")}


def _mailbox(rng) -> str:
    return r19.email_unlisted(rng) if rng.random() < 0.5 else rc.email(rng)


def _spelled(rng, v: str, y: str, bracketed: bool) -> str:
    at, dot = EMAIL_Y[y]
    case = rng.choice((str.lower, str.upper, str.title))
    local, dom = v.split("@")
    if bracketed:
        o, c = rng.choice((("[", "]"), ("(", ")"), ("{", "}"), ("<", ">")))
        at_s = rng.choice((f" {o}{case(at)}{c} ", f"{o}@{c}", f" {o} {case(at)} {c} "))
        dot_s = rng.choice((f" {o}{case(dot)}{c} ", f"{o}.{c}", f"{o}{case(dot)}{c}"))
    else:
        at_s, dot_s = f" {case(at)} ", f" {case(dot)} "
    return " ".join((local + at_s + dot_s.join(dom.split("."))).split())


def _sep_spelled(rng, v: str, y: str) -> str:
    at, dot = EMAIL_Y[y]
    s = rng.choice(("_", "-", " _", "- "))
    local, dom = v.split("@")
    return local + s + at + s + (s + dot + s).join(dom.split("."))


def _encode(rng, v: str) -> str:
    return rng.choice((rc._b64, rc._b32, rc._hex, rc._urlenc))(rng, v)


def _rot_rev(rng, v: str) -> str:
    return rng.choice((lambda t: codecs.encode(t, "rot13"), lambda t: t[::-1],
                       lambda t: " ".join(w[::-1] for w in t.split(" "))))(v)


def email_disguise(rng, dname: str, y: str, x: str) -> str:
    v = _mailbox(rng)
    if y not in EMAIL_Y:
        y = rng.choice(tuple(EMAIL_Y))
    if dname == "D01":
        return in_context(rng, x, v)
    if dname == "D09":
        return in_context(rng, x, rc._fullwidth(v))
    if dname == "D10":
        return in_context(rng, x, rc._invisible(rng, v))
    if dname == "D16":
        return in_context(rng, x, _spelled(rng, v, y, False))
    if dname == "D17":
        return in_context(rng, x, _spelled(rng, v, y, True))
    if dname == "D18":
        return in_context(rng, x, _sep_spelled(rng, v, y))
    if dname == "D19":
        return in_context(rng, x, _encode(rng, v))
    if dname == "D20":
        return in_context(rng, x, _rot_rev(rng, v))
    if dname == "D21":
        base = rng.choice((lambda: v, lambda: _spelled(rng, v, y, False), lambda: _spelled(rng, v, y, True),
                           lambda: _encode(rng, v), lambda: _rot_rev(rng, v)))()
        return in_context(rng, x, rc.chunk(rng, base))
    if dname == "D22":                                   # the WHOLE query (context included) rot13'd / reversed
        q = in_context(rng, x, rng.choice((v, _spelled(rng, v, "en", False))))
        return rng.choice((lambda t: codecs.encode(t, "rot13"), lambda t: t[::-1],
                           lambda t: " ".join(w[::-1] for w in t.split(" "))))(q)
    raise KeyError(dname)


EMAIL_D = ("D01", "D09", "D10", "D16", "D17", "D18", "D19", "D20", "D21", "D22")
EMAIL_WORD_D = ("D16", "D17", "D18")

# ------------------------------------------------------------------ address (C7)
def address_disguise(rng, dname: str) -> str:
    a = rc.address(rng)
    if dname == "D01":
        return a
    if dname == "D08":
        z = rng.choice(rc.ND_ZEROS)
        return J(chr(z + int(c)) if c.isdigit() else c for c in a)
    if dname == "D09":
        return rc._fullwidth(a)
    if dname == "D10":
        return rc._invisible(rng, a)
    if dname == "D23":
        return rng.choice((str.upper, rc._fullwidth, lambda t: J(rng.choice(_NFKC_OF.get(c, (c,))) for c in t),
                           lambda t: rc._invisible(rng, t.upper())))(a)
    if dname == "D24":
        street = r19.address_long_tail(rng).split(",")[0].split(" - ")[0]
        tail = " ".join(rng.choice(r19._TAIL_WORDS) for _ in range(rng.randint(0, 8)))
        return (street + " " + tail).strip() if rng.random() < 0.7 else (a + " " + tail).strip()
    raise KeyError(dname)


_NFKC_OF: dict = {}
for _c in NFKC_LETTERS:
    _NFKC_OF.setdefault(unicodedata.normalize("NFKC", _c).lower(), []).append(_c)
ADDRESS_D = ("D01", "D08", "D09", "D10", "D23", "D24")


# ------------------------------------------------------------------ the matrix (M-1)
def cells() -> list:
    """Every applicable (C, D, X, Y) cell of section 2 (Y is "-" where the disguise has no words)."""
    out = []
    for c in ("C1", "C2", "C3", "C5"):
        for d in DIGIT_D:
            ys = ("en", "es") if d in ("D13", "D14") else ("-",)
            out += [(c, d, x, y) for x in CONTEXTS for y in ys]
    for d in VANITY_D:
        ys = ("en", "es") if d == "D14" else ("-",)
        out += [("C4", d, x, y) for x in CONTEXTS for y in ys]
    for d in EMAIL_D:
        ys = tuple(EMAIL_Y) if d in EMAIL_WORD_D else ("-",)
        out += [("C6", d, x, y) for x in CONTEXTS for y in ys]
    out += [("C7", d, x, "-") for d in ADDRESS_D for x in CONTEXTS]
    return out


def cell_rng(cell, k: int, seed: int = SEED) -> random.Random:
    return random.Random(f"{seed}:{':'.join(cell)}:{k}")


def make(cell, rng) -> str:
    c, d, x, y = cell
    if c in DIGIT_GEN:
        return in_context(rng, x, digit_disguise(rng, c, d, y, DIGIT_GEN[c](rng)))
    if c == "C4":
        return in_context(rng, x, vanity_disguise(rng, d, y))
    if c == "C6":
        return email_disguise(rng, d, y, x)
    return in_context(rng, x, address_disguise(rng, d))


def matrix(n: int):
    """Yield (cell, k, text): ``n`` values per cell."""
    for cell in cells():
        for k in range(n):
            yield cell, k, make(cell, cell_rng(cell, k))


# ------------------------------------------------------------------ the boundary set (M-2), enumerated in full
INVISIBLE_LIST = ("\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
                  "\u2060", "\u2061", "\u2062", "\u2063", "\u2064", "\ufeff", "\u00ad", "\u2066", "\u2067", "\u2068",
                  "\u2069", "\u061c", "\u180e", "\u034f")
VANITY_CODES = ("800", "833", "844", "855", "866", "877", "888")


def _bvalues(cls: str, k: int) -> list:
    rng = random.Random(f"{SEED}:bnd:{cls}")
    if cls == "C6":
        return [rc.email(rng) if i % 2 else r19.email_unlisted(rng) for i in range(k)]
    return [DIGIT_GEN[cls](rng) for _ in range(k)]


def _uatp(rng) -> str:
    body = "1" + J(str(rng.randint(0, 9)) for _ in range(13))
    return body + rc._luhn_digit(body)


def _diners36(rng) -> str:
    body = "36" + J(str(rng.randint(0, 9)) for _ in range(11))
    return body + rc._luhn_digit(body)


def boundary():
    """Yield (family, text) for every case of the boundary set; every case must be dropped."""
    gap3 = (string.ascii_lowercase, string.ascii_uppercase, "-./*~ ", string.ascii_lowercase + "-. ",
            NFKC_LETTERS[:200], OTHER_SCRIPT_LETTERS)
    for cls in ("C1", "C2", "C3", "C5"):
        for d in _bvalues(cls, 12):
            for gi, pool in enumerate(gap3):
                rng = random.Random(f"{SEED}:g3:{cls}:{d}:{gi}")
                yield "gap_exactly_3", _digits_form(rng, cls, d, pool, every=True, exact=3)
                yield "gap_mixed_1_3", _digits_form(rng, cls, d, J(pool) + "x-", every=True)
        d = _bvalues(cls, 1)[0]
        plain = rc._plain_digits(random.Random(1), CLS_NAME[cls], d)
        for z in rc.ND_ZEROS:
            yield "nd_block", J(chr(z + int(c)) if c.isdigit() else c for c in plain)
        for ch in INVISIBLE_LIST:
            yield "invisible", ch.join(plain)
        for words in (rc.EN, rc.ES):
            for sep in ("-", " ", "", ", "):
                yield "number_words", _spell(random.Random(2), plain, words, sep)
            for i in range(len(d)):
                t = d[:i] + " " + words[int(d[i])] + " " + d[i + 1:]
                yield "one_number_word", ("+" if cls == "C3" else "") + " ".join(t.split())
        for pre in ("+", "+x", "+xy", "+x-y") if cls == "C3" else ():
            yield "plus_gap_0_3", pre + d
    rng = random.Random(f"{SEED}:e164")
    for n in (8, 15):
        for _ in range(20):
            d = str(rng.randint(1, 9)) + J(str(rng.randint(0, 9)) for _ in range(n - 1))
            yield f"e164_{n}", "+" + d
            yield f"e164_{n}", "+" + "x".join((d[:3], d[3:6], d[6:]))
    for _ in range(20):
        u, di = _uatp(rng), _diners36(rng)
        for t in (u, " ".join((u[:4], u[4:10], u[10:])), di, " ".join(di[i:i + 4] for i in range(0, 14, 4))):
            yield "uatp_diners36", t
    for code in VANITY_CODES:
        for lead in ("", "1", "1-", "1 "):
            for w in ("flowers", "callnow", "getcash", "plumb3r"):
                yield "vanity_code", f"{lead}{code}-{w}"
                yield "vanity_code", f"{lead}{code}{w}"
    for v in _bvalues("C6", 24):
        raw = v.encode()
        forms = (base64.b64encode(raw).decode(), base64.b64encode(raw).decode().rstrip("="),
                 base64.urlsafe_b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode().rstrip("="),
                 base64.b32encode(raw).decode(), base64.b32encode(raw).decode().rstrip("="),
                 base64.b32encode(raw).decode().lower(), base64.b32encode(raw).decode().rstrip("=").lower(),
                 raw.hex(), raw.hex().upper(), J(f"%{b:02x}" for b in raw), v.replace("@", "%40"))
        for f in forms:
            yield "encoding", f
            for i in range(1, len(f)):
                yield "chunk_every_position", f[:i] + " " + f[i:]
        for f in (_spelled(random.Random(4), v, "es", False), _spelled(random.Random(5), v, "de", True),
                  _sep_spelled(random.Random(6), v, "en")):
            for i in range(1, len(f)):
                if f[i - 1] != " " and f[i] != " ":
                    yield "chunk_every_position", f[:i] + " " + f[i:]
        for t in (v, rc._spelled_email(random.Random(3), v)):
            q = "rug cleaner " + t + " tonight"
            for view in (codecs.encode(q, "rot13"), q[::-1], " ".join(w[::-1] for w in q.split())):
                yield "whole_query_rot13_reversal", view


# ------------------------------------------------------------------ composed pairs (section 3), structural
DTX2 = ("gap_ascii", "gap_nfkc", "gap_other", "gap_sep", "script_digits", "fullwidth", "zw_bidi", "spelled_en",
        "spelled_es", "spelled_run", "spelled_hyphen", "spelled_mixed")
FAMILY = {"gap_ascii": "gap", "gap_nfkc": "gap", "gap_other": "gap", "gap_sep": "gap", "script_digits": "glyph",
          "fullwidth": "glyph", "zw_bidi": "zw", "spelled_en": "spelled", "spelled_es": "spelled",
          "spelled_run": "spelled", "spelled_hyphen": "spelled", "spelled_mixed": "spelled",
          "spelled_any": "e_spelled", "bracketed_any": "e_spelled", "sep_spelled": "e_spelled"}
_GAP_POOL = {"gap_ascii": string.ascii_letters, "gap_nfkc": NFKC_LETTERS, "gap_other": OTHER_SCRIPT_LETTERS,
             "gap_sep": rc.SEPARATORS}


def _cp_apply(rng, name: str, lead: str, units: list, gaps: list):
    """CP-3: a value is units and gaps; a gap is only ever between units and at most 3 characters; a spelled
    transform's joiner fills only empty gaps, so a number word may touch gap letters."""
    if name in _GAP_POOL:
        pool = _GAP_POOL[name]
        gaps = [J(rng.choice(pool) for _ in range(rng.randint(1, 3))) if rng.random() < 0.6 else g for g in gaps]
    elif name == "script_digits":
        z = rng.choice(rc.ND_ZEROS)
        units = [chr(z + int(u)) if len(u) == 1 and u.isascii() and u.isdigit() else u for u in units]
    elif name == "fullwidth":
        units, gaps, lead = [rc._fullwidth(u) for u in units], [rc._fullwidth(g) for g in gaps], rc._fullwidth(lead)
    elif name == "zw_bidi":
        units = [rc._invisible(rng, u) for u in units]
    elif name.startswith("spelled"):
        words = rc.ES if name == "spelled_es" or (name != "spelled_en" and rng.random() < 0.5) else rc.EN
        joiner = {"spelled_run": "", "spelled_hyphen": "-"}.get(name, " ")
        frac = 0.5 if name == "spelled_mixed" else 1.0

        def word(u):
            core = u.strip(J(rc.INVISIBLE))
            if core.isascii() and core.isdigit() and len(core) == 1 and rng.random() < frac:
                return u.replace(core, words[int(core)])
            return u
        units = [word(u) for u in units]
        gaps = [g if g else joiner for g in gaps]
    return lead, units, gaps


def _cp_start(rng, cls: str, d: str):
    t = rc._plain_digits(rng, CLS_NAME[cls], d)
    units, gaps, cur, lead = [], [], "", ""
    for c in t:
        if c.isdigit():
            if units:
                gaps.append(cur)
            else:
                lead = cur
            units.append(c)
            cur = ""
        else:
            cur += c
    return lead, units, gaps


E_TX = ("spelled_any", "bracketed_any", "sep_spelled", "base64", "base32", "hex", "url_encoded", "rot13", "reversed",
        "chunked")
V_TX = ("vanity_code_words_en", "vanity_code_words_es", "vanity_keypad_spaced", "vanity_letter_gap_code",
        "vanity_sep_in_code", "fullwidth", "zw_bidi", "context_glued")


def cp_pairs() -> list:
    """Every ordered pair (class, a, b), a != b, not within one family."""
    out = []
    for c in ("C1", "C2", "C3", "C5"):
        out += [(c, a, b) for a in DTX2 for b in DTX2 if a != b and FAMILY[a] != FAMILY[b]]
    gen = set(r19.VANITY_R19)
    out += [("C4", a, b) for a in V_TX for b in V_TX if a != b and not (a in gen and b in gen)]
    out += [("C6", a, b) for a in E_TX for b in E_TX if a != b and FAMILY.get(a, a) != FAMILY.get(b, b)]
    return out


def _etx(rng, name: str, t: str) -> str:
    if name in ("spelled_any", "bracketed_any"):
        y = rng.choice(tuple(EMAIL_Y))
        return _spelled(rng, t, y, name == "bracketed_any") if t.count("@") == 1 else rc._spelled_email(rng, t)
    if name == "sep_spelled":
        return _sep_spelled(rng, t, rng.choice(tuple(EMAIL_Y))) if t.count("@") == 1 else t
    return rc.EMAIL_TRANSFORMS["zero_width_bidi" if name == "zw_bidi" else name](rng, t)


def _vtx(rng, name: str, t):
    if t is None:
        return r19.VANITY_R19[name](rng) if name in r19.VANITY_R19 else _vanity_plain(rng)
    if name == "fullwidth":
        return rc._fullwidth(t)
    if name == "zw_bidi":
        return rc._invisible(rng, t)
    if name == "context_glued":
        return in_context(rng, f"X{rng.randint(1, 12)}", t)
    return t


def composed(n: int, seed: int = SEED + 3):
    """Yield (class, "a+b", text): ``n`` cases, the pairs of ``cp_pairs`` in turn; a quarter in context X1-X12."""
    pairs = cp_pairs()
    for i in range(n):
        c, a, b = pairs[i % len(pairs)]
        rng = random.Random(f"{seed}:{i}")
        if c == "C6":
            v = _mailbox(rng)
            t = _etx(rng, b, _etx(rng, a, v))
        elif c == "C4":
            first = a if a in r19.VANITY_R19 else (b if b in r19.VANITY_R19 else None)
            t = _vtx(rng, first or "plain", None)
            for name in (a, b):
                if name != first:
                    t = _vtx(rng, name, t)
        else:
            lead, units, gaps = _cp_start(rng, c, DIGIT_GEN[c](rng))
            for name in (a, b):
                lead, units, gaps = _cp_apply(rng, name, lead, units, gaps)
            t = lead + J(u + (gaps[j] if j < len(gaps) else "") for j, u in enumerate(units))
        if rng.random() < 0.25:
            t = in_context(rng, f"X{rng.randint(1, 12)}", t)
        yield c, f"{a}+{b}", t


# ------------------------------------------------------------------ the ordinary-query corpus (section 5, OQ-1)
OQ_SIZES = {"plain_text": 2500, "multilingual": 2500, "question": 2000, "local": 1500, "price": 1200, "size": 1200,
            "tire_size": 900, "model_number": 1500, "date": 1000, "time": 600, "year": 1000, "zip": 1000,
            "zip_year": 800, "version": 1000, "flight": 900, "product_code": 900, "part_number": 1500,
            "business_address": 500, "quantity": 1000, "date_time": 900}
TEXT_CLASSES = ("plain_text", "multilingual", "question", "local")
# heads in the 19 languages of OQ-1 (non-Latin as escapes)
_ML = {"es": "limpieza de alfombras", "fr": "nettoyage tapis", "de": "teppichreinigung", "it": "pulizia tappeti",
       "pt": "limpeza de tapete", "nl": "tapijt reinigen", "sv": "mattv\u00e4tt", "pl": "pranie dywan\u00f3w",
       "tr": "hal\u0131 y\u0131kama", "ru": "\u0447\u0438\u0441\u0442\u043a\u0430 \u043a\u043e\u0432\u0440\u043e\u0432",
       "ar": "\u062a\u0646\u0638\u064a\u0641 \u0627\u0644\u0633\u062c\u0627\u062f",
       "hi": "\u0915\u093e\u0932\u0940\u0928 \u0938\u092b\u093e\u0908",
       "ja": "\u30ab\u30fc\u30da\u30c3\u30c8 \u6383\u9664", "zh": "\u5730\u6bef\u6e05\u6d01",
       "ko": "\uce74\ud3ab \uccad\uc18c", "th": "\u0e0b\u0e31\u0e01\u0e1e\u0e23\u0e21",
       "vi": "gi\u1eb7t th\u1ea3m", "id": "cuci karpet", "el": "\u03ba\u03b1\u03b8\u03b1\u03c1\u03b9\u03c3\u03bc\u03cc\u03c2 \u03c7\u03b1\u03bb\u03b9\u03ce\u03bd"}
_ML_MOD = ("", "precio", "cerca", "pas cher", "g\u00fcnstig", "\u5b89\u3044", "\u0446\u0435\u043d\u0430", "barato",
           "\u0e23\u0e32\u0e04\u0e32", "murah")
_CURRENCY = ("${}", "{} usd", "\u20ac{}", "{} eur", "\u00a3{}", "\u00a5{}")
_SW = ("python", "node", "ios", "android", "windows", "macos", "ubuntu", "java", "chrome", "firefox", "kernel",
       "firmware", "bios", "wordpress", "excel", "minecraft")
_BIZ = ("starbucks", "walmart", "home depot", "target", "cvs", "costco", "mcdonalds", "ups store", "post office")


def _r(rng, a, b):
    return str(rng.randint(a, b))


def _oq_price(rng):
    v = rng.choice((_r(rng, 5, 999), f"{_r(rng, 1, 999)}.99", f"{_r(rng, 1, 9)},{_r(rng, 100, 999)}"))
    return f"{rng.choice(rc._HEADS)} {rng.choice(('under ', 'for ', ''))}{rng.choice(_CURRENCY).format(v)}"


def _oq_model(rng):
    return rng.choice((
        lambda: f"ddr{rng.choice('45')} {rng.choice(('3200', '3600', '5600', '6000', '6400'))} "
                f"{rng.choice(('16gb', '32gb', '64gb'))}",
        lambda: f"rtx {rng.choice(('3060', '4070', '4090', '5080'))} {rng.choice(('ti', 'super', ''))}",
        lambda: f"rx {rng.choice(('6600', '7800', '7900'))} xt", lambda: f"iphone {_r(rng, 11, 16)} pro 256gb",
        lambda: f"galaxy s{_r(rng, 20, 25)} ultra", lambda: f"pixel {_r(rng, 6, 9)} pro",
        lambda: rc._model(rng)))()


def _oq_date(rng):
    y, m, d = rng.randint(1995, 2027), rng.randint(1, 12), rng.randint(1, 28)
    return f"{rng.choice(rc._EVENTS)} " + rng.choice((
        f"{y}-{m:02d}-{d:02d}", f"{m}/{d}/{y}", f"{d}.{m}.{y}", f"{d:02d}/{m:02d}/{y}", f"{y}/{m}/{d}", f"{m}-{d}-{y}",
        f"{rng.choice(rc._MONTHS)} {d} {y}", f"{d} {rng.choice(rc._MONTHS)}", f"{y}{m:02d}{d:02d}"))


def _oq_version(rng):
    v = rng.choice((f"{_r(rng, 1, 30)}.{_r(rng, 0, 20)}", f"{_r(rng, 1, 30)}.{_r(rng, 0, 20)}.{_r(rng, 0, 30)}",
                    f"{_r(rng, 1, 130)}.0.{_r(rng, 1000, 6999)}.{_r(rng, 1, 200)}"))
    return rng.choice((f"{rng.choice(_SW)} {v}", f"version {v}", f"v{v} release notes", f"{v} update"))


def _oq_flight(rng):
    m, d, hh, mm = rng.randint(1, 12), rng.randint(1, 28), rng.randint(0, 23), rng.choice(("00", "15", "30", "45"))
    car, num = rng.choice(("ua", "dl", "aa", "ba", "lh", "af", "kl", "wn", "ek", "qr")), rng.randint(1, 2999)
    return rng.choice((f"flight {m}/{d} {hh}:{mm}", f"flight {car}{num} {m}/{d} {hh}:{mm}", f"{car} {num} status",
                       f"{car}{num} {hh}:{mm}", f"flight {car} {num} {m}/{d}"))


def _oq_product(rng):
    r = rng.random()
    if r < 0.4:
        u = r19._check_upc(J(str(rng.randint(0, 9)) for _ in range(11)))
        return rng.choice((f"upc {u}", f"{rng.choice(rc._BRANDS)} upc {u}", f"barcode {u} {rng.choice(rc._HEADS)}"))
    if r < 0.8:
        e = r19._check_ean13(rng.choice(("978", "979")) + J(str(rng.randint(0, 9)) for _ in range(9)))
        return rng.choice((f"isbn {e}", f"{e} paperback", f"book isbn {e}"))
    return r19._sku(rng)


def _oq_business(rng):
    a = rc.address(rng)
    return rng.choice((f"{rng.choice(_BIZ)} {a}", f"{a} {rng.choice(_BIZ)}", f"{rng.choice(_BIZ)} near {a}"))


OQ_MAKERS = {
    "plain_text": lambda rng: f"{rng.choice(rc._MODS)} {rng.choice(rc._HEADS)}",
    "multilingual": lambda rng: f"{rng.choice(tuple(_ML.values()))} {rng.choice(_ML_MOD)}",
    "question": lambda rng: rng.choice(("how to clean", "what is the best", "how much does", "where to buy",
                                        "can i use", "is it safe to use", "who makes")) + " " + rng.choice(rc._HEADS),
    "local": lambda rng: f"{rng.choice(rc._HEADS)} near me {rng.choice(('', 'open now', '24 hours'))}",
    "price": _oq_price,
    "size": lambda rng: rc._size(rng),
    "tire_size": lambda rng: r19._tire(rng),
    "model_number": _oq_model,
    "date": _oq_date,
    "time": lambda rng: r19._time(rng),
    "year": lambda rng: f"{_r(rng, 1990, 2026)} {rng.choice(rc._CARS + rc._HEADS)}",
    "zip": lambda rng: f"{rng.choice(rc._HEADS)} {rng.randint(501, 99950):05d}",
    "zip_year": lambda rng: f"{rng.choice(rc._HEADS)} {rng.randint(501, 99950):05d} {_r(rng, 2019, 2026)}",
    "version": _oq_version,
    "flight": _oq_flight,
    "product_code": _oq_product,
    "part_number": lambda rng: f"part number {_r(rng, 10, 99)}-{_r(rng, 1000, 9999)}-{_r(rng, 10, 99)}"
                               if rng.random() < 0.6 else r19._part(rng),
    "business_address": _oq_business,
    "quantity": lambda rng: r19._qty(rng),
    "date_time": lambda rng: r19._date_time(rng),
}


def ordinary(seed: int = SEED + 5):
    """The frozen OQ corpus: yield (class, index, text), exactly ``OQ_SIZES`` per class (24,400)."""
    for cls, n in OQ_SIZES.items():
        for i in range(n):
            yield cls, i, " ".join(OQ_MAKERS[cls](random.Random(f"{seed}:{cls}:{i}")).split())
