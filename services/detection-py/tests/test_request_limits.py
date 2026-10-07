"""
In-process checks of the request limits (fix wave 1, Sep 24 2026). The
real-socket behavior (413 before parsing, /health responsiveness, header
cap) is in test_request_limits_live.py; these pin the per-route contract.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api import MAX_BATCH_ITEMS, MAX_BODY_BYTES, ROUTE_BODY_LIMITS, _BodyLimitMiddleware, app
from conftest import TEST_SERVICE_TOKEN
from _fx import AS_OF, AS_OF_WIRE, TENANT, make_finding  # noqa: F401

client = TestClient(app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})
anon = TestClient(app)

_ORDER = {
    "order_id": "ord_x", "customer_id": "c", "placed_at": "2026-06-12T10:00:00Z", "status": "completed",
    "line_items": [{"sku": "S", "unit_price_usd": "1.00", "quantity": 1}], "discounts": [],
    "affiliate": None, "source_platform": "shopify", "recovery_attempted": False,
}

# route -> its list field. Only the item COUNT matters here: the cap
# is checked by pydantic before any item is looked at in detail.
ROUTES = {
    "/agents/affiliate-coupon-extension/detect": "orders",
    "/agents/discount-misuse/detect": "orders",
    "/agents/abandoned-cart-coverage/detect": "orders",
    "/agents/renewal-never-triggered/detect": "subscriptions",
    "/agents/server-side-attribution/detect": "events",
    "/agents/cross-channel-attribution/detect": "touchpoints",
    "/agents/platform-integration/detect": "statuses",
    "/agents/contract-pricing-term-drift/detect": "terms",
    "/correlation/overlaps": "findings",
}


def test_limits_are_the_documented_values():
    # LOW-C (fix wave 1): the body limit is per route, computed from the
    # worst case of the route's largest legal batch (test_body_limits.py);
    # MAX_BODY_BYTES is the largest of them. It used to be a flat 2 MiB,
    # which refused 1,000 orders of 30 line items.
    # Oct 6 2026: 35 MiB — identifier fields are now ASCII-safe (E-12), one byte per character in
    # the worst case instead of six (request_limits.ASCII_SAFE_PATTERNS).
    assert MAX_BODY_BYTES == 35 * 1024 * 1024
    assert MAX_BATCH_ITEMS == 1000


@pytest.mark.parametrize("path,field", ROUTES.items())
def test_batch_over_the_item_cap_is_422(path, field):
    r = client.post(path, json={field: [{}] * (MAX_BATCH_ITEMS + 1)})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert any(d["type"] == "too_long" and d["loc"] == ["body", field] for d in detail), detail


def test_batch_at_the_item_cap_is_accepted():
    orders = [{**_ORDER, "order_id": f"ord_{i}"} for i in range(MAX_BATCH_ITEMS)]
    r = client.post("/agents/discount-misuse/detect", json={"client_id": TENANT, "orders": orders})
    assert r.status_code == 200, r.text[:500]


def test_oversized_content_length_is_413_on_every_route_even_unauthenticated():
    big = b" " * (MAX_BODY_BYTES + 1)  # over every route's limit
    for path in [*ROUTES, "/health", "/fixtures/orders"]:
        r = anon.request("POST" if path in ROUTES else "GET", path, content=big,
                         headers={"Content-Type": "application/json"})
        assert r.status_code == 413, (path, r.status_code)


def test_oversized_chunked_body_is_413():
    limit = ROUTE_BODY_LIMITS["/agents/discount-misuse/detect"]

    def gen():
        for _ in range(limit // 65536 + 2):
            yield b" " * 65536
    r = client.post("/agents/discount-misuse/detect", content=gen(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_body_at_the_limit_is_parsed_not_refused():
    body = b'{"client_id":"fixture-pool","orders": []}'
    body = body + b" " * (ROUTE_BODY_LIMITS["/agents/discount-misuse/detect"] - len(body))
    r = client.post("/agents/discount-misuse/detect", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text



def test_oversized_head_is_431_under_any_launcher():
    # serve.py's h11 parser refuses this while reading (see the live test);
    # the middleware re-checks it for any other launcher.
    r = client.get("/health", headers={"X-Big": "a" * (20 * 1024)})
    assert r.status_code == 431


def test_slow_body_is_cut_off_with_408():
    inner = FastAPI()

    @inner.post("/echo")
    async def echo(request: Request):
        return {"n": len(await request.body())}

    app_under_test = _BodyLimitMiddleware(inner, read_timeout=0.3)
    sent: list[dict] = []

    async def run():
        async def receive():
            if not hasattr(receive, "first"):
                receive.first = True
                return {"type": "http.request", "body": b"{", "more_body": True}
            await asyncio.sleep(3600)  # the rest of the body never arrives

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/echo", "raw_path": b"/echo", "query_string": b"",
                 "headers": [(b"content-type", b"application/json")], "http_version": "1.1",
                 "scheme": "http", "server": ("t", 80), "client": ("c", 1), "root_path": ""}
        await asyncio.wait_for(app_under_test(scope, receive, send), 5)

    asyncio.run(run())
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 408, sent
