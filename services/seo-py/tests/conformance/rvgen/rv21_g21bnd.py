"""rv-priya21 boundary set (M-2, enumerated) + round-21 probes. iter_cases() -> (meta, q). meta: g (group), s (sub).
Groups: gap3, mixgap, nd, zw, numw, enc, chunkall, rot, uatp, diners36, vanity_codes, e164_len, plus_gap (M-2);
c2 (every one of the 24 C-2 glue characters before / after / both, every class), vg21 (spelled code digit glued to the
keypad part), atmulti (two or more at-words, round 21 (a)), short (decoded under 8 characters, round 21 (b)),
schunk (1-2 character chunks, round 21 (c))."""
# Ported into the permanent conformance suite (priya-spec/2 FG-3, CAL-2) from review rv-priya21, file h/g21bnd.py
# (sha256 58250ec763387d55836a14cbc90ebd6a67e5c43d87127ba29150c7daff4e14c9): the reviewer's generator, seeded; synthetic values only, made at run time. Changes: non-ASCII
# literals written as escapes, imports from the package, the command-line driver and environment overrides
# removed (N / PER fixed at the review's defaults). The random call order is unchanged, so the corpus is the
# review's own (sha256 pinned in data/manifest.json).
import random

from rvgen import rv21_p21 as P
from rvgen import rv21_c21 as Q

R0 = 2121


def _r(*k):
    return random.Random("rv21bnd|" + "|".join(map(str, k)))


def digit_vals(r):
    return [("card", P.card(r)), ("nanp", P.nanp(r)), ("e164", P.e164(r)), ("ssn", P.ssn(r))]


def pl(c, s):
    return "+" + s if c == "e164" else s


def m2():
    for i in range(300):                                   # gaps of exactly 3, every gap / some gaps; mixed
        r = _r("gap3", i)
        for c, d in digit_vals(r):
            for kind in ("ascii", "nfkc", "other", "sep", "mixed"):
                yield {"g": "gap3", "s": c + ":" + kind}, pl(c, P.gap_digits(r, d, kind, every=i % 2 == 0, exact3=True))
    for i in range(200):
        r = _r("mix", i)
        for c, d in digit_vals(r):
            yield {"g": "mixgap", "s": c}, pl(c, P.gap_digits(r, d, "mixed", every=True))
    for z in P.ND_ZEROS:                                   # every Nd block, every class
        r = _r("nd", z)
        for c, d in digit_vals(r):
            yield {"g": "nd", "s": c}, pl(c, P.to_block(d, z))
        one, code, seven = P.vanity(r)
        yield {"g": "nd", "s": "vanity"}, P.to_block(one + code + seven, z)
        yield {"g": "nd", "s": "address"}, P.to_block(Q.address(r, "strong")[1], z)
    for zc in P.ZW:                                        # every listed invisible character, every class
        for i in range(12):
            r = _r("zw", ord(zc), i)
            vals = digit_vals(r) + [("email", P.email(r)), ("address", Q.address(r)[1]),
                                    ("vanity", "".join(P.vanity(r)))]
            for c, d in vals:
                s = pl(c, d)
                k = r.randint(1, len(s) - 1)
                yield {"g": "zw", "s": c}, s[:k] + zc + s[k:] if i % 2 else zc.join(s)
    for i in range(150):                                   # number words: hyphens / spaces / run / one among digits
        r = _r("numw", i)
        for c, d in digit_vals(r):
            for lang in ("en", "es"):
                for j in ("-", " ", "", "  "):
                    yield {"g": "numw", "s": c + ":" + lang}, pl(c, P.spell(r, d, lang, joiner=j))
                yield {"g": "numw1", "s": c + ":" + lang}, pl(c, P.spell_mixed(r, d, lang, one=True))
    for i in range(120):                                   # every encoding with / without padding
        r = _r("enc", i)
        e = P.email(r, short=i % 6 == 0)
        for k in Q.ENC:
            yield {"g": "enc", "s": k}, Q.encode(r, e, k)
    for i in range(30):                                    # chunked encodings split at EVERY single position
        r = _r("chunkall", i)
        e = P.email(r, short=i % 5 == 0)
        for k in ("b64", "b64unp", "b32", "b32lnp", "hex"):
            s = Q.encode(r, e, k)
            for p in range(1, len(s)):
                yield {"g": "chunkall", "s": k}, s[:p] + " " + s[p:]
    for i in range(300):                                   # rot13 / reversal of the whole query and of each token
        r = _r("rot", i)
        q = Q.in_context(r, Q.LANGS[i % 12], P.email(r))
        yield {"g": "rot", "s": "rot13"}, Q.rot13(q)
        yield {"g": "rot", "s": "rev"}, q[::-1]
        yield {"g": "rot", "s": "tokrev"}, " ".join(w[::-1] for w in q.split(" "))
    for i in range(400):
        r = _r("uatp", i)
        d = P.luhn_fix("1" + P.rdig(r, 13))
        yield {"g": "uatp", "s": "-"}, r.choice([d, d[:4] + " " + d[4:9] + " " + d[9:]])
        d = P.luhn_fix("36" + P.rdig(r, 11))
        yield {"g": "diners36", "s": "-"}, r.choice([d, d[:4] + " " + d[4:10] + " " + d[10:]])
    for code in P.VANITY_CODES:                            # every vanity code x with / without the leading 1
        for one in ("", "1"):
            for i in range(40):
                r = _r("van", code, one, i)
                s7 = P.vanity(r)[2]
                yield {"g": "vanity_codes", "s": code + one}, r.choice(
                    ["{o}{c}{s}", "{o}-{c}-{s}", "{o} ({c}) {s}", "{o}{c} {s}"]).format(o=one, c=code, s=s7.upper())
    for n in (8, 15):
        for i in range(200):
            r = _r("e164len", n, i)
            yield {"g": "e164_len", "s": str(n)}, "+" + P.e164(r, n)
    for k in range(4):                                     # '+' then 0..3 characters before the first digit
        for i in range(150):
            r = _r("plus", k, i)
            g = "".join(r.choice([" ", "(", "-", ".", "\u00a0", "\u3000", "/", "x", "\u0435"]) for _ in range(k))
            yield {"g": "plus_gap", "s": str(k)}, "+" + g + P.e164(r)


def c2():
    for gi, g in enumerate(P.GLUE):
        for i in range(10):
            r = _r("c2", gi, i)
            vals = [("card", P.card(r)), ("nanp", P.nanp(r)), ("ssn", P.ssn(r)[:3] + "-" + P.ssn(r)[3:5] + "-" + "1234"),
                    ("e164", "+" + P.e164(r)), ("email", P.email(r)), ("email_sp", Q.spelled_email(r)),
                    ("email_us", Q.underscore_email(r)), ("email_enc", Q.encode(r, P.email(r))),
                    ("address", Q.address(r)[1]), ("address_ord", Q.address(r, "ordinary")[1]),
                    ("vanity", "1" + r.choice(P.VANITY_CODES) + P.vanity(r)[2])]
            for c, v in vals:
                lang = Q.LANGS[(gi + i) % 12]
                w1, w2 = Q.CTX[lang][0][i % 2], Q.CTX[lang][1][i % 2]
                yield {"g": "c2", "s": c + ":before"}, w1 + g + v
                yield {"g": "c2", "s": c + ":after"}, v + g + w2
                yield {"g": "c2", "s": c + ":both"}, w1 + g + v + g + w2


def vg21():
    for code in P.VANITY_CODES:
        for lang in ("en", "es"):
            for i in range(30):
                r = _r("vg", code, lang, i)
                w = P.NUMW[lang]
                one = "1" if i % 2 else ""
                digits = list(one + code)
                s7 = P.vanity(r)[2]
                j = r.randrange(len(digits))
                # (1) the LAST code digit spelled and glued to the keypad part; (2) every digit spelled, run, glued
                last = "".join(digits[:-1]) + r.choice([" ", "-", ""]) + w[int(digits[-1])] + s7
                yield {"g": "vg21", "s": "last:" + lang}, P.rcase(r, last)
                yield {"g": "vg21", "s": "all:" + lang}, P.rcase(r, "".join(w[int(x)] for x in digits) + s7)
                digits[j] = w[int(digits[j])]
                yield {"g": "vg21", "s": "one:" + lang}, P.rcase(r, "".join(digits) + s7)


def atmulti():
    """the local part / domain holds at-words; 2..6 at-words in all."""
    for i in range(400):
        r = _r("atm", i)
        aw = r.choice(P.AT_WORDS)
        dw = r.choice(P.DOT_WORDS)
        loc = r.choice(["{a}", "{a}-{a}", "meet-{a}-noon", "{a}{a}", "x_{a}_y", "cat-{a}", "{a}-home-{a}"]).format(a=aw)
        dom = P.label(r)
        tld = r.choice(["com", "at", "de", "io", "kukac", "dot", "point"])
        k = i % 5
        if k == 0:
            yield {"g": "atmulti", "s": "spaced"}, loc + " " + aw.upper() + " " + dom + " " + dw.upper() + " " + tld
        elif k == 1:
            yield {"g": "atmulti", "s": "under"}, loc + "_AT_" + dom + "_DOT_" + tld
        elif k == 2:
            yield {"g": "atmulti", "s": "dash"}, loc + "-at-" + dom + "-dot-" + tld
        elif k == 3:
            ctx = " ".join([r.choice(["look", "at", "this", "arroba", "kukac", "malpa"]) for _ in range(5)])
            yield {"g": "atmulti", "s": "ctx"}, ctx + " " + loc + " " + aw + " " + dom + " " + dw + " " + tld
        else:
            yield {"g": "atmulti", "s": "bracket"}, loc + " [at] " + aw + " [dot] " + tld        # domain = at-word


def short():
    """e-mails of 6-7 characters under every encoding, rot13 / reversed, and chunked (round 21 (b))."""
    for i in range(150):
        r = _r("short", i)
        e = P.email(r, short=True)
        for k in Q.ENC:
            s = Q.encode(r, e, k)
            yield {"g": "short", "s": k}, s
            yield {"g": "short", "s": k + ":rot"}, Q.rot13(s)
            yield {"g": "short", "s": k + ":rev"}, s[::-1]
            yield {"g": "short", "s": k + ":ctx"}, Q.in_context(r, Q.LANGS[i % 12], s)
            yield {"g": "short", "s": k + ":ch1"}, Q.chunk(r, s, 1)
        yield {"g": "short", "s": "rot13"}, Q.rot13(e)
        yield {"g": "short", "s": "rev"}, e[::-1]
        yield {"g": "short", "s": "b64(rev)"}, Q.encode(r, e[::-1], "b64")
        yield {"g": "short", "s": "b64(rot)"}, Q.encode(r, Q.rot13(e), "b64np")


def schunk():
    """encodings in pieces of 1-2 characters (merge rule), pieces of 3 between them, other white space."""
    for i in range(300):
        r = _r("sch", i)
        e = P.email(r, short=i % 4 == 0)
        k = Q.ENC[i % len(Q.ENC)]
        s = Q.encode(r, e, k)
        sizes = [[1], [2], [1, 2], [1, 3], [2, 3], [1, 1, 1, 3]][i % 6]
        out, j, n = [], 0, 0
        while j < len(s):
            m = sizes[n % len(sizes)]
            out.append(s[j:j + m])
            j += m
            n += 1
        sep = [" ", "\t", "\u00a0", "\u3000", "  ", " \u200b"][i % 6]
        q = sep.join(out)
        yield {"g": "schunk", "s": k + ":" + "".join(map(str, sizes)) + ":%x" % ord(sep[-1])}, q
        yield {"g": "schunk", "s": "ctx"}, Q.in_context(r, Q.LANGS[i % 12], q)


def iter_cases():
    for gen in (m2, c2, vg21, atmulti, short, schunk):
        yield from gen()
