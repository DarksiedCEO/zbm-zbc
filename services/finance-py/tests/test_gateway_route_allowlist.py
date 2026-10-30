"""ADR 0023 (public edge hardening; AEGIS A1 M1): the internet-facing stripe-gateway's caller ``rail_gateway`` reaches
exactly ONE route, ``POST /fin/v1/stripe/events``; payout-rail webhooks moved to their own caller
``payout_rail_gateway``, which reaches exactly ``POST /fin/v1/rails/{stripe|trolley}/events``. Every route the app
serves is enumerated from the app itself (a new route is covered without editing this file), called with each caller's
token and a valid service bearer, and must answer 403, except the caller's own route(s) and ``GET /health`` (open to
anyone without a token; the caller gets exactly the anonymous answer)."""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute

from helpers import ANDRE_TOKEN, Harness, rid

OWN = {
    "rail_gateway": {("POST", "/fin/v1/stripe/events")},
    "payout_rail_gateway": {("POST", "/fin/v1/rails/{rail}/events")},
}


def _routes(app):
    for r in app.routes:
        if isinstance(r, APIRoute):
            for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
                yield method, r.path


def _concrete(path: str) -> str:
    return re.sub(r"\{rail\}", "stripe", re.sub(r"\{(?!rail\})[^}]+\}", "x-1", path))


@pytest.mark.parametrize("caller", sorted(OWN))
def test_every_route_but_its_own_is_403(caller):
    h = Harness().ready()
    seen = 0
    reached = set()
    for method, path in _routes(h.app):
        r = h.client.request(method, _concrete(path), headers=h.headers(caller=caller),
                             json={"request_id": rid()} if method in ("POST", "PUT") else None)
        seen += 1
        if (method, path) in OWN[caller]:
            assert r.status_code != 403, (method, path, r.status_code, r.text[:200])
            reached.add((method, path))
        elif path == "/health":
            assert r.status_code == 200 and r.json() == h.client.get("/health").json()
        else:
            assert r.status_code == 403, (caller, method, path, r.status_code, r.text[:200])
    assert reached == OWN[caller]
    assert seen > 60


def test_gateway_token_cannot_read_general_finance_routes():
    """AEGIS A1 M1 repro, now refused: the gateway's credentials used to read treasury, invoices, rules, audit."""
    h = Harness().ready()
    for path in ("/fin/v1/treasury", "/fin/v1/rules", "/fin/v1/integrity", "/fin/v1/intelligences"):
        assert h.get(path, caller="rail_gateway").status_code == 403, path
        assert h.get(path, caller="payout_rail_gateway").status_code == 403, path
    # and it cannot post payout-rail events or open a dispute
    ev = {"event_id": "evt-x", "type": "paid", "item_id": "it-1", "signature": "sig-ok-test-only"}
    assert h.post("/fin/v1/rails/stripe/events", {"request_id": rid(), "events": [ev]},
                  caller="rail_gateway").status_code == 403
    assert h.post("/fin/v1/disputes", {"request_id": rid()}, caller="rail_gateway").status_code == 403
    # the payout-rail caller cannot post Stripe events
    assert h.post("/fin/v1/stripe/events", {"request_id": rid(), "payload": "{}", "signature": "t=1,v1=0"},
                  caller="payout_rail_gateway").status_code == 403


def test_andre_s_token_does_not_widen_the_gateway_token():
    h = Harness().ready()
    r = h.client.get("/fin/v1/treasury", headers=h.headers(caller="rail_gateway", andre=ANDRE_TOKEN))
    assert r.status_code == 403


@pytest.mark.parametrize("caller", sorted(OWN))
def test_health_only_as_the_anonymous_get_and_no_other_spelling(caller):
    h = Harness().ready()
    anon = h.client.get("/health").json()
    assert h.client.get("/health", headers=h.headers(caller=caller)).json() == anon
    for method, path in (("POST", "/health"), ("GET", "/health/"), ("GET", "/Health"),
                         ("GET", "/fin/v1/stripe/events"), ("POST", "/fin/v1/stripe/events/"),
                         ("POST", "/fin/v1/stripe/Events"), ("POST", "/fin/v1/rails/paypal/events"),
                         ("POST", "/fin/v1/rails/stripe/events/x")):
        r = h.client.request(method, path, headers=h.headers(caller=caller))
        assert r.status_code == 403, (caller, method, path, r.status_code)


def test_other_callers_still_reach_the_routes_open_to_any_caller():
    h = Harness().ready()
    for caller in ("scheduler", "onboarding", "compliance_38"):
        assert h.get("/fin/v1/integrity", caller=caller).status_code == 200
        assert h.get("/fin/v1/rules", caller=caller).status_code != 403
