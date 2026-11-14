"""rv-priya20 probe set: vanity numbers whose LAST code digit (or whole code) is a number word glued to the keypad part
(D11 / D12 / D14 x the glued usual format '1800flowers'), and the same with a space (control)."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya20, file h/g20vg.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver removed. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import random
from rvgen import rv20_p20 as P


def iter_cases():
    r = random.Random("rv20-vglue")
    for code in P.VANITY_CODES:
        for one in ("", "1"):
            for lang in ("en", "es"):
                w = P.NUMW[lang]
                for _k in range(10):
                    _o, _c, s7 = P.vanity(r)
                    s7 = P.rcase(r, s7)
                    digits = one + code
                    full = " ".join(w[int(x)] for x in digits)
                    last = " ".join(digits[:-1]) + " " + w[int(digits[-1])]
                    mixed = digits[:-1] + w[int(digits[-1])]
                    for form, s in (("D11_glued", full + s7), ("D11_space", full + " " + s7),
                                    ("D14_glued", last + s7), ("D14_space", last + " " + s7),
                                    ("D14_tight_glued", mixed + s7), ("D14_tight_space", mixed + " " + s7)):
                        yield {"c": "vanity", "d": form, "y": lang}, s
