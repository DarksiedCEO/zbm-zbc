"""AEGIS focused check of e0a69a9..4f10b5f (Oct 6 2026): 1) a handed-over claim must be the current claim (token);
2) close() under the locks, and a closed instance never deletes the live instance's pending / discarded files;
3) /svc/v1/status reports a closed instance as closed, never ok."""

from __future__ import annotations

import threading

import pytest

import api
import config as config_mod
import store as store_mod
from helpers import FakeLedger, Harness, base_env


def _env(d):
    return base_env(SVC_NON_PRODUCTION=None, SVC_DATA_DIR=str(d))


# --------------------------------------------------------------------------------------------------- item 1

def test_item1_an_unclaimed_or_stale_token_is_refused(tmp_path):
    from ledger import Recorder
    from service import SupportService
    d = tmp_path / "d"
    s = config_mod.load(_env(d))
    lock = s.data_dir_lock

    def make(token):
        return SupportService(s, Recorder(FakeLedger()), store_mod.RecordLog(s.data_dir),
                              store_mod.BodyStore(s.data_dir, s.hmac_key), lock_token=token)
    with pytest.raises(store_mod.DataDirBusy, match="not held"):
        make("forged-token")                                       # nothing claimed: refused
    stale = lock.claim()
    assert lock.release_claim(stale) is True
    with pytest.raises(store_mod.DataDirBusy, match="not held"):
        make(stale)                                                # a released (stale) token: refused
    live = lock.claim()
    with pytest.raises(store_mod.DataDirBusy, match="not held"):
        make("x" * 32)                                             # someone else's claim is held: refused
    assert lock.claimed and lock.holds(live)                       # and the refused attempt left it alone
    svc = make(live)
    assert svc._lock_token == live
    svc.close()
    assert not lock.claimed


def test_item1_a_stale_release_never_frees_another_instances_claim(tmp_path):
    d = tmp_path / "d"
    _, s1 = api.build(_env(d))
    old = s1._lock_token
    s1.close()
    _, s2 = api.build(_env(d))
    lock = s2._dir_lock
    assert lock.release_claim(old) is False and lock.claimed       # s1's old token cannot free s2's claim
    with pytest.raises(store_mod.DataDirBusy):
        api.build(_env(d))
    s2.close()


# --------------------------------------------------------------------------------------------------- item 2

def test_item2_a_closed_instance_cannot_delete_the_live_instances_pending_or_discarded_files(tmp_path):
    d = tmp_path / "d"
    h1 = Harness(tmp_path, data_dir=str(d))
    h1.ok(h1.chat("I need help"), 201)
    old_log = h1.svc.log
    h2 = h1.restart()                                              # two instances in one process; h1's is closed
    h2.svc.log.write_pending(b"live pending line")
    h2.svc.log.write_discarded(b"live discarded line")
    with pytest.raises(store_mod.StoreWriteError, match="closed"):
        old_log.clear_pending()
    with pytest.raises(store_mod.StoreWriteError, match="closed"):
        old_log.clear_discarded()
    assert (d / "pending.line").read_bytes() == b"live pending line"
    assert (d / "pending.discarded").read_bytes() == b"live discarded line"
    h2.svc.log.clear_pending()
    h2.svc.log.clear_discarded()


def test_item2_close_waits_for_a_commit_in_progress(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"))
    order = []
    h.svc.lock.acquire()                                           # a commit holds the service lock
    t = threading.Thread(target=lambda: (h.svc.close(), order.append("closed")))
    t.start()
    t.join(timeout=0.5)
    order.append("commit done")
    h.svc.lock.release()
    t.join(timeout=10)
    assert order == ["commit done", "closed"] and h.svc._closed


def test_item2_the_closed_check_is_inside_the_log_lock(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"))
    rec, line = h.svc.log.prepare("job_ran", "2026-10-06T18:00:00Z", {"effects": [], "actor": "scheduler"})
    before = len(h.svc.log)
    result = {}

    def append():
        try:
            h.svc.log.append_prepared(rec, line)
            result["r"] = "written"
        except store_mod.StoreWriteError:
            result["r"] = "refused"
    h.svc.log.lock.acquire()                                       # the append waits on the log lock ...
    t = threading.Thread(target=append)
    t.start()
    t.join(timeout=0.3)
    h.svc.log.closed = True                                        # ... while the instance is closed
    h.svc.log.lock.release()
    t.join(timeout=10)
    assert result["r"] == "refused"
    assert len(h.svc.log) == before                                # nothing was written (AEGIS a5dd261 I6)


# --------------------------------------------------------------------------------------------------- item 3

def test_item3_status_reports_a_closed_instance_as_closed(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"))
    assert h.ok(h.get("/svc/v1/status"))["status"] == "ok"
    h.svc.close()
    st = h.ok(h.get("/svc/v1/status"))
    assert st["status"] == "closed" and st["closed"] is True and st["integrity"]["ok"] is False
    assert h.client.get("/health").json() == {"status": "closed"}
