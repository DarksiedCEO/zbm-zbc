"""
Deterministic text normalisation and phrase matching.

Submitted text (captions, bios, transcripts) is DATA. It is only ever
normalised and searched for configured phrases; it is never interpreted,
executed, or allowed to change which rules apply.

`canonical()` removes the evasions before any match (fix wave 1, F12;
fix wave 2, N3):
- NFKC (fullwidth / mathematical alphanumerics / ligatures -> plain letters);
- every Unicode Default_Ignorable_Code_Point (`DEFAULT_IGNORABLE_RANGES`,
  the complete list from DerivedCoreProperties.txt: soft hyphen, CGJ,
  Hangul fillers, zero-width space/joiners, bidi marks/embeddings/
  overrides/isolates, word joiner and invisible operators, variation
  selectors, BOM, tag characters, ...) is removed — the ones that RENDER
  AS A BLANK (Hangul fillers U+115F/U+1160/U+3164/U+FFA0, Mongolian vowel
  separator U+180E) become a space, the rest are deleted — and so is any
  other format character (Unicode Cf);
- diacritics are stripped (NFKD, drop combining marks, NFC);
- casefold;
- lookalike letters map to the Latin letter they imitate (`fold_table()`):
  (1) a hand table (`CONFUSABLES`) of Cyrillic, Greek, Armenian, Cherokee
  and IPA / small-capital lookalikes, taken from Unicode confusables.txt
  (the visually near-identical entries); (2) a GENERATED table of every
  Latin-script letter whose Unicode name is "LATIN ... LETTER [SMALL
  CAPITAL|SCRIPT|DOTLESS|LONG] <X> [WITH <modifier>]" — stroked, barred,
  hooked, small-capital, dotless letters (ǥ ħ ɨ ƀ ł ø ɢ ʀ ı ...) fold to
  <x>. Mathematical alphanumerics and fullwidth forms are folded by NFKC.
  No new dependency: the generated part is derived from Python's own
  `unicodedata` (Unicode 14 on Python 3.11);
- anything that isn't a letter, digit, `#` or `@` becomes a space.

`match_phrase()` then answers EXACT (whole-word match on canonical text),
LOOSE (the phrase appears only once separator-split letters are rejoined —
"g u a r a n t e e d", "g.u.a.r", "guaran teed" — or after leetspeak
folding "gu4r4nteed"), or NONE. `contains_phrase()` is True for EXACT.

Symbols and digits that stand in for letters (fix wave 4, NS): `canonical()`
turns every symbol into a space, so "return$", "G€t", "r¡ch", "ri¢h" and
"6et" used to look like different words. `near_miss()` answers, for one
phrase, whether the text could be that phrase once
(1) a SKELETON mapping (`SKELETON`: $->s, €->e, ¢->c, ¡/!/|/1->i/l, 6->g,
    2->z, +->t, (->c, ¥->y, £->l/e, 0->o, 3->e, 4->a, 5->s, 7->t, 8->b,
    9->g, @->a, ...) is applied inside words that contain letters, or
(2) every non-letter character inside a word is a WILDCARD (it may stand
    for one letter or be an inserted extra character), and an ASCII 'l'
    may stand for 'i' and vice versa; at most half of each phrase word's
    letters may be stood in for, so a price like "$20" never matches a
    word on its own.
Clip Review sends a near miss to a human. Two backstops don't depend on any
phrase at all: `mixed_symbol_words()` lists words that mix letters with
symbols or digits (anything but ordinary apostrophes, hyphens, periods and
ampersands between letters, letters-only #hashtags/@mentions, and numbers
with a unit suffix such as 2nd, 1990s, 9am, $40k, 1080p); and
`unfolded_latin_letters()` lists Latin-script letters outside Basic Latin,
the Latin-1 letters and the fold table — unknown lookalikes are routed to
a human instead of being enumerated. Both are never an automatic pass.

`obfuscation_signals()` says whether text shows evasion patterns at all:
any bidi control, Hangul/Mongolian filler or tag character ANYWHERE; any
other default-ignorable or format character touching a letter or digit;
lookalike letters among Latin text; letters of two scripts inside one
word; a run of 4+ single letters split by separators; a Latin letter outside
the recognised set (`unfolded_latin_letters()`). `non_latin_letters()`
lists letters outside the Latin script (Clip Review sends a clip of an
English-language campaign with any of them to a human). Any signal means
never an automatic pass.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from enum import Enum

_NON_WORD = re.compile(r"[^\w#@]+", re.UNICODE)

# Lookalike -> Latin. Lower-case only: applied after casefold. Deliberately
# the common, visually near-identical ones (Unicode confusables.txt subset).
CONFUSABLES: dict[str, str] = {
    # Cyrillic
    "а": "a", "б": "b", "в": "b", "г": "r", "д": "d", "е": "e", "ё": "e", "ж": "x", "з": "3", "и": "u",
    "й": "u", "к": "k", "л": "n", "м": "m", "н": "h", "о": "o", "п": "n", "р": "p", "с": "c", "т": "t",
    "у": "y", "ф": "f", "х": "x", "ц": "u", "ч": "4", "ш": "w", "щ": "w", "ъ": "b", "ы": "bi", "ь": "b",
    "э": "e", "ю": "io", "я": "r", "ѕ": "s", "і": "i", "ї": "i", "ј": "j", "ԁ": "d", "ӏ": "l", "ԛ": "q",
    "ԝ": "w", "һ": "h", "ҁ": "c", "ү": "y", "ұ": "y", "ґ": "r", "є": "e", "ѡ": "w", "ѵ": "v",
    # Greek
    "α": "a", "β": "b", "γ": "y", "δ": "d", "ε": "e", "ζ": "z", "η": "n", "θ": "o", "ι": "i", "κ": "k",
    "λ": "l", "μ": "u", "ν": "v", "ξ": "e", "ο": "o", "π": "n", "ρ": "p", "σ": "o", "ς": "c", "τ": "t",
    "υ": "u", "φ": "f", "χ": "x", "ψ": "w", "ω": "w", "ϲ": "c", "ϳ": "j", "ϸ": "p",
    # Armenian
    "ա": "w", "ց": "g", "հ": "h", "ո": "n", "ս": "u", "օ": "o", "ք": "p", "զ": "q",
    # Latin extended / IPA lookalikes that NFKC leaves alone
    "ɡ": "g", "ɑ": "a", "ı": "i", "ȷ": "j", "ɩ": "i", "ɪ": "i", "ʏ": "y", "ʀ": "r", "ɴ": "n", "ʜ": "h",
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m",
    "ᴏ": "o", "ᴘ": "p", "ꞯ": "q", "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ᴢ": "z", "ŀ": "l",
    "ſ": "s", "ƿ": "p", "ɢ": "g", "ʛ": "g", "ɋ": "q", "ʠ": "q", "ɾ": "r", "ɼ": "r", "ʋ": "v", "ʍ": "w",
    # insular / medieval Latin letters (fix wave 4; AEGIS round 3). Anything
    # else outside the recognised set goes to a human (unfolded_latin_letters).
    "ꭇ": "r", "ꞃ": "r", "ꝛ": "r", "ᵹ": "g", "ꞅ": "s", "ꜧ": "h", "ꞇ": "t", "ꝺ": "d", "ꝼ": "f",
    # more Cyrillic / Greek from confusables.txt
    "ԍ": "g", "ԃ": "d", "ԋ": "h", "ԏ": "t", "ӡ": "3", "ҽ": "e", "ҿ": "e", "ӏ": "l", "ᴫ": "n",
    "ϝ": "f", "ϻ": "m", "ϙ": "q", "ͱ": "h", "ͷ": "n",
    # Cherokee (after casefold Cherokee small letters U+AB70.. become U+13A0..)
    "Ꭰ": "d", "Ꭱ": "r", "Ꭲ": "t", "Ꭵ": "i", "Ꭹ": "y", "Ꭺ": "a", "Ꭻ": "j", "Ꭼ": "e", "Ꮃ": "w",
    "Ꮇ": "m", "Ꮋ": "h", "Ꮍ": "y", "Ꮐ": "g", "Ꮒ": "h", "Ꮓ": "z", "Ꮟ": "b", "Ꮢ": "r", "Ꮤ": "w",
    "Ꮥ": "s", "Ꮩ": "v", "Ꮪ": "s", "Ꮮ": "l", "Ꮯ": "c", "Ꮲ": "p", "Ꮶ": "k", "Ꮷ": "d", "Ᏻ": "g",
    "Ᏼ": "b", "Ꮻ": "o", "Ꮎ": "o",
}

# Default_Ignorable_Code_Point — the complete list from Unicode
# DerivedCoreProperties.txt (unchanged in substance since Unicode 6).
DEFAULT_IGNORABLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
    (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)
# Ignorables that render as a blank: folded to a space, not deleted.
_BLANK_IGNORABLES = frozenset(map(chr, (0x115F, 0x1160, 0x3164, 0xFFA0, 0x180E)))
# Always an evasion signal in customer-facing text, wherever they appear.
_BIDI_CONTROLS = frozenset(map(chr, (0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))))
_TAGS = (0xE0000, 0xE007F)


_IGNORABLE_SET = frozenset(chr(cp) for lo, hi in DEFAULT_IGNORABLE_RANGES for cp in range(lo, hi + 1))


def is_default_ignorable(ch: str) -> bool:
    return ch in _IGNORABLE_SET


def _invisible(ch: str) -> bool:
    return ch in _IGNORABLE_SET or unicodedata.category(ch) == "Cf"


_LATIN_NAME = re.compile(
    r"LATIN (?:SMALL CAPITAL |SMALL |CAPITAL )?LETTER (?:SMALL CAPITAL |SCRIPT |DOTLESS |LONG )?([A-Z])(?: WITH .+)?")


def _generated_latin_folds() -> dict[str, str]:
    out: dict[str, str] = {}
    for cp in range(0x80, 0x20000):
        ch = chr(cp)
        name = unicodedata.name(ch, "")
        if not name.startswith("LATIN "):
            continue
        m = _LATIN_NAME.fullmatch(name)
        if m:
            base = m.group(1).lower()
            out[ch] = base
            out.setdefault(ch.casefold(), base) if len(ch.casefold()) == 1 else None
    return out


_FOLDS: dict[str, str] = {**_generated_latin_folds(), **CONFUSABLES}
_CONFUSABLE_TABLE = str.maketrans(_FOLDS)


def fold_table() -> dict[str, str]:
    """Every lookalike -> Latin letter mapping `canonical()` applies (after casefold)."""
    return dict(_FOLDS)


# Lookalikes that are ordinary letters of real Latin-script languages
# (Turkish dotless i, long s, Catalan l·l) are folded for matching but are
# not, by themselves, an obfuscation signal.
_ORDINARY_LATIN = frozenset("ıſŀ")
_SIGNAL_CHARS = frozenset(CONFUSABLES) - _ORDINARY_LATIN

# Leetspeak folding — used ONLY for LOOSE matching (it would corrupt real
# numbers), inside tokens that also contain letters.
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g", "@": "a"})

# Symbols / digits that commonly stand in for letters (fix wave 4, NS) ->
# the letter(s) they may stand for. Used ONLY for near-miss matching inside
# words that also contain letters (so "$20 off" is never read as "s20").
SKELETON: dict[str, str] = {
    "$": "s", "§": "s", "5": "s", "€": "e", "£": "le", "3": "e", "&": "e", "¢": "c", "(": "c", "<": "c",
    "[": "c", "{": "c", "©": "c", "¡": "i", "!": "il", "|": "li", "1": "il", "ǀ": "l", "6": "gb", "9": "gq",
    "2": "z", "+": "t", "7": "tl", "†": "t", "¥": "y", "0": "o", "°": "o", "4": "a", "@": "a", "^": "a",
    "8": "b", "ß": "b", "®": "r", "#": "h", "%": "x", "×": "x", "µ": "u", "¿": "i",
}
# ASCII letters that pass for each other in most fonts (near-miss only).
_HOMOGLYPH_PAIRS = frozenset({("l", "i"), ("i", "l")})
# Word-internal punctuation that ordinary English uses between letters.
_ORDINARY_WORD = re.compile(r"[^\W\d_]+(?:['’.&-][^\W\d_]+)*")
_TAG_WORD = re.compile(r"[#@][^\W\d_]+(?:_[^\W\d_]+)*")
_NUMBER_WORD = re.compile(
    r"[$€£¥]?[0-9][0-9,.]*(?:st|nd|rd|th|s|k|m|b|x|p|am|pm|h|hr|hrs|min|mins|yr|yrs|mo)?", re.IGNORECASE)
_WORD_SPLIT = re.compile(r"[\s/–—…]+")
_LEAD_STRIP = "\"'“”‘’([{«‹¿¡*_~"
_TRAIL_STRIP = ".,!?;:\"'“”‘’)]}»›…*_~%"
_LATIN1_LETTERS = frozenset(ch for ch in map(chr, range(0xC0, 0x100)) if ch.isalpha())

SINGLE_LETTER_RUN = 4  # "g u a r ..." (4+) is a signal; "U.S.A." (3) is not


class PhraseMatch(str, Enum):
    EXACT = "exact"
    LOOSE = "loose"
    NONE = "none"


# Characters NFKC turns into a SPACE plus a combining mark (spacing
# diacritics: ¨ ¯ ´ ¸ ˘ ˙ ˚ ˛ ˜ ˝ ‾ ...). Left alone they would split a word
# in two ("Ris¯×" -> "Ris  ×") and hide it from the word-level checks; they
# become a plain symbol (U+00B7 MIDDLE DOT) inside the word instead.
_SPACING_DIACRITICS = {cp: "\u00b7" for cp in range(0x80, 0x30000)
                       if not chr(cp).isspace() and unicodedata.normalize("NFKC", chr(cp))[:1] == " "}


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", (text or "").translate(_SPACING_DIACRITICS))


# Non-letters that NFKC turns INTO letters (™ -> "TM", Ⓖ -> "G", ㎏ -> "kg"):
# canonical() folds them (so "Ⓖⓔⓣ ⓡⓘⓒⓗ" is still an exact never-say), but the
# word-level views (mixed words, near misses) see them as the symbols they
# are, so "ri™sk" is a word mixing letters with a symbol.
_SYMBOLS_TO_LETTERS = {cp: "\u00b7" for cp in range(0x80, 0x30000)
                       if not unicodedata.category(chr(cp)).startswith("L")
                       and any(c.isalpha() for c in unicodedata.normalize("NFKC", chr(cp)))}
# Letters in a compatibility FORM (superscript ª ᵃ, subscript, circled,
# squared, small forms, other <compat> letters): folded for matching, but a
# signal — nobody writes a caption in superscript letters by accident.
# Latin ligatures ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ (copy-paste from PDFs) are exempt.
_COMPAT_FORM_TAGS = ("<super>", "<sub>", "<circle>", "<square>", "<small>", "<vertical>", "<compat>")
_COMPAT_LETTER_FORMS = frozenset(
    chr(cp) for cp in range(0x80, 0x30000)
    if unicodedata.category(chr(cp)).startswith("L") and not 0xFB00 <= cp <= 0xFB06
    and unicodedata.decomposition(chr(cp)).startswith(_COMPAT_FORM_TAGS))


def _nfkc_words(text: str) -> str:
    """NFKC for the WORD-LEVEL views: symbols stay symbols."""
    return _nfkc((text or "").translate(_SYMBOLS_TO_LETTERS))


def _strip_marks(t: str) -> str:
    d = unicodedata.normalize("NFKD", t)
    return unicodedata.normalize("NFC", "".join(ch for ch in d if not unicodedata.combining(ch)))


def _drop_format(t: str) -> str:
    """Blank-rendering ignorables -> space; every other default-ignorable
    or format (Cf) character -> deleted."""
    return "".join(" " if ch in _BLANK_IGNORABLES else ch for ch in t
                   if ch in _BLANK_IGNORABLES or not _invisible(ch))


@functools.lru_cache(maxsize=64)
def canonical(text: str) -> str:
    t = _nfkc(text)
    t = _drop_format(t)
    t = _strip_marks(t).casefold()
    t = t.translate(_CONFUSABLE_TABLE)
    t = _NON_WORD.sub(" ", t)
    return " ".join(t.split())


# Backwards-compatible name: every caller that normalised text now gets the
# full canonical form.
normalize = canonical


def _tokens(text: str) -> list[str]:
    return canonical(text).split()


def _span_match(tokens: list[str], target: str) -> bool:
    """Does some contiguous run of tokens, concatenated, equal `target`
    exactly (starting and ending on token boundaries)?"""
    n = len(tokens)
    for i in range(n):
        acc = ""
        for j in range(i, n):
            acc += tokens[j]
            if acc == target:
                return True
            if len(acc) >= len(target) or not target.startswith(acc):
                break
    return False


def match_phrase(haystack: str, phrase: str) -> PhraseMatch:
    p = canonical(phrase)
    if not p:
        return PhraseMatch.NONE
    h = canonical(haystack)
    if f" {p} " in f" {h} ":
        return PhraseMatch.EXACT
    squashed = p.replace(" ", "")
    toks = h.split()
    if _span_match(toks, squashed):
        return PhraseMatch.LOOSE
    leet = [t.translate(_LEET) if any(c.isalpha() for c in t) else t for t in toks]
    if leet != toks and (f" {p} " in f" {' '.join(leet)} " or _span_match(leet, squashed)):
        return PhraseMatch.LOOSE
    return PhraseMatch.NONE


def contains_phrase(haystack: str, phrase: str) -> bool:
    """Whole-word(s) phrase match on canonical text (confusables folded)."""
    return match_phrase(haystack, phrase) is PhraseMatch.EXACT


def mentions_phrase(haystack: str, phrase: str) -> bool:
    """EXACT, LOOSE or a near miss — for refusals where a false positive only
    costs a rewrite."""
    return match_phrase(haystack, phrase) is not PhraseMatch.NONE or near_miss(haystack, phrase) is not None


@functools.lru_cache(maxsize=16)
def _folded(text: str) -> str:
    """canonical() minus the last step: symbols are KEPT (near-miss input)."""
    t = _nfkc_words(text)
    t = _drop_format(t)
    # ß is kept as one character (casefold would make it "ss"), so its
    # SKELETON reading as "b" ("doußle") is still available.
    t = _strip_marks(t).replace("\u1e9e", "\ue000").replace("\u00df", "\ue000")
    return t.casefold().translate(_CONFUSABLE_TABLE).replace("\ue000", "\u00df")


def _skeleton_views(folded: str) -> list[str]:
    """The folded text with SKELETON symbols read as letters inside words
    that contain a letter — once with each symbol's first reading, once
    with its second — then canonicalised (other symbols -> space)."""
    views = []
    for alt in (0, 1):
        table = {ord(k): v[min(alt, len(v) - 1)] for k, v in SKELETON.items()}
        words = [w.translate(table) if any(c.isalpha() for c in w) else w for w in folded.split()]
        views.append(" ".join(_NON_WORD.sub(" ", " ".join(words)).split()))
    return views


def _word_cost(w: str, p: str, cap: int) -> int | None:
    """Least number of stand-ins needed to read text word `w` as phrase word
    `p` (None if more than `cap`). Linear in len(w) * len(p), both bounded.
    - an ASCII letter must equal the phrase letter (or be its l/i homoglyph, 1);
    - a symbol or digit may be a SKELETON reading of the phrase letter (0),
      stand in for any letter (1), or be an inserted extra character (0);
    - any other letter (a lookalike the fold table doesn't know, another
      script) may stand in for a letter (1) or be inserted (1)."""
    n, m = len(w), len(p)
    if n < m or n > 3 * m + 8:
        return None
    if w.isascii() and w.isalpha():
        if n != m:
            return None
        cost = 0
        for a, b in zip(w, p):
            if a != b:
                if (a, b) not in _HOMOGLYPH_PAIRS:
                    return None
                cost += 1
        return cost if cost <= cap else None
    inf = cap + 1
    prev = [0] + [inf] * m
    for ch in w:
        ascii_letter = ch.isascii() and ch.isalpha()
        foreign_letter = ch.isalpha() and not ascii_letter
        readings = SKELETON.get(ch, "")
        cur = [inf] * (m + 1)
        for j in range(m + 1):
            c = prev[j]
            if c > cap:
                continue
            if not ascii_letter:  # inserted extra character
                ins = c + (1 if foreign_letter else 0)
                if ins < cur[j]:
                    cur[j] = ins
            if j < m:
                pc = p[j]
                if ch == pc:
                    step = 0
                elif ascii_letter:
                    step = 1 if (ch, pc) in _HOMOGLYPH_PAIRS else inf
                elif pc in readings:
                    step = 0
                else:
                    step = 1
                if c + step < cur[j + 1]:
                    cur[j + 1] = c + step
        prev = cur
    return prev[m] if prev[m] <= cap else None


def near_miss(haystack: str, phrase: str) -> str | None:
    """How `phrase` shows up in `haystack` only once symbols/digits are read
    as letters (see the module docstring), or None. A description, e.g.
    "symbols/digits standing in for letters", for the human reviewer."""
    p = canonical(phrase)
    if not p:
        return None
    folded = _folded(haystack)
    squashed = p.replace(" ", "")
    for v in _skeleton_views(folded):
        if f" {p} " in f" {v} " or _span_match(v.split(), squashed):
            return "symbols/digits standing in for letters"
    words = folded.split()
    pwords = p.split()
    memo: dict[tuple[str, str], bool] = {}

    def fits(w: str, pw: str, cap: int) -> bool:
        key = (w, pw)
        if key not in memo:
            memo[key] = _word_cost(w, pw, cap) is not None
        return memo[key]

    caps = [max(1, (len(pw) + 1) // 2) for pw in pwords]
    m = len(pwords)
    for k in range(len(words) - m + 1):
        if all(fits(words[k + t], pwords[t], caps[t]) for t in range(m)):
            return "symbols, digits or unknown letters in place of letters"
    if m > 1:
        cap = max(1, len(squashed) // 3)
        for w in words:
            if fits(w, squashed, cap):
                return "symbols, digits or unknown letters in place of letters (words run together)"
    return None


def mixed_symbol_words(text: str, limit: int = 5) -> list[str]:
    """Words that mix letters with symbols or digits (rule (b), fix wave 4):
    e.g. "return$", "G€t", "6et", "t@lks", "mp4". Ordinary text is left
    alone: surrounding punctuation is ignored; apostrophes, hyphens, periods
    and ampersands between letters ("don't", "co-op", "U.S.", "R&D");
    letters-only #hashtags / @mentions; numbers, prices and numbers with a
    unit suffix ("$20", "1,200", "2nd", "1990s", "9am", "$40k", "1080p")."""
    out: list[str] = []
    for raw in _WORD_SPLIT.split(_drop_format(_nfkc_words(text))):
        w = raw.lstrip(_LEAD_STRIP).rstrip(_TRAIL_STRIP)
        # emoji / pictographs next to a word are decoration, not a letter
        # (a never-say phrase written with one is still a near_miss()).
        while w and unicodedata.category(w[0]) == "So" and w[0] not in SKELETON:
            w = w[1:]
        while w and unicodedata.category(w[-1]) == "So" and w[-1] not in SKELETON:
            w = w[:-1]
        w = w.lstrip(_LEAD_STRIP).rstrip(_TRAIL_STRIP)
        if not w or not any(ch.isalpha() for ch in w) or all(ch.isalpha() for ch in w):
            continue
        if _ORDINARY_WORD.fullmatch(w) or _TAG_WORD.fullmatch(w) or _NUMBER_WORD.fullmatch(w):
            continue
        if raw not in out:
            out.append(raw[:40])
            if len(out) >= limit:
                break
    return out


def unfolded_latin_letters(text: str) -> list[str]:
    """Latin-script letters outside Basic Latin, the Latin-1 letters and the
    fold table, as 'U+XXXX' (rule (c), fix wave 4): lookalikes nobody has
    enumerated go to a human instead of passing as unknown letters."""
    found: dict[str, str] = {}
    for ch in _letters_view(text):
        if (ch.isalpha() and not ch.isascii() and ch not in _LATIN1_LETTERS and ch not in _FOLDS
                and ch.casefold() not in _FOLDS and _script(ch) == "LATIN"):
            found.setdefault(ch, f"U+{ord(ch):04X}")
    return list(found.values())


_SCRIPT_FAMILY = {"HIRAGANA": "JAPANESE", "KATAKANA": "JAPANESE", "CJK": "JAPANESE", "HALFWIDTH": "JAPANESE"}


@functools.lru_cache(maxsize=4096)
def _script(ch: str) -> str:
    """Approximate Unicode script of a letter, from its name (Python has no
    Script property): 'LATIN', 'CYRILLIC', 'GREEK', 'CHEROKEE', 'HANGUL', ...
    Letters with no script word in their name (e.g. 'MODIFIER LETTER ...')
    are 'other' — i.e. NOT Latin, which errs toward a human look."""
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return "other"
    first = name.split(" ", 1)[0]
    if first in ("MODIFIER", "FULLWIDTH"):
        return "LATIN" if " LATIN " in f" {name} " else "other"
    return _SCRIPT_FAMILY.get(first, first)


@functools.lru_cache(maxsize=16)
def _letters_view(text: str) -> str:
    """NFKC text with ignorables/format characters removed (fillers -> space)."""
    return _drop_format(_nfkc(text))


def non_latin_letters(text: str) -> list[str]:
    """Letters (after NFKC, so fullwidth / mathematical Latin count as Latin)
    whose script is not Latin, as 'U+XXXX (SCRIPT)' — empty if none."""
    found: dict[str, str] = {}
    for ch in _letters_view(text):
        if ch.isalpha() and _script(ch) != "LATIN":
            found.setdefault(ch, f"U+{ord(ch):04X} ({_script(ch).lower()})")
    return list(found.values())


def obfuscation_signals(text: str) -> list[str]:
    """Evasion patterns in `text` (empty list = none). Conservative on
    purpose: a false positive costs a human look, a false negative lets a
    never-say line through automatically."""
    signals: list[str] = []
    raw = _nfkc(text)
    bidi = sorted({f"U+{ord(c):04X}" for c in raw if c in _BIDI_CONTROLS})
    if bidi:
        signals.append("bidirectional control character(s) (can reverse or reorder what is displayed): " + ", ".join(bidi))
    blanks = sorted({f"U+{ord(c):04X}" for c in (text or "") + raw if c in _BLANK_IGNORABLES})
    if blanks:
        signals.append("invisible filler character(s) that render as a blank: " + ", ".join(blanks))
    tags = sorted({f"U+{ord(c):04X}" for c in raw if _TAGS[0] <= ord(c) <= _TAGS[1]})
    if tags:
        signals.append("invisible tag character(s): " + ", ".join(tags[:5]))
    # any other ignorable / format character touching a letter or digit (other ignorables skipped over)
    # (linear: the nearest visible neighbour on each side is carried along,
    # never re-scanned — a run of 50,000 invisibles is not quadratic)
    hidden: set[str] = set()
    inv = [_invisible(ch) for ch in raw]
    left_alnum = [False] * len(raw)
    last = False
    for i, ch in enumerate(raw):
        left_alnum[i] = last
        if not inv[i]:
            last = ch.isalnum()
    last = False
    for i in range(len(raw) - 1, -1, -1):
        ch = raw[i]
        if inv[i] and ch not in _BIDI_CONTROLS and ch not in _BLANK_IGNORABLES and (left_alnum[i] or last):
            hidden.add(f"U+{ord(ch):04X}")
        if not inv[i]:
            last = ch.isalnum()
    if hidden:
        signals.append("invisible character(s) inside or beside a word: " + ", ".join(sorted(hidden)[:5]))
    t = _letters_view(text).casefold()
    has_latin = any(ch.isalpha() and _script(ch) == "LATIN" for ch in t)
    lookalikes = sorted({ch for ch in t if ch in _SIGNAL_CHARS})
    if lookalikes and has_latin:
        signals.append("mixed-script text: lookalike letter(s) "
                       + ", ".join(f"U+{ord(c):04X}" for c in lookalikes[:5]) + " among Latin letters")
    mixed = []
    for word in _NON_WORD.split(t):
        scripts = {_script(ch) for ch in word if ch.isalpha()}
        if len(scripts) > 1:
            mixed.append(f"{'+'.join(sorted(s.lower() for s in scripts))}")
    if mixed:
        signals.append("letters of more than one script inside one word (" + ", ".join(sorted(set(mixed))[:3]) + ")")
    forms = sorted({f"U+{ord(c):04X}" for c in (text or "") if c in _COMPAT_LETTER_FORMS})
    if forms:
        signals.append("letter(s) written in a compatibility form (superscript, subscript, circled, squared): "
                       + ", ".join(forms[:5]))
    unknown = unfolded_latin_letters(text)
    if unknown:
        signals.append("Latin letter(s) outside the recognised set (possible lookalike the fold table does not "
                       "know): " + ", ".join(unknown[:5]))
    run = 0
    for tok in canonical(text).split():
        run = run + 1 if len(tok) == 1 and tok.isalpha() else 0
        if run >= SINGLE_LETTER_RUN:
            signals.append(f"{SINGLE_LETTER_RUN}+ single letters split by separators (e.g. 'g u a r')")
            break
    return signals


_LOOKALIKES: dict[str, list[str]] | None = None


def lookalikes_of(letter: str) -> list[str]:
    """Every character `canonical()` folds to the ASCII `letter` (test /
    fuzz helper): fold-table entries plus NFKC sources (mathematical
    alphanumerics, fullwidth, ...)."""
    global _LOOKALIKES
    if _LOOKALIKES is None:
        table: dict[str, list[str]] = {}
        for cp in range(0x80, 0x20000):
            ch = chr(cp)
            if unicodedata.category(ch) in ("Cs", "Co", "Cn") or is_default_ignorable(ch):
                continue
            c = canonical(ch)
            if len(c) == 1 and c.isascii() and c.isalpha():
                table.setdefault(c, []).append(ch)
        _LOOKALIKES = table
    return list(_LOOKALIKES.get(letter.lower(), []))


def word_count(text: str) -> int:
    return len(canonical(text).split())
