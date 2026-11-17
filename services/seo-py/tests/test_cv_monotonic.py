"""C2-13d (lead ruling 10:40 PT, F13c-1): the OWNER DECISION 2026-11-15 ~10:00 PT "mask more" is MONOTONIC - for every
input the CV-3 / CV-4 mask of the build (runs also joined over CV-3-masked pieces longer than PIECE_MAX) is a SUPERSET
of the mask of CV-4 as written (cv_shape 2f9eb8de). The old mask is rebuilt here from cv_shape's own primitives
(VS cores + ``runs(v, None)`` + ``run_masked``), so the property is checked on the canonical view, before AL-1.
Corpora: the synthetic F13c-1 probe (made-up characters), the committed CV-8 samples, the CV-10c CI split set and the
VM CI cells (four lead variants + the value-removed text). Counts only; no text is ever printed."""
from __future__ import annotations

import json
import pathlib
import random
import unicodedata as u

import pytest

from agents import cv_shape as CV

DATA = pathlib.Path(__file__).parent / "conformance" / "data" / "cv8"


def _old_view(v: str) -> list:
    """CV-3 / CV-4 as written (pre-10:00 PT): VS cores + runs of fragments only."""
    hand = CV.handed(v)
    if hand:
        w = list(v)
        for a, b, _lane in hand:
            w[a:b] = " " * (b - a)
        v = "".join(w)
    found = []
    for a, b in CV.vs_tokens(v):
        x, y = CV.core_bounds(v[a:b])
        found.append((a + x, a + y))
    found += [(a, b) for a, b, ps in CV.runs(v, None) if CV.run_masked(v, ps)]
    return found


def _pts(spans) -> set:
    return {k for a, b in spans for k in range(a, b)}


def violations(texts) -> tuple:
    """(texts, texts where old mask is not a subset of new, texts where new masks strictly more)."""
    n = bad = more = 0
    for t in texts:
        if not t:
            continue
        n += 1
        v, _src = CV.canonical(t)
        old, new = _pts(_old_view(v)), _pts(CV.view_spans(v))
        bad += not old <= new
        more += old < new
    return n, bad, more


def test_f13c1_probe_short_split_value_stays_masked_next_to_a_long_masked_token():
    split = "Qx7 Wv3 Kz9"                              # synthetic 3-fragment split value
    for n in range(CV.PIECE_MAX + 1, 2 * CV.RUN_MAX):
        t = split + " " + ("Ab3%" * 40)[:n]            # synthetic valueish one-piece token
        sp = CV.value_spans(t)
        assert all(any(a <= k < b for a, b in sp) for k in range(len(split)) if t[k] != " "), n
    assert violations([split + " " + ("Ab3%" * 40)[:n] for n in range(1, 2 * CV.RUN_MAX)])[1] == 0


def _cv8_texts():
    for f in sorted(DATA.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                t = json.loads(line)
                yield (t.get("text") or t.get("query") or t.get("q") or "") if isinstance(t, dict) else str(t)


def test_cv8_samples_old_mask_subset_of_new():
    n, bad, more = violations(_cv8_texts())
    print("monotonic cv8 texts", n, "violations", bad, "more_masked", more)
    assert n >= 3_000 and bad == 0


def test_cv10c_ci_split_set_old_mask_subset_of_new():
    from conformance import gen_split_v2 as FS
    texts = (c["text"] for c in FS.candidates() if c["vi"] % 5 == 0 and FS.keep(c, CV.fragment, CV.valueish)[0])
    n, bad, more = violations(texts)
    print("monotonic cv10c-ci texts", n, "violations", bad, "more_masked", more)
    assert n > 0 and bad == 0


def _vm_texts(corpus: str):
    from conformance import gen_values_v2 as VG
    from conformance import test_core_vm as T
    from conformance import vm_cells as V
    vals = {s: VG.values(s, 10_000) for s in VG.SEEDS}
    words = VG.prose_pool()
    sub = T._subset(corpus)
    for i, _meta, parts in V.cells(corpus):
        if i not in sub or parts is None:
            continue
        pre, _l, sep, _v, suf, nf = parts
        val = vals[VG.SEEDS[i % 3]][(i // 3) % len(vals[VG.SEEDS[i % 3]])]
        for lab in V.leads(random.Random(f"{corpus}:{i}"), parts[1], words):
            head = u.normalize(nf, pre + lab + sep)
            yield head + val + u.normalize(nf, suf)
            yield head + u.normalize(nf, suf)


@pytest.mark.parametrize("corpus", ["e1", "e1b", "e1c", "p9"])
def test_vm_ci_cells_old_mask_subset_of_new(corpus):
    from conformance import vm_cells as V
    if not V.available():
        pytest.skip("VM corpora absent")
    n, bad, more = violations(_vm_texts(corpus))
    print("monotonic vm", corpus, "texts", n, "violations", bad, "more_masked", more)
    assert n > 0 and bad == 0
