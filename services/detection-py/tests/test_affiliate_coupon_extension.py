from decimal import Decimal

from _fx import AS_OF, TENANT  # noqa: F401

from agents import affiliate_coupon_extension as agent
from fixtures_loader import load_orders
from zbm_schema import CauseCertainty, EvidenceClass


def test_legit_affiliate_order_not_flagged():
    orders = load_orders()
    findings = agent.detect(orders, client_id=TENANT)
    flagged_ids = {f.entity_id for f in findings}
    assert "ord_1001" not in flagged_ids, "control case (legit, within-window affiliate order) was wrongly flagged"


def test_stretched_affiliate_window_is_flagged_with_named_cause():
    orders = load_orders()
    findings = agent.detect(orders, client_id=TENANT)
    match = [f for f in findings if f.entity_id == "ord_1002"]
    assert len(match) == 1, "fraudulent affiliate-extension order was not detected"
    finding = match[0]
    assert finding.cause_certainty == CauseCertainty.NAMED
    assert finding.recoverable_value is not None
    # E-4: the commission at risk (10% of the $120.00 subtotal), not the order.
    assert finding.recoverable_value.amount_usd == Decimal("12.00")
    assert finding.evidence_class == EvidenceClass.ESTIMATED


def test_orders_without_affiliate_attribution_are_ignored():
    orders = load_orders()
    findings = agent.detect(orders, client_id=TENANT)
    flagged_ids = {f.entity_id for f in findings}
    # ord_1003, ord_1004, ord_1005, ord_1006 have no affiliate block at all
    for oid in ("ord_1003", "ord_1004", "ord_1005", "ord_1006"):
        assert oid not in flagged_ids


def test_every_finding_carries_the_correlation_key():
    orders = load_orders()
    findings = agent.detect(orders, client_id=TENANT)
    for f in findings:
        assert f.entity_id, "Finding missing entity_id — breaks Decision 3 double-count correlation"
        assert f.entity_type == "order"
        assert f.client_id == TENANT
