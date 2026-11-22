"""Review round 19: the round-18 generator (``privacy_r18_corpus``) extended with the shapes the independent review
of round 18 measured and the round-18 corpus did not hold, a composed-pair corpus built the way the reviewer built it,
and a realistic ordinary-query corpus that is at least half digit-bearing. Every personal-data value is SYNTHETIC
and made at run time from a seeded random generator; nothing personal-data shaped is written in this file.

New list shapes (ADR 0047, "Personal data", the list), each a class x disguise with alone / in-context placement:
  - vanity toll-free numbers: the code as number words (English, Spanish) with a 7-letter word, keypad letters spaced,
    letter gaps inside the digits of 1 + 8xx, 1-3 separators inside the 8xx code;
  - e-mails spelled or bracketed with every supported at-word (at, arroba, arobase, chiocciola, klammeraffe) and
    dot-word (dot, punto, ponto, punkt), with a top-level domain outside the classifier's word list (3-24 letters);
    e-mails spelled with '_' / '-' around the keywords;
  - street addresses with an ordinary-word suffix followed by 4-8 tokens of surrounding text;
  - card / phone / SSN digits with exactly ONE digit spelled.
Composed pairs (``composed_r19``): one value, two different list transforms, every ordered pair, the digits modelled
as units and gaps so that no gap between two digit units is longer than three characters and no gap falls inside a
spelled word (a number word glued to gap letters, "five" + letters + "zero", is a gap between two units)."""

from __future__ import annotations

import random
import string

import privacy_r18_corpus as rc

J = "".join

# ------------------------------------------------------------------ e-mails: at-words, dot-words, unlisted TLDs
AT_WORDS = ("at", "arroba", "arobase", "chiocciola", "klammeraffe")
DOT_WORDS = ("dot", "punto", "ponto", "punkt")
# top-level domains that are real (new gTLDs / city / brand TLDs) and NOT in the classifier's e-mail TLD word list
UNLISTED_TLDS = ("photography", "berlin", "ninja", "lawyer", "academy", "consulting", "rocks", "studio", "london",
                 "guru", "team", "work", "family", "coffee", "pizza", "school", "church", "farm", "garden", "health",
                 "house", "land", "money", "party", "social", "software", "solar", "tax", "tickets", "tours",
                 "training", "video", "vision", "wiki", "wine", "works", "yoga", "koeln", "bayern", "paris", "tokyo",
                 "nyc", "sydney", "quebec", "scot", "wales", "cymru", "golf", "fitness", "dental", "clinic", "immo",
                 "haus", "reise", "kaufen", "jetzt", "ovh", "moe", "ltd", "llc", "gmbh", "international",
                 "construction", "photography", "versicherung", "accountants", "engineering", "properties")


def unlisted_tld(rng: random.Random) -> str:
    if rng.random() < 0.75:
        return rng.choice(UNLISTED_TLDS)
    return J(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(3, 24)))


def email_unlisted(rng: random.Random) -> str:
    """A synthetic mailbox whose top-level domain is outside the word list and longer than two letters."""
    v = rc.email(rng)
    local, dom = v.split("@")
    label = dom.split(".")[0]
    return f"{local}@{label}.{unlisted_tld(rng)}"


def _at_form(rng, word: str, bracketed: bool) -> str:
    w = word.upper() if rng.random() < 0.15 else word
    if bracketed:
        o, c = rng.choice((("[", "]"), ("(", ")"), ("{", "}"), ("<", ">")))
        return rng.choice((f" {o}{w}{c} ", f"{o}{w}{c}", f" {o} {w} {c} "))
    return f" {w} "


def spelled_any(rng, v: str, bracketed: bool) -> str:
    at, dot = rng.choice(AT_WORDS), rng.choice(DOT_WORDS)
    at_s = _at_form(rng, at, bracketed)
    local, dom = v.split("@")
    parts = dom.split(".")
    out = [local, at_s]
    for i, p in enumerate(parts):
        if i:
            out.append(_at_form(rng, dot, bracketed) if (not bracketed or rng.random() < 0.8) else ".")
        out.append(p)
    return " ".join(J(out).split())


def sep_spelled(rng, v: str) -> str:
    """'jane_at_example_dot_com', 'jane-at-example-dot-com' (an e-mail spelled with '_' / '-' as separators)."""
    s = rng.choice(("_", "-"))
    at, dot = rng.choice(AT_WORDS), rng.choice(DOT_WORDS)
    local, dom = v.split("@")
    return local + s + at + s + (s + dot + s).join(dom.split("."))


# ------------------------------------------------------------------ vanity toll-free numbers, spelled / gapped
VANITY_CODES = ("800", "833", "844", "855", "866", "877", "888")
VANITY_WORDS7 = ("flowers", "callnow", "getcash", "lawyers", "plumber", "rooting", "carpets", "dentist", "helpnow",
                 "gotjunk", "tickets", "cleaner", "roofers", "pestfix", "contact", "billing", "support", "rentcar",
                 "goflyer", "mattres", "hotdeal", "paydays", "taxhelp", "mychart", "doctors", "repairs", "pickups")


def _vanity_tail(rng) -> str:
    w = rng.choice(VANITY_WORDS7)
    return J(c.upper() if rng.random() < 0.2 else c for c in w)


def _num_words(rng, digits: str, lang) -> list:
    return [lang[int(c)] for c in digits]


def vanity_code_words(rng, lang) -> str:
    """'one eight zero zero flowers', 'uno ocho ocho ocho callnow': the code (and the leading 1) as number words."""
    code = rng.choice(VANITY_CODES)
    lead = rng.random() < 0.6
    words = _num_words(rng, ("1" if lead else "") + code, lang)
    sep = rng.choice((" ", " ", "-", ", ", " - "))
    tail = _vanity_tail(rng)
    if rng.random() < 0.3:                              # the word spaced out too
        tail = rng.choice((" ", "-", ".")).join(tail)
    return sep.join(words) + rng.choice((" ", " ", "-", ": ")) + tail


def vanity_keypad_spaced(rng) -> str:
    code = rng.choice(VANITY_CODES)
    lead = rng.choice(("", "1 ", "1-", "1."))
    s = rng.choice((" ", "-", ".", " - ", "/", "\u2022"))
    return lead + code + rng.choice((" ", "-", "")) + s.join(_vanity_tail(rng))


def vanity_letter_gap_code(rng) -> str:
    """Letter gaps (1-3 letters) inside the digit part 1 + 8xx ('1x8yz00 flowers')."""
    code = rng.choice(VANITY_CODES)
    digits = "1" + code if rng.random() < 0.7 else code
    out = [digits[0]]
    gapped = False
    for c in digits[1:]:
        if rng.random() < 0.6:
            out.append(J(rng.choice(string.ascii_letters) for _ in range(rng.randint(1, 3))))
            gapped = True
        out.append(c)
    if not gapped:
        out.insert(1, rng.choice(string.ascii_lowercase))
    return J(out) + rng.choice((" ", "-", "", " ")) + _vanity_tail(rng)


def vanity_sep_in_code(rng) -> str:
    """1-3 separators of the list inside the 8xx code ('8-0-0 flowers', '8;88flowers')."""
    code = rng.choice(VANITY_CODES)
    digits = ("1" if rng.random() < 0.5 else "") + code
    out = [digits[0]]
    for c in digits[1:]:
        if rng.random() < 0.7:
            out.append(J(rng.choice(rc.SEPARATORS[:400:3] + tuple(" -.;/*~")) for _ in range(rng.randint(1, 3))))
        out.append(c)
    t = J(out)
    if t.isdigit():
        t = t[:-1] + "-" + t[-1]
    return t + rng.choice((" ", "-", "", ". ")) + _vanity_tail(rng)


VANITY_R19 = {
    "vanity_code_words_en": lambda rng: vanity_code_words(rng, rc.EN),
    "vanity_code_words_es": lambda rng: vanity_code_words(rng, rc.ES),
    "vanity_keypad_spaced": vanity_keypad_spaced,
    "vanity_letter_gap_code": vanity_letter_gap_code,
    "vanity_sep_in_code": vanity_sep_in_code,
}

# ------------------------------------------------------------------ addresses with long surrounding text
_TAIL_WORDS = ("open", "now", "tonight", "parking", "near", "me", "downtown", "best", "hours", "today", "please",
               "deliver", "to", "pickup", "at", "after", "5pm", "apartment", "for", "rent", "sale", "photos", "map",
               "directions", "zillow", "price", "history", "owner", "thanks", "asap", "2", "bedroom", "listing",
               "cerca", "de", "aqui", "bitte", "liefern", "merci", "x2", "pls", "ok")
_ORD_SUFFIXES = ("road", "lane", "drive", "court", "way", "terrace", "place", "loop", "plaza")


def address_long_tail(rng) -> str:
    s = f"{rng.randint(1, 99999)} {rng.choice(rc._DIRS)}{rng.choice(rc._STREETS)} {rng.choice(_ORD_SUFFIXES)}"
    tail = " ".join(rng.choice(_TAIL_WORDS) for _ in range(rng.randint(4, 8)))
    return s + rng.choice((" ", ", ", " - ")) + tail


# ------------------------------------------------------------------ one spelled digit among digits
def one_word(rng, cls: str, d: str) -> str:
    """The value written plainly, then exactly ONE of its digits spelled (English or Spanish), glued or spaced."""
    t = rc._plain_digits(rng, cls, d)
    pos = [i for i, c in enumerate(t) if c.isdigit()]
    i = rng.choice(pos)
    w = rng.choice((rc.EN, rc.ES))[int(t[i])]
    if rng.random() < 0.5:
        w = " " + w + " "
    return " ".join((t[:i] + w + t[i + 1:]).split())


# ------------------------------------------------------------------ M1 (round 19 additions)
def closed_list_r19(per_class: int = 2000, seed: int = 19):
    """Yield (class, disguise, placement, text, value) for the round-19 shapes, ``per_class`` values per class and
    disguise, alone and in context."""
    rng = random.Random(seed)
    for _ in range(per_class):
        for name, f in VANITY_R19.items():
            t = f(rng)
            yield "vanity", name, "alone", t, ""
            yield "vanity", name, "context", rc.in_context(rng, t), ""
    for _ in range(per_class):
        v = email_unlisted(rng)
        for name, t in (("atword_unlisted_tld_spelled", spelled_any(rng, v, False)),
                        ("atword_unlisted_tld_bracketed", spelled_any(rng, v, True)),
                        ("sep_spelled_unlisted_tld", sep_spelled(rng, v))):
            yield "email", name, "alone", t, v
            yield "email", name, "context", rc.in_context(rng, t), v
        w = rc.email(rng)
        for name, t in (("atword_spelled", spelled_any(rng, w, False)),
                        ("atword_bracketed", spelled_any(rng, w, True)),
                        ("sep_spelled", sep_spelled(rng, w))):
            yield "email", name, "alone", t, w
            yield "email", name, "context", rc.in_context(rng, t), w
    for _ in range(per_class):
        t = address_long_tail(rng)
        yield "address", "ordinary_suffix_long_tail", "alone", t, t.split(",")[0]
        yield "address", "ordinary_suffix_long_tail", "context", rc.in_context(rng, t), t.split(",")[0]
    for cls, gen in rc.DIGIT_CLASSES.items():
        for _ in range(per_class):
            v = gen(rng)
            t = one_word(rng, cls, v)
            yield cls, "one_number_word", "alone", t, v
            yield cls, "one_number_word", "context", rc.in_context(rng, t), v


def closed_list_all(per_class: int = 2000):
    """The round-18 list corpus and the round-19 additions."""
    yield from rc.closed_list(per_class)
    yield from closed_list_r19(per_class)


# ------------------------------------------------------------------ M2: composed pairs, units and gaps
GAP_POOLS = {
    "sep_gap": rc.SEPARATORS,
    "letter_gap": rc.ASCII_LETTERS,
    "nfkc_letter_gap": rc.NFKC_LETTERS,
    "mixed_gap": rc.ASCII_LETTERS + J(rc.SEPARATORS[::7]) + J(rc.OTHER_LETTERS) + J(rc.NFKC_LETTERS[::50]),
}
DIGIT_T = ("sep_gap", "letter_gap", "nfkc_letter_gap", "mixed_gap", "script_digits", "fullwidth", "zero_width_bidi",
           "spelled")


def _start(rng, cls: str, d: str):
    """The value as units (one digit each) and gaps (what stands between two units), plainly grouped."""
    t = rc._plain_digits(rng, cls, d)
    units, gaps, cur, lead = [], [], "", ""
    for c in t:
        if c.isdigit():
            if units:
                gaps.append(cur)
            elif cur:
                lead = cur
            units.append(c)
            cur = ""
        else:
            cur += c
    return lead, units, gaps


def _apply(rng, name: str, lead: str, units: list, gaps: list, lang):
    if name in GAP_POOLS:
        pool = GAP_POOLS[name]
        gaps = [rc._gap(rng, pool) if rng.random() < 0.6 else g for g in gaps]
        if not any(g for g in gaps if any(ch.isalpha() for ch in g) or name == "sep_gap"):
            k = rng.randrange(len(gaps))
            gaps[k] = rc._gap(rng, pool)
    elif name == "script_digits":
        z = rng.choice(rc.ND_ZEROS)
        units = [chr(z + int(u)) if u.isascii() and u.isdigit() else u for u in units]
    elif name == "fullwidth":
        units = [rc._fullwidth(u) for u in units]
        gaps = [rc._fullwidth(g) for g in gaps]
        lead = rc._fullwidth(lead)
    elif name == "zero_width_bidi":
        units = [rc._invisible(rng, u) for u in units]
    elif name == "spelled":
        def word(u):
            core = u.strip("".join(rc.INVISIBLE))
            if core.isascii() and core.isdigit() and rng.random() < 0.85:
                return u.replace(core, lang[int(core)])
            return u
        units = [word(u) for u in units]
        sep = rng.choice((" ", " ", "-", ", ", ""))
        gaps = [g if g else sep for g in gaps]
    return lead, units, gaps


def composed_r19(n: int = 42000, seed: int = 1919):
    """``n`` cases: every class (card, NANP, E.164, SSN, vanity, e-mail) x every ordered pair of two different list
    transforms, evenly; a quarter in surrounding text. Digit values are units and gaps (no gap longer than three
    characters, none inside a spelled word); e-mails use the round-18 transforms with every at-word / dot-word, and a
    third of them a top-level domain outside the word list."""
    rng = random.Random(seed)
    digit_pairs = [(a, b) for a in DIGIT_T for b in DIGIT_T if a != b]
    e_names = ["spelled_any", "bracketed_any", "sep_spelled", "base64", "base32", "hex", "url_encoded", "rot13",
               "reversed", "chunked", "zero_width_bidi", "fullwidth"]
    email_pairs = [(a, b) for a in e_names for b in e_names if a != b]
    v_names = list(VANITY_R19) + ["fullwidth", "zero_width_bidi", "context_glued"]
    vanity_pairs = [(a, b) for a in v_names for b in v_names if a != b]
    classes = ("card", "phone", "phone_intl", "ssn", "email", "vanity")

    def etf(name, t):
        if name == "spelled_any":
            return spelled_any(rng, t, False) if "@" in t else rc._spelled_email(rng, t)
        if name == "bracketed_any":
            return spelled_any(rng, t, True) if "@" in t else rc._bracketed_email(rng, t)
        if name == "sep_spelled":
            return sep_spelled(rng, t) if t.count("@") == 1 else t
        return rc.EMAIL_TRANSFORMS[name](rng, t)

    def vtf(name, t):
        if t is None:
            return VANITY_R19[name](rng) if name in VANITY_R19 else rc.vanity_form(rng, rc.vanity(rng))
        if name == "fullwidth":
            return rc._fullwidth(t)
        if name == "zero_width_bidi":
            return rc._invisible(rng, t)
        if name == "context_glued":
            return rc.in_context(rng, t)
        return t
    i = 0
    while i < n:
        cls = classes[i % len(classes)]
        k = i // len(classes)
        if cls == "email":
            a, b = email_pairs[k % len(email_pairs)]
            v = email_unlisted(rng) if rng.random() < 1 / 3 else rc.email(rng)
            t = etf(b, etf(a, v))
        elif cls == "vanity":
            a, b = vanity_pairs[k % len(vanity_pairs)]
            v = ""
            first = a if a in VANITY_R19 else (b if b in VANITY_R19 else None)
            t = vtf(first or "written", None)
            for name in (a, b):
                if name != first:
                    t = vtf(name, t)
        else:
            a, b = digit_pairs[k % len(digit_pairs)]
            v = rc.DIGIT_CLASSES[cls](rng)
            lang = rng.choice((rc.EN, rc.ES))
            lead, units, gaps = _start(rng, cls, v)
            for name in (a, b):
                lead, units, gaps = _apply(rng, name, lead, units, gaps, lang)
            t = lead + J(u + (gaps[j] if j < len(gaps) else "") for j, u in enumerate(units))
        if rng.random() < 0.25:
            t = rc.in_context(rng, t)
        yield cls, f"{a}+{b}", t, v
        i += 1


# ------------------------------------------------------------------ M3: realistic ordinary queries
_FLIGHT_CARRIERS = ("ua", "dl", "aa", "ba", "lh", "af", "kl", "wn", "b6", "as", "ib", "ek", "qr", "nh")
_TIRE_LOAD = ("", "", " 116s", " 121r", " 94h", " 91v", " xl", " lt")
_PART_WORDS = ("part number", "part no", "part #", "p/n", "pn", "oem", "oem part", "replacement part", "mpn")


def _check_upc(d11: str) -> str:
    s = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(d11))
    return d11 + str((10 - s % 10) % 10)


def _check_ean13(d12: str) -> str:
    s = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(d12))
    return d12 + str((10 - s % 10) % 10)


def _upc_isbn(rng) -> str:
    r = rng.random()
    if r < 0.5:
        u = _check_upc(J(str(rng.randint(0, 9)) for _ in range(11)))
        return rng.choice((f"upc {u}", f"{u} upc", f"upc code {u}", f"barcode {u}", f"{u} barcode lookup",
                           f"gtin {u}", f"upc {u} {rng.choice(rc._HEADS)}", f"{rng.choice(rc._BRANDS)} upc {u}"))
    e = _check_ean13(rng.choice(("978", "979")) + J(str(rng.randint(0, 9)) for _ in range(9)))
    hy = f"{e[:3]}-{e[3]}-{e[4:7]}-{e[7:12]}-{e[12]}"
    return rng.choice((f"isbn {e}", f"isbn {hy}", f"{e} book", f"isbn-13 {e}", f"{e} paperback", f"ean {e}",
                       f"libro isbn {e}", f"buch isbn {hy}", f"{e}"))


def _tire(rng) -> str:
    w = rng.choice(range(155, 356, 10))
    a = rng.choice(range(30, 86, 5))
    rim = rng.randint(13, 24)
    return rng.choice((f"{w} {a} {rim}", f"{w}/{a}/{rim}", f"{w} {a}r{rim}", f"{w}/{a} r{rim}", f"{w}-{a}-{rim}",
                       f"p{w} {a} {rim}", f"lt{w} {a} {rim}", f"{w}/{a}zr{rim}", f"{w} {a} zr{rim}")) \
        + rng.choice(_TIRE_LOAD) + rng.choice(("", " tires", " all season", " winter tires", " price", " neumaticos",
                                               " reifen", " pneus"))


def _part(rng) -> str:
    d = rng.choice((f"{rng.randint(10, 99)}-{rng.randint(1000, 9999)}-{rng.randint(10, 99)}",
                    f"{rng.randint(100, 999)}-{rng.randint(100, 999)}-{rng.randint(10, 99)}",
                    f"{rng.randint(10000, 99999)}-{rng.randint(100, 999)}",
                    f"{rng.randint(1, 9)}{rng.choice('abcdefghjk')}{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
                    f"{rng.randint(10, 99)} {rng.randint(1000, 9999)} {rng.randint(10, 99)}"))
    return rng.choice((f"{rng.choice(_PART_WORDS)} {d}", f"{rng.choice(rc._BRANDS)} {rng.choice(_PART_WORDS)} {d}",
                       f"{d} {rng.choice(('part', 'replacement', 'filter', 'gasket'))}"))


def _flight(rng) -> str:
    m, dd, hh, mm = rng.randint(1, 12), rng.randint(1, 28), rng.randint(0, 23), rng.choice(("00", "15", "30", "45",
                                                                                              "05", "50", "20"))
    num = rng.randint(1, 2999)
    car = rng.choice(_FLIGHT_CARRIERS)
    return rng.choice((f"flight {m}/{dd} {hh}:{mm}", f"flight {car}{num} {m}/{dd} {hh}:{mm}",
                       f"{car} {num} {m}/{dd}", f"flight {car}{num} status", f"vuelo {car}{num} {dd}/{m}",
                       f"flight {m}/{dd} {hh}:{mm} {rng.choice(('departure', 'arrival', 'delayed'))}",
                       f"{car}{num} {hh}:{mm}"))


def _time(rng) -> str:
    return rng.choice((f"{rng.choice(rc._HEADS)} open {rng.randint(1, 12)}:{rng.choice(('00', '30'))} "
                       f"{rng.choice(('am', 'pm'))}", f"{rng.choice(rc._EVENTS)} {rng.randint(0, 23)}:"
                       f"{rng.choice(('00', '15', '30', '45'))}", f"{rng.randint(1, 12)}pm {rng.choice(rc._HEADS)}"))


def _date_time(rng) -> str:
    y, m, d = rng.randint(2019, 2027), rng.randint(1, 12), rng.randint(1, 28)
    t = f"{rng.randint(0, 23)}:{rng.choice(('00', '30'))}"
    return rng.choice((f"{rng.choice(rc._EVENTS)} {y}-{m:02d}-{d:02d} {t}", f"{m}/{d}/{y} {t} {rng.choice(rc._EVENTS)}",
                       f"{rng.choice(rc._EVENTS)} {d}.{m}.{y} {t}"))


def _sku(rng) -> str:
    L = "ABCDEFGHJKLMNPRSTUVWXYZ"
    return rng.choice((f"sku {rng.randint(100000, 999999)}", f"{rng.choice(rc._BRANDS)} sku {rng.choice(L)}{rng.choice(L)}"
                       f"{rng.randint(1000, 99999)}", f"item {rng.randint(10000, 999999)}",
                       f"asin b0{J(rng.choice(L + '0123456789') for _ in range(8)).lower()}",
                       f"model {rng.choice(L).lower()}{rng.randint(100, 9999)}-{rng.randint(10, 99)}"))


def _qty(rng) -> str:
    return f"{rng.randint(2, 500)} {rng.choice(('pack', 'count', 'pcs', 'lb', 'oz', 'kg', 'ml', 'piezas', 'stueck'))} " \
           f"{rng.choice(rc._HEADS)}"


def ordinary_r19(n: int = 24000, seed: int = 2019):
    """``n`` ordinary queries, 55% digit-bearing (prices, sizes, tire sizes spaced / slashed, UPC / ISBN / SKU, part
    numbers, model numbers, dates, times, date + time, flights, ZIP, ZIP + year, versions, quantities, years) and 45%
    text (English and other languages). Yields (class, text). An assumed mix, stated in ADR 0047; every class's own
    rate is reported."""
    rng = random.Random(seed)
    makers = {
        "plain_text": (0.15, lambda: f"{rng.choice(rc._MODS)} {rng.choice(rc._HEADS)}"),
        "multilingual": (0.12, lambda: f"{rng.choice(rc._HEADS_I18N)} {rng.choice(rc._MODS)}"),
        "question": (0.09, lambda: rng.choice(("how to clean", "what is the best", "how much does", "where to buy",
                                               "can i use", "c\u00f3mo limpiar", "wie reinigt man", "comment nettoyer",
                                               "how long does", "is it safe to use", "at what age can", "who makes"))
                     + " " + rng.choice(rc._HEADS) + rng.choice(("", "?", " cost", " at home", " at walmart"))),
        "local": (0.09, lambda: f"{rng.choice(rc._HEADS)} near me {rng.choice(('', 'open now', '24 hours', 'cheap'))}"),
        "price": (0.05, lambda: rc._price(rng)),
        "size": (0.04, lambda: f"{rc._size(rng)} {rng.choice(('', 'price', 'near me', 'best'))}"),
        "tire_size": (0.05, lambda: _tire(rng)),
        "upc_isbn": (0.04, lambda: _upc_isbn(rng)),
        "sku": (0.03, lambda: _sku(rng)),
        "part_number": (0.04, lambda: _part(rng)),
        "model_number": (0.05, lambda: f"{rc._model(rng)} {rng.choice(('', 'price', 'review', 'vs', 'deal', 'specs'))}"),
        "date": (0.04, lambda: rng.choice((f"{rng.choice(rc._EVENTS)} {rc._date(rng)}",
                                           f"{rc._date(rng)} {rng.choice(rc._EVENTS)}"))),
        "time": (0.02, lambda: _time(rng)),
        "date_time": (0.02, lambda: _date_time(rng)),
        "flight": (0.03, lambda: _flight(rng)),
        "zip": (0.03, lambda: rng.choice((f"{rng.choice(rc._HEADS)} {rng.randint(501, 99950):05d}",
                                          f"{rng.choice(rc._HEADS)} near {rng.randint(501, 99950):05d}"))),
        "zip_year": (0.02, lambda: f"{rng.choice(rc._HEADS)} {rng.randint(501, 99950):05d} {rng.randint(2019, 2026)}"),
        "version": (0.03, lambda: f"{rc._version(rng)} {rng.choice(('update', 'download', 'release notes', ''))}"),
        "quantity": (0.03, lambda: _qty(rng)),
        "year": (0.03, lambda: f"{rng.randint(1990, 2026)} {rng.choice(rc._CARS + rc._HEADS)} "
                               f"{rng.choice(('', 'review', 'best'))}"),
    }
    plan = []
    for cls, (share, _f) in makers.items():
        plan += [cls] * round(share * 1000)
    random.Random(seed).shuffle(plan)
    for i in range(n):
        cls = plan[i % len(plan)]
        yield cls, " ".join(makers[cls][1]().split())


TEXT_CLASSES = ("plain_text", "multilingual", "question", "local")


if __name__ == "__main__":                                # pragma: no cover - a reviewer's tool
    from collections import Counter
    print("closed list r19", Counter((c, n) for c, n, _p, _t, _v in closed_list_r19(50)))
    print("composed r19", Counter(c for c, *_ in composed_r19(4200)))
    o = Counter(c for c, _ in ordinary_r19(2400))
    print("ordinary r19", o, "digit-bearing share", sum(v for k, v in o.items() if k not in TEXT_CLASSES) / 2400)
