"""Mock-only receipt delivery contract: no network/provider credentials."""
import copy
import secrets

import pytest

from helpers import ANDRE_TOKEN, Harness, rid


class MockEnvelopeMail:
    enabled = True
    def __init__(self):
        self.calls = []
        self.outcome = 'retry'
    def deliver_receipt(self, envelope):
        from ports import ReceiptDeliveryOutcome
        self.calls.append(copy.deepcopy(envelope))
        return ReceiptDeliveryOutcome(self.outcome)


@pytest.fixture
def mailworld(tmp_path):
    h = Harness(data_dir=str(tmp_path / 'data')).ready()
    h.fund_campaign()
    rec, = h.svc.db['client_receipts'].values()
    port = MockEnvelopeMail()
    h.svc.ports.client_mail = port
    address = 'owned-' + secrets.token_hex(8) + '@example.invalid'
    return h, rec, port, address


def contact(world, address=None, **kw):
    h, rec, _, original = world
    return h.client.put('/fin/v1/clients/' + rec['client_id'] + '/billing-contact',
                        json={'request_id': rid(), 'email': original if address is None else address},
                        headers=h.headers(**kw))


def send(world):
    h, rec, _, _ = world
    return h.post('/fin/v1/client-receipts/' + rec['client_receipt_id'] + '/send',
                  {'request_id': rid()}, caller='scheduler')


def test_contact_and_frozen_envelope_replay_privacy(mailworld):
    h, rec, port, address = mailworld
    assert contact(mailworld, andre=ANDRE_TOKEN).status_code == 200
    assert send(mailworld).status_code == 200
    first, = port.calls
    assert first['to'] == address and rec['amount_paid'] in first['text']
    assert 'held' not in first['text'].lower() and 'sanctions' not in first['text'].lower()
    h.clock.advance(minutes=16)
    port.outcome = 'accepted'
    assert send(mailworld).json()['sent'] is True
    assert port.calls == [first, first]
    view = h.ok(h.get('/fin/v1/client-receipts/' + rec['client_receipt_id']))
    assert address not in str(view) and 'envelope' not in view
    assert address not in str(h.ledger.events)
    assert len(h.svc.entries) == 1


def test_founder_contact_only_and_no_contact_no_attempt(mailworld):
    h, rec, port, _ = mailworld
    assert contact(mailworld, caller='scheduler').status_code == 403
    assert send(mailworld).status_code == 200
    assert not port.calls
    assert h.svc.db['client_receipts'][rec['client_receipt_id']]['attempts'] == 0


@pytest.mark.parametrize('address', ['a\r\nb@example.invalid', 'two@example.invalid,other@example.invalid',
                                     'a\u202e@example.invalid', 'a@example.invalid' + 'x'*255])
def test_invalid_contacts_are_refused_without_ledger_pii(mailworld, address):
    h, _, _, _ = mailworld
    assert contact(mailworld, address, andre=ANDRE_TOKEN).status_code == 422
    assert address not in str(h.ledger.events)


def test_changed_contact_never_rewrites_or_retries_frozen_envelope(mailworld):
    h, rec, port, _ = mailworld
    assert contact(mailworld, andre=ANDRE_TOKEN).status_code == 200
    send(mailworld)
    first = copy.deepcopy(port.calls)
    assert contact(mailworld, 'new@example.invalid', andre=ANDRE_TOKEN).status_code == 200
    h.clock.advance(minutes=16)
    response = send(mailworld)
    assert response.status_code == 200 and response.json()['sent'] is False
    assert port.calls == first
    assert h.svc.db['client_receipts'][rec['client_receipt_id']]['status'] == 'delivery_uncertain'


@pytest.mark.parametrize('advance', [{'hours': 23}, {'hours': 48}, {'minutes': -1}])
def test_deadline_or_clock_rollback_stops_automatic_retry(mailworld, advance):
    h, rec, port, _ = mailworld
    contact(mailworld, andre=ANDRE_TOKEN)
    send(mailworld)
    h.clock.advance(**advance)
    assert send(mailworld).status_code == 200
    assert len(port.calls) == 1
    assert h.svc.db['client_receipts'][rec['client_receipt_id']]['status'] == 'delivery_uncertain'


def test_restart_keeps_identical_recipient_body_key_and_deadline(mailworld):
    h, rec, port, _ = mailworld
    contact(mailworld, andre=ANDRE_TOKEN)
    send(mailworld)
    first = port.calls[0]
    h.clock.advance(minutes=16)
    h2 = Harness(clock=h.clock, data_dir=h.settings.data_dir, ledger=h.ledger, fakes=h.f)
    h2.svc.ports.client_mail = port
    world = (h2, rec, port, mailworld[3])
    assert send(world).status_code == 200 and port.calls == [first, first]


def test_disabled_default_has_no_delivery_claim(mailworld):
    from ports import NotBuiltClientMail
    h, rec, _, _ = mailworld
    h.svc.ports.client_mail = NotBuiltClientMail()
    assert send(mailworld).status_code == 200
    assert h.svc.db['client_receipts'][rec['client_receipt_id']]['attempts'] == 0


def test_ledger_refusal_before_claim_prevents_mock_call(mailworld):
    h, _, port, _ = mailworld
    contact(mailworld, andre=ANDRE_TOKEN)
    h.ledger.fail_on_type = 'client_receipt_send_claimed'
    assert send(mailworld).status_code == 503 and not port.calls
    h.ledger.fail_on_type = None
    assert send(mailworld).status_code == 200 and len(port.calls) == 1


@pytest.mark.parametrize('answer', [True, 1, 'accepted', {'outcome': 'accepted'}, None])
def test_untyped_truthy_answers_are_uncertain_never_sent(mailworld, answer):
    h, rec, port, _ = mailworld
    contact(mailworld, andre=ANDRE_TOKEN)
    port.deliver_receipt = lambda envelope: answer
    assert send(mailworld).json()['sent'] is False
    assert h.svc.db['client_receipts'][rec['client_receipt_id']]['status'] == 'delivery_uncertain'
    assert h.ledger.of_type('adapter_answer_refused')


def test_unknown_typed_code_is_refused_without_free_text(mailworld):
    from ports import ReceiptDeliveryOutcome
    h, rec, port, _ = mailworld
    contact(mailworld, andre=ANDRE_TOKEN)
    canary = 'private-' + secrets.token_hex(16)
    port.deliver_receipt = lambda envelope: ReceiptDeliveryOutcome(canary)
    response = send(mailworld)
    assert response.json()['sent'] is False
    assert canary not in response.text and canary not in str(h.ledger.events)
    assert h.svc.db['client_receipts'][rec['client_receipt_id']]['status'] == 'delivery_uncertain'


def test_retry_attempt_cap_never_resets_envelope(mailworld):
    h, rec, port, _ = mailworld
    contact(mailworld, andre=ANDRE_TOKEN)
    for _ in range(8):
        send(mailworld)
        h.clock.advance(minutes=16)
    assert len(port.calls) == 7 and all(x == port.calls[0] for x in port.calls)
    current = h.svc.db['client_receipts'][rec['client_receipt_id']]
    assert current['attempts'] == 7 and current['status'] == 'delivery_uncertain'


def test_contact_disable_and_review_cannot_requeue(mailworld):
    h, rec, port, _ = mailworld
    contact(mailworld, andre=ANDRE_TOKEN)
    send(mailworld)
    h.ok(h.client.put('/fin/v1/clients/' + rec['client_id'] + '/billing-contact',
                     json={'request_id': rid(), 'email': None}, headers=h.headers(andre=ANDRE_TOKEN)))
    h.clock.advance(minutes=16)
    send(mailworld)
    body = {'request_id': rid(), 'evidence': {'ref': rid(), 'sha256': secrets.token_hex(32)}}
    path = '/fin/v1/client-receipts/' + rec['client_receipt_id'] + '/delivery-review'
    assert h.post(path, body, caller='scheduler').status_code == 403
    result = h.ok(h.post(path, body, andre=ANDRE_TOKEN))
    assert result['sent'] is False and result['client_receipt']['status'] == 'delivery_reviewed'
    assert h.ok(h.post(path, body, andre=ANDRE_TOKEN)) == result
    contact(mailworld, andre=ANDRE_TOKEN)
    h.clock.advance(days=2)
    assert send(mailworld).json()['sent'] is False and len(port.calls) == 1


def test_crashed_claim_restart_uses_original_envelope(mailworld):
    h, rec, port, _ = mailworld
    contact(mailworld, andre=ANDRE_TOKEN)
    class Crash(BaseException):
        pass
    def crash(envelope):
        port.calls.append(copy.deepcopy(envelope))
        raise Crash()
    port.deliver_receipt = crash
    with pytest.raises(Crash):
        h.svc.send_client_receipt("scheduler", rid(), rec["client_receipt_id"])
    saved = copy.deepcopy(h.svc.db['receipt_envelopes'][rec['client_receipt_id']])
    assert h.svc.db['client_receipts'][rec['client_receipt_id']]['attempts'] == 1
    h.clock.advance(minutes=16)
    h2 = Harness(clock=h.clock, data_dir=h.settings.data_dir, ledger=h.ledger, fakes=h.f)
    replacement = MockEnvelopeMail()
    h2.svc.ports.client_mail = replacement
    assert send((h2, rec, replacement, mailworld[3])).status_code == 200
    assert replacement.calls == port.calls
    assert h2.svc.db['receipt_envelopes'][rec['client_receipt_id']] == saved


def test_concurrent_claim_refused_and_late_accept_preserves_withdrawal(mailworld):
    from ports import ReceiptDeliveryOutcome
    h, rec, port, _ = mailworld
    contact(mailworld, andre=ANDRE_TOKEN)
    def during(envelope):
        port.calls.append(copy.deepcopy(envelope))
        assert send(mailworld).json()['sent'] is False
        h.ok(h.post('/fin/v1/receipts/' + rec['receipt_id'] + '/return',
                    {'request_id': rid(), 'return_ref_sha256': secrets.token_hex(32),
                     'return_code': 'R01', 'value_date': '2026-10-02'}, caller='bank_feed'))
        return ReceiptDeliveryOutcome('accepted')
    port.deliver_receipt = during
    assert send(mailworld).json()['sent'] is True
    assert len(port.calls) == 1
    assert h.svc.db['client_receipts'][rec['client_receipt_id']]['status'] == 'withdrawn_after_send'
    assert send(mailworld).json()['sent'] is False and len(port.calls) == 1


def test_backoff_and_saved_body_survive_template_input_change(mailworld):
    h, rec, port, _ = mailworld
    contact(mailworld, andre=ANDRE_TOKEN)
    send(mailworld)
    assert send(mailworld).json()['sent'] is False and len(port.calls) == 1
    # Owned fixture simulates newer rendering inputs; retry must read the saved bytes, never rerender.
    h.svc.db['client_receipts'][rec['client_receipt_id']]['what_you_bought'] = 'changed template input'
    h.clock.advance(minutes=16)
    send(mailworld)
    assert port.calls[0] == port.calls[1]


def test_unmarked_legacy_port_is_disabled(mailworld):
    h, rec, _, _ = mailworld
    class Unmarked:
        def send_receipt(self, client, receipt):
            pytest.fail('legacy bool port must not be called without test-only injection marker')
    h.svc.ports.client_mail = Unmarked()
    assert send(mailworld).json()['sent'] is False
    assert h.svc.db['client_receipts'][rec['client_receipt_id']]['attempts'] == 0


def test_legacy_test_fixture_cannot_bypass_existing_envelope(mailworld):
    from fakes import FakeClientMail
    h, rec, _, _ = mailworld
    contact(mailworld, andre=ANDRE_TOKEN)
    send(mailworld)
    legacy = FakeClientMail()
    h.svc.ports.client_mail = legacy
    h.clock.advance(minutes=16)
    assert send(mailworld).json()['sent'] is False and legacy.sent == []
    assert h.svc.db['client_receipts'][rec['client_receipt_id']]['attempts'] == 1


def test_late_failure_cannot_undo_stale_claim_success(mailworld):
    from ports import ReceiptDeliveryOutcome
    h, rec, port, _ = mailworld
    contact(mailworld, andre=ANDRE_TOKEN)
    def delayed(envelope):
        port.calls.append(copy.deepcopy(envelope))
        h.clock.advance(minutes=16)
        def accept(second):
            port.calls.append(copy.deepcopy(second))
            return ReceiptDeliveryOutcome('accepted')
        port.deliver_receipt = accept
        assert send(mailworld).json()['sent'] is True
        return ReceiptDeliveryOutcome('retry')
    port.deliver_receipt = delayed
    assert send(mailworld).json()['sent'] is False
    assert h.svc.db['client_receipts'][rec['client_receipt_id']]['status'] == 'sent'
    assert len(port.calls) == 2 and port.calls[0] == port.calls[1]


def test_adapter_mutation_does_not_mutate_persisted_envelope(mailworld):
    from ports import ReceiptDeliveryOutcome
    h, rec, port, address = mailworld
    contact(mailworld, andre=ANDRE_TOKEN)
    def mutate(envelope):
        envelope['to'] = 'changed@example.invalid'
        envelope['text'] = 'changed'
        return ReceiptDeliveryOutcome('retry')
    port.deliver_receipt = mutate
    send(mailworld)
    saved = h.svc.db['receipt_envelopes'][rec['client_receipt_id']]['envelope']
    assert saved['to'] == address and saved['text'].startswith('Payment receipt\n')


@pytest.mark.parametrize('status,attempts', [('pending_send', 1), ('sending', 0)])
def test_historical_attempt_without_envelope_never_mints_new_key(mailworld, status, attempts):
    h, rec, port, _ = mailworld
    # Owned fixture models old-version persisted attempts, with unknown provider acceptance/body/key.
    current = h.svc.db['client_receipts'][rec['client_receipt_id']]
    current.update(status=status, attempts=attempts, claimed_at='2026-10-01T00:00:00Z')
    contact(mailworld, andre=ANDRE_TOKEN)
    assert send(mailworld).json()['sent'] is False
    assert not port.calls and not h.svc.db['receipt_envelopes']
    assert h.svc.db['client_receipts'][rec['client_receipt_id']]['status'] == 'delivery_uncertain'
