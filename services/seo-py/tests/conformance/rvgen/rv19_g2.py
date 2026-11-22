"""Reviewer generator part 2: spelled forms, vanity, e-mail, address, contexts."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya19, file h/g2.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver replaced by a function. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import base64
import codecs
from urllib.parse import quote, quote_plus

from rvgen.rv19_g1 import WORDS

# ---------------------------------------------------------------- spelled digits (D11-D14)


def spelled(r, d, lang, mode="space"):
    w = WORDS[lang]
    toks = [w[int(c)] for c in d]
    if mode == "space":
        return " ".join(toks)
    if mode == "hyphen":
        return "-".join(toks)
    if mode == "run":
        return "".join(toks)
    if mode == "upper":
        return " ".join(toks).upper()
    raise ValueError(mode)


def spelled_mixed(r, d, lang, one=False):
    """D14: digits with number words among them; one=True: exactly ONE number word."""
    w = WORDS[lang]
    n = len(d)
    pos = {r.randrange(n)} if one else set(r.sample(range(n), r.randint(2, max(2, n // 2))))
    out = []
    for i, c in enumerate(d):
        if i in pos:
            out.append(" " + w[int(c)] + " ")
        else:
            out.append(c)
    return " ".join("".join(out).split())


# ---------------------------------------------------------------- vanity (C4)
CODES = ["800", "833", "844", "855", "866", "877", "888"]
KEYPAD_WORDS = ["flowers", "gofedex", "callnow", "getcash", "pizzahut", "mattres", "dentist", "lawyers", "hotels",
                "carpets", "plumber", "roofing"]


def vanity_parts(r):
    one = r.random() < 0.5
    code = r.choice(CODES)
    if r.random() < 0.5:
        kp = r.choice(KEYPAD_WORDS)[:7].ljust(7, "s")
    else:
        kp = "".join(r.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(r.randint(1, 7)))
        kp = "".join(r.sample(list(kp + "".join(str(r.randint(0, 9)) for _ in range(7 - len(kp)))), 7))
        if not any(c.isalpha() for c in kp):
            kp = "a" + kp[1:]
    kp = kp.upper() if r.random() < 0.5 else kp
    return ("1" if one else ""), code, kp


def vanity_join(r, one, code, kp, code_txt=None):
    c = code_txt if code_txt is not None else code
    k = r.randint(0, 3)
    if k == 0:
        s = c + "-" + kp
    elif k == 1:
        s = c + " " + kp
    elif k == 2:
        s = "(" + c + ") " + kp
    else:
        s = c + "-" + kp[:3] + "-" + kp[3:]
    if one:
        s = one + r.choice(["-", " ", "", "."]) + s
    return s


# ---------------------------------------------------------------- e-mail (C6)
SYL = ["ja", "ne", "mar", "co", "li", "sa", "ro", "ber", "ta", "ki", "mo", "ra", "vel", "dan", "pe", "tri", "lu",
       "zo", "ha", "ny"]
LANG_AT_DOT = {"en": ("at", "dot"), "es": ("arroba", "punto"), "pt": ("arroba", "ponto"), "fr": ("arobase", "point"),
               "it": ("chiocciola", "punto"), "de": ("klammeraffe", "punkt")}
COMMON_TLD = ["com", "net", "org", "io", "co", "uk", "de", "info", "dev", "app"]


def word(r, a=2, b=4):
    return "".join(r.choice(SYL) for _ in range(r.randint(a, b)))


def email_parts(r, minlen=0):
    while True:
        local = word(r)
        if r.random() < 0.3:
            local += r.choice([".", "_", ""]) + word(r, 1, 2)
        if r.random() < 0.3:
            local += str(r.randint(1, 99))
        labels = [word(r, 2, 3)]
        if r.random() < 0.2:
            labels.append(word(r, 1, 2))
        if r.random() < 0.5:
            tld = r.choice(COMMON_TLD)
        else:
            tld = "".join(r.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(r.randint(2, 24)))
        e = local + "@" + ".".join(labels) + "." + tld
        if len(e) >= minlen:
            return local, labels, tld


def email_plain(p):
    local, labels, tld = p
    return local + "@" + ".".join(labels) + "." + tld


def rcase(r, w):
    k = r.randint(0, 2)
    return w if k == 0 else (w.upper() if k == 1 else w.capitalize())


def email_spelled(r, p, lang):
    at, dot = LANG_AT_DOT[lang]
    local, labels, tld = p
    parts = [local, rcase(r, at)] + sum(([lb, rcase(r, dot)] for lb in labels), [])[:-1] + [rcase(r, dot), tld]
    return " ".join(parts)


AT_BR = ["[at]", "(at)", "{at}", "<at>", "[@]"]
DOT_BR = ["[.]", "(dot)", "[dot]", "{dot}", "<dot>"]


def email_bracketed(r, p, lang):
    local, labels, tld = p
    if lang == "en":
        at, dot = r.choice(AT_BR), r.choice(DOT_BR)
    else:
        a, d = LANG_AT_DOT[lang]
        o, c = r.choice(["[]", "()", "{}", "<>"])
        at, dot = o + a + c, o + d + c
    sp = r.choice([" ", ""])
    return local + sp + at + sp + (sp + dot + sp).join(labels) + sp + dot + sp + tld


def email_underscored(r, p, lang):
    local, labels, tld = p
    a, d = LANG_AT_DOT[lang]
    s1 = r.choice(["_", "-"])
    s2 = r.choice(["_", "-"])
    at = s1 + a + s1
    dot = s2 + d + s2
    if r.random() < 0.5:
        at, dot = " " + at + " ", " " + dot + " "
    return local + at + dot.join(labels) + dot + tld


def b32(s, lower, pad):
    e = base64.b32encode(s.encode()).decode()
    if not pad:
        e = e.rstrip("=")
    return e.lower() if lower else e


def encodings(r, e):
    """D19 forms: (subkind, text)."""
    raw = e.encode()
    k = r.randint(0, 9)
    if k == 0:
        return "b64std_pad", base64.b64encode(raw).decode()
    if k == 1:
        return "b64std_nopad", base64.b64encode(raw).decode().rstrip("=")
    if k == 2:
        return "b64url", base64.urlsafe_b64encode(raw).decode().rstrip("=" if r.random() < 0.5 else "")
    if k == 3:
        return "b32up", b32(e, False, r.random() < 0.5)
    if k == 4:
        return "b32low", b32(e, True, r.random() < 0.5)
    if k == 5:
        return "hex", raw.hex() if r.random() < 0.5 else raw.hex().upper()
    if k == 6:
        return "url_whole", "".join("%%%02X" % b for b in raw)
    if k == 7:
        return "url_part", e.replace("@", "%40")
    if k == 8:
        return "url_lib", quote(e, safe="")
    return "url_plus", quote_plus(e).replace(".", "%2E" if r.random() < 0.5 else ".")


def rot13(s):
    return codecs.encode(s, "rot13")


def chunk(r, s):
    """D21: pieces with spaces at random interior positions (1-3 cuts)."""
    if len(s) < 3:
        return s
    cuts = sorted(r.sample(range(1, len(s)), min(len(s) - 1, r.randint(1, 3))))
    out, last = [], 0
    for c in cuts:
        out.append(s[last:c])
        last = c
    out.append(s[last:])
    return " ".join(x for x in out)


# ---------------------------------------------------------------- address (C7)
NAMES = ["oak", "maple", "elm", "main", "cedar", "pine", "walnut", "chestnut", "willow", "birch", "park", "lake",
         "hill", "river", "sunset", "highland", "lincoln", "jefferson", "madison", "franklin", "kings", "queens",
         "church", "mill", "spring", "meadow", "forest", "valley", "ridge", "harbor", "cherry", "jackson", "adams",
         "grant", "union", "market", "bridge", "garden", "laurel", "magnolia"]
STRONG = ["st", "ave", "blvd", "rd", "dr", "ln", "ct", "pl", "ter", "cir", "hwy", "pkwy", "street", "avenue",
          "boulevard"]
ORDINARY = ["road", "lane", "drive", "court", "way", "terrace", "place", "loop", "plaza"]
DIRS = ["n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west"]
CITIES = ["springfield", "riverside", "fairview", "georgetown", "salem", "boise", "austin", "dayton"]
STATES = ["ca", "tx", "ny", "fl", "il", "pa", "wa", "ga", "nc", "az", "oregon", "texas", "ohio", "idaho"]
UK_AREAS = ["sw", "se", "nw", "n", "e", "w", "ec", "wc", "b", "m", "l", "ls", "ox", "cb", "eh", "g", "cf", "bs", "ab",
            "yo"]
FR = ["rue", "chemin", "impasse", "quai", "allee"]


def street(r, kind):
    num = str(r.randint(1, 99999))
    parts = [num]
    if r.random() < 0.3:
        parts.append(r.choice(DIRS))
    parts += r.sample(NAMES, r.randint(1, 3))
    parts.append(r.choice(STRONG if kind == "strong" else ORDINARY))
    return " ".join(parts)


def zipc(r, plus4=False):
    z = "%05d" % r.randint(1001, 99950)
    return z + ("-%04d" % r.randint(1, 9999) if plus4 else "")


def address(r, form=None):
    """D01 forms of the address rule; returns (form, text)."""
    form = form or r.choice(["strong", "ordinary", "full", "pobox", "pmb", "rr", "zip4", "statezip", "uk", "ca", "fr"])
    if form in ("strong", "ordinary"):
        return form, street(r, form)
    if form == "full":
        s = street(r, r.choice(["strong", "ordinary"]))
        return form, s + ", " + r.choice(CITIES) + ", " + r.choice(STATES) + " " + zipc(r, r.random() < 0.3)
    if form == "pobox":
        return form, r.choice(["po box ", "p.o. box ", "PO Box "]) + str(r.randint(1, 99999))
    if form == "pmb":
        return form, "pmb " + str(r.randint(1, 9999))
    if form == "rr":
        return form, r.choice(["rr ", "hc ", "rural route "]) + str(r.randint(1, 99)) + " box " + str(r.randint(1, 999))
    if form == "zip4":
        return form, r.choice(CITIES) + " " + zipc(r, True)
    if form == "statezip":
        return form, r.choice(CITIES) + " " + r.choice(STATES) + " " + zipc(r)
    if form == "uk":
        dist = str(r.randint(1, 20))
        if len(dist) == 1 and r.random() < 0.2:
            dist += r.choice("abehmnprw")
        while True:     # the rule's inward part: never an ordinal / size unit (ADR 0047: st nd rd th tb gb ...)
            inward = str(r.randint(0, 9)) + "".join(r.choice("abdefghjlnpqrstuwxyz") for _ in range(2))
            if inward[1:] not in ("st", "nd", "rd", "th", "tb", "gb", "kb", "pb", "hp", "lb", "ft", "hz", "dr", "bd"):
                break
        return form, r.choice(UK_AREAS) + dist + " " + inward
    if form == "ca":
        L1, L = "abceghjklmnprstvxy", "abceghjklmnprstvwxyz"
        return form, (r.choice(L1) + str(r.randint(0, 9)) + r.choice(L) + " " + str(r.randint(0, 9)) + r.choice(L)
                      + str(r.randint(0, 9)))
    if form == "fr":
        k = r.choice(FR + ["place", "route", "avenue", "boulevard"])
        art = r.choice(["de la ", "du ", "des ", "de ", "d'"]) if k in ("place", "route", "avenue", "boulevard") \
            else r.choice(["", "de la ", "du ", "des "])
        return form, str(r.randint(1, 400)) + " " + k + " " + art + r.choice(["sherbrooke", "lilas", "seine",
                                                                              "concorde", "moulin", "roses"])
    raise ValueError(form)
