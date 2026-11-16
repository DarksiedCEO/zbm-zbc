"""rv-priya21 ASCII-glue probe (outside v1.1 C-2: the ASCII forms of the glue set): class value glued to a Latin word
by one ASCII punctuation character, before / after. iter_cases()."""
# Ported into the permanent conformance suite (priya-spec/2 FG-3, CAL-2) from review rv-priya21, file h/g21ag.py
# (sha256 bf6522cb0ceae3cad0df914298da530f30a816fe8c557d59ac27c2b7563cf07f): the reviewer's generator, seeded; synthetic values only, made at run time. Changes: non-ASCII
# literals written as escapes, imports from the package, the command-line driver and environment overrides
# removed (N / PER fixed at the review's defaults). The random call order is unchanged, so the corpus is the
# review's own (sha256 pinned in data/manifest.json).
import random
import string
from rvgen import rv21_p21 as P
from rvgen import rv21_c21 as Q
ASCII_GLUE = [c for c in string.punctuation if c not in "@"]


def iter_cases():
    for g in ASCII_GLUE:
        r = random.Random("rv21ag|" + g)
        for i in range(10):
            vals = [("card", P.card(r)), ("nanp", P.nanp(r)), ("email", P.email(r)), ("email_sp", Q.spelled_email(r)),
                    ("address", Q.address(r, "strong")[1]), ("address_ord", Q.address(r, "ordinary")[1]),
                    ("vanity", "1" + r.choice(P.VANITY_CODES) + P.vanity(r)[2])]
            for c, v in vals:
                yield {"c": c, "g": g, "s": "before"}, "thanks" + g + v
                yield {"c": c, "g": g, "s": "after"}, v + g + "thanks"

