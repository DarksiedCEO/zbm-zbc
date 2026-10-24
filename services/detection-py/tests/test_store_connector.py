"""ADR 0020: the read-only Shopify store connector. Fake GraphQL only: no network, no wall clock, no /proc."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import timedelta
from decimal import Decimal

import httpcore
import httpx
import pytest

from _store_fakes import (CC_ID, CC_SECRET, CC_TOKEN, NOW, SECRETS, TOKEN, TOKEN_B, Clock, FakeShopify,
                          MemoryLedger, code_app, conn, cost, money, order, write_connections, write_secret)
from agents import discount_misuse
from store_connector import shopify_models as m
from store_connector.config import Secret, load_connections, read_secret_file
from store_connector.net import DeadlineBackend, DnsGate, Resolver
from store_connector.reasons import NO_OUTBOUND, REASONS, StoreError
from store_connector.service import StoreService
from store_connector.shop_domain import ShopDomainError, graphql_url, shop_handle
from store_connector.shopify_client import ShopifySession, check_scopes
from store_connector.translate import CUSTOMER_NOT_COLLECTED, translate
from zbm_schema import EvidenceClass, Order


# ---- harness -------------------------------------------------------------------------------------------------

def _service(tmp_path, fake: FakeShopify, conns=None, *, ledger=None, clock=None, now=NOW, kill_file=None,
             disabled=False, deadline=120.0):
    if conns is None:
        tok = write_secret(tmp_path, "acme.token", TOKEN)
        conns = [conn("acme", "acme-store", tok)]
    path = write_connections(tmp_path, conns) if conns != "none" else None
    clock = clock or Clock()
    holder = {"now": now}
    svc = StoreService(connections_file=path, kill_file=kill_file, disabled=disabled,
                       ledger=ledger if ledger is not None else MemoryLedger(), transport=fake.transport,
                       monotonic=clock.monotonic, sleep=clock.sleep, now=lambda: holder["now"],
                       sync_deadline_s=deadline, new_sync_id=iter(f"{i:032x}" for i in range(1, 99)).__next__)
    svc._holder, svc._clock = holder, clock
    return svc


def _session(fake: FakeShopify, clock: Clock | None = None, deadline=60.0, **kw) -> ShopifySession:
    clock = clock or Clock()
    s = ShopifySession("acme-store", deadline_s=deadline, transport=fake.transport, monotonic=clock.monotonic,
                       sleep=clock.sleep, **kw)
    s.use_static_token(Secret(TOKEN))
    return s


def _err(fn, *a, **kw) -> str:
    with pytest.raises(StoreError) as e:
        fn(*a, **kw)
    return e.value.code


def _stacked(n: int) -> dict:
    return order(n, codes=[code_app("SAVE10", pct=10.0), code_app("TAKE5", amount="5.00")])


# ---- reason codes ----------------------------------------------------------------------------------------------

def test_reason_codes_are_closed_and_messages_fixed():
    assert NO_OUTBOUND <= set(REASONS)
    with pytest.raises(AssertionError):
        StoreError("SOMETHING_ELSE")
    e = StoreError("NOT_CONNECTED")
    assert e.detail() == {"reason": "NOT_CONNECTED", "message": REASONS["NOT_CONNECTED"][1]}
    assert repr(e) == "StoreError(NOT_CONNECTED)"


# ---- SSRF: the shop domain and the resolved addresses ---------------------------------------------------------

@pytest.mark.parametrize("raw", ["acme-store", "ACME-STORE", "acme-store.myshopify.com", "Acme-Store.MyShopify.com",
                                 "a1b", "x" * 60, "localhost"])
def test_valid_shop_handles(raw):
    assert shop_handle(raw) == raw.lower().removesuffix(".myshopify.com")


@pytest.mark.parametrize("raw", [
    "", "a", "-acme", "acme-", "acme.store", "acme_store", "acme store", "evil.com", "acme.myshopify.com.evil.com",
    "a.b.myshopify.com", "https://acme.myshopify.com", "acme.myshopify.com/", "acme.myshopify.com:443",
    "acme.myshopify.com.", "user@acme.myshopify.com", "acme.myshopify.com?x=1", "127.0.0.1", "[::1]", "169.254.169.254",
    "ab", "xn--acme-9ra", "ab--cd", "x" * 61, "acmeK", "Kacme",  # Kelvin sign folds to "k"
    "acme\x00.myshopify.com", "acme%2emyshopify.com", "myshopify.com", ".myshopify.com", None, 123,
])
def test_ssrf_shop_domains_are_refused(raw):
    with pytest.raises(ShopDomainError):
        shop_handle(raw)


def test_the_url_is_built_only_from_the_handle():
    assert graphql_url("acme-store", m.API_VERSION) == \
        f"https://acme-store.myshopify.com/admin/api/{m.API_VERSION}/graphql.json"


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
                                  "0.0.0.0", "::1", "fe80::1", "fc00::1", "224.0.0.1", "::ffff:127.0.0.1"])
def test_resolver_refuses_non_public_addresses(addr):
    r = Resolver(lambda h, p: [addr], DnsGate(2))
    with pytest.raises(httpcore.ConnectError):
        r("acme-store.myshopify.com", 443, 5.0)


def test_resolver_refuses_address_literal_hosts_and_returns_public_addresses():
    r = Resolver(lambda h, p: ["23.227.38.65", "23.227.38.65"], DnsGate(2))
    assert r("acme-store.myshopify.com", 443, 5.0) == ["23.227.38.65"]
    with pytest.raises(httpcore.ConnectError):
        r("23.227.38.65", 443, 5.0)


def test_backend_never_connects_to_a_private_answer():
    class Inner:
        calls = []

        def connect_tcp(self, *a, **kw):
            self.calls.append(a)
            raise AssertionError("must not connect")

    clock = Clock()
    inner = Inner()
    be = DeadlineBackend(clock.monotonic, clock.t + 10, inner=inner, resolver=Resolver(lambda h, p: ["10.1.2.3"]))
    with pytest.raises(httpcore.ConnectError):
        be.connect_tcp("acme-store.myshopify.com", 443)
    assert inner.calls == []


def test_dns_gate_refuses_when_busy():
    gate = DnsGate(1)
    slot = gate.try_take()
    with pytest.raises(httpcore.ConnectError):
        Resolver(lambda h, p: ["23.227.38.65"], gate)("acme-store.myshopify.com", 443, 5.0)
    slot.release()
    slot.release()                                   # released once only
    assert gate.busy == 0


# ---- success, pagination, translation --------------------------------------------------------------------------

def test_success_paginates_with_cursors_and_translates(tmp_path):
    nodes = [_stacked(i) if i % 5 == 0 else order(i) for i in range(23)]
    fake = FakeShopify(orders=nodes)
    svc = _service(tmp_path, fake)
    out = svc.sync("acme")
    assert out["orders_read"] == 23 and out["orders_analysed"] == 23 and out["truncated_by"] is None
    order_calls = [b for b in fake.graphql_bodies if b["query"] == m.ORDERS_QUERY]
    assert [b["variables"]["after"] for b in order_calls] == [None, "c:10", "c:20"]
    assert all(b["variables"]["first"] == m.ORDERS_PAGE_SIZE for b in order_calls)
    assert order_calls[0]["variables"]["q"] == "created_at:>='2026-08-25T12:00:00Z'"
    body = json.loads(svc.snapshot("acme"))
    assert body["client_id"] == "acme" and body["snapshot_id"] == out["snapshot_id"]
    orders = [Order.model_validate(o) for o in body["orders"]]
    findings = discount_misuse.detect(orders, client_id="acme")
    assert len(findings) == 5
    f = findings[0]
    assert f.evidence_class == EvidenceClass.OBSERVED and f.customer_id == CUSTOMER_NOT_COLLECTED
    # $100 subtotal: 10% = $10, then $5 -> stack $15; best single code $10 -> $5.00 beyond policy
    assert f.recoverable_value.amount_usd == Decimal("5.00")
    assert f.entity_id.startswith("shopify-order-")


def test_max_orders_truncates_and_records_the_window(tmp_path):
    fake = FakeShopify(orders=[order(i) for i in range(30)])
    tok = write_secret(tmp_path, "t", TOKEN)
    svc = _service(tmp_path, fake, [conn("acme", "acme-store", tok, max_orders=15)])
    out = svc.sync("acme")
    assert out["orders_read"] == 15 and out["truncated_by"] == "max_orders"
    calls = [b["variables"]["first"] for b in fake.graphql_bodies if b["query"] == m.ORDERS_QUERY]
    assert calls == [10, 5]
    cov = json.loads(svc.snapshot("acme"))["coverage"]
    assert cov["window"]["truncated_by"] == "max_orders"
    assert cov["window"]["oldest_order_read"] < cov["window"]["newest_order_read"]


def test_cursor_that_does_not_advance_is_refused():
    def same_cursor(req):
        return FakeShopify().json({"data": {"orders": {"pageInfo": {"hasNextPage": True, "endCursor": None},
                                                       "nodes": [order(1)]}}, "extensions": cost()})
    fake = FakeShopify(orders=[], script=[None, same_cursor])
    s = _session(fake)
    s.preflight()
    assert _err(s.read_orders, now=NOW, lookback_days=30, max_orders=100, max_cost_points=10_000) == \
        "STORE_RESPONSE_INVALID"


def test_lookback_beyond_60_days_needs_read_all_orders(tmp_path):
    fake = FakeShopify(orders=[order(1)])
    s = _session(fake)
    s.preflight()
    r = s.read_orders(now=NOW, lookback_days=200, max_orders=10, max_cost_points=10_000)
    assert r.lookback_days == 60 and r.lookback_limited_by_scope
    fake2 = FakeShopify(orders=[order(1)], scopes=("read_orders", "read_all_orders"))
    s2 = _session(fake2)
    s2.preflight()
    r2 = s2.read_orders(now=NOW, lookback_days=200, max_orders=10, max_cost_points=10_000)
    assert r2.lookback_days == 200 and not r2.lookback_limited_by_scope


def test_translation_excludes_what_the_agent_would_misstate():
    nodes = [order(1, test=True), order(2, cancelled=True), order(3, status="REFUNDED"), order(4, status=None),
             order(5, more_lines=True),
             order(6, codes=[code_app("SHIPFREE", pct=100.0, target="SHIPPING_LINE"), code_app("A", pct=5.0)]),
             order(7, codes=[code_app("ONLYHATS", pct=20.0, sel="ENTITLED"), code_app("B", pct=5.0)]),
             order(8, codes=[code_app("EACH5", amount="5.00", alloc="EACH"), code_app("C", pct=5.0)]),
             order(9, lines=[{"sku": "X", "quantity": 1, "originalUnitPriceSet": money("1.005")}]),
             order(10, lines=[{"sku": None, "quantity": 1, "originalUnitPriceSet": money("0.00")}]),
             order(11, codes=[{"__typename": "AutomaticDiscountApplication", "allocationMethod": "ACROSS",
                               "targetSelection": "ALL", "targetType": "LINE_ITEM",
                               "value": {"__typename": "PricingPercentageValue", "percentage": 10.0}}]),
             order(12, codes=[code_app("Z" * 65, pct=5.0)]),
             order(1)]
    parsed = [m.OrderNode.model_validate_json(json.dumps(n)) for n in nodes]
    orders, counts = translate(parsed)
    assert [o.order_id for o in orders] == ["shopify-order-5010", "shopify-order-5011"]
    assert counts["orders_excluded"] == {
        "cancelled": 1, "discount_code_format": 1, "duplicate_in_read": 1, "financial_status_refunded": 1,
        "financial_status_unknown": 1, "line_items_over_cap": 1, "money_format_or_currency": 1,
        "per_item_fixed_discount_not_modelled": 1, "shipping_discount_code_not_modelled": 1,
        "targeted_discount_code_not_modelled": 1, "test_order": 1}
    assert counts["line_items_dropped_zero_value"] == 1 and counts["non_code_discounts_left_out"] == 1
    assert all(o.customer_id == CUSTOMER_NOT_COLLECTED for o in orders)


# ---- throttling -----------------------------------------------------------------------------------------------

def _throttled(requested=500, available=100, restore=50):
    def resp(req):
        return FakeShopify().json({"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                                   "extensions": cost(requested=requested, actual=0, available=available,
                                                      restore=restore)})
    return resp


def test_throttled_waits_from_extensions_cost_then_succeeds():
    clock = Clock()
    fake = FakeShopify(orders=[order(1)], script=[None, _throttled(), _throttled(requested=150, available=100)])
    s = _session(fake, clock)
    s.preflight()
    r = s.read_orders(now=NOW, lookback_days=30, max_orders=10, max_cost_points=10_000)
    assert len(r.orders) == 1
    assert clock.sleeps == [8.0, 1.0]            # (500-100)/50 = 8 s; (150-100)/50 = 1 s
    assert r.stats.throttle_waits == 2


def test_throttle_wait_is_capped_and_retries_are_bounded():
    clock = Clock()
    fake = FakeShopify(orders=[order(1)], script=[None] + [_throttled(requested=1000, available=0, restore=1)] * 20)
    s = _session(fake, clock, deadline=600.0)
    s.preflight()
    assert _err(s.read_orders, now=NOW, lookback_days=30, max_orders=10, max_cost_points=10_000) == "STORE_THROTTLED"
    assert clock.sleeps == [10.0] * 8             # each wait capped at 10 s, 8 retries, then stop


def test_http_429_backs_off_one_second():
    clock = Clock()
    fake = FakeShopify(orders=[order(1)], script=[None, httpx.Response(429, content=b"")])
    s = _session(fake, clock)
    s.preflight()
    s.read_orders(now=NOW, lookback_days=30, max_orders=10, max_cost_points=10_000)
    assert clock.sleeps == [1.0]


def test_paces_between_pages_when_the_bucket_runs_low():
    clock = Clock()

    def low(req):
        return FakeShopify().json({"data": {"orders": {"pageInfo": {"hasNextPage": True, "endCursor": "c:1"},
                                                       "nodes": [order(1)]}},
                                   "extensions": cost(requested=500, actual=400, available=200, restore=100)})
    fake = FakeShopify(orders=[order(1), order(2)], script=[None, low])
    s = _session(fake, clock)
    s.preflight()
    r = s.read_orders(now=NOW, lookback_days=30, max_orders=10, max_cost_points=10_000)
    assert clock.sleeps == [3.0] and len(r.orders) == 2


def test_throttle_wait_past_the_deadline_stops_without_sleeping():
    clock = Clock()
    fake = FakeShopify(orders=[order(1)], script=[None, _throttled(requested=1000, available=0, restore=100)])
    s = _session(fake, clock, deadline=5.0)
    s.preflight()
    assert _err(s.read_orders, now=NOW, lookback_days=30, max_orders=10, max_cost_points=10_000) == \
        "STORE_DEADLINE_EXCEEDED"
    assert clock.sleeps == []


def test_max_cost_exceeded_halves_the_page_size():
    too_costly = FakeShopify().json({"errors": [{"message": "Query cost is 2003, which exceeds the single query max "
                                                 "cost limit (1000).", "extensions": {"code": "MAX_COST_EXCEEDED"}}]})
    fake = FakeShopify(orders=[order(i) for i in range(4)], script=[None, too_costly, too_costly])
    s = _session(fake)
    s.preflight()
    r = s.read_orders(now=NOW, lookback_days=30, max_orders=100, max_cost_points=10_000)
    firsts = [b["variables"]["first"] for b in fake.graphql_bodies if b["query"] == m.ORDERS_QUERY]
    assert firsts == [10, 5, 2, 2] and len(r.orders) == 4 and r.stats.page_size_final == 2


def test_max_cost_exceeded_at_page_size_one_fails():
    too_costly = FakeShopify().json({"errors": [{"message": "x", "extensions": {"code": "MAX_COST_EXCEEDED"}}]})
    fake = FakeShopify(orders=[order(1)], script=[None] + [too_costly] * 10)
    s = _session(fake)
    s.preflight()
    assert _err(s.read_orders, now=NOW, lookback_days=30, max_orders=100, max_cost_points=10_000) == \
        "STORE_QUERY_TOO_COSTLY"


def test_cost_budget_truncates_the_read(tmp_path):
    fake = FakeShopify(orders=[order(i) for i in range(40)])
    s = _session(fake)
    s.preflight()
    r = s.read_orders(now=NOW, lookback_days=30, max_orders=1000, max_cost_points=100)
    assert r.truncated_by == "cost_budget" and len(r.orders) == 20     # 2 + 50 + 50 points


# ---- HTTP and GraphQL failures ---------------------------------------------------------------------------------

@pytest.mark.parametrize("status,code", [(401, "STORE_AUTH_REFUSED"), (403, "STORE_AUTH_REFUSED"),
                                         (402, "STORE_SHOP_UNAVAILABLE"), (423, "STORE_SHOP_UNAVAILABLE"),
                                         (404, "STORE_RESPONSE_INVALID"), (302, "STORE_RESPONSE_INVALID"),
                                         (400, "STORE_RESPONSE_INVALID")])
def test_http_statuses(status, code):
    fake = FakeShopify(script=[httpx.Response(status, headers={"location": "https://evil.example/"}, content=b"{}")])
    assert _err(_session(fake).preflight) == code
    assert len(fake.requests) == 1               # never retried, redirect never followed


@pytest.mark.parametrize("gql_code,code", [("ACCESS_DENIED", "STORE_AUTH_REFUSED"),
                                           ("SHOP_INACTIVE", "STORE_SHOP_UNAVAILABLE"),
                                           ("SOMETHING_NEW", "STORE_RESPONSE_INVALID")])
def test_graphql_error_codes(gql_code, code):
    fake = FakeShopify(script=[FakeShopify().json({"errors": [{"message": "m", "extensions": {"code": gql_code}}]})])
    assert _err(_session(fake).preflight) == code


def test_5xx_is_retried_a_bounded_number_of_times():
    clock = Clock()
    fake = FakeShopify(script=[httpx.Response(503, content=b"")] * 5)
    assert _err(_session(fake, clock).preflight) == "STORE_UNAVAILABLE"
    assert len(fake.requests) == 3 and clock.sleeps == [1.0, 1.0]


def test_connect_errors_are_retried_then_unavailable():
    def boom(req):
        raise httpx.ConnectError("refused")
    fake = FakeShopify(script=[boom] * 5)
    assert _err(_session(fake).preflight) == "STORE_UNAVAILABLE"


@pytest.mark.parametrize("body,headers", [
    (b"not json", {}),
    (b'{"data": {"shop": {"currencyCode": "USD"}}}', {}),
    (b"[]", {}),
    (b'{"data": null}', {}),
    (b'{"data":{"shop":{"currencyCode":"USD","myshopifyDomain":"acme-store.myshopify.com"},'
     b'"currentAppInstallation":{"accessScopes":[{"handle":"read_orders"}]}},'
     b'"errors":[{"message":"partial"}]}', {}),
    (b"{}", {"content-type": "text/html"}),
    (b"{}", {"content-encoding": "gzip"}),
    (b'{"a":' * 5000 + b"1" + b"}" * 5000, {}),
])
def test_malformed_bodies_are_refused(body, headers):
    h = {"content-type": "application/json", "x-shopify-api-version": m.API_VERSION, **headers}
    fake = FakeShopify(script=[httpx.Response(200, stream=httpx.ByteStream(body), headers=h)])
    assert _err(_session(fake).preflight) == "STORE_RESPONSE_INVALID"


def test_huge_body_is_refused_at_the_cap():
    big = b'{"data": {"x": "' + b"a" * (3 * 1024 * 1024) + b'"}}'
    fake = FakeShopify(script=[httpx.Response(200, content=big, headers={"content-type": "application/json",
                                                                          "x-shopify-api-version": m.API_VERSION})])
    assert _err(_session(fake).preflight) == "STORE_RESPONSE_INVALID"


def test_api_version_fall_forward_is_refused():
    fake = FakeShopify(version="2027-01")
    assert _err(_session(fake).preflight) == "STORE_API_VERSION_MISMATCH"


def test_an_order_node_with_unrequested_fields_is_refused():
    pii = order(1, extra={"email": "jane@example.com", "customer": {"firstName": "Jane"}})
    fake = FakeShopify(orders=[pii])
    s = _session(fake)
    s.preflight()
    assert _err(s.read_orders, now=NOW, lookback_days=30, max_orders=10, max_cost_points=10_000) == \
        "STORE_RESPONSE_INVALID"


def test_shop_identity_and_currency_are_checked():
    def other_shop(req):
        return FakeShopify().json({"data": {"shop": {"currencyCode": "USD", "myshopifyDomain": "other.myshopify.com"},
                                            "currentAppInstallation": {"accessScopes": [{"handle": "read_orders"}]}}})
    assert _err(_session(FakeShopify(script=[other_shop])).preflight) == "STORE_SHOP_MISMATCH"
    assert _err(_session(FakeShopify(currency="CAD")).preflight) == "STORE_CURRENCY_UNSUPPORTED"


class _Drip(httpx.SyncByteStream):
    def __init__(self, clock: Clock, step: float, n: int):
        self.clock, self.step, self.n = clock, step, n

    def __iter__(self):
        for _ in range(self.n):
            self.clock.t += self.step                # the clock moves while the body trickles in
            yield b" "


def test_slow_drip_body_stops_at_the_deadline():
    clock = Clock()

    def drip(req):
        return httpx.Response(200, headers={"content-type": "application/json",
                                            "x-shopify-api-version": m.API_VERSION}, stream=_Drip(clock, 1.0, 10_000))
    fake = FakeShopify(script=[drip])
    start = clock.t
    assert _err(_session(fake, clock, deadline=30.0).preflight) == "STORE_DEADLINE_EXCEEDED"
    assert clock.t - start <= 31.0


def test_deadline_covers_the_whole_session():
    clock = Clock()
    fake = FakeShopify(orders=[order(i) for i in range(30)])
    s = _session(fake, clock, deadline=10.0)
    s.preflight()
    clock.t += 11.0
    assert _err(s.read_orders, now=NOW, lookback_days=30, max_orders=100, max_cost_points=10_000) == \
        "STORE_DEADLINE_EXCEEDED"


# ---- scopes -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("scopes,code", [
    ({"read_orders", "write_orders"}, "STORE_TOKEN_HAS_WRITE_SCOPE"),
    ({"write_orders"}, "STORE_TOKEN_HAS_WRITE_SCOPE"),
    ({"read_orders", "write_products"}, "STORE_TOKEN_HAS_WRITE_SCOPE"),
    ({"read_orders", "read_customers"}, "STORE_TOKEN_EXCESS_SCOPE"),
    ({"read_orders", "read_products"}, "STORE_TOKEN_EXCESS_SCOPE"),
    ({"read_all_orders"}, "STORE_SCOPE_MISSING"),
    (set(), "STORE_SCOPE_MISSING"),
])
def test_scope_policy(scopes, code):
    assert _err(check_scopes, scopes) == code


def test_scope_policy_accepts_the_minimum():
    check_scopes({"read_orders"})
    check_scopes({"read_orders", "read_all_orders"})


def test_write_scope_token_is_refused_before_any_order_is_read(tmp_path):
    fake = FakeShopify(orders=[order(1)], scopes=("read_orders", "write_orders"))
    svc = _service(tmp_path, fake)
    assert _err(svc.sync, "acme") == "STORE_TOKEN_HAS_WRITE_SCOPE"
    assert [b["query"] for b in fake.graphql_bodies] == [m.PREFLIGHT_QUERY]
    assert _err(svc.snapshot, "acme") == "NOT_SYNCED"


def test_queries_are_read_only_constants_without_customer_fields():
    for q in (m.PREFLIGHT_QUERY, m.ORDERS_QUERY):
        assert q.startswith("query ") and "mutation" not in q
        words = set(re.findall(r"[A-Za-z_]+", q))
        assert not words & set(m.FORBIDDEN_FIELDS), words & set(m.FORBIDDEN_FIELDS)


# ---- client credentials -----------------------------------------------------------------------------------------

def _cc_conns(tmp_path):
    return [{"client_id": "acme", "platform": "shopify", "shop": "acme-store.myshopify.com",
             "credential": {"kind": "client_credentials",
                            "client_id_file": write_secret(tmp_path, "cid", CC_ID),
                            "client_secret_file": write_secret(tmp_path, "csec", CC_SECRET)},
             "approval": {"approved_by": "andre", "approved_on": "2026-10-24", "approval_ref": "a-2"}}]


def test_client_credentials_are_exchanged_for_a_session_token(tmp_path):
    fake = FakeShopify(orders=[_stacked(1)], tokens={})
    svc = _service(tmp_path, fake, _cc_conns(tmp_path))
    assert svc.sync("acme")["orders_analysed"] == 1
    first = fake.requests[0]
    assert first.url.path == "/admin/oauth/access_token" and first.method == "POST"
    assert first.headers["content-type"] == "application/x-www-form-urlencoded"
    assert all(r.headers.get("x-shopify-access-token") == CC_TOKEN for r in fake.requests[1:])


def test_client_credentials_with_a_write_scope_are_refused_before_graphql(tmp_path):
    fake = FakeShopify(orders=[order(1)], tokens={}, cc_scope="read_orders,write_orders")
    svc = _service(tmp_path, fake, _cc_conns(tmp_path))
    assert _err(svc.sync, "acme") == "STORE_TOKEN_HAS_WRITE_SCOPE"
    assert fake.graphql_bodies == []


def test_client_credentials_refused():
    fake = FakeShopify(tokens={})
    s = ShopifySession("acme-store", deadline_s=30, transport=fake.transport)
    assert _err(s.exchange_client_credentials, Secret("wrong-id-123"), Secret("wrong-secret-123")) == \
        "STORE_AUTH_REFUSED"


# ---- service: NOT_CONNECTED, kill switch, budgets, record-first, credentials ------------------------------------

def test_not_connected_makes_no_outbound_call(tmp_path):
    fake = FakeShopify()
    svc = _service(tmp_path, fake, "none")
    assert svc.status("acme")["connected"] is False and svc.status("acme")["reason"] == "NOT_CONNECTED"
    assert _err(svc.sync, "acme") == "NOT_CONNECTED"
    assert _err(svc.snapshot, "acme") == "NOT_CONNECTED"
    assert fake.requests == []


def test_kill_switch_file_stops_reads_at_once_and_mid_read(tmp_path):
    kill = os.path.join(str(tmp_path), "KILL")
    nodes = [order(i) for i in range(30)]

    fake = FakeShopify(orders=nodes)
    orig = fake.handle
    armed = [True]

    def handle(req):
        body = json.loads(req.content)
        resp = orig(req)
        if body["query"] == m.ORDERS_QUERY and armed:
            armed.pop()
            open(kill, "w").close()               # switched on after the first page
        return resp
    fake.handle = handle
    svc = _service(tmp_path, fake, kill_file=kill)
    assert _err(svc.sync, "acme") == "STORE_READS_DISABLED"
    n = len(fake.requests)
    assert n == 2                                 # preflight + one page, then stopped
    assert _err(svc.sync, "acme") == "STORE_READS_DISABLED"
    assert _err(svc.snapshot, "acme") == "STORE_READS_DISABLED"
    assert len(fake.requests) == n
    os.remove(kill)
    svc.sync("acme")


def test_disabled_env_switch(tmp_path):
    fake = FakeShopify()
    svc = _service(tmp_path, fake, disabled=True)
    assert _err(svc.sync, "acme") == "STORE_READS_DISABLED" and fake.requests == []


def test_daily_budget_is_per_tenant_and_resets_next_day(tmp_path):
    fake = FakeShopify(shops={"acme-store": [order(1)], "bravo-store": [order(2)]},
                       tokens={"acme-store": TOKEN, "bravo-store": TOKEN_B})
    conns = [conn("acme", "acme-store", write_secret(tmp_path, "a", TOKEN), syncs_per_day=1),
             conn("bravo", "bravo-store", write_secret(tmp_path, "b", TOKEN_B), syncs_per_day=1)]
    svc = _service(tmp_path, fake, conns)
    svc.sync("acme")
    n = len(fake.requests)
    assert _err(svc.sync, "acme") == "STORE_BUDGET_EXHAUSTED" and len(fake.requests) == n
    svc.sync("bravo")                              # another tenant's budget is its own
    svc._holder["now"] = NOW + timedelta(days=1)
    svc.sync("acme")


def test_record_first_no_ledger_means_no_read(tmp_path):
    from store_connector.ledger import UnconfiguredLedger
    fake = FakeShopify(orders=[order(1)])
    svc = _service(tmp_path, fake, ledger=UnconfiguredLedger())
    assert _err(svc.sync, "acme") == "LEDGER_UNAVAILABLE" and fake.requests == []


def test_snapshot_kept_only_after_the_completion_record(tmp_path):
    fake = FakeShopify(orders=[order(1)])
    ledger = MemoryLedger(fail_types=("rr_store_read_completed",))
    svc = _service(tmp_path, fake, ledger=ledger)
    assert _err(svc.sync, "acme") == "LEDGER_UNAVAILABLE"
    assert _err(svc.snapshot, "acme") == "NOT_SYNCED"
    assert [e["event_type"] for e in ledger.events] == ["rr_store_read_requested"]


def test_ledger_evidence_of_a_sync(tmp_path):
    fake = FakeShopify(orders=[_stacked(1), order(2)])
    ledger = MemoryLedger()
    svc = _service(tmp_path, fake, ledger=ledger)
    out = svc.sync("acme")
    req, done = ledger.events
    assert req["event_id"] == f"rrs.{1:032x}.requested" and done["event_id"] == f"rrs.{1:032x}.completed"
    assert req["subject_id"] == done["subject_id"] == "acme"
    assert done["payload"]["snapshot_id"] == out["snapshot_id"] and done["payload"]["orders_read"] == 2
    assert done["payload"]["granted_scopes"] == ["read_orders"]


def test_failed_sync_is_recorded_with_its_reason(tmp_path):
    fake = FakeShopify(script=[httpx.Response(401, content=b"")])
    ledger = MemoryLedger()
    svc = _service(tmp_path, fake, ledger=ledger)
    assert _err(svc.sync, "acme") == "STORE_AUTH_REFUSED"
    assert [e["event_type"] for e in ledger.events] == ["rr_store_read_requested", "rr_store_read_failed"]
    assert ledger.events[1]["payload"]["reason"] == "STORE_AUTH_REFUSED"


def test_snapshot_expires_after_its_max_age(tmp_path):
    fake = FakeShopify(orders=[order(1)])
    tok = write_secret(tmp_path, "t", TOKEN)
    svc = _service(tmp_path, fake, [conn("acme", "acme-store", tok, snapshot_max_age_hours=2)])
    svc.sync("acme")
    svc._holder["now"] = NOW + timedelta(hours=3)
    assert _err(svc.snapshot, "acme") == "SNAPSHOT_STALE"
    svc._holder["now"] = NOW
    assert _err(svc.snapshot, "acme") == "NOT_SYNCED"          # dropped, not just hidden


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_credential_file_must_be_private(tmp_path, mode):
    fake = FakeShopify()
    tok = write_secret(tmp_path, "t", TOKEN, mode=mode)
    svc = _service(tmp_path, fake, [conn("acme", "acme-store", tok)])
    assert _err(svc.sync, "acme") == "STORE_CREDENTIAL_UNREADABLE" and fake.requests == []


def test_credential_symlink_and_missing_file_refused(tmp_path):
    tok = write_secret(tmp_path, "real", TOKEN)
    link = os.path.join(str(tmp_path), "link")
    os.symlink(tok, link)
    for path in (link, os.path.join(str(tmp_path), "absent"), "relative/path"):
        assert _err(read_secret_file, path) == "STORE_CREDENTIAL_UNREADABLE"


@pytest.mark.parametrize("bad", [
    {"version": 2, "connections": []},
    {"version": 1, "connections": [{"client_id": "acme", "platform": "shopify", "shop": "evil.com",
                                    "credential": {"kind": "static_token", "token_file": "/x"},
                                    "approval": {"approved_by": "andre", "approved_on": "2026-10-24",
                                                 "approval_ref": "r"}}]},
    {"version": 1, "connections": [{"client_id": "acme", "platform": "shopify", "shop": "acme",
                                    "credential": {"kind": "static_token", "token": "shpat_inline"},
                                    "approval": {"approved_by": "andre", "approved_on": "2026-10-24",
                                                 "approval_ref": "r"}}]},
    {"version": 1, "connections": [{"client_id": "acme", "platform": "shopify", "shop": "acme",
                                    "credential": {"kind": "static_token", "token_file": "/x"},
                                    "approval": {"approved_by": "someone", "approved_on": "2026-10-24",
                                                 "approval_ref": "r"}}]},
    {"version": 1, "connections": [{"client_id": "acme", "platform": "shopify", "shop": "acme",
                                    "credential": {"kind": "static_token", "token_file": "rel"},
                                    "approval": {"approved_by": "andre", "approved_on": "2026-10-24",
                                                 "approval_ref": "r"}}]},
])
def test_connections_file_is_strict(tmp_path, bad):
    p = os.path.join(str(tmp_path), "c.json")
    with open(p, "w") as f:
        json.dump(bad, f)
    assert _err(load_connections, p) == "STORE_CONFIG_INVALID"


def test_budget_ceilings_cannot_be_raised(tmp_path):
    p = write_connections(tmp_path, [conn("acme", "acme", "/x", max_orders=5000)])
    assert _err(load_connections, p) == "STORE_CONFIG_INVALID"


# ---- cross-tenant isolation --------------------------------------------------------------------------------------

def test_cross_tenant_isolation(tmp_path):
    fake = FakeShopify(shops={"acme-store": [_stacked(1)], "bravo-store": [order(2), order(3)]},
                       tokens={"acme-store": TOKEN, "bravo-store": TOKEN_B})
    conns = [conn("acme", "acme-store", write_secret(tmp_path, "a", TOKEN)),
             conn("bravo", "bravo-store", write_secret(tmp_path, "b", TOKEN_B))]
    svc = _service(tmp_path, fake, conns)
    svc.sync("acme")
    assert {r.url.host for r in fake.requests} == {"acme-store.myshopify.com"}
    assert {r.headers["x-shopify-access-token"] for r in fake.requests} == {TOKEN}
    assert _err(svc.snapshot, "bravo") == "NOT_SYNCED"
    fake.requests.clear()
    svc.sync("bravo")
    assert {r.url.host for r in fake.requests} == {"bravo-store.myshopify.com"}
    assert {r.headers["x-shopify-access-token"] for r in fake.requests} == {TOKEN_B}
    a, b = json.loads(svc.snapshot("acme")), json.loads(svc.snapshot("bravo"))
    assert a["client_id"] == "acme" and len(a["orders"]) == 1
    assert b["client_id"] == "bravo" and len(b["orders"]) == 2
    assert not {o["order_id"] for o in a["orders"]} & {o["order_id"] for o in b["orders"]}
    # deregister acme: its snapshot is dropped, bravo's is untouched
    write_connections(tmp_path, [conns[1]])
    assert _err(svc.snapshot, "acme") == "NOT_CONNECTED"
    write_connections(tmp_path, conns)
    assert _err(svc.snapshot, "acme") == "NOT_SYNCED"
    assert json.loads(svc.snapshot("bravo"))["client_id"] == "bravo"


def test_one_shop_cannot_be_registered_under_two_tenants(tmp_path):
    tok = write_secret(tmp_path, "a", TOKEN)
    p = write_connections(tmp_path, [conn("acme", "acme-store", tok), conn("bravo", "ACME-STORE.myshopify.com", tok)])
    assert _err(load_connections, p) == "STORE_CONFIG_INVALID"


def test_a_token_for_another_shop_is_refused_by_that_shop(tmp_path):
    # bravo registered with acme's token: bravo's shop refuses it; acme's shop is never contacted
    fake = FakeShopify(shops={"acme-store": [order(1)], "bravo-store": [order(2)]},
                       tokens={"acme-store": TOKEN, "bravo-store": TOKEN_B})
    svc = _service(tmp_path, fake, [conn("bravo", "bravo-store", write_secret(tmp_path, "x", TOKEN))])
    assert _err(svc.sync, "bravo") == "STORE_AUTH_REFUSED"
    assert {r.url.host for r in fake.requests} == {"bravo-store.myshopify.com"}


# ---- the token never leaks; no raw PII is stored ----------------------------------------------------------------

def test_token_never_leaks(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []
    scenarios = [
        FakeShopify(orders=[_stacked(1)]),
        FakeShopify(script=[httpx.Response(401, content=TOKEN.encode())]),            # echoed back by "Shopify"
        FakeShopify(script=[FakeShopify().json({"errors": [{"message": f"bad token {TOKEN}",
                                                            "extensions": {"code": "ACCESS_DENIED"}}]})]),
        FakeShopify(scopes=("read_orders", "write_orders")),
        FakeShopify(script=[httpx.Response(200, content=b"<html>" + TOKEN.encode())]),
    ]
    for i, fake in enumerate(scenarios):
        d = tmp_path / f"s{i}"
        d.mkdir()
        ledger = MemoryLedger()
        svc = _service(d, fake, ledger=ledger)
        try:
            seen.append(json.dumps(svc.sync("acme")))
            seen.append(svc.snapshot("acme").decode())
        except StoreError as e:
            seen += [str(e), repr(e), json.dumps(e.detail())]
        seen += [repr(svc), json.dumps(svc.status("acme")), json.dumps(ledger.events)]
    s = ShopifySession("acme-store", deadline_s=30, transport=FakeShopify().transport)
    s.use_static_token(Secret(TOKEN))
    seen += [repr(s), repr(Secret(TOKEN)), str(Secret(TOKEN)), repr(s.stats)]
    with pytest.raises(TypeError):
        __import__("pickle").dumps(Secret(TOKEN))
    seen.append(caplog.text)
    blob = "\n".join(seen)
    assert TOKEN not in blob and "shpat_" not in blob


def test_client_secret_never_leaks(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeShopify(orders=[order(1)], tokens={})
    ledger = MemoryLedger()
    svc = _service(tmp_path, fake, _cc_conns(tmp_path), ledger=ledger)
    out = svc.sync("acme")
    blob = "\n".join([json.dumps(out), svc.snapshot("acme").decode(), json.dumps(ledger.events),
                      json.dumps(svc.status("acme")), caplog.text, repr(svc)])
    for secret in SECRETS:
        assert secret not in blob


def test_no_raw_pii_is_stored(tmp_path):
    fake = FakeShopify(orders=[_stacked(1), order(2)])
    ledger = MemoryLedger()
    svc = _service(tmp_path, fake, ledger=ledger)
    svc.sync("acme")
    body = json.loads(svc.snapshot("acme"))
    allowed = {"order_id", "customer_id", "placed_at", "status", "line_items", "discounts", "affiliate",
               "source_platform", "recovery_attempted"}
    for o in body["orders"]:
        assert set(o) == allowed and o["customer_id"] == CUSTOMER_NOT_COLLECTED and o["affiliate"] is None
        for li in o["line_items"]:
            assert set(li) == {"sku", "unit_price_usd", "quantity"}
    text = json.dumps(body) + json.dumps(ledger.events)
    assert "@" not in text
    # a store answer that carries a customer field is refused whole; nothing of it is kept
    fake2 = FakeShopify(orders=[order(3, extra={"email": "jane@example.com"})])
    d = tmp_path / "pii"
    d.mkdir()
    ledger2 = MemoryLedger()
    svc2 = _service(d, fake2, ledger=ledger2)
    assert _err(svc2.sync, "acme") == "STORE_RESPONSE_INVALID"
    assert _err(svc2.snapshot, "acme") == "NOT_SYNCED"
    assert "jane" not in json.dumps(ledger2.events)


def test_one_sync_at_a_time(tmp_path):
    fake = FakeShopify(orders=[order(1)])
    svc = _service(tmp_path, fake)
    assert svc._sync_lock.acquire(blocking=False)
    try:
        assert _err(svc.sync, "acme") == "STORE_SYNC_IN_PROGRESS" and fake.requests == []
    finally:
        svc._sync_lock.release()


def test_snapshot_size_cap(tmp_path, monkeypatch):
    import store_connector.service as service_mod
    monkeypatch.setattr(service_mod, "MAX_SNAPSHOT_BYTES", 100)
    fake = FakeShopify(orders=[order(1)])
    svc = _service(tmp_path, fake)
    assert _err(svc.sync, "acme") == "STORE_SNAPSHOT_TOO_LARGE"

