"""Reviewer's own generator primitives (rv-priya19), written from priya-spec/1 text, independent of the builder's suite.
All values synthetic and made at run time from seeded RNGs."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya19, file h/g1.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver replaced by a function. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import random
import unicodedata

EN = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
ES = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve"]
WORDS = {"en": EN, "es": ES}


def luhn_complete(body: str) -> str:
    s = 0
    for i, ch in enumerate(reversed(body)):
        x = int(ch)
        if i % 2 == 0:
            x = x * 2 - 9 if x > 4 else x * 2
        s += x
    return body + str((10 - s % 10) % 10)


def luhn_ok(d: str) -> bool:
    s = 0
    for i, ch in enumerate(reversed(d)):
        x = int(ch)
        if i % 2:
            x = x * 2 - 9 if x > 4 else x * 2
        s += x
    return s % 10 == 0


# (name, prefix generator, lengths)
NETWORKS = [
    ("visa", lambda r: "4", (13, 16, 19)),
    ("mc", lambda r: str(r.randint(51, 55)), (16,)),
    ("mc2", lambda r: str(r.randint(2221, 2720)), (16,)),
    ("amex", lambda r: r.choice(["34", "37"]), (15,)),
    ("disc", lambda r: r.choice(["6011", "65", "644", "649"]), (16, 19)),
    ("diners36", lambda r: "36", (14,)),
    ("diners38", lambda r: r.choice(["38", "39"]), (14, 16)),
    ("diners30", lambda r: "30" + str(r.randint(0, 5)), (14,)),
    ("jcb", lambda r: str(r.randint(3528, 3589)), (16, 19)),
    ("unionpay", lambda r: "62", (16, 17, 18, 19)),
    ("mir", lambda r: "220" + str(r.randint(0, 4)), (16,)),
    ("uatp", lambda r: "1", (15,)),
]


def card(r: random.Random) -> str:
    name, pre, lens = r.choice(NETWORKS)
    p = pre(r)
    n = r.choice(lens)
    body = p + "".join(str(r.randint(0, 9)) for _ in range(n - 1 - len(p)))
    return luhn_complete(body)


def nanp(r: random.Random) -> str:
    d = str(r.randint(2, 9)) + "%02d" % r.randint(0, 99) + str(r.randint(2, 9)) + "%02d" % r.randint(0, 99) \
        + "%04d" % r.randint(0, 9999)
    return ("1" + d) if r.random() < 0.3 else d


def e164(r: random.Random) -> str:
    n = r.randint(8, 15)
    return str(r.randint(1, 9)) + "".join(str(r.randint(0, 9)) for _ in range(n - 1))


def ssn(r: random.Random) -> str:
    while True:
        a = r.randint(1, 899)
        if a != 666:
            break
    return "%03d%02d%04d" % (a, r.randint(1, 99), r.randint(1, 9999))


def fmt_digits(r: random.Random, cls: str, d: str) -> str:
    """D01: the class's usual formats."""
    if cls == "card":
        k = r.randint(0, 3)
        if k == 0:
            return d
        sep = r.choice([" ", "-"])
        if len(d) == 15 and d[0] == "3":
            return sep.join([d[:4], d[4:10], d[10:]])
        return sep.join(d[i:i + 4] for i in range(0, len(d), 4))
    if cls == "nanp":
        one, core = ("1", d[1:]) if len(d) == 11 else ("", d)
        k = r.randint(0, 4)
        if k == 0:
            s = core
        elif k == 1:
            s = "(%s) %s-%s" % (core[:3], core[3:6], core[6:])
        elif k == 2:
            s = "-".join([core[:3], core[3:6], core[6:]])
        elif k == 3:
            s = ".".join([core[:3], core[3:6], core[6:]])
        else:
            s = " ".join([core[:3], core[3:6], core[6:]])
        if one:
            s = r.choice(["1 ", "1-", "+1 ", "1"]) + s
        return s
    if cls == "e164":
        k = r.randint(0, 2)
        if k == 0:
            return "+" + d
        if k == 1:
            return "+" + d[:2] + " " + " ".join(d[i:i + 3] for i in range(2, len(d), 3))
        return "+" + d[:1] + "-" + d[1:4] + "-" + d[4:]
    if cls == "ssn":
        k = r.randint(0, 3)
        if k == 0:
            return d
        sep = [" ", "-", "."][k - 1]
        return sep.join([d[:3], d[3:5], d[5:]])
    raise ValueError(cls)


def _chars(pred):
    return [chr(i) for i in range(0x110000) if pred(chr(i))]


ASCII_LET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
NFKC_LET = [c for c in _chars(lambda c: unicodedata.category(c)[0] == "L" and not c.isascii())
            if len(unicodedata.normalize("NFKC", c)) == 1 and unicodedata.normalize("NFKC", c) in ASCII_LET]
OTHER_LET = ([chr(i) for i in range(0x0430, 0x0450)] + [chr(i) for i in range(0x03B1, 0x03CA) if i != 0x03C2]
             + [chr(i) for i in range(0x05D0, 0x05EB)] + [chr(i) for i in range(0x0627, 0x064B)
                                                         if unicodedata.category(chr(i)) == "Lo"])


def _sep_ok(c):
    cat = unicodedata.category(c)
    if cat[0] not in "PSZ":
        return False
    n = unicodedata.normalize("NFKC", c)
    return len(n) == 1 and not any(x.isalnum() for x in n)


SEP_ALL = _chars(_sep_ok)
SEP_BY_CAT = {}
for _c in SEP_ALL:
    SEP_BY_CAT.setdefault(unicodedata.category(_c), []).append(_c)
SEP_CATS = sorted(SEP_BY_CAT)
# separators whose NFKC is longer than one character (U+2057 -> four primes): a side probe, not a matrix cell
SEP_EXPANDING = [c for c in _chars(lambda c: unicodedata.category(c)[0] in "PSZ")
                 if len(unicodedata.normalize("NFKC", c)) > 1
                 and not any(x.isalnum() for x in unicodedata.normalize("NFKC", c))]

ND_BLOCKS = sorted({ord(c) - unicodedata.digit(c) for c in _chars(lambda c: unicodedata.category(c) == "Nd")})
ND_BLOCKS_NONASCII = [z for z in ND_BLOCKS if z != 0x30]
ZW = ["\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2060",
      "\u2061", "\u2062", "\u2063", "\u2064", "\ufeff", "\u00ad"]


def to_block(s: str, z: int) -> str:
    return "".join(chr(z + int(c)) if c.isdigit() and c.isascii() else c for c in s)


def fullwidth(s: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else ("\u3000" if c == " " else c) for c in s)


def fw_digits(s: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if c.isdigit() and c.isascii() else c for c in s)


def insert_zw(r: random.Random, s: str) -> str:
    out = list(s)
    for _ in range(r.randint(1, 4)):
        out.insert(r.randint(0, len(out)), r.choice(ZW))
    return "".join(out)


def gap_str(r: random.Random, kind: str) -> str:
    """One gap (1-3 characters, or exactly 3) of the D-kind."""
    if kind == "ascii":
        return "".join(r.choice(ASCII_LET) for _ in range(r.randint(1, 3)))
    if kind == "three":
        pool = ASCII_LET + "".join(SEP_ALL[:200])
        return "".join(r.choice(pool) for _ in range(3))
    if kind == "nfkc":
        return "".join(r.choice(NFKC_LET) for _ in range(r.randint(1, 3)))
    if kind == "other":
        return "".join(r.choice(OTHER_LET) for _ in range(r.randint(1, 3)))
    if kind == "sep":
        cat = r.choice(SEP_CATS)
        return "".join(r.choice(SEP_BY_CAT[cat]) for _ in range(r.randint(1, 3)))
    if kind == "mixed":
        n = 3 if r.random() < 0.5 else r.randint(2, 3)
        while True:
            g = [r.choice(ASCII_LET) if r.random() < 0.5 else r.choice(SEP_ALL) for _ in range(n)]
            if any(c in ASCII_LET for c in g) and any(c not in ASCII_LET for c in g):
                return "".join(g)
    raise ValueError(kind)


def gapped(r: random.Random, d: str, kind: str, every: bool) -> str:
    """Digits d with gaps of the kind between digits: some gaps (at least one) or every gap."""
    n = len(d) - 1
    pos = set(range(n)) if every else set(r.sample(range(n), r.randint(1, max(1, n // 3))))
    out = []
    for i, c in enumerate(d):
        out.append(c)
        if i in pos:
            out.append(gap_str(r, kind))
    return "".join(out)
