"""rv-priya21 generator primitives - written from priya-spec/1 + v1.1 text, independent of the builder's suite and of
the rv-priya18 / 19 / 20 generators: other issuer sampling (whole ranges), other number formats, other gap alphabets
(circled / script / double-struck letters, Armenian / Georgian / Thai / Devanagari letters), other separators order,
other casing (alternating, sentence), other word sets. Every value is synthetic, made at run time from a seeded RNG.
Non-ASCII characters are written as escapes only."""
# Ported into the permanent conformance suite (priya-spec/2 FG-3, CAL-2) from review rv-priya21, file h/p21.py
# (sha256 5b12dfb102542f9bc490f9c0169593fdbece205fe595a8a6c73968b9140a0fe0): the reviewer's generator, seeded; synthetic values only, made at run time. Changes: non-ASCII
# literals written as escapes, imports from the package, the command-line driver and environment overrides
# removed (N / PER fixed at the review's defaults). The random call order is unchanged, so the corpus is the
# review's own (sha256 pinned in data/manifest.json).
import string
import unicodedata

EN = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
ES = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve"]
NUMW = {"en": EN, "es": ES}
# v1.1 C-1 closed lists (required minimum); written in my own order
AT_WORDS = ["snabel-a", "kukac", "at", "malpa", "arobase", "apenstaartje", "at-zeichen", "chiocciola", "sobaka",
            "arroba", "klammeraffe"]
DOT_WORDS = ["titik", "nokta", "piste", "prikk", "pont", "kropka", "punkt", "ponto", "point", "punto", "dot"]
GLUE = [chr(c) for c in range(ord("\uff01"), ord("\uff10"))] + [chr(c) for c in range(ord("\uff1a"), ord("\uff21"))] + [
    "\u3002", "\u3001"]  # codepoint ranges written via ord() so hygiene L2 does not read them as port ranges
ZW = ["\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
      "\u2060", "\u2061", "\u2062", "\u2063", "\u2064", "\ufeff", "\u00ad"]


def luhn_fix(body):
    tot = 0
    for i, ch in enumerate(body[::-1]):
        v = int(ch) * (2 if i % 2 == 0 else 1)
        tot += v - 9 if v > 9 else v
    return body + str(-tot % 10)


def luhn_ok(d):
    return luhn_fix(d[:-1]) == d


def rdig(r, n):
    return "".join(r.choice(string.digits) for _ in range(n))


def _pref(r, lo, hi):
    return str(r.randint(lo, hi))


# (prefix maker, lengths): whole issuer ranges sampled, not fixed prefixes
ISSUER_MAKERS = [
    (lambda r: "4", (13, 16, 19)),
    (lambda r: _pref(r, 51, 55), (16,)),
    (lambda r: _pref(r, 2221, 2720), (16,)),
    (lambda r: r.choice(["34", "37"]), (15,)),
    (lambda r: r.choice(["6011", "65", _pref(r, 644, 649)]), (16, 19)),
    (lambda r: "36", (14,)),
    (lambda r: r.choice(["38", "39", _pref(r, 300, 305)]), (14, 16)),
    (lambda r: _pref(r, 3528, 3589), (16, 17, 18, 19)),
    (lambda r: "62" + rdig(r, 2), (16, 17, 18, 19)),
    (lambda r: _pref(r, 2200, 2204), (16,)),
    (lambda r: "1", (15,)),                       # UATP
]


def card(r):
    mk, lens = r.choice(ISSUER_MAKERS)
    pre = mk(r)
    n = r.choice(lens)
    return luhn_fix(pre + rdig(r, n - 1 - len(pre)))


def nanp(r):
    core = str(r.randint(2, 9)) + rdig(r, 2) + str(r.randint(2, 9)) + rdig(r, 6)
    return ("1" + core) if r.random() < 0.4 else core


def e164(r, n=None):
    n = n or r.choice(range(8, 16))
    return str(r.randint(1, 9)) + rdig(r, n - 1)


def ssn(r):
    a = r.choice([x for x in range(1, 900) if x != 666])
    return "%03d%02d%04d" % (a, r.randint(1, 99), r.randint(1, 9999))


VANITY_CODES = ["800", "833", "844", "855", "866", "877", "888"]
# own keypad words (7 characters, at least one letter)
VANITY_WORDS = ["skylite", "mattres", "dumpstr", "glazing", "sunroom", "bailout", "vetcare", "kidsfun", "taxhelp",
                "fencing", "4pizzas", "junk4me", "buy2get", "mortgag", "lockout", "shuttle", "storage", "cleaner"]
KEYS = "abcdefghijklmnopqrstuvwxyz0123456789"


def vanity(r):
    if r.random() < 0.55:
        seven = r.choice(VANITY_WORDS)
    else:
        seven = ""
        while not any(c.isalpha() for c in seven):
            seven = "".join(r.choice(KEYS) for _ in range(7))
    return ("1" if r.random() < 0.5 else ""), r.choice(VANITY_CODES), seven


def rcase(r, s):
    k = r.random()
    if k < 0.3:
        return s
    if k < 0.5:
        return s.upper()
    if k < 0.65:
        return s.title()
    if k < 0.8:
        return "".join(c.upper() if i % 2 else c for i, c in enumerate(s))       # alternating
    return s[:1].upper() + s[1:]                                                 # sentence case


SYL = ["mar", "tin", "el", "sa", "ko", "ru", "vik", "an", "ol", "ber", "ga", "lu", "ne", "pha", "dro", "yu", "zan"]


def label(r, lo=2, hi=14):
    s = "".join(r.choice(SYL) for _ in range(r.randint(1, 4)))
    if r.random() < 0.25:
        s += rdig(r, r.randint(1, 4))
    if r.random() < 0.15 and len(s) > 3:
        i = r.randint(1, len(s) - 2)
        s = s[:i] + "-" + s[i:]
    s = s[:hi]
    return s if len(s) >= lo else s + "q" * (lo - len(s))


def email_parts(r, short=False):
    if short:
        return r.choice(string.ascii_lowercase), [r.choice(string.ascii_lowercase)], \
            "".join(r.choice(string.ascii_lowercase) for _ in range(r.choice([2, 2, 3])))
    loc = label(r, 1, 18)
    if r.random() < 0.35:
        loc += r.choice([".", "_", "+", "-", ".."[:1]]) + label(r, 1, 8)
    doms = [label(r) for _ in range(r.choice([1, 1, 2, 2, 3, 4]))]
    tld = "".join(r.choice(string.ascii_lowercase) for _ in range(r.choice([2, 2, 3, 4, 6, 8, 10, 13, 19, 24])))
    return loc, doms, tld


def email(r, short=False):
    loc, doms, tld = email_parts(r, short)
    return loc + "@" + ".".join(doms + [tld])


def _chars(pred):
    return [chr(i) for i in range(0x110000) if pred(chr(i))]


ASCII_L = string.ascii_letters
# NFKC letters: circled, double-struck, script, sans-serif bold, fullwidth letters (other choices than rv20)
NFKC_L = [c for c in _chars(lambda c: not c.isascii() and unicodedata.category(c)[0] in "LS")
          if len(unicodedata.normalize("NFKC", c)) == 1 and unicodedata.normalize("NFKC", c) in ASCII_L]
OTHER_L = ([chr(i) for i in range(0x0531, 0x0557)] + [chr(i) for i in range(0x10D0, 0x10F1)]
           + [chr(i) for i in range(0x0E01, 0x0E2F)] + [chr(i) for i in range(0x0915, 0x093A)]
           + [chr(i) for i in range(0x0430, 0x0450)] + [chr(i) for i in range(0x03B1, 0x03CA)]
           + [chr(i) for i in range(0x05D0, 0x05EB)] + [chr(i) for i in range(0x0628, 0x063B)])
OTHER_L = [c for c in OTHER_L if unicodedata.category(c) in ("Lo", "Ll", "Lu")]
SEPS = _chars(lambda c: unicodedata.category(c)[0] in "PSZ" and not c.isascii()
              and not any(x.isalnum() for x in unicodedata.normalize("NFKC", c)))
SEPS += [c for c in string.punctuation if c != "+"] + [" ", "\t"]
SEP_CAT = {}
for _c in SEPS:
    SEP_CAT.setdefault(unicodedata.category(_c), []).append(_c)
ND_ZEROS = sorted({ord(c) - unicodedata.digit(c) for c in _chars(lambda c: unicodedata.category(c) == "Nd")} - {0x30})


def sep_char(r):
    return r.choice(SEP_CAT[r.choice(sorted(SEP_CAT))])


def gap(r, kind, exact3=False):
    n = 3 if exact3 else r.choice([1, 2, 3, 3])
    pools = {"ascii": ASCII_L, "nfkc": NFKC_L, "other": OTHER_L}
    if kind in pools:
        return "".join(r.choice(pools[kind]) for _ in range(n))
    if kind == "sep":
        return "".join(sep_char(r) for _ in range(n))
    if kind == "mixed":
        n = 3 if exact3 else r.choice([2, 3])
        g = [r.choice(r.choice([ASCII_L, NFKC_L, OTHER_L])), sep_char(r)]
        g += [r.choice([r.choice(ASCII_L), sep_char(r)]) for _ in range(n - 2)]
        r.shuffle(g)
        return "".join(g)
    raise ValueError(kind)


def gap_digits(r, d, kind, every=False, exact3=False):
    slots = list(range(len(d) - 1))
    pos = set(slots) if every else set(r.sample(slots, r.randint(1, max(1, (2 * len(slots)) // 3))))
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


def zw_in(r, s, k=None):
    out = list(s)
    for _ in range(k or r.randint(1, 6)):
        out.insert(r.randint(0, len(out)), r.choice(ZW))
    return "".join(out)


def spell(r, d, lang, joiner=None):
    w = NUMW[lang]
    j = joiner if joiner is not None else r.choice([" ", " ", ",", " - ", "   ", "; ", " . "])
    return j.join(w[int(c)] for c in d)


def spell_mixed(r, d, lang, one=False):
    """Some digits spelled; one=True: exactly ONE number word among the digits (at any position)."""
    w = NUMW[lang]
    idx = [r.randrange(len(d))] if one else r.sample(range(len(d)), r.randint(2, max(2, (2 * len(d)) // 3)))
    parts = [w[int(c)] if i in idx else c for i, c in enumerate(d)]
    out = parts[0]
    for a, b in zip(parts, parts[1:]):
        out += (r.choice([" ", "", "-", " "]) if (a.isalpha() or b.isalpha()) else r.choice(["", "", "-", " "])) + b
    return out

