"""Reviewer generator part 4: e-mail and address cases, and the M-1 driver.
python g4.py OUT.jsonl N SEED"""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya19, file h/g4.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver replaced by a function. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import random

from rvgen import rv19_g1 as g1
from rvgen import rv19_g2 as g2
from rvgen import rv19_g3 as g3

EMAIL_D = ["D01", "D09", "D10", "D16", "D17", "D18", "D19", "D20", "D21", "D22"]
EMAIL_Y = {"D16": list(g2.LANG_AT_DOT), "D17": list(g2.LANG_AT_DOT), "D18": list(g2.LANG_AT_DOT)}


def email_case(r, D, y, x):
    """Returns (sub, query) - for D22 the context is part of the transformed whole query."""
    p = g2.email_parts(r, minlen=16 if D in ("D19", "D21") else 0)
    e = g2.email_plain(p)
    if D == "D01":
        return "", g3.contextual(r, e, x)
    if D == "D09":
        return "", g3.contextual(r, g1.fullwidth(e), x)
    if D == "D10":
        return "", g3.contextual(r, g1.insert_zw(r, e), x)
    if D == "D16":
        return y, g3.contextual(r, g2.email_spelled(r, p, y), x)
    if D == "D17":
        return y, g3.contextual(r, g2.email_bracketed(r, p, y), x)
    if D == "D18":
        return y, g3.contextual(r, g2.email_underscored(r, p, y), x)
    if D == "D19":
        sub, t = g2.encodings(r, e)
        return sub, g3.contextual(r, t, x)
    if D == "D20":
        k = r.randint(0, 2)
        if k == 0:
            return "rot13", g3.contextual(r, g2.rot13(e), x)
        if k == 1:
            return "rev", g3.contextual(r, e[::-1], x)
        sp = g2.email_spelled(r, p, "en")
        return "wordrev", g3.contextual(r, " ".join(w[::-1] for w in sp.split()), x)
    if D == "D21":
        k = r.randint(0, 4)
        if k == 0:
            base, t = "plain", e
        elif k == 1:
            base, t = "spelled", g2.email_spelled(r, p, "en")
        elif k == 2:
            base, t = "bracketed", g2.email_bracketed(r, p, "en")
        elif k == 3:
            sub, t = g2.encodings(r, e)
            base = "enc_" + sub
        else:
            base, t = ("rot13", g2.rot13(e)) if r.random() < 0.5 else ("rev", e[::-1])
        return base, g3.contextual(r, g2.chunk(r, t), x)
    if D == "D22":
        base = e if r.random() < 0.5 else g2.email_spelled(r, p, "en")
        q = g3.contextual(r, base, x)
        k = r.randint(0, 2)
        if k == 0:
            return "rot13", g2.rot13(q)
        if k == 1:
            return "rev", q[::-1]
        return "wordrev", " ".join(w[::-1] for w in q.split(" "))
    raise ValueError(D)


ADDR_D = ["D01", "D08", "D09", "D10", "D23", "D24"]
TAIL = ["tonight", "hours", "parking", "reviews", "for sale", "apartment", "near me", "photos", "map", "zillow",
        "open", "pizza", "phone", "price", "weather", "school", "rent", "owner", "history", "permit"]


def address_case(r, D, x):
    form, a = g2.address(r)
    if D == "D01":
        return form, g3.contextual(r, a, x, addr=True)
    if D == "D08":
        z = r.choice(g1.ND_BLOCKS_NONASCII)
        return form + ":%05x" % z, g3.contextual(r, g1.to_block(a, z), x, addr=True)
    if D == "D09":
        return form, g3.contextual(r, g1.fullwidth(a), x, addr=True)
    if D == "D10":
        return form, g3.contextual(r, g1.insert_zw(r, a), x, addr=True)
    if D == "D23":
        k = r.randint(0, 4)
        if k == 0:
            return form + ":upper", g3.contextual(r, a.upper(), x, addr=True)
        if k == 1:
            # NFKC letters: mathematical bold small letters for every ASCII letter
            t = "".join(chr(0x1D41A + ord(c) - 97) if "a" <= c <= "z" else c for c in a.lower())
            return form + ":nfkc", g3.contextual(r, t, x, addr=True)
        if k == 2:
            return form + ":fwall", g3.contextual(r, g1.fullwidth(a), x, addr=True)
        if k == 3:
            z = r.choice(g1.ND_BLOCKS_NONASCII)
            return form + ":script", g3.contextual(r, g1.to_block(a, z), x, addr=True)
        return form + ":zw", g3.contextual(r, g1.insert_zw(r, a), x, addr=True)
    if D == "D24":
        kind = r.choice(["ordinary", "strong"])
        s = g2.street(r, kind)
        n = r.randint(0, 8)
        tail = " ".join(r.choice(g2.CITIES + g2.NAMES + TAIL) for _ in range(n))
        return kind + ":%d" % n, g3.contextual(r, (s + " " + tail).strip(), x, addr=True)
    raise ValueError(D)


def cells():
    for c in ("card", "nanp", "e164", "ssn"):
        for D in g3.DIGIT_D:
            for y in (["en", "es"] if D in ("D13", "D14") else (["en"] if D == "D11" else (["es"] if D == "D12" else [""]))):
                yield c, D, y
    for D in g3.VAN_D:
        for y in (["en", "es"] if D in ("D13", "D14") else (["en"] if D == "D11" else (["es"] if D == "D12" else [""]))):
            yield "vanity", D, y
    for D in EMAIL_D:
        for y in EMAIL_Y.get(D, [""]):
            yield "email", D, y
    for D in ADDR_D:
        yield "address", D, ""


def cases(n, seed):
    """The review's M-1 driver: n values per (class, disguise, language, context) cell, one RNG per cell."""
    for c, D, y in cells():
        for x in range(13):
            r = random.Random("%d|%s|%s|%s|%d" % (seed, c, D, y, x))
            for _ in range(n):
                if c in g3.VAL:
                    sub, v = g3.digit_case(r, c, D, y or "en")
                    q = g3.contextual(r, v, x)
                elif c == "vanity":
                    sub, v = g3.vanity_case(r, D, y or "en")
                    q = g3.contextual(r, v, x)
                elif c == "email":
                    sub, q = email_case(r, D, y, x)
                else:
                    sub, q = address_case(r, D, x)
                yield {"c": c, "d": D, "x": x, "y": y, "s": sub, "q": q}
