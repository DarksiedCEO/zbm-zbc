"""Read-only receipt inventory; owned fixtures, never delivery authorization."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import json
import secrets

import pytest

from helpers import ANDRE_TOKEN, CALLERS, Harness
from service import rid

PATH = '/fin/v1/client-receipts/backlog'
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def record(n=0, **changes):
    key = rid('crc', n)
    return key, {'client_receipt_id': key, 'invoice_id': rid('inv', n), 'entity': 'zbm',
                 'status': 'pending_send', 'attempts': 0, 'created_at': '2026-10-31T23:00:00Z',
                 'paid_on': '2026-10-29', **changes}


def project(records, envelopes=None, now=NOW, **query):
    from receipt_backlog import snapshot_rows, page
    return page(snapshot_rows(records, {} if envelopes is None else envelopes), now, **query)


def token(value):
    text = value if type(value) is str else json.dumps(value, sort_keys=True, separators=(',', ':'))
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')


def test_real_payment_inventory_is_private_and_success_has_no_writes(tmp_path):
    h = Harness(data_dir=str(tmp_path / 'data')).ready()
    h.fund_campaign()
    before = copy.deepcopy(h.svc.db), copy.deepcopy(h.ledger.events)
    journal = (tmp_path / 'data' / 'fin_log.jsonl').read_bytes()
    answer = h.ok(h.get(PATH, caller=None, andre=ANDRE_TOKEN))
    assert answer['total'] == 1 and answer['rows'][0]['status'] == 'pending_send'
    assert answer['rows'][0]['client_receipt_id'] == next(iter(h.svc.db['client_receipts']))
    assert answer['acceptance_record_validated'] is False and answer['delivery_verified'] is False
    assert (h.svc.db, h.ledger.events) == before
    assert (tmp_path / 'data' / 'fin_log.jsonl').read_bytes() == journal
    for forbidden in ('amount_paid', 'description', 'client_id', 'body', 'provider_message_id', 'email'):
        assert forbidden not in answer['rows'][0]


@pytest.mark.parametrize('caller', [None, *CALLERS])
def test_founder_required_and_denial_audit_retained(caller):
    h = Harness().ready()
    before = len(h.ledger.events)
    r = h.get(PATH, caller=caller)
    assert r.status_code == 403
    # Named-only identities may be refused by the outer route restriction before founder auditing.
    assert len(h.ledger.events) >= before
    if caller is None:
        assert len(h.ledger.events) > before
        assert 'receipt-backlog' in str(h.ledger.events[before:])


def test_bad_bearer_and_closed_service_refuse():
    h = Harness().ready()
    assert h.client.get(PATH, headers=h.headers(andre=ANDRE_TOKEN, bearer=secrets.token_hex(32))).status_code == 401
    h.svc.close()
    assert h.get(PATH, caller=None, andre=ANDRE_TOKEN).status_code == 503


def test_three_pages_stable_unknown_first_and_cursor_limit_inherited():
    rows = dict(record(n, created_at=None if n < 2 else '2026-10-31T23:00:00Z') for n in range(7))
    first = project(rows, limit='3')
    second = project(rows, cursor=first['next_cursor'])
    third = project(rows, cursor=second['next_cursor'])
    found = first['rows'] + second['rows'] + third['rows']
    assert len({r['row_ref'] for r in found}) == 7 and third['next_cursor'] is None
    assert all(r['created_at'] is None for r in found[:2])
    assert second['limit'] == 3 and first['snapshot'] == third['snapshot']
    assert project(copy.deepcopy(rows), cursor=first['next_cursor']) == second


def test_snapshot_changes_only_for_projected_stored_data_not_clock_or_private_fields():
    from errors import Conflict
    rows = dict(record(n, created_at='2026-11-01T00:01:00Z') for n in range(3))
    first = project(rows, limit='1')
    later = project(rows, now=NOW + timedelta(minutes=2), cursor=first['next_cursor'])
    assert first['snapshot'] == later['snapshot']
    assert first['rows'][0]['creation_age_seconds'] is None
    assert later['rows'][0]['creation_age_seconds'] == 60
    next(iter(rows.values()))['description'] = secrets.token_hex(20)
    assert project(rows)['snapshot'] == first['snapshot']
    next(iter(rows.values()))['status'] = 'withdrawn'
    with pytest.raises(Conflict, match='snapshot_changed'):
        project(rows, cursor=first['next_cursor'])


@pytest.mark.parametrize('limit', ['', '0', '101', '-1', '01', '+1', '1.0', 'true', ' 1', '1'*500, True, 1])
def test_strict_query_limit(limit):
    from errors import Invalid
    with pytest.raises(Invalid):
        project({}, limit=limit)


@pytest.mark.parametrize('cursor', ['', 'a'*257, '%%%=', token('{}'), token('{"v":1,"v":1}'),
    token('{"v":NaN}'), token({'v':True,'snapshot':'0'*64,'offset':0,'limit':1}),
    token({'v':1,'snapshot':'0'*64,'offset':False,'limit':1}),
    token({'v':1,'snapshot':'0'*64,'offset':0,'limit':1,'extra':0})])
def test_malformed_cursor_refused(cursor):
    from errors import Invalid
    with pytest.raises(Invalid):
        project({}, cursor=cursor)


def test_cursor_noncanonical_limit_mismatch_and_range():
    from errors import Invalid
    rows = dict(record(n) for n in range(2))
    first = project(rows, limit='1')
    with pytest.raises(Invalid):
        project(rows, limit='2', cursor=first['next_cursor'])
    with pytest.raises(Invalid):
        project(rows, cursor=first['next_cursor'] + '=')
    with pytest.raises(Invalid):
        project(rows, cursor=token({'v':1,'snapshot':first['snapshot'],'offset':2,'limit':1}))


@pytest.mark.parametrize('status', ['pending_send','sending','sent','withdrawn','withdrawn_after_send',
    'delivery_uncertain','delivery_failed','delivery_reviewed',None,[], 'private-value'])
def test_all_states_visible(status):
    key, rec = record(status=status)
    row, = project({key:rec})['rows']
    assert row['status'] == (status if type(status) is str and status != 'private-value' else 'unknown')


def test_malformed_keys_rows_and_huge_fields_are_bounded_placeholders():
    key, rec = record(attempts=1 << 100000, created_at='x'*100000)
    records = {key: rec, 'x'*100000: rec, 42: rec, rid('crc', 'bad'): None}
    result = project(records)
    assert result['total'] == 4 and len({r['row_ref'] for r in result['rows']}) == 4
    assert len(json.dumps(result)) < 10000
    assert sum(r['client_receipt_id'] is None for r in result['rows']) == 3
    valid = next(r for r in result['rows'] if r['client_receipt_id'])
    assert valid['attempts'] is None and valid['created_at'] is None
    assert project(records)['snapshot'] == result['snapshot']


def test_capacity_and_empty_inventory():
    from errors import Unavailable
    rows = dict(record(n) for n in range(5000))
    answer = project(rows, limit='100')
    assert answer['total'] == 5000 and len(answer['rows']) == 100
    rows.update([record(5000)])
    with pytest.raises(Unavailable, match='capacity'):
        project(rows)
    assert project({})['rows'] == [] and project({})['next_cursor'] is None


@pytest.mark.parametrize('status', ['sent', 'delivery_reviewed', 'withdrawn', 'withdrawn_after_send', 'delivery_uncertain'])
def test_acceptance_is_record_presence_not_provider_validation(status):
    key, rec = record(status=status, sent_at='2026-10-31T23:05:00Z', delivery_review={'private': secrets.token_hex(20)})
    private = object()  # Must not inspect/copy/serialize private body or provider ID.
    envelope = {'schema':'receipt-resend-v1', 'request':{'client_receipt_id':key,'body':private},
                'acceptance':{'accepted_at':'2026-10-31T23:06:00Z','provider_message_id':private}}
    answer = project({key:rec}, {key:envelope})
    row, = answer['rows']
    assert row['acceptance_record'] == 'present'
    assert row['acceptance_recorded_at'] == '2026-10-31T23:06:00Z'
    assert row['legacy_sent_at'] == '2026-10-31T23:05:00Z'
    assert row['manual_delivery_review_recorded'] is True
    assert 'acceptance_state_mismatch' not in row['issues']
    assert answer['acceptance_record_validated'] is False


@pytest.mark.parametrize('envelope', [None, [], {}, {'schema':'unknown'},
    {'schema':'receipt-resend-v1','request':{},'acceptance':None}])
def test_unsupported_envelope_is_unknown(envelope):
    key, rec = record(status='sent', sent_at='2026-10-31T23:05:00Z')
    row, = project({key:rec}, {key:envelope})['rows']
    assert row['acceptance_record'] == 'unknown'
    assert row['acceptance_recorded_at'] is None


def test_legacy_sent_is_not_modern_acceptance_and_bad_clock_is_visible():
    key, rec = record(status='sent', sent_at='2026-10-31T23:05:00Z', created_at='2026-11-01T01:30:00-07:00')
    row, = project({key:rec}, now=datetime(2026,11,1))['rows']
    assert row['acceptance_record'] == 'absent' and row['acceptance_recorded_at'] is None
    assert row['created_at'] == '2026-11-01T08:30:00Z' and row['creation_age_seconds'] is None


def test_detached_snapshot_remains_consistent_then_old_cursor_refuses_change():
    from receipt_backlog import snapshot_rows, page
    from errors import Conflict
    records = dict(record(n) for n in range(3))
    detached = snapshot_rows(records, {})
    first = page(detached, NOW, limit='1')
    for rec in records.values():
        rec['status'] = 'withdrawn'
    assert page(detached, NOW, limit='1') == first
    with pytest.raises(Conflict):
        project(records, cursor=first['next_cursor'])


@pytest.mark.parametrize('value', ['2026-10-31', '2026-10-31T00:00:00', 'private', True, [], '9999-12-31T23:59:59-01:00'])
def test_bad_recorded_times_are_unknown_and_private(value):
    key, rec = record(created_at=value, claimed_at=value, last_attempt_at=value, withdrawn_at=value)
    row, = project({key:rec})['rows']
    assert row['created_at'] is None and row['creation_age_seconds'] is None
    assert row['claimed_at'] is None and row['withdrawn_at'] is None
    assert 'private' not in json.dumps(row)


def test_subsecond_order_is_chronological_and_dst_age_is_elapsed():
    records = dict([record(0, created_at='2026-11-01T00:00:00.1Z'), record(1, created_at='2026-11-01T00:00:00Z')])
    answer = project(records, now=NOW + timedelta(seconds=1))
    assert [r['client_receipt_id'] for r in answer['rows']] == [rid('crc',1), rid('crc',0)]
    key, rec = record(created_at='2026-11-01T01:30:00-07:00')
    row, = project({key:rec}, now=datetime.fromisoformat('2026-11-01T01:30:00-08:00'))['rows']
    assert row['creation_age_seconds'] == 3600


@pytest.mark.parametrize('slot', [[], False, {}, {'accepted_at':'bad'}, {'accepted_at':None}])
def test_malformed_acceptance_slot_is_unknown(slot):
    key, rec = record()
    env = {'schema':'receipt-resend-v1','request':{'client_receipt_id':key},'acceptance':slot}
    row, = project({key:rec}, {key:env})['rows']
    assert row['acceptance_record'] == 'unknown' and row['acceptance_recorded_at'] is None


def test_valid_absent_slot_identity_mismatch_and_attempt_bounds():
    key, rec = record(attempts=2147483647)
    env = {'schema':'receipt-resend-v1','request':{'client_receipt_id':key},'acceptance':None}
    row, = project({key:rec},{key:env})['rows']
    assert row['acceptance_record'] == 'absent' and row['attempts'] == 2147483647
    rec['client_receipt_id'] = rid('crc', 'other')
    row, = project({key:rec},{key:env})['rows']
    assert row['client_receipt_id'] is None and row['invoice_id'] is None
    assert row['acceptance_record'] == 'unknown'


def test_maximal_page_is_bounded_and_collection_shape_refuses():
    from errors import Unavailable
    rows = dict(record(n, status=[], entity=[], paid_on=[], attempts=True, created_at=[],
        claimed_at=[], last_attempt_at=[], sent_at=[], withdrawn_at=[], delivery_review=False) for n in range(100))
    answer = project(rows, limit='100')
    assert len(json.dumps(answer).encode()) < 262144 and len(answer['rows']) == 100
    with pytest.raises(Unavailable):
        project([])
    with pytest.raises(Unavailable):
        project({}, envelopes=[])


def test_actual_send_records_finance_acceptance_time_and_restart_is_read_only(tmp_path, monkeypatch):
    from helpers import rid as request_id
    from test_service_sow import legal, draft, decide
    from ports import ReceiptDeliveryOutcome
    import receipt_resend_adapter as bridge
    h = Harness(data_dir=str(tmp_path/'data')).ready()
    h.svc.ports.legal = legal()
    invoice = h.ok(decide(h, draft(h)))['invoice']
    h.receive('zbm','1010',invoice['total'],invoice['invoice_id'])
    rec, = h.svc.db['client_receipts'].values()
    h.clock.at = datetime.now(timezone.utc)
    h.ok(h.client.put('/fin/v1/clients/'+rec['client_id']+'/billing-contact',
        json={'request_id':request_id(),'email':'owned-backlog@example.test'}, headers=h.headers(andre=ANDRE_TOKEN)))
    h.svc.ports.client_mail = bridge.ResendReceiptMail(bridge.profile('owned','revision','Owned <from@example.test>'),secrets.token_hex(32),1)
    calls = []
    private = 'owned-' + secrets.token_hex(16)
    def accepted(self, request, now):
        calls.append(request)
        h.clock.advance(seconds=1)
        return ReceiptDeliveryOutcome('accepted',private)
    monkeypatch.setattr(bridge.ResendReceiptTransport,'send_frozen',accepted)
    sent = h.ok(h.post('/fin/v1/client-receipts/'+rec['client_receipt_id']+'/send',
                      {'request_id':request_id()}, caller='scheduler'))
    assert sent['sent'] and len(calls) == 1
    before = copy.deepcopy(h.svc.db), copy.deepcopy(h.ledger.events)
    first = h.ok(h.get(PATH, caller=None, andre=ANDRE_TOKEN))
    row, = first['rows']
    assert row['acceptance_record'] == 'present' and row['acceptance_recorded_at'] == h.clock.at.replace(microsecond=0).isoformat().replace('+00:00','Z')
    assert first['delivery_verified'] is False and private not in json.dumps(first)
    assert (h.svc.db,h.ledger.events) == before and len(calls) == 1
    h2 = Harness(clock=h.clock, data_dir=h.settings.data_dir, ledger=h.ledger, fakes=h.f)
    events = copy.deepcopy(h.ledger.events)
    assert h2.ok(h2.get(PATH,caller=None,andre=ANDRE_TOKEN)) == first
    assert h.ledger.events == events and len(calls) == 1


def test_http_snapshot_releases_money_lock_before_page_and_mutation_invalidates_cursor(monkeypatch):
    import threading
    import receipt_backlog
    from helpers import rid as request_id
    h = Harness().ready()
    h.fund_campaign(campaign='one')
    h.fund_campaign(campaign='two')
    entered, release = threading.Event(), threading.Event()
    original = receipt_backlog.page
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(receipt_backlog,'page',paused)
    result = []
    thread = threading.Thread(target=lambda: result.append(h.get(PATH,caller=None,andre=ANDRE_TOKEN,limit='1')))
    thread.start()
    try:
        assert entered.wait(5)
        assert h.svc.lock.acquire(timeout=1)
        h.svc.lock.release()
        rec = next(iter(h.svc.db['client_receipts'].values()))
        # Existing normal send path changes a projected status/attempt; the view invokes no sender.
        answer = h.post('/fin/v1/client-receipts/'+rec['client_receipt_id']+'/send',
                        {'request_id':request_id()},caller='scheduler')
        assert answer.status_code == 200
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    first = h.ok(result[0])
    assert first['rows'][0]['status'] == 'pending_send'
    monkeypatch.setattr(receipt_backlog,'page',original)
    assert h.get(PATH,caller=None,andre=ANDRE_TOKEN,cursor=first['next_cursor']).status_code == 409
