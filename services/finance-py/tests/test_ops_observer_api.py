"""Exact auth and owned service lifecycle. No external network."""
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import config
import health_poll
from helpers import Harness, base_env, rid
from service import rid as opaque_id
from errors import Unavailable

PATH = "/fin/v1/ops/observations/run"


def harness(tmp_path, **settings):
    return Harness(data_dir=str(tmp_path / 'finance'), env={"FIN_OPS_OBSERVER_ENABLED": "1", **settings})


def run(h):
    return h.post(PATH, {"request_id": rid()}, caller="ops_alerts")


def test_default_disabled_no_poll_or_state(hr, monkeypatch):
    monkeypatch.setattr(health_poll, "probe", lambda *a, **k: pytest.fail("disabled poll"))
    assert run(hr).json() == {"status": "disabled", "persisted": False}


@pytest.mark.parametrize("method,path", [("GET", PATH), ("POST", PATH + "/"), ("POST", PATH.replace("ops", "%6fps")),
    ("POST", "/fin/v1/jobs/stripe-sessions/run"), ("GET", "/fin/v1/breaks"), ("POST", "/fin/v1/invoices")])
def test_narrow_caller_refused_before_body(hr, method, path):
    r = hr.client.request(method, path, headers={**hr.headers("ops_alerts"), "Content-Type": "application/json"}, content=b"not-json", follow_redirects=False)
    assert r.status_code == 403


@pytest.mark.parametrize("caller", ["scheduler", "ops_cron", "onboarding", "bank_feed"])
def test_other_callers_cannot_observe(hr, caller):
    assert hr.post(PATH, {"request_id": rid()}, caller=caller).status_code == 403


def test_service_auth_required(hr):
    assert hr.post(PATH, {"request_id": rid()}, caller="ops_alerts", bearer=secrets.token_hex(32)).status_code == 401


def test_distinct_optional_token_and_strict_settings():
    token = secrets.token_hex(32)
    with pytest.raises(RuntimeError):
        config.load(base_env(FIN_CALLER_TOKENS=json.dumps({"ops_cron": token, "ops_alerts": token})))
    for env in ({"FIN_OPS_OBSERVER_ENABLED": "yes"}, {"FIN_OPS_OBSERVER_ENABLED": "1"},
                {"FIN_OPS_HEALTH_URLS": "private_alias=http://example.test"},
                {"FIN_OPS_HEALTH_URLS": "finance=http://secret@example.test"}):
        with pytest.raises(RuntimeError): config.load(base_env(**env))


def test_local_observation_privacy_restart_and_resolution(tmp_path):
    h = harness(tmp_path)
    canary = secrets.token_hex(32)
    key = opaque_id('brk', 'founder_alert', secrets.token_hex(16))
    h.svc.db['breaks'][key] = {'break_id': key, 'kind': 'founder_alert', 'category': 'compliance_hold',
        'leg': 'compliance_hold', 'status': 'open', 'subject': canary, 'note': canary, 'severity': canary}
    before = len(h.ledger.events)
    a = run(h); assert a.status_code == 200 and a.json()['persisted']
    assert a.json()['open_count'] == 1 and canary not in a.text
    assert a.json()['coverage']['remote:holds'] == 'UNCONFIGURED' and a.json()['status'] == 'degraded'
    raw = (tmp_path/'finance'/'ops-observer.json').read_text(); assert canary not in raw
    h.svc.db['breaks'][key]['status'] = 'resolved'
    assert run(h).json()['open_count'] == 0
    assert len(h.ledger.events) == before
    last = h.svc._ops_observer.current['preview_id']
    h.svc.close()
    h2 = Harness(data_dir=str(tmp_path/"finance"), ledger=h.ledger, env={"FIN_OPS_OBSERVER_ENABLED":"1"})
    try: assert h2.svc._ops_observer.current['preview_id'] == last
    finally: h2.svc.close()


def test_no_money_lock_during_poll_or_close_wait_and_no_stale_writer(tmp_path, monkeypatch):
    h = harness(tmp_path, FIN_OPS_HEALTH_URLS="finance=http://owned.example.test")
    entered = threading.Event(); release = threading.Event(); close_started = threading.Event()
    def blocked(name, url, **kwargs):
        entered.set(); assert release.wait(5)
        return health_poll.Reading(name, True, 'ok')
    monkeypatch.setattr(health_poll, 'probe', blocked)
    def close(): close_started.set(); h.svc.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(h.svc.observe_ops)
        assert entered.wait(5)
        closing = pool.submit(close); assert close_started.wait(5)
        try:
            assert h.svc.lock.acquire(timeout=1)
            h.svc.lock.release()
            assert not closing.done()
        finally: release.set()
        assert future.result(timeout=5)['persisted']
        closing.result(timeout=5)
    before = (tmp_path/'finance'/'ops-observer.json').read_bytes()
    with pytest.raises(Unavailable): h.svc.observe_ops()
    assert (tmp_path/'finance'/'ops-observer.json').read_bytes() == before


def test_serial_observations_have_unique_generations(tmp_path):
    h = harness(tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda _: h.svc.observe_ops(), range(8)))
        assert sorted(r['generation'] for r in rows) == list(range(1, 9))
        assert all(r['persisted'] for r in rows)
        assert h.svc._ops_observer.current['generation'] == 8
    finally: h.svc.close()


def test_health_unknown_removed_and_known_recovery(tmp_path, monkeypatch):
    h = harness(tmp_path, FIN_OPS_HEALTH_URLS='finance=http://owned.example.test')
    code = ['maintenance']
    monkeypatch.setattr(health_poll, 'probe', lambda name, url, **k: health_poll.Reading(name, code[0] == 'ok', code[0]))
    try:
        assert run(h).json()['open_count'] == 1
        code[0] = 'unreachable'
        r = run(h).json()
        assert r['coverage']['health:finance'] == 'UNKNOWN' and r['open_count'] == 2
        h.settings.ops_health_targets = {}
        r = run(h).json()
        assert r['coverage']['health:finance'] == 'UNCONFIGURED' and r['stale_count'] == 2
        h.settings.ops_health_targets = {'finance':'http://owned.example.test'}
        code[0] = 'ok'
        r = run(h).json()
        assert r['coverage']['health:finance'] == 'COMPLETE' and r['open_count'] == 0
    finally: h.svc.close()


@pytest.mark.parametrize('field', ['status', 'reasons', 'degraded_reasons', 'warnings'])
def test_actual_classifier_private_canary_never_persists(tmp_path, monkeypatch, field):
    import httpx
    canary = 'private_' + secrets.token_hex(16)
    body = {'status':'ok', field:canary if field == 'status' else [canary]}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=body))
    real = health_poll.probe
    monkeypatch.setattr(health_poll, 'probe', lambda name, url, **k: real(name, url, transport=transport, **k))
    h = harness(tmp_path, FIN_OPS_HEALTH_URLS='finance=http://owned.example.test')
    try:
        r = run(h)
        assert r.status_code == 200 and r.json()['persisted']
        assert r.json()['coverage']['health:finance'] == 'UNKNOWN'
        assert canary not in r.text + (tmp_path/'finance'/'ops-observer.json').read_text()
    finally: h.svc.close()


def test_all_other_routes_refuse_ops_alerts(hr):
    import re
    checked = 0
    for route in hr.app.routes:
        path = getattr(route, 'path', '')
        if not path.startswith('/fin/') or path == PATH: continue
        path = re.sub(r'\{[^}]+\}', 'owned', path)
        for method in getattr(route, 'methods', ()):
            if method in ('HEAD', 'OPTIONS'): continue
            r = hr.client.request(method, path, headers=hr.headers('ops_alerts'), json={'request_id':rid()})
            assert r.status_code == 403, (method, path)
            checked += 1
    assert checked > 50
