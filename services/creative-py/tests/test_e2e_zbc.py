"""End-to-end ZBC campaign flow over the API (TestClient; fakes for ledger + clock only)."""

from conftest import TEST_FOUNDER_TOKEN
from fakes import PassingCompliance, PassingVerification
from flows import C, ok, zbc_open
from samples import CAMPAIGN, zbc_clip


def test_full_campaign_flow_rulebook_to_eligibility(api):
    live = zbc_open(api)
    assert live["status"] == "live" and live["version"] == 1

    kit = api.zbc.kits[CAMPAIGN]
    assert kit.status == "signed" and len(kit.seeds) == 3
    assert kit.seed_clips_produced is False  # Enigma/Phantom Canvas stand-in: not commissioned

    d = ok(api.post("/zbc/clips", zbc_clip()), 201)
    assert d["outcome"] == "pass", d
    assert d["rulebook_version"] == 1

    el = ok(api.post(f"/zbc/clips/{d['submission_id']}/payout-eligibility"))
    assert el["eligible"] is False
    assert any(b.startswith("verification_and_integrity:") for b in el["blockers"])
    assert any(b.startswith("compliance_38:") for b in el["blockers"])

    types = [e["event_type"] for e in api.ledger.events]
    for t in ("rulebook_drafted", "rulebook_approved", "rulebook_signed_by_andre", "rights_clearance_checked",
              "crossing_clipper_network", "rulebook_live", "moment_map_built", "hook_sheets_built",
              "crossing_creative_agents", "campaign_kit_built", "campaign_kit_signed_by_andre", "clip_reviewed",
              "crossing_verification_integrity", "crossing_compliance_38", "payout_eligibility_decided"):
        assert t in types, t
    assert {e["department"] for e in api.ledger.events} == {"creative_production"}


def test_eligibility_true_only_when_all_three_gates_pass_with_test_fakes(make_api):
    from shared.departments import Departments

    api = make_api(departments=Departments(compliance=PassingCompliance(), verification=PassingVerification()))
    zbc_open(api)
    ok(api.post("/zbc/clips", zbc_clip()), 201)
    el = ok(api.post("/zbc/clips/clip_001/payout-eligibility"))
    assert el["eligible"] is True and el["blockers"] == []
    # a rejected clip stays ineligible even with passing fakes
    ok(api.post("/zbc/clips", zbc_clip("clip_002", caption="no disclosure here")), 201)
    el2 = ok(api.post("/zbc/clips/clip_002/payout-eligibility"))
    assert el2["eligible"] is False and el2["blockers"][0].startswith("clip_review:")


def test_revision_flow_old_clips_judged_by_old_version(api, clock):
    from datetime import timedelta

    from samples import zbc_goal

    zbc_open(api)
    # v2 adds a never-say line
    goal2 = zbc_goal(never_say=["guaranteed returns", "get rich"])
    v2 = ok(api.post(f"{C}/revisions", {"actor_id": "zbc_rulebook_writer", "goal": goal2}), 201)
    assert v2["version"] == 2 and v2["supersedes_version"] == 1
    ok(api.post(f"{C}/rulebooks/2/review", {"actor_id": "zbc_campaign_rulebook"}))
    ok(api.post(f"{C}/rulebooks/2/sign", andre=TEST_FOUNDER_TOKEN))
    clock.at = clock.at + timedelta(hours=2)
    ok(api.post(f"{C}/rulebooks/2/go-live"))
    assert api.zbc.rulebooks.get(CAMPAIGN, 1).status.value == "superseded"

    # clip made under v1 (posted while v1 was live) says "get rich": fine under v1
    old = ok(api.post("/zbc/clips", zbc_clip("clip_old", transcript="Get rich slowly. This budget myth. Listen on Pod Plus.")), 201)
    assert old["outcome"] == "pass" and old["rulebook_version"] == 1
    # a clip posted after v2 went live can't claim v1
    late = api.post("/zbc/clips", zbc_clip("clip_late", posted_at=clock.now().isoformat()))
    assert late.status_code == 409 and "live window" in late.json()["detail"]
    # same content under v2 is rejected, citing v2's new never-say rule id
    new = ok(api.post("/zbc/clips", zbc_clip("clip_new", rulebook_version=2, posted_at=clock.now().isoformat(),
                                             transcript="Get rich slowly. This budget myth. Listen on Pod Plus.")), 201)
    assert new["outcome"] == "reject"
    ns = [b["rule_id"] for b in new["broken_rules"]]
    assert ns == ["NS-02"]
