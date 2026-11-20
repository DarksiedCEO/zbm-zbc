"""Reviewer's own boundary set (M-2), enumerated: python g5.py OUT.jsonl"""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya19, file h/g5.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver replaced by a function. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import base64
import random

from rvgen import rv19_g1 as g1
from rvgen import rv19_g2 as g2
from rvgen import rv19_g3 as g3

def cases():
    r = random.Random(1919)
    out = []


    def add(c, d, q, s=""):
        out.append({"c": c, "d": d, "x": 0, "y": "", "s": s, "q": q})


    DIG = {"card": g1.card, "nanp": g1.nanp, "e164": g1.e164, "ssn": g1.ssn}

    # 1. every Nd block x class (digit classes, vanity, address)
    for z in g1.ND_BLOCKS_NONASCII:
        for c, f in DIG.items():
            for _ in range(3):
                d = f(r)
                add(c, "B-nd", g1.to_block(g1.fmt_digits(r, c, d), z), "%05x" % z)
        for _ in range(3):
            one, code, kp = g2.vanity_parts(r)
            add("vanity", "B-nd", g1.to_block(g2.vanity_join(r, one, code, kp), z), "%05x" % z)
            _f, a = g2.address(r, r.choice(["strong", "full", "pobox", "zip4", "statezip"]))
            add("address", "B-nd", g1.to_block(a, z), "%05x" % z)

    # 2. every zero-width / bidi character at every position of a value (one insertion)
    for zc in g1.ZW:
        for c, f in DIG.items():
            q = g1.fmt_digits(r, c, f(r))
            for i in range(len(q) + 1):
                add(c, "B-zw", q[:i] + zc + q[i:], "%04x" % ord(zc))
        one, code, kp = g2.vanity_parts(r)
        q = g2.vanity_join(r, one, code, kp)
        for i in range(len(q) + 1):
            add("vanity", "B-zw", q[:i] + zc + q[i:], "%04x" % ord(zc))
        e = g2.email_plain(g2.email_parts(r))
        for i in range(len(e) + 1):
            add("email", "B-zw", e[:i] + zc + e[i:], "%04x" % ord(zc))
        _f, a = g2.address(r, "strong")
        for i in range(len(a) + 1):
            add("address", "B-zw", a[:i] + zc + a[i:], "%04x" % ord(zc))

    # 3. gaps of exactly 3 everywhere, per pool, and letters+separators mixed
    for c, f in DIG.items():
        for kind in ("ascii3", "sep3", "nfkc3", "other3", "mixed3"):
            for _ in range(30):
                d = f(r)
                o = []
                for i, ch in enumerate(d):
                    o.append(ch)
                    if i < len(d) - 1:
                        if kind == "ascii3":
                            g = "".join(r.choice(g1.ASCII_LET) for _ in range(3))
                        elif kind == "sep3":
                            g = "".join(r.choice(g1.SEP_ALL) for _ in range(3))
                        elif kind == "nfkc3":
                            g = "".join(r.choice(g1.NFKC_LET) for _ in range(3))
                        elif kind == "other3":
                            g = "".join(r.choice(g1.OTHER_LET) for _ in range(3))
                        else:
                            g = r.choice(g1.ASCII_LET) + r.choice(g1.SEP_ALL) + r.choice(g1.ASCII_LET + "-. ")
                        o.append(g)
                add(c, "B-gap3", ("+" if c == "e164" else "") + "".join(o), kind)

    # 4. E.164 at 8..15 digits, '+' separated from the first digit by 0..3 characters
    for n in range(8, 16):
        for k in range(4):
            for _ in range(15):
                d = str(r.randint(1, 9)) + "".join(str(r.randint(0, 9)) for _ in range(n - 1))
                g = "".join(r.choice(g1.ASCII_LET + " -.(") for _ in range(k))
                add("e164", "B-plus", "+" + g + d, "%d:%d" % (n, k))

    # 5. every vanity code x with / without the leading 1, separators, spelled code
    for code in g2.CODES:
        for one in ("", "1"):
            for _ in range(20):
                kp = g2.vanity_parts(r)[2]
                add("vanity", "B-van", g2.vanity_join(r, one, code, kp), code + one)
            for lang in ("en", "es"):
                kp = g2.vanity_parts(r)[2]
                add("vanity", "B-van-sp", g2.spelled(r, one + code, lang) + " " + kp, code + one + lang)

    # 6. UATP and Diners 36
    for _ in range(60):
        d = g1.luhn_complete("1" + "".join(str(r.randint(0, 9)) for _ in range(13)))
        add("card", "B-uatp", g1.fmt_digits(r, "card", d))
        d = g1.luhn_complete("36" + "".join(str(r.randint(0, 9)) for _ in range(11)))
        add("card", "B-diners36", g1.fmt_digits(r, "card", d))

    # 7. every encoding with and without padding, and chunked at EVERY position (one split)
    for _ in range(12):
        e = g2.email_plain(g2.email_parts(r, minlen=16))
        raw = e.encode()
        forms = {"b64p": base64.b64encode(raw).decode(), "b64n": base64.b64encode(raw).decode().rstrip("="),
                 "b64up": base64.urlsafe_b64encode(raw).decode(), "b64un": base64.urlsafe_b64encode(raw).decode().rstrip("="),
                 "b32Up": g2.b32(e, False, True), "b32Un": g2.b32(e, False, False), "b32lp": g2.b32(e, True, True),
                 "b32ln": g2.b32(e, True, False), "hexl": raw.hex(), "hexu": raw.hex().upper(),
                 "urlw": "".join("%%%02X" % b for b in raw), "urlp": e.replace("@", "%40")}
        for k, t in forms.items():
            add("email", "B-enc", t, k)
            for i in range(1, len(t)):
                add("email", "B-enc-chunk", t[:i] + " " + t[i:], k)

    # 8. rot13 and reversal of the whole query and of each token (e-mail in context)
    for x in range(1, 13):
        for _ in range(15):
            p = g2.email_parts(r)
            base = g2.email_plain(p) if r.random() < 0.5 else g2.email_spelled(r, p, "en")
            q = g3.contextual(r, base, x)
            add("email", "B-rot13q", g2.rot13(q), str(x))
            add("email", "B-revq", q[::-1], str(x))
            add("email", "B-revtok", " ".join(w[::-1] for w in q.split(" ")), str(x))

    # 9. number words: one word among digits at EVERY position; hyphen / run / upper
    for c, f in DIG.items():
        for lang in ("en", "es"):
            for _ in range(3):
                d = f(r)
                for i in range(len(d)):
                    q = d[:i] + " " + g1.WORDS[lang][int(d[i])] + " " + d[i + 1:]
                    add(c, "B-oneword", ("+" if c == "e164" else "") + q.strip(), lang)
            for mode in ("hyphen", "run", "upper", "space"):
                for _ in range(10):
                    add(c, "B-spelled", ("+" if c == "e164" else "") + g2.spelled(r, f(r), lang, mode), lang + mode)
    return out
