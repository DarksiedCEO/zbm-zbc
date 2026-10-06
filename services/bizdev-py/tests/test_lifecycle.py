"""Instance lifecycle (service-py ADR 0014 rounds 5-5e): one instance per data directory with a single-use adopt
token, close() leaves the instance inert (no file and no ledger writes; integrity and job routes 503), and the
ledger's chain verify() runs outside the service lock with its verdict always reported."""

from __future__ import annotations

import os
import threading

import pytest

import config as config_mod
import store as store_mod
from helpers import FakeLedger, Harness, base_env, rid, write_key
from ledger import LedgerRecordError, Recorder
from service import BizDevService


def _act(h, i=0):
    return h.post("/partners", {"request_id": rid(), "partner_key": f"life-{i:04d}", "kind": "referral",
                                "brands": ["zbm"], "name": f"Life {i}", "domain": f"l{i}.test"})


def _settings(tmp_path):
    return config_mod.load(base_env(NBD_DATA_DIR=str(tmp_path / "d"),
                                    NBD_PII_HASH_KEY_FILE=write_key(tmp_path / "k.key")))


class SlowVerify(FakeLedger):
    def __init__(self):
        super().__init__()
        self.slow = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.during = None

    def verify(self) -> bool:
        if self.during is not None:
            hook, self.during = self.during, None
            hook()
        if self.slow:
            self.started.set()
            self.release.wait(timeout=30)
        return super().verify()


def _bg(fn):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", fn()))
    t.start()
    return t, out


def test_a_second_instance_in_the_same_process_is_refused(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"))
    with pytest.raises(store_mod.DataDirBusy):
        Harness(tmp_path, data_dir=str(tmp_path / "d"))
    h.ok(_act(h), 201)                                         # the first one still serves


def test_a_claim_token_is_adopted_once(tmp_path):
    s = _settings(tmp_path)
    lock = s.data_dir_lock
    token = lock.claim()
    svc = BizDevService(s, Recorder(FakeLedger()), store_mod.RecordLog(s.data_dir), lock_token=token)
    with pytest.raises(store_mod.DataDirBusy):
        BizDevService(s, Recorder(FakeLedger()), store_mod.RecordLog(s.data_dir), lock_token=token)
    assert lock.release_claim(token) is False and lock.claimed           # the claimer's token no longer releases it
    svc.close()
    assert not lock.claimed


def test_a_stale_or_foreign_token_is_refused(tmp_path):
    s = _settings(tmp_path)
    lock = s.data_dir_lock
    with pytest.raises(store_mod.DataDirBusy):
        BizDevService(s, Recorder(FakeLedger()), store_mod.RecordLog(s.data_dir), lock_token="0" * 32)
    assert not lock.claimed


def test_a_failed_start_gives_the_claim_back(tmp_path):
    s = _settings(tmp_path)
    led = FakeLedger()
    led.fail_reads = True

    class Boom(store_mod.RecordLog):
        def iter_records(self, start_seq=1):
            raise store_mod.StoreCorrupt("boom")
    with pytest.raises(store_mod.StoreCorrupt):
        BizDevService(s, Recorder(led), Boom(s.data_dir))
    assert not s.data_dir_lock.claimed
    BizDevService(s, Recorder(FakeLedger()), store_mod.RecordLog(s.data_dir)).close()


def test_a_closed_instance_is_inert(tmp_path):
    led = FakeLedger()
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=led)
    h.ok(_act(h), 201)
    h.svc.close()
    events, lines = len(led.events), len(h.svc.log)
    size = os.path.getsize(os.path.join(h.settings.data_dir, "bizdev_log.jsonl"))
    h.refused(_act(h, 1), 503, "SERVICE_CLOSED")
    h.refused(h.job("integrity"), 503, "SERVICE_CLOSED")
    h.refused(h.job("send-queue"), 503, "SERVICE_CLOSED")
    h.refused(h.get("/audit/integrity", caller="compliance_38"), 503, "SERVICE_CLOSED")
    h.refused(h.get("/audit/export", caller="compliance_38"), 503, "SERVICE_CLOSED")
    assert h.client.get("/health").status_code == 503
    assert h.ok(h.get("/status"))["status"] == "closed"
    assert h.svc.verify_integrity(force=True, always=True)["ok"] is False
    with pytest.raises(store_mod.StoreWriteError):
        h.svc.log.write_pending(b"x")
    with pytest.raises(store_mod.StoreWriteError):
        h.svc.log.clear_pending()
    assert len(led.events) == events and len(h.svc.log) == lines
    assert os.path.getsize(os.path.join(h.settings.data_dir, "bizdev_log.jsonl")) == size
    assert led.verify_calls == 0


def test_a_closed_instance_does_not_roll_its_pending_line_forward(tmp_path):
    class Lossy(FakeLedger):
        lose = 0

        def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary):
            if self.lose and event_type == "log_anchor":
                self.lose -= 1
                raise LedgerRecordError("lost")
            super().record_event(event_id, department, event_type, actor, subject_id, payload, summary)
    led = Lossy()
    h1 = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=led)
    h1.ok(_act(h1), 201)
    led.lose = 2
    h1.refused(_act(h1, 1), 503)
    assert h1.svc._own_pending is not None
    h2 = h1.restart()
    h2.ok(_act(h2, 2), 201)
    before = len(led.events)
    h1.refused(h1.job("integrity"), 503, "SERVICE_CLOSED")
    assert len(led.events) == before and h2.svc.integrity["ok"] is True


def test_slow_ledger_verify_does_not_block_requests(tmp_path):
    led = SlowVerify()
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=led)
    h.ok(_act(h), 201)
    led.slow = True
    job, job_out = _bg(lambda: h.job("integrity"))
    assert led.started.wait(timeout=10)
    req, req_out = _bg(lambda: _act(h, 1))
    req.join(timeout=5)
    blocked = req.is_alive()
    led.release.set()
    req.join(timeout=30)
    job.join(timeout=30)
    assert not blocked and req_out["r"].status_code == 201
    assert job_out["r"].json()["ledger_valid"] is True


def test_close_during_verify_writes_nothing(tmp_path):
    led = SlowVerify()
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=led)
    h.ok(_act(h), 201)
    events, lines = len(led.events), len(h.svc.log)
    led.during = h.svc.close
    h.refused(h.job("integrity"), 503, "SERVICE_CLOSED")
    assert len(led.events) == events and len(h.svc.log) == lines


def test_a_failed_ledger_verify_is_always_reported(tmp_path):
    led = SlowVerify()
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=led)
    h.ok(_act(h), 201)
    calls = []

    def tampered_and_busy():
        calls.append(1)
        h.ok(_act(h, 100 + len(calls)), 201)            # a commit lands during every verify()
        return False
    led.verify = tampered_and_busy
    a = h.ok(h.get("/audit/integrity", caller="compliance_38"))
    assert a["ledger_valid"] is False and a["log_length"] == len(h.svc.log)
    assert h.ok(h.job("integrity"))["ledger_valid"] is False
    assert len(calls) == 2


def test_a_truthy_non_bool_verify_is_not_valid(tmp_path):
    h = Harness(tmp_path)
    h.ledger.verify = lambda: "yes"
    assert h.ok(h.job("integrity"))["ledger_valid"] is False


def test_jobs_run_one_at_a_time(tmp_path):
    h = Harness(tmp_path)
    assert h.svc._tick_lock.acquire(blocking=False)
    try:
        h.refused(h.job("send-queue"), 409, "JOB_RUNNING")
    finally:
        h.svc._tick_lock.release()
    h.ok(h.job("send-queue"))
