"""Bug sweep C (Oct 6 2026, integration 5d49ee9) — compliance-py (department 38). Each test pins one finding and FAILS
on 5d49ee9 (the sweep's probes were lost; these are re-derived from the code):

  R6   evidence ids leave out the payload hash or carry a timestamp: a retry after a state change is a lasting 409/503
  E-5/F-3  the old store: one fsync error bricks the log; no single-writer lock; no inert close()
  M    the Change Watcher fetched every source (network) under the service lock; C-11's ledger verify / entries and
       the chain re-read ran under it too
"""

from __future__ import annotations

import os
import threading

import pytest

import config as config_mod
from helpers import ANDRE_TOKEN, Harness, base_env, creator_facts, rid
from ledger import LedgerNotRecorded


def _fail_once(h, pred, kind=None):
    """The ledger refuses the first event matching ``pred`` (and, for an anchor, whose line is of ``kind``)."""
    real = h.ledger.record_event
    state = {"n": 0}

    def flaky(event_id, department, event_type, actor, subject_id, payload, summary):
        if state["n"] == 0 and pred(event_type) and (kind is None or payload.get("kind") == kind):
            state["n"] += 1
            raise LedgerNotRecorded("simulated outage for one write")
        return real(event_id, department, event_type, actor, subject_id, payload, summary)
    h.ledger.record_event = flaky
    return state


def _lock_free(svc) -> bool:
    got: list = []

    def probe():
        ok = svc.lock.acquire(blocking=False)
        got.append(ok)
        if ok:
            svc.lock.release()
    t = threading.Thread(target=probe)
    t.start()
    t.join(5)
    return bool(got and got[0])


# ============================================================================================ R6-M1 evidence

def test_r6_internal_controls_retried_after_the_clock_moved_is_not_a_lasting_409(hs):
    """``control_result_recorded`` carried ``tested_at`` (the clock) in a payload whose id had no payload hash: the
    same scheduler request retried a minute later was a 409 on every retry."""
    st = _fail_once(hs, lambda t: t == "local_log_appended", kind="control_result")
    fixed = rid("ctl")
    r = hs.post("/compliance/v1/controls/internal/run", {"request_id": fixed}, caller="scheduler")
    assert r.status_code == 503 and st["n"] == 1
    hs.clock.advance(minutes=1)
    r = hs.post("/compliance/v1/controls/internal/run", {"request_id": fixed}, caller="scheduler")
    assert r.status_code == 200, r.text
    ev = hs.get("/compliance/v1/audit/evidence", event_type="control_result_recorded").json()
    assert ev["counts"]["committed"] >= 1 and ev["counts"]["attempted"] >= 1
    for e in hs.ledger.of_type("control_result_recorded"):
        assert "rk" in e["payload"] and "seq" in e["payload"]


def test_r6_a_hold_release_retried_with_a_corrected_reason_is_not_a_lasting_409(hs):
    s = hs.screen("clipper-1")
    r = hs.rule("clipper-1", "zbc_creator", creator_facts(s["screen_id"], network_country_signal="RU")).json()
    hid = [u for u in r["unmet"] if u["code"].startswith("hold_open:")][0]["code"].split(":", 1)[1]
    _fail_once(hs, lambda t: t == "local_log_appended")
    assert hs.post(f"/compliance/v1/holds/{hid}/release", {"request_id": rid(), "reason": "VPN"},
                   andre=ANDRE_TOKEN).status_code == 503
    r = hs.post(f"/compliance/v1/holds/{hid}/release", {"request_id": rid(), "reason": "VPN; verified by Andre"},
                andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text


def test_r6_restart_after_a_retried_commit_needs_no_reconcile(tmp_path):
    x = Harness(data_dir=str(tmp_path / "d"))
    x.approve_seed()
    _fail_once(x, lambda t: t == "local_log_appended", kind="control_result")
    fixed = rid("ctl")
    x.post("/compliance/v1/controls/internal/run", {"request_id": fixed}, caller="scheduler")
    x.clock.advance(minutes=1)
    assert x.post("/compliance/v1/controls/internal/run", {"request_id": fixed},
                  caller="scheduler").status_code == 200
    y = Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock, ports=x.ports)
    assert y.get("/health").json()["reconcile_required"] is False


# ============================================================================================ E-5 / F-3 store

def test_store_one_fsync_error_never_bricks_the_log(tmp_path, monkeypatch):
    import store as store_mod
    d = str(tmp_path / "d")
    h = Harness(data_dir=d)
    h.approve_seed()
    real = os.fsync
    hit = {"n": 0}

    def flaky(fd):
        hit["n"] += 1
        if hit["n"] == 1:
            raise OSError(5, "simulated EIO on fsync")
        return real(fd)
    monkeypatch.setattr(store_mod.os, "fsync", flaky)
    r = h.post("/compliance/v1/sanctions/screen", {"request_id": rid(), "subject_id": "s-1", "role": "payee",
                                                   "owner_of": None, "legal_name": "N", "aliases": [],
                                                   "dob": "1990-01-01", "country": "US", "region": "US-CA"},
               caller="onboarding")
    assert r.status_code == 503 and hit["n"] >= 1
    monkeypatch.setattr(store_mod.os, "fsync", real)
    h.screen("s-2")
    assert h.svc.log.verify()
    with open(os.path.join(d, store_mod.LOG_NAME), "rb") as fh:
        assert fh.read().count(b"\n") == len(h.svc.log)


def test_store_a_short_write_is_cut_back_and_refused(tmp_path, monkeypatch):
    import store as store_mod
    h = Harness(data_dir=str(tmp_path / "d"))
    h.approve_seed()
    real = os.pwrite
    monkeypatch.setattr(store_mod.os, "pwrite", lambda fd, data, off: real(fd, data[: len(data) // 2], off))
    r = h.post("/compliance/v1/sanctions/screen", {"request_id": rid(), "subject_id": "s-3", "role": "payee",
                                                   "owner_of": None, "legal_name": "N", "aliases": [],
                                                   "dob": "1990-01-01", "country": "US", "region": "US-CA"},
               caller="onboarding")
    assert r.status_code == 503
    monkeypatch.setattr(store_mod.os, "pwrite", real)
    h.screen("s-4")
    assert h.svc.log.verify() and h.svc.log.fault is None


def test_single_writer_and_an_inert_closed_instance(tmp_path):
    import api
    d = str(tmp_path / "d")
    h = Harness(data_dir=d)
    h.approve_seed()
    with pytest.raises(Exception) as ei:
        api.build_service(config_mod.load({**base_env(), "COMPLIANCE_DATA_DIR": d}), h.clock, h.ports, h.ledger)
    assert "data-directory claim" in str(ei.value) or "already holds this data directory" in str(ei.value)
    h.svc.close()
    assert h.svc.closed
    r = h.post("/compliance/v1/sanctions/screen", {"request_id": rid(), "subject_id": "s-5", "role": "payee",
                                                   "owner_of": None, "legal_name": "N", "aliases": [],
                                                   "dob": "1990-01-01", "country": "US", "region": "US-CA"},
               caller="onboarding")
    assert r.status_code == 503
    h2 = Harness(data_dir=d, ledger=h.ledger, clock=h.clock, ports=h.ports)
    assert h2.svc.version_number == h.svc.version_number


# ============================================================================================ mediums: I/O off the lock

def test_m_the_change_watcher_fetches_with_the_service_lock_free(hs):
    hs.svc.config.watcher_enabled = True
    seen = []
    real = hs.ports.fetcher.fetch

    def fetch(url):
        seen.append(_lock_free(hs.svc))
        return real(url)
    hs.ports.fetcher.fetch = fetch
    r = hs.post("/compliance/v1/watcher/run", {"request_id": rid("w")}, caller="scheduler")
    assert r.status_code == 200, r.text
    assert seen and all(seen)


def test_m_internal_controls_read_the_ledger_with_the_service_lock_free(hs):
    seen = []
    real_verify, real_entries = hs.ledger.verify, hs.ledger.entries

    def verify():
        seen.append(_lock_free(hs.svc))
        return real_verify()

    def entries():
        seen.append(_lock_free(hs.svc))
        return real_entries()
    hs.ledger.verify, hs.ledger.entries = verify, entries
    out = hs.run_controls()
    assert out["results"]["C-11"]["result"] == "pass", out["results"]["C-11"]
    assert seen and all(seen)
