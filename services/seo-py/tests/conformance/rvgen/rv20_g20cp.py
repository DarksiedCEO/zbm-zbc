"""rv-priya20 composed pairs (CP-1..CP-3): every ordered pair (a, b), a != b, not within one family, per class;
STRUCTURAL: units (digits / number words) and gaps; a gap only between units, never > 3 characters; spelled + gap:
the gap replaces the word joiner. E-mail: b(a(value)). 25% in context. iter_cases() -> (meta, q)."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya20, file h/g20cp.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver removed. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import base64
import random
import re
from urllib.parse import quote

from rvgen import rv20_p20 as P
from rvgen import rv20_p20b as Q

PER = 80
DTX = ["gap_ascii", "gap_nfkc", "gap_other", "gap_sep", "script_digits", "fullwidth", "zw_bidi", "spelled_en",
       "spelled_es", "spelled_run", "spelled_hyphen", "spelled_mixed"]
FAM = {"gap_ascii": "gap", "gap_nfkc": "gap", "gap_other": "gap", "gap_sep": "gap", "script_digits": "scr",
       "fullwidth": "scr", "spelled_en": "sp", "spelled_es": "sp", "spelled_run": "sp", "spelled_hyphen": "sp",
       "spelled_mixed": "sp", "zw_bidi": "zw", "keypad_spaced": "kp"}
VTX = ["gap_sep", "gap_ascii", "script_digits", "fullwidth", "zw_bidi", "spelled_en", "spelled_es", "spelled_mixed",
       "keypad_spaced"]
ETX = ["spelled", "bracket", "underscore", "b64", "b32", "hex", "url", "rot13", "reverse", "wordrev", "chunk"]
EFAM = {"spelled": "sp", "bracket": "sp", "underscore": "sp", "b64": "enc", "b32": "enc", "hex": "enc", "url": "enc"}


def pairs(tx, fam):
    return [(a, b) for a in tx for b in tx if a != b and not (fam.get(a) and fam.get(a) == fam.get(b))]


class V:
    """units: list of [kind, text, digit]; gaps: list of str between units (len(units)-1)."""

    def __init__(self, digits):
        self.u = [["d", c, c] for c in digits]
        self.g = [""] * (len(digits) - 1)
        self.joiner = None

    def render(self):
        out = [self.u[0][1]]
        for i, gp in enumerate(self.g):
            a, b = self.u[i], self.u[i + 1]
            if not gp and (a[0] == "w" or b[0] == "w") and self.joiner is not None:
                gp = self.joiner
            out.append(gp + b[1])
        return "".join(out)


def apply(r, v, t, z):
    n = len(v.g)
    if t.startswith("gap_"):
        kind = t[4:]
        for i in r.sample(range(n), r.randint(1, max(1, n // 2))):
            room = 3 - len(v.g[i])
            if room > 0:
                v.g[i] += P.gap(r, kind)[:room]
    elif t == "zw_bidi":
        for i in r.sample(range(n), r.randint(1, max(1, n // 3))):
            if len(v.g[i]) < 3:
                v.g[i] += r.choice(P.ZW)
    elif t == "script_digits":
        for u in v.u:
            if u[0] == "d":
                u[1] = P.to_block(u[2], z)
    elif t == "fullwidth":
        for u in v.u:
            u[1] = P.fw(u[1])
        v.g = [P.fw(x) for x in v.g]
    elif t.startswith("spelled"):
        lang = {"spelled_en": "en", "spelled_es": "es"}.get(t, r.choice(["en", "es"]))
        idx = range(len(v.u))
        if t == "spelled_mixed":
            idx = r.sample(range(len(v.u)), r.choice([1, 2, max(2, len(v.u) // 2)]))
        for i in idx:
            u = v.u[i]
            if u[0] == "d":
                u[0], u[1] = "w", P.NUMW[lang][int(u[2])]
        v.joiner = {"spelled_run": "", "spelled_hyphen": "-"}.get(t, " " if v.joiner is None else v.joiner)
        if t == "spelled_mixed":
            v.joiner = r.choice([" ", "-", ""])
    elif t == "keypad_spaced":
        pass
    else:
        raise ValueError(t)


def digit_pair(r, c, a, b):
    d = {"card": P.card, "nanp": P.nanp, "e164": P.e164, "ssn": P.ssn}[c](r)
    v = V(d)
    z = r.choice(P.ND_ZEROS)
    apply(r, v, a, z)
    apply(r, v, b, z)
    s = v.render()
    return ("+" + s) if c == "e164" else s


def vanity_pair(r, a, b):
    one, code, seven = P.vanity(r)
    v = V(one + code)
    z = r.choice(P.ND_ZEROS)
    ks = seven
    for t in (a, b):
        if t == "keypad_spaced":
            ks = " ".join(ks)
        else:
            apply(r, v, t, z)
            if t in ("script_digits",):
                ks = P.to_block(ks, z)
            if t == "fullwidth":
                ks = P.fw(ks)
    return v.render() + r.choice([" ", "-"]) + ks


def etx(r, t, s):
    if t == "spelled":
        w_at, w_dot = r.choice(P.AT_WORDS), r.choice(P.DOT_WORDS)
        return s.replace("@", " " + w_at + " ").replace(".", " " + w_dot + " ")
    if t == "bracket":
        return s.replace("@", r.choice(Q.BR_AT)).replace(".", r.choice(Q.BR_DOT))
    if t == "underscore":
        return s.replace("@", r.choice(["_at_", "-at-"])).replace(".", r.choice(["_dot_", "-dot-"]))
    if t == "b64":
        e = base64.b64encode(s.encode()).decode()
        return e if r.random() < 0.5 else e.rstrip("=")
    if t == "b32":
        e = base64.b32encode(s.encode()).decode()
        return e if r.random() < 0.5 else e.lower().rstrip("=")
    if t == "hex":
        return s.encode().hex()
    if t == "url":
        return quote(s, safe="") if r.random() < 0.5 else "".join("%%%02X" % x for x in s.encode())
    if t == "rot13":
        return Q.rot13(s)
    if t == "reverse":
        return s[::-1]
    if t == "wordrev":
        return " ".join(w[::-1] for w in s.split(" ")) if " " in s else re.sub(r"[^@.]+", lambda m: m.group()[::-1], s)
    if t == "chunk":
        return Q.chunk(r, s)
    raise ValueError(t)


def email_pair(r, a, b):
    return etx(r, b, etx(r, a, P.email(r)))


def cases_for(c):
    if c in ("card", "nanp", "e164", "ssn"):
        return pairs(DTX, FAM)
    if c == "vanity":
        return pairs(VTX, FAM)
    return pairs(ETX, EFAM)


def iter_cases():
    for c in ("card", "nanp", "e164", "ssn", "vanity", "email"):
        for a, b in cases_for(c):
            r = random.Random("rv20cp|%s|%s|%s" % (c, a, b))
            for i in range(PER):
                if c == "vanity":
                    q = vanity_pair(r, a, b)
                elif c == "email":
                    q = email_pair(r, a, b)
                else:
                    q = digit_pair(r, c, a, b)
                ctx = r.random() < 0.25
                if ctx:
                    q = Q.in_context(r, r.choice(Q.LANGS), q)
                yield {"c": c, "a": a, "b": b, "ctx": int(ctx), "i": i}, q
