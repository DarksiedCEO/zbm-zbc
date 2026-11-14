"""Review round 18: a generator for the CLOSED threat list of ADR 0047 ("Personal data", the list), and an ordinary-query
corpus to measure over-drop against. Every personal-data value is SYNTHETIC and made at run time from a seeded random
generator (valid Luhn / issuer prefix, NANP shape, SSA area rules, made-up mailbox names); nothing personal-data
shaped is written in this file. ``python -m privacy_r18_corpus`` (from ``tests/``, ``src`` on the path) prints counts
and classes only.

The list (ADR 0047): card numbers, phone numbers (NANP, E.164, vanity toll-free), SSNs, e-mail addresses and street
addresses; for the digit values: any gap of up to 3 non-digit characters between digits (letters, NFKC letters and
any separator), digits in any script or fullwidth, zero-width / bidi characters, spelled digits (English, Spanish);
for e-mails: spelled, bracketed, base64 / base32 / hex / URL-encoded, rot13 or reversed, whole or chunked; any of
these inside surrounding text; composed pairs (two list transforms on one value)."""

from __future__ import annotations

import base64
import codecs
import random
import unicodedata

J = "".join

# ------------------------------------------------------------------ values
# (network, issuer prefixes, length): prefixes as (lo, hi) inclusive ranges of the leading digits
NETWORKS = (
    ("visa", ((4, 4),), (13, 16, 19)), ("mastercard", ((51, 55), (2221, 2720)), (16,)),
    ("amex", ((34, 34), (37, 37)), (15,)), ("discover", ((6011, 6011), (644, 649), (65, 65)), (16, 19)),
    ("diners", ((300, 305), (36, 36), (38, 39)), (14, 16)), ("jcb", ((3528, 3589),), (16, 19)),
    ("unionpay", ((62, 62),), (16, 17, 18, 19)), ("maestro", ((50, 50), (56, 58), (6304, 6304), (67, 67)), (16, 19)),
    ("mir", ((2200, 2204),), (16,)), ("rupay", ((60, 60), (81, 82), (508, 508)), (16,)),
    ("troy", ((9792, 9792),), (16,)), ("uatp", ((1, 1),), (15,)),
)


def _luhn_digit(body: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(body)):
        d = int(ch)
        if i % 2 == 0:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return str((10 - total % 10) % 10)


def card(rng: random.Random) -> str:
    _name, prefixes, lengths = rng.choice(NETWORKS)
    lo, hi = rng.choice(prefixes)
    p = str(rng.randint(lo, hi))
    n = rng.choice(lengths)
    body = p + J(str(rng.randint(0, 9)) for _ in range(n - 1 - len(p)))
    return body + _luhn_digit(body)


def _nxx(rng: random.Random) -> str:
    while True:
        s = str(rng.randint(2, 9)) + str(rng.randint(0, 8)) + str(rng.randint(0, 9))
        if s[1:] != "11":
            return s


def nanp(rng: random.Random) -> str:
    return _nxx(rng) + _nxx(rng) + J(str(rng.randint(0, 9)) for _ in range(4))


def ssn(rng: random.Random) -> str:
    while True:
        a = rng.randint(1, 899)
        if a != 666:
            return f"{a:03d}{rng.randint(1, 99):02d}{rng.randint(1, 9999):04d}"


_CC = ("44", "49", "33", "34", "39", "61", "81", "91", "52", "55", "31", "46", "353", "27", "65")


def e164(rng: random.Random) -> str:
    cc = rng.choice(_CC)
    return cc + J(str(rng.randint(0, 9)) for _ in range(rng.randint(9, 10)))


_NAMES = ("jane", "john", "maria", "li", "wei", "omar", "fatima", "ivan", "olga", "kenji", "aiko", "pedro", "ana",
          "lucas", "emma", "noah", "zoe", "raj", "priya", "chen", "sofia", "mateo", "lena", "yusuf", "nora", "elif",
          "tom", "sam", "alex", "kim", "dana", "max", "ines", "hugo", "mia", "leo", "ada", "eva", "ray", "jo")
_SURNAMES = ("doe", "smith", "garcia", "nguyen", "kowalski", "mueller", "rossi", "silva", "tanaka", "khan", "obi",
             "jensen", "dubois", "novak", "park", "lopez", "brown", "martin", "fischer", "ito", "costa", "ali")
_PROVIDERS = ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "proton.me", "aol.com",
              "gmx.de", "web.de", "yandex.ru", "mail.com", "zoho.com", "example.com", "example.org", "example.net",
              "fastmail.com", "qq.com", "live.co.uk", "orange.fr", "libero.it")
_WORDS_DOMAIN = ("acme", "bluefin", "northwind", "contoso", "fabrikam", "globex", "initech", "umbrella", "vandelay",
                 "hooli", "piedpiper", "wayne", "stark", "oscorp", "tyrell", "cyberdyne", "soylent", "wonka")
_TLDS = ("com", "net", "org", "io", "co", "us", "de", "fr", "es", "it", "co.uk", "com.au", "ca", "nl", "info", "biz")


def email(rng: random.Random) -> str:
    first, last = rng.choice(_NAMES), rng.choice(_SURNAMES)
    local = rng.choice((first + "." + last, first + last, first[0] + last, first + "_" + last, first + "." + last
                        + str(rng.randint(1, 99)), last + str(rng.randint(1950, 2010)), first + "-" + last))
    dom = rng.choice(_PROVIDERS) if rng.random() < 0.6 else rng.choice(_WORDS_DOMAIN) + "." + rng.choice(_TLDS)
    return local + "@" + dom


_STREETS = ("main", "oak", "maple", "cedar", "elm", "pine", "washington", "lake", "hill", "park", "sunset", "river",
            "church", "mill", "spring", "highland", "franklin", "lincoln", "jackson", "madison", "willow", "walnut")
_SUFFIXES = ("st", "street", "ave", "avenue", "blvd", "boulevard", "rd", "dr", "ln", "ct", "pl", "hwy", "pkwy",
             "road", "lane", "drive", "court", "way", "terrace", "place")
_DIRS = ("", "", "", "n ", "s ", "e ", "w ", "north ", "south ")
_CITY_STATE = ("", "", "springfield il 62704", "austin tx 78701", "denver co 80202", "boise id 83702",
               "miami fl 33130", "seattle wa 98101", "", "apt 4b", "suite 210", "unit 12")


def address(rng: random.Random) -> str:
    s = f"{rng.randint(1, 99999)} {rng.choice(_DIRS)}{rng.choice(_STREETS)} {rng.choice(_SUFFIXES)}"
    tail = rng.choice(_CITY_STATE)
    return (s + " " + tail).strip()


_KEYPAD = {c: str(d) for d, letters in ((2, "abc"), (3, "def"), (4, "ghi"), (5, "jkl"), (6, "mno"), (7, "pqrs"),
                                        (8, "tuv"), (9, "wxyz")) for c in letters}
_VANITY_WORDS = ("flowers", "callnow", "getcash", "lawyers", "plumber", "rooting", "carpets", "dentist", "movers",
                 "helpnow", "gotjunk", "tickets", "hotels", "rentals", "cleaner", "roofers", "pestfix", "towing")


def vanity(rng: random.Random) -> str:
    """A toll-free code and seven keypad characters (letters, a few digits), as written: returns the letters form."""
    code = rng.choice(("800", "833", "844", "855", "866", "877", "888"))
    w = rng.choice(_VANITY_WORDS)
    w = (w + J(str(rng.randint(0, 9)) for _ in range(7)))[:7]
    if rng.random() < 0.3:
        i = rng.randrange(7)
        w = w[:i] + str(rng.randint(0, 9)) + w[i + 1:]
    return code + w


# ------------------------------------------------------------------ digit transforms
# separator characters by category (P*, S*, Z*), none NFKC-turns into a letter or digit; zero-width / bidi format chars
def _category_chars(prefixes: str, step: int) -> tuple:
    out, n = [], 0
    for cp in range(0x20, 0x30000):
        c = chr(cp)
        if unicodedata.category(c)[0] not in prefixes:
            continue
        if any(ch.isalnum() for ch in unicodedata.normalize("NFKC", c)):
            continue
        n += 1
        if n % step == 0:
            out.append(c)
    return tuple(out)


SEPARATORS = _category_chars("PSZ", 5)
INVISIBLE = ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c",
             "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069", "\u061c", "\u180e", "\u00ad", "\u034f")
ASCII_LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _nfkc_letters() -> tuple:
    """Characters that NFKC turns into an ASCII letter (fullwidth, mathematical, circled, modifier letters ...)."""
    out = []
    for cp in range(0x80, 0x20000):
        c = chr(cp)
        n = unicodedata.normalize("NFKC", c)
        if len(n) == 1 and n in ASCII_LETTERS:
            out.append(c)
    return tuple(out)


NFKC_LETTERS = _nfkc_letters()
OTHER_LETTERS = ("\u0430", "\u0431", "\u0436", "\u03b1", "\u03bb", "\u05d0", "\u0627", "\u0628", "\u4e00", "\u6771",
                 "\u3042", "\u30a2", "\uac00", "\u0e01", "\u0915", "\u00e9", "\u00df", "\u00f1", "\u0142")


def nd_zeros() -> tuple:
    """The zero of every decimal-digit (Nd) block Unicode has, ASCII excluded."""
    out = []
    for cp in range(0x80, 0x110000):
        c = chr(cp)
        if unicodedata.category(c) == "Nd" and unicodedata.digit(c) == 0:
            if all(unicodedata.category(chr(cp + k)) == "Nd" and unicodedata.digit(chr(cp + k)) == k
                   for k in range(10)):
                out.append(cp)
    return tuple(out)


ND_ZEROS = nd_zeros()
EN = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
ES = ("cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve")


def _split(rng: random.Random, d: str) -> list:
    """Random grouping of a digit string: groups of 1-4 digits."""
    out, i = [], 0
    while i < len(d):
        k = rng.randint(1, 4)
        out.append(d[i:i + k])
        i += k
    return out


def _gap(rng: random.Random, pool) -> str:
    return J(rng.choice(pool) for _ in range(rng.randint(1, 3)))


def _join_digits(rng, d: str, pool) -> str:
    """Every digit boundary of a random grouping gets a gap of 1-3 characters from ``pool``."""
    return J(g + (_gap(rng, pool) if i < len(gs) - 1 else "") for gs in [_split(rng, d)] for i, g in enumerate(gs))


def _map_ascii_digits(t: str, f) -> str:
    return J(f(int(c)) if "0" <= c <= "9" else c for c in t)


def _plain_digits(rng, cls: str, d: str) -> str:
    if cls == "card":
        if len(d) == 15:
            gs = [d[:4], d[4:10], d[10:]]
        else:
            gs = [d[i:i + 4] for i in range(0, len(d), 4)]
        return rng.choice((" ", "-", "", ".")).join(gs)
    if cls == "ssn":
        return rng.choice(("-", " ", "", ".")).join((d[:3], d[3:5], d[5:]))
    if cls == "phone_intl":
        return "+" + rng.choice((d, d[:2] + " " + d[2:5] + " " + d[5:], d[:2] + "-" + d[2:6] + "-" + d[6:]))
    a, e, line = d[:3], d[3:6], d[6:]
    return rng.choice((f"({a}) {e}-{line}", f"{a}-{e}-{line}", f"{a}.{e}.{line}", f"{a}{e}{line}",
                       f"+1 {a} {e} {line}", f"1-{a}-{e}-{line}", f"{a} {e} {line}"))


def _prefix(cls: str) -> str:
    return "+" if cls == "phone_intl" else ""


def _spell(rng, t: str) -> str:
    """Every ASCII digit spelled (English or Spanish, sometimes a few digits left); other characters kept."""
    words = rng.choice((EN, ES))
    sep = rng.choice((" ", " ", "-", ", ", "", " "))
    keep = rng.random() < 0.3
    out = []
    for c in t:
        if "0" <= c <= "9":
            out.append(c if keep and rng.random() < 0.25 else words[int(c)])
        elif c.strip():
            out.append(c)
    return sep.join(out)


def _script(rng, t: str) -> str:
    z = rng.choice(ND_ZEROS)
    mixed = rng.random() < 0.3
    return _map_ascii_digits(t, lambda k: chr((rng.choice(ND_ZEROS) if mixed else z) + k))


def _fullwidth(t: str) -> str:
    return J(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in t)


def _invisible(rng, t: str) -> str:
    return J(c + (J(rng.choice(INVISIBLE) for _ in range(rng.randint(1, 2))) if rng.random() < 0.7 else "")
             for c in t)


# each digit transform takes (rng, cls, text) where text is the value's current form (its digits are ASCII digits
# until a script / fullwidth transform runs); the first transform of a chain receives the bare digit string
def _t_plain(rng, cls, t):
    return _plain_digits(rng, cls, t) if t.isdigit() else t


def _regroup(rng, cls, t, pool):
    if t.isdigit():
        return _prefix(cls) + _join_digits(rng, t, pool)
    # already transformed: put gaps between existing characters at random digit boundaries
    out = []
    for i, c in enumerate(t):
        out.append(c)
        if c.isdigit() and i + 1 < len(t) and t[i + 1].isdigit() and rng.random() < 0.6:
            out.append(_gap(rng, pool))
    return J(out)


DIGIT_TRANSFORMS = {
    "plain": _t_plain,
    "sep_gap": lambda rng, cls, t: _regroup(rng, cls, t, SEPARATORS),
    "letter_gap": lambda rng, cls, t: _regroup(rng, cls, t, ASCII_LETTERS),
    "nfkc_letter_gap": lambda rng, cls, t: _regroup(rng, cls, t, NFKC_LETTERS),
    "mixed_gap": lambda rng, cls, t: _regroup(rng, cls, t, ASCII_LETTERS + J(SEPARATORS[::7]) + J(OTHER_LETTERS)
                                              + J(NFKC_LETTERS[::50])),
    "script_digits": lambda rng, cls, t: _script(rng, _t_plain(rng, cls, t)),
    "fullwidth": lambda rng, cls, t: _fullwidth(_t_plain(rng, cls, t)),
    "zero_width_bidi": lambda rng, cls, t: _invisible(rng, _t_plain(rng, cls, t)),
    "spelled": lambda rng, cls, t: _spell(rng, _t_plain(rng, cls, t)),
}


def vanity_form(rng, v: str) -> str:
    """A vanity toll-free number as written: with or without the leading 1, any separators of the list (0-3
    punctuation / symbol / space characters) between the code and the letters and inside the letters, any case."""
    code, rest = v[:3], v[3:]
    seps = (" ", "-", ".", "", "\u2022", "\u2013", "/", "*", "~", " - ", "\u30fb", "\u00b7")
    lead = rng.choice(("", "1", "1 ", "1-", "+1 ", "1.", "1\u2022"))
    code = rng.choice((code, "(" + code + ")"))
    cut = rng.choice(([3], [], [3], [2], [1, 4]))
    body, last = [], 0
    for c in cut:
        body.append(rest[last:c])
        last = c
    body.append(rest[last:])
    s = rng.choice(seps)
    text = lead + code + rng.choice(seps[:4]) + s.join(body)
    return J(c.upper() if rng.random() < 0.3 else c for c in text)


# ------------------------------------------------------------------ e-mail transforms
_AT_FORMS = (" at ", " [at] ", " (at) ", " {at} ", " <at> ", "[@]", "(@)", " AT ", " arroba ", "[at]", "(at)")
_DOT_FORMS = (" dot ", " [dot] ", " (dot) ", " {dot} ", "[.]", "(.)", " DOT ", " punto ", "[dot]", "(dot)")


def _spelled_email(rng, t):
    at, dot = rng.choice(_AT_FORMS[:1] + _AT_FORMS[7:9]), rng.choice(_DOT_FORMS[:1] + _DOT_FORMS[6:8])
    return " ".join(t.replace("@", at).replace(".", dot).split())


def _bracketed_email(rng, t):
    at, dot = rng.choice(_AT_FORMS), rng.choice(_DOT_FORMS)
    return " ".join(t.replace("@", at).replace(".", dot if rng.random() < 0.8 else ".").split())


def _b64(rng, t):
    raw = t.encode("utf-8")
    s = base64.urlsafe_b64encode(raw).decode() if rng.random() < 0.3 else base64.b64encode(raw).decode()
    return s.rstrip("=") if rng.random() < 0.4 else s


def _b32(rng, t):
    s = base64.b32encode(t.encode("utf-8")).decode()
    s = s.rstrip("=") if rng.random() < 0.5 else s
    return s.lower() if rng.random() < 0.4 else s


def _hex(rng, t):
    s = t.encode("utf-8").hex()
    return s.upper() if rng.random() < 0.3 else s


def _urlenc(rng, t):
    mode = rng.random()
    if mode < 0.4:
        return J(f"%{b:02x}" if rng.random() < 0.5 else f"%{b:02X}" for b in t.encode("utf-8"))
    if mode < 0.7:
        return t.replace("@", "%40").replace(".", "%2e" if rng.random() < 0.5 else "%2E")
    return J(f"%{ord(c):02x}" if not c.isalnum() or rng.random() < 0.4 else c for c in t)


def _rot13(rng, t):
    return codecs.encode(t, "rot13")


def _reversed(rng, t):
    return t[::-1] if rng.random() < 0.7 else " ".join(w[::-1] for w in t.split(" "))


def chunk(rng, t: str) -> str:
    """Split into 2-5 pieces with spaces (an encoded or plain value written in chunks)."""
    if len(t) < 6:
        return t
    k = rng.randint(2, min(5, len(t) // 3))
    cuts = sorted(rng.sample(range(1, len(t)), k - 1))
    return " ".join(t[a:b] for a, b in zip([0, *cuts], [*cuts, len(t)], strict=True))


EMAIL_TRANSFORMS = {
    "plain": lambda rng, t: t,
    "spelled": _spelled_email,
    "bracketed": _bracketed_email,
    "base64": _b64,
    "base32": _b32,
    "hex": _hex,
    "url_encoded": _urlenc,
    "rot13": _rot13,
    "reversed": _reversed,
    "chunked": chunk,
    "zero_width_bidi": lambda rng, t: _invisible(rng, t),
    "fullwidth": lambda rng, t: _fullwidth(t),
}
# the list's "whole or chunked": each encoding also in chunks (a single disguise, not a composed pair)
EMAIL_SINGLE = [n for n in EMAIL_TRANSFORMS if n != "chunked"] + [
    f"{n}_chunked" for n in ("base64", "base32", "hex", "url_encoded", "rot13", "reversed", "spelled", "bracketed")]


def email_single(rng, name: str, v: str) -> str:
    if name.endswith("_chunked"):
        return chunk(rng, EMAIL_TRANSFORMS[name[:-8]](rng, v))
    return EMAIL_TRANSFORMS[name](rng, v)


# ------------------------------------------------------------------ context
CONTEXT_BEFORE = ("rug cleaner", "my number is", "contact", "best pizza near me", "precio", "\u6771\u4eac",
                  "\U0001f600", "call me", "email:", "kontakt bitte", "\u0441\u0432\u044f\u0437\u0430\u0442\u044c\u0441\u044f",
                  "info", "please send to", "carte", "tel", "wohnung mieten", "pet shop", "\u043f\u043e\u0447\u0442\u0430",
                  "rug\t", "plumber 24/7", "order 2", "size 10", "\u30e1\u30fc\u30eb", "\U0001f4de", "(", "\u300c")
CONTEXT_AFTER = ("tonight", "rug cleaner", "open now", "barato", "\u6771\u4eac", "\U0001f525", "thanks", "asap", "!!",
                 "pls", "ok", "\u0441\u043f\u0430\u0441\u0438\u0431\u043e", "merci", "2024", "for sale", "near me", ")",
                 "\u300d", "\U0001f600", "x2", "danke", "\u3067\u3059")


def in_context(rng, t: str) -> str:
    m = rng.random()
    b, a = rng.choice(CONTEXT_BEFORE), rng.choice(CONTEXT_AFTER)
    sep1, sep2 = rng.choice((" ", " ", "\t", ": ", " - ")), rng.choice((" ", " ", "\t", ", "))
    if m < 0.35:
        return b + sep1 + t
    if m < 0.7:
        return t + sep2 + a
    return b + sep1 + t + sep2 + a


# ------------------------------------------------------------------ the M1 corpus (closed list, single disguise)
DIGIT_CLASSES = {"card": card, "phone": nanp, "phone_intl": e164, "ssn": ssn}


def closed_list(per_class: int = 2000, seed: int = 18):
    """Yield (class, disguise, placement, text, value_digits_or_text) for every class x disguise x alone / in context,
    ``per_class`` values per class (each value under every disguise, alone and in context)."""
    rng = random.Random(seed)
    for cls, gen in DIGIT_CLASSES.items():
        for _ in range(per_class):
            v = gen(rng)
            for name, f in DIGIT_TRANSFORMS.items():
                t = f(rng, cls, v)
                yield cls, name, "alone", t, v
                yield cls, name, "context", in_context(rng, t), v
    for _ in range(per_class):
        v = vanity(rng)
        for name in ("vanity_written", "vanity_zero_width_bidi", "vanity_fullwidth"):
            t = vanity_form(rng, v)
            t = _invisible(rng, t) if name == "vanity_zero_width_bidi" else _fullwidth(t) if name == \
                "vanity_fullwidth" else t
            yield "vanity", name, "alone", t, v
            yield "vanity", name, "context", in_context(rng, t), v
    for _ in range(per_class):
        v = email(rng)
        for name in EMAIL_SINGLE:
            t = email_single(rng, name, v)
            yield "email", name, "alone", t, v
            yield "email", name, "context", in_context(rng, t), v
    for _ in range(per_class):
        v = address(rng)
        for name, f in (("plain", lambda t: t), ("zero_width_bidi", lambda t: _invisible(rng, t)),
                        ("fullwidth", _fullwidth)):
            t = f(v)
            yield "address", name, "alone", t, v
            yield "address", name, "context", in_context(rng, t), v


# ------------------------------------------------------------------ M2: composed pairs
def composed(n: int = 40000, seed: int = 1818):
    """``n`` cases, each ONE value under TWO different list transforms (the second applied to the first's output),
    spread evenly over the classes and the ordered transform pairs; a quarter in context."""
    rng = random.Random(seed)
    digit_pairs = [(a, b) for a in DIGIT_TRANSFORMS for b in DIGIT_TRANSFORMS if a != b and "plain" not in (a, b)]
    email_names = [k for k in EMAIL_TRANSFORMS if k != "plain"]
    email_pairs = [(a, b) for a in email_names for b in email_names if a != b]
    classes = (*DIGIT_CLASSES, "email")
    i = 0
    while i < n:
        cls = classes[i % len(classes)]
        if cls == "email":
            a, b = email_pairs[(i // len(classes)) % len(email_pairs)]
            v = email(rng)
            t = EMAIL_TRANSFORMS[b](rng, EMAIL_TRANSFORMS[a](rng, v))
        else:
            a, b = digit_pairs[(i // len(classes)) % len(digit_pairs)]
            v = DIGIT_CLASSES[cls](rng)
            t = DIGIT_TRANSFORMS[b](rng, cls, DIGIT_TRANSFORMS[a](rng, cls, v))
        if rng.random() < 0.25:
            t = in_context(rng, t)
        yield cls, f"{a}+{b}", t, v
        i += 1


# ------------------------------------------------------------------ M3: ordinary queries
_HEADS = ("rug cleaner", "carpet cleaning", "plumber", "electrician", "dog food", "cat litter", "running shoes",
          "laptop", "office chair", "coffee maker", "air fryer", "tv", "mattress", "hotel", "flights", "car insurance",
          "dentist", "yoga mat", "phone case", "headphones", "monitor", "printer ink", "lawn mower", "roof repair",
          "hvac repair", "pizza", "sushi", "gym", "hair salon", "pet grooming", "cat tree", "dog bed")
_HEADS_I18N = ("limpieza de alfombras", "fontanero", "comida para perros", "zapatillas", "hotel barato",
               "teppichreinigung", "klempner", "hundefutter", "laufschuhe", "nettoyage tapis", "plombier",
               "croquettes chat", "pulizia tappeti", "idraulico", "\u6771\u4eac \u30db\u30c6\u30eb",
               "\u30ab\u30fc\u30da\u30c3\u30c8 \u6383\u9664", "\u5ba0\u7269\u98df\u54c1", "\u0447\u0438\u0441\u0442\u043a\u0430 \u043a\u043e\u0432\u0440\u043e\u0432",
               "\uce74\ud3ab \uccad\uc18c", "limpeza de tapete", "encanador", "tapijt reinigen", "loodgieter")
_MODS = ("near me", "prices", "best", "cheap", "reviews", "open now", "how to clean", "for sale", "deals", "vs",
         "cerca de mi", "precio", "in der n\u00e4he", "pas cher", "\u5b89\u3044", "\u0446\u0435\u043d\u0430", "", "")


def _r(rng, a, b):
    return str(rng.randint(a, b))


_BRANDS = ("samsung", "lg", "sony", "dell", "hp", "lenovo", "asus", "acer", "canon", "nikon", "bosch", "makita",
           "dewalt", "ryobi", "whirlpool", "ge", "kitchenaid", "dyson", "shark", "bissell", "hoover", "philips")
_SOFTWARE = ("python", "node", "ios", "android", "windows", "macos", "ubuntu", "java", "php", "django", "react",
             "chrome", "firefox", "kernel", "firmware", "bios", "wordpress", "excel", "office", "minecraft")
_EVENTS = ("concert", "events", "hotel deals", "holiday", "sale", "fireworks", "marathon", "flights", "weather",
           "farmers market", "open house", "conferencia", "feiertag", "soldes", "festival")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december", "enero", "mars", "oktober", "dec", "nov", "jan")
_CARS = ("honda civic", "toyota camry", "ford f-150", "chevy silverado", "tesla model 3", "jeep wrangler",
         "subaru outback", "bmw 3 series", "vw golf", "kia sorento", "mazda cx-5", "nissan rogue")


def _date(rng) -> str:
    y, m, d = rng.randint(1995, 2027), rng.randint(1, 12), rng.randint(1, 28)
    return rng.choice((f"{y}-{m:02d}-{d:02d}", f"{m}/{d}/{y}", f"{m:02d}/{d:02d}/{y}", f"{d:02d}.{m:02d}.{y}",
                       f"{rng.choice(_MONTHS)} {d} {y}", f"{d} {rng.choice(_MONTHS)} {y}", f"{rng.choice(_MONTHS)} {d}",
                       f"{m}/{d}", f"{y}", f"{rng.choice(_MONTHS)} {y}", f"{y}/{m:02d}/{d:02d}",
                       f"{y}-{m:02d}-{d:02d} {rng.randint(1, 12)}{rng.choice(('am', 'pm'))}",
                       f"{m}/{d} {rng.randint(1, 12)}pm", f"{y} {y + 1}", f"{y}-{y + 1} season"))


def _model(rng) -> str:
    L = "abcdefghjkmnprstuvwxz"
    return rng.choice((
        f"{rng.choice(_BRANDS)} {rng.choice(L)}{_r(rng, 100, 9999)}", f"{rng.choice(_BRANDS)} {rng.choice(L)}"
        f"{rng.choice(L)}{_r(rng, 10, 999)}{rng.choice(L)}", f"rtx {rng.choice(('3060', '3070', '3080', '4060', '4070', '4080', '4090', '5090'))}",
        f"iphone {_r(rng, 11, 16)} {rng.choice(('pro', 'pro max', 'plus', ''))} {rng.choice(('128gb', '256gb', '512gb', ''))}",
        f"galaxy s{_r(rng, 20, 25)} {rng.choice(('ultra', 'fe', ''))}", f"{rng.choice(_BRANDS)} {_r(rng, 10, 99)} inch tv",
        f"i{rng.choice('3579')}-{_r(rng, 10, 14)}{_r(rng, 100, 999)}k", f"ryzen {rng.choice('3579')} {_r(rng, 3, 9)}{_r(rng, 600, 950)}x",
        f"ddr{rng.choice('45')} {rng.choice(('3200', '3600', '5600', '6000', '6400'))} {rng.choice(('16gb', '32gb', '64gb'))}",
        f"{rng.choice(_BRANDS)} {rng.choice(L)}{rng.choice(L)}-{_r(rng, 100, 9999)}", f"{_r(rng, 1, 9)}{rng.choice(L)}{_r(rng, 10, 99)} bulb",
        f"{rng.choice(_CARS)} {_r(rng, 2005, 2026)}", f"{rng.choice(_BRANDS)} {_r(rng, 1000, 99999)}",
        f"{_r(rng, 0, 20)}w{_r(rng, 20, 50)} oil", f"cr{rng.choice(('2032', '2025', '2016', '1632'))} battery"))


def _size(rng) -> str:
    return rng.choice((
        f"{_r(rng, 18, 33)}5/{_r(rng, 3, 8)}5r{_r(rng, 14, 22)}" + rng.choice(("", "", f" {_r(rng, 82, 120)}{rng.choice('hvwty')}")),
        f"{_r(rng, 2, 12)}x{_r(rng, 3, 15)} rug", f"size {_r(rng, 5, 14)}{rng.choice(('', '.5'))} {rng.choice(('mens', 'womens', 'kids', 'wide'))}",
        f"eu {_r(rng, 35, 47)}", f"{_r(rng, 24, 40)}x{_r(rng, 28, 36)} jeans", f"{_r(rng, 10, 30)}x{_r(rng, 10, 30)}x1 air filter",
        f"{_r(rng, 1, 4)}x{_r(rng, 2, 12)} lumber {_r(rng, 6, 16)} ft", f"{_r(rng, 5, 75)} gallon", f"{_r(rng, 8, 40)}x{_r(rng, 10, 60)} frame",
        f"m{_r(rng, 3, 12)} x {rng.choice(('0.5', '1.25', '1.5', '1.75'))} bolt", f"{_r(rng, 1, 50)} lb bag", f"{_r(rng, 6, 20)} x {_r(rng, 6, 20)} shed"))


def _version(rng) -> str:
    sw = rng.choice(_SOFTWARE)
    return rng.choice((f"{sw} {_r(rng, 1, 30)}", f"{sw} {_r(rng, 1, 30)}.{_r(rng, 0, 20)}",
                       f"{sw} {_r(rng, 1, 30)}.{_r(rng, 0, 20)}.{_r(rng, 0, 30)}",
                       f"{sw} version {_r(rng, 1, 20)}.{_r(rng, 0, 9)}.{_r(rng, 0, 9999)}",
                       f"{sw} {_r(rng, 1, 130)}.0.{_r(rng, 1000, 9999)}.{_r(rng, 1, 200)}", f"{sw} v{_r(rng, 1, 9)}.{_r(rng, 0, 9)}"))


def _price(rng) -> str:
    h = rng.choice(_HEADS)
    return rng.choice((f"{h} under ${_r(rng, 20, 2000)}", f"{h} ${_r(rng, 5, 999)}.99", f"{h} {_r(rng, 10, 3000)} dollars",
                       f"{h} precio {_r(rng, 10, 900)} euros", f"{h} \u20ac{_r(rng, 5, 2000)}", f"{h} under {_r(rng, 50, 1000)}",
                       f"{h} for ${_r(rng, 1, 99)}", f"{h} {_r(rng, 10, 90)}% off", f"{h} \u00a3{_r(rng, 5, 900)}",
                       f"{h} ${_r(rng, 1, 9)},{_r(rng, 100, 999)}", f"{h} {_r(rng, 100, 4000)} {rng.choice(('eur', 'usd', 'yen'))}"))


def _zip(rng) -> str:
    z = f"{rng.randint(501, 99950):05d}"
    h = rng.choice(_HEADS)
    if rng.random() < 0.15:
        return f"{h} {z} {_r(rng, 2019, 2026)}"
    return rng.choice((f"{h} {z}", f"{h} near {z}", f"{z} {h}", f"{h} in {z}"))


# class -> (share of the corpus, maker); text-only classes 80%, digit-bearing classes 20% (an assumed mix, stated in
# ADR 0047; every class's own rate is reported, so any other mix can be computed from them)
def ordinary(n: int = 24000, seed: int = 2018):
    """``n`` ordinary queries in classes. Yields (class, text)."""
    rng = random.Random(seed)
    makers = {
        "plain_text": (0.30, lambda: f"{rng.choice(_MODS)} {rng.choice(_HEADS)}"),
        "multilingual": (0.20, lambda: f"{rng.choice(_HEADS_I18N)} {rng.choice(_MODS)}"),
        "question": (0.15, lambda: rng.choice(("how to clean", "what is the best", "how much does", "where to buy",
                                               "can i use", "c\u00f3mo limpiar", "wie reinigt man",
                                               "comment nettoyer", "how long does", "is it safe to use"))
                     + " " + rng.choice(_HEADS) + rng.choice(("", "?", " cost", " at home"))),
        "local": (0.15, lambda: f"{rng.choice(_HEADS)} near me {rng.choice(('', 'open now', '24 hours', 'cheap'))}"),
        "price": (0.03, lambda: _price(rng)),
        "date": (0.025, lambda: rng.choice((f"{rng.choice(_EVENTS)} {_date(rng)}", f"{_date(rng)} {rng.choice(_EVENTS)}"))),
        "version": (0.02, lambda: f"{_version(rng)} {rng.choice(('update', 'download', 'release notes', ''))}"),
        "size": (0.03, lambda: f"{_size(rng)} {rng.choice(('', 'tires', 'price', 'near me', 'best'))}"),
        "zip_year": (0.02, lambda: _zip(rng)),
        "model_number": (0.04, lambda: f"{_model(rng)} {rng.choice(('', 'price', 'review', 'vs', 'deal', 'specs'))}"),
        "year": (0.02, lambda: f"{_r(rng, 1990, 2026)} {rng.choice(_CARS + _HEADS)} {rng.choice(('', 'review', 'best'))}"),
        "count_quantity": (0.015, lambda: f"{_r(rng, 2, 500)} {rng.choice(('pack', 'count', 'pcs', 'lb', 'oz', 'kg'))} "
                           f"{rng.choice(_HEADS)}"),
    }
    plan = []
    for cls, (share, _f) in makers.items():
        plan += [cls] * round(share * n)
    for i in range(n):
        cls = plan[i % len(plan)]
        yield cls, " ".join(makers[cls][1]().split())


if __name__ == "__main__":                                # pragma: no cover - a reviewer's tool
    from collections import Counter
    print("closed list", Counter((c, p) for c, _n, p, _t, _v in closed_list(200)))
    print("composed", Counter(c for c, *_ in composed(4000)))
    print("ordinary", Counter(c for c, _ in ordinary(2400)))
