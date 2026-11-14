"""Build P1 (closed-list matrix) and P2 (composed pairs) corpora as JSONL: {"c":class,"d":disguise,"p":place,"q":query}.
No value is printed; the files stay in work/."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya18, file rv/build_p1p2.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver replaced by a function. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import random

from rvgen import rv18_gen as G

PER = 2000


TLD_LISTED = {"com", "net", "org", "edu", "gov", "mil", "int", "info", "biz", "name", "pro", "mobi", "io", "ai",
              "app", "dev", "xyz", "online", "site", "shop", "store", "tech", "blog", "cloud", "email", "mail",
              "live", "news", "club", "top", "art", "design", "agency", "media", "world", "life", "space",
              "website", "group", "company", "business", "network", "systems", "today", "tips", "zone", "global",
              "local", "travel", "jobs", "asia", "museum", "digital", "solutions", "services"}


def tldg(e):
    t = e.rsplit(".", 1)[1]
    return "tld_listed" if t in TLD_LISTED else "tld_2" if len(t) == 2 else "tld_unlisted"


def p1():
    r = random.Random(18_001)
    rows = []
    for cls in G.DIGIT_CLASSES:
        for _ in range(PER):
            d, f = G.CLASSES[cls](r)
            digits = d  # e164 keeps '+'
            for name in G.DIGIT_T:
                for place in ("alone", "ctx"):
                    q = G.digit_disguise(name, f, digits, r)
                    rows.append((cls, name, place, G.in_context(q, r) if place == "ctx" else q, ""))
    for _ in range(PER):
        d, f = G.vanity(r)
        for name in G.VANITY_T:
            for place in ("alone", "ctx"):
                q = G.vanity_disguise(name, f, r)
                rows.append(("vanity", name, place, G.in_context(q, r) if place == "ctx" else q))
    for _ in range(PER):
        e, _ = G.email(r)
        for name in G.EMAIL_T:
            for place in ("alone", "ctx"):
                rows.append(("email", name, place, G.email_case(name, e, r, place == "ctx", False), tldg(e)))
        for name in G.CHUNKABLE:
            for place in ("alone", "ctx"):
                rows.append(("email", name + "+chunk", place, G.email_case(name, e, r, place == "ctx", True), tldg(e)))
        # whole-text transforms (the list: rot13 / reversed, the whole text or each word) over a plain e-mail in text
        for wname, fn in (("q_rot13", G.whole_rot13), ("q_rev", G.whole_rev), ("q_words_rev", G.words_rev)):
            for place in ("alone", "ctx"):
                base = G.in_context(e, r) if place == "ctx" else e
                rows.append(("email", wname, place, fn(base, r), tldg(e)))
        # spelled e-mail then the whole-query transforms are composed (P2), not here
    for _ in range(PER):
        _n, a = G.address(r)
        for name in G.ADDR_T:
            for place in ("alone", "ctx"):
                q = G.addr_disguise(name, a, r)
                x = ("strong" if a.split()[-1] in G.STRONG or any(w in G.STRONG for w in a.split()[1:]) else "ordinary")
                if place == "ctx":
                    ci = r.randrange(len(G.ADDR_CONTEXT))
                    q = G.ADDR_CONTEXT[ci].format(q)
                    x += f"/ctx{ci}"
                rows.append(("address", name, place, q, x))
    return rows


# ---- composed pairs: two list transforms applied in order to one value
DIG_TX = ["gap_ascii", "gap_nfkc", "gap_other", "gap_sep", "gap_mixed", "script_digits", "fullwidth", "zw_bidi",
          "spelled_en", "spelled_es", "spelled_run", "spelled_mixed"]
EM_TX = ["spelled", "bracketed", "b64", "b64url", "b32", "hex", "url_whole", "url_part", "rot13", "rev_whole",
         "rev_words", "chunk", "zw_bidi", "fullwidth"]


def em_apply(name, s, r):
    if name == "chunk":
        return G.chunk(s, r)
    if name == "rev_words":
        return G.words_rev(s, r)
    if name in ("spelled", "bracketed"):
        if "@" in s and s.count("@") == 1:
            return G.spelled_email(s, r, name == "bracketed")
        # already transformed (no '@' left): spell the '.'s only
        return s.replace(".", r.choice(G.DOT_SPELL))
    return G.EMAIL_T[name](s, r)


def dig_apply(name, s, r):
    return G.DIGIT_T[name](s, r)


def p2(n_target):
    r = random.Random(18_002)
    rows = []
    dig_pairs = [(a, b) for a in DIG_TX for b in DIG_TX if a != b]
    em_pairs = [(a, b) for a in EM_TX for b in EM_TX if a != b]
    plan = []
    for cls in G.DIGIT_CLASSES:
        plan += [(cls, p) for p in dig_pairs]
    plan += [("email", p) for p in em_pairs]
    reps = n_target // len(plan) + 1
    for _k in range(reps):
        for cls, (a, b) in plan:
            if cls == "email":
                e, _ = G.email(r)
                q = em_apply(b, em_apply(a, e, r), r)
            else:
                d, f = G.CLASSES[cls](r)
                q = dig_apply(b, dig_apply(a, d if r.random() < 0.5 else f, r), r)
            place = "ctx" if r.random() < 0.25 else "alone"
            if place == "ctx":
                q = G.in_context(q, r)
            rows.append((cls, a + ">" + b, place, q, tldg(e) if cls == "email" else ""))
    return rows




# ---- structural digit composition (v2): a value is units (digit strings or words) and gaps; each transform acts in
# its own terms so a composed pair stays inside the list (no gap over 3, no gap inside a word)
DTX2 = ["gap_ascii", "gap_nfkc", "gap_other", "gap_sep", "script_digits", "fullwidth", "zw_bidi", "spelled_en",
        "spelled_es", "spelled_run", "spelled_hyphen", "spelled_mixed"]


def compose_digits(d, a, b, r):
    plus = d.startswith("+")
    units = list(d.lstrip("+"))
    gaps = [None] * (len(units) - 1)          # None: no gap chosen yet
    script = None
    zwf = False
    spelled = None                            # (words, joiner)
    mixed = False
    for t in (a, b):
        if t.startswith("gap_"):
            pool = {"gap_ascii": list(G.LETTERS_ASCII), "gap_nfkc": G.NFKC_LETTERS, "gap_other": G.OTHER_LETTERS,
                    "gap_sep": [c for c in G.SEPARATORS if c != "+"]}[t]
            every = r.random() < 0.5
            for i in range(len(gaps)):
                if every or r.random() < 0.5:
                    gaps[i] = "".join(r.choice(pool) for _ in range(r.randint(1, 3)))
        elif t in ("script_digits", "fullwidth"):
            script = 0xFF10 if t == "fullwidth" else r.choice(G.NON_ASCII_ZEROS)
        elif t == "zw_bidi":
            zwf = True
        else:
            words = G.EN if (t == "spelled_en" or (t != "spelled_es" and r.random() < 0.5)) else G.ES
            joiner = {"spelled_en": " ", "spelled_es": " ", "spelled_run": "", "spelled_hyphen": "-",
                      "spelled_mixed": " "}[t]
            spelled = (words, joiner)
            mixed = t == "spelled_mixed"
    out = []
    for i, u in enumerate(units):
        if spelled and (not mixed or r.random() < 0.5):
            u = spelled[0][int(u)]
            is_word = True  # noqa: F841
        else:
            u = chr(script + int(u)) if script is not None else u
            is_word = False  # noqa: F841
        if zwf:
            u = "".join(c + (r.choice(G.ZW_BIDI) if r.random() < 0.4 else "") for c in u)
        out.append(u)
        if i < len(gaps):
            g = gaps[i]
            if g is None:
                g = spelled[1] if spelled else ""
            elif spelled and spelled[1] == " ":
                g = g if len(g) < 3 else g[:2]
                g = " " + g if False else g
            if zwf and r.random() < 0.5:
                g += r.choice(G.ZW_BIDI)
            out.append(g)
    s = "".join(out)
    return ("+" if plus else "") + s


def p2v2(n_target):
    r = random.Random(18_005)
    rows = []
    pairs = [(a, b) for a in DTX2 for b in DTX2 if a != b and not (a.startswith("gap_") and b.startswith("gap_"))
             and not (a.startswith("spelled") and b.startswith("spelled"))
             and not (a in ("script_digits", "fullwidth") and b in ("script_digits", "fullwidth"))]
    em_pairs = [(a, b) for a in EM_TX for b in EM_TX if a != b]
    plan = [(c, p) for c in G.DIGIT_CLASSES for p in pairs] + [("email", p) for p in em_pairs]
    for _k in range(n_target // len(plan) + 1):
        for cls, (a, b) in plan:
            x = ""
            if cls == "email":
                e, _ = G.email(r)
                x = tldg(e)
                q = em_apply(b, em_apply(a, e, r), r)
            else:
                d, f = G.CLASSES[cls](r)
                q = compose_digits(d, a, b, r)
            place = "ctx" if r.random() < 0.25 else "alone"
            if place == "ctx":
                q = G.in_context(q, r)
            rows.append({"c": cls, "d": a + ">" + b, "p": place, "q": q, "x": x})
    return rows
