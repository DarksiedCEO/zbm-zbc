"""Bug sweep D (Oct 6 2026 sweep at integration 5d49ee9) -- delivery-py (department 28). Each test pins one finding and
FAILS on 5d49ee9 (the sweep's probes were lost; these are re-derived from the code):

  H        ``git commit`` ran before anything recorded the commit (only a generic git crossing); a failure after it
           left a commit no record named, invisible until (at best) a restart. Now record-first: ``commit_attempted``
           (parent, message hash, staged files) BEFORE the commit, its outcome after; orphans surfaced continuously
           (GET /dlv/v1/commits/orphans, /health) and resolved at restart
  E-5/F-3  the old store: one fsync error left a line on disk memory did not hold, and the log refused every later
           append and every restart; no single-writer guard
"""

from __future__ import annotations

import os

import pytest

from helpers import Harness


def _types(h: Harness, run_id: str) -> list[str]:
    return [e["event_type"] for e in h.ledger.events
            if e["subject_id"] == run_id or e["subject_id"].startswith(run_id + ":")]


def test_h_each_commit_is_recorded_before_git_commit_runs_and_its_outcome_after():
    h = Harness()
    try:
        run_id = h.submit().json()["run_id"]
        assert h.run(run_id)["status"] == "awaiting_review"
        evs = h.ledger.events
        commits = [i for i, e in enumerate(evs) if e["event_type"] == "crossing_git_requested"
                   and e["payload"].get("op") == "commit"]
        attempts = [i for i, e in enumerate(evs) if e["event_type"] == "commit_attempted"]
        recorded = [i for i, e in enumerate(evs) if e["event_type"] == "commit_recorded"]
        assert len(commits) == len(attempts) == len(recorded) == 2, (commits, attempts, recorded)
        for a, c, r in zip(attempts, commits, recorded, strict=True):
            assert a < c < r                                   # attempt recorded, then git commit, then the outcome
            assert evs[a]["payload"]["parent_sha"] and evs[a]["payload"]["message_sha256"]
        assert h.svc.runs[run_id].get("pending_commit") is None
        assert h.get("/dlv/v1/commits/orphans").json()["count"] == 0
    finally:
        h.close()


def _fail_after_commit(h: Harness, monkeypatch, *, record_outcome: bool = True):
    """The step right after ``git commit`` (the diff evidence) fails once: the commit exists, nothing names it."""
    real = h.svc.evidence_put
    hit = {"n": 0}

    def flaky(run_id, kind, content):
        if kind == "diff" and hit["n"] == 0 and (h.svc.runs.get(run_id) or {}).get("pending_commit"):
            hit["n"] += 1
            raise OSError(28, "simulated ENOSPC after the commit")
        return real(run_id, kind, content)
    monkeypatch.setattr(h.svc, "evidence_put", flaky)
    if not record_outcome:
        monkeypatch.setattr(h.svc, "commit_outcome", lambda *a, **k: None)
    return hit


def test_h_an_orphaned_commit_is_visible_at_once_not_only_after_a_restart(monkeypatch):
    h = Harness()
    try:
        hit = _fail_after_commit(h, monkeypatch)
        run_id = h.submit().json()["run_id"]
        assert hit["n"] == 1
        assert h.run(run_id)["status"] != "awaiting_review"
        o = h.get("/dlv/v1/commits/orphans").json()
        assert o["count"] == 1 and o["orphans"][0]["status"] == "orphaned" and o["orphans"][0]["sha"], o
        assert h.get("/health").json()["orphan_commits"] == 1
        outcome = [e for e in h.ledger.events if e["event_type"] == "commit_attempt_outcome"]
        assert len(outcome) == 1 and outcome[0]["payload"]["outcome"] == "orphaned"
        assert "commit_attempted" in _types(h, run_id)
    finally:
        h.close()


def test_h_an_unresolved_attempt_is_an_orphan_until_the_restart_resolves_it(monkeypatch):
    h = Harness()
    try:
        _fail_after_commit(h, monkeypatch, record_outcome=False)       # its outcome could not be recorded either
        run_id = h.submit().json()["run_id"]
        o = h.get("/dlv/v1/commits/orphans").json()
        assert o["count"] == 1 and o["orphans"][0]["status"] == "attempt_unresolved", o
        h2 = Harness(tmp=h.tmp, ledger=h.ledger, wire_harness=False)    # restart on the same data directory
        o2 = h2.get("/dlv/v1/commits/orphans").json()
        assert o2["count"] == 1 and o2["orphans"][0]["status"] == "orphaned" and o2["orphans"][0]["sha"], o2
        assert h2.svc.runs[run_id].get("pending_commit") is None
        out = [e for e in h2.ledger.events if e["event_type"] == "commit_attempt_outcome"]
        assert len(out) == 1 and out[0]["payload"]["outcome"] == "orphaned"
    finally:
        h.close()


# ============================================================================================ E-5 / F-3 store


def test_store_one_fsync_error_never_bricks_the_log(monkeypatch):
    import zbm_delivery.store as store_mod
    h = Harness(wire_harness=False)
    try:
        real = os.fsync
        hit = {"n": 0}

        def flaky(fd):
            hit["n"] += 1
            if hit["n"] == 1:
                raise OSError(5, "simulated EIO on fsync")
            return real(fd)
        monkeypatch.setattr(store_mod.os, "fsync", flaky)
        from zbm_delivery.errors import Unavailable
        with pytest.raises(Unavailable):
            h.svc._record_plain("dlv-test-sweepd-1", "fix_run_refused", "intel_01_gate", "probe",
                                {"request_id": "x"}, "probe")
        monkeypatch.setattr(store_mod.os, "fsync", real)
        n = len(h.svc.log)
        h.svc._record_plain("dlv-test-sweepd-2", "fix_run_refused", "intel_01_gate", "probe",
                            {"request_id": "y"}, "probe")
        assert len(h.svc.log) == n + 1 and h.svc.log.verify()
        with open(h.svc.log.path, "rb") as fh:
            assert fh.read().count(b"\n") == len(h.svc.log)
        # the file is a whole, verified chain (the old store left the failed line on disk: memory and file disagreed and
        # every later append and every restart refused). A restart then reports the failed line's anchor for Andre's
        # reconcile, as before (evidence_audit), instead of refusing on a torn or extra line.
        from zbm_delivery.store import RecordLog
        assert len(RecordLog(os.path.dirname(h.svc.log.path)).records) == len(h.svc.log)
    finally:
        h.close()


def test_store_a_second_live_instance_on_one_data_directory_is_refused():
    from zbm_delivery.api import build_service
    from zbm_delivery.store import DataDirBusy
    h = Harness(wire_harness=False)
    try:
        with pytest.raises(DataDirBusy):
            build_service(h.settings, h.env, clock=h.clock, ledger=h.ledger, docker=h.docker, git=h.git,
                          gate_report=h.svc.gate, wire_harness=False)
    finally:
        h.close()
