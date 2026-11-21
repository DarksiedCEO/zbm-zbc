"""Owned sidecars and synthetic observations only."""
import json
import os
import secrets
from datetime import datetime, timezone, timedelta
import pytest
import ops_observer as O
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
def snap(source="health:finance", coverage="COMPLETE", items=None, at=NOW):
    return O.Snapshot(source, coverage, at, items or {})
def test_source_isolation_and_restart(tmp_path):
    s = O.ObserverState(str(tmp_path))
    a = s.commit([snap(items={"write_refused": "service_unhealthy"}), snap("health:legal", items={"maintenance": "service_unhealthy"})], NOW)
    assert a["persisted"] and a["open_count"] == 2
    s = O.ObserverState(str(tmp_path)); assert s.current["preview_id"] == a["preview_id"]
    b = s.commit([snap()], NOW + timedelta(seconds=1))
    assert b["open_count"] == b["stale_count"] == 1 and b["coverage"]["health:legal"] == "UNCONFIGURED"
@pytest.mark.parametrize("mode", ["missing", "corrupt", "oversize", "symlink", "fifo"])
def test_established_bad_file_refuses_reset(tmp_path, mode):
    s = O.ObserverState(str(tmp_path)); s.commit([snap()], NOW)
    p = tmp_path / O.STATE_FILE; p.unlink()
    if mode == "corrupt": p.write_bytes(b'{')
    if mode == "oversize": p.write_bytes(b' ' * (O.MAX_BYTES + 1))
    if mode == "symlink": p.symlink_to(tmp_path / O.MARKER_FILE)
    if mode == "fifo": os.mkfifo(p)
    r = O.ObserverState(str(tmp_path)).commit([snap()], NOW)
    assert r["persisted"] is False and r["status"] == "degraded" and r["generation"] == 0
@pytest.mark.parametrize("coverage", ["UNKNOWN", "INCOMPLETE", "UNCONFIGURED"])
def test_noncomplete_retains_prior(tmp_path, coverage):
    s = O.ObserverState(str(tmp_path)); s.commit([snap(items={"maintenance": "service_unhealthy"})], NOW)
    r = s.commit([snap(coverage=coverage)], NOW + timedelta(seconds=1))
    assert r["open_count"] == r["stale_count"] == 1
@pytest.mark.parametrize("delta", [-1, 301])
def test_future_or_stale_snapshot_cannot_clear(tmp_path, delta):
    s = O.ObserverState(str(tmp_path)); s.commit([snap(items={"maintenance": "service_unhealthy"})], NOW)
    r = s.commit([snap(at=NOW - timedelta(seconds=delta))], NOW)
    assert r["open_count"] == r["stale_count"] == 1
def test_private_values_refused(tmp_path):
    s = O.ObserverState(str(tmp_path)); canary = "private_" + secrets.token_hex(12)
    r = s.commit([snap(items={canary: "service_unhealthy"})], NOW)
    assert r["persisted"] is False and canary not in json.dumps(r)
    assert not (tmp_path / O.STATE_FILE).exists()
def test_post_replace_fsync_failure_latches(tmp_path, monkeypatch):
    s = O.ObserverState(str(tmp_path)); first = s.commit([snap()], NOW)
    real = O.os.fsync; calls = []
    def fail(fd):
        calls.append(fd)
        if len(calls) == 2: raise OSError("private fake failure")
        return real(fd)
    monkeypatch.setattr(O.os, "fsync", fail)
    r = s.commit([snap(items={"maintenance": "service_unhealthy"})], NOW + timedelta(seconds=1))
    assert r["persisted"] is False and r["generation"] == first["generation"]
    assert s.commit([], NOW)["persisted"] is False and "private" not in json.dumps(r)
def test_clock_rollback_does_not_publish(tmp_path):
    s = O.ObserverState(str(tmp_path)); a = s.commit([snap()], NOW)
    r = s.commit([snap()], NOW - timedelta(seconds=1))
    assert r["persisted"] is False and r["generation"] == a["generation"]

@pytest.mark.parametrize('step', ['write', 'replace', 'file_fsync'])
def test_save_failure_preserves_last_known(tmp_path, monkeypatch, step):
    s = O.ObserverState(str(tmp_path)); a = s.commit([snap(items={'maintenance':'service_unhealthy'})], NOW)
    old = (tmp_path/O.STATE_FILE).read_bytes()
    def fail(*a, **k): raise OSError('private')
    if step == 'write': monkeypatch.setattr(O.os, 'write', fail)
    if step == 'replace': monkeypatch.setattr(O.os, 'replace', fail)
    if step == 'file_fsync': monkeypatch.setattr(O.os, 'fsync', fail)
    b = s.commit([snap()], NOW)
    assert b['persisted'] is False and b['generation'] == a['generation'] and b['open_count'] == 1
    assert (tmp_path/O.STATE_FILE).read_bytes() == old


def test_total_item_bound_refuses_instead_of_evicting(tmp_path):
    from service import rid
    s = O.ObserverState(str(tmp_path))
    items = {rid('brk', n): 'compliance_hold' for n in range(O.MAX_ITEMS + 1)}
    r = s.commit([snap('local:founder', items=items)], NOW)
    assert not r['persisted'] and r['generation'] == 0
