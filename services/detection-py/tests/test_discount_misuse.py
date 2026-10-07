from decimal import Decimal

from _fx import AS_OF, TENANT  # noqa: F401

from agents import discount_misuse as agent
from fixtures_loader import load_orders


def test_single_discount_not_flagged():
    orders = load_orders()
    findings = agent.detect(orders, client_id=TENANT)
    flagged_ids = {f.entity_id for f in findings}
    assert "ord_1004" not in flagged_ids, "control case (single legit discount) was wrongly flagged"


def test_stacked_discounts_flagged():
    orders = load_orders()
    findings = agent.detect(orders, client_id=TENANT)
    flagged_ids = {f.entity_id for f in findings}
    assert "ord_1003" in flagged_ids
    assert "ord_1007" in flagged_ids  # dual-leak correlation fixture also stacks discounts


def test_stacked_discount_value_is_computed_sequentially():
    orders = load_orders()
    findings = agent.detect(orders, client_id=TENANT)
    match = [f for f in findings if f.entity_id == "ord_1003"][0]
    # subtotal 400.00; 15% off -> 340.00 remaining, given=60.00; then 20% of 340 -> given=68.00
    # total given = 128.00. E-4: the best single code (20% of 400.00 = 80.00) was
    # the customer's to take; only the excess, 48.00, is the leak.
    assert match.recoverable_value.amount_usd == Decimal("48.00")
    assert "$128.00" in match.cause_description and "$80.00" in match.cause_description


def test_orders_with_no_discounts_are_ignored():
    orders = load_orders()
    findings = agent.detect(orders, client_id=TENANT)
    flagged_ids = {f.entity_id for f in findings}
    assert "ord_1005" not in flagged_ids
    assert "ord_1006" not in flagged_ids
