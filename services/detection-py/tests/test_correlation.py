from _fx import AS_OF, TENANT  # noqa: F401

from agents import affiliate_coupon_extension, discount_misuse
from fixtures_loader import load_orders
from zbm_schema.correlation import find_overlapping_entities


def test_ord_1007_is_caught_as_double_claimed_by_two_agents():
    """
    ord_1007 is deliberately built (see fixtures/orders.json) to trigger
    BOTH the affiliate-coupon-extension agent and the discount-misuse
    agent. This proves Decision 3's anti-double-counting safeguard is
    real, not just documented: before any dollar figure is presented,
    the correlation layer must be able to see that two agents both
    claimed the same order.
    """
    orders = load_orders()
    combined = affiliate_coupon_extension.detect(orders, client_id=TENANT) + discount_misuse.detect(orders, client_id=TENANT)

    overlaps = find_overlapping_entities(combined)

    key = f"{TENANT}|order|ord_1007"  # (client_id, entity_type, entity_id) — E-3
    assert key in overlaps
    assert len(overlaps[key]) == 2
    claiming_agents = {f.agent_id for f in overlaps[key]}
    assert claiming_agents == {"affiliate-coupon-extension-v1", "discount-misuse-v1"}


def test_non_overlapping_orders_are_not_reported():
    orders = load_orders()
    combined = affiliate_coupon_extension.detect(orders, client_id=TENANT) + discount_misuse.detect(orders, client_id=TENANT)
    overlaps = find_overlapping_entities(combined)
    # ord_1002 (affiliate only) and ord_1003 (discount only) are single-claimed
    assert f"{TENANT}|order|ord_1002" not in overlaps
    assert f"{TENANT}|order|ord_1003" not in overlaps
