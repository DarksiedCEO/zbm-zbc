"""SPEC section 14: O-01 (correct findings on ordinary sites, per-site multiset equality with the generator's
ground truth), O-02 (differential, lead gate), O-04 (useful owner display), O-05 (grouping), O-07 (slow sites)."""

from __future__ import annotations

import collections
import os
import time

import httpx
import pytest

from agents_run import run3
from common import check_ran, rng
from gen import sites

BAD_STATES = {"BLOCKED", "INSUFFICIENT_EVIDENCE", "FAILED"}


def _multiset(envs) -> collections.Counter:
    return collections.Counter((e["agent"], f["code"], f["severity"], f["data_class"], f["decision"])
                               for e in envs if e for f in e["findings"])


def _corpus(n: int, seed: int):
    r = rng(seed, "o01")
    for i in range(n):
        yield i, sites.build(r, i)


@pytest.mark.parametrize("seed", [7])
def test_O_01_ordinary_sites_findings_equal_the_ground_truth(seed):
    ran = 0
    for i, s in _corpus(40, seed):
        _, a, b, c, pages, _ = run3(s["web"], s["paths"])
        got = _multiset([a, b, c])
        want = collections.Counter(s["truth"])
        assert got == want, (i, s["lang"], s["cms"], sorted((got - want).elements()), sorted((want - got).elements()))
        for e in (a, b, c):
            assert e["outcome"] == "OK", (i, e["agent"], e["outcome"])
        for p in pages.values():
            assert p["fetch"].state == "OK"
        ran += 1
    check_ran("O-01", ran)


@pytest.mark.lead_gate
def test_O_01_lead_gate_400_sites():
    for i, s in _corpus(400, 20261113):
        _, a, b, c, _, _ = run3(s["web"], s["paths"])
        assert _multiset([a, b, c]) == collections.Counter(s["truth"]), (i, s["lang"], s["cms"])


@pytest.mark.lead_gate
def test_O_02_differential_against_reference_trees():
    """devtools/conformance/o02_differential.py on the reference trees named in CONFORMANCE_REF_TREES (``:``-separated
    directories of a4af277 / 91db0abb services/seo-py, exported with ``git archive``): every changed finding is
    listed with the spec item that requires it; 0 unexplained."""
    import subprocess
    import sys
    from pathlib import Path
    refs = [x for x in os.environ.get("CONFORMANCE_REF_TREES", "").split(":") if x]
    assert refs, "CONFORMANCE_REF_TREES not set (lead gate): export a4af277 and 91db0abb with git archive"
    script = Path(__file__).resolve().parents[2] / "devtools" / "conformance" / "o02_differential.py"
    for ref in refs:
        r = subprocess.run([sys.executable, str(script), "--ref", ref, "--n", "400"], capture_output=True, text=True,
                           timeout=3600)
        assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]


def test_O_04_every_finding_with_a_location_has_a_where_and_well_known_names_readable():
    r = rng(5, "o04")
    for i in range(6):
        s = sites.build(r, i)
        _, a, b, c, _, ev = run3(s["web"], s["paths"])
        for e in (a, b, c):
            for f in e["findings"]:
                if f.get("url") is not None:
                    assert f["url"].startswith("http://site.test/"), f["url"]
        sm = b["facts"].get("sitemaps_declared") or []
        for u in sm:
            assert "sitemap" in u.lower(), (u, s["cms"])


def test_O_05_grouping_by_code_equals_grouping_by_raw_value():
    from primitives import codes
    from agents_run import coder
    r = rng(9, "o05")
    raw = [sites.word(r, "en", r.randint(2, 4)) for _ in range(3000)]
    with codes.coding(coder(), own=("site.test",)):
        coded = [codes.code("seg", v) for v in raw]
    by_raw = collections.Counter(raw)
    by_code = collections.Counter(coded)
    assert sorted(by_raw.values()) == sorted(by_code.values())


def test_O_07_slow_sites_give_the_same_findings():
    r = rng(11, "o07")
    for i in range(3):
        s = sites.build(r, i)
        fast = _multiset(run3(s["web"], s["paths"])[1:4])
        orig = s["web"].handle

        def slow(req, orig=orig):
            time.sleep(0.05)
            return orig(req)
        s["web"].handle = slow
        t0 = time.monotonic()
        got = _multiset(run3(s["web"], s["paths"])[1:4])
        assert got == fast and time.monotonic() - t0 < 30
    assert httpx
