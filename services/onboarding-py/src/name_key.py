"""
The 1099 payee key for a legal name (bug sweep D, AEGIS O-1). Stdlib only.

Two spellings of ONE person's legal name must give ONE key, or the person's 1099 total is split and under-reported:
zero-width and other format characters (Unicode Cf: U+200B, the soft hyphen U+00AD, bidi controls...) and the other
default-ignorables are removed; NFKC; casefold; apostrophe, hyphen and dash variants fold to ``'`` and ``-``; every
other punctuation and whitespace run is one space; lookalike letters fold to Latin with the SAME table creative-py's
``shared/text.py`` uses (``CONFUSABLES`` + the generated Latin-letter folds, copied here: services share no code), so
"Pаt" (Cyrillic a) and "Pát" key as "pat". Over-merging two different people is the safe direction for a 1099
threshold (it can only raise a total); the W-9 holder at ZBC payouts/tax reconciles the final filing.
"""

from __future__ import annotations

import re
import unicodedata

# copied from services/creative-py/src/shared/text.py (keep in step)
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
    "ԍ": "g", "ԃ": "d", "ԋ": "h", "ԏ": "t", "ӡ": "3", "ҽ": "e", "ҿ": "e", "ᴫ": "n",
    "ϝ": "f", "ϻ": "m", "ϙ": "q", "ͱ": "h", "ͷ": "n",
    # Cherokee (after casefold Cherokee small letters U+AB70.. become U+13A0..)
    "Ꭰ": "d", "Ꭱ": "r", "Ꭲ": "t", "Ꭵ": "i", "Ꭹ": "y", "Ꭺ": "a", "Ꭻ": "j", "Ꭼ": "e", "Ꮃ": "w",
    "Ꮇ": "m", "Ꮋ": "h", "Ꮍ": "y", "Ꮐ": "g", "Ꮒ": "h", "Ꮓ": "z", "Ꮟ": "b", "Ꮢ": "r", "Ꮤ": "w",
    "Ꮥ": "s", "Ꮩ": "v", "Ꮪ": "s", "Ꮮ": "l", "Ꮯ": "c", "Ꮲ": "p", "Ꮶ": "k", "Ꮷ": "d", "Ᏻ": "g",
    "Ᏼ": "b", "Ꮻ": "o", "Ꮎ": "o",
}

DEFAULT_IGNORABLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
    (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)
_IGNORABLE = frozenset(chr(cp) for lo, hi in DEFAULT_IGNORABLE_RANGES for cp in range(lo, hi + 1))


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


_FOLDS = str.maketrans({**_generated_latin_folds(), **CONFUSABLES})
# apostrophes / quotes and hyphens / dashes / minus signs that a name may carry in any of their forms
_APOSTROPHES = "\u2018\u2019\u201b\u02bc\u02bb\u02bd\u2032\u00b4\u0060\uff07\u055a\ua78c"
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d\u2e3a\u2e3b\u058a\u05be"
_PUNCT_FOLD = str.maketrans({**{c: "'" for c in _APOSTROPHES}, **{c: "-" for c in _DASHES}})


def has_format_chars(value: str) -> bool:
    """True when ``value`` holds a Unicode format character (Cf) or another default-ignorable code point."""
    return any(ch in _IGNORABLE or unicodedata.category(ch) == "Cf" for ch in value)


def name_key_text(legal_name: str) -> str:
    """The canonical text a legal name is keyed on."""
    t = "".join(ch for ch in legal_name if ch not in _IGNORABLE and unicodedata.category(ch) != "Cf")
    t = unicodedata.normalize("NFKC", t).casefold()
    t = t.translate(_PUNCT_FOLD).translate(_FOLDS)
    t = unicodedata.normalize("NFKC", t)
    out, prev_space = [], True
    for ch in t:
        if ch.isalnum() or ch in "'-":
            out.append(ch)
            prev_space = False
        elif not prev_space:
            out.append(" ")
            prev_space = True
    return "".join(out).strip()
