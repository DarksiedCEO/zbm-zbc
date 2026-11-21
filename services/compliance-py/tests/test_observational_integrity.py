"""Read-only observations of actual Compliance stores and synthetic Ledger evidence."""
import copy
from pathlib import Path
import pytest
from helpers import Harness
from ledger import LedgerQueryFailed


@pytest.fixture
def h(tmp_path):
    harness = Harness(data_dir=str(tmp_path / 'store'))
    try:
        yield harness
    finally:
        harness.svc.close()


def forbid_writes(h, monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError('observation attempted a write/repair/control')
    for name in ('_record', '_commit', 'ensure_seed_proposal', '_write_lease',
                 'run_internal_controls', '_draft_reverify', '_idem_store'):
        monkeypatch.setattr(h.svc, name, refused)
    for name in ('prepare', 'append', 'append_prepared'):
        monkeypatch.setattr(h.svc.log, name, refused)
    monkeypatch.setattr(h.ledger, 'record_event', refused)


def test_existing_evidence_passes_without_writes_or_cache_mutation(h, monkeypatch):
    disk = Path(h.svc.log.path).read_bytes()
    events = copy.deepcopy(h.ledger.events)
    state = copy.deepcopy((h.svc.proposals, h.svc.idem, h.svc.reconcile_required))
    forbid_writes(h, monkeypatch)
    first = h.svc.observe_integrity()
    assert first['integrity']['ok'] is True and first['ledger_valid'] is True
    assert first['log_length'] == len(h.svc.log) and len(first['log_head_sha256']) == 64
    assert h.svc.observe_integrity() == first
    assert Path(h.svc.log.path).read_bytes() == disk and h.ledger.events == events
    assert (h.svc.proposals, h.svc.idem, h.svc.reconcile_required) == state


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'truncated', 'same_length_rehash'])
def test_local_damage_is_red_without_repair(h, monkeypatch, damage):
    from store import encode
    import hashlib, json
    path = Path(h.svc.log.path)
    original = path.read_bytes()
    if damage == 'missing':
        path.unlink()
    elif damage == 'corrupt':
        path.write_bytes(b'not-json\n')
    elif damage == 'truncated':
        path.write_bytes(b'\n'.join(original.splitlines()[:-1]) + b'\n')
    else:
        lines = original.splitlines()
        record = json.loads(lines[-1]);record['at'] = '2026-01-01T00:00:00Z'
        record['record_sha256'] = hashlib.sha256(encode({k:v for k,v in record.items() if k != 'record_sha256'})).hexdigest()
        path.write_bytes(b'\n'.join(lines[:-1] + [encode(record)]) + b'\n')
    before = path.read_bytes() if path.exists() else None
    forbid_writes(h, monkeypatch)
    assert h.svc.observe_integrity()['integrity']['ok'] is False
    assert (path.read_bytes() if path.exists() else None) == before


def test_missing_ledger_anchor_reuses_existing_assessment(h, monkeypatch):
    h.ledger.events[:] = [e for e in h.ledger.events if e['event_type'] != 'local_log_appended']
    forbid_writes(h, monkeypatch)
    result = h.svc.observe_integrity()
    assert result['integrity']['ok'] is False and result['ledger_valid'] is True


@pytest.mark.parametrize('value', [False, 1, None])
def test_ledger_verification_requires_actual_true(h, monkeypatch, value):
    monkeypatch.setattr(h.ledger, 'verify', lambda: value)
    forbid_writes(h, monkeypatch)
    result = h.svc.observe_integrity()
    assert result['integrity']['ok'] is False and result['ledger_valid'] is False


def test_unreadable_ledger_is_red(h, monkeypatch):
    h.ledger.readable = False
    forbid_writes(h, monkeypatch)
    assert h.svc.observe_integrity()['integrity']['ok'] is False


def test_local_commit_during_ledger_read_invalidates_observation(h, monkeypatch):
    entries = h.ledger.entries
    def read_then_commit():
        before = entries()
        h.svc._write_lease()
        return before
    monkeypatch.setattr(h.ledger, 'entries', read_then_commit)
    result = h.svc.observe_integrity()
    assert result['integrity']['ok'] is False
    assert result['integrity']['problem'] == 'local evidence changed during observation'


def test_fault_and_closed_are_red_without_ledger_calls(h, monkeypatch):
    def unexpected():
        raise AssertionError('invalid local state should not read Ledger')
    monkeypatch.setattr(h.ledger, 'entries', unexpected)
    h.svc.log.fault = 'synthetic'
    assert h.svc.observe_integrity()['integrity']['ok'] is False
    h.svc.log.fault = None
    h.svc.close()
    assert h.svc.observe_integrity()['integrity']['ok'] is False


def test_in_memory_store_cannot_claim_persisted_evidence():
    h = Harness()
    try:
        assert h.svc.observe_integrity()['integrity']['ok'] is False
    finally:
        h.svc.close()


def test_empty_local_snapshot_is_not_healthy(h, monkeypatch):
    h.svc.log._lines.clear()
    Path(h.svc.log.path).write_bytes(b'')
    forbid_writes(h, monkeypatch)
    assert h.svc.observe_integrity()['integrity']['ok'] is False


def test_ledger_verification_exception_is_red(h, monkeypatch):
    def unavailable():
        raise LedgerQueryFailed('synthetic unreadable')
    monkeypatch.setattr(h.ledger, 'verify', unavailable)
    forbid_writes(h, monkeypatch)
    assert h.svc.observe_integrity()['integrity']['ok'] is False
