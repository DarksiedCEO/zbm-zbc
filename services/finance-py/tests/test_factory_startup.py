"""Actual app factories forward dormant startup guards; no network/providers."""
import hashlib
import pytest
import api
import config as config_mod
from clock import FixedClock
from helpers import base_env, NOW
from fakes import FakeLedgerClient
from store import RecordLog


def settings(path):
    return config_mod.load(base_env(FIN_DATA_DIR=str(path)))


def files(path):
    return {str(f.relative_to(path)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in path.rglob('*') if f.is_file() and not f.name.endswith('.lock')}


def claim_available(cfg):
    claim = cfg.data_dir_lock.claim()
    assert claim
    assert cfg.data_dir_lock.release_claim(claim)


def test_factory_missing_seed_refuses_before_writes_and_releases_claim(tmp_path):
    cfg = settings(tmp_path / 'missing')
    RecordLog(cfg.data_dir)
    before = files(tmp_path)
    ledger = FakeLedgerClient()
    with pytest.raises(RuntimeError, match='expected persisted seed proposal'):
        api.build_service(cfg, FixedClock(NOW), ledger=ledger, require_existing_seed=True)
    assert ledger.calls == 0 and files(tmp_path) == before
    claim_available(cfg)


def test_factory_default_provisions_and_existing_restart_only_leases(tmp_path):
    cfg = settings(tmp_path / 'seeded')
    ledger = FakeLedgerClient()
    initial = api.build_service(cfg, FixedClock(NOW), ledger=ledger)
    try:
        assert any(p['kind'] == 'seed' for p in initial.proposals.values())
    finally:
        initial.close()
    count = len(ledger.events)
    restarted = api.build_service(cfg, FixedClock(NOW), ledger=ledger, require_existing_seed=True)
    try:
        assert any(p['kind'] == 'seed' for p in restarted.proposals.values())
        assert [e['event_type'] for e in ledger.events[count:]] == ['instance_lease', 'local_log_appended']
    finally:
        restarted.close()
    claim_available(cfg)


@pytest.mark.parametrize('invalid', [1, 'true', None])
def test_factory_invalid_guard_refuses_before_any_construction(tmp_path, invalid):
    cfg = settings(tmp_path / 'invalid')
    before = files(tmp_path)
    ledger = FakeLedgerClient()
    with pytest.raises(ValueError, match='require_existing_seed must be boolean'):
        api.build_service(cfg, FixedClock(NOW), ledger=ledger, require_existing_seed=invalid)
    assert ledger.calls == 0 and files(tmp_path) == before
    claim_available(cfg)
