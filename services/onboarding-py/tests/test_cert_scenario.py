"""
CERTIFICATION TYPE 1 — SCENARIO TESTS (locked spec, "Certification").

Includes the three scenarios the spec names:
  - personal ad account granted instead of business
  - clipper with bought followers
  - client's stated numbers disagree with account data
plus end-to-end lane flows over the HTTP API (TestClient).

Certification type 4 (independent review) happens OUTSIDE this
workstream; passing these tests does not certify any intelligence for real
clients.
"""

from conftest import GOOD_GRANT, Clock, andre_resolve_body, client_for, make_service, start_body

SHOPIFY_META_HTML = """
<html><head>
<script src="https://cdn.shopify.com/s/files/theme.js"></script>
<script>!function(f,b,e,v){}(window,document,'script','https://connect.facebook.net/en_US/fbevents.js');
fbq('init', '123456789012345');</script>
</head><body>Acme Widgets</body></html>
"""


def _ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text)
    return r.json()


def test_full_client_lane_flow_activates_only_when_everything_is_met():
    svc = make_service(all_fakes=True)
    c = client_for(svc)

    started = _ok(c.post("/onboarding/clients", json=start_body()), 201)
    assert "I'm an AI" in started["first_message"] and "Andre" in started["first_message"]
    assert started["access_link"]["send_to"] == "lee@acme.example"  # P22: login holder, not signer
    # Threshold unset => fail closed: the deal escalates and says why.
    assert started["deal_size_ruling"]["escalate"] is True
    assert "not set" in started["deal_size_ruling"]["reason"]
    deal_esc = started["escalation"]
    assert deal_esc["hard"] is True and deal_esc["trigger"] == "deal_size"

    facts = _ok(c.post("/onboarding/clients/client_a/intake/facts", json={"vertical": "ecommerce", "facts": [
        {"field": "monthly_revenue_usd", "value": "10000.00", "provenance": "client_stated", "evidence": "intake chat", "observed_at": "2026-09-24T17:00:00Z"},
        {"field": "primary_goal", "value": "recover abandoned carts", "provenance": "client_stated", "evidence": "intake chat", "observed_at": "2026-09-24T17:00:00Z"},
    ]}))
    assert facts["profile"]["fields"]["primary_goal"]["confidence"] == "medium"
    assert facts["next_question"] is not None

    scan = _ok(c.post("/onboarding/clients/client_a/access/website-scan", json={"html": SHOPIFY_META_HTML}))
    assert {d["platform"] for d in scan["detected"]} == {"shopify", "meta"}
    assert all(p["send_to"] == "lee@acme.example" for p in scan["access_requests"])

    g = _ok(c.post("/onboarding/clients/client_a/access/grants", json=GOOD_GRANT))
    assert g["verification"]["usable"] is True
    assert g["permission_receipt_status"] == "sent"
    assert "How to revoke" in g["permission_receipt"]["text"]

    audit = _ok(c.post("/onboarding/clients/client_a/audit", json={
        "account_data": {"orders": [{"order_id": "o1"}]}, "observed_monthly_revenue_usd": "9800.00"}))
    assert audit["risk"]["kind"] == "nothing"
    assert audit["baseline"]["totals_by_classification"] == {"observed": "240.00", "attributed": "80.50"}

    plan = _ok(c.post("/onboarding/clients/client_a/plan", json={"client_priorities": ["abandoned_carts"]}))
    assert plan["items"][0]["topic"] == "abandoned_carts"

    # The hard deal-size escalation is still open: activation is blocked, and says exactly why.
    blocked = c.post("/onboarding/clients/client_a/activate")
    assert blocked.status_code == 409
    assert blocked.json()["unmet"] == [
        "compliance_15/no_unacknowledged_hard_escalation: a hard escalation is open and not yet resolved by Andre"
    ]

    # Resolving is Andre's decision: the shared service token alone is refused
    # (fix wave 1, F4); his approval token for exactly this action is required.
    shared_only = c.post(f"/onboarding/clients/client_a/escalations/{deal_esc['escalation_id']}/resolve",
                         json={"resolution": "Andre approved the deal as scoped", "snag_category": "deal_review"})
    assert shared_only.status_code == 403
    _ok(c.post(f"/onboarding/clients/client_a/escalations/{deal_esc['escalation_id']}/resolve",
               json=andre_resolve_body("client_a", deal_esc["escalation_id"], "Andre approved the deal as scoped", "deal_review")))
    act = _ok(c.post("/onboarding/clients/client_a/activate"))
    assert act["activated"] is True and act["unmet"] == []
    assert act["handoff"]["accepted"] is True
    assert svc.depts.handoff.received[0]["verified_access"] == ["shopify"]

    mom = _ok(c.post("/onboarding/clients/client_a/momentum"))
    assert mom["finding_id"] == "f1"
    win = _ok(c.post("/onboarding/clients/client_a/first-win", json={"finding_id": "f1"}))
    assert win["recommend_question"] and "0 to 10" in win["recommend_question"]
    rs = _ok(c.post("/onboarding/clients/client_a/recommend-score", json={"score": 10}))
    assert rs["path"] == "referral_path"

    types = svc.ledger.types()
    for t in ["onboarding_started", "first_message_sent", "deal_size_ruling", "escalation_raised", "andre_push_request",
              "client_commitment_made", "revenue_recovery_request", "revenue_recovery_ruling", "risk_ruling",
              "merged_plan", "contract_lookup_request", "compliance_dept_request", "contract_gate_ruling",
              "compliance_gate_ruling", "activation_handoff_request", "activation_handoff_ruling", "activation_ruling",
              "escalation_resolved", "first_win", "recommend_score"]:
        assert t in types, t
    assert all(e["department"] == "onboarding" for e in svc.ledger.events)


def test_scenario_personal_ad_account_granted_instead_of_business():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    grant = {"platform": "meta", "account_id": "dana-personal", "account_type": "personal", "granted_role": "analyst",
             "account_last_activity_at": "2026-09-22T00:00:00Z"}
    r = _ok(c.post("/onboarding/clients/client_a/access/grants", json=grant))
    v = r["verification"]
    assert v["usable"] is False
    assert [p["code"] for p in v["problems"]] == ["personal_account"]
    assert "BUSINESS" in v["problems"][0]["fix_instruction"]
    assert "received" not in r["client_message"].lower()  # never "access received" on a bad grant
    assert "need one fix" in r["client_message"]
    assert r["permission_receipt"] is None


def test_scenario_stale_account_and_insufficient_role_get_exact_fixes():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    grant = {"platform": "google_ads", "account_id": "old-ads", "account_type": "business", "granted_role": "read_only",
             "account_last_activity_at": "2025-01-01T00:00:00Z", "job": "setup"}
    v = _ok(c.post("/onboarding/clients/client_a/access/grants", json=grant))["verification"]
    codes = [p["code"] for p in v["problems"]]
    assert codes == ["stale_account", "insufficient_role"]
    assert "change ZBM's access level from 'read_only' to 'standard'" in v["problems"][1]["fix_instruction"]
    assert "2025-01-01" in v["problems"][0]["fix_instruction"]


def test_scenario_clipper_with_bought_followers_is_declined():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    app = {"creator_id": "clip_1", "legal_name": "Sam Clipper", "date_of_birth": "1998-05-01",
           "follower_count": 250000, "avg_engagement_rate": 0.002, "follower_growth_30d_ratio": 3.0,
           "fake_follower_ratio": 0.45, "content_history_posts": 300, "network_fit_tags": ["gaming"],
           "w9_received": True, "creator_agreement_signed": True, "disclosure_training_completed": True}
    r = _ok(c.post("/zbc/creators/applications", json=app), 201)
    assert r["vetting"]["outcome"] == "decline"
    assert any("bought" in x for x in r["vetting"]["reasons"])
    assert r["activation"] is None
    assert svc.depts.payouts.activated == []


def test_scenario_stated_numbers_disagree_one_attempt_then_escalate():
    clock = Clock()
    svc = make_service(all_fakes=True, clock=clock)
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    _ok(c.post("/onboarding/clients/client_a/intake/facts", json={"facts": [
        {"field": "monthly_revenue_usd", "value": "50000.00", "provenance": "client_stated", "evidence": "intake", "observed_at": "2026-09-24T17:00:00Z"}]}))
    a = _ok(c.post("/onboarding/clients/client_a/audit", json={"account_data": {"orders": [{"order_id": "o1"}]},
                                                               "observed_monthly_revenue_usd": "12000.00"}))
    assert a["risk"]["kind"] == "soft_trigger"
    assert "numbers don't match" in a["risk"]["reasons"][0]
    assert a["client_facing_numbers"] == "paused"
    assert a["escalation"] is None  # soft: ONE resolution attempt first
    issue = a["soft_issue"]
    assert "help us understand" in a["client_message"]
    # The attempt did not resolve it -> escalate, and the briefing says what was tried.
    e = _ok(c.post(f"/onboarding/clients/client_a/issues/{issue['issue_id']}/outcome", json={"resolved": False}))
    esc = e["escalation"]
    assert esc["trigger"] == "audit_anomaly" and esc["hard"] is False
    assert "help us understand" in esc["briefing"]["what_the_agent_already_tried"]
    assert "numbers don't match" in esc["briefing"]["exactly_where_the_snag_is"]


def test_zbc_creator_lane_instant_activation_on_approval():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    app = {"creator_id": "clip_ok", "legal_name": "Ria Good", "date_of_birth": "2000-01-01",
           "follower_count": 20000, "avg_engagement_rate": 0.05, "fake_follower_ratio": 0.02, "content_history_posts": 120,
           "network_fit_tags": ["beauty"], "w9_received": True, "creator_agreement_signed": True,
           "disclosure_training_completed": True}
    r = _ok(c.post("/zbc/creators/applications", json=app), 201)
    assert r["vetting"]["outcome"] == "approve"
    assert r["activation"]["activated"] is True
    assert r["activation"]["materials"]["tracking_link_ids"]
    assert svc.depts.payouts.activated == ["clip_ok"]
    assert "payout_activation_request" in svc.ledger.types()


def test_zbc_brand_lane_rented_first_per_campaign_approval_and_prove_before_scale():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    body = start_body("brand_1", lane="zbc_brand")
    body["contract"]["services"] = ["zbc_brand_campaign"]
    _ok(c.post("/onboarding/clients", json=body), 201)
    p = _ok(c.post("/zbc/brands/brand_1/campaigns", json={"campaign_id": "camp_1", "regulated": True,
                                                            "wants_owned_addon": True, "requested_budget_usd": "5000.00"}), 201)
    assert p["plan"]["distribution"] == "rented"
    assert p["plan"]["owned_addon_offered"] is True and p["plan"]["owned_addon_selected"] is True
    assert p["plan"]["proving_campaign"]["budget_usd"] == "500.00"
    # Brand account not yet through the gates -> the campaign cannot be approved.
    r = c.post("/zbc/brands/brand_1/campaigns/camp_1/approve", json={"brand_yes_campaign_id": "camp_1", "plan_digest": p["plan_digest"]})
    assert r.status_code == 409
    assert any(u.startswith("brand_account_activated") for u in r.json()["unmet"])
    # Scaling is refused before approval / before proof.
    s = _ok(c.post("/zbc/brands/brand_1/campaigns/camp_1/proving-result", json={"views_delivered": 1000, "clicks": 10, "evidence": "observed"}))
    assert s["may_scale"] is False
