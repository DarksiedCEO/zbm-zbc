"""End-to-end ZBM flow over the API: brief -> approve -> job -> export -> rights -> quality -> gate -> Andre."""

from conftest import TEST_FOUNDER_TOKEN
from fakes import PassingCompliance
from flows import ok, zbm_work_at_quality
from samples import zbm_work


def test_brief_to_gate_stops_at_compliance_stand_in(api):
    brief, job, w = zbm_work_at_quality(api)
    assert brief["status"] == "approved" and brief["approved_by"] == "zbm_creative_lead"
    assert all(c["commissioned"] is False for c in job["commissions"])  # external makers: contract only
    wid = w["work_id"]
    item = ok(api.get(f"/zbm/work/{wid}"))
    assert item["stage"] == "rights_cleared"
    assert item["export_validation"]["verdict"] == "pass"
    assert any("aspect_ratio" in g for g in item["export_validation"]["registry_coverage_gaps"])

    q = ok(api.post(f"/zbm/work/{wid}/quality", {"actor_id": "zbm_creative_quality", "notes": []}))
    assert q["stage"] == "quality_passed"
    g = ok(api.post(f"/zbm/work/{wid}/compliance"))
    assert g["stage"] == "compliance_blocked"
    assert "not allowed yet" in g["compliance"]["reason"]
    # Andre can't approve past a blocked gate, even with the right token
    r = api.post(f"/zbm/work/{wid}/final-approval", andre=TEST_FOUNDER_TOKEN)
    assert r.status_code == 409 and "Compliance (38)" in r.json()["detail"]

    types = [e["event_type"] for e in api.ledger.events]
    for t in ("rights_record_added", "brief_drafted", "brief_approved", "production_opened", "work_submitted",
              "export_validated", "rights_checked", "quality_passed", "crossing_compliance_38"):
        assert t in types, t
    assert "andre_final_approval" not in types


def test_full_flow_to_andre_final_approval_with_test_compliance_fake(make_api):
    from shared.departments import Departments

    api = make_api(departments=Departments(compliance=PassingCompliance()))
    _, _, w = zbm_work_at_quality(api)
    wid = w["work_id"]
    ok(api.post(f"/zbm/work/{wid}/quality", {"actor_id": "zbm_creative_quality", "notes": []}))
    assert ok(api.post(f"/zbm/work/{wid}/compliance"))["stage"] == "compliance_passed"
    assert api.post(f"/zbm/work/{wid}/final-approval").status_code == 403           # no token
    assert api.post(f"/zbm/work/{wid}/final-approval", andre="wrong").status_code == 403
    fin = ok(api.post(f"/zbm/work/{wid}/final-approval", andre=TEST_FOUNDER_TOKEN))
    assert fin["stage"] == "approved_by_andre" and fin["final_approval"]["approved_by"] == "andre"
    assert api.ledger.of_type("andre_final_approval")


def test_two_round_cap_then_escalation_to_andre(api):
    _, job, w = zbm_work_at_quality(api)
    r1 = ok(api.post(f"/zbm/work/{w['work_id']}/quality",
                     {"actor_id": "zbm_creative_quality", "notes": ["Lighting is flat; not premium."]}))
    assert r1["stage"] == "sent_back" and r1["round"] == 1
    w2 = ok(api.post(f"/zbm/jobs/{job['job_id']}/work", zbm_work()), 201)
    assert w2["round"] == 2
    ok(api.post(f"/zbm/work/{w2['work_id']}/export-validation"))
    ok(api.post(f"/zbm/work/{w2['work_id']}/rights"))
    r2 = ok(api.post(f"/zbm/work/{w2['work_id']}/quality",
                     {"actor_id": "zbm_creative_quality", "notes": ["Still flat."]}))
    assert r2["stage"] == "escalated_to_andre"
    assert r2["quality_decision"]["reported_to"] == "andre"
    # no third round
    r3 = api.post(f"/zbm/jobs/{job['job_id']}/work", zbm_work())
    assert r3.status_code == 409
    # Andre decides the escalation with his token
    assert api.post(f"/zbm/work/{w2['work_id']}/escalation", {"decision": "accept"}).status_code == 403
    acc = ok(api.post(f"/zbm/work/{w2['work_id']}/escalation", {"decision": "accept"}, andre=TEST_FOUNDER_TOKEN))
    assert acc["stage"] == "escalation_accepted_by_andre"
    assert ok(api.post(f"/zbm/work/{w2['work_id']}/compliance"))["stage"] == "compliance_blocked"
