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

ASCII lookalike SPELLINGS (fix wave 5, NEW-1): "retums" (rn for m),
"rnoney", "guaranteecl" (cl for d), "vv" for w, "kure". Instead of another
enumeration, `visual_near_miss()` is a similarity gate: the phrase is
compared with the text on visual skeletons (UTS #39's m -> rn plus
typographic pairs) under a bounded Damerau-Levenshtein distance; see the
block comment above VISUAL_SKELETON. Fix wave 6 (N1) made it a LETTER
STREAM gate: the text's tokens are concatenated (a split is irrelevant:
"ge t rl ch", "make r n oney"), the skeleton is applied to the stream,
runs of a repeated letter are collapsed ("geeet" -> "get"), and the
budget is 1/2/3 edits for 4-6/7-10/11+ letters; windows start at a word
start and end at a word end. `near_miss()` includes it, and the adjacency
policy (`phrase_words_in_order()`: the phrase's words in order within 2
other words, "make big money"), so every caller of `near_miss()` /
`mentions_phrase()` gets both; `visual_lookalike_exact()` names a window
that READS AS the phrase (Clip Review rejects it). N3: an entry of <= 4
letters gets no edit budget unless the rulebook opts it in (`fuzzy`).

Vowel-drop and phonetic respellings (fix wave 7, AEGIS round 6 NEW-2 /
NEW-3): "mk mny", "grnteed rtrns", "phree money", "get ritch" are 2-4
letter edits away and passed the visual gate. Two more signals, each a
human's call on its own, both reached through `near_miss()`:
`skeleton_near_miss()` compares CONSONANT SKELETONS (vowels dropped unless
word-initial, runs collapsed) on the token stream under a 0/1/2 budget,
and `phonetic_near_miss()` compares a simplified Metaphone-style
`phonetic_key()` per word (in order within the adjacency policy) or over a
run of tokens joined; see the block comment above VOWELS for the guards
(function words, vowel-drop evidence) that keep ordinary text out. The
per-word share rule of the visual gate is a 60% letter share
(`letter_share`, WORD_SHARE), no longer a hard per-word cap that could
reduce a match the budget allowed ("get rchi", "make munny").

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
from collections import Counter, OrderedDict
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


def mentions_phrase(haystack: str, phrase: str, fuzzy: bool = False) -> bool:
    """EXACT, LOOSE or a near miss — for refusals where a false positive only
    costs a rewrite."""
    return match_phrase(haystack, phrase) is not PhraseMatch.NONE or near_miss(haystack, phrase, fuzzy) is not None


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


def near_miss(haystack: str, phrase: str, fuzzy: bool = False) -> str | None:
    """How `phrase` shows up in `haystack` only once symbols/digits are read
    as letters (see the module docstring), as a readable respelling
    (`visual_near_miss`), or as its words in order with a filler between
    them (`phrase_words_in_order`) — or None. A description, e.g.
    "symbols/digits standing in for letters", for the human reviewer.
    `fuzzy`: the rulebook author opted a short entry into the similarity
    gate (N3)."""
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
    hit = visual_near_miss(haystack, phrase, fuzzy)
    if hit is not None:
        dist, _, window = hit
        return (f"lookalike letters or a small misspelling ({window[:60]!r} is {dist} edit(s) from it once "
                "rn/m, cl/d, vv/w, nn/m, ri/n, ii/u and l/i are read alike, stretched letters collapsed and "
                "spaces ignored)")
    span = phrase_words_in_order(haystack, phrase)
    if span is not None:
        return f"its words in order with other words between them ({span[:60]!r})"
    skel = skeleton_near_miss(haystack, phrase, fuzzy)
    if skel is not None:
        d, window = skel
        return (f"its consonants with the vowels dropped or changed ({window[:60]!r} is {d} edit(s) from it "
                "once vowels are ignored)")
    sound = phonetic_near_miss(haystack, phrase, fuzzy)
    if sound is not None:
        return f"a phonetic respelling ({sound[:60]!r} sounds like it)"
    return None


# --- visual-lookalike SPELLINGS in plain ASCII (fix wave 5, NEW-1) ---------------------
#
# Earlier waves enumerated lookalikes one class at a time (Unicode letters,
# symbols, digits, the l/i pair); plain-ASCII respellings still passed:
# "Guaranteed retums" (rn for m), "make rnoney", "guaranteecl returns" (cl
# for d), "vv" for w, "miracle kure". Instead of a sixth enumeration, a
# SIMILARITY gate: the caption's letter stream is compared with every
# never-say phrase on a visual SKELETON with a bounded edit distance (fix
# wave 6 moved it from windows of words to the stream; see the block
# comment above _tracked_contract).
#
# The multi-character ASCII confusables. Unicode UTS #39 confusables.txt
# (Unicode 18.0.0, 2026-08-06, read for this fix) has exactly ONE entry
# whose source and prototype are both plain ASCII letters and whose
# prototype is more than one letter: m -> rn (006D ; 0072 006E). Its only
# other all-ASCII entries are single characters: I -> l, 1 -> l, 0 -> O
# (digits are already handled by SKELETON / _word_cost above). Everything
# else below is a typographic (kerning) lookalike that confusables.txt does
# NOT list, chosen by hand:
#   near-identical in common sans-serif faces — cl -> d, vv -> w
#     (VISUAL_SKELETON, together with UTS #39's rn -> m);
#   merely similar — nn -> m, uu -> w, ci -> a, ri -> n, ii -> u
#     (VISUAL_EXTENDED / VISUAL_EXTENDED_2).
# Only the first group decides REJECT (below); the second only ever sends
# the clip to a human.
#
# Skeleton VIEWS; the phrase and the text are always mapped the same way,
# and the smallest distance over the views counts.
# (A) STANDARD — rn -> m (UTS #39), cl -> d, vv -> w, applied as
#     contractions so an inserted m costs one edit, not two.
# (0) RAW — I -> l only, so a transposition across "rn" ("retunrs") stays
#     one edit.
# (B) EXTENDED — the "merely similar" pairs as well, contracted to the
#     letter they imitate, taken in two orders (pairs overlap: "ciire" is
#     ci+i or c+ii).
# Fix wave 6 (N1) dropped the split-pair views of fix wave 5 (A-, B-, A+:
# the views without the pairs the phrase itself contains, and the
# expansion direction): the gate now runs on the letter stream with
# budgets of 2-3 edits for phrases of 7+ letters, which absorb a pair
# split by an edit ("returens"); VISUAL_EXPANDED is kept for reference
# only. Any other misspelling ("kure", "retrns") is caught by the edit
# budget (visual_budget) in every view.
# Every view first maps i -> l (UTS #39: I -> l; text is casefolded), so a
# dotless/undotted i costs nothing to DETECT; it still never REJECTS by
# itself ("get rlch" is a human's call, as it was before this wave —
# `_reads_as_phrase` ignores the i/l fold).
VISUAL_SKELETON: tuple[tuple[str, str], ...] = (("rn", "m"), ("cl", "d"), ("vv", "w"))
VISUAL_EXTENDED: tuple[tuple[str, str], ...] = (
    ("rn", "m"), ("nn", "m"), ("vv", "w"), ("uu", "w"), ("cl", "d"), ("rl", "n"), ("ll", "u"))
# View B works after i -> l, so its pairs are written with l for i
# (ri -> "rl", ii -> "ll", ci -> "cl"); "cl" imitates both d and a, and
# pairs overlap ("ciire" = ci+i or c+ii), so B is taken in two orders.
VISUAL_EXTENDED_2: tuple[tuple[str, str], ...] = (
    ("ll", "u"), ("rl", "n"), ("cl", "a"), ("uu", "w"), ("vv", "w"), ("nn", "m"), ("rn", "m"))
# The expansion direction (m -> rn, d -> cl, w -> vv). Not a view since fix
# wave 6 (kept for reference: `lookalikes_of`-style tooling and tests).
VISUAL_EXPANDED: tuple[tuple[str, str], ...] = (("m", "rn"), ("d", "cl"), ("w", "vv"))


def visual_view(word: str, rules: tuple[tuple[str, str], ...]) -> str:
    """`word` (casefolded) with i -> l, then each (pair -> letter) rule in order."""
    word = word.replace("i", "l")
    for src, proto in rules:
        word = word.replace(src, proto)
    return word


def visual_skeleton(word: str) -> str:
    """rn -> m (UTS #39), cl -> d, vv -> w — the near-identical pairs only,
    WITHOUT the i/l fold. Two words with the same skeleton read as each
    other ("rnoney" / "money", "guaranteecl" / "guaranteed"); contraction
    instead of expansion, so an inserted m is one edit, not two."""
    for src, proto in VISUAL_SKELETON:
        word = word.replace(src, proto)
    return word


def _reads_as_phrase(tokens: tuple[str, ...], pwords: list[str]) -> bool:
    """The text words `tokens` are `pwords`, word for word, once the
    near-identical pairs are read alike (visual_skeleton). Decides REJECT;
    the i/l fold and the merely-similar pairs never do."""
    return len(tokens) == len(pwords) and all(visual_skeleton(t) == visual_skeleton(p) for t, p in zip(tokens, pwords))


def _views_for(phrase_l: str) -> list[tuple[tuple[str, str], ...]]:
    """The skeleton views a phrase is compared under (fix wave 6, N1): raw
    (i -> l only), the near-identical pairs, and the merely-similar pairs in
    both orders. The split-pair views of fix wave 5 (A-, B-, A+) are no
    longer needed: the stream ignores word boundaries and the budgets
    absorb a pair split by an edit."""
    return [(), VISUAL_SKELETON, VISUAL_EXTENDED, VISUAL_EXTENDED_2]


SHORT_ENTRY_LETTERS = 4  # N3: never-say entries this short are exact-only unless `fuzzy: true`


def visual_budget(letters: int, fuzzy: bool = False, raw_letters: int | None = None) -> int:
    """Edit budget (Damerau-Levenshtein, optimal string alignment) for a
    phrase whose run-collapsed skeleton has `letters` letters (fix wave 6,
    N1): 1 for 4-6, 2 for 7-10, 3 above; 0 for 3 or fewer (skeleton-
    identical only: one edit from "win" or "fee" is half the ordinary
    words of English). N3: an entry of at most SHORT_ENTRY_LETTERS letters
    as written (`raw_letters`, default `letters`) gets 0 unless the
    rulebook author opted it in (`fuzzy`): one edit from "cure" is
    sure/pure/core/care/cute, 11% of ordinary captions."""
    raw = letters if raw_letters is None else raw_letters
    if letters <= 3 or (raw <= SHORT_ENTRY_LETTERS and not fuzzy):
        return 0
    if letters <= 6:
        return 1
    return 2 if letters <= 10 else 3


def collapse_runs(s: str, keep: int = 1) -> str:
    """Every run of one repeated character cut to `keep` characters:
    collapse_runs("geeet") == "get"; collapse_runs("geeet", 2) == "geet"."""
    out: list[str] = []
    run = 0
    prev = ""
    for ch in s:
        run = run + 1 if ch == prev else 1
        prev = ch
        if run <= keep:
            out.append(ch)
    return "".join(out)


def _osa_within(a: str, b: str, k: int) -> int | None:
    """Optimal-string-alignment distance between a and b if <= k, else None.
    Banded: O(len(a) * (2k + 1))."""
    la, lb = len(a), len(b)
    if abs(la - lb) > k:
        return None
    if k == 0:
        return 0 if a == b else None
    inf = k + 1
    prev2: list[int] = []
    prev = [j if j <= k else inf for j in range(lb + 1)]
    for i in range(1, la + 1):
        cur = [inf] * (lb + 1)
        if i <= k:
            cur[0] = i
        lo, hi = max(1, i - k), min(lb, i + k)
        best = cur[0]
        ai = a[i - 1]
        for j in range(lo, hi + 1):
            bj = b[j - 1]
            v = prev[j - 1] + (ai != bj)
            if prev[j] + 1 < v:
                v = prev[j] + 1
            if cur[j - 1] + 1 < v:
                v = cur[j - 1] + 1
            if i > 1 and j > 1 and ai == b[j - 2] and a[i - 2] == bj and prev2[j - 2] + 1 < v:
                v = prev2[j - 2] + 1
            cur[j] = v if v < inf else inf
            if cur[j] < best:
                best = cur[j]
        if best > k:
            return None
        prev2, prev = prev, cur
    return prev[lb] if prev[lb] <= k else None


@functools.lru_cache(maxsize=16)
def _canonical_tokens(text: str) -> tuple[str, ...]:
    return tuple(canonical(text).split())


# --- the letter stream (fix wave 6, N1) -------------------------------------------------
#
# AEGIS round 5 still passed readable respellings: stretched letters
# ("geeet riiich"), doubled letters in short phrases ("gget ricch"), one
# lookalike plus a split into more tokens than the phrase has words ("ge t
# rl ch", "pas si ve inc orne"), a pair split by a space ("make r n oney").
# Root causes: a 1-edit budget for phrases up to 8 letters and windows of
# at most one token more than the phrase. Now the phrase is compared with
# the LETTER STREAM of the text — every token concatenated, so how the
# evader splits it is irrelevant — in this order:
#   (1) the view (i -> l, then the pair rules) is applied to the STREAM, so
#       a pair split across tokens ("r n") still contracts;
#   (2) runs of one repeated letter are collapsed to a single letter, on
#       the stream and on the phrase alike ("geeet" -> "get"), but never
#       across a word boundary ("sell lemons" keeps "lemons");
#   (3) budget `visual_budget()` on the collapsed skeleton: any window of
#       the stream that STARTS at a token start and ENDS at a token end and
#       is within budget -> human_review; a window that READS AS the phrase
#       (`_reads_as_phrase`) -> reject.
# Word-boundary anchoring is what keeps "target rich" / "budget rich" out:
# the phrase's letters straddling a real word boundary mid-word are not a
# reading of it.
#
# Cost: a bit-parallel scan (Myers 1999 with Hyyrö's 2003 transposition
# term, Damerau/OSA distance) — one pass over the stream per view for a
# whole PACK of phrases at once, each phrase in its own bit field; a Python
# loop of ~20 big-integer operations per character, so linear in the text
# whatever its content (the word-window gate's cost depended on the
# text's alphabet). Restricting the start of a match to token starts is
# done through the top row of the matrix (0 at a token start, 1
# elsewhere — a lower bound, so the scan reports a superset), and every
# reported end is confirmed by the exact banded distance
# (`_osa_within`) from the aligned starts within budget. A hit is only
# ever checked at a token end.


def _tracked_contract(s: str, st: bytearray, en: bytearray, tok: list[int], src: str, proto: str):
    """`s.replace(src, proto)` (len(src) == 2, len(proto) == 1) carrying the
    flags: st[p] / en[p] (len(s) + 1) say a window may start / end before
    position p; tok[q] is the token index of character q. A start flag
    inside the pair moves to the merged character, an end flag inside it
    to just after."""
    if src not in s:
        return s, st, en, tok
    out: list[str] = []
    nst = bytearray()
    nen = bytearray()
    ntok: list[int] = []
    i, n = 0, len(s)
    pend = 0
    while True:
        k = s.find(src, i)
        if k < 0:
            break
        out.append(s[i:k])
        nst += st[i:k]
        nen += en[i:k]
        if k > i:
            nen[len(nen) - (k - i)] |= pend
            pend = 0
        ntok += tok[i:k]
        out.append(proto)
        nst.append(st[k] | st[k + 1])
        nen.append(en[k] | pend)
        pend = en[k + 1]
        ntok.append(tok[k])
        i = k + 2
    out.append(s[i:])
    nst += st[i:n]
    nen += en[i:n]
    if n > i:
        nen[len(nen) - (n - i)] |= pend
        pend = 0
    ntok += tok[i:]
    nst.append(0)
    nen.append(en[n] | pend)
    return "".join(out), nst, nen, ntok


def _tracked_collapse(s: str, st: bytearray, en: bytearray, tok: list[int]):
    """Runs of one repeated character -> one character, carrying the flags
    like _tracked_contract (a start inside the run moves to the survivor,
    an end inside it to just after)."""
    out: list[str] = []
    nst = bytearray()
    nen = bytearray()
    ntok: list[int] = []
    prev = ""
    pend = 0
    for i, ch in enumerate(s):
        if ch == prev:
            nst[-1] |= st[i]
            pend |= en[i]
            continue
        out.append(ch)
        nst.append(st[i])
        nen.append(en[i] | pend)
        pend = 0
        ntok.append(tok[i])
        prev = ch
    nst.append(0)
    nen.append(en[len(s)] | pend)
    return "".join(out), nst, nen, ntok


@functools.lru_cache(maxsize=64)
def _stream(text: str, rules: tuple[tuple[str, str], ...], collapse: bool = True) -> tuple[str, bytes, bytes, tuple[int, ...]]:
    """The letter stream of `text` in one view: (stream, start flags, end
    flags, token index per character). start[p] == 1 if a window may start
    at position p, end[p] == 1 if one may end there (a token started /
    ended there). Built once per (text, view), shared by every phrase:
    i -> l, then each pair rule over the whole stream in order (the same
    sequence `visual_view` applies to a word), then the run collapse. A
    boundary INSIDE a contracted pair or a collapsed run is kept: its start
    flag moves to the surviving character, its end flag to just after it —
    "sell lemons" -> "selemons" still has a window "lemons" and a window
    "sel"; "pas si ve" -> "pasive" like the phrase."""
    toks = _canonical_tokens(text)
    s = "".join(toks).replace("i", "l")
    n = len(s)
    st = bytearray(n + 1)
    en = bytearray(n + 1)
    tok: list[int] = []
    pos = 0
    for ti, t in enumerate(toks):
        st[pos] = 1
        pos += len(t)
        en[pos] = 1
        tok += [ti] * len(t)
    st[n] = 0
    for src, proto in rules:
        s, st, en, tok = _tracked_contract(s, st, en, tok, src, proto)
    if collapse:
        s, st, en, tok = _tracked_collapse(s, st, en, tok)
    return s, bytes(st), bytes(en), tuple(tok)


class _Pack:
    """Several patterns packed into one big integer, one W-bit field each
    (pattern bits at the top of the field, the lowest bit a carry guard),
    scanned together (see the block comment above)."""

    __slots__ = ("W", "n", "PAT", "FIRST", "TOPS", "LSBS", "CK", "scores0", "pm")

    def __init__(self, pats: list[tuple[str, int]]):
        # A field holds the pattern's bits (top m bits), a carry guard (the
        # lowest bit), and doubles as a W-bit distance counter that the
        # "<= k" test adds 2^(W-1) - 1 - k to: so 2^(W-1) > max(m, k).
        m_max = max(len(p) for p, _ in pats)
        k_max = max(k for _, k in pats)
        W = max(m_max + 1, max(m_max, k_max).bit_length() + 1)
        self.W, self.n = W, len(pats)
        PAT = FIRST = TOPS = LSBS = CK = scores0 = 0
        pm: dict[str, int] = {}
        for i, (p, k) in enumerate(pats):
            base = i * W
            m = len(p)
            first = base + W - m
            PAT |= ((1 << m) - 1) << first
            FIRST |= 1 << first
            TOPS |= 1 << (base + W - 1)
            LSBS |= 1 << base
            CK |= ((1 << (W - 1)) - 1 - k) << base
            scores0 |= m << base
            for q, ch in enumerate(p):
                pm[ch] = pm.get(ch, 0) | (1 << (first + q))
        self.PAT, self.FIRST, self.TOPS, self.LSBS, self.CK, self.scores0, self.pm = PAT, FIRST, TOPS, LSBS, CK, scores0, pm

    def scan(self, S: str, ST: bytes, EN: bytes):
        """Yields (end position j, pattern index) for every token end j
        (EN[j]) at which some pattern is within its budget of a window
        ending there (the relaxed-start superset — the top row is 0 at a
        token start ST[j], 1 elsewhere; confirm with _osa_within)."""
        W, PAT, FIRST, TOPS, LSBS, CK = self.W, self.PAT, self.FIRST, self.TOPS, self.LSBS, self.CK
        get = self.pm.get
        sh = W - 1
        Pv, Mv, scores, D0p, Eqp = PAT, 0, self.scores0, 0, 0
        top = 0 if ST[0] else 1
        for j, c in enumerate(S, 1):
            Eq = get(c, 0)
            ntop = 0 if ST[j] else 1
            # The top row steps 1 -> 0 at a token start: a -1 horizontal
            # delta at row 0, which Myers' recurrence (row-0 deltas of 0 or
            # +1 only) has no input for. It forces D[1][j] = D[0][j-1] —
            # exactly what a match at row 1 does — so it enters as a
            # pseudo-match in the first cell of every field (Eqm); the real
            # Eq alone feeds the transposition term.
            Eqm = Eq | FIRST if ntop < top else Eq
            D0 = (((((Eqm & Pv) + Pv) & PAT) ^ Pv) | Eqm | Mv | (((~D0p & Eq) << 1) & Eqp)) & PAT
            Ph = (Mv | ~(D0 | Pv)) & PAT
            Mh = Pv & D0
            scores += (Ph >> sh) & LSBS
            scores -= (Mh >> sh) & LSBS
            Ph = (Ph << 1) & PAT
            Mh = (Mh << 1) & PAT
            if ntop > top:
                Ph |= FIRST
            elif ntop < top:
                Mh |= FIRST
            top = ntop
            Pv = (Mh | ~(D0 | Ph)) & PAT  # Hyyro's form: the vertical deltas follow D0 (which holds the transposition)
            Mv = Ph & D0
            D0p, Eqp = D0, Eq
            if EN[j]:
                h = ~(scores + CK) & TOPS
                while h:
                    low = h & -h
                    h ^= low
                    yield j, (low.bit_length() - W) // W


PACK_BITS = 1024  # patterns are packed into integers of about this many bits


def _packs(pats: list[tuple[str, int]]) -> list[tuple[list[int], _Pack]]:
    """Group pattern indexes into packs of about PACK_BITS bits (a pack is
    as wide as its longest pattern per field)."""
    order = sorted(range(len(pats)), key=lambda i: len(pats[i][0]))
    out: list[tuple[list[int], _Pack]] = []
    group: list[int] = []
    for i in order:
        w = len(pats[i][0]) + 1
        if group and (len(group) + 1) * w > PACK_BITS:
            out.append((group, _Pack([pats[g] for g in group])))
            group = []
        group.append(i)
    if group:
        out.append((group, _Pack([pats[g] for g in group])))
    return out


def _word_shares_ok(window: str, words: list[tuple[str, str]], total: int) -> bool:
    """Can `window` be cut into len(words) consecutive pieces so that the
    pieces' edits from the words sum to at most `total` and every piece
    that is not its word keeps at least WORD_SHARE of that word's letters
    (`letter_share`)? `words`: (the word in the pattern's form, the word
    as written in the same view — the share is measured against the
    letters as written, so "for" keeps 2 of "free"'s 4, not 2 of the
    collapsed "fre"'s 3). Segmented banded DP over the window (short: the
    phrase's length plus its budget).

    Fix wave 6 gave each word a hard cap of its own tier (1 edit for a
    word of up to 6 letters), to stop a DIFFERENT ordinary word from
    absorbing all the edits ("more" for "money"). AEGIS round 6 (NEW-3)
    showed the cap REDUCED the match: two edits in one short word ("get
    rchi", "make munny") passed although the phrase's budget allowed
    them. A share rule never reduces a match the budget allows for a
    piece that is still mostly the word; it only refuses a piece that has
    lost the word's letters."""
    n = len(window)
    inf = total + 1
    best = [0] + [inf] * n  # best[e]: least cost to match words so far with window[:e]
    for w, written in words:
        m = len(w)
        nxt = [inf] * (n + 1)
        for a in range(n + 1):
            if best[a] >= inf:
                continue
            for b in range(max(a, a + m - total), min(n, a + m + total) + 1):
                d = _osa_within(window[a:b], w, total - best[a])
                if d is None or best[a] + d >= nxt[b]:
                    continue
                if d and letter_share(window[a:b], written) < WORD_SHARE:
                    continue
                nxt[b] = best[a] + d
        best = nxt
    return best[n] <= total


def _reads_as_phrase(window_text: str, squashed: str) -> bool:
    """`window_text` (the original tokens the window spans) READS AS the
    phrase: identical once rn/m, cl/d, vv/w are read alike
    (visual_skeleton, no i/l fold) and stretched runs of 3+ letters are
    cut to one ("geeet" is not a spelling of anything). A doubled letter
    is left alone: met/meet, of/off, to/too are different words, so a
    double is an edit for the budget, never a reading. Decides REJECT."""
    return _collapse_stretched(visual_skeleton(window_text.replace(" ", ""))) == _collapse_stretched(visual_skeleton(squashed))


def _collapse_stretched(s: str) -> str:
    """Runs of 3+ of one character -> one character; doubles stay."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        j = i
        while j < n and s[j] == s[i]:
            j += 1
        out.append(s[i] if j - i >= 3 else s[i:j])
        i = j
    return "".join(out)


# Results per haystack, filled by any batch and read by every single-phrase
# call, so Clip Review's one scan for all its phrases is not repeated per
# rule. Bounded by DISTINCT haystacks (a key is a reference to the text).
_VNM_MEMO: "OrderedDict[str, dict[tuple[str, bool], tuple[int, bool, str] | None]]" = OrderedDict()
_VNM_MEMO_TEXTS = 16


def visual_near_misses(haystack: str, phrases: tuple[tuple[str, bool], ...]) -> dict[str, tuple[int, bool, str] | None]:
    """`visual_near_miss()` for several (phrase, fuzzy) at once — one scan
    of the stream per view for the whole set. Returns {phrase: result}."""
    memo = _VNM_MEMO.get(haystack)
    if memo is None:
        memo = _VNM_MEMO[haystack] = {}
        while len(_VNM_MEMO) > _VNM_MEMO_TEXTS:
            _VNM_MEMO.popitem(last=False)
    _VNM_MEMO.move_to_end(haystack)
    todo = [(p, bool(f)) for p, f in dict.fromkeys((p, bool(f)) for p, f in phrases) if (p, bool(f)) not in memo]
    if todo:
        for (p, f), res in _scan_phrases(haystack, tuple(todo)).items():
            memo[(p, f)] = res
    return {p: memo[(p, bool(f))] for p, f in phrases}


def _scan_phrases(haystack: str, phrases: tuple[tuple[str, bool], ...]) -> dict[tuple[str, bool], tuple[int, bool, str] | None]:
    toks = _canonical_tokens(haystack)
    specs: list[tuple[tuple[str, bool], str, int, bool]] = []  # (key, squashed, raw letters, fuzzy)
    result: dict[tuple[str, bool], tuple[int, bool, str] | None] = {}
    for phrase, fuzzy in phrases:
        p = canonical(phrase)
        result[(phrase, fuzzy)] = None
        if p:
            sq = p.replace(" ", "")
            specs.append(((phrase, fuzzy), sq, len(sq), fuzzy))
    if not specs or not toks:
        return result
    for rules in _views_for(""):
        S, ST, EN, tok = _stream(haystack, rules)
        # Two skeletons per phrase against the collapsed text: the phrase
        # collapsed ("geeet" ~ "get" at no cost) and as written (an
        # insertion that splits the phrase's own double, "frete" for
        # "free", is one edit against "free" but two against "fre"). The
        # budget is the collapsed skeleton's tier either way.
        pats: list[tuple[str, int]] = []
        owner: list[int] = []
        wskels: list[list[tuple[str, str]]] = []
        for i, (key, sq, raw, fz) in enumerate(specs):
            pskel = _stream(key[0], rules)[0]  # the phrase through the same transform as the text
            k = visual_budget(len(pskel), fz, raw)
            pwords = canonical(key[0]).split()
            for collapse in (True, False):
                variant = _stream(key[0], rules, collapse)[0]
                if not collapse and variant == pskel:
                    continue
                pats.append((variant, k))
                owner.append(i)
                wskels.append([(_stream(w, rules, collapse)[0], _stream(w, rules, False)[0]) for w in pwords])
        dist_memo: dict[tuple[str, str], int | None] = {}  # (window, pattern) -> distance; repeated text repeats windows
        share_memo: dict[tuple[str, int], bool] = {}
        for group, pack in _packs(pats):
            for j, gi in pack.scan(S, ST, EN):
                pidx = group[gi]
                idx = owner[pidx]
                key, sq, _, _ = specs[idx]
                best = result[key]
                if best is not None and best[1]:
                    continue
                pskel, k = pats[pidx]
                m = len(pskel)
                exact_only = best is not None and best[0] == 0  # only a READING could still improve on it
                for s in range(max(0, j - m - k), j - m + k + 1):
                    if s >= j or not ST[s]:
                        continue
                    w = S[s:j]
                    if exact_only:
                        if w != pskel:
                            continue
                        d = 0
                    else:
                        mk = (w, pskel)
                        d = dist_memo.get(mk, -1)
                        if d == -1:
                            d = dist_memo[mk] = _osa_within(w, pskel, k)
                        if d is None:
                            continue
                        if d >= 2 and len(wskels[pidx]) > 1:
                            sk = (w, pidx)
                            okay = share_memo.get(sk)
                            if okay is None:
                                okay = share_memo[sk] = _word_shares_ok(w, wskels[pidx], k)
                            if not okay:
                                continue  # the edits are one whole short word swapped for another
                    window = " ".join(toks[tok[s]:tok[j - 1] + 1])
                    reads = d == 0 and _reads_as_phrase(window, sq)
                    cand = (d, reads, window)
                    if best is None or (cand[0], not cand[1]) < (best[0], not best[1]):
                        best = cand
                        exact_only = d == 0
                        if reads:
                            break
                result[key] = best
    return result


def visual_near_miss(haystack: str, phrase: str, fuzzy: bool = False) -> tuple[int, bool, str] | None:
    """The closest word-aligned window of `haystack`'s letter stream to
    `phrase` over the skeleton views, if within `visual_budget()`:
    (distance, reads_as_phrase, window text). reads_as_phrase is True only
    for a window IDENTICAL to the phrase once rn/m, cl/d, vv/w are read
    alike and stretched letters are collapsed (`_reads_as_phrase`; the i/l
    fold, the merely-similar pairs and a doubled letter don't count). None
    if no window is within budget. `fuzzy`: the rulebook author opted a
    short entry in (N3)."""
    return visual_near_misses(haystack, ((phrase, bool(fuzzy)),))[phrase]


def visual_lookalike_exact(haystack: str, phrase: str, fuzzy: bool = False) -> str | None:
    """The words of `haystack` that READ AS `phrase` — the letters of the
    phrase, on word boundaries, however they are split, once rn/m (UTS
    #39), cl/d, vv/w are read alike and stretched letters are collapsed:
    "make rnoney", "make r n oney", "pas si ve inc orne", "geeet riiich" —
    or None. ("get rlch" is NOT one: l for i is a human's call.)"""
    hit = visual_near_miss(haystack, phrase, fuzzy)
    if hit is not None and hit[1]:
        return hit[2]
    return None


# --- adjacency policy (fix wave 6, N1 (d); ADR 0005 decision 22) ---------------------------
#
# The spec never said how far apart the words of a never-say phrase may be
# and still be "said". Policy: the phrase's words, in order, with at most
# ADJACENCY_GAP other words between consecutive ones, is a human's call
# ("make big money", "get so rich", "guaranteed monthly returns"); further
# apart ("make a lot of money") is not caught by this rule (documented
# limitation: paraphrase is out of scope).

ADJACENCY_GAP = 2


@functools.lru_cache(maxsize=4096)
def word_key(w: str) -> str:
    """A word as the adjacency policy compares it: i -> l, rn/m, cl/d, vv/w
    read alike, stretched letters collapsed."""
    return collapse_runs(visual_view(w, VISUAL_SKELETON))


def phrase_words_in_order(haystack: str, phrase: str, max_gap: int = ADJACENCY_GAP) -> str | None:
    """The shortest span of `haystack` words that contains the words of the
    multi-word `phrase` in order with at most `max_gap` words between
    consecutive ones (each word exact on canonical text, or the same once
    rn/m, cl/d, vv/w, i/l are read alike and stretched letters collapsed),
    or None. Single-word phrases: None (nothing to space out)."""
    pwords = canonical(phrase).split()
    if len(pwords) < 2:
        return None
    toks = _canonical_tokens(haystack)
    pkeys = [word_key(w) for w in pwords]
    keys = [word_key(t) for t in toks]
    best: tuple[int, int] | None = None
    for start in (i for i, k in enumerate(keys) if k == pkeys[0]):
        pos = start
        okay = True
        for pk in pkeys[1:]:
            nxt = next((j for j in range(pos + 1, min(len(keys), pos + max_gap + 2)) if keys[j] == pk), None)
            if nxt is None:
                okay = False
                break
            pos = nxt
        if okay and (best is None or pos - start < best[1] - best[0]):
            best = (start, pos)
    return " ".join(toks[best[0]:best[1] + 1]) if best else None


# --- vowel-drop and phonetic respellings (fix wave 7; AEGIS round 6 NEW-2 / NEW-3) -----------
#
# The visual gate judges LETTERS: a respelling that drops vowels ("mk mny",
# "grntd rtrns") or spells the sound another way ("phree money", "get
# ritch", "make munny") is 2-4 letter edits away and passed. Instead of
# chasing those classes, two more standard signals, each of which alone
# sends the clip to a human (never a reject, never a pass):
#
# (a) CONSONANT SKELETON. `consonant_skeleton()`: the vowels a e i o u y
#     are dropped unless word-initial, runs collapsed ("money" -> "mn",
#     "munny" -> "mn", "income" -> "incm"). The phrase's skeleton is
#     compared with the token-aligned windows of the text's skeleton
#     stream (the same bit-parallel scan as the visual gate, so splits are
#     irrelevant) under `skeleton_budget()`: 0 edits for a skeleton of up
#     to 3 consonants, 1 for 4-6, 2 above. A skeleton is lossy ("for
#     many" is the skeleton of "free money"), so two guards keep ordinary
#     text out — both measured on the three caption corpora (fix wave 7
#     tests): a window containing a FUNCTION WORD (`FUNCTION_WORDS`:
#     determiners, pronouns, prepositions, conjunctions, auxiliaries,
#     common adverbs — a bounded, documented list, no dictionary) that is
#     not itself a word of the phrase is not a respelling ("for many",
#     "make my", "risk for"); and a window at 1+ edits must be one token
#     per phrase word, every edited token keeping WORD_SHARE of its word's
#     skeleton letters, with at least one edited token that DROPPED a
#     vowel (fewer vowels than its phrase word: "get rch", "gt rich") —
#     a consonant difference in a fully vowelled word ("no rush", "form"
#     for "free money") is the visual gate's business, not this one's.
# (b) PHONETIC KEY. `phonetic_key()`: a simplified Metaphone-style key,
#     in-repo (no dependency): initial kn/gn/pn -> n, wr -> r, wh -> w,
#     ps -> s, x -> s; ph -> f; ck, q, hard c -> k; c before e/i/y -> s;
#     ch, sh, tch, tsch, -cia-/-tia- -> X (sh); th -> 0; dg(e/i/y) -> j; gh
#     silent after a vowel (weight / wait), else k; gn -> n; every vowel
#     dropped except a word-initial one (one class, A) — so ee/ea/ie/y/i
#     and ou/ew/u never differ, a silent e is gone, and doubled letters
#     collapse; voiced/unvoiced pairs merge (b/p, d/t, v/f, z/s, g/k).
#     A phrase matches when its words' keys appear in order in the text's
#     token keys within the adjacency policy (ADJACENCY_GAP), on a window
#     that is not the phrase itself and holds no function word that the
#     phrase lacks: "phree money" -> F-R, M-N = the keys of "free money".
# Both respect N3: an entry of at most SHORT_ENTRY_LETTERS letters is
# exact-only unless the rulebook opts it in (`fuzzy`).

VOWELS = frozenset("aeiouy")
WORD_SHARE = 0.6  # an edited phrase word's piece must keep this share of the word's letters

# The 150 or so function words of English (closed classes). A window of the
# text that contains one of these, unless the phrase itself has that word,
# is ordinary text, not a respelling: "for many", "make my", "risk for".
FUNCTION_WORDS = frozenset("""
a an the this that these those my your his her its our their mine yours ours theirs
i me you he him she it we us they them who whom whose which what where when why how
and or but nor so yet for if then than as because while although though unless until since
of in on at to from by with about into onto over under up down out off through between among after before
above below near around across along against during without within upon per via
is am are was were be been being do does did done has have had having will would shall should can could may might must
not no yes very too also just only even still again once ever never always often
all any some each every both few more most much many such other another same own
here there now then today
""".split())


def consonant_skeleton(word: str) -> str:
    """`word` (canonical, a-z) without its vowels (a e i o u y) except a
    word-initial one, runs collapsed: "money" -> "mn", "income" -> "incm",
    "success" -> "scs"."""
    if not word:
        return ""
    return collapse_runs(word[0] + "".join(c for c in word[1:] if c not in VOWELS))


def skeleton_budget(consonants: int) -> int:
    """Edit budget on consonant skeletons: 0 for up to 3, 1 for 4-6, 2 above."""
    return 0 if consonants <= 3 else (1 if consonants <= 6 else 2)


def letter_share(piece: str, word: str) -> float:
    """The fraction of `word`'s letters (as a multiset) that `piece` also
    has: letter_share("munny", "money") == 0.6 (m, n, y)."""
    if not word:
        return 0.0
    have = Counter(piece)
    return sum(min(have[c], k) for c, k in Counter(word).items()) / len(word)


_PH_INITIAL = (("kn", "n"), ("gn", "n"), ("pn", "n"), ("wr", "r"), ("wh", "w"), ("ps", "s"), ("x", "s"))
_SOFT = ("e", "i", "y")


@functools.lru_cache(maxsize=4096)
def phonetic_key(word: str) -> str:
    """Simplified Metaphone-style key of a canonical word (see the block
    comment above): phonetic_key("phree") == phonetic_key("free") == "FR",
    phonetic_key("ritch") == phonetic_key("rich") == "RX"."""
    w = "".join(c for c in word if "a" <= c <= "z")
    if not w:
        return ""
    for src, dst in _PH_INITIAL:
        if w.startswith(src):
            w = dst + w[len(src):]
            break
    out: list[str] = []
    i, n = 0, len(w)
    while i < n:
        c = w[i]
        nxt = w[i + 1] if i + 1 < n else ""
        nxt2 = w[i + 2] if i + 2 < n else ""
        prev = w[i - 1] if i > 0 else ""
        if c == prev and c != "c":  # a doubled letter (cc is judged by what follows it)
            i += 1
        elif c in VOWELS:
            if i == 0:
                out.append("A")
            i += 1
        elif c == "p" and nxt == "h":
            out.append("F")
            i += 2
        elif c == "t":
            if nxt == "c" and nxt2 == "h":
                out.append("X")
                i += 3
            elif nxt == "s" and w[i + 2:i + 4] == "ch":
                out.append("X")
                i += 4
            elif nxt == "i" and nxt2 in ("a", "o"):
                out.append("X")
                i += 1
            elif nxt == "h":
                out.append("0")
                i += 2
            else:
                out.append("T")
                i += 1
        elif c == "c":
            if nxt == "h":
                out.append("X")
                i += 2
            elif nxt == "i" and nxt2 in ("a", "o"):
                out.append("X")
                i += 1
            elif nxt == "k":
                out.append("K")
                i += 2
            elif nxt in _SOFT:
                out.append("S")
                i += 1
            else:
                out.append("K")
                i += 1
        elif c == "s":
            if nxt == "h":
                out.append("X")
                i += 2
            elif nxt == "c" and nxt2 == "h":
                out.append("SK")
                i += 3
            else:
                out.append("S")
                i += 1
        elif c == "g":
            if nxt == "h":
                if prev not in VOWELS:
                    out.append("K")
                i += 2
            elif nxt == "n":
                i += 1
            else:
                out.append("K")
                i += 1
        elif c == "d":
            if nxt == "g" and nxt2 in _SOFT:
                out.append("J")
                i += 2
            else:
                out.append("T")
                i += 1
        elif c in "qk":
            out.append("K")
            i += 1
        elif c in "zs":
            out.append("S")
            i += 1
        elif c in "vf":
            out.append("F")
            i += 1
        elif c in "bp":
            out.append("P")
            i += 1
        elif c == "x":
            out.append("KS")
            i += 1
        elif c == "w":
            if nxt in VOWELS or i == 0:
                out.append("W")
            i += 1
        elif c == "h":
            if not (prev in VOWELS and nxt not in VOWELS) and (nxt in VOWELS or i == 0):
                out.append("H")
            i += 1
        else:
            out.append(c.upper())
            i += 1
    return collapse_runs("".join(out))


def _vowel_count(w: str) -> int:
    return sum(1 for c in w if c in VOWELS)


@functools.lru_cache(maxsize=16)
def _skeleton_stream(text: str) -> tuple[str, bytes, bytes, tuple[int, ...], tuple[str, ...]]:
    """The consonant skeleton of every token, concatenated, with token
    start / end flags and the token index per character (like `_stream`),
    plus the per-token skeletons."""
    toks = _canonical_tokens(text)
    skels = tuple(consonant_skeleton(t) for t in toks)
    n = sum(map(len, skels))
    st = bytearray(n + 1)
    en = bytearray(n + 1)
    tok: list[int] = []
    pos = 0
    for ti, s in enumerate(skels):
        st[pos] = 1
        pos += len(s)
        en[pos] = 1
        tok += [ti] * len(s)
    st[n] = 0
    return "".join(skels), bytes(st), bytes(en), tuple(tok), skels


@functools.lru_cache(maxsize=16)
def _token_keys(text: str) -> tuple[str, ...]:
    return tuple(phonetic_key(t) for t in _canonical_tokens(text))


@functools.lru_cache(maxsize=16)
def _token_consonants(text: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """Per token: its consonant count, and — for a token of 4+ letters — the
    first letter of the key of any run of tokens it starts (every rule
    that decides the first key letter looks at most 3 letters ahead:
    "tsch"), else ''."""
    toks = _canonical_tokens(text)
    return (tuple(len(t) - _vowel_count(t) for t in toks),
            tuple(phonetic_key(t[:4])[:1] if len(t) >= 4 else "" for t in toks))


def _short_entry(phrase: str, fuzzy: bool) -> bool:
    """N3: an entry of at most SHORT_ENTRY_LETTERS letters is exact-only
    unless the rulebook opted it in."""
    return len(canonical(phrase).replace(" ", "")) <= SHORT_ENTRY_LETTERS and not fuzzy


def _no_function_word(window: list[str], pwords: list[str]) -> bool:
    return not any(t in FUNCTION_WORDS and t not in pwords for t in window)


_SNM_MEMO: "OrderedDict[str, dict[tuple[str, bool], tuple[int, str] | None]]" = OrderedDict()


def skeleton_near_misses(haystack: str, phrases: tuple[tuple[str, bool], ...]) -> dict[str, tuple[int, str] | None]:
    """`skeleton_near_miss()` for several (phrase, fuzzy) at once: one
    bit-parallel scan of the text's skeleton stream for the whole set."""
    memo = _SNM_MEMO.get(haystack)
    if memo is None:
        memo = _SNM_MEMO[haystack] = {}
        while len(_SNM_MEMO) > _VNM_MEMO_TEXTS:
            _SNM_MEMO.popitem(last=False)
    _SNM_MEMO.move_to_end(haystack)
    todo = [(p, bool(f)) for p, f in dict.fromkeys((p, bool(f)) for p, f in phrases) if (p, bool(f)) not in memo]
    if todo:
        for key, res in _scan_skeletons(haystack, tuple(todo)).items():
            memo[key] = res
    return {p: memo[(p, bool(f))] for p, f in phrases}


def _scan_skeletons(haystack: str, phrases: tuple[tuple[str, bool], ...]) -> dict[tuple[str, bool], tuple[int, str] | None]:
    S, ST, EN, tok, skels = _skeleton_stream(haystack)
    toks = _canonical_tokens(haystack)
    result: dict[tuple[str, bool], tuple[int, str] | None] = {key: None for key in phrases}
    pats: list[tuple[str, int]] = []
    owner: list[tuple[str, bool]] = []
    pwords_of: dict[tuple[str, bool], list[str]] = {}
    for phrase, fuzzy in phrases:
        pwords = canonical(phrase).split()
        if not pwords or _short_entry(phrase, fuzzy):
            continue
        pskel = "".join(consonant_skeleton(w) for w in pwords)
        pats.append((pskel, skeleton_budget(len(pskel))))
        owner.append((phrase, fuzzy))
        pwords_of[(phrase, fuzzy)] = pwords
    if not pats or not S:
        return result
    for group, pack in _packs(pats):
        for j, gi in pack.scan(S, ST, EN):
            key = owner[group[gi]]
            best = result[key]
            if best is not None and best[0] == 0:
                continue
            pskel, k = pats[group[gi]]
            pwords = pwords_of[key]
            m = len(pskel)
            for s in range(max(0, j - m - k), j - m + k + 1):
                if s >= j or not ST[s]:
                    continue
                d = _osa_within(S[s:j], pskel, k)
                if d is None or (best is not None and d >= best[0]):
                    continue
                lo, hi = tok[s], tok[j - 1] + 1
                window = list(toks[lo:hi])
                if not _no_function_word(window, pwords):
                    continue
                if d >= 1:
                    if hi - lo != len(pwords):
                        continue
                    okay, dropped = True, False
                    for t, ts, w in zip(window, skels[lo:hi], pwords):
                        ws = consonant_skeleton(w)
                        if ts == ws:
                            continue
                        if letter_share(ts, ws) < WORD_SHARE:
                            okay = False
                            break
                        if _vowel_count(t) < _vowel_count(w):
                            dropped = True
                    if not (okay and dropped):
                        continue
                best = (d, " ".join(window))
                if d == 0:
                    break
            result[key] = best
    return result


def skeleton_near_miss(haystack: str, phrase: str, fuzzy: bool = False) -> tuple[int, str] | None:
    """(edits, window text) if the consonant skeleton of a token-aligned
    window of `haystack` is within `skeleton_budget()` of the phrase's —
    "mk mny", "get rch", "grnteed rtrns", "make munny" — under the guards
    described above; else None."""
    return skeleton_near_misses(haystack, ((phrase, bool(fuzzy)),))[phrase]


def phonetic_near_miss(haystack: str, phrase: str, fuzzy: bool = False, max_gap: int = ADJACENCY_GAP) -> str | None:
    """The shortest window of `haystack` whose token keys are the phrase's
    word keys in order within the adjacency policy ("phree money", "get
    ritch", "kno risque", "luze wait fast"), else the first run of tokens
    whose letters, joined, have the key of the phrase's letters joined
    ("rizkphree", "phree m oney"), or None; the phrase written exactly is
    not reported (it is an exact match, not a respelling)."""
    pwords = canonical(phrase).split()
    if not pwords or _short_entry(phrase, fuzzy):
        return None
    pkeys = [phonetic_key(w) for w in pwords]
    if any(not k for k in pkeys):
        return None
    toks = _canonical_tokens(haystack)
    keys = _token_keys(haystack)
    best: tuple[int, int] | None = None
    for start in (i for i, k in enumerate(keys) if k == pkeys[0]):
        pos = start
        hits = [start]
        okay = True
        for pk in pkeys[1:]:
            nxt = next((j for j in range(pos + 1, min(len(keys), pos + max_gap + 2)) if keys[j] == pk), None)
            if nxt is None:
                okay = False
                break
            pos = nxt
            hits.append(pos)
        if not okay or (best is not None and pos - start >= best[1] - best[0]):
            continue
        matched = [toks[i] for i in hits]
        if all(t == w for t, w in zip(matched, pwords)) or not _no_function_word(matched, pwords):
            continue  # the phrase itself (an exact match, not a respelling); or a function word standing for a word
        best = (start, pos)
    if best is None:
        # the phrase's letters run together or split otherwise ("rizkphree", "phree m oney", "risk ph
        # rree"): the key of a run of up to len(pwords) + max_gap tokens, joined, equals the phrase's
        squashed = "".join(pwords)
        pkey = phonetic_key(squashed)
        width = len(pwords) + max_gap
        pcons = len(squashed) - _vowel_count(squashed)
        cons, heads = _token_consonants(haystack)
        for start in range(len(toks)):
            if heads[start] and heads[start] != pkey[0]:
                continue  # a token of 4+ letters fixes the first key letter of any run it starts
            joined = ""
            c = 0
            for end in range(start, min(len(toks), start + width)):
                joined += toks[end]
                c += cons[end]
                if len(joined) > len(squashed) + 4 or c > pcons + 3:
                    break
                # a key drops vowels and merges a few consonant pairs (ph, ck, tch, gh, kn, wr), so
                # a run with fewer consonants than the phrase's key, or many more, cannot share its key
                if c < len(pkey) - 2:
                    continue
                if phonetic_key(joined) == pkey:
                    window = list(toks[start:end + 1])
                    if joined == squashed or not _no_function_word(window, pwords):
                        break
                    return " ".join(window)
    return " ".join(toks[best[0]:best[1] + 1]) if best else None


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
