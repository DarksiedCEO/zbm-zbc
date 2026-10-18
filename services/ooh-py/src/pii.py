"""Personal data in free text (AEGIS wave 2 F-2, OOH-3/4 security F3, re-verification N1/N3, OOH2-PII-1). This
department keeps no personal data: an e-mail address, a phone number, a payment-card number or a US social security
number in a free-text value is refused (attributes, campaign names, the client approval document reference) or
redacted (vendor text), before anything is written to the log or the ledger.

Every screen reads ``screen_form`` (vendor text is stored in it too): NFKC; zero-width, format, private-use,
and invisible filler characters removed (unassigned characters kept as typed), and a combining mark on a digit
dropped, to a fixed point; every script's decimal
digits read as ASCII; any other non-printable character read as a space and runs of spaces collapsed. Then %40, HTML entities (&#64; &commat; &#46;) and bracket
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
* social security number (onboarding-py redaction.py's shapes, widened in AEGIS r9): 3-2-4 digits split by any
  dash, a dot, an underscore, a colon, a slash or one space (runs of spaces are one space by then), not inside a
  longer digit run (letters may touch it), and nine digits within 20 characters after "SSN", "social security
  (number)", "ITIN", "TIN" or "tax id", which may touch them. Not seen: nine digits with no separator and no such
  word; "123 - 45 - 6789" (a separator with spaces around it); digit groups split by letters, %20 or entities.
In the three screened free-text fields of OOH2-PII-1 (unit vendor text, face text attributes, the client approval
document reference) one more rule decides, before any of the shapes below matter: any cluster of nine or more digits
joined by characters that are not ASCII letters is personal data, in the raw and in the HTML-decoded text
(``screen_free_text``; founder design change and decisions, ADR 0022).
Known limit (founder decision): URL escapes other than %40 and JS/CSS escapes are not decoded; their hex letters
end a cluster ("4111%2D1111%2D1111%2D1111" is kept).
Known false positives (refused, never stored): a ten-digit reference ("io-0000012345"), "<word> at <word>.<word>"
("14x48 at Wilshire.Blvd"), a three-and-four-digit pair with a dash ("Route 101-2025").
Names, spelled-out digits and letters used as separators cannot be detected reliably: preparers must not type them."""

from __future__ import annotations

import html
import re
import unicodedata

_ZW = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
# HTML entities and bracket spellings of "@" and "." read as the character (final red team NF-3)
_AT_SPELLINGS = re.compile(r"&#0*64;|&#x0*40;|&commat;|<at>|\[at\]|\(at\)|\{at\}", re.I)
_DOT_SPELLINGS = re.compile(r"&#0*46;|&#x0*2e;|&period;|<dot>|\[dot\]|\(dot\)|\{dot\}|\[\.\]|\(\.\)|\{\.\}|[\u3002\uff0e\uff61]",
                            re.I)
_LOCAL = r"[^\s@]{1,64}"                 # an address's local part (bounded: delta red team DF-6, regex cost)
# A word character for the e-mail and phone shapes, the same on every Unicode version (AEGIS r12 reliability R12-2:
# `\w` follows the Python build's Unicode database, so a CJK Extension I letter, unassigned in Python 3.12 and a
# letter in 3.13, made "a@b<it>d.com" an address on one and not the other): ASCII letters, digits and "_", and every
# non-ASCII character that is not a space (wider than `\w`: fails closed).
_W = r"(?:[A-Za-z0-9_]|[^\x00-\x7f\s])"
_DOM = rf"(?:{_W}|-)+"
_WORD_DOM = rf"[a-z](?:{_W}|-)*"
# e-mail, three shapes:
# * tight "@" (no space on either side): any domain, dots with or without spaces ("jane@163.com", "jane@example .com",
#   delta red team DF-2);
# * spaced "@": a dot takes spaces on both sides or none ("Digital @ Sunset. Faces west" is not an address: NF-4);
# * the word " at ": a domain that starts with a letter ("Open at 6.pm" is not an address).
_DOTSEP = r"(?:\.|\s+\.\s+|\s+dot\s+)"
_EMAIL_TIGHT = re.compile(_LOCAL + r"@" + _DOM + r"(?:\s*\.\s*[a-z]{2,}|\s+dot\s+[a-z]{2,})+",
                          re.I)
_EMAIL_STRONG = re.compile(_LOCAL + r"(?:\s+@\s*|\s*@\s+)" + _DOM + r"(?:" + _DOTSEP + r"[a-z]{2,})+", re.I)
_EMAIL_WORD = re.compile(rf"(?:{_W}|[.+-]){{1,64}}\s+at\s+" + _WORD_DOM + r"(?:" + _DOTSEP + r"[a-z]{2,})+", re.I)
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
_INTL = re.compile(r"(?<![A-Za-z0-9_+])(?<![^\x00-\x7f\s])\+\d{1,3}(?:[" + _SEPCH + r"()]*\d+)+")
_INTL_MIN_DIGITS = 8


# Characters that show nothing but are not format characters (AEGIS r10 source M-1): the combining grapheme joiner,
# Hangul and Mongolian fillers, Khmer inherent vowels, Mongolian variation selectors, the blank braille pattern and
# the variation selectors.
_INVISIBLE = re.compile("[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u2800\u3164\ufe00-\ufe0f\uffa0"
                        "\U000e0100-\U000e01ef]")
# format and private-use characters: removed, not read as a space. Unassigned characters (Cn) are neither removed nor
# read as a space but kept as typed (founder decision, AEGIS r11 reliability N1: which code points are unassigned
# differs between Unicode versions, so removing them made the kept text and the request hash depend on the Python
# build); the digit-cluster rule joins across them anyway.
_REMOVED = ("Cf", "Co")


def strip_format(text: str, hidden: str = "") -> str:
    """NFKC; zero-width, format, private-use and the invisible characters above replaced by ``hidden``
    (removed by default), and a combining mark on a digit dropped (AEGIS r9 security F2, r10 source M-1); every decimal
    digit of any script and every other character with a digit value (circled, dingbat, Ethiopic) read as its ASCII
    digit (r9 source S-1: "٤٢٤٢ ٤٢٤٢ …" is a card; r12 security S12-1). Removing a hidden character
    rejoins a digit group it split (a card); reading it as a space keeps it as a separator (an SSN): the screen reads
    both forms."""
    t = _INVISIBLE.sub(hidden, _ZW.sub(hidden, unicodedata.normalize("NFKC", text)))
    out = []
    for ch in t:
        cat = unicodedata.category(ch)
        if cat in ("Mn", "Mc", "Me") and out and out[-1].isdigit():
            continue
        if cat in _REMOVED:
            if hidden:
                out.append(hidden)
            continue
        d = unicodedata.digit(ch, None) if ch > "\x7f" else None      # every digit, not only decimal ones (AEGIS
        out.append(str(d) if d is not None else ch)                    # r12 security S12-1: "❶", "⓵", Ethiopic "፩")
    return "".join(out)


def screen_form(text: str, hidden: str = "") -> str:
    """The one form every screen reads and vendor text is stored in (AEGIS r10 security F1: the screen read the raw
    text while storage read a cleaned copy, so "123  45  6789" was redacted in the line but hashed in clear):
    ``strip_format``, every other non-printable character (controls, line and paragraph separators, other spaces)
    read as a space, and runs of spaces collapsed to one."""
    t = text
    for _ in range(5):                    # to a fixed point (AEGIS r12 source S-2b: NFKC ran before a hidden character
        once = " ".join("".join(ch if ch.isprintable() or unicodedata.category(ch) == "Cn" else " "   # was removed,
                                for ch in strip_format(t, hidden)).split())                          # so one pass
        if once == t:                                                                                # was not enough)
            break
        t = once
    return t


def _norm(text: str, hidden: str = "") -> str:
    """``screen_form``; %40, HTML entities and bracket spellings of @ and . read as the character."""
    t = screen_form(text, hidden)
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
_DIGIT_RUN = re.compile(r"[0-9]+(?:[^0-9a-zA-Z]+[0-9]+)*")      # no re.I: it folded dotless ı into "i" (r9 F6)
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


# US social security numbers (onboarding-py redaction.py's two shapes, made general in AEGIS r10 after separator
# lists kept missing spellings: r9 F3, r10 data D10-H1): 3, 2 and 4 digits split by one to three characters that are
# neither letters nor digits ("123-45-6789", "123 - 45 - 6789", "123--45--6789", "123..45..6789", "123,45,6789",
# "123|45|6789"), not inside a longer digit run (letters may touch it); and nine digits, one to three such characters
# allowed between any two, within 20 characters after an SSN word, which may touch them ("SSN123456789", "TIN 123 45
# 6789").
_SSEP = r"[^0-9A-Za-z\n]{1,3}"
_SSN = re.compile(r"(?<!\d)\d{3}" + _SSEP + r"\d{2}" + _SSEP + r"\d{4}(?!\d)")
_SSN_WORDS = r"ssn|social\s*security(?:\s*(?:number|no|nr|#))?|itin|tin|tax\s*id"
_SSN_VALUE = re.compile(rf"(?<![a-z])(?:{_SSN_WORDS})(?![a-z])[^\d\n]{{0,20}}(\d(?:[^0-9A-Za-z\n]{{0,3}}\d){{8}})(?!\d)",
                        re.I)


def _ssn_spans(t: str) -> list:
    out = [m.span() for m in _SSN.finditer(t)]
    return out + [m.span(1) for m in _SSN_VALUE.finditer(t)]


def _spans(t: str) -> list:
    return [m.span() for m in _matches(t)] + card_spans(t) + _ssn_spans(t)


def _screened_form(text: str) -> str:
    """The form the screen found personal data in: hidden characters removed, else read as spaces."""
    t = _norm(text)
    if _spans(t):
        return t
    alt = _norm(text, " ")
    return alt if _spans(alt) else t


def has_personal_data(text: str) -> bool:
    return bool(_spans(_screened_form(text)))


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


# The three screened free-text fields of OOH2-PII-1 (unit vendor text, a face text attribute, the client approval
# document reference): founder design change, Oct 18 2026, to end the spelling-by-spelling loop. After ``_norm``, any
# cluster of CLUSTER_MIN_DIGITS or more digits joined by characters that are not ASCII letters — whatever those characters
# are — is personal data. The card and SSN rules no longer decide; they only label what was redacted. Trade-off
# (accepted): a date range, a price list or a reference with nine or more digits and no letter between them is
# redacted or refused too ("2026-11-01 - 2026-11-30", "io-2026-1018-0001").
CLUSTER_MIN_DIGITS = 9


_ASCII_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


def number_cluster_spans(t: str) -> list:
    """The spans, first digit to last, of every run of text without an ASCII letter that holds CLUSTER_MIN_DIGITS
    digits. Only A-Z and a-z end a cluster (founder decision, AEGIS r11 runtime O11-1 / security S11-1: Unicode letters
    that look like punctuation, "ー", "ʼ", "ǀ", tatweel, joined digits unseen); every other character joins it."""
    out, first, last, count = [], None, None, 0
    for i, ch in enumerate(t + "a"):
        if ch in _ASCII_LETTERS:
            if count >= CLUSTER_MIN_DIGITS:
                out.append((first, last + 1))
            first, last, count = None, None, 0
        elif "0" <= ch <= "9":
            first = i if first is None else first
            last, count = i, count + 1
    return out


def _label(seg: str) -> str:
    if "@" in seg or _EMAIL_WORD.search(seg):
        return "email"
    if card_spans(seg):
        return "card"
    if _ssn_spans(seg):
        return "ssn"
    if _PHONE.search(seg) or _INTL.search(seg):
        return "phone"
    return "number"


def _free_text_spans(t: str) -> list:
    return sorted(_spans(t) + number_cluster_spans(t))


def _redact_all(t: str, find, kinds: set) -> str:
    """``t`` with every span ``find`` reports replaced by "[redacted]" (labelled into ``kinds``), until none is left."""
    for _ in range(4):
        spans = sorted(find(t))
        if not spans:
            return t
        out, pos = [], 0
        for lo, hi in spans:
            if hi <= pos:
                continue
            kinds.add(_label(t[lo:hi]))
            out.append(t[pos:max(lo, pos)])
            out.append("[redacted]")
            pos = hi
        t = "".join(out) + t[pos:]
    return "[redacted]" if find(t) else t


_MAX_DECODE = 16          # HTML-entity decoding passes; text still changing after that is redacted whole


def _fully_decoded(t: str):
    """``t`` with HTML entities decoded until nothing changes (entities that spell entities: "&amp;amp;nbsp;"), or None
    when it still changes after _MAX_DECODE passes (AEGIS r12 source S-1: three passes let four levels through)."""
    for _ in range(_MAX_DECODE):
        once = html.unescape(t)
        if once == t:
            return t
        t = once
    return None


def _screen_stages(t: str, kinds: set) -> str:
    """The two stages every form of the text goes through (AEGIS r11 source S-1): the cluster rule on the text as it
    would be kept, then every rule after the @ and . spellings are read."""
    t = _redact_all(screen_form(t), number_cluster_spans, kinds)
    return _redact_all(_norm(t), _free_text_spans, kinds)


def _forms(t: str):
    """Every form a reader could make of ``t``: as kept, ``screen_form``, ``_norm``, and the same three of the fully
    HTML-decoded text; None when the decoding never settles."""
    d = _fully_decoded(t)
    if d is None:
        return None
    out = []
    for f in (t, d):
        out += [f, screen_form(f), _norm(f)]
    return out


def _still_holds_personal_data(t: str) -> bool:
    forms = _forms(t)
    return forms is None or any(number_cluster_spans(f) for f in forms) or any(_free_text_spans(_norm(f)) for f in forms)


def screen_free_text(text: str) -> tuple:
    """(the text as kept, the sorted kinds of what was redacted) for the three screened free-text fields.
    - The raw text goes through both stages (``_screen_stages``). The raw text decoded to a fixed point (founder
      decision after r11) goes through the same two stages (AEGIS r12 source S-2a: one stage, after the @ spellings
      were read, let "1234567&nbsp;%40" through), and that result is kept when the decoded form held personal data.
    - Backstop, on whatever is returned: if any form of it (``_forms``) still shows a cluster or another span, or the
      decoding never settles (r12 source S-1: four levels of nesting), the whole value is "[redacted]" — an edge case
      not yet found redacts rather than keeps.
    With nothing found, the text as kept is ``screen_form(text)``."""
    kinds = set()
    t = _screen_stages(text, kinds)
    d = _fully_decoded(text)
    if d is None:
        return "[redacted]", sorted(kinds | {"number"})
    if d != text:
        found = set()
        t2 = _screen_stages(d, found)
        if found:
            kinds |= found
            t = t2
    kept = " ".join(t.split()) if kinds else screen_form(text)
    if _still_holds_personal_data(kept):
        return "[redacted]", sorted(kinds | {"number"})
    return kept, sorted(kinds)


def free_text_has_personal_data(text: str) -> bool:
    return bool(screen_free_text(text)[1])


def redact(text: str) -> str:
    """The normalised text with every e-mail-, phone-, card- or SSN-like span replaced by "[redacted]" (case is kept),
    repeated until none is left (a redaction can bring two parts of a number together: delta red team DF-7)."""
    t = _screened_form(text)
    for _ in range(4):
        if not _spans(t):
            return t
        t = _redact_once(t)
    return t if not _spans(t) else "[redacted]"
