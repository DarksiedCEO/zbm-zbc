"""
F14 — huge money used to crash the service with 500.

`unit_price_usd = "1" + "0"*30 + ".00"` made `Decimal.quantize` raise
`decimal.InvalidOperation` (more than the default context's 28 significant
digits). InvalidOperation is not a ValueError, so pydantic did not turn it
into a validation error and it escaped as 500 Internal Server Error.

Fix (ADR 0003 section 1a, a contract amendment): every money amount is
< 10^15 dollars (max "999999999999999.99"), enforced the same way in
Python (422), Go (reject) and the dashboard (display guard); every Decimal
operation in money paths runs under the explicit MONEY_CONTEXT; and an
order whose subtotal would exceed the maximum is rejected at validation
time (422), so no arithmetic result can leave the range.
"""

from datetime import datetime, timezone
from decimal import Decimal, getcontext, localcontext
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agents import abandoned_cart_coverage, discount_misuse
from api import app
from conftest import TEST_SERVICE_TOKEN
from zbm_schema import (
    DiscountApplication,
    Order,
    OrderLineItem,
    format_money,
    percent_of,
    quantize_money,
    to_money,
)
from zbm_schema.money import MONEY_CONTEXT
from zbm_schema.tier2 import ContractTerm

client = TestClient(
    app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}, raise_server_exceptions=False
)
MAX = "999999999999999.99"
OVER = "1000000000000000.00"


def _order_json(lines, status="abandoned_cart", discounts=None):
    return {
        "order_id": "o", "customer_id": "c", "placed_at": "2026-06-01T00:00:00Z", "status": status,
        "source_platform": "x",
        "line_items": [{"sku": f"s{i}", "unit_price_usd": p, "quantity": q} for i, (p, q) in enumerate(lines)],
        "discounts": discounts or [],
    }


def _order(lines, discounts=None, status="abandoned_cart"):
    return Order(
        order_id="o", customer_id="c", placed_at=datetime(2026, 6, 1, tzinfo=timezone.utc), status=status,
        source_platform="x",
        line_items=[OrderLineItem(sku=f"s{i}", unit_price_usd=p, quantity=q) for i, (p, q) in enumerate(lines)],
        discounts=discounts or [],
    )


def _half_up_percent(amount: str, pct: str) -> Decimal:
    """Reference: amount * pct / 100 in exact rationals, rounded half-up to cents."""
    cents = Fraction(amount) * Fraction(pct)  # = amount*pct/100 in cents
    want = (cents.numerator * 2 + cents.denominator) // (2 * cents.denominator)
    return Decimal(f"{want // 100}.{want % 100:02d}")  # no context-dependent operation


def _post_cart(lines):
    return client.post("/agents/abandoned-cart-coverage/detect", json={"orders": [_order_json(lines)]})


# --- the reproduction -------------------------------------------------------

def test_f14_reproduction_huge_unit_price_is_422_not_500():
    r = _post_cart([("1" + "0" * 30 + ".00", 1)])
    assert r.status_code == 422, (r.status_code, r.text[:300])


def test_huge_quantity_is_422_not_500():
    r = _post_cart([("10.00", 10**40)])
    assert r.status_code == 422, (r.status_code, r.text[:300])


def test_huge_contract_value_is_422_not_500():
    r = client.post("/agents/contract-pricing-term-drift/detect", json={"terms": [{
        "term_id": "t", "client_id": "c", "term_type": "minimum_spend",
        "contracted_value_usd": "9" * 27 + ".99", "actual_billed_value_usd": "0.01", "period_label": "p"}]})
    assert r.status_code == 422, (r.status_code, r.text[:300])


# --- the boundary, exactly --------------------------------------------------

def test_maximum_unit_price_is_accepted_and_round_trips_exactly():
    r = _post_cart([(MAX, 1)])
    assert r.status_code == 200, r.text[:300]
    assert r.json()["findings"][0]["recoverable_value"]["amount_usd"] == MAX


def test_one_cent_over_the_maximum_is_rejected():
    assert _post_cart([(OVER, 1)]).status_code == 422
    with pytest.raises(ValueError):
        to_money(OVER)


def test_subtotal_exactly_at_the_maximum_is_accepted():
    r = _post_cart([("500000000000000.00", 1), ("499999999999999.99", 1)])
    assert r.status_code == 200, r.text[:300]
    assert r.json()["findings"][0]["recoverable_value"]["amount_usd"] == MAX


def test_subtotal_one_cent_over_the_maximum_is_422():
    r = _post_cart([("500000000000000.00", 1), ("500000000000000.00", 1)])
    assert r.status_code == 422, (r.status_code, r.text[:300])
    r = _post_cart([(MAX, 1), ("0.01", 1)])
    assert r.status_code == 422, (r.status_code, r.text[:300])


def test_two_maximum_lines_are_422_not_500():
    r = _post_cart([(MAX, 1), (MAX, 1)])
    assert r.status_code == 422, (r.status_code, r.text[:300])


def test_quantity_times_price_over_the_maximum_is_422():
    r = _post_cart([("100000000000000.00", 10)])
    assert r.status_code == 422, (r.status_code, r.text[:300])
    r = _post_cart([("99999999999999.99", 10)])  # 999999999999999.90, just under
    assert r.status_code == 200, r.text[:300]
    assert r.json()["findings"][0]["recoverable_value"]["amount_usd"] == "999999999999999.90"


def test_subtotal_of_many_large_lines_is_exact_and_never_raises():
    # 50 lines (the per-order maximum, zbm_schema/limits.py — this test used
    # 1000 lines before orders had a line-item cap) of 19,999,999,999,999.99
    # = 999,999,999,999,999.50 (< max)
    o = _order([("19999999999999.99", 1)] * 50)
    assert o.subtotal_usd == Decimal("999999999999999.50")
    assert format_money(o.subtotal_usd) == "999999999999999.50"
    [f] = abandoned_cart_coverage.detect([o])
    assert f.model_dump(mode="json")["recoverable_value"]["amount_usd"] == "999999999999999.50"


def test_order_model_rejects_over_maximum_subtotal_with_validation_error():
    with pytest.raises(ValidationError):
        _order([(MAX, 1), ("0.01", 1)])


@pytest.mark.parametrize("value,expected", [
    (Decimal("999999999999999.994"), MAX),       # rounds down to the max: accepted
    (Decimal("999999999999999.99"), MAX),
    (999999999999999, "999999999999999.00"),
])
def test_to_money_boundary_values_that_are_accepted(value, expected):
    assert format_money(to_money(value)) == expected


@pytest.mark.parametrize("value", [
    Decimal("999999999999999.995"),   # rounds UP to 10^15: rejected
    Decimal(OVER), Decimal("1e15"), Decimal("1e40"), Decimal("1e100000"),
    10**15, 10**5000, 1e15, 1e300,
], ids=["d995", "over", "1e15", "1e40", "1e100000", "int1e15", "int1e5000", "float1e15", "float1e300"])
def test_to_money_over_maximum_raises_value_error_never_invalid_operation(value):
    with pytest.raises(ValueError):
        to_money(value)


def test_quantize_money_out_of_range_raises_value_error_not_invalid_operation():
    for v in (Decimal("1e40"), Decimal("-1e40"), Decimal(OVER), Decimal("1e100000")):
        with pytest.raises(ValueError):
            quantize_money(v)


# --- explicit context, not the ambient one ----------------------------------

def test_money_arithmetic_ignores_a_hostile_ambient_decimal_context():
    """Every money operation runs under MONEY_CONTEXT, so a caller (or a
    library) that lowered the thread's context precision cannot make money
    arithmetic round or raise."""
    o = _order([("499999999999999.99", 1), ("500000000000000.00", 1)])
    d = DiscountApplication(code="a", percent_off=33.33333333333333)
    with localcontext() as ctx:
        ctx.prec = 5
        assert getcontext().prec == 5
        assert o.subtotal_usd == Decimal(MAX)
        assert format_money(o.subtotal_usd) == MAX
        assert to_money(MAX) == Decimal(MAX)
        assert percent_of(Decimal(MAX), 33.33333333333333) == _half_up_percent(MAX, "33.33333333333333")
        term = ContractTerm(term_id="t", client_id="c", term_type="minimum_spend",
                            contracted_value_usd=MAX, actual_billed_value_usd="0.01", period_label="p")
        assert term.drift_usd == Decimal("999999999999999.98")
        o2 = _order([(MAX, 1)], discounts=[d, DiscountApplication(code="b", amount_off_usd="0.01")], status="completed")
        [f] = discount_misuse.detect([o2])
        assert f.recoverable_value.amount_usd > 0


def test_percent_of_maximum_is_exact_half_up():
    # 999999999999999.99 * 12.5% = 124999999999999.99875 -> 125000000000000.00 half-up
    assert percent_of(Decimal(MAX), 12.5) == Decimal("125000000000000.00")
    # 33.33333333333333% of the max, compared against exact rational arithmetic
    assert percent_of(Decimal(MAX), 33.33333333333333) == _half_up_percent(MAX, "33.33333333333333")


def test_money_context_is_documented_and_has_the_stated_precision():
    assert MONEY_CONTEXT.prec >= 40
