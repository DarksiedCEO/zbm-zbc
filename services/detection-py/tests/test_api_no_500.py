"""
Sweep: invalid input must yield 4xx, never 500 (fix wave 1, Sep 24 2026).

Every case below returned 500 Internal Server Error before the fix:
  - an affiliate click timestamp without a timezone next to an order
    timestamp with one: `aware - naive` raised TypeError inside the agent;
  - a JSON body containing NaN / Infinity: the request was rejected, but
    FastAPI's default 422 handler echoed the NaN input back and
    `json.dumps(allow_nan=False)` crashed while rendering the error;
  - huge money / huge quantity (see test_money_bounds.py, F14).
"""

import pytest
from fastapi.testclient import TestClient

from api import app
from conftest import TEST_SERVICE_TOKEN
from _fx import AS_OF, AS_OF_WIRE, TENANT, make_finding  # noqa: F401

client = TestClient(
    app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}, raise_server_exceptions=False
)
JSON = {"Content-Type": "application/json"}


def _order(**kw):
    o = {"order_id": "o", "customer_id": "c", "placed_at": "2026-06-01T00:00:00Z", "status": "completed",
         "source_platform": "x", "line_items": [{"sku": "s", "unit_price_usd": "10.00", "quantity": 1}]}
    o.update(kw)
    return o


@pytest.mark.parametrize("click,order", [
    ("2026-06-01T00:00:00", "2026-06-20T00:00:00Z"),   # naive click, aware order
    ("2026-06-01T00:00:00Z", "2026-06-20T00:00:00"),   # aware click, naive order
    ("2026-06-01T00:00:00", "2026-06-20T00:00:00"),    # both naive: ambiguous instant
])
def test_timestamps_without_timezone_are_422_not_500(click, order):
    aff = {"affiliate_id": "a", "click_timestamp": click, "order_timestamp": order, "attribution_window_hours": 24}
    r = client.post("/agents/affiliate-coupon-extension/detect", json={"client_id": TENANT, "orders": [_order(affiliate=aff)]})
    assert r.status_code == 422, (r.status_code, r.text[:300])


@pytest.mark.parametrize("path,body", [
    ("/agents/abandoned-cart-coverage/detect", b"NaN"),
    ("/agents/abandoned-cart-coverage/detect", b'{"client_id":"fixture-pool","orders": NaN}'),
    ("/agents/abandoned-cart-coverage/detect", b'{"client_id":"fixture-pool","orders": [Infinity]}'),
    ("/agents/discount-misuse/detect",
     b'{"client_id":"fixture-pool","orders":[{"order_id":"o","customer_id":"c","placed_at":"2026-06-01T00:00:00Z","status":"completed",'
     b'"source_platform":"x","line_items":[{"sku":"s","unit_price_usd":"10.00","quantity":1}],'
     b'"discounts":[{"code":"a","percent_off":NaN},{"code":"b","percent_off":-Infinity}]}]}'),
    ("/correlation/overlaps", b'{"findings": [{"finding_id": NaN}]}'),
])
def test_nan_and_infinity_in_body_are_422_not_500(path, body):
    r = client.post(path, content=body, headers=JSON)
    assert r.status_code == 422, (r.status_code, r.text[:300])


@pytest.mark.parametrize("body", [
    b"", b"{", b"\xff\xfe{", b"[]", b"null", b'"x"', b'{"client_id":"fixture-pool","orders": {}}',
    b'{"client_id":"fixture-pool","orders":[{"order_id":"o","customer_id":"c","placed_at":"2026-06-01T00:00:00Z","status":"x",'
    b'"source_platform":"x","line_items":[{"sku":"s","unit_price_usd":"1.00","quantity":' + b"9" * 5000 + b"}]}]}",
    b'{"client_id":"fixture-pool","orders":' + b"[" * 100000 + b"]" * 100000 + b"}",
])
def test_malformed_bodies_are_4xx_not_500(body):
    r = client.post("/agents/abandoned-cart-coverage/detect", content=body, headers=JSON)
    assert 400 <= r.status_code < 500, (r.status_code, r.text[:300])


def test_validation_errors_do_not_echo_the_input_back():
    """The 422 body names the location and the reason, not the rejected
    value itself (which is what crashed on NaN, and can be arbitrarily big)."""
    r = client.post("/agents/abandoned-cart-coverage/detect",
                    json={"client_id": TENANT, "orders": [_order(line_items=[{"sku": "s", "unit_price_usd": "1" * 5000 + ".00", "quantity": 1}])]})
    assert r.status_code == 422
    assert "1" * 100 not in r.text
    assert len(r.text) < 2000
    detail = r.json()["detail"]
    assert detail and all(set(d) <= {"type", "loc", "msg"} for d in detail)


def test_every_post_route_rejects_an_empty_object_with_422():
    for path in [
        "/agents/affiliate-coupon-extension/detect", "/agents/discount-misuse/detect",
        "/agents/abandoned-cart-coverage/detect", "/agents/renewal-never-triggered/detect",
        "/agents/server-side-attribution/detect", "/agents/cross-channel-attribution/detect",
        "/agents/platform-integration/detect", "/agents/contract-pricing-term-drift/detect",
        "/correlation/overlaps",
    ]:
        r = client.post(path, json={})
        assert r.status_code == 422, (path, r.status_code, r.text[:200])


@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_non_json_content_type_is_415(ctype):
    r = client.post("/agents/abandoned-cart-coverage/detect", content=b'{"client_id":"fixture-pool","orders": []}', headers={"Content-Type": ctype})
    assert r.status_code == 415, (r.status_code, r.text[:200])


def test_json_content_type_with_charset_is_accepted():
    r = client.post("/agents/abandoned-cart-coverage/detect", content=b'{"client_id":"fixture-pool","orders": []}',
                    headers={"Content-Type": "application/json; charset=utf-8"})
    assert r.status_code == 200, r.text[:200]
