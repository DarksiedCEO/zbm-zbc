"""core-spec/2 CV-10a (value matrix VM, label-independence meta-test) and CV-10b (F-1, Greek UPPER split after the
inner sigma). Cells come from the review corpora rv-sink8 E1 / E1b / E1c and rv-sink9 p9 (vm_cells.py); values are
VG (CV-9) values drawn at run time; labels are referenced by cell index only. Failures print corpus ids, cell indexes
and meta classes, never a label text or a value.

CI lane (G-2): >= 10 cells per (entry, SP / form class, SEP class) stratum of every corpus (all cells of a smaller
stratum), sharded by test id. Heavy lane (lead): every cell, CONFORMANCE_HEAVY=1 CONFORMANCE_SHARD=i/n."""
from __future__ import annotations

import collections
import json
import os
import random

import pytest

from conformance import vm_cells as V

pytestmark = [pytest.mark.conformance,
              pytest.mark.skipif(not V.available(), reason="VM corpora (rv-sink8 / rv-sink9 work folders) absent")]
CI_PER_STRATUM = 10
CI_SEED = 20261114
CI_SHARDS = {"e1": 24, "e1b": 1, "e1c": 2, "p9": 6}
_SUBSETS: dict = {}


def _stratum(corpus: str, meta: list) -> tuple:
    """G-2 stratum. p9 meta carries SP; E-corpus meta carries a form class 'family|form': the family stands for SP."""
    if corpus == "p9":
        return V.group_key(corpus, meta)
    return (corpus, meta[0], meta[1].split("|")[0], V.E_SEP_CLASS[int(meta[2])])


def _subset(corpus: str) -> frozenset:
    # lead ruling 21:50 PT F12b-2 (ruling F12-3 as written): 10 cells per stratum sampled with a fixed seed.
    if corpus not in _SUBSETS:
        strata: dict = collections.defaultdict(list)
        for i, meta, parts in V.cells(corpus):
            if parts is not None:
                strata[_stratum(corpus, meta)].append(i)
        r = random.Random(CI_SEED)
        keep = set()
        for k in sorted(strata):
            ids = strata[k]
            keep.update(ids if len(ids) <= CI_PER_STRATUM else r.sample(ids, CI_PER_STRATUM))
        _SUBSETS[corpus] = frozenset(keep)
    return _SUBSETS[corpus]


def _evidence(name: str, res: dict) -> None:
    d = os.environ.get("VM_EVIDENCE_DIR")
    if d:
        with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(res, f)


def _check(res: dict) -> None:
    # lead ruling 21:35 PT F12-1: identity = (a) value core masked, (b) mask end, (c) masked value code points;
    # a masked label glued to the value (ruling 16:45) is not a difference - counted and printed.
    # Ruling 23:58: separator masked identically in all four variants (class A) allowed - counted (sep_masked_cells).
    # Ruling 00:10: M1 / M2 / M3 (+ E wrapper) cells judged on the value core - counted per mechanism (cells / diff).
    print(res["corpus"], "evaluated", res["evaluated"], "glued_label_cells", res["glued_label_cells"],
          "sep_masked_cells", res["sep_masked_cells"], "mech_cells", res["mech_cells"], "mech_diff", res["mech_diff"],
          "shown", res["shown"], "span_diff", res["span_diff"])
    assert res["shown"] == 0, (res["corpus"], "value shown", res["shown"], res["shown_ids"][:10])
    assert res["span_diff"] == 0, (res["corpus"], "span differs across leads", res["span_diff"],
                                   res["span_diff_by_sep"], res["span_diff_ids"][:10])


@pytest.mark.parametrize(("corpus", "shard"), [(c, k) for c, n in CI_SHARDS.items() for k in range(n)])
def test_vm_ci_subset_value_masked_and_span_identical_across_leads(corpus, shard):
    sub = _subset(corpus)
    res = V.run(corpus, select=lambda i, _m: i in sub, shard=(shard, CI_SHARDS[corpus]))
    _evidence(f"ci_{corpus}_{shard}", res)
    assert res["evaluated"] > 0
    _check(res)


def test_vm_corpora_sizes_and_decomposition():
    for corpus, n in V.SIZES.items():
        cnt = collections.Counter("value" if p else "none" for _i, _m, p in V.cells(corpus))
        assert sum(cnt.values()) == n, corpus
        if corpus != "p9":
            assert cnt["none"] == 0, corpus             # every E cell carries a value and a label
    assert len(_subset("e1b")) >= CI_PER_STRATUM


@pytest.mark.conformance_heavy
@pytest.mark.parametrize("corpus", list(V.SIZES))
def test_vm_full_value_masked_and_span_identical_across_leads(corpus):
    i, n = (int(x) for x in os.environ.get("CONFORMANCE_SHARD", "0/1").split("/"))
    res = V.run(corpus, shard=(i, n))
    _evidence(f"full_{corpus}_{i}of{n}", res)
    _check(res)


# CV-10b F-1 (lead ruling 21:50 PT, option (a): cells by construction; count guards = the exact class sizes).
F1_RV9_SPELLINGS, F1_RV9_CELLS = 10, 60          # 10 spellings x {space, NBSP} x {": ", " = ", " is "}
# Lead ruling 22:05 PT F12c-1: the spec's 18+18 / 33+33 are the rv-sink9 review's lost-vs-baseline SUBSETS of these
# classes; the whole class is tested (stricter, contains them). Guards = the exact measured class sizes (CV lane).
F1_E1B_SP_NBSP, F1_E1B_NL_ZWSP = 60, 60          # e1b S22 UPPER after the inner sigma, SEP 0/2/7
F1_PINNED = 72                                   # e1b S22 UPPER at the first sigma ("to"|"bas-"), sp + NBSP, SEP 0/2/7
# Lead ruling 22:05 PT F12c-2 (rv-sink9 NOTES.md line 7): H4 = space / H5 = NBSP split after "PROS" (sigma at
# pos 11) = the first-sigma split, x {": ", " = ", " is "} x the 10 spellings; present in the first-sigma class.
F1_H45_SPELLINGS, F1_H4, F1_H5, F1_H45_SIGMA_POS = 10, 30, 30, [11]
F1_VALUES_PER_CELL = 10


def _f1_shown(cells: list, tag: str, vals: list, r: random.Random) -> list:
    from agents import cv_shape as CV
    shown = []
    for i, parts in cells:
        for _ in range(F1_VALUES_PER_CELL):
            s, _diff, _glued = V.measure(parts, vals[r.randrange(len(vals))], [parts[1]], CV.value_spans,
                                         CV.core_bounds)
            if s:
                shown.append((tag, i))
    return shown


def _f1_vals() -> list:
    from conformance import gen_values_v2 as VG
    return [v for s in VG.SEEDS for v in VG.values(s, 400)]


def test_f1_rv_sink9_sixty_cells_by_construction():
    n_sp, cells = V.f1_rv9_cells()
    shown = _f1_shown(list(enumerate(cells)), "rv9", _f1_vals(), random.Random(CI_SEED))
    print("f1 rv9 spellings", n_sp, "cells", len(cells), "values", len(cells) * F1_VALUES_PER_CELL, "shown",
          len(shown))
    _evidence("f1_rv9", {"spellings": n_sp, "cells": len(cells), "shown": len(shown)})
    assert (n_sp, len(cells)) == (F1_RV9_SPELLINGS, F1_RV9_CELLS), (n_sp, len(cells))
    assert not shown, (len(shown), shown[:10])


def test_f1_e1b_inner_sigma_and_pinned_cells():
    sets = {"e1b_sp_nbsp": V.e1b_s22_sigma(2, ("sp", "nbsp")), "e1b_nl_zwsp": V.e1b_s22_sigma(2, ("nl", "zwsp+sp")),
            "pinned_first_sigma": V.e1b_s22_sigma(1, ("sp", "nbsp"))}
    n_h, h45, h_pos = V.h45_cells()
    vals, r = _f1_vals(), random.Random(CI_SEED)
    shown = [x for k, c in sets.items() for x in _f1_shown([(i, p) for i, _m, p in c], k, vals, r)]
    shown += [x for k, c in h45.items() for x in _f1_shown(list(enumerate(c)), k, vals, r)]
    first_labels = {p[1] for _i, _m, p in sets["pinned_first_sigma"]}
    h_missing = sum(1 for c in h45.values() for p in c if p[1] not in first_labels)
    counts = {k: len(c) for k, c in sets.items()} | {k: len(c) for k, c in h45.items()}
    print("f1 e1b", counts, "h45 spellings", n_h, "h45 sigma pos", h_pos, "h45 not in first-sigma class", h_missing,
          "values per cell", F1_VALUES_PER_CELL, "shown", len(shown))
    _evidence("f1_e1b", {"cells": counts, "h45_spellings": n_h, "h45_sigma_pos": h_pos, "h45_missing": h_missing,
                         "shown": len(shown)})
    assert not shown, (len(shown), shown[:10])
    assert (counts["e1b_sp_nbsp"], counts["e1b_nl_zwsp"]) == (F1_E1B_SP_NBSP, F1_E1B_NL_ZWSP), counts
    assert counts["pinned_first_sigma"] == F1_PINNED, counts
    assert (n_h, counts["H4"], counts["H5"], h_pos) == (F1_H45_SPELLINGS, F1_H4, F1_H5, F1_H45_SIGMA_POS), counts
    assert h_missing == 0, h_missing
