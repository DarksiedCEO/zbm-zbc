"""rv-priya21 D21 probe corpus: base e-mail form x chunk pattern x 100, X0 and in context. iter_cases() -> (meta, q)."""
# Ported into the permanent conformance suite (priya-spec/2 FG-3, CAL-2) from review rv-priya21, file h/g21d21.py
# (sha256 5b2125ce28610cf2915310ee164ba309a94acedfea659dffdaed6f02f5ce5266): the reviewer's generator, seeded; synthetic values only, made at run time. Changes: non-ASCII
# literals written as escapes, imports from the package, the command-line driver and environment overrides
# removed (N / PER fixed at the review's defaults). The random call order is unchanged, so the corpus is the
# review's own (sha256 pinned in data/manifest.json).
import random
from rvgen import rv21_p21 as P
from rvgen import rv21_c21 as Q

BASES = ["plain", "spelled", "bracket", "rot13", "reverse", "wordrev"] + Q.ENC
PATS = {"r15": None, "1": [1], "2": [2], "12": [1, 2], "3": [3], "1113": [1, 1, 1, 3], "123": [1, 2, 3], "4": [4]}


def base(r, b):
    e = P.email(r)
    if b == "plain":
        return e
    if b == "spelled":
        return Q.spelled_email(r)
    if b == "bracket":
        return Q.bracket_email(r)
    if b == "rot13":
        return Q.rot13(e)
    if b == "reverse":
        return e[::-1]
    if b == "wordrev":
        return Q.wordrev(e)
    return Q.encode(r, e, b)


def chunk(r, s, pat):
    if pat is None:
        return Q.chunk(r, s)
    out, j, n = [], 0, 0
    while j < len(s):
        m = pat[n % len(pat)]
        out.append(s[j:j + m])
        j += m
        n += 1
    return " ".join(out)


def iter_cases():
    for b in BASES:
        for pn, pat in PATS.items():
            r = random.Random("rv21d21|%s|%s" % (b, pn))
            for i in range(100):
                q = chunk(r, base(r, b), pat)
                ctx = i % 4 == 0
                if ctx:
                    q = Q.in_context(r, Q.LANGS[(i // 4) % 12], q)
                yield {"b": b, "p": pn, "ctx": int(ctx), "i": i}, q

