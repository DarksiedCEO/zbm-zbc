"""SPEC section 9: Osei-lite quarantine and observations (B-OS-01..05) and B-FT-10 (one observation per fetch)."""

from __future__ import annotations

import gzip
import hashlib
from datetime import timedelta

import pytest

from agents import osei
from agents_run import run3
from cert_web import Web, page, site
from clock import FixedClock, parse_iso
from common import check_ran, rng
from helpers import T0

BASES = ("wire", "decoded", "read_before_stop", "block_prefix", "serialised_answer", "none_in_hand")
KEYS = {"kind", "reason", "url", "bytes", "sha256", "size_basis", "truncated", "total_bytes", "declared_bytes", "at"}


def _o():
    return osei.Osei(FixedClock(T0))


@pytest.mark.parametrize("reason", osei.REASONS)
def test_B_OS_01_every_record_has_the_closed_shape(reason):
    o = _o()
    for i in range(10):
        data = b"x" * (i + 1)
        q = o.quarantine("sitemap", reason, "http://site.test/s.xml", data, basis="decoded")
        assert set(q) == KEYS, set(q) ^ KEYS
        assert q["size_basis"] in BASES and q["bytes"] == len(data)
        assert q["sha256"] == hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError):
        o.quarantine("sitemap", reason, None, b"x", basis="free text")


def test_B_OS_01_truncated_gzip_is_recorded_on_the_wire():
    body = gzip.compress(b"<urlset/>")[:12]
    web = site(Web(), robots="User-agent: *\nSitemap: http://site.test/s.xml.gz\n")
    web.route("/s.xml.gz", body, ctype="application/gzip")
    c, *_ = run3(web)
    q = [x for x in c.osei.summary()["quarantined"] if x["reason"] == "DECOMPRESS_FAILED"][0]
    assert q["bytes"] == 12 and q["size_basis"] == "wire"
    assert q["sha256"] == hashlib.sha256(body).hexdigest() != hashlib.sha256(b"").hexdigest()


def test_B_OS_01_too_large_read_before_stop_and_declared():
    web = site(Web())
    web.route("/", b"a" * (300 * 1024), ctype="text/html")
    c, *_ = run3(web)
    q = [x for x in c.osei.summary()["quarantined"] if x["reason"] == "TOO_LARGE"][0]
    assert q["size_basis"] == "read_before_stop" and q["truncated"] is True and q["bytes"] <= 256 * 1024 + 65536


def test_B_OS_01_no_bytes_in_hand():
    q = _o().quarantine("page", "TOO_LARGE", None, None)
    assert q["size_basis"] == "none_in_hand" and q["bytes"] is None and q["sha256"] is None


def test_B_OS_02_a_quarantined_body_is_never_parsed():
    evil = "<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'b'>]><urlset><url><loc>http://site.test/q</loc></url>" \
           "</urlset>"
    web = site(Web())
    web.route("/sitemap.xml", evil, ctype="application/xml")
    _, _, d, _, _, _ = run3(web)
    assert d["facts"]["sitemap_urls"] == 0
    assert {f["code"] for f in d["findings"]} & {"SITEMAP_QUARANTINED"}
    assert not {f["code"] for f in d["findings"]} & {"SITEMAP_LOC_NOT_ABSOLUTE", "SITEMAP_LASTMOD_INVALID"}


def test_B_OS_03_B_FT_10_one_observation_per_fetch_due_by_tier():
    r = rng(7, "os03")
    ran = 0
    for i in range(50):
        web = site(Web())
        paths = ["/"] + ([f"/p{r.randint(0, 9)}"] if r.random() < 0.5 else [])
        for p in paths:
            web.route(p, page(canonical="http://site.test" + (p if r.random() < 0.7 else "/other")))
        web.route("/other", page())
        c, *_ = run3(web, paths)
        obs = c.osei.summary()["observations"]
        assert len(obs) + c.osei.observations_dropped == len(web.requests), (len(obs), web.requests)
        for o in obs:
            tier = osei.REFRESH_TIERS.get(o["kind"].split("_")[0] if o["kind"].startswith("page") else o["kind"],
                                          timedelta(days=7))
            assert parse_iso(o["refresh_due"]) - parse_iso(o["observed_at"]) == tier
            assert o["url"] is None or "?" not in o["url"]
        ran += 1
    check_ran("B-OS-03", ran)
    check_ran("B-FT-10", ran)


def test_B_OS_04_caps_counted():
    o = _o()
    for i in range(270):
        o.quarantine("page", "TOO_LARGE", None, b"x")
    for i in range(570):
        o.observed("page", None, "OK")
    s = o.summary()
    assert len(s["quarantined"]) == 200 and s["quarantined_dropped"] == 70
    assert len(s["observations"]) == 500 and s["observations_dropped"] == 70


def test_B_OS_05_refresh_tier_table():
    assert {k: v.days for k, v in osei.REFRESH_TIERS.items()} == {
        "robots": 1, "sitemap": 1, "llms_txt": 7, "page": 7, "structured_data": 7, "ai_probe": 7,
        "entity_record": 180}
