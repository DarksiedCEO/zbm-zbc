"""Reviewer's own composed pairs (CP-1..CP-4), structural: python g6.py OUT.jsonl PER_PAIR SEED
A digit value is UNITS (a digit, in ASCII / one script / fullwidth, or a number word) and GAPS between units; a gap is
only between units, never inside a word, never over 3 characters; a gap replaces the word joiner (CP-3).
E-mail pairs are b(a(value))."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya19, file h/g6.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver replaced by a function. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import random

from rvgen import rv19_g1 as g1
from rvgen import rv19_g2 as g2
from rvgen import rv19_g3 as g3

DTX = ["gap_ascii", "gap_nfkc", "gap_other", "gap_sep", "script_digits", "fullwidth", "zw_bidi", "spelled_en",
       "spelled_es", "spelled_run", "spelled_hyphen", "spelled_mixed"]
FAM = {"gap_ascii": "gap", "gap_nfkc": "gap", "gap_other": "gap", "gap_sep": "gap", "script_digits": "digitform",
       "fullwidth": "digitform", "zw_bidi": "zw", "spelled_en": "spelled", "spelled_es": "spelled",
       "spelled_run": "spelled", "spelled_hyphen": "spelled", "spelled_mixed": "spelled", "kp_spaced": "kp"}
VTX = DTX + ["kp_spaced"]
ETX = ["spelled", "bracketed", "underscored", "b64", "b32", "hex", "url", "rot13", "reversed", "wordrev", "chunk"]
EFAM = {"spelled": "spelled", "bracketed": "spelled", "underscored": "spelled", "b64": "enc", "b32": "enc",
        "hex": "enc", "url": "enc", "rot13": "rot", "reversed": "rev", "wordrev": "rev", "chunk": "chunk"}


def pairs(tx, fam):
    return [(a, b) for a in tx for b in tx if a != b and not (fam[a] == fam[b] and fam[a] in ("gap", "spelled",
                                                                                              "digitform"))]


class Val:
    def __init__(self, digits):
        self.u = [["a", c] for c in digits]          # unit: [form, digit] form a / s:<z> / f / w:<lang>
        self.g = [None] * (len(digits) - 1)          # gap text between units i and i+1
        self.join = " "                              # joiner of two words (spelled mode)
        self.lang = "en"

    def render(self):
        out = []
        for i, (form, c) in enumerate(self.u):
            if form == "a":
                t = c
            elif form.startswith("s:"):
                t = chr(int(form[2:], 16) + int(c))
            elif form == "f":
                t = chr(0xFF10 + int(c))
            else:
                t = g1.WORDS[form[2:]][int(c)]
            out.append(t)
            if i < len(self.g):
                nxt_word = self.u[i + 1][0].startswith("w:")
                if self.g[i] is not None:
                    out.append(self.g[i])
                elif form.startswith("w:") or nxt_word:
                    out.append(self.join if (form.startswith("w:") and nxt_word) else " ")
        return "".join(out)


def apply(r, v, t, first):
    n = len(v.u)
    subset = (lambda k: set(range(k))) if first else (lambda k: set(r.sample(range(k), max(1, r.randint(k // 2, k)))))
    if t.startswith("gap_"):
        kind = {"gap_ascii": "ascii", "gap_nfkc": "nfkc", "gap_other": "other", "gap_sep": "sep"}[t]
        pos = set(range(n - 1)) if r.random() < 0.4 else set(r.sample(range(n - 1), r.randint(1, max(1, (n - 1) // 2))))
        for p in pos:
            v.g[p] = g1.gap_str(r, kind)
    elif t == "zw_bidi":
        for p in r.sample(range(n - 1), r.randint(1, min(4, n - 1))):
            cur = v.g[p] or ""
            if len(cur) < 3:
                v.g[p] = cur + r.choice(g1.ZW)
    elif t in ("script_digits", "fullwidth"):
        z = "%x" % r.choice(g1.ND_BLOCKS_NONASCII)
        for i in subset(n):
            if v.u[i][0] == "a" or not first:
                v.u[i][0] = ("s:" + z) if t == "script_digits" else "f"
    elif t.startswith("spelled_"):
        lang = {"spelled_es": "es", "spelled_en": "en"}.get(t) or r.choice(["en", "es"])
        if t == "spelled_mixed":
            idx = set(r.sample(range(n), r.randint(1, max(1, n // 2))))
        else:
            idx = subset(n)
        for i in idx:
            v.u[i][0] = "w:" + lang
        v.join = {"spelled_run": "", "spelled_hyphen": "-"}.get(t, " ")
    elif t == "kp_spaced":
        pass
    else:
        raise ValueError(t)


def digit_pair(r, c, a, b):
    d = g3.VAL[c](r)
    v = Val(d)
    apply(r, v, a, True)
    apply(r, v, b, False)
    s = v.render()
    return ("+" + r.choice(["", " "]) + s) if c == "e164" else s


def vanity_pair(r, a, b):
    one, code, kp = g2.vanity_parts(r)
    v = Val(one + code)
    for k, t in enumerate((a, b)):
        if t == "kp_spaced":
            kp = " ".join(kp)
        else:
            apply(r, v, t, k == 0)
    return v.render() + r.choice([" ", "-"]) + kp


def etx(r, t, s, p):
    if t == "spelled":
        return g2.email_spelled(r, p, r.choice(list(g2.LANG_AT_DOT))) if s == g2.email_plain(p) else s.replace(
            "@", " at ").replace(".", " dot ")
    if t == "bracketed":
        return g2.email_bracketed(r, p, "en") if s == g2.email_plain(p) else s.replace("@", " [at] ").replace(".", " [dot] ")
    if t == "underscored":
        return g2.email_underscored(r, p, "en") if s == g2.email_plain(p) else s.replace("@", "_at_").replace(".", "_dot_")
    if t in ("b64", "b32", "hex", "url"):
        raw = s.encode()
        import base64
        if t == "b64":
            return base64.b64encode(raw).decode().rstrip("=" if r.random() < 0.5 else "")
        if t == "b32":
            return g2.b32(s, r.random() < 0.3, r.random() < 0.5)
        if t == "hex":
            return raw.hex()
        return "".join("%%%02X" % x for x in raw) if r.random() < 0.5 else s.replace("@", "%40")
    if t == "rot13":
        return g2.rot13(s)
    if t == "reversed":
        return s[::-1]
    if t == "wordrev":
        return " ".join(w[::-1] for w in s.split(" "))
    if t == "chunk":
        return g2.chunk(r, s)
    raise ValueError(t)


def cases(per, seed, only=None):
    """The review's composed-pair driver: per cases for each ordered pair, one RNG per pair
    (``only``: a list of (class, a, b) jobs, in its order)."""
    if True:
        jobs = [(c, a, b) for c in ("card", "nanp", "e164", "ssn") for a, b in pairs(DTX, FAM)]
        jobs += [("vanity", a, b) for a, b in pairs(VTX, FAM)]
        jobs += [("email", a, b) for a in ETX for b in ETX if a != b]
        for c, a, b in (only or jobs):
            r = random.Random("%d|%s|%s|%s" % (seed, c, a, b))
            for i in range(per):
                ctx = i % 4 == 0                       # 25% in context
                x = r.randint(1, 12) if ctx else 0
                if c == "email":
                    p = g2.email_parts(r, minlen=16)
                    s = etx(r, b, etx(r, a, g2.email_plain(p), p), p)
                elif c == "vanity":
                    s = vanity_pair(r, a, b)
                else:
                    s = digit_pair(r, c, a, b)
                q = g3.contextual(r, s, x)
                yield {"c": c, "a": a, "b": b, "x": x, "q": q}
