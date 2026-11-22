"""P1 boundary cases of the list (own generator)."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya18, file rv/build_bnd.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver replaced by a function. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import random

from rvgen import rv18_gen as G


def cases():
    r = random.Random(18_003)
    rows = []


    def add(c, d, p, q, x=""):
        rows.append({"c": c, "d": d, "p": p, "q": q, "x": x})


    # 1. every Unicode decimal-digit block, each digit class
    for z in G.ND_ZEROS:
        for cls in G.DIGIT_CLASSES:
            for _ in range(10):
                d, f = G.CLASSES[cls](r)
                q = "".join(chr(z + int(c)) if c.isdigit() else c for c in f)
                add(cls, f"nd_block_{z:05X}", "alone", q)
                add(cls, f"nd_block_{z:05X}", "ctx", G.in_context(q, r))
    # 2. every zero-width / bidi character, between every character
    for ch in G.ZW_BIDI:
        for cls in list(G.DIGIT_CLASSES) + ["email", "vanity", "address"]:
            for _ in range(10):
                d, f = G.CLASSES[cls](r)
                q = ch.join(f)
                add(cls, f"zw_{ord(ch):04X}", "alone", q)
                add(cls, f"zw_{ord(ch):04X}", "ctx", G.in_context(q, r))
    # 3. encoded e-mails split in two at every position, and chunked at every chunk size
    ENCS = {"b64": lambda e: __import__("base64").b64encode(e.encode()).decode(),
            "b64_nopad": lambda e: __import__("base64").b64encode(e.encode()).decode().rstrip("="),
            "b64url": lambda e: __import__("base64").urlsafe_b64encode(e.encode()).decode().rstrip("="),
            "b32": lambda e: __import__("base64").b32encode(e.encode()).decode(),
            "b32_nopad_lower": lambda e: __import__("base64").b32encode(e.encode()).decode().rstrip("=").lower(),
            "hex": lambda e: e.encode().hex(), "HEX": lambda e: e.encode().hex().upper(),
            "url_whole": lambda e: "".join("%{:02X}".format(b) for b in e.encode())}
    for name, fn in ENCS.items():
        for _ in range(40):
            e, _ = G.email(r)
            s = fn(e)
            for i in range(1, len(s)):
                q = s[:i] + " " + s[i:]
                add("email", f"split2_{name}", "alone", q, f"pos{i}")
                add("email", f"split2_{name}", "ctx", G.in_context(q, r), f"pos{i}")
        for size in range(1, 9):
            for _ in range(40):
                e, _ = G.email(r)
                s = fn(e)
                q = " ".join(s[i:i + size] for i in range(0, len(s), size))
                add("email", f"chunk{size}_{name}", "alone", q)
                add("email", f"chunk{size}_{name}", "ctx", G.in_context(q, r))
    # 4. vanity: every code, with / without 1, separators
    for code in ("800", "833", "844", "855", "866", "877", "888"):
        for one in ("", "1", "1-", "1 ", "+1 "):
            for _ in range(20):
                w = r.choice(G.VANITY_WORDS)
                for f in (f"{one}{code}-{w}", f"{one}({code}) {w[:3]}-{w[3:]}", f"{one}{code} {w.upper()}",
                          f"{one}{code}.{w}"):
                    add("vanity", f"code{code}_one[{one.strip()}]", "alone", f)
                    add("vanity", f"code{code}_one[{one.strip()}]", "ctx", G.in_context(f, r))
    # 5. UATP and Diners 36, formatted variants
    for net, pf, n in (("uatp", "1", 15), ("diners36", "36", 14)):
        for _ in range(2000):
            d = G.luhn_complete(pf, n, r)
            f = r.choice([d, d[:4] + " " + d[4:10] + " " + d[10:], "-".join(d[i:i + 4] for i in range(0, n, 4))])
            add("card", net, "alone", f)
            add("card", net, "ctx", G.in_context(f, r))
    # 6. exactly 3-char gaps: all letters / all separators / mixed, between every digit
    for cls in G.DIGIT_CLASSES:
        for kind, pool in (("letters3", list(G.LETTERS_ASCII)), ("seps3", [c for c in G.SEPARATORS]),
                           ("other3", G.OTHER_LETTERS), ("nfkc3", G.NFKC_LETTERS)):
            for _ in range(200):
                d, f = G.CLASSES[cls](r)
                q = G._gap_between(d, r, pool, 3, 3, True)
                add(cls, "gap_exact3_" + kind, "alone", q)
                add(cls, "gap_exact3_" + kind, "ctx", G.in_context(q, r))
    # 7. number words: hyphen / space / run together / Spanish / mixed case, each digit class
    for cls in G.DIGIT_CLASSES:
        for kind in ("spelled_en", "spelled_es", "spelled_hyphen", "spelled_run", "spelled_mixed", "spelled_upper"):
            for _ in range(200):
                d, f = G.CLASSES[cls](r)
                q = G.DIGIT_T[kind](d, r)
                add(cls, "nw_" + kind, "alone", q)
                add(cls, "nw_" + kind, "ctx", G.in_context(q, r))
    return rows
