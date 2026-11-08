"""Unpaid service cancellation only; owned fixtures, no provider calls."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from helpers import ANDRE_TOKEN, Harness, rid
from test_service_sow import decide, draft, legal


@pytest.fixture
def world(tmp_path):
    h = Harness(data_dir=str(tmp_path / 'finance')).ready()
    h.svc.ports.legal = legal()
    invoice = h.ok(decide(h, draft(h)))['invoice']
    return h, invoice


def void(world, request_id=None, **over):
    h, inv = world
    return h.post('/fin/v1/invoices/' + inv['invoice_id'] + '/void',
                  {'request_id': request_id or rid(), 'content_sha256': inv['content_sha256'],
                   'reason': 'issued_in_error', **over}, andre=ANDRE_TOKEN)


def test_void_exact_original_inverse_no_cash_and_terminal(world):
    h, inv = world
    original = copy.deepcopy(h.svc.entries[-1])
    before = dict(h.svc.balances)
    result = void(world)
    assert result.status_code == 200, result.text
    after = result.json()['invoice']
    assert after['status'] == 'void'
    assert all(after[k] == v for k, v in inv.items() if k != 'status')
    assert h.svc.entries[-2] == original
    reversal = h.svc.entries[-1]
    assert reversal['memo_code'] == 'F11v' and reversal['reverses_entry_id'] == original['entry_id']
    assert reversal['lines'] == [
        {'account': '1100', 'subledger': 'client:' + inv['client_id'], 'debit': '0.00', 'credit': '100.00'},
        {'account': '4110', 'subledger': None, 'debit': '100.00', 'credit': '0.00'}]
    for key, value in before.items():
        if key[1] not in ('1100', '4110'):
            assert h.svc.balances[key] == value
    assert h.svc.balances[('zbm', '1100', 'client:' + inv['client_id'])] == 0
    assert h.svc.balances[('zbm', '4110', None)] == 0
    assert void(world).status_code == 409
    assert decide(h, inv).status_code == 409


@pytest.mark.parametrize('caller', [None, 'scheduler', 'onboarding', 'seo_02'])
def test_founder_only(world, caller):
    h, inv = world
    count = len(h.svc.entries)
    result = h.post('/fin/v1/invoices/' + inv['invoice_id'] + '/void',
                    {'request_id': rid(), 'content_sha256': inv['content_sha256'], 'reason': 'issued_in_error'},
                    caller=caller)
    assert result.status_code == 403
    assert len(h.svc.entries) == count


@pytest.mark.parametrize('over,expected', [({'content_sha256': '0'*64}, 409),
    ({'reason': 'arbitrary private text'}, 422), ({'amount': '100.00'}, 422)])
def test_strict_request_and_content_binding(world, over, expected):
    h, _ = world
    before = len(h.svc.entries)
    assert void(world, **over).status_code == expected
    assert len(h.svc.entries) == before


@pytest.mark.parametrize('change', [
    {'status': 'draft'}, {'status': 'paid'}, {'status': 'returned'}, {'kind': 'retainer'},
    {'kind': 'media_prepayment'}, {'entity': 'zbc'}, {'currency': 'EUR'},
    {'held_compliance': {'held': True}}, {'held_compliance_history': ['owned']},
    {'paid_at': '2026-10-02'}, {'refunded': '1.00'}, {'charged_back': True},
])
def test_noneligible_invoice_refuses_without_reversal(world, change):
    h, inv = world
    h.svc.db['invoices'][inv['invoice_id']].update(change)
    before = len(h.svc.entries)
    assert void(world).status_code == 409
    assert len(h.svc.entries) == before


@pytest.mark.parametrize('status', ['open', 'expired', 'processing', 'complete', 'unknown'])
def test_any_checkout_history_refuses(world, status):
    h, inv = world
    h.svc.db['stripe_sessions']['owned'] = {'invoice_id': inv['invoice_id'], 'status': status}
    before = len(h.svc.entries)
    assert void(world).status_code == 409
    assert len(h.svc.entries) == before


@pytest.mark.parametrize('amount', ['50.00', '100.00'])
def test_genuine_bank_history_including_unapplied_reference_refuses(world, amount):
    h, inv = world
    h.receive('zbm', '1010', amount, inv['invoice_id'])
    rec, = h.svc.db['receipts'].values()
    if amount == '50.00':
        assert rec['invoice_id'] is None and rec['reference_token'] == inv['invoice_id']
    before = len(h.svc.entries)
    assert void(world).status_code == 409
    assert len(h.svc.entries) == before


@pytest.mark.parametrize('linked', [True, False])
def test_genuine_correction_refuses_even_without_reversal_link(world, linked):
    h, inv = world
    original = h.svc.entries[-1]
    body = {'request_id': rid(), 'effective_date': '2026-10-02'}
    if linked:
        body['reverses_entry_id'] = original['entry_id']
    else:
        body['lines'] = [{'account': '4110', 'debit': '10.00', 'credit': '0.00'},
                         {'account': '1100', 'subledger': 'client:' + inv['client_id'],
                          'debit': '0.00', 'credit': '10.00'}]
    h.ok(h.post('/fin/v1/journal/zbm/corrections', body, andre=ANDRE_TOKEN), 201)
    before = len(h.svc.entries)
    assert void(world).status_code == 409
    assert len(h.svc.entries) == before


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'amount', 'client'])
def test_original_posting_ambiguity_refuses(world, damage):
    h, _ = world
    if damage == 'missing':
        h.svc.entries.clear()
    elif damage == 'duplicate':
        h.svc.entries.append(copy.deepcopy(h.svc.entries[-1]))
    elif damage == 'amount':
        h.svc.entries[-1]['lines'][0]['debit'] = '90.00'
        h.svc.entries[-1]['lines'][1]['credit'] = '90.00'
    else:
        h.svc.entries[-1]['lines'][0]['subledger'] = 'client:other'
    before = len(h.svc.entries)
    assert void(world).status_code == 409
    assert len(h.svc.entries) == before


def test_replay_restart_and_expired_cache_never_reverse_twice(world):
    h, inv = world
    request = rid()
    first = void(world, request)
    assert first.status_code == 200
    assert void(world, request).json() == first.json()
    count = len(h.svc.entries)
    h2 = Harness(data_dir=h.settings.data_dir, clock=h.clock, ledger=h.ledger, fakes=h.f)
    assert void((h2, inv), request).json() == first.json()
    h2.clock.advance(minutes=16)
    assert void((h2, inv), request).status_code == 409
    assert len(h2.svc.entries) == count


def test_late_bank_fact_after_void_is_retained_unapplied(world):
    h, inv = world
    assert void(world).status_code == 200
    h.receive('zbm', '1010', '100.00', inv['invoice_id'])
    rec, = h.svc.db['receipts'].values()
    assert rec['status'] == 'unapplied' and rec['invoice_id'] is None
    assert h.svc.db['invoices'][inv['invoice_id']]['status'] == 'void'
    assert len([e for e in h.svc.entries if e['memo_code'] == 'F11v']) == 1


def test_two_competing_voids_commit_once(world):
    h, inv = world
    barrier = threading.Barrier(2)
    def call():
        barrier.wait(timeout=10)
        return void(world).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call) for _ in range(2)]
        assert sorted(f.result(timeout=10) for f in futures) == [200, 409]
    assert len([e for e in h.svc.entries if e['memo_code'] == 'F11v']) == 1


@pytest.mark.parametrize('fault', ['ledger', 'log'])
def test_failed_commit_preserves_issued_and_retry_converges(world, fault):
    h, inv = world
    request = rid()
    if fault == 'ledger':
        h.ledger.fail_all = True
    else:
        h.svc.log.fail_next_append = True
    assert void(world, request).status_code == 503
    assert h.svc.db['invoices'][inv['invoice_id']]['status'] == 'issued'
    assert not [e for e in h.svc.entries if e['memo_code'] == 'F11v']
    h.ledger.fail_all = False
    assert void(world, request).status_code == 200
    assert len([e for e in h.svc.entries if e['memo_code'] == 'F11v']) == 1


def test_current_locked_period_refuses(world):
    h, _ = world
    h.svc.db['locks']['zbm|2026-10'] = {'locked': True}
    assert void(world).status_code == 422
    assert not [e for e in h.svc.entries if e['memo_code'] == 'F11v']


def test_unbound_other_client_revenue_correction_is_conservatively_ambiguous(world):
    h, _ = world
    h.ok(h.post('/fin/v1/journal/zbm/corrections', {'request_id': rid(), 'effective_date': '2026-10-02',
        'lines': [{'account': '4110', 'debit': '10.00', 'credit': '0.00'},
                  {'account': '1100', 'subledger': 'client:other', 'debit': '0.00', 'credit': '10.00'}]},
        andre=ANDRE_TOKEN), 201)
    assert void(world).status_code == 409


def test_void_uses_open_current_period_without_rewriting_locked_original(world):
    h, inv = world
    original = copy.deepcopy(h.svc.entries[-1])
    h.svc.db['locks']['zbm|2026-10'] = {'locked': True}
    h.clock.advance(days=31)
    assert void(world).status_code == 200
    assert h.svc.entries[-1]['period'] == '2026-11'
    assert h.svc.entries[-2] == original


@pytest.mark.parametrize('damage', ['no_reference', 'wrong_source', 'not_inverse', 'retainer'])
def test_f11v_journal_guard_rejects_non_service_reversal(world, damage):
    from intelligences import i01_journal as J
    h, inv = world
    original = copy.deepcopy(h.svc.entries[-1])
    assert void(world).status_code == 200
    entry = copy.deepcopy(h.svc.entries[-1])
    originals = {original['entry_id']: original}
    if damage == 'no_reference':
        entry['reverses_entry_id'] = None
    elif damage == 'wrong_source':
        entry['source'] = {'kind': 'invoice', 'id': 'other'}
    elif damage == 'not_inverse':
        entry['lines'][0]['credit'] = '50.00'
        entry['lines'][1]['debit'] = '50.00'
    else:
        original['lines'][1]['account'] = '2110'
        original['lines'][1]['subledger'] = 'client:' + inv['client_id']
        entry['lines'] = J.reversal_lines(original)
    assert any(r['code'] == 'JOURNAL_INVALID' for r in J.validate(entry, set(), originals))


def test_bank_gather_paused_before_void_commits_preserves_late_fact(world, monkeypatch):
    h, inv = world
    entered, release = threading.Event(), threading.Event()
    original = h.f['compliance'].client_activation
    def paused(client):
        entered.set()
        assert release.wait(timeout=10)
        return original(client)
    monkeypatch.setattr(h.f['compliance'], 'client_activation', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(h.receive, 'zbm', '1010', '100.00', inv['invoice_id'])
        try:
            assert entered.wait(timeout=10)
            assert void(world).status_code == 200
        finally:
            release.set()
        pending.result(timeout=10)
    receipt, = h.svc.db['receipts'].values()
    assert receipt['status'] == 'unapplied' and receipt['reference_token'] == inv['invoice_id']
    assert h.svc.db['invoices'][inv['invoice_id']]['status'] == 'void'


@pytest.mark.parametrize('first', ['checkout', 'void'])
def test_checkout_turn_and_void_are_mutually_exclusive(world, monkeypatch, first):
    from contextlib import contextmanager
    from datetime import timedelta
    from clock import iso
    from ports import StripeCheckout
    h, inv = world
    h.settings.stripe_incoming = True
    entered, release, void_entered = threading.Event(), threading.Event(), threading.Event()
    class OwnedCheckout:
        calls = 0
        def create_checkout(self, invoice, total, methods, key, expires, label):
            self.calls += 1
            entered.set()
            assert release.wait(timeout=10)
            return StripeCheckout('created', 'cs_owned', 'https://checkout.stripe.com/c/pay/owned',
                                  iso(h.clock.now() + timedelta(hours=23)))
    port = OwnedCheckout()
    h.svc.ports.stripe_in = port
    original_turn = h.svc._stripe_turn
    @contextmanager
    def turn(what):
        if what == 'unpaid invoice void':
            void_entered.set()
        with original_turn(what):
            yield
    monkeypatch.setattr(h.svc, '_stripe_turn', turn)
    def checkout():
        return h.post('/fin/v1/invoices/' + inv['invoice_id'] + '/stripe-checkout',
                      {'request_id': rid()}, caller='onboarding')
    if first == 'void':
        assert void(world).status_code == 200
        assert checkout().status_code == 409
        assert port.calls == 0
        return
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(checkout)
        try:
            assert entered.wait(timeout=10)
            cancellation = pool.submit(void, world)
            assert void_entered.wait(timeout=10)
        finally:
            release.set()
        response = pending.result(timeout=10)
        assert response.status_code == 200, response.text
        assert cancellation.result(timeout=10).status_code == 409
    assert h.svc.db['invoices'][inv['invoice_id']]['status'] == 'issued'
    assert len(h.svc.db['stripe_sessions']) == 1


@pytest.mark.parametrize('resolution', [None, {'kind': 'acknowledged', 'evidence': 'owned'}])
def test_open_unassessed_checkout_break_blocks_void_even_acknowledged(world, resolution):
    h, _ = world
    h.svc.db['breaks']['owned'] = {'leg': 'stripe_unknown_session', 'status': 'open',
                                 'amount_unassessed': True, 'resolution': resolution}
    before = len(h.svc.entries)
    assert void(world).status_code == 409
    assert len(h.svc.entries) == before


def test_receipt_nested_hold_attribution_is_payment_history(world):
    h, inv = world
    h.svc.db['receipts']['owned'] = {'invoice_id': None,
        'held_compliance': {'attributed_invoice_id': inv['invoice_id']}}
    assert void(world).status_code == 409
