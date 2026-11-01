"""Persisted health history is untrusted; bad input must not hide service degradation."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import health_poll as HP

NOW = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize('bad', [None, 1, True, 'invalid', {'x': 'y'}])
def test_bad_history_container_recovers_on_next_reading(bad):
    state = {'health_hist': {'finance': bad}}
    assert HP.track(state, [HP.Reading('finance', True, 'ok')], NOW) == []
    assert state['health_hist']['finance'] == [[NOW.isoformat(), True]]


@pytest.mark.parametrize('field', ['health', 'health_hist'])
def test_naive_persisted_time_is_discarded(field):
    old = '2026-10-31T23:00:00'
    state = {field: {'finance': old if field == 'health' else [[old, False]]}}
    assert HP.track(state, [HP.Reading('finance', False, 'maintenance')], NOW) == []
    assert state['health']['finance'] == NOW.isoformat()


def test_future_history_after_clock_rollback_does_not_trigger_false_flapping():
    future = (NOW + timedelta(hours=1)).isoformat()
    state = {'health_hist': {'finance': [[future, False], [future, False]]}, 'health': {'finance': future}}
    assert HP.track(state, [HP.Reading('finance', False, 'maintenance')], NOW) == []
    assert state['health']['finance'] == NOW.isoformat()
    assert state['health_hist']['finance'] == [[NOW.isoformat(), False]]
    assert HP.track(state, [HP.Reading('finance', False, 'maintenance')], NOW + timedelta(minutes=10)) == [
        ('service_unhealthy', 'finance', 'maintenance')]


@pytest.mark.parametrize('url', ['http://finance.invalid:abc', 'http://finance.invalid:65536', 'http://finance.invalid:0'])
def test_invalid_port_is_a_configuration_alert(url):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'status': 'ok'}))
    result = HP.run({}, {'FIN_OPS_HEALTH_URLS': 'finance=' + url}, NOW, transport)
    assert result == [('health_poll_unconfigured', 'ops-alerts', 'malformed')]


@pytest.mark.parametrize('value', [0, 1, 'false', 'true', None, [], {}])
def test_malformed_write_ok_is_not_a_healthy_reading(value):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'status': 'ok', 'write_ok': value}))
    reading = HP.probe('finance', 'http://finance.test', transport)
    assert reading.ok is False and reading.code == 'bad_write_ok'


def test_boolean_write_ok_and_absent_legacy_field_have_explicit_results():
    for extra, expected in [({}, True), ({'write_ok': True}, True), ({'write_ok': False}, False)]:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'status': 'ok', **extra}))
        assert HP.probe('finance', 'http://finance.test', transport).ok is expected


def test_oversized_response_is_refused_before_consuming_the_full_stream():
    consumed = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(128):
                consumed.append(1)
                yield b' ' * 8192
            yield b'{"status":"ok"}'

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Body()))
    reading = HP.probe('finance', 'http://finance.test', transport)
    assert reading == HP.Reading('finance', False, 'response_too_large')
    assert len(consumed) <= 9


def test_encoded_response_is_refused_without_decompression():
    import gzip
    compressed = gzip.compress(b'{"status":"ok"}')
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={'Content-Encoding': 'gzip'}, content=compressed))
    assert HP.probe('finance', 'http://finance.test', transport) == HP.Reading('finance', False, 'bad_encoding')


def test_excessive_configured_targets_are_refused_before_any_request():
    requests = []
    transport = httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, json={'status': 'ok'}))
    targets = ','.join(f'service{i}=http://service{i}.test' for i in range(65))
    assert HP.run({}, {'FIN_OPS_HEALTH_URLS': targets}, NOW, transport) == [
        ('health_poll_unconfigured', 'ops-alerts', 'malformed')]
    assert requests == []


def test_stream_past_probe_budget_is_refused(monkeypatch):
    import time
    clock = [0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])

    class SlowBody(httpx.SyncByteStream):
        def __iter__(self):
            clock[0] = 6
            yield b'{"status":"ok"}'

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowBody()))
    assert HP.probe('finance', 'http://finance.test', transport) == HP.Reading('finance', False, 'deadline_exceeded')


def test_poll_stops_starting_requests_after_aggregate_budget(monkeypatch):
    import time
    clock = [0]
    calls = []
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])

    def handle(request):
        calls.append(request)
        clock[0] += 10
        return httpx.Response(200, json={'status': 'ok'})

    readings = HP.poll({f'service{i}': f'http://service{i}.test' for i in range(8)}, httpx.MockTransport(handle))
    assert len(calls) <= 3
    assert len(readings) == 8 and all(not reading.ok for reading in readings)
    assert any(reading.code == 'poll_budget_exhausted' for reading in readings)


@pytest.mark.parametrize('value', [{}, None, 0, False, ''])
def test_malformed_warning_container_never_reports_healthy(value):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'status': 'ok', 'warnings': value}))
    assert HP.probe('finance', 'http://finance.test', transport) == HP.Reading('finance', False, 'bad_warning')


@pytest.mark.parametrize('field', ['reasons', 'degraded_reasons'])
def test_contradictory_ok_status_with_degradation_reason_is_unhealthy(field):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={'status': 'ok', field: ['maintenance: private detail']}))
    assert HP.probe('finance', 'http://finance.test', transport) == HP.Reading('finance', False, 'maintenance')

@pytest.mark.parametrize('field', ['reasons', 'degraded_reasons'])
@pytest.mark.parametrize('value', [False, None, {}, '', 0, ['maintenance'] * 17])
def test_malformed_or_truncated_reasons_are_unknown(field, value):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'status':'ok', field:value}))
    assert HP.probe('finance', 'http://finance.test', transport).code == 'bad_reason'


def test_both_reason_arrays_classified_without_silent_preference():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'status':'ok', 'reasons':['maintenance'], 'degraded_reasons':['log_write_fault']}))
    assert HP.probe('finance', 'http://finance.test', transport).code == 'log_write_fault,maintenance'


def test_excessive_warnings_are_unknown():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'status':'ok','warnings':['ledger_anchor_unkeyed'] * 9}))
    assert HP.probe('finance', 'http://finance.test', transport).code == 'bad_warning'

@pytest.mark.parametrize('body,expected', [
    ({'status':'degraded','reasons':['maintenance'],'warnings':False}, 'bad_warning'),
    ({'status':'ok','reasons':['maintenance'],'write_ok':'private'}, 'bad_write_ok'),
    ({'status':None}, 'bad_status'),
    ({'status':'degraded','reasons':['maintenance'],'warnings':['private_code']}, 'bad_warning'),
    ({'status':'ok','write_ok':False,'warnings':False}, 'bad_warning'),
    ({'status':'maintenance','degraded_reasons':[None]}, 'bad_reason'),
    ({'status':1,'reasons':['maintenance']}, 'bad_status'),
])
def test_all_recognized_fields_validate_before_definitive_result(body, expected):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    assert HP.probe('finance', 'http://finance.test', transport).code == expected


def test_non_json_error_response_is_unknown_feed():
    transport = httpx.MockTransport(lambda request: httpx.Response(503, content=b'not-json'))
    assert HP.probe('finance', 'http://finance.test', transport).code == 'bad_body'

@pytest.mark.parametrize('field', ['log_write_fault', 'reconcile_required', 'reconcile_mode'])
@pytest.mark.parametrize('value', [0, 1, 'false', None, [], {}])
def test_integrity_flags_are_strict_booleans_before_reason_selection(field, value):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'status':'ok', 'reasons':['maintenance'], field:value}))
    assert HP.probe('finance', 'http://finance.test', transport).code == 'bad_body'


def test_integrity_flags_cannot_be_hidden_by_known_reasons():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'status':'ok','reasons':['maintenance'],'log_write_fault':True,'reconcile_mode':True}))
    reading = HP.probe('finance', 'http://finance.test', transport)
    assert not reading.ok and set(reading.code.split(',')) == {'maintenance','log_write_fault','reconcile_mode'}


def test_legal_optional_warning_discards_caller_canary():
    import secrets
    canary = 'owned_' + secrets.token_hex(16)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'status':'warning','warnings':['optional_caller_unset:' + canary]}))
    r = HP.probe('legal', 'http://legal.test', transport)
    assert r == HP.Reading('legal', False, 'warning:optional_caller_unset')
    assert canary not in repr(r)
