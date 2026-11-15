"""marcus-pipeline-spec/1 G (CB-23 / AC-14): killing tests for the generated mutants of the new service module
src/svc_link_index.py (review MR-2 finding F1, card MP-C5). Each test names the guard it pins. Synthetic values only."""

from __future__ import annotations

import json

import pytest

from conformance.marcus_pipeline.util import all_rows, build, graphs, job, watch
from errors import Conflict, Forbidden, Invalid

CRAWL = "CC-MAIN-2026-40"


def _ok_build(h) -> dict:
    v = build(h)
    assert v["outcome"] in ("OK", "PARTIAL"), v
    return v


def test_G_AC_14_CB_9_build_view_crawls_methodology_scorecard_and_rel_unknown(mp):
    """The job view keeps the requested crawl ids and the stated methodology; the scorecard counts finished jobs."""
    w = watch(mp)
    assert w["outcome"] == "OK" and w["crawl_ids"] == []
    v = _ok_build(mp)
    assert v["crawl_ids"] == [CRAWL]
    assert v["resume_of"] is None and v["snapshot_id"]
    assert "gzip-streamed and verified by the WAT Fetcher" in v["methodology"]
    assert mp.svc.link_index_scorecard("cc_release_watcher") == {"runs": 1, "outcomes": {"OK": 1}}
    assert mp.svc.link_index_scorecard("cc_wat_fetcher")["runs"] == 1
    sid = v["snapshot_id"]
    assert [s["snapshot_id"] for s in mp.svc.complete_snapshots()] == [sid]
    rows = all_rows(mp, sid)
    assert rows
    hosts = {r["dst"].split("/")[2] for r in rows}
    assert all(h.endswith(("acme.test", "globex.test")) for h in hosts), hosts
    rels = {r.get("rel") for r in rows}
    assert rels == {"UNKNOWN"}, rels                                       # B-MP-24: no spike recorded -> UNKNOWN


def test_B_MP_40_a_graph_import_needs_a_complete_snapshot_and_records_its_ranks(mp):
    watch(mp)
    g0 = graphs(mp)
    assert g0["outcome"] == "INSUFFICIENT_EVIDENCE", g0
    assert mp.svc.graph_ranks == {}
    _ok_build(mp)
    g = graphs(mp)
    assert g["outcome"] in ("OK", "PARTIAL"), g
    assert len(mp.svc.graph_ranks) == 1
    (key, rec), = mp.svc.graph_ranks.items()
    assert key == (g["release_id"], g["kind"]) and rec["snapshot_id"] == g["facts"]["snapshot_id"]


def test_G_AC_14_a_retired_snapshot_record_marks_it_retired(mp):
    mp.svc.index_snapshots["snp_synthetic"] = {"snapshot_id": "snp_synthetic", "status": "published", "order": 0}
    mp.svc._a_index_snapshot_retired({"snapshot_id": "snp_synthetic"}, "2026-01-01T00:00:00Z")
    assert mp.svc.index_snapshots["snp_synthetic"]["status"] == "retired"
    mp.svc._a_index_snapshot_retired({"snapshot_id": "snp_absent"}, "2026-01-01T00:00:00Z")   # unknown: no-op


def test_G_AC_14_bad_requests_are_refused_not_returned(mp):
    watch(mp)
    with pytest.raises(Invalid):
        mp.svc.link_index_start("dashboard", {"request_id": "r-synthetic-1", "service": "cc_bogus"})
    with pytest.raises(Invalid):
        mp.svc._li_request("cc_graph_importer", {"release_id": "x", "kind": "bogus_graph"}, "lij_synthetic")
    with pytest.raises(Invalid):
        mp.svc._li_request("cc_index_builder", {"crawl_ids": [CRAWL], "paths": ["not/a/wat/path"]}, "lij_s2")
    with pytest.raises(Conflict):
        mp.svc._li_request("cc_index_builder", {"crawl_ids": ["CC-MAIN-2001-01"]}, "lij_s3")
    data, paths = mp.svc._li_request("cc_index_builder", {"crawl_ids": [CRAWL]}, "lij_s4")
    assert paths is None and data["crawl_ids"] == [CRAWL] and data["paths_count"] is None
    assert data["resume_of"] is None and data["snapshot_id"]


def _fake_job(mp, jid: str, code: str, sid: str) -> None:
    mp.svc.link_index_jobs[jid] = {"job_id": jid, "service": "cc_index_builder", "status": "interrupted",
                                   "outcome": "FAILED", "facts": {}, "not_connected": [], "crawl_ids": [CRAWL],
                                   "snapshot_id": sid, "release_id": None, "kind": None, "resume_of": None,
                                   "requested_at": None, "produced_at": None, "interrupted_code": code}


def test_B_MP_13_only_a_crashed_unpublished_build_is_resumable(mp):
    _fake_job(mp, "lij_killed", "KILLED_GLOBAL", "snp_killed")
    with pytest.raises(Conflict):
        mp.svc._li_request("cc_index_builder", {"resume": "lij_killed"}, "lij_r1")
    _fake_job(mp, "lij_died", "PROCESS_DIED", "snp_died")
    data, paths = mp.svc._li_request("cc_index_builder", {"resume": "lij_died"}, "lij_r2")
    assert data["resume_of"] == "lij_died" and data["snapshot_id"] == "snp_died" and paths is None


def test_B_MP_24_a_spike_record_is_andre_only(mp):
    with pytest.raises(Forbidden):
        mp.svc.record_cc_spike({"rel_capture": "VERIFIED", "approval_ref": "0" * 64}, andre=False)
    assert mp.svc.cc_spike is None


def test_AC_10_without_a_keyring_the_watcher_runs_and_a_build_is_refused(mp, monkeypatch):
    monkeypatch.setattr(mp.svc.index_store, "keyring", None)
    assert watch(mp)["outcome"] == "OK"
    job(mp, "cc_index_builder", code=503, crawl_ids=[CRAWL])


def test_G_AC_14_a_discarded_build_never_deletes_a_published_snapshot(mp):
    watch(mp)
    sid = _ok_build(mp)["snapshot_id"]
    before = all_rows(mp, sid)
    ctx = {"service": "cc_index_builder", "job_id": "lij_synthetic", "step": ""}
    from ledger import derived_id
    mp.svc._li_discard(ctx, {"snapshot_id": derived_id("snp", "lij_synthetic")}, keep_stage=False)
    assert all_rows(mp, sid) == before
    assert [s["snapshot_id"] for s in mp.svc.complete_snapshots()] == [sid]


def test_G_AC_14_the_start_sweep_keeps_a_published_snapshot(mpd):
    watch(mpd)
    sid = _ok_build(mpd)["snapshot_id"]
    before = all_rows(mpd, sid)
    mpd.svc.link_index_sweep()
    assert [s["snapshot_id"] for s in mpd.svc.complete_snapshots()] == [sid]
    assert all_rows(mpd, sid) == before


def test_G_AC_14_a_release_seen_evidence_names_the_release_not_the_job(mp):
    watch(mp)
    pay = [e.get("_payload") for e in mp.ledger.events if isinstance(e.get("_payload"), dict)]
    seen = [p for p in pay if "published" in p and "release_id" in p]
    assert seen, [sorted(p)[:6] for p in pay][:6]
    assert any("job_id" not in p for p in seen), json.dumps(seen[0], sort_keys=True)[:200]


def _keep(mp, monkeypatch, n: int) -> None:
    import dataclasses
    try:
        monkeypatch.setattr(mp.svc.settings, "marcus_snapshots_kept", n)
    except (AttributeError, dataclasses.FrozenInstanceError):
        monkeypatch.setattr(mp.svc, "settings", dataclasses.replace(mp.svc.settings, marcus_snapshots_kept=n))


@pytest.mark.parametrize("pin, retired", [("same", True), ("later", False)])
def test_B_MP_34_a_report_pins_a_snapshot_only_while_its_retention_has_not_ended(mp, monkeypatch, pin, retired):
    """Q-MP-3: kept while ``retain_until > now``; a retention ending exactly now no longer pins (frozen clock)."""
    from clock import iso
    watch(mp)
    old = _ok_build(mp)["snapshot_id"]
    new = _ok_build(mp)["snapshot_id"]
    assert old != new
    t = mp.svc.now()
    monkeypatch.setattr(mp.svc, "now", lambda: t)
    until = iso(t) if pin == "same" else "9999-12-31T23:59:59Z"
    mp.svc.backlink_reports["rpt_synthetic"] = {"snapshot_id": old, "retain_until": until}
    _keep(mp, monkeypatch, 1)
    mp.svc._li_retention()
    assert (mp.svc.index_snapshots[old]["status"] == "retired") is retired
    assert mp.svc.index_snapshots[new]["status"] == "published"
