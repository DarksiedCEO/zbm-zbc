"""
README gap #6 — money is exact Decimal end to end.

Most assertions here compare against `Decimal("...")` or against the exact
JSON string. Both would FAIL under the old float implementation, even where
the old `round(x, 2)` produced a value that printed correctly:
`Decimal("0.30") == 0.3` is False, because Decimal compares against the
float's true binary value (0.299999999999999988897769753748...).
Several cases also produced a WRONG CENT under float (`round(2.675, 2)`
is 2.67, `round(1.005, 2)` is 1.0) — those are marked "float gave".
"""

import json
import re
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agents import (
    abandoned_cart_coverage,
    affiliate_coupon_extension,
    contract_pricing_term_drift,
    discount_misuse,
    renewal_never_triggered,
    server_side_attribution,
)
from api import app
from conftest import TEST_SERVICE_TOKEN
from fixtures_loader import (
    load_contract_terms,
    load_orders,
    load_server_side_events,
    load_subscriptions,
)
from safety.hallucination_check import check
from zbm_schema import (
    CauseCertainty,
    DecisionConfidence,
    DiscountApplication,
    Finding,
    LabeledValue,
    LeakCategory,
    Order,
    OrderLineItem,
    ValueClassification,
    format_money,
    percent_of,
    to_money,
)
from zbm_schema.tier2 import ContractTerm

WIRE = re.compile(r"^(0|[1-9][0-9]*)\.[0-9]{2}$")
client = TestClient(app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})


def _lv(amount) -> LabeledValue:
    return LabeledValue(amount_usd=amount, classification=ValueClassification.OBSERVED, confidence=DecisionConfidence.HIGH)


def _order(items: list[tuple[str, int]], discounts=None, oid="ord_t") -> Order:
    return Order(
        order_id=oid, customer_id="cust_t", placed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        status="completed", source_platform="shopify",
        line_items=[OrderLineItem(sku=f"S{i}", unit_price_usd=p, quantity=q) for i, (p, q) in enumerate(items)],
        discounts=discounts or [],
    )


def _finding(desc: str, amount) -> Finding:
    return Finding(
        finding_id="t-1", agent_id="test", leak_category=LeakCategory.DISCOUNT_MISUSE,
        entity_type="order", entity_id="ord_t", customer_id="cust_t",
        cause_certainty=CauseCertainty.NAMED, cause_description=desc,
        recoverable_value=None if amount is None else _lv(amount),
    )


# --- input conversion -------------------------------------------------------

def test_float_fixture_input_49_99_stays_exactly_49_99():
    v = _lv(49.99)
    assert isinstance(v.amount_usd, Decimal)
    assert v.amount_usd == Decimal("49.99")
    # Decimal(49.99) would be 49.99000000000000198951966012828052043914794921875
    assert v.amount_usd != Decimal(49.99)
    assert v.model_dump(mode="json")["amount_usd"] == "49.99"


def test_int_str_and_decimal_inputs_all_normalize_to_cents():
    assert _lv(5).amount_usd == Decimal("5.00")
    assert str(_lv(5).amount_usd) == "5.00"
    # A non-canonical STRING is rejected (fix wave 1, F15: strings are wire
    # values and must match the shared vectors); the same value as a Decimal
    # is a computed amount and is quantized to cents.
    with pytest.raises(ValidationError):
        _lv("12.3")
    assert str(_lv(Decimal("12.3")).amount_usd) == "12.30"
    assert str(_lv(Decimal("7")).amount_usd) == "7.00"


# Fix wave 1: these used string inputs ("2.675"). Non-canonical strings are
# now rejected (they are wire values — see fixtures/money_vectors.json), so
# the half-up rule is exercised with Decimal inputs (computed amounts) and
# float inputs (the fixture path), which is where rounding legitimately happens.
@pytest.mark.parametrize("raw,expected", [
    (Decimal("2.675"), "2.68"),   # float gave round(2.675, 2) == 2.67
    (2.675, "2.68"),              # same, via the float->str path
    (Decimal("1.005"), "1.01"),   # float gave round(1.005, 2) == 1.0
    (1.005, "1.01"),
    (Decimal("0.125"), "0.13"),   # float round-half-even gave 0.12
    (Decimal("0.135"), "0.14"),
    (Decimal("10.004"), "10.00"),
    (Decimal("10.0049999"), "10.00"),
])
def test_rounding_is_half_up_at_the_005_boundary(raw, expected):
    assert str(_lv(raw).amount_usd) == expected


@pytest.mark.parametrize("bad", [
    float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", "inf", "1e3", " 1.00", "",
    "abc", "1.2.3", True, None, [1], "0.00", 0, "0.004", 0.004, "-1.00", -5,
])
def test_positive_money_rejects_non_finite_malformed_zero_and_negative(bad):
    with pytest.raises(ValidationError):
        _lv(bad)


def test_non_positive_money_field_accepts_zero():
    term = ContractTerm(term_id="t", client_id="c", term_type="minimum_spend",
                        contracted_value_usd="10.00", actual_billed_value_usd=0, period_label="p")
    assert term.actual_billed_value_usd == Decimal("0.00")
    assert term.model_dump(mode="json")["actual_billed_value_usd"] == "0.00"


def test_to_money_never_uses_decimal_of_float():
    assert to_money(0.1) == Decimal("0.10")
    assert to_money(0.1) + to_money(0.2) == Decimal("0.30")  # float: 0.1 + 0.2 == 0.30000000000000004


# --- arithmetic ---------------------------------------------------------------

def test_subtotal_of_ten_cents_plus_twenty_cents_is_exactly_thirty_cents():
    o = _order([(0.10, 1), (0.20, 1)])
    assert o.subtotal_usd == Decimal("0.30")  # float sum was 0.30000000000000004


def test_two_dollars_one_cent_style_value_is_exact():
    # 0.67 * 3 is 2.0100000000000002 in float
    o = _order([("0.67", 3)])
    assert o.subtotal_usd == Decimal("2.01")
    assert format_money(o.subtotal_usd) == "2.01"


def test_many_line_item_subtotal_is_exact():
    # 50 lines: the per-order maximum (zbm_schema/limits.py, LOW-C fix wave 1;
    # this test used 1,583 lines before orders had a line-item cap).
    items = [("0.10", 1)] * 20 + [("19.99", 3)] * 20 + [("0.07", 11)] * 10
    o = _order(items)
    expected = Decimal("0.10") * 20 + Decimal("19.99") * 3 * 20 + Decimal("0.07") * 11 * 10
    assert expected == Decimal("1209.10")
    assert o.subtotal_usd == Decimal("1209.10")
    # Sanity: the naive float sum of the same items is NOT exact.
    float_sum = sum(float(p) * q for p, q in items)
    assert Decimal(float_sum) != Decimal("1209.10")


def test_percent_of_rounds_half_up_exactly():
    assert percent_of(Decimal("2.01"), 50) == Decimal("1.01")  # 1.005 -> 1.01; float gave 1.0
    assert percent_of(Decimal("127.50"), 25) == Decimal("31.88")  # 31.875 -> 31.88
    assert percent_of(Decimal("10.00"), 12.5) == Decimal("1.25")
    assert percent_of(Decimal("0.10"), 33.3) == Decimal("0.03")  # 0.0333 -> 0.03


def test_stacked_percent_discounts_are_exact_and_quantized_per_line():
    # ord_1007 fixture: 150.00, 15% -> 22.50 given (127.50 left), 25% of 127.50 = 31.875 -> 31.88
    finding = [f for f in discount_misuse.detect(load_orders()) if f.entity_id == "ord_1007"][0]
    assert finding.recoverable_value.amount_usd == Decimal("54.38")
    assert "$54.38" in finding.cause_description


def test_amount_off_discount_is_exact_and_capped_at_remaining():
    o = _order([("10.10", 1)], discounts=[
        DiscountApplication(code="A", percent_off=10),     # 1.01 given, 9.09 left
        DiscountApplication(code="B", amount_off_usd=0.07),  # 0.07 given, 9.02 left
        DiscountApplication(code="C", amount_off_usd="50.00"),  # capped at 9.02
    ])
    f = discount_misuse.detect([o])[0]
    assert f.recoverable_value.amount_usd == Decimal("10.10")


def test_contract_drift_is_exact_decimal_subtraction():
    term = ContractTerm(term_id="t", client_id="c", term_type="minimum_spend",
                        contracted_value_usd=0.3, actual_billed_value_usd=0.1, period_label="p")
    assert term.drift_usd == Decimal("0.20")  # float: 0.3 - 0.1 == 0.19999999999999998
    f = contract_pricing_term_drift.detect([term])[0]
    assert f.recoverable_value.amount_usd == Decimal("0.20")
    assert check(f) is None


# --- every real agent emits Decimal money + canonical text -------------------

def _all_money_findings():
    orders = load_orders()
    return (
        affiliate_coupon_extension.detect(orders)
        + discount_misuse.detect(orders)
        + abandoned_cart_coverage.detect(orders)
        + renewal_never_triggered.detect(load_subscriptions())
        + server_side_attribution.detect(load_server_side_events())
        + contract_pricing_term_drift.detect(load_contract_terms())
    )


def test_every_real_finding_amount_is_decimal_and_stated_canonically_in_text():
    findings = [f for f in _all_money_findings() if f.recoverable_value is not None]
    assert len(findings) >= 7
    for f in findings:
        amt = f.recoverable_value.amount_usd
        assert isinstance(amt, Decimal), f.finding_id
        assert amt == amt.quantize(Decimal("0.01")), f.finding_id
        assert f"${format_money(amt)}" in f.cause_description, f.finding_id


def test_abandoned_cart_89_99_fixture_value_is_exact():
    f = [f for f in abandoned_cart_coverage.detect(load_orders()) if f.entity_id == "ord_1005"][0]
    assert f.recoverable_value.amount_usd == Decimal("89.99")


# --- JSON ---------------------------------------------------------------------

def test_json_round_trip_returns_identical_strings():
    for f in _all_money_findings():
        once = f.model_dump_json()
        again = Finding.model_validate_json(once).model_dump_json()
        assert once == again
        if f.recoverable_value is not None:
            amt = json.loads(once)["recoverable_value"]["amount_usd"]
            assert isinstance(amt, str) and WIRE.match(amt), amt


def test_json_number_input_is_rejected_but_python_float_fixture_input_is_accepted():
    # Changed in fix wave 1 (F15): this test used to assert that a JSON
    # NUMBER 49.99 was accepted from JSON text. Money on the wire is a
    # string (contract section 1) and orchestrator-go and the ledger reject
    # a JSON number, so JSON-text validation now rejects it too. The
    # fixture path (json.loads + model_validate, python mode) still takes
    # 49.99 through str() exactly.
    with pytest.raises(ValidationError):
        LabeledValue.model_validate_json('{"amount_usd": 49.99, "classification": "observed", "confidence": "high"}')
    lv = LabeledValue.model_validate(json.loads('{"amount_usd": 49.99, "classification": "observed", "confidence": "high"}'))
    assert lv.amount_usd == Decimal("49.99")
    assert lv.model_dump_json() == '{"amount_usd":"49.99","classification":"observed","confidence":"high"}'


# --- hallucination check agrees with formatting --------------------------------

def test_hallucination_check_requires_the_canonical_two_decimal_figure():
    assert check(_finding("Leaked $12.30 in discounts.", "12.30")) is None
    assert check(_finding("Leaked $12.3 in discounts.", "12.30")) is not None
    assert check(_finding("Leaked $12 in discounts.", "12.00")) is not None


def test_hallucination_check_handles_thousands_separators():
    assert check(_finding("Shortfall of $1,200.00 this period.", "1200.00")) is None


def test_hallucination_check_does_not_truncate_extra_fraction_digits():
    # The old regex stopped at two fraction digits, so "$12.345" read as 12.34.
    assert check(_finding("Leaked $12.345 in discounts.", "12.34")) is not None
    assert check(_finding("Leaked $12.345 in discounts.", "12.35")) is not None


def test_hallucination_check_has_no_float_tolerance():
    # Old check accepted anything within 0.005 of the claim.
    assert check(_finding("Leaked $2.00 in discounts.", "2.01")) is not None
    assert check(_finding("Leaked $2.01 in discounts.", "2.01")) is None


def test_hallucination_check_sentence_final_period_is_not_part_of_the_figure():
    assert check(_finding("This order leaked $75.00.", "75.00")) is None


# --- over the real REST surface ---------------------------------------------

def test_fixture_endpoints_serve_money_as_two_decimal_strings():
    orders = client.get("/fixtures/orders").json()
    prices = [li["unit_price_usd"] for o in orders for li in o["line_items"]]
    assert "89.99" in prices
    assert all(isinstance(p, str) and WIRE.match(p) for p in prices)
    subs = client.get("/fixtures/subscriptions").json()
    assert all(WIRE.match(s["plan_price_usd"]) for s in subs)
    terms = client.get("/fixtures/tier2/contract-terms").json()
    assert all(WIRE.match(t["actual_billed_value_usd"]) for t in terms)


def test_detect_endpoint_accepts_string_money_and_returns_string_money():
    body = {"orders": [{
        "order_id": "ord_x", "customer_id": "c", "placed_at": "2026-06-01T00:00:00Z",
        "status": "abandoned_cart", "source_platform": "shopify",
        "line_items": [{"sku": "a", "unit_price_usd": "0.10", "quantity": 1},
                       {"sku": "b", "unit_price_usd": "0.20", "quantity": 1}],
    }]}
    r = client.post("/agents/abandoned-cart-coverage/detect", json=body)
    assert r.status_code == 200
    f = r.json()["findings"][0]
    assert f["recoverable_value"]["amount_usd"] == "0.30"
    assert "$0.30" in f["cause_description"]


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "1e2", "12,30", "", "0.00", True])
def test_detect_endpoint_rejects_invalid_money_with_422(bad):
    body = {"orders": [{
        "order_id": "ord_x", "customer_id": "c", "placed_at": "2026-06-01T00:00:00Z",
        "status": "abandoned_cart", "source_platform": "shopify",
        "line_items": [{"sku": "a", "unit_price_usd": bad, "quantity": 1}],
    }]}
    r = client.post("/agents/abandoned-cart-coverage/detect", json=body)
    assert r.status_code == 422


def test_correlation_endpoint_round_trips_string_money_unchanged():
    orders = client.get("/fixtures/orders").json()
    aff = client.post("/agents/affiliate-coupon-extension/detect", json={"orders": orders}).json()["findings"]
    disc = client.post("/agents/discount-misuse/detect", json={"orders": orders}).json()["findings"]
    overlaps = client.post("/correlation/overlaps", json={"findings": aff + disc}).json()
    amounts = sorted(f["recoverable_value"]["amount_usd"] for f in overlaps["ord_1007"])
    assert amounts == ["150.00", "54.38"]


# --- signed zero / negative inputs (found in review of the WIP draft) --------

@pytest.mark.parametrize("bad", ["-0.00", "-0.004", "-0", -0.0, Decimal("-0.001"), "-12.30"])
def test_zero_allowed_money_field_rejects_negative_inputs_including_signed_zero(bad):
    # Before the fix, "-0.004" quantized to Decimal("-0.00"), passed ge=0 and
    # serialized as "-0.00" — a string outside the contract's wire pattern.
    with pytest.raises(ValidationError):
        ContractTerm(term_id="t", client_id="c", term_type="minimum_spend",
                     contracted_value_usd="10.00", actual_billed_value_usd=bad, period_label="p")


def test_quantize_never_returns_signed_zero():
    from zbm_schema import quantize_money
    q = quantize_money(Decimal("-0.001"))
    assert q == 0 and not q.is_signed() and format_money(q) == "0.00"


def test_every_money_value_on_every_fixture_endpoint_matches_wire_pattern():
    paths = ["/fixtures/orders", "/fixtures/subscriptions", "/fixtures/tier2/server-side-events",
             "/fixtures/tier2/contract-terms"]
    seen = 0

    def walk(node):
        nonlocal seen
        if isinstance(node, dict):
            for k, v in node.items():
                if k.endswith("_usd") and v is not None:
                    assert isinstance(v, str) and WIRE.match(v), (k, v)
                    seen += 1
                else:
                    walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    for p in paths:
        walk(client.get(p).json())
    assert seen >= 20
