"""Owned simulated Stripe truth; no provider calls or credentials from the environment."""
import json
import secrets
from dataclasses import replace

import pytest

from clock import FixedClock
from helpers import ANDRE_TOKEN, NOW, Harness, rid
from stripe_incoming import StripeIncoming
from stripe_sim import SimStripe


@pytest.fixture
def world(tmp_path):
    key, secret = 'sk_test_' + secrets.token_hex(24), 'whsec_' + secrets.token_hex(24)
    clock = FixedClock(NOW)
    sim = SimStripe(clock, key=key, whsec=secret)
    port = StripeIncoming(key, secret, False, 'https://example.invalid/ok', 'https://example.invalid/no',
                          transport=sim.transport(), clock=clock)
    keyfile, secretfile = tmp_path / 'key', tmp_path / 'secret'
    keyfile.write_text(key)
    secretfile.write_text(secret)
    keyfile.chmod(0o600)
    secretfile.chmod(0o600)
    env = {'FIN_STRIPE_INCOMING': '1', 'FIN_STRIPE_SECRET_KEY_FILE': str(keyfile),
           'FIN_STRIPE_WEBHOOK_SECRET_FILE': str(secretfile),
           'FIN_STRIPE_SUCCESS_URL': 'https://example.invalid/ok',
           'FIN_STRIPE_CANCEL_URL': 'https://example.invalid/no'}
    h = Harness(env=env, clock=clock, stripe_in=port, data_dir=str(tmp_path / 'data')).ready()
    sid, pi = 'cs_test_' + secrets.token_hex(12), 'pi_' + secrets.token_hex(12)
    sim.sessions[sid] = {'id': sid, 'object': 'checkout.session', 'status': 'complete',
                         'payment_intent': pi, 'livemode': False}
    sim.pis[pi] = {'id': pi, 'object': 'payment_intent', 'status': 'processing', 'amount_received': 0,
                   'currency': 'usd', 'metadata': {}, 'livemode': False, 'latest_charge': None}
    return h, sim, sid, pi


def event(world, **kwargs):
    h, sim, sid, _ = world
    payload, signature = sim.event('checkout.session.completed', {'id': sid, 'object': 'checkout.session'})
    return {'request_id': rid(), 'payload': payload, 'signature': signature, **kwargs}


def send(world, body=None, **kwargs):
    return world[0].post('/fin/v1/stripe/events', body or event(world), caller='rail_gateway', **kwargs)


def anomaly(h):
    return [b for b in h.svc.db['breaks'].values() if b['leg'] == 'stripe_unknown_session']


def test_unknown_processing_is_durable_private_and_not_money(world):
    h, sim, sid, pi = world
    before = len(h.svc.entries)
    assert send(world).json()['status'] == 'unknown_session_break'
    b, = anomaly(h)
    assert b['payment_state'] == 'processing' and b['amount_unassessed'] is True
    assert b['difference'] == '0.00' and b['severity'] == 'high' and b['status'] == 'open'
    assert len(h.svc.entries) == before and not h.svc.db['receipts']
    assert sid not in json.dumps(b) and pi not in json.dumps(b)
    audit = [e for e in h.ledger.events if e.get('event_type') == 'break_opened']
    assert audit and sid not in json.dumps(audit) and pi not in json.dumps(audit)
    shared = h.svc.list_breaks(full=False)['breaks']
    assert not any('payment_ref' in x or 'session_ref' in x for x in shared)
    h2 = Harness(env=h.env, clock=h.clock, stripe_in=h.svc.ports.stripe_in, data_dir=h.settings.data_dir,
                 ledger=h.ledger, fakes=h.f)
    assert anomaly(h2) == [b]


def test_duplicate_and_processing_to_success_never_regresses(world):
    h, sim, sid, pi = world
    body = event(world)
    assert send(world, body).status_code == 200
    assert send(world, body).json()['status'] == 'duplicate'
    assert send(world).status_code == 200 and len(anomaly(h)) == 1
    sim.pis[pi].update(status='succeeded', amount_received=5000, currency='eur')
    assert send(world).status_code == 200
    b, = anomaly(h)
    assert b['payment_state'] == 'succeeded' and b['difference'] == '0.00'
    sim.pis[pi]['status'] = 'processing'
    assert send(world).status_code == 200
    assert anomaly(h)[0]['payment_state'] == 'succeeded'


@pytest.mark.parametrize('state', ['requires_payment_method', 'canceled', 'requires_action'])
def test_unpaid_truth_does_not_open_anomaly(world, state):
    h, sim, _, pi = world
    sim.pis[pi]['status'] = state
    assert send(world).json()['status'] == 'unknown_session'
    assert not anomaly(h)


@pytest.mark.parametrize('missing', ['session', 'payment'])
def test_not_found_truth_does_not_invent_payment(world, missing):
    h, sim, sid, pi = world
    (sim.sessions if missing == 'session' else sim.pis).pop(sid if missing == 'session' else pi)
    assert send(world).json()['status'] == 'unknown_session'
    assert not anomaly(h)


def test_unavailable_truth_and_bad_signature_are_retryable_without_break(world):
    h, sim, _, _ = world
    body = event(world)
    sim.fail_next = [503] * 10
    assert send(world, body).status_code == 503 and not anomaly(h)
    sim.fail_next.clear()
    assert send(world, {**body, 'signature': 'invalid'}).status_code == 409
    assert not anomaly(h)
    assert send(world, body).json()['status'] == 'unknown_session_break'


@pytest.mark.parametrize('defect', ['payment_id', 'session_id', 'oversize', 'mode'])
def test_inconsistent_or_unbounded_truth_is_not_acknowledged(world, monkeypatch, defect):
    h, _, _, _ = world
    port = h.svc.ports.stripe_in
    original = port.payment if defect in ('payment_id', 'mode') else port.session
    def altered(ref):
        value = original(ref)
        if defect == 'payment_id': return replace(value, payment_intent='pi_other')
        if defect == 'mode': return replace(value, livemode=True)
        return replace(value, session_id='cs_other' if defect == 'session_id' else 'cs_' + 'x' * 500)
    monkeypatch.setattr(port, 'payment' if defect in ('payment_id', 'mode') else 'session', altered)
    assert send(world).status_code in (400, 409, 422, 503)
    assert not anomaly(h) and not h.svc.db['stripe_events']


def test_ledger_failure_does_not_consume_event(world):
    h, _, _, _ = world
    body = event(world)
    h.ledger.fail_on_type = 'break_opened'
    assert send(world, body).status_code == 503
    assert not anomaly(h) and not h.svc.db['stripe_events']
    h.ledger.fail_on_type = None
    assert send(world, body).json()['status'] == 'unknown_session_break'


def test_founder_evidence_resolution_and_new_success_reopens_only_once(world):
    h, sim, _, pi = world
    assert send(world).status_code == 200
    b, = anomaly(h)
    path = '/fin/v1/breaks/' + b['break_id'] + '/resolution'
    body = {'request_id': rid(), 'explanation_code': 'unknown',
            'evidence': [{'ref': 'external-review', 'sha256': 'a' * 64}]}
    assert h.post(path, body, caller='scheduler').status_code == 403
    assert h.post(path, {**body, 'request_id': rid(), 'evidence': []}, andre=ANDRE_TOKEN).status_code == 422
    assert h.post(path, body, andre=ANDRE_TOKEN).status_code == 200
    assert anomaly(h)[0]['status'] == 'resolved'
    assert send(world).status_code == 200 and anomaly(h)[0]['status'] == 'resolved'
    sim.pis[pi]['status'] = 'succeeded'
    assert send(world).status_code == 200 and anomaly(h)[0]['status'] == 'open'
    assert h.post(path, {**body, 'request_id': rid()}, andre=ANDRE_TOKEN).status_code == 200
    sim.pis[pi]['status'] = 'processing'
    assert send(world).status_code == 200 and anomaly(h)[0]['status'] == 'resolved'


def test_local_store_failure_does_not_consume_event(world, monkeypatch):
    from store import StoreWriteError
    h, _, _, _ = world
    body = event(world)
    with monkeypatch.context() as m:
        def refused(*args):
            raise StoreWriteError('owned failure')
        m.setattr(h.svc.log, 'append_prepared', refused)
        assert send(world, body).status_code == 503
        assert not anomaly(h) and not h.svc.db['stripe_events']
    assert send(world, body).json()['status'] == 'unknown_session_break'


@pytest.mark.parametrize('extra', [{'entry_id': 'unrelated-entry'}, {'clears_by': '2026-10-03'},
                                   {'explanation_code': 'timing_in_transit'}])
def test_generic_resolution_cannot_claim_money_recovered(world, extra):
    h, _, _, _ = world
    assert send(world).status_code == 200
    b, = anomaly(h)
    body = {'request_id': rid(), 'explanation_code': 'unknown',
            'evidence': [{'ref': 'external-review', 'sha256': 'a' * 64}], **extra}
    response = h.post('/fin/v1/breaks/' + b['break_id'] + '/resolution', body, andre=ANDRE_TOKEN)
    assert response.status_code == 422
    assert anomaly(h)[0]['status'] == 'open'


def test_existing_session_keeps_normal_dispatch(world, monkeypatch):
    h, _, sid, _ = world
    h.svc.db['stripe_sessions'][sid] = {'session_id': sid, 'status': 'complete'}
    calls = []
    def payment(op, truth):
        calls.append(truth)
        return 'existing_payment_dispatch'
    monkeypatch.setattr(h.svc, '_stripe_payment', payment)
    assert send(world).json()['status'] == 'existing_payment_dispatch'
    assert len(calls) == 1 and not anomaly(h)


def test_session_cannot_change_payment_identity(world):
    h, sim, sid, pi = world
    assert send(world).status_code == 200
    other = 'pi_' + secrets.token_hex(12)
    sim.pis[other] = {**sim.pis[pi], 'id': other}
    sim.sessions[sid]['payment_intent'] = other
    before = dict(anomaly(h)[0])
    assert send(world).status_code == 422
    assert anomaly(h) == [before]
