"""priya-spec/1.1 C-5: the generators of reviews rv-priya18 (of 644b4901), rv-priya19 (of 5bf0d2a5) and rv-priya20
(of a394a985) are part of the permanent suite (``rvgen``, seeded; synthetic values made at run time; each reproduces the review's own corpus,
sha256 pinned in ``data/manifest.json``). Every personal-data case is WITHHELD (priya-spec/2 section 4 Z-PROMOTED: 0 READABLE at the function level, every
corpus, every cell, every ordered pair; the v1 CP-4 / C-3 percentage caps are REPLACED, 0.2), and every
reviewer ordinary corpus is labelled by both independent oracles (the suite's ``oracle.priya_list`` and the
review's ``rvgen.rv19_oracle``): mandated drops required; heavy lane (GATE-2H re-pin, G-7): the v1 equality with
ADR 0047's 33472699 table (``priya-oq-table[-corpus]``) is REPLACED by priya-spec/2 section 7 OQ-4 / OQ-DISCLOSE
(n, WITHHELD, mandated, not-mandated WITHHELD equal the ADR v2 table ``priya-oq2-table-<corpus>`` exactly) AND OQ-CAP
(not-mandated WITHHELD <= 5.0% of the not-mandated queries of the corpus and of each plain / question / local class).
Z-2 service path: spec 2 F-7 (no code key -> ``VAULT_UNAVAILABLE``) needs the synthetic V-2 key (P2-5), set here.
OQ-2 labels (lead ruling 22:55 PT, GATE-2H2): the labelling authority is the independent oracle (rv ``oracle.py`` + the
ADR 0047 address rule = ``rvgen.rv19_oracle``); OQ-4 / OQ-CAP count with its labels. The suite-vs-reviewer agreement
check is kept as a self-check: each disagreement is REPORTED (corpus, class, index, both rule labels; never the text)
and fails only if it changes an OQ-CAP verdict (corpus or a capped class); a mandated query READABLE always fails.
CI lane: a fixed stride of each corpus; heavy lane: every case (``CONFORMANCE_SHARD=i/n`` splits it)."""

from __future__ import annotations

import collections
import os

import corpora
import pytest
from oracle import priya_list as o
from priya_common import define, dropped, readable_report
from rvgen import rv19_oracle as rvo
from test_priya_ordinary import CAP, CAPPED, adr_table_v2

LIST = ("rv18_p1", "rv18_bnd", "rv19_m1", "rv19_b19", "rv20_m20", "rv20_b20", "rv20_vg", "rv21_m21", "rv21_b21",
        "rv21_d21", "rv21_short")
CP = ("rv18_p2v2", "rv19_cp19", "rv19_cpx19", "rv20_cp20", "rv21_cp21")
OQ = ("rv18_ord", "rv19_oq19", "rv20_oq20", "rv21_oq21")
# priya-spec/2 section 4 Z-PROMOTED: 0 READABLE on every frozen CP corpus, every ordered pair (0.2: CP-4 / C-3 caps
# REPLACED; 9: P-r11 removed). v1 kept rv19_cp19 1 and rv20_cp20 1 (spec 12 "current"); the v2 build measures 0
# (lead 19:10 PT zabs on priya_gate 730102c8). A residual may only shrink: every entry is 0.
KEPT = {"rv18_p2v2": 0, "rv19_cp19": 0, "rv19_cpx19": 0, "rv20_cp20": 0, "rv21_cp21": 0}


def _shard():
    s = os.environ.get("CONFORMANCE_SHARD", "0/1")
    i, n = (int(x) for x in s.split("/"))
    return i, n


def _icases(name: str, heavy: bool):
    if not heavy:
        yield from corpora.ci_rows(name)
        return
    i, n = _shard()
    for k, (key, q) in enumerate(corpora.rows(name)):
        if k % n == i:
            yield k, key, q


def _cases(name: str, heavy: bool):
    yield from ((key, q) for _k, key, q in _icases(name, heavy))


def _list(name: str, heavy: bool):
    total, kept = 0, collections.Counter()
    for key, q in _cases(name, heavy):
        total += 1
        if not dropped(q):
            kept[key] += 1
    readable_report(name, total, sum(kept.values()))
    assert total > 0
    assert sum(kept.values()) == 0, (name, total, dict(kept))       # cell ids and counts only


def _cp(name: str, heavy: bool):
    total, kept = collections.Counter(), collections.Counter()
    for key, q in _cases(name, heavy):
        total[key] += 1
        kept[key] += not dropped(q)
    n, k = sum(total.values()), sum(kept.values())
    readable_report(name, n, k)
    assert n > 0 and k <= KEPT[name] == 0, (name, k, n)            # spec 4: 0 overall (no 1% cap)
    pairs = {p: c for p, c in kept.items() if c}
    assert not pairs, (name, pairs)                                  # spec 4: every ordered pair 0 (no 5% cap, C-3)


def _oq_tally(name: str, heavy: bool):
    """Per class: m = OQ-2 label (``rvo``, the authority), s = the suite oracle's label (self-check only)."""
    t, dis = collections.defaultdict(collections.Counter), []
    for idx, cls, q in _icases(name, heavy):
        d, s, m = dropped(q), o.mandated(q), rvo.mandated(q)
        c = t[cls]
        c["n"] += 1
        c["drop"] += d
        c["mand"] += bool(m)
        c["nm"] += d and not m
        c["notm"] += not m
        c["nm_s"] += d and not s
        c["notm_s"] += not s
        c["leak"] += (bool(m) or bool(s)) and not d
        if d and bool(m) != bool(s):
            dis.append((cls, idx, s or "-", m or "-"))
    return t, dis


def _over(t, nm: str, notm: str) -> dict:
    """OQ-CAP cells over 5.0%: the corpus and each capped class (plain / question / local)."""
    cells = {"corpus": (sum(c[nm] for c in t.values()), sum(c[notm] for c in t.values()))}
    cells |= {cls: (c[nm], c[notm]) for cls, c in t.items() if cls in CAPPED}
    return {k: f"{a}/{b}" for k, (a, b) in cells.items() if b and 100.0 * a / b > CAP}


def _oq(name: str, heavy: bool):
    t, dis = _oq_tally(name, heavy)
    assert sum(c["leak"] for c in t.values()) == 0, {k: c["leak"] for k, c in t.items() if c["leak"]}
    for cls, idx, s, m in dis:                                       # ids + both labels only, never query text
        print(f"C5-OQ-DISAGREE {name} {cls}#{idx} suite={s} oq2={m}")
    print(f"C5-OQ-DISAGREE {name} total={len(dis)}")
    if dis and _shard()[1] == 1:                                     # ruling 22:55 PT: fails only on a flipped verdict
        a, b = sorted(_over(t, "nm", "notm")), sorted(_over(t, "nm_s", "notm_s"))
        assert a == b, (name, "disagreement flips an OQ-CAP verdict", {"oq2": a, "suite": b}, dis[:20])
    if heavy and _shard()[1] == 1:
        adr = adr_table_v2(name)                                     # spec 7 OQ-4 / OQ-DISCLOSE (v2 table)
        assert adr is not None, (name, "ADR 0047 has no priya-oq2-table for this corpus")
        table = {cls: r[:4] for cls, r in adr.items()}
        got = {cls: (c["n"], c["drop"], c["mand"], c["nm"]) for cls, c in t.items()}
        assert got == table, {cls: (table.get(cls), v) for cls, v in got.items() if table.get(cls) != v}
        over = _over(t, "nm", "notm")
        assert not over, (name, over)                                # spec 7 OQ-CAP 5.0% (owner decision 2)


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
def test_C5_Z2_service_path_reviewer_corpora_heavy(tmp_path, caplog, monkeypatch):
    """Z-2 on the reviewers' corpora: 12,000 list rows drawn from them (a fixed stride of each) plus every case the
    function level keeps (the measured residual of ``KEPT``) through fake Search Console and Bing -> Priya -> every
    output: no row the function level drops reaches any output (the service path adds no leak of its own; the
    function-level keeps are the residual measured above)."""
    from agents import priya
    from test_priya import VAULT_ENV
    from test_priya_service_path import _run

    for k, v in VAULT_ENV.items():                                   # spec 2 F-7 / V-2: synthetic code key (P2-5)
        monkeypatch.setenv(k, v)
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
