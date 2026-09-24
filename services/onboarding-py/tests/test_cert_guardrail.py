"""
CERTIFICATION TYPE 3 — GUARDRAIL TESTS (locked spec: "Guardrail tests run
on every change"). Each test pins one locked rule that no client, document
or conversation can override.

Independent review (certification type 4) happens outside this workstream.
"""

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from config import ConfigError, OnboardingConfig, load_config
from conftest import ANDRE_KEY, GOOD_GRANT, Clock, client_for, finding, make_service, start_body
from guardrails import OutboundBlocked, check_outbound
from integrations.departments import NotBuiltComplianceDepartment
from integrations.vault import RefusingVault, VaultRefused
from ledger import FakeLedgerClient, UnconfiguredLedgerClient
from memory import approval_token
from onboarding_schema import BriefingPack


def _ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text)
    return r.json()


FRESH_CLIENT_UNMET = [
    "contract_14/contract_terms_available: no contract terms on file (contract storage location undecided; stand-in holds nothing)",
    "compliance_15/p1_wording_counsel_approved: P1 disclosure wording not yet approved by counsel (Cal. B&P Code 17941)",
    "compliance_15/p23_clause_counsel_approved: P23 CCPA/CPRA clause not yet drafted/approved by counsel",
    "compliance_15/audit_baseline_complete: Audit and Baseline (6) has not produced a baseline",
    "compliance_15/merged_plan_agreed: no merged plan, or the client has not chosen on every disagreement",
    "compliance_15/access_verified_p21: no platform access verified as actually working (P21)",
    "compliance_15/permission_receipt_sent_p3: a permission receipt (P3) has not been sent for every verified grant",
    "compliance_15/no_unacknowledged_hard_escalation: a hard escalation is open and not yet resolved by Andre",
    "compliance_15/billing_setup_p16: billing setup (P16) not confirmed by the Billing department",
    "compliance_15/compliance_department_38_ruling: Compliance department (38) is not built — not allowed yet",
]


# --- activation gates (14 + 15) --------------------------------------------------------


def test_activation_with_honest_stand_ins_is_blocked_with_the_exact_unmet_list():
    svc = make_service()
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    r = c.post("/onboarding/clients/client_a/activate")
    assert r.status_code == 409
    body = r.json()
    assert body["activated"] is False
    assert body["unmet"] == FRESH_CLIENT_UNMET
    assert body["handoff"] is None
    types = svc.ledger.types()
    assert types.count("contract_gate_ruling") == 1 and types.count("compliance_gate_ruling") == 1
    assert "activation_handoff_request" not in types  # nothing crosses until both gates pass


def _ready_client(svc, c):
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    _ok(c.post("/onboarding/clients/client_a/access/grants", json=GOOD_GRANT))
    _ok(c.post("/onboarding/clients/client_a/audit", json={"account_data": {"orders": [{"order_id": "o1"}]}}))
    _ok(c.post("/onboarding/clients/client_a/plan", json={"client_priorities": ["abandoned_carts"]}))
    esc = svc.clients["client_a"].escalation_ids[0]
    _ok(c.post(f"/onboarding/clients/client_a/escalations/{esc}/resolve", json={"resolution": "ok", "snag_category": "deal_review"}))


def test_gate_14_fails_alone_blocks_activation():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    _ready_client(svc, c)
    terms = svc.depts.contracts.get("client_a")
    svc.depts.contracts.put(terms.model_copy(update={"ccpa_cpra_clause_present": False}))
    r = c.post("/onboarding/clients/client_a/activate")
    assert r.status_code == 409
    assert r.json()["unmet"] == ["contract_14/p23_ccpa_cpra_clause_in_contract: CCPA/CPRA customer-data clause missing from the contract"]
    assert r.json()["compliance"]["passed"] is True
    assert svc.depts.handoff.received == []


def test_gate_15_fails_alone_blocks_activation():
    svc = make_service(all_fakes=True)
    svc.depts.compliance = NotBuiltComplianceDepartment()
    c = client_for(svc)
    _ready_client(svc, c)
    r = c.post("/onboarding/clients/client_a/activate")
    assert r.status_code == 409
    assert r.json()["contract"]["passed"] is True
    assert r.json()["unmet"] == ["compliance_15/compliance_department_38_ruling: Compliance department (38) is not built — not allowed yet"]
    assert svc.depts.handoff.received == []


def test_plan_drift_outside_contract_blocks_via_gate_14():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    _ready_client(svc, c)
    _ok(c.post("/onboarding/clients/client_a/plan", json={"client_priorities": ["abandoned_carts", "ad_spend_waste"]}))
    r = c.post("/onboarding/clients/client_a/activate")
    assert r.status_code == 409
    assert r.json()["contract"]["drift"] == ["plan includes 'digital_advertising', which is not in the signed contract"]


def test_compliance_stand_in_says_not_allowed_yet():
    ruling = NotBuiltComplianceDepartment().rule("x", "client", {})
    assert ruling.allowed is False and ruling.detail == "not allowed yet"


def test_vault_stand_in_refuses_and_never_echoes():
    with pytest.raises(VaultRefused) as ei:
        RefusingVault().store("client_a", "shopify", "sup3r-secret-value")
    assert "sup3r-secret-value" not in str(ei.value)
    assert RefusingVault.certified is False


# --- ledger fail-closed ---------------------------------------------------------------


def test_ledger_failure_stops_the_action_and_the_api_says_so():
    svc = make_service(ledger=FakeLedgerClient(fail=True))
    c = client_for(svc)
    r = c.post("/onboarding/clients", json=start_body())
    assert r.status_code == 503
    assert r.json()["proceeded"] is False
    assert "client_a" not in svc.clients  # nothing happened


def test_unconfigured_ledger_refuses_everything_that_needs_a_record():
    svc = make_service(ledger=UnconfiguredLedgerClient())
    c = client_for(svc)
    r = c.post("/onboarding/clients", json=start_body())
    assert r.status_code == 503 and "not configured" in r.json()["detail"]
    r = c.post("/zbc/creators/applications", json={"creator_id": "c1", "legal_name": "A B", "applied_on": "2026-09-24",
                                                   "follower_count": 1, "avg_engagement_rate": 0.1})
    assert r.status_code == 503 and svc.creators == {}


def test_ledger_failure_mid_flow_leaves_state_unchanged():
    led = FakeLedgerClient()
    svc = make_service(all_fakes=True, ledger=led)
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    led.fail = True
    r = c.post("/onboarding/clients/client_a/audit", json={"account_data": {"orders": [{"order_id": "o1"}]}})
    assert r.status_code == 503 and r.json()["proceeded"] is False
    assert svc.clients["client_a"].baseline is None
    assert svc.rr.calls == []  # the crossing itself did not happen: the request record came first
    r = c.post("/onboarding/clients/client_a/access/grants", json=GOOD_GRANT)
    assert r.status_code == 503 and svc.clients["client_a"].verifications == {}


def test_every_ledger_body_matches_the_contract_shape():
    import re

    svc = make_service(all_fakes=True)
    c = client_for(svc)
    _ready_client(svc, c)
    c.post("/onboarding/clients/client_a/activate")
    assert svc.ledger.events
    for e in svc.ledger.events:
        assert set(e) == {"event_id", "department", "event_type", "actor", "subject_id", "payload_sha256", "summary"}
        assert re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", e["event_id"]) and e["event_id"].startswith("onb-")
        assert e["department"] == "onboarding"
        assert re.fullmatch(r"[a-z0-9_]{1,64}", e["event_type"]) and re.fullmatch(r"[a-z0-9_]{1,64}", e["actor"])
        assert re.fullmatch(r"[0-9a-f]{64}", e["payload_sha256"])
        assert 1 <= len(e["summary"]) <= 280 and not re.search(r"[\x00-\x1f\x7f]", e["summary"])


# --- outbound text: guarantees, labeled dollars, honest identity --------------------------


@pytest.mark.parametrize("text", [
    "We guarantee results.", "This is risk-free.", "You will make more money.", "We are 100% sure.",
    "You'll double your sales.", "Recover $4,000 this month.", "That's 5,000 dollars back.", "Worth USD 300.",
])
def test_outbound_filter_blocks_guarantees_and_unlabeled_dollars(text):
    with pytest.raises(OutboundBlocked):
        check_outbound(text)


@pytest.mark.parametrize("text", [
    "We can't guarantee results.", "No guarantees, just evidence.", "Recovered $240.00 (observed, high confidence).",
])
def test_outbound_filter_allows_honest_text(text):
    assert check_outbound(text) == text


def _client_facing_strings(obj, key=None):
    keys = {"first_message", "reply", "client_message", "recap", "text", "summary_text", "applicant_message",
            "recommend_question", "client_commitment_text"}
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _client_facing_strings(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _client_facing_strings(v, key)
    elif isinstance(obj, str) and key in keys:
        yield obj


def test_every_client_facing_string_in_a_full_flow_passes_the_outbound_filter():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    outs = [_ok(c.post("/onboarding/clients", json=start_body()), 201)]
    outs.append(_ok(c.post("/onboarding/clients/client_a/messages", json={"text": "hi, I want to talk to a real person"})))
    outs.append(_ok(c.post("/onboarding/clients/client_a/access/grants", json=GOOD_GRANT)))
    outs.append(_ok(c.post("/onboarding/clients/client_a/audit", json={"account_data": {"orders": [{"order_id": "o1"}]}})))
    outs.append(_ok(c.post("/onboarding/clients/client_a/plan", json={"client_priorities": ["renewals"]})))
    outs.append(_ok(c.post("/onboarding/clients/client_a/recap")))
    outs.append(_ok(c.post("/onboarding/clients/client_a/first-win", json={"finding_id": "f1"})))
    outs.append(_ok(c.post("/onboarding/clients/client_a/recommend-score", json={"score": 3})))
    strings = [s for o in outs for s in _client_facing_strings(o)]
    assert len(strings) >= 8
    for s in strings:
        assert check_outbound(s) == s


def test_first_message_discloses_ai_and_offers_a_human_in_every_lane():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    for body in (start_body(), start_body("brand_1", lane="zbc_brand")):
        m = _ok(c.post("/onboarding/clients", json=body), 201)["first_message"]
        assert "I'm an AI, not a person" in m and "talk to a human" in m and "Andre" in m
    a = _ok(c.post("/zbc/creators/applications", json={"creator_id": "c1", "legal_name": "Al B", "date_of_birth": "1990-01-01",
                                                       "applied_on": "2026-09-24", "follower_count": 1, "avg_engagement_rate": 0.1}), 201)
    assert "I'm an AI, not a person" in a["first_message"] and "Andre" in a["first_message"]


# --- escalation triggers -------------------------------------------------------------


def test_hard_trigger_client_asks_for_human_escalates_immediately_with_no_attempt():
    svc = make_service(all_fakes=True, config=replace(OnboardingConfig(), deal_size_threshold_usd=Decimal("100000.00")))
    c = client_for(svc)
    assert _ok(c.post("/onboarding/clients", json=start_body()), 201)["escalation"] is None
    r = _ok(c.post("/onboarding/clients/client_a/messages", json={"text": "Can I speak to a real person please?"}))
    assert r["intent"] == "human_request"
    esc = r["escalation"]
    assert esc["hard"] is True and esc["attempted_resolution"] is None
    assert "hard trigger" in esc["briefing"]["what_the_agent_already_tried"]
    assert r["reply"].endswith("Andre will get back to you today.")


def test_human_request_with_push_not_wired_makes_no_false_promise():
    svc = make_service()  # NotWiredPushNotifier
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    r = _ok(c.post("/onboarding/clients/client_a/messages", json={"text": "I want to talk to a human"}))
    assert r["escalation"]["push_delivered"] is False
    assert r["escalation"]["client_message_status"].startswith("held: briefing not delivered")
    assert "today" not in r["reply"] and "tomorrow" not in r["reply"] and "shortly" not in r["reply"]
    assert svc.clients["client_a"].commitments == {}


@pytest.mark.parametrize("threshold,deal,escalates,why", [
    (None, "10.00", True, "not set"),
    ("5000.00", "4999.99", False, "at or below"),
    ("5000.00", "5000.00", False, "at or below"),
    ("5000.00", "5000.01", True, "above"),
    ("5000.00", None, True, "unknown"),
])
def test_deal_size_hard_trigger_fails_closed_while_threshold_unset(threshold, deal, escalates, why):
    cfg = replace(OnboardingConfig(), deal_size_threshold_usd=None if threshold is None else Decimal(threshold))
    svc = make_service(all_fakes=True, config=cfg)
    c = client_for(svc)
    r = _ok(c.post("/onboarding/clients", json=start_body(deal_size_usd=deal)), 201)
    assert r["deal_size_ruling"]["escalate"] is escalates
    assert why in r["deal_size_ruling"]["reason"]
    assert (r["escalation"] is not None) is escalates


def test_soft_stuck_trigger_one_attempt_then_escalate_with_configurable_window():
    clock = Clock()
    cfg = replace(OnboardingConfig(), deal_size_threshold_usd=Decimal("100000.00"))
    svc = make_service(all_fakes=True, config=cfg, clock=clock)
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    clock.advance(hours=47)
    assert _ok(c.post("/onboarding/clients/client_a/tick"))["stuck"] is None
    clock.advance(hours=2)  # 49h > default 48h window
    t = _ok(c.post("/onboarding/clients/client_a/tick"))
    assert t["stuck"]["escalation"] is None and t["stuck"]["soft_issue"]["status"] == "attempted"
    clock.advance(hours=1)
    assert _ok(c.post("/onboarding/clients/client_a/tick"))["stuck"]["escalation"] is None  # still waiting on the one attempt
    clock.advance(hours=24)
    t = _ok(c.post("/onboarding/clients/client_a/tick"))
    assert t["stuck"]["escalation"]["trigger"] == "stuck" and t["stuck"]["escalation"]["hard"] is False
    assert t["stuck"]["escalation"]["attempted_resolution"].startswith("Quick check-in")

    cfg2 = replace(cfg, stuck_window_hours=12)
    clock2 = Clock()
    svc2 = make_service(all_fakes=True, config=cfg2, clock=clock2)
    c2 = client_for(svc2)
    _ok(c2.post("/onboarding/clients", json=start_body()), 201)
    clock2.advance(hours=13)
    assert _ok(c2.post("/onboarding/clients/client_a/tick"))["stuck"] is not None
    assert load_config({"ONBOARDING_STUCK_WINDOW_HOURS": "12"}).stuck_window_hours == 12
    assert OnboardingConfig().stuck_window_hours == 48


def test_briefing_pack_has_exactly_the_locked_contents():
    assert list(BriefingPack.model_fields) == [
        "who_the_client_is", "what_the_account_pull_found", "what_the_client_said_they_care_about",
        "exactly_where_the_snag_is", "what_the_agent_already_tried", "sixty_second_summary", "the_one_decision",
        "recommended_action",
    ]
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    _ok(c.post("/onboarding/clients/client_a/audit", json={"account_data": {"orders": [{"order_id": "o1"}]}}))
    _ok(c.post("/onboarding/clients/client_a/plan", json={"client_priorities": ["abandoned_carts"]}))
    r = _ok(c.post("/onboarding/clients/client_a/messages", json={"text": "let me talk to Andre"}))
    b = r["escalation"]["briefing"]
    assert "Acme Widgets" in b["who_the_client_is"]
    assert "3 finding(s)" in b["what_the_account_pull_found"]
    assert b["what_the_client_said_they_care_about"] == "abandoned carts"
    assert len(b["sixty_second_summary"].split()) <= 151
    pushed = svc.depts.notifier.sent[-1][1]
    assert set(pushed) == set(BriefingPack.model_fields)  # pushed BEFORE Andre engages


# --- client memory walls, institutional memory, playbook ---------------------------------


def test_client_a_data_never_appears_in_client_b_outputs():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    a = start_body("client_a", business_name="Zanzibar Marmalade Works",
                   signer={"name": "Quentin Aardvark", "email": "quentin@zanzibar.example"})
    b = start_body("client_b")
    _ok(c.post("/onboarding/clients", json=a), 201)
    _ok(c.post("/onboarding/clients", json=b), 201)
    _ok(c.post("/onboarding/clients/client_a/intake/facts", json={"facts": [
        {"field": "primary_goal", "value": "stop the Xylophone coupon leak", "provenance": "client_stated", "evidence": "chat", "observed_at": "2026-09-24T17:00:00Z"}]}))
    _ok(c.post("/onboarding/clients/client_a/messages", json={"text": "Our secret project is Operation Kumquat"}))
    _ok(c.post("/onboarding/clients/client_a/access/grants", json=dict(GOOD_GRANT, account_id="zanzibar-store")))
    outs = []
    for m, p, body in [
        ("post", "/onboarding/clients/client_b/intake/facts", {"facts": []}),
        ("post", "/onboarding/clients/client_b/messages", {"text": "hello"}),
        ("post", "/onboarding/clients/client_b/recap", None),
        ("post", "/onboarding/clients/client_b/access/grants", GOOD_GRANT),
        ("post", "/onboarding/clients/client_b/audit", {"account_data": {"orders": [{"order_id": "o"}]}}),
        ("post", "/onboarding/clients/client_b/plan", {"client_priorities": ["renewals"]}),
        ("get", "/onboarding/clients/client_b", None),
        ("get", "/onboarding/clients/client_b/health", None),
        ("post", "/onboarding/clients/client_b/activate", None),
        ("post", "/onboarding/clients/client_b/exit", {"memory_choice": "export_then_destroy"}),
    ]:
        kw = {"json": body} if body is not None else {}
        outs.append(getattr(c, m)(p, **kw).text)
    blob = "\n".join(outs) + json.dumps(svc.depts.handoff.received)
    for marker in ["Zanzibar", "Quentin", "Xylophone", "Kumquat", "zanzibar-store", "client_a"]:
        assert marker not in blob, marker


def test_institutional_memory_strips_identifiers():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body(business_name="Zanzibar Marmalade Works")), 201)
    esc = svc.clients["client_a"].escalation_ids[0]
    _ok(c.post(f"/onboarding/clients/client_a/escalations/{esc}/resolve", json={
        "resolution": "Called Dana at dana@acme.example / +1 (415) 555-0100 about Zanzibar Marmalade Works, site zanzibar.com, order 1234567",
        "snag_category": "deal_review"}))
    pats = _ok(c.get("/learning/proposals"))["institutional_patterns"]
    blob = json.dumps(pats)
    for ident in ["Dana", "dana@acme.example", "555-0100", "Zanzibar", "zanzibar.com", "1234567", "client_a"]:
        assert ident not in blob, ident
    assert pats[0]["snag_category"] == "deal_review"


def test_playbook_changes_only_with_andre_token_and_is_versioned_forever():
    svc = make_service()
    c = client_for(svc)
    body = {"rule_id": "stuck_nudge", "version": 1, "text": "Nudge at 24h.", "approval_token": "not-a-real-token"}
    r = c.post("/playbook/rules", json=body)
    assert r.status_code == 403
    tok = approval_token(ANDRE_KEY, "stuck_nudge", 1, "Nudge at 24h.")
    assert c.post("/playbook/rules", json=dict(body, text="Nudge at 1h.", approval_token=tok)).status_code == 403  # tampered text
    _ok(c.post("/playbook/rules", json=dict(body, approval_token=tok)))
    assert c.post("/playbook/rules", json=dict(body, approval_token=tok)).status_code == 403  # replay: wrong version now
    tok2 = approval_token(ANDRE_KEY, "stuck_nudge", 2, "Nudge at 36h.")
    _ok(c.post("/playbook/rules", json={"rule_id": "stuck_nudge", "version": 2, "text": "Nudge at 36h.", "approval_token": tok2}))
    hist = _ok(c.get("/playbook"))["history"]
    assert [(h["version"], h["text"]) for h in hist] == [(1, "Nudge at 24h."), (2, "Nudge at 36h.")]
    assert svc.ledger.types().count("playbook_change") == 2 and "playbook_change_refused" in svc.ledger.types()

    no_key = make_service()
    no_key.playbook.approval_key = None
    assert client_for(no_key).post("/playbook/rules", json=dict(body, approval_token=tok)).status_code == 403


def test_learning_loop_proposals_never_change_the_playbook():
    svc = make_service(all_fakes=True)
    for i in range(3):
        svc.institutional.record({"lane": "client", "trigger": "stuck", "snag_category": "access_link_ignored"})
    props = svc.propose_rules()["proposals"]
    assert props and props[0]["status"].startswith("proposed")
    assert svc.playbook.history == []


# --- ZBC creator rules ---------------------------------------------------------------------


def _app(**over):
    base = {"creator_id": "clip_1", "legal_name": "Pat Young", "date_of_birth": "2008-09-25", "applied_on": "2026-09-24",
            "follower_count": 20000, "avg_engagement_rate": 0.05, "fake_follower_ratio": 0.02, "content_history_posts": 120,
            "network_fit_tags": ["beauty"], "w9_received": True, "creator_agreement_signed": True, "disclosure_training_completed": True}
    base.update(over)
    return base


def test_clipper_under_18_declined_with_no_guardian_path():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    r = _ok(c.post("/zbc/creators/applications", json=_app()), 201)  # turns 18 tomorrow
    assert r["vetting"]["outcome"] == "decline"
    assert "no guardian or parental-consent path" in r["vetting"]["reasons"][0]
    assert "18 or older" in r["applicant_message"] and "guardian" not in r["applicant_message"].lower()
    assert r["activation"] is None
    for smuggle in ({"guardian_consent": True}, {"parental_consent": True}, {"guardian_name": "Mom"}):
        assert c.post("/zbc/creators/applications", json=_app(creator_id="clip_g", **smuggle)).status_code == 422


def test_clipper_turning_18_on_application_day_is_eligible():
    svc = make_service(all_fakes=True)
    r = _ok(client_for(svc).post("/zbc/creators/applications", json=_app(date_of_birth="2008-09-24")), 201)
    assert r["vetting"]["outcome"] == "approve"


def test_clipper_missing_dob_is_incomplete_not_guessed():
    svc = make_service(all_fakes=True)
    r = _ok(client_for(svc).post("/zbc/creators/applications", json=_app(date_of_birth=None)), 201)
    assert r["vetting"]["outcome"] == "incomplete"


def test_w9_required_before_payout_activation():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    r = _ok(c.post("/zbc/creators/applications", json=_app(date_of_birth="2000-01-01", w9_received=False)), 201)
    assert r["activation"]["activated"] is False
    assert "compliance_15/w9_on_file_p8: W-9 not on file (P8): required before payout activation" in r["activation"]["unmet"]
    assert svc.depts.payouts.activated == []
    assert c.post("/zbc/creators/clip_1/payments", json={"amount_usd": "50.00", "paid_on": "2026-09-24"}).status_code == 409
    _ok(c.post("/zbc/creators/clip_1/w9", json={"received": True}))
    act = _ok(c.post("/zbc/creators/clip_1/activate"))
    assert act["activated"] is True and svc.depts.payouts.activated == ["clip_1"]


def test_creator_activation_blocked_by_honest_stand_ins():
    svc = make_service()
    c = client_for(svc)
    r = _ok(c.post("/zbc/creators/applications", json=_app(date_of_birth="2000-01-01")), 201)
    assert r["vetting"]["outcome"] == "approve"
    assert r["activation"]["unmet"] == [
        "compliance_15/p1_wording_counsel_approved: P1 disclosure wording not yet approved by counsel (Cal. B&P Code 17941)",
        "compliance_15/age_verified_18_plus: age 18+ not verified by Verification and Integrity (not built — not allowed yet)",
        "compliance_15/compliance_department_38_ruling: Compliance department (38) is not built — not allowed yet",
    ]


def test_1099_threshold_is_configuration():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    _ok(c.post("/zbc/creators/applications", json=_app(date_of_birth="2000-01-01")), 201)
    r1 = _ok(c.post("/zbc/creators/clip_1/payments", json={"amount_usd": "1999.99", "paid_on": "2026-03-01"}))
    assert r1["threshold_usd"] == "2000.00" and r1["form_1099_required"] is False
    r2 = _ok(c.post("/zbc/creators/clip_1/payments", json={"amount_usd": "0.01", "paid_on": "2026-04-01"}))
    assert r2["paid_to_date_usd"] == "2000.00" and r2["form_1099_required"] is True
    r3 = _ok(c.post("/zbc/creators/clip_1/payments", json={"amount_usd": "5.00", "paid_on": "2027-01-02"}))
    assert r3["threshold_usd"] is None and r3["form_1099_required"] is True and "accountant" in r3["detail"]
    cfg = load_config({"ONBOARDING_1099_THRESHOLDS": '{"2026": "2000.00", "2027": "2100.00"}'})
    assert cfg.threshold_1099(2027) == Decimal("2100.00")


def test_p9_pre_post_disclosure_check():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    _ok(c.post("/zbc/creators/applications", json=_app(date_of_birth="2000-01-01")), 201)
    assert _ok(c.post("/zbc/creators/clip_1/posts/check", json={"caption": "#ad Loving this serum"}))["allowed"] is True
    no = _ok(c.post("/zbc/creators/clip_1/posts/check", json={"caption": "Loving this serum"}))
    assert no["allowed"] is False and "#ad" in no["detail"]
    buried = _ok(c.post("/zbc/creators/clip_1/posts/check", json={"caption": "x" * 120 + " #ad"}))
    assert buried["allowed"] is False
    guar = _ok(c.post("/zbc/creators/clip_1/posts/check", json={"caption": "#ad guaranteed to clear your skin"}))
    assert guar["allowed"] is False


# --- Spanish off, facts expire, momentum, recommend ------------------------------------------


def test_spanish_is_off_at_launch_and_cannot_be_turned_on():
    with pytest.raises(ConfigError):
        OnboardingConfig(spanish_enabled=True)
    with pytest.raises(ConfigError):
        load_config({"ONBOARDING_SPANISH_ENABLED": "true"})
    svc = make_service()
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    r = _ok(c.post("/onboarding/clients/client_a/messages", json={"text": "¿Hablas español?"}))
    assert r["intent"] == "spanish_request" and "Spanish isn't available yet" in r["reply"]


def test_unverified_platform_facts_are_never_quoted_to_the_client():
    svc = make_service()  # shipped PLATFORM_KNOWLEDGE: never verified
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    s = _ok(c.post("/onboarding/clients/client_a/access/website-scan", json={"html": "cdn.shopify.com"}))
    assert s["access_requests"][0]["quotable"] is False
    assert s["access_requests"][0]["client_message_status"].startswith("held: platform facts have never been verified")


def test_momentum_never_picks_an_unproven_win_and_first_win_must_be_provable():
    svc = make_service(all_fakes=True, findings=[
        finding("fa", "abandoned_cart_coverage", "900.00", "attributed", "very_high"),
        finding("fb", "renewal_never_triggered", "900.00", "observed", "medium"),
        finding("fc", "discount_misuse", "900.00", "observed", "low", certainty="uncertain"),
    ])
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    _ok(c.post("/onboarding/clients/client_a/audit", json={"account_data": {"orders": [{"order_id": "o1"}]}}))
    m = _ok(c.post("/onboarding/clients/client_a/momentum"))
    assert m["finding_id"] is None and set(m["rejected"]) == {"fa", "fb", "fc"}
    r = c.post("/onboarding/clients/client_a/first-win", json={"finding_id": "fa"})
    assert r.status_code == 409 and "unproven" in r.json()["detail"]


def test_recommend_score_only_after_first_real_win_and_routes_correctly():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    assert c.post("/onboarding/clients/client_a/recommend-score", json={"score": 10}).status_code == 409
    _ok(c.post("/onboarding/clients/client_a/audit", json={"account_data": {"orders": [{"order_id": "o1"}]}}))
    assert c.post("/onboarding/clients/client_a/recommend-score", json={"score": 10}).status_code == 409
    _ok(c.post("/onboarding/clients/client_a/first-win", json={"finding_id": "f1"}))
    r = _ok(c.post("/onboarding/clients/client_a/recommend-score", json={"score": 6}))
    assert r["path"] == "soft_escalation" and r["escalation"] is None and r["soft_issue"]["trigger"] == "low_recommend_score"
    out = _ok(c.post(f"/onboarding/clients/client_a/issues/{r['soft_issue']['issue_id']}/outcome", json={"resolved": False}))
    assert out["escalation"]["trigger"] == "low_recommend_score"


# --- P3 receipt, P4 exit, P22 routing ---------------------------------------------------------


def test_p22_access_link_goes_to_login_holder_or_signer_with_forward_note():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    r = _ok(c.post("/onboarding/clients", json=start_body("client_s", login_holder=None)), 201)
    assert r["access_link"]["send_to"] == "dana@acme.example" and r["access_link"]["send_to_is_signer"] is True
    s = _ok(c.post("/onboarding/clients/client_s/access/website-scan", json={"html": "cdn.shopify.com"}))
    assert s["access_requests"][0]["steps"][0].startswith("If someone else on your team holds these logins")


def test_p3_receipt_says_what_zbm_can_and_cannot_see_and_how_to_revoke():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    r = _ok(c.post("/onboarding/clients/client_a/access/grants", json=GOOD_GRANT))["permission_receipt"]
    assert r["can_see"] and r["cannot_see"] and r["how_to_revoke"]
    assert "What ZBM can see" in r["text"] and "What ZBM cannot see" in r["text"] and "How to revoke" in r["text"]


def test_p4_clean_exit_plan_revokes_destroys_exports_then_locks():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    _ok(c.post("/onboarding/clients/client_a/access/grants", json=GOOD_GRANT))
    r = _ok(c.post("/onboarding/clients/client_a/exit", json={"memory_choice": "export_then_destroy"}))
    actions = [s["action"] for s in r["exit_plan"]["steps"]]
    assert actions == ["revoke_access", "destroy_vault_secrets", "export_reports", "client_memory", "confirm_to_client"]
    assert r["memory_export"]["memory"]["business_name"] == "Acme Widgets"
    assert svc.memory.view("client_a") == {}
    assert c.post("/onboarding/clients/client_a/messages", json={"text": "hi"}).status_code == 409


def test_client_can_see_and_delete_their_memory():
    svc = make_service()
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    assert _ok(c.get("/onboarding/clients/client_a"))["memory"]["business_name"] == "Acme Widgets"
    assert _ok(c.delete("/onboarding/clients/client_a/memory"))["deleted"] is True
    assert _ok(c.get("/onboarding/clients/client_a"))["memory"] == {}


# --- LabeledValue rule on consumed findings ---------------------------------------------------


def test_unlabeled_or_malformed_findings_are_rejected_not_shown():
    bad = finding("fx", "discount_misuse", "12.00")
    bad["recoverable_value"].pop("confidence")
    nan = finding("fn", "discount_misuse", "NaN")
    neg = finding("fneg", "discount_misuse", "-5.00")
    svc = make_service(all_fakes=True, findings=[bad, nan, neg, finding("fok", "discount_misuse", 49.99)])
    c = client_for(svc)
    _ok(c.post("/onboarding/clients", json=start_body()), 201)
    a = _ok(c.post("/onboarding/clients/client_a/audit", json={"account_data": {"orders": [{"order_id": "o1"}]}}))
    assert sorted(a["rejected_findings"]) == ["fn", "fneg", "fx"]
    assert a["findings"][0]["recoverable_value"]["amount_usd"] == "49.99"  # legacy float -> exact via str()
