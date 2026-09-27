"""Spec §G certification scenarios S1-S10 (plus the §E.1 thin-client mappings they name)."""

from __future__ import annotations

from datetime import datetime, timezone

from builders import executed_msa
from clock import FixedClock
from contract_maps import cn_doc_version, compliance_doc_version, creative_signoff, vi_takedowns
from helpers import ANDRE_TOKEN, Harness, rid, sha

POST = "a" * 64


def _music(x, platform="tiktok", present=True, source="commercial_library", track="t-1", changed=False, repost=False):
    return x.ok(x.post("/legal/v1/music/rulings", {
        "request_id": rid("mus"), "subject_kind": "zbc_clip", "subject_id": "clip-1", "platform": platform, "paid": True,
        "music": {"present": present, "source": source, "track_or_license_id": track},
        "reposted_or_reedited_by_zbc": repost, "music_changed_since_approval": changed}, caller="creative_production"))


# --- S1 ------------------------------------------------------------------------------------------------------------

WRITES = [
    ("/legal/v1/documents/sow/versions", "andre", {"request_id": "s1a", "version": "1.0", "entity": "zbm", "text": "x"}),
    ("/legal/v1/acceptances", "hub", {"request_id": "s1b", "party_ref": "clipper:c1", "signer_identity_ref": "c1",
                                      "doc_id": "clipper_agreement", "version": "1.0", "doc_sha256": POST,
                                      "presented_sha256": POST, "method": "clickwrap_unticked_box",
                                      "presentation": "link", "affirmative_act": True}),
    ("/legal/v1/requests", "hub", {"request_id": "s1c", "channel": "email", "requester_ref": "r", "kind": "question"}),
    ("/legal/v1/takedowns", "hub", {"request_id": "s1d", "target": {"kind": "platform_post", "post_ref_sha256": POST,
                                                                     "platform": "tiktok"},
                                    "elements": {e: True for e in ("signature", "work_identified", "material_located",
                                                                   "contact", "good_faith_statement", "perjury_statement")}}),
    ("/legal/v1/filings", "andre", {"request_id": "s1e", "entity": "zbc", "kind": "dmca_agent_designation",
                                    "filed_on": "2026-10-01"}),
    ("/legal/v1/memos", "andre", {"request_id": "s1f", "counsel_ref": "eng-1", "memo_date": "2026-10-01",
                                  "content_b64": "bWVtbw==", "cites": {}}),
    ("/legal/v1/jobs/obligations/run", "scheduler", {"request_id": "s1g"}),
]


def test_s1_rules_not_approved_every_action_refused(h):
    for path, who, body in WRITES:
        r = h.post(path, body, andre=ANDRE_TOKEN) if who == "andre" else h.post(path, body, caller=who)
        assert r.status_code == 409, (path, r.status_code, r.text[:300])
        assert r.json()["detail"] == "RULES_NOT_IN_FORCE" and r.json()["reasons"][0]["rule_id"] == "LG-00"
    cur = h.ok(h.get("/legal/v1/documents/clipper_agreement/current", caller="compliance_38"))
    assert cur["available"] is False and "LG-00" in cur["reason"]
    assert h.ok(h.get("/legal/v1/takedowns/count", caller="verification_integrity", post_ref_sha256=POST))["available"] is False
    s = h.ok(h.post("/legal/v1/signoffs", {"request_id": "s1s", "topic": "ai_generative_fill", "subject_id": "w1",
                                            "facts": {"asset_ids": ["a1"]}}, caller="creative_production"))
    assert s["allowed"] is False and "RULES_NOT_IN_FORCE" in s["reason"]
    assert _music(h, present=False, source="none", track=None)["allowed"] is False


# --- S2, S3 --------------------------------------------------------------------------------------------------------

def test_s2_approved_clipper_agreement_is_current_and_thin_clients_answer_available(he):
    v = he.approve_doc("clipper_agreement", "ZBC clipper agreement v1.0 text")
    r = he.get("/legal/v1/documents/clipper_agreement/current", caller="clipper_network")
    body = he.ok(r)
    assert body["available"] and body["current_version"] == "1.0" and body["doc_sha256"] == v["sha256"] == sha(
        "ZBC clipper agreement v1.0 text")
    c = compliance_doc_version(he.get("/legal/v1/documents/clipper_agreement/current", caller="compliance_38"))
    assert c.available and c.current_version == "1.0"
    n = cn_doc_version(r)
    assert n.available and n.version == "1.0" and n.doc_sha256 == v["sha256"]


def test_s3_review_by_passes_no_current_version(he):
    he.approve_doc("clipper_agreement", "ZBC clipper agreement v1.0 text")
    he.clock.advance(days=89)
    assert compliance_doc_version(he.get("/legal/v1/documents/clipper_agreement/current", caller="compliance_38")).available
    he.clock.advance(days=2)
    r = he.get("/legal/v1/documents/clipper_agreement/current", caller="clipper_network")
    body = he.ok(r)
    assert body["current_version"] is None and "NO_CURRENT_VERSION" in body["reason"]
    assert not compliance_doc_version(r).available and not cn_doc_version(r).available


def test_s3_unapproved_draft_is_never_current(he):
    he.upload("clipper_agreement", "draft only")
    body = he.ok(he.get("/legal/v1/documents/clipper_agreement/current", caller="clipper_network"))
    assert body["current_version"] is None


# --- S4 ------------------------------------------------------------------------------------------------------------

def test_s4_clickwrap_insufficient_until_cq19_then_sufficient_for_new_acceptances(he):
    v = he.approve_doc("clipper_agreement", "ZBC clipper agreement v1.0 text")
    a1 = he.clickwrap("clipper_agreement", "1.0", v["sha256"], party="clipper:cn-clp-1")
    assert a1["evidence_sufficient"] is False and a1["reasons"][0]["cq_id"] == "CQ-19"
    he.verify_cq("CQ-19")
    a2 = he.clickwrap("clipper_agreement", "1.0", v["sha256"], party="clipper:cn-clp-2")
    assert a2["evidence_sufficient"] is True
    old = he.ok(he.get(f"/legal/v1/acceptances/{a1['acceptance_id']}", caller="finance_31"))
    assert old["evidence_sufficient"] is False          # a record never changes after the fact
    assert set(old) == {"acceptance_id", "doc_id", "version", "doc_sha256", "accepted_at", "method",
                        "evidence_sufficient", "party_ref", "rules_pinned"}


# --- S5 ------------------------------------------------------------------------------------------------------------

def test_s5_executed_msa_creates_bound_obligations_and_a_missed_one_alerts_andre():
    x = Harness(wired=True)
    x.approve_rules()
    x.engage()
    fill, acc = executed_msa(x)
    assert acc["evidence_sufficient"] is True and len(acc["obligation_ids"]) == 2
    obs = {o["obligation_code"]: o for o in x.ok(x.get("/legal/v1/obligations", party_ref="client:acme"))["items"]}
    assert obs["renewal_notice"]["due"] == "2027-08-31"                      # end_date 2027-09-30 - 30 days
    assert obs["payment_terms"]["due"] == "2026-10-16"      # 2026-10-01 + 10 business days (Columbus Day skipped)
    assert obs["payment_terms"]["owner_department"] == "finance_31"
    x.clock.advance(days=11)                                                  # 2026-10-12: not yet
    assert x.job("obligations")["summary"] == {"due_soon": 0, "missed": 0}
    x.clock.advance(days=1)                                                   # 2026-10-13: within lead (3 days)
    assert x.job("obligations")["summary"] == {"due_soon": 1, "missed": 0}
    x.clock.advance(days=4)                                                   # 2026-10-17: past due
    assert x.job("obligations")["summary"] == {"due_soon": 0, "missed": 1}
    o = x.ok(x.get("/legal/v1/obligations", status="missed"))["items"][0]
    assert o["obligation_code"] == "payment_terms"
    assert any(k == "obligation_missed" for k, _, _ in x.ports.push.sent)     # Andre alerted
    assert x.ledger.of_type("obligation_missed")
    # done only by the owner department, with evidence
    r = x.post(f"/legal/v1/obligations/{obs['renewal_notice']['obligation_id']}/done",
               {"request_id": rid(), "evidence": {"ref": "notice-1", "sha256": "b" * 64}}, caller="finance_31")
    assert r.status_code == 409 and r.json()["detail"] == "NOT_OWNER"


def test_s5_no_obligation_from_an_insufficient_acceptance(he):
    fill, acc = executed_msa(he, verify_cq19=False)
    assert acc["evidence_sufficient"] is False and acc["obligation_ids"] == []
    assert he.ok(he.get("/legal/v1/obligations"))["items"] == []


# --- S6 ------------------------------------------------------------------------------------------------------------

def test_s6_subpoena_is_s1_same_day_counsel_hold_and_return_date(hr):
    m = hr.ok(hr.post("/legal/v1/requests", {"request_id": rid(), "channel": "mail", "requester_ref": "court-1",
                                              "kind": "subpoena", "deadlines": {"return_date": "2026-10-20"},
                                              "subject_refs": ["clipper:cn-clp-9"], "custodians": ["andre"]},
                      caller="hub"), 201)
    assert (m["severity"], m["route"]) == ("S1", "counsel_same_day")
    assert m["deadlines"]["return_date"] == "2026-10-20" and len(m["hold_ids"]) == 1
    assert "SUBPOENA_NOTHING_PRODUCED" in {r["code"] for r in m["reasons"]}
    assert hr.ok(hr.get("/legal/v1/holds/check", subject_ref="clipper:cn-clp-9"))["held"] is True
    assert hr.ledger.of_type("hold_issued") and hr.ledger.of_type("crossing_counsel_channel_requested")
    r = hr.post("/legal/v1/requests", {"request_id": rid(), "channel": "mail", "requester_ref": "c", "kind": "subpoena"},
                caller="hub")
    assert r.status_code == 422                                              # a subpoena needs its return date


# --- S7 ------------------------------------------------------------------------------------------------------------

def _valid_notice(x):
    return x.ok(x.post("/legal/v1/takedowns", {
        "request_id": rid("td"), "target": {"kind": "platform_post", "post_ref_sha256": POST, "platform": "tiktok"},
        "elements": {e: True for e in ("signature", "work_identified", "material_located", "contact",
                                       "good_faith_statement", "perjury_statement")}}, caller="hub"), 201)


def _at(y, mo, d):
    return FixedClock(datetime(y, mo, d, 20, 0, tzinfo=timezone.utc))      # 12:00/13:00 in Los Angeles


def test_s7_counter_notice_window_thanksgiving_example():
    x = Harness(clock=_at(2026, 11, 20))
    x.approve_rules()
    n = _valid_notice(x)
    cn = x.ok(x.post(f"/legal/v1/takedowns/{n['notice_id']}/counter-notice", {"request_id": rid()}, caller="hub"))
    assert (cn["received_date"], cn["restore_not_before"], cn["restore_not_after"]) == \
        ("2026-11-20", "2026-12-07", "2026-12-11")


def test_s7_monday_receipt_without_holidays_is_plus_14_and_plus_18_calendar_days():
    x = Harness(clock=_at(2027, 3, 1))
    x.approve_rules()
    n = _valid_notice(x)
    cn = x.ok(x.post(f"/legal/v1/takedowns/{n['notice_id']}/counter-notice", {"request_id": rid()}, caller="hub"))
    assert (cn["restore_not_before"], cn["restore_not_after"]) == ("2027-03-15", "2027-03-19")


# --- S8 ------------------------------------------------------------------------------------------------------------

def test_s8_dmca_agent_expires_three_years_later_with_a_60_day_alert(hr):
    f = hr.ok(hr.apost("/legal/v1/filings", {"request_id": rid(), "entity": "zbc", "kind": "dmca_agent_designation",
                                             "filed_on": "2026-10-01", "reference": "DMCA-123"}), 201)
    assert (f["status"], f["expires_on"], f["alert_lead_days"]) == ("filed", "2029-10-01", 60)
    assert "USD 6" in f["fee_reference"]
    hr.clock.at = datetime(2029, 8, 1, 17, tzinfo=timezone.utc)
    assert hr.job("filings")["summary"]["due_soon"] == 0
    hr.clock.at = datetime(2029, 8, 2, 17, tzinfo=timezone.utc)                 # 60 days before 2029-10-01
    assert hr.job("filings")["summary"]["due_soon"] == 1
    hr.clock.at = datetime(2029, 10, 1, 17, tzinfo=timezone.utc)
    s = hr.job("filings")["summary"]
    assert s["lapsed"] == 1
    fl = hr.ok(hr.get("/legal/v1/filings"))["items"][0]
    assert fl["status"] == "lapsed" and fl["matter_id"]


# --- S9 ------------------------------------------------------------------------------------------------------------

def test_s9_music_policy(he):
    assert _music(he, present=False, source="none", track=None)["allowed"] is True
    r = _music(he)
    assert r["allowed"] is False and [(x["code"], x["cq_id"]) for x in r["reasons"]] == [("HELD_PENDING_COUNSEL", "CQ-21")]
    assert {x["code"] for x in _music(he, platform="youtube")["reasons"]} == {"PLATFORM_LIBRARY_UNVERIFIED",
                                                                                "HELD_PENDING_COUNSEL"}
    assert _music(he, source="licensed")["reasons"][0]["code"] == "MUSIC_LICENSED_BLOCKED"
    he.verify_cq("CQ-21")
    ok = _music(he)
    assert ok["allowed"] is True and ok["review_label"].startswith("counsel_memo:")
    assert _music(he, platform="youtube")["allowed"] is False               # YouTube library rule unverified
    assert _music(he, track=None)["allowed"] is False
    swap = _music(he, changed=True, repost=True)
    assert swap["allowed"] is False and swap["reasons"][0]["code"] == "MUSIC_CHANGED"   # always, even after CQ-21
    assert _music(he, present=False, source="none", track=None, changed=True)["allowed"] is False


# --- S10 -----------------------------------------------------------------------------------------------------------

def _signoff(x, assets=("a1",), topic="ai_generative_fill"):
    return x.post("/legal/v1/signoffs", {"request_id": rid("sg"), "topic": topic, "subject_id": "camp-1",
                                          "facts": {"asset_ids": list(assets)}}, caller="creative_production")


def test_s10_creative_signoff_false_until_the_topic_memo(he):
    g = creative_signoff(_signoff(he))
    assert g.allowed is False and g.department == "legal_37" and "SIGNOFF_NOT_VERIFIED" in g.reason
    assert creative_signoff(_signoff(he, topic="unknown_topic")).allowed is False
    memo = he.memo(cites={"signoff_topics": ["ai_generative_fill"]},
                   signoff_scopes={"ai_generative_fill": {"asset_ids": ["a1", "a2"]}})
    g = creative_signoff(_signoff(he, ("a1",)))
    assert g.allowed is True and g.reference == memo["memo_id"]
    assert creative_signoff(_signoff(he, ("a1", "a9"))).allowed is False        # outside the standing scope
    r = _signoff(he, ("a2",))
    assert r.json()["facts_sha256"] and r.json()["request_id"] and r.json()["rules_pinned"] is True
    he.clock.advance(days=91)                                                   # memo date + 90 days
    assert creative_signoff(_signoff(he)).allowed is False


def test_vi_takedown_count_mapping(hr):
    assert vi_takedowns(hr.get("/legal/v1/takedowns/count", caller="verification_integrity",
                               post_ref_sha256=POST)) .notices == 0
    n = _valid_notice(hr)
    a = vi_takedowns(hr.get("/legal/v1/takedowns/count", caller="verification_integrity", post_ref_sha256=POST))
    assert a.available and a.notices == 1
    hr.ok(hr.post(f"/legal/v1/takedowns/{n['notice_id']}/withdraw", {"request_id": rid()}, caller="hub"))
    assert vi_takedowns(hr.get("/legal/v1/takedowns/count", caller="verification_integrity",
                               post_ref_sha256=POST)).notices == 0
    assert not vi_takedowns(hr.get("/legal/v1/takedowns/count", caller="creative_production",
                                   post_ref_sha256=POST)).available          # wrong caller -> 403 -> negative


def test_day_one_effect_nothing_current_nothing_sufficient(hr):
    for d in ("clipper_agreement", "privacy_policy", "terms", "zbc_order_form", "client_msa"):
        assert hr.ok(hr.get(f"/legal/v1/documents/{d}/current", caller="compliance_38"))["current_version"] is None
    r = hr.apost("/legal/v1/memos", {"request_id": rid(), "counsel_ref": "eng-x", "memo_date": "2026-10-01",
                                     "content_b64": "bWVtbw==", "cites": {"cq_ids": ["CQ-21"]},
                                     "answers": [{"cq_id": "CQ-21", "resolution": "verified_rule"}]})
    assert r.status_code == 409 and r.json()["detail"] == "ENGAGEMENT_NOT_APPROVED"

