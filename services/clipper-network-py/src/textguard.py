"""
Client-supplied text is DATA (spec §C design rules; copied from compliance-py src/textguard.py).

- ``has_control_chars``: C0/C1/DEL and lone surrogates are refused at the
  schema edge (422), except tab/newline where a field allows multi-line text
  (none of the fact fields do).
- ``normalize``: NFKC + case-fold + whitespace collapse, for vocabulary checks
  (disclosure labels) and for the Change Watcher's normalized-text hash.
- ``scan_injection``: the onboarding-py guardrail family (onboarding-py
  src/guardrails.py ``_INJECTION``, copied, not imported: services do not
  import each other). A hit NEVER changes a ruling; it is recorded as
  ``injection_text_ignored`` (rule names and counts only) and that is all.
- ``money_or_earnings``: CN-26 / §0.1.8 — money strings, currency markers and
  earnings / guarantee vocabulary. A template body or variable that trips it
  is refused (422); CN never renders a money value or an earnings promise.
  Matched after ``fold_for_matching`` (bug sweep C: NFKC, invisible characters,
  diacritics and confusables, so homoglyphs do not bypass it).
- ``looks_like_phone`` / ``is_email``: recruiting recipients (CN-08 / CN-09).
Nothing here evaluates, formats or templates client text.
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any, Iterator

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")
_CONTROL_STRICT = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")

_INJECTION = [
    ("ignore_instructions", re.compile(r"(?i)\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|all|your|the|any)\b.{0,20}\b(instructions?|rules?|guidelines?|policy|policies|prompt|guardrails?)")),
    ("role_reassignment", re.compile(r"(?i)\b(you are now|act as|pretend (?:to be|you are)|from now on you)\b")),
    ("system_prompt_probe", re.compile(r"(?i)\b(system prompt|developer message|hidden instructions?|jailbreak)\b")),
    ("fake_role_marker", re.compile(r"(?im)^[^\S\n]*(system|assistant|developer)\s*:")),
    ("ai_directive_comment", re.compile(r"(?i)<!--\s*(ai|assistant|agent|llm|bot)\b")),
    ("approval_forgery", re.compile(r"(?i)\b(auto[- ]?approve|approve (?:this|me|all)|mark (?:as )?(?:approved|verified|compliant)|skip (?:the )?(?:vetting|verification|compliance|checks?))\b")),
    ("credential_exfiltration", re.compile(r"(?i)\b(reveal|show|send|print|output|tell me)\b.{0,30}\b(password|credential|token|secret|api key)s?\b")),
    ("guarantee_coercion", re.compile(r"(?i)\b(say|tell them|promise|state) (?:that )?(?:we|you) (?:guarantee|will guarantee)\b")),
]
# Bounded scan: at most this many characters of any one string are scanned
# (every pattern above is linear; the cap bounds the total work per request).
SCAN_MAX_CHARS = 16_384


def has_control_chars(value: str, allow_newlines: bool = False) -> bool:
    return bool((_CONTROL if allow_newlines else _CONTROL_STRICT).search(value))


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def tokens(text: str) -> list[str]:
    """Whole tokens of normalized text; '#' is kept as part of a token."""
    return re.findall(r"#?[^\W_]+", normalize(text))


def scan_injection(text: str) -> list[str]:
    head = text[:SCAN_MAX_CHARS]
    return [name for name, rx in _INJECTION if rx.search(head)]


def iter_strings(obj: Any, depth: int = 0) -> Iterator[str]:
    if depth > 40:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from iter_strings(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from iter_strings(v, depth + 1)


def injection_rules_in(obj: Any) -> list[str]:
    found: set[str] = set()
    for s in iter_strings(obj):
        found.update(scan_injection(s))
    return sorted(found)


_ENTITY = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " "}
_BLOCKS = ("script", "style", "noscript")


def _strip_markup(text: str) -> str:
    """Linear-time tag stripper (str.find only, no backtracking regex): drops
    <script>/<style>/<noscript> blocks with their content, then every tag.
    An unclosed block or tag drops the rest of the document (fail safe:
    unreadable markup never becomes 'text')."""
    out: list[str] = []
    low = text.lower()
    i, n = 0, len(text)
    while i < n:
        lt = text.find("<", i)
        if lt < 0:
            out.append(text[i:])
            break
        out.append(text[i:lt])
        gt = text.find(">", lt + 1)
        if gt < 0:
            break
        name = low[lt + 1:gt].split(None, 1)[0].rstrip("/") if gt > lt + 1 else ""
        if name in _BLOCKS:
            end = low.find(f"</{name}", gt + 1)
            if end < 0:
                break
            close = text.find(">", end)
            if close < 0:
                break
            i = close + 1
        else:
            i = gt + 1
        out.append(" ")
    return "".join(out)


def normalized_page_text(raw: bytes) -> str:
    """Change Watcher normalization (spec C.4): decode, strip scripts/styles
    and tags, NFKC, collapse whitespace. Deterministic; no HTML engine."""
    text = _strip_markup(raw.decode("utf-8", errors="replace"))
    for k, v in _ENTITY.items():
        text = text.replace(k, v)
    return " ".join(unicodedata.normalize("NFKC", text).split())


# --- CN-26 / §0.1.8: no money, no earnings claims, no guarantees ----------------------------------------------
_MONEY = [
    ("currency_symbol", re.compile(r"[$\u00a2-\u00a5\u20a0-\u20cf\uff04\ufe69\ufdfc]")),
    ("currency_code", re.compile(r"(?i)\b(usd|eur|gbp|cad|aud|jpy|inr|mxn|brl|usdt|usdc|btc|eth|dollars?|euros?|pounds? sterling|bucks|cents?)\b")),
    ("money_amount", re.compile(r"\b\d{1,3}(?:[,.]\d{3})*[.,]\d{2}\b|\b\d+\s?(?:k|m)\b(?!\w)")),
    ("earnings_word", re.compile(r"(?i)\b(earn|earns|earned|earning|earnings|income|profit|profits|paycheck|salary|wage|wages|"
                                 r"cash|money|payout|payouts|paid|payment|payments|cpm|rpm|per\s+(?:1k|1,?000|thousand|million)\s+views|"
                                 r"make\s+\w+\s+(?:a|per)\s+(?:day|week|month)|side\s+hustle|passive|rich|get\s+paid)\b")),
    ("guarantee_word", re.compile(r"(?i)\b(guarantee|guarantees|guaranteed|promise|promised|promises|assured|risk[- ]free|"
                                  r"certain(?:ly)?\s+(?:to|will)|will\s+go\s+viral|viral\s+guaranteed)\b")),
]


# Lookalike -> Latin, lower case (bug sweep C: the CN-26 money blocklist was bypassed with homoglyphs, "еarn cаsh"
# with Cyrillic е/а). The visually near-identical Cyrillic / Greek / Armenian / Cherokee / IPA entries of Unicode
# confusables.txt, copied from creative-py's ``shared/text.py`` CONFUSABLES (the shared table every service that folds
# lookalikes copies; services do not import each other), plus every "LATIN ... LETTER <X> [WITH ...]" letter, generated
# from ``unicodedata`` as creative-py does.
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
    "ꭇ": "r", "ꞃ": "r", "ꝛ": "r", "ᵹ": "g", "ꞅ": "s", "ꜧ": "h", "ꞇ": "t", "ꝺ": "d", "ꝼ": "f",
    "ԍ": "g", "ԃ": "d", "ԋ": "h", "ԏ": "t", "ӡ": "3", "ҽ": "e", "ҿ": "e", "ᴫ": "n",
    "ϝ": "f", "ϻ": "m", "ϙ": "q", "ͱ": "h", "ͷ": "n",
    # Cherokee (after casefold Cherokee small letters U+AB70.. become U+13A0..)
    "Ꭰ": "d", "Ꭱ": "r", "Ꭲ": "t", "Ꭵ": "i", "Ꭹ": "y", "Ꭺ": "a", "Ꭻ": "j", "Ꭼ": "e", "Ꮃ": "w",
    "Ꮇ": "m", "Ꮋ": "h", "Ꮍ": "y", "Ꮐ": "g", "Ꮒ": "h", "Ꮓ": "z", "Ꮟ": "b", "Ꮢ": "r", "Ꮤ": "w",
    "Ꮥ": "s", "Ꮩ": "v", "Ꮪ": "s", "Ꮮ": "l", "Ꮯ": "c", "Ꮲ": "p", "Ꮶ": "k", "Ꮷ": "d", "Ᏻ": "g",
    "Ᏼ": "b", "Ꮻ": "o", "Ꮎ": "o",
}
_LATIN_NAME = re.compile(
    r"LATIN (?:SMALL CAPITAL |SMALL |CAPITAL )?LETTER (?:SMALL CAPITAL |SCRIPT |DOTLESS |LONG )?([A-Z])(?: WITH .+)?")


def _generated_latin_folds() -> dict[str, str]:
    out: dict[str, str] = {}
    for cp in range(0x80, 0x20000):
        ch = chr(cp)
        m = _LATIN_NAME.fullmatch(unicodedata.name(ch, ""))
        if m:
            out[ch] = m.group(1).lower()
            if len(ch.casefold()) == 1:
                out.setdefault(ch.casefold(), m.group(1).lower())
    return out


_FOLD_TABLE = str.maketrans({**_generated_latin_folds(), **CONFUSABLES})


def fold_for_matching(text: str) -> str:
    """The text a blocklist is matched against (bug sweep C): NFKC (fullwidth, ligatures, mathematical letters),
    every format / Default_Ignorable character removed (zero-width space and joiners, soft hyphen, bidi controls,
    variation selectors), diacritics stripped (NFKD, combining marks dropped), casefold, lookalike letters mapped to
    the Latin letter they imitate. Currency signs and digits are left as they are (their own patterns match them)."""
    t = unicodedata.normalize("NFKC", text)
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cf" and ch not in "\u034f\u115f\u1160\u3164\uffa0")
    t = "".join(ch for ch in unicodedata.normalize("NFKD", t) if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", t).casefold().translate(_FOLD_TABLE)


def money_or_earnings(text: str) -> list[str]:
    """Names of the money / earnings / guarantee patterns found in ``text``. Bug sweep C: matched against
    ``fold_for_matching(text)`` (NFKC + invisible characters dropped + diacritics + confusables), and against the plain
    NFKC text too, so neither "еarn" (Cyrillic е), "g\u200buaranteed" (zero-width space) nor "éarn" slips through.
    Empty list = clean."""
    if not isinstance(text, str):
        return []
    head = text[:SCAN_MAX_CHARS]
    folded = fold_for_matching(head)
    views = (unicodedata.normalize("NFKC", head), folded)
    hits = [name for name, rx in _MONEY if any(rx.search(v) for v in views)]
    # AEGIS L-4: the WORD patterns are also matched on the obfuscation-collapsed view (sales-py S1-H3 / influencer
    # i07 normaliser): leetspeak inside a word that also has letters ("guarant33d", "gu4r4nteed"), punctuation
    # between letters ("e.a.r.n") and runs of 3+ single letters ("g u a r a n t e e d", "c a s h") are folded. The
    # symbol and amount patterns are not (a "$" or "5" folded to a letter would hide or invent money)
    word = _collapse(folded)
    hits += [name for name, rx in _MONEY if name in _WORD_PATTERNS and name not in hits and rx.search(word)]
    return [name for name, _ in _MONEY if name in hits]


_WORD_PATTERNS = ("currency_code", "earnings_word", "guarantee_word")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})


def _collapse(t: str) -> str:
    t = re.sub(r"(?<=[^\W\d_])[\u2028\u2029](?=[^\W\d_])", "", t)   # a line separator inside a word joins it
    t = re.sub(r"[\u2028\u2029\s]+", " ", t)
    words = []
    for w in t.split(" "):
        # leet inside a word that also has letters: g4in -> gain (a bare number such as 2024 stays a number)
        words.append(w.translate(_LEET) if any(c.isalpha() for c in w) and any(c in "0134 57@$" for c in w) else w)
    t = " ".join(words)
    t = re.sub(r"(?<=[^\W\d_])[^\w\s]+(?=[^\W\d_])", "", t)        # e.a.r.n -> earn
    out, run = [], []
    for w in t.split() + [""]:                                        # g u a r a n t e e d -> guaranteed
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        out += ["".join(run)] if len(run) >= 3 else run
        run = []
        if w:
            out.append(w)
    return " ".join(out)


# --- display names (AEGIS N16-11) -------------------------------------------------------------------------------
DISPLAY_NAME_MAX = 80
_NAME_PUNCT = " .'-"
_DOMAINISH = re.compile(r"[^\W_]\.[^\W\d_]{2,}")          # "evil.example", "www.x.com" (not "J.R. Smith")


def display_name_problem(value: str) -> str | None:
    """Why ``value`` is not an acceptable clipper display name, or None. A display name is rendered into ZBC's own
    messages, so it may hold only letters (any script, with their combining marks), digits, the space and
    ``. ' -``; at least one letter; at most 80 characters; no format/bidi controls (Unicode Cf), no line or
    paragraph separators, no URL- or domain-like text, no money or earnings words (CN-26)."""
    if not isinstance(value, str) or not value or len(value) > DISPLAY_NAME_MAX:
        return f"1..{DISPLAY_NAME_MAX} characters"
    if value != value.strip() or "  " in value:
        return "no leading, trailing or doubled spaces"
    for ch in value:
        cat = unicodedata.category(ch)
        if cat[0] in "LM" or cat == "Nd" or ch in _NAME_PUNCT:
            continue
        return f"character U+{ord(ch):04X} ({cat}) is not a letter, digit, space or . ' -"
    if not any(unicodedata.category(ch)[0] == "L" for ch in value):
        return "a name needs at least one letter"
    if _DOMAINISH.search(value) or re.search(r"(?i)\b(?:https?|www)\b", value):
        return "no web addresses in a display name"
    if money_or_earnings(value):
        return "no money or earnings words (CN-26)"
    return None


def escape_for_channel(value: str, channel: str) -> str:
    """Clipper-supplied text placed into a message body, escaped for the channel's format: email and in-app
    bodies are HTML (a messaging provider must send them as such), Discord posts are Markdown."""
    if channel in ("email", "in_app"):
        return html.escape(value, quote=True)
    if channel == "discord_server_post":
        return re.sub(r"([\\*_~`|>\[\]()#])", r"\\\1", value)
    raise ValueError("unknown channel")


_PHONE = re.compile(r"^\s*(?:\+|00)?[\d\s().\-]{7,24}\s*$")
_EMAIL = re.compile(r"^[A-Za-z0-9.!#%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


def looks_like_phone(value: str) -> bool:
    return isinstance(value, str) and bool(_PHONE.match(value)) and sum(c.isdigit() for c in value) >= 7


def is_email(value: str) -> bool:
    return isinstance(value, str) and len(value) <= 254 and bool(_EMAIL.match(value))


def normalize_email(value: str) -> str:
    """Lowercase + NFKC, no dot/plus folding (the V&I C.7 normalization)."""
    return unicodedata.normalize("NFKC", value).strip().lower()
