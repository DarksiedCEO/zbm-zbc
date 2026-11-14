"""rv-priya20 generator primitives - written from priya-spec/1 + v1.1 text, independent of the builder's suite and of
the rv-priya18 / rv-priya19 generators (different value shapes, word sets, casing, scripts, glue and contexts).
Every value is synthetic and made at run time from a seeded RNG. Non-ASCII characters are written as escapes."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya20, file h/p20.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver removed. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import string
import unicodedata

EN = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
ES = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve"]
NUMW = {"en": EN, "es": ES}
AT_WORDS = ["at", "arroba", "arobase", "chiocciola", "klammeraffe", "at-zeichen", "malpa", "kukac", "snabel-a",
            "apenstaartje", "sobaka"]
DOT_WORDS = ["dot", "punto", "point", "ponto", "punkt", "kropka", "pont", "prikk", "piste", "nokta", "titik"]
# C-2 glue punctuation: fullwidth U+FF01-U+FF0F, U+FF1A-U+FF20, ideographic full stop / comma
GLUE = [chr(c) for c in range(0xFF01, 0xFF01 + 15)] + [chr(c) for c in range(0xFF1A, 0xFF1A + 7)] + ["\u3002", "\u3001"]
ZW = ["\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
      "\u2060", "\u2061", "\u2062", "\u2063", "\u2064", "\ufeff", "\u00ad"]


def luhn_fix(body: str) -> str:
    tot = 0
    for i, ch in enumerate(body[::-1]):
        v = int(ch) * (2 if i % 2 == 0 else 1)
        tot += v - 9 if v > 9 else v
    return body + str(-tot % 10)


def rdig(r, n):
    return "".join(r.choice(string.digits) for _ in range(n))


ISSUERS = [("4", (13, 16, 19)), ("51", (16,)), ("53", (16,)), ("55", (16,)), ("2221", (16,)), ("2499", (16,)),
           ("2720", (16,)), ("34", (15,)), ("37", (15,)), ("6011", (16, 19)), ("65", (16,)), ("647", (16,)),
           ("36", (14,)), ("38", (14,)), ("39", (14,)), ("300", (14,)), ("305", (14,)), ("3530", (16,)),
           ("3589", (19,)), ("62", (16, 19)), ("2200", (16,)), ("2204", (16,)), ("1", (15,))]


def card(r):
    pre, lens = r.choice(ISSUERS)
    n = r.choice(lens)
    return luhn_fix(pre + rdig(r, n - 1 - len(pre)))


def nanp(r):
    core = str(r.randint(2, 9)) + rdig(r, 2) + str(r.randint(2, 9)) + rdig(r, 6)
    return "1" + core if r.random() < 0.35 else core


def e164(r):
    return str(r.randint(1, 9)) + rdig(r, r.choice([8, 9, 10, 11, 12, 13, 14, 15]) - 1)


def ssn(r):
    a = r.choice([x for x in range(1, 900) if x != 666])
    return "%03d%02d%04d" % (a, r.randint(1, 99), r.randint(1, 9999))


VANITY_CODES = ["800", "833", "844", "855", "866", "877", "888"]
KEYPAD = "abcdefghijklmnopqrstuvwxyz"
VANITY_WORDS = ["plumber", "getcash", "callnow", "lawyers", "flowers", "rentcar", "hotdeal", "moveout", "fixroof",
                "petcare", "dentist", "tow4you", "go2bank", "carpets", "roofer1", "hvac247", "jobs4us", "loans4u"]


def vanity(r):
    """(lead-1, code, 7 keypad chars with at least one letter)."""
    seven = r.choice(VANITY_WORDS) if r.random() < 0.5 else ""
    while not seven or not any(c.isalpha() for c in seven):
        seven = "".join(r.choice(KEYPAD + "23456789") for _ in range(7))
    return ("1" if r.random() < 0.5 else ""), r.choice(VANITY_CODES), seven


def rcase(r, s):
    k = r.random()
    if k < 0.4:
        return s
    if k < 0.6:
        return s.upper()
    if k < 0.8:
        return s.title()
    return "".join(c.upper() if r.random() < 0.5 else c for c in s)


LOC_SYL = ["ka", "zo", "mi", "ru", "te", "lo", "va", "ni", "qu", "xe", "bo", "fi", "da", "we", "yo", "ha"]


def label(r, lo=2, hi=12):
    s = "".join(r.choice(LOC_SYL) for _ in range(r.randint(1, 4)))
    if r.random() < 0.3:
        s += rdig(r, r.randint(1, 3))
    if r.random() < 0.15 and len(s) > 3:
        i = r.randint(1, len(s) - 2)
        s = s[:i] + "-" + s[i:]
    return s[:hi] if len(s) >= lo else s + "x" * (lo - len(s))


def email_parts(r):
    loc = label(r, 1, 20)
    if r.random() < 0.3:
        loc += r.choice([".", "_", "+", "-"]) + label(r, 1, 8)
    doms = [label(r) for _ in range(r.choice([1, 1, 1, 2, 3]))]
    tld = "".join(r.choice(string.ascii_lowercase) for _ in range(r.choice([2, 2, 3, 3, 4, 5, 7, 11, 16, 24])))
    return loc, doms, tld


def email(r):
    loc, doms, tld = email_parts(r)
    return loc + "@" + ".".join(doms + [tld])


def _chars(pred):
    return [chr(i) for i in range(0x110000) if pred(chr(i))]


ASCII_L = string.ascii_letters
NFKC_L = [c for c in _chars(lambda c: not c.isascii() and unicodedata.category(c)[0] == "L")
          if len(unicodedata.normalize("NFKC", c)) == 1 and unicodedata.normalize("NFKC", c) in ASCII_L]
OTHER_L = ([chr(i) for i in range(0x0410, 0x0450)] + [chr(i) for i in range(0x0391, 0x03CA)
                                                      if unicodedata.category(chr(i))[0] == "L"]
           + [chr(i) for i in range(0x05D0, 0x05EB)] + [chr(i) for i in range(0x0620, 0x064B)
                                                       if unicodedata.category(chr(i)) == "Lo"])
SEPS = _chars(lambda c: unicodedata.category(c)[0] in "PSZ" and not c.isascii()
              and not any(x.isalnum() for x in unicodedata.normalize("NFKC", c)))
SEPS += [c for c in string.punctuation if c != "+"] + [" "]
SEP_CAT = {}
for _c in SEPS:
    SEP_CAT.setdefault(unicodedata.category(_c), []).append(_c)
ND_ZEROS = sorted({ord(c) - unicodedata.digit(c) for c in _chars(lambda c: unicodedata.category(c) == "Nd")} - {0x30})


def sep_char(r):
    return r.choice(SEP_CAT[r.choice(sorted(SEP_CAT))])


def gap(r, kind, exact3=False):
    n = 3 if exact3 else r.randint(1, 3)
    if kind == "ascii":
        return "".join(r.choice(ASCII_L) for _ in range(n))
    if kind == "nfkc":
        return "".join(r.choice(NFKC_L) for _ in range(n))
    if kind == "other":
        return "".join(r.choice(OTHER_L) for _ in range(n))
    if kind == "sep":
        return "".join(sep_char(r) for _ in range(n))
    if kind == "mixed":
        n = 3 if exact3 else r.randint(2, 3)
        g = [r.choice(ASCII_L), sep_char(r)] + [r.choice([r.choice(ASCII_L), sep_char(r)]) for _ in range(n - 2)]
        r.shuffle(g)
        return "".join(g)
    raise ValueError(kind)


def gap_digits(r, d, kind, every=False, exact3=False):
    slots = list(range(len(d) - 1))
    pos = set(slots) if every else set(r.sample(slots, r.randint(1, max(1, len(slots) // 2))))
    return "".join(c + (gap(r, kind, exact3) if i in pos else "") for i, c in enumerate(d))


def to_block(s, z):
    return "".join(chr(z + int(c)) if "0" <= c <= "9" else c for c in s)


def fw(s, digits_only=False):
    out = []
    for c in s:
        if "0" <= c <= "9" or (not digits_only and "!" <= c <= "~"):
            out.append(chr(ord(c) + 0xFEE0))
        elif c == " " and not digits_only:
            out.append("\u3000")
        else:
            out.append(c)
    return "".join(out)


def zw_in(r, s):
    out = list(s)
    for _ in range(r.randint(1, 5)):
        out.insert(r.randint(0, len(out)), r.choice(ZW))
    return "".join(out)


def spell(r, d, lang, joiner=None):
    w = NUMW[lang]
    j = joiner if joiner is not None else r.choice([" ", " ", ", ", "-", "  ", " / "])
    return j.join(w[int(c)] for c in d)


def spell_mixed(r, d, lang, one=False):
    """Some digits spelled; one=True: exactly ONE number word among digits."""
    w = NUMW[lang]
    idx = [r.randrange(len(d))] if one else r.sample(range(len(d)), r.randint(2, max(2, len(d) // 2)))
    parts = []
    for i, c in enumerate(d):
        parts.append(w[int(c)] if i in idx else c)
    out = parts[0]
    for a, b in zip(parts, parts[1:], strict=False):
        out += (r.choice([" ", "", "-"]) if (a.isalpha() or b.isalpha()) else r.choice(["", "", " "])) + b
    return out
