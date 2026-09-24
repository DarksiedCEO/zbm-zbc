"""One-module-per-intelligence unit tests (pure decision functions)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from conftest import finding
from intelligences import (
    i01_client_understanding as i01,
    i02_conversation as i02,
    i03_priority_fusion as i03,
    i06_audit_baseline as i06,
    i07_momentum_moment as i07,
    i08_risk_anomaly as i08,
    i10_escalation_briefing as i10,
    i11_creator_vetting as i11,
    i12_brand_campaign as i12,
    i13_learning_loop as i13,
    i14_contract_obligation as i14,
    i15_compliance as i15,
    registry,
)
from onboarding_schema import ClipperApplication, ContractTerms, Lane, Provenance, TriggerKind

T = datetime(2026, 9, 24, 17, 0, tzinfo=timezone.utc)


def test_registry_has_15_with_phase_and_not_certified_status():
    reg = registry()
    assert [r["number"] for r in reg] == list(range(1, 16))
    phases = {r["number"]: r["phase"] for r in reg}
    assert all(phases[n] == 1 for n in (1, 2, 3, 4, 6, 8, 9, 10, 14, 15))
    assert phases[5] == 2 and phases[7] == 2 and phases[11] == 3 and phases[12] == 3
    assert "1 (logging)" in str(phases[13])
    for r in reg:
        assert "NOT certified" in r["status"]


# --- 1 Client Understanding ---------------------------------------------------


def test_i01_confidence_from_provenance_conflict_and_minimum_data():
    facts = [
        i01.Fact("business_name", "Acme", Provenance.CONTRACT, "contract", T),
        i01.Fact("time_zone", "America/New_York", Provenance.CLIENT_STATED, "chat", T),
        i01.Fact("website", "acme.example", Provenance.INFERRED, "email domain", T),
        i01.Fact("platforms", ["shopify"], Provenance.ACCOUNT_PULL, "pull", T),
        i01.Fact("platforms", ["woocommerce"], Provenance.WEBSITE, "tag", T),
        i01.Fact("favorite_color", "blue", Provenance.CLIENT_STATED, "chat", T),
    ]
    p = i01.build_profile("c", Lane.CLIENT, facts, short_list_size=20)
    assert p.fields["business_name"].confidence.value == "very_high"
    assert p.fields["time_zone"].confidence.value == "medium"
    assert p.fields["website"].confidence.value == "low"
    assert p.fields["platforms"].conflict is True and p.fields["platforms"].confidence.value == "low"
    assert p.fields["platforms"].value == ["shopify"]  # higher provenance kept as the prefill, not "truth"
    assert p.dropped_fields == ["favorite_color"]
    assert p.gaps[0].field == "platforms" and p.gaps[0].reason == "conflict" and p.gaps[0].prefill == ["shopify"]
    assert {g.field for g in p.gaps if g.reason == "missing"} >= {"login_holder", "primary_goal"}


def test_i01_client_confirmation_newer_than_conflict_resolves_it():
    facts = [i01.Fact("vertical", "retail", Provenance.WEBSITE, "site", T),
             i01.Fact("vertical", "ecommerce", Provenance.CLIENT_CONFIRMED, "chat", T + timedelta(minutes=5))]
    p = i01.build_profile("c", Lane.CLIENT, facts)
    assert p.fields["vertical"].conflict is False and p.fields["vertical"].value == "ecommerce"


def test_i01_short_list_is_capped():
    assert len(i01.build_profile("c", Lane.CLIENT, [], short_list_size=5).gaps) == 5


# --- 2 Conversation -----------------------------------------------------------------


def test_i02_vertical_wording_and_max_asks():
    p = i01.build_profile("c", Lane.CLIENT, [i01.Fact("business_name", "A", Provenance.CONTRACT, "c", T),
                                              i01.Fact("login_holder", "Lee", Provenance.CLIENT_CONFIRMED, "c", T),
                                              i01.Fact("time_zone", "UTC", Provenance.CLIENT_CONFIRMED, "c", T)])
    q = i02.next_question(p, "ecommerce", {})
    assert q.field == "primary_goal" and "lost sales" in q.text
    q2 = i02.next_question(p, "ecommerce", {"primary_goal": 3})
    assert q2.field == "vertical"


def test_i02_never_echoes_a_money_prefill():
    p = i01.build_profile("c", Lane.CLIENT, [
        i01.Fact("monthly_revenue_usd", "12000.00", Provenance.ACCOUNT_PULL, "pull", T),
        i01.Fact("monthly_revenue_usd", "50000.00", Provenance.CLIENT_STATED, "chat", T)])
    q = i02.next_question(p, None, {})
    assert q.field == "monthly_revenue_usd" and q.is_confirmation is False
    assert "12000" not in q.text and "50000" not in q.text


def test_i02_stuck_signal_window_and_friction():
    assert i02.stuck_signal(T, T + timedelta(hours=48), 48).stuck is False
    assert i02.stuck_signal(T, T + timedelta(hours=48, seconds=1), 48).stuck is True
    s = i02.stuck_signal(T, T + timedelta(hours=1), 48, {"login_holder": 3})
    assert s.stuck and "asked about login_holder 3 times" in s.reason


def test_i02_intents():
    assert i02.decide_reply("can I talk to a person?").intent == "human_request"
    assert i02.decide_reply("what's the password you used").intent == "credential_request"
    assert i02.decide_reply("will you guarantee more sales?").intent == "guarantee_request"
    assert i02.decide_reply("pause my campaigns please").intent == "account_change_request"
    assert i02.decide_reply("we sell shoes").intent == "answer"


def test_i02_recap_lists_what_was_said_next_steps_and_commitments():
    r = i02.recap("Dana", {"primary_goal": "fewer abandoned carts"}, ["Share access."], ["Andre will get back to you today."])
    assert "Primary goal: fewer abandoned carts" in r and "Share access." in r and "Andre will get back to you today." in r
    assert "talk with a person" in r


# --- 3 Priority Fusion ----------------------------------------------------------------


def _consumed(raw):
    return i06.consume(raw, {})[0]


def test_i03_client_order_is_kept_and_disagreement_is_shown_with_recommendation():
    fs = _consumed([finding("f1", "abandoned_cart_coverage", "300.00", "observed", "very_high"),
                    finding("f2", "discount_misuse", "40.00", "attributed", "low")])
    plan = i03.fuse("c", fs, ["ad_spend_waste", "discount_abuse"])
    assert [i.topic for i in plan.items] == ["ad_spend_waste", "discount_abuse", "abandoned_carts"]
    assert plan.items[2].source == "audit"
    # One disagreement on the strongest audit topic; the client's #1 having no
    # evidence is folded into it (and stated on the item itself) rather than
    # raised twice.
    assert [d.topic for d in plan.disagreements] == ["abandoned_carts"]
    assert plan.items[0].note.startswith("No audit evidence")
    assert all(d.needs_client_choice for d in plan.disagreements)
    assert plan.recommended_order[0] == "abandoned_carts"  # recommended, but NOT applied
    assert "$300.00 (observed, very_high confidence)" in plan.summary_text


def test_i03_never_sums_values_and_excludes_double_counts():
    fs = i06.consume([finding("a", "discount_misuse", "10.00", entity="o1"), finding("b", "discount_misuse", "20.00", entity="o2"),
                      finding("c", "discount_misuse", "999.00", entity="o3")], {"o3": []})[0]
    plan = i03.fuse("c", fs, ["discount_abuse"])
    assert plan.items[0].value.amount_usd == Decimal("20.00")
    assert plan.items[0].evidence_finding_ids == ["a", "b"]


# --- 6 Audit and Baseline ----------------------------------------------------------------


def test_i06_totals_per_classification_and_exclusions():
    fs, rej = i06.consume([
        finding("a", "discount_misuse", "10.00", "observed"), finding("b", "discount_misuse", "5.55", "observed"),
        finding("c", "abandoned_cart_coverage", "7.00", "attributed"),
        finding("d", "discount_misuse", "100.00", "observed", "low", certainty="uncertain"),
        finding("e", "discount_misuse", "50.00", entity="dup"),
        {"not": "a finding"},
    ], {"dup": []})
    b = i06.baseline(fs)
    assert rej == ["<no id>"]
    assert b.model_dump(mode="json")["totals_by_classification"] == {"observed": "15.55", "attributed": "7.00"}
    assert b.uncertain_findings == ["d"] and b.double_count_entities == ["dup"] and set(b.excluded_from_totals) == {"d", "e"}


# --- 7 Momentum ----------------------------------------------------------------------------


def test_i07_picks_highest_scoring_provable():
    fs = _consumed([finding("slow", "contract_pricing_term_drift", "900.00", "financially_verified", "very_high"),
                    finding("fast", "abandoned_cart_coverage", "50.00", "observed", "high")])
    assert i07.pick(fs).finding_id == "fast"


# --- 8 Risk and Anomaly ----------------------------------------------------------------------


def test_i08_rulings():
    assert i08.assess([], None, None).kind == "nothing"
    assert i08.assess(["account_suspended"], None, None).kind == "hard_stop"
    assert i08.assess(["chargeback_rate_high"], None, None).kind == "soft_trigger"
    u = i08.assess(["weird_new_signal"], None, None)
    assert u.kind == "soft_trigger" and "not ignored" in u.reasons[0]
    assert i08.assess([], Decimal("10000"), Decimal("7600")).kind == "nothing"  # 24% apart
    assert i08.assess([], Decimal("10000"), Decimal("7400")).kind == "soft_trigger"  # 26% apart


# --- 10 Escalation Briefing ------------------------------------------------------------------


def test_i10_briefing_decision_table_is_fixed_per_trigger_and_summary_capped():
    b = i10.build_briefing("Acme", None, ["x"], TriggerKind.STUCK, "word " * 400, "tried a reminder")
    assert b.the_one_decision == i10.DECISIONS[TriggerKind.STUCK][0]
    assert len(b.sixty_second_summary.split()) <= i10.SUMMARY_MAX_WORDS + 1
    assert set(i10.DECISIONS) == set(TriggerKind)


# --- 11 Creator Vetting -------------------------------------------------------------------------


TODAY = date(2026, 9, 24)


def _app(**kw):
    base = dict(creator_id="c1", legal_name="A B", date_of_birth=date(2000, 1, 1),
                follower_count=10000, avg_engagement_rate=0.05, fake_follower_ratio=0.02, content_history_posts=50,
                network_fit_tags=["x"])
    base.update(kw)
    return ClipperApplication(**base)


def test_i11_age_math_including_leap_day():
    assert i11.age_on(date(2008, 2, 29), date(2026, 2, 28)) == 17
    assert i11.age_on(date(2008, 2, 29), date(2026, 3, 1)) == 18
    assert i11.vet(_app(date_of_birth=date(2008, 2, 29)), date(2026, 2, 28)).outcome.value == "decline"
    assert i11.vet(_app(date_of_birth=date(2008, 2, 29)), date(2026, 3, 1)).outcome.value == "approve"


def test_i11_outcomes():
    assert i11.vet(_app(), TODAY).outcome.value == "approve"
    assert i11.vet(_app(fake_follower_ratio=None), TODAY).outcome.value == "send_to_andre"
    assert i11.vet(_app(fake_follower_ratio=0.2), TODAY).outcome.value == "send_to_andre"
    assert i11.vet(_app(engagement_pod_signal=0.8), TODAY).outcome.value == "decline"
    assert i11.vet(_app(brand_safety_flags=["hate_speech"]), TODAY).outcome.value == "decline"
    assert i11.vet(_app(brand_safety_flags=["profanity"]), TODAY).outcome.value == "send_to_andre"
    assert i11.vet(_app(follower_growth_30d_ratio=2.0, avg_engagement_rate=0.001), TODAY).outcome.value == "send_to_andre"
    # decline outranks everything, including a missing date of birth
    assert i11.vet(_app(date_of_birth=None, fake_follower_ratio=0.9), TODAY).outcome.value == "decline"


# --- 12 Brand Campaign ------------------------------------------------------------------------------


def test_i12_rented_first_owned_only_as_regulated_addon_and_per_campaign_approval():
    p = i12.plan_campaign("b", "c1", regulated=False, wants_owned_addon=True, requested_budget_usd=Decimal("100.00"),
                          budget_cap_usd=Decimal("500.00"))
    assert p.distribution.value == "rented" and p.owned_addon_offered is False and p.owned_addon_selected is False
    assert "Andre" in p.owned_addon_note and p.proving_campaign["budget_usd"] == "100.00"
    d = i12.plan_digest(p)
    assert i12.approve(p, "c1", d)[0] is True
    assert i12.approve(p, "c2", d)[0] is False
    changed = p.model_copy(update={"success_measures": ["x"]})
    assert i12.approve(changed, "c1", d)[0] is False
    assert i12.may_scale(p, {"evidence": "observed", "views_delivered": 10})[0] is False  # not approved
    ap = p.model_copy(update={"approved": True})
    assert i12.may_scale(ap, {"evidence": "estimated", "views_delivered": 10})[0] is False
    assert i12.may_scale(ap, {"evidence": "observed", "views_delivered": 10})[0] is True
    for m in p.success_measures:
        assert "guarantee" not in m.lower()


# --- 13 Learning Loop and Client Health -------------------------------------------------------------


def test_i13_health_bands_and_early_warning():
    h = i13.health(0, 0, 0, None, 1, 48, None, T)
    assert h.score == 100 and h.band == "green" and not h.early_warning
    h2 = i13.health(2, 2, 1, 5, 60, 48, None, T, previous_score=100)
    assert h2.band == "red" and h2.early_warning
    h3 = i13.health(1, 1, 0, None, 1, 48, T + timedelta(days=3), T, previous_score=100)
    assert h3.scorecard == {"time_to_first_win_days": 3.0, "stalls": 1, "escalations": 1}
    assert h3.early_warning is False  # 87: dropped 13 points, below the 15-point warning line


# --- 14 Contract and Obligation ------------------------------------------------------------------


def _terms(**kw):
    base = dict(client_id="c", signed=True, start_date=date(2026, 9, 1), services=["revenue_recovery"],
                monthly_spend_cap_usd="1000.00", ccpa_cpra_clause_present=True)
    base.update(kw)
    return ContractTerms(**base)


def test_i14_check_action():
    d = date(2026, 9, 24)
    assert i14.check_action(_terms(), d, service="revenue_recovery") == (True, [])
    assert i14.check_action(_terms(), d, service="out_of_home")[0] is False
    assert i14.check_action(_terms(), d, commitment_category="guaranteed_roi")[0] is False
    assert i14.check_action(_terms(), d, spend_usd=Decimal("1000.01"))[0] is False
    assert i14.check_action(_terms(end_date=date(2026, 9, 1)), d)[0] is False
    assert i14.check_action(None, d)[0] is False
    assert i14.check_action(_terms(monthly_spend_cap_usd=None), d, spend_usd=Decimal("1.00"))[0] is False


def test_i14_gate_unsigned_and_out_of_term():
    g = i14.client_gate(_terms(signed=False, start_date=date(2026, 10, 1)), date(2026, 9, 24), [], [])
    assert g.unmet == ["contract_signed: contract is not signed", "contract_in_term: today is outside the contract term"]


# --- 15 Compliance ------------------------------------------------------------------------------


def test_i15_missing_facts_count_as_unmet():
    g = i15.gate(Lane.CLIENT, {})
    assert not g.passed and len(g.unmet) == len(i15.CLIENT_REQUIREMENTS)
    all_true = {key: True for _, key, _ in i15.CLIENT_REQUIREMENTS}
    assert i15.gate(Lane.CLIENT, all_true).passed
    assert not i15.gate(Lane.CLIENT, dict(all_true, billing_ready="yes")).passed  # only literal True counts
