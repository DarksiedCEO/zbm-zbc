"""core-spec/1 O-2, the suite's INDEPENDENT oracle: does a text hold "the label's own words"? Decided from the suite's
own copy of the closed lists (``core_ref``) and the boundary rule of ADR 0051 decision 73 written out here, never from
``agents.secret_guard`` (Latin letters are read through the project's ``lookalikes`` table, a dependency of the guard,
not the guard).

A text holds the label's own words when, on its folded form, an entry of E stands in it in a spelling of one LISTED
class of section 2.1 (a combination outside the list is not one, P-2):
  whole    the label's words as whole words of their script (no letter of the script before or after), apart by
           nothing, one space, "_", "-", a no-break space or an F2 join (1-3 of . / : , + | - _ or a digit, " - "),
           a declared proclitic before / ending after                                    SP-01..19, 23, 32, 34, 35
  spelled  every unit apart by the same 1-3 characters that are no letter; or each word spelled with "_" and the
           words apart by a space                                                         SP-20..22, 24, 40
  split    the whole form with ONE space, "_", "-" or "." inside one word                 SP-25
  before   letters of the script glued before the label (whole / spelled), the label ending a word    SP-26, 27, 29
  after    the label (whole / spelled) starting a word, letters glued after it            SP-28, 29
  inside   a Han / kana / Thai label anywhere in an unspaced run                          SP-31
The folded form: NFKC, invisible characters and the tatweel dropped, lower case (the final sigma stays final), Greek
and Latin accents and the Arabic / Hebrew vowel points dropped (the hamza and madda stay: they make other letters),
the marks of the marked scripts (Devanagari, Thai) kept. The Latin long labels are read with every non-letter removed
(L-2); the short ones as a whole letter run, the last camelCase piece, spelled or split once."""
import itertools
import re
import unicodedata

import lookalikes

from conformance import core_ref as R

_DROP = dict.fromkeys(map(ord, "\u0640\u200b\u200c\u200d\u2060\u00ad\ufeff\u034f"))
_KEEP_MARKS = frozenset(map(chr, (0x653, 0x654, 0x655)))
_MARKED = ("DEVANAGARI", "THAI", "KATAKANA", "HIRAGANA")
F2 = r"(?: - |[./:,+|\-_\d]{1,3})"
SEP_WORDS = rf"(?:[ _\-\u00a0\n\r\u2028\u2029\u0085]|{F2})?"
SPLIT = "[ _\\-.\u00a0\n\r\u2028\u2029\u0085]"
GAP = r"[^\w]{1,4}|_{1,3}|\d{1,3}"
_N = itertools.count()
_FOLD = lookalikes.Table()
_LAT = {"\u0131": "i", "\u0142": "l", "\u00f8": "o", "\u0111": "d"}


def _script(c: str) -> str:
    return unicodedata.name(c, "?").split(" ")[0].split("-")[0]


def fold(s: str, keep_marks: bool = False) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_DROP).lower()
    if not keep_marks:
        s = "".join(c for c in unicodedata.normalize("NFD", s)
                    if unicodedata.category(c)[0] != "M" or c in _KEEP_MARKS)
        s = unicodedata.normalize("NFC", s)
    return s


def latin(s: str) -> str:
    f = _FOLD.fold_cased(unicodedata.normalize("NFKC", s).translate(_DROP))
    f = "".join(_LAT.get(c, c) for c in f)
    f = "".join(c for c in unicodedata.normalize("NFKD", f) if not unicodedata.combining(c))
    return _FOLD.fold_cased(f)


def _units(w: str) -> list:
    out: list = []
    for c in w:
        if out and unicodedata.category(c)[0] == "M":
            out[-1] += c
        elif c.isalpha():
            out.append(c)
    return out


def _w(w: str, end: bool = True) -> str:
    """A folded word as a pattern: a final sigma of the entry may be written medial (glued, upper case), and a sigma
    ending the entry's word may be written final (lower case writes it so at a word's end)."""
    return "".join("[\u03c2\u03c3]" if c == "\u03c2" or (end and c == "\u03c3" and i == len(w) - 1) else re.escape(c)
                   for i, c in enumerate(w))


def _letters_class(script: str) -> str:
    return {"CYRILLIC": "\u0400-\u052f", "GREEK": "\u0370-\u03ff\u1f00-\u1fff", "ARABIC": "\u0600-\u06ff\u0750-\u077f",
            "HEBREW": "\u0590-\u05ff", "HANGUL": "\u1100-\u11ff\u3130-\u318f\uac00-\ud7af",
            "DEVANAGARI": "\u0900-\u097f", "LATIN": "a-z"}[script]


def _entry_patterns(e: str, eid: str) -> list:
    script = next((_script(c) for c in e if c.isalpha() and not c.isascii()), "LATIN")
    marked = script in _MARKED
    words = [fold(w, marked) for w in e.split(" ")]
    tails = list(R.EL_ENDINGS) if eid == "S22" else {"S00": R.RU_ENDINGS, "S19": R.HI_ENDINGS}.get(
        eid, R.KO_PARTICLES if eid in ("S12", "S13", "S14", "S15") else ("",))
    tails = sorted({fold(t, marked) for t in tails}, key=len, reverse=True)
    pro = {"S16": R.AR_PROCLITICS, "S17": R.AR_PROCLITICS, "S21": R.HE_PROCLITICS}.get(eid)
    pre = "" if pro is None else f"[{re.escape(fold(pro[0]))}]{{0,{pro[1]}}}"
    end = "(?:" + "|".join(_w(t) for t in tails) + ")" if tails != [""] else ""
    us = _units("".join(words))
    if script in ("CJK", "HIRAGANA", "KATAKANA", "THAI"):
        return [f"(?:{GAP})?".join(_w(u) for u in us)]
    L = f"[{_letters_class(script)}]"
    whole = pre + SEP_WORDS.join(_w(w) for w in words) + end

    def spelled() -> str:
        g = f"g{next(_N)}"
        return _w(us[0]) + f"(?P<{g}>{GAP})" + f"(?P={g})".join(_w(u) for u in us[1:]) + end
    alts = [whole, " ".join("_".join(_w(u) for u in _units(w)) for w in words)]
    for wi, w in enumerate(words):
        wu = _units(w)
        for k in range(1, len(wu)):
            ws = [_w(x) for x in words]
            ws[wi] = _w("".join(wu[:k]), False) + SPLIT + _w("".join(wu[k:]))
            alts.append(pre + "[ _\\-\u00a0]?".join(ws) + end)
    two = len(us) < 3                                     # (a two-unit label spelled: a word of its own, P-6e/f)
    core = "(?:" + "|".join(alts + [spelled()]) + ")"
    out = [f"(?<!{L}){core}(?!{L})"]
    out += [f"{L}+(?:{whole})(?!{L})", f"(?<!{L})(?:{whole}){L}+"]
    if not two:
        out += [f"{L}+(?:{spelled()})(?!{L})", f"(?<!{L})(?:{spelled()}){L}+"]
    return out


def _build():
    pats = []
    for eid, e, kind, _sc, _g in R.entries():
        if kind != "joined":
            pats += _entry_patterns(e, eid)
    for w in R.EL_WRITTEN:
        pats += _entry_patterns(w, "S22")
    pats += _entry_patterns(R.KO_APART, "S12")             # (S12's two words apart, with its particles)
    for e, ns in R.NATURAL.items():
        if e in R.PIECES:
            for n in ns:
                pats += _entry_patterns(latin(n), "P")
    return re.compile("|".join(f"(?:{p})" for p in pats))


_RX = _build()
_JOINED = sorted({"".join(c for c in latin(x) if c.isalpha()) for x in R.JOINED} |
                 {"".join(c for c in latin(n) if c.isalpha()) for e, ns in R.NATURAL.items() if e not in R.PIECES
                  for n in ns}, key=len, reverse=True)
_PIECES = frozenset(R.PIECES)
_WORD = re.compile(r"[^\W\d_]+")


def own_words(text: str) -> bool:
    lat = latin(text)
    if _RX.search(fold(text)) or _RX.search(fold(text, True)) or _RX.search(lat):
        return True
    if any(k in "".join(c for c in lat if c.isalpha()) for k in _JOINED):
        return True
    for m in _WORD.finditer(unicodedata.normalize("NFKC", text).translate(_DROP)):
        w = m.group(0)
        k = len(w) - 1
        while k > 0 and not (w[k - 1].islower() and w[k].isupper()):
            k -= 1
        if k > 0 and latin(w[k:]) in _PIECES:
            return True
    return False
