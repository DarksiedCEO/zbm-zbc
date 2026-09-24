"""
Deterministic text normalisation and phrase matching.

Submitted text (captions, bios, transcripts) is DATA. It is only ever
normalised and searched for configured phrases; it is never interpreted,
executed, or allowed to change which rules apply.

`canonical()` removes the cheap evasions before any match (fix wave 1, F12):
- NFKC (fullwidth / mathematical / ligature forms -> plain letters);
- every format character (Unicode Cf: zero-width space/joiners, soft
  hyphen, bidi controls, word joiner, BOM) is deleted;
- diacritics are stripped (NFKD, drop combining marks, NFC);
- casefold;
- common confusables — Cyrillic, Greek, Armenian and Latin-extended
  lookalikes of Latin letters — map to the Latin letter (`CONFUSABLES`);
- anything that isn't a letter, digit, `#` or `@` becomes a space.

`match_phrase()` then answers EXACT (whole-word match on canonical text),
LOOSE (the phrase appears only once separator-split letters are rejoined —
"g u a r a n t e e d", "g.u.a.r", "guaran teed" — or after leetspeak
folding "gu4r4nteed"), or NONE. `contains_phrase()` is True for EXACT.

`obfuscation_signals()` says whether text shows evasion patterns at all:
mixed-script confusables, a format character hidden between letters, or a
run of 4+ single letters split by separators. Clip Review sends any clip
with a signal to the human queue — never an automatic pass.
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
    "ſ": "s", "ƿ": "p",
}
_CONFUSABLE_TABLE = str.maketrans(CONFUSABLES)
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
    return "".join(ch for ch in t if unicodedata.category(ch) != "Cf")


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


def _script(ch: str) -> str:
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return "other"
    for s in ("LATIN", "CYRILLIC", "GREEK", "ARMENIAN"):
        if name.startswith(s) or f" {s} " in f" {name} ":
            return s
    return "other"


def obfuscation_signals(text: str) -> list[str]:
    """Evasion patterns in `text` (empty list = none). Conservative on
    purpose: a false positive costs a human look, a false negative lets a
    never-say line through automatically."""
    signals: list[str] = []
    t = unicodedata.normalize("NFKC", text or "").casefold()
    has_latin = any(ch.isalpha() and _script(ch) == "LATIN" for ch in t)
    lookalikes = sorted({ch for ch in t if ch in _SIGNAL_CHARS})
    if lookalikes and has_latin:
        signals.append("mixed-script text: lookalike letter(s) "
                       + ", ".join(f"U+{ord(c):04X}" for c in lookalikes[:5]) + " among Latin letters")
    hidden = [ch for i, ch in enumerate(t) if unicodedata.category(ch) == "Cf"
              and 0 < i < len(t) - 1 and t[i - 1].isalnum() and t[i + 1].isalnum()]
    if hidden:
        signals.append("invisible format character(s) inside a word: "
                       + ", ".join(sorted({f"U+{ord(c):04X}" for c in hidden})))
    run = 0
    for tok in canonical(text).split():
        run = run + 1 if len(tok) == 1 and tok.isalpha() else 0
        if run >= SINGLE_LETTER_RUN:
            signals.append(f"{SINGLE_LETTER_RUN}+ single letters split by separators (e.g. 'g u a r')")
            break
    return signals


def word_count(text: str) -> int:
    return len(canonical(text).split())
