"""Unit tests, one block per ZBM intelligence (1-8)."""

from datetime import date, timedelta

import pytest

from conftest import NOW
from samples import TODAY, zbm_clearance, zbm_requirements, zbm_work
from shared.departments import NotBuiltLegal37
from shared.errors import GuardrailViolation, PreconditionFailed
from shared.rights import ClearanceRecord
from zbm import audience_insight, brief_writer, creative_lead, creative_quality, hook_retention, placement_spec
from zbm import rights_provenance
from zbm.brief import Deliverable, RightsNeed
from zbm.brief_writer import ClientRequirements
from zbm.creative_memory import ZbmCreativeMemory
from zbm.results import PerformanceResult


def _req(**over):
    return ClientRequirements.model_validate(zbm_requirements(**over))


def _draft(registry, **over):
    return brief_writer.draft(_req(**over), "brief-t", registry, TODAY)


# --- 1 Brief Writer ---------------------------------------------------------------------

def test_writer_drafts_complete_brief_for_the_maker(registry):
    b = _draft(registry)
    assert b.status.value == "draft" and b.issues == [] and b.open_questions == []
    assert b.spec_row_ids == {"d1": ["yt-shorts-max-length"]}
    assert b.maker_summary.startswith("MAKE: 1x 30s 9:16 mp4 for youtube/shorts")
    assert b.fields.insight == "Home baristas quit oat milk because it won't foam."


def test_writer_never_invents_missing_fields(registry):
    b = _draft(registry, hook=None, audience="", insight_candidates=[])
    assert b.status.value == "incomplete" and b.fields is None
    assert any(q.startswith("hook:") for q in b.open_questions)
    assert any(q.startswith("audience:") for q in b.open_questions)
    assert any(q.startswith("insight:") for q in b.open_questions)


def test_writer_lists_rule_issues(registry):
    b = _draft(registry, key_message="It froths and it pours.",
               deliverables=[{"deliverable_id": "d1", "platform": "youtube", "placement": "shorts",
                              "length_seconds": 240, "aspect_ratio": "9:16", "format": "mp4", "count": 1}])
    assert any("K3" in i for i in b.issues)
    assert any("exceeds yt-shorts-max-length" in i for i in b.issues)


def test_writer_flags_unsourced_platform(registry):
    b = _draft(registry, deliverables=[{"deliverable_id": "d1", "platform": "tiktok", "placement": "feed",
                                        "length_seconds": 30, "aspect_ratio": "9:16", "format": "mp4", "count": 1}])
    assert any("no Platform Rules Registry spec rows for tiktok/feed" in i for i in b.issues)


def test_advisory_row_is_a_warning_not_an_issue(registry):
    b = _draft(registry, deliverables=[{"deliverable_id": "d1", "platform": "instagram", "placement": "reels",
                                        "length_seconds": 200, "aspect_ratio": "9:16", "format": "mp4", "count": 1}])
    assert b.issues == [] and any("[advisory]" in w for w in b.warnings)


# --- 2 Creative Lead -------------------------------------------------------------------

def test_lead_approves_clean_brief(registry, actors):
    assert creative_lead.review(_draft(registry), "zbm_creative_lead", actors, registry, TODAY).outcome == "approved"


def test_lead_rechecks_rather_than_trusting_writer(registry, actors):
    b = _draft(registry).model_copy(update={"issues": []})
    later = date(2026, 10, 23)  # row expired since drafting
    rev = creative_lead.review(b, "zbm_creative_lead", actors, registry, later)
    assert rev.outcome == "sent_back" and any("expired" in i for i in rev.issues)


def test_lead_sends_back_incomplete(registry, actors):
    rev = creative_lead.review(_draft(registry, hook=None), "zbm_creative_lead", actors, registry, TODAY)
    assert rev.outcome == "sent_back" and any("hook" in i for i in rev.issues)


def test_lead_refuses_self_approval_and_wrong_role(registry, actors):
    from shared.actors import Role

    actors.add("sam", [Role.ZBM_BRIEF_WRITER, Role.ZBM_CREATIVE_LEAD])
    b = brief_writer.draft(_req(), "brief-s", registry, TODAY, drafted_by="sam")
    with pytest.raises(GuardrailViolation, match="self-approval"):
        creative_lead.review(b, "sam", actors, registry, TODAY)
    with pytest.raises(GuardrailViolation, match="does not hold role"):
        creative_lead.review(b, "zbm_placement_spec", actors, registry, TODAY)
    with pytest.raises(GuardrailViolation, match="unknown actor"):
        creative_lead.review(b, "nobody", actors, registry, TODAY)


def test_lead_cannot_review_approved(registry, actors):
    b = _draft(registry).model_copy(update={"status": brief_writer.BriefStatus.APPROVED})
    with pytest.raises(PreconditionFailed):
        creative_lead.review(b, "zbm_creative_lead", actors, registry, TODAY)


# --- 3 Audience Insight ------------------------------------------------------------------

def _cand(stmt, *sources, kind="interview"):
    return audience_insight.InsightCandidate(statement=stmt, evidence=[
        {"source_id": s, "kind": kind, "ref": "r"} for s in sources])


def test_insight_needs_two_independent_sources():
    sel = audience_insight.select_insight([_cand("A", "s1", "s1")])
    assert sel.insight is None and "only 1 distinct source" in sel.rejected[0][1]


def test_insight_picks_best_supported_and_is_order_independent():
    cands = [_cand("B", "s1", "s2"), _cand("A", "s1", "s2", "s3"), _cand("C", "s1", "s2", "s3", kind="measured")]
    assert audience_insight.select_insight(cands).insight == "C"
    assert audience_insight.select_insight(list(reversed(cands))).insight == "C"
    tie = [_cand("Zed", "a", "b"), _cand("Alpha", "c", "d")]
    assert audience_insight.select_insight(tie).insight == audience_insight.select_insight(tie[::-1]).insight == "Alpha"


# --- 4 Placement Spec / Export Validator ----------------------------------------------------

D = Deliverable(deliverable_id="d1", platform="youtube", placement="shorts", length_seconds=30,
                aspect_ratio="9:16", format="mp4", count=1)


def _decl(**over):
    base = dict(platform="youtube", placement="shorts", length_seconds=30.0, aspect_ratio="9:16", format="mp4",
                codec="h264", file_ref="f1")
    base.update(over)
    return placement_spec.DeclaredExport(**base)


def test_export_passes_with_reasons_and_states_coverage_gaps(registry):
    v = placement_spec.validate_export(_decl(), D, registry, TODAY)
    assert v.verdict == "pass"
    assert any(c.source == "registry:yt-shorts-max-length" for c in v.checks)
    assert {g.split(":")[0] for g in v.registry_coverage_gaps} == {"aspect_ratio", "format", "codec", "safe_zones"}


@pytest.mark.parametrize("over,prop", [({"aspect_ratio": "16:9"}, "aspect_ratio"), ({"format": "mov"}, "format"),
                                       ({"length_seconds": 31.0}, "length_seconds"), ({"placement": "feed"}, "placement")])
def test_export_fails_with_the_property_named(registry, over, prop):
    v = placement_spec.validate_export(_decl(**over), D, registry, TODAY)
    assert v.verdict == "fail" and any(c.property == prop and c.result == "fail" for c in v.checks)


def test_export_over_registry_hard_limit_fails(registry):
    d = D.model_copy(update={"length_seconds": 200})
    v = placement_spec.validate_export(_decl(length_seconds=200.0), d, registry, TODAY)
    assert v.verdict == "fail"
    assert any(c.source == "registry:yt-shorts-max-length" and c.result == "fail" for c in v.checks)


def test_export_blocks_on_expired_row_never_guesses(registry):
    v = placement_spec.validate_export(_decl(), D, registry, date(2026, 10, 23))
    assert v.verdict == "fail"
    assert any(c.result == "blocked" and "expired on 2026-10-23" in c.reason for c in v.checks)


def test_export_with_no_registry_rows_is_blocked(registry):
    d = D.model_copy(update={"platform": "tiktok", "placement": "feed"})
    v = placement_spec.validate_export(_decl(platform="tiktok", placement="feed"), d, registry, TODAY)
    assert v.verdict == "fail" and any(c.property == "registry" and c.result == "blocked" for c in v.checks)


def test_spec_row_write_owner_enforced(registry, recorder, actors, ledger):
    zbc_row = registry.get("ig-repost-watermark-derecommendation")
    with pytest.raises(GuardrailViolation):
        placement_spec.write_spec_row(registry, recorder, actors, "zbm_placement_spec", zbc_row)
    with pytest.raises(GuardrailViolation):
        placement_spec.write_spec_row(registry, recorder, actors, "zbc_platform_rules",
                                      registry.get("yt-shorts-max-length"))
    assert ledger.events == []


# --- 5 Rights and Provenance ------------------------------------------------------------

NEEDS = [RightsNeed(asset_id="footage_kitchen_01", asset_kind="footage", use="paid_advertising")]


def _rights_with_record(rights, **over):
    rights.commit_record(ClearanceRecord.model_validate(zbm_clearance(**over)))
    return rights


def test_rights_fail_closed_without_record(rights):
    r = rights_provenance.check("w1", ["footage_kitchen_01"], NEEDS, False, rights, NotBuiltLegal37(), TODAY)
    assert not r.cleared and "no clearance record" in r.blockers[0]


def test_rights_cleared_with_record_and_stamp_not_applied(rights):
    r = rights_provenance.check("w1", ["footage_kitchen_01"], NEEDS, False, _rights_with_record(rights),
                                NotBuiltLegal37(), TODAY)
    assert r.cleared and r.provenance_stamp.startswith("not_applied: c2pa-rs")


@pytest.mark.parametrize("over", [{"permitted_uses": ["organic_social"]}, {"valid_until": "2026-09-01"}])
def test_rights_wrong_use_or_expired_not_cleared(rights, over):
    r = rights_provenance.check("w1", ["footage_kitchen_01"], NEEDS, False, _rights_with_record(rights, **over),
                                NotBuiltLegal37(), TODAY)
    assert not r.cleared


def test_rights_undeclared_and_unlisted_assets_fail(rights):
    _rights_with_record(rights)
    assert not rights_provenance.check("w1", [], NEEDS, False, rights, NotBuiltLegal37(), TODAY).cleared
    r = rights_provenance.check("w1", ["footage_kitchen_01", "stock_song"], NEEDS, False, rights, NotBuiltLegal37(), TODAY)
    assert not r.cleared and any("stock_song" in b for b in r.blockers)


def test_generative_fill_off_by_default_legal_stand_in_blocks(rights):
    _rights_with_record(rights, permitted_uses=["paid_advertising", "ai_generative_fill"])
    r = rights_provenance.check("w1", ["footage_kitchen_01"], NEEDS, True, rights, NotBuiltLegal37(), TODAY)
    assert not r.cleared and any("Legal (37) is not built yet" in b for b in r.blockers)


# --- 6 Hook and Retention / 7 Creative Memory ------------------------------------------------

def _res(i, hook, rate, prov="measured", **over):
    d = dict(result_id=f"r{i}", client_id="client_acme", creative_id=f"c{i}", vertical="food", platform="youtube",
             placement="shorts", hook_type=hook, provenance=prov, measurement_source="YouTube Analytics export",
             measurement_ref=f"export-{i}", impressions=10000, metrics={"hook_hold_rate_3s": rate, "trial_signups": rate * 1000})
    d.update(over)
    return PerformanceResult(**d)


def test_hook_advice_only_from_measured_with_min_samples():
    results = [_res(1, "question", 0.6), _res(2, "question", 0.7), _res(3, "question", 0.65),
               _res(4, "demo", 0.9), _res(5, "demo", 0.95),  # only 2 samples
               _res(6, "demo", 0.99, prov="self_reported"), _res(7, "demo", 0.99, measurement_ref=None)]
    rep = hook_retention.advise(results, "youtube", "shorts")
    assert [a.hook_type for a in rep.ranked] == ["question"] and rep.ranked[0].median == 0.65
    assert any("self_reported" in e for e in rep.excluded)
    assert any("without a measurement source" in e for e in rep.excluded)


def test_hook_advice_empty_says_so():
    rep = hook_retention.advise([], "youtube", "shorts")
    assert rep.ranked == [] and "never invented" in rep.note


def test_memory_learns_only_measured_winners_against_numeric_target():
    from zbm.brief import SuccessMetric

    target = SuccessMetric(metric="trial_signups", comparator=">=", target=500, unit="signups", measured_by="crm")
    m = ZbmCreativeMemory()
    assert m.evaluate_winner(_res(1, "q", 0.6), target).learned is True
    assert m.evaluate_winner(_res(2, "q", 0.4), target).learned is False
    assert m.evaluate_winner(_res(3, "q", 0.9, prov="self_reported"), target).learned is False
    assert m.evaluate_winner(_res(4, "q", 0.9, prov="estimated"), target).learned is False


def test_memory_over_api_rejects_self_reported(make_api):
    from flows import ok, zbm_approved_brief

    api = make_api()
    brief = zbm_approved_brief(api)
    good = _res(1, "q", 0.6).model_dump(mode="json")
    assert ok(api.post("/zbm/memory/results", {"brief_id": brief["brief_id"], "result": good}))["learned"] is True
    bad = _res(2, "q", 0.9, prov="self_reported").model_dump(mode="json")
    assert ok(api.post("/zbm/memory/results", {"brief_id": brief["brief_id"], "result": bad}))["learned"] is False
    assert len(api.zbm.memory.winners_for("client_acme")) == 1


# --- 8 Creative Quality --------------------------------------------------------------------

def _fields(registry):
    return _draft(registry).fields


def _decl_q(**over):
    q = dict(zbm_work()["quality"])
    q.update(over)
    return creative_quality.QualityDeclaration(**q)


def test_quality_passes_clean_work(registry):
    assert creative_quality.judge(_fields(registry), _decl_q(), [], 1).outcome == "pass"


@pytest.mark.parametrize("over,code", [({"hook_ends_at_seconds": 3.0}, "Q1"), ({"opening_text": " "}, "Q1"),
                                       ({"script_text": "Something else."}, "Q2"), ({"supers": []}, "Q3"),
                                       ({"disclosure_text": ""}, "Q4")])
def test_quality_findings(registry, over, code):
    d = creative_quality.judge(_fields(registry), _decl_q(**over), [], 1)
    assert d.outcome == "send_back" and any(f.startswith(code) for f in d.findings)


def test_quality_premium_bar_can_send_back_anything_and_caps_at_two(registry):
    f = _fields(registry)
    assert creative_quality.judge(f, _decl_q(), ["Not premium."], 1).outcome == "send_back"
    d2 = creative_quality.judge(f, _decl_q(), ["Still not."], 2)
    assert d2.outcome == "escalate_to_andre" and d2.reported_to == "andre"
    with pytest.raises(PreconditionFailed):
        creative_quality.judge(f, _decl_q(), [], 3)
