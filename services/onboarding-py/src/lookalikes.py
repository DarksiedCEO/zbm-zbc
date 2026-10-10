"""
Lookalike folding, one copy per service that folds lookalike letters (war room fixes WR-F001..WR-F005, ADR 0018).

Services do not import each other, so this file is copied BYTE-IDENTICAL into ``src/lookalikes.py`` of
clipper-network-py, onboarding-py, service-py and verification-py: the hygiene lint (rule L4) fails on any difference,
and ``devtools/test_lookalikes.py`` regenerates the data block below from the vendored Unicode data and checks the
shared hand table against creative-py's. Standard library only.

What a caller gets (``fold`` / ``fold_cased``), in this order:

1. ``strip_invisible``: every Unicode format character (Cf: zero-width space and joiners, soft hyphen, bidi controls,
   word joiner, BOM, tag characters) and every other Default_Ignorable_Code_Point (the complete list from
   DerivedCoreProperties.txt, as creative-py's ``shared/text.py``) is deleted;
2. NFKC (full-width letters AND digits, mathematical letters, ligatures);
3. the letters a HAND table maps whose casefold is a different letter are folded as written (the final sigma ``ς`` is
   ``c``; casefolding first made it ``σ``, which is ``o``: WR-F003);
4. casefold (``fold_cased``) — or not (``fold``, for a caller whose own table is case-sensitive);
5. the lookalike table, precedence high to low:
   (a) the calling service's own table (unchanged, so no key or match churn for anything it already folded);
   (b) ``SHARED``: creative-py ``shared/text.py`` ``CONFUSABLES``, the repo's shared hand table (Cyrillic, Greek,
       Armenian, Cherokee, IPA / small capitals; ``β ɡ η ζ ς`` among them);
   (c) ``LATIN_NAMED``: every "LATIN ... LETTER [SMALL CAPITAL|SCRIPT|DOTLESS|LONG] <X> [WITH ...]" letter -> x,
       generated from ``unicodedata`` at import as creative-py does;
   (d) ``SKELETON``: generated from Unicode confusables.txt (the UTS #39 skeleton data; version and sha256 below):
       every single non-Latin-script LETTER (general category L*) whose prototype is ASCII letters only, mapped to
       those letters in lower case (case as the data has it). Latin-script
       letters are left to (c) and the hand tables on purpose: "æ", "œ", "ĳ" are letters of real names, and folding
       them now would change keys already stored.
   ``fold_cased`` folds an upper-case SKELETON letter as written (before casefolding) only when no layer maps that
   letter's casefold: a letter some table already folded keeps its fold (no churn).

Diacritics, leetspeak and letter spacing are the caller's business (each service has its own rules for them); the
callers apply leetspeak AFTER this fold (a full-width digit is a digit only after NFKC: WR-F004) and join spaced
letters with leet digits counted as letters (WR-F002).
"""

from __future__ import annotations

import re
import unicodedata

VERSION = "lookalikes-1"

DEFAULT_IGNORABLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
    (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)
_IGNORABLE = frozenset(chr(cp) for lo, hi in DEFAULT_IGNORABLE_RANGES for cp in range(lo, hi + 1))

# creative-py src/shared/text.py CONFUSABLES, copied (devtools/test_lookalikes.py checks it is the same table)
SHARED: dict[str, str] = {
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
    # insular / medieval Latin letters
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

# BEGIN GENERATED SKELETON (devtools/lookalikes/generate.py; do not edit by hand)
# Unicode confusables.txt 15.1.0 (UTS #39), sha256 8289f833e4cf78fde56b2080dc0e42934ef5182c9c3f4dd1fbdf2bced69fd5ed: 1149 entries
SKELETON_SOURCE = "Unicode confusables.txt 15.1.0"
SKELETON_SOURCE_SHA256 = "8289f833e4cf78fde56b2080dc0e42934ef5182c9c3f4dd1fbdf2bced69fd5ed"
SKELETON: dict[str, str] = {
    "\u037a": "i", "\u037f": "j", "\u0391": "a", "\u0392": "b", "\u0395": "e", "\u0396": "z", "\u0397": "h",
    "\u0399": "l", "\u039a": "k", "\u039c": "m", "\u039d": "n", "\u039f": "o", "\u03a1": "p", "\u03a4": "t",
    "\u03a5": "y", "\u03a7": "x", "\u03b1": "a", "\u03b3": "y", "\u03b9": "i", "\u03bd": "v", "\u03bf": "o",
    "\u03c1": "p", "\u03c3": "o", "\u03c5": "u", "\u03d2": "y", "\u03dc": "f", "\u03f1": "p", "\u03f2": "c",
    "\u03f3": "j", "\u03f9": "c", "\u03fa": "m", "\u0405": "s", "\u0406": "l", "\u0408": "j", "\u0410": "a",
    "\u0412": "b", "\u0415": "e", "\u041a": "k", "\u041c": "m", "\u041d": "h", "\u041e": "o", "\u0420": "p",
    "\u0421": "c", "\u0422": "t", "\u0423": "y", "\u0425": "x", "\u042b": "bl", "\u042c": "b", "\u042e": "lo",
    "\u0430": "a", "\u0433": "r", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0443": "y",
    "\u0445": "x", "\u0455": "s", "\u0456": "i", "\u0458": "j", "\u0461": "w", "\u0474": "v", "\u0475": "v",
    "\u04ae": "y", "\u04af": "y", "\u04bb": "h", "\u04bd": "e", "\u04c0": "l", "\u04cf": "i", "\u04d4": "ae",
    "\u04d5": "ae", "\u0501": "d", "\u050c": "g", "\u051b": "q", "\u051c": "w", "\u051d": "w", "\u054d": "u",
    "\u054f": "s", "\u0555": "o", "\u0561": "w", "\u0563": "q", "\u0566": "q", "\u0570": "h", "\u0578": "n",
    "\u057c": "n", "\u057d": "u", "\u0581": "g", "\u0584": "f", "\u0585": "o", "\u05d5": "l", "\u05d8": "v",
    "\u05df": "l", "\u05e1": "o", "\u05f0": "ll", "\u0627": "l", "\u0647": "o", "\u06be": "o", "\u06c1": "o",
    "\u06d5": "o", "\u07ca": "l", "\u0b20": "o", "\u0d20": "o", "\u101d": "o", "\u10e7": "y", "\u10ff": "o",
    "\u1200": "u", "\u12d0": "o", "\u13a0": "d", "\u13a1": "r", "\u13a2": "t", "\u13a5": "i", "\u13a9": "y",
    "\u13aa": "a", "\u13ab": "j", "\u13ac": "e", "\u13b3": "w", "\u13b7": "m", "\u13bb": "h", "\u13bd": "y",
    "\u13c0": "g", "\u13c2": "h", "\u13c3": "z", "\u13cf": "b", "\u13d2": "r", "\u13d4": "w", "\u13d5": "s",
    "\u13d9": "v", "\u13da": "s", "\u13de": "l", "\u13df": "c", "\u13e2": "p", "\u13e6": "k", "\u13e7": "d",
    "\u13f3": "g", "\u13f4": "b", "\u142f": "v", "\u144c": "u", "\u146d": "p", "\u146f": "d", "\u1472": "b",
    "\u148d": "j", "\u14aa": "l", "\u1541": "x", "\u157c": "h", "\u157d": "x", "\u1587": "r", "\u15af": "b",
    "\u15b4": "f", "\u15c5": "a", "\u15de": "d", "\u15ea": "d", "\u15f0": "m", "\u15f7": "b", "\u16b7": "x",
    "\u16c1": "l", "\u16d5": "k", "\u16d6": "m", "\u1d26": "r", "\u1fbe": "i", "\u2102": "c", "\u210a": "g",
    "\u210b": "h", "\u210c": "h", "\u210d": "h", "\u210e": "h", "\u2110": "l", "\u2111": "l", "\u2112": "l",
    "\u2113": "l", "\u2115": "n", "\u2119": "p", "\u211a": "q", "\u211b": "r", "\u211c": "r", "\u211d": "r",
    "\u2124": "z", "\u2128": "z", "\u212a": "k", "\u212c": "b", "\u212d": "c", "\u212f": "e", "\u2130": "e",
    "\u2131": "f", "\u2133": "m", "\u2134": "o", "\u2139": "i", "\u213d": "y", "\u2145": "d", "\u2146": "d",
    "\u2147": "e", "\u2148": "i", "\u2149": "j", "\u2c85": "r", "\u2c8e": "h", "\u2c92": "l", "\u2c94": "k",
    "\u2c98": "m", "\u2c9a": "n", "\u2c9e": "o", "\u2c9f": "o", "\u2ca2": "p", "\u2ca3": "p", "\u2ca4": "c",
    "\u2ca5": "c", "\u2ca6": "t", "\u2ca8": "y", "\u2cac": "x", "\u2cd0": "l", "\u2d38": "v", "\u2d39": "e",
    "\u2d4f": "l", "\u2d54": "o", "\u2d55": "q", "\u2d5d": "x", "\ua4d0": "b", "\ua4d1": "p", "\ua4d2": "d",
    "\ua4d3": "d", "\ua4d4": "t", "\ua4d6": "g", "\ua4d7": "k", "\ua4d9": "j", "\ua4da": "c", "\ua4dc": "z",
    "\ua4dd": "f", "\ua4df": "m", "\ua4e0": "n", "\ua4e1": "l", "\ua4e2": "s", "\ua4e3": "r", "\ua4e6": "v",
    "\ua4e7": "h", "\ua4ea": "w", "\ua4eb": "x", "\ua4ec": "y", "\ua4ee": "a", "\ua4f0": "e", "\ua4f2": "l",
    "\ua4f3": "o", "\ua4f4": "u", "\ua647": "i", "\ua698": "oo", "\ua699": "oo", "\ua6df": "v", "\uab75": "i",
    "\uab81": "r", "\uab83": "w", "\uab93": "z", "\uaba9": "v", "\uabaa": "s", "\uabaf": "c", "\ufba6": "o",
    "\ufba7": "o", "\ufba8": "o", "\ufba9": "o", "\ufbaa": "o", "\ufbab": "o", "\ufbac": "o", "\ufbad": "o",
    "\ufe8d": "l", "\ufe8e": "l", "\ufee9": "o", "\ufeea": "o", "\ufeeb": "o", "\ufeec": "o", "\uff21": "a",
    "\uff22": "b", "\uff23": "c", "\uff25": "e", "\uff28": "h", "\uff29": "l", "\uff2a": "j", "\uff2b": "k",
    "\uff2d": "m", "\uff2e": "n", "\uff2f": "o", "\uff30": "p", "\uff33": "s", "\uff34": "t", "\uff38": "x",
    "\uff39": "y", "\uff3a": "z", "\uff41": "a", "\uff43": "c", "\uff45": "e", "\uff47": "g", "\uff48": "h",
    "\uff49": "i", "\uff4a": "j", "\uff4c": "l", "\uff4f": "o", "\uff50": "p", "\uff53": "s", "\uff56": "v",
    "\uff58": "x", "\uff59": "y", "\U00010282": "b", "\U00010286": "e", "\U00010287": "f", "\U0001028a": "l",
    "\U00010290": "x", "\U00010292": "o", "\U00010295": "p", "\U00010296": "s", "\U00010297": "t", "\U000102a0": "a",
    "\U000102a1": "b", "\U000102a2": "c", "\U000102a5": "f", "\U000102ab": "o", "\U000102b0": "m", "\U000102b1": "t",
    "\U000102b2": "y", "\U000102b4": "x", "\U000102cf": "h", "\U00010301": "b", "\U00010302": "c", "\U00010309": "l",
    "\U00010311": "m", "\U00010315": "t", "\U00010317": "x", "\U00010404": "o", "\U00010415": "c", "\U0001041b": "l",
    "\U00010420": "s", "\U0001042c": "o", "\U0001043d": "c", "\U00010448": "s", "\U000104b4": "r", "\U000104c2": "o",
    "\U000104ce": "u", "\U000104ea": "o", "\U000104f6": "u", "\U00010513": "n", "\U00010516": "o", "\U00010518": "k",
    "\U0001051c": "c", "\U0001051d": "v", "\U00010525": "f", "\U00010526": "l", "\U00010527": "x", "\U00011700": "rn",
    "\U00011706": "v", "\U0001170a": "w", "\U0001170e": "w", "\U0001170f": "w", "\U000118a0": "v", "\U000118a2": "f",
    "\U000118a3": "l", "\U000118a4": "y", "\U000118a6": "e", "\U000118a9": "z", "\U000118ae": "e", "\U000118b2": "l",
    "\U000118b5": "o", "\U000118b8": "u", "\U000118bc": "t", "\U000118c0": "v", "\U000118c1": "s", "\U000118c2": "f",
    "\U000118c3": "i", "\U000118c4": "z", "\U000118c8": "o", "\U000118d7": "o", "\U000118d8": "u", "\U000118dc": "y",
    "\U00016f08": "v", "\U00016f0a": "t", "\U00016f16": "l", "\U00016f28": "l", "\U00016f35": "r", "\U00016f3a": "s",
    "\U00016f40": "a", "\U00016f42": "u", "\U00016f43": "y", "\U0001d400": "a", "\U0001d401": "b", "\U0001d402": "c",
    "\U0001d403": "d", "\U0001d404": "e", "\U0001d405": "f", "\U0001d406": "g", "\U0001d407": "h", "\U0001d408": "l",
    "\U0001d409": "j", "\U0001d40a": "k", "\U0001d40b": "l", "\U0001d40c": "m", "\U0001d40d": "n", "\U0001d40e": "o",
    "\U0001d40f": "p", "\U0001d410": "q", "\U0001d411": "r", "\U0001d412": "s", "\U0001d413": "t", "\U0001d414": "u",
    "\U0001d415": "v", "\U0001d416": "w", "\U0001d417": "x", "\U0001d418": "y", "\U0001d419": "z", "\U0001d41a": "a",
    "\U0001d41b": "b", "\U0001d41c": "c", "\U0001d41d": "d", "\U0001d41e": "e", "\U0001d41f": "f", "\U0001d420": "g",
    "\U0001d421": "h", "\U0001d422": "i", "\U0001d423": "j", "\U0001d424": "k", "\U0001d425": "l", "\U0001d426": "rn",
    "\U0001d427": "n", "\U0001d428": "o", "\U0001d429": "p", "\U0001d42a": "q", "\U0001d42b": "r", "\U0001d42c": "s",
    "\U0001d42d": "t", "\U0001d42e": "u", "\U0001d42f": "v", "\U0001d430": "w", "\U0001d431": "x", "\U0001d432": "y",
    "\U0001d433": "z", "\U0001d434": "a", "\U0001d435": "b", "\U0001d436": "c", "\U0001d437": "d", "\U0001d438": "e",
    "\U0001d439": "f", "\U0001d43a": "g", "\U0001d43b": "h", "\U0001d43c": "l", "\U0001d43d": "j", "\U0001d43e": "k",
    "\U0001d43f": "l", "\U0001d440": "m", "\U0001d441": "n", "\U0001d442": "o", "\U0001d443": "p", "\U0001d444": "q",
    "\U0001d445": "r", "\U0001d446": "s", "\U0001d447": "t", "\U0001d448": "u", "\U0001d449": "v", "\U0001d44a": "w",
    "\U0001d44b": "x", "\U0001d44c": "y", "\U0001d44d": "z", "\U0001d44e": "a", "\U0001d44f": "b", "\U0001d450": "c",
    "\U0001d451": "d", "\U0001d452": "e", "\U0001d453": "f", "\U0001d454": "g", "\U0001d456": "i", "\U0001d457": "j",
    "\U0001d458": "k", "\U0001d459": "l", "\U0001d45a": "rn", "\U0001d45b": "n", "\U0001d45c": "o", "\U0001d45d": "p",
    "\U0001d45e": "q", "\U0001d45f": "r", "\U0001d460": "s", "\U0001d461": "t", "\U0001d462": "u", "\U0001d463": "v",
    "\U0001d464": "w", "\U0001d465": "x", "\U0001d466": "y", "\U0001d467": "z", "\U0001d468": "a", "\U0001d469": "b",
    "\U0001d46a": "c", "\U0001d46b": "d", "\U0001d46c": "e", "\U0001d46d": "f", "\U0001d46e": "g", "\U0001d46f": "h",
    "\U0001d470": "l", "\U0001d471": "j", "\U0001d472": "k", "\U0001d473": "l", "\U0001d474": "m", "\U0001d475": "n",
    "\U0001d476": "o", "\U0001d477": "p", "\U0001d478": "q", "\U0001d479": "r", "\U0001d47a": "s", "\U0001d47b": "t",
    "\U0001d47c": "u", "\U0001d47d": "v", "\U0001d47e": "w", "\U0001d47f": "x", "\U0001d480": "y", "\U0001d481": "z",
    "\U0001d482": "a", "\U0001d483": "b", "\U0001d484": "c", "\U0001d485": "d", "\U0001d486": "e", "\U0001d487": "f",
    "\U0001d488": "g", "\U0001d489": "h", "\U0001d48a": "i", "\U0001d48b": "j", "\U0001d48c": "k", "\U0001d48d": "l",
    "\U0001d48e": "rn", "\U0001d48f": "n", "\U0001d490": "o", "\U0001d491": "p", "\U0001d492": "q", "\U0001d493": "r",
    "\U0001d494": "s", "\U0001d495": "t", "\U0001d496": "u", "\U0001d497": "v", "\U0001d498": "w", "\U0001d499": "x",
    "\U0001d49a": "y", "\U0001d49b": "z", "\U0001d49c": "a", "\U0001d49e": "c", "\U0001d49f": "d", "\U0001d4a2": "g",
    "\U0001d4a5": "j", "\U0001d4a6": "k", "\U0001d4a9": "n", "\U0001d4aa": "o", "\U0001d4ab": "p", "\U0001d4ac": "q",
    "\U0001d4ae": "s", "\U0001d4af": "t", "\U0001d4b0": "u", "\U0001d4b1": "v", "\U0001d4b2": "w", "\U0001d4b3": "x",
    "\U0001d4b4": "y", "\U0001d4b5": "z", "\U0001d4b6": "a", "\U0001d4b7": "b", "\U0001d4b8": "c", "\U0001d4b9": "d",
    "\U0001d4bb": "f", "\U0001d4bd": "h", "\U0001d4be": "i", "\U0001d4bf": "j", "\U0001d4c0": "k", "\U0001d4c1": "l",
    "\U0001d4c2": "rn", "\U0001d4c3": "n", "\U0001d4c5": "p", "\U0001d4c6": "q", "\U0001d4c7": "r", "\U0001d4c8": "s",
    "\U0001d4c9": "t", "\U0001d4ca": "u", "\U0001d4cb": "v", "\U0001d4cc": "w", "\U0001d4cd": "x", "\U0001d4ce": "y",
    "\U0001d4cf": "z", "\U0001d4d0": "a", "\U0001d4d1": "b", "\U0001d4d2": "c", "\U0001d4d3": "d", "\U0001d4d4": "e",
    "\U0001d4d5": "f", "\U0001d4d6": "g", "\U0001d4d7": "h", "\U0001d4d8": "l", "\U0001d4d9": "j", "\U0001d4da": "k",
    "\U0001d4db": "l", "\U0001d4dc": "m", "\U0001d4dd": "n", "\U0001d4de": "o", "\U0001d4df": "p", "\U0001d4e0": "q",
    "\U0001d4e1": "r", "\U0001d4e2": "s", "\U0001d4e3": "t", "\U0001d4e4": "u", "\U0001d4e5": "v", "\U0001d4e6": "w",
    "\U0001d4e7": "x", "\U0001d4e8": "y", "\U0001d4e9": "z", "\U0001d4ea": "a", "\U0001d4eb": "b", "\U0001d4ec": "c",
    "\U0001d4ed": "d", "\U0001d4ee": "e", "\U0001d4ef": "f", "\U0001d4f0": "g", "\U0001d4f1": "h", "\U0001d4f2": "i",
    "\U0001d4f3": "j", "\U0001d4f4": "k", "\U0001d4f5": "l", "\U0001d4f6": "rn", "\U0001d4f7": "n", "\U0001d4f8": "o",
    "\U0001d4f9": "p", "\U0001d4fa": "q", "\U0001d4fb": "r", "\U0001d4fc": "s", "\U0001d4fd": "t", "\U0001d4fe": "u",
    "\U0001d4ff": "v", "\U0001d500": "w", "\U0001d501": "x", "\U0001d502": "y", "\U0001d503": "z", "\U0001d504": "a",
    "\U0001d505": "b", "\U0001d507": "d", "\U0001d508": "e", "\U0001d509": "f", "\U0001d50a": "g", "\U0001d50d": "j",
    "\U0001d50e": "k", "\U0001d50f": "l", "\U0001d510": "m", "\U0001d511": "n", "\U0001d512": "o", "\U0001d513": "p",
    "\U0001d514": "q", "\U0001d516": "s", "\U0001d517": "t", "\U0001d518": "u", "\U0001d519": "v", "\U0001d51a": "w",
    "\U0001d51b": "x", "\U0001d51c": "y", "\U0001d51e": "a", "\U0001d51f": "b", "\U0001d520": "c", "\U0001d521": "d",
    "\U0001d522": "e", "\U0001d523": "f", "\U0001d524": "g", "\U0001d525": "h", "\U0001d526": "i", "\U0001d527": "j",
    "\U0001d528": "k", "\U0001d529": "l", "\U0001d52a": "rn", "\U0001d52b": "n", "\U0001d52c": "o", "\U0001d52d": "p",
    "\U0001d52e": "q", "\U0001d52f": "r", "\U0001d530": "s", "\U0001d531": "t", "\U0001d532": "u", "\U0001d533": "v",
    "\U0001d534": "w", "\U0001d535": "x", "\U0001d536": "y", "\U0001d537": "z", "\U0001d538": "a", "\U0001d539": "b",
    "\U0001d53b": "d", "\U0001d53c": "e", "\U0001d53d": "f", "\U0001d53e": "g", "\U0001d540": "l", "\U0001d541": "j",
    "\U0001d542": "k", "\U0001d543": "l", "\U0001d544": "m", "\U0001d546": "o", "\U0001d54a": "s", "\U0001d54b": "t",
    "\U0001d54c": "u", "\U0001d54d": "v", "\U0001d54e": "w", "\U0001d54f": "x", "\U0001d550": "y", "\U0001d552": "a",
    "\U0001d553": "b", "\U0001d554": "c", "\U0001d555": "d", "\U0001d556": "e", "\U0001d557": "f", "\U0001d558": "g",
    "\U0001d559": "h", "\U0001d55a": "i", "\U0001d55b": "j", "\U0001d55c": "k", "\U0001d55d": "l", "\U0001d55e": "rn",
    "\U0001d55f": "n", "\U0001d560": "o", "\U0001d561": "p", "\U0001d562": "q", "\U0001d563": "r", "\U0001d564": "s",
    "\U0001d565": "t", "\U0001d566": "u", "\U0001d567": "v", "\U0001d568": "w", "\U0001d569": "x", "\U0001d56a": "y",
    "\U0001d56b": "z", "\U0001d56c": "a", "\U0001d56d": "b", "\U0001d56e": "c", "\U0001d56f": "d", "\U0001d570": "e",
    "\U0001d571": "f", "\U0001d572": "g", "\U0001d573": "h", "\U0001d574": "l", "\U0001d575": "j", "\U0001d576": "k",
    "\U0001d577": "l", "\U0001d578": "m", "\U0001d579": "n", "\U0001d57a": "o", "\U0001d57b": "p", "\U0001d57c": "q",
    "\U0001d57d": "r", "\U0001d57e": "s", "\U0001d57f": "t", "\U0001d580": "u", "\U0001d581": "v", "\U0001d582": "w",
    "\U0001d583": "x", "\U0001d584": "y", "\U0001d585": "z", "\U0001d586": "a", "\U0001d587": "b", "\U0001d588": "c",
    "\U0001d589": "d", "\U0001d58a": "e", "\U0001d58b": "f", "\U0001d58c": "g", "\U0001d58d": "h", "\U0001d58e": "i",
    "\U0001d58f": "j", "\U0001d590": "k", "\U0001d591": "l", "\U0001d592": "rn", "\U0001d593": "n", "\U0001d594": "o",
    "\U0001d595": "p", "\U0001d596": "q", "\U0001d597": "r", "\U0001d598": "s", "\U0001d599": "t", "\U0001d59a": "u",
    "\U0001d59b": "v", "\U0001d59c": "w", "\U0001d59d": "x", "\U0001d59e": "y", "\U0001d59f": "z", "\U0001d5a0": "a",
    "\U0001d5a1": "b", "\U0001d5a2": "c", "\U0001d5a3": "d", "\U0001d5a4": "e", "\U0001d5a5": "f", "\U0001d5a6": "g",
    "\U0001d5a7": "h", "\U0001d5a8": "l", "\U0001d5a9": "j", "\U0001d5aa": "k", "\U0001d5ab": "l", "\U0001d5ac": "m",
    "\U0001d5ad": "n", "\U0001d5ae": "o", "\U0001d5af": "p", "\U0001d5b0": "q", "\U0001d5b1": "r", "\U0001d5b2": "s",
    "\U0001d5b3": "t", "\U0001d5b4": "u", "\U0001d5b5": "v", "\U0001d5b6": "w", "\U0001d5b7": "x", "\U0001d5b8": "y",
    "\U0001d5b9": "z", "\U0001d5ba": "a", "\U0001d5bb": "b", "\U0001d5bc": "c", "\U0001d5bd": "d", "\U0001d5be": "e",
    "\U0001d5bf": "f", "\U0001d5c0": "g", "\U0001d5c1": "h", "\U0001d5c2": "i", "\U0001d5c3": "j", "\U0001d5c4": "k",
    "\U0001d5c5": "l", "\U0001d5c6": "rn", "\U0001d5c7": "n", "\U0001d5c8": "o", "\U0001d5c9": "p", "\U0001d5ca": "q",
    "\U0001d5cb": "r", "\U0001d5cc": "s", "\U0001d5cd": "t", "\U0001d5ce": "u", "\U0001d5cf": "v", "\U0001d5d0": "w",
    "\U0001d5d1": "x", "\U0001d5d2": "y", "\U0001d5d3": "z", "\U0001d5d4": "a", "\U0001d5d5": "b", "\U0001d5d6": "c",
    "\U0001d5d7": "d", "\U0001d5d8": "e", "\U0001d5d9": "f", "\U0001d5da": "g", "\U0001d5db": "h", "\U0001d5dc": "l",
    "\U0001d5dd": "j", "\U0001d5de": "k", "\U0001d5df": "l", "\U0001d5e0": "m", "\U0001d5e1": "n", "\U0001d5e2": "o",
    "\U0001d5e3": "p", "\U0001d5e4": "q", "\U0001d5e5": "r", "\U0001d5e6": "s", "\U0001d5e7": "t", "\U0001d5e8": "u",
    "\U0001d5e9": "v", "\U0001d5ea": "w", "\U0001d5eb": "x", "\U0001d5ec": "y", "\U0001d5ed": "z", "\U0001d5ee": "a",
    "\U0001d5ef": "b", "\U0001d5f0": "c", "\U0001d5f1": "d", "\U0001d5f2": "e", "\U0001d5f3": "f", "\U0001d5f4": "g",
    "\U0001d5f5": "h", "\U0001d5f6": "i", "\U0001d5f7": "j", "\U0001d5f8": "k", "\U0001d5f9": "l", "\U0001d5fa": "rn",
    "\U0001d5fb": "n", "\U0001d5fc": "o", "\U0001d5fd": "p", "\U0001d5fe": "q", "\U0001d5ff": "r", "\U0001d600": "s",
    "\U0001d601": "t", "\U0001d602": "u", "\U0001d603": "v", "\U0001d604": "w", "\U0001d605": "x", "\U0001d606": "y",
    "\U0001d607": "z", "\U0001d608": "a", "\U0001d609": "b", "\U0001d60a": "c", "\U0001d60b": "d", "\U0001d60c": "e",
    "\U0001d60d": "f", "\U0001d60e": "g", "\U0001d60f": "h", "\U0001d610": "l", "\U0001d611": "j", "\U0001d612": "k",
    "\U0001d613": "l", "\U0001d614": "m", "\U0001d615": "n", "\U0001d616": "o", "\U0001d617": "p", "\U0001d618": "q",
    "\U0001d619": "r", "\U0001d61a": "s", "\U0001d61b": "t", "\U0001d61c": "u", "\U0001d61d": "v", "\U0001d61e": "w",
    "\U0001d61f": "x", "\U0001d620": "y", "\U0001d621": "z", "\U0001d622": "a", "\U0001d623": "b", "\U0001d624": "c",
    "\U0001d625": "d", "\U0001d626": "e", "\U0001d627": "f", "\U0001d628": "g", "\U0001d629": "h", "\U0001d62a": "i",
    "\U0001d62b": "j", "\U0001d62c": "k", "\U0001d62d": "l", "\U0001d62e": "rn", "\U0001d62f": "n", "\U0001d630": "o",
    "\U0001d631": "p", "\U0001d632": "q", "\U0001d633": "r", "\U0001d634": "s", "\U0001d635": "t", "\U0001d636": "u",
    "\U0001d637": "v", "\U0001d638": "w", "\U0001d639": "x", "\U0001d63a": "y", "\U0001d63b": "z", "\U0001d63c": "a",
    "\U0001d63d": "b", "\U0001d63e": "c", "\U0001d63f": "d", "\U0001d640": "e", "\U0001d641": "f", "\U0001d642": "g",
    "\U0001d643": "h", "\U0001d644": "l", "\U0001d645": "j", "\U0001d646": "k", "\U0001d647": "l", "\U0001d648": "m",
    "\U0001d649": "n", "\U0001d64a": "o", "\U0001d64b": "p", "\U0001d64c": "q", "\U0001d64d": "r", "\U0001d64e": "s",
    "\U0001d64f": "t", "\U0001d650": "u", "\U0001d651": "v", "\U0001d652": "w", "\U0001d653": "x", "\U0001d654": "y",
    "\U0001d655": "z", "\U0001d656": "a", "\U0001d657": "b", "\U0001d658": "c", "\U0001d659": "d", "\U0001d65a": "e",
    "\U0001d65b": "f", "\U0001d65c": "g", "\U0001d65d": "h", "\U0001d65e": "i", "\U0001d65f": "j", "\U0001d660": "k",
    "\U0001d661": "l", "\U0001d662": "rn", "\U0001d663": "n", "\U0001d664": "o", "\U0001d665": "p", "\U0001d666": "q",
    "\U0001d667": "r", "\U0001d668": "s", "\U0001d669": "t", "\U0001d66a": "u", "\U0001d66b": "v", "\U0001d66c": "w",
    "\U0001d66d": "x", "\U0001d66e": "y", "\U0001d66f": "z", "\U0001d670": "a", "\U0001d671": "b", "\U0001d672": "c",
    "\U0001d673": "d", "\U0001d674": "e", "\U0001d675": "f", "\U0001d676": "g", "\U0001d677": "h", "\U0001d678": "l",
    "\U0001d679": "j", "\U0001d67a": "k", "\U0001d67b": "l", "\U0001d67c": "m", "\U0001d67d": "n", "\U0001d67e": "o",
    "\U0001d67f": "p", "\U0001d680": "q", "\U0001d681": "r", "\U0001d682": "s", "\U0001d683": "t", "\U0001d684": "u",
    "\U0001d685": "v", "\U0001d686": "w", "\U0001d687": "x", "\U0001d688": "y", "\U0001d689": "z", "\U0001d68a": "a",
    "\U0001d68b": "b", "\U0001d68c": "c", "\U0001d68d": "d", "\U0001d68e": "e", "\U0001d68f": "f", "\U0001d690": "g",
    "\U0001d691": "h", "\U0001d692": "i", "\U0001d693": "j", "\U0001d694": "k", "\U0001d695": "l", "\U0001d696": "rn",
    "\U0001d697": "n", "\U0001d698": "o", "\U0001d699": "p", "\U0001d69a": "q", "\U0001d69b": "r", "\U0001d69c": "s",
    "\U0001d69d": "t", "\U0001d69e": "u", "\U0001d69f": "v", "\U0001d6a0": "w", "\U0001d6a1": "x", "\U0001d6a2": "y",
    "\U0001d6a3": "z", "\U0001d6a4": "i", "\U0001d6a8": "a", "\U0001d6a9": "b", "\U0001d6ac": "e", "\U0001d6ad": "z",
    "\U0001d6ae": "h", "\U0001d6b0": "l", "\U0001d6b1": "k", "\U0001d6b3": "m", "\U0001d6b4": "n", "\U0001d6b6": "o",
    "\U0001d6b8": "p", "\U0001d6bb": "t", "\U0001d6bc": "y", "\U0001d6be": "x", "\U0001d6c2": "a", "\U0001d6c4": "y",
    "\U0001d6ca": "i", "\U0001d6ce": "v", "\U0001d6d0": "o", "\U0001d6d2": "p", "\U0001d6d4": "o", "\U0001d6d6": "u",
    "\U0001d6e0": "p", "\U0001d6e2": "a", "\U0001d6e3": "b", "\U0001d6e6": "e", "\U0001d6e7": "z", "\U0001d6e8": "h",
    "\U0001d6ea": "l", "\U0001d6eb": "k", "\U0001d6ed": "m", "\U0001d6ee": "n", "\U0001d6f0": "o", "\U0001d6f2": "p",
    "\U0001d6f5": "t", "\U0001d6f6": "y", "\U0001d6f8": "x", "\U0001d6fc": "a", "\U0001d6fe": "y", "\U0001d704": "i",
    "\U0001d708": "v", "\U0001d70a": "o", "\U0001d70c": "p", "\U0001d70e": "o", "\U0001d710": "u", "\U0001d71a": "p",
    "\U0001d71c": "a", "\U0001d71d": "b", "\U0001d720": "e", "\U0001d721": "z", "\U0001d722": "h", "\U0001d724": "l",
    "\U0001d725": "k", "\U0001d727": "m", "\U0001d728": "n", "\U0001d72a": "o", "\U0001d72c": "p", "\U0001d72f": "t",
    "\U0001d730": "y", "\U0001d732": "x", "\U0001d736": "a", "\U0001d738": "y", "\U0001d73e": "i", "\U0001d742": "v",
    "\U0001d744": "o", "\U0001d746": "p", "\U0001d748": "o", "\U0001d74a": "u", "\U0001d754": "p", "\U0001d756": "a",
    "\U0001d757": "b", "\U0001d75a": "e", "\U0001d75b": "z", "\U0001d75c": "h", "\U0001d75e": "l", "\U0001d75f": "k",
    "\U0001d761": "m", "\U0001d762": "n", "\U0001d764": "o", "\U0001d766": "p", "\U0001d769": "t", "\U0001d76a": "y",
    "\U0001d76c": "x", "\U0001d770": "a", "\U0001d772": "y", "\U0001d778": "i", "\U0001d77c": "v", "\U0001d77e": "o",
    "\U0001d780": "p", "\U0001d782": "o", "\U0001d784": "u", "\U0001d78e": "p", "\U0001d790": "a", "\U0001d791": "b",
    "\U0001d794": "e", "\U0001d795": "z", "\U0001d796": "h", "\U0001d798": "l", "\U0001d799": "k", "\U0001d79b": "m",
    "\U0001d79c": "n", "\U0001d79e": "o", "\U0001d7a0": "p", "\U0001d7a3": "t", "\U0001d7a4": "y", "\U0001d7a6": "x",
    "\U0001d7aa": "a", "\U0001d7ac": "y", "\U0001d7b2": "i", "\U0001d7b6": "v", "\U0001d7b8": "o", "\U0001d7ba": "p",
    "\U0001d7bc": "o", "\U0001d7be": "u", "\U0001d7c8": "p", "\U0001d7ca": "f", "\U0001ee00": "l", "\U0001ee24": "o",
    "\U0001ee64": "o", "\U0001ee80": "l", "\U0001ee84": "o",
}
# END GENERATED SKELETON

_LATIN_NAME = re.compile(
    r"LATIN (?:SMALL CAPITAL |SMALL |CAPITAL )?LETTER (?:SMALL CAPITAL |SCRIPT |DOTLESS |LONG )?([A-Z])(?: WITH .+)?")


def _latin_named() -> dict[str, str]:
    out: dict[str, str] = {}
    for cp in range(0x80, 0x20000):
        ch = chr(cp)
        m = _LATIN_NAME.fullmatch(unicodedata.name(ch, ""))
        if m:
            out[ch] = m.group(1).lower()
            if len(ch.casefold()) == 1:
                out.setdefault(ch.casefold(), m.group(1).lower())
    return out


LATIN_NAMED: dict[str, str] = _latin_named()


class Table:
    """A service's fold: its own table over the shared layers (see the module docstring, step 5)."""

    __slots__ = ("_early", "_full", "mapping")

    def __init__(self, own: dict[str, str] | None = None) -> None:
        own = dict(own or {})
        self.mapping: dict[str, str] = {**SKELETON, **LATIN_NAMED, **SHARED, **own}
        hand = {**SHARED, **own}
        # step 3: hand-table letters that casefold would turn into ANOTHER letter (the final sigma), and upper-case
        # skeleton letters whose casefold nothing maps (folding them as written cannot change an existing fold)
        early = {k: v for k, v in hand.items() if len(k) == 1 and k.lower() == k and k.casefold() != k}
        early.update({k: v for k, v in SKELETON.items() if k not in hand and k.casefold() != k
                      and k.casefold() not in self.mapping})
        self._early = str.maketrans(early)
        self._full = str.maketrans(self.mapping)

    def fold(self, text: str) -> str:
        """Steps 1-5 without casefolding: invisible characters out, NFKC, the table (case-sensitive), lower case,
        the table again. For a service whose own table lists upper-case letters."""
        t = unicodedata.normalize("NFKC", strip_invisible(text)).translate(self._early).translate(self._full)
        return t.lower().translate(self._full)

    def fold_cased(self, text: str) -> str:
        """Steps 1-5: invisible characters out, NFKC, the early letters, casefold, the table."""
        t = unicodedata.normalize("NFKC", strip_invisible(text)).translate(self._early)
        return t.casefold().translate(self._full)

    def casefold(self, text: str) -> str:
        """Steps 3-4 only (the early letters, then casefold), for a caller that has already removed invisible
        characters and applied NFKC its own way, and strips diacritics between casefolding and the table."""
        return text.translate(self._early).casefold()

    def map(self, text: str) -> str:
        """Step 5 only: the table."""
        return text.translate(self._full)


def is_invisible(ch: str) -> bool:
    return ch in _IGNORABLE or unicodedata.category(ch) == "Cf"


def strip_invisible(text: str) -> str:
    """Every format character (Cf) and every Default_Ignorable_Code_Point deleted."""
    if text.isascii():
        return text
    return "".join(ch for ch in text if ch not in _IGNORABLE and unicodedata.category(ch) != "Cf")
