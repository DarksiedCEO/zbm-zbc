"""Actual SEO/store hook; existing synthetic ledger boundary, no network."""
import copy
from pathlib import Path
import pytest
from errors import Unavailable
from helpers import Harness, FakeLedger, rid
from store import StoreWriteError


@pytest.fixture
def observed(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"))
    try:
        yield h
    finally:
        h.client.close()
        h.svc.close()


def snapshot(h):
    return (dict((str(p.relative_to(h.settings.data_dir)), p.read_bytes())
                 for p in Path(h.settings.data_dir).rglob('*') if p.is_file()),
            copy.deepcopy(h.ledger.events), copy.deepcopy(h.svc.integrity), h.svc._last_integrity_try,
            copy.deepcopy(h.svc.tenants), copy.deepcopy(h.svc.entities), h.svc._own_pending)


def forbid_writes(h, monkeypatch):
    def fail(*a, **k):raise AssertionError('observational hook attempted a write/repair')
    for name in ('_commit','_anchor','_seed','_settle_pending','_after_integrity'):
        monkeypatch.setattr(h.svc,name,fail)
    monkeypatch.setattr(h.ledger,'record_event',fail)
    for name in ('append_prepared','write_pending','clear_pending','write_discarded','clear_discarded'):
        monkeypatch.setattr(h.svc.log,name,fail)


def test_observe_success_fresh_reads_zero_writes(observed,monkeypatch):
    before=snapshot(observed);forbid_writes(observed,monkeypatch)
    assert observed.svc.observe_integrity()['integrity']['ok']
    assert snapshot(observed)==before
    observed.ledger.verify_result=False
    result=observed.svc.observe_integrity()
    assert not result['integrity']['ok'] and result['ledger_valid'] is False
    assert snapshot(observed)==before


@pytest.mark.parametrize('kind',['pending','discarded'])
def test_unsettled_files_refuse_without_repair(observed,monkeypatch,kind):
    _,line=observed.svc.log.prepare('tenant_created','2026-10-09T18:00:00Z',{'tenant_id':'extra','kind':'own','actor':'seo'})
    getattr(observed.svc.log,'write_'+kind)(line)
    before=snapshot(observed);forbid_writes(observed,monkeypatch)
    result=observed.svc.observe_integrity()
    assert not result['integrity']['ok'] and 'reconciliation' in result['integrity']['problem']
    assert snapshot(observed)==before


def test_real_anchored_pending_refuses_then_existing_repair_still_works(observed,monkeypatch):
    with monkeypatch.context() as m:
        def fail(*a,**k):raise StoreWriteError('synthetic append failure')
        m.setattr(observed.svc.log,'append_prepared',fail)
        with pytest.raises(Unavailable):observed.svc.create_tenant({'tenant_id':'extra','kind':'own','request_id':rid()})
    assert observed.svc._own_pending is not None
    before=snapshot(observed)
    assert not observed.svc.observe_integrity()['integrity']['ok']
    assert snapshot(observed)==before
    assert observed.svc.verify_integrity(force=True,always=True)['ok']
    assert 'extra' in observed.svc.tenants and observed.svc._own_pending is None


def test_unseeded_service_refuses_then_existing_seed_still_works(tmp_path):
    ledger=FakeLedger();ledger.fail=True
    h=Harness(tmp_path,data_dir=str(tmp_path/'data'),ledger=ledger)
    try:
        ledger.fail=False;before=snapshot(h)
        assert not h.svc.observe_integrity()['integrity']['ok']
        assert snapshot(h)==before and not h.svc.tenants
        assert h.svc.verify_integrity(force=True,always=True)['ok']
        assert 'zbm' in h.svc.tenants and 'ent-zbm' in h.svc.entities
    finally:h.client.close();h.svc.close()


def test_missing_anchor_and_read_failure_refuse(observed,monkeypatch):
    observed.ledger.events=[e for e in observed.ledger.events if e['event_type']!='log_anchor']
    before=snapshot(observed);forbid_writes(observed,monkeypatch)
    assert not observed.svc.observe_integrity()['integrity']['ok'];assert snapshot(observed)==before
    observed.ledger.fail_reads=True
    assert not observed.svc.observe_integrity()['integrity']['ok'];assert snapshot(observed)==before


def test_local_file_tamper_refuses(observed):
    p=Path(observed.svc.log.path);p.write_bytes(p.read_bytes()+b'bad')
    before=snapshot(observed)
    assert not observed.svc.observe_integrity()['integrity']['ok'];assert snapshot(observed)==before


def test_mutation_during_ledger_read_invalidates_observation(observed,monkeypatch):
    original=observed.ledger.entries
    def mutate():
        entries=original()
        observed.svc.create_tenant({'tenant_id':'extra','kind':'own','request_id':rid()})
        return entries
    monkeypatch.setattr(observed.ledger,'entries',mutate)
    result=observed.svc.observe_integrity()
    assert not result['integrity']['ok'] and 'changed' in result['integrity']['problem']
    assert 'extra' in observed.svc.tenants


def test_closed_service_refuses_without_ledger_io(observed,monkeypatch):
    observed.svc.close()
    def fail():raise AssertionError('closed service read ledger')
    monkeypatch.setattr(observed.ledger,'entries',fail);monkeypatch.setattr(observed.ledger,'verify',fail)
    with pytest.raises(Unavailable):observed.svc.observe_integrity()
