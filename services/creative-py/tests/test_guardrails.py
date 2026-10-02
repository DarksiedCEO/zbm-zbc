"""Cross-cutting guardrails the spec locks, each proven through the API or the model itself."""

import typing
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from conftest import TEST_FOUNDER_TOKEN
from flows import C, ok, zbc_live, zbc_open, zbm_work_at_quality
from samples import CAMPAIGN, zbc_clip, zbc_goal, zbm_requirements


# --- self-approval ------------------------------------------------------------------------

def test_brief_drafter_can_never_approve_own_brief_even_holding_both_roles(make_api):
    from shared.actors import ActorRegistry, Role

    actors = ActorRegistry()
    actors.add("sam", [Role.ZBM_BRIEF_WRITER, Role.ZBM_CREATIVE_LEAD])
    api = make_api(actors=actors)
    b = ok(api.post("/zbm/briefs", {"actor_id": "sam", "requirements": zbm_requirements()}), 201)
    r = api.post(f"/zbm/briefs/{b['brief_id']}/review", {"actor_id": "sam"})
    assert r.status_code == 403 and "self-approval refused" in r.json()["detail"]
    assert api.zbm.get_brief(b["brief_id"]).status.value == "draft"
    assert api.ledger.of_type("guardrail_refusal")
    # a different lead can
    assert ok(api.post(f"/zbm/briefs/{b['brief_id']}/review", {"actor_id": "zbm_creative_lead"}))["status"] == "approved"


def test_nothing_enters_production_without_an_approved_brief(api):
    b = ok(api.post("/zbm/briefs", {"requirements": zbm_requirements()}), 201)
    r = api.post(f"/zbm/briefs/{b['brief_id']}/jobs")
    assert r.status_code == 409 and "approved brief" in r.json()["detail"]
    assert not api.zbm.jobs


def test_brief_writer_role_cannot_approve(api):
    b = ok(api.post("/zbm/briefs", {"requirements": zbm_requirements()}), 201)
    r = api.post(f"/zbm/briefs/{b['brief_id']}/review", {"actor_id": "zbm_creative_quality"})
    assert r.status_code == 403


# --- frozen rulebook ------------------------------------------------------------------------

def test_live_rulebook_is_frozen_and_changes_create_a_new_version(api):
    zbc_live(api)
    before = api.zbc.rulebooks.get(CAMPAIGN, 1).model_dump()
    r = api.put(f"{C}/rulebooks/1", {"actor_id": "zbc_rulebook_writer", "goal": zbc_goal(never_say=[])})
    assert r.status_code == 409 and "FROZEN" in r.json()["detail"]
    assert api.zbc.rulebooks.get(CAMPAIGN, 1).model_dump() == before
    # re-approving or re-signing a live version is refused too
    assert api.post(f"{C}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"}).status_code == 409
    assert api.post(f"{C}/rulebooks/1/sign", andre=TEST_FOUNDER_TOKEN).status_code == 409
    v2 = ok(api.post(f"{C}/revisions", {"actor_id": "zbc_rulebook_writer", "goal": zbc_goal(never_say=[])}), 201)
    assert v2["version"] == 2 and v2["status"] == "draft"
    # fix wave 9 (AEGIS round 8 L2): a version carries its retired-id COUNT; the ids are paginated
    assert v2["retired_rule_count"] >= 1 and "retired_rule_ids" not in v2
    page = ok(api.get(f"{C}/rulebooks/2/retired-rule-ids"))
    assert "NS-01" in page["ids"]


def test_store_refuses_in_place_replacement_of_frozen_version(api):
    from shared.errors import FrozenError

    zbc_live(api)
    live = api.zbc.rulebooks.get(CAMPAIGN, 1)
    tampered = live.model_copy(update={"objective": "Something else entirely."})
    with pytest.raises(FrozenError):
        api.zbc.rulebooks.check_replace(tampered)


# --- rule-citation integrity ------------------------------------------------------------------

def test_decision_cannot_cite_a_rule_not_in_its_rulebook_version(api):
    from zbc.clip_review import ClipReviewDecision, RuleCitationError, make_decision

    zbc_live(api)
    rb = api.zbc.rulebooks.get(CAMPAIGN, 1)
    base = dict(submission_id="x", campaign_id=CAMPAIGN, rulebook_version=1, decided_by="zbc_clip_review",
                decided_at=datetime(2026, 9, 24, tzinfo=timezone.utc))
    with pytest.raises(RuleCitationError):
        make_decision(rb, outcome="reject", broken_rules=[{"rule_id": "NS-99", "reason": "made up"}], **base)
    with pytest.raises(RuleCitationError):
        make_decision(rb, outcome="reject", broken_rules=[], **base)  # a rejection must cite a rule
    with pytest.raises(ValueError, match="only be built against a rulebook"):  # not even without one
        ClipReviewDecision(outcome="pass", **base)
    with pytest.raises(RuleCitationError):  # nor against a different version
        make_decision(rb, outcome="pass", **{**base, "rulebook_version": 2})
    ok_dec = make_decision(rb, outcome="reject", broken_rules=[{"rule_id": "NS-01", "reason": "said it"}], **base)
    assert ok_dec.broken_rules[0].rule_id == "NS-01"


def test_every_automatic_rejection_cites_rule_ids_from_that_version(api):
    zbc_open(api)
    d = ok(api.post("/zbc/clips", zbc_clip(caption="nothing", transcript="guaranteed returns", is_raw_repost=True,
                                           has_third_party_watermark=True, length_seconds=500,
                                           added_asset_ids=["pirated_song"])), 201)
    assert d["outcome"] == "reject"
    ids = {b["rule_id"] for b in d["broken_rules"]}
    assert ids <= api.zbc.rulebooks.get(CAMPAIGN, 1).rule_ids()
    assert {"DC-01", "NS-01", "OR-01", "OW-01", "SP-01", "RC-01", "MS-01"} <= ids


def test_human_reviewer_cannot_cite_a_rule_not_in_that_version(api):
    zbc_open(api)
    d = ok(api.post("/zbc/clips", zbc_clip(resolution_height_px=None)), 201)
    assert d["outcome"] == "human_review"
    r = api.post(f"/zbc/clips/{d['submission_id']}/human-review",
                 {"actor_id": "zbc_clip_human_reviewer", "outcome": "reject",
                  "broken_rules": [{"rule_id": "XX-01", "reason": "vibes"}]})
    assert r.status_code == 422
    assert api.zbc.get_decision(d["submission_id"]).outcome == "human_review"
    r2 = api.post(f"/zbc/clips/{d['submission_id']}/human-review",
                  {"actor_id": "zbc_clip_human_reviewer", "outcome": "reject", "broken_rules": []})
    assert r2.status_code == 422  # no rule on the page, no rejection
    good = ok(api.post(f"/zbc/clips/{d['submission_id']}/human-review",
                       {"actor_id": "zbc_clip_human_reviewer", "outcome": "reject",
                        "broken_rules": [{"rule_id": "QF-01", "reason": "visibly 480p"}]}))
    assert good["outcome"] == "reject" and good["decided_by"] == "zbc_clip_human_reviewer"


# --- expired registry rows ------------------------------------------------------------------------

def test_expired_registry_row_blocks_zbm_brief_and_export(make_api, clock):
    from shared.clock import FixedClock

    late = FixedClock(datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc))  # rows expired 2026-10-23
    api = make_api(clock=late)
    b = ok(api.post("/zbm/briefs", {"requirements": zbm_requirements(deadline="2026-11-30")}), 201)
    assert any("expired on 2026-10-23" in i for i in b["issues"])
    rev = ok(api.post(f"/zbm/briefs/{b['brief_id']}/review", {"actor_id": "zbm_creative_lead"}))
    assert rev["status"] == "sent_back" and any("expired" in i for i in rev["review_issues"])


def test_expired_row_blocks_export_validation_after_approval(api, clock):
    _, _, w = zbm_work_at_quality(api)  # approved + validated while rows valid
    assert api.zbm.get_work(w["work_id"]).export_validation["verdict"] == "pass"
    from zbm.placement_spec import validate_export

    brief = api.zbm.get_brief(w["brief_id"])
    res = validate_export(api.zbm.get_work(w["work_id"]).submission.declared, brief.fields.deliverables[0],
                          api.app.state.registry, datetime(2026, 10, 23, tzinfo=timezone.utc).date())
    assert res.verdict == "fail"
    assert any(c.result == "blocked" and "expired" in c.reason for c in res.checks)


def test_expired_row_blocks_zbc_rulebook_approval(make_api):
    from shared.clock import FixedClock

    api = make_api(clock=FixedClock(datetime(2026, 10, 30, tzinfo=timezone.utc)))
    rb = ok(api.post(f"{C}/rulebooks", {"goal": zbc_goal()}), 201)
    assert rb["blocking_issues"]
    rev = ok(api.post(f"{C}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"}))
    assert rev["status"] == "sent_back"


def test_registry_row_view_says_why_it_is_blocked(api):
    row = ok(api.get("/registry/rows/tiktok-originality-unverified"))
    assert row["usable"] is False and "unverified" in row["usability_reason"]
    assert all(not (r["platform"] == "tiktok" and r["status"] == "verified") for r in ok(api.get("/registry/rows"))["rows"])


# --- no money in review decisions; payout never eligible while stand-ins fail closed ----------------

MONEY_WORDS = ("amount", "usd", "pay", "payout", "money", "price", "rate", "fee", "currency", "cost")


def _annotations_contain_decimal(tp) -> bool:
    if tp is Decimal:
        return True
    return any(_annotations_contain_decimal(a) for a in typing.get_args(tp))


def test_clip_review_decision_has_no_money_fields():
    from zbc.clip_review import BrokenRule, ClipReviewDecision

    for model in (ClipReviewDecision, BrokenRule):
        for name, f in model.model_fields.items():
            assert not any(w in name.lower() for w in MONEY_WORDS), name
            assert not _annotations_contain_decimal(f.annotation), name


def test_payout_eligibility_carries_no_amounts():
    import dataclasses

    from zbc.payout_eligibility import PayoutEligibility

    for f in dataclasses.fields(PayoutEligibility):
        assert not any(w in f.name.lower() for w in ("amount", "usd", "money", "price", "rate", "fee")), f.name


def test_payout_never_eligible_while_stand_ins_fail_closed(api):
    zbc_open(api)
    ok(api.post("/zbc/clips", zbc_clip()), 201)
    ok(api.post("/zbc/clips", zbc_clip("clip_rej", caption="no disclosure")), 201)
    ok(api.post("/zbc/clips", zbc_clip("clip_hr", resolution_height_px=None)), 201)
    for sid in ("clip_001", "clip_rej", "clip_hr"):
        el = ok(api.post(f"/zbc/clips/{sid}/payout-eligibility"))
        assert el["eligible"] is False
        assert any("Verification and Integrity is not built yet" in b for b in el["blockers"])
        assert any("Compliance (38) is not built yet" in b for b in el["blockers"])
        assert set(el) >= {"submission_id", "eligible", "blockers"}
        assert not any(w in k for k in el for w in ("amount", "usd", "payout_amount"))


# --- ledger failure: the decision does not take effect -----------------------------------------------

def test_ledger_failure_means_brief_approval_did_not_happen(api):
    b = ok(api.post("/zbm/briefs", {"requirements": zbm_requirements()}), 201)
    api.ledger.fail_next = True
    r = api.post(f"/zbm/briefs/{b['brief_id']}/review", {"actor_id": "zbm_creative_lead"})
    assert r.status_code == 503 and r.json()["took_effect"] is False
    assert api.zbm.get_brief(b["brief_id"]).status.value == "draft"


def test_ledger_failure_means_andre_signature_did_not_happen(api):
    from flows import zbc_rights_on_file

    zbc_rights_on_file(api)
    ok(api.post(f"{C}/rulebooks", {"goal": zbc_goal()}), 201)
    ok(api.post(f"{C}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"}))
    api.ledger.fail_next = True
    r = api.post(f"{C}/rulebooks/1/sign", andre=TEST_FOUNDER_TOKEN)
    assert r.status_code == 503
    assert api.zbc.rulebooks.get(CAMPAIGN, 1).status.value == "approved"


def test_ledger_failure_means_clip_decision_not_stored(api):
    zbc_open(api)
    api.ledger.fail_all = True
    r = api.post("/zbc/clips", zbc_clip())
    assert r.status_code == 503 and "did NOT take effect" in r.json()["detail"]
    assert "clip_001" not in api.zbc.decisions


def test_unconfigured_ledger_refuses_every_decision(make_api):
    from shared.ledger import UnconfiguredLedgerClient

    api = make_api(ledger=UnconfiguredLedgerClient())
    r = api.post("/zbm/briefs", {"requirements": zbm_requirements()})
    assert r.status_code == 503 and "not configured" in r.json()["detail"]
    assert not api.zbm.briefs


# --- Andre's token ---------------------------------------------------------------------------------

def test_founder_token_equal_to_service_token_is_treated_as_unset(make_api):
    from conftest import TEST_SERVICE_TOKEN

    api = make_api(founder_token=TEST_SERVICE_TOKEN)
    from flows import zbc_rights_on_file

    zbc_rights_on_file(api)
    ok(api.post(f"{C}/rulebooks", {"goal": zbc_goal()}), 201)
    ok(api.post(f"{C}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"}))
    r = api.post(f"{C}/rulebooks/1/sign", andre=TEST_SERVICE_TOKEN)
    assert r.status_code == 403 and "not configured" in r.json()["detail"]


def test_non_ascii_founder_token_is_403_not_500(api):
    from flows import zbc_rights_on_file

    zbc_rights_on_file(api)
    ok(api.post(f"{C}/rulebooks", {"goal": zbc_goal()}), 201)
    ok(api.post(f"{C}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"}))
    r = api.client.post(f"{C}/rulebooks/1/sign", headers={"X-Andre-Approval-Token": b"caf\xc3\xa9"})
    assert r.status_code == 403


# --- go-live needs rights; clips need a signed kit ---------------------------------------------------

def test_go_live_fails_closed_without_sublicense(api):
    from samples import zbc_license, zbc_music_clearance, ZBC_ASSETS

    ok(api.post("/rights/licenses", {"actor_id": "rights_desk",
                                     "license": zbc_license(sublicense_to_clippers=False)}), 201)
    ok(api.post("/rights/clearances", {"actor_id": "rights_desk", "record": zbc_music_clearance()}), 201)
    ok(api.post(f"{C}/rulebooks", {"goal": zbc_goal()}), 201)
    ok(api.post(f"{C}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"}))
    ok(api.post(f"{C}/rulebooks/1/sign", andre=TEST_FOUNDER_TOKEN))
    rc = ok(api.post(f"{C}/rights-check", {"assets": ZBC_ASSETS}))
    assert rc["cleared"] is False
    r = api.post(f"{C}/rulebooks/1/go-live")
    assert r.status_code == 409 and "sublicense" in r.json()["detail"]
    assert api.zbc.rulebooks.get(CAMPAIGN, 1).status.value == "signed"


def test_go_live_without_any_rights_check_fails_closed(api):
    from flows import zbc_rights_on_file

    zbc_rights_on_file(api)
    ok(api.post(f"{C}/rulebooks", {"goal": zbc_goal()}), 201)
    ok(api.post(f"{C}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"}))
    ok(api.post(f"{C}/rulebooks/1/sign", andre=TEST_FOUNDER_TOKEN))
    assert api.post(f"{C}/rulebooks/1/go-live").status_code == 409


def test_clips_refused_until_andre_signs_the_kit(api):
    from samples import zbc_kit_request, zbc_source

    zbc_live(api)
    assert api.post("/zbc/clips", zbc_clip()).status_code == 409
    ok(api.post(f"{C}/moment-map", zbc_source()))
    ok(api.post(f"{C}/hook-sheets"))
    ok(api.post(f"{C}/kit", zbc_kit_request()), 201)
    assert api.post("/zbc/clips", zbc_clip()).status_code == 409
    assert api.post(f"{C}/kit/sign").status_code == 403
    ok(api.post(f"{C}/kit/sign", andre=TEST_FOUNDER_TOKEN))
    assert api.post("/zbc/clips", zbc_clip()).status_code == 201


def test_registry_row_ownership_enforced_over_api(api):
    row = ok(api.get("/registry/rows/yt-shorts-max-length"))
    body_row = {k: row[k] for k in ("row_id", "platform", "placement", "rule_key", "value", "unit", "enforcement",
                                    "source_url", "verified_at", "expires_at", "owner", "status", "note")}
    # ZBC Platform Rules cannot write a ZBM spec row
    r = api.put("/registry/rows/yt-shorts-max-length", {"actor_id": "zbc_platform_rules", "row": body_row})
    assert r.status_code == 403
    # ...nor take it over by relabelling the owner
    r2 = api.put("/registry/rows/yt-shorts-max-length",
                 {"actor_id": "zbc_platform_rules", "row": {**body_row, "owner": "zbc_platform_rules"}})
    assert r2.status_code == 403 and "owned by" in r2.json()["detail"]
    # a random actor owns nothing
    assert api.put("/registry/rows/yt-shorts-max-length", {"actor_id": "zbm_creative_lead", "row": body_row}).status_code == 403
    # the owner re-verifies it (new dates) -> recorded
    fresh = {**body_row, "verified_at": "2026-09-24", "expires_at": "2026-10-24"}
    out = ok(api.put("/registry/rows/yt-shorts-max-length", {"actor_id": "zbm_placement_spec", "row": fresh}))
    assert out["row"]["expires_at"] == "2026-10-24" and api.ledger.of_type("registry_row_written")
    # a shelf life over 30 days is refused
    too_long = {**body_row, "verified_at": "2026-09-24", "expires_at": "2026-12-24"}
    assert api.put("/registry/rows/yt-shorts-max-length",
                   {"actor_id": "zbm_placement_spec", "row": too_long}).status_code == 422
