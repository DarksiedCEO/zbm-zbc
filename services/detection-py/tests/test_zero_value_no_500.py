"""
N6 (AEGIS round 2, CONFIRMED): valid input must never make an agent raise.

Before this fix, two stacked 0% codes on a $50 order (or two 10% codes on a
$0.01 item) made discount-misuse compute a discount of 0.00; LabeledValue
requires amount > 0, so pydantic raised inside the agent and the whole
request died with 500. The same class hit abandoned-cart-coverage and
affiliate-coupon-extension for a valid order with no line items (subtotal
0.00).

Rule (docs/adr/0001, "Zero-value findings"): when the dollar value a finding
would claim computes to 0.00 after cent rounding, the agent emits NO finding
— there is nothing to recover, and a 0.00 figure cannot be labeled.

Defense in depth (same ADR): if an agent ever does raise a validation error
for an item, the API answers 422 naming that item, never 500, and never a
silently partial finding list.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api
from agents import (
    abandoned_cart_coverage,
    affiliate_coupon_extension,
    contract_pricing_term_drift,
    cross_channel_attribution,
    discount_misuse,
    platform_integration,
    renewal_never_triggered,
    server_side_attribution,
)
from conftest import TEST_SERVICE_TOKEN
from safety import hallucination_check
from zbm_schema import MAX_MONEY, LabeledValue, Order, Subscription
from zbm_schema.tier2 import (
    ChannelTouchpoint,
    ContractTerm,
    PlatformConnectionStatus,
    ServerSideAttributionEvent,
)
from _fx import AS_OF, AS_OF_WIRE, TENANT, make_finding  # noqa: F401

client = TestClient(
    api.app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}, raise_server_exceptions=False
)


def _order(price="50.00", discounts=(), status="completed", line_items=None, affiliate=None, **kw):
    o = {
        "order_id": "o1", "customer_id": "c", "placed_at": "2026-06-01T00:00:00Z", "status": status,
        "source_platform": "shopify",
        "line_items": [{"sku": "s", "unit_price_usd": price, "quantity": 1}] if line_items is None else line_items,
        "discounts": list(discounts),
    }
    if affiliate is not None:
        o["affiliate"] = affiliate
    o.update(kw)
    return o


_FAR_OUTSIDE = {"affiliate_id": "a", "click_timestamp": "2026-06-01T00:00:00Z",
                "order_timestamp": "2026-06-20T00:00:00Z", "attribution_window_hours": 24}
_SLIGHTLY_OUTSIDE = {"affiliate_id": "a", "click_timestamp": "2026-06-01T00:00:00Z",
                     "order_timestamp": "2026-06-02T12:00:00Z", "attribution_window_hours": 24}


# --- the AEGIS reproduction (review2/det500b.py) and its siblings ----------

@pytest.mark.parametrize("price,discounts", [
    ("0.01", [{"code": "A", "percent_off": 10.0}, {"code": "B", "percent_off": 10.0}]),
    ("50.00", [{"code": "A", "percent_off": 0.0}, {"code": "B", "percent_off": 0.0}]),
    ("0.04", [{"code": "A", "percent_off": 1.0}, {"code": "B", "percent_off": 12.4}]),
])
def test_stacked_codes_that_give_away_nothing_are_200_with_no_finding(price, discounts):
    r = client.post("/agents/discount-misuse/detect", json={"client_id": TENANT, "orders": [_order(price, discounts)]})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert r.json()["findings"] == []


def test_zero_amount_off_codes_are_rejected_as_input_not_500():
    # amount_off_usd is positive-only money: "0.00" is invalid input (422).
    ds = [{"code": "A", "amount_off_usd": "0.00"}, {"code": "B", "amount_off_usd": "0.00"}]
    r = client.post("/agents/discount-misuse/detect", json={"client_id": TENANT, "orders": [_order("50.00", ds)]})
    assert r.status_code == 422, (r.status_code, r.text[:300])


def test_zero_value_order_does_not_fail_the_rest_of_the_batch():
    ds0 = [{"code": "A", "percent_off": 0.0}, {"code": "B", "percent_off": 0.0}]
    ds1 = [{"code": "A", "percent_off": 10.0}, {"code": "B", "percent_off": 10.0}]
    orders = [_order("50.00", ds0, order_id="zero"), _order("100.00", ds1, order_id="real")]
    r = client.post("/agents/discount-misuse/detect", json={"client_id": TENANT, "orders": orders})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    fs = r.json()["findings"]
    assert [f["entity_id"] for f in fs] == ["real"]
    # 19.00 given (10.00 + 10% of 90.00); E-4: minus the best single code's 10.00 -> 9.00.
    assert fs[0]["recoverable_value"]["amount_usd"] == "9.00"


@pytest.mark.parametrize("path,order", [
    ("/agents/abandoned-cart-coverage/detect", _order(status="abandoned_cart", line_items=[])),
    ("/agents/affiliate-coupon-extension/detect", _order(line_items=[], affiliate=_FAR_OUTSIDE)),
    ("/agents/affiliate-coupon-extension/detect", _order(line_items=[], affiliate=_SLIGHTLY_OUTSIDE)),
    ("/agents/discount-misuse/detect", _order(line_items=[], discounts=[
        {"code": "A", "percent_off": 10.0}, {"code": "B", "amount_off_usd": "5.00"}])),
])
def test_order_with_no_line_items_is_200_with_no_finding(path, order):
    r = client.post(path, json={"client_id": TENANT, "orders": [order]})
    assert r.status_code == 200, (path, r.status_code, r.text[:300])
    assert r.json()["findings"] == []


def test_contract_drift_below_a_cent_is_not_a_finding():
    # contracted == billed: drift 0.00 -> no finding (already the rule; pinned).
    term = {"term_id": "t", "client_id": TENANT, "term_type": "minimum_spend", "contracted_value_usd": "0.01",
            "actual_billed_value_usd": "0.01", "period_label": "2026-06"}
    r = client.post("/agents/contract-pricing-term-drift/detect", json={"client_id": TENANT, "terms": [term]})
    assert r.status_code == 200 and r.json()["findings"] == []


# --- API: an agent error for one item is a 422 naming it, never a 500 ------

_real_discount_detect = discount_misuse.detect
_real_cross_channel_detect = cross_channel_attribution.detect


def _boom_on(bad_entity):
    def detect(items, **tenant):
        out = []
        for it in items:
            key = getattr(it, "order_id", None) or getattr(it, "subscription_id", None)
            if key == bad_entity:
                LabeledValue(amount_usd="0.00", classification="observed", confidence="high")  # raises
            out.extend(_real_discount_detect([it], **tenant))
        return out
    return detect


def test_agent_validation_error_is_422_naming_the_item_not_500(monkeypatch):
    monkeypatch.setattr(api.discount_misuse, "detect", _boom_on("bad"))
    ds = [{"code": "A", "percent_off": 10.0}, {"code": "B", "percent_off": 10.0}]
    orders = [_order("100.00", ds, order_id="ok1"), _order("100.00", ds, order_id="bad"),
              _order("100.00", ds, order_id="ok2")]
    r = client.post("/agents/discount-misuse/detect", json={"client_id": TENANT, "orders": orders})
    assert r.status_code == 422, (r.status_code, r.text[:300])
    detail = r.json()["detail"]
    assert len(detail) == 1
    d = detail[0]
    assert set(d) <= {"type", "loc", "msg"}
    assert d["type"] == "agent_value_error"
    assert d["loc"] == ["body", "orders", 1]
    assert "order_id=bad" in d["msg"]
    # No partial finding list is returned for a failed batch.
    assert "findings" not in r.json()


def test_agent_non_validation_value_error_is_also_422(monkeypatch):
    from zbm_schema import MoneyRangeError

    def detect(items, **tenant):
        raise MoneyRangeError("money must be less than 10^15 dollars")

    monkeypatch.setattr(api.renewal_never_triggered, "detect", detect)
    sub = {"subscription_id": "s1", "customer_id": "c", "plan_price_usd": "10.00", "renewal_interval_days": 30,
           "next_renewal_due_at": "2026-06-01T00:00:00Z", "status": "lapsed_no_renewal_attempt"}
    r = client.post("/agents/renewal-never-triggered/detect", json={"client_id": TENANT, "as_of": AS_OF_WIRE, "subscriptions": [sub]})
    assert r.status_code == 422, (r.status_code, r.text[:300])
    assert r.json()["detail"][0]["loc"] == ["body", "subscriptions", 0]


def test_cross_channel_items_are_isolated_per_order_not_per_touchpoint(monkeypatch):
    seen = []

    def detect(tps, **tenant):
        seen.append(sorted({t.order_id for t in tps}))
        return _real_cross_channel_detect(tps, **tenant)

    monkeypatch.setattr(api.cross_channel_attribution, "detect", detect)
    tps = [
        {"order_id": "A", "channel": "meta", "touchpoint_sequence": 1, "is_paid_channel": True,
         "is_credited_conversion_channel": False},
        {"order_id": "B", "channel": "email", "touchpoint_sequence": 1, "is_paid_channel": False,
         "is_credited_conversion_channel": True},
        {"order_id": "A", "channel": "email", "touchpoint_sequence": 2, "is_paid_channel": False,
         "is_credited_conversion_channel": True},
    ]
    r = client.post("/agents/cross-channel-attribution/detect", json={"client_id": TENANT, "touchpoints": tps})
    assert r.status_code == 200
    assert seen == [["A"], ["B"]]  # one call per order, first-appearance order
    assert [f["entity_id"] for f in r.json()["findings"]] == ["A"]  # the grouping still sees both touches


# --- property / fuzz test: every agent over generated valid input ----------

_MONEY_EDGES = ["0.01", "0.02", "0.03", "0.05", "0.09", "0.10", "0.99", "1.00", "49.99", "50.00",
                "100.00", "999.99", "999999999999.99", "999999999999999.98", str(MAX_MONEY)]
_PERCENT_EDGES = [0.0, 0.1, 0.5, 1.0, 10.0, 12.5, 15.0, 33.333, 49.5, 50.0, 99.99, 100.0]


def _money(rng):
    if rng.random() < 0.6:
        return rng.choice(_MONEY_EDGES)
    return f"{rng.randint(0, 10 ** rng.randint(0, 14))}.{rng.randint(0, 99):02d}".replace("0.00", "0.01")


def _gen_order(rng, i):
    n_items = rng.choice([0, 1, 1, 2, 3])
    items = [{"sku": f"s{j}", "unit_price_usd": _money(rng), "quantity": rng.choice([1, 1, 2, 3, 7, 1000])}
             for j in range(n_items)]
    discounts = []
    for j in range(rng.choice([0, 1, 2, 2, 3, 4])):
        if rng.random() < 0.7:
            discounts.append({"code": f"C{j}", "percent_off": rng.choice(_PERCENT_EDGES + [rng.uniform(0, 100)])})
        else:
            discounts.append({"code": f"C{j}", "amount_off_usd": _money(rng)})
    o = {"order_id": f"o{i}", "customer_id": "c", "placed_at": "2026-06-01T00:00:00Z",
         "status": rng.choice(["completed", "abandoned_cart", "cancelled"]), "source_platform": "x",
         "line_items": items, "discounts": discounts, "recovery_attempted": rng.random() < 0.3}
    if rng.random() < 0.5:
        click = datetime(2026, 6, 1, tzinfo=timezone.utc)
        gap = timedelta(hours=rng.choice([-5, 0, 1, 24, 25, 47, 48, 49, 500, 10 ** 6]) + rng.random())
        o["affiliate"] = {"affiliate_id": "a", "click_timestamp": click.isoformat(),
                          "order_timestamp": (click + gap).isoformat(),
                          "attribution_window_hours": rng.choice([1, 24, 72, 10 ** 6])}
        if rng.random() < 0.7:  # E-4: a known commission rate makes the affiliate figure computable
            o["affiliate"]["commission_rate_percent"] = rng.choice(_PERCENT_EDGES + [rng.uniform(0, 100)])
    return o


def _valid(model, raw):
    try:
        return model.model_validate(raw)
    except ValidationError:
        return None  # invalid input is the API's 422, not this test's subject


def _check(findings):
    for f in findings:
        if f.recoverable_value is not None:
            assert Decimal("0.01") <= f.recoverable_value.amount_usd <= MAX_MONEY
        assert hallucination_check.check(f) is None, (f.finding_id, f.cause_description)


def test_every_agent_survives_generated_valid_input():
    rng = random.Random(20260924)
    n_orders = n_findings = 0
    for i in range(3000):
        o = _valid(Order, _gen_order(rng, i))
        if o is None:
            continue
        n_orders += 1
        for agent in (affiliate_coupon_extension, discount_misuse, abandoned_cart_coverage):
            fs = agent.detect([o], client_id=TENANT)
            _check(fs)
            n_findings += len(fs)
    assert n_orders > 2000 and n_findings > 500  # the generator really exercises the agents

    for i in range(1000):
        sub = _valid(Subscription, {
            "subscription_id": f"s{i}", "customer_id": "c", "plan_price_usd": _money(rng),
            "renewal_interval_days": rng.choice([1, 30, 365]), "next_renewal_due_at": "2026-06-01T00:00:00Z",
            "status": rng.choice(["active", "past_due", "cancelled", "lapsed_no_renewal_attempt"])})
        ev = _valid(ServerSideAttributionEvent, {
            "order_id": f"o{i}", "channel": "meta", "order_value_usd": _money(rng),
            "pixel_attributed": rng.random() < 0.5, "server_confirmed": rng.random() < 0.5})
        term = _valid(ContractTerm, {
            "term_id": f"t{i}", "client_id": TENANT, "term_type": rng.choice(["minimum_spend", "escalator", "overage_rate"]),
            "contracted_value_usd": _money(rng), "actual_billed_value_usd": rng.choice(["0.00", _money(rng)]),
            "period_label": "2026-06"})
        st = PlatformConnectionStatus(client_id=TENANT, platform="p", client_reports_using_it=rng.random() < 0.5,
                                      integration_connected=rng.random() < 0.5)
        for agent, item in ((renewal_never_triggered, sub), (server_side_attribution, ev),
                            (contract_pricing_term_drift, term), (platform_integration, st)):
            if item is not None:
                extra = {"as_of": AS_OF} if agent is renewal_never_triggered else {}
                _check(agent.detect([item], client_id=TENANT, **extra))
        tps = [ChannelTouchpoint(order_id=f"x{i}", channel=rng.choice(["meta", "google", "email"]),
                                 touchpoint_sequence=rng.randint(1, 3), is_paid_channel=rng.random() < 0.5,
                                 is_credited_conversion_channel=rng.random() < 0.5)
               for _ in range(rng.randint(0, 4))]
        _check(cross_channel_attribution.detect(tps, client_id=TENANT))


def test_generated_orders_over_http_never_500():
    rng = random.Random(7)
    orders = [_gen_order(rng, i) for i in range(400)]
    valid = [o for o in orders if _valid(Order, o) is not None]
    for path in ("/agents/discount-misuse/detect", "/agents/abandoned-cart-coverage/detect",
                 "/agents/affiliate-coupon-extension/detect"):
        r = client.post(path, json={"client_id": TENANT, "orders": valid})
        assert r.status_code == 200, (path, r.status_code, r.text[:300])
