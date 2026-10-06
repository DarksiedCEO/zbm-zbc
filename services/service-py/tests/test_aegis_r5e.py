"""AEGIS review of cc27b69 (Oct 6 2026, not blocking): L1 the ledger's chain verify() ran inside the service lock
(every request waited on it); Info: once adopted, the claimer's token must not release the claim."""

from __future__ import annotations

import threading

import config as config_mod
import store as store_mod
from helpers import FakeLedger, Harness, base_env


class SlowVerify(FakeLedger):
    """verify() blocks until released (a slow ledger) and can run a hook while it is in flight."""

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


# --------------------------------------------------------------------------------------------------- L1

def test_l1_a_slow_ledger_verify_does_not_block_a_concurrent_request(tmp_path):
    led = SlowVerify()
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=led)
    h.ok(h.chat("first"), 201)
    led.slow = True
    job, job_out = _bg(lambda: h.job("integrity"))
    assert led.started.wait(timeout=10)                            # verify() is in flight ...
    req, req_out = _bg(lambda: h.chat("second"))
    req.join(timeout=5)
    blocked = req.is_alive()
    led.release.set()                                              # (always let the slow call finish)
    req.join(timeout=30)
    job.join(timeout=30)
    assert not blocked                                             # ... and a request completed meanwhile
    assert req_out["r"].status_code == 201
    r = job_out["r"].json()
    assert job_out["r"].status_code == 200 and r["integrity"]["ok"] is True and r["ledger_valid"] is True


def test_l1_close_during_verify_writes_nothing(tmp_path):
    led = SlowVerify()
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=led)
    h.ok(h.chat("first"), 201)
    events, lines = len(led.events), len(h.svc.log)
    led.during = h.svc.close                                       # the instance closes while verify() runs
    r = h.job("integrity")
    assert r.status_code == 503 and r.json()["detail"] == "SERVICE_CLOSED"
    assert len(led.events) == events and len(h.svc.log) == lines   # zero ledger writes, zero log lines
    led.during = h.svc.close
    r = h.get("/svc/v1/audit/integrity", caller="compliance_38")
    assert r.status_code == 503 and r.json()["detail"] == "SERVICE_CLOSED"


def test_l1_a_log_change_during_verify_is_retried_never_paired_stale(tmp_path):
    led = SlowVerify()
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=led)
    h.ok(h.chat("first"), 201)
    led.during = lambda: h.ok(h.chat("during"), 201)            # a commit lands while verify() runs
    a = h.ok(h.get("/svc/v1/audit/integrity", caller="compliance_38"))
    assert a["log_length"] == len(h.svc.log) and a["ledger_valid"] is True   # retried on the new state


def test_l1_a_log_that_keeps_changing_reports_the_current_result_without_a_ledger_verdict(tmp_path):
    led = SlowVerify()
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=led)
    h.ok(h.chat("first"), 201)
    orig = led.verify

    def churn():
        h.ok(h.chat("churn"), 201)
        return FakeLedger.verify(led)
    led.verify = churn                                             # every verify() races a commit
    a = h.ok(h.get("/svc/v1/audit/integrity", caller="compliance_38"))
    led.verify = orig
    assert a["ledger_valid"] is None                               # no verdict computed from stale state
    assert a["log_length"] == len(h.svc.log) and a["integrity"] == h.svc.integrity


# --------------------------------------------------------------------------------------------------- Info

def test_info_the_claimers_token_cannot_release_an_adopted_claim(tmp_path):
    from ledger import Recorder
    from service import SupportService
    s = config_mod.load(base_env(SVC_NON_PRODUCTION=None, SVC_DATA_DIR=str(tmp_path / "d")))
    lock = s.data_dir_lock
    token = lock.claim()
    svc = SupportService(s, Recorder(FakeLedger()), store_mod.RecordLog(s.data_dir),
                         store_mod.BodyStore(s.data_dir, s.hmac_key), lock_token=token)
    assert lock.release_claim(token) is False and lock.claimed     # the claimer's token no longer releases it
    assert svc._lock_token != token and lock.holds(svc._lock_token)
    svc.close()                                                    # only the adopting service gives it back
    assert not lock.claimed
