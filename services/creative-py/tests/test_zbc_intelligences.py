"""Unit tests, one block per ZBC intelligence (1, 1a, 2-8) plus the payout gate."""

from datetime import date, timedelta

import pytest

from conftest import NOW
from fakes import AcceptingCreativeAgents, PassingCompliance, PassingLegal, PassingVerification
from samples import TODAY, zbc_clip, zbc_goal, zbc_kit_request, zbc_license, zbc_music_clearance, zbc_source
from shared.departments import NotBuiltCompliance38, NotBuiltLegal37, NotBuiltVerificationIntegrity, NotWiredCreativeAgents
from shared.errors import GuardrailViolation, PreconditionFailed, ValidationFailed
from shared.registry import RegistryRow
from shared.rights import CampaignLicense, ClearanceRecord
from zbc import (
    campaign_kit,
    campaign_rulebook,
    clip_review,
    hook_angle,
    payout_eligibility,
    platform_rules,
    rights_clearance,
    rulebook_writer,
    source_mining,
)
from zbc.creative_memory import ClipResult, ZbcCreativeMemory
from zbc.rulebook import RuleKind, RulebookStatus
from zbc.rulebook_writer import CampaignGoal


def _goal(**over):
    return CampaignGoal.model_validate(zbc_goal(**over))


def _rb(registry, **over):
    return rulebook_writer.draft(_goal(**over), registry, TODAY)


def _live(registry, **over):
    return _rb(registry, **over).model_copy(update={"status": RulebookStatus.LIVE, "live_at": NOW})


# --- 1a Rulebook Writer -------------------------------------------------------------------

def test_writer_drafts_every_section_with_stable_ids(registry):
    rb = _rb(registry)
    ids = [r.rule_id for r in rb.rules]
    assert ids == ["OB-01", "MS-01", "NS-01", "DC-01", "PF-01", "SP-01", "SP-02", "OR-01", "OW-01", "QF-01",
                   "RC-01", "MD-01"]
    assert rb.blocking_issues == ()
    sp = {r.params["platform"]: r for r in rb.rules_of(RuleKind.SPEC_LENGTH)}
    assert sp["youtube"].params["max_seconds"] == 180 and sp["youtube"].rationale_row_ids == ("yt-shorts-max-length",)
    assert set(rb.one(RuleKind.ORIGINALITY_TRANSFORM).rationale_row_ids) == {
        "ig-repost-watermark-derecommendation", "yt-reused-content-monetization"}
    assert rb.one(RuleKind.MIN_DAYS_LIVE).params == {"days": 14}
    assert _rb(registry).model_dump() == rb.model_dump()  # deterministic


def test_writer_blocks_tiktok_it_cannot_source(registry):
    rb = _rb(registry, platforms=[{"platform": "tiktok", "placement": "feed"}])
    assert any("tiktok/feed: no usable length rule" in b for b in rb.blocking_issues)
    assert any("tiktok/feed: no usable originality rule" in b and "unverified" in b for b in rb.blocking_issues)


def test_writer_campaign_cap_can_only_tighten(registry):
    rb = _rb(registry, campaign_max_length_seconds=60)
    assert {r.params["max_seconds"] for r in rb.rules_of(RuleKind.SPEC_LENGTH)} == {60}


def test_revision_keeps_unchanged_ids_and_retires_changed(registry):
    v1 = _rb(registry)
    v2 = rulebook_writer.revise(v1, _goal(never_say=["get rich"], must_say=["Listen on Pod Plus"]), registry, TODAY, 2)
    ids = {r.rule_id for r in v2.rules}
    assert "MS-01" in ids and "NS-02" in ids and "NS-01" not in ids
    assert v2.retired_rule_ids == ("NS-01",) and v2.supersedes_version == 1
    v3 = rulebook_writer.revise(v2.model_copy(update={"status": RulebookStatus.LIVE}),
                                _goal(never_say=["guaranteed returns"]), registry, TODAY, 3)
    assert "NS-03" in {r.rule_id for r in v3.rules}  # NS-01's text again, but a retired id is never reused
    assert set(v3.retired_rule_ids) == {"NS-01", "NS-02"}


# --- 1 Campaign Rulebook --------------------------------------------------------------------

def test_campaign_rulebook_approves_clean_draft(registry, actors):
    assert campaign_rulebook.review(_rb(registry), "zbc_campaign_rulebook", actors, registry, TODAY).outcome == "approved"


def test_campaign_rulebook_refuses_drafter_and_wrong_role(registry, actors):
    from shared.actors import Role

    actors.add("riley", [Role.ZBC_RULEBOOK_WRITER, Role.ZBC_CAMPAIGN_RULEBOOK])
    rb = rulebook_writer.draft(_goal(), registry, TODAY, drafted_by="riley")
    with pytest.raises(GuardrailViolation, match="self-approval"):
        campaign_rulebook.review(rb, "riley", actors, registry, TODAY)
    with pytest.raises(GuardrailViolation):
        campaign_rulebook.review(rb, "zbm_creative_lead", actors, registry, TODAY)


@pytest.mark.parametrize("over,code", [
    ({"platforms": [{"platform": "tiktok", "placement": "feed"}]}, "R1"),
    ({"must_say": []}, "R2"),
    ({"must_say": ["guaranteed returns today"]}, "R4"),
    ({"angles": [{"name": "X", "description": "d", "keywords": ["budget"], "hook_lines": ["guaranteed returns now"]}]}, "R5"),
])
def test_campaign_rulebook_sends_back(registry, actors, over, code):
    rev = campaign_rulebook.review(_rb(registry, **over), "zbc_campaign_rulebook", actors, registry, TODAY)
    assert rev.outcome == "sent_back" and any(i.startswith(code) for i in rev.issues), rev.issues


def test_campaign_rulebook_rechecks_rows_at_approval(registry, actors):
    rev = campaign_rulebook.review(_rb(registry), "zbc_campaign_rulebook", actors, registry, date(2026, 10, 23))
    assert rev.outcome == "sent_back" and any(i.startswith("R3") for i in rev.issues)


def test_only_drafts_can_be_reviewed(registry, actors):
    with pytest.raises(PreconditionFailed):
        campaign_rulebook.review(_live(registry), "zbc_campaign_rulebook", actors, registry, TODAY)


# --- 2 Source Mining ------------------------------------------------------------------------

def test_moment_map_validates_ranges_overlaps_durations_and_angles(registry):
    mm = source_mining.build(source_mining.SourceMaterial.model_validate(zbc_source()), _live(registry), registry, TODAY)
    assert [m.segment_id for m in mm.moments] == ["s1", "s2", "s3", "s4"]
    rej = {r.segment_id: " ".join(r.reasons) for r in mm.rejected}
    assert "M4" in rej["s5"] and "M3 overlaps s2" in rej["s6"] and "M5" in rej["s7"] and "too long for" in rej["s7"]
    assert "M6" in rej["s8"] and "M1" in rej["s9"]
    s1 = mm.moments[0]
    assert s1.matched_angle_ids == ["A01"] and s1.fits == ["instagram/reels", "youtube/shorts"]


def test_moment_map_refuses_unlicensed_source(registry):
    src = {**zbc_source(), "source_asset_id": "downloaded_from_somewhere"}
    with pytest.raises(PreconditionFailed, match="rights holder"):
        source_mining.build(source_mining.SourceMaterial.model_validate(src), _live(registry), registry, TODAY)


def test_moment_map_blocks_platform_on_expired_row(registry):
    mm = source_mining.build(source_mining.SourceMaterial.model_validate(zbc_source()), _live(registry), registry,
                             date(2026, 10, 23))
    assert mm.moments == [] and all("blocked for" in " ".join(r.reasons) for r in mm.rejected
                                    if r.segment_id in ("s1", "s2", "s3", "s4"))


def test_duplicate_segment_ids_rejected(registry):
    src = zbc_source()
    src["segments"] = [src["segments"][0], {**src["segments"][2], "segment_id": "s1"}]
    mm = source_mining.build(source_mining.SourceMaterial.model_validate(src), _live(registry), registry, TODAY)
    assert any("M2" in " ".join(r.reasons) for r in mm.rejected)


# --- 3 Hook and Angle ----------------------------------------------------------------------

def _mm(registry, rb):
    return source_mining.build(source_mining.SourceMaterial.model_validate(zbc_source()), rb, registry, TODAY)


def test_hook_sheets_from_approved_angles_only(registry):
    rb = _live(registry, angles=[{"name": "Money myths", "description": "d", "keywords": ["budget", "myth"],
                                  "hook_lines": ["This budget myth costs you", "Guaranteed returns on this myth",
                                                 "One two three four five six seven"]},
                                 {"name": "Founder", "description": "d", "keywords": ["garage"]}])
    sheets = hook_angle.sheets(_mm(registry, rb), rb)
    s1 = next(s for s in sheets if s.moment_id == "m-s1")
    assert s1.angle_id == "A01" and "This budget myth costs you" in s1.hooks
    reasons = " ".join(r["reason"] for r in s1.rejected_hooks)
    assert "H4 breaks never-say NS-01" in reasons and "H3" in reasons
    assert all(len(h.split()) <= hook_angle.HOOK_MAX_WORDS for s in sheets for h in s.hooks)


def test_hook_sheet_refuses_unapproved_angle(registry):
    rb = _live(registry)
    m = _mm(registry, rb).moments[0]
    with pytest.raises(GuardrailViolation):
        hook_angle.sheet_for(m, "A99", rb)
    with pytest.raises(GuardrailViolation):
        hook_angle.sheet_for(m, "A02", rb)  # approved, but this moment didn't match it


# --- 4 Platform Rules ----------------------------------------------------------------------

def test_platform_rules_lookup_and_tiktok_blocked(registry):
    assert {r.row_id for r in platform_rules.originality_rows(registry, "youtube", "shorts", TODAY).usable} == {
        "yt-reused-content-monetization"}
    tk = platform_rules.originality_rows(registry, "tiktok", "feed", TODAY)
    assert tk.usable == [] and "unverified" in tk.blocked[0]


def test_platform_rules_writes_only_its_rows(registry, recorder, actors, ledger):
    with pytest.raises(GuardrailViolation):
        platform_rules.write_originality_row(registry, recorder, actors, "zbc_platform_rules",
                                             registry.get("yt-shorts-max-length"))
    with pytest.raises(GuardrailViolation):
        platform_rules.write_originality_row(registry, recorder, actors, "zbm_placement_spec",
                                             registry.get("tiktok-originality-unverified"))
    row = registry.get("yt-reused-content-monetization").model_copy(
        update={"verified_at": date(2026, 9, 24), "expires_at": date(2026, 10, 24)})
    platform_rules.write_originality_row(registry, recorder, actors, "zbc_platform_rules", row)
    assert registry.get("yt-reused-content-monetization").expires_at == date(2026, 10, 24)
    assert ledger.of_type("registry_row_written")


def test_verified_row_requires_https_source_and_dates():
    with pytest.raises(ValueError):
        RegistryRow(row_id="x", platform="tiktok", placement="all", rule_key="k", value="v", enforcement="policy",
                    owner="zbc_platform_rules", status="verified", source_url="http://x", verified_at=TODAY,
                    expires_at=TODAY + timedelta(days=10))


# --- 5 Rights and Clearance ---------------------------------------------------------------

def _rights(rights, lic=None, music=True):
    rights.commit_license(CampaignLicense.model_validate(lic or zbc_license()))
    if music:
        rights.commit_record(ClearanceRecord.model_validate(zbc_music_clearance()))
    return rights


A = [rights_clearance.DeclaredAsset(asset_id="src_ep42", kind="footage"),
     rights_clearance.DeclaredAsset(asset_id="music_brand_sting", kind="music")]


def test_rights_cleared_with_sublicensing_licence_and_music_record(rights):
    r = rights_clearance.check_campaign("camp_pod_01", A, _rights(rights), NotBuiltLegal37(), TODAY)
    assert r.cleared and r.license_id == "lic_pod_01"


def test_rights_fail_closed_without_licence(rights):
    r = rights_clearance.check_campaign("camp_pod_01", A, rights, NotBuiltLegal37(), TODAY)
    assert not r.cleared and r.blockers[0].startswith("C1")


def test_rights_requires_sublicense_to_clippers(rights):
    r = rights_clearance.check_campaign("camp_pod_01", A, _rights(rights, zbc_license(sublicense_to_clippers=False)),
                                        NotBuiltLegal37(), TODAY)
    assert not r.cleared and any(b.startswith("C2") for b in r.blockers)


def test_rights_flags_uncleared_music_likeness_footage(rights):
    extra = A + [rights_clearance.DeclaredAsset(asset_id="guest_face", kind="likeness"),
                 rights_clearance.DeclaredAsset(asset_id="other_show_clip", kind="footage")]
    r = rights_clearance.check_campaign("camp_pod_01", extra, _rights(rights, music=False), NotBuiltLegal37(), TODAY)
    flagged = {f["asset_id"]: f["reason"][:2] for f in r.flags}
    assert flagged == {"music_brand_sting": "C4", "guest_face": "C4", "other_show_clip": "C3"}
    assert not r.cleared


def test_rights_expired_licence_and_no_assets(rights):
    _rights(rights, zbc_license(valid_until="2026-09-20"))
    assert not rights_clearance.check_campaign("camp_pod_01", A, rights, NotBuiltLegal37(), TODAY).cleared
    assert not rights_clearance.check_campaign("camp_pod_01", [], rights, NotBuiltLegal37(), TODAY).cleared


def test_generative_fill_needs_licence_and_legal(rights):
    _rights(rights, zbc_license(ai_generative_fill_permitted=True))
    r = rights_clearance.check_campaign("camp_pod_01", A, rights, NotBuiltLegal37(), TODAY, uses_ai_generative_fill=True)
    assert not r.cleared and any("Legal (37)" in b for b in r.blockers)
    r2 = rights_clearance.check_campaign("camp_pod_01", A, rights, PassingLegal(), TODAY, uses_ai_generative_fill=True)
    assert r2.cleared


# --- 6 Campaign Kit -----------------------------------------------------------------------

def _kit_inputs(registry):
    rb = _live(registry)
    mm = _mm(registry, rb)
    return rb, mm, hook_angle.sheets(mm, rb)


def test_kit_builds_seeds_and_commissions_via_contract(registry):
    rb, mm, sh = _kit_inputs(registry)
    spec = campaign_kit.build("kit-1", rb, mm, sh, campaign_kit.KitRequest.model_validate(zbc_kit_request()))
    # build is pure: every commission is only REQUESTED until the workflow has recorded the kit
    assert all(c["status"] == "requested" and c["commissioned"] is None for c in spec.commissions)
    kit = campaign_kit.apply_receipts(spec, campaign_kit.commission_seeds(spec, NotWiredCreativeAgents()))
    assert [s.moment_id for s in kit.seeds] == ["m-s1", "m-s2", "m-s3"]  # all score 2 -> by start time
    s = kit.seeds[0]
    assert s.disclosure == "#ad" and s.must_say == ["Listen on Pod Plus"]
    assert s.required_transformation_elements == ["original_commentary", "captions_added"]
    assert {"DC-01", "OR-01", "MS-01"} <= set(s.cites_rule_ids)
    assert len(kit.commissions) == 6 and not kit.seed_clips_produced
    assert all("not wired" in c["reason"] for c in kit.commissions)
    assert any("NS-01" in d for d in kit.dont) and any("OW-01" in d for d in kit.dont)
    spec2 = campaign_kit.build("kit-2", rb, mm, sh, campaign_kit.KitRequest.model_validate(zbc_kit_request()))
    kit2 = campaign_kit.apply_receipts(spec2, campaign_kit.commission_seeds(spec2, AcceptingCreativeAgents()))
    assert kit2.seed_clips_produced


def test_kit_needs_enough_moments_and_bounds(registry):
    rb, mm, sh = _kit_inputs(registry)
    with pytest.raises(PreconditionFailed, match="K2"):
        campaign_kit.build("k", rb, mm, sh, campaign_kit.KitRequest.model_validate(zbc_kit_request(seed_count=5)))
    with pytest.raises(ValueError):
        campaign_kit.KitRequest.model_validate(zbc_kit_request(seed_count=6))
    with pytest.raises(ValueError):
        campaign_kit.KitRequest.model_validate(zbc_kit_request(seed_count=2))


def test_kit_refuses_uncleared_brand_asset_and_never_say_do_example(registry):
    rb, mm, sh = _kit_inputs(registry)
    with pytest.raises(ValidationFailed) as e:
        campaign_kit.build("k", rb, mm, sh, campaign_kit.KitRequest.model_validate(
            zbc_kit_request(brand_asset_ids=["random_song"], do_examples=["Promise guaranteed returns"])))
    assert any("K5" in i for i in e.value.issues) and any("K6" in i for i in e.value.issues)


def test_kit_only_from_live_rulebook(registry):
    rb, mm, sh = _kit_inputs(registry)
    with pytest.raises(PreconditionFailed, match="K1"):
        campaign_kit.build("k", rb.model_copy(update={"status": RulebookStatus.SIGNED}), mm, sh,
                           campaign_kit.KitRequest.model_validate(zbc_kit_request()))


# --- 7 Creative Memory ----------------------------------------------------------------------

def _result(**over):
    d = dict(result_id="r1", campaign_id="camp_pod_01", submission_id="clip_001", vertical="podcasts",
             platform="youtube", angle_id="A01", hook="h", source="platform_export", reported_views=999999)
    d.update(over)
    return ClipResult(**d)


def test_memory_learns_nothing_from_stand_in():
    m = ZbcCreativeMemory()
    d = m.evaluate(_result(), NotBuiltVerificationIntegrity())
    assert not d.learned and "not built yet" in d.reason


def test_memory_rejects_self_reported_without_asking():
    class Spy(PassingVerification):
        called = False

        def attest_result(self, result_id, facts):
            Spy.called = True
            return super().attest_result(result_id, facts)

    d = ZbcCreativeMemory().evaluate(_result(source="self_reported"), Spy())
    assert not d.learned and not Spy.called


def test_memory_stores_attested_views_not_reported_views():
    m = ZbcCreativeMemory()
    d = m.evaluate(_result(reported_views=10_000_000), PassingVerification(verified_views=4321))
    assert d.learned and d.winner.verified_views == 4321
    m.commit(d.winner)
    assert m.winners("podcasts", "youtube")[0].verified_views == 4321


# --- 8 Clip Review ----------------------------------------------------------------------------

def _review(registry, rb=None, now=NOW, **over):
    rb = rb or _live(registry)
    return clip_review.review(clip_review.ClipSubmission.model_validate(zbc_clip(**over)), rb, registry, now)


def test_clean_clip_passes(registry):
    d = _review(registry)
    assert d.outcome == "pass" and d.broken_rules == () and "OR-01" in d.checks


@pytest.mark.parametrize("over,rule", [
    ({"platform": "tiktok", "placement": "feed"}, "PF-01"),
    ({"length_seconds": 181}, "SP-01"),
    ({"angle_id": "A09"}, "OB-01"),
    ({"is_raw_repost": True}, "OR-01"),
    ({"transformation_elements": []}, "OR-01"),
    ({"transformation_elements": ["captions_added"]}, "OR-01"),
    ({"has_third_party_watermark": True}, "OW-01"),
    ({"caption": "budget myth"}, "DC-01"),
    ({"transcript": "This budget myth costs you."}, "MS-01"),
    ({"transcript": "Guaranteed returns! Listen on Pod Plus."}, "NS-01"),
    ({"resolution_height_px": 480}, "QF-01"),
    ({"added_asset_ids": ["top40_song"]}, "RC-01"),
])
def test_each_rule_rejects_citing_its_id(registry, over, rule):
    d = _review(registry, **over)
    assert d.outcome == "reject" and rule in [b.rule_id for b in d.broken_rules]


def test_disclosure_via_platform_label(registry):
    assert _review(registry, caption="budget myth", paid_partnership_label=True).outcome == "pass"


@pytest.mark.parametrize("over,needle", [
    ({"resolution_height_px": None}, "resolution not declared"),
    ({"caption": "#ad", "on_screen_text": "", "transcript": "Listen on Pod Plus."}, "borderline on-brief"),
    ({"transformation_elements": ["original_commentary", "captions_added", "ai_dance"]}, "unrecognised"),
])
def test_borderline_goes_to_human_queue(registry, over, needle):
    d = _review(registry, **over)
    assert d.outcome == "human_review" and any(needle in r for r in d.human_review_reasons)


def test_stale_registry_row_is_never_an_automatic_pass(registry):
    d = _review(registry, now=NOW.replace(month=10, day=23))
    assert d.outcome == "human_review" and any("can't be relied on" in r for r in d.human_review_reasons)


def test_missing_rule_means_no_rejection(registry):
    rb = _live(registry)
    no_ow = rb.model_copy(update={"rules": tuple(r for r in rb.rules if r.kind is not RuleKind.ORIGINALITY_WATERMARK)})
    d = _review(registry, rb=no_ow, has_third_party_watermark=True)
    assert d.outcome == "human_review" and d.broken_rules == ()


# --- payout gate ----------------------------------------------------------------------------

def test_payout_gate_names_every_blocker(registry):
    sub = clip_review.ClipSubmission.model_validate(zbc_clip())
    d = _review(registry)
    p = payout_eligibility.evaluate(sub, d, NotBuiltVerificationIntegrity(), NotBuiltCompliance38())
    assert not p.eligible and len(p.blockers) == 2
    p2 = payout_eligibility.evaluate(sub, d, PassingVerification(), NotBuiltCompliance38())
    assert not p2.eligible and p2.blockers[0].startswith("compliance_38")
    p3 = payout_eligibility.evaluate(sub, d, PassingVerification(), PassingCompliance())
    assert p3.eligible
    rej = _review(registry, has_third_party_watermark=True)
    p4 = payout_eligibility.evaluate(sub, rej, PassingVerification(), PassingCompliance())
    assert not p4.eligible and p4.blockers == ("clip_review: outcome is 'reject', not 'pass'",)
