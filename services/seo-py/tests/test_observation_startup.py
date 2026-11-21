"""Constructor-only observation; real service/log, synthetic Ledger, no network."""
import copy
from pathlib import Path

import pytest
import api
from errors import Unavailable
from helpers import Harness, FakeLedger, base_env, rid, write_key
from ledger import Recorder
from service import SeoService
from store import RecordLog, StoreCorrupt, StoreWriteError


def fail(*args, **kwargs):
    raise AssertionError('startup attempted repair or write')


def freeze(monkeypatch, ledger):
    for name in ('_commit', '_anchor', '_seed', '_settle_pending', '_after_integrity'):
        monkeypatch.setattr(SeoService, name, fail)
    for name in ('append_prepared', 'write_pending', 'clear_pending', 'write_discarded', 'clear_discarded'):
        monkeypatch.setattr(RecordLog, name, fail)
    monkeypatch.setattr(ledger, 'record_event', fail)


def state(h):
    return ({str(p.relative_to(h.settings.data_dir)): p.read_bytes()
             for p in Path(h.settings.data_dir).rglob('*') if p.is_file()}, copy.deepcopy(h.ledger.events))


@pytest.fixture
def seeded(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / 'data'))
    h.client.close()
    h.svc.close()
    yield h
    h.svc.close()


def construct(h, **kwargs):
    return SeoService(h.settings, Recorder(h.ledger), RecordLog(h.settings.data_dir),
                      clock=h.clock, **kwargs)


@pytest.mark.parametrize('bad', [None, 0, 1, 'true'])
def test_strict_bool_before_factory_or_constructor_effects(monkeypatch, bad):
    monkeypatch.setattr(api.config_mod, 'load', fail)
    with pytest.raises(TypeError, match='must be a bool'):
        api.build({}, observe_existing_on_start=bad)
    with pytest.raises(TypeError, match='must be a bool'):
        SeoService(None, None, None, observe_existing_on_start=bad)


def test_populated_constructor_is_observational_and_later_service_is_usable(seeded, monkeypatch):
    before = state(seeded)
    with monkeypatch.context() as m:
        freeze(m, seeded.ledger)
        svc = construct(seeded, observe_existing_on_start=True)
        assert svc.integrity['ok'] is True
        assert state(seeded) == before
    try:
        svc.create_tenant({'tenant_id': 'later', 'kind': 'own', 'request_id': rid()})
        assert 'later' in svc.tenants
        assert state(seeded) != before  # constructor option is not persistent read-only mode
    finally:
        svc.close()


def test_factory_forwards_to_actual_service(seeded, monkeypatch):
    monkeypatch.setattr(api, 'UnconfiguredLedgerClient', lambda: seeded.ledger)
    before = state(seeded)
    freeze(monkeypatch, seeded.ledger)
    _, svc = api.build(seeded.env, observe_existing_on_start=True)
    try:
        assert svc.integrity['ok'] and state(seeded) == before
    finally:
        svc.close()


@pytest.mark.parametrize('kind', ['pending', 'discarded'])
@pytest.mark.parametrize('content', ['next', 'stale', 'anchored'])
def test_sidecars_refuse_unchanged_and_release_claim(seeded, monkeypatch, kind, content):
    log = RecordLog(seeded.settings.data_dir)
    if content == 'stale':
        line = log.raw_lines()[0]
    else:
        rec, line = log.prepare('tenant_created', '2026-10-09T18:00:00Z',
                                {'tenant_id': 'extra', 'kind': 'own', 'actor': 'seo'})
        if content == 'anchored':
            from service import sha256_hex
            eid, payload = seeded.svc._anchor_ids(log.epoch, rec['seq'], sha256_hex(line))
            seeded.svc._anchor(eid, 'seo', log.epoch, payload, rec['kind'], rec['seq'])
    getattr(log, 'write_' + kind)(line)
    before = state(seeded)
    with monkeypatch.context() as m:
        freeze(m, seeded.ledger)
        with pytest.raises(Unavailable, match='INTEGRITY_UNVERIFIED'):
            construct(seeded, observe_existing_on_start=True)
        assert state(seeded) == before
        assert not seeded.settings.data_dir_lock.claimed
    svc = construct(seeded)  # ordinary recovery remains available after refusal
    try:
        if content == 'anchored':
            assert 'extra' in svc.tenants
    finally:
        svc.close()


@pytest.mark.parametrize('fault', ['read', 'false', 'none', 'one', 'anchor', 'corrupt', 'deleted'])
def test_failed_evidence_refuses_without_write(seeded, monkeypatch, fault):
    if fault == 'read':
        seeded.ledger.fail_reads = True
    elif fault in ('false', 'none', 'one'):
        monkeypatch.setattr(seeded.ledger, 'verify', lambda: {'false': False, 'none': None, 'one': 1}[fault])
    elif fault == 'anchor':
        seeded.ledger.events = []
    elif fault == 'corrupt':
        Path(seeded.svc.log.path).write_bytes(b'torn')
    elif fault == 'deleted':
        Path(seeded.svc.log.path).unlink()
    before = state(seeded)
    freeze(monkeypatch, seeded.ledger)
    with pytest.raises((Unavailable, StoreCorrupt)):
        construct(seeded, observe_existing_on_start=True)
    assert state(seeded) == before
    assert not seeded.settings.data_dir_lock.claimed


@pytest.mark.parametrize('persistent', [False, True])
def test_empty_refuses_before_provision_then_default_provisions(tmp_path, monkeypatch, persistent):
    import config
    ledger = FakeLedger()
    settings = config.load(base_env(SEO_DATA_DIR=str(tmp_path / 'data') if persistent else None,
                                   SEO_LOG_HASH_KEY_FILE=write_key(tmp_path / 'log.key') if persistent else None))
    with monkeypatch.context() as m:
        freeze(m, ledger)
        with pytest.raises(Unavailable):
            SeoService(settings, Recorder(ledger), RecordLog(settings.data_dir), observe_existing_on_start=True)
        assert ledger.events == []
        if persistent:
            assert not settings.data_dir_lock.claimed
            assert not list(Path(settings.data_dir).glob('*.jsonl'))
    svc = SeoService(settings, Recorder(ledger), RecordLog(settings.data_dir))
    try:
        assert 'zbm' in svc.tenants and 'ent-zbm' in svc.entities
        assert ledger.events
    finally:
        svc.close()


def test_factory_refusal_releases_its_claim(tmp_path, monkeypatch):
    import config
    ledger = FakeLedger()
    env = base_env(SEO_DATA_DIR=str(tmp_path / 'data'), SEO_LOG_HASH_KEY_FILE=write_key(tmp_path / 'log.key'))
    monkeypatch.setattr(api, 'UnconfiguredLedgerClient', lambda: ledger)
    with pytest.raises(Unavailable):
        api.build(env, observe_existing_on_start=True)
    assert not config.load(env).data_dir_lock.claimed
    _, svc = api.build(env)
    svc.close()


@pytest.mark.parametrize('missing', ['tenant', 'entity'])
def test_individually_missing_seed_refuses_actual_anchored_history(tmp_path, monkeypatch, missing):
    with monkeypatch.context() as m:
        if missing == 'entity':
            m.setattr(SeoService, '_seed_entity', lambda self: None)
        else:
            m.setattr(SeoService, '_seed', lambda self: self._seed_entity())
        h = Harness(tmp_path, data_dir=str(tmp_path / 'data'))
    h.client.close()
    h.svc.close()
    assert bool(h.svc.tenants) is (missing != 'tenant')
    assert bool(h.svc.entities) is (missing != 'entity')
    before = state(h)
    freeze(monkeypatch, h.ledger)
    with pytest.raises(Unavailable):
        construct(h, observe_existing_on_start=True)
    assert state(h) == before
    assert not h.settings.data_dir_lock.claimed


@pytest.mark.parametrize('name', ['pending.line', 'pending.discarded'])
@pytest.mark.parametrize('contents', [b'', b'not a record'])
def test_factory_refuses_present_malformed_sidecar_unchanged(seeded, monkeypatch, name, contents):
    sidecar = Path(seeded.settings.data_dir) / name
    sidecar.write_bytes(contents)
    before = state(seeded)
    monkeypatch.setattr(api, 'UnconfiguredLedgerClient', lambda: seeded.ledger)
    freeze(monkeypatch, seeded.ledger)
    svc = None
    try:
        with pytest.raises(Unavailable, match='INTEGRITY_UNVERIFIED'):
            _, svc = api.build(seeded.env, observe_existing_on_start=True)
    finally:
        if svc is not None:
            svc.close()
    assert sidecar.read_bytes() == contents
    assert state(seeded) == before
    assert not seeded.settings.data_dir_lock.claimed


@pytest.mark.parametrize('name', ['pending.line', 'pending.discarded'])
def test_default_empty_sidecar_behavior_unchanged(seeded, name):
    sidecar = Path(seeded.settings.data_dir) / name
    sidecar.write_bytes(b'')
    svc = construct(seeded)
    try:
        assert svc.integrity['ok'] is True
        assert sidecar.exists() and sidecar.read_bytes() == b''
    finally:
        svc.close()
