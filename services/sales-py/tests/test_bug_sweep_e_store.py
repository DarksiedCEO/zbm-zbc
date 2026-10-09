"""Bug sweep E (Oct 9 2026; finance-py M1 pattern, AEGIS 5a56a3a M1 / c0869c4 M1): a SHORT write of a log line
(disk full, quota, a signal: ``pwrite`` returns fewer bytes than asked, no exception) left a partial line on disk that
memory did not hold, and the error went unnoticed: the append "succeeded". Now a short write is a failed write: the
file is cut back to its previous length and ``StoreWriteError`` is raised; if the cut-back itself fails the log
carries ``fault`` (LOCAL_LOG_WRITE_FAULT: integrity red, so unauthenticated ``/health`` says ``degraded``; the boolean
``log_write_fault`` is in the authenticated status view, ``service.health()`` -- the public ``/health`` stays exactly
``{"status": ...}`` per AEGIS L8) and refuses every write.
The pending-line file (``_write_file``) refuses a short write too. The store's API is unchanged."""

from __future__ import annotations

import os

import pytest

import store as store_mod
from helpers import Harness


def _short(real):
    return lambda fd, data, off: real(fd, data[: len(data) // 2], off)


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
    log.append_prepared(rec, line)                       # the same line goes in whole on the retry
    assert log.verify() and len(store_mod.RecordLog(str(tmp_path / "d"))) == 2


def test_failed_cut_back_sets_the_fault_and_refuses_writes(tmp_path, monkeypatch):
    log = store_mod.RecordLog(str(tmp_path / "d"))
    log.append("probe", "2026-10-01T00:00:00Z", {"n": 1})
    monkeypatch.setattr(store_mod.os, "pwrite", _short(os.pwrite))
    monkeypatch.setattr(store_mod.os, "ftruncate", lambda fd, n: (_ for _ in ()).throw(OSError(5, "EIO")))
    with pytest.raises(store_mod.StoreWriteError):
        log.append("probe", "2026-10-01T00:00:01Z", {"n": 2})
    monkeypatch.undo()
    assert log.fault and "could not be cut back" in log.fault
    with pytest.raises(store_mod.StoreWriteError):       # the torn tail is still there: fail closed
        log.append("probe", "2026-10-01T00:00:02Z", {"n": 3})
    assert len(log) == 1


def test_pending_file_short_write_is_refused(tmp_path, monkeypatch):
    path = str(tmp_path / "pending.line")
    real = os.write
    monkeypatch.setattr(store_mod.os, "write", lambda fd, data: real(fd, data[:3]))
    with pytest.raises(store_mod.StoreWriteError, match="ShortWrite"):
        store_mod._write_file(path, b"0123456789", "pending line")
    monkeypatch.undo()
    assert not os.path.exists(path) and not os.path.exists(path + ".tmp")   # a short write never becomes the file


def test_service_reports_local_log_write_fault(tmp_path, monkeypatch):
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"))
    assert h.svc.health()["log_write_fault"] is False
    monkeypatch.setattr(store_mod.os, "pwrite", _short(os.pwrite))
    monkeypatch.setattr(store_mod.os, "ftruncate", lambda fd, n: (_ for _ in ()).throw(OSError(5, "EIO")))
    with pytest.raises(store_mod.StoreWriteError):
        h.svc.log.append("probe", "2026-10-01T00:00:00Z", {"n": 1})
    monkeypatch.undo()
    integ = h.svc.verify_integrity(force=True, always=True)
    assert integ["ok"] is False and integ["problem"].startswith("LOCAL_LOG_WRITE_FAULT")
    status = h.svc.health()                              # the authenticated /<prefix>/v1/status view
    assert status["log_write_fault"] is True and status["integrity"]["ok"] is False
    assert h.client.get("/health").json() == {"status": "degraded"}
