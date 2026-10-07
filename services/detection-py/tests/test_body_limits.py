"""
LOW-C (fix wave 1, Sep 24 2026): 1,000 orders x 30 line items is 2.12 MiB
and was refused with 413, although the API advertises 1,000 items per batch.
The 2 MiB limit had been sized from a typical order; no field had a limit,
so the largest legal batch had no size at all.

Now every request-model field is bounded (zbm_schema/limits.py), each
route's body limit is computed from the worst case of its largest legal
batch (request_limits.py), and these tests BUILD that worst case for every
route — every string at its max length made of characters that JSON must
escape (6 bytes each), every list full, every optional present, numbers at
their longest — and check it is accepted (200, not 413), that the builder
reaches the computed bound (so the bound is not loose), and that one byte
more than the limit is still 413.

Also: the heavy-request cap (503 + Retry-After) in-process; the live
/health latency bound under 16 concurrent large batches is in
test_request_limits_live.py.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import api
from api import (
    DEFAULT_BODY_BYTES,
    MAX_BATCH_ITEMS,
    ROUTE_BODY_LIMITS,
    ROUTE_REQUEST_MODELS,
    _BodyLimitMiddleware,
    app,
)
from conftest import TEST_SERVICE_TOKEN
from request_limits import HEADROOM, MIB, UnboundedField, body_limit_for, worst_case_json_bytes
from zbm_schema import compute_finding_id, percent_of
from zbm_schema import limits as L
from _fx import AS_OF, AS_OF_WIRE, TENANT, make_finding  # noqa: F401

client = TestClient(app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})

ESC = "\u0001"  # JSON writes it as \u0001: 6 bytes per character, the most any character takes
DT = "2026-09-24T12:34:56.123456789+05:30"  # RFC3339Nano with offset: the longest canonical datetime
MAX_PRICE_50 = "19999999999999.99"  # 50 of these (qty 1) stay under the order-subtotal bound


def s(n: int) -> str:
    return ESC * n


# Oct 6 2026: identifiers and finding fields have an ASCII-safe charset (E-12), so their worst case
# is one byte per character (request_limits.ASCII_SAFE_PATTERNS); a normalized label (limits.Slug)
# is still sized at six: a legal raw label is "a" padded with whitespace that JSON escapes.
def ident(n: int) -> str:
    return "a" * n


def raw_slug(n: int) -> str:
    return "a" + "\x1f" * (n - 1)


TENANT_WORST = ident(L.ID_MAX_CHARS)


def compact(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


def worst_order() -> dict:
    return {
        "order_id": ident(L.ID_MAX_CHARS),
        "customer_id": ident(L.ID_MAX_CHARS),
        "placed_at": DT,
        "status": raw_slug(L.LABEL_MAX_CHARS),
        "line_items": [
            {"sku": s(L.SKU_MAX_CHARS), "unit_price_usd": MAX_PRICE_50, "quantity": 1}
            for _ in range(L.MAX_LINE_ITEMS_PER_ORDER)
        ],
        "discounts": [
            {"code": s(L.DISCOUNT_CODE_MAX_CHARS), "percent_off": 1.2345678901234567e-05, "amount_off_usd": None}
            for _ in range(L.MAX_DISCOUNTS_PER_ORDER)
        ],
        "affiliate": {
            "affiliate_id": ident(L.ID_MAX_CHARS),
            "commission_rate_percent": 1.2345678901234567e-05,
            "click_timestamp": DT,
            "order_timestamp": DT,
            "attribution_window_hours": L.MAX_ATTRIBUTION_WINDOW_HOURS,
        },
        "source_platform": s(L.LABEL_MAX_CHARS),
        "recovery_attempted": False,
    }


WORST_RATE = "99." + "9" * (L.RATE_PERCENT_MAX_CHARS - 3)


def worst_item(field: str) -> dict:
    if field == "orders":
        return worst_order()
    if field == "subscriptions":
        return {"subscription_id": ident(L.ID_MAX_CHARS), "customer_id": ident(L.ID_MAX_CHARS),
                "plan_price_usd": "999999999999999.99", "renewal_interval_days": L.MAX_RENEWAL_INTERVAL_DAYS,
                "last_renewal_at": DT, "next_renewal_due_at": DT, "status": "lapsed_no_renewal_attempt"}
    if field == "events":
        return {"order_id": ident(L.ID_MAX_CHARS), "channel": s(L.LABEL_MAX_CHARS),
                "order_value_usd": "999999999999999.99", "pixel_attributed": False, "server_confirmed": False}
    if field == "touchpoints":
        return {"order_id": ident(L.ID_MAX_CHARS), "channel": s(L.LABEL_MAX_CHARS),
                "touchpoint_sequence": L.MAX_TOUCHPOINT_SEQUENCE, "is_paid_channel": False,
                "is_credited_conversion_channel": False}
    if field == "statuses":
        return {"client_id": TENANT_WORST, "platform": raw_slug(L.LABEL_MAX_CHARS),
                "client_reports_using_it": False, "integration_connected": False}
    if field == "terms":
        return {"term_id": ident(L.ID_MAX_CHARS), "client_id": TENANT_WORST, "term_type": "overage_rate",
                "contracted_value_usd": "999999999999999.99", "actual_billed_value_usd": "999999999999999.99",
                "period_label": ident(L.PERIOD_LABEL_MAX_CHARS)}
    if field == "findings":
        agent, entity, period = ident(L.AGENT_ID_MAX_CHARS), ident(L.ID_MAX_CHARS), ident(L.PERIOD_LABEL_MAX_CHARS)
        return {"finding_id": compute_finding_id(TENANT_WORST, agent, "contract_term", entity, period),
                "client_id": TENANT_WORST, "agent_id": agent,
                "leak_category": "cross_channel_misattribution_risk", "entity_type": "contract_term",
                "entity_id": entity, "period_label": period, "customer_id": ident(L.ID_MAX_CHARS),
                "cause_certainty": "uncertain", "cause_description": s(L.CAUSE_DESCRIPTION_MAX_CHARS),
                # AEGIS M2 (Oct 7 2026): the longest labels (financially_verified) need OBSERVED evidence.
                # L1: the longest value_basis (a 48-character rate) and the amount it reproduces.
                "recoverable_value": {"amount_usd": str(percent_of(Decimal("999999999999999.99"), Decimal(WORST_RATE))),
                                      "classification": "financially_verified", "confidence": "medium"},
                "value_basis": {"base_usd": "999999999999999.99", "rate_percent": WORST_RATE},
                "evidence_class": "OBSERVED", "methodology_id": ident(L.METHODOLOGY_ID_MAX_CHARS),
                "methodology": s(L.METHODOLOGY_MAX_CHARS),
                "detected_at": DT}
    raise AssertionError(field)


FIELD_OF = {model: next(f for f in model.model_fields if f not in ("client_id", "as_of"))
            for model in set(ROUTE_REQUEST_MODELS.values())}
_BODY_CACHE: dict[str, bytes] = {}


def worst_body(field: str) -> bytes:
    if field not in _BODY_CACHE:
        head: dict = {} if field == "findings" else {"client_id": TENANT_WORST}
        if field == "subscriptions":
            head["as_of"] = DT
        _BODY_CACHE[field] = compact({**head, field: [worst_item(field)] * MAX_BATCH_ITEMS})
    return _BODY_CACHE[field]


def post(path: str, body: bytes):
    return client.post(path, content=body, headers={"Content-Type": "application/json"})


# --- the reproduction -----------------------------------------------------------

def test_finding_repro_1000_orders_of_30_line_items_is_accepted():
    order = {
        "order_id": "ord_000000", "customer_id": "cust_000000", "placed_at": "2026-06-12T10:00:00Z",
        "status": "completed",
        "line_items": [{"sku": f"SKU-LONGER-NAME-{k:04d}", "unit_price_usd": "120.00", "quantity": 1} for k in range(30)],
        "discounts": [{"code": "AFF-EXTEND10", "percent_off": 10.0, "amount_off_usd": None}],
        "affiliate": {"affiliate_id": "aff_stale_3", "click_timestamp": "2026-06-01T09:00:00Z",
                      "order_timestamp": "2026-06-12T10:00:00Z", "attribution_window_hours": 24},
        "source_platform": "shopify", "recovery_attempted": False,
    }
    body = json.dumps({"client_id": TENANT, "orders": [{**order, "order_id": f"ord_{i:06d}"} for i in range(1000)]}).encode()
    assert len(body) > 2.1 * MIB  # the finding's 2.12 MiB
    r = post("/agents/affiliate-coupon-extension/detect", body)
    assert r.status_code == 200, (r.status_code, r.text[:300])


# --- worst case per route ---------------------------------------------------------

@pytest.mark.parametrize("path", sorted(ROUTE_REQUEST_MODELS))
def test_worst_case_legal_batch_is_accepted_on_every_route(path):
    model = ROUTE_REQUEST_MODELS[path]
    body = worst_body(FIELD_OF[model])
    bound = worst_case_json_bytes(model)
    # The builder is legal and reaches the computed bound (within 5%), and
    # the route's limit is at least that bound.
    assert 0.95 * bound <= len(body) <= bound, (len(body), bound)
    assert len(body) <= ROUTE_BODY_LIMITS[path]
    r = post(path, body)
    assert r.status_code == 200, (r.status_code, r.text[:500])
    if path == "/agents/discount-misuse/detect":
        # every order stacks 10 max-length codes: the longest description an
        # agent writes still fits the Finding's own limit. The 1,000 orders are
        # one repeated row, so they are one finding (E-10: same entity, same
        # finding -> reported once).
        findings = r.json()["findings"]
        assert len(findings) == 1
        assert all(len(f["cause_description"]) <= L.CAUSE_DESCRIPTION_MAX_CHARS for f in findings)


@pytest.mark.parametrize("path", sorted(ROUTE_REQUEST_MODELS))
def test_one_byte_over_the_route_limit_is_413(path):
    limit = ROUTE_BODY_LIMITS[path]
    r = client.request("POST", path, content=b" " * (limit + 1), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert str(limit) in r.json()["detail"]


def test_route_limits_are_the_computed_worst_case_plus_headroom():
    for path, model in ROUTE_REQUEST_MODELS.items():
        worst = worst_case_json_bytes(model)
        limit = ROUTE_BODY_LIMITS[path]
        assert limit == body_limit_for(model)
        assert limit >= worst * HEADROOM and limit % MIB == 0 and limit < worst * HEADROOM + MIB
    assert api.MAX_BODY_BYTES == max(ROUTE_BODY_LIMITS.values())
    # The numbers as documented in ADR 0001 "Request limits".
    # Oct 6 2026: orders 36 -> 35 MiB (ASCII-safe identifiers, E-12); findings 11 -> 13 MiB (each
    # finding now carries client_id, period_label, evidence_class and a methodology note, E-3/E-4);
    # subscriptions 2 -> 1 MiB.
    assert ROUTE_BODY_LIMITS["/agents/discount-misuse/detect"] == 35 * MIB
    assert ROUTE_BODY_LIMITS["/correlation/overlaps"] == 13 * MIB
    assert ROUTE_BODY_LIMITS["/agents/renewal-never-triggered/detect"] == 1 * MIB
    assert ROUTE_BODY_LIMITS["/agents/server-side-attribution/detect"] == 1 * MIB


def test_every_body_route_is_in_the_limit_table():
    body_routes = {r.path for r in app.routes if "POST" in getattr(r, "methods", set())}
    assert body_routes == set(ROUTE_REQUEST_MODELS)


def test_a_model_field_without_a_limit_cannot_be_sized():
    from pydantic import BaseModel

    class Unbounded(BaseModel):
        name: str

    class UnboundedList(BaseModel):
        xs: list[int]

    for m in (Unbounded, UnboundedList):
        with pytest.raises(UnboundedField):
            worst_case_json_bytes(m)


@pytest.mark.parametrize("field,value,loc", [
    ("line_items", None, ["body", "orders", 0, "line_items"]),
    ("discounts", None, ["body", "orders", 0, "discounts"]),
    ("order_id", s(L.ID_MAX_CHARS + 1), ["body", "orders", 0, "order_id"]),
], ids=["51-line-items", "11-discounts", "65-char-order-id"])
def test_over_a_field_limit_is_422_naming_the_field(field, value, loc):
    order = worst_order()
    if field == "line_items":
        order["line_items"] = order["line_items"] + [order["line_items"][0]]
    elif field == "discounts":
        order["discounts"] = order["discounts"] + [order["discounts"][0]]
    else:
        order[field] = value
    r = post("/agents/discount-misuse/detect", compact({"client_id": TENANT, "orders": [order]}))
    assert r.status_code == 422
    assert any(d["loc"] == loc and d["type"] in ("too_long", "string_too_long") for d in r.json()["detail"]), r.json()


def test_paths_without_a_body_keep_a_small_limit():
    anon = TestClient(app)
    r = anon.request("GET", "/health", content=b" " * (DEFAULT_BODY_BYTES + 1))
    assert r.status_code == 413


# --- heavy-request concurrency cap -----------------------------------------------

def _run_concurrently(mw, requests):
    """Drive the middleware directly: each request's body arrives only when
    released, so the first ones are held in flight while the others arrive."""
    results: dict[int, dict] = {}

    async def run():
        release = asyncio.Event()

        async def one(i, headers):
            sent = []

            async def receive():
                await release.wait()
                return {"type": "http.request", "body": b"{}", "more_body": False}

            async def send(m):
                sent.append(m)

            scope = {"type": "http", "method": "POST", "path": "/echo", "raw_path": b"/echo", "query_string": b"",
                     "headers": headers, "http_version": "1.1", "scheme": "http", "server": ("t", 80),
                     "client": ("c", 1), "root_path": ""}
            await mw(scope, receive, send)
            results[i] = {"status": sent[0]["status"], "headers": dict(sent[0]["headers"])}

        tasks = [asyncio.create_task(one(i, h)) for i, h in enumerate(requests)]
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(run())
    return results


def _echo_app():
    inner = FastAPI()

    @inner.post("/echo")
    async def echo(request: Request):
        return {"n": len(await request.body())}  # reads the body: stays in flight until it arrives

    return inner


def test_heavy_requests_over_the_cap_get_503_with_retry_after():
    mw = _BodyLimitMiddleware(_echo_app(), route_limits={"/echo": 10 * MIB}, heavy_body=1000, max_heavy=2)
    big = [(b"content-type", b"application/json"), (b"content-length", b"5000")]
    res = _run_concurrently(mw, [big] * 5)
    statuses = sorted(r["status"] for r in res.values())
    # Content-Length says 5000 but the body is "{}": the app still answers
    # (200 or a 4xx); what matters is that exactly 3 were turned away.
    assert statuses.count(503) == 3, statuses
    busy = [r for r in res.values() if r["status"] == 503]
    assert all(r["headers"][b"retry-after"] == b"1" for r in busy)
    assert mw.heavy_in_flight == 0  # released after each response


def test_chunked_body_counts_as_heavy_and_small_bodies_are_never_capped():
    mw = _BodyLimitMiddleware(_echo_app(), route_limits={"/echo": 10 * MIB}, heavy_body=1000, max_heavy=1)
    chunked = [(b"content-type", b"application/json"), (b"transfer-encoding", b"chunked")]
    small = [(b"content-type", b"application/json"), (b"content-length", b"2")]
    res = _run_concurrently(mw, [chunked, chunked, small, small, small, small])
    assert [res[i]["status"] for i in range(6)] == [200, 503, 200, 200, 200, 200]
    assert mw.heavy_in_flight == 0
