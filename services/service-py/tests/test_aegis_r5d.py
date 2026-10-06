"""AEGIS review of a5dd261 (Oct 6 2026): M1 a closed instance ran integrity and re-recorded its own pending line on
the ledger; L2 the audit integrity route answered from a closed instance; L3 claim / holds / release_claim were not
atomic; L4 a claim token could be adopted twice; I5 constructing a body store swept bodies/*.tmp before the claim
was verified; I7 /health answered 200 for a closed instance. (I6 is fixed in tests/test_aegis_r5c.py.)"""

from __future__ import annotations

import threading

import pytest

import config as config_mod
import store as store_mod
from helpers import FakeLedger, Harness, base_env
from ledger import LedgerRecordError


def _env(d):
    return base_env(SVC_NON_PRODUCTION=None, SVC_DATA_DIR=str(d))


class Lossy(FakeLedger):
    """The anchor never lands and the answer is lost: the instance keeps its own pending line, unconfirmed."""
    lose = 0

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary):
        if self.lose and event_type == "log_anchor":
            self.lose -= 1
            raise LedgerRecordError("lost")
        super().record_event(event_id, department, event_type, actor, subject_id, payload, summary)


# --------------------------------------------------------------------------------------------------- M1 / L2

def _closed_h1_with_own_pending_and_live_h2(tmp_path):
    led = Lossy()
    h1 = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=led)
    h1.ok(h1.chat("first"), 201)                                   # h1 commits
    led.lose = 2
    assert h1.chat("second").status_code == 503                    # h1 is left with an unconfirmed own pending line
    assert h1.svc._own_pending is not None
    h2 = h1.restart()                                              # h1 is closed; h2 owns the data directory
    h2.ok(h2.chat("third"), 201)                                   # h2 commits
    assert h2.svc.integrity["ok"] is True
    return led, h1, h2


def test_m1_a_closed_instances_integrity_job_is_refused_and_records_nothing(tmp_path):
    led, h1, h2 = _closed_h1_with_own_pending_and_live_h2(tmp_path)
    before = len(led.events)
    r = h1.job("integrity")                                        # POST /svc/v1/jobs/integrity/run on h1's app
    assert r.status_code == 503 and r.json()["detail"] == "SERVICE_CLOSED"
    assert len(led.events) == before                               # zero new ledger events from the closed instance
    h2.svc._last_integrity_try = 0
    assert h2.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert h2.ok(h2.job("integrity"))["integrity"]["ok"] is True


def test_m1_verify_integrity_on_a_closed_instance_does_no_ledger_io(tmp_path):
    led, h1, h2 = _closed_h1_with_own_pending_and_live_h2(tmp_path)
    reads = []
    orig = led.entries
    led.entries = lambda: (reads.append(1), orig())[1]
    before = len(led.events)
    res = h1.svc.verify_integrity(force=True, always=True)
    assert res["ok"] is False and res["problem"] == "this service instance is closed"
    assert reads == [] and len(led.events) == before
    h2.svc._last_integrity_try = 0
    assert h2.svc.verify_integrity(force=True, always=True)["ok"] is True


def test_l2_the_audit_integrity_route_refuses_after_close(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"))
    h.ok(h.chat("help"), 201)
    assert h.ok(h.get("/svc/v1/audit/integrity", caller="compliance_38"))["integrity"]["ok"] is True
    h.svc.close()
    r = h.get("/svc/v1/audit/integrity", caller="compliance_38")
    assert r.status_code == 503 and r.json()["detail"] == "SERVICE_CLOSED"   # never the cached ok


# --------------------------------------------------------------------------------------------------- L3

def test_l3_concurrent_claims_yield_exactly_one_success(tmp_path, monkeypatch):
    import secrets
    s = config_mod.load(_env(tmp_path / "d"))
    lock = s.data_dir_lock
    real = secrets.token_hex

    def slow(n):                                                   # widen the check-then-set window
        threading.Event().wait(0.05)
        return real(n)
    monkeypatch.setattr(secrets, "token_hex", slow)
    n = 8
    barrier = threading.Barrier(n)
    won, busy = [], []

    def attempt():
        barrier.wait()
        try:
            won.append(lock.claim())
        except store_mod.DataDirBusy:
            busy.append(1)
    ts = [threading.Thread(target=attempt) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert len(won) == 1 and len(busy) == n - 1 and lock.holds(won[0])
    assert lock.release_claim(won[0]) is True


# --------------------------------------------------------------------------------------------------- L4

def test_l4_a_claim_token_is_adopted_once(tmp_path):
    from ledger import Recorder
    from service import SupportService
    s = config_mod.load(_env(tmp_path / "d"))
    token = s.data_dir_lock.claim()

    def make():
        return SupportService(s, Recorder(FakeLedger()), store_mod.RecordLog(s.data_dir),
                              store_mod.BodyStore(s.data_dir, s.hmac_key), lock_token=token)
    first = make()
    with pytest.raises(store_mod.DataDirBusy, match="already adopted"):
        make()                                                     # the same token, a second time: refused
    assert s.data_dir_lock.holds(first._lock_token)                # the first instance keeps its claim
    first.close()
    assert not s.data_dir_lock.claimed


# --------------------------------------------------------------------------------------------------- I5

def test_i5_a_refused_construction_leaves_the_live_instances_tmp_body(tmp_path):
    from ledger import Recorder
    from service import SupportService
    d = tmp_path / "d"
    h = Harness(tmp_path, data_dir=str(d))
    inflight = d / "bodies" / "inflight.tmp"
    inflight.write_bytes(b"half-written body of the live instance")
    s = h.svc.settings
    with pytest.raises(store_mod.DataDirBusy):
        SupportService(s, Recorder(FakeLedger()), store_mod.RecordLog(s.data_dir),
                       store_mod.BodyStore(s.data_dir, s.hmac_key), lock_token="forged")
    store_mod.BodyStore(s.data_dir, s.hmac_key)                    # a bare construction deletes nothing either
    assert inflight.read_bytes() == b"half-written body of the live instance"
    h2 = h.restart()                                               # a verified start does sweep it
    assert not inflight.exists() and h2.svc.integrity["ok"] is True


# --------------------------------------------------------------------------------------------------- I7

def test_i7_health_is_503_when_closed(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"))
    r = h.client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    h.svc.close()
    r = h.client.get("/health")
    assert r.status_code == 503 and r.json() == {"status": "closed"}
