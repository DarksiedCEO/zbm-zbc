"""marcus-pipeline-spec/1 section 1: the Release Watcher (B-MP-01..04; GP-1)."""

from __future__ import annotations

import json

import pytest

import config as config_mod
from conformance.marcus_pipeline import gen_cc
from conformance.marcus_pipeline.util import job, kinds_logged, watch
from helpers import base_env, rid

REGISTRY = {
    "cc_release_watcher": {
        "assignment": "notice a new Common Crawl crawl or web-graph release and record it",
        "inputs": ["the cc_releases port (v1: a fixture release list shaped as CC's published crawl list, GP-1)"],
        "outputs": ["cc_release_seen records {crawl id, kind, published date}"],
        "name_pending": True, "proposed_name": "Ruth"},
}


def test_B_MP_01_good_release_list_is_read_and_recorded(mp):
    j = watch(mp)
    assert j["outcome"] == "OK", j
    seen = sorted((r["kind"], r["id"]) for r in mp.svc.cc_releases.values())
    assert seen == [("crawl", "CC-MAIN-2026-40"), ("crawl", "CC-MAIN-2026-44"),
                    ("domain_graph", gen_cc.GRAPH), ("host_graph", gen_cc.GRAPH)]
    facts = j["facts"]
    assert facts["doc_source"] and facts["read_date"] == "2026-11-13"       # the fixture names its source and date
    assert facts["releases_seen"] == 4 and facts["releases_new"] == 4


def _bad_crawls():
    good = gen_cc.crawl_item("CC-MAIN-2026-40")
    cases = {
        "id_grammar": [{**good, "id": "CC-MAIN-26-40"}],
        "id_lower": [{**good, "id": "cc-main-2026-40"}],
        "week_54": [{**good, "id": "CC-MAIN-2026-54"}],
        "week_00": [{**good, "id": "CC-MAIN-2026-00"}],
        "extra_key": [{**good, "extra": "x"}],
        "missing_key": [{k: v for k, v in good.items() if k != "cdx-api"}],
        "http_scheme": [{**good, "timegate": good["timegate"].replace("https", "http")}],
        "other_host": [{**good, "cdx-api": "https://index.commoncrawl.org.evil.test/CC-MAIN-2026-40-index"}],
        "userinfo": [{**good, "timegate": "https://u:p@index.commoncrawl.org/CC-MAIN-2026-40/"}],
        "port": [{**good, "timegate": "https://index.commoncrawl.org:8443/CC-MAIN-2026-40/"}],
        "duplicate": [good, good],
        "not_list": {"crawls": [good]},
        "name_type": [{**good, "name": 7}],
        "name_long": [{**good, "name": "x" * 1000}],
    }
    return cases


@pytest.mark.parametrize("case", sorted(_bad_crawls()))
def test_B_MP_01_any_other_shape_is_quarantined_SOURCE_SCHEMA_UNKNOWN(mp, case):
    (mp.fx / "collinfo.json").write_text(json.dumps(_bad_crawls()[case]))
    j = watch(mp)
    assert j["outcome"] == "QUARANTINED" and j["facts"]["reason"] == "SOURCE_SCHEMA_UNKNOWN", j
    assert mp.svc.cc_releases == {}
    assert "cc_release_seen" not in kinds_logged(mp)


def test_B_MP_01_crawl_id_year_and_week_edges():
    """The reading of ``CC-MAIN-YYYY-WW``: years 2008 (Common Crawl's first) to 2099, ISO weeks 1 to the year's last."""
    from marcus.release_watcher import valid_crawl_id
    good = ("CC-MAIN-2008-01", "CC-MAIN-2008-52", "CC-MAIN-2099-53", "CC-MAIN-2026-01", "CC-MAIN-2026-53",
            "CC-MAIN-2020-53")
    bad = ("CC-MAIN-2007-52", "CC-MAIN-2100-01", "CC-MAIN-2026-00", "CC-MAIN-2008-53", "CC-MAIN-2021-53", None, 2026)
    assert [s for s in good if not valid_crawl_id(s)] == []
    assert [s for s in bad if valid_crawl_id(s)] == []


@pytest.mark.parametrize("graphs", [
    [gen_cc.graph_item(kind="page_graph")], [gen_cc.graph_item(published="2026-13-01")],
    [gen_cc.graph_item(published="20261020")], [{**gen_cc.graph_item(), "url": "https://data.commoncrawl.org/"}],
    [gen_cc.graph_item(gid="CC MAIN 2026")], [gen_cc.graph_item(gid="cc-main-" + "a" * 80)],
    [gen_cc.graph_item(), gen_cc.graph_item()], "graphs"])
def test_B_MP_01_graph_release_shapes_are_strict(mp, graphs):
    (mp.fx / "graphinfo.json").write_text(json.dumps(graphs))
    j = watch(mp)
    assert j["outcome"] == "QUARANTINED" and j["facts"]["reason"] == "SOURCE_SCHEMA_UNKNOWN"
    assert mp.svc.cc_releases == {}


@pytest.mark.parametrize("raw", [b"", b"not json", b"[" * 200 + b"]" * 200, b'{"a": NaN}', b"\xff\xfe",
                                 b"[" + b"1," * 600_000 + b"1]"])
def test_B_MP_01_unreadable_or_hostile_list_files_are_quarantined(mp, raw):
    (mp.fx / "collinfo.json").write_bytes(raw)
    j = watch(mp)
    assert j["outcome"] == "QUARANTINED" and j["facts"]["reason"] == "SOURCE_SCHEMA_UNKNOWN"


def test_B_MP_02_each_release_recorded_once_and_never_a_data_fetch(mp, monkeypatch):
    from marcus import wat_fetcher
    opened = []
    monkeypatch.setattr(wat_fetcher.FixtureWatSource, "open", lambda self, *a, **k: opened.append(a))
    first = watch(mp)
    again = watch(mp)
    assert first["facts"]["releases_new"] == 4 and again["facts"]["releases_new"] == 0
    assert kinds_logged(mp).count("cc_release_seen") == 4
    assert opened == []                                                       # never fetches data
    body = {"request_id": rid()}
    a = job(mp, "cc_release_watcher", **body)
    b = job(mp, "cc_release_watcher", **body)                                 # a replay answers the first job
    assert a["job_id"] == b["job_id"]
    assert kinds_logged(mp).count("link_index_job_requested") == 3


def test_B_MP_02_a_new_release_is_recorded_after_restart_once(mpd):
    watch(mpd)
    gen_cc.write_releases(mpd.fx, crawls=gen_cc.CRAWLS + ("CC-MAIN-2026-48",))
    h2 = mpd.restart()
    j = watch(h2)
    assert j["facts"]["releases_new"] == 1
    seen = [r for r in h2.svc.log.iter_records() if r["kind"] == "cc_release_seen"]
    assert len(seen) == 5 and len({(r["data"]["kind"], r["data"]["release_id"]) for r in seen}) == 5
    rec = [r["data"] for r in seen if r["data"]["release_id"] == "CC-MAIN-2026-48"][0]
    assert rec["kind"] == "crawl" and rec["published"] is None                # GP-1's crawl shape has no date


@pytest.mark.parametrize("name", ["SEO_CC_RELEASES_ENABLED", "SEO_CC_WAT_ENABLED"])
def test_B_MP_03_live_ports_are_NOT_BUILT_in_v1(name):
    assert name in config_mod.NOT_BUILT
    with pytest.raises(RuntimeError):
        config_mod.load(base_env(**{name: "1"}))
    config_mod.load(base_env(**{name: "0"}))


def test_B_MP_03_v2_budget_one_request_a_day_and_default_not_connected(make_mp):
    from marcus import release_watcher
    assert release_watcher.LIVE_REQUESTS_PER_DAY == 1
    h = make_mp(fixture=False)
    assert h.svc.ports.cc_releases.connected is False
    j = watch(h)
    assert j["outcome"] == "NOT_CONNECTED" and j["not_connected"] == ["cc_releases"]
    assert h.svc.cc_releases == {}


def test_B_MP_04_registry_byte_for_byte(mp):
    import svc_manager
    for agent, block in REGISTRY.items():
        shown = {**svc_manager.REGISTRY[agent], **svc_manager.NAMES[agent]}       # naming beside the block (Q-MP-1)
        assert set(svc_manager.REGISTRY[agent]) == {"assignment", "inputs", "outputs"}
        assert json.dumps(shown, sort_keys=True) == json.dumps(block, sort_keys=True)
        assert agent in svc_manager.AGENTS and agent not in svc_manager.NOT_BUILT_AGENTS
    dept = mp.ok(mp.get("/department"))
    card = dept["agents"]["cc_release_watcher"]
    assert {**card["registry"], "name_pending": card["name_pending"], "proposed_name": card["proposed_name"]} \
        == REGISTRY["cc_release_watcher"]
