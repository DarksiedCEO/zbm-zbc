"""The INDEPENDENT oracle of priya-spec/1 (OQ-2, Z-1): does a query hold a value of the closed list of ADR 0047?

Written from the list's text, not from ``search_performance`` (it imports nothing of the classifier). It answers
``mandated(q) -> rule name | None``; the rule names are the spec's: stream_card, stream_nanp, stream_ssn,
stream_e164, vanity, email, address (spelled digits feed the stream rules; ``spelled_`` is prefixed when a number
word was needed).

Reading of the list (priya-spec/1 sections 1-2, CP-3):
- characters: NFKC per character (fullwidth letters and digits become ASCII), every decimal digit of any script is
  its digit, combining marks, format characters (Cf) and the listed invisibles are removed, case is folded; every
  other character counts as ONE character of a gap;
- units: a digit; a number word (English zero..nine; Spanish cero..nueve, uno / una) standing as a whole word, or
  inside a run of letters that splits completely into number words and runs of at most three other letters ("oh"
  only as a whole word), a leftover run only BETWEEN two units (priya-spec/1.1 C-4: "phone" is a word);
- the stream: consecutive groups of units whose gaps are at most three characters of any kind; every window of
  whole consecutive groups is read as one number: a card (13-19 digits, Luhn, an issuer prefix or UATP 1 / 15), an
  SSN (9 digits, valid parts), a NANP phone (10, or 11 after a 1; area code and exchange 2-9), or after a '+' up to
  three characters before the first digit an E.164 phone (8-15 digits);
- vanity: 1 (optional), 8xx (800 833 844 855 866 877 888) with gaps of at most three non-digit characters between
  its digits, then seven keypad characters (letters / digits, at least one letter) with at most three
  non-alphanumeric characters before and between them, not glued to letters or digits on either side;
- e-mail: a mailbox (local part, '@', labels, '.', a TLD of 2-24 letters) as written, with the at-words / dot-words
  of priya-spec/1.1 C-1 read as '@' / '.', brackets and '_' / '-' around them, rot13'd, reversed (whole or word by word),
  or base64 / base32 / hex / percent encoded (whole words, chunks joined);
- address: the ADR 0047 address rule, written out below from the ADR's text."""

from __future__ import annotations

import base64
import binascii
import codecs
import re
import unicodedata
from urllib.parse import unquote

EN = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
ES = ("cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve")
WORDS = {w: str(i) for i, w in enumerate(EN)}
WORDS.update({w: str(i) for i, w in enumerate(ES)})
WORDS["una"] = "1"
WHOLE_ONLY = {"oh": "0"}
INVISIBLE = frozenset("\u200b\u200c\u200d\u2060\ufeff\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068"
                      "\u2069\u061c\u180e\u00ad\u034f\u2061\u2062\u2063\u2064")
GAP = 3


def chars(q: str) -> str:
    """The text as the oracle reads it: one output character per kept input character."""
    out = []
    for c in unicodedata.normalize("NFC", q or ""):
        cat = unicodedata.category(c)
        if c in INVISIBLE or cat in ("Cf", "Mn", "Me") or "\ufe00" <= c <= "\ufe0f":
            continue
        if cat == "Nd":
            out.append(str(unicodedata.digit(c)))
            continue
        if cat[0] == "Z" and c not in " \u3000":
            out.append(c)          # a space character other than the typed ones: a separator of the list (D06)
            continue
        n = unicodedata.normalize("NFKC", c)
        if len(n) == 1 and n.isascii() and (n.isalnum() or not c.isascii()):
            c = n
        else:
            d = unicodedata.normalize("NFKD", c)
            base = "".join(x for x in d if unicodedata.category(x) not in ("Mn", "Me"))
            if len(base) == 1 and base.isascii() and base.isalpha():
                c = base
        out.append(c.lower() if len(c.lower()) == 1 else c)
    return "".join(out)


def _split_letters(run: str, lead_max: int = GAP, trail_max: int = GAP):
    """Split a run of ASCII letters into [(digit or None, text)]: number words and leftover runs of at most three
    letters between them; None when the run does not split so (or holds no number word). priya-spec/1.1 C-4 (CP-3: a
    gap stands only BETWEEN two units): a leftover may begin the run only with at most ``lead_max`` letters and end
    it only with at most ``trail_max`` (the room a gap to the neighbouring unit leaves), so "phone", "someone",
    "oneplus" standing as words hold no number word."""
    best = {len(run): []}
    for i in range(len(run) - 1, -1, -1):
        options = []
        for w, d in WORDS.items():
            if run.startswith(w, i) and i + len(w) in best:
                options.append([(d, w)] + best[i + len(w)])
        for k in range(1, GAP + 1):
            j = i + k
            if i == 0 and k > lead_max:
                break
            if j == len(run) and k > trail_max:
                continue
            if j in best and (not best[j] or best[j][0][0] is not None):
                options.append([(None, run[i:j])] + best[j])
        if options:
            best[i] = max(options, key=lambda o: sum(1 for d, _ in o if d is not None))
    seq = best.get(0)
    if not seq or not any(d is not None for d, _ in seq):
        return None
    return seq


def _first_word_at(run: str):
    """How many letters stand before the first number word of a letter run (0-3), or None."""
    for k in range(min(GAP, len(run)) + 1):
        if any(run.startswith(w, k) for w in WORDS):
            return k
    return None


# the white space typed between words (ASCII, and the ideographic space): it ends a word
_WORD_END = re.compile("[ \t\n\r\f\v\u3000]")


def units(text: str) -> tuple[str, list]:
    """The text with number words read as digits (a whole word, or a letter run that splits as ``_split_letters``
    says), and how many number words were read. Positions: each number word becomes ONE character (its digit), so
    gaps are measured in the remaining characters. A letter run's leftover may face a neighbouring unit (a digit, or
    a number word of the next / previous run) directly, or across characters that are neither letters, digits nor
    typed white space, the whole gap at most three characters; typed white space ends a word (priya-spec/1.1
    C-4)."""
    out, n, i = [], 0, 0
    runs = list(re.finditer(r"[a-z]+", text))
    prev_tail = None                          # letters after the last unit read before this run, None: no unit
    last_end = 0
    for idx, m in enumerate(runs):
        out.append(text[i:m.start()])
        run = m.group()
        between = text[last_end:m.start()]
        # digits between the previous run and this one are units; letters after them: none
        last_digit = re.search(r"\d(?=\D*$)", between)
        if last_digit:
            stretch = between[last_digit.end():]
            lead = GAP - len(stretch) if not _WORD_END.search(stretch) else 0
        elif idx and prev_tail is not None and not _WORD_END.search(between):
            lead = GAP - len(between) - prev_tail
        else:
            lead = 0
        nxt = text[m.end():]
        stretch = re.match(r"[^a-z0-9]*", nxt).group()
        follow = nxt[len(stretch):]
        trail = 0
        if not _WORD_END.search(stretch) and follow:
            if follow[0].isdigit():
                trail = GAP - len(stretch)
            else:
                k = _first_word_at(re.match(r"[a-z]*", follow).group())
                if k is not None:
                    trail = GAP - len(stretch) - k
        if run in WORDS or run in WHOLE_ONLY:
            out.append(WORDS.get(run) or WHOLE_ONLY[run])
            n += 1
            prev_tail = 0
        else:
            seq = _split_letters(run, max(lead, 0), max(trail, 0))
            if seq is None:
                out.append(run)
                prev_tail = None
            else:
                out.append("".join(d if d is not None else t for d, t in seq))
                n += sum(1 for d, _ in seq if d is not None)
                prev_tail = len(seq[-1][1]) if seq[-1][0] is None else 0
        i = last_end = m.end()
    out.append(text[i:])
    return "".join(out), n


def luhn(d: str) -> bool:
    s = 0
    for i, ch in enumerate(reversed(d)):
        x = int(ch)
        if i % 2:
            x = x * 2 - 9 if x > 4 else x * 2
        s += x
    return s % 10 == 0


def issuer(d: str) -> bool:
    p2, p3, p4 = int(d[:2]), int(d[:3]), int(d[:4])
    return (d[0] == "4" or 50 <= p2 <= 58 or 2221 <= p4 <= 2720 or p2 in (34, 36, 37, 38, 39) or 300 <= p3 <= 305
            or 3528 <= p4 <= 3589 or d[0] == "6" or p2 in (81, 82) or 2200 <= p4 <= 2204 or p4 == 9792)


def is_card(d: str) -> bool:
    return 13 <= len(d) <= 19 and (issuer(d) or (len(d) == 15 and d[0] == "1")) and luhn(d)


def is_ssn(d: str) -> bool:
    return len(d) == 9 and d[:3] not in ("000", "666") and d[0] != "9" and d[3:5] != "00" and d[5:] != "0000"


def is_nanp(d: str) -> bool:
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return len(d) == 10 and d[0] in "23456789" and d[3] in "23456789"


def stream(text: str):
    """The stream rules on one view: the rule name, or None."""
    groups = [(m.start(), m.end(), m.group()) for m in re.finditer(r"\d+", text)]
    for a in range(len(groups)):
        start = groups[a][0]
        lo = max(groups[a - 1][1] if a else 0, start - GAP - 1)
        plus = "+" in text[lo:start]
        d = ""
        for b in range(a, len(groups)):
            if b > a and groups[b][0] - groups[b - 1][1] > GAP:
                break
            d += groups[b][2]
            if len(d) > 19:
                break
            if is_card(d):
                return "stream_card"
            if is_ssn(d):
                return "stream_ssn"
            if is_nanp(d):
                return "stream_nanp"
            if plus and 8 <= len(d) <= 15:
                return "stream_e164"
    return None


_VG = r"[^0-9]{0,3}"
_VANITY = re.compile(r"(?<![a-z0-9])(?:1" + _VG + r")?8" + _VG + r"(?:0" + _VG + r"0|3" + _VG + r"3|4" + _VG + r"4|5"
                     + _VG + r"5|6" + _VG + r"6|7" + _VG + r"7|8" + _VG + r"8)[^a-z0-9]{0,3}"
                     r"([a-z0-9](?:[^a-z0-9]{0,3}[a-z0-9]){6})(?![a-z0-9])")


def vanity(text: str) -> bool:
    return any(re.search(r"[a-z]", m.group(1)) for m in _VANITY.finditer(text))


# ------------------------------------------------------------------ e-mail
# priya-spec/1.1 C-1: the closed at-word and dot-word lists (any case; a hyphenated word also spaced or run together)
AT_WORDS = ("at", "arroba", "arobase", "chiocciola", "klammeraffe", "at-zeichen", "malpa", "kukac", "snabel-a",
            "apenstaartje", "sobaka")
DOT_WORDS = ("dot", "punto", "point", "ponto", "punkt", "kropka", "pont", "prikk", "piste", "nokta", "titik")
_MAILBOX = re.compile(r"[a-z0-9_+\-.]@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,24}(?![a-z])")
_BRACKET = r"[\[\(\{<]"
_CLOSE = r"[\]\)\}>]"


def _despell(t: str) -> str:
    """At-words and dot-words (bare between spaces, or in brackets, or between '_' / '-') read as '@' / '.'; spaces
    around '@' and '.' removed."""
    # dot-words first: an at-word right after a dot-word is the top-level domain's letters ("... dot at")
    for words, sym in ((DOT_WORDS, "."), (AT_WORDS, "@")):
        forms = set(words) | {w.replace("-", " ") for w in words} | {w.replace("-", "") for w in words}
        alt = "|".join(re.escape(w) for w in sorted(forms, key=len, reverse=True))
        t = re.sub(_BRACKET + r"\s*(?:" + alt + r"|" + re.escape(sym) + r")\s*" + _CLOSE, " " + sym + " ", t)
        t = re.sub(r"(?:(?<=\s)|(?<=^)|(?<=[_\-]))(?:" + alt + r")(?=\s|$|[_\-])",
                   lambda m, t=t, sym=sym: m.group() if sym == "@" and t[:m.start()].rstrip(" \t_-").endswith(".")
                   else sym, t)
    t = re.sub(r"[\s_\-]*([@.])[\s_\-]*", r"\1", t)
    return t


def _mail_any(t: str) -> bool:
    return bool(_MAILBOX.search(t)) or bool(_MAILBOX.search(_despell(t)))


def _decodes(tok: str):
    s = tok.strip("=")
    if len(s) >= 8 and re.fullmatch(r"[A-Za-z0-9+/_\-]+", s):
        for fn in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                yield fn(s + "=" * (-len(s) % 4)).decode("ascii")
            except (binascii.Error, ValueError, UnicodeDecodeError):
                pass
    if len(s) >= 8 and re.fullmatch(r"[A-Za-z2-7]+", s):
        b = s.upper()
        b = b[:len(b) - len(b) % 8 + (0, 0, 2, 2, 4, 5, 5, 7)[len(b) % 8]]
        try:
            yield base64.b32decode(b + "=" * (-len(b) % 8)).decode("ascii")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            pass
    if len(s) >= 10 and re.fullmatch(r"(?:[0-9a-fA-F]{2})+", s):
        try:
            yield bytes.fromhex(s).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            pass
    if "%" in tok:
        yield unquote(tok)


def email(raw: str, text: str) -> bool:
    text = re.sub(r"%\s*([0-9a-f])\s*([0-9a-f])", r"%\1\2", text)
    text = unquote(text) if "%" in text else text
    views = [text, codecs.encode(text, "rot13"), text[::-1], " ".join(w[::-1] for w in text.split())]
    if any(_mail_any(v) or _MAILBOX.search(_despell(v)[::-1]) for v in views):
        return True
    # chunks: consecutive words joined (their edge punctuation set aside)
    # (a chunk boundary may fall next to the '.' of the domain: "x@y. zz" - a full stop is kept at a word's edge)
    words = [re.sub(r"^[^a-z0-9@.\[\(\{<]+|[^a-z0-9@.\]\)\}>]+$", "", w) for w in text.split()]
    for i in range(len(words)):
        for j in range(i + 1, min(len(words), i + 12) + 1):
            if _mail_any("".join(words[i:j])):
                return True
    toks = [re.sub(r"^[^A-Za-z0-9+/=%_-]+|[^A-Za-z0-9+/=%_-]+$", "", w) for w in raw.split()]
    joined = ["".join(toks[i:j]) for i in range(len(toks)) for j in range(i + 1, min(len(toks), i + 12) + 1)]
    for s in joined + [unquote(raw)]:
        for v in (s, codecs.encode(s, "rot13"), s[::-1]):
            for u in _decodes(v):
                u = u.lower()
                if any(_mail_any(x) for x in (u, codecs.encode(u, "rot13"), u[::-1])):
                    return True
                if any(_mail_any(y.lower()) for w in u.split() for y in _decodes(w)):
                    return True
    return False


# ------------------------------------------------------------------ the ADR 0047 address rule, written out
STRONG = ("street", "avenue", "boulevard", "st", "ave", "av", "rd", "blvd", "ln", "dr", "ct", "pl", "ter", "cir",
          "hwy", "pkwy", "trl", "cres", "aly", "plz", "rte", "fwy", "expy", "tpke")
ORDINARY = ("road", "lane", "drive", "court", "way", "terrace", "place", "loop", "plaza")
STOP = set("a an the of for in on at to and or my your with near by from best top cheap cheapest hour hours hr hrs "
           "day days week weeks month months year years minute minutes bedroom bedrooms bed beds bath baths car cars "
           "star stars person people room rooms story stories way ways things tips step steps lane lanes door doors "
           "inch inches ft foot feet mile miles unit units flat apt suite ste places easy simple quick great ton amp "
           "gallon season stroke percent sq ct pl ln rd dr st old new good".split())
DIRS = set("n s e w ne nw se sw north south east west".split())
STATES = set("al ak az ar ca co ct de dc fl ga hi id il ia ks ky la ma md mi mn ms mo mt ne nv nh nj nm ny nc nd oh "
             "pa ri sc sd tn tx ut vt va wa wv wi wy".split())


def address(text: str) -> bool:
    w = re.findall(r"[a-z0-9#]+", text)
    for i, tok in enumerate(w):
        if not re.fullmatch(r"\d{1,6}[a-z]?", tok) or (i and w[i - 1] in ("top", "best")):
            continue
        j = i + 1
        if j < len(w) and w[j] in DIRS:
            j += 1
        adj = j < len(w) and w[j] in ("old", "new", "good")
        k = j + (1 if adj else 0)
        names = 0
        while k < len(w) and names < 3 and w[k] not in STOP and w[k] not in STRONG + ORDINARY \
                and re.fullmatch(r"[a-z][a-z'.-]*|\d{1,4}(?:st|nd|rd|th)", w[k]):
            names += 1
            k += 1
        if k < len(w) and (w[k] in STRONG or w[k] in ORDINARY) and (names or adj):
            if names or k + 1 == len(w):
                return True
    if re.search(r"(?<![a-z0-9])p\.?\s*o\.?\s*box\s*#?\s*\d+|(?<![a-z0-9])pmb\s*#?\s*\d+|"
                 r"(?<![a-z0-9])(?:rr|hc|rural route)\s*#?\s*\d+\s*,?\s*box\s*#?\s*\d+|(?<!\d)\d{5}\s*-\s*\d{4}(?!\d)",
                 text):
        return True
    if re.search(r"(?<![a-z0-9])[abceghj-nprstvxy]\d[abceghj-nprstv-z]\s?\d[abceghj-nprstv-z]\d(?![a-z0-9])", text):
        return True                                       # Canadian postal code
    if re.search(r"(?:^|\s)\d{1,5}(?:\s*(?:bis|ter))?\s*,?\s+(?:(?:rue|chemin|impasse|allee|quai)\s+|"
                 r"(?:place|route|avenue|boulevard)\s+(?:de|du|des|d')\s*)[a-z]", text):
        return True                                       # French / Quebec leading forms
    for m in re.finditer(r"(?:^|[\s,])([a-z]{2})(?:\.\s*,?\s*|\s*,\s*|\s+)(\d{5})(?!\d)", text):
        if m.group(1) in STATES and not re.match(r"\s*(?:btu|miles|mi|km|jobs|sq|sqft|ft|zip|code|people|units)\b",
                                                  text[m.end():]):
            return True
    return False


def mandated(q: str):
    """The rule of the closed list a query holds a value of, or None."""
    text = chars(q)
    if len(q) > 512:
        return "over_long"
    r = stream(text)
    if r:
        return r
    spelled, n = units(text)
    if n:
        r = stream(spelled) or stream("".join(c for c in spelled if c == "+" or c.isalnum()))
        if r:
            return "spelled_" + r
    if vanity(text) or (n and vanity(units_whole(text))):
        return "vanity"
    if email(q, text):
        return "email"
    if address(text):
        return "address"
    return None


def units_whole(text: str) -> str:
    """Only WHOLE number words read as digits (the vanity code spelled; the seven letters may hold a number word)."""
    return re.sub(r"[a-z]+", lambda m: WORDS.get(m.group()) or WHOLE_ONLY.get(m.group()) or m.group(), text)
