"""The common base (SUITE_PLAN part 1: CB-1..CB-21) for the five v1 Marcus pipeline services, the data-safety and write
rules D-MP-01..02 / W-MP-1..2, and AC-6, AC-9, AC-12, AC-13 (ledger) of marcus-pipeline-spec/1 section 7."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import config as config_mod
import sink_guard
from conformance.marcus_pipeline import gen_cc
from conformance.marcus_pipeline.util import (KINDS, OUTCOMES, SERVICES, build, graphs, kinds_logged, post_job, stored_bytes, watch)
from helpers import base_env, rid

C1 = "CC-MAIN-2026-40"


def _all_three(h):
    watch(h)
    b = build(h)
    g = graphs(h)
    return [h.svc.link_index_job_view(j) for j in h.svc.link_index_jobs] + [b, g]


# ---------------------------------------------------------------------------------------------- CB-1

def test_CB_1_registry_department_lifecycle_and_scorecards(mp):
    import svc_manager
    for s in SERVICES:
        assert s in svc_manager.AGENTS and s in svc_manager.REGISTRY and s not in svc_manager.NOT_BUILT_AGENTS
        e = svc_manager.REGISTRY[s]
        assert set(e) == {"assignment", "inputs", "outputs"} and svc_manager.NAMES[s]["name_pending"] is True
    for later in ("link_verifier", "spam_scorer", "rdap_enricher", "marcus"):
        assert later in svc_manager.NOT_BUILT_AGENTS and later not in svc_manager.AGENTS
    dept = mp.ok(mp.get("/department"))
    for s in SERVICES:
        card = dept["agents"][s]["scorecard"]
        assert card["runs"] == 0 and card["failure_rate"] is None and card["not_connected_rate"] is None
    _all_three(mp)
    dept = mp.ok(mp.get("/department"))
    assert dept["agents"]["cc_index_builder"]["scorecard"]["runs"] == 1
    assert dept["agents"]["cc_release_watcher"]["scorecard"]["runs"] == 1
    assert dept["agents"]["cc_wat_fetcher"]["scorecard"]["outcomes"] == {"OK": 1}
    for s in SERVICES:
        r = mp.post(f"/agents/{s}/lifecycle", {"request_id": rid(), "to": "watch", "expected_state": "active",
                                              "reason": "QUALITY_REVIEW"}, caller="dashboard")
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------------------------- CB-2

def _restrict(h, agent):
    r = h.post(f"/agents/{agent}/lifecycle", {"request_id": rid(), "to": "restricted", "expected_state": "active",
                                              "reason": "FOUNDER_DECISION"}, andre=True)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("agent,service", [("cc_release_watcher", "cc_release_watcher"),
                                           ("cc_wat_fetcher", "cc_index_builder"),
                                           ("cc_link_extractor", "cc_index_builder"),
                                           ("cc_index_builder", "cc_index_builder"),
                                           ("cc_graph_importer", "cc_graph_importer")])
def test_CB_2_restricted_service_does_not_run_and_its_routes_answer_403(mp, agent, service):
    watch(mp)
    if service == "cc_graph_importer":
        build(mp)
    _restrict(mp, agent)
    n = len(kinds_logged(mp))
    body = {"crawl_ids": [C1]} if service == "cc_index_builder" else (
        {"release_id": gen_cc.GRAPH, "kind": "host_graph"} if service == "cc_graph_importer" else {})
    r = post_job(mp, service, **body)
    assert r.status_code == 403 and r.json()["detail"] == "AGENT_RESTRICTED"
    assert len(kinds_logged(mp)) == n


def test_CB_2_restricted_mid_run_stops_the_build(mp):
    watch(mp)

    def hook(step):
        if step == "file_done:0":
            _restrict(mp, "cc_link_extractor")
    mp.svc._mp_hook = hook
    j = build(mp)
    assert j["outcome"] == "KILLED" and j["interrupted_code"] == "AGENT_RESTRICTED"
    assert mp.svc.complete_snapshots() == []


# ---------------------------------------------------------------------------------------------- CB-3 / CB-4 / CB-5

def _envelope_ok(v):
    assert v["outcome"] in OUTCOMES
    assert v["findings"] == [] and isinstance(v["facts"], dict)
    assert isinstance(v["methodology"], str) and v["methodology"]
    assert isinstance(v["limitations"], list) and isinstance(v["not_connected"], list)
    assert "decision" not in json.dumps(v)


def test_CB_3_CB_4_every_envelope_is_closed_and_decision_free(mp, make_mp):
    views = _all_three(mp)
    h = make_mp(fixture=False)
    views.append(watch(h))
    (mp.fx / "collinfo.json").write_text("[]x")
    views.append(watch(mp))
    for v in views:
        _envelope_ok(v)


SCORE = re.compile(r"score|grade|percent|rating", re.I)


def _keys(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _keys(v)
    elif isinstance(o, list):
        for v in o:
            yield from _keys(v)


def test_CB_5_no_score_grade_rating_or_percent_key_anywhere(mp):
    views = _all_three(mp)
    docs = list(views)
    sid = views[-2]["snapshot_id"]
    docs.append(mp.svc.index_store.read_manifest(sid))
    rec = mp.svc.graph_ranks[(gen_cc.GRAPH, "host_graph")]
    docs.append(mp.svc.index_store.read_graph(rec["name"], rec["sha256"]))
    docs += [r["data"] for r in mp.svc.log.iter_records() if r["kind"] in KINDS]
    assert [k for d in docs for k in _keys(d) if SCORE.search(str(k))] == []


# ---------------------------------------------------------------------------------------------- CB-6 / AC-9

SWITCHES = ("global", "write", "capability:link_index", "provider:commoncrawl")
STEPS = ("release_read", "file_open:0", "file_records:0", "stage_written:0", "file_done:0", "file_open:1",
         "shards_written", "manifest_built")


@pytest.mark.parametrize("switch", SWITCHES)
@pytest.mark.parametrize("step", STEPS)
def test_CB_6_AC_9_kill_switch_at_every_step(make_mp, monkeypatch, switch, step):
    from marcus import wat_fetcher
    h = make_mp(sub=f"{switch.replace(':', '_')}_{step.replace(':', '_')}")
    watch(h)
    reads = []
    real = wat_fetcher.FixtureWatSource.open
    monkeypatch.setattr(wat_fetcher.FixtureWatSource, "open", lambda self, p: reads.append(p) or real(self, p))
    state = {"at": None}

    def hook(s):
        if s == step and state["at"] is None:
            h.switch(switch, True, andre=True)
            state["at"] = len(reads)
    h.svc._mp_hook = hook
    j = build(h)
    assert state["at"] is not None
    assert j["outcome"] == "KILLED" and j["interrupted_code"].startswith("KILLED_"), j
    assert len(reads) == state["at"]                                     # no read after the engage
    assert h.svc.complete_snapshots() == [] and h.svc.index_store.names("manifest") == []
    assert h.svc.index_store.names("shard") == [] and h.svc.index_store.stage_names() == []   # result discarded
    assert "link_index_job_interrupted" in kinds_logged(h)


@pytest.mark.parametrize("switch", SWITCHES)
def test_CB_6_engaged_before_start_refuses(mp, switch):
    watch(mp)
    mp.switch(switch, True, andre=True)
    r = post_job(mp, "cc_index_builder", crawl_ids=[C1])
    assert r.status_code == 403 and r.json()["detail"].startswith("KILLED_")


# ---------------------------------------------------------------------------------------------- CB-7 / AC-13

def test_CB_7_record_first_the_port_sees_the_requested_record_committed(mp, monkeypatch):
    from marcus import release_watcher, wat_fetcher
    seen = []
    rr, ro = release_watcher.FixtureReleases.read, wat_fetcher.FixtureWatSource.open

    def spy(fn):
        def w(self, *a):
            k = kinds_logged(mp)
            seen.append(k.count("link_index_job_requested") > k.count("link_index_job_recorded")
                        + k.count("link_index_job_interrupted"))
            return fn(self, *a)
        return w
    monkeypatch.setattr(release_watcher.FixtureReleases, "read", spy(rr))
    monkeypatch.setattr(wat_fetcher.FixtureWatSource, "open", spy(ro))
    watch(mp)
    build(mp)
    assert seen and all(seen)
    led = [e["event_type"] for e in mp.ledger.events]
    assert "link_index_job_requested" in led and "link_index_job_recorded" in led


def test_CB_7_AC_13_ledger_refusing_writes_no_run(mp, monkeypatch):
    from marcus import wat_fetcher
    watch(mp)
    opened = []
    monkeypatch.setattr(wat_fetcher.FixtureWatSource, "open", lambda self, p: opened.append(p))
    mp.ledger.fail = True
    r = post_job(mp, "cc_index_builder", crawl_ids=[C1])
    assert r.status_code == 503 and r.json()["detail"] == "LEDGER_UNAVAILABLE"
    assert opened == []


def test_CB_7_crash_marks_the_job_failed_with_a_safe_location(mp):
    watch(mp)
    mp.svc._mp_hook = lambda s: (_ for _ in ()).throw(ValueError("boom")) if s == "file_done:0" else None
    j = build(mp)
    assert j["outcome"] == "FAILED" and j["interrupted_code"] == "RUN_FAILED" and j["facts"]["location"] == "file_done"
    assert "boom" not in json.dumps(j)


# ---------------------------------------------------------------------------------------------- CB-8

def test_CB_8_one_running_job_per_service_and_replays(mp):
    watch(mp)
    answers = []

    def hook(s):
        if s == "file_done:0" and not answers:
            answers.append(post_job(mp, "cc_index_builder", crawl_ids=[C1]))
            answers.append(post_job(mp, "cc_release_watcher"))
    mp.svc._mp_hook = hook
    body = {"request_id": rid()}
    j = build(mp, **body)
    assert answers[0].status_code == 409 and answers[0].json()["detail"] == "RUN_IN_PROGRESS"
    assert answers[1].status_code == 201                                  # another service runs
    mp.svc._mp_hook = None
    again = build(mp, **body)
    assert again["job_id"] == j["job_id"] and len([x for x in mp.svc.link_index_jobs.values()
                                                    if x["service"] == "cc_index_builder"]) == 1


# ---------------------------------------------------------------------------------------------- CB-9 / CB-10

@pytest.mark.parametrize("caller", ["dashboard", "scheduler", "seo_agent", "hub", "finance_31", "compliance_38"])
def test_CB_9_route_matrix(mp, caller):
    watch(mp)
    jid = next(iter(mp.svc.link_index_jobs))
    tenant = "acme" if caller == "hub" else None
    r = mp.client.post("/seo/v1/link-index/jobs", json={"request_id": rid(), "service": "cc_release_watcher"},
                       headers=mp.headers(caller, tenant=tenant))
    assert r.status_code == (201 if caller in ("dashboard", "scheduler") else 403), (caller, r.text)
    g = mp.client.get(f"/seo/v1/link-index/jobs/{jid}", headers=mp.headers(caller, tenant=tenant))
    assert g.status_code == (200 if caller in ("dashboard", "scheduler", "compliance_38") else 403), (caller, g.text)
    if g.status_code == 200:
        view = {k: v for k, v in g.json().items() if k not in ("methodology", "limitations")}   # closed sentences
        assert sink_guard.violations("link_index_job_recorded", view) == []             # machine facts only


def test_CB_10_no_tenant_scope_and_a_missing_job_answers_like_any_other(mp):
    watch(mp)
    r = mp.client.get("/seo/v1/link-index/jobs/seo-lij-" + "0" * 40, headers=mp.headers("dashboard"))
    q = mp.client.get("/seo/v1/link-index/jobs/seo-lij-" + "a" * 40, headers=mp.headers("dashboard"))
    assert r.status_code == q.status_code == 404 and r.content == q.content
    assert mp.client.get("/seo/v1/link-index/jobs/x", headers=mp.headers("dashboard")).status_code == 422
    t = mp.client.post("/seo/v1/link-index/jobs", json={"request_id": rid(), "service": "cc_release_watcher"},
                       headers={**mp.headers("dashboard"), "X-SEO-Tenant-Token": "t" * 40})
    assert t.status_code == 403


# ---------------------------------------------------------------------------------------------- CB-11 / AC-12

@pytest.mark.parametrize("name", ["SEO_CC_RELEASES_ENABLED", "SEO_CC_WAT_ENABLED", "SEO_LINK_VERIFIER_ENABLED",
                                  "SEO_SPAM_SCORER_ENABLED", "SEO_RDAP_ENABLED"])
def test_CB_11_AC_12_live_and_v2_adapters_refuse_start(name):
    assert name in config_mod.NOT_BUILT
    for v in ("1", "true", "yes"):
        with pytest.raises(RuntimeError) as e:
            config_mod.load(base_env(**{name: v}))
        assert v not in str(e.value).split(":")[-1] or name in str(e.value)


def test_CB_11_ports_default_not_connected(make_mp):
    h = make_mp(fixture=False)
    st = h.svc.ports.status()
    assert st["cc_releases"] == "NOT_CONNECTED" and st["cc_wat"] == "NOT_CONNECTED"
    h2 = make_mp(sub="with")
    st2 = h2.svc.ports.status()
    assert st2["cc_releases"] == "connected" and st2["cc_wat"] == "connected"


# ---------------------------------------------------------------------------------------------- CB-12 / CB-13

def test_CB_12_zero_spend_no_metered_port(mp):
    before = (dict(mp.svc.engine_runs), [p.__class__.__name__ for p in mp.svc.ports.engines.values()])
    _all_three(mp)
    assert dict(mp.svc.engine_runs) == before[0]
    assert not any(e["event_type"].startswith(("engine_", "reservation")) for e in mp.ledger.events)


def test_CB_13_network_audit_hook_is_active():
    import socket
    from conformance.marcus_pipeline import conftest as c
    n = len(c.ESCAPES)
    try:
        socket.getaddrinfo("example.invalid", 443)
    except Exception:  # noqa: BLE001 - the suite's socket guard may refuse it as well
        pass
    assert len(c.ESCAPES) == n + 1
    del c.ESCAPES[n:]


# ---------------------------------------------------------------------------------------------- CB-14 / W-MP-1 / W-MP-2

def test_CB_14_W_MP_1_every_job_record_through_commit_and_the_sink_guard(mp, monkeypatch):
    # Observed at the CALL SITES (service.py, ledger.py hold the module reference): the real guard runs unchanged and
    # no guard entry point is replaced (tests/test_sink_guard.py EXPECTED_GUARD_PATCHES stays as it is).
    import ledger
    import service
    seen = []

    class _Spy:
        def __getattr__(self, name):
            return getattr(sink_guard, name)

        def enforce(self, kind, data):
            seen.append(kind)
            return sink_guard.enforce(kind, data)
    monkeypatch.setattr(service, "sink_guard", _Spy())
    monkeypatch.setattr(ledger, "sink_guard", _Spy())
    _all_three(mp)
    logged = [k for k in kinds_logged(mp) if k in KINDS]
    assert logged and all(k in seen for k in logged)
    assert all(sink_guard.LEDGER + e["event_type"] in seen for e in mp.ledger.events
               if e["event_type"] in KINDS)


def test_W_MP_2_index_writes_only_through_the_encrypted_store(mpd):
    _all_three(mpd)
    data = Path(mpd.settings.data_dir)
    files = sorted(str(p.relative_to(data)) for p in data.rglob("*") if p.is_file())
    other = [f for f in files if not f.startswith("marcus_index/") and not f.startswith(("seo-", "citation_vault",
                                                                                        "operator_text_vault"))]
    assert all(not f.endswith((".json", ".jsonl.tmp", ".rows")) for f in other), other
    for f in files:
        if f.startswith("marcus_index/"):
            blob = (data / f).read_bytes()
            assert blob[:4] == b"MIX1" and b"acme.test" not in blob and b"https://" not in blob, f


# ---------------------------------------------------------------------------------------------- CB-15..CB-21

def test_CB_15_outside_values_never_stored_outside_the_encrypted_index(mp):
    files, truth = gen_cc.corpus(15, 2, 20)
    gen_cc.write_crawl(mp.fx, C1, files)
    watch(mp)
    build(mp)
    blob = stored_bytes(mp)
    srcs = {r["src"].split("/")[2] for r in truth}
    assert not any(s.encode() in blob for s in srcs)                        # referring hosts: in the index only
    anchors = {r["anchor"] for r in truth if len(r["anchor"]) > 8}
    assert not any(a.encode() in blob for a in anchors)


@pytest.mark.xfail(strict=True, reason="CL-0: the code kinds `anchor` and `url` (CB-16) are added to primitives/"
                                       "codes.py's closed list, which is not on the base (186471eb)")
def test_CB_16_new_code_kinds_anchor_and_url_BLOCKED_until_codes():
    from primitives import codes  # noqa: F401
    assert {"anchor", "url"} <= set(codes.KINDS)


def test_CB_17_python_logging_takes_machine_values_only(mp, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    cases = gen_cc.hostile_cases(4, 16)
    for i, (_n, data, _w) in enumerate(cases):
        p = mp.fx / gen_cc.wat_path(C1, 50 + i)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    watch(mp)
    build(mp, paths=[gen_cc.wat_path(C1, 50 + i) for i in range(len(cases))])
    for rec in caplog.records:
        if rec.name.startswith("seo"):
            for a in (rec.args or ()):
                assert isinstance(a, (int, float)) or sink_guard.violations("x", {"a": a}) == [], rec.name


def test_CB_18_error_bodies_echo_nothing(mp):
    planted = "zq" + "Xk9" * 8
    for body in ({"service": planted}, {"service": "cc_index_builder", "crawl_ids": [planted]},
                 {"service": "cc_index_builder", "crawl_ids": [C1], "paths": [planted]},
                 {"service": "cc_graph_importer", "release_id": planted, "kind": "host_graph"},
                 {"service": "cc_index_builder", "crawl_ids": [C1], "resume": planted}, {planted: 1}):
        r = post_job(mp, **{"service": "cc_release_watcher", **body})
        assert r.status_code in (404, 409, 422), r.text
        assert planted not in r.text


def test_CB_19_produced_contracts_validate_and_forgeries_are_refused(mp):
    from marcus import graph_importer, index_builder
    views = _all_three(mp)
    man = mp.svc.index_store.read_manifest(views[-2]["snapshot_id"])
    index_builder.validate_manifest(man)
    rec = mp.svc.graph_ranks[(gen_cc.GRAPH, "host_graph")]
    graph_importer.validate_graph(mp.svc.index_store.read_graph(rec["name"], rec["sha256"]))
    forged = [{**man, "contract": "marcus.index_snapshot/2"}, {**man, "extra": 1},
              {k: v for k, v in man.items() if k != "shards"}, {**man, "counts": {**man["counts"], "rows": 10 ** 12}},
              {**man, "counts": {**man["counts"], "rows": -1}}, {**man, "crawls": ["CC-MAIN-2026-40\u200b"]},
              {**man, "complete": "true"}, {**man, "shards": [{"shard_id": 1, "sha256": "x"}]},
              {**man, "built_at": float("nan")}, {**man, "snapshot_id": "seo-snp-" + "\u0430" * 40}]
    for f in forged:
        with pytest.raises(index_builder.ContractInvalid):
            index_builder.validate_manifest(f)


def test_CB_20_CB_21_job_views_carry_tier_time_and_a_resolving_evidence_pointer(mp):
    for v in _all_three(mp):
        assert v["refresh_tier"] == "monthly" and v["produced_at"]
        ev = v["evidence_ref"]
        assert set(ev) == {"run_id", "contract", "item_key"} and ev["contract"] == "link_index.job/1"
        got = mp.ok(mp.get(f"/link-index/jobs/{ev['run_id']}"))
        assert got["job_id"] == ev["run_id"] and got["service"] == ev["item_key"]


# ---------------------------------------------------------------------------------------------- AC-6

def test_AC_6_oversized_inputs_refused_with_a_closed_reason(mp):
    from marcus import wat_fetcher
    watch(mp)
    cap = wat_fetcher.MAX_TARGETED_PATHS
    assert cap == 1_000
    paths = [gen_cc.wat_path(C1, i % 99999, seg=i // 99999) for i in range(cap + 1)]
    r = post_job(mp, "cc_index_builder", crawl_ids=[C1], paths=paths)
    assert r.status_code in (413, 422)
    r = post_job(mp, "cc_index_builder", crawl_ids=[C1] * 5)
    assert r.status_code == 422
    r = post_job(mp, "cc_index_builder", crawl_ids=["C" * 1600])
    assert r.status_code == 422
    pf = mp.fx / "crawl-data" / C1 / "wat.paths"
    pf.write_text("".join(gen_cc.wat_path(C1, i % 99999, seg=i // 99999) + "\n"
                          for i in range(wat_fetcher.MAX_PATHS + 1)))
    j = build(mp)
    assert j["outcome"] == "BLOCKED" and j["facts"]["reason"] == "PATH_LIST_TOO_LARGE"


# ---------------------------------------------------------------------------------------------- D-MP-01 / D-MP-02

def test_D_MP_01_the_index_is_never_in_the_log_ledger_or_any_view(mp):
    files, truth = gen_cc.corpus(101, 2, 20)
    gen_cc.write_crawl(mp.fx, C1, files)
    views = _all_three(mp)
    blob = stored_bytes(mp) + json.dumps(views).encode() + mp.get("/department").content
    for r in truth:
        host = r["src"].split("/")[2].encode()
        assert host not in blob
        path = "/".join(r["dst"].split("/")[3:]).split("?")[0]
        if len(path) > 12:
            assert path.encode() not in blob
    for rec in mp.svc.log.iter_records():
        assert sink_guard.violations(rec["kind"], rec["data"], stored=True) == []


def test_D_MP_02_no_committed_fixture_holds_cc_data():
    here = Path(__file__).resolve().parent
    assert sorted(p.suffix for p in here.iterdir() if p.is_file()) == [".py"] * len(
        [p for p in here.iterdir() if p.is_file()])
    data = here.parent / "data"
    assert not data.exists() or not any("wat" in p.name or "ranks" in p.name for p in data.iterdir())
