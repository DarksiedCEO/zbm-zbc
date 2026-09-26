"""Spec §G scenario certification tests S1-S10 (no network; fakes for every port)."""

from __future__ import annotations

from datetime import timedelta

from clock import iso
from fakes import FakeMessaging, KIT_SHA
from helpers import ANDRE_TOKEN, Harness, code_set, codes, rid
from ports import Certification, Ports


def test_s1_rules_not_approved_every_decision_is_rules_not_in_force():
    h = Harness()
    assert h.client.get("/health").json()["rules_version"] is None
    r = h.apply()
    assert r.status_code == 201          # intake is not a decision; it is recorded
    cid = r.json()["clipper_id"]
    adm = h.admit(cid).json()
    assert adm["admitted"] is False and codes(adm) == {("CN-00", "RULES_NOT_IN_FORCE")}
    assert adm["unmet_lines"] == ["cn/CN-00/RULES_NOT_IN_FORCE: no Andre-approved rule version is in force; every "
                                  "decision is negative"]
    enr = h.enrol(cid).json()
    assert enr["eligible"] is False and codes(enr) == {("CN-00", "RULES_NOT_IN_FORCE")}
    ann = h.post("/cn/v1/campaigns/camp-1/rulebook-announcements", {"request_id": rid(), "version": 2},
                 caller="creative_production").json()
    assert ann["allowed"] is False and "RULES_NOT_IN_FORCE" in ann["reason"] and ann["department"] == "clipper_network"
    for path in ("/cn/v1/tiers/run", "/cn/v1/messages/flush", "/cn/v1/discipline/sync", "/cn/v1/disputes/sla-run"):
        out = h.run(path).json()
        assert out["ran"] is False and out["unmet"][0]["code"] == "RULES_NOT_IN_FORCE", path
    assert h.config().status_code == 409
    # no port was asked anything on the admission (nothing to decide without rules)
    assert "age_subject" not in h.vi.calls


def test_s2_clean_us_applicant_admitted_t0_with_disclosure_inside_quiet_hours():
    h = Harness().ready()
    cid = h.ready_applicant()
    r = h.admit(cid)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["admitted"] is True and j["unmet"] == [] and j["status"] == "active"
    assert j["rules_pinned"] is True and j["request_id"] and len(j["facts_sha256"]) == 64
    c = h.get(f"/cn/v1/clippers/{cid}").json()
    assert c["status"] == "active" and c["tier"] == "T0" and c["admission_id"] == j["admission_id"]
    welcome = [m for m in h.messages(cid) if m["template_id"] == "admission_decision"][0]
    assert "CN-16" in welcome["rule_ids_cited"]
    # queued inside the recipient-local window (11:00 in Los Angeles) and sent at once
    assert welcome["send_after"] == iso(h.clock.now()) and welcome["delivery_status"] == "sent"
    body = [b for (mid, ch, rcpt, b) in h.ports.messaging.sent if mid == welcome["message_id"]][0]
    assert "automated system" in body and "reach a person (Andre)" in body and "admitted" in body
    assert h.ledger.of_type("admission_ruling")[-1]["event_id"] == j["admission_id"]


def test_s3_each_dependency_unavailable_in_turn_gives_exactly_that_code():
    for port, attr in (("verification_integrity", "vi"), ("compliance_38", "compliance"), ("finance_31", "finance"),
                       ("legal_37", "legal")):
        h = Harness().ready()
        cid = h.ready_applicant()
        setattr(h.ports, attr, getattr(Ports(), attr))     # the fail-closed stand-in, only for this port
        j = h.admit(cid).json()
        assert j["admitted"] is False
        assert code_set(j) == {f"DEPENDENCY_UNAVAILABLE:{port}"}, (port, j["unmet_lines"])
        assert h.get(f"/cn/v1/clippers/{cid}").json()["status"] == "applicant"   # waiting, not refused


def _cert(n, platform="tiktok", views=20_000, status="certified", campaign=None):
    return Certification(f"vi-cert-{n}", f"sub-{n}", campaign or f"camp-{n % 3}", platform, status, views)


def test_s4_tier_promotion_s2_cap_and_revision():
    h = Harness().ready()
    cid = h.admitted_clipper()
    h.vi.certs[cid] = [_cert(1), _cert(2)]
    h.clock.advance(days=31)
    assert h.run("/cn/v1/tiers/run").json()["changed"] == []            # 2 clips: still T0
    h.vi.certs[cid].append(_cert(3))
    out = h.run("/cn/v1/tiers/run").json()
    assert [(c["from"], c["to"]) for c in out["changed"]] == [("T0", "T1")]   # 3rd certified clip and 30 days
    th = h.ledger.of_type("tier_changed")[-1]
    assert th["subject_id"] == cid
    # an S2 strike caps the tier at T0
    h.vi.add_strike(cid, "S2")
    h.run("/cn/v1/discipline/sync")
    assert h.get(f"/cn/v1/clippers/{cid}").json()["tier"] == "T0"
    assert h.run("/cn/v1/tiers/run").json()["changed"] == []             # still capped while the S2 is active
    # a V&I revision changes only the revised value in the inputs
    h2 = Harness().ready()
    c2 = h2.admitted_clipper()
    h2.vi.certs[c2] = [_cert(i, views=10_000, campaign=f"camp-{i % 3}") for i in range(15)]
    h2.clock.advance(days=91)
    assert h2.run("/cn/v1/tiers/run").json()["changed"][0]["to"] == "T2"
    before = len(h2.svc.st["tier_history"])
    h2.vi.certs[c2][0] = _cert(0, views=9_000, status="revised", campaign="camp-0")
    for i in range(1, 8):
        h2.vi.certs[c2][i] = _cert(i, views=9_000, status="revised", campaign=f"camp-{i % 3}")
    out = h2.run("/cn/v1/tiers/run").json()["changed"][0]
    assert out["to"] == "T1" and out["inputs"]["median_certified_views"] == 9_000
    assert out["inputs"]["certified_clips"] == 15          # revised certifications still count; only the value moved
    assert len(h2.svc.st["tier_history"]) == before + 1


def test_s5_enrolment_happy_path_kit_delivered_and_acknowledged():
    h = Harness().ready()
    cid = h.admitted_clipper()
    assert h.config().status_code == 200
    j = h.enrol(cid).json()
    assert j["eligible"] is True and j["enrolment_id"] and j["kit_delivery_id"]
    kd = h.svc.st["kits"][j["kit_delivery_id"]]
    assert kd["kit_sha256"] == KIT_SHA and kd["rulebook_version"] == 1 and kd["rate_card_version"] == "v1"
    assert h.ledger.of_type("kit_delivered")
    ack = h.post(f"/cn/v1/enrolments/{j['enrolment_id']}/kit-acknowledgment",
                 {"request_id": rid(), "kit_delivery_id": j["kit_delivery_id"], "kit_sha256": KIT_SHA, "rulebook_version": 1,
                  "rate_card_version": "v1", "rulebook_received": True, "disclosure_section_received": True},
                 caller="hub")
    assert ack.status_code == 200 and ack.json()["acknowledged_at"]
    assert h.ledger.of_type("kit_acknowledged")
    # a wrong hash is refused
    bad = h.post(f"/cn/v1/enrolments/{j['enrolment_id']}/kit-acknowledgment",
                 {"request_id": rid(), "kit_delivery_id": j["kit_delivery_id"], "kit_sha256": "0" * 64, "rulebook_version": 1,
                  "rate_card_version": "v1", "rulebook_received": True, "disclosure_section_received": True}, caller="hub")
    assert bad.status_code == 409


def test_s6_rulebook_announcement_queued_to_every_active_enrolment_and_stand_in_is_not_allowed():
    h = Harness().ready()
    a, b = h.admitted_clipper("a@example.com"), h.admitted_clipper("b@example.com")
    h.config()
    assert h.enrol(a).json()["eligible"] and h.enrol(b).json()["eligible"]
    h.ports.creative.live["camp-1"] = 2
    ann = h.post("/cn/v1/campaigns/camp-1/rulebook-announcements", {"request_id": rid(), "version": 2,
                                                                     "facts": {"changed_rules": ["NS-3"]}},
                 caller="creative_production").json()
    assert ann["allowed"] is True and ann["department"] == "clipper_network" and ann["reference"].startswith("cn-ann-")
    for cid in (a, b):
        assert any(m["template_id"] == "rulebook_announced" for m in h.messages(cid))
    assert ann["kit_delivered"] is True
    # the messaging stand-in: not delivered -> allowed false
    h.ports.messaging = Ports().messaging
    ann2 = h.post("/cn/v1/campaigns/camp-1/rulebook-announcements", {"request_id": rid(), "version": 3},
                  caller="creative_production").json()
    assert ann2["allowed"] is False and "not delivered" in ann2["reason"]


def test_s7_rate_card_change_three_days_out_is_refused():
    h = Harness().ready()
    assert h.config().status_code == 200
    r = h.config(rate_card_ref={"finance_doc_id": "rc-1", "version": "v2", "sha256": "b" * 64},
                 rate_card_effective_at=iso(h.clock.now() + timedelta(days=3)))
    assert r.status_code == 409 and r.json()["unmet"][0]["rule_id"] == "CN-17"
    assert h.ledger.of_type("network_config_refused")
    ok = h.config(rate_card_ref={"finance_doc_id": "rc-1", "version": "v2", "sha256": "b" * 64},
                  rate_card_effective_at=iso(h.clock.now() + timedelta(days=7)))
    assert ok.status_code == 200 and ok.json()["rate_card_changed"] is True


def test_s8_message_at_2230_local_is_deferred_to_0800():
    h = Harness().ready()
    # 2026-09-29 05:30 UTC = 22:30 on 2026-09-28 in Los Angeles (PDT, UTC-7)
    h.clock.at = h.clock.at.replace(day=29, hour=5, minute=30)
    r = h.apply()
    cid = r.json()["clipper_id"]
    m = [x for x in h.messages(cid) if x["template_id"] == "application_received"][0]
    assert m["delivery_status"] == "deferred" and m["send_after"] == "2026-09-29T15:00:00Z"   # 08:00 PDT
    assert h.ledger.of_type("message_deferred_quiet_hours")
    assert not h.ports.messaging.sent
    h.run("/cn/v1/messages/flush")
    assert not h.ports.messaging.sent               # still night there
    h.clock.at = h.clock.at.replace(hour=15, minute=0)
    h.run("/cn/v1/messages/flush")
    assert [s[0] for s in h.ports.messaging.sent] == [m["message_id"]]


def test_s9_second_appeal_for_one_clip_refused_and_sla_day_8_pushes():
    h = Harness().ready()
    cid = h.admitted_clipper()
    h.vi.add_strike(cid, "S1", subject_refs=("sub-77",))
    h.run("/cn/v1/discipline/sync")
    notice = [m for m in h.messages(cid) if m["template_id"] == "strike_notice"][0]
    body = {"clipper_id": cid, "notice_message_id": notice["message_id"], "subject_kind": "clip_flag",
            "subject_ref": "sub-77", "statement": "The clip was live the whole time.", "evidence_refs": ["ev-a"]}
    first = h.post("/cn/v1/disputes", {"request_id": rid(), **body}, caller="hub")
    assert first.status_code == 201 and first.json()["status"] == "open", first.text
    second = h.post("/cn/v1/disputes", {"request_id": rid(), **body}, caller="hub")
    assert second.json()["status"] == "refused"
    assert ("CN-18", "DISPUTE_ALREADY_FILED") in {(u["rule_id"], u["code"]) for u in second.json()["decision_items"]}
    assert h.ledger.of_type("dispute_refused")
    # business day 8 -> Andre is pushed (the push stand-in -> recorded as not delivered)
    h.ports.push = Ports().push
    h.clock.advance(days=7)                       # Mon + 7 calendar days = 5 business days
    assert h.run("/cn/v1/disputes/sla-run").json()["pushed"] == []
    h.clock.advance(days=3)                       # 8 business days
    pushed = h.run("/cn/v1/disputes/sla-run").json()["pushed"]
    assert pushed == [{"dispute_id": first.json()["dispute_id"], "delivered": False}]
    ev = h.ledger.of_type("dispute_sla_warning")[-1]
    assert ev["payload"]["push_delivered"] is False


def test_s10_s3_suspends_now_proposes_ban_and_only_andre_bans_then_vi_bans():
    h = Harness().ready()
    cid = h.admitted_clipper()
    h.config()
    enr = h.enrol(cid).json()
    h.vi.add_strike(cid, "S3")
    out = h.run("/cn/v1/discipline/sync").json()
    assert len(out["ban_proposals"]) == 1
    c = h.get(f"/cn/v1/clippers/{cid}").json()
    assert c["status"] == "suspended" and c["active_suspension"]["kind"] == "s3_full"
    assert h.svc.st["enrolments"][enr["enrolment_id"]]["status"] == "paused"
    assert h.vi.bans == []                              # never automatic
    prop = out["ban_proposals"][0]
    body = {"request_id": rid(), "proposal_id": prop, "decision": "approve", "note": "bought views upheld"}
    assert h.post(f"/cn/v1/clippers/{cid}/ban-decision", body, caller="scheduler").status_code == 403
    assert h.vi.bans == []
    r = h.post(f"/cn/v1/clippers/{cid}/ban-decision", body, andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text
    assert r.json()["vi_ban_propagation"] == "done" and h.vi.bans and h.vi.bans[0][0] == cid
    assert h.ledger.of_type("ban_approved_by_andre") and h.ledger.of_type("ban_propagation_requested")
    off = h.get(f"/cn/v1/clippers/{cid}/offboarding", caller="hub").json()
    assert off["trigger"] == "ban" and off["clipper_status"] == "offboarding"
