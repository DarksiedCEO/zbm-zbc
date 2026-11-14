"""priya-spec/1.1 C-5: the generators of reviews rv-priya18 (of 644b4901) and rv-priya19 (of 5bf0d2a5) are part of
the permanent suite (``rvgen``, seeded; synthetic values made at run time; each reproduces the review's own corpus,
sha256 pinned in ``data/manifest.json``). Every personal-data case is dropped (Z-1 at the function level, raw and
stored key), every composed corpus meets CP-4 (<= 1.0% overall, <= 5% for EVERY ordered pair, v1.1 C-3), and every
reviewer ordinary corpus is labelled by both independent oracles (the suite's ``oracle.priya_list`` and the
review's ``rvgen.rv19_oracle``): mandated drops required, not-mandated drops equal to ADR 0047's table for that
corpus. CI lane: a fixed stride of each corpus; heavy lane: every case (``CONFORMANCE_SHARD=i/n`` splits it)."""

from __future__ import annotations

import collections
import os

import corpora
import pytest
from oracle import priya_list as o
from priya_common import define, dropped
from rvgen import rv19_oracle as rvo
from test_priya_ordinary import adr_table

LIST = ("rv18_p1", "rv18_bnd", "rv19_m1", "rv19_b19")
CP = ("rv18_p2v2", "rv19_cp19", "rv19_cpx19")
OQ = ("rv18_ord", "rv19_oq19")
# the kept cases measured on the final code of round 20 (ADR 0047); a residual may only shrink. rv19_cp19: one
# vanity number whose seven keypad characters hold a number word ("1one..."), residual of priya-spec/1 section 8
KEPT = {"rv18_p2v2": 0, "rv19_cp19": 1, "rv19_cpx19": 0}


def _shard():
    s = os.environ.get("CONFORMANCE_SHARD", "0/1")
    i, n = (int(x) for x in s.split("/"))
    return i, n


def _cases(name: str, heavy: bool):
    if not heavy:
        yield from ((key, q) for _i, key, q in corpora.ci_rows(name))
        return
    i, n = _shard()
    for k, (key, q) in enumerate(corpora.rows(name)):
        if k % n == i:
            yield key, q


def _list(name: str, heavy: bool):
    total, kept = 0, collections.Counter()
    for key, q in _cases(name, heavy):
        total += 1
        if not dropped(q):
            kept[key] += 1
    assert total > 0
    assert sum(kept.values()) == 0, (name, total, dict(kept))       # cell ids and counts only


def _cp(name: str, heavy: bool):
    total, kept = collections.Counter(), collections.Counter()
    for key, q in _cases(name, heavy):
        total[key] += 1
        kept[key] += not dropped(q)
    n, k = sum(total.values()), sum(kept.values())
    assert n > 0 and k <= 0.01 * n, (name, k, n)
    assert k <= KEPT[name], (name, k)
    if heavy and _shard()[1] == 1:
        worst = max((kept[p] / total[p], p) for p in total)
        assert worst[0] <= 0.05, (name, worst)                       # v1.1 C-3: every ordered pair <= 5%


def _oq_tally(name: str, heavy: bool):
    t = collections.defaultdict(collections.Counter)
    for cls, q in _cases(name, heavy):
        d, m, r = dropped(q), o.mandated(q), rvo.mandated(q)
        c = t[cls]
        c["n"] += 1
        c["drop"] += d
        c["mand"] += bool(m)
        c["nm"] += d and not m
        c["leak"] += (bool(m) or bool(r)) and not d
        c["disagree"] += d and bool(m) != bool(r)
    return t


def _oq(name: str, heavy: bool):
    t = _oq_tally(name, heavy)
    assert sum(c["leak"] for c in t.values()) == 0, {k: c["leak"] for k, c in t.items() if c["leak"]}
    assert sum(c["disagree"] for c in t.values()) == 0, {k: c["disagree"] for k, c in t.items() if c["disagree"]}
    if heavy and _shard()[1] == 1:
        table = adr_table(name)
        got = {cls: (c["n"], c["drop"], c["mand"], c["nm"]) for cls, c in t.items()}
        assert got == table, {cls: (table.get(cls), v) for cls, v in got.items() if table.get(cls) != v}


for _n in LIST:
    define(globals(), f"test_C5_Z1_{_n}_ci", lambda n=_n: _list(n, False), (pytest.mark.conformance,))
    define(globals(), f"test_C5_Z1_{_n}_heavy", lambda n=_n: _list(n, True), (pytest.mark.conformance_heavy,))
for _n in CP:
    define(globals(), f"test_C5_CP4_{_n}_ci", lambda n=_n: _cp(n, False), (pytest.mark.conformance,))
    define(globals(), f"test_C5_CP4_{_n}_heavy", lambda n=_n: _cp(n, True), (pytest.mark.conformance_heavy,))
for _n in OQ:
    define(globals(), f"test_C5_OQ_{_n}_ci", lambda n=_n: _oq(n, False), (pytest.mark.conformance,))
    define(globals(), f"test_C5_OQ_{_n}_heavy", lambda n=_n: _oq(n, True), (pytest.mark.conformance_heavy,))


@pytest.mark.conformance
def test_C5_reviewer_corpora_in_the_registry():
    assert set(LIST + CP) <= set(corpora.PERSONAL) and set(OQ) <= set(corpora.ORDINARY)
    assert all(corpora.CI_STRIDE.get(n, 1) for n in LIST + CP)            # every corpus has a CI draw


@pytest.mark.conformance_heavy
def test_C5_Z2_service_path_reviewer_corpora_heavy(tmp_path, caplog):
    """Z-2 on the reviewers' corpora: 12,000 list rows drawn from them (a fixed stride of each) plus every case the
    function level keeps (the measured residual of ``KEPT``) through fake Search Console and Bing -> Priya -> every
    output: no row the function level drops reaches any output (the service path adds no leak of its own; the
    function-level keeps are the residual measured above)."""
    from agents import priya
    from test_priya_service_path import _run

    names = LIST + CP
    per = 12000 // len(names)
    rows, keeps = [], []
    for n in names:
        allq = [q for _k, q in corpora.rows(n)]
        rows += allq[::max(1, len(allq) // per)][:per]
        if n in KEPT and KEPT[n]:
            keeps += [q for q in allq if not dropped(q)]
    rows = [q for q in dict.fromkeys(rows) if dropped(q)]
    assert len(rows) >= 11500 and len(keeps) == sum(KEPT.values())
    led, blob = _run(tmp_path, caplog, rows + keeps, [])
    texts = {r["query"]["text"] for r in led["rows"]}
    assert not texts & {priya.normalize(t) for t in rows}
    needles = {x for t in rows for x in (t, priya.normalize(t)) if len(x) >= 6}
    assert {out: sum(1 for n in needles if n in b) for out, b in blob.items()} == {"api": 0, "logs": 0, "disk": 0}
