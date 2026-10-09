"""Bug sweep E (Oct 9 2026), legal-py. Each test fails on fefd5be (the old store / no evidence view) and passes now.

* E-5 / F-3: the old ``store.py`` opened the log ``O_APPEND``: ONE fsync error left a line on disk that memory did not
  hold, the next append wrote after it, and the next restart refused (the chain broke) -- bricked for good. The store
  is now finance-py's (exact-size append at the expected offset, adopt / cut back, ``O_NOFOLLOW``), with the
  data-directory lock (``DataDirLock``: one process, one service instance) and ``close()``.
* finance-py AEGIS 5a56a3a M1 / c0869c4 M1: a SHORT write is a failed write (cut back, ``StoreWriteError``); a cut-back
  that fails sets ``fault`` (LOCAL_LOG_WRITE_FAULT in integrity, ``log_write_fault`` in /health) and refuses writes.
* R6-M1 (bizdev-py's): typed evidence carries ``rk`` and ``seq``, its id hashes the payload (no time), the line names
  it, and ``GET /legal/v1/audit/evidence`` tells ``committed`` from ``attempted``.
* Slow I/O under the service lock: ``integrity()`` reads the ledger and re-verifies the chain OUTSIDE the lock.
"""

from __future__ import annotations

import os
import threading

import pytest

import store as store_mod
from helpers import ANDRE_TOKEN, Harness, rid


def _short(real):
    return lambda fd, data, off: real(fd, data[: len(data) // 2], off)


def _eio(*_a, **_k):
    raise OSError(5, "EIO")


def test_one_fsync_error_does_not_brick_the_log(tmp_path, monkeypatch):
    d = str(tmp_path / "d")
    log = store_mod.RecordLog(d)
    log.append("probe", "2026-10-01T00:00:00Z", {"n": 1})
    real, calls = os.fsync, []

    def fsync_once(fd):
        calls.append(fd)
        if len(calls) == 1:
            raise OSError(5, "EIO")
        return real(fd)
    monkeypatch.setattr(store_mod.os, "fsync", fsync_once)
    with pytest.raises(store_mod.StoreWriteError):
        log.append("probe", "2026-10-01T00:00:01Z", {"n": 2})
    monkeypatch.undo()
    log.append("probe", "2026-10-01T00:00:02Z", {"n": 3})          # the next write goes in
    assert log.verify() and len(log) == 2 and log.fault is None
    assert [r["data"]["n"] for r in store_mod.RecordLog(d).records] == [1, 3]   # and a restart starts


def test_short_write_is_cut_back_and_raises(tmp_path, monkeypatch):
    log = store_mod.RecordLog(str(tmp_path / "d"))
    log.append("probe", "2026-10-01T00:00:00Z", {"n": 1})
    size = os.path.getsize(log.path)
    rec, line = log.prepare("probe", "2026-10-01T00:00:01Z", {"n": 2})
    monkeypatch.setattr(store_mod.os, "pwrite", _short(os.pwrite))
    with pytest.raises(store_mod.StoreWriteError, match="short write"):
        log.append_prepared(rec, line)
    monkeypatch.undo()
    assert os.path.getsize(log.path) == size and len(log) == 1 and log.fault is None
    log.append_prepared(rec, line)
    assert log.verify() and len(store_mod.RecordLog(str(tmp_path / "d"))) == 2


def test_failed_cut_back_sets_the_fault_and_refuses_writes(tmp_path, monkeypatch):
    log = store_mod.RecordLog(str(tmp_path / "d"))
    log.append("probe", "2026-10-01T00:00:00Z", {"n": 1})
    monkeypatch.setattr(store_mod.os, "pwrite", _short(os.pwrite))
    monkeypatch.setattr(store_mod.os, "ftruncate", _eio)
    with pytest.raises(store_mod.StoreWriteError):
        log.append("probe", "2026-10-01T00:00:01Z", {"n": 2})
    monkeypatch.undo()
    assert log.fault and "could not be cut back" in log.fault
    with pytest.raises(store_mod.StoreWriteError, match="LOCAL_LOG_WRITE_FAULT"):
        log.append("probe", "2026-10-01T00:00:02Z", {"n": 3})
    assert len(log) == 1


def test_service_reports_local_log_write_fault(tmp_path, monkeypatch):
    h = Harness(data_dir=str(tmp_path / "data"))
    h.approve_rules()
    assert h.ok(h.client.get("/health"))["log_write_fault"] is False
    assert h.ok(h.get("/legal/v1/integrity"))["status"] == "green"
    monkeypatch.setattr(store_mod.os, "pwrite", _short(os.pwrite))
    monkeypatch.setattr(store_mod.os, "ftruncate", _eio)
    r = h.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "r",
                                      "kind": "question"}, caller="hub")
    monkeypatch.undo()
    assert r.status_code == 503
    integ = h.ok(h.get("/legal/v1/integrity"))
    assert integ["status"] == "red" and any(p.startswith("LOCAL_LOG_WRITE_FAULT") for p in integ["problems"])
    health = h.ok(h.client.get("/health"))
    assert health["status"] == "degraded" and health["log_write_fault"] is True


def test_blob_short_write_never_becomes_a_blob(tmp_path, monkeypatch):
    blobs = store_mod.BlobStore(str(tmp_path / "d"))
    real = os.write
    monkeypatch.setattr(store_mod.os, "write", lambda fd, data: real(fd, data[:3]))
    with pytest.raises(store_mod.StoreWriteError):
        blobs.put(b"0123456789")
    monkeypatch.undo()
    assert os.listdir(blobs.dir) == []


def test_one_service_instance_per_data_directory(tmp_path):
    d = str(tmp_path / "d")
    x = Harness(data_dir=d)
    x.approve_rules()
    with pytest.raises(store_mod.DataDirBusy):                # a second instance while the first holds it
        x.svc.__class__(x.settings, x.svc.recorder, store_mod.RecordLog(d), store_mod.BlobStore(d),
                        _seeds(x), x.ports, x.clock)
    with pytest.raises(store_mod.DataDirBusy):                # a second process: the flock
        store_mod.DataDirLock(d)
    n = len(x.svc.log)
    x.svc.close()                                             # a closed instance writes nothing, records nothing
    calls = x.ledger.calls
    assert x.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "r",
                                         "kind": "question"}, caller="hub").status_code == 503
    assert x.ledger.calls == calls and len(x.svc.log) == n
    assert x.ok(x.client.get("/health"))["status"] == "closed"
    y = Harness(data_dir=d, ledger=x.ledger, clock=x.clock, ports=x.ports)   # after close: starts
    assert y.ok(y.get("/legal/v1/integrity"))["status"] == "green"


def _seeds(x):
    import api  # noqa: F401
    import config as config_mod
    from service import Seeds
    raw = config_mod.read_seeds(x.settings)
    return Seeds(raw["legal_rules_seed.json"], raw["documents.json"], raw["counsel_questions.json"],
                 raw["retention.json"], raw["signoff_topics.json"], raw["advice_patterns.json"],
                 raw["us_federal_holidays.json"])


def test_integrity_reads_the_ledger_and_the_chain_outside_the_service_lock(tmp_path):
    h = Harness(data_dir=str(tmp_path / "d"))
    held: list[bool] = []

    def probe():
        got = []

        def other():                       # another thread: can it take the service lock right now?
            ok = h.svc.lock.acquire(blocking=False)
            if ok:
                h.svc.lock.release()
            got.append(ok)
        t = threading.Thread(target=other)
        t.start()
        t.join()
        held.append(not got[0])

    real_entries, real_verify = h.ledger.entries, h.svc.log.verify
    h.ledger.entries = lambda: (probe(), real_entries())[1]
    h.svc.log.verify = lambda: (probe(), real_verify())[1]
    assert h.ok(h.get("/legal/v1/integrity"))["status"] == "green"
    assert held and not any(held)          # neither read ran with the service lock held


def _request(h, request_id):
    return h.post("/legal/v1/requests", {"request_id": request_id, "channel": "email", "requester_ref": "r",
                                         "kind": "question"}, caller="hub")


def test_evidence_view_retry_after_state_change_has_exactly_one_committed(hr):
    h = hr
    first = rid()
    h.svc.log.fail_next_append = True
    assert _request(h, first).status_code == 503                 # evidence recorded first, line never written
    assert _request(h, rid()).status_code in (200, 201)          # the state changes (the log moves on)
    assert _request(h, first).status_code in (200, 201)          # the retry: a new id, never a lasting 409
    view = h.ok(h.client.get("/legal/v1/audit/evidence", headers=h.headers(andre=ANDRE_TOKEN),
                             params={"limit": 1000}))
    assert view["rule"] == "unanchored evidence = attempted, not done"
    typed = [e for e in h.ledger.events if isinstance(e.get("payload"), dict) and "rk" in e["payload"]
             and e["payload"]["rk"].endswith(first)]
    assert typed, "the request's typed evidence carries rk"
    rows = {r["event_id"]: r for r in view["evidence"]}
    by_type: dict = {}
    for e in typed:
        by_type.setdefault(e["event_type"], []).append(rows[e["event_id"]]["status"])
    for statuses in by_type.values():                            # per logical action: one committed, one attempted
        assert sorted(statuses) == ["attempted", "committed"], by_type
    assert view["committed"] + view["attempted"] == view["total"]


def test_positive_control_a_same_payload_retry_dedupes(hr):
    h = hr
    h.svc.log.fail_next_append = True
    request_id = rid()
    assert _request(h, request_id).status_code == 503
    n = len(h.ledger.events)
    h.svc.log.fail_next_append = False
    assert _request(h, request_id).status_code in (200, 201)     # same state, same payloads: the same events
    new = h.ledger.events[n:]
    assert all(e["event_type"] == "local_log_appended" for e in new), [e["event_type"] for e in new]


def test_evidence_view_needs_andre_or_compliance(h):
    assert h.client.get("/legal/v1/audit/evidence", headers=h.headers(caller="hub")).status_code == 403
    assert h.client.get("/legal/v1/audit/evidence", headers=h.headers(caller="compliance_38")).status_code == 200


def test_a_refusal_retried_later_is_one_event_not_one_per_timestamp(hr):
    h = hr
    h.svc.log.fail_next_append = True                            # the refusal's line is not written
    bad = h.post("/legal/v1/rules/decisions", {"request_id": rid(), "decisions": []}, andre="wrong-token")
    assert bad.status_code in (401, 403)
    h.clock.advance(minutes=5)                                   # old ids carried the time: a second event
    again = h.post("/legal/v1/rules/decisions", {"request_id": rid(), "decisions": []}, andre="wrong-token")
    assert again.status_code in (401, 403)
    refused = h.ledger.of_type("founder_approval_refused")
    assert len(refused) == 1
    view = h.ok(h.client.get("/legal/v1/audit/evidence", headers=h.headers(andre=ANDRE_TOKEN),
                             params={"event_type": "founder_approval_refused"}))
    assert view["total"] == 1 and view["committed"] == 1
