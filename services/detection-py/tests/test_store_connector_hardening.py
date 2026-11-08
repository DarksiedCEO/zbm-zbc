"""RR store connector hardening before the first real store (Pussycat Alley test store), Nov 8 2026.

Each fix is tied to what Shopify documents (shopify.dev, read Nov 8 2026 through Shopify's docs search and Admin
schema tools; sources in docs/runbooks/rr-pca-first-connection.md):

- HTTP 429 carries ``Retry-After`` (seconds, e.g. ``2.0``): honoured, clamped to 1-10 s.
- A refused query (THROTTLED, MAX_COST_EXCEEDED) has no ``actualQueryCost``: it no longer eats the cost budget.
- ``Decimal`` is "arbitrary precision ... serialized as a string" (examples "29.99", "29.999"): an amount with more
  than 6 decimals no longer refuses the whole read; a cent-exact amount with trailing zeros is accepted; an amount
  that is not a whole number of cents excludes only its order.
- Large stores: a read that runs out of time after at least one page keeps the pages read, labelled
  ``truncated_by: "deadline"``, instead of losing the whole sync.
- ``now`` in any timezone gives the same UTC ``created_at`` filter; a naive ``now`` is refused.
- A failed sync says what Shopify answered as closed tokens (stage, HTTP status, GraphQL error codes), never text.

Fake GraphQL only: no network, no wall clock. Synthetic data only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

import rr_report
from _store_fakes import (NOW, SECRETS, TOKEN, Clock, FakeShopify, MemoryLedger, code_app, conn, money, order,
                          write_secret)
from store_connector import shopify_models as m
from store_connector.reasons import NO_OUTBOUND, StoreError
from store_connector.shopify_client import DIAG_GQL_CODES, MAX_THROTTLE_WAIT_S, _gql_codes, _retry_after
from store_connector.translate import _usd, translate
from test_store_connector import _err, _service, _session


def _read(s, **kw):
    s.preflight()
    args = {"now": NOW, "lookback_days": 30, "max_orders": 1000, "max_cost_points": 100_000, **kw}
    return s.read_orders(**args)


# ---- Retry-After ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,wait", [("2.0", 2.0), ("3", 3.0), ("0", 1.0), ("-5", 1.0), ("999", 10.0),
                                        ("nan", 1.0), ("inf", 10.0), ("abc", 1.0), ("", 1.0), (None, 1.0)])
def test_retry_after_is_parsed_and_clamped(value, wait):
    assert _retry_after(value) == wait


def test_http_429_waits_as_long_as_retry_after_says():
    clock = Clock()
    fake = FakeShopify(orders=[order(1)], script=[None, httpx.Response(429, headers={"retry-after": "4.0"},
                                                                       content=b"")])
    r = _read(_session(fake, clock))
    assert clock.sleeps == [4.0] and len(r.orders) == 1


def test_http_429_with_a_hostile_retry_after_is_bounded():
    clock = Clock()
    fake = FakeShopify(orders=[order(1)],
                       script=[None] + [httpx.Response(429, headers={"retry-after": "86400"}, content=b"")] * 20)
    s = _session(fake, clock, deadline=600.0)
    s.preflight()
    assert _err(s.read_orders, now=NOW, lookback_days=30, max_orders=10, max_cost_points=10_000) == \
        "STORE_THROTTLED"
    assert clock.sleeps == [MAX_THROTTLE_WAIT_S] * 8


def test_token_exchange_429_honours_retry_after():
    from _store_fakes import CC_ID, CC_SECRET
    from store_connector.config import Secret
    clock = Clock()
    fake = FakeShopify()
    orig, n = fake.handle, [0]

    def handle(req):
        if req.url.path == "/admin/oauth/access_token" and n[0] == 0:
            n[0] += 1
            return httpx.Response(429, headers={"retry-after": "2.5"}, content=b"")
        return orig(req)
    fake.handle = handle
    s = _session(fake, clock)
    s.exchange_client_credentials(Secret(CC_ID), Secret(CC_SECRET))
    assert clock.sleeps == [2.5]


# ---- cost accounting ----------------------------------------------------------------------------------------------

def _no_actual(code: str, requested=500, available=0):
    def resp(req):
        return FakeShopify().json({"errors": [{"message": "m", "extensions": {"code": code}}],
                                   "extensions": {"cost": {"requestedQueryCost": requested, "actualQueryCost": None,
                                                           "throttleStatus": {"maximumAvailable": 1000,
                                                                              "currentlyAvailable": available,
                                                                              "restoreRate": 100}}}})
    return resp


def test_refused_queries_do_not_eat_the_cost_budget():
    fake = FakeShopify(orders=[order(i) for i in range(3)],
                       script=[None, _no_actual("THROTTLED"), _no_actual("THROTTLED"),
                               _no_actual("MAX_COST_EXCEEDED", requested=2003)])
    r = _read(_session(fake), max_cost_points=200)
    # preflight 2 + one orders page 50 (actual); the 3 refused queries cost nothing
    assert r.stats.cost_points == 52.0 and r.truncated_by is None and len(r.orders) == 3


def test_a_good_answer_without_actual_cost_counts_the_estimate():
    def no_actual_ok(req):
        return FakeShopify().json({"data": {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": "c:1"},
                                                       "nodes": [order(1)]}},
                                   "extensions": {"cost": {"requestedQueryCost": 77, "actualQueryCost": None,
                                                           "throttleStatus": {"maximumAvailable": 1000,
                                                                              "currentlyAvailable": 900,
                                                                              "restoreRate": 100}}}})
    r = _read(_session(FakeShopify(script=[None, no_actual_ok])))
    assert r.stats.cost_points == 2.0 + 77.0          # conservative: the estimate counts


# ---- money: arbitrary-precision Decimal ----------------------------------------------------------------------------

def _nodes(raw: list) -> list:
    return m.OrdersData.model_validate_json(json.dumps(
        {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": raw}})).orders.nodes


def test_trailing_zero_amounts_are_exact_cents():
    lines = [{"sku": "S", "quantity": 1, "originalUnitPriceSet": money("29.9900")}]
    orders, counts = translate(_nodes([order(1, lines=lines)]))
    assert counts["orders_analysed"] == 1
    li = orders[0].line_items[0]
    assert str(li.unit_price_usd) == "29.99" and type(li.unit_price_usd).__name__ == "Decimal"


@pytest.mark.parametrize("amount", ["29.999", "10.1234567", "0.000000000000000001"])
def test_sub_cent_amount_excludes_only_its_order(amount):
    bad = order(1, lines=[{"sku": "S", "quantity": 1, "originalUnitPriceSet": money(amount)}])
    orders, counts = translate(_nodes([bad, order(2)]))
    assert counts["orders_analysed"] == 1 and orders[0].order_id == "shopify-order-5002"
    assert counts["orders_excluded"] == {"money_format_or_currency": 1}


def test_seven_decimal_amount_no_longer_refuses_the_whole_sync(tmp_path):
    """Regression: a Decimal with 7+ decimals used to fail the model pattern, so ONE odd order lost the whole read."""
    bad = order(1, lines=[{"sku": "S", "quantity": 1, "originalUnitPriceSet": money("10.1234567")}])
    fake = FakeShopify(orders=[bad, order(2), order(3)])
    out = _service(tmp_path, fake).sync("acme")
    assert out["orders_read"] == 3 and out["orders_analysed"] == 2
    assert out["orders_excluded"] == {"money_format_or_currency": 1}


@pytest.mark.parametrize("amount", ["1." + "0" * 19, "1e3", "NaN", "Infinity", " 1.00", "1,00", "0x10", "1" * 16,
                                    "--1"])
def test_malformed_amounts_still_refuse_the_read(amount):
    """Abuse: the shape stays bounded and strict (no exponent, no NaN, no 20-digit fraction, no padding)."""
    bad = order(1, lines=[{"sku": "S", "quantity": 1, "originalUnitPriceSet": money(amount)}])
    fake = FakeShopify(orders=[bad])
    s = _session(fake)
    s.preflight()
    assert _err(s.read_orders, now=NOW, lookback_days=30, max_orders=10, max_cost_points=10_000) == \
        "STORE_RESPONSE_INVALID"


def test_a_json_number_for_money_is_refused():
    """Money must arrive as Shopify's string Decimal; a JSON float (binary, lossy) is never accepted."""
    bad = order(1)
    bad["totalRefundedSet"] = {"shopMoney": {"amount": 0.1, "currencyCode": "USD"}}
    s = _session(FakeShopify(orders=[bad]))
    s.preflight()
    assert _err(s.read_orders, now=NOW, lookback_days=30, max_orders=10, max_cost_points=10_000) == \
        "STORE_RESPONSE_INVALID"


def test_negative_amount_excludes_its_order():
    bad = order(1, codes=[code_app("X", amount="5.00")], lines=[{"sku": "S", "quantity": 1,
                                                               "originalUnitPriceSet": money("-1.00")}])
    _, counts = translate(_nodes([bad]))
    assert counts["orders_analysed"] == 0


# ---- large stores: the deadline keeps what was read ----------------------------------------------------------------

class _SlowShop(FakeShopify):
    """Every orders page takes ``page_s`` seconds of the injected clock."""

    def __init__(self, clock: Clock, page_s: float, **kw):
        super().__init__(**kw)
        self.clock, self.page_s = clock, page_s

    def handle(self, request):
        if json.loads(request.content or b"{}").get("query") == m.ORDERS_QUERY:
            self.clock.t += self.page_s
        return super().handle(request)


def test_a_large_store_is_truncated_by_the_deadline_not_lost(tmp_path):
    clock = Clock()
    fake = _SlowShop(clock, 9.0, orders=[order(i) for i in range(1000)])
    ledger = MemoryLedger()
    svc = _service(tmp_path, fake, clock=clock, deadline=120.0, ledger=ledger)
    out = svc.sync("acme")
    assert out["truncated_by"] == "deadline"
    # 120 s deadline, 30 s page reserve: a page starts while at least 30 s is left (t = 0, 9, ..., 90), so 11 pages
    # end by t = 99; with 21 s left no 12th page is asked for
    assert out["pages"] == 11 and out["orders_read"] == 110
    cov = json.loads(svc.snapshot("acme"))["coverage"]
    assert cov["window"]["truncated_by"] == "deadline"
    assert cov["window"]["newest_order_read"] > cov["window"]["oldest_order_read"]
    assert [e["event_type"] for e in ledger.events] == ["rr_store_read_requested", "rr_store_read_completed"]
    assert ledger.events[1]["payload"]["truncated_by"] == "deadline"


def test_no_page_read_before_the_deadline_is_still_a_failure():
    clock = Clock()
    fake = _SlowShop(clock, 61.0, orders=[order(i) for i in range(30)])
    s = _session(fake, clock, deadline=60.0)
    s.preflight()
    # the only page "arrives" after the deadline: nothing complete was read, so nothing is kept
    assert _err(s.read_orders, now=NOW, lookback_days=30, max_orders=100, max_cost_points=10_000) == \
        "STORE_DEADLINE_EXCEEDED"


def test_a_later_page_that_runs_out_of_time_keeps_the_earlier_pages():
    clock = Clock()

    def stall(req):
        clock.t += 100.0                                  # this page never finishes within the deadline
        raise httpx.ReadTimeout("slow")
    fake = FakeShopify(orders=[order(i) for i in range(30)], script=[None, None, stall])
    s = _session(fake, clock, deadline=60.0)
    r = _read(s)
    assert r.truncated_by == "deadline" and len(r.orders) == 10


def test_a_pacing_wait_into_the_reserve_truncates_without_sleeping():
    clock = Clock()

    def low(req):
        return FakeShopify().json({"data": {"orders": {"pageInfo": {"hasNextPage": True, "endCursor": "c:1"},
                                                       "nodes": [order(1)]}},
                                   "extensions": {"cost": {"requestedQueryCost": 900, "actualQueryCost": 800,
                                                           "throttleStatus": {"maximumAvailable": 1000,
                                                                              "currentlyAvailable": 0,
                                                                              "restoreRate": 100}}}})
    fake = FakeShopify(orders=[order(1), order(2)], script=[None, low])
    s = _session(fake, clock, deadline=11.0)            # reserve 2.75 s; a 9 s pause would leave 2 s
    r = _read(s)
    assert r.truncated_by == "deadline" and clock.sleeps == [] and len(r.orders) == 1


def test_the_kill_switch_is_never_turned_into_a_truncation(tmp_path):
    clock = Clock()
    flag = {"on": False}

    class _KillAfterFirstPage(_SlowShop):
        def handle(self, request):
            resp = super().handle(request)
            if json.loads(request.content or b"{}").get("query") == m.ORDERS_QUERY:
                flag["on"] = True
            return resp
    fake = _KillAfterFirstPage(clock, 1.0, orders=[order(i) for i in range(30)])
    s = _session(fake, clock, deadline=60.0, kill_check=lambda: flag["on"])
    s.preflight()
    assert _err(s.read_orders, now=NOW, lookback_days=30, max_orders=100, max_cost_points=10_000) == \
        "STORE_READS_DISABLED"


# ---- timezone ----------------------------------------------------------------------------------------------------

def test_the_created_at_filter_is_utc_whatever_timezone_now_is_in():
    la = timezone(timedelta(hours=-8))
    fake_utc, fake_la = FakeShopify(orders=[order(1)]), FakeShopify(orders=[order(1)])
    _read(_session(fake_utc), now=NOW)
    _read(_session(fake_la), now=NOW.astimezone(la))
    q = [b["variables"]["q"] for f in (fake_utc, fake_la) for b in f.graphql_bodies if b["query"] == m.ORDERS_QUERY]
    assert q == ["created_at:>='2026-09-24T12:00:00Z'"] * 2


def test_a_naive_now_is_refused():
    s = _session(FakeShopify(orders=[order(1)]))
    s.preflight()
    with pytest.raises(ValueError):
        s.read_orders(now=datetime(2026, 10, 24, 12, 0, 0), lookback_days=30, max_orders=10, max_cost_points=10_000)


def test_a_naive_service_clock_is_a_clear_error_before_any_outbound_call(tmp_path):
    """PR #45 review L-e: a programming error gets its own code (not STORE_RESPONSE_INVALID), and nothing is sent."""
    fake = FakeShopify(orders=[order(1)])
    ledger = MemoryLedger()
    svc = _service(tmp_path, fake, ledger=ledger, now=datetime(2026, 10, 24, 12, 0, 0))
    assert _err(svc.sync, "acme") == "STORE_CLOCK_INVALID"
    assert fake.requests == [] and ledger.events == []
    assert "STORE_CLOCK_INVALID" in NO_OUTBOUND and StoreError("STORE_CLOCK_INVALID").http_status == 500


# ---- upstream diagnostics ------------------------------------------------------------------------------------------

def test_a_failed_sync_reports_closed_upstream_tokens(tmp_path):
    hostile = [{"message": f"leak {TOKEN} jane@example.com", "extensions": {"code": "ACCESS_DENIED"}},
               {"message": "x", "extensions": {"code": "lower case or spaces"}},
               {"message": "x", "extensions": {"code": "9STARTS_WITH_A_DIGIT"}}]
    fake = FakeShopify(script=[FakeShopify().json({"errors": hostile})])
    ledger = MemoryLedger()
    svc = _service(tmp_path, fake, ledger=ledger)
    with pytest.raises(Exception) as e:
        svc.sync("acme")
    assert e.value.code == "STORE_AUTH_REFUSED"
    # PR #45 review L-b: only allow-listed codes pass; any other upstream-chosen code is "OTHER"
    assert e.value.upstream == {"stage": "preflight", "http_status": 200,
                                "graphql_codes": ["ACCESS_DENIED", "OTHER"]}
    failed = ledger.events[-1]
    assert failed["event_type"] == "rr_store_read_failed"
    assert failed["payload"]["upstream"] == e.value.upstream
    assert "stage=preflight http=200 gql=ACCESS_DENIED,OTHER" in failed["summary"]
    dumped = json.dumps(ledger.events) + json.dumps(e.value.upstream)
    assert not any(sec in dumped for sec in SECRETS) and "jane@" not in dumped and "leak" not in dumped
    assert "9STARTS" not in dumped and "lower case" not in dumped


@pytest.mark.parametrize("code", ["SECRET_SHPAT_ABC", "EMAIL_JANE_EXAMPLE_COM", "BAD_REQUEST", "", None])
def test_unlisted_or_missing_graphql_codes_become_other(tmp_path, code):
    err = {"message": "m", "extensions": {"code": code}} if code is not None else {"message": "m"}
    svc = _service(tmp_path, FakeShopify(script=[FakeShopify().json({"errors": [err]})]))
    with pytest.raises(StoreError) as e:
        svc.sync("acme")
    assert e.value.code == "STORE_RESPONSE_INVALID" and e.value.upstream["graphql_codes"] == ["OTHER"]


@pytest.mark.parametrize("code", sorted(DIAG_GQL_CODES))
def test_listed_graphql_codes_are_reported_as_is(code):
    env = m.Envelope.model_validate_json(json.dumps({"errors": [{"message": "m", "extensions": {"code": code}},
                                                                {"message": "m", "extensions": {"code": code}}]}))
    assert _gql_codes(env) == [code]


def test_the_allow_list_is_fixed():
    assert DIAG_GQL_CODES == {"ACCESS_DENIED", "SHOP_INACTIVE", "INTERNAL_SERVER_ERROR", "THROTTLED",
                              "MAX_COST_EXCEEDED"}


def test_store_error_declares_upstream():
    assert StoreError.upstream is None and StoreError("NOT_CONNECTED").upstream is None


def test_a_transport_failure_reports_no_http_status(tmp_path):
    def boom(req):
        raise httpx.ConnectError("refused")
    ledger = MemoryLedger()
    svc = _service(tmp_path, FakeShopify(script=[boom] * 5), ledger=ledger)
    with pytest.raises(Exception) as e:
        svc.sync("acme")
    assert e.value.code == "STORE_UNAVAILABLE"
    assert e.value.upstream == {"stage": "preflight", "http_status": None, "graphql_codes": []}
    assert "http=- gql=-" in ledger.events[-1]["summary"]


def test_a_refusal_before_any_request_has_no_upstream(tmp_path):
    svc = _service(tmp_path, FakeShopify(), disabled=True)
    with pytest.raises(Exception) as e:
        svc.sync("acme")
    assert e.value.code == "STORE_READS_DISABLED" and getattr(e.value, "upstream", None) is None


def test_the_ledger_summary_stays_within_its_contract(tmp_path):
    codes = [{"message": "m", "extensions": {"code": c}} for c in sorted(DIAG_GQL_CODES - {"THROTTLED"})]
    codes += [{"message": "m", "extensions": {"code": f"CODE_{c}_" + "Z" * 57}} for c in "ABCDEFG"]   # 64 chars
    ledger = MemoryLedger()
    svc = _service(tmp_path, FakeShopify(script=[FakeShopify().json({"errors": codes})]), ledger=ledger)
    with pytest.raises(Exception):
        svc.sync("acme")
    s = ledger.events[-1]["summary"]
    assert len(s) <= 280 and all(0x20 <= ord(ch) <= 0x7e for ch in s)
    got = ledger.events[-1]["payload"]["upstream"]["graphql_codes"]
    assert set(got) <= DIAG_GQL_CODES | {"OTHER"} and "OTHER" in got and len(got) <= 6


# ---- PR #45 review L-c: no negative zero ----------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["-0.00", "-0", "0.000", "-0.0000000"])
def test_zero_is_never_negative_zero(raw):
    d = _usd(m.MoneyV2(amount=raw, currencyCode="USD"))
    assert d == 0 and str(d) == "0.00" and not d.is_signed()


# ---- PR #45 review L-a: the truncation boundary is exclusive --------------------------------------------------------

def test_orders_at_the_truncation_timestamp_are_left_out(tmp_path):
    """A truncated read's oldest timestamp may continue on the page never read: orders AT it are excluded, so every
    analysed order lies in (oldest_order_read, taken_at], a range read in full."""
    tie = NOW - timedelta(hours=50)
    nodes = [order(i) for i in range(1, 9)] + [order(100 + i, created=tie) for i in range(4)]
    tok = write_secret(tmp_path, "t", TOKEN)
    svc = _service(tmp_path, FakeShopify(orders=nodes), [conn("acme", "acme-store", tok, max_orders=10)])
    out = svc.sync("acme")
    assert out["truncated_by"] == "max_orders" and out["orders_read"] == 10
    assert out["orders_excluded"] == {"at_truncation_boundary": 2} and out["orders_analysed"] == 8
    snap = json.loads(svc.snapshot("acme"))
    w = snap["coverage"]["window"]
    assert datetime.fromisoformat(w["oldest_order_read"]) == tie and w["oldest_order_read_exclusive"] is True
    assert all(datetime.fromisoformat(o["placed_at"].replace("Z", "+00:00")) > tie for o in snap["orders"])


def test_an_untruncated_read_keeps_its_oldest_orders(tmp_path):
    tie = NOW - timedelta(hours=50)
    nodes = [order(i) for i in range(1, 4)] + [order(100 + i, created=tie) for i in range(2)]
    svc = _service(tmp_path, FakeShopify(orders=nodes))
    out = svc.sync("acme")
    assert out["truncated_by"] is None and out["orders_analysed"] == 5 and out["orders_excluded"] == {}
    assert json.loads(svc.snapshot("acme"))["coverage"]["window"]["oldest_order_read_exclusive"] is False


# ---- PR #45 review H: a report from a deadline-truncated read -------------------------------------------------------

def _scan_from(svc, client_id: str, amount: str = "100.00") -> dict:
    snap = json.loads(svc.snapshot(client_id))
    f = {"client_id": client_id, "entity_id": snap["orders"][0]["order_id"], "finding_id": "f1",
         "agent_id": "discount-misuse-v1", "cause_description": "2 discount codes stacked on one order (A, B)",
         "methodology": "m", "evidence_class": "OBSERVED",
         "recoverable_value": {"amount_usd": amount, "classification": "observed", "confidence": "high"}}
    return {"scan_id": "s", "client_id": client_id, "as_of": NOW.isoformat(),
            "data_source": f"shopify:{snap['snapshot_id']}", "non_live_data_source": False,
            "store_coverage": snap["coverage"], "store_snapshot_id": snap["snapshot_id"], "findings": [f],
            "ledger_verify": {"valid": True}}


def test_a_report_from_a_deadline_truncated_read_shows_measured_figures_only(tmp_path):
    clock = Clock()
    fake = _SlowShop(clock, 9.0, orders=[order(i) for i in range(1, 1001)])     # one order an hour, newest first
    svc = _service(tmp_path, fake, clock=clock, deadline=120.0)
    out = svc.sync("acme")
    assert out["truncated_by"] == "deadline" and out["orders_read"] == 110
    rep = rr_report.build_report(_scan_from(svc, "acme"))
    # the oldest order read is 110 h old: exactly 4.5833... days, never rounded to 5 (or to 1)
    assert Decimal(rep["window_days_exact"]) == Decimal(110) / Decimal(24)
    assert rep["window_days"] == 4.58 and rep["window_basis"] == "oldest_order_read"
    assert rep["projections"] == []
    assert "under the 14-day minimum" in rep["projection_withheld"]
    md = rr_report.render_markdown(rep)
    assert "No yearly projection" in md and "a year**" not in md
    assert "$100.00" in md and "deadline limit" in md
    assert "at truncation boundary" in md                                       # the boundary order is listed


def _truncated_scan(oldest: str, lookback: int = 60, truncated_by: str | None = "deadline") -> dict:
    window = {"requested_start": "2026-08-25T12:00:00+00:00", "lookback_days": lookback, "truncated_by": truncated_by,
              "oldest_order_read": oldest, "newest_order_read": "2026-10-24T11:00:00+00:00"}
    cov = {"shop": "acme-store", "api_version": "2026-10", "taken_at": "2026-10-24T12:00:00+00:00", "window": window,
           "counts": {}, "customer_data": "none", "agents": {"discount-misuse-v1": {"covered": True, "why": "x"}}}
    f = {"client_id": "acme", "entity_id": "shopify-order-1", "finding_id": "f1", "agent_id": "discount-misuse-v1",
         "cause_description": "c", "methodology": "m", "evidence_class": "OBSERVED",
         "recoverable_value": {"amount_usd": "100.00", "classification": "observed", "confidence": "high"}}
    return {"scan_id": "s", "client_id": "acme", "as_of": "2026-10-24T12:00:00Z", "data_source": "shopify:snap-x",
            "non_live_data_source": False, "store_coverage": cov, "store_snapshot_id": "snap-x", "findings": [f],
            "ledger_verify": {"valid": True}}


def test_projection_uses_exact_fractional_days():
    rep = rr_report.build_report(_truncated_scan("2026-10-04T00:00:00+00:00"))     # 20.5 days of 60 (34%)
    (p,) = rep["projections"]
    assert Decimal(rep["window_days_exact"]) == Decimal("20.5")
    assert p["amount_usd"] == "1780.49"                     # 100 x 365 / 20.5; whole days gave 1825.00 or 1738.10
    assert "20.50 days" in p["basis"] and "x 365/20.50" in p["basis"]


@pytest.mark.parametrize("oldest", [
    "2026-10-10T21:36:00+00:00",     # 13.6 days: rounding to whole days made this 14 and projected it
    "2026-10-23T01:00:00+00:00",     # 35 h
    "2026-10-24T06:00:00+00:00",     # 6 h
])
def test_short_truncated_windows_get_no_projection(oldest):
    rep = rr_report.build_report(_truncated_scan(oldest))
    assert rep["projections"] == [] and "under the 14-day minimum" in rep["projection_withheld"]
    assert "No yearly projection" in rr_report.render_markdown(rep)


def test_a_truncated_window_under_a_quarter_of_the_lookback_gets_no_projection():
    rep = rr_report.build_report(_truncated_scan("2026-10-09T00:00:00+00:00", lookback=90))   # 15.5 of 90 days
    assert rep["projections"] == []
    assert "15.50 of the 90 days asked (under 25%)" in rep["projection_withheld"]
    rep = rr_report.build_report(_truncated_scan("2026-10-09T00:00:00+00:00", lookback=60))   # 15.5 of 60: 26%
    assert len(rep["projections"]) == 1 and rep["projection_withheld"] is None


def test_a_full_window_still_projects_and_never_shows_the_note():
    rep = rr_report.build_report(_truncated_scan("2026-10-04T00:00:00+00:00", truncated_by=None))
    assert rep["window_days"] == 60 and rep["projections"][0]["amount_usd"] == "608.33"
    assert rep["projection_withheld"] is None and "No yearly projection" not in rr_report.render_markdown(rep)
