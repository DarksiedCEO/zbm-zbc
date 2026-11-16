"""core-spec/2 BL-1..BL-3 (owner default Q2-8): the bounded label rule for PROSE values in the client view.

Scope (closed, BL-1): entries L-2 (44), L-3 (27), L-4 (23, as written) and L-5 (2) = 96 (H1-H5 excluded); spellings
SP-01..05, 18, 38, 39; separators SEP-1 and SEP-2 (SP-39 carries its own). In scope the ONE token after the separator
is masked whatever its shape: a prose value (CV-9: an ordinary lower-case 4+ letter word) is never shown.
BL-2 negatives: M-2 bare label word (no separator, no value) and SEP-3b (``the password was reset``, Title / lower verb)
are N in the client view: masked 0.

CI here = a seeded subset of every (entry, SP, SEP class) triple; the full BL-3 cell matrix (every form x every
SEP-1 / SEP-2 variant x NFC / NFD x bare / prose / sentence x 5 prose values x the 3 VG seeds) is the heavy lane
``python -m tests.test_cv_bounded_label --full`` (lead job). Synthetic values only; ids and counts only, never a text."""
import functools
import random
import sys
import unicodedata
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import callum, secret_guard  # noqa: E402
from conformance import core_matrix as M  # noqa: E402
from conformance import core_ref as R  # noqa: E402
from conformance import gen_values_v2 as G  # noqa: E402

BL_SP = (1, 2, 3, 4, 5, 18, 38, 39)
BL_SEP = ("1", "2")
N_PROSE = 5
PER_TRIPLE = 3                      # CI: cells per (entry, SP, SEP class) triple, per seed
Q2_11_EID = "J23"                   # sharedaccesssignature: K-1 strong key vs M-2 (spec section 11 / Q2-11)


def bl_entries() -> list:
    """BL-1: L-2 + L-3 + L-4 + L-5 = 96 entries (ids J*, P*, S*, F1 / F2); H1-H5 excluded."""
    return [x for x in R.entries() if x[2] in ("joined", "piece", "script", "full")]


def bl_triples():
    """(eid, sp, sep_class, forms) of every in-scope triple (M-1 applicability, as the v1 matrix)."""
    for eid, e, kind, _sc, _g in bl_entries():
        for sp in BL_SP:
            if not M.applies(eid, kind, sp, e):
                continue
            fs = M.forms(eid, sp)
            if fs:
                for sc in (("own",) if sp == 39 else BL_SEP):
                    yield eid, sp, sc, fs


def _seps(sc: str) -> list:
    return M.SEPS["1"][:1] if sc == "own" else M.SEPS[sc]


def place(body: str, placement: str, nf: str) -> str:
    if placement == "prose":
        body = "Please note the following " + body
    elif placement == "sentence":
        body = "Please note the following " + body + " and keep it safe today."
    return unicodedata.normalize(nf, body)


def bl_text(form: str, sep: str, value: str, placement: str, nf: str) -> str:
    body = form.replace("{v}", value) if "{v}" in form else form + sep + value
    return place(body, placement, nf)


def shown(text: str, value: str, nf: str) -> bool:
    """The prose value is readable in the owner's client view of ``text`` (the real ``callum._display``)."""
    v = unicodedata.normalize(nf, value)
    out = callum._display("answer", text)
    rendered = out["value"] if out else ""
    # the value occurs once as the cell's value; any other occurrence (inside the label or the filler) is the
    # owner's text: shown iff the rendering keeps more occurrences than the text minus the value
    return rendered.count(v) > text.count(v) - 1


def _n_cells(sc: str, fs: list) -> int:
    return len(fs) * len(_seps(sc)) * len(M.PLACEMENTS) * len(M.NFS) * N_PROSE


def _cell(sc, fs, i, vals):
    seps = _seps(sc)
    f, rest = divmod(i, len(seps) * len(M.PLACEMENTS) * len(M.NFS) * N_PROSE)
    s, rest = divmod(rest, len(M.PLACEMENTS) * len(M.NFS) * N_PROSE)
    p, rest = divmod(rest, len(M.NFS) * N_PROSE)
    n, j = divmod(rest, N_PROSE)
    nf = M.NFS[n]
    return bl_text(fs[f][1], seps[s][1], vals[j], M.PLACEMENTS[p], nf), vals[j], nf


def bl_cells(seed: int, full: bool = False):
    """(cell id, text, value, nf) of BL-3: every cell (``full``) or ``PER_TRIPLE`` seeded cells of every triple."""
    vals = G.prose(seed, N_PROSE)
    for eid, sp, sc, fs in bl_triples():
        n = _n_cells(sc, fs)
        if full:
            idx = range(n)
        else:
            r = random.Random(f"bl:{seed}:{eid}:{sp}:{sc}")
            idx = sorted(r.sample(range(n), min(PER_TRIPLE, n)))
        for i in idx:
            t, v, nf = _cell(sc, fs, i, vals)
            yield f"{eid}|SP{sp:02d}|{sc}|{i}", t, v, nf


def negatives(seed: int, full: bool = False):
    """BL-2: M-2 bare label word (SP rows whose bare verdict is N: SP-01..05, 18) and SEP-3b with a Title / lower verb
    + a prose word, in every placement and Unicode form (CV verdict N, ``core_matrix.verdict``)."""
    words = G.prose(seed, N_PROSE)
    verbs = [(k, s) for k, s in M.SEPS["3b"] if not s.strip().isupper()]
    for eid, e, kind, _sc, _g in bl_entries():
        for sp in (1, 2, 3, 4, 5, 18):
            if not M.applies(eid, kind, sp, e):
                continue
            for tag, form in M.forms(eid, sp):
                assert M.verdict(eid, sp, tag, "bare", "", "cv") == "N"
                h = sum(map(ord, eid + tag)) + sp
                rows = [("bare", "")] + (verbs if full else [verbs[(h + seed) % len(verbs)]])
                for j, (vid, sep) in enumerate(rows):
                    if vid != "bare":
                        assert M.verdict(eid, sp, tag, "3b", sep, "cv") == "N"
                    pn = [(p, nf) for p in M.PLACEMENTS for nf in M.NFS]
                    for p, nf in (pn if full else [pn[(h + j) % len(pn)]]):
                        body = form if vid == "bare" else form + sep + words[j % N_PROSE]
                        yield eid, f"{eid}|SP{sp:02d}|{tag}|{vid}|{p}|{nf}", place(body, p, nf)


def run(full: bool = False, seeds=G.SEEDS) -> dict:
    out = {"cells": 0, "shown": 0, "shown_ids": [], "neg": 0, "neg_masked": 0, "neg_ids": [], "q2_11": 0}
    for seed in seeds:
        for cid, t, v, nf in bl_cells(seed, full):
            out["cells"] += 1
            if shown(t, v, nf):
                out["shown"] += 1
                out["shown_ids"].append(f"{seed}|{cid}")
        for eid, cid, t in negatives(seed, full):
            out["neg"] += 1
            f = secret_guard.client_view_findings(t)
            if f and eid == Q2_11_EID and f == ["strong_key"]:
                out["q2_11"] += 1              # K-1 wins over M-2 for SharedAccessSignature (owner default Q2-11)
            elif f:
                out["neg_masked"] += 1
                out["neg_ids"].append(f"{seed}|{cid}")
    return out


def full_count() -> int:
    """The exact BL-3 cell count (96 entries x in-scope SP forms x SEP-1 / SEP-2 rows x NFC / NFD x 3 placements x
    5 prose values x 3 seeds), from the generator itself."""
    return sum(_n_cells(sc, fs) for _e, _s, sc, fs in bl_triples()) * len(G.SEEDS)


def test_scope_is_the_closed_96_entries():
    ids = [x[0] for x in bl_entries()]
    assert len(ids) == 96
    assert sum(i.startswith("J") for i in ids) == 44 and sum(i.startswith("P") for i in ids) == 27
    assert sum(i.startswith("S") for i in ids) == 23 and {i for i in ids if i.startswith("F")} == {"F1", "F2"}
    assert not any(i.startswith("H") for i in ids)


def test_every_entry_has_cells_on_both_separator_classes():
    seen: dict = {}
    for eid, _sp, sc, _fs in bl_triples():
        seen.setdefault(eid, set()).add(sc)
    assert len(seen) == 96 and all({"1", "2", "own"} <= s for s in seen.values())


def test_prose_values_are_prose():
    for seed in G.SEEDS:
        for v in G.prose(seed, N_PROSE):
            assert len(v) >= 4 and v == v.lower() and not secret_guard.client_view_findings(v)


@functools.lru_cache(maxsize=None)
def _ci(seed: int) -> dict:
    return run(seeds=(seed,))


@pytest.mark.parametrize("seed", G.SEEDS)
def test_bl3_prose_value_shown_zero(seed):
    r = _ci(seed)
    assert r["cells"] > 0
    assert r["shown"] == 0, r["shown_ids"][:20]


@pytest.mark.parametrize("seed", G.SEEDS)
def test_bl2_negatives_masked_zero(seed):
    r = _ci(seed)
    assert r["neg"] > 0
    assert r["neg_masked"] == 0, r["neg_ids"][:20]


def test_bl2_script_entries_full_negatives_masked_zero():
    """C2-16b: the FULL BL-2 rows (every SEP-3b verb x placement x NF) of the L-4 script entries, all 3 seeds. A prose
    word of a script with combining marks (Thai / Devanagari vowel signs) after `` was `` is prose, not a value (the
    CI pick of one verb per form missed it: 780 masked on the full matrix, seeds 0/1)."""
    n = bad = 0
    ids = []
    script = {x[0] for x in bl_entries() if x[2] == "script"}
    for seed in G.SEEDS:
        for eid, cid, t in negatives(seed, full=True):
            if eid in script:
                n += 1
                if secret_guard.client_view_findings(t):
                    bad += 1
                    ids.append(f"{seed}|{cid}")
    assert n > 0
    assert bad == 0, ids[:20]


def test_q2_11_k1_wins_only_for_the_shared_access_signature_forms():
    """Owner default Q2-11: a bare "SharedAccessSignature" (K-1 family) is CV-masked although M-2 says N; it is
    counted apart, never as a BL-2 pass or miss, and only that entry with only the strong-key finding is excepted."""
    assert R.entries()[23][:2] == ("J23", "sharedaccesssignature")
    assert "strong_key" in secret_guard.client_view_findings("SharedAccessSignature")
    assert secret_guard.client_view_findings("Shared Access Signature") == ["strong_key"]
    assert secret_guard.client_view_findings("Sharedaccesssignature") == []


def test_one_token_only_the_word_after_the_value_is_kept():
    """BL-1: ONE token after the separator; the sentence around it is the owner's text (whole-field mask allowed by
    F8-2 / F8-4 is stricter; a span mask must not swallow more than the value)."""
    t = bl_text("password", ": ", G.prose(51, 1)[0], "sentence", "NFC")
    out = callum._display("answer", t)
    assert out["alert"] is True
    assert G.prose(51, 1)[0] not in out["value"]


if __name__ == "__main__":                                # lead job: the full BL-3 matrix
    import json
    full = "--full" in sys.argv
    res = run(full=full)
    res["expected_full_cells"] = full_count()
    res["shown_ids"] = res["shown_ids"][:200]
    res["neg_ids"] = res["neg_ids"][:200]
    print(json.dumps(res, indent=1))
