"""rv-priya21 M-1 matrix: cells (C, D, X, Y) x N values. iter_cases() -> (meta, q); seed 'rv21|c|d|y|x' per cell."""
# Ported into the permanent conformance suite (priya-spec/2 FG-3, CAL-2) from review rv-priya21, file h/g21m.py
# (sha256 fcfdfbde3399a53c08e58ad3bb6ac2c882dc6c3d68ed93161548ab2119f6add4): the reviewer's generator, seeded; synthetic values only, made at run time. Changes: non-ASCII
# literals written as escapes, imports from the package, the command-line driver and environment overrides
# removed (N / PER fixed at the review's defaults). The random call order is unchanged, so the corpus is the
# review's own (sha256 pinned in data/manifest.json).
import random

from rvgen import rv21_p21 as P
from rvgen import rv21_c21 as Q

N = 200
DIGIT = ["card", "nanp", "e164", "ssn"]
XS = ["X0"] + ["X%d" % (i + 1) for i in range(12)]


def fmt(r, c, d):
    if c == "card":
        s = r.choice([" ", "-", " - ", "/", "_", ""])
        if len(d) == 15 and r.random() < 0.6:
            return s.join([d[:4], d[4:10], d[10:]])
        if len(d) == 14 and r.random() < 0.6:
            return s.join([d[:4], d[4:10], d[10:]])
        if r.random() < 0.2:
            return d
        return s.join(d[i:i + 4] for i in range(0, len(d), 4))
    if c == "nanp":
        one, core = (d[0], d[1:]) if len(d) == 11 else ("", d)
        f = r.choice(["{a} {b} {c}", "({a}){b}.{c}", "{a}{b}{c}", "{a}-{b}{c}", "[{a}] {b}-{c}", "{a}_{b}_{c}",
                      "{a} - {b} - {c}", "{a}.{b}{c}"]).format(a=core[:3], b=core[3:6], c=core[6:])
        return (r.choice(["1", "1 ", "+1", "+1-", "1 (", "+1."]) + f) if one else f
    if c == "e164":
        k = r.randrange(5)
        if k == 0:
            return "+" + d
        if k == 1:
            return "+" + d[:1] + " " + " ".join(d[i:i + 3] for i in range(1, len(d), 3))
        if k == 2:
            return "+" + d[:2] + "." + d[2:5] + "." + d[5:]
        if k == 3:
            return "+" + r.choice([" ", "(", " (", "--", "\u00a0"]) + d          # OI-3: '+' then 1-3 characters
        return "+" + d[:3] + "/" + d[3:]
    s = r.choice(["-", " ", ".", "", "_", " - "])
    return d[:3] + s + d[3:5] + s + d[5:]


def value(r, c):
    return {"card": P.card, "nanp": P.nanp, "e164": P.e164, "ssn": P.ssn}[c](r)


def plus(r, c, s):
    if c != "e164":
        return s
    return "+" + r.choice(["", "", "", " ", "(", "- "]) + s


def digit_case(r, c, dz, y):
    d = value(r, c)
    if dz == "D01":
        return fmt(r, c, d)
    if dz == "D02":
        return plus(r, c, P.gap_digits(r, d, "ascii", every=r.random() < 0.4))
    if dz == "D03":
        return plus(r, c, P.gap_digits(r, d, r.choice(["ascii", "sep", "nfkc", "other"]), every=True, exact3=True))
    if dz == "D04":
        return plus(r, c, P.gap_digits(r, d, "nfkc", every=r.random() < 0.4))
    if dz == "D05":
        return plus(r, c, P.gap_digits(r, d, "other", every=r.random() < 0.4))
    if dz == "D06":
        return plus(r, c, P.gap_digits(r, d, "sep", every=r.random() < 0.4))
    if dz == "D07":
        return plus(r, c, P.gap_digits(r, d, "mixed", every=r.random() < 0.4, exact3=r.random() < 0.5))
    if dz == "D08":
        z = P.ND_ZEROS[r.randrange(len(P.ND_ZEROS))]
        return P.to_block(fmt(r, c, d), z)
    if dz == "D09":
        return P.fw(fmt(r, c, d), digits_only=r.random() < 0.4)
    if dz == "D10":
        return P.zw_in(r, fmt(r, c, d))
    if dz in ("D11", "D12"):
        return plus(r, c, P.rcase(r, P.spell(r, d, "en" if dz == "D11" else "es")))
    if dz == "D13":
        k = r.randrange(4)
        s = P.spell(r, d, y, joiner=["-", "", " ", "--"][k])
        return plus(r, c, s.upper() if r.random() < 0.5 else P.rcase(r, s))
    if dz == "D14":
        return plus(r, c, P.rcase(r, P.spell_mixed(r, d, y, one=r.random() < 0.5)))
    raise ValueError(dz)


def vanity_case(r, dz, y):
    one, code, seven = P.vanity(r)
    s7 = P.rcase(r, seven)
    lead = (one + r.choice([" ", "-", "", "."])) if one else ""
    if dz == "D01":
        return r.choice(["{l}{c} {s}", "{l}{c}-{s}", "{l}{c}{s}", "{l}({c}){s}", "{l}{c}/{s}", "{l}{c} {a}-{b}"]).format(
            l=lead, c=code, s=s7, a=s7[:3], b=s7[3:])
    if dz == "D06":
        g = lambda: P.gap(r, "sep")  # noqa: E731
        return (one + g() if one else "") + code[0] + g() + code[1] + g() + code[2] + g() + \
            "".join(ch + (g() if r.random() < 0.3 else "") for ch in s7)
    if dz == "D08":
        return P.to_block(lead + code + r.choice(["-", " ", ""]) + s7, r.choice(P.ND_ZEROS))
    if dz == "D09":
        return P.fw(lead + code + r.choice(["-", " ", ""]) + s7, digits_only=r.random() < 0.4)
    if dz == "D10":
        return P.zw_in(r, lead + code + r.choice(["-", " ", ""]) + s7)
    if dz in ("D11", "D12"):
        w = P.NUMW["en" if dz == "D11" else "es"]
        j = r.choice([" ", "-", "", " "])
        sp = P.rcase(r, j.join(w[int(x)] for x in one + code))
        return sp + r.choice([" ", "-", "", " / "]) + s7
    if dz == "D14":
        w = P.NUMW[y]
        digits = list(one + code)
        i = r.randrange(len(digits))
        digits[i] = P.rcase(r, w[int(digits[i])])
        return r.choice([" ", "", "-"]).join(digits) + r.choice([" ", "", "-"]) + s7
    if dz == "D15":
        k = r.randrange(3)
        if k == 0:
            return lead + code + r.choice([" ", "-"]) + r.choice([" ", ".", "-", " - "]).join(s7)
        g = lambda: P.gap(r, "ascii")  # noqa: E731
        if k == 1:
            return "1" + g() + code[0] + g() + code[1] + g() + code[2] + r.choice([" ", "-", ""]) + s7
        return (one or "1") + code[0] + g() + code[1:] + " " + s7
    raise ValueError(dz)


def email_case(r, dz, y, idx):
    if dz == "D01":
        e = P.email(r)
        return r.choice(["{e}", "{E}", "({e})", "email:{e}", "{e},", "'{e}'", "{T}"]).format(
            e=e, E=e.upper(), T=e.title())
    if dz == "D09":
        e = P.email(r)
        return P.fw(e) if r.random() < 0.6 else P.fw(e.split("@")[0]) + "\uff20" + e.split("@")[1]
    if dz == "D10":
        return P.zw_in(r, P.email(r))
    if dz == "D16":
        return Q.spelled_email(r, at_w=y, dot_w=P.DOT_WORDS[idx % len(P.DOT_WORDS)])
    if dz == "D17":
        return Q.bracket_email(r)
    if dz == "D18":
        return Q.underscore_email(r)
    if dz == "D19":
        return Q.encode(r, P.email(r, short=r.random() < 0.2))
    if dz == "D20":
        e = P.email(r, short=r.random() < 0.15)
        k = r.randrange(5)
        return [Q.rot13(e), e[::-1], Q.wordrev(e), Q.rot13(e.upper()), e.title()[::-1]][k]
    if dz == "D21":
        k = r.randrange(5)
        base = [P.email(r), Q.spelled_email(r), Q.bracket_email(r), Q.encode(r, P.email(r)),
                Q.rot13(P.email(r))][k]
        return Q.chunk(r, base, size=1 if r.random() < 0.3 else None)
    raise ValueError(dz)


def address_case(r, dz):
    kind, s = Q.address(r)
    if dz == "D01":
        return P.rcase(r, s) if r.random() < 0.3 else s
    if dz == "D08":
        return P.to_block(s, r.choice(P.ND_ZEROS))
    if dz == "D09":
        return P.fw(s)
    if dz == "D10":
        return P.zw_in(r, s)
    if dz == "D23":
        k = r.randrange(6)
        if k == 0:
            return s.upper()
        if k == 1:                    # mathematical sans-serif / double-struck lower letters
            base = r.choice([0x1D5BA, 0x1D552, 0x1D4EA])
            return "".join(chr(base + ord(c) - 97) if "a" <= c <= "z" else c for c in s.lower())
        if k == 2:
            return P.fw(s.upper() if r.random() < 0.5 else s)
        if k == 3:
            return P.to_block(s, r.choice(P.ND_ZEROS))
        if k == 4:
            return P.zw_in(r, s.upper())
        return "".join(chr(0x24D0 + ord(c) - 97) if "a" <= c <= "z" else c for c in s.lower())   # circled letters
    if dz == "D24":
        kind, s = Q.address(r, r.choice(["ordinary", "ordinary", "ordinary", "strong"]))
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
        v = P.email(r) if dz == "D22" else email_case(r, dz, y, i)
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
            r = random.Random("rv21|%s|%s|%s|%s" % (c, dz, y, x))
            for i in range(N):
                yield {"c": c, "d": dz, "y": y, "x": x, "i": i}, one(r, c, dz, y, x, i)
