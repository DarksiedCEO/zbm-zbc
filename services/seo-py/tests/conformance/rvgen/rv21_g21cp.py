"""rv-priya21 composed pairs (CP-1..CP-4), built STRUCTURALLY: a value is units (digits or number words) and gaps; a
gap only ever lies BETWEEN units, is never longer than 3 characters and never inside a word; a gap transform meeting a
spelled transform replaces the word joiner. Every ordered pair (a, b), a != b, outside one family; each pair equally
often (PER cases); 25% in context X1-X12. E-mail pairs are b(a(value)). iter_cases() -> (meta, q)."""
# Ported into the permanent conformance suite (priya-spec/2 FG-3, CAL-2) from review rv-priya21, file h/g21cp.py
# (sha256 761431e8c1ec869a5ca310b6473f299f34e246d9ea245b5154700e270ef379d9): the reviewer's generator, seeded; synthetic values only, made at run time. Changes: non-ASCII
# literals written as escapes, imports from the package, the command-line driver and environment overrides
# removed (N / PER fixed at the review's defaults). The random call order is unchanged, so the corpus is the
# review's own (sha256 pinned in data/manifest.json).
import random

from rvgen import rv21_p21 as P
from rvgen import rv21_c21 as Q

PER = 75
DTX = ["gap_ascii", "gap_nfkc", "gap_other", "gap_sep", "script_digits", "fullwidth", "zw_bidi", "spelled_en",
       "spelled_es", "spelled_run", "spelled_hyphen", "spelled_mixed"]
DFAM = {"gap_ascii": "gap", "gap_nfkc": "gap", "gap_other": "gap", "gap_sep": "gap", "script_digits": "sf",
        "fullwidth": "sf", "spelled_en": "sp", "spelled_es": "sp", "spelled_run": "sp", "spelled_hyphen": "sp",
        "spelled_mixed": "sp"}
VTX = ["sep_gap", "script_digits", "fullwidth", "zw_bidi", "spelled_en", "spelled_es", "spelled_mixed",
       "keypad_spaced", "letter_gap_code"]
VFAM = {"sep_gap": "gap", "letter_gap_code": "gap", "script_digits": "sf", "fullwidth": "sf", "spelled_en": "sp",
        "spelled_es": "sp", "spelled_mixed": "sp"}
ETX = ["spelled", "bracket", "underscore", "b64", "b64u", "b32", "b32l", "hex", "pct", "rot13", "reverse", "wordrev",
       "chunk"]
EFAM = {"spelled": "mk", "bracket": "mk", "underscore": "mk", "b64": "enc", "b64u": "enc", "b32": "enc",
        "b32l": "enc", "hex": "enc", "pct": "enc", "reverse": "rev", "wordrev": "rev"}


def pairs(tx, fam, valid=lambda a, b: True):
    return [(a, b) for a in tx for b in tx if a != b and (fam.get(a) is None or fam.get(a) != fam.get(b))
            and valid(a, b)]


DPAIRS = pairs(DTX, DFAM)
VPAIRS = pairs(VTX, VFAM)
EPAIRS = pairs(ETX, EFAM, lambda a, b: not (EFAM.get(b) == "mk" and EFAM.get(a) == "enc"))


class V:
    """units (text, is_word) and the gaps between them."""

    def __init__(self, digits):
        self.u = [[c, False] for c in digits]
        self.g = [""] * (len(digits) - 1)

    def render(self):
        out = self.u[0][0]
        for gp, (t, _w) in zip(self.g, self.u[1:]):
            out += gp + t
        return out


def _fwc(c):
    return chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c


def apply_digit(r, v, t, z):
    n = len(v.g)
    if t.startswith("gap_") or t in ("sep_gap", "letter_gap_code"):
        kind = {"gap_ascii": "ascii", "gap_nfkc": "nfkc", "gap_other": "other", "gap_sep": "sep", "sep_gap": "sep",
                "letter_gap_code": "ascii"}[t]
        slots = range(n) if r.random() < 0.4 else r.sample(range(n), r.randint(1, max(1, n // 2)))
        for i in slots:
            v.g[i] = P.gap(r, kind)                         # replaces a word joiner if any
    elif t == "script_digits":
        for x in v.u:
            if not x[1]:
                x[0] = "".join(chr(z + int(c)) if c.isdigit() and c.isascii() else c for c in x[0])
    elif t == "fullwidth":
        for x in v.u:
            x[0] = "".join(_fwc(c) for c in x[0])
        v.g = ["".join(_fwc(c) for c in gp) for gp in v.g]
    elif t == "zw_bidi":
        for i in r.sample(range(n), r.randint(1, max(1, n // 2))):
            if len(v.g[i]) < 3:
                v.g[i] += r.choice(P.ZW)
    elif t.startswith("spelled"):
        lang = "es" if t == "spelled_es" else r.choice(["en", "es"]) if t == "spelled_mixed" else "en"
        idx = range(len(v.u))
        if t == "spelled_mixed":
            idx = r.sample(range(len(v.u)), r.randint(1, max(1, len(v.u) // 2)))
        joiner = {"spelled_run": "", "spelled_hyphen": "-"}.get(t, r.choice([" ", " ", ", "]))
        style = r.choice(["lower", "upper", "title"])
        for i in idx:
            x = v.u[i]
            if x[1]:
                continue
            d = x[0]
            val = int(d.translate({ord(c): str(int(__import__("unicodedata").digit(c))) for c in d if c.isdigit()}))
            w = P.NUMW[lang][val]
            x[0] = w.upper() if style == "upper" else w.title() if style == "title" else w
            x[1] = True
        for i in range(n):                                   # the joiner only where no gap stands
            if (v.u[i][1] or v.u[i + 1][1]) and v.g[i] == "":
                v.g[i] = joiner if t != "spelled_mixed" else r.choice([" ", ""])
    else:
        raise ValueError(t)


def digit_pair(r, c, a, b):
    d = {"card": P.card, "nanp": P.nanp, "e164": P.e164, "ssn": P.ssn}[c](r)
    v = V(d)
    z = r.choice(P.ND_ZEROS)
    apply_digit(r, v, a, z)
    apply_digit(r, v, b, z)
    s = v.render()
    return ("+" + s) if c == "e164" else s


def vanity_pair(r, a, b):
    one, code, seven = P.vanity(r)
    v = V(one + code)
    z = r.choice(P.ND_ZEROS)
    keys = list(seven)
    joint = r.choice([" ", "-", ""])
    for t in (a, b):
        if t in ("keypad_spaced",):
            keys = [k + r.choice([" ", "-", "."]) for k in keys[:-1]] + keys[-1:]
            continue
        if t == "letter_gap_code" and len(v.g) == 0:
            continue
        apply_digit(r, v, t, z)
        if t == "script_digits":
            keys = ["".join(chr(z + int(ch)) if ch.isdigit() and ch.isascii() else ch for ch in k) for k in keys]
        elif t == "fullwidth":
            keys = ["".join(_fwc(ch) for ch in k) for k in keys]
        elif t == "zw_bidi" and r.random() < 0.5:
            i = r.randrange(len(keys) - 1)
            keys[i] += r.choice(P.ZW)
        elif t == "sep_gap" and r.random() < 0.5:
            joint = P.gap(r, "sep")
    return v.render() + joint + P.rcase(r, "".join(keys))


def email_tx(r, t, s):
    if t == "spelled":
        aw, dw = r.choice(P.AT_WORDS), r.choice(P.DOT_WORDS)
        st = r.choice(["lower", "upper", "title"])
        return s.replace("@", " " + Q.kwcase(r, aw, st) + " ").replace(".", " " + Q.kwcase(r, dw, st) + " ")
    if t == "bracket":
        return s.replace("@", r.choice(Q.BR_AT)).replace(".", r.choice(Q.BR_DOT))
    if t == "underscore":
        return s.replace("@", r.choice(["_at_", "-at-", "_AT_"])).replace(".", r.choice(["_dot_", "-dot-"]))
    if t in ("b64", "b64u", "b32", "b32l", "hex", "pct"):
        k = {"b64": r.choice(["b64", "b64np"]), "b64u": r.choice(["b64u", "b64unp"]),
             "b32": r.choice(["b32", "b32np"]), "b32l": r.choice(["b32l", "b32lnp"]), "hex": r.choice(["hex", "HEX"]),
             "pct": r.choice(["pct", "pctpart", "qplus"])}[t]
        return Q.encode(r, s, k)
    if t == "rot13":
        return Q.rot13(s)
    if t == "reverse":
        return s[::-1]
    if t == "wordrev":
        return Q.wordrev(s)
    if t == "chunk":
        return Q.chunk(r, s)
    raise ValueError(t)


def email_pair(r, a, b):
    e = P.email(r, short=r.random() < 0.15)
    return email_tx(r, b, email_tx(r, a, e))


def jobs():
    for c in ("card", "nanp", "e164", "ssn"):
        for a, b in DPAIRS:
            yield c, a, b
    for a, b in VPAIRS:
        yield "vanity", a, b
    for a, b in EPAIRS:
        yield "email", a, b


def iter_cases():
    for c, a, b in jobs():
        r = random.Random("rv21cp|%s|%s|%s" % (c, a, b))
        for i in range(PER):
            if c == "vanity":
                q = vanity_pair(r, a, b)
            elif c == "email":
                q = email_pair(r, a, b)
            else:
                q = digit_pair(r, c, a, b)
            ctx = i % 4 == 0
            if ctx:
                q = Q.in_context(r, Q.LANGS[(i // 4) % 12], q)
            yield {"c": c, "a": a, "b": b, "ctx": int(ctx), "i": i}, q
