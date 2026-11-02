"""Actual existing-store constructors; no provider or network operations."""
import hashlib
import pytest
import api
import config as config_mod
from helpers import Harness, base_env
from store import RecordLog


def _existing_mode(monkeypatch, value=True):
    # Exercise the constructor guard independently of factory input validation.
    # Override the forwarded keyword rather than passing it twice.
    constructor = api.ComplianceService
    def construct(*args, **kwargs):
        kwargs["require_existing_seed"] = value
        return constructor(*args, **kwargs)
    monkeypatch.setattr(api, "ComplianceService", construct)


def _files(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file() and not p.name.endswith(".lock")}


def _claim_available(path):
    settings = config_mod.load(base_env(COMPLIANCE_DATA_DIR=str(path)))
    token = settings.data_dir_lock.claim()
    assert token
    assert settings.data_dir_lock.release_claim(token)


def test_existing_seed_missing_refuses_before_writes_and_releases_claim(tmp_path, monkeypatch):
    from fakes import FakeLedgerClient
    path = tmp_path / "unseeded"
    settings = config_mod.load(base_env(COMPLIANCE_DATA_DIR=str(path)))
    RecordLog(str(path))
    before = _files(path)
    ledger = FakeLedgerClient()
    _existing_mode(monkeypatch)
    with pytest.raises(RuntimeError, match="expected persisted seed proposal"):
        Harness(data_dir=str(path), ledger=ledger)
    assert ledger.calls == 0
    assert _files(path) == before
    _claim_available(path)


def test_default_provisions_then_existing_seed_replays_without_new_seed(tmp_path, monkeypatch):
    path = tmp_path / "preseeded"
    initial = Harness(data_dir=str(path))
    assert any(p["kind"] == "seed" for p in initial.svc.proposals.values())
    ledger = initial.ledger
    initial.svc.close()
    count = len(ledger.events)
    _existing_mode(monkeypatch)
    restarted = Harness(data_dir=str(path), ledger=ledger)
    try:
        assert any(p["kind"] == "seed" for p in restarted.svc.proposals.values())
        assert not any(e["event_type"] in {"seed_loaded", "rules_proposal_created", "register_proposal_created"}
                       for e in ledger.events[count:])
    finally:
        restarted.svc.close()
    _claim_available(path)


@pytest.mark.parametrize("invalid", [1, "true", None])
def test_existing_seed_requires_bool_without_writes_or_claim_leak(tmp_path, monkeypatch, invalid):
    path = tmp_path / "strict"
    initial = Harness(data_dir=str(path))
    ledger = initial.ledger
    initial.svc.close()
    calls, before = ledger.calls, _files(path)
    _existing_mode(monkeypatch, invalid)
    with pytest.raises(RuntimeError, match="must be boolean"):
        Harness(data_dir=str(path), ledger=ledger)
    assert ledger.calls == calls
    assert _files(path) == before
    _claim_available(path)
