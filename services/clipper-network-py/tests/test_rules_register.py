"""The CN rule register (§B.9): seed approval, proposals, weakening flags, stale checks, counsel memos, templates."""

from __future__ import annotations

import copy

import pytest

from helpers import ANDRE_TOKEN, Harness, codes, rid


def rule(h, rid_):
    return copy.deepcopy(h.rule(rid_))


def test_seed_is_one_proposal_and_approval_publishes_version_1():
    h = Harness()
    inbox = h.inbox()
    assert [p["kind"] for p in inbox] == ["seed"]
    r = h.approve(inbox[0])
    assert r["rules_version"] == 1
    v = h.get("/cn/v1/rules").json()
    assert len(v["rules"]) == 35 and len(v["templates"]) == 17 and v["rules_pinned"] is True
    ev = h.ledger.of_type("rules_version_published")
    assert ev and ev[0]["event_id"].startswith("cn-ver-")


@pytest.mark.parametrize("path,value,reason", [
    ("t1.min_certified_clips", 1, "parameter_weakened:t1.min_certified_clips"),
    ("max_active_enrolments.T0", 9, "parameter_weakened:max_active_enrolments.T0"),
    ("t2.median_exclude_platforms", [], "parameter_weakened:t2.median_exclude_platforms"),
    ("platform_anchors", True, "parameter_weakened:platform_anchors"),
])
def test_weakening_parameter_changes_are_flagged_and_need_acknowledgment(path, value, reason):
    h = Harness()
    h.approve_seed()
    r = rule(h, "CN-10")
    cur = r["parameters"]
    parts = path.split(".")
    for p in parts[:-1]:
        cur = cur[p]
    cur[parts[-1]] = value
    p = h.propose_rule({"kind": "amend", "target_id": "CN-10", "rule": r}).json()["proposal"]
    assert p["weakening"] is True and reason in p["weakening_reasons"]
    d = h.decide([{"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve"}])
    assert d.status_code == 422 and "acknowledge_weakening" in d.text
    assert h.approve(p, ack=True)["rules_version"] == 2
    assert h.ledger.of_type("rules_proposal_approved")[-1]["payload"]["acknowledged_weakening"] is True


def test_strengthening_change_is_not_flagged():
    h = Harness()
    h.approve_seed()
    r = rule(h, "CN-17")
    r["parameters"]["rate_notice_days"] = 14
    p = h.propose_rule({"kind": "amend", "target_id": "CN-17", "rule": r}).json()["proposal"]
    assert p["weakening"] is False
    h.approve(p)
    assert h.rule("CN-17")["parameters"]["rate_notice_days"] == 14


def test_guardian_path_is_refused_outright():
    h = Harness()
    h.approve_seed()
    r = rule(h, "CN-01")
    r["parameters"]["guardian_path"] = True
    x = h.propose_rule({"kind": "amend", "target_id": "CN-01", "rule": r})
    assert x.status_code == 422 and "no guardian path" in x.text


def test_parameter_shape_must_match_the_seed():
    h = Harness()
    h.approve_seed()
    r = rule(h, "CN-19")
    r["parameters"]["table"]["S2"]["days"] = "thirty"
    assert h.propose_rule({"kind": "amend", "target_id": "CN-19", "rule": r}).status_code == 422
    r = rule(h, "CN-19")
    r["parameters"]["extra"] = 1
    assert h.propose_rule({"kind": "amend", "target_id": "CN-19", "rule": r}).status_code == 422


def test_server_set_fields_and_unknown_fields_refused():
    h = Harness()
    h.approve_seed()
    r = rule(h, "CN-17")
    r["approved_by"] = "andre"
    assert h.propose_rule({"kind": "amend", "target_id": "CN-17", "rule": r}).status_code == 422
    assert h.post("/cn/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": "CN-17",
                                             "rule": rule(h, "CN-17"), "approved": True}, andre=ANDRE_TOKEN).status_code == 422


def test_stale_proposal_is_409():
    h = Harness()
    h.approve_seed()
    r1, r2 = rule(h, "CN-17"), rule(h, "CN-17")
    r1["parameters"]["rate_notice_days"] = 10
    r2["parameters"]["rate_notice_days"] = 12
    p1 = h.propose_rule({"kind": "amend", "target_id": "CN-17", "rule": r1}).json()["proposal"]
    p2 = h.propose_rule({"kind": "amend", "target_id": "CN-17", "rule": r2}).json()["proposal"]
    h.approve(p1)
    d = h.decide([{"proposal_id": p2["proposal_id"], "content_sha256": p2["content_sha256"], "decision": "approve"}])
    assert d.status_code == 409 and "stale" in d.text


def test_counsel_rows_change_only_by_memo_and_cq03_blocks_sag_aftra_enrolment():
    h = Harness()
    h.approve_seed()
    r = rule(h, "CN-CQ-01")
    r["statement"] = "resolved by me"
    assert h.propose_rule({"kind": "amend", "target_id": "CN-CQ-01", "rule": r}).status_code == 422
    assert h.propose_rule({"kind": "retire", "target_id": "CN-CQ-01"}).status_code == 422
    h.clear_counsel("CN-CQ-01")
    assert h.rule("CN-CQ-01")["status"] == "resolved"
    cid = h.admitted_clipper(sag_aftra_member=True)
    h.config()
    assert ("CN-CQ-03", "COUNSEL_HOLD") in codes(h.enrol(cid).json())
    h.clear_counsel("CN-CQ-03")
    assert h.enrol(cid).json()["eligible"] is True


def test_cn00_cannot_be_retired_and_retire_is_weakening():
    h = Harness()
    h.approve_seed()
    assert h.propose_rule({"kind": "retire", "target_id": "CN-00"}).status_code == 422
    p = h.propose_rule({"kind": "retire", "target_id": "CN-25"}).json()["proposal"]
    assert p["weakening_reasons"] == ["rule_retired"]


def test_new_rule_carries_no_parameters():
    h = Harness()
    h.approve_seed()
    new = {"rule_id": "CN-27", "title": "Test", "statement": "A new house rule.", "kind": "founder", "sources": [],
           "parameters": {}, "status": "in_force"}
    p = h.propose_rule({"kind": "new", "rule": new}).json()["proposal"]
    assert p["weakening"] is False
    new2 = dict(new, rule_id="CN-28", parameters={"x": 1})
    assert h.propose_rule({"kind": "new", "rule": new2}).status_code == 422


def test_proposals_need_a_version_in_force_first():
    h = Harness()
    assert h.propose_rule({"kind": "retire", "target_id": "CN-25"}).status_code == 409


def test_template_amend_flags_channel_and_body_and_keeps_required_elements():
    h = Harness()
    h.approve_seed()
    t = copy.deepcopy(next(x for x in h.get("/cn/v1/rules").json()["templates"] if x["template_id"] == "recruiting_invite"))
    t2 = dict(t, version=2, body=t["body"].replace("Advertisement. ", ""))
    assert h.propose_template({"kind": "amend", "target_id": "recruiting_invite", "template": t2}).status_code == 422
    t3 = dict(t, version=2, body=t["body"].replace("18+ only", "all ages"))
    assert h.propose_template({"kind": "amend", "target_id": "recruiting_invite", "template": t3}).status_code == 422
    a = copy.deepcopy(next(x for x in h.get("/cn/v1/rules").json()["templates"] if x["template_id"] == "appeal_outcome"))
    a4 = dict(a, version=2, body=a["body"] + " Thanks.")
    p = h.propose_template({"kind": "amend", "target_id": "appeal_outcome", "template": a4}).json()["proposal"]
    assert p["weakening_reasons"] == ["body_changed"]
    h.approve(p, ack=True)
    assert h.ledger.of_type("template_version_published")
    a5 = dict(a4, version=2)
    assert h.propose_template({"kind": "amend", "target_id": "appeal_outcome", "template": a5}).status_code == 422  # version
    bad_channel = dict(a4, version=3, channels=["email", "in_app", "sms"])
    assert h.propose_template({"kind": "amend", "target_id": "appeal_outcome", "template": bad_channel}).status_code == 422


def test_messages_carry_the_template_version_in_force():
    h = Harness().ready()
    a = copy.deepcopy(next(x for x in h.get("/cn/v1/rules").json()["templates"] if x["template_id"] == "application_received"))
    a2 = dict(a, version=2, body=a["body"] + " Welcome.")
    p = h.propose_template({"kind": "amend", "target_id": "application_received", "template": a2}).json()["proposal"]
    h.approve(p, ack=True)
    cid = h.apply().json()["clipper_id"]
    m = h.messages(cid)[0]
    assert m["template_version"] == 2 and h.ports.messaging.sent[-1][3].endswith("Welcome.")


def test_rules_decisions_atomic_all_or_nothing():
    h = Harness()
    h.approve_seed()
    r = rule(h, "CN-17")
    r["parameters"]["rate_notice_days"] = 10
    ok = h.propose_rule({"kind": "amend", "target_id": "CN-17", "rule": r}).json()["proposal"]
    weak = h.propose_rule({"kind": "retire", "target_id": "CN-25"}).json()["proposal"]
    d = h.decide([{"proposal_id": ok["proposal_id"], "content_sha256": ok["content_sha256"], "decision": "approve"},
                  {"proposal_id": weak["proposal_id"], "content_sha256": weak["content_sha256"], "decision": "approve"}])
    assert d.status_code == 422
    assert h.rule("CN-17")["parameters"]["rate_notice_days"] == 7 and h.get("/cn/v1/rules").json()["rules_version"] == 1
