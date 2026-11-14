"""The label FORMS of core-spec/1 section 2.1: for one entry of E (``core_ref.entries``) and one spelling class SP,
the label spellings the class defines (no separator, no value). A pure function of (entry, class, seed): nothing is
read from ``agents.secret_guard``. ``core_matrix`` puts each form before a separator and a run-time value.

Each form is (tag, text, inword): ``tag`` names the variant (ASCII, no label word), ``inword`` is True for the
in-word classes SP-26..31 (they hold the label inside a longer written word)."""
import json
import random
import unicodedata
from pathlib import Path

from conformance import core_ref as R

DATA = Path(__file__).resolve().parent / "data"
WORDS = json.loads((DATA / "words.json").read_text(encoding="ascii"))["words"]
TATWEEL = "\u0640"
NBSP = "\u00a0"
JOINERS = (("zwj", "\u200d"), ("zwnj", "\u200c"), ("zwsp", "\u200b"), ("shy", "\u00ad"), ("wj", "\u2060"))
SPACERS = (("sp", " "), ("us", "_"), ("hy", "-"), ("dot", "."))
# SP-22 (F3): the separators between spelled units, each alone / space before / after / both
F3_SEPS = ("/", ",", ", ", ":", "|", "+", ";", "~", "*", "=", "\u00b7", "\u2022", " . ")
# SP-23 (F2): what joins the words of a multi-word entry (1-3 such characters, a digit)
F2_JOINS = (".", "/", ":", ",", "+", "|", "--", "__", " - ", "1", "7", "./", ".:", "-1-", "+/", ",,", "|.", "._.")
SPLIT_SEPS = (("sp", " "), ("us", "_"), ("hy", "-"), ("dot", "."), ("nbsp", NBSP), ("nl", "\n"), ("zwsp_sp", "\u200b "))
LANG = {"S00": ("ru", "uk"), "S01": ("ru",), "S09": ("ja",), "S10": ("ja",), "S11": ("ja",), "S16": ("ar",),
        "S17": ("ar",), "S18": ("fa",), "S19": ("hi",), "S20": ("th",), "S21": ("he",), "S22": ("el",),
        "F1": ("ru",), "F2": ("el",), "H1": ("ar",), "H2": ("ar",), "H3": ("fa",), "H4": ("el",), "H5": ("el",)}
LANG.update({f"S{i:02d}": ("zh",) for i in range(2, 9)})
LANG.update({f"S{i:02d}": ("ko",) for i in range(12, 16)})
MULTI = ("S01", "S16", "S17", "S18", "S22", "F2")            # multi-word script entries
UNSPACED = tuple(f"S{i:02d}" for i in range(2, 12)) + ("S20",)
MARKED = ("S19", "S20")
HANGUL = ("S12", "S13", "S14", "S15")
GREEK = ("S22", "F2", "H4", "H5")
ARABIC = ("S16", "S17", "S18", "H1", "H2", "H3")
FINAL = "\u03c2"
MEDIAL = "\u03c3"
# SP-36: one lookalike of another script for a Latin letter, and one that carries a mark
LOOK = {"a": "\u0430", "e": "\u0435", "o": "\u043e", "p": "\u0440", "c": "\u0441", "x": "\u0445", "y": "\u0443",
        "i": "\u0456", "s": "\u0455", "j": "\u0458"}
# the code points whose lower case is the final or the medial sigma by context: their Latin reading depends on where
# they stand, so they are no lookalike of one fixed letter inside a label
SIGMA_CPS = frozenset((0x3A3, 0x3F9, 0x1D6BA, 0x1D6F4, 0x1D72E, 0x1D768, 0x1D7A2))
LOOK_MARK = {"a": "\u03ac", "o": "\u03cc", "i": "\u03af"}


def by_id() -> dict:
    return {eid: (e, kind, sc) for eid, e, kind, sc, _g in R.entries()}


def endings(eid: str) -> tuple:
    return {"S00": R.RU_ENDINGS, "S12": R.KO_PARTICLES, "S13": R.KO_PARTICLES, "S14": R.KO_PARTICLES,
            "S15": R.KO_PARTICLES, "S19": R.HI_ENDINGS, "S22": R.EL_ENDINGS}.get(eid, ("",))


def proclitics(eid: str):
    return {"S16": R.AR_PROCLITICS, "S17": R.AR_PROCLITICS, "S21": R.HE_PROCLITICS}.get(eid)


def latin(kind: str) -> bool:
    return kind in ("joined", "piece")


def bases(eid: str) -> list:
    """The entry as listed, then its natural spellings (Latin) or its declared written spellings (S22)."""
    e, kind, _sc = by_id()[eid]
    out = [e]
    if latin(kind):
        out += list(R.NATURAL.get(e, ()))
    if eid == "S22":
        out += [w for w in R.EL_WRITTEN if w != R.EL_WRITTEN[-1]]
    return list(dict.fromkeys(out))


def multi_bases(eid: str) -> list:
    """The spellings with two words or more of the multi-word SCRIPT entries (and S12 written apart): the "multi-word
    entry" of SP-18 / 19 / 23 / 24 (a Latin entry is one word; its natural spellings are SP-01..05 bases)."""
    if eid == "S12":
        return [R.KO_APART]
    if eid not in MULTI:
        return []
    return [b for b in bases(eid) if " " in b.strip()]


def units(s: str) -> list:
    """The spelling units of ``s``: each letter with the marks written on it (spaces and punctuation dropped)."""
    out: list = []
    for c in s:
        if out and unicodedata.category(c)[0] == "M":
            out[-1] += c
        elif c.isalpha():
            out.append(c)
    return out


def alternating(s: str, start_upper: bool) -> str:
    out, up, = [], start_upper
    for c in s:
        if c.isalpha():
            out.append(c.upper() if up else c.lower())
            up = not up
        else:
            out.append(c)
    return "".join(out)


def inner(w: str, j: str, at: int = 2) -> str:
    """``j`` put inside the first word of ``w`` of 3 letters or more, after its ``at``-th character."""
    parts = w.split(" ")
    for i, p in enumerate(parts):
        if len(p) >= 3:
            parts[i] = p[:at] + j + p[at:]
            return " ".join(parts)
    return w[:1] + j + w[1:] if len(w) > 1 else w


def strip_marks(s: str) -> str:
    return unicodedata.normalize("NFC", "".join(c for c in unicodedata.normalize("NFD", s)
                                               if unicodedata.category(c)[0] != "M"))


def greek_unaccent(s: str) -> str:
    return strip_marks(s)


def halfwidth(s: str) -> str | None:
    """The half-width katakana spelling of ``s`` (NFKC gives ``s`` back), or None."""
    table = {}
    for o in range(0xFF61, 0xFFA0):
        table.setdefault(unicodedata.normalize("NFKC", chr(o)), chr(o))
    for o in range(0xFF61, 0xFFA0):
        for m in ("\uff9e", "\uff9f"):
            table.setdefault(unicodedata.normalize("NFKC", chr(o) + m), chr(o) + m)
    out = "".join(table.get(c, "") for c in s)
    return out if unicodedata.normalize("NFKC", out) == s else None


def fullwidth(s: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if c.isascii() and c.isalpha() else c for c in s)


def math_bold(s: str) -> str:
    def one(c):
        if "A" <= c <= "Z":
            return chr(0x1D400 + ord(c) - 65)
        if "a" <= c <= "z":
            return chr(0x1D41A + ord(c) - 97)
        return c
    return "".join(one(c) for c in s)


def nouns(eid: str, n: int | None = None) -> list:
    out: list = []
    for lang in LANG.get(eid, ()):
        out += WORDS.get(lang, [])
    return out if n is None else out[:n]


def script_letters(eid: str) -> list:
    """SP-27: every letter of the entry's script (lower case where the script has case)."""
    def rng(a, b):
        return [chr(o) for o in range(a, b + 1) if chr(o).isalpha()]
    if eid in HANGUL:
        return rng(0xAC00, 0xD7A3)
    if eid in ("S00", "S01", "F1"):
        return [c for c in rng(0x430, 0x45F) + ["\u0491", "\u045e"] if c.lower() == c]
    if eid in GREEK:
        return [c for c in rng(0x3AC, 0x3CE) if c != FINAL]
    if eid in ARABIC:
        return rng(0x621, 0x63A) + rng(0x641, 0x64A) + ["\u067e", "\u0686", "\u0698", "\u06a9", "\u06af", "\u06cc"]
    if eid == "S21":
        return rng(0x5D0, 0x5EA)
    if eid in ("S09", "S11"):
        return rng(0x30A1, 0x30FA)
    if eid in UNSPACED and eid != "S20":
        return rng(0x4E00, 0x9FFF)
    if eid == "S20":
        return rng(0xE01, 0xE2E)
    return []


def suffixes(eid: str, seed: int) -> list:
    """SP-28: 30+ letter strings written after the label in its own word (1-3 letters of its script), and natural
    ones (Russian adjective forms, Persian possessive / plural, Hebrew plural, Arabic "-iya", Korean nouns)."""
    natural = {
        "ru": ("\u043d\u044b\u0439", "\u043d\u0430\u044f", "\u043d\u043e\u0435", "\u043d\u044b\u0435",
               "\u043d\u043e\u0433\u043e", "\u043d\u044b\u043c", "\u043d\u044b\u0445", "\u044c\u043d\u044b\u0439"),
        "fa": ("\u0647\u0627", "\u0647\u0627\u06cc", "\u0645", "\u062a", "\u0634", "\u0645\u0627\u0646"),
        "ar": ("\u064a\u0629", "\u0627\u062a", "\u064a"),
        "he": ("\u05d5\u05ea", "\u05d9\u05dd"),
        "ko": ("\uad00\ub9ac", "\ubcc0\uacbd", "\uc124\uc815", "\uc785\ub825", "\ud655\uc778"),
        "el": ("\u03b9\u03ba\u03bf\u03c2", "\u03bf\u03c5", "\u03bf\u03b9"),
    }
    lets = [c for c in script_letters(eid) if eid not in HANGUL or c < "\uad00"] or script_letters(eid)
    r = random.Random(f"{seed}:{eid}:suffix")
    out = []
    for lang in LANG.get(eid, ()):
        out += list(natural.get(lang, ()))
    while len(set(out)) < 34 and lets:
        out.append("".join(r.choice(lets) for _ in range(r.randint(1, 3))))
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------------------------------------ the classes


def _sp_case(eid, kind, sp):
    out = []
    for i, b in enumerate(bases(eid)):
        if sp == 1 and i:
            continue
        if sp == 1:
            out.append(("listed", b))
        elif sp == 2:
            out.append((f"b{i}", b.lower()))
        elif sp == 3:
            out.append((f"b{i}", b.upper()))
        elif sp == 4:
            out.append((f"b{i}", b.title()))
        elif sp == 5:
            out.append((f"b{i}.cap", b.capitalize()))
            if kind != "piece":
                out += [(f"b{i}.alt0", alternating(b, False)), (f"b{i}.alt1", alternating(b, True))]
    return out


def _sp_compat(eid, kind):
    out = []
    if latin(kind):
        for i, b in enumerate(bases(eid)):
            out += [(f"b{i}.fw", fullwidth(b)), (f"b{i}.fwu", fullwidth(b.upper())), (f"b{i}.math", math_bold(b))]
    elif eid in ("S09", "S11"):
        h = halfwidth(by_id()[eid][0])
        if h:
            out.append(("halfwidth", h))
    return out


def _sp_marks_absent(eid, kind):
    e = by_id()[eid][0]
    if latin(kind) and e in R.LATIN_MARKED:
        return [("listed", e), ("upper", e.upper()), ("title", e.title())]
    if eid in GREEK:
        forms = [e] + ([R.CV_SCRIPT_FULL[1]] if eid == "F2" else []) + (list(R.EL_WRITTEN) if eid == "S22" else [])
        out = []
        for i, f in enumerate(dict.fromkeys(forms)):
            u = greek_unaccent(f)
            out += [(f"w{i}", u), (f"w{i}.upper", u.upper())]
        return out
    if eid in ("S16", "S17", "S18"):
        return [("listed", e)]
    return []


def _sp_marks_added(eid, kind):
    e = by_id()[eid][0]
    if eid in ("S16", "S17", "S18", "H1", "H2", "H3"):
        fat, kas = "\u064e", "\u0650"
        return [("fatha1", e[0] + fat + e[1:]), ("kasra2", e[:2] + kas + e[2:]),
                ("every", "".join(c + fat if c.isalpha() else c for c in e))]
    if eid == "S21":
        return [("hiriq1", e[0] + "\u05b4" + e[1:]), ("qamats4", e[:4] + "\u05b8" + e[4:])]
    if latin(kind):
        vow = {"a": "\u00e1", "e": "\u00e9", "i": "\u00ed", "o": "\u00f3", "u": "\u00fa"}
        for k, c in enumerate(e):
            if c in vow:
                return [("acute", e[:k] + vow[c] + e[k + 1:]), ("acute.upper", (e[:k] + vow[c] + e[k + 1:]).upper())]
    return []


def _sp_partial_accents():
    """SP-14 (F1) on S22: the written forms with only the first / only the second word accented, the enclitic
    second accent, glued with the final or medial sigma at the join, and in UPPER."""
    out = []
    forms = [R.CV_SCRIPT_FULL[1]] + [w for w in R.EL_WRITTEN[:-1] if w != R.CV_SCRIPT_FULL[1]]
    for i, w in enumerate(forms):
        a, b = w.split(" ")
        ua, ub = greek_unaccent(a), greek_unaccent(b)
        vowels = "\u03b1\u03b5\u03b7\u03b9\u03bf\u03c5\u03c9"
        acute = {"\u03b1": "\u03ac", "\u03b5": "\u03ad", "\u03b7": "\u03ae", "\u03b9": "\u03af", "\u03bf": "\u03cc",
                 "\u03c5": "\u03cd", "\u03c9": "\u03ce"}
        k = max(j for j, c in enumerate(b) if c in vowels)
        encl = b[:k] + acute[b[k]] + b[k + 1:]
        for tag, (x, y) in (("first", (a, ub)), ("second", (ua, b)), ("both", (a, b)), ("enclitic", (a, encl))):
            out.append((f"w{i}.{tag}.spaced", f"{x} {y}"))
            for sg_tag, sg in (("fin", x), ("med", x[:-1] + MEDIAL if x.endswith(FINAL) else x)):
                out.append((f"w{i}.{tag}.glued.{sg_tag}", sg + y))
            out.append((f"w{i}.{tag}.upper", f"{x} {y}".upper()))
    return out


def _sp_tatweel(eid, kind, sp):
    out = []
    if sp == 15 and eid in ARABIC:
        for i, b in enumerate(bases(eid)):
            out.append((f"b{i}.in", inner(b, TATWEEL)))
            if " " in b:
                out.append((f"b{i}.join", b.replace(" ", TATWEEL)))
            else:
                out.append((f"b{i}.mid", b[:len(b) // 2] + TATWEEL + b[len(b) // 2:]))
    if sp == 16 and latin(kind):
        for i, b in enumerate(bases(eid)):
            out.append((f"b{i}.in", inner(b, TATWEEL, 1)))
            if " " in b:
                out.append((f"b{i}.between", b.replace(" ", TATWEEL)))
    return out


def _sp_joiners(eid):
    out = []
    for i, b in enumerate(bases(eid)):
        for jn, j in JOINERS:
            if len(b.replace(" ", "")) >= 2:
                out.append((f"b{i}.{jn}.in", inner(b, j, 1)))
            if " " in b:
                out.append((f"b{i}.{jn}.join", b.replace(" ", j)))
                out.append((f"b{i}.{jn}.atspace", b.replace(" ", j + " ", 1)))
    return out


def _sp_apart(eid):
    out = []
    for i, b in enumerate(multi_bases(eid)):
        for sn, s in (("sp", " "), ("us", "_"), ("hy", "-"), ("nbsp", NBSP)):
            out.append((f"m{i}.{sn}", b.replace(" ", s)))
    return out


def _sp_glued(eid):
    e = by_id()[eid][0]
    if eid.startswith("H"):
        out = [("listed", e), ("upper", e.upper()), ("title", e.title()), ("in.tatweel", inner(e, TATWEEL))]
        return out + [(f"in.{jn}", inner(e, j)) for jn, j in JOINERS]
    if eid not in ("S01", "S16", "S17", "S18", "S22", "F2"):
        return []
    out = []
    for i, b in enumerate(multi_bases(eid)):
        g = b.replace(" ", "")
        out += [(f"m{i}.glued", g), (f"m{i}.upper", g.upper()), (f"m{i}.title", b.title().replace(" ", ""))]
        a = b.split(" ")
        if a[0].endswith(FINAL):
            out.append((f"m{i}.medial", a[0][:-1] + MEDIAL + "".join(a[1:])))
        out.append((f"m{i}.tatweel", b.replace(" ", TATWEEL)))
        out += [(f"m{i}.{jn}", b.replace(" ", j)) for jn, j in JOINERS]
        sm = strip_marks(g)
        if sm != unicodedata.normalize("NFC", g):
            out += [(f"m{i}.nomarks", sm), (f"m{i}.nomarks.upper", sm.upper())]
    return out


def _sp_spelled(eid, kind, seps):
    out = []
    for i, b in enumerate(bases(eid)[:2]):
        us = units(b)
        if len(us) < 2:
            continue
        for sn, s in seps:
            out.append((f"b{i}.{sn}", s.join(us)))
    return out


def _f3_seps():
    out = []
    for k, s in enumerate(F3_SEPS):
        out.append((f"f3.{k}", s))
        if not s.startswith(" "):
            out += [(f"f3.{k}.b", " " + s), (f"f3.{k}.a", s + " "), (f"f3.{k}.ba", " " + s + " ")]
    return out


def _sp_f2(eid):
    out = []
    for i, b in enumerate(multi_bases(eid)):
        for k, j in enumerate(F2_JOINS):
            out.append((f"m{i}.j{k}", b.replace(" ", j)))
    return out


def _sp_underscored_words(eid):
    if eid not in ("S01", "S16", "S17", "S18", "S22"):
        return []
    out = []
    for i, b in enumerate(multi_bases(eid)):
        words = b.split(" ")
        out.append((f"m{i}", " ".join("_".join(units(w)) for w in words)))
        out.append((f"m{i}.upper", " ".join("_".join(units(w)) for w in words).upper()))
    return out


def _split_forms(eid, kind):
    """SP-25: the label forms split once (every declared ending / written form, lower and UPPER)."""
    e = by_id()[eid][0]
    if latin(kind):
        forms = bases(eid)
    elif eid == "S12":
        forms = [e + x for x in endings(eid)] + [R.KO_APART + x for x in endings(eid)]
    elif eid == "S22":
        forms = [e + x for x in endings(eid)] + list(R.EL_WRITTEN)
    else:
        forms = [e + x for x in endings(eid)]
    out = []
    for fi, f in enumerate(dict.fromkeys(forms)):
        words = f.split(" ")
        for wi, w in enumerate(words):
            us = units(w)
            for k in range(1, len(us)):
                sig = ".sig" if us[k - 1] in (FINAL, MEDIAL) else ""
                for sn, s in SPLIT_SEPS:
                    nw = "".join(us[:k]) + s + "".join(us[k:])
                    t = " ".join(words[:wi] + [nw] + words[wi + 1:])
                    out.append((f"f{fi}.w{wi}.k{k}.{sn}{sig}", t))
                    if t.upper() != t:
                        out.append((f"f{fi}.w{wi}.k{k}.{sn}{sig}.upper", t.upper()))
    return out


def _inword(eid, sp, seed):
    """SP-26 (noun before), SP-27 (letter before), SP-28 (letters after), SP-29 (the five combinations)."""
    labels = bases(eid)[:1]                     # the label as listed
    out = []
    if sp == 26:
        for i, lab in enumerate(labels):
            out += [(f"b{i}.n{k}", n + lab) for k, n in enumerate(nouns(eid))]
    elif sp == 27:
        for i, lab in enumerate(labels[:1]):
            out += [(f"b{i}.l{ord(c):x}", c + lab) for c in script_letters(eid)]
    elif sp == 28:
        sfx = suffixes(eid, seed)
        ns = nouns(eid, 4)
        for i, lab in enumerate(labels):
            out += [(f"b{i}.s{k}", lab + s) for k, s in enumerate(sfx)]
            out += [(f"b{i}.s{k}.n{m}", lab + s + "-" + n) for k, s in enumerate(sfx[:8]) for m, n in enumerate(ns)]
    elif sp == 29:
        r = random.Random(f"{seed}:{eid}:sp29")
        ns = nouns(eid, 30)
        lets = script_letters(eid)
        lets = r.sample(lets, min(40, len(lets)))
        sfx = suffixes(eid, seed)[:30]
        for i, b in enumerate(multi_bases(eid) if eid in MULTI or eid == "S12" else []):
            for k, j in enumerate(F2_JOINS):
                f2 = b.replace(" ", j)
                out += [(f"a.m{i}.j{k}.n{m}", n + f2) for m, n in enumerate(ns)]
                out += [(f"b.m{i}.j{k}.l{ord(c):x}", c + f2) for c in lets]
                out += [(f"c.m{i}.j{k}.s{m}", f2 + s) for m, s in enumerate(sfx)]
        lab = by_id()[eid][0]
        us = units(lab)
        # (a two-unit label spelled is a word of its own: P-6e / P-6f, so no letters before or after it)
        for sn, s in (_f3_seps() if len(us) >= 3 else ()):
            sp_ = s.join(us)
            out += [(f"d.{sn}.n{m}", n + sp_) for m, n in enumerate(ns)]
            out += [(f"e.{sn}.s{m}", sp_ + x) for m, x in enumerate(sfx)]
    return out


def _sp_unspaced_inword(eid):
    lab = by_id()[eid][0]
    ns = nouns(eid, 30)
    out = [(f"n{k}.before", n + lab) for k, n in enumerate(ns)]
    out += [(f"n{k}.after", lab + n) for k, n in enumerate(ns)]
    out += [(f"n{k}.both", n + lab + ns[(k + 1) % len(ns)]) for k, n in enumerate(ns)]
    return out


def _sp_declared(eid):
    e = by_id()[eid][0]
    out = []
    if eid in ("S00", "S12", "S13", "S14", "S15", "S19", "S22"):
        out += [(f"end{k}", e + x) for k, x in enumerate(endings(eid))]
    if eid == "S12":
        out += [(f"apart.end{k}", R.KO_APART + x) for k, x in enumerate(endings(eid))]
    if eid == "S22":
        out += [(f"written{k}", w) for k, w in enumerate(R.EL_WRITTEN)]
    p = proclitics(eid)
    if p:
        letters, n = p
        combos = [""]
        for _ in range(n):
            combos = combos + [c + x for c in combos for x in letters if len(c) < n]
        for c in dict.fromkeys(combos):
            if c:
                out.append((f"pro.{'.'.join(f'{ord(x):x}' for x in c)}", c + e))
    return out


def _sp_h4(sp):
    h4 = R.HISTORY[3][1]
    a, b = h4[:7], h4[7:]                     # "code" (medial sigma) + the stem of "access"
    fa = a[:-1] + FINAL
    if sp == 34:
        forms = [("glued", h4), ("sp.fin", f"{fa} {b}"), ("sp.med", f"{a} {b}"), ("us.fin", f"{fa}_{b}"),
                 ("us.med", f"{a}_{b}")]
        return forms + [(t + ".upper", x.upper()) for t, x in forms]
    return [("upper.dot", f"{a}.{b}".upper()), ("title.dot", f"{a}.{b}".title())]


def lookalike_forms(eid, sp, table) -> list:
    """SP-36 (c) / SP-37: each frozen code point of ``table`` ({letter: [code points]}) in place of that letter in
    the listed label, in the code point's own case."""
    e = by_id()[eid][0]
    out = []
    for k, cps in sorted(table.items()):
        if k not in e:
            continue
        for o in cps:
            if o in SIGMA_CPS:
                continue                          # read as a letter only at a word's end (the final sigma)
            c = chr(o)
            up = c.lower() != c
            lab = (e.upper() if up else e).replace(k.upper() if up else k, c, 1)
            out.append((f"cp{o:x}", lab))
    return out


def _sp_lookalike(eid):
    e = by_id()[eid][0]
    out = []
    for k, c in LOOK.items():
        if k in e:
            out.append((f"one.{k}", e.replace(k, c, 1)))
    for k, c in LOOK_MARK.items():
        if k in e:
            out.append((f"mark.{k}", e.replace(k, c, 1)))
    return out


def _sp_cased_compounds(eid):
    out = []
    for i, b in enumerate(bases(eid)[:2]):
        words = b.replace("-", " ").replace("_", " ").replace("'", "").replace("\u2019", "").split(" ")
        camel = "".join(w.capitalize() for w in words)
        out += [(f"b{i}.camel", "user" + camel), (f"b{i}.camel2", "my" + camel),
                (f"b{i}.snake", "db_" + "_".join(words)), (f"b{i}.scream", "DB_" + "_".join(words).upper()),
                (f"b{i}.app", "APP_" + "".join(words).upper()), (f"b{i}.kebab", "x-" + "-".join(words))]
    return out


def _sp_markup(eid):
    lab = by_id()[eid][0]
    return [("json", '{"' + lab + '": "{v}"}'), ("json1", "{'" + lab + "': '{v}'}"),
            ("xml", "<" + lab.replace(" ", "_") + ">{v}</" + lab.replace(" ", "_") + ">"),
            ("table", lab + " | {v}"), ("table2", "| " + lab + " | {v} |"), ("wrapped", '"' + lab + '\n": "{v}"')]


def _sp_letter_spaced(eid):
    out = []
    for i, b in enumerate(bases(eid)[:2]):
        words = b.replace("-", " ").split(" ")
        out.append((f"b{i}.spaced", " _ ".join(" ".join(w) for w in words)))
        out.append((f"b{i}.spaced.upper", " _ ".join(" ".join(w) for w in words).upper()))
        if len(words) > 1:
            for k, s in enumerate(("__", " _", "_ ", "-.", "  ", "_-")):
                out.append((f"b{i}.bound{k}", s.join(words)))
    return out
