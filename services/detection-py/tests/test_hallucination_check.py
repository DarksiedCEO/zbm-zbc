from decimal import Decimal

from _fx import AS_OF, TENANT, make_finding

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
from fixtures_loader import (
    load_channel_touchpoints,
    load_contract_terms,
    load_orders,
    load_platform_connections,
    load_server_side_events,
    load_subscriptions,
)
from safety.hallucination_check import HallucinationViolation, check, check_all
from zbm_schema import (
    CauseCertainty,
    DecisionConfidence,
    Finding,
    LabeledValue,
    ValueClassification,
)


def test_every_real_agent_output_passes_the_hallucination_check():
    """
    Real regression test: runs every Tier 1 + Tier 2 agent against the
    shared fixture pool and checks EVERY finding's cause_description
    against its own recoverable_value. This is exactly the Failure Mode #1
    safeguard exercised for real, not just unit-tested in isolation.
    """
    orders = load_orders()
    subs = load_subscriptions()

    all_findings: list[Finding] = []
    all_findings += affiliate_coupon_extension.detect(orders, client_id=TENANT)
    all_findings += discount_misuse.detect(orders, client_id=TENANT)
    all_findings += abandoned_cart_coverage.detect(orders, client_id=TENANT)
    all_findings += renewal_never_triggered.detect(subs, client_id=TENANT, as_of=AS_OF)
    all_findings += server_side_attribution.detect(load_server_side_events(), client_id=TENANT)
    all_findings += cross_channel_attribution.detect(load_channel_touchpoints(), client_id=TENANT)
    all_findings += platform_integration.detect(load_platform_connections(), client_id=TENANT)
    all_findings += contract_pricing_term_drift.detect(load_contract_terms(), client_id=TENANT)

    assert len(all_findings) > 0, "sanity check: fixtures should produce findings"

    violations = check_all(all_findings)
    assert violations == [], f"hallucination check found real violations in production agent output: {violations}"


def test_catches_a_fabricated_unbacked_dollar_claim():
    bad_finding = make_finding(
        cause_certainty=CauseCertainty.NAMED,
        cause_description="This order leaked $500.00 in discounts.",  # claims a number
        recoverable_value=None,  # ...but no LabeledValue backs it up
    )
    v = check(bad_finding)
    assert v is not None
    assert isinstance(v, HallucinationViolation)


def test_catches_a_mismatched_dollar_figure():
    bad_finding = make_finding(
        cause_certainty=CauseCertainty.NAMED,
        cause_description="This order leaked $500.00 in discounts.",
        recoverable_value=LabeledValue(
            amount_usd="75.00",  # description says 500, evidence says 75 — divergence
            classification=ValueClassification.OBSERVED, confidence=DecisionConfidence.HIGH,
        ),
    )
    v = check(bad_finding)
    assert v is not None


def test_matching_dollar_figure_passes():
    good_finding = make_finding(
        cause_certainty=CauseCertainty.NAMED,
        cause_description="This order leaked $75.00 in discounts.",
        recoverable_value=LabeledValue(
            amount_usd=Decimal("75.00"), classification=ValueClassification.OBSERVED, confidence=DecisionConfidence.HIGH,
        ),
    )
    assert check(good_finding) is None
