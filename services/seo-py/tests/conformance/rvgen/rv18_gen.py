"""rv-priya18 independent corpus generator. All personal-data values are synthetic and made at run time.
Nothing here is imported from the builder's corpus."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya18, file rv/gen.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver replaced by a function. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
from __future__ import annotations

import base64
import random
import string
import unicodedata
import urllib.parse

# ----------------------------------------------------------------------------- values
def luhn_complete(prefix: str, length: int, rnd: random.Random) -> str:
    d = prefix + "".join(rnd.choice(string.digits) for _ in range(length - len(prefix) - 1))
    for c in "0123456789":
        s = d + c
        tot = 0
        for i, ch in enumerate(reversed(s)):
            x = int(ch)
            if i % 2:
                x = x * 2 - 9 if x > 4 else x * 2
            tot += x
        if tot % 10 == 0:
            return s
    raise AssertionError


CARD_NETS = [  # (name, prefix chooser, lengths)
    ("visa", lambda r: "4", (13, 16, 19)),
    ("mc", lambda r: str(r.randint(51, 55)), (16,)),
    ("mc2", lambda r: str(r.randint(2221, 2720)), (16,)),
    ("amex", lambda r: r.choice(["34", "37"]), (15,)),
    ("disc", lambda r: r.choice(["6011", "65", str(r.randint(644, 649))]), (16, 19)),
    ("diners", lambda r: r.choice([str(r.randint(300, 305)), "36", "38", "39"]), (14, 16)),
    ("diners36", lambda r: "36", (14,)),
    ("jcb", lambda r: str(r.randint(3528, 3589)), (16, 19)),
    ("unionpay", lambda r: "62", (16, 17, 18, 19)),
    ("maestro", lambda r: r.choice(["50", "56", "57", "58", "6304", "67"]), (13, 16, 19)),
    ("mir", lambda r: str(r.randint(2200, 2204)), (16,)),
    ("rupay", lambda r: r.choice(["60", "81", "82", "508"]), (16,)),
    ("verve", lambda r: r.choice(["506", "650"]), (16, 19)),
    ("troy", lambda r: "9792", (16,)),
    ("uatp", lambda r: "1", (15,)),
]


def card(r: random.Random) -> tuple[str, str]:
    """(digits, formatted)"""
    name, pf, lens = r.choice(CARD_NETS) if r.random() > 0.15 else r.choice([CARD_NETS[-1], CARD_NETS[6]])
    n = r.choice(lens)
    d = luhn_complete(pf(r), n, r)
    if n == 15 and name in ("amex", "uatp"):
        groups = [d[:4], d[4:10], d[10:]] if name == "amex" else [d[:4], d[4:9], d[9:]]
    elif n == 14:
        groups = [d[:4], d[4:10], d[10:]]
    else:
        groups = [d[i:i + 4] for i in range(0, n, 4)]
    sep = r.choice([" ", "-", "", " "])
    return d, sep.join(groups)


def nanp(r: random.Random) -> tuple[str, str]:
    a = str(r.randint(2, 9)) + "".join(r.choice(string.digits) for _ in range(2))
    e = str(r.randint(2, 9)) + "".join(r.choice(string.digits) for _ in range(2))
    s = "".join(r.choice(string.digits) for _ in range(4))
    one = r.random() < 0.3
    fmt = r.choice(["{a}-{e}-{s}", "({a}) {e}-{s}", "{a}.{e}.{s}", "{a} {e} {s}", "{a}{e}{s}", "({a}){e}-{s}"])
    f = fmt.format(a=a, e=e, s=s)
    if one:
        f = r.choice(["1 ", "1-", "+1 ", "1"]) + f
    return ("1" if one else "") + a + e + s, f


E164_CC = ["44", "49", "33", "34", "39", "31", "46", "47", "61", "81", "82", "86", "91", "52", "55", "27", "234", "971",
           "353", "351", "48", "90", "7", "62", "63", "64", "65", "66", "20", "212"]


def e164(r: random.Random) -> tuple[str, str]:
    cc = r.choice(E164_CC)
    total = r.randint(max(8, len(cc) + 6), 15)
    rest = str(r.randint(1, 9)) + "".join(r.choice(string.digits) for _ in range(total - len(cc) - 1))
    d = cc + rest
    style = r.random()
    if style < 0.35:
        f = "+" + d
    elif style < 0.7:
        parts, i = [], 0
        while i < len(rest):
            k = r.randint(2, 4)
            parts.append(rest[i:i + k])
            i += k
        f = "+" + cc + " " + " ".join(parts)
    else:
        f = "+" + cc + "-" + rest[:3] + "-" + rest[3:]
    return "+" + d, f


def ssn(r: random.Random) -> tuple[str, str]:
    while True:
        a = r.randint(1, 899)
        if a != 666:
            break
    g = r.randint(1, 99)
    s = r.randint(1, 9999)
    d = f"{a:03d}{g:02d}{s:04d}"
    f = r.choice([f"{a:03d}-{g:02d}-{s:04d}", f"{a:03d} {g:02d} {s:04d}", d, f"{a:03d}.{g:02d}.{s:04d}"])
    return d, f


VANITY_WORDS = ["flowers", "getcash", "callnow", "lawyers", "plumber", "hotdogs", "carpets", "dentist", "roofing",
                "contact", "goforit", "mattres", "pizzas1", "4mycar1", "go4help", "fixit4u", "helpnow", "sunshin",
                "rentals", "cleaner", "towtruk", "movers1", "mytaxi1", "a1roofs", "repairs", "jewelry", "insurer"]


def vanity(r: random.Random) -> tuple[str, str]:
    code = r.choice(["800", "833", "844", "855", "866", "877", "888"])
    w = r.choice(VANITY_WORDS).replace("-", "")
    if r.random() < 0.4:  # random keypad chars with >= 1 letter
        w = "".join(r.choice(string.ascii_lowercase + string.digits) for _ in range(7))
        if not any(c.isalpha() for c in w):
            w = "a" + w[1:]
    if r.random() < 0.5:
        w = w.upper()
    one = r.random() < 0.5
    sep = r.choice(["-", " ", ".", "", " - "])
    body = r.choice([w, w[:3] + sep + w[3:]])
    f = (r.choice(["1-", "1 ", "1", "1."]) if one else "") + r.choice([code + sep, "(" + code + ") ", code + " "]) + body
    return ("1" if one else "") + code + w.lower(), f


FIRST = ["anna", "ben", "carla", "dmitri", "elena", "farid", "grace", "hiro", "ines", "jon", "kemal", "lucia", "marta",
         "nils", "olu", "priti", "quinn", "rosa", "sven", "tara", "ugo", "vera", "wei", "ximena", "yusuf", "zoe"]
LAST = ["alvarez", "brown", "chen", "dubois", "eriksen", "fischer", "garcia", "haddad", "ivanova", "jones", "kowalski",
        "lopez", "moreau", "nakamura", "okafor", "patel", "quintero", "rossi", "silva", "tanaka", "ueda", "vasquez",
        "weber", "xu", "yilmaz", "zhang"]
SYL = ["mail", "post", "net", "web", "box", "inbox", "corp", "tech", "data", "home", "star", "blue", "north", "acme",
       "zen", "fast", "kiwi", "orbit", "nova", "pixel", "river", "stone", "cloud", "metro"]
TLDS = (["com"] * 8 + ["net", "org", "de", "fr", "es", "it", "nl", "co.uk", "com.au", "io", "info", "biz", "ca", "us",
        "mx", "br", "jp", "in", "edu", "gov", "app", "dev", "shop", "online"]
        + ["consulting", "photography", "london", "berlin", "church", "family", "studio", "marketing", "works",
           "email", "solutions", "ninja", "guru", "realty", "law", "travel", "museum", "academy", "coffee"])


def email(r: random.Random) -> tuple[str, str]:
    f, l = r.choice(FIRST), r.choice(LAST)  # noqa: E741
    local = r.choice([f + "." + l, f + l, f[0] + l, f + "_" + l, f + l + str(r.randint(1, 99)), f + "+" + r.choice(SYL),
                      l + "." + f, f + "-" + l])
    dom = r.choice(SYL) + r.choice(["", r.choice(SYL), "-" + r.choice(SYL), str(r.randint(1, 9))])
    e = local + "@" + dom + "." + r.choice(TLDS)
    return e, e


STREET_NAMES = ["main", "oak", "maple", "elm", "cedar", "pine", "washington", "lake", "hill", "park", "sunset",
                "kings", "queens", "mill", "church", "river", "spring", "north ridge", "willow creek", "martin luther king",
                "old mill", "good hope", "vista", "hawthorne", "jefferson", "lincoln", "bay", "harbor", "orchard"]
STRONG = ["st", "street", "ave", "avenue", "blvd", "boulevard", "rd", "dr", "ln", "ct", "pl", "ter", "cir", "hwy",
          "pkwy", "trl"]
ORDINARY = ["road", "lane", "drive", "court", "way", "terrace", "place", "loop", "plaza"]
STATES = ["ca", "ny", "tx", "fl", "wa", "il", "ma", "ga", "nc", "az", "co", "nj", "texas", "california", "ohio"]


def address(r: random.Random) -> tuple[str, str]:
    num = str(r.randint(1, 99999))
    if r.random() < 0.1:
        num += r.choice("ab")
    d = r.choice(["", "", "", "n ", "s ", "e ", "w ", "ne ", "sw ", "north ", "west "])
    suf = r.choice(STRONG + ORDINARY)
    a = f"{num} {d}{r.choice(STREET_NAMES)} {suf}"
    x = r.random()
    if x < 0.25:
        a += r.choice([" apt ", " suite ", " unit ", " #"]) + str(r.randint(1, 999))
    elif x < 0.45:
        a += " " + r.choice(STATES) + " " + f"{r.randint(10000, 99999)}"
    return num, a


CLASSES = {"card": card, "nanp": nanp, "e164": e164, "ssn": ssn, "vanity": vanity, "email": email,
           "address": address}
DIGIT_CLASSES = ("card", "nanp", "e164", "ssn")

# ----------------------------------------------------------------------------- transform material
ZW_BIDI = ["\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
           "\u2060", "\u2061", "\u2062", "\u2063", "\u2064", "\u2066", "\u2067", "\u2068", "\u2069", "\ufeff",
           "\u061c", "\u180e", "\u00ad", "\u034f", "\u206a", "\u206b", "\u206c", "\u206d", "\u206e", "\u206f"]


def nd_blocks() -> list:
    """Every Unicode decimal-digit block: list of zero code points (cat Nd, digit value 0, ten in a row)."""
    zeros = []
    for cp in range(0x110000):
        c = chr(cp)
        if unicodedata.category(c) == "Nd" and unicodedata.digit(c, None) == 0:
            if all(unicodedata.digit(chr(cp + k), None) == k for k in range(10)):
                zeros.append(cp)
    return zeros


ND_ZEROS = nd_blocks()
NON_ASCII_ZEROS = [z for z in ND_ZEROS if z != 0x30]

LETTERS_ASCII = string.ascii_letters
# letters NFKC turns into ASCII letters: fullwidth, math bold/italic/script/fraktur/double-struck/sans/mono, circled
NFKC_LETTERS = [c for c in (chr(x) for x in list(range(0xFF21, 0xFF21 + 26)) + list(range(0xFF41, 0xFF41 + 26))
                            + list(range(0x1D400, 0x1D6A4)) + list(range(0x24B6, 0x24EA)))
                if unicodedata.category(c).startswith("L") or unicodedata.category(c) == "So"]
NFKC_LETTERS = [c for c in NFKC_LETTERS if len(unicodedata.normalize("NFKC", c)) == 1
                and unicodedata.normalize("NFKC", c).isascii() and unicodedata.normalize("NFKC", c).isalpha()]
OTHER_LETTERS = list("\u0430\u0431\u0432\u0433\u0434\u0435\u0436\u0437\u0438\u0439\u043a\u043b\u043c\u043d\u043e\u043f\u0440\u0441\u0442\u0443\u0444\u0445\u0446\u0447\u0448\u0449\u044b\u044d\u044e\u044f\u03b1\u03b2\u03b3\u03b4\u03b5\u03b6\u03b7\u03b8\u03b9\u03ba\u03bb\u03bc\u03bd\u03be\u03c0\u03c1\u03c3\u03c4\u03c5\u03c6\u03c7\u03c8\u03c9\u05d0\u05d1\u05d2\u05d3\u05d4\u05d5\u05d6\u05d7\u05d8\u05d9\u05db\u05dc\u05de\u05e0\u05e1\u05e2\u05e4\u05e6\u05e7\u05e8\u05e9\u05ea\u0627\u0628\u062a\u062b\u062c\u062d\u062e\u062f\u0630\u0631\u0632\u0633\u0634\u0635\u0636\u0637\u0638\u0639\u063a\u0641\u0642\u0643\u0644\u0645\u0646\u0647\u0648\u064a"
                     "\u3042\u3044\u3046\u3048\u304a\u304b\u304d\u304f\u3051\u3053\u3055\u3057\u3059\u305b\u305d\u30a2\u30a4\u30a6\u30a8\u30aa\u30ab\u30ad\u30af\u30b1\u30b3\u6f22\u5b57\u4e2d\u56fd\u4eba\u65e5\u672c\u8a9e\ud55c\uad6d\uc5b4\uac00\ub098\ub2e4\ub77c\ub9c8\ubc14\uc0ac\u0905\u0906\u0907\u0908\u0909\u090a\u0915\u0916\u0917\u0918"
                     "\u0e01\u0e02\u0e04\u0e07\u0e08\u0e09\u0e0a\u0e0b\u03b1\u03b2\u03b3")
SEPARATORS = list(" -./_,:;()[]{}<>|\\*~^'\"!?&=#$%@`") + ["\u2010", "\u2011", "\u2012", "\u2013", "\u2014",
                                                           "\u2015", "\u2212", "\u00b7", "\u2022", "\u00a0", "\u3000",
                                                           "\u2007", "\u202f", "\u3001", "\u3002", "\uff0c", "\uff0e",
                                                           "\u00a7", "\u00b6", "\u2020", "\u2605", "\u2192",
                                                           "\U0001F600", "\U0001F4DE", "\u00bb", "\u00ab"]
# '+' as a gap char would make a later group look like an E.164 start - include it, it is a separator too
SEPARATORS.append("+")
ALL_GAP = {"ascii_letters": list(LETTERS_ASCII), "nfkc_letters": NFKC_LETTERS, "other_letters": OTHER_LETTERS,
           "separators": SEPARATORS}

EN = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
ES = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve"]

CONTEXT = [
    "call {} now", "my number is {} thanks", "pls text {} asap", "{} who is this", "is {} a scam",
    "mi numero es {} gracias", "llamame al {} por favor", "rappelez moi au {} svp", "meine nummer ist {} danke",
    "chiamami al {} grazie", "meu numero {} obrigado", "\u043d\u043e\u043c\u0435\u0440 {} \u0441\u043f\u0430\u0441\u0438\u0431\u043e", "{} \u96fb\u8a71\u756a\u53f7", "\u7535\u8bdd {} \u8c22\u8c22",
    "{} \uc804\ud654\ubc88\ud638 \uc54c\ub824\uc918", "\u0627\u062a\u0635\u0644 \u0628\u064a {} \u0634\u0643\u0631\u0627", "\u092e\u0947\u0930\u093e \u0928\u0902\u092c\u0930 {} \u0939\u0948", "\U0001F4DE {} \U0001F600", "\u300c{}\u300d",
    "send to {} please", "contact {} for details", "{} contact", "reach me at {} after work",
    "pago con tarjeta {} vence", "card {} cvv", "ssn {} lookup", "email {} support", "envia a {} hoy",
    "\u00e9crivez \u00e0 {} merci", "schreib an {} bitte", "lives at {} near the park", "deliver to {} tonight",
    "entrega en {} ma\u00f1ana", "{} reviews and complaints", "who lives at {}",
]


def in_context(v: str, r: random.Random) -> str:
    return r.choice(CONTEXT).format(v)


# ----------------------------------------------------------------------------- digit disguises
def _gap_between(d: str, r: random.Random, pool: list, lo: int, hi: int, every: bool) -> str:
    out = [d[0]]
    for c in d[1:]:
        if every or r.random() < 0.5:
            out.append("".join(r.choice(pool) for _ in range(r.randint(lo, hi))))
        out.append(c)
    return "".join(out)


def gap_ascii(d, r):
    return _gap_between(d, r, ALL_GAP["ascii_letters"], 1, 3, r.random() < 0.5)


def gap_ascii3(d, r):  # exactly 3 everywhere
    return _gap_between(d, r, ALL_GAP["ascii_letters"], 3, 3, True)


def gap_nfkc(d, r):
    return _gap_between(d, r, ALL_GAP["nfkc_letters"], 1, 3, r.random() < 0.5)


def gap_other(d, r):
    return _gap_between(d, r, ALL_GAP["other_letters"], 1, 3, r.random() < 0.5)


def gap_sep(d, r):
    return _gap_between(d, r, ALL_GAP["separators"], 1, 3, r.random() < 0.5)


def gap_mixed(d, r):
    pool = ALL_GAP["ascii_letters"] + ALL_GAP["separators"] + ALL_GAP["other_letters"] + ALL_GAP["nfkc_letters"]
    return _gap_between(d, r, pool, 1, 3, r.random() < 0.5)


def gap_mixed3(d, r):
    def g():
        return r.choice(LETTERS_ASCII) + r.choice(SEPARATORS) + r.choice(LETTERS_ASCII + "".join(OTHER_LETTERS))
    out = [d[0]]
    for c in d[1:]:
        out.append(g())
        out.append(c)
    return "".join(out)


def script_digits(d, r):
    z = r.choice(NON_ASCII_ZEROS)
    return "".join(chr(z + int(c)) if c.isdigit() else c for c in d)


def fullwidth(d, r):
    return "".join(chr(0xFF10 + int(c)) if c.isdigit() else c for c in d)


def zw(d, r):
    out = []
    for c in d:
        out.append(c)
        if r.random() < 0.6:
            out.append("".join(r.choice(ZW_BIDI) for _ in range(r.randint(1, 3))))
    return "".join(out)


def spelled_en(d, r):
    return " ".join(EN[int(c)] if c.isdigit() else c for c in d if c not in " -.()")


def spelled_es(d, r):
    return " ".join(ES[int(c)] if c.isdigit() else c for c in d if c not in " -.()")


def spelled_hyphen(d, r):
    w = EN if r.random() < 0.5 else ES
    return "-".join(w[int(c)] if c.isdigit() else c for c in d if c not in " -.()")


def spelled_run(d, r):
    w = EN if r.random() < 0.5 else ES
    return "".join(w[int(c)] if c.isdigit() else c for c in d if c not in " -.()")


def spelled_mixed(d, r):
    w = EN if r.random() < 0.5 else ES
    return " ".join((w[int(c)] if r.random() < 0.5 else c) if c.isdigit() else c for c in d if c not in " -.()")


def spelled_upper(d, r):
    return spelled_en(d, r).upper()


DIGIT_T = {"plain": None, "gap_ascii": gap_ascii, "gap_ascii3": gap_ascii3, "gap_nfkc": gap_nfkc,
           "gap_other": gap_other, "gap_sep": gap_sep, "gap_mixed": gap_mixed, "gap_mixed3": gap_mixed3,
           "script_digits": script_digits, "fullwidth": fullwidth, "zw_bidi": zw, "spelled_en": spelled_en,
           "spelled_es": spelled_es, "spelled_hyphen": spelled_hyphen, "spelled_run": spelled_run,
           "spelled_mixed": spelled_mixed, "spelled_upper": spelled_upper}


def digit_disguise(name: str, formatted: str, digits: str, r: random.Random) -> str:
    """Apply a disguise to the formatted value. For gap disguises the digits are the bare digit string (with '+'
    kept for E.164) so the gap is the only separator; for others the formatted value."""
    fn = DIGIT_T[name]
    if fn is None:
        return formatted
    # gap disguises act on the bare digits (E.164 keeps its '+'), so the inserted gap is the only gap (<= 3)
    base = formatted if name in ("script_digits", "fullwidth", "zw_bidi") or (not name.startswith("gap")
                                                                          and r.random() < 0.5) else digits
    return fn(base, r)


# ----------------------------------------------------------------------------- vanity disguises
def vanity_disguise(name, formatted, r):
    if name == "plain":
        return formatted
    if name == "zw_bidi":
        return zw(formatted, r)
    if name == "fullwidth":
        return "".join(chr(ord(c) + 0xFEE0) if c.isalnum() and c.isascii() else c for c in formatted)
    if name == "script_digits":
        return script_digits(formatted, r)
    bare = "".join(c for c in formatted if c.isalnum())
    if name == "gap_sep":      # 1-3 separators between keypad characters, inside the 8xx code too
        return _gap_between(bare, r, [c for c in SEPARATORS if not c.isalnum()], 1, 3, r.random() < 0.5)
    if name == "gap_letters_code":   # letters only inside the digit part (1 + 8xx): the list's digit gap
        k = 4 if bare[0] == "1" else 3
        return _gap_between(bare[:k], r, list(LETTERS_ASCII), 1, 3, True) + " " + bare[k:]
    if name == "spelled_en":
        k = 4 if bare[0] == "1" else 3
        return " ".join(EN[int(c)] for c in bare[:k]) + " " + bare[k:]
    if name == "spelled_es":
        k = 4 if bare[0] == "1" else 3
        return " ".join(ES[int(c)] for c in bare[:k]) + " " + bare[k:]
    if name == "spelled_spaced":
        k = 4 if bare[0] == "1" else 3
        return " ".join(EN[int(c)] for c in bare[:k]) + " " + " ".join(bare[k:])
    raise KeyError(name)


VANITY_T = ["plain", "zw_bidi", "fullwidth", "script_digits", "gap_sep", "gap_letters_code", "spelled_en", "spelled_es",
            "spelled_spaced"]


# ----------------------------------------------------------------------------- e-mail disguises
def rot13(s):
    return s.translate(str.maketrans(string.ascii_lowercase + string.ascii_uppercase,
                                     string.ascii_lowercase[13:] + string.ascii_lowercase[:13]
                                     + string.ascii_uppercase[13:] + string.ascii_uppercase[:13]))


def chunk(s: str, r: random.Random, size: tuple = (2, 8)) -> str:
    out, i = [], 0
    while i < len(s):
        k = r.randint(*size)
        out.append(s[i:i + k])
        i += k
    return " ".join(out)


AT_SPELL = [" at ", " AT ", " (at) ", " [at] ", " {at} ", " <at> ", " [@] ", "(at)", "[at]", " arroba ", " ARROBA ",
            "[arroba]", " (arroba) "]
DOT_SPELL = [" dot ", " DOT ", " (dot) ", " [dot] ", " {dot} ", " <dot> ", " [.] ", "(dot)", "[dot]", " punto ",
             "[punto]", " PUNTO ", " (punto) "]
EXOTIC_AT = [" _at_ ", " -at- ", "_at_", "-at-"]
EXOTIC_DOT = [" _dot_ ", " -dot- ", "_dot_", "-dot-"]


def spelled_email(e, r, bracket=False):
    local, dom = e.split("@")
    ats = [a for a in AT_SPELL if ("[" in a or "(" in a or "{" in a or "<" in a) == bracket] or AT_SPELL
    dots = [a for a in DOT_SPELL if ("[" in a or "(" in a or "{" in a or "<" in a) == bracket] or DOT_SPELL
    d = r.choice(dots)
    sp_local = local.replace(".", d) if r.random() < 0.5 else local
    return sp_local + r.choice(ats) + dom.replace(".", d)


def b64(e, r):
    raw = base64.b64encode(e.encode()).decode()
    if r.random() < 0.5:
        raw = raw.rstrip("=")
    return raw


def b64url(e, r):
    raw = base64.urlsafe_b64encode(e.encode()).decode()
    if r.random() < 0.5:
        raw = raw.rstrip("=")
    return raw


def b32(e, r):
    raw = base64.b32encode(e.encode()).decode()
    if r.random() < 0.5:
        raw = raw.rstrip("=")
    if r.random() < 0.5:
        raw = raw.lower()
    return raw


def hexe(e, r):
    h = e.encode().hex()
    return h.upper() if r.random() < 0.5 else h


def urlenc_whole(e, r):
    s = "".join("%{:02X}".format(b) if r.random() < 0.5 else "%{:02x}".format(b) for b in e.encode())
    return s


def urlenc_part(e, r):
    return e.replace("@", r.choice(["%40", "%40"])).replace(".", r.choice(["%2E", "%2e", "."]))


def urlenc_lib(e, r):
    return urllib.parse.quote(e, safe="")


def rev_whole(e, r):
    return e[::-1]


def rev_words(e, r):
    return " ".join(w[::-1] for w in e.split())


EMAIL_T = {
    "plain": lambda e, r: e,
    "spelled": lambda e, r: spelled_email(e, r, False),
    "bracketed": lambda e, r: spelled_email(e, r, True),
    "b64": b64, "b64url": b64url, "b32": b32, "hex": hexe,
    "url_whole": urlenc_whole, "url_part": urlenc_part, "url_lib": urlenc_lib,
    "rot13": lambda e, r: rot13(e), "rev_whole": rev_whole,
    "zw_bidi": lambda e, r: zw(e, r),
    "fullwidth": lambda e, r: "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in e),
    "spelled_exotic": lambda e, r: e.split("@")[0] + r.choice(EXOTIC_AT) + e.split("@")[1].replace(".", r.choice(EXOTIC_DOT)),
    "plain_spaced": lambda e, r: e.replace("@", " @ ").replace(".", " . "),
}
# reversal of the whole query (context included) / of each word, and rot13 of whole query, are applied after context
CHUNKABLE = ["spelled", "bracketed", "b64", "b64url", "b32", "hex", "url_whole", "rot13", "rev_whole", "plain"]


def email_case(name: str, e: str, r: random.Random, ctx: bool, chunked: bool) -> str:
    v = EMAIL_T[name](e, r)
    if chunked:
        v = chunk(v, r)
    return in_context(v, r) if ctx else v


# whole-query transforms of the list (applied to a query that already holds the e-mail, context included)
def whole_rot13(q, r):
    return rot13(q)


def whole_rev(q, r):
    return q[::-1]


def words_rev(q, r):
    return " ".join(w[::-1] for w in q.split())


# ----------------------------------------------------------------------------- address disguises
def addr_disguise(name, a, r):
    if name == "plain":
        return a
    if name == "zw_bidi":
        return zw(a, r)
    if name == "fullwidth_digits":
        return fullwidth(a, r)
    if name == "script_digits":
        return script_digits(a, r)
    if name == "fullwidth_all":
        return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else ("\u3000" if c == " " else c) for c in a)
    if name == "upper":
        return a.upper()
    if name == "nfkc_letters":
        m = {chr(0x61 + i): chr(0x1D41A + i) for i in range(26)}   # math bold small
        return "".join(m.get(c, c) if r.random() < 0.5 else c for c in a)
    raise KeyError(name)


ADDR_T = ["plain", "zw_bidi", "fullwidth_digits", "script_digits", "fullwidth_all", "upper", "nfkc_letters"]
ADDR_CONTEXT = ["{} hours", "deliver to {} tonight", "{} open late tonight near me", "who lives at {}",
                "direcci\u00f3n {} por favor gracias", "{} \u3067\u3059", "\u300c {} \u300d \U0001F600", "{}, thanks!",
                "send it to {} asap", "{} - is this the right address?", "adresse: {} merci beaucoup",
                "mi casa {} ven", "{} zillow", "{} for sale price history", "{} 2 bedroom apartment rent",
                "parcel {} not delivered yet what do i do"]
