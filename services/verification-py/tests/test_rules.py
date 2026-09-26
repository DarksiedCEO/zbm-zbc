"""The V&I rule register (spec §B.6): only Andre changes it; weakening is flagged and must be acknowledged;
founder rules can never be weakened; cited rules can never be retired; stale or changed proposals are 409."""

from __future__ import annotations

import pytest

from helpers import ANDRE_TOKEN, rid


def propose(h, body, code=201):
    r = h.post("/vi/v1/rules/proposals", {"request_id": rid(), **body}, andre=ANDRE_TOKEN)
    assert r.status_code == code, r.text
    return r.json().get("proposal") if code == 201 else r.json()


def decide(h, p, decision="approve", ack=None, code=200):
    d = {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": decision}
    if ack is not None:
        d["acknowledge_weakening"] = ack
    r = h.post("/vi/v1/rules/decisions", {"request_id": rid(), "decisions": [d]}, andre=ANDRE_TOKEN)
    assert r.status_code == code, r.text
    return r.json()


def row(h, rid_):
    return dict(h.svc.current.by_id()[rid_])


def test_seed_proposal_waits_for_andre_and_publishes_version_1(h):
    v = h.rules()
    assert v["rules_version"] is None and v["rules_pinned"] is True
    seed = [p for p in v["open_proposals"] if p["kind"] == "seed"]
    assert len(seed) == 1 and len(seed[0]["proposed_rows"]) == 28
    out = h.approve_rules()
    assert out["rules_version"] == 1
    v = h.rules()
    assert v["rules_version"] == 1 and len(v["rules"]) == 28 and v["open_proposals"] == []
    assert h.ledger.of_type("rules_version_published")
    assert {r["rule_id"] for r in v["rules"] if r["status"] == "unverified"} == {"VI-15c", "VI-15d", "VI-15e", "VI-22"}


def test_seed_approval_needs_the_exact_content_hash(h):
    seed = [p for p in h.rules()["open_proposals"] if p["kind"] == "seed"][0]
    decide(h, dict(seed, content_sha256="0" * 64), code=409)
    assert h.rules()["rules_version"] is None


def test_weakening_amend_needs_acknowledgment(hr):
    r = row(hr, "VI-15c")
    r["statement"] = "A softer statement."
    p = propose(hr, {"kind": "amend", "target_id": "VI-15c", "proposed_row": r})
    assert p["weakening"] is True and "text_changed" in p["weakening_reasons"]
    decide(hr, p, code=422)
    assert hr.rules()["rules_version"] == 1
    decide(hr, p, ack=True)
    assert hr.rules()["rules_version"] == 2
    ev = [e for e in hr.ledger.of_type("rules_proposal_decided") if e["payload"]["proposal_id"] == p["proposal_id"]]
    assert ev[0]["payload"]["acknowledged_weakening"] is True


def test_unverified_to_verified_is_weakening_and_vi22_flip_changes_copyright_strike(hr):
    r = row(hr, "VI-22")
    r["status"] = "verified"
    p = propose(hr, {"kind": "amend", "target_id": "VI-22", "proposed_row": r})
    assert "unverified_to_verified" in p["weakening_reasons"]
    decide(hr, p, ack=True)
    from test_cert_scenarios import hr13, run_to_day
    post_ref = hr.clean_clip("v22")
    run_to_day(hr, 14)
    a = hr13(hr, "v22", post_ref)
    assert a["copyright_strike"] is False and a["verified_views"] is True and a["reasons"] == []


@pytest.mark.parametrize("rule,mut", [("VI-06", {"basis_obligation_ids": ["HR-13"]}),
                                      ("VI-11", {"statement": "Softer."}), ("VI-00", {"parameters": {"x": 1}}),
                                      ("VI-21", {"kind": "spec_choice"})])
def test_founder_rules_can_never_be_weakened(hr, rule, mut):
    r = {**row(hr, rule), **mut}
    out = propose(hr, {"kind": "amend", "target_id": rule, "proposed_row": r}, code=422)
    assert "founder-locked" in out["detail"]


def test_founder_rule_can_be_strengthened(hr):
    r = row(hr, "VI-11")
    r["source_urls"] = r["source_urls"] + ["https://www.ftc.gov/system/files/ftc_gov/pdf/coppa-age-verification-policy-statement.pdf"]
    p = propose(hr, {"kind": "amend", "target_id": "VI-11", "proposed_row": r})
    assert p["weakening"] is False
    decide(hr, p)


@pytest.mark.parametrize("rule", ["VI-03", "VI-15a", "VI-16", "VI-02", "VI-22"])
def test_cited_rules_cannot_be_retired(hr, rule):
    propose(hr, {"kind": "retire", "target_id": rule}, code=422)


def test_add_then_retire_a_new_rule(hr):
    new = {"rule_id": "VI-30", "title": "Extra rule", "statement": "An added rule.", "source_urls": [],
           "basis_obligation_ids": [], "kind": "spec_choice", "parameters": {}, "status": "verified"}
    p = propose(hr, {"kind": "add", "proposed_row": new})
    assert p["weakening"] is False
    decide(hr, p)
    p = propose(hr, {"kind": "retire", "target_id": "VI-30"})
    assert p["weakening"] is True
    decide(hr, p, code=422)
    decide(hr, p, ack=True)
    assert "VI-30" not in hr.svc.current.by_id()


def test_server_only_fields_unknown_keys_and_floats_refused(hr):
    r = row(hr, "VI-15c")
    for bad in ({**r, "approved_by": "andre"}, {**r, "in_force": True}, {**r, "rules_version": 3}, {**r, "extra": 1},
                {**r, "parameters": {"threshold": 0.5}}, {**r, "source_urls": ["http://insecure.example"]}):
        propose(hr, {"kind": "amend", "target_id": "VI-15c", "proposed_row": bad}, code=422)
    assert hr.post("/vi/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": "VI-15c",
                                              "proposed_row": r, "approved_by": "andre"},
                   andre=ANDRE_TOKEN).status_code == 422


def test_stale_proposal_is_409_and_decisions_are_atomic(hr):
    r1 = {**row(hr, "VI-15c"), "statement": "Version A."}
    r2 = {**row(hr, "VI-15c"), "statement": "Version B."}
    p1 = propose(hr, {"kind": "amend", "target_id": "VI-15c", "proposed_row": r1})
    p2 = propose(hr, {"kind": "amend", "target_id": "VI-15c", "proposed_row": r2})
    decide(hr, p1, ack=True)
    decide(hr, p2, ack=True, code=409)                                 # drafted against the old row
    v = hr.rules()["rules_version"]
    new = {"rule_id": "VI-31", "title": "t", "statement": "s", "source_urls": [], "basis_obligation_ids": [],
           "kind": "spec_choice", "parameters": {}, "status": "verified"}
    p3 = propose(hr, {"kind": "add", "proposed_row": new})
    r = hr.post("/vi/v1/rules/decisions", {"request_id": rid(), "decisions": [
        {"proposal_id": p3["proposal_id"], "content_sha256": p3["content_sha256"], "decision": "approve"},
        {"proposal_id": p2["proposal_id"], "content_sha256": p2["content_sha256"], "decision": "approve",
         "acknowledge_weakening": True}]}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and hr.rules()["rules_version"] == v and "VI-31" not in hr.svc.current.by_id()
    decide(hr, p1, code=409)                                           # already approved


def test_rejection_changes_nothing(hr):
    r = {**row(hr, "VI-15c"), "statement": "Rejected text."}
    p = propose(hr, {"kind": "amend", "target_id": "VI-15c", "proposed_row": r})
    decide(hr, p, decision="reject")
    assert hr.rules()["rules_version"] == 1


def test_rules_rows_are_cited_in_every_reason_with_their_source(hr):
    hr.ok(hr.register("x1", "c1", "snapchat", post_ref="https://snap.example/1"), 201)
    hr.advance(days=1)
    c = hr.cert("x1")
    pnp = [r for r in c["reasons"] if r["code"] == "PLATFORM_NOT_PAYABLE"][0]
    assert pnp["rule_id"] == "VI-18" and pnp["source_url"].startswith("https://")
    assert all(line.startswith("vi/VI-") for line in c["reason_lines"])
