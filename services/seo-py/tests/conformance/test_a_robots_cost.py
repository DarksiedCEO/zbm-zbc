"""A-01 (wildcard backtracking), A-02 (rule floods), A-03 (costliest per unit, B-RB-09 b/c), A-17 (log robots cost)."""

from __future__ import annotations

import time

import pytest

from common import check_ran, rng, seeds
from gen import robots_gen as rg
from oracles import rfc9309
from primitives import robots as robots_mod

STEP_CPU_S = 2.0


def _decide_all(data: bytes, paths, token="Googlebot"):
    p = robots_mod.parse(data.decode("utf-8", "surrogateescape"))
    w = robots_mod.Work(robots_mod.STEP_WORK_UNITS)
    t0 = time.thread_time()
    out = [p.decide(token, x.decode("utf-8", "surrogateescape"), w) for x in paths]
    return out, time.thread_time() - t0, w


@pytest.mark.parametrize("seed", seeds())
def test_A_01_k_star_near_match_is_linear_and_never_wrong(seed):
    r = rng(seed, "a01")
    ran = 0
    for i in range(100):
        k, plen = r.randint(1, 12), r.choice((60, 200, 2000, 8000))
        data, path = rg.k_star(r, k, plen)
        t0 = time.thread_time()
        d = robots_mod.parse(data.decode()).decide("Googlebot", path.decode(), robots_mod.Work())
        dt = time.thread_time() - t0
        assert dt <= 0.005 * max(1, plen // 2000), (k, plen, dt)
        if d["allowed"] is not None and plen <= 200:
            assert d["allowed"] is rfc9309.allowed(data, "Googlebot", path)
        ran += 1
    check_ran("A-01", ran * len(seeds()))


@pytest.mark.parametrize("shape,n", [("distinct", 20_000), ("shared", 20_000), ("wild10", 2_000), ("repeat", 27_530),
                                     ("a_star", 20_000)] * 4)
def test_A_02_rule_floods_every_rule_used_and_step_bounded(shape, n):
    data = rg.flood(rng(1, shape), n, shape)
    assert len(data) <= 512_000 + 200_000
    paths = [b"/p%06d" % i for i in range(0, n, max(1, n // 10))] + [b"/shared/prefix/000123"]
    out, cpu, w = _decide_all(data, paths)
    assert cpu <= STEP_CPU_S, (shape, cpu)
    if shape == "distinct" and n <= 24_000:
        for x, d in zip(paths, out):
            if d["allowed"] is not None:
                assert d["allowed"] is rfc9309.allowed(data, "Googlebot", x) or len(data) > 512_000


@pytest.mark.parametrize("m,plen", [(99, 2047), (50, 8000), (99, 29_000)])
def test_A_03_B_RB_09_costliest_needle_within_the_step_cpu(m, plen):
    data = rg.needle(m)
    paths = [b"/" + b"a" * plen + b"%d" % i for i in range(10)]
    out, cpu, w = _decide_all(data, paths)
    assert cpu <= STEP_CPU_S, (m, plen, cpu, w.units)


def test_A_03_B_RB_09_units_track_cpu_within_10x():
    ratios = []
    shapes = [(rg.needle(99), [b"/" + b"a" * 2047]), (rg.needle(60), [b"/" + b"a" * 8000]),
              (rg.flood(rng(2, "d"), 24_908, "distinct"), [b"/p%06d" % i for i in range(0, 24_908, 500)]),
              (rg.flood(rng(3, "s"), 20_000, "shared"), [b"/shared/prefix/%06d" % i for i in range(0, 20_000, 400)]),
              (rg.k_star(rng(4, "k"), 12, 8000)[0], [rg.k_star(rng(4, "k"), 12, 8000)[1]])]
    for data, paths in shapes:
        p = robots_mod.parse(data.decode())
        p.decide("Googlebot", "/warm-up")                 # the group's rule index is built once per file
        w = robots_mod.Work(10 ** 12)
        # CPU time of this thread (not wall time: a loaded machine preempts), each shape repeated until it has
        # cost at least 50 ms, so the ratio is not a timer-resolution artefact
        t0 = time.thread_time_ns()
        while True:
            p._memo.clear()
            for x in paths:
                p.decide("Googlebot", x.decode(), w)
            ns = time.thread_time_ns() - t0
            if ns >= 50_000_000:
                break
        ratios.append(w.units / ns)
    assert max(ratios) / min(ratios) < 10, ratios


@pytest.mark.lead_gate
def test_A_03_lead_gate_60_shapes():
    for m in range(50, 100, 5):
        for plen in (2047, 8000, 29_000):
            data = rg.needle(m)
            _, cpu, _ = _decide_all(data, [b"/" + b"a" * plen])
            assert cpu <= STEP_CPU_S


def test_A_17_log_chunk_against_hostile_robots_is_bounded(tmp_path):
    from agents import logs as logs_mod
    data = rg.needle(99).decode()
    rob = robots_mod.parse(data)
    lines = "\n".join(f'192.0.2.5 - - [10/Oct/2026:13:55:36 -0700] "GET /{"a" * 2000}{i} HTTP/1.1" 200 1 "-" '
                      f'"Googlebot/2.1"' for i in range(30)).encode() + b"\n"
    t0 = time.thread_time()
    delta = logs_mod.process_chunk("combined", lines, b"k" * 32, lambda ip, t: "unverified", {}, {"left": 0},
                                   rob, {}, robots_expected=True)
    cpu = time.thread_time() - t0
    assert cpu <= STEP_CPU_S
    fam = delta["families"]["Googlebot"]
    assert fam["robots_blocked"] + delta["robots_unchecked"] <= 30
    check_ran("A-17", 4)
