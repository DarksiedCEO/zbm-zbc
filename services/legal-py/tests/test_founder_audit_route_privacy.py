"""Refused founder headers must not make request-path identifiers durable."""
import json
import secrets
from types import SimpleNamespace

import pytest
from starlette.requests import Request

import api


def test_acceptance_refusal_records_static_route_without_identifier(hr):
    marker = 'private-' + secrets.token_hex(8)
    response = hr.client.get('/legal/v1/acceptances/' + marker,
                             headers=hr.headers(andre=secrets.token_hex(32)))
    assert response.status_code == 403
    events = hr.ledger.of_type('founder_approval_refused')
    assert events and events[-1]['payload']['route'] == 'acceptances/get'
    assert marker not in json.dumps(hr.svc.log.records)
    assert marker not in json.dumps(events)


def test_authorized_founder_still_reaches_acceptance_lookup(hr):
    response = hr.client.get('/legal/v1/acceptances/lg-acc-' + '0' * 26,
                             headers=hr.headers(andre=hr.settings.andre_token))
    assert response.status_code == 404
    assert not hr.ledger.of_type('founder_approval_refused')


@pytest.mark.parametrize('route,expected', [
    (SimpleNamespace(path='/legal/v1/acceptances/{acceptance_id}'),
     '/legal/v1/acceptances/{acceptance_id}'),
    (None, 'unclassified'),
    (SimpleNamespace(path=None), 'unclassified'),
])
def test_missing_explicit_label_uses_template_or_fixed_fallback(route, expected):
    marker = secrets.token_hex(16)
    request = Request({'type': 'http', 'method': 'GET', 'path': '/legal/v1/acceptances/' + marker,
                       'headers': [], 'route': route})
    assert api._founder_audit_route(request) == expected
    assert marker not in api._founder_audit_route(request)
