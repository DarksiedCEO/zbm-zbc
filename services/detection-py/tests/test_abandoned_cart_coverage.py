from _fx import AS_OF, TENANT  # noqa: F401

from agents import abandoned_cart_coverage as agent
from fixtures_loader import load_orders
from zbm_schema import EvidenceClass


def test_uncovered_abandoned_cart_is_flagged():
    orders = load_orders()
    findings = agent.detect(orders, client_id=TENANT)
    flagged_ids = {f.entity_id for f in findings}
    assert "ord_1005" in flagged_ids


def test_recovered_abandoned_cart_is_not_flagged():
    orders = load_orders()
    findings = agent.detect(orders, client_id=TENANT)
    flagged_ids = {f.entity_id for f in findings}
    assert "ord_1006" not in flagged_ids, (
        "control case: recovery_attempted=True must not be flagged — "
        "that's the recovery system working, not a leak"
    )


def test_completed_orders_are_ignored():
    orders = load_orders()
    findings = agent.detect(orders, client_id=TENANT)
    flagged_ids = {f.entity_id for f in findings}
    for oid in ("ord_1001", "ord_1002", "ord_1003", "ord_1004"):
        assert oid not in flagged_ids


def test_cart_value_is_not_claimed_as_recoverable():
    # E-4 (Oct 6 2026): the whole cart value used to be the "recoverable"
    # figure. Only the share a recovery flow wins back is recoverable, and no
    # recovery rate is known — so no dollar figure, evidence UNKNOWN.
    orders = load_orders()
    findings = agent.detect(orders, client_id=TENANT)
    match = [f for f in findings if f.entity_id == "ord_1005"][0]
    assert match.recoverable_value is None
    assert match.evidence_class == EvidenceClass.UNKNOWN
    assert "$" not in match.cause_description
