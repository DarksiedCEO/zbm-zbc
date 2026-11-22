"""core-spec/2 CV-10a value matrix VM (+ CV-10b F-1 cells): the review corpora rv-sink8 E1 / E1b / E1c and rv-sink9
p9, each cell split into (prefix, label, separator, value, suffix), its value replaced by a VG value (CV-9) and its
label by four lead variants: the original label; nothing; one ordinary word; a random capital Greek string split once
by a space or NBSP. Measured: characters of the value's CV-2 core outside every CV mask span (shown, must be 0) and
the masked span of the value, relative to the value start, across the four variants (must be identical).

Corpora are read from the review work folders (never copied into the repo; env RV_SINK8_WORK / RV_SINK9_WORK).
Never prints a label text or a value: only corpus ids, cell indexes, meta classes and counts.

Heavy lane (lead):  python tests/conformance/vm_cells.py --full [--corpus e1] [--shard i/n]  -> JSON counts."""
from __future__ import annotations

import json
import os
import random
import re
import sys
import unicodedata as u

_SCR = "/tmp/claude-0/-home-claude/532895db-b797-5fa4-bc06-893a3fd5370e/scratchpad"
R8 = os.environ.get("RV_SINK8_WORK", _SCR + "/rv-sink8/work")
R9 = os.environ.get("RV_SINK9_WORK", _SCR + "/rv-sink9/work")
SIZES = {"e1": 578_854, "e1b": 48_860, "e1c": 24_100, "p9": 23_603}
NBSP = "\u00a0"
GREEK_UPPER = "".join(chr(c) for c in range(0x391, 0x3AA) if c != 0x3A2)

# rv-sink8 h/gen_e1.py SEPS (identical list in gen_e1b / gen_e1c); written with escapes (RUF001).
E_SEPS = [": ", ":", "=", " = ", " : ", "\uff1a", "\uff1a ", " is ", " Is ", " IS ", " was ", " Was ", " WAS ",
          " are ", " Are ", " ARE ", " - ", " -> ", " => ", "|", " ", "\t", "\n", '": "']
_E_VAL = re.compile(r"(?:maple|river|cobalt|tiger|window|garden|silver|orange|planet|harbor)[0-9]{2}")
_P_VAL = re.compile(r"[A-HJKMNP-Za-hjkmnp-z2-9#%*+!?@$^~]{8,12}")
P_PRE = {"bare": ("", ""), "prose": ("Please note the ", ""), "sentence": ("Please note the ", " for today, thanks.")}


def available() -> bool:
    gen = os.path.join(R9, "h", "gen_p9.py")
    return all(os.path.exists(p + ".jsonl") for p in paths().values()) and os.path.exists(gen)


def paths() -> dict:
    c8 = os.path.join(R8, "c")
    return {"e1": os.path.join(c8, "e1"), "e1b": os.path.join(c8, "e1b"), "e1c": os.path.join(c8, "e1c"),
            "p9": os.path.join(R9, "c", "p9")}


def _p9_pools() -> dict:
    """SEP pools + CLOSE / OPEN of rv-sink9 h/gen_p9.py (its header only, up to PLACE; no cell is generated)."""
    src = open(os.path.join(R9, "h", "gen_p9.py"), encoding="utf-8").read()
    head = "\n".join(ln for ln in src[: src.index("\nPLACE = ")].splitlines()
                     if "secret_guard" not in ln and "sys.path" not in ln)
    ns: dict = {"__file__": os.path.join(R9, "h", "gen_p9.py"), "__name__": "gen_p9_header"}
    exec(compile(head, "gen_p9_header", "exec"), ns)  # noqa: S102 - review generator header (own scratchpad)
    return {"1": ns["SEP1"], "2": ns["SEP2"], "3a": ns["SEP3"], "4": ns["SEP4"], "CLOSE": ns["CLOSE"],
            "OPEN": ns["OPEN"]}


def _split_e(text: str, meta: list):
    """(pre, label, sep, value, suf, nf) of an E1/E1b/E1c cell, or None when the cell does not decompose."""
    sep = E_SEPS[int(meta[2])]
    ctx = meta[3] if len(meta) > 3 else "0"
    pre, suf = ("", "")
    if ctx == "1":
        pre, suf = ('{"', '"}') if sep == '": "' else ("Note ", " ok")
    ms = list(_E_VAL.finditer(text))
    if not ms or not text.startswith(pre) or not text.endswith(suf):
        return None
    m = ms[-1]
    if m.end() != len(text) - len(suf) or text[m.start() - len(sep): m.start()] != sep:
        return None
    label = text[len(pre): m.start() - len(sep)]
    return (pre, label, sep, m.group(0), suf, "NFC") if label else None


def _split_p(text: str, meta: list, pools: dict):
    """(pre, label, sep, value, suf, nf) of a p9 cell; None for cells without a valueish value (bare / 3b / own)."""
    sc, k, pl, nf = meta[2], meta[3], meta[4], meta[5]
    if sc not in ("1", "2", "3a", "4") or pl not in P_PRE:
        return None
    pre, suf = P_PRE[pl]
    if not text.startswith(pre) or not text.endswith(suf):
        return None
    core = text[len(pre): len(text) - len(suf)]
    base = pools[sc][int(k)]
    i = core.rfind(base)
    if i < 0:
        return None
    j = i + len(base)
    opn = max((o for o in pools["OPEN"] if core.startswith(o, j)), key=len, default="")
    val = core[j + len(opn):]
    if not _P_VAL.fullmatch(val):
        return None
    head = core[:i]
    cls = max((c for c in pools["CLOSE"] if c and head.endswith(c)), key=len, default="")
    label = head[: len(head) - len(cls)]
    return (pre, label, cls + base + opn, val, suf, nf) if label else None


def cells(corpus: str):
    """Yield (index, meta, parts or None) for every cell of a corpus, in file order."""
    base = paths()[corpus]
    pools = _p9_pools() if corpus == "p9" else None
    with open(base + ".jsonl", encoding="utf-8") as ft, open(base + ".meta", encoding="utf-8") as fm:
        for i, (lt, lm) in enumerate(zip(ft, fm)):
            text, meta = json.loads(lt), lm.rstrip("\n").split("\t")
            yield i, meta, (_split_p(text, meta, pools) if pools else _split_e(text, meta))


# E-corpus separator id -> SEP class (as gen_p9 pools: SEP-1 symbols, SEP-2 is / dash, SEP-3a verbs, SEP-4 space).
E_SEP_CLASS = ["1", "1", "1", "1", "1", "1", "1", "2", "2", "2", "3a", "3a", "3a", "3a", "3a", "3a", "2", "1", "1",
               "1", "4", "4", "4", "1"]


def group_key(corpus: str, meta: list) -> tuple:
    """(entry, SP / spelling class, SEP class) of a cell (G-2 stratum)."""
    if corpus == "p9":
        return (corpus, meta[0], meta[1], meta[2])
    return (corpus, meta[0], meta[1], E_SEP_CLASS[int(meta[2])])


def greek_split(r: random.Random, sp: str) -> str:
    w = "".join(r.choice(GREEK_UPPER) for _ in range(r.randint(6, 14)))
    k = r.randint(1, len(w) - 1)
    return w[:k] + sp + w[k:]


def leads(r: random.Random, label: str, words: list) -> list:
    """The four CV-10a lead variants (label words by index only)."""
    return [label, "", words[r.randrange(len(words))], greek_split(r, r.choice((" ", NBSP)))]


# Whitespace that separates a label from its value. U+200B is NOT in it: it is Cf, dropped from the canonical view
# (CV-1), so a label holding it stays one CV-2 token with the value = glued (ruling 16:45 / 21:35 F12-1; C2-12e).
_GAP = frozenset(" \t\n\r\x0b\x0c\u2028\u2029") | {NBSP}


def _cover(k: int, spans) -> bool:
    return any(x <= k < y for x, y in spans)


def measure_ex(parts: tuple, val: str, labels: list, value_spans, core_bounds) -> dict:
    """CV-10a identity over the four lead variants. Rulings: 16:45 (glued P s V token masked whole), 21:35 F12-1
    ((a) value core masked, (b) mask end identical, (c) masked value code points identical), 23:58 (class A: the mask
    OUTSIDE the value - separator and suffix, offsets from the value - must be identical across the four variants,
    except a glued label), 00:10 (M1 / M2 / M3: identity on the value CORE; label characters, the separator points
    covered by a label-joined run and the single wrapper character next to the core (E) may differ; counted).
    Mechanism of a variant, computed from the variant itself: a span covering the value starts in the label across
    whitespace; its label points masked with the value REMOVED (label + sep + suffix) by a span without whitespace =
    M1 (CV-3 token), by a span holding whitespace = M2 (CV-4 run); label points not masked without the value = M3.
    Ruling 10:40 PT (F13c-2, "Jrun"): separator points covered by a span that STARTS in the separator, holds
    whitespace (a joined CV-4 run) and reaches into the value may differ across the four variants (extra masking from
    a joined run); counted as mechanism "Jrun" only when those sets differ. The value core must still be identical."""
    pre, _label, sep, _v, suf, nf = parts
    a0, b0 = core_bounds(val)
    pre_n, sep_n, suf_n = u.normalize(nf, pre), u.normalize(nf, sep), u.normalize(nf, suf)
    shown, glued, unclassified, mech, sep_masked, jrun = 0, False, False, set(), False, False
    rows = []
    for lab in labels:
        head = u.normalize(nf, pre + lab + sep)
        t = head + val + suf_n
        vs, ve = len(head), len(head) + len(val)
        ls, lb = vs - len(sep_n), len(pre_n)          # separator start, label start (normalized text)
        allsp = value_spans(t)
        sp = [(x, y) for x, y in allsp if y > vs and x < ve]
        shown += sum(1 for k in range(vs + a0, vs + b0) if not _cover(k, sp))
        end = max((y for _x, y in sp), default=vs) - vs
        masked = frozenset(k - vs for k in range(vs, ve) if _cover(k, sp))
        sepm = frozenset(k - vs for k in range(ls, vs) if _cover(k, allsp))
        sufm = frozenset(k - ve for k in range(ve, len(t)) if _cover(k, allsp))
        sep_masked |= any(_cover(k, [s for s in sp if s[0] >= ls]) for k in range(ls, vs))
        joined, gl, vm, jr = set(), set(), set(), set()
        # spans that start in the label and cover the separator (with or without the value); a label span that stops
        # inside the separator = label token / run whose last piece holds separator chars glued to it (counted Jsep)
        for x, y in allsp:
            if ls <= x < vs < y and any(c in _GAP for c in t[x:vs]):
                jr |= {k - vs for k in range(x, vs)}     # 10:40 Jrun: joined run from the separator into the value
            if x >= ls or y <= ls:
                continue
            if y > vs and not any(c in _GAP for c in t[x:vs]):
                glued = True                          # glued P s V token (16:45): its separator points are exempt
                gl |= {k - vs for k in range(max(x, ls), vs)}
                continue
            if x < lb:
                unclassified = True                   # reaches into the fixed prefix: not a label mechanism
                continue
            if y <= vs:
                vm.add("Jsep")
            else:
                jrun = True                           # label-joined run reaching into the value (00:10 E edge)
            joined |= {k - vs for k in range(ls, min(y, vs))}
            t0 = head + suf_n
            s0 = value_spans(t0)
            for k in range(x, ls):
                if t[k] in _GAP:
                    continue
                cov = [s for s in s0 if s[0] <= k < s[1]]
                if not cov:
                    vm.add("M3")
                else:
                    vm.add("M2" if any(c in _GAP for s in cov for c in t0[s[0]:s[1]]) else "M1")
        mech |= vm
        rows.append((end, masked, sepm, sufm, frozenset(joined), frozenset(gl), frozenset(jr)))
    gx = frozenset().union(*(r[5] for r in rows))   # separator points of a glued token in some variant (23:58)
    jrx = frozenset()
    if len({r[6] for r in rows}) != 1:              # 10:40 Jrun: only when the joined-run separator mask differs
        mech.add("Jrun")
        jrx = frozenset().union(*(r[6] for r in rows))
    if not mech:
        diff = bool(shown) or unclassified or len({(r[0], r[1], r[2] - gx, r[3]) for r in rows}) != 1
        return {"shown": shown, "diff": diff, "glued": glued, "mech": (), "wrapper": False, "sep_masked": sep_masked}
    core = frozenset(range(a0, b0))
    # E (00:10 "single wrapper character at the edge of a label-joined run"): M2 / M3, or any label span that holds
    # whitespace and reaches into the value - also when its label token is M1 on its own shape (C2-12h, p9 23166)
    wrap = frozenset(k for k in (a0 - 1, b0) if 0 <= k < len(val)) if mech & {"M2", "M3"} or jrun else frozenset()
    jx = frozenset().union(*(r[4] for r in rows)) | gx | jrx
    same_core = len({r[1] & core for r in rows}) == 1
    same_rest = len({(r[1] - core - wrap, r[2] - jx, r[3]) for r in rows}) == 1
    wrapper = len({r[1] & wrap for r in rows}) != 1
    diff = bool(shown) or unclassified or not same_core or not same_rest
    return {"shown": shown, "diff": diff, "glued": glued, "mech": tuple(sorted(mech)), "wrapper": wrapper,
            "sep_masked": sep_masked}


def measure(parts: tuple, val: str, labels: list, value_spans, core_bounds) -> tuple:
    """(shown, diff, glued) of measure_ex (kept for the CV-10b single-variant callers)."""
    m = measure_ex(parts, val, labels, value_spans, core_bounds)
    return m["shown"], m["diff"], m["glued"]


def run(corpus: str, select=None, shard=(0, 1), seed_vals=None) -> dict:
    """Evaluate one corpus (optionally only cells where select(i, meta) is true, and shard i/n by index)."""
    from agents import cv_shape as CV
    from conformance import gen_values_v2 as VG
    vals = seed_vals or {s: VG.values(s, 10_000) for s in VG.SEEDS}
    words = VG.prose_pool()
    out = {"corpus": corpus, "cells": 0, "evaluated": 0, "no_value": 0, "shown": 0, "span_diff": 0,
           "glued_label_cells": 0, "sep_masked_cells": 0, "mech_cells": {}, "mech_diff": {}, "shown_ids": [], "span_diff_ids": [], "span_diff_by_sep": {}}
    for i, meta, parts in cells(corpus):
        if i % shard[1] != shard[0]:
            continue
        out["cells"] += 1
        if select is not None and not select(i, meta):
            continue
        if parts is None:
            out["no_value"] += 1
            continue
        r = random.Random(f"{corpus}:{i}")
        seed = VG.SEEDS[i % 3]
        val = vals[seed][(i // 3) % len(vals[seed])]
        m = measure_ex(parts, val, leads(r, parts[1], words), CV.value_spans, CV.core_bounds)
        shown, diff = m["shown"], m["diff"]
        out["evaluated"] += 1
        out["glued_label_cells"] += m["glued"]
        out["sep_masked_cells"] += m["sep_masked"]           # ruling 23:58 class A (allowed when identical)
        for k in m["mech"] + (("E",) if m["wrapper"] else ()):  # ruling 00:10, per mechanism (cell counts)
            out["mech_cells"][k] = out["mech_cells"].get(k, 0) + 1
            if diff:
                out["mech_diff"][k] = out["mech_diff"].get(k, 0) + 1
        if shown:
            out["shown"] += 1
            if len(out["shown_ids"]) < 50:
                out["shown_ids"].append([i] + meta[:4])
        if diff:
            out["span_diff"] += 1
            sid = repr(parts[2].strip() or "ws")
            out["span_diff_by_sep"][sid] = out["span_diff_by_sep"].get(sid, 0) + 1
            if len(out["span_diff_ids"]) < 50:
                out["span_diff_ids"].append([i] + meta[:4])
    return out


# CV-10b F-1 cells by construction (lead ruling 21:50 PT, option (a)); labels never printed.
SIGMA = "\u03a3"
F1_SEPS = (": ", " = ", " is ")
E_SPLITS = {"sp": " ", "nbsp": "\u00a0", "nl": "\n", "zwsp+sp": "\u200b "}   # rv-sink8 h/gen_e1b.py SPLITS (subset)


def _frozen_guard():
    """secret_guard of rv-sink8 vers/186471eb: the frozen entry lists gen_p9.py / gen_e1b.py read (loaded by path)."""
    import importlib.util as iu
    p = os.path.join(R8, "vers", "186471eb", "services", "seo-py", "src", "agents", "secret_guard.py")
    spec = iu.spec_from_file_location("rv8_186471eb_secret_guard", p)
    m = iu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def s22_spellings() -> list:
    """The S22 entry's frozen spellings, built as gen_e1b.py builds them (endings on the last word + written forms)."""
    g = _frozen_guard()
    e = g._SCRIPT[22]
    f = g._SCRIPT_FORMS.get(e, {})
    ws = e.split(" ")
    forms = {tuple(ws[:-1] + [ws[-1] + x]) for x in (f.get("endings") or ("",))}
    forms |= {tuple(x.split(" ")) for x in f.get("written", ())}
    return sorted(forms)


def sigma_split(form: tuple, nth: int, sp: str):
    """The form in UPPER, its 'access' word (the last word) split once by sp right after its nth capital sigma;
    None when that sigma is missing or ends the word (no inner position)."""
    up = [w.upper() for w in form]
    w = up[-1]
    sig = [k for k, c in enumerate(w) if c == SIGMA]
    if len(sig) < nth or sig[nth - 1] + 1 >= len(w):
        return None
    k = sig[nth - 1] + 1
    return " ".join([*up[:-1], w[:k] + sp + w[k:]])


def f1_rv9_cells() -> tuple:
    """(spellings with an inner 2nd sigma, parts of the rv-sink9 F-1 cells): spelling x {space, NBSP} x F1_SEPS."""
    labs = [(sigma_split(f, 2, " "), sigma_split(f, 2, E_SPLITS["nbsp"])) for f in s22_spellings()]
    labs = [x for x in labs if x[0] is not None]
    return len(labs), [("", lab, s, "", "", "NFC") for pair in labs for lab in pair for s in F1_SEPS]


def h45_cells() -> tuple:
    """CV-10b H4 / H5 (lead ruling 22:05 PT F12c-2; rv-sink9 NOTES.md line 7 "S22 UPPER split after 'PROS'
    (pos 11)"): the first-sigma split of the access word of the same 10 spellings as f1_rv9_cells, H4 = space,
    H5 = NBSP, x F1_SEPS. Returns (spellings, {"H4": parts, "H5": parts}, sorted set of the split sigma's index)."""
    forms = [f for f in s22_spellings() if sigma_split(f, 2, " ") is not None]
    pos = set()
    for f in forms:
        lab = " ".join(w.upper() for w in f)
        pos.add(len(lab) - len(f[-1]) + f[-1].upper().index(SIGMA))
    out = {h: [("", sigma_split(f, 1, sp), s, "", "", "NFC") for f in forms for s in F1_SEPS]
           for h, sp in (("H4", " "), ("H5", NBSP))}
    return len(forms), out, sorted(pos)


def e1b_s22_sigma(nth: int, splits: tuple) -> list:
    """(index, meta, parts) of e1b cells: entry 22, UPPER, split by one of ``splits`` right after the nth sigma of
    the access word, SEP id 0 / 2 / 7 (": ", "=", " is ")."""
    want = {(sn, sigma_split(f, nth, E_SPLITS[sn])) for sn in splits for f in s22_spellings()}
    out = []
    for i, meta, parts in cells("e1b"):
        if parts is None or meta[0] != "22" or meta[2] not in ("0", "2", "7"):
            continue
        if meta[1].startswith("split_") and meta[1].endswith("_upper") and (meta[1][6:-6], parts[1]) in want:
            out.append((i, meta, parts))
    return out


def _main(argv: list) -> int:
    corpora = [argv[argv.index("--corpus") + 1]] if "--corpus" in argv else list(SIZES)
    sh = tuple(int(x) for x in argv[argv.index("--shard") + 1].split("/")) if "--shard" in argv else (0, 1)
    res = [run(c, shard=sh) for c in corpora]
    print(json.dumps({"shard": sh, "results": res}, indent=1))
    return 0 if all(r["shown"] == 0 and r["span_diff"] == 0 for r in res) else 1


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:0] = [os.path.dirname(here), os.path.join(os.path.dirname(os.path.dirname(here)), "src")]
    sys.exit(_main(sys.argv[1:]))
