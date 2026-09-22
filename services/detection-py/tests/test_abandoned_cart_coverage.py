from agents import abandoned_cart_coverage as agent
from fixtures_loader import load_orders
from zbm_schema import ValueClassification


def test_uncovered_abandoned_cart_is_flagged():
    orders = load_orders()
    findings = agent.detect(orders)
    flagged_ids = {f.entity_id for f in findings}
    assert "ord_1005" in flagged_ids


def test_recovered_abandoned_cart_is_not_flagged():
    orders = load_orders()
    findings = agent.detect(orders)
    flagged_ids = {f.entity_id for f in findings}
    assert "ord_1006" not in flagged_ids, (
        "control case: recovery_attempted=True must not be flagged — "
        "that's the recovery system working, not a leak"
    )


def test_completed_orders_are_ignored():
    orders = load_orders()
    findings = agent.detect(orders)
    flagged_ids = {f.entity_id for f in findings}
    for oid in ("ord_1001", "ord_1002", "ord_1003", "ord_1004"):
        assert oid not in flagged_ids


def test_cart_value_is_labeled_incremental_not_observed():
    orders = load_orders()
    findings = agent.detect(orders)
    match = [f for f in findings if f.entity_id == "ord_1005"][0]
    # cart value is real (observed), but whether it *recovers* if pursued is not certain —
    # so the recoverable estimate must be INCREMENTAL, never overstated as OBSERVED/VERIFIED.
    assert match.recoverable_value.classification == ValueClassification.INCREMENTAL
