"""Priya gate, stage 1 building blocks: the canonical view and the safe shapes (priya-spec/2 2.1, 2.5).

Pure functions of their input and the frozen data file ``confusables-15.1.0.txt`` (UTS #39, Unicode 15.1.0, sha256
pinned below; vendored byte-identical from Wave-1 b1611751, ruling CL-5). No network, no clock.

canonical(q)  CAN-1 NFKC + casefold; CL-1 accent fold (NFKD, drop Mn, NFC) together with CAN-2 (drop Cc Cf Mn Me);
              CAN-3 every Nd digit to ASCII; CAN-4 for a token whose letters mix scripts (Common / Inherited ignored;
              CL-2 "Highly Restrictive" sets count as one script) every non-Latin letter whose confusable prototype
              is ASCII letters is replaced by that prototype. V, T and every query use this same function (CAN-6).
tokens(q)     CAN-5: the maximal runs of letters, marks and digits of canonical(q) ("_" and all else separate).
safe_spans(t) CL-7: on the canonical TEXT t, the half-open token-index spans (i, j) covered by a 2.5 safe shape.
og(q, V, T)   2.2 stage 1: ("PASS", None) or ("WITHHOLD", "OG-n"), n = the first firing rule in table order.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import itertools
import hashlib
import re
import unicodedata
import zlib
from array import array
from dataclasses import dataclass, field
from pathlib import Path

from agents.priya_numwords import NUMWORDS

CONFUSABLES_PATH = Path(__file__).with_name("confusables-15.1.0.txt")
CONFUSABLES_SHA256 = "8289f833e4cf78fde56b2080dc0e42934ef5182c9c3f4dd1fbdf2bced69fd5ed"
_DROP = frozenset(("Cc", "Cf", "Mn", "Me"))
# CL-2: UTS #39 Highly Restrictive - these script sets (and any single script) count as one script
_ONE_SCRIPT_SETS = (
    frozenset(("LATIN", "HAN", "HIRAGANA", "KATAKANA")),
    frozenset(("LATIN", "HAN", "BOPOMOFO")),
    frozenset(("LATIN", "HAN", "HANGUL")),
)
# first word of a Unicode character name -> script, where that word is not the script's own name
_NAME_SCRIPT = {"CJK": "HAN", "IDEOGRAPHIC": "HAN", "KANGXI": "HAN"}
# names whose first word marks a Common-script letter (ignored for the mixed test)
_COMMON_WORDS = frozenset(("KATAKANA-HIRAGANA", "MODIFIER"))

_SKELETON: dict[str, str] | None = None


class ConfusablesMismatch(RuntimeError):
    """The vendored confusables file does not match its pinned sha256 (fail closed: never guess a table)."""


def _skeleton() -> dict[str, str]:
    """Single code point -> its UTS #39 prototype, for prototypes made only of ASCII letters (lower-cased)."""
    global _SKELETON
    if _SKELETON is None:
        try:
            raw = CONFUSABLES_PATH.read_bytes()
        except OSError as exc:  # missing / unreadable = mismatch (NB-3)
            raise ConfusablesMismatch(str(CONFUSABLES_PATH)) from exc
        if hashlib.sha256(raw).hexdigest() != CONFUSABLES_SHA256:
            raise ConfusablesMismatch(str(CONFUSABLES_PATH))
        table: dict[str, str] = {}
        for line in raw.decode("utf-8").splitlines():
            body = line.split("#", 1)[0].strip()
            if not body:
                continue
            src, dst = (f.strip() for f in body.split(";")[:2])
            if " " in src:
                continue
            proto = "".join(chr(int(h, 16)) for h in dst.split()).casefold()
            if proto.isascii() and proto.isalpha():
                table[chr(int(src, 16))] = proto
        _SKELETON = table
    return _SKELETON


def script_of(ch: str) -> str:
    """The script of a letter ("" for a non-letter or a Common-script letter): ASCII and every character Unicode
    names LATIN ... -> LATIN; otherwise the first word of the character's name, CJK / IDEOGRAPHIC -> HAN."""
    if not unicodedata.category(ch).startswith("L"):
        return ""
    if ch.isascii():
        return "LATIN"
    word = unicodedata.name(ch, "").split(" ", 1)[0]
    if word in _COMMON_WORDS:
        return ""
    return _NAME_SCRIPT.get(word, word)


def mixed_script(token: str) -> bool:
    """True when the letters of ``token`` mix 2+ scripts under CL-2 (Common / Inherited / digits ignored)."""
    scripts = {s for s in map(script_of, set(token)) if s}
    if len(scripts) < 2:
        return False
    return not any(scripts <= allowed for allowed in _ONE_SCRIPT_SETS)


def _is_token_char(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "LMN"


def _base(q: str) -> str:
    """CAN-1, CL-1 + CAN-2, CAN-3."""
    s = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", q).casefold())
    s = "".join(c for c in s if unicodedata.category(c) not in _DROP)
    s = unicodedata.normalize("NFC", s)
    return "".join(
        str(unicodedata.decimal(c)) if unicodedata.category(c) == "Nd" else c for c in s
    )


def _token_bounds(text: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    start = -1
    for i, ch in enumerate(text):
        if _is_token_char(ch):
            if start < 0:
                start = i
        elif start >= 0:
            out.append((start, i))
            start = -1
    if start >= 0:
        out.append((start, len(text)))
    return out


def canonical(q: str) -> str:
    """The canonical view of ``q`` (CAN-1..CAN-4, CL-1, CL-2); separators are kept so safe shapes can be read."""
    s = _base(q)
    parts: list[str] = []
    last = 0
    for a, b in _token_bounds(s):
        tok = s[a:b]
        if mixed_script(tok):
            table = _skeleton()
            tok = "".join(
                table.get(c, c) if script_of(c) not in ("", "LATIN") else c for c in tok
            )
        parts.append(s[last:a])
        parts.append(tok)
        last = b
    parts.append(s[last:])
    return "".join(parts)


def tokens(q: str) -> list[str]:
    """CAN-5 tokens of canonical(q)."""
    text = canonical(q)
    return [text[a:b] for a, b in _token_bounds(text)]


# ---- 2.5 safe shapes (closed list; any change is a spec change) ----
# LP-3 closed unit / model-word lists of search_performance.py at 33472699 (_GLUED_AFTER, _GLUED_BEFORE) + 2.5 extras
_LP3 = (
    "gb mb tb kb ghz mhz khz hz w kw kwh wh v mv mah ah a ma mm cm km m ml l kg mg g lb lbs oz in ft hp fps mp k p "
    "pcs pk gal qt psi rpm nm th st nd rd ddr lpddr gddr pcie usb gen rtx gtx rx cr ps wifi hdmi ipv"
).split()
_EXTRA = "cm m km in ft yd lb kg g oz ml l mph kmh hp".split()
UNITS = tuple(sorted(set(_LP3) | set(_EXTRA), key=lambda u: (-len(u), u)))
_U = "(?:" + "|".join(UNITS) + ")"
# OQ-1 month names (tests/privacy_r18_corpus.py _MONTHS, the OQ-1 date class generator)
MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "enero",
    "mars",
    "oktober",
    "dec",
    "nov",
    "jan",
)
_MON = "(?:" + "|".join(sorted(MONTHS, key=lambda m: (-len(m), m))) + ")"
_Y = r"(?:19\d\d|20[0-3]\d)"
_MM = r"(?:0?[1-9]|1[0-2])"
_DD = r"(?:0?[1-9]|[12]\d|3[01])"
_DM = r"(?:0?[1-9]|[12]\d|3[01])"
MODEL_RE = re.compile(
    r"(?=[a-z0-9]{1,10}$)(?:[a-z]{1,4}\d+[a-z]{0,3}|\d{1,2}k)"
)  # CL-6: + \d{1,2}k only
_SHAPES = (
    rf"{_Y}([-/.]){_MM}\1{_DD}|{_DM}([-/.]){_DM}\2{_Y}|{_Y}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])",  # OQ-1 dates
    rf"{_MON} {_DD}(?: {_Y})?|{_DD} {_MON}(?: {_Y})?",  # month name + day
    r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)? ?(?:am|pm)?",  # hh:mm[:ss] [am|pm]
    r"(?:p|lt)?\d{3}[/ -]\d{2}(?:[/ -]?z?r|[/ -])\d{2}(?: (?:\d{2,3}(?:/\d{2,3})?[a-z]|xl|lt))?",  # tire size
    rf"\d+(?:\.\d+)?(?: ?x ?\d+(?:\.\d+)?){{1,2}}(?: ?{_U})?",  # dimension
    r"(?:[$\u20ac\u00a3\u00a5])(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d\d)?)"  # price, symbol first
    r"|(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d\d)?) (?:usd|eur)",  # price, code after
    r"v?\d+(?:\.\d+){1,3}",  # version
    rf"\d+ ?{_U}",  # quantity + unit
    r"\d+(?:st|nd|rd|th)",  # ordinal
    # CAL-5 (owner decision after CAL-4; spec 2.5 v2.1 owner-parameter update): closed, narrow additions
    r"(?:1[0-2]|0?[1-9]) ?(?:am|pm)",  # clock h am/pm (5pm, 11 am)
    r"(?:[01]?\d|2[0-3])h[0-5]\d",  # clock 24h with h (17h30)
    rf"{_Y}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])t(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?z?",  # ISO date-time
    rf"{_MON} {_Y}|{_Y} {_MON}",  # month name + year
    r"(?=[a-z0-9]{4,12}(?![^\W_]))(?:[a-z]{1,4}\d{2,5}[a-z]{1,3}\d{1,4}[a-z]?|\d{1,5}[a-z]{1,3}\d{2,4})",  # code
    r"[a-z]{2,4}-\d{2,6}",  # hyphenated code, letters-digits (abc-1234)
    _Y,  # year
    r"(?:[a-z]{1,4}\d+[a-z]{0,3}|\d{1,2}k)",  # model token (length <= 10 checked below)
)
_SHAPE_RES = tuple(re.compile(rf"(?<![^\W_])(?:{p})(?![^\W_])") for p in _SHAPES)


def _b_decodings(s: str) -> list[bytes]:
    """base32 (RFC 4648, case-insensitive, padding optional), base64 and base64url decodings of s that succeed."""
    out: list[bytes] = []
    try:
        out.append(base64.b32decode(s.upper() + "=" * (-len(s) % 8)))
    except (binascii.Error, ValueError):
        pass
    pad = "=" * (-len(s) % 4)
    try:
        out.append(base64.b64decode(s + pad, validate=True))
    except (binascii.Error, ValueError):
        pass
    try:
        out.append(base64.urlsafe_b64decode(s + pad))
    except (binascii.Error, ValueError):
        pass
    return out


def _decodes_to_at(tok: str) -> bool:
    """priya-spec/2.1 2.5 (owner ~18:20 PT): a model-token-shaped token is NOT safe when a decoding of it or of it
    reversed (base32 / base64 / base64url) is printable ASCII containing "@"."""
    for s in (tok, tok[::-1]):
        for d in _b_decodings(s):
            if d and b"@" in d and all(0x20 <= c < 0x7F for c in d):
                return True
    return False


def safe_spans(text: str) -> list[tuple[int, int]]:
    """Half-open token-index spans of the canonical ``text`` covered by a safe shape; overlapping spans merged."""
    bounds = _token_bounds(text)
    found: list[tuple[int, int]] = []
    for rx in _SHAPE_RES:
        for m in rx.finditer(text):
            if rx is _SHAPE_RES[-1] and (
                not MODEL_RE.fullmatch(m.group(0)) or _decodes_to_at(m.group(0))
            ):
                continue
            idx = [
                k for k, (a, b) in enumerate(bounds) if a >= m.start() and b <= m.end()
            ]
            if idx:
                found.append((idx[0], idx[-1] + 1))
    merged: list[tuple[int, int]] = []
    for i, j in sorted(found):
        if merged and i < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(j, merged[-1][1]))
        else:
            merged.append((i, j))
    return merged


# ---- 2.2 stage-1 rules OG-1..OG-8 (closed; the table order is the reason precedence) ----
OG_RULES = ("OG-1", "OG-2", "OG-3", "OG-4", "OG-5", "OG-6", "OG-7", "OG-8")
MAX_QUERY_CHARS = 512  # F-10 / OG-8
# priya-spec/1.1 C-1 closed lists, = search_performance.AT_WORDS / DOT_WORDS (a test pins the equality)
AT_WORDS = (
    "at",
    "arroba",
    "arobase",
    "chiocciola",
    "klammeraffe",
    "at-zeichen",
    "malpa",
    "kukac",
    "snabel-a",
    "apenstaartje",
    "sobaka",
)
DOT_WORDS = (
    "dot",
    "punto",
    "point",
    "ponto",
    "punkt",
    "kropka",
    "pont",
    "prikk",
    "piste",
    "nokta",
    "titik",
)
# OG-7 unsegmented scripts; every other script is space-separated (OG-6)
UNSEGMENTED = frozenset(
    ("HAN", "HIRAGANA", "KATAKANA", "HANGUL", "THAI", "LAO", "KHMER", "MYANMAR")
)
# the space-separated scripts of V's 42 languages (2.3 V-1); a token of any other one fires OG-6
V_SCRIPTS = frozenset(
    ("LATIN", "CYRILLIC", "GREEK", "ARABIC", "HEBREW", "DEVANAGARI", "BENGALI", "TAMIL")
)
_SEQS: dict[str, tuple[tuple[str, ...], ...]] = {}


def _seqs(name: str) -> tuple[tuple[str, ...], ...]:
    """Canonical token sequences of a closed word list, longest first (multi-token words: "at-zeichen")."""
    if name not in _SEQS:
        words = {
            "AT": AT_WORDS,
            "DOT": DOT_WORDS,
            "NUM": tuple(w for _, w in NUMWORDS),
        }[name]
        seqs = {tuple(tokens(w)) for w in words} - {()}
        _SEQS[name] = tuple(sorted(seqs, key=lambda x: (-len(x), x)))
    return _SEQS[name]


def _count(toks: list[str], seqs: tuple[tuple[str, ...], ...]) -> int:
    """Non-overlapping matches of the sequences in toks, scanning left to right, longest first."""
    n = i = 0
    while i < len(toks):
        for sq in seqs:
            if tuple(toks[i : i + len(sq)]) == sq:
                n += 1
                i += len(sq)
                break
        else:
            i += 1
    return n


def _scripts(tok: str) -> set[str]:
    return {s for s in map(script_of, set(tok)) if s}


_CJK = frozenset(("HAN", "HIRAGANA", "KATAKANA"))
_CJK_NUM: tuple[str, ...] | None = None  # the CJK number-list words, longest first; built once (GATE-3X, PF-1)


def _cjk_num() -> tuple[str, ...]:
    global _CJK_NUM
    if _CJK_NUM is None:
        _CJK_NUM = tuple(sorted(
            {sq[0] for sq in _seqs("NUM") if len(sq) == 1 and _scripts(sq[0]) & _CJK},
            key=lambda w: (-len(w), w),
        ))
    return _CJK_NUM


def _num_toks(toks: list[str]) -> list[str]:
    """OG-5 view of toks: a token with Han / kana letters (ja / zh write no spaces) is split greedily, longest
    list word first, into number-list words and single characters; every other token is kept as is."""
    cjk = _cjk_num()
    out: list[str] = []
    for t in toks:
        if not _scripts(t) & _CJK:
            out.append(t)
            continue
        i = 0
        while i < len(t):
            w = next((w for w in cjk if t.startswith(w, i)), t[i])
            out.append(w)
            i += len(w)
    return out


def _digits_only(tok: str) -> bool:
    return tok.isascii() and tok.isdigit()


def _og4b(toks: list[str], safe: set[int]) -> bool:
    """OG-4 (b) (priya-spec/2.1, owner 2026-11-14 15:40 PT): a run of >= 3 consecutive 1-character tokens (letter or
    digit, digit-only tokens included; outside a safe-shape span) where one of the 2 tokens just before the run is an
    OG-5 number word or a digit-only token - whatever the concatenation."""
    num = {sq[0] for sq in _seqs("NUM") if len(sq) == 1}
    start = None
    for k, t in enumerate(toks + [""]):  # "" closes the last run
        if len(t) == 1 and k not in safe:
            start = k if start is None else start
            continue
        if start is not None and k - start >= 3:
            if any(
                w in num or _digits_only(w) for w in toks[max(0, start - 2) : start]
            ):
                return True
        start = None
    return False


def _og_fired(q: str, vocab, tenant) -> list[str]:
    text = canonical(q)
    toks = [text[a:b] for a, b in _token_bounds(text)]
    safe = {k for i, j in safe_spans(text) for k in range(i, j)}

    def known(w: str) -> bool:
        return w in vocab or w in tenant

    out: list[str] = []
    if "@" in q or "@" in text:
        out.append("OG-1")
    if any(
        mixed_script(t)
        or (
            any(c.isdigit() for c in t)
            and any(script_of(c) not in ("", "LATIN") for c in t)
        )
        for t in toks
    ):
        out.append("OG-2")
    if _count(toks, _seqs("AT")) and _count(toks, _seqs("DOT")):
        out.append("OG-3")
    run: list[str] = []
    for k, t in enumerate(toks + [""]):  # "" closes the last run
        if t and k not in safe and len(t) <= 2 and not _digits_only(t):
            run.append(t)
            continue
        if len(run) >= 3 and not known("".join(run)):
            out.append("OG-4")
            break
        run = []
    if "OG-4" not in out and _og4b(toks, safe):
        out.append("OG-4")
    if _count(_num_toks(toks), _seqs("NUM")) >= 4:
        out.append("OG-5")
    for k, t in enumerate(toks):
        sc = _scripts(t)
        if k in safe or _digits_only(t) or not sc or sc & UNSEGMENTED:
            continue
        if (
            any(c.isdigit() for c in t)
            or sc - V_SCRIPTS
            or (sum(script_of(c) != "" for c in t) >= 2 and not known(t))
        ):
            out.append("OG-6")
            break
    unseg = [t for t in toks if _scripts(t) & UNSEGMENTED]
    if any("LATIN" in _scripts(t) for t in unseg) or (
        unseg and sum(unicodedata.numeric(c, None) is not None for c in text) >= 7
    ):
        out.append("OG-7")
    if len(q) > MAX_QUERY_CHARS:
        out.append("OG-8")
    return out


def og_fired(q: str, vocab, tenant=frozenset()) -> list[str]:
    """Every OG rule that fires on q, in table order (the verdict uses the first; PF-3 evaluation order is free).
    Fail closed (V-3, NB-3): a V that failed its pin, or a confusables file that fails its pin -> ["OG-6"].
    Lead ruling 12:00 PT (I-17): over the OG-8 bound -> ["OG-8"] before any other rule runs (bounded cost)."""
    if len(q) > MAX_QUERY_CHARS:
        return ["OG-8"]
    if getattr(vocab, "status", "OK") != "OK":
        return ["OG-6"]
    try:
        _skeleton()  # pin checked for EVERY query, not only those reaching CAN-4 (cached once it passes)
        return _og_fired(q, vocab, tenant)
    except ConfusablesMismatch:
        return ["OG-6"]


def og(q: str, vocab, tenant=frozenset()) -> tuple[str, str | None]:
    """2.2: ("PASS", None) or ("WITHHOLD", first firing OG rule id). vocab = V, tenant = T (canonical words)."""
    fired = og_fired(q, vocab, tenant)
    return ("WITHHOLD", fired[0]) if fired else ("PASS", None)


# ---- 2.3 V-3 / OG-PIN: the vocabulary artifact V, loaded once at service start ----
VOCAB_PATH = Path(__file__).with_name("priya_vocab.txt.gz")
VOCAB_SHA256 = "7239fa821cbec6631ea96ea2adb70fcc4e7fe6c6f7f266a8a4129195acec7c68"  # UNCOMPRESSED text (CL-4)
VOCAB_N = 1861560
GATE_STATUSES = ("OK", "VOCAB_MISMATCH", "CONFUSABLES_MISMATCH")


@dataclass(frozen=True, eq=False)
class Vocab:
    """V as loaded. status != "OK" -> empty and every query is WITHHELD OG-6 (fail closed); sha256 = observed text sha.
    PF-4: kept as the pinned UTF-8 text (one word per line, strictly sorted bytewise - a property of the pinned file)
    plus a uint32 line-start index; membership = binary search (no per-word str objects)."""

    status: str
    sha256: str | None
    text: bytes = b""
    starts: array = field(default_factory=lambda: array("I", [0]))

    def __len__(self) -> int:
        return len(self.starts) - 1

    def _word(self, i: int) -> bytes:
        return self.text[self.starts[i] : self.starts[i + 1] - 1]

    def __contains__(self, w: object) -> bool:
        if not isinstance(w, str) or not w:
            return False
        key = w.encode("utf-8", "surrogatepass")
        lo, hi = 0, len(self)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._word(mid) < key:
                lo = mid + 1
            else:
                hi = mid
        return lo < len(self) and self._word(lo) == key


def _bad(status: str, sha: str | None = None) -> Vocab:
    return Vocab(status, sha)


def load_vocab(
    path: Path | None = None,
    expected_sha256: str = VOCAB_SHA256,
    expected_n: int = VOCAB_N,
) -> Vocab:
    """Load and check V (never raises). Confusables are checked first (NB-3), then V's text sha256 and count."""
    try:
        _skeleton()
    except ConfusablesMismatch:
        return _bad("CONFUSABLES_MISMATCH")
    try:
        text = gzip.decompress(Path(path or VOCAB_PATH).read_bytes())
    except (OSError, EOFError, zlib.error):
        return _bad("VOCAB_MISMATCH")
    sha = hashlib.sha256(text).hexdigest()
    if sha != expected_sha256 or not text.endswith(b"\n"):
        return _bad("VOCAB_MISMATCH", sha)
    lines = text[:-1].split(b"\n")
    if len(lines) != expected_n or not all(lines):
        return _bad("VOCAB_MISMATCH", sha)
    starts = array("I", [0])
    starts.extend(itertools.accumulate(len(x) + 1 for x in lines))
    del lines
    return Vocab("OK", sha, text, starts)


def gate_status(vocab) -> str:
    """For /health and the run report: "OK", "VOCAB_MISMATCH" or "CONFUSABLES_MISMATCH" (one of GATE_STATUSES)."""
    status = getattr(vocab, "status", "OK")
    if status != "OK":
        return status
    try:
        _skeleton()
    except ConfusablesMismatch:
        return "CONFUSABLES_MISMATCH"
    return "OK"
