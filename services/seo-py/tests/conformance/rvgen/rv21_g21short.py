"""rv-priya21 short e-mail pairs (6-8 characters): inner transform x encoding, X0 / context. iter_cases()."""
# Ported into the permanent conformance suite (priya-spec/2 FG-3, CAL-2) from review rv-priya21, file h/g21short.py
# (sha256 18b1b1f3d195b705c34fd2d47db4256a654b159620339804fed3f3f0ee0faac1): the reviewer's generator, seeded; synthetic values only, made at run time. Changes: non-ASCII
# literals written as escapes, imports from the package, the command-line driver and environment overrides
# removed (N / PER fixed at the review's defaults). The random call order is unchanged, so the corpus is the
# review's own (sha256 pinned in data/manifest.json).
import random
from rvgen import rv21_p21 as P
from rvgen import rv21_c21 as Q
INNER = ["none", "reverse", "wordrev", "rot13", "upper", "title"]


def iter_cases():
    for inner in INNER:
        for k in Q.ENC:
            r = random.Random("rv21short|%s|%s" % (inner, k))
            for i in range(100):
                e = P.email(r, short=True) if i % 3 else (P.email(r, short=True) + r.choice("abc"))[:8]
                v = {"none": e, "reverse": e[::-1], "wordrev": Q.wordrev(e), "rot13": Q.rot13(e), "upper": e.upper(),
                     "title": e.title()}[inner]
                q = Q.encode(r, v, k)
                if i % 2:
                    q = Q.in_context(r, Q.LANGS[i % 12], q)
                yield {"c": inner, "d": k, "ctx": i % 2, "i": i}, q

