"""
AEGIS approved-with-conditions review of fix-revrec 8bdebde (Oct 7 2026),
detection side:

  M2  renewal was labeled OBSERVED/HIGH on ESTIMATED evidence. One rule now
      holds for every finding: labels never claim more than the evidence
      class supports (zbm_schema.labels_exceed_evidence, enforced by Finding).
  M3  a future `as_of` was accepted, so a renewal not yet due was reported as
      missed (probe P11's regression through as_of). Refused beyond the
      stated clock-skew tolerance (api.AS_OF_CLOCK_SKEW, 60 s).
  L1  the affiliate commission's base (order subtotal) and rate were only in
      the prose; they are now a checked `value_basis` on the finding.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from _fx import AS_OF, TENANT, make_finding
from conftest import TEST_SERVICE_TOKEN
from fastapi.testclient import TestClient

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
from fixtures_loader import (
    load_channel_touchpoints,
    load_contract_terms,
    load_orders,
    load_platform_connections,
    load_server_side_events,
    load_subscriptions,
)
from zbm_schema import (
    DecisionConfidence,
    EvidenceClass,
    LabeledValue,
    ValueBasis,
    ValueClassification,
    labels_exceed_evidence,
    rate_percent_text,
)

client = TestClient(api.app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}, raise_server_exceptions=False)


def _all_fixture_findings():
    return (
        affiliate_coupon_extension.detect(load_orders(), client_id=TENANT)
        + discount_misuse.detect(load_orders(), client_id=TENANT)
        + abandoned_cart_coverage.detect(load_orders(), client_id=TENANT)
        + renewal_never_triggered.detect(load_subscriptions(), client_id=TENANT, as_of=AS_OF)
        + server_side_attribution.detect(load_server_side_events(), client_id=TENANT)
        + cross_channel_attribution.detect(load_channel_touchpoints(), client_id=TENANT)
        + platform_integration.detect(load_platform_connections(), client_id=TENANT)
        + contract_pricing_term_drift.detect(load_contract_terms(), client_id=TENANT)
    )


# --- M2 ------------------------------------------------------------------------------

def test_m2_renewal_labels_no_longer_claim_observation():
    [f] = renewal_never_triggered.detect(load_subscriptions(), client_id=TENANT, as_of=AS_OF)
    assert f.evidence_class == EvidenceClass.ESTIMATED
    assert (f.recoverable_value.classification, f.recoverable_value.confidence) == (
        ValueClassification.INCREMENTAL, DecisionConfidence.MEDIUM)


def test_m2_the_pre_fix_renewal_labels_cannot_be_constructed():
    with pytest.raises(ValueError, match="claim more than the evidence supports"):
        make_finding(recoverable_value=LabeledValue(amount_usd=Decimal("39.00"),
                                                    classification=ValueClassification.OBSERVED,
                                                    confidence=DecisionConfidence.HIGH),
                     evidence_class=EvidenceClass.ESTIMATED)


@pytest.mark.parametrize("evidence,classification,confidence", list(itertools.product(
    [EvidenceClass.OBSERVED, EvidenceClass.ESTIMATED, EvidenceClass.MODELED],
    list(ValueClassification), list(DecisionConfidence))))
def test_m2_invariant_holds_for_every_label_combination(evidence, classification, confidence):
    value = LabeledValue(amount_usd=Decimal("1.00"), classification=classification, confidence=confidence)
    overclaims = evidence != EvidenceClass.OBSERVED and (
        classification in (ValueClassification.OBSERVED, ValueClassification.FINANCIALLY_VERIFIED)
        or confidence in (DecisionConfidence.HIGH, DecisionConfidence.VERY_HIGH))
    assert (labels_exceed_evidence(evidence, value) is not None) == overclaims
    if overclaims:
        with pytest.raises(ValueError):
            make_finding(recoverable_value=value, evidence_class=evidence)
    else:
        assert make_finding(recoverable_value=value, evidence_class=evidence).recoverable_value == value


def test_m2_every_agent_emits_labels_its_evidence_supports():
    # Every finding type the fixture pool produces (all eight agents). The Finding model would
    # already refuse a violation; this also pins that each valued finding type is covered.
    findings = _all_fixture_findings()
    valued = {f.agent_id for f in findings if f.recoverable_value is not None}
    assert valued == {"affiliate-coupon-extension-v1", "discount-misuse-v1",
                      "renewal-never-triggered-v1", "contract-pricing-term-drift-v1"}
    for f in findings:
        assert labels_exceed_evidence(f.evidence_class, f.recoverable_value) is None, f


# --- M3 ------------------------------------------------------------------------------

def _renewal(as_of: str):
    sub = {"subscription_id": "s1", "customer_id": "c", "plan_price_usd": "30.00", "renewal_interval_days": 30,
           "next_renewal_due_at": "2099-01-01T00:00:00Z", "status": "lapsed_no_renewal_attempt"}
    r = client.post("/agents/renewal-never-triggered/detect",
                    json={"client_id": TENANT, "as_of": as_of, "subscriptions": [sub]})
    return r.status_code, r.json()


def test_m3_future_as_of_is_refused_and_flags_nothing():
    # P11 through as_of: an as_of past the 2099 due date used to report it as missed.
    s, j = _renewal("2100-01-01T00:00:00Z")
    assert s == 422, j
    assert j["detail"][0]["loc"] == ["body", "as_of"] and "future" in j["detail"][0]["msg"]


def test_m3_clock_skew_tolerance_is_60_seconds_and_stated():
    assert api.AS_OF_CLOCK_SKEW == timedelta(seconds=60)
    now = datetime.now(timezone.utc)
    s, _ = _renewal((now + timedelta(seconds=30)).isoformat())
    assert s == 200  # within the tolerance
    s, j = _renewal((now + timedelta(seconds=120)).isoformat())
    assert s == 422 and "60 s clock-skew" in j["detail"][0]["msg"], j
    s, _ = _renewal((now - timedelta(days=1)).isoformat())
    assert s == 200


# --- L1 ------------------------------------------------------------------------------

def test_l1_affiliate_commission_records_its_base_and_rate():
    by = {f.entity_id: f for f in affiliate_coupon_extension.detect(load_orders(), client_id=TENANT)}
    f = by["ord_1002"]
    assert f.value_basis == ValueBasis(base_usd=Decimal("120.00"), rate_percent="10")
    assert f.recoverable_value.amount_usd == Decimal("12.00")
    assert by["ord_1007"].value_basis is None  # no rate, no figure, no basis
    # On the wire, exactly as orchestrator-go records it.
    wire = f.model_dump(mode="json")
    assert wire["value_basis"] == {"base_usd": "120.00", "rate_percent": "10"}


def test_l1_value_basis_must_reproduce_the_amount():
    value = LabeledValue(amount_usd=Decimal("12.00"), classification=ValueClassification.ATTRIBUTED,
                         confidence=DecisionConfidence.MEDIUM)
    make_finding(recoverable_value=value, evidence_class=EvidenceClass.ESTIMATED,
                 value_basis=ValueBasis(base_usd=Decimal("120.00"), rate_percent="10"))
    with pytest.raises(ValueError, match="is not value_basis"):
        make_finding(recoverable_value=value, evidence_class=EvidenceClass.ESTIMATED,
                     value_basis=ValueBasis(base_usd=Decimal("400.00"), rate_percent="10"))
    with pytest.raises(ValueError, match="without a recoverable_value"):
        make_finding(value_basis=ValueBasis(base_usd=Decimal("120.00"), rate_percent="10"))


@pytest.mark.parametrize("rate,text", [(10, "10"), (15.0, "15"), (12.5, "12.5"), (1e-05, "0.00001"),
                                       (100, "100"), (0.1, "0.1")])
def test_l1_rate_text_is_the_exact_rate_used(rate, text):
    assert rate_percent_text(rate) == text


@pytest.mark.parametrize("bad", ["0", "100.01", "1e5", "-1", "01", ".5", "5.", "0." + "1" * 33])
def test_l1_rate_percent_is_bounded(bad):
    with pytest.raises(ValueError):
        ValueBasis(base_usd=Decimal("1.00"), rate_percent=bad)
