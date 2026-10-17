"""Personal data in free text (AEGIS wave 2 F-2, OOH-3/4 security F3, re-verification N1/N3). This department keeps
no personal data: an e-mail address or a phone number in a free-text value is refused (attributes, campaign names)
or redacted (vendor text).

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


def _spans(t: str) -> list:
    return [m.span() for m in _matches(t)]


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
    """The normalised text with every e-mail- or phone-like span replaced by "[redacted]" (case is kept), repeated
    until none is left (a redaction can bring two parts of a number together: delta red team DF-7)."""
    t = _norm(text)
    for _ in range(4):
        if not _spans(t):
            return t
        t = _redact_once(t)
    return t if not _spans(t) else "[redacted]"
