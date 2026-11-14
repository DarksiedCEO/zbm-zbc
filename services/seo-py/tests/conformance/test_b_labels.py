"""SPEC section 11: the uncertainty labels (B-UL-01..04), as invariants over every finding this module's mixed
corpus produces (ordinary sites and hostile ones) and every envelope the agents_run helper has seen so far."""

from __future__ import annotations

import gzip
import json
import re

import pytest

from agents_run import SEEN, run3
from cert_web import Web, site
from common import rng
from gen import sites

INFERRED = {"SITEMAP_LASTMOD_ALL_IDENTICAL"}
IE_NOT_INFO = {"PAGE_UNREACHABLE": "high", "SITEMAP_UNREADABLE": "medium"}
# B-SE-01 (COVERED at 91db0abb) names ROBOTS_UNREACHABLE critical/measured/ACT with its fetch_state for 5xx, 429 and
# network failure (RFC 9309: disallow all). Read literally, B-UL-02 would make the network-failure case IE. The spec
# item that names the code wins here; recorded as open question Q-UL-1 of the Wave-1 v2 report.
SPEC_NAMED_ACT = {"ROBOTS_UNREACHABLE"}


def _hostile_webs(r):
    out = []
    w = site(Web())
    w.route("/robots.txt", "", status=503, ctype="text/plain")
    out.append(w)
    w = site(Web())
    w.route("/robots.txt", gzip.compress(b"") * 120_000, ctype="text/plain", content_encoding="gzip")
    out.append(w)
    w = site(Web())
    w.route("/", "", status=302, location="/" + "a" * 2050)
    out.append(w)
    w = site(Web())
    w.route("/sitemap.xml", "x", status=500, ctype="application/xml")
    w.route("/llms.txt", "x", status=502, ctype="text/plain")
    out.append(w)
    w = site(Web())
    w.route("/", "<html><head><link rel='canonical' href='http://site.test/t'></head><body>x</body></html>")
    w.route("/t", b"\x1f\x8b\0", ctype="text/html", content_encoding="gzip")
    out.append(w)
    w = site(Web(), robots="User-agent: *\nDisallow: /\n" + "#" * 600_000 + "\n")
    out.append(w)
    return out


def _envs():
    r = rng(7, "labels")
    envs = []
    for i in range(14):
        s = sites.build(r, i)
        _, a, b, c, _, _ = run3(s["web"], s["paths"])
        envs += [x for x in (a, b, c) if x]
    for w in _hostile_webs(r):
        _, a, b, c, _, _ = run3(w, ["/"])
        envs += [x for x in (a, b, c) if x]
    return envs + list(SEEN)


@pytest.fixture(scope="module")
def envs():
    return _envs()


def _bad_detail(d) -> bool:
    if not isinstance(d, dict):
        return False
    fs = d.get("fetch_state")
    return (fs is not None and fs != "OK") or "cap" in d or "undecided" in d or d.get("complete") is False


def test_B_UL_01_data_class_and_effect_class(envs):
    bad = []
    for e in envs:
        for f in e["findings"]:
            if f["data_class"] == "inferred" and f["code"] not in INFERRED:
                bad.append(f["code"])
            if f["code"] in INFERRED and f["data_class"] != "inferred":
                bad.append(f["code"])
            if f["data_class"] not in ("measured", "inferred") or f.get("effect_class") is not None:
                bad.append(f["code"])
    assert not bad, sorted(set(bad))


def test_B_UL_02_own_failures_are_ie_and_info(envs):
    bad = []
    for e in envs:
        for f in e["findings"]:
            if _bad_detail(f.get("detail")) and f["decision"] in ("ACT", "TEST") and f["code"] not in SPEC_NAMED_ACT:
                bad.append(f["code"])
            if f["decision"] == "INSUFFICIENT_EVIDENCE" and f["severity"] != IE_NOT_INFO.get(f["code"], "info"):
                bad.append(f["code"] + ":" + f["severity"])
    assert not bad, sorted(set(bad))


def test_B_UL_03_no_score_grade_percent_rating(envs):
    rx = re.compile(r'"[^"]*(score|grade|percent|rating)[^"]*"\s*:', re.I)
    hits = [m.group(0) for e in envs for m in rx.finditer(json.dumps(e))]
    assert not hits, hits[:5]


def test_B_UL_04_outcome_findings_facts_separate(envs):
    for e in envs:
        assert {"outcome", "findings", "facts", "not_connected"} <= set(e)
        if e["outcome"] in ("NOT_CONNECTED", "KILLED"):
            assert e["findings"] == []
