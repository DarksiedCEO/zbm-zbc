"""rv-priya20 M-1 matrix: cells (C, D, X, Y) x N values. iter_cases() -> (meta, q); seed 2020 per cell."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya20, file h/g20m.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver removed. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import random

from rvgen import rv20_p20 as P
from rvgen import rv20_p20b as Q

N = 200
DIGIT = ["card", "nanp", "e164", "ssn"]
XS = ["X0"] + ["X%d" % (i + 1) for i in range(12)]


def fmt(r, c, d):
    if c == "card":
        k = r.randrange(4)
        if k == 0:
            return d
        s = r.choice([" ", "-", "  ", "."])
        if k == 1 and len(d) == 15:
            return s.join([d[:4], d[4:10], d[10:]])
        return s.join(d[i:i + 4] for i in range(0, len(d), 4))
    if c == "nanp":
        one, core = (d[0], d[1:]) if len(d) == 11 else ("", d)
        f = r.choice(["{a}{b}{c}", "({a}) {b}-{c}", "{a}-{b}-{c}", "{a}.{b}.{c}", "{a} {b} {c}", "({a}){b}-{c}",
                      "{a}/{b}-{c}"]).format(a=core[:3], b=core[3:6], c=core[6:])
        return (r.choice(["1 ", "1-", "1.", "+1 ", "1"]) + f) if one else f
    if c == "e164":
        k = r.randrange(4)
        if k == 0:
            return "+" + d
        if k == 1:
            return "+" + d[:2] + " " + " ".join(d[i:i + 4] for i in range(2, len(d), 4))
        if k == 2:
            return "+" + d[:3] + "-" + d[3:]
        return "+" + d[0] + " (" + d[1:4] + ") " + d[4:]
    s = r.choice(["", "-", " ", ".", "/"])
    return d[:3] + s + d[3:5] + s + d[5:]


def value(r, c):
    return {"card": P.card, "nanp": P.nanp, "e164": P.e164, "ssn": P.ssn}[c](r)


def plus(c, s):
    return ("+" + s) if c == "e164" else s


def digit_case(r, c, dz, y):
    d = value(r, c)
    if dz == "D01":
        return fmt(r, c, d)
    if dz == "D02":
        return plus(c, P.gap_digits(r, d, "ascii", every=r.random() < 0.5))
    if dz == "D03":
        return plus(c, P.gap_digits(r, d, r.choice(["ascii", "sep", "mixed"]), every=True, exact3=True))
    if dz == "D04":
        return plus(c, P.gap_digits(r, d, "nfkc", every=r.random() < 0.5))
    if dz == "D05":
        return plus(c, P.gap_digits(r, d, "other", every=r.random() < 0.5))
    if dz == "D06":
        return plus(c, P.gap_digits(r, d, "sep", every=r.random() < 0.5))
    if dz == "D07":
        return plus(c, P.gap_digits(r, d, "mixed", every=r.random() < 0.5, exact3=r.random() < 0.5))
    if dz == "D08":
        return P.to_block(fmt(r, c, d), r.choice(P.ND_ZEROS))
    if dz == "D09":
        return P.fw(fmt(r, c, d), digits_only=r.random() < 0.5)
    if dz == "D10":
        return P.zw_in(r, fmt(r, c, d))
    if dz in ("D11", "D12"):
        return plus(c, P.rcase(r, P.spell(r, d, "en" if dz == "D11" else "es")))
    if dz == "D13":
        k = r.randrange(3)
        s = P.spell(r, d, y, joiner=["-", "", " "][k])
        return plus(c, s.upper() if k == 2 or r.random() < 0.3 else s)
    if dz == "D14":
        return plus(c, P.spell_mixed(r, d, y, one=r.random() < 0.5))
    raise ValueError(dz)


def vanity_case(r, dz, y):
    one, code, seven = P.vanity(r)
    s7 = P.rcase(r, seven)
    lead = (one + r.choice(["-", " ", ""])) if one else ""
    if dz == "D01":
        return r.choice(["{l}{c}-{s}", "{l}({c}) {s}", "{l}{c} {s}", "{l}{c}{s}", "{l}{c}.{s}"]).format(
            l=lead, c=code, s=s7)
    if dz == "D06":
        g = lambda: P.gap(r, "sep")  # noqa: E731
        return one + (g() if one else "") + code[0] + g() + code[1] + g() + code[2] + g() + s7
    if dz == "D08":
        return P.to_block(lead + code + "-" + s7, r.choice(P.ND_ZEROS))
    if dz == "D09":
        return P.fw(lead + code + "-" + s7, digits_only=r.random() < 0.5)
    if dz == "D10":
        return P.zw_in(r, lead + code + " " + s7)
    if dz in ("D11", "D12"):
        w = P.NUMW["en" if dz == "D11" else "es"]
        digits = (one + code)
        return P.rcase(r, " ".join(w[int(x)] for x in digits)) + r.choice([" ", "-"]) + s7
    if dz == "D14":
        w = P.NUMW[y]
        digits = list(one + code)
        i = r.randrange(len(digits))
        digits[i] = w[int(digits[i])]
        return r.choice([" ", ""]).join(digits) + " " + s7 if r.random() < 0.5 else " ".join(digits) + s7
    if dz == "D15":
        if r.random() < 0.5:
            return lead + code + " " + " ".join(s7)
        g = lambda: P.gap(r, "ascii")  # noqa: E731
        return "1" + g() + code[0] + g() + code[1] + g() + code[2] + r.choice([" ", "-"]) + s7
    raise ValueError(dz)


def email_case(r, dz, y, idx):
    if dz == "D01":
        e = P.email(r)
        return r.choice(["{e}", "{E}", "mailto:{e}", "<{e}>", "{e}."]).format(e=e, E=e.upper())
    if dz == "D09":
        return P.fw(P.email(r))
    if dz == "D10":
        return P.zw_in(r, P.email(r))
    if dz == "D16":
        return Q.spelled_email(r, at_w=y, dot_w=P.DOT_WORDS[idx % len(P.DOT_WORDS)])
    if dz == "D17":
        return Q.bracket_email(r)
    if dz == "D18":
        return Q.underscore_email(r)
    if dz == "D19":
        return Q.encode_email(r, P.email(r))
    if dz == "D20":
        k = r.randrange(4)
        e = P.email(r)
        if k == 0:
            return Q.rot13(e)
        if k == 1:
            return e[::-1]
        if k == 2:                        # word by word: each label reversed in place
            import re
            return re.sub(r"[^@.]+", lambda m: m.group()[::-1], e)
        return Q.rot13(e.upper())
    if dz == "D21":
        k = r.randrange(5)
        base = [P.email(r), Q.spelled_email(r), Q.bracket_email(r), Q.encode_email(r, P.email(r)),
                Q.rot13(P.email(r))][k]
        return Q.chunk(r, base)
    if dz == "D22":
        return None                       # built on the whole query below
    raise ValueError(dz)


def address_case(r, dz):
    kind, s = Q.address(r)
    if dz == "D01":
        return s
    if dz == "D08":
        return P.to_block(s, r.choice(P.ND_ZEROS))
    if dz == "D09":
        return P.fw(s)
    if dz == "D10":
        return P.zw_in(r, s)
    if dz == "D23":
        k = r.randrange(5)
        if k == 0:
            return s.upper()
        if k == 1:
            return "".join(r.choice([c, chr(0x1D41A + ord(c) - 97)]) if "a" <= c <= "z" else c for c in s)
        if k == 2:
            return P.fw(s)
        if k == 3:
            return P.to_block(s, r.choice(P.ND_ZEROS))
        return P.zw_in(r, s)
    if dz == "D24":
        kind, s = Q.address(r, r.choice(["ordinary", "ordinary", "strong"]))
        return Q.address_in_text(r, s, r.randint(0, 8))
    raise ValueError(dz)


def cells():
    for c in DIGIT:
        for dz in ["D%02d" % i for i in range(1, 15)]:
            for y in (["en", "es"] if dz in ("D13", "D14") else ["-"]):
                yield c, dz, y
    for dz in ["D01", "D06", "D08", "D09", "D10", "D11", "D12", "D14", "D15"]:
        for y in (["en", "es"] if dz == "D14" else ["-"]):
            yield "vanity", dz, y
    for dz in ["D01", "D09", "D10", "D17", "D18", "D19", "D20", "D21", "D22"]:
        yield "email", dz, "-"
    for y in P.AT_WORDS:
        yield "email", "D16", y
    for dz in ["D01", "D08", "D09", "D10", "D23", "D24"]:
        yield "address", dz, "-"


def one(r, c, dz, y, x, i):
    if c in DIGIT:
        v = digit_case(r, c, dz, y)
    elif c == "vanity":
        v = vanity_case(r, dz, y)
    elif c == "email":
        v = email_case(r, dz, y, i)
        if v is None:
            v = P.email(r) if r.random() < 0.7 else P.email(r).upper()
    else:
        v = address_case(r, dz)
    q = v if x == "X0" else Q.in_context(r, Q.LANGS[int(x[1:]) - 1], v)
    if c == "email" and dz == "D22":
        k = r.randrange(3)
        q = Q.rot13(q) if k == 0 else (q[::-1] if k == 1 else " ".join(w[::-1] for w in q.split(" ")))
    return q


def iter_cases():
    for c, dz, y in cells():
        for x in XS:
            r = random.Random("rv20|%s|%s|%s|%s" % (c, dz, y, x))
            for i in range(N):
                yield {"c": c, "d": dz, "y": y, "x": x, "i": i}, one(r, c, dz, y, x, i)
