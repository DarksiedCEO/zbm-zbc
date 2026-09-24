from decimal import Decimal

from agents import (
    contract_pricing_term_drift,
    cross_channel_attribution,
    platform_integration,
    server_side_attribution,
)
from fixtures_loader import (
    load_channel_touchpoints,
    load_contract_terms,
    load_platform_connections,
    load_server_side_events,
)
from zbm_schema import CauseCertainty


# --- Agent E: Server-Side Attribution -------------------------------------

def test_server_confirmed_pixel_missed_is_flagged():
    events = load_server_side_events()
    findings = server_side_attribution.detect(events)
    flagged = {f.entity_id for f in findings}
    assert flagged == {"ord_3001"}


def test_pixel_and_server_agree_not_flagged():
    events = load_server_side_events()
    findings = server_side_attribution.detect(events)
    flagged = {f.entity_id for f in findings}
    assert "ord_3002" not in flagged


def test_pixel_fired_server_unconfirmed_not_flagged_by_this_agent():
    events = load_server_side_events()
    findings = server_side_attribution.detect(events)
    flagged = {f.entity_id for f in findings}
    assert "ord_3003" not in flagged  # different leak category, out of scope here


# --- Agent F: Cross-Channel Attribution -----------------------------------

def test_paid_first_touch_overridden_flagged_uncertain_no_dollar_value():
    tps = load_channel_touchpoints()
    findings = cross_channel_attribution.detect(tps)
    match = [f for f in findings if f.entity_id == "ord_4001"]
    assert len(match) == 1
    assert match[0].cause_certainty == CauseCertainty.UNCERTAIN
    assert match[0].recoverable_value is None  # Failure Mode #3 — no model, no fabricated number


def test_single_touch_order_not_flagged():
    tps = load_channel_touchpoints()
    findings = cross_channel_attribution.detect(tps)
    flagged = {f.entity_id for f in findings}
    assert "ord_4002" not in flagged


def test_paid_first_touch_that_keeps_credit_not_flagged():
    tps = load_channel_touchpoints()
    findings = cross_channel_attribution.detect(tps)
    flagged = {f.entity_id for f in findings}
    assert "ord_4003" not in flagged


# --- Agent G: Platform Integration -----------------------------------------

def test_reported_but_unconnected_platform_flagged():
    statuses = load_platform_connections()
    findings = platform_integration.detect(statuses)
    flagged = {f.entity_id for f in findings}
    assert "client_a1:tiktok_shop" in flagged


def test_connected_platform_not_flagged():
    statuses = load_platform_connections()
    findings = platform_integration.detect(statuses)
    flagged = {f.entity_id for f in findings}
    assert "client_a1:shopify" not in flagged


def test_unused_platform_not_flagged():
    statuses = load_platform_connections()
    findings = platform_integration.detect(statuses)
    flagged = {f.entity_id for f in findings}
    assert "client_a1:amazon" not in flagged


# --- Agent H: Contract & Pricing-Term Drift --------------------------------

def test_minimum_spend_shortfall_flagged_with_correct_drift_amount():
    terms = load_contract_terms()
    findings = contract_pricing_term_drift.detect(terms)
    assert len(findings) == 1
    f = findings[0]
    assert f.entity_id == "term_b2_min_1"
    assert f.recoverable_value.amount_usd == Decimal("900.00")  # 5000 - 4100


def test_exact_match_not_flagged():
    terms = load_contract_terms()
    findings = contract_pricing_term_drift.detect(terms)
    flagged = {f.entity_id for f in findings}
    assert "term_b2_overage_1" not in flagged


def test_billed_above_minimum_not_flagged():
    terms = load_contract_terms()
    findings = contract_pricing_term_drift.detect(terms)
    flagged = {f.entity_id for f in findings}
    assert "term_b2_min_2" not in flagged
    assert len(findings) == 1
