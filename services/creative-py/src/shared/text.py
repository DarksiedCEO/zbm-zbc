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

`obfuscation_signals()` says whether text shows evasion patterns at all:
any bidi control, Hangul/Mongolian filler or tag character ANYWHERE; any
other default-ignorable or format character touching a letter or digit;
lookalike letters among Latin text; letters of two scripts inside one
word; a run of 4+ single letters split by separators. `non_latin_letters()`
lists letters outside the Latin script (Clip Review sends a clip of an
English-language campaign with any of them to a human). Any signal means
never an automatic pass.
"""

from __future__ import annotations

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


def is_default_ignorable(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in DEFAULT_IGNORABLE_RANGES)


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

SINGLE_LETTER_RUN = 4  # "g u a r ..." (4+) is a signal; "U.S.A." (3) is not


class PhraseMatch(str, Enum):
    EXACT = "exact"
    LOOSE = "loose"
    NONE = "none"


def _strip_marks(t: str) -> str:
    d = unicodedata.normalize("NFKD", t)
    return unicodedata.normalize("NFC", "".join(ch for ch in d if not unicodedata.combining(ch)))


def _drop_format(t: str) -> str:
    """Blank-rendering ignorables -> space; every other default-ignorable
    or format (Cf) character -> deleted."""
    return "".join(" " if ch in _BLANK_IGNORABLES else ch for ch in t
                   if ch in _BLANK_IGNORABLES or not (is_default_ignorable(ch) or unicodedata.category(ch) == "Cf"))


def canonical(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
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
    """EXACT or LOOSE — for refusals where a false positive only costs a rewrite."""
    return match_phrase(haystack, phrase) is not PhraseMatch.NONE


_SCRIPT_FAMILY = {"HIRAGANA": "JAPANESE", "KATAKANA": "JAPANESE", "CJK": "JAPANESE", "HALFWIDTH": "JAPANESE"}


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


def _letters_view(text: str) -> str:
    """NFKC text with ignorables/format characters removed (fillers -> space)."""
    return _drop_format(unicodedata.normalize("NFKC", text or ""))


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
    raw = unicodedata.normalize("NFKC", text or "")
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
    hidden: set[str] = set()
    for i, ch in enumerate(raw):
        if ch in _BIDI_CONTROLS or ch in _BLANK_IGNORABLES or not (is_default_ignorable(ch) or unicodedata.category(ch) == "Cf"):
            continue
        k = i - 1
        while k >= 0 and (is_default_ignorable(raw[k]) or unicodedata.category(raw[k]) == "Cf"):
            k -= 1
        n = i + 1
        while n < len(raw) and (is_default_ignorable(raw[n]) or unicodedata.category(raw[n]) == "Cf"):
            n += 1
        if (k >= 0 and raw[k].isalnum()) or (n < len(raw) and raw[n].isalnum()):
            hidden.add(f"U+{ord(ch):04X}")
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
