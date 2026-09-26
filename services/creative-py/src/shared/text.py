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

Fix wave 8 (AEGIS round 7): three more things reach every caller through
`near_miss()`. (1) The consonant and phonetic signals read each token
both as written and with the near-identical pairs contracted (rn -> m,
cl -> d, vv -> w; `READINGS`), so "mk rnunny", "phree rnny" and "lose
vvait fst" are judged as "mk munny", "phree mny", "lose wait fst"; the
skeleton signal's vowel-drop evidence counts every token that dropped
vowels and kept its consonants ("grnteed"). (2) `stacked_near_miss()`:
two of the three similarity signals each within their budget +
STACK_EXTRA on the SAME token window, with respelling evidence, is a
human's call ("ovrnlte success"). (3) `symbol_stand_in()`: a phrase word
stood in by a symbol from SYMBOL_LEXICON ("make 💰", "make $$$ fast",
"guaranteed 📈") or by any other pictograph occupying that word's place.
Cost (N7-7): every per-text view (`_span_index`, `_leet_view`,
`_skeleton_views`, `_folded_words`, `_word_keys`, `_key_positions`,
`_run_starts`, phrase streams) is built once per distinct text and
shared by every phrase; ASCII text skips the Unicode normalisation (the
same result); the packed scans report a lower-bound distance per hit,
take a strict and a relaxed budget per pattern and let the consumer
switch a decided pattern off mid-scan; one DP confirms every start of a
hit (`_osa_suffixes`). The skeleton signal's one-edit tolerance for a
two-consonant word ("luze" / "lose") needs the token to sound like the
word ("made" is not "money").

Fix wave 9 (AEGIS round 8). (1) `LETTERLIKE`: every letter-like symbol
(enclosed, squared, negative squared / circled, regional indicators,
mathematical, fullwidth, small capitals, superscript / subscript),
generated from Unicode names at import, is read as its Latin letter by
every view (`_nfkc`); styled letters are themselves an obfuscation signal,
and so is text that canonicalisation mostly strips (`stripped_share`, the
fail-safe for a style nobody mapped). (2) `symbol_stand_in`: any symbol in
a never-say word's place, across line breaks. (3) Cost: the symbol reading
of `near_miss` indexes the text's words (`_other_candidates`), phrase runs
are keyed once per text (`_run_keys`), relaxed hits are confirmed only when
the stacked rule asks (`_materialised`), memos are per thread (Clip Review
runs off the workflow lock), and short texts have their own caches.

Fix wave 10 (AEGIS round 9). (1) N9-1: currency / math-symbol "fancy text"
("₥₳₭€ ₥⊙₦€¥": no letter at all) — `CURRENCY_MATH_LOOKALIKES`, a curated,
glyph-checked table, is part of the SKELETON reading (a hit is a human's
call: a currency sign is also money), and a word made mostly of symbols
that can be letters counted toward the fail-safe (replaced in fix wave 11
by Rule A). (2) N9-2: regional indicators are
never a signal and never folded; `regional_reading()` is a candidate
reading whose never-say hits Clip Review sends to a human. (3) N9-7: the 26
grade-1 Braille letters are letter-like (read and a signal), U+2800 is a
space, and a run of STRIPPED_MIN consecutive stripped characters is a
fail-safe window of its own. (4) N9-5: the England / Scotland / Wales flag
tag sequences and a standalone enclosed-letter emoji with VS16 are not
signals (`_signal_view`); a #hashtag may hold ASCII digits.

Fix wave 11 (AEGIS round 10). Design ruling: FAIL CLOSED on what the gate
cannot read, instead of adding reading tables. (1) N10-1, Rule A
(`unreadable_words`): a word made mostly of symbols outside the ordinary
set (punctuation, Latin-1, emoji — a block approximation) is a human's
call, whatever alphabet it borrows; Rule B: a divider ("━━━━") is not a
word. A never-say phrase read EXACTLY through `CURRENCY_MATH_LOOKALIKES`
(`currency_math_readings`) is now a rejection: tables only upgrade. (2)
N10-2 / N10-3: the regional reading is matched as a letter stream
(`regional_streams`) and across field joints (Clip Review).

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
import threading
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
# Currency / math-symbol "fancy text" (fix wave 10, AEGIS round 9 N9-1): "₥₳₭€ ₥⊙₦€¥" is make money
# written with NO letter at all, so neither the letter maps nor the letter-bearing SKELETON reading
# saw it, and it passed. Each entry below was checked against its Unicode name and glyph: the symbol
# IS a Latin letter with strokes / bars / an enclosing circle, or the letter's Greek / math twin used
# for it by fancy-text generators. Read like the rest of SKELETON (a stand-in, never a fold), so a
# never-say phrase NEAR a reading through them is a human's call. Fix wave 11 (design ruling: tables
# only upgrade): the phrase read EXACTLY through them (`currency_math_readings`) is a rejection.
# Left out on purpose (no single Latin letter in the glyph, or NFKC already reads it): ₪ NEW SHEQEL
# (two interlocked hooks), ₨ RUPEE (NFKC "Rs"), ₠ ₧ ₯ ₰ ₶ ₷ (multi-letter ligatures), ₻ ₼ ₾ ⃀
# (no clear letter). A word made mostly of such symbols is the fail-safe's business (Rule A,
# `unreadable_words`, fix wave 11), whether or not it is in this table.
CURRENCY_MATH_LOOKALIKES: dict[str, str] = {
    # Sc: currency signs drawn as a stroked / barred Latin letter
    "₥": "m", "₳": "a", "₭": "k", "₦": "n", "₩": "w", "₣": "f", "₫": "d", "฿": "b", "₮": "t", "₤": "le",
    "₱": "p", "₴": "s", "₲": "g", "₵": "c", "₡": "c", "₢": "c", "₺": "tl", "₸": "t", "₹": "r", "₽": "p",
    "₿": "b",
    # Sm: math operators shaped like a letter (circled operators read as o; ⊕ is also the q of
    # fancy-text generators, its second reading)
    "⊙": "o", "⊕": "oq", "⊗": "o", "⊘": "o", "⊖": "o", "∅": "o", "∪": "u", "∩": "n", "∈": "e", "∊": "e",
    "∃": "e", "∀": "a", "∆": "a", "∂": "d", "√": "v", "∨": "v", "⨯": "x", "⌡": "j", "∏": "n", "∑": "e",
    "⊤": "t",
    # So: symbols drawn as a letter
    "♄": "h", "℮": "e", "℗": "p",
}
SKELETON.update({k: v for k, v in CURRENCY_MATH_LOOKALIKES.items() if k not in SKELETON})
# The table as a READING (fix wave 11): every table symbol, and every non-ASCII symbol stand-in of
# SKELETON (€ £ ¥ ¢ © ® § † ° × ¡ ¿), read as its letter — first reading, then second reading ("⊕" is
# o, then q) — the way SKELETON's two tables are. Clip Review rejects a never-say phrase found EXACTLY in
# one of them; nothing else uses it (a near miss through the table stays SKELETON's human_review).
_CM_SOURCE = {**{k: v for k, v in SKELETON.items() if not k.isascii() and not k.isalpha()}, **CURRENCY_MATH_LOOKALIKES}
_CURRENCY_MATH_READINGS = tuple(str.maketrans({k: v[min(alt, len(v) - 1)] for k, v in _CM_SOURCE.items()}) for alt in (0, 1))
_CURRENCY_MATH_RE = re.compile("[" + re.escape("".join(_CM_SOURCE)) + "]")


def currency_math_readings(text: str) -> tuple[str, ...]:
    """`text` with each currency / math lookalike read as its letter ("₥₳₭€ ₥⊙₦€¥" -> "make money"),
    first and second readings (one if they agree); () when it has none."""
    if not text or text.isascii() or not _CURRENCY_MATH_RE.search(text):
        return ()
    return tuple(dict.fromkeys(text.translate(t) for t in _CURRENCY_MATH_READINGS))


# ASCII letters that pass for each other in most fonts (near-miss only).
_HOMOGLYPH_PAIRS = frozenset({("l", "i"), ("i", "l")})
# Word-internal punctuation that ordinary English uses between letters.
_ORDINARY_WORD = re.compile(r"[^\W\d_]+(?:['’.&-][^\W\d_]+)*")
# a #hashtag / @mention of letters and ASCII digits ("#5k", "#35mm", "#tram28": fix wave 10, N9-5); a
# phrase written in one ("#g3t #r1ch", "#getrich") is still read by near_miss() — the digits are stand-ins there
_TAG_WORD = re.compile(r"[#@](?:[^\W\d_]|[0-9])+(?:_(?:[^\W\d_]|[0-9])+)*")
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


# --- letter-like symbols (fix wave 9, AEGIS round 8 H1) ------------------------------------------
#
# "🅼🅰🅺🅴 🅼🅾🅽🅴🆈" (negative squared), "🅜🅐🅚🅔" (negative circled) and
# "🇲🇦🇰🇪" (regional indicators) are symbols (Unicode So) with NO NFKC
# decomposition, so canonical() turned them into spaces and an exact never-
# say phrase passed (36/36). LETTERLIKE maps EVERY letter-like symbol to
# the Latin letter it depicts (regional indicators excepted since fix wave
# 10: see below). It is GENERATED at import from Unicode names
# (Python's own unicodedata, no dependency, nothing hand-listed): every
# code point whose name is a style prefix followed by CAPITAL / SMALL /
# LETTER and one Latin letter A-Z — Enclosed Alphanumerics (circled,
# parenthesized), the Enclosed Alphanumeric Supplement (squared, negative
# squared / circled, crossed, tortoise-shell, regional indicators),
# Mathematical Alphanumeric Symbols, the Letterlike Symbols (script,
# black-letter, double-struck, turned ...), fullwidth forms, small
# capitals, superscript / subscript / modifier letters. Names of another
# script (Greek, Cyrillic ...), digits, combining and tag characters, and
# any letter "WITH" a diacritic are not letter-like. tests/test_fix_wave_9
# checks the map against an independent derivation over every code point
# of those blocks.
#
# Regional indicators (fix wave 10, AEGIS round 9 N9-2) are NOT in LETTERLIKE.
# Two of them side by side are one flag emoji ("🇬🇧"), and a run of them is
# a row of flags as often as it is a word, so no rule on run lengths can
# tell them apart: wave 9's "every run exactly two long means flags" passed
# "🇲🇦 🇰🇪 🇨🇦 🇸🇭" (make cash, four valid flags) and sent a genuine row of
# eight flags to a human. Now they are never a signal by themselves and
# never read as letters by canonical(); `regional_reading()` gives the text
# with every regional indicator read as its letter, and Clip Review matches
# every never-say phrase against that reading as well — a hit there is a
# human's call (it may be flags), never a rejection; no hit, no flag.
#
# Braille (fix wave 10, N9-7): the 26 letters of grade-1 (uncontracted)
# Braille, U+2801.., are letter-like — READ as their letters by every view
# and a styled-letter signal like the rest. They are derived from the dot
# numbers in the code points' Unicode names ("BRAILLE PATTERN DOTS-134" is
# m), not typed in. U+2800 BRAILLE PATTERN BLANK is a space: creators paste
# it for blank caption lines, and it separates Braille words.
_LETTERLIKE_NAME = re.compile(
    r"(?:MATHEMATICAL|FULLWIDTH|CIRCLED|PARENTHESIZED|SQUARED|NEGATIVE|CROSSED|TORTOISE SHELL BRACKETED|"
    r"REGIONAL INDICATOR|DOUBLE-STRUCK|SCRIPT|BLACK-LETTER|TURNED|REVERSED|ROTATED|INVERTED|MODIFIER LETTER|SUPERSCRIPT|"
    r"SUBSCRIPT|LATIN)\b(?: [A-Z-]+)*? (?:CAPITAL|SMALL|LETTER) ([A-Z])")
_NOT_LETTERLIKE = ("GREEK", "CYRILLIC", "HEBREW", "ARABIC", "DIGIT", "COMBINING", "TAG ", " WITH ")


def _generated_letterlike() -> dict[str, str]:
    out: dict[str, str] = {}
    for cp in range(0x80, 0x30000):
        name = unicodedata.name(chr(cp), "")
        m = _LETTERLIKE_NAME.fullmatch(name)
        if m and not any(s in name for s in _NOT_LETTERLIKE) and not 0xC0 <= cp <= 0xFF:
            out[chr(cp)] = m.group(1).lower()
    return out


# Grade-1 Braille letters by their dots (the standard alphabet: a = 1, b = 12, ... z = 1356).
BRAILLE_DOTS = {"a": "1", "b": "12", "c": "14", "d": "145", "e": "15", "f": "124", "g": "1245", "h": "125",
                "i": "24", "j": "245", "k": "13", "l": "123", "m": "134", "n": "1345", "o": "135", "p": "1234",
                "q": "12345", "r": "1235", "s": "234", "t": "2345", "u": "136", "v": "1236", "w": "2456",
                "x": "1346", "y": "13456", "z": "1356"}


def _braille_letters() -> dict[str, str]:
    """{Braille cell: letter}, found by NAME ("BRAILLE PATTERN DOTS-<dots>") in U+2800..U+28FF."""
    by_dots = {dots: letter for letter, dots in BRAILLE_DOTS.items()}
    out = {}
    for cp in range(0x2801, 0x2900):
        name = unicodedata.name(chr(cp), "")
        if name.startswith("BRAILLE PATTERN DOTS-") and name[len("BRAILLE PATTERN DOTS-"):] in by_dots:
            out[chr(cp)] = by_dots[name[len("BRAILLE PATTERN DOTS-"):]]
    return out


BRAILLE_LETTERS: dict[str, str] = _braille_letters()
BRAILLE_BLANK = "\u2800"
_REGIONAL = frozenset(chr(cp) for cp in range(0x1F1E6, 0x1F200))
_GENERATED = _generated_letterlike()
REGIONAL_LETTERS: dict[str, str] = {k: v for k, v in _GENERATED.items() if k in _REGIONAL}
LETTERLIKE: dict[str, str] = {**{k: v for k, v in _GENERATED.items() if k not in _REGIONAL}, **BRAILLE_LETTERS}
_LETTERLIKE_TABLE = str.maketrans({**LETTERLIKE, BRAILLE_BLANK: " "})
_REGIONAL_TABLE = str.maketrans(REGIONAL_LETTERS)


def _char_class(chars) -> str:
    """A regex character class of `chars` written as code-point RANGES (a
    class of 1,000 single characters is scanned linearly per character)."""
    cps = sorted(map(ord, chars))
    parts, i = [], 0
    while i < len(cps):
        j = i
        while j + 1 < len(cps) and cps[j + 1] == cps[j] + 1:
            j += 1
        parts.append(re.escape(chr(cps[i])) + ("-" + re.escape(chr(cps[j])) if j > i else ""))
        i = j + 1
    return "[" + "".join(parts) + "]"


_LETTERLIKE_RE = re.compile(_char_class(LETTERLIKE))
_MAPPED_RE = re.compile(_char_class({*LETTERLIKE, BRAILLE_BLANK}))
_REGIONAL_RUN = re.compile("[\U0001F1E6-\U0001F1FF]+")


def letterlike_chars(text: str) -> list[str]:
    """The letter-like symbols in `text`, as 'U+XXXX'. Regional indicators
    are not (fix wave 10, N9-2: they are a reading, `regional_reading`)."""
    if not text or text.isascii() or not _LETTERLIKE_RE.search(text):
        return []
    return list(dict.fromkeys(f"U+{ord(c):04X}" for c in _LETTERLIKE_RE.findall(text)))


def regional_reading(text: str) -> str | None:
    """`text` with every regional indicator read as the letter it depicts
    ("🇲🇦 🇰🇪 🇨🇦 🇸🇭" -> "ma ke ca sh"), or None if it has none. A
    CANDIDATE reading only (fix wave 10, N9-2): Clip Review looks for the
    never-say phrases in it, and a hit is a human's call (it may be flags)."""
    if not text or text.isascii() or not _REGIONAL_RUN.search(text):
        return None
    return text.translate(_REGIONAL_TABLE)


# A run of regional indicators with nothing but spaces / invisible characters between them (fix wave 11,
# AEGIS round 10 N10-2): "🇺🇸🇲🇦🇰🇪 🇲🇴🇳🇪🇾🇬🇧" is ONE letter stream, "usmakemoneygb".
_REGIONAL_STREAM = re.compile("[\U0001F1E6-\U0001F1FF](?:[\\s\u00ad\u034f\u180e\u200b-\u200f\u2060-\u2064\ufe00-\ufe0f\u20e3\ufeff]*"
                              "[\U0001F1E6-\U0001F1FF])*")


def regional_streams(text: str) -> list[tuple[int, int, str]]:
    """Every run of regional indicators in `text` — whitespace and invisible joiners / selectors between
    them ignored — as (start, end, the letters it reads as), in order (fix wave 11, N10-2): flags carry
    no word boundaries, so Clip Review matches a never-say phrase ANYWHERE inside a stream, the way the
    main gate's letter streams match across spaces ("🇺🇸🇲🇦🇰🇪 🇲🇴🇳🇪🇾🇬🇧" -> "usmakemoneygb")."""
    if not text or text.isascii() or not _REGIONAL_RUN.search(text):
        return []
    return [(m.start(), m.end(), "".join(REGIONAL_LETTERS[c] for c in m.group() if c in REGIONAL_LETTERS))
            for m in _REGIONAL_STREAM.finditer(text)]


def phrase_stream(phrase: str) -> str:
    """`phrase`'s letters as a stream: canonical, spaces dropped, runs collapsed (`collapse_runs`) — found
    inside a collapsed regional-indicator stream (`regional_streams`), it is there across any flags."""
    return collapse_runs(canonical(phrase).replace(" ", ""))


def _map_letterlike(text: str) -> str:
    if not text or text.isascii():
        return text
    return _map_letterlike_cached(text)


@functools.lru_cache(maxsize=32)
def _map_letterlike_cached(text: str) -> str:
    if not _MAPPED_RE.search(text):
        return text
    return text.translate(_LETTERLIKE_TABLE)


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", _map_letterlike(text or "").translate(_SPACING_DIACRITICS))


# Non-letters that NFKC turns INTO letters (™ -> "TM", Ⓖ -> "G", ㎏ -> "kg"):
# canonical() folds them (so "Ⓖⓔⓣ ⓡⓘⓒⓗ" is still an exact never-say), but the
# word-level views (mixed words, near misses) see them as the symbols they
# are, so "ri™sk" is a word mixing letters with a symbol.
# A letter-like symbol (LETTERLIKE, fix wave 9) is NOT one of these: it is
# read as its letter in every view (and is itself an obfuscation signal).
_SYMBOLS_TO_LETTERS = {cp: "\u00b7" for cp in range(0x80, 0x30000)
                       if not unicodedata.category(chr(cp)).startswith("L") and chr(cp) not in LETTERLIKE
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


@functools.lru_cache(maxsize=65536)
def _plain_char(ch: str) -> bool:
    """No decomposition and not a combining mark: NFKD leaves it alone."""
    return not unicodedata.decomposition(ch) and not unicodedata.combining(ch)


def _strip_marks(t: str) -> str:
    if t.isascii() or (unicodedata.is_normalized("NFC", t) and all(_plain_char(ch) for ch in set(t))):
        return t  # nothing to decompose or strip, already composed (fix wave 9, M1: 0.1 s on 65 KB)
    d = unicodedata.normalize("NFKD", t)
    return unicodedata.normalize("NFC", "".join(ch for ch in d if not unicodedata.combining(ch)))


_DROP_TABLE = {**{ord(c): None for c in _IGNORABLE_SET},
               **{cp: None for cp in range(0x80, 0x30000) if unicodedata.category(chr(cp)) == "Cf"},
               **{ord(c): " " for c in _BLANK_IGNORABLES}}


def _drop_format(t: str) -> str:
    """Blank-rendering ignorables -> space; every other default-ignorable
    or format (Cf) character -> deleted. One str.translate (fix wave 9,
    M1: the per-character generator was 0.3 s on a 65 KB clip)."""
    return t.translate(_DROP_TABLE) if not t.isascii() else t


SHORT_TEXT = 512  # a text up to this long (a phrase, a word, a field edge) is cached in the large caches


def canonical(text: str) -> str:
    """See the module docstring. Short texts (phrases, words, joints)
    are memoised in a large cache, long ones (a clip's fields) in a small
    one: a 100-phrase rulebook no longer evicts its own phrases (fix wave
    9, M1)."""
    text = text or ""
    return _canonical_short(text) if len(text) <= SHORT_TEXT else _canonical_long(text)


@functools.lru_cache(maxsize=16384)
def _canonical_short(text: str) -> str:
    return _canonical(text)


@functools.lru_cache(maxsize=64)
def _canonical_long(text: str) -> str:
    return _canonical(text)


def _canonical(text: str) -> str:
    if text.isascii():  # nothing to normalise, fold or strip below U+0080 (fix wave 8, N7-7: 50 KB in C)
        return " ".join(_NON_WORD.sub(" ", text.lower()).split())
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


@functools.lru_cache(maxsize=64)
def _span_index(text: str) -> tuple[str, bytes, bytes]:
    """`text`'s tokens concatenated, with start / end flags per position
    (start[p]: a token starts at p; end[p]: one ends there). Built once
    per distinct text and shared by every phrase (fix wave 8, N7-7: the
    per-phrase token walk was 3 s of a 99-phrase review)."""
    toks = text.split()
    S = "".join(toks)
    n = len(S)
    st = bytearray(n + 1)
    en = bytearray(n + 1)
    pos = 0
    for t in toks:
        st[pos] = 1
        pos += len(t)
        en[pos] = 1
    return S, bytes(st), bytes(en)


def _span_match(tokens: list[str] | tuple[str, ...], target: str) -> bool:
    """Does some contiguous run of tokens, concatenated, equal `target`
    exactly (starting and ending on token boundaries)?"""
    return _span_in(_span_index(" ".join(tokens)), target)


def _span_in(index: tuple[str, bytes, bytes], target: str) -> bool:
    S, ST, EN = index
    if not target:
        return False
    m = len(target)
    i = S.find(target)
    while i >= 0:
        if ST[i] and EN[i + m]:
            return True
        i = S.find(target, i + 1)
    return False


@functools.lru_cache(maxsize=64)
def _leet_view(canonical_text: str) -> str | None:
    """The canonical text with leetspeak folded inside tokens that contain
    a letter, or None if that changes nothing. Once per distinct text."""
    toks = canonical_text.split()
    leet = [t.translate(_LEET) if any(c.isalpha() for c in t) else t for t in toks]
    return None if leet == toks else " ".join(leet)


@functools.lru_cache(maxsize=16)
def _padded(t: str) -> str:
    """` t `, once per text (fix wave 9, M1: rebuilt per phrase, 100 x 65 KB)."""
    return f" {t} "


def match_phrase(haystack: str, phrase: str) -> PhraseMatch:
    p = canonical(phrase)
    if not p:
        return PhraseMatch.NONE
    h = canonical(haystack)
    if f" {p} " in _padded(h):
        return PhraseMatch.EXACT
    squashed = p.replace(" ", "")
    if _span_in(_span_index(h), squashed):
        return PhraseMatch.LOOSE
    leet = _leet_view(h)
    if leet is not None and (f" {p} " in _padded(leet) or _span_in(_span_index(leet), squashed)):
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
    text = text or ""
    if text.isascii():
        return text.lower()
    t = _nfkc_words(text)
    t = _drop_format(t)
    # ß is kept as one character (casefold would make it "ss"), so its
    # SKELETON reading as "b" ("doußle") is still available.
    t = _strip_marks(t).replace("\u1e9e", "\ue000").replace("\u00df", "\ue000")
    return t.casefold().translate(_CONFUSABLE_TABLE).replace("\ue000", "\u00df")


@functools.lru_cache(maxsize=16)
def _skeleton_views(folded: str) -> tuple[str, str]:
    """The folded text with SKELETON symbols read as letters inside words
    that contain a letter — once with each symbol's first reading, once
    with its second — then canonicalised (other symbols -> space). Once
    per distinct text (fix wave 8, N7-7)."""
    views = []
    words = folded.split()
    lettered = [w.isalpha() or any(map(str.isalpha, w)) for w in words]
    for table in _SKELETON_TABLES:
        out = [w.translate(table) if has else w for w, has in zip(words, lettered)]
        views.append(" ".join(_NON_WORD.sub(" ", " ".join(out)).split()))
    return tuple(views)


_SKELETON_TABLES = tuple({ord(k): v[min(alt, len(v) - 1)] for k, v in SKELETON.items()} for alt in (0, 1))


@functools.lru_cache(maxsize=16)
def _folded_words(haystack: str) -> tuple[tuple[str, ...], dict[str, tuple[int, ...]], dict[str, tuple[str, ...]], tuple[str, ...]]:
    """The folded text's words; the positions of each distinct word; for
    the plain-ASCII-letter words, the distinct words under each l/i
    normalisation (the only way one plain word can stand for another in
    `_word_cost`); and the distinct words that are NOT plain ASCII
    letters. Once per distinct text (fix wave 8, N7-7)."""
    words = tuple(_folded(haystack).split())
    positions: dict[str, list[int]] = {}
    for i, w in enumerate(words):
        positions.setdefault(w, []).append(i)
    plain: dict[str, list[str]] = {}
    other: list[str] = []
    for w in positions:
        if w.isascii() and w.isalpha():
            plain.setdefault(w.replace("l", "i"), []).append(w)
        else:
            other.append(w)
    return (words, {w: tuple(v) for w, v in positions.items()}, {k: tuple(v) for k, v in plain.items()},
            tuple(other))


@functools.lru_cache(maxsize=16)
def _other_index(haystack: str):
    """The folded text's words that are NOT plain ASCII letters (`_folded_words`),
    indexed for `_other_candidates` by the first and last of their ASCII letters
    (i -> l read alike): ({(first, last): [(word, letters)]}, {letter: [...]} for
    one ASCII letter, [...] for none, a per-phrase-word memo). Once per text."""
    by_ends: dict[tuple[str, str], list[tuple[str, str]]] = {}
    by_one: dict[str, list[tuple[str, str]]] = {}
    none: list[tuple[str, str]] = []
    for w in _folded_words(haystack)[3]:
        letters = "".join(c for c in w if c.isascii() and c.isalpha()).replace("l", "i")
        if len(letters) >= 2:
            by_ends.setdefault((letters[0], letters[-1]), []).append((w, letters))
        elif letters:
            by_one.setdefault(letters, []).append((w, letters))
        else:
            none.append((w, letters))
    return by_ends, by_one, none, {}


def _is_subsequence(a: str, b: str) -> bool:
    it = iter(b)
    return all(c in it for c in a)


def _other_candidates(haystack: str, pw: str) -> list[str]:
    """The non-plain words of `haystack` that `_word_cost` could read as
    phrase word `pw` — a superset, by two NECESSARY conditions: an ASCII
    letter of the word is never inserted or stood in for (it equals its
    phrase letter, or is its l/i homoglyph), so the word's ASCII letters
    are a subsequence of `pw` (i and l read alike); and its length is
    within `_word_cost`'s bounds. Fix wave 9 (AEGIS round 8 M1): every
    such word was costed against every phrase word (488,870 DP runs, 1.8 s
    of a 100-phrase review); the index cuts that to the candidates."""
    by_ends, by_one, none, memo = _other_index(haystack)
    got = memo.get(pw)
    if got is None:
        pn = pw.replace("l", "i")
        m = len(pw)
        lo, hi = m, 3 * m + 8
        got = []
        for key in {(pn[i], pn[j]) for i in range(len(pn)) for j in range(i + 1, len(pn))}:
            got += [w for w, letters in by_ends.get(key, ()) if lo <= len(w) <= hi and _is_subsequence(letters, pn)]
        for ch in set(pn):
            got += [w for w, _ in by_one.get(ch, ()) if lo <= len(w) <= hi]
        got += [w for w, _ in none if lo <= len(w) <= hi]
        memo[pw] = got
    return got


@functools.lru_cache(maxsize=65536)
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


def near_miss(haystack: str, phrase: str, fuzzy: bool = False, stacked: bool = True, stage: str = "all") -> str | None:
    """How `phrase` shows up in `haystack` only once symbols/digits are read
    as letters (see the module docstring), as a readable respelling
    (`visual_near_miss`), or as its words in order with a filler between
    them (`phrase_words_in_order`) — or None. A description, e.g.
    "symbols/digits standing in for letters", for the human reviewer.
    `fuzzy`: the rulebook author opted a short entry into the similarity
    gate (N3). `stacked`: also the stacked rule and the symbol lexicon
    (fix wave 8; Clip Review runs those itself, batched, for the phrases
    no other signal caught). `stage` "early": only the signals before the
    consonant skeleton (fix wave 9, M1: Clip Review batches the skeleton
    and phonetic scans for the phrases the early signals leave, and asks
    `skeleton_signal` / `phonetic_signal` itself — the same first signal
    per phrase, in the same order)."""
    p = canonical(phrase)
    if not p:
        return None
    folded = _folded(haystack)
    squashed = p.replace(" ", "")
    for v in _skeleton_views(folded):
        if f" {p} " in _padded(v) or _span_in(_span_index(v), squashed):
            return "symbols/digits standing in for letters"
    words, positions, plain, other = _folded_words(haystack)
    pwords = p.split()

    def fitting(pw: str, cap: int) -> list[str]:
        """The distinct text words that can be read as `pw`."""
        out = [w for w in plain.get(pw.replace("l", "i"), ()) if _word_cost(w, pw, cap) is not None]
        out += [w for w in _other_candidates(haystack, pw) if _word_cost(w, pw, cap) is not None]
        return out

    caps = [max(1, (len(pw) + 1) // 2) for pw in pwords]
    m = len(pwords)
    fits: list[list[str]] = []
    for pw, cap in zip(pwords, caps):
        fits.append(fitting(pw, cap))
        if not fits[-1]:
            break  # a phrase word no text word can be read as: no reading of the phrase (fix wave 9, M1)
    if len(fits) == m and all(fits):
        rest = [frozenset(f) for f in fits[1:]]
        n = len(words)
        for w0 in fits[0]:
            for k in positions[w0]:
                if k + m <= n and all(words[k + t] in rest[t - 1] for t in range(1, m)):
                    return "symbols, digits or unknown letters in place of letters"
    if m > 1:
        cap = max(1, len(squashed) // 3)
        if fitting(squashed, cap):
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
    if stage == "early":
        return None
    how = skeleton_signal(haystack, phrase, fuzzy)
    if how is not None or stage == "skeleton":
        return how
    how = phonetic_signal(haystack, phrase, fuzzy)
    if how is not None:
        return how
    return stacked_or_symbol(haystack, phrase, fuzzy) if stacked else None


def skeleton_signal(haystack: str, phrase: str, fuzzy: bool = False) -> str | None:
    """`near_miss()`'s consonant-skeleton signal, as its description."""
    skel = skeleton_near_miss(haystack, phrase, fuzzy)
    if skel is None:
        return None
    d, window = skel
    return (f"its consonants with the vowels dropped or changed ({window[:60]!r} is {d} edit(s) from it "
            "once vowels are ignored)")


def phonetic_signal(haystack: str, phrase: str, fuzzy: bool = False) -> str | None:
    """`near_miss()`'s phonetic signal, as its description."""
    sound = phonetic_near_miss(haystack, phrase, fuzzy)
    return None if sound is None else f"a phonetic respelling ({sound[:60]!r} sounds like it)"


def stacked_or_symbol(haystack: str, phrase: str, fuzzy: bool = False) -> str | None:
    """The last two signals of `near_miss()` (fix wave 8): the stacked
    rule, then a symbol standing for a word."""
    stacked = stacked_near_miss(haystack, phrase, fuzzy)
    if stacked is not None:
        return (f"a respelling that two similarity signals each nearly accept ({stacked[:60]!r}: letters, "
                "consonants and sound are all close to it)")
    sym = symbol_stand_in(haystack, phrase)
    if sym is not None:
        return f"a symbol standing for one of its words ({sym[:120]})"
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


@functools.lru_cache(maxsize=131072)
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


def _osa_suffixes(tail: str, p: str, k: int) -> list[int | None]:
    """[OSA distance between `p` and tail[len(tail) - t:] if <= k, else
    None, for t = 0 .. len(tail)] — every window ending at the end of
    `tail` at once, in one banded DP over the reversed strings (OSA is
    unchanged by reversing both). Equal, per window, to
    `_osa_within(tail[-t:], p, k)` (fix wave 8, N7-7: the confirmation of a
    scan hit computed one DP per candidate start)."""
    m, n = len(p), len(tail)
    out: list[int | None] = [None] * (n + 1)
    if m <= k:
        out[0] = m
    if n == 0:
        return out
    a = tail[::-1]  # the text, reversed: column t is the window of the last t characters
    b = p[::-1]
    inf = k + 1
    # rows over the pattern (i), columns over the text (t), band |i - t| <= k
    prev2: list[int] = []
    prev = [t if t <= k else inf for t in range(n + 1)]
    for i in range(1, m + 1):
        cur = [inf] * (n + 1)
        if i <= k:
            cur[0] = i
        lo, hi = max(1, i - k), min(n, i + k)
        best = cur[0]
        bi = b[i - 1]
        for t in range(lo, hi + 1):
            at = a[t - 1]
            v = prev[t - 1] + (bi != at)
            if prev[t] + 1 < v:
                v = prev[t] + 1
            if cur[t - 1] + 1 < v:
                v = cur[t - 1] + 1
            if i > 1 and t > 1 and bi == a[t - 2] and b[i - 2] == at and prev2[t - 2] + 1 < v:
                v = prev2[t - 2] + 1
            cur[t] = v if v < inf else inf
            if cur[t] < best:
                best = cur[t]
        if best > k:
            return out
        prev2, prev = prev, cur
    for t in range(n + 1):
        if prev[t] <= k:
            out[t] = prev[t]
    return out


def _canonical_tokens(text: str) -> tuple[str, ...]:
    return _canonical_tokens_short(text) if len(text) <= SHORT_TEXT else _canonical_tokens_long(text)


@functools.lru_cache(maxsize=16384)
def _canonical_tokens_short(text: str) -> tuple[str, ...]:
    return tuple(canonical(text).split())


@functools.lru_cache(maxsize=16)
def _canonical_tokens_long(text: str) -> tuple[str, ...]:
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


_RUNS = re.compile(r"(.)\1+", re.DOTALL)


def _tracked_collapse(s: str, st: bytearray, en: bytearray, tok: list[int]):
    """Runs of one repeated character -> one character, carrying the flags
    like _tracked_contract (a start inside the run moves to the survivor,
    an end inside it to just after). Only the runs are visited (fix wave
    9, M1: a per-character loop over 65 KB, four views)."""
    out: list[str] = []
    nst = bytearray()
    nen = bytearray()
    ntok: list[int] = []
    i = 0
    pend = 0
    for m in _RUNS.finditer(s):
        a, b = m.span()
        # the stretch before the run, as is
        out.append(s[i:a + 1])
        nst += st[i:a + 1]
        nen += en[i:a + 1]
        if a + 1 > i:
            nen[len(nen) - (a + 1 - i)] |= pend
            pend = 0
        ntok += tok[i:a + 1]
        # the run's other characters: their start flags move to the survivor, end flags to after it
        for q in range(a + 1, b):
            nst[-1] |= st[q]
            pend |= en[q]
        i = b
    out.append(s[i:])
    nst += st[i:len(s)]
    nen += en[i:len(s)]
    if len(s) > i:
        nen[len(nen) - (len(s) - i)] |= pend
        pend = 0
    ntok += tok[i:]
    nst.append(0)
    nen.append(en[len(s)] | pend)
    return "".join(out), nst, nen, ntok


@functools.lru_cache(maxsize=16)
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
    return _build_stream(text, rules, collapse)


@functools.lru_cache(maxsize=16384)
def _phrase_skel(text: str, rules: tuple[tuple[str, str], ...], collapse: bool = True) -> str:
    """`_phrase_stream(text, rules, collapse)[0]` without the flags: the
    same string (i -> l, each pair rule as str.replace, left to right, then
    the run collapse). Fix wave 9 (M1): the phrase side needs only this."""
    t = "".join(_canonical_tokens(text)).replace("i", "l")
    for src, proto in rules:
        t = t.replace(src, proto)
    return collapse_runs(t) if collapse else t


@functools.lru_cache(maxsize=8192)
def _phrase_stream(text: str, rules: tuple[tuple[str, str], ...], collapse: bool = True) -> tuple[str, bytes, bytes, tuple[int, ...]]:
    """`_stream` for PHRASES and phrase words: a separate, larger cache so
    a long phrase list does not evict the text streams (fix wave 8, N7-7)."""
    return _build_stream(text, rules, collapse)


def _build_stream(text: str, rules: tuple[tuple[str, str], ...], collapse: bool) -> tuple[str, bytes, bytes, tuple[int, ...]]:
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
    scanned together (see the block comment above). A pattern is
    (text, k) or (text, k, kx) with kx >= k: hits within k are reported
    always, hits within kx only while the pattern's RELAXED bit is live
    (fix wave 8: the stacked rule's budget + STACK_EXTRA candidates, cut
    off per pattern once its consumer has enough of them)."""

    __slots__ = ("W", "n", "PAT", "FIRST", "TOPS", "LSBS", "CK", "CKX", "relaxable", "scores0", "pm")

    def __init__(self, pats: list[tuple]):
        # A field holds the pattern's bits (top m bits), a carry guard (the
        # lowest bit), and doubles as a W-bit distance counter that the
        # "<= k" test adds 2^(W-1) - 1 - k to: so 2^(W-1) > max(m, k).
        m_max = max(len(p[0]) for p in pats)
        k_max = max(p[-1] for p in pats)
        W = max(m_max + 1, max(m_max, k_max).bit_length() + 1)
        self.W, self.n = W, len(pats)
        PAT = FIRST = TOPS = LSBS = CK = CKX = scores0 = 0
        relaxable = False
        pm: dict[str, int] = {}
        for i, pat in enumerate(pats):
            p, k, kx = pat[0], pat[1], pat[-1]
            relaxable = relaxable or kx > k
            base = i * W
            m = len(p)
            first = base + W - m
            PAT |= ((1 << m) - 1) << first
            FIRST |= 1 << first
            TOPS |= 1 << (base + W - 1)
            LSBS |= 1 << base
            CK |= ((1 << (W - 1)) - 1 - k) << base
            CKX |= ((1 << (W - 1)) - 1 - kx) << base
            scores0 |= m << base
            for q, ch in enumerate(p):
                pm[ch] = pm.get(ch, 0) | (1 << (first + q))
        self.PAT, self.FIRST, self.TOPS, self.LSBS, self.CK, self.CKX = PAT, FIRST, TOPS, LSBS, CK, CKX
        self.relaxable, self.scores0, self.pm = relaxable, scores0, pm

    def bit(self, i: int) -> int:
        """Pattern i's bit in a `live` mask."""
        return 1 << (i * self.W + self.W - 1)

    def scan(self, S: str, ST: bytes, EN: bytes, live: list[int] | None = None):
        """Yields (end position j, pattern index, distance) for every token
        end j (EN[j]) at which some pattern is within its budget of a
        window ending there (the relaxed-start superset — the top row is 0
        at a token start ST[j], 1 elsewhere — so the distance is a LOWER
        bound of the token-aligned one; confirm with _osa_within).
        `live`: [patterns still wanted, patterns whose kx hits are still
        wanted] as masks of `bit(i)`, read at every token end, so the
        consumer can switch a pattern off while the scan runs (default:
        all, both)."""
        W, PAT, FIRST, TOPS, LSBS, CK, CKX = self.W, self.PAT, self.FIRST, self.TOPS, self.LSBS, self.CK, self.CKX
        if live is None:
            live = [TOPS, TOPS]
        relaxable = self.relaxable
        field = (1 << W) - 1
        get = self.pm.get
        sh = W - 1
        Pv, Mv, scores, D0p, Eqp = PAT, 0, self.scores0, 0, 0
        acc = 0  # the top-row deltas since the last token end, not yet shifted into `scores` (fix wave 9, M1)
        top = 0 if ST[0] else 1
        # Every term below stays inside PAT (Pv, Mv, Ph, Mh, Eq and FIRST are
        # subsets of it), so `~x & PAT` is written `PAT ^ x` and the outer
        # masks of the textbook form are dropped (fix wave 8, N7-7: 6 fewer
        # big-integer operations per character; same values).
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
            D0 = ((((Eqm & Pv) + Pv) & PAT) ^ Pv) | Eqm | Mv | (((Eq ^ (Eq & D0p)) << 1) & Eqp)
            Ph = Mv | (PAT ^ (D0 | Pv))
            Mh = Pv & D0
            # the top-row deltas, one per field: both masked values are multiples of 2^sh, so their
            # running sum is too, and its shift, taken at a token end, is exact (= the sum of the
            # shifted deltas)
            acc += (Ph & TOPS) - (Mh & TOPS)
            Ph = (Ph << 1) & PAT
            Mh = (Mh << 1) & PAT
            if ntop > top:
                Ph |= FIRST
            elif ntop < top:
                Mh |= FIRST
            top = ntop
            Pv = Mh | (PAT ^ (D0 | Ph))  # Hyyro's form: the vertical deltas follow D0 (which holds the transposition)
            Mv = Ph & D0
            D0p, Eqp = D0, Eq
            if EN[j]:
                scores += acc >> sh
                acc = 0
                h = ~(scores + CKX) & TOPS
                if h:
                    if relaxable:
                        h = ((~(scores + CK) & h) | (h & live[1])) & live[0]
                    else:
                        h &= live[0]
                while h:
                    low = h & -h
                    h ^= low
                    base = low.bit_length() - W
                    yield j, base // W, (scores >> base) & field


PACK_BITS = 4096  # patterns are packed into integers of about this many bits (4,096: measured best, fix wave 8)


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
# Fix wave 9 (M1): Clip Review now runs OFF the workflow lock, so several
# reviews can run at once in the worker threads. The per-text memos below
# are PER THREAD (`_tls_store`): a review never sees another thread's
# half-filled or evicted entry (the lru_caches hold pure functions' results
# and are thread-safe).
_TLS = threading.local()


def _tls_store(name: str) -> OrderedDict:
    d = getattr(_TLS, name, None)
    if d is None:
        d = OrderedDict()
        setattr(_TLS, name, d)
    return d


def clear_memos() -> None:
    """Empty this thread's per-text memos (tests measure cold cost)."""
    for name in list(vars(_TLS)):
        getattr(_TLS, name).clear()


_VNM_MEMO_TEXTS = 16
# The token spans (start index, end index) of the same haystack within the
# phrase's visual budget PLUS STACK_EXTRA (fix wave 8, class B: the
# stacked rule's relaxed windows). Filled by a SEPARATE, lazy scan
# (`relaxed_visual_spans`) for the phrases that had no other hit, so the
# ordinary path costs what it did; at most MAX_RELAXED_SPANS per phrase.
STACK_EXTRA = 1  # each signal's budget is relaxed by this much for the stacked rule
MAX_RELAXED_SPANS = 16


def _memo_batch(memo_store, span_store, haystack: str, phrases, scan):
    """Shared memo discipline: one batch `scan` per haystack for the
    (phrase, fuzzy) keys not yet known, filling the result memo and the
    relaxed-span memo together; bounded by distinct haystacks."""
    memo = memo_store.get(haystack)
    if memo is None:
        memo = memo_store[haystack] = {}
        span_store[haystack] = {}
        while len(memo_store) > _VNM_MEMO_TEXTS:
            gone, _ = memo_store.popitem(last=False)
            span_store.pop(gone, None)
    memo_store.move_to_end(haystack)
    todo = [(p, bool(f)) for p, f in dict.fromkeys((p, bool(f)) for p, f in phrases) if (p, bool(f)) not in memo]
    if todo:
        res, spans = scan(haystack, tuple(todo))
        memo.update(res)
        span_store[haystack].update(spans)
    return {p: memo[(p, bool(f))] for p, f in phrases}


def _materialised(spans: dict, phrases) -> dict[str, frozenset[tuple[int, int]]]:
    """The relaxed spans of `phrases`, confirming the lazily recorded ones
    now (fix wave 9, M1: a relaxed hit is only confirmed for a phrase the
    stacked rule is actually asked about — every other signal found
    nothing — never for the phrases another signal finds; the same hits,
    confirmed the same way, give the same spans)."""
    out = {}
    for p, f in phrases:
        v = spans.get((p, bool(f)), frozenset())
        if callable(v):
            v = spans[(p, bool(f))] = v()
        out[p] = v
    return out


def visual_near_misses(haystack: str, phrases: tuple[tuple[str, bool], ...]) -> dict[str, tuple[int, bool, str] | None]:
    """`visual_near_miss()` for several (phrase, fuzzy) at once — one scan
    of the stream per view for the whole set. Returns {phrase: result}."""
    return _memo_batch(_tls_store("vnm"), _tls_store("vnm_spans"), haystack, phrases, _scan_phrases)


def relaxed_visual_spans(haystack: str, phrases: tuple[tuple[str, bool], ...]) -> dict[str, frozenset[tuple[int, int]]]:
    """{phrase: token spans of `haystack` within the phrase's visual
    budget + STACK_EXTRA (share rule applied at that level)}, from the
    same scan; empty for a short entry (N3) and for a phrase found
    exactly (the stacked rule is never consulted for it)."""
    visual_near_misses(haystack, phrases)
    return _materialised(_tls_store("vnm_spans").get(haystack, {}), phrases)


def visual_spans(haystack: str, phrase: str, fuzzy: bool = False) -> frozenset[tuple[int, int]]:
    return relaxed_visual_spans(haystack, ((phrase, bool(fuzzy)),))[phrase]


MAX_RELAXED_CONFIRMS = 32  # per phrase: scan hits beyond the budget proper confirmed for the stacked rule


def _scan_phrases(haystack: str, phrases: tuple[tuple[str, bool], ...]):
    """-> ({(phrase, fuzzy): best}, {(phrase, fuzzy): relaxed spans}); see
    `visual_near_misses` / `relaxed_visual_spans`. The packed scan runs
    at budget + STACK_EXTRA and reports a lower bound of each hit's
    distance: a hit within the budget proper is confirmed and judged as
    before (`best`); a hit beyond it is only a relaxed candidate, skipped
    once the phrase is found within budget (the stacked rule is then
    never consulted) or after MAX_RELAXED_CONFIRMS such hits (bounded
    work: the stacked rule needs one window), else confirmed at budget +
    STACK_EXTRA with the share rule at that level and recorded as a span
    (at most MAX_RELAXED_SPANS). Cost (fix wave 8, N7-7): a pattern whose
    phrase is decided is switched off in the running scan (`_Pack.scan`
    `live`), a view whose stream equals an earlier one's scans only the
    patterns it has not seen, a hit that cannot beat the best window
    found (its distance is a lower bound) is skipped, and one DP per hit
    confirms every candidate start (`_osa_suffixes`)."""
    toks = _canonical_tokens(haystack)
    specs: list[tuple[tuple[str, bool], str, int, bool]] = []  # (key, squashed, raw letters, fuzzy)
    result: dict[tuple[str, bool], tuple[int, bool, str] | None] = {}
    spans: dict[tuple[str, bool], set[tuple[int, int]]] = {}
    relaxed_left: dict[tuple[str, bool], int] = {}
    for phrase, fuzzy in phrases:
        p = canonical(phrase)
        result[(phrase, fuzzy)] = None
        spans[(phrase, fuzzy)] = set()
        relaxed_left[(phrase, fuzzy)] = 0 if _short_entry(phrase, fuzzy) else MAX_RELAXED_CONFIRMS  # N3
        if p:
            sq = p.replace(" ", "")
            specs.append(((phrase, fuzzy), sq, len(sq), fuzzy))
    if not specs or not toks:
        return result, {k: frozenset(v) for k, v in spans.items()}
    # (text tail, pattern, bound) -> distances of every window ending there (`_osa_suffixes`); the
    # key is plain text, so it holds across views, and repeated text repeats tails
    dist_memo: dict[tuple[str, str, int], list[int | None]] = {}
    share_memo: dict[tuple[str, tuple, int], bool] = {}
    # patterns already scanned against an identical stream (views often leave the text unchanged:
    # a text without the pairs of VISUAL_EXTENDED reads the same in both of its orders): a second
    # scan of the same (stream, pattern, budgets) finds exactly the same windows
    scanned: dict[tuple, set[tuple]] = {}
    views = _views_for("")
    streams = [_stream(haystack, rules) for rules in views]
    # relaxed hits, recorded and confirmed lazily (`_materialised`; fix wave 9, M1)
    pending: dict[tuple[str, bool], list[tuple]] = {}
    bits: dict[tuple[str, bool], list[tuple[list[int], int]]] = {}

    def switch_off(key, which: int) -> None:
        for live, bit in bits.get(key, ()):
            for w in range(which, 2):
                live[w] &= ~bit

    def entries_for(vi: int, pool: set | None) -> list[tuple]:
        """This view's (spec index, variant, k, kx, word skeletons) per phrase not yet read."""
        rules = views[vi]
        out = []
        for i, (key, sq, raw, fz) in enumerate(specs):
            if result[key] is not None and result[key][1]:
                continue  # read as the phrase in an earlier view: nothing left to find
            # Two skeletons per phrase against the collapsed text: the phrase collapsed ("geeet" ~ "get"
            # at no cost) and as written (an insertion that splits the phrase's own double, "frete" for
            # "free", is one edit against "free" but two against "fre"). The budget is the collapsed
            # skeleton's tier either way.
            pskel = _phrase_skel(key[0], rules)  # the phrase through the same transform as the text
            k = visual_budget(len(pskel), fz, raw)
            extra = STACK_EXTRA if relaxed_left[key] > 0 and not (result[key] is not None and result[key][0] == 0) else 0
            pwords = canonical(key[0]).split()
            for collapse in (True, False):
                variant = _phrase_skel(key[0], rules, collapse)
                if not collapse and variant == pskel:
                    continue
                ent = (i, variant, k, k + extra,
                       tuple((_phrase_skel(w, rules, collapse), _phrase_skel(w, rules, False)) for w in pwords))
                if pool is not None:
                    if ent[:4] in pool:
                        continue
                    pool.add(ent[:4])
                out.append(ent)
        return out

    for vi in range(len(views)):
        S, ST, EN, tok = streams[vi]
        work = entries_for(vi, scanned.setdefault(streams[vi], set()))
        if not work:
            continue
        pats = [(e[1], e[2], e[3]) for e in work]
        packs = _packs(pats)
        # each key's patterns as (live masks of its pack, its bit): switched off in the running scans
        # once the key is decided (read as the phrase) or wants no more relaxed candidates
        lives = []
        for group, pack in packs:
            live = [pack.TOPS, pack.TOPS]
            lives.append(live)
            for gi, pidx in enumerate(group):
                bits.setdefault(specs[work[pidx][0]][0], []).append((live, pack.bit(gi)))
        for (group, pack), live in zip(packs, lives):
            for j, gi, dscan in pack.scan(S, ST, EN, live):
                pidx = group[gi]
                idx, pskel, k, kx, wsk = work[pidx]
                key, sq, _, _ = specs[idx]
                best = result[key]
                if best is not None and best[1]:
                    switch_off(key, 0)
                    continue
                exact_only = best is not None and best[0] == 0  # only a READING could still improve on it
                relaxed = dscan > k  # beyond the budget proper for sure (the scan's distance is a lower bound)
                if best is not None and 0 < best[0] <= dscan:
                    continue  # no window ending here can be closer than the one found (a lower bound)
                m = len(pskel)
                starts = [s0 for s0 in range(max(0, j - m - kx), min(j, j - m + kx + 1)) if ST[s0]]
                if relaxed:
                    if best is not None or relaxed_left[key] <= 0:
                        # found within budget (the stacked rule is never consulted for it), or enough
                        switch_off(key, 1)
                        continue
                    # at most MAX_RELAXED_CONFIRMS relaxed hits per phrase (bounded work, see the
                    # docstring); a relaxed hit only ever yields spans (its windows are all beyond the
                    # budget, never a `best`), so it is recorded here and confirmed only if the stacked
                    # rule asks for this phrase (`_materialised`; fix wave 9, M1)
                    relaxed_left[key] -= 1
                    pending.setdefault(key, []).append((S, tok, pskel, k, kx, wsk, j, starts))
                    continue
                if not exact_only:
                    t0 = max(0, j - m - kx)
                    mk = (S[t0:j], pskel, kx)
                    dists = dist_memo.get(mk)
                    if dists is None:
                        dists = dist_memo[mk] = _osa_suffixes(mk[0], pskel, kx)
                for s0 in starts:
                    w = S[s0:j]
                    if exact_only:
                        if w != pskel:
                            continue
                        d = 0
                    else:
                        d = dists[j - s0]
                        if d is None:
                            continue
                        total = k if d <= k else kx
                        if d >= 2 and len(wsk) > 1:
                            sk = (w, wsk, total)
                            okay = share_memo.get(sk)
                            if okay is None:
                                okay = share_memo[sk] = _word_shares_ok(w, wsk, total)
                            if not okay:
                                continue  # the edits are one whole short word swapped for another
                        if d > k:
                            if len(spans[key]) < MAX_RELAXED_SPANS:
                                spans[key].add((tok[s0], tok[j - 1] + 1))
                            continue
                    lo, hi = tok[s0], tok[j - 1] + 1
                    if d <= kx and len(spans[key]) < MAX_RELAXED_SPANS:
                        spans[key].add((lo, hi))
                    window = " ".join(toks[lo:hi])
                    reads = d == 0 and _reads_as_phrase(window, sq)
                    cand = (d, reads, window)
                    if best is None or (cand[0], not cand[1]) < (best[0], not best[1]):
                        best = cand
                        exact_only = d == 0
                        if exact_only:
                            switch_off(key, 1)  # found exactly: the stacked rule is never consulted
                        if reads:
                            switch_off(key, 0)
                            break
                result[key] = best
    for key, best in result.items():
        if best is not None and best[0] == 0:
            spans[key] = set()  # found exactly: the stacked rule is never consulted
    out: dict = {k: frozenset(v) for k, v in spans.items()}
    for key, recs in pending.items():
        if not (result[key] is not None and result[key][0] == 0):
            out[key] = functools.partial(_confirm_relaxed_visual, recs, spans[key], dist_memo, share_memo)
    return result, out


def _confirm_relaxed_visual(recs, found: set, dist_memo: dict, share_memo: dict) -> frozenset[tuple[int, int]]:
    """The recorded relaxed hits of one phrase, confirmed as `_scan_phrases`
    confirmed them before fix wave 9: every window within budget +
    STACK_EXTRA (share rule at that level) is a span, at most
    MAX_RELAXED_SPANS."""
    spans = set(found)
    for S, tok, pskel, k, kx, wsk, j, starts in recs:
        m = len(pskel)
        t0 = max(0, j - m - kx)
        mk = (S[t0:j], pskel, kx)
        dists = dist_memo.get(mk)
        if dists is None:
            dists = dist_memo[mk] = _osa_suffixes(mk[0], pskel, kx)
        for s0 in starts:
            d = dists[j - s0]
            if d is None:
                continue
            w = S[s0:j]
            if d >= 2 and len(wsk) > 1:
                sk = (w, wsk, kx)
                okay = share_memo.get(sk)
                if okay is None:
                    okay = share_memo[sk] = _word_shares_ok(w, wsk, kx)
                if not okay:
                    continue
            if len(spans) < MAX_RELAXED_SPANS:
                spans.add((tok[s0], tok[j - 1] + 1))
    return frozenset(spans)


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


@functools.lru_cache(maxsize=16)
def _word_keys(text: str) -> tuple[str, ...]:
    return tuple(word_key(t) for t in _canonical_tokens(text))


@functools.lru_cache(maxsize=16)
def _word_key_positions(text: str) -> dict[str, tuple[int, ...]]:
    """{word key: its token positions} (fix wave 9, M1: found by a scan of
    every token per phrase)."""
    out: dict[str, list[int]] = {}
    for i, k in enumerate(_word_keys(text)):
        out.setdefault(k, []).append(i)
    return {k: tuple(v) for k, v in out.items()}


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
    keys = _word_keys(haystack)
    best: tuple[int, int] | None = None
    for start in _word_key_positions(haystack).get(pkeys[0], ()):
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


@functools.lru_cache(maxsize=16384)
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


@functools.lru_cache(maxsize=65536)
def letter_share(piece: str, word: str) -> float:
    """The fraction of `word`'s letters (as a multiset) that `piece` also
    has: letter_share("munny", "money") == 0.6 (m, n, y)."""
    if not word:
        return 0.0
    have = Counter(piece)
    return sum(min(have[c], k) for c, k in Counter(word).items()) / len(word)


_PH_INITIAL = (("kn", "n"), ("gn", "n"), ("pn", "n"), ("wr", "r"), ("wh", "w"), ("ps", "s"), ("x", "s"))
_SOFT = ("e", "i", "y")
# The first letter of a key, from the first letter of the word (fix wave 8,
# N7-7: prunes the run-of-tokens phonetic pass — a run whose first letter
# cannot start the phrase's key is never keyed). Every rule that decides
# the first key letter looks at the first letter and at most three more.
_FIRST_KEY: dict[str, str] = {
    "a": "A", "e": "A", "i": "A", "o": "A", "u": "A", "y": "A", "b": "P", "c": "KSX", "d": "TJ", "f": "F",
    "g": "KNJ", "h": "HAPKSXTJFNLMRWFTS0", "j": "J", "k": "KN", "l": "L", "m": "M", "n": "N", "p": "PFNSX",
    "q": "K", "r": "R", "s": "SXK", "t": "TX0", "v": "F", "w": "WR", "x": "SKX", "z": "S",
}


@functools.lru_cache(maxsize=65536)
def phonetic_key(word: str) -> str:
    """Simplified Metaphone-style key of a canonical word (see the block
    comment above): phonetic_key("phree") == phonetic_key("free") == "FR",
    phonetic_key("ritch") == phonetic_key("rich") == "RX"."""
    w = _AZ_ONLY.sub("", word)
    if not w:
        return ""
    w = _ph_initial(w)
    out: list[str] = []
    _ph_advance(w, 0, out, len(w))
    return collapse_runs("".join(out))


def _ph_initial(w: str) -> str:
    for src, dst in _PH_INITIAL:
        if w.startswith(src):
            return dst + w[len(src):]
    return w


def _ph_advance(w: str, i: int, out: list[str], stop: int) -> int:
    """The steps of `phonetic_key` over `w` (a-z, initial rule applied)
    that START before `stop`, appended to `out`; returns where the next
    step starts. A step at i reads w[i-1 .. i+3] only, so the steps that
    start before len(w) - 3 are final whatever is appended to `w` later
    (fix wave 9, M1: `_run_keys` keys a growing run of tokens without
    re-reading it)."""
    n = len(w)
    while i < stop:
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
    return i


def _vowel_count(w: str) -> int:
    return sum(1 for c in w if c in VOWELS)


@functools.lru_cache(maxsize=4096)
def _vis(word: str, vis: bool = True) -> str:
    """A token as the consonant and phonetic signals ALSO read it (fix
    wave 8, class B): the near-identical pairs contracted, rn -> m,
    cl -> d, vv -> w (`visual_skeleton`), so "rnunny" is judged as "munny"
    and "vvght" as "wght". Both readings are scanned, the phrase's words
    read the same way each time, because a genuine rn / cl ("grnteed",
    "miracle") must keep matching as written. `vis=False`: as written."""
    return visual_skeleton(word) if vis else word


READINGS = (False, True)  # the consonant / phonetic signals judge each token as written and contracted


@functools.lru_cache(maxsize=32)
def _skeleton_stream(text: str, vis: bool = False) -> tuple[str, bytes, bytes, tuple[int, ...], tuple[str, ...]]:
    """The consonant skeleton of every token (as written, or with the
    visual pairs contracted, `_vis`), concatenated, with token start / end
    flags and the token index per character (like `_stream`), plus the
    per-token skeletons. A #hashtag / @mention is not a word: its
    skeleton is empty (fix wave 8, N7-5: "pssv #ad incum" is "passive
    income" with the disclosure tag between the halves)."""
    toks = _canonical_tokens(text)
    skels = tuple("" if t[0] in "#@" else consonant_skeleton(_vis(t, vis)) for t in toks)
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


@functools.lru_cache(maxsize=32)
def _token_keys(text: str, vis: bool = False) -> tuple[str, ...]:
    return tuple(phonetic_key(_vis(t, vis)) for t in _canonical_tokens(text))


_AZ_ONLY = re.compile(r"[^a-z]+")


@functools.lru_cache(maxsize=32)
def _token_consonants(text: str, vis: bool = False) -> tuple[tuple[int, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Per token: its consonant count; for a token of 4+ a-z letters, the
    first letter of the key of any run of tokens it starts (every rule
    that decides the first key letter looks at most 3 letters ahead:
    "tsch"), else ''; the token as read (`_vis`); and its a-z letters
    (all `phonetic_key` reads of it)."""
    toks = tuple(_vis(t, vis) for t in _canonical_tokens(text))
    letters = tuple(_AZ_ONLY.sub("", t) for t in toks)
    return (tuple(len(t) - _vowel_count(t) for t in toks),
            tuple(phonetic_key(t[:4])[:1] if len(t) >= 4 else "" for t in letters), toks, letters)


@functools.lru_cache(maxsize=32)
def _key_positions(text: str, vis: bool = False) -> dict[str, tuple[int, ...]]:
    """{phonetic key: the token positions with it}. Once per text (N7-7)."""
    out: dict[str, list[int]] = {}
    for i, k in enumerate(_token_keys(text, vis)):
        out.setdefault(k, []).append(i)
    return {k: tuple(v) for k, v in out.items()}




def _run_starts(text: str, vis: bool, letter: str) -> tuple[int, ...]:
    """The token positions that can start a run of tokens whose joined key
    starts with `letter` (`_FIRST_KEY`, and a 4+-letter token's own first
    key letter). Per text and letter, once (fix wave 8, N7-7: the scan of
    every token per phrase was a third of a 99-phrase review)."""
    store = _tls_store("run_starts")
    per = store.get((text, vis))
    if per is None:
        per = store[(text, vis)] = {}
        while len(store) > 32:
            store.popitem(last=False)
    got = per.get(letter)
    if got is None:
        _, heads, _, letters = _token_consonants(text, vis)
        # a token without any a-z letter contributes nothing to a run's key: never pruned
        got = per[letter] = tuple(i for i, (h, t) in enumerate(zip(heads, letters))
                                  if (not h or h == letter) and (not t or letter in _FIRST_KEY[t[0]]))
    return got


def _short_entry(phrase: str, fuzzy: bool) -> bool:
    """N3: an entry of at most SHORT_ENTRY_LETTERS letters is exact-only
    unless the rulebook opted it in."""
    return len(canonical(phrase).replace(" ", "")) <= SHORT_ENTRY_LETTERS and not fuzzy


def _no_function_word(window: list[str], pwords: list[str]) -> bool:
    return not any(t in FUNCTION_WORDS and t not in pwords for t in window)




def skeleton_near_misses(haystack: str, phrases: tuple[tuple[str, bool], ...]) -> dict[str, tuple[int, str] | None]:
    """`skeleton_near_miss()` for several (phrase, fuzzy) at once: one
    bit-parallel scan of the text's skeleton stream per reading for the
    whole set."""
    return _memo_batch(_tls_store("snm"), _tls_store("snm_spans"), haystack, phrases, _scan_skeletons)


def relaxed_skeleton_spans(haystack: str, phrases: tuple[tuple[str, bool], ...]) -> dict[str, frozenset[tuple[int, int]]]:
    """{phrase: token spans of `haystack` whose consonant skeleton is
    within the phrase's skeleton budget + STACK_EXTRA under the guards
    (one token per phrase word, no foreign function word, the strict
    letter share — but not the vowel-drop evidence: the stacked rule
    supplies it)}, from the same scan; empty for a short entry (N3) and
    for a phrase found exactly."""
    skeleton_near_misses(haystack, phrases)
    return _materialised(_tls_store("snm_spans").get(haystack, {}), phrases)


def skeleton_spans(haystack: str, phrase: str, fuzzy: bool = False) -> frozenset[tuple[int, int]]:
    return relaxed_skeleton_spans(haystack, ((phrase, bool(fuzzy)),))[phrase]


def _scan_skeletons(haystack: str, phrases: tuple[tuple[str, bool], ...]):
    """-> ({(phrase, fuzzy): best}, {(phrase, fuzzy): relaxed spans}); see
    `skeleton_near_misses` / `relaxed_skeleton_spans`. Once per reading
    (READINGS: tokens as written, and with the visual pairs contracted),
    the closest window over both; relaxed candidates bounded as in
    `_scan_phrases`."""
    toks = _canonical_tokens(haystack)
    result: dict[tuple[str, bool], tuple[int, str] | None] = {key: None for key in phrases}
    spans: dict[tuple[str, bool], set[tuple[int, int]]] = {key: set() for key in phrases}
    relaxed_left: dict[tuple[str, bool], int] = {key: MAX_RELAXED_CONFIRMS for key in phrases}
    dist_memo: dict[tuple[str, str, int], list[int | None]] = {}  # as in `_scan_phrases`
    scanned: dict[tuple, set[tuple]] = {}  # as in `_scan_phrases`
    streams = {vis: _skeleton_stream(haystack, vis) for vis in READINGS}
    pending: dict[tuple[str, bool], list[tuple]] = {}  # relaxed hits, confirmed lazily (fix wave 9, M1)

    def entries_for(vis: bool, pool: set | None) -> list[tuple]:
        out = []
        for phrase, fuzzy in phrases:
            pwords = canonical(phrase).split()
            key = (phrase, fuzzy)
            if not pwords or _short_entry(phrase, fuzzy) or (result[key] is not None and result[key][0] == 0):
                continue
            wskel = tuple(consonant_skeleton(_vis(w, vis)) for w in pwords)
            pskel = "".join(wskel)
            k = skeleton_budget(len(pskel))
            ent = (key, wskel, pskel, k, k + (STACK_EXTRA if relaxed_left[key] > 0 else 0), vis)
            if pool is not None:
                if ent[:5] in pool:
                    continue  # the same words against the same stream in the other reading: nothing new
                pool.add(ent[:5])
            out.append(ent)
        return out

    for vis in READINGS:
        S, ST, EN, tok, skels = streams[vis]
        work = entries_for(vis, scanned.setdefault(streams[vis], set()))
        if not work or not S:
            continue
        pats = [(e[2], e[3], e[4]) for e in work]
        for group, pack in _packs(pats):
            live = [pack.TOPS, pack.TOPS]
            for j, gi, dscan in pack.scan(S, ST, EN, live):
                key, wskel, pskel, k, kx, pvis = work[group[gi]]
                best = result[key]
                if best is not None and best[0] == 0:
                    live[0] &= ~pack.bit(gi)
                    continue
                if best is not None and best[0] <= dscan:
                    continue  # no window ending here can be closer than the one found (a lower bound)
                m = len(pskel)
                t0 = max(0, j - m - kx)
                starts = [s0 for s0 in range(t0, min(j, j - m + kx + 1)) if ST[s0]]
                if dscan > k:
                    if best is not None or relaxed_left[key] <= 0:
                        live[1] &= ~pack.bit(gi)  # found (the stacked rule is never consulted), or enough
                        continue
                    relaxed_left[key] -= 1  # a relaxed hit: bounded, recorded and confirmed lazily as in `_scan_phrases`
                    pending.setdefault(key, []).append((streams[vis], pskel, k, kx, pvis, j, starts))
                    continue
                pwords = canonical(key[0]).split()
                mk = (S[t0:j], pskel, kx)
                dists = dist_memo.get(mk)
                if dists is None:
                    dists = dist_memo[mk] = _osa_suffixes(mk[0], pskel, kx)
                for s0 in starts:
                    d = dists[j - s0]
                    if d is None:
                        continue
                    if d <= k and best is not None and d >= best[0]:
                        continue
                    lo, hi = tok[s0], tok[j - 1] + 1
                    words_here = [(t, ts) for t, ts in zip(toks[lo:hi], skels[lo:hi]) if t[0] not in "#@"]  # tags are not words
                    window = [t for t, _ in words_here]
                    if not _no_function_word(window, pwords):
                        continue
                    if d >= 1:
                        if len(window) != len(pwords):
                            continue
                        okay, strict, dropped = True, True, False
                        for (t, ts), w in zip(words_here, pwords):
                            # fix wave 8 (class B): a token that dropped vowels is evidence whether or not
                            # its skeleton still equals the word's ("grnteed" for "guaranteed" does); a
                            # drop keeps the consonants ("recommend" for "recommended" is an inflection)
                            if _vowel_count(t) < _vowel_count(w) and len(t) - _vowel_count(t) >= len(w) - _vowel_count(w):
                                dropped = True
                            ws = consonant_skeleton(_vis(w, pvis))
                            if ts != ws and letter_share(ts, ws) < WORD_SHARE:
                                # a two-consonant skeleton cannot keep 60% after one edit ("lz" for
                                # "ls"): one edit is tolerated by the signal proper — which also needs
                                # the vowel-drop evidence — when the token SOUNDS like the word ("luze"
                                # / "lose"; not "made" / "money": "fragrance free, made" is not "free
                                # money"); a relaxed span keeps the strict share ("mn" is not "mk")
                                strict = False
                                if d > k or not (len(ws) <= 2 and _osa_within(ts, ws, 1) is not None
                                                 and any(phonetic_key(_vis(t, v)) == phonetic_key(_vis(w, v))
                                                         for v in READINGS)):
                                    okay = False
                                    break
                        if not okay:
                            continue
                        if strict and len(spans[key]) < MAX_RELAXED_SPANS:
                            spans[key].add((lo, hi))
                        if d > k or not dropped:
                            continue
                    best = (d, " ".join(window))
                    if d == 0:
                        break
                result[key] = best
    for key, best in result.items():
        if best is not None and best[0] == 0:
            spans[key] = set()
    out: dict = {k: frozenset(v) for k, v in spans.items()}
    for key, recs in pending.items():
        if not (result[key] is not None and result[key][0] == 0):
            out[key] = functools.partial(_confirm_relaxed_skeleton, toks, key, recs, spans[key], dist_memo)
    return result, out


def _confirm_relaxed_skeleton(toks, key, recs, found: set, dist_memo: dict) -> frozenset[tuple[int, int]]:
    """The recorded relaxed hits of one phrase, confirmed as `_scan_skeletons`
    confirmed them before fix wave 9: a window of one token per phrase
    word, no foreign function word, every edited word keeping its strict
    letter share, is a span, at most MAX_RELAXED_SPANS."""
    spans = set(found)
    pwords = canonical(key[0]).split()
    for stream, pskel, k, kx, pvis, j, starts in recs:
        S, ST, EN, tok, skels = stream
        m = len(pskel)
        t0 = max(0, j - m - kx)
        mk = (S[t0:j], pskel, kx)
        dists = dist_memo.get(mk)
        if dists is None:
            dists = dist_memo[mk] = _osa_suffixes(mk[0], pskel, kx)
        for s0 in starts:
            d = dists[j - s0]
            if d is None:
                continue
            lo, hi = tok[s0], tok[j - 1] + 1
            words_here = [(t, ts) for t, ts in zip(toks[lo:hi], skels[lo:hi]) if t[0] not in "#@"]
            window = [t for t, _ in words_here]
            if not _no_function_word(window, pwords) or len(window) != len(pwords):
                continue
            strict = True
            for (t, ts), w in zip(words_here, pwords):
                ws = consonant_skeleton(_vis(w, pvis))
                if ts != ws and letter_share(ts, ws) < WORD_SHARE:
                    strict = False
                    break
            if strict and len(spans) < MAX_RELAXED_SPANS:
                spans.add((lo, hi))
    return frozenset(spans)


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
    not reported (it is an exact match, not a respelling). Tokens are
    read as written and with the visual pairs contracted (READINGS)."""
    for vis in READINGS:
        hit = _phonetic_near_miss(haystack, phrase, fuzzy, max_gap, vis)
        if hit is not None:
            return hit
    return None


RUN_MAX_TOKENS = 8  # runs of tokens keyed once per text (fix wave 9, M1): up to this many tokens ...
RUN_MAX_CHARS = 64  # ... and this many letters; a longer phrase takes the per-phrase loop


def _run_keys(text: str, vis: bool, letter: str, lim: int, cmax: int, cmin: int,
              targets: frozenset[str]) -> dict[str, list[tuple[int, int, int, int, str]]]:
    """{phonetic key: [(start, end, letters, consonants, joined), ...] in
    (start, end) order} for every key in `targets` and every run of up to
    RUN_MAX_TOKENS tokens with that key, starting at a `_run_starts(letter)`
    token, until the run has more than `lim` letters or `cmax` consonants
    (a run with fewer than `cmin` is skipped) — what the per-phrase loop of
    `_phonetic_near_miss` computed for every phrase, computed ONCE per
    (text, reading, first letter) for all the phrases of a batch (fix wave
    9, AEGIS round 8 M1: that loop was 0.6 s of a 100-phrase review).
    The key of a growing run is built incrementally (`_ph_advance`), and a
    start is abandoned as soon as the final part of its key is no prefix
    of any target. A later call with limits or a target outside the
    stored ones recomputes with the union."""
    k = (text, vis, letter)
    store = _tls_store("run_keys")
    got = store.get(k)
    if got is not None and got[0] >= lim and got[1] >= cmax and got[2] <= cmin and targets <= got[3]:
        return got[4]
    if got is not None:
        lim, cmax, cmin, targets = max(lim, got[0]), max(cmax, got[1]), min(cmin, got[2]), targets | got[3]
    # every prefix of a target -> the fewest key letters still to come for some target with it
    prefixes: dict[str, int] = {}
    for t in targets:
        for i in range(len(t) + 1):
            prefixes[t[:i]] = min(prefixes.get(t[:i], 99), len(t) - i)
    cons, _, vtoks, letters = _token_consonants(text, vis)
    n = len(vtoks)
    # the written reading's runs, reused for the contracted reading wherever a run's tokens read the same in
    # both (fix wave 9, M1): only runs through a token holding rn / cl / vv are keyed again
    reuse = None
    if vis:
        base = store.get((text, False, letter))
        if base is not None and base[0] >= lim and base[1] >= cmax and base[2] <= cmin and targets <= base[3]:
            wtoks = _token_consonants(text, False)[2]
            pre = [0] * (n + 1)
            for t in range(n):
                pre[t + 1] = pre[t] + (vtoks[t] != wtoks[t])
            reuse = (base[5], pre)
    out: dict[str, list[tuple[int, int, int, int, str]]] = {}
    by_start: dict[int, list[tuple[str, tuple[int, int, int, int, str]]]] = {}
    # a start whose FIRST token already commits a key part that no target begins with has no run to key
    # (the loop below would stop at its first token): decided once per distinct token
    opens: dict[str, bool] = {}
    for start in _run_starts(text, vis, letter):
        first = letters[start]
        if len(first) >= 2:
            ok = opens.get(first)
            if ok is None:
                w0 = _ph_initial(first)
                part: list[str] = []
                _ph_advance(w0, 0, part, len(w0) - 3)
                ok = opens[first] = not part or collapse_runs("".join(part)) in prefixes
            if not ok:
                by_start[start] = []
                continue
        if reuse is not None:
            stop = min(n, start + RUN_MAX_TOKENS)
            if reuse[1][stop] == reuse[1][start]:
                got_here = [(key, ent) for key, ent in reuse[0].get(start, ()) if key in targets]
                for key, ent in got_here:
                    out.setdefault(key, []).append(ent)
                by_start[start] = got_here
                continue
        here: list[tuple[str, tuple[int, int, int, int, str]]] = []
        joined = ""
        c = 0
        raw = ""  # the run's a-z letters
        w = ""  # ... with the initial rule applied (fixed once two letters are known)
        ci, cout = 0, []  # the FINAL steps of the key so far (`_ph_advance`)
        done = ""  # collapse_runs of them: a prefix of the key of every longer run from this start
        for end in range(start, min(n, start + RUN_MAX_TOKENS)):
            joined += vtoks[end]
            c += cons[end]
            if len(joined) > lim or c > cmax:
                break
            if len(raw) < 2:
                raw += letters[end]
                w = _ph_initial(raw)
            else:
                w += letters[end]
            if len(raw) >= 2:
                before = len(cout)
                ci = _ph_advance(w, ci, cout, len(w) - 3)
                if len(cout) != before:
                    done = collapse_runs("".join(cout))
                    if done not in prefixes:
                        break  # no longer run from here can have a target key
            if c < cmin:
                continue
            if len(raw) >= 2 and prefixes[done] > 2 * (len(w) - ci) + 1:
                continue  # the unfinished steps (at most 3 letters + 1) cannot supply the key letters a target still needs
            if len(raw) >= 2:
                tail = cout.copy()
                _ph_advance(w, ci, tail, len(w))
                key = collapse_runs("".join(tail))
            else:
                key = phonetic_key(joined)
            if key in targets:
                ent = (start, end, len(joined), c, joined)
                out.setdefault(key, []).append(ent)
                here.append((key, ent))
        by_start[start] = here
    store[k] = (lim, cmax, cmin, targets, out, by_start)
    while len(store) > 64:
        store.popitem(last=False)
    return out


def _run_params(phrase: str, vis: bool, max_gap: int = ADJACENCY_GAP) -> tuple[str, str, int, int, int, int] | None:
    """(squashed, key, width, letter limit, consonant limit, fewest consonants) of the run
    loop for `phrase` in reading `vis`, or None if it takes the plain loop."""
    pwords = canonical(phrase).split()
    squashed = _vis("".join(pwords), vis)
    pkey = phonetic_key(squashed)
    width = len(pwords) + max_gap
    if not pkey or width > RUN_MAX_TOKENS or len(squashed) + 4 > RUN_MAX_CHARS:
        return None
    return squashed, pkey, width, len(squashed) + 4, len(squashed) - _vowel_count(squashed) + 3, len(pkey) - 2


def warm_phonetic_runs(haystack: str, phrases: tuple[tuple[str, bool], ...]) -> None:
    """Key the runs of `haystack` once for a whole batch of phrases: per
    reading and first key letter, to the largest limits any of them needs."""
    for vis in READINGS:
        need: dict[str, tuple[int, int, int, frozenset[str]]] = {}
        for phrase, fuzzy in phrases:
            if _short_entry(phrase, fuzzy):
                continue
            prm = _run_params(phrase, vis)
            if prm is None:
                continue
            _, pkey, _, lim, cmax, cmin = prm
            lo = need.get(pkey[0], (0, 0, cmin, frozenset()))
            need[pkey[0]] = (max(lo[0], lim), max(lo[1], cmax), min(lo[2], cmin), lo[3] | {pkey})
        for letter, (lim, cmax, cmin, targets) in need.items():
            _run_keys(haystack, vis, letter, lim, cmax, cmin, targets)


def _keyed_run(haystack: str, vis: bool, pwords: list[str], squashed: str, pkey: str, width: int, pcons: int) -> str | None:
    """The per-phrase run loop of `_phonetic_near_miss`, replayed on the
    runs whose key IS the phrase's (`_run_keys`): the same first window,
    the same stops (a run too long or with too many consonants ends its
    start's search; a run with too few is skipped; the phrase itself or a
    run with a function word ends it)."""
    toks = _canonical_tokens(haystack)
    lim = len(squashed) + 4
    stopped: set[int] = set()
    for start, end, letters, c, joined in _run_keys(haystack, vis, pkey[0], lim, pcons + 3, len(pkey) - 2,
                                                    frozenset((pkey,))).get(pkey, ()):
        if start in stopped:
            continue
        if end - start + 1 > width or letters > lim or c > pcons + 3:
            stopped.add(start)
            continue
        if c < len(pkey) - 2:
            continue
        window = list(toks[start:end + 1])
        if joined == squashed or not _no_function_word(window, pwords):
            stopped.add(start)
            continue
        return " ".join(window)
    return None


def _phonetic_near_miss(haystack: str, phrase: str, fuzzy: bool, max_gap: int, vis: bool) -> str | None:
    pwords = canonical(phrase).split()
    if not pwords or _short_entry(phrase, fuzzy):
        return None
    pkeys = [phonetic_key(_vis(w, vis)) for w in pwords]
    if any(not k for k in pkeys):
        return None
    toks = _canonical_tokens(haystack)
    keys = _token_keys(haystack, vis)
    best: tuple[int, int] | None = None
    for start in _key_positions(haystack, vis).get(pkeys[0], ()):
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
        squashed = _vis("".join(pwords), vis)
        pkey = phonetic_key(squashed)
        width = len(pwords) + max_gap
        pcons = len(squashed) - _vowel_count(squashed)
        if pkey and width <= RUN_MAX_TOKENS and len(squashed) + 4 <= RUN_MAX_CHARS:
            return _keyed_run(haystack, vis, pwords, squashed, pkey, width, pcons)
        cons, _, vtoks, _ = _token_consonants(haystack, vis)
        # a token of 4+ letters fixes the first key letter of any run it starts; any other token's first
        # letter bounds it (_FIRST_KEY): only the tokens that can start the phrase's key are tried
        for start in _run_starts(haystack, vis, pkey[0]):
            joined = ""
            c = 0
            for end in range(start, min(len(toks), start + width)):
                joined += vtoks[end]
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


# --- stacked respellings (fix wave 8; AEGIS round 7 class B) --------------------------------
#
# "grnteed retunrs", "overnlte sccss", "lose vvait fst" — a vowel drop, a
# homophone AND a lookalike in one phrase — were outside every single
# signal's budget. Two root causes are fixed above (the skeleton signal's
# vowel-drop evidence skipped the token that dropped them). On top, as
# required: when TWO of the three independent similarity signals — the
# visual gate (letter edits), the consonant skeleton, the phonetic key —
# each score within their budget + STACK_EXTRA on the SAME token window,
# that window is a human's call even if neither alone is within budget.
# The relaxed visual and skeleton windows come from the same packed scans
# as the signals themselves (`visual_spans`, `skeleton_spans`); the
# phonetic check (joined word keys within one edit, no foreign function
# word) is only made on those windows, so the cost is a few edit distances
# per phrase. N3 holds: a short entry is exact-only.


def _phonetic_within(haystack: str, span: tuple[int, int], pwords: list[str], k: int = STACK_EXTRA) -> bool:
    lo, hi = span
    toks = _canonical_tokens(haystack)
    window = list(toks[lo:hi])
    if not window or not _no_function_word(window, pwords):
        return False
    return any(_osa_within("".join(_token_keys(haystack, vis)[lo:hi]),
                           "".join(phonetic_key(_vis(w, vis)) for w in pwords), k) is not None for vis in READINGS)


def _dropped_vowels(t: str, w: str) -> bool:
    """`t` is `w` with vowels dropped: fewer vowels, no fewer consonants
    ("grnteed" for "guaranteed"; "recommend" for "recommended" is not)."""
    return _vowel_count(t) < _vowel_count(w) and len(t) - _vowel_count(t) >= len(w) - _vowel_count(w)


def _respelling_evidence(window: list[str], pwords: list[str]) -> bool:
    """The window shows a RESPELLING, not merely a nearby ordinary word:
    a token that dropped vowels (`_dropped_vowels`), a lookalike pair
    (rn / cl / vv) or an l for an i that the phrase word lacks; for a
    window that is not one token per word, a token without any vowel or
    with a pair. "target rich" and "doctors recommend" have none."""
    if len(window) == len(pwords):
        for t, w in zip(window, pwords):
            if _dropped_vowels(t, w):
                return True
            if any(pr in t and pr not in w for pr in ("rn", "cl", "vv")):
                return True
            if t.count("l") > w.count("l") and t.count("i") < w.count("i"):
                return True
        return False
    return any(_vowel_count(t) == 0 or any(pr in t for pr in ("rn", "cl", "vv")) for t in window)


def stacked_near_miss(haystack: str, phrase: str, fuzzy: bool = False) -> str | None:
    """The first token window of `haystack` on which at least two of the
    three signals (visual, consonant skeleton, phonetic) are each within
    their budget + STACK_EXTRA and which shows respelling evidence
    (`_respelling_evidence`), as text — or None."""
    pwords = canonical(phrase).split()
    if not pwords or _short_entry(phrase, fuzzy):
        return None
    v = visual_spans(haystack, phrase, fuzzy)
    sk = skeleton_spans(haystack, phrase, fuzzy)
    toks = _canonical_tokens(haystack)
    for span in sorted(v | sk):
        lo, hi = span
        window = list(toks[lo:hi])
        if window == pwords or not _respelling_evidence(window, pwords):
            continue  # the phrase itself (an exact match), or ordinary words near it
        score = (span in v) + (span in sk)
        if score < 2 and _phonetic_within(haystack, span, pwords):
            score += 1
        if score >= 2:
            return " ".join(window)
    return None


# --- symbols standing for words (fix wave 8; AEGIS round 7 N7-4; fix wave 9, round 8 M2) --------
#
# "make 💰", "make $$$ fast", "free 💸", "guaranteed 📈", "get 💎 quick"
# passed: canonical() turns a symbol into a space, so the phrase was simply
# missing a word. SYMBOL_LEXICON maps the pictographs and symbol runs that
# stand for a CONCEPT in marketing text to the words they stand for.
#
# Fix wave 9 (AEGIS round 8 M2): the wave-8 rule counted a symbol outside
# the lexicon only where it closed the statement, never across a line
# break, and never in another field — "make 💱 This budget myth", "risk
# ∅", "beat the 📈", "make\n💰", "make" in the caption + "💰" in the bio
# passed (17/41, 7/12, 12/12). Now (`symbol_stand_in`):
#   ANY symbol — a character that is neither a letter nor punctuation:
#   Unicode So / Sk / Sc / Sm and unassigned / private-use code points; a
#   run of them is one symbol — in the place of ONE word of a never-say
#   phrase, IMMEDIATELY next to the rest of the phrase (the phrase minus
#   that word, its words in order within the adjacency policy), is a
#   stand-in, a human's call — whatever follows it. A lexicon symbol whose
#   concepts include the word it stands for may sit within the adjacency
#   policy's gap, may stand for several words, and in a phrase of three or
#   more words may stand in with its first or last word left out ("no ⚖
#   fast": "lose weight fast"). Line breaks are read across. Clip Review also
#   reads the rule across every field boundary (zbc/clip_review.py).
# Not a stand-in: a currency sign attached to digits ("$20", "20$", "$40k")
# is a price; the words that ARE written must include a content word ("no
# 🎯", "your 💰" say nothing); a LEXICON symbol that names the written
# word beside it and not the missing one illustrates that word
# ("Guaranteed ✅ delivery", "Doctor 🩺 appointments", "$ money") — the
# one exception to "any symbol", measured on the round-8 corpus (without
# it, 2 of its 40 emoji captions were flagged). Cost of the rule on
# ordinary emoji text: an emoji right after a word that begins a never-say
# phrase is read as the phrase ("Get 🎟 tickets", "Free 🚚 shipping" go to
# a human) — measured in tests/test_fix_wave_9.py. The lexicon is a
# bounded, documented list; extend it here, not in the rulebook.

_MONEY = ("money", "cash", "dollars", "dollar", "rich", "wealth", "wealthy", "profit", "profits", "income",
          "returns", "earnings", "pay", "paid")
_GROWTH = ("growth", "returns", "gains", "profit", "profits", "success", "results", "up")
SYMBOL_LEXICON: dict[str, tuple[str, ...]] = {
    # money / wealth
    "💰": _MONEY, "💵": _MONEY, "💴": _MONEY, "💶": _MONEY, "💷": _MONEY, "💸": _MONEY, "🪙": _MONEY, "💲": _MONEY,
    "🤑": _MONEY, "🏦": _MONEY + ("bank",), "💳": _MONEY, "💹": _MONEY + _GROWTH,
    "$": _MONEY, "€": _MONEY, "£": _MONEY, "¥": _MONEY, "₹": _MONEY, "₿": _MONEY,
    # gems / wealth
    "💎": ("rich", "wealth", "wealthy", "gem", "gems", "diamond", "diamonds", "luxury", "premium"),
    "👑": ("rich", "wealth", "king", "queen", "royal", "premium"),
    # growth / gains
    "📈": _GROWTH, "🚀": _GROWTH + ("rocket", "fast", "quick"), "📊": _GROWTH, "🔥": ("fire", "hot", "fast", "quick"),
    "⚡": ("fast", "quick", "instant", "instantly"), "💨": ("fast", "quick"),
    # certainty
    "🔒": ("guaranteed", "guarantee", "secure", "safe", "locked", "lock"), "🔐": ("guaranteed", "guarantee", "secure", "safe"),
    "✅": ("guaranteed", "guarantee", "proven", "approved", "verified", "certified", "yes", "check"),
    "✔": ("guaranteed", "guarantee", "proven", "approved", "verified", "certified", "yes", "check"),
    "☑": ("guaranteed", "guarantee", "proven", "approved", "verified", "certified", "yes", "check"),
    "💯": ("guaranteed", "proven", "percent"), "🏆": ("success", "win", "winner", "winning", "best"),
    "🥇": ("success", "win", "winner", "winning", "best"),
    # free / no
    "🆓": ("free",), "🚫": ("no", "zero", "never"), "❌": ("no", "zero", "never"), "⛔": ("no", "zero", "never"),
    # health
    "💊": ("cure", "cures", "pill", "pills", "medicine", "drug"), "🩺": ("doctor", "doctors", "medical"),
    "⚕": ("doctor", "doctors", "medical"), "🧑": ("doctor",), "👨": ("doctor",), "👩": ("doctor",),
    "🔬": ("clinically", "scientifically", "proven", "lab"), "🧪": ("clinically", "scientifically", "proven", "lab"),
    "⚖": ("weight",), "🏃": ("fast", "quick", "run"), "😴": ("overnight", "sleep", "night"), "💤": ("overnight", "sleep", "night"),
    "🌙": ("overnight", "night"), "🛌": ("overnight", "sleep"),
}
_SYMBOL_CATEGORIES = ("So", "Sk", "Sc", "Sm", "Co", "Cn")


@functools.lru_cache(maxsize=65536)
def _is_symbol_char(ch: str) -> bool:
    """Neither a letter nor punctuation: a symbol (So/Sk/Sc/Sm) or an
    unassigned / private-use code point (fix wave 9, M2)."""
    return ch in SYMBOL_LEXICON or (unicodedata.category(ch) in _SYMBOL_CATEGORIES and not _invisible(ch))


_SYMBOL_SCAN = re.compile(r"[\w#@]+|[^\w#@\s]")


@functools.lru_cache(maxsize=16)
def _symbol_tokens(text: str) -> tuple[tuple[str, str, frozenset[str]], ...]:
    """The folded text as (kind, token, concepts): kind "w" for a word,
    "t" for a #hashtag / @mention, "s" for a run of symbols (concepts:
    the union of the lexicon's, empty for symbols it does not know), "n"
    for a currency sign attached to a number. Line breaks and punctuation
    are not tokens (fix wave 9: the rule reads across line breaks; one
    regex pass, M1)."""
    folded = _folded(text)
    n = len(folded)
    out: list[tuple[str, str, frozenset[str]]] = []
    run_a = run_b = -1

    def close_run() -> None:
        run = folded[run_a:run_b]
        price = (run_b < n and folded[run_b].isdigit()) or (run_a > 0 and folded[run_a - 1].isdigit())
        if price:
            out.append(("n", run, frozenset()))
        else:
            out.append(("s", run, frozenset(w for c in run for w in SYMBOL_LEXICON.get(c, ()))))

    for m in _SYMBOL_SCAN.finditer(folded):
        a, b = m.span()
        ch = folded[a]
        if b - a == 1 and not (ch.isalnum() or ch in "#@_"):
            if not _is_symbol_char(ch):
                continue
            if run_b == a:
                run_b = b  # the run goes on
                continue
            if run_a >= 0:
                close_run()
            run_a, run_b = a, b
            continue
        if run_a >= 0:
            close_run()
            run_a = run_b = -1
        out.append(("t" if ch in "#@" else "w", m.group(), frozenset()))
    if run_a >= 0:
        close_run()
    return tuple(out)


@functools.lru_cache(maxsize=16)
def _symbol_index(text: str) -> tuple[tuple[tuple[str, str, str | None, frozenset[str]], ...], dict[str, tuple[int, ...]]]:
    """`_symbol_tokens` with each word's `word_key`, and {word key: its
    token positions}: built once per text, shared by every phrase (M1)."""
    toks = tuple((kind, tok, word_key(tok) if kind == "w" else None, concepts)
                 for kind, tok, concepts in _symbol_tokens(text))
    pos: dict[str, list[int]] = {}
    for i, t in enumerate(toks):
        if t[0] == "w":
            pos.setdefault(t[2], []).append(i)
    return toks, {k: tuple(v) for k, v in pos.items()}


_ASCII_SYMBOLS = frozenset(ch for ch in map(chr, range(128)) if unicodedata.category(ch) in _SYMBOL_CATEGORIES)  # $ + < = > ^ ` | ~


def _has_symbol(text: str) -> bool:
    if text.isascii():
        return not _ASCII_SYMBOLS.isdisjoint(text)
    return any(_is_symbol_char(ch) for ch in set(text))


def symbol_stand_in(haystack: str, phrase: str, max_gap: int = ADJACENCY_GAP) -> str | None:
    """A description of how the multi-word `phrase` appears in `haystack`
    with a word (or, lexicon symbols, words) stood in by a symbol (see the
    block comment above), or None. Single-word phrases: None (a symbol
    alone says nothing)."""
    pwords = canonical(phrase).split()
    if len(pwords) < 2 or not _has_symbol(haystack):
        return None
    content = [i for i, w in enumerate(pwords) if w not in FUNCTION_WORDS]
    if not content:
        return None
    toks, key_pos = _symbol_index(haystack)
    pkeys = [word_key(w) for w in pwords]
    n = len(pwords)

    def step(pos: int, k: int, direction: int, tight: bool):
        """The token for phrase word k next to `pos` in `direction`: the
        word itself within the gap, a symbol right beside it, or a lexicon
        symbol naming it within the gap (`tight`: right beside only)."""
        reach = 1 if tight else max_gap + 1
        sym = None
        for d in range(1, reach + 1):
            j = pos + d * direction
            if j < 0 or j >= len(toks):
                break
            kind, _, key, concepts = toks[j]
            if kind == "w" and key == pkeys[k]:
                return j, "word"
            if kind == "s" and sym is None:
                if pwords[k] in concepts:
                    sym = (j, "known")
                elif d == 1:
                    sym = (j, "unknown")
        return sym

    def align(a: int, q: int, omit: int | None):
        hows: dict[int, tuple[int, str]] = {a: (q, "word")}
        for direction, order in ((1, range(a + 1, n)), (-1, range(a - 1, -1, -1))):
            pos, prev = q, "word"
            for k in order:
                if k == omit:
                    continue
                got = step(pos, k, direction, prev == "unknown")
                if got is None:
                    return None
                if got[1] == "unknown" and prev != "word":
                    return None  # an unknown symbol stands right beside a WRITTEN word of the phrase
                hows[k] = got
                pos, prev = got
        return hows

    for a in content:
        for q in key_pos.get(pkeys[a], ()):
            # a word left out entirely: only the first or the last (the rest is a contiguous part of the phrase)
            for omit in (None, *(i for i in (0, n - 1) if i != a and n >= 3)):
                hows = align(a, q, omit)
                if hows is None:
                    continue
                kinds = {k: h for k, (_, h) in hows.items()}
                unknown = [k for k, h in kinds.items() if h == "unknown"]
                known = [k for k, h in kinds.items() if h == "known"]
                if not unknown and not known:
                    continue  # the phrase's own words
                if len(unknown) + (omit is not None) > 1 or (omit is not None and not known):
                    continue  # more than the phrase minus one word is missing
                illustrates = False
                for k in unknown:
                    concepts = toks[hows[k][0]][3]
                    beside = [pwords[k2] for k2 in (k - 1, k + 1) if kinds.get(k2) == "word"]
                    if concepts and any(w in concepts for w in beside):
                        illustrates = True  # a lexicon symbol naming the written word beside it
                if illustrates:
                    continue
                parts = []
                for k in sorted(hows):
                    j, h = hows[k]
                    if h == "known":
                        parts.append(f"{toks[j][1]!r} standing for {pwords[k]!r}")
                    elif h == "unknown":
                        parts.append(f"{toks[j][1]!r} where {pwords[k]!r} would be")
                lo, hi = min(j for j, _ in hows.values()), max(j for j, _ in hows.values())
                return "; ".join(parts) + f" ({' '.join(t[1] for t in toks[lo:hi + 1])!r})"
    return None


def symbol_fragment_at_edges(text: str, phrase: str) -> str | None:
    """The phrase minus one word, written as the first or the last words
    of `text` (a #tag skipped), or None — Clip Review's check for a field
    that is nothing but a symbol (fix wave 9, M2: "make ..." in the
    caption + "💰" as the bio; fields have no reading order). The words
    written must include a content word."""
    pwords = canonical(phrase).split()
    if len(pwords) < 2:
        return None
    toks, _ = _symbol_index(text)
    words = [t for t in toks if t[0] == "w"]
    if not words:
        return None
    pkeys = [word_key(w) for w in pwords]
    for i in range(len(pwords)):
        frag = pkeys[:i] + pkeys[i + 1:]
        if not any(w not in FUNCTION_WORDS for w in pwords[:i] + pwords[i + 1:]):
            continue
        k = len(frag)
        for edge in (words[:k], words[-k:]):
            if len(edge) == k and [t[2] for t in edge] == frag:
                return f"{' '.join(t[1] for t in edge)!r} with {pwords[i]!r} missing"
    return None


_TAG_RUN = re.compile(r"[#@][\w#@]*")
_ALNUM = re.compile(r"[^\W_]")


def symbol_only(text: str) -> bool:
    """`text` has symbols and nothing else a reader takes as words (#tags
    and punctuation aside; a letter-like symbol is a letter)."""
    if not text or not _has_symbol(text):
        return False
    t = _TAG_RUN.sub(" ", _map_letterlike(text))
    return _ALNUM.search(t) is None and _has_symbol(t)


def mixed_symbol_words(text: str, limit: int = 5) -> list[str]:
    """Words that mix letters with symbols or digits (rule (b), fix wave 4):
    e.g. "return$", "G€t", "6et", "t@lks", "mp4". Ordinary text is left
    alone: surrounding punctuation is ignored; apostrophes, hyphens, periods
    and ampersands between letters ("don't", "co-op", "U.S.", "R&D");
    letters-only #hashtags / @mentions; numbers, prices and numbers with a
    unit suffix ("$20", "1,200", "2nd", "1990s", "9am", "$40k", "1080p")."""
    out: list[str] = []
    # a divider (Rule B, fix wave 11: one box-drawing / block / geometric character repeated) is a space
    for raw in _WORD_SPLIT.split(_DIVIDER.sub(" ", _drop_format(_nfkc_words(text)))):
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
    view = _letters_view(text)
    if view.isascii():
        return []
    want = {ch for ch in set(view) if ch.isalpha() and not ch.isascii() and ch not in _LATIN1_LETTERS
            and ch not in _FOLDS and ch.casefold() not in _FOLDS and _script(ch) == "LATIN"}
    for ch in view if want else ():
        if ch in want:
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
    view = _letters_view(text)
    if view.isascii():
        return []
    want = {ch for ch in set(view) if ch.isalpha() and _script(ch) != "LATIN"}
    for ch in view if want else ():
        if ch in want:
            found.setdefault(ch, f"U+{ord(ch):04X} ({_script(ch).lower()})")
    return list(found.values())


# --- the fail-safe (fix wave 9, AEGIS round 8 H1 (c)) -------------------------------------------
#
# H1 was one symbol style nobody had mapped. Whatever style comes next
# (Braille patterns, box drawing, private-use glyphs, a block added to
# Unicode later), text that canonical() STRIPS — characters that are
# neither letter nor digit once letter-like symbols are mapped — by more
# than STRIPPED_SHARE_LIMIT of its non-space characters is an obfuscation
# signal, so no style can ever produce an automatic pass. The share is
# taken over the whole field AND over every run of 1 to STRIPPED_WINDOW
# consecutive words holding at least STRIPPED_MIN characters that are
# stripped: a field-wide share alone is diluted by ordinary text around
# the styled words ("⡍⡁⡅⡑ ⡍⡕⡝⡑⡽ This budget myth. Listen on Pod Plus.
# #ad" strips 23% overall, 100% of its first two words); since fix wave 10
# (N9-7) also over any run of STRIPPED_MIN consecutive stripped characters
# on its own, so one long word glued to them cannot dilute the window. Not
# counted as stripped (ordinary text, measured on the round-8 corpus and the
# emoji captions): punctuation, currency / math / modifier symbols (a word
# made mostly of them, or of any symbol it cannot read, is Rule A's business
# since fix wave 11: `unreadable_words`, below), format and
# default-ignorable characters (their own signals cover them), the emoji
# keycap, anything in Latin-1, and the emoji / pictograph blocks (arrows,
# technical, geometric shapes, miscellaneous symbols, dingbats,
# supplemental arrows and symbols, U+1F000-U+1FAFF). Box drawing and block
# elements ARE counted (decorative separators such as "━━━━" go to a
# human: the documented cost of the fail-safe; since fix wave 11 a run of ONE
# repeated such character is a divider, Rule B, and not counted). 30%: a caption of
# pictographs strips 0%; a phrase in an unmapped style strips ~100%.
STRIPPED_SHARE_LIMIT = 0.30
STRIPPED_WINDOW = 4  # words
STRIPPED_MIN = 3  # stripped characters in a window before its share is judged
_PICTOGRAPH_RANGES = ((0x2190, 0x23FF), (0x25A0, 0x27BF), (0x2900, 0x297F), (0x2B00, 0x2BFF), (0x3030, 0x3030),
                      (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299), (0x1F000, 0x1FAFF))


@functools.lru_cache(maxsize=65536)
def _stripped(ch: str) -> bool:
    """Would canonical() strip `ch` (after the letter-like map) as a
    symbol it cannot read — and is it NOT an ordinary one?"""
    if ch.isspace() or ch.isalnum() or ch in "#@_" or ord(ch) < 0x100 or ch == "\u20e3" or _invisible(ch):
        return False
    cat = unicodedata.category(ch)
    if cat[0] in "PZ" or cat in ("Sc", "Sm", "Sk"):
        return False
    cp = ord(ch)
    return not any(lo <= cp <= hi for lo, hi in _PICTOGRAPH_RANGES)


def stripped_share(text: str) -> float:
    """The largest share of stripped characters (`_stripped`) over the
    whole of `text` and over every window of 1..STRIPPED_WINDOW
    consecutive words with at least STRIPPED_MIN stripped characters;
    letter-like symbols count as the letters they are, and a divider
    (Rule B, `_DIVIDER`) is not text. 0.0 if nothing is stripped. (Fix wave
    11: the wave-10 count of currency / math "symbol-lettered" words moved
    to Rule A, `unreadable_words`, which does not depend on any table.)"""
    if not text or text.isascii():
        return 0.0
    t = _DIVIDER.sub(" ", _map_letterlike(text))
    chars = set(t)
    bad = {ch for ch in chars if _stripped(ch)}
    if not bad:
        return 0.0
    words = t.split()
    sizes = [len(w) for w in words]
    counts = [sum(w.count(c) for c in bad) if not w.isascii() else 0 for w in words]
    total = sum(sizes)
    best = sum(counts) / total if total else 0.0
    if re.search(_char_class(bad) + "{%d}" % STRIPPED_MIN, t):
        # a run of STRIPPED_MIN consecutive stripped characters is a window of its own (fix wave 10,
        # N9-7): a styled phrase glued to a long ordinary word ("𝌀𝌁𝌂Supercalifragilistic...") no
        # longer dilutes a word window below the limit
        return 1.0
    for i in range(len(words)):
        n = s = 0
        for j in range(i, min(len(words), i + STRIPPED_WINDOW)):
            n += sizes[j]
            s += counts[j]
            if s >= STRIPPED_MIN and s / n > best:
                best = s / n
    return best


# --- Rule A: fail closed on unreadable symbols (fix wave 11, AEGIS round 10 N10-1) ----------------
#
# Design ruling (binding): the gate does not try to READ every symbol alphabet — round 10 spelled
# never-say phrases in arrows, math operators and technical symbols ("♏⍺⋊⋿ ♏○⋂⋿¥") that no table
# listed, and 549 more single-letter lookalikes are read as nothing. What it cannot read, it sends to
# a human. Reading tables (CURRENCY_MATH_LOOKALIKES, the regional reading) only ever UPGRADE that to
# a rejection; the fail-safe itself never depends on them.
#
# "Cannot read" (`_unreadable`): a code point of category So / Sm / Sc / Sk / Co / Cn (after the
# letter-like map) that is not ORDINARY. Ordinary:
#   * anything below U+0100 (Latin-1: $ £ ¥ © ® ° ± × ...), punctuation (P*: not a symbol category);
#     the keycap U+20E3 and VS15 / VS16 are marks, not symbols, so never counted either;
#   * a symbol NFKC turns into letters / digits or into Latin-1 (™ -> TM, ㎏ -> kg, № -> No, ﹩ -> $):
#     every reading of the gate reads it;
#   * emoji. Python has no Emoji_Presentation / Extended_Pictographic property and emoji-data.txt may not
#     be downloaded here, so this is a BLOCK APPROXIMATION, not the property: every code point of the
#     emoji blocks EMOJI_BLOCKS (Misc Symbols and Pictographs, Emoticons, Transport and Map, Supplemental
#     Symbols and Pictographs, Symbols and Pictographs Extended-A — unassigned code points included, so
#     an emoji newer than this Python's Unicode database is not "unreadable"), plus EMOJI_ELSEWHERE: the
#     code points emoji-data gives an emoji property in blocks that are otherwise NOT ordinary (Arrows,
#     Misc Technical, Enclosed Alphanumerics, Geometric Shapes, Miscellaneous Symbols, Dingbats,
#     Supplemental Arrows-B, Misc Symbols and Arrows, CJK) — the design ruling's list, written out
#     below. Miscellaneous Symbols and Dingbats are NOT ordinary as whole blocks (fail closed): they hold
#     letter-shaped symbols (☾ ❍ ♇ ❘ ✕ ⚬ ☉) that spelled "no cost" as "n❍ ☾❍∫✝" past every other rule;
#     the cost is decoration of two different non-emoji stars / notes / sparkles in one word ("★★★★☆",
#     "♪♫", "✧˖°" go to a human; "★★★★★" and "♪ song ♪" do not) — and the
#     emoji of the Mahjong / Playing Cards / Enclosed Alphanumeric and Ideographic Supplement / Geometric
#     Shapes Extended blocks (🀄 🃏 🆎 🆑-🆚 🈁 🈯 🟠-🟫 🟰) and the regional indicators (flags: read by
#     `regional_reading`).
# NOT ordinary (so a word made of them is unreadable): Arrows, Mathematical Operators, Miscellaneous
# Technical (APL), Letterlike Symbols, Enclosed Alphanumerics, Box Drawing / Block Elements, Geometric
# Shapes, Miscellaneous Symbols, Dingbats (their emoji excepted), Misc Math Symbols, Supplemental Arrows,
# Braille cells that are no letter, private use and unassigned code points, and every other symbol.
#
# Rule A (`unreadable_words`): a word (whitespace-delimited) whose letters-and-unreadable-symbols
# sequence is at least half unreadable symbols, or has 2 unreadable among any 3 consecutive, is
# "unreadable symbols" — a human's call. Digits, punctuation, marks, invisible characters and ordinary
# symbols are left out of that sequence, so they cannot dilute a word ("♏..⍺..⋊..⋿", "♏11⍺11⋊",
# "♏🔥🔥⍺🔥🔥⋊"), and the 3-window judges a styled run glued to a long word on its own
# ("♏⍺⋊⋿Supercalifragilistic"). Not a word: one unreadable symbol, alone or repeated, with no letter
# ("→", "∞", "≥", "€€€", "₹₹₹": at most one letter's worth, and no never-say word is one repeated
# letter), and a price ("₹499", "€1.5m").
# Rule B (`_DIVIDER`): a run of ONE Box Drawing / Block Elements / Geometric Shapes character repeated
# 3+ times ("━━━━━━", "▬▬▬", "■■■") is a divider: blanked before Rule A and the stripped-share fail-safe.
# A run mixing different such characters ("╔══") still counts.
EMOJI_BLOCKS = ((0x1F300, 0x1F5FF), (0x1F600, 0x1F64F), (0x1F680, 0x1F6FF), (0x1F900, 0x1F9FF), (0x1FA70, 0x1FAFF))
# The design ruling's explicit list (emoji per Unicode emoji-data in the otherwise not-ordinary blocks):
_RULING_EMOJI = ("↔↕↖↗↘↙↩↪ ⌚⌛⌨ ⏏ ⏩-⏳ ⏸⏹⏺ Ⓜ ▪▫▶◀◻◼◽◾ ☀-☄ ☎☑☔☕☘☝☠☢☣☦☪☮☯☸☹☺♀♂♈-♓♟♠♣♥♦♨♻♾♿ "
                 "⚒-⚗⚙⚛⚜⚠⚡⚧⚪⚫⚰⚱⚽⚾⛄⛅⛈⛎⛏⛑⛓⛔⛩⛪⛰-⛵⛷-⛺⛽ ✂✅✈✉✊✋✌✍✏✒✔✖✝✡✨✳✴❄❇❌❎❓❔❕❗❣❤➕➖➗➡➰➿ "
                 "⤴⤵ ⬅⬆⬇⬛⬜⭐⭕ 〰〽㊗㊙")
# Emoji in the other symbol blocks of the Supplementary Multilingual Plane (emoji-data, same property):
_SMP_EMOJI = ((0x1F004, 0x1F004), (0x1F0CF, 0x1F0CF), (0x1F170, 0x1F171), (0x1F17E, 0x1F17F), (0x1F18E, 0x1F18E),
              (0x1F191, 0x1F19A), (0x1F1E6, 0x1F1FF), (0x1F201, 0x1F202), (0x1F21A, 0x1F21A), (0x1F22F, 0x1F22F),
              (0x1F232, 0x1F23A), (0x1F250, 0x1F251), (0x1F7E0, 0x1F7EB), (0x1F7F0, 0x1F7F0))


def _expand(spec: str) -> frozenset[str]:
    out: set[str] = set()
    parts = spec.replace(" ", "")
    i = 0
    while i < len(parts):
        if i + 2 < len(parts) and parts[i + 1] == "-":
            out.update(chr(cp) for cp in range(ord(parts[i]), ord(parts[i + 2]) + 1))
            i += 3
        else:
            out.add(parts[i])
            i += 1
    return frozenset(out)


EMOJI_ELSEWHERE = _expand(_RULING_EMOJI) | frozenset(chr(cp) for lo, hi in _SMP_EMOJI for cp in range(lo, hi + 1))
_UNREADABLE_CATEGORIES = frozenset(("So", "Sm", "Sc", "Sk", "Co", "Cn"))


@functools.lru_cache(maxsize=65536)
def _unreadable(ch: str) -> bool:
    """A symbol the gate cannot read (Rule A): So / Sm / Sc / Sk / Co / Cn, not ordinary (see above)."""
    cp = ord(ch)
    if cp < 0x100 or unicodedata.category(ch) not in _UNREADABLE_CATEGORIES:
        return False
    if any(lo <= cp <= hi for lo, hi in EMOJI_BLOCKS) or ch in EMOJI_ELSEWHERE:
        return False
    n = unicodedata.normalize("NFKC", ch)
    if n != ch and (any(c.isalnum() for c in n) or all(ord(c) < 0x100 for c in n)):
        return False
    return True


_DIVIDER = re.compile(r"([\u2500-\u25FF])\1{2,}")
UNREADABLE_WINDOW = 3  # consecutive letters / unreadable symbols of a word: 2 unreadable among them is a styled run


def _price(word: str) -> bool:
    """A number with at most one currency sign before or after it and an optional unit ("₹499",
    "€1.5m", "12,50€"): not a word of letters."""
    w = word.strip(_LEAD_STRIP + _TRAIL_STRIP)
    if w[:1] and unicodedata.category(w[0]) == "Sc":
        w = w[1:]
    elif w[-1:] and unicodedata.category(w[-1]) == "Sc":
        w = w[:-1]
    return bool(_NUMBER_WORD.fullmatch(w))


def unreadable_words(text: str, limit: int = 5) -> list[str]:
    """Rule A (fix wave 11, N10-1): the words of `text` made mostly of symbols the gate cannot read
    (`_unreadable`, after the letter-like map, dividers blanked) — see the comment above. Empty if none."""
    if not text or text.isascii():
        return []
    t = _map_letterlike(text)
    if not any(_unreadable(c) for c in set(t)):
        return []
    t = _DIVIDER.sub(" ", t)
    out: list[str] = []
    for word in t.split():
        seq = [_unreadable(c) for c in word if c.isalpha() or _unreadable(c)]
        n = sum(seq)
        if not n:
            continue
        if n == len(seq) and len({c for c in word if _unreadable(c)}) == 1:
            continue  # one symbol, alone or repeated, no letter: "→", "∞", "€€€"
        if _price(word):
            continue
        if 2 * n >= len(seq) or any(sum(seq[i:i + UNREADABLE_WINDOW]) >= 2 for i in range(len(seq) - 1)):
            out.append(word[:40])
            if len(out) >= limit:
                break
    return out


# Ordinary emoji that the signals below would otherwise read as evasion (fix wave 10, AEGIS round 9 N9-5):
# (1) the three emoji tag sequences Unicode defines as RGI flags — England, Scotland, Wales: U+1F3F4 WAVING
#     BLACK FLAG, the tag letters of gbeng / gbsct / gbwls, U+E007F CANCEL TAG. Any other tag sequence,
#     tag letters anywhere else, or anything glued on is still a signal;
# (2) an enclosed-letter emoji with its emoji presentation selector (VS16) standing alone as a word:
#     "🅿️ Free parking", "Ⓜ️ Two stops", "Blood types 🅰️🅱️🅾️" — every character of the word is an enclosed
#     letter followed by U+FE0F. Only the SIGNALS skip it: every phrase check still reads it as its letters,
#     so a never-say phrase spelled that way is still found (and rejected when exact).
_RGI_SUBDIVISION_FLAGS = tuple("\U0001F3F4" + "".join(chr(0xE0000 + ord(c)) for c in code) + "\U000E007F"
                               for code in ("gbeng", "gbsct", "gbwls"))
_ENCLOSED_LETTERS = frozenset(ch for ch in LETTERLIKE if 0x2460 <= ord(ch) <= 0x24FF or 0x1F100 <= ord(ch) <= 0x1F1FF)
_EMOJI_LETTER_WORD = re.compile("(?<!\\S)(?:" + _char_class(_ENCLOSED_LETTERS) + "\uFE0F)+(?=[\\s.,!?;:]|$)")


def _signal_view(text: str) -> str:
    """`text` for the obfuscation signals: the RGI subdivision flags and standalone enclosed-letter emoji
    (see above) replaced by a plain pictograph (U+1F3F4) — nothing else changes."""
    if not text or text.isascii():
        return text
    if "\U0001F3F4" in text:
        for flag in _RGI_SUBDIVISION_FLAGS:
            text = text.replace(flag, "\U0001F3F4")
    if "\uFE0F" in text:
        text = _EMOJI_LETTER_WORD.sub("\U0001F3F4", text)
    return text


def obfuscation_signals(text: str) -> list[str]:
    """Evasion patterns in `text` (empty list = none). Conservative on
    purpose: a false positive costs a human look, a false negative lets a
    never-say line through automatically. Judged on `_signal_view(text)`
    (fix wave 10, N9-5: three flags and standalone letter emoji are emoji)."""
    signals: list[str] = []
    text = _signal_view(text)
    styled = letterlike_chars(text)
    if styled:
        signals.append("letter-like symbol(s), i.e. styled letters (enclosed, squared, circled, mathematical, "
                       "fullwidth, small-capital, superscript/subscript, Braille): " + ", ".join(styled[:5]))
    share = stripped_share(text)
    if share > STRIPPED_SHARE_LIMIT:
        signals.append(f"{share:.0%} of the text is stripped by canonicalisation (symbols it cannot read as letters, "
                       f"over the {STRIPPED_SHARE_LIMIT:.0%} fail-safe): possibly an unmapped letter style")
    unreadable = unreadable_words(text)
    if unreadable:
        signals.append("unreadable symbols: word(s) made mostly of symbols the gate cannot read as letters "
                       "(fail-closed, fix wave 11 Rule A): " + ", ".join(repr(w) for w in unreadable))
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
    if len(raw.translate(_DROP_TABLE)) == len(raw) and not any(c in _BLANK_IGNORABLES for c in set(raw)):
        raw_has_invisible = False  # nothing to look for below (fix wave 9, M1)
    else:
        raw_has_invisible = True
    inv = [_invisible(ch) for ch in raw] if raw_has_invisible else []
    if raw_has_invisible:
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
    chars = set(t)
    has_latin = any(ch.isalpha() and _script(ch) == "LATIN" for ch in chars)
    lookalikes = sorted({ch for ch in chars if ch in _SIGNAL_CHARS})
    if lookalikes and has_latin:
        signals.append("mixed-script text: lookalike letter(s) "
                       + ", ".join(f"U+{ord(c):04X}" for c in lookalikes[:5]) + " among Latin letters")
    mixed = []
    if len({_script(ch) for ch in chars if ch.isalpha()}) > 1:  # one script in the text: no word mixes two
        for word in _NON_WORD.split(t):
            scripts = {_script(ch) for ch in word if ch.isalpha()}
            if len(scripts) > 1:
                mixed.append(f"{'+'.join(sorted(s.lower() for s in scripts))}")
    if mixed:
        signals.append("letters of more than one script inside one word (" + ", ".join(sorted(set(mixed))[:3]) + ")")
    forms = sorted({f"U+{ord(c):04X}" for c in set(text or "") if c in _COMPAT_LETTER_FORMS})
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
