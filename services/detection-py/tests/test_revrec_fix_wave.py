"""
Revenue Recovery fix wave (Oct 6 2026): one regression test per probe of the
backend bug sweep that reproduced on integration 5d49ee9
(scratchpad/sweep-E/probe_detection.py P1-P12, probe_nil_findings.py) and per
finding E-3, E-4, E-10..E-14. Every test here fails on 5d49ee9.

The orchestrator-side probes (E-1 nil findings, E-2 partial/duplicate scans,
E-6 batching past the 1,000-item cap) are Go tests in services/orchestrator-go.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from _fx import AS_OF, AS_OF_WIRE, TENANT
from conftest import TEST_SERVICE_TOKEN
from fastapi.testclient import TestClient

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
from api import app
from fixtures_loader import (
    load_channel_touchpoints,
    load_contract_terms,
    load_orders,
    load_platform_connections,
    load_server_side_events,
    load_subscriptions,
)
from zbm_schema import EvidenceClass, compute_finding_id
from zbm_schema import limits as L

client = TestClient(app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}, raise_server_exceptions=False)


def post(path, body):
    r = client.post(path, json=body)
    return r.status_code, r.json()


def order(oid, prices, **kw):
    return dict(order_id=oid, customer_id="c1", placed_at="2026-06-01T00:00:00Z",
                status=kw.pop("status", "completed"),
                line_items=[{"sku": "s", "unit_price_usd": p, "quantity": 1} for p in prices],
                source_platform="shopify", **kw)


def far_affiliate(**kw):
    # 19 days after the click on a 24 h window: well past the 2x threshold.
    return dict(affiliate_id="aff", click_timestamp="2026-06-01T00:00:00Z",
                order_timestamp="2026-06-20T00:00:00Z", attribution_window_hours=24, **kw)


# --- E-3: finding identity ----------------------------------------------------

def test_p1_two_terms_of_one_client_and_period_are_two_findings():
    terms = [{"term_id": "term_b2_min_1", "client_id": TENANT, "term_type": "minimum_spend",
                  "contracted_value_usd": "5000.00", "actual_billed_value_usd": "4100.00", "period_label": "2026-06"},
             {"term_id": "term_b2_min_2", "client_id": TENANT, "term_type": "minimum_spend",
                  "contracted_value_usd": "2000.00", "actual_billed_value_usd": "1500.00", "period_label": "2026-06"}]
    s, j = post("/agents/contract-pricing-term-drift/detect", {"client_id": TENANT, "terms": terms})
    assert s == 200, j
    ids = [f["finding_id"] for f in j["findings"]]
    assert len(ids) == 2 and len(set(ids)) == 2, ids
    assert sorted(f["recoverable_value"]["amount_usd"] for f in j["findings"]) == ["500.00", "900.00"]


def test_p2_client_and_platform_no_longer_concatenate_into_one_id():
    a = {"client_id": "a-b", "platform": "c", "client_reports_using_it": True, "integration_connected": False}
    b = {"client_id": "a", "platform": "b-c", "client_reports_using_it": True, "integration_connected": False}
    s1, j1 = post("/agents/platform-integration/detect", {"client_id": "a-b", "statuses": [a]})
    s2, j2 = post("/agents/platform-integration/detect", {"client_id": "a", "statuses": [b]})
    assert (s1, s2) == (200, 200), (j1, j2)
    (f1,), (f2,) = j1["findings"], j2["findings"]
    assert f1["finding_id"] != f2["finding_id"]
    assert (f1["client_id"], f1["entity_id"]) == ("a-b", "c")
    assert (f2["client_id"], f2["entity_id"]) == ("a", "b_c")  # platform is a normalized slug (E-11)


def test_same_order_id_under_two_tenants_is_two_findings_and_never_an_overlap():
    o = order("ord_1", ["100.00"], discounts=[{"code": "A", "percent_off": 10}, {"code": "B", "percent_off": 10}])
    _, ja = post("/agents/discount-misuse/detect", {"client_id": "tenant-a", "orders": [o]})
    _, jb = post("/agents/discount-misuse/detect", {"client_id": "tenant-b", "orders": [o]})
    o2 = order("ord_1", ["100.00"], status="abandoned_cart")
    _, jc = post("/agents/abandoned-cart-coverage/detect", {"client_id": "tenant-b", "orders": [o2]})
    fa, fb, fc = ja["findings"][0], jb["findings"][0], jc["findings"][0]
    assert fa["finding_id"] != fb["finding_id"]
    s, ov = post("/correlation/overlaps", {"findings": [fa, fb, fc]})
    assert s == 200
    # tenant-a's ord_1 is NOT tenant-b's ord_1; only tenant-b's has two agents on it.
    assert list(ov) == ["tenant-b|order|ord_1"]
    assert {f["agent_id"] for f in ov["tenant-b|order|ord_1"]} == {"discount-misuse-v1", "abandoned-cart-coverage-v1"}


def test_finding_id_is_the_documented_hash_cross_language_vectors():
    # The same vectors are pinned in orchestrator-go internal/orchestrator/finding_id_test.go.
    assert compute_finding_id("fixture-pool", "discount-misuse-v1", "order", "ord_1007", None) == \
        "rrf1-5b3a1a510e08d4f61650cebebf008903eb533767"
    assert compute_finding_id("client_b2", "contract-pricing-term-drift-v1", "contract_term", "term_b2_min_1",
                              "2026-06") == "rrf1-7ffe4cd8e48f5047025947ef37f5fdc688fa0fca"
    assert compute_finding_id("gid-tenant", "renewal-never-triggered-v1", "subscription",
                              "gid://shopify/Subscription/9", "2026-05-15") == \
        "rrf1-2710bcc11b6ee6f5770fb721fd6b92c91b4c4642"


def test_a_forged_finding_id_is_refused_by_correlation():
    o = order("o9", ["50.00"], status="abandoned_cart")
    _, j = post("/agents/abandoned-cart-coverage/detect", {"client_id": TENANT, "orders": [o]})
    f = dict(j["findings"][0], finding_id="rrf1-" + "0" * 40)
    s, _ = post("/correlation/overlaps", {"findings": [f]})
    assert s == 422


def test_a_term_or_status_of_another_tenant_is_refused():
    term = {"term_id": "t", "client_id": "someone-else", "term_type": "minimum_spend",
                "contracted_value_usd": "10.00", "actual_billed_value_usd": "1.00", "period_label": "2026-06"}
    s, j = post("/agents/contract-pricing-term-drift/detect", {"client_id": TENANT, "terms": [term]})
    assert s == 422 and j["detail"][0]["loc"] == ["body", "terms", 0], j
    st = {"client_id": "someone-else", "platform": "amazon", "client_reports_using_it": True, "integration_connected": False}
    s, j = post("/agents/platform-integration/detect", {"client_id": TENANT, "statuses": [st]})
    assert s == 422, j


def test_detect_requires_a_tenant():
    s, j = post("/agents/abandoned-cart-coverage/detect", {"orders": []})
    assert s == 422 and any(d["loc"] == ["body", "client_id"] for d in j["detail"]), j


# --- E-4: no overstated dollar figures ------------------------------------------

def test_p3_discount_stacking_claims_only_the_excess_over_the_best_single_code():
    o = order("o1", ["100.00"], discounts=[{"code": "WELCOME10", "percent_off": 10}, {"code": "EXTRA5", "percent_off": 5}])
    _, j = post("/agents/discount-misuse/detect", {"client_id": TENANT, "orders": [o]})
    (f,) = j["findings"]
    # 10.00 + 5% of 90.00 = 14.50 given; WELCOME10 alone = 10.00 -> 4.50 beyond policy.
    assert f["recoverable_value"]["amount_usd"] == "4.50"
    assert f["evidence_class"] == "OBSERVED" and f["methodology_id"] == "disc_excess_best_code"


def test_discount_best_single_code_can_be_an_amount_off_code():
    o = order("o2", ["100.00"], discounts=[{"code": "P10", "percent_off": 10}, {"code": "FLAT30", "amount_off_usd": "30.00"}])
    _, j = post("/agents/discount-misuse/detect", {"client_id": TENANT, "orders": [o]})
    # 10.00 + 30.00 = 40.00 given; FLAT30 alone = 30.00 -> 10.00.
    assert j["findings"][0]["recoverable_value"]["amount_usd"] == "10.00"


def test_p4_server_side_attribution_claims_no_dollar_figure():
    ev = {"order_id": "o9", "channel": "meta", "order_value_usd": "250.00", "pixel_attributed": False, "server_confirmed": True}
    _, j = post("/agents/server-side-attribution/detect", {"client_id": TENANT, "events": [ev]})
    (f,) = j["findings"]
    assert f["recoverable_value"] is None
    assert f["evidence_class"] == "UNKNOWN"
    assert "$" not in f["cause_description"] and "250.00" not in f["cause_description"]


def test_p5_affiliate_claims_commission_times_rate_or_nothing():
    no_rate = order("o2", ["400.00"], affiliate=far_affiliate())
    with_rate = order("o3", ["400.00"], affiliate=far_affiliate(commission_rate_percent=10))
    _, j = post("/agents/affiliate-coupon-extension/detect", {"client_id": TENANT, "orders": [no_rate, with_rate]})
    by = {f["entity_id"]: f for f in j["findings"]}
    assert by["o2"]["recoverable_value"] is None and by["o2"]["evidence_class"] == "UNKNOWN"
    assert by["o2"]["cause_certainty"] == "named"
    assert by["o3"]["recoverable_value"] == {"amount_usd": "40.00", "classification": "attributed", "confidence": "medium"}
    assert by["o3"]["evidence_class"] == "ESTIMATED"


def test_every_finding_carries_an_evidence_class_and_a_methodology_note():
    findings = (
        affiliate_coupon_extension.detect(load_orders(), client_id=TENANT)
        + discount_misuse.detect(load_orders(), client_id=TENANT)
        + abandoned_cart_coverage.detect(load_orders(), client_id=TENANT)
        + renewal_never_triggered.detect(load_subscriptions(), client_id=TENANT, as_of=AS_OF)
        + server_side_attribution.detect(load_server_side_events(), client_id=TENANT)
        + cross_channel_attribution.detect(load_channel_touchpoints(), client_id=TENANT)
        + platform_integration.detect(load_platform_connections(), client_id=TENANT)
        + contract_pricing_term_drift.detect(load_contract_terms(), client_id=TENANT)
    )
    assert len(findings) == 10
    for f in findings:
        assert f.methodology and f.methodology_id
        assert (f.recoverable_value is None) == (f.evidence_class == EvidenceClass.UNKNOWN), f.finding_id
    # The whole fixture pool's claimed dollars, finding by finding (ADR 0001 "Evidence class and
    # methodology" lists the before/after).
    claimed = sorted((f.agent_id, f.entity_id, f.recoverable_value and str(f.recoverable_value.amount_usd),
                      f.evidence_class.value) for f in findings)
    assert claimed == sorted([
        ("affiliate-coupon-extension-v1", "ord_1002", "12.00", "ESTIMATED"),
        ("affiliate-coupon-extension-v1", "ord_1007", None, "UNKNOWN"),
        ("discount-misuse-v1", "ord_1003", "48.00", "OBSERVED"),
        ("discount-misuse-v1", "ord_1007", "16.88", "OBSERVED"),
        ("abandoned-cart-coverage-v1", "ord_1005", None, "UNKNOWN"),
        ("renewal-never-triggered-v1", "sub_2001", "39.00", "ESTIMATED"),
        ("server-side-attribution-v1", "ord_3001", None, "UNKNOWN"),
        ("cross-channel-attribution-v1", "ord_4001", None, "UNKNOWN"),
        ("platform-integration-v1", "tiktok_shop", None, "UNKNOWN"),
        ("contract-pricing-term-drift-v1", "term_b2_min_1", "900.00", "OBSERVED"),
    ])


# --- E-10 / E-11 / E-12 ---------------------------------------------------------

def test_p6_status_is_normalized_not_case_sensitive_free_text():
    for status in ("Abandoned_Cart", " abandoned-cart ", "ABANDONED CART"):
        o = order("o3", ["80.00"], status=status)
        s, j = post("/agents/abandoned-cart-coverage/detect", {"client_id": TENANT, "orders": [o]})
        assert s == 200 and len(j["findings"]) == 1, (status, j)


def test_p7_empty_or_unsafe_ids_are_refused():
    for bad in ("", " ", "a b", "a|b", "a\nb", "ab\n", "-x", "a=b"):
        o = order(bad, ["10.00"], status="abandoned_cart")
        s, j = post("/agents/abandoned-cart-coverage/detect", {"client_id": TENANT, "orders": [o]})
        assert s == 422 and any(d["loc"][-1] == "order_id" for d in j["detail"]), (bad, j)
    s, _ = post("/agents/abandoned-cart-coverage/detect", {"client_id": "", "orders": []})
    assert s == 422
    s, _ = post("/agents/abandoned-cart-coverage/detect", {"client_id": "a/b", "orders": []})
    assert s == 422  # a tenant id must be ledger-safe ([A-Za-z0-9._:-])


def test_p8_duplicate_rows_are_one_finding_and_never_a_same_agent_overlap():
    o = order("dup", ["50.00"], status="abandoned_cart")
    s, j = post("/agents/abandoned-cart-coverage/detect", {"client_id": TENANT, "orders": [o, o]})
    assert s == 200 and len(j["findings"]) == 1
    s, ov = post("/correlation/overlaps", {"findings": j["findings"] * 2})
    assert s == 200 and ov == {}


def test_two_different_rows_with_one_id_are_refused_not_merged():
    a = order("dup", ["50.00"], discounts=[{"code": "A", "percent_off": 10}, {"code": "B", "percent_off": 10}])
    b = order("dup", ["70.00"], discounts=[{"code": "A", "percent_off": 10}, {"code": "B", "percent_off": 10}])
    s, j = post("/agents/discount-misuse/detect", {"client_id": TENANT, "orders": [a, b]})
    assert s == 422 and j["detail"][0]["type"] == "duplicate_entity", j


# --- E-13: renewal as of the scan, timezone-aware ---------------------------------

def _sub(due, status="lapsed_no_renewal_attempt"):
    return {"subscription_id": "s1", "customer_id": "c", "plan_price_usd": "30.00", "renewal_interval_days": 30,
                "next_renewal_due_at": due, "status": status}


def test_p11_renewal_not_yet_due_as_of_the_scan_is_not_flagged():
    s, j = post("/agents/renewal-never-triggered/detect",
                {"client_id": TENANT, "as_of": AS_OF_WIRE, "subscriptions": [_sub("2099-01-01T00:00:00Z")]})
    assert s == 200 and j["findings"] == []
    s, j = post("/agents/renewal-never-triggered/detect",
                {"client_id": TENANT, "as_of": AS_OF_WIRE, "subscriptions": [_sub(AS_OF_WIRE)]})
    assert s == 200 and len(j["findings"]) == 1  # due exactly at as_of counts
    assert j["findings"][0]["period_label"] == "2026-10-01"


def test_p12_naive_datetimes_are_refused():
    s, _ = post("/agents/renewal-never-triggered/detect",
                {"client_id": TENANT, "as_of": AS_OF_WIRE, "subscriptions": [_sub("2026-06-30T23:30:00")]})
    assert s == 422
    s, _ = post("/agents/renewal-never-triggered/detect",
                {"client_id": TENANT, "as_of": "2026-10-01T00:00:00", "subscriptions": [_sub("2026-06-30T00:00:00Z")]})
    assert s == 422
    o = order("o1", ["10.00"], status="abandoned_cart")
    o["placed_at"] = "2026-06-01T00:00:00"
    s, _ = post("/agents/abandoned-cart-coverage/detect", {"client_id": TENANT, "orders": [o]})
    assert s == 422


def test_renewal_as_of_is_required():
    s, j = post("/agents/renewal-never-triggered/detect", {"client_id": TENANT, "subscriptions": []})
    assert s == 422 and any(d["loc"] == ["body", "as_of"] for d in j["detail"]), j


# --- E-14: negative click -> order gap ---------------------------------------------

def test_negative_click_to_order_gap_is_flagged_uncertain_with_no_figure():
    aff = {"affiliate_id": "aff", "click_timestamp": "2026-06-02T00:00:00Z", "order_timestamp": "2026-06-01T00:00:00Z",
               "attribution_window_hours": 24, "commission_rate_percent": 10}
    s, j = post("/agents/affiliate-coupon-extension/detect",
                {"client_id": TENANT, "orders": [order("neg", ["100.00"], affiliate=aff)]})
    assert s == 200
    (f,) = j["findings"]
    assert f["cause_certainty"] == "uncertain"
    assert f["recoverable_value"] is None and f["evidence_class"] == "UNKNOWN"
    assert f["methodology_id"] == "aff_negative_gap"


# --- the ledger summary budget (orchestrator-go writes one per finding) -----------

def test_finding_field_bounds_match_the_ledger_summary_budget():
    # orchestrator-go internal/orchestrator/ledger_record.go encodes these into a 280-character
    # ledger summary; its test proves the worst case (275) fits. A change here must change both.
    assert (L.AGENT_ID_MAX_CHARS, L.ID_MAX_CHARS, L.PERIOD_LABEL_MAX_CHARS, L.METHODOLOGY_ID_MAX_CHARS) == (32, 64, 16, 24)
    assert L.FINDING_ID_PATTERN == r"^rrf1-[0-9a-f]{40}$"


@pytest.mark.parametrize("field,value", [("agent_id", "a" * 33), ("methodology_id", "m" * 25),
                                         ("period_label", "p" * 17), ("entity_id", "e" * 65)])
def test_finding_fields_over_the_summary_budget_are_refused(field, value):
    from _fx import make_finding
    with pytest.raises(ValueError):
        make_finding(**{field: value})


def test_fixture_tenant_is_the_one_tier2_rows_carry():
    assert {t.client_id for t in load_contract_terms()} == {TENANT}
    assert {p.client_id for p in load_platform_connections()} == {TENANT}
    assert Decimal("900.00") == contract_pricing_term_drift.detect(load_contract_terms(), client_id=TENANT)[0].recoverable_value.amount_usd
