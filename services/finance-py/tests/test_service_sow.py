"""Service issuance requires Legal's registered SOW type and portal provenance.

Normal HTTP adapter contract tests; no provider calls. Existing reference, party,
entity and basis checks remain independent of these two new fields.
"""
import secrets

import httpx
import pytest

from clients import HttpLegal
from helpers import ANDRE_TOKEN, Harness, rid
from test_legal_client import ACC, NOW, SHA, acceptance_doc, version_doc


def legal(doc_id='sow_seo_audit', doc_type='sow', via='client_portal', version_over=None, acceptance_over=None):
    version = version_doc(doc_id=doc_id, doc_type=doc_type, **(version_over or {}))
    acceptance = acceptance_doc(doc_id=doc_id, via=via, **(acceptance_over or {}))
    if doc_type is None:
        version.pop('doc_type')
    if via is None:
        acceptance.pop('via')
    def answer(request):
        return httpx.Response(200, json=version if '/versions/' in request.url.path else acceptance)
    return HttpLegal('http://legal.internal', secrets.token_hex(32), secrets.token_hex(32),
                     transport=httpx.MockTransport(answer), now=lambda: NOW)


def draft(h, doc_id='sow_seo_audit'):
    body = {'request_id': rid(), 'entity': 'zbm', 'client_id': 'zbm-client-1', 'kind': 'service',
            'lines': [{'line_code': 'creative_services', 'quantity': 1, 'unit_price': '100.00'}],
            'payment_methods': ['ach'],
            'legal_ref': {'doc_id': doc_id, 'version': '1.1', 'doc_sha256': SHA, 'acceptance_id': ACC}}
    return h.ok(h.post('/fin/v1/invoices', body, caller='onboarding'), 201)['invoice']


def decide(h, inv, request_id=None):
    return h.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision",
                  {'request_id': request_id or rid(), 'content_sha256': inv['content_sha256'], 'decision': 'approve'},
                  andre=ANDRE_TOKEN)


@pytest.mark.parametrize('doc_id,doc_type,via', [
    ('client_msa', 'client_msa', 'client_portal'),
    ('sow_seo_audit', None, 'client_portal'),
    ('sow_seo_audit', 'SOW', 'client_portal'),
    ('sow_seo_audit', ['sow'], 'client_portal'),
    ('sow_seo_audit', 'sow', None),
    ('sow_seo_audit', 'sow', 'onboarding'),
    ('sow_seo_audit', 'sow', True),
    ('sow_seo_audit', 'sow', ['client_portal']),
])
def test_service_approval_requires_literal_registered_sow_and_portal(doc_id, doc_type, via):
    h = Harness().ready()
    h.svc.ports.legal = legal(doc_id, doc_type, via)
    inv = draft(h, doc_id)
    before = len(h.svc.entries)
    response = decide(h, inv)
    assert response.status_code == 409, response.text
    assert h.svc.db['invoices'][inv['invoice_id']]['status'] == 'draft'
    assert len(h.svc.entries) == before


@pytest.mark.parametrize('doc_id', ['sow', 'sow_seo_audit', 'sow_revenue_recovery'])
def test_registered_sow_from_portal_issues_with_exact_reference(doc_id):
    h = Harness().ready()
    h.svc.ports.legal = legal(doc_id)
    inv = draft(h, doc_id)
    response = decide(h, inv)
    assert response.status_code == 200, response.text
    issued = response.json()['invoice']
    assert issued['status'] == 'issued'
    assert issued['legal_ref'] == inv['legal_ref'] and issued['content_sha256'] == inv['content_sha256']


def test_msa_billing_readiness_still_uses_a_real_msa():
    h = Harness().ready()
    h.svc.ports.legal = legal('client_msa', 'client_msa')
    h.ok(h.put('/fin/v1/clients/zbm-client-1/billing-profile', {
        'request_id': rid(), 'entity': 'zbm', 'payment_method': 'ach',
        'msa': {'doc_id': 'client_msa', 'version': '1.1', 'doc_sha256': SHA, 'acceptance_id': ACC}}, andre=ANDRE_TOKEN))
    assert h.ok(h.get('/fin/v1/clients/zbm-client-1/billing-readiness', caller='onboarding'))['allowed'] is True


def test_successful_issuance_replay_after_restart_does_not_reissue_or_revalidate(tmp_path):
    h = Harness(data_dir=str(tmp_path / 'finance')).ready()
    h.svc.ports.legal = legal()
    inv = draft(h)
    request_id = rid()
    first = h.ok(decide(h, inv, request_id))
    count = len(h.svc.entries)
    restarted = Harness(data_dir=str(tmp_path / 'finance'), clock=h.clock, ledger=h.ledger, fakes=h.f)
    restarted.f['legal'].available = False
    assert restarted.ok(decide(restarted, inv, request_id)) == first
    assert len(restarted.svc.entries) == count
    assert restarted.svc.db['invoices'][inv['invoice_id']]['legal_ref'] == inv['legal_ref']
    assert decide(restarted, inv).status_code == 409
    restarted.clock.advance(minutes=16)
    assert decide(restarted, inv, request_id).status_code == 409
    restarted.svc.close()
