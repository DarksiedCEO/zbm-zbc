"""Personal data in free text (AEGIS wave 2 F-2, OOH-3/4 security F3, re-verification N1/N3, OOH2-PII-1). This
department keeps no personal data: an e-mail address, a phone number, a payment-card number or a US social security
number in a free-text value is refused (attributes, campaign names, the client approval document reference) or
redacted (vendor text), before anything is written to the log or the ledger.

Text is NFKC-normalised; zero-width characters are removed; %40, HTML entities (&#64; &commat; &#46;) and bracket
spellings ([at] (at) {at} <at>, [dot] (dot) <dot> [.] (.)) and full-width or ideographic dots are read as the
character. Then, case-insensitively:
* e-mail: ``@`` (spaces allowed around it) followed by a domain that starts with a letter and has a dotted TLD, or
  the word `` at `` followed by such a domain. A dot takes spaces on both sides or none ("jane @ example . com" is
  refused, "Digital @ Sunset. Faces west" passes), a spelled-out `` dot `` takes spaces;
* phone: a North American number (optional +1, 3-3-4 digits with optional separators or parentheses), a seven-digit
  local number with a dash (555-1234), an international number (+, country code, eight or more digits), or
  ten or more digits in a row. Separators are spaces, dots, slashes, underscores, every dash and the minus sign,
  commas, colons, bars, middle dots, apostrophes and ampersands. A number does not start right after a digit or
  after a digit and a decimal point, so dates, hours, ZIP+4, comma-grouped counts and GPS coordinates pass.
* payment card (OOH2-PII-1): a run of digit groups, with any separators between them, holding 13-19 digits that
  start with a card-network prefix and pass the Luhn check (the rule the client funds reference already used:
  ``card_spans``, which ``svc_campaigns.names_a_card`` calls); an unbroken run of ten or more digits is refused as
  a phone number anyway;
* social security number (onboarding-py redaction.py's shapes): ``ddd-dd-dddd`` with a hyphen or another dash, not
  glued to a word or a dash, and nine digits within 20 characters after "SSN", "social security (number)", "ITIN"
  or "tax id". A nine-digit run with spaces and no such word is not seen.
Known false positives (refused, never stored): a ten-digit reference ("io-0000012345"), "<word> at <word>.<word>"
("14x48 at Wilshire.Blvd"), a three-and-four-digit pair with a dash ("Route 101-2025").
Names, spelled-out digits and letters used as separators cannot be detected reliably: preparers must not type them."""

from __future__ import annotations

import re
import unicodedata

_ZW = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
# HTML entities and bracket spellings of "@" and "." read as the character (final red team NF-3)
_AT_SPELLINGS = re.compile(r"&#0*64;|&#x0*40;|&commat;|<at>|\[at\]|\(at\)|\{at\}", re.I)
_DOT_SPELLINGS = re.compile(r"&#0*46;|&#x0*2e;|&period;|<dot>|\[dot\]|\(dot\)|\{dot\}|\[\.\]|\(\.\)|\{\.\}|[\u3002\uff0e\uff61]",
                            re.I)
_LOCAL = r"[^\s@]{1,64}"                 # an address's local part (bounded: delta red team DF-6, regex cost)
_DOM = r"[\w-]+"
_WORD_DOM = r"[a-z][\w-]*"
# e-mail, three shapes:
# * tight "@" (no space on either side): any domain, dots with or without spaces ("jane@163.com", "jane@example .com",
#   delta red team DF-2);
# * spaced "@": a dot takes spaces on both sides or none ("Digital @ Sunset. Faces west" is not an address: NF-4);
# * the word " at ": a domain that starts with a letter ("Open at 6.pm" is not an address).
_DOTSEP = r"(?:\.|\s+\.\s+|\s+dot\s+)"
_EMAIL_TIGHT = re.compile(_LOCAL + r"@" + _DOM + r"(?:\s*\.\s*[a-z]{2,}|\s+dot\s+[a-z]{2,})+",
                          re.I)
_EMAIL_STRONG = re.compile(_LOCAL + r"(?:\s+@\s*|\s*@\s+)" + _DOM + r"(?:" + _DOTSEP + r"[a-z]{2,})+", re.I)
_EMAIL_WORD = re.compile(r"[\w.+-]{1,64}\s+at\s+" + _WORD_DOM + r"(?:" + _DOTSEP + r"[a-z]{2,})+", re.I)
# Phone separators: spaces, dots, slashes, underscores, dashes (U+2010-2015, U+2212 minus), commas, colons, bars,
# middle dots, apostrophes and ampersands, in any mix (final red team NF-3; delta red team 2 E3: "310-5551234",
# "310, 555, 1234"). The screen errs toward refusing: a false positive is refused and never stored, a miss is kept.
_SEPCH = "-\\s./_,:|·'&‐-―−"
_SEP = "[" + _SEPCH + "]*"
# A number does not start right after a digit, or after a digit and a decimal point (GPS 34.101 -118.3265: NF-4).
_START = r"(?<!\d)(?<!\d\.)"
_PHONE = re.compile(
    _START + r"(?:\+?1" + _SEP + r")?\(?\d{3}\)?" + _SEP + r"\d{3}" + _SEP + r"\d{4}(?!\d)"   # North American
    + r"|" + _START + r"\d{3}[‐-―−-]\d{4}(?![\d.,]\d|\d)"                          # local 555-1234
    + r"|(?<!\d)\d{10,}(?!\d)")                                                                   # ten digits or more
# An international number: "+", a country code, and at least eight digits in all (+52 55 1234 5678, +44 20 7946 0958)
_INTL = re.compile(r"(?<![\w+])\+\d{1,3}(?:[" + _SEPCH + r"()]*\d+)+")
_INTL_MIN_DIGITS = 8


def _norm(text: str) -> str:
    """NFKC; zero-width characters removed; every other control or format character read as a space (it is a space
    once vendor text is stored, red team OOH-3/4 F4); %40, HTML entities and bracket spellings of @ and . read as the
    character."""
    t = unicodedata.normalize("NFKC", text)
    t = _ZW.sub("", t)
    t = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in t)
    t = t.replace("%40", "@")
    t = _AT_SPELLINGS.sub("@", t)
    return _DOT_SPELLINGS.sub(".", t)


def _matches(t: str) -> list:
    out = [m for rx in (_EMAIL_TIGHT, _EMAIL_STRONG, _EMAIL_WORD, _PHONE) for m in rx.finditer(t)]
    return out + [m for m in _INTL.finditer(t) if sum(ch.isdigit() for ch in m.group()) >= _INTL_MIN_DIGITS]


# Card numbers start with a card-network prefix (IIN): Visa 4, Mastercard 51-55 and 2221-2720, Maestro 50/56-58,
# Mir 2200-2204, Amex 34/37, JCB 35, Diners 30/36/38/39, Discover, UnionPay and RuPay 6, RuPay 81/82, Uzcard 8600,
# Troy 9792, Humo 9860 (third delta review F1). A digit run that starts otherwise (0, 1, 7, most of 2, 8 and 9: dates
# such as 2026…, ABA routing numbers) is never read as a card, so a bank reference with a date is not refused by
# chance (second delta review E2). A 15-digit run is the length of an ACH trace number, whose ABA routing prefix can
# be 21-32 or 61-72: it counts only with a prefix no routing prefix starts with (4, 34-38, 50-58, 6011).
_CARD_PREFIX = re.compile(r"^(?:4|5[0-8]|220[0-4]|222[1-9]|22[3-9][0-9]|2[3-6][0-9]{2}|27[01][0-9]|2720|3[0-9]|6"
                          r"|8[12]|8600|9792|9860)")
_CARD_PREFIX_15 = re.compile(r"^(?:4|3[4-8]|5[0-8]|6011)")
_DIGIT_RUN = re.compile(r"[0-9]+(?:[^0-9a-z]+[0-9]+)*", re.I)
_GROUP = re.compile(r"[0-9]+")


def aba_routing(digits: str) -> bool:
    """A nine-digit ABA routing number: 3, 7, 1 weights over its digits sum to a multiple of ten."""
    if len(digits) != 9:
        return False
    return sum(int(d) * w for d, w in zip(digits, (3, 7, 1) * 3)) % 10 == 0


def luhn_card(digits: str) -> bool:
    """A payment-card number, never stored (re-verification N2)."""
    if not 13 <= len(digits) <= 19 or not _CARD_PREFIX.match(digits) \
            or (len(digits) == 15 and not _CARD_PREFIX_15.match(digits)):
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def card_spans(text: str) -> list:
    """The spans of digit runs (groups split by anything but a letter or a digit) that hold a payment-card number. A
    card is read across any separators ("4242.4242.4242.4242", "4242_4242…": final red team NF-2), and every
    contiguous part of a run is a candidate, so a card followed by another group is still seen (delta red team DF-1)."""
    out = []
    for run in _DIGIT_RUN.finditer(text):
        groups = _GROUP.findall(run.group())
        if any(_card_from(groups, i) for i in range(len(groups))):
            out.append(run.span())
    return out


def _card_from(groups: list, i: int) -> bool:
    joined = ""
    for j in range(i, len(groups)):
        # card groupings (4-4-4-4, 4-6-5, 4-4-4-4-3) have no group shorter than three digits, so a date's "-10-17" is
        # never joined into a card; a candidate stops growing past 19 digits, and an ABA routing number (nine digits
        # passing the routing checksum) is never joined to another group (third delta review F2; fourth G1:
        # "4242-424242424242" is still a card)
        if j > i and not all(len(g) >= 3 and not aba_routing(g) for g in (groups[i], groups[j])):
            return False
        joined += groups[j]
        if len(joined) > 19:
            return False
        if luhn_card(joined):
            return True
    return False


# US social security numbers (onboarding-py redaction.py's ``_SSN`` and ``_SSN_VALUE``; every dash, not only "-")
_DASH = "[-‐-―−]"
_SSN = re.compile(r"(?<![\w‐-―−-])\d{3}" + _DASH + r"\d{2}" + _DASH + r"\d{4}(?![\w‐-―−-])")
_SSN_WORDS = r"ssn|social\s+security(?:\s+(?:number|no|nr|#))?|itin|tax\s+id"
_SSN_VALUE = re.compile(rf"(?<![^\W_])(?:{_SSN_WORDS})(?![^\W_])[^\d\n]{{0,20}}(\d[\d\s‐-―−-]{{7,12}}\d)(?!\d)", re.I)


def _ssn_spans(t: str) -> list:
    out = [m.span() for m in _SSN.finditer(t)]
    return out + [m.span(1) for m in _SSN_VALUE.finditer(t) if sum(ch.isdigit() for ch in m.group(1)) == 9]


def _spans(t: str) -> list:
    return [m.span() for m in _matches(t)] + card_spans(t) + _ssn_spans(t)


def has_personal_data(text: str) -> bool:
    return bool(_spans(_norm(text)))


def _redact_once(t: str) -> str:
    spans = sorted(_spans(t))
    out, pos = [], 0
    for lo, hi in spans:
        if hi <= pos:
            continue
        out.append(t[pos:max(lo, pos)])
        out.append("[redacted]")
        pos = hi
    return "".join(out) + t[pos:]


def redact(text: str) -> str:
    """The normalised text with every e-mail-, phone-, card- or SSN-like span replaced by "[redacted]" (case is kept),
    repeated until none is left (a redaction can bring two parts of a number together: delta red team DF-7)."""
    t = _norm(text)
    for _ in range(4):
        if not _spans(t):
            return t
        t = _redact_once(t)
    return t if not _spans(t) else "[redacted]"
