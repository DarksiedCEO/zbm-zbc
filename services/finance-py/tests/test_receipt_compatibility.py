"""Local receipt reader compatibility; no mail or provider operations."""
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

OLD = '8e632340b70f32389a4fa3149023b0ed68dbe7c0'
NEW = '774e46b2ca5fc9b5758eaba909450156b27d88c9'


def guard():
    spec = importlib.util.find_spec('receipt_compatibility')
    assert spec is not None, 'receipt compatibility guard is absent'
    return importlib.import_module('receipt_compatibility')


def record(coll, rec):
    return {'data': {'ops': [['put', {'coll': coll, 'id': 'owned-fixture', 'rec': rec}]]}}


@pytest.mark.parametrize('coll', ['client_contacts', 'receipt_envelopes'])
def test_legacy_refuses_new_collection_history(coll):
    assert guard().assess([record(coll, {'envelope': {}} if coll == 'receipt_envelopes' else {})], OLD) == ('receipt_collections_unsupported',)


@pytest.mark.parametrize('status', ['delivery_uncertain', 'delivery_failed', 'delivery_reviewed'])
def test_legacy_refuses_terminal_history_even_after_later_status(status):
    history = [record('client_receipts', {'status': status}), record('client_receipts', {'status': 'sent'})]
    assert guard().assess(history, OLD) == ('receipt_states_unsupported',)
    assert guard().assess(history, NEW) == ()


@pytest.mark.parametrize('target', ['', 'latest', 'receipt-v1', NEW + 'x'])
def test_unknown_target_cannot_claim_capability(target):
    assert guard().assess([], target) == ('unknown_target',)


@pytest.mark.parametrize('status', ['future_delivery', None, True, ['sent']])
def test_unknown_receipt_state_refused_for_current_reader(status):
    assert guard().assess([record('client_receipts', {'status': status})], NEW) == ('unknown_receipt_semantics',)


@pytest.mark.parametrize('ops', [None, {}, [['put']], [['put', None]], [['put', {'coll': 'client_receipts'}]]])
def test_malformed_operation_refuses_sanitized(ops):
    assert guard().assess([{'data': {'ops': ops}}], NEW) == ('invalid_history',)


def test_legacy_normal_statuses_supported():
    history = [record('client_receipts', {'status': s}) for s in
               ('pending_send', 'sending', 'sent', 'withdrawn', 'withdrawn_after_send')]
    assert guard().assess(history, OLD) == ()


def test_startup_guard_before_replay_releases_claim(tmp_path, monkeypatch):
    g = guard()
    from helpers import Harness
    import api
    h = Harness(data_dir=str(tmp_path / 'data')).ready()
    h.svc.close()
    before = Path(h.svc.log.path).read_bytes()
    events = len(h.ledger.events)
    original = g.require_current
    def refuse(records):
        assert h.settings.data_dir_lock.claimed
        raise RuntimeError('receipt_compatibility_refused')
    monkeypatch.setattr(g, 'require_current', refuse)
    with pytest.raises(RuntimeError, match='receipt_compatibility_refused'):
        api.build_service(h.settings, h.clock, ledger=h.ledger)
    assert not h.settings.data_dir_lock.claimed
    assert Path(h.svc.log.path).read_bytes() == before
    assert len(h.ledger.events) == events
    monkeypatch.setattr(g, 'require_current', original)
    service = api.build_service(h.settings, h.clock, ledger=h.ledger)
    service.close()


def test_local_preflight_locked_snapshot_and_cleanup(tmp_path):
    g = guard()
    from store import DataDirLock, RecordLog
    data = tmp_path / 'data'
    data.mkdir()
    log = RecordLog(str(data))
    Path(log.path).write_bytes(b"")
    lock = DataDirLock(str(data))
    assert g.preflight(str(data), NEW)['reasons'] == ['directory_unavailable']
    lock.release()
    result = g.preflight(str(data), NEW)
    assert result['reasons'] == [] and result['snapshot_only'] is True
    assert result['artifact_verified'] is False and result['deployment_authorized'] is False
    assert result['target_source_sha'] == NEW
    again = DataDirLock(str(data))
    again.release()
    assert len(log) == 0


@pytest.mark.parametrize('mode', ['symlink', 'torn', 'oversize', 'directory'])
def test_preflight_refuses_unsafe_journal_without_contents(tmp_path, mode, monkeypatch):
    g = guard()
    from store import LOG_NAME
    data = tmp_path / 'data'
    data.mkdir()
    path = data / LOG_NAME
    if mode == 'symlink':
        target = tmp_path / 'private-value'
        target.write_text('private-value')
        path.symlink_to(target)
    elif mode == 'directory':
        path.mkdir()
    else:
        path.write_bytes(b'private-value')
        if mode == 'oversize':
            monkeypatch.setattr(g, 'MAX_LOG_BYTES', 2)
    result = g.preflight(str(data), NEW)
    assert result['reasons'] == ['history_unavailable']
    assert 'private-value' not in json.dumps(result)


def test_cli_json_refusal_has_no_arbitrary_input(tmp_path):
    g = guard()
    result = subprocess.run([sys.executable, '-B', g.__file__, '--data-dir', str(tmp_path),
                             '--target', 'private-untrusted-value'], capture_output=True, text=True)
    assert result.returncode == 2
    answer = json.loads(result.stdout)
    assert answer['reasons'] == ['unknown_target']
    assert 'private-untrusted-value' not in result.stdout + result.stderr


@pytest.mark.parametrize('feature', ['contact', 'uncertain'])
def test_real_service_history_capability_after_writer_stops(tmp_path, feature):
    g = guard()
    from helpers import Harness, rid
    from test_receipt_envelope import MockEnvelopeMail
    from store import RecordLog
    h = Harness(data_dir=str(tmp_path / 'data')).ready()
    h.fund_campaign()
    rec, = h.svc.db['client_receipts'].values()
    if feature == 'contact':
        import secrets
        h.svc.put_receipt_contact(rid(), rec['client_id'],
                                 {'email': secrets.token_hex(8) + '@example.invalid'})
    else:
        h.f['client_mail'].accept = False
        h.svc.send_client_receipt('scheduler', rid(), rec['client_receipt_id'])
        h.svc.ports.client_mail = MockEnvelopeMail()
        h.svc.send_client_receipt('scheduler', rid(), rec['client_receipt_id'])
        assert h.svc.db['client_receipts'][rec['client_receipt_id']]['status'] == 'delivery_uncertain'
    records = list(RecordLog(h.settings.data_dir).iter_records())
    reasons = g.assess(records, OLD)
    assert reasons == (('receipt_collections_unsupported',) if feature == 'contact' else
                       ('receipt_states_unsupported',))
    assert g.assess(records, NEW) == ()
    h.svc.close()


def test_unknown_durable_state_startup_refuses_before_lease(tmp_path):
    guard()
    from helpers import Harness, rid
    from service import Op
    import api
    h = Harness(data_dir=str(tmp_path / 'data')).ready()
    h.fund_campaign()
    rec, = h.svc.db['client_receipts'].values()
    op = Op(h.svc, rid(), 'test', 'test')
    op.put('client_receipts', rec['client_receipt_id'], {**rec, 'status': 'future_status'})
    h.svc._commit(op)
    h.svc.close()
    before = Path(h.svc.log.path).read_bytes()
    events = len(h.ledger.events)
    with pytest.raises(RuntimeError, match='receipt_compatibility_refused'):
        api.build_service(h.settings, h.clock, ledger=h.ledger)
    assert not h.settings.data_dir_lock.claimed
    assert Path(h.svc.log.path).read_bytes() == before
    assert len(h.ledger.events) == events


def test_preflight_lock_remains_held_through_assessment(tmp_path, monkeypatch):
    g = guard()
    from store import LOG_NAME, DataDirBusy, DataDirLock
    data = tmp_path / 'data'
    data.mkdir()
    (data / LOG_NAME).write_bytes(b'')
    original = g.assess
    def assess(records, target):
        with pytest.raises(DataDirBusy):
            DataDirLock(str(data))
        return original(records, target)
    monkeypatch.setattr(g, 'assess', assess)
    assert g.preflight(str(data), NEW)['reasons'] == []


@pytest.mark.parametrize('coll,rec', [
    ('client_receipts', {'status': 'sent', 'future_retry_authority': True}),
    ('client_contacts', {'email': None, 'revision': 'owned', 'future_policy': True}),
    ('receipt_envelopes', {'envelope': {'future_payload': True}}),
])
def test_future_receipt_fields_are_not_silently_understood(coll, rec):
    assert guard().assess([record(coll, rec)], NEW) == ('unknown_receipt_semantics',)


def test_unknown_inner_operation_is_not_silently_ignored():
    assert guard().assess([{'data': {'ops': [['future_receipt_reset', {}]]}}], NEW) == ('unknown_operation',)


def test_cli_argument_errors_never_echo_untrusted_values(tmp_path):
    g = guard()
    result = subprocess.run([sys.executable, '-B', g.__file__, '--data-dir', str(tmp_path),
                             '--target', NEW, '--private-untrusted-value'], capture_output=True, text=True)
    assert result.returncode == 2
    assert 'private-untrusted-value' not in result.stdout + result.stderr
    assert json.loads(result.stdout)['reasons'] == ['invalid_arguments']


def test_preflight_verifies_complete_chain_before_capability(tmp_path):
    g = guard()
    from store import RecordLog
    data = tmp_path / 'data'
    data.mkdir()
    log = RecordLog(str(data))
    log.append('operation', 'owned', record('client_receipts', {'status': 'delivery_uncertain'})['data'])
    log.append('operation', 'owned', record('client_receipts', {'status': 'sent'})['data'])
    before = Path(log.path).read_bytes()
    assert g.preflight(str(data), OLD)['reasons'] == ['receipt_states_unsupported']
    assert g.preflight(str(data), NEW)['reasons'] == []
    assert Path(log.path).read_bytes() == before
    Path(log.path).write_bytes(before.replace(b'delivery_uncertain', b'pending_send'))
    result = g.preflight(str(data), NEW)
    assert result['reasons'] == ['history_unavailable']
    assert 'journal_sha256' not in result


@pytest.mark.parametrize('target', [OLD, NEW])
def test_unknown_collection_never_receives_clear_preflight(target):
    assert guard().assess([record('future_receipt_policy', {})], target) == ('unknown_collection',)


def test_legitimate_evidence_only_operations_remain_supported():
    ops = [[kind, {}] for kind in ('lease', 'reconcile', 'founder_refused', 'injection', 'audit', 'evidence')]
    assert guard().assess([{'data': {'ops': ops}}], NEW) == ()
