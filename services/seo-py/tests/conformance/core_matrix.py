"""The label matrix of core-spec/1 section 2 (M-1..M-6, P-1): cells, their REQUIRED verdicts by rule (never by what
a release read: M-5), the stratified CI sample (M-4) and the full heavy enumeration.

A cell = entry x SP x form x separator class / variant x placement x Unicode form x a run-time value. Verdict codes:
"R" must be read, "N" must not be read, "O" open, "X:<r>" declared residual r (section 7)."""
import hashlib
import json
import random
import unicodedata
from pathlib import Path

from conformance import core_forms as F
from conformance import core_ref as R

SEED = 51
DATA = Path(__file__).resolve().parent / "data"

# ------------------------------------------------------------------------------------------------ SP table (2.1)
# sp -> (STRICT, CV); per-entry exceptions are applied in ``sp_verdict``
SP_VERDICT = {
    1: ("R", "R"), 2: ("R", "R"), 3: ("R", "R"), 4: ("R", "R"), 5: ("R", "R"), 6: ("X:d3", "R"), 7: ("R", "R"),
    8: ("R", "R"), 9: ("R", "R"), 10: ("R", "R"), 11: ("R", "R"), 12: ("X:b", "X:b"), 13: ("R", "R"),
    14: ("R", "R"), 15: ("R", "R"), 16: ("R", "X:d6"), 17: ("R", "R"), 18: ("R", "R"), 19: ("R", "R"),
    20: ("R", "R"), 21: ("X:d5", "R"), 22: ("R", "O"), 23: ("R", "O"), 24: ("R", "R"), 25: ("R", "O"),
    26: ("R", "R"), 27: ("R", "R"), 28: ("R", "O"), 29: ("R", "O"), 30: ("X:hi", "X:hi"), 31: ("R", "R"),
    32: ("R", "R"), 33: ("X:71", "X:71"), 34: ("R", "R"), 35: ("R", "O"), 36: ("R", "R"), 37: ("R", "X:hg"),
    38: ("R", "R"), 39: ("R", "R"), 40: ("R", "R"),
}
INWORD = frozenset((26, 27, 28, 29, 30, 31))
BARE_WHOLE = frozenset(range(1, 25)) | {32}          # M-2: whole-word forms read bare by STRICT, never by CV
LATIN_KINDS = ("joined", "piece")


def applies(eid: str, kind: str, sp: int, e: str) -> bool:
    """M-1: the applicability table of the SP rows."""
    lat = kind in LATIN_KINDS
    scr = not lat
    hist = eid.startswith("H")
    multi = eid in F.MULTI or eid == "S12" or bool(F.multi_bases(eid))
    table = {
        1: True, 2: True, 3: True, 4: True, 5: True, 6: kind == "piece" and len(e) <= 4,
        7: lat or eid in ("S09", "S11"), 8: lat and "i" in e,
        9: lat and any("ss" in b for b in F.bases(eid)), 10: lat and any(c in e for c in "lod"),
        11: (lat and e in R.LATIN_MARKED) or eid in F.GREEK or eid in ("S16", "S17", "S18"),
        12: eid in ("S09", "S19", "S20"), 13: lat or eid in F.ARABIC or eid == "S21", 14: eid == "S22",
        15: eid in F.ARABIC, 16: lat, 17: True, 18: multi and not hist, 19: eid in ("S01", "S16", "S17", "S18",
                                                                                  "S22", "F2") or hist,
        20: True, 21: e == "pw", 22: True, 23: multi and not hist, 24: eid in ("S01", "S16", "S17", "S18", "S22"),
        25: True, 26: (scr and eid not in ("S19", "F1", "F2")), 27: (scr and eid not in ("S19", "F1", "F2")),
        28: (scr and eid not in ("S19", "F1", "F2")), 29: (scr and eid not in ("S19", "F1", "F2")),
        30: eid == "S19", 31: eid in F.UNSPACED, 32: eid in ("S00", "S12", "S13", "S14", "S15", "S16", "S17",
                                                             "S19", "S21", "S22"),
        33: eid == "S22", 34: eid == "H4", 35: eid == "H4", 36: lat, 37: lat, 38: lat, 39: True, 40: lat,
    }
    return table[sp]


def lookalike_table(which: str) -> dict:
    """The frozen code points of SP-36 (c) ("set") and SP-37 ("hg"): {ASCII letter: [code points]}."""
    d = json.loads((DATA / "lookalikes.json").read_text(encoding="ascii"))
    return {k: v for k, v in d[which].items()}


def forms(eid: str, sp: int) -> list:
    """The (tag, text) forms of one (entry, SP) (``core_forms``); in-word classes SP-26..31 included."""
    e, kind, _sc = F.by_id()[eid]
    if sp in (1, 2, 3, 4, 5):
        out = F._sp_case(eid, kind, sp)
    elif sp == 6:
        out = [("alt0", F.alternating(e, False)), ("alt1", F.alternating(e, True))]
    elif sp == 7:
        out = F._sp_compat(eid, kind)
    elif sp == 8:
        k = e.index("i")
        out = [("dotted", e[:k].upper() + "\u0130" + e[k + 1:].upper()), ("dotless", e[:k] + "\u0131" + e[k + 1:])]
    elif sp == 9:
        out = []
        for i, b in enumerate(F.bases(eid)):
            if "ss" in b:
                sh = b.replace("ss", "\u00df")
                out += [(f"b{i}.lower", sh), (f"b{i}.title", sh.title()), (f"b{i}.cap", b.upper().replace("SS", "\u1e9e")),
                        (f"b{i}.compound", "WLAN-" + sh.capitalize())]
    elif sp == 10:
        out = []
        for c, (lo, up) in (("l", ("\u0142", "\u0141")), ("o", ("\u00f8", "\u00d8")), ("d", ("\u0111", "\u0110"))):
            if c in e:
                out += [(f"{c}.lower", e.replace(c, lo, 1)), (f"{c}.upper", e.upper().replace(c.upper(), up, 1))]
    elif sp == 11:
        out = F._sp_marks_absent(eid, kind)
    elif sp == 12:
        out = [("nomarks", F.strip_marks(e))]
    elif sp == 13:
        out = F._sp_marks_added(eid, kind)
    elif sp == 14:
        out = F._sp_partial_accents()
    elif sp in (15, 16):
        out = F._sp_tatweel(eid, kind, sp)
    elif sp == 17:
        out = F._sp_joiners(eid)
    elif sp == 18:
        out = F._sp_apart(eid)
    elif sp == 19:
        out = F._sp_glued(eid)
    elif sp == 20:
        out = F._sp_spelled(eid, kind, F.SPACERS)
    elif sp == 21:
        out = [(sn, s.join("pw")) for sn, s in F.SPACERS]
    elif sp == 22:
        out = F._sp_spelled(eid, kind, F._f3_seps())
    elif sp == 23:
        out = F._sp_f2(eid)
    elif sp == 24:
        out = F._sp_underscored_words(eid)
    elif sp == 25:
        out = F._split_forms(eid, kind)
    elif sp in (26, 27, 28, 29):
        out = F._inword(eid, sp, SEED)
    elif sp == 30:
        out = F._inword("S19", 26, SEED)[:30] + F._inword("S19", 28, SEED)[:30]
    elif sp == 31:
        out = F._sp_unspaced_inword(eid)
    elif sp == 32:
        out = F._sp_declared(eid)
    elif sp == 33:
        a = "\u03ba\u03c9\u03b4\u03b9\u03ba"
        b = "\u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7\u03c2"
        out = [("gen", a + "\u03bf\u03cd " + b), ("acc", a + "\u03cc " + b), ("pl", a + "\u03bf\u03af " + b)]
    elif sp in (34, 35):
        out = F._sp_h4(sp)
    elif sp == 36:
        out = F._sp_lookalike(eid) + F.lookalike_forms(eid, sp, lookalike_table("set"))
    elif sp == 37:
        out = F.lookalike_forms(eid, sp, lookalike_table("hg"))
    elif sp == 38:
        out = F._sp_cased_compounds(eid)
    elif sp == 39:
        out = F._sp_markup(eid)
    else:
        out = F._sp_letter_spaced(eid)
    seen: set = set()
    return [(t, x) for t, x in out if x and not (x in seen or seen.add(x))]


# ------------------------------------------------------------------------------------------------ SEP table (2.2)

def _cases(word: str) -> list:
    """Every upper / lower mix of ``word``'s letters (``was``: 8)."""
    out = [""]
    for c in word:
        out = [x + y for x in out for y in ((c.lower(), c.upper()) if c.isalpha() else (c,))]
    return list(dict.fromkeys(out))


def _four(word: str) -> list:
    return list(dict.fromkeys((word.lower(), word.upper(), word.title(), F.alternating(word, False))))


SEP1_BASE = (":", ": ", " : ", "=", " = ", "=>", "->", "\u2192", "\u21d2", "|", " | ", ">", "\uff1a")
CLOSE = ('"', "'", "]", ")", "*", "`")
OPEN = ('"', "'", "[", "(", "*", "`")


def _sep1() -> list:
    out = []
    for i, s in enumerate(SEP1_BASE):
        out.append((f"1.{i}", s))
        for k in range(1, 4):
            out.append((f"1.{i}.c{k}", CLOSE[(i + k) % 6] * k + s))
            out.append((f"1.{i}.o{k}", s + OPEN[(i + 2 * k) % 6] * k))
        out.append((f"1.{i}.co", CLOSE[i % 6] + s + OPEN[(i + 1) % 6]))
    return out


VERBS = ("was", "are", "would be", "will be", "remains")


def _verbs() -> list:
    out = []
    for v in VERBS:
        for k, c in enumerate(_cases(v) if v in ("was", "are") else _four(v)):
            out.append((f"{v.replace(' ', '_')}.{k}", f" {c} "))
    return out


SEPS = {
    "1": _sep1(),
    "2": [(f"2.{k}", s) for k, s in enumerate(_cases(" is ") + [" - ", " \u2013 ", " \u2014 "])],
    "3a": _verbs(),
    "3b": _verbs(),
    "4": [(f"4.{k}", s) for k, s in enumerate((" ", "\t", "\n", "  ", ", ", "; ", " / ", " -- ", "... ", " , "))],
    "4b": [(f"4b.{k}", s) for k, s in enumerate((" ", "\t", "\n", "  ", ", ", "; ", " / ", " -- ", "... ", " , "))],
    "bare": [("bare", "")],
}
SEP_CLASSES = tuple(SEPS)
PROSE = ("reset", "changed", "required", "updated", "accepted", "expired", "ready", "invalid", "missing", "sent")
VALUE_LETTERS = "bcdfghjkmnqrvxzBCDFGHJKMNQRVXZ"   # no vowel, p, s, t, w: a value never spells a label
VALUE_SYMBOLS = "!#%^*+?~"
PLACEMENTS = ("bare", "prose", "sentence")
NFS = ("NFC", "NFD")


def value(key: str) -> str:
    """A run-time valueish value (core-spec/1 section 0): 8-24 characters, letters, digits and one symbol."""
    r = random.Random(f"{SEED}:{key}")
    n = r.randint(8, 14)
    body = [r.choice(VALUE_LETTERS) for _ in range(n - 2)] + [str(r.randint(0, 9)), r.choice(VALUE_SYMBOLS)]
    head, rest = body[0], body[1:]
    r.shuffle(rest)
    return head + "".join(rest)


def sp_verdict(eid: str, sp: int, tag: str, lane: str) -> str:
    """The SP row's verdict for one form, with the rows' named exceptions."""
    s, c = SP_VERDICT[sp]
    e = F.by_id()[eid][0]
    if sp == 25 and eid == "S19" and ".k2." in tag:
        return "X:c"                                       # R-c: the Hindi label's two halves apart (PIN: N)
    if sp == 25 and eid == "S20" and ".k3." in tag:
        return "Q:SP25T"                                   # the Thai label's two words apart = the pinned Thai pairs
    if sp == 25 and ".sig.upper" in tag:
        return "Q:SP25S"                                   # UPPER split after a sigma = "CODE TO BAS-" (PIN)
    if lane == "strict":
        if e == "pw" and sp in (20, 22, 25, 40):
            return "X:d5"                                  # R-d5: "pw" spelled letter by letter
        return s
    if sp == 25 and (eid in ("S22", "H4", "H5", "S20")) and (".sp" in tag or ".nbsp" in tag) and "zwsp" not in tag:
        return "R"
    if sp == 29 and tag[:2] in ("a.", "b.", "d."):
        return "Q:SP29"
    if sp == 31 and (".after" in tag or ".both" in tag):
        return "Q:SP31"
    return c


# Open spec questions (never judged, reported): the cells whose required verdict the frozen text leaves undecided
#   Q:SP29  SP-29 client view "R where the in-word part alone is CV R (SP-26 / 27)": literally noun / letter before +
#           SP-22 / 23 is CV R, but SP-22 / 23 are CV O and P-5 states 0 client-view misses at 186471eb
#   Q:SP31  SP-31 client view for a Han / kana / Thai label with letters AFTER it in the run (SP-28, letters after, is
#           CV O; no release's client view reads them)
#   Q:SP25T SP-25 asks for S20 split by a space in both lanes; at its compound boundary ("code" | "pass") that is the
#           pinned Thai pair of O-4 PIN, which must stay N (bare and with a value)
#   Q:SP25S SP-25 asks for every split in UPPER; split right after a sigma, the Greek label in capitals is the
#           pinned "CODE TO BAS-" phrases of O-4 PIN (capitals do not tell a final sigma)
QUESTIONS = {"Q:SP29", "Q:SP31", "Q:SP25T", "Q:SP25S"}


def short_mixed(eid: str, sp: int, form: str) -> bool:
    """SP-36 (c): a lookalike whose compatibility form changes the case inside a short piece label is the SP-06 text
    (a short label in mixed case: X(d3) in the strict lane)."""
    e, kind, _sc = F.by_id()[eid]
    k = unicodedata.normalize("NFKC", form)
    return sp in (36, 37) and kind == "piece" and len(e) <= 4 and k.isascii() and k.lower() != k and k.upper() != k


def verdict(eid: str, sp: int, tag: str, sep_class: str, sep: str, lane: str) -> str:
    """The required verdict of one cell (2.1 x 2.2 x M-2)."""
    v = sp_verdict(eid, sp, tag, lane)
    if v.startswith("X"):
        return v
    if sp == 39:
        return v                                           # the form carries its own separator (SEP-1 shapes)
    if lane == "strict":
        if sep_class in ("1", "2", "3a", "4"):
            return v
        # SEP-3b / 4b (a prose word after): R as "the label word" is, i.e. as the bare form is read (M-2)
        return v if sp in BARE_WHOLE else "O"              # 3b, 4b, bare
    if sep_class in ("1", "2", "3a"):
        return v
    if sep_class == "3b":
        return "O" if sep.strip().isupper() else "N"
    if sep_class in ("4", "4b"):
        return "O"
    return "N" if sp in BARE_WHOLE else "O"


def cell_text(form: str, sep_class: str, sep: str, placement: str, nf: str, key: str) -> str:
    """One cell's text: the form, the separator and a run-time value, placed, in one Unicode form."""
    if "{v}" in form:
        body = form.replace("{v}", value(key))
    elif sep_class == "bare":
        body = form
    elif sep_class == "3b":
        w = PROSE[int(hashlib.sha256(key.encode()).hexdigest(), 16) % len(PROSE)]
        body = form + sep + (w.upper() if sep.strip().isupper() else w)
    else:
        body = form + sep + value(key)
    if placement == "prose":
        body = "Please note the following " + body
    elif placement == "sentence":
        body = "Please note the following " + body + (" for the account today." if sep_class == "bare"
                                                       else " and keep it safe today.")
    return unicodedata.normalize(nf, body)


class Cell:
    __slots__ = ("eid", "sp", "tag", "sep_class", "sep_id", "placement", "nf", "text", "strict", "cv")

    def __init__(self, eid, sp, tag, sep_class, sep_id, placement, nf, text, strict, cv):
        self.eid, self.sp, self.tag, self.sep_class, self.sep_id = eid, sp, tag, sep_class, sep_id
        self.placement, self.nf, self.text, self.strict, self.cv = placement, nf, text, strict, cv

    def key(self) -> str:
        return f"{self.eid}|SP{self.sp:02d}|{self.tag}|{self.sep_id}|{self.placement}|{self.nf}"


def triples():
    """Every (entry, SP, SEP class) the matrix holds, with its forms: (eid, sp, sep_class, forms)."""
    for eid, e, kind, _sc, _g in R.entries():
        for sp in range(1, 41):
            if not applies(eid, kind, sp, e):
                continue
            fs = forms(eid, sp)
            if not fs:
                continue
            for sc in (("own",) if sp == 39 else SEP_CLASSES):
                yield eid, sp, sc, fs


def _make(eid, sp, sc, fs, i) -> Cell:
    seps = SEPS["1"][:1] if sc == "own" else SEPS[sc]
    n_s, n_p, n_n = len(seps), len(PLACEMENTS), len(NFS)
    f, rest = divmod(i, n_s * n_p * n_n)
    s, rest = divmod(rest, n_p * n_n)
    p, n = divmod(rest, n_n)
    return _cell(eid, sp, sc, fs, f, s, p, n)


def _cell(eid, sp, sc, fs, f, s, p, n) -> Cell:
    seps = SEPS["1"][:1] if sc == "own" else SEPS[sc]
    tag, form = fs[f]
    sep_id, sep = seps[s]
    key = f"{eid}|SP{sp:02d}|{tag}|{sep_id}|{PLACEMENTS[p]}|{NFS[n]}"
    text = cell_text(form, sc, sep, PLACEMENTS[p], NFS[n], key)
    cls = "1" if sc == "own" else sc
    strict = "X:d3" if short_mixed(eid, sp, form) else verdict(eid, sp, tag, cls, sep, "strict")
    return Cell(eid, sp, tag, sc, sep_id, PLACEMENTS[p], NFS[n], text, strict, verdict(eid, sp, tag, cls, sep, "cv"))


def triple_size(sp, sc, fs) -> int:
    seps = SEPS["1"][:1] if sc == "own" else SEPS[sc]
    return len(fs) * len(seps) * len(PLACEMENTS) * len(NFS)


def judged(c: Cell) -> bool:
    return c.strict in ("R", "N") or c.cv in ("R", "N")


def ci_cells(per_triple: int = 10, only_sp=None):
    """M-4: the stratified CI sample: ``per_triple`` cells of every (entry, SP, SEP class) triple that is not X in
    both lanes (seeded; the same cells every run)."""
    for eid, sp, sc, fs in triples():
        if only_sp is not None and sp not in only_sp:
            continue
        n = triple_size(sp, sc, fs)
        r = random.Random(f"{SEED}:{eid}:{sp}:{sc}")
        idx = sorted(r.sample(range(n), min(per_triple, n)))
        cells = [_make(eid, sp, sc, fs, i) for i in idx]
        if any(judged(c) for c in cells):
            yield from cells


HEAVY_PER_FORM = 2                       # separator variants per (form, SEP class) in the heavy lane


def all_cells(only_sp=None, only_eid=None, shard=None):
    """The heavy lane's cells: EVERY form of every (entry, SP) x every SEP class x ``HEAVY_PER_FORM`` separator
    variants, the variants, placements and Unicode forms rotating over the forms so that each one is used across a
    triple (the full cross product, 1,152 cells per form, is about 456 million cells). ``shard``: (i, n)."""
    k = 0
    for eid, sp, sc, fs in triples():
        if (only_sp is not None and sp not in only_sp) or (only_eid is not None and eid not in only_eid):
            continue
        k += 1
        if shard is not None and k % shard[1] != shard[0]:
            continue
        seps = SEPS["1"][:1] if sc == "own" else SEPS[sc]
        off = int(hashlib.sha256(f"{eid}{sp}{sc}".encode()).hexdigest()[:6], 16)
        for f in range(len(fs)):
            for r in range(min(HEAVY_PER_FORM, len(seps))):
                s = (off + f * HEAVY_PER_FORM + r) % len(seps)
                yield _cell(eid, sp, sc, fs, f, s, (off + f + r) % len(PLACEMENTS), (off // 7 + f + r) % len(NFS))


def stream_hash(cells) -> str:
    """The manifest hash of a cell stream: sha256 over every cell's key and text (never stored)."""
    h = hashlib.sha256()
    for c in cells:
        h.update(c.key().encode("utf-8") + b"\0" + c.text.encode("utf-8") + b"\0")
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------ the lanes

def read_strict(sg, text: str) -> bool:
    return sg.check(text) == "a_label"


def read_cv(sg, text: str) -> bool:
    return "label_value" in sg.client_view_findings(text)


def masked_cv(sg, text: str) -> bool:
    return bool(sg.client_view_findings(text))


def judge(sg, cells) -> dict:
    """M-6: every R cell read, every N cell not read, in each lane. Returns counts by (lane, verdict, outcome) and the
    misses by (entry, SP, SEP class, lane, verdict) - ids only, never a text."""
    out = {"counts": {}, "misses": {}}
    for c in cells:
        for lane, v in (("strict", c.strict), ("cv", c.cv)):
            vv = v.split(":")[0]
            if lane == "strict":
                got = read_strict(sg, c.text)
            else:
                got = read_cv(sg, c.text)
            k = f"{lane}:{vv}:{'read' if got else 'unread'}"
            out["counts"][k] = out["counts"].get(k, 0) + 1
            if (vv == "R" and not got) or (vv == "N" and got):
                mk = f"{c.eid}|SP{c.sp:02d}|{c.sep_class}|{lane}|{vv}"
                out["misses"][mk] = out["misses"].get(mk, 0) + 1
    return out
