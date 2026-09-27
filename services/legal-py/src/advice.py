"""
Advice-text guard (Legal spec §A.3, LG-01). Deterministic; no model.

Every rendered outbound text (template fills, hold notices, routing notices, outbound DMCA notices) and every
template variable passes ``check`` before it can leave Legal; the counsel-approved template BODY is not scanned
(counsel wrote it). A hit refuses the output with ``ADVICE_TEXT_BLOCKED`` and the service records
``advice_text_blocked`` (pattern ids only, never the text). Legal-generated reason messages pass through the
same guard at construction (``reasons.item``), so a code path that tried to word a reason as advice fails loudly.

Normalization, applied before the patterns (``seed/advice_patterns.json``), is built to defeat the obvious
evasions:
  1. HTML entities decoded; NFKC (fullwidth, ligatures, compatibility forms);
  2. format characters (Unicode category Cf: zero-width spaces/joiners, bidi controls, soft hyphens) removed
     in one view and turned into spaces in another;
  3. a small confusables table folds Cyrillic/Greek look-alikes to Latin letters; case-fold;
  4. apostrophe variants unified and contractions expanded ("you're" -> "you are", "don't" -> "don t");
  5. everything that is not a letter (of any script), digit or apostrophe becomes a space; whitespace collapsed.
The patterns run on these views (for both format-character treatments): the normalized text; a leetspeak-folded copy (0->o, 1->i, 3->e, 4->a, 5->s,
7->t, @->a, $->s); and each run of >= 3 single-character tokens re-joined ("y o u  m u s t", "y-o-u m-u-s-t")
against the space-free form of every pattern. False positives are preferred to false negatives (a blocked
variable goes back to Andre; a leaked answer is a B&P §6126 problem).

The guard is DEFENSE IN DEPTH, not the control (AEGIS N17-7). The control is structural: no route answers a
non-Andre caller with caller-supplied free text (only ids, enums, dates, amounts, hashes and Legal's own reason
lines), document text leaves only through the blob store by hash, and every document version -- template fills
and SOWs included -- becomes current only after counsel signs off on its exact hash. A phrase list can always be
evaded; the structure cannot.
"""

from __future__ import annotations

import html
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "seed", "advice_patterns.json")
SCAN_MAX_CHARS = 65_536

_CONFUSABLES = {
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "ѕ": "s", "і": "i", "ј": "j", "ԁ": "d", "ɡ": "g", "ԛ": "q", "ԝ": "w", "ү": "y", "һ": "h",
    "ӏ": "l", "ᴜ": "u", "ս": "u", "ⅼ": "l",
    # Greek
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
    "γ": "y", "η": "n", "μ": "u", "ς": "s", "σ": "o", "ϲ": "c",
}
_APOS = dict.fromkeys(map(ord, "‘’‛ʼʻ`´′＇"), "'")
_CONTRACTIONS = [
    (r"\byou're\b", "you are"), (r"\byou've\b", "you have"), (r"\byou'll\b", "you will"),
    (r"\byou'd better\b", "you had better"), (r"\byou'd\b", "you would"), (r"\bu're\b", "u are"),
    (r"\bit's\b", "it is"), (r"\bthat's\b", "that is"), (r"\bthis's\b", "this is"), (r"\bwe're\b", "we are"),
    (r"\bthey're\b", "they are"), (r"\bisn't\b", "is not"), (r"\baren't\b", "are not"), (r"\bwasn't\b", "was not"),
    (r"\bweren't\b", "were not"), (r"\bdoesn't\b", "does not"), (r"\bwouldn't\b", "would not"),
    (r"\bcan't\b", "cannot"), (r"\bwon't\b", "will not"), (r"\bi'm\b", "i am"), (r"\bi'd\b", "i would"),
    (r"\by'all\b", "y all"), (r"\bya'll\b", "y all"),
]
_CONTRACTIONS_RX = [(re.compile(p), r) for p, r in _CONTRACTIONS]
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "|": "l"})
# letters of every script survive (AEGIS N17-7: non-English advice -- "Sie müssen", "你应该" -- is not erased)
_NON_WORD = re.compile(r"(?:[^\w']|_)+")


def _fold(text: str, cf: str = "") -> str:
    t = html.unescape(text[:SCAN_MAX_CHARS])
    t = unicodedata.normalize("NFKC", t)
    t = "".join(cf if unicodedata.category(ch) == "Cf" else ch for ch in t)
    t = t.casefold()
    t = "".join(_CONFUSABLES.get(ch, ch) for ch in t)
    return t.translate(_APOS)


def _words(t: str) -> str:
    for rx, rep in _CONTRACTIONS_RX:
        t = rx.sub(rep, t)
    t = t.replace("n't", " not")
    t = _NON_WORD.sub(" ", t).replace("'", " ")
    return " ".join(t.split())


def normalize(text: str) -> str:
    """The guard's normalized view of ``text`` (exposed for tests)."""
    return _words(_fold(text))


def _spaced_runs(norm: str) -> list[str]:
    runs, cur = [], []
    for tok in norm.split(" "):
        if len(tok) == 1:
            cur.append(tok)
        else:
            if len(cur) >= 3:
                runs.append("".join(cur))
            cur = []
    if len(cur) >= 3:
        runs.append("".join(cur))
    return runs


@dataclass(frozen=True)
class Pattern:
    pid: str
    rx: re.Pattern
    compact: re.Pattern
    examples: tuple


def _compact(regex: str) -> re.Pattern:
    return re.compile(regex.replace(r"\b", "").replace(" ", ""))


class AdviceGuard:
    def __init__(self, patterns: list[Pattern], seed_sha256: str):
        if not patterns:
            raise RuntimeError("advice guard has no patterns; refusing to start")
        self.patterns = patterns
        self.seed_sha256 = seed_sha256

    @classmethod
    def load(cls, raw: bytes) -> "AdviceGuard":
        import hashlib
        doc = json.loads(raw)
        pats = []
        for p in doc["patterns"]:
            if not re.fullmatch(r"AP-[0-9]{2}", p["id"]):
                raise RuntimeError("advice pattern id format")
            pats.append(Pattern(p["id"], re.compile(p["regex"]), _compact(p["regex"]), tuple(p.get("examples", ()))))
        return cls(pats, hashlib.sha256(raw).hexdigest())

    def scan(self, text: str) -> list[str]:
        """Pattern ids that match ``text`` (empty list = passes)."""
        if not isinstance(text, str) or not text:
            return []
        views, runs = [], []
        for cf in ("", " "):           # a format character removed (joins "leg\u200bally") and as a space
            folded = _fold(text, cf)
            for v in (_words(folded), _words(folded.translate(_LEET))):
                views.append(v)
                runs += _spaced_runs(v)
        hits = []
        for p in self.patterns:
            if any(p.rx.search(v) for v in views) or any(p.compact.search(r) for r in runs):
                hits.append(p.pid)
        return hits

    def scan_all(self, texts: Iterable[str]) -> list[str]:
        found: set[str] = set()
        for t in texts:
            found.update(self.scan(t))
        return sorted(found)


_DEFAULT: Optional[AdviceGuard] = None


def default_guard() -> AdviceGuard:
    """The guard loaded from the pinned seed (config checks the pin at start-up)."""
    global _DEFAULT
    if _DEFAULT is None:
        with open(DEFAULT_PATH, "rb") as fh:
            _DEFAULT = AdviceGuard.load(fh.read())
    return _DEFAULT
