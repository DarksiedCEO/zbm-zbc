"""Register proposals/decisions/versions (spec B), controls (A.4), resolver (C.5), publish types (A.3), audit (C.9)."""

from datetime import datetime, timedelta, timezone

import pytest

from clock import iso
from controls import SEED_CONTROLS
from helpers import (ANDRE_TOKEN, NOW, Harness, client_facts, clip_facts, creator_facts, publish_facts, rid,
                     unmet_codes)
from intelligences.i05_control_monitor import test_c05 as c05
from intelligences.i07_jurisdiction import Params, resolve_person, resolve_target


def _row(h, oid):
    return dict(h.svc.current.by_id()[oid])


def _ev(url, when=NOW, excerpt="quoted text"):
    return {"source_url": url, "fetched_at": iso(when), "snapshot_sha256": "a" * 64, "normalized_text_sha256": "b" * 64,
            "quoted_excerpt": excerpt, "doc_number": None}


# --- proposal validation (B.5) -----------------------------------------------------------------

def test_verified_requires_matching_primary_evidence_and_dates(hs):
    row = {**_row(hs, "PLT-META-01"), "source_quality": "primary", "status": "verified", "verified_at": "2026-09-26"}
    url = row["source_url"]
    cases = [
        ({"proposed_row": row}, "requires evidence"),
        ({"proposed_row": row, "evidence": _ev("https://www.facebook.com/other")}, "must equal"),
        ({"proposed_row": {**row, "source_quality": "secondary"}, "evidence": _ev(url)}, "primary or vendor"),
        ({"proposed_row": {**row, "verified_at": "2026-09-20"}, "evidence": _ev(url)}, "fetched_at"),
        ({"proposed_row": {**row, "status": "expired"}, "evidence": _ev(url)}, "only to verified or unverified"),
    ]
    for body, msg in cases:
        r = hs.propose({"kind": "amend", "target_id": "PLT-META-01", **body})
        assert r.status_code == 422 and msg in r.json()["detail"], (msg, r.text)
    ok = hs.propose({"kind": "amend", "target_id": "PLT-META-01", "proposed_row": {**row, "expires_at": "2099-01-01"},
                     "evidence": _ev(url)})
    assert ok.status_code == 201, ok.text
    p = ok.json()["proposal"]
    assert p["proposed_row"]["expires_at"] == "2026-10-26"  # recomputed: platform policy, 30 days
    assert p["diff"]["status"] == {"old": "unverified", "new": "verified"}


def test_andre_verifying_the_meta_row_unblocks_instagram(hs):
    hs.approve(*[hs.memo_supersede(c) for c in ("CQ-01", "CQ-03", "CQ-11")])
    hs.activate_creator(accounts=[{"platform": "instagram", "handle_sha256": "e" * 64}])
    hs.activate_brand(platforms=("instagram",))
    assert ("PLT-META-01", "rule_not_in_force") in unmet_codes(hs.review("zbc_clip", rid(), clip_facts(platform="instagram")).json())
    row = {**_row(hs, "PLT-META-01"), "source_quality": "primary", "status": "verified", "verified_at": "2026-09-26"}
    p = hs.propose({"kind": "amend", "target_id": "PLT-META-01", "proposed_row": row, "evidence": _ev(row["source_url"])})
    hs.approve(p.json()["proposal"])
    r = hs.review("zbc_clip", rid(), clip_facts(platform="instagram")).json()
    assert r["allowed"] is True, r["unmet_lines"]


def test_ids_are_never_reused_and_targets_must_match(hs):
    row = _row(hs, "US-FTC-437-01")
    assert hs.propose({"kind": "new", "proposed_row": {**row, "status": "unverified", "verified_at": None}}).status_code == 422
    assert hs.propose({"kind": "amend", "target_id": "US-FTC-5-01",
                       "proposed_row": {**row, "status": "unverified", "verified_at": None}}).status_code == 422
    assert hs.propose({"kind": "retire", "target_id": "NOPE-1"}).status_code == 422


def test_house_rules_only_from_andre_and_jurisdiction_lists_change_through_approval(hs):
    hr05 = _row(hs, "HR-05")
    new = {**hr05, "parameters": {**hr05["parameters"], "operate": ["US", "GB", "CA", "AU"]}}
    assert hs.propose({"kind": "amend", "target_id": "HR-05", "proposed_row": new}, andre=None,
                      caller="legal_37").status_code == 422
    assert hs.rule("c-au", "client", client_facts(country="AU", region=None, targets=("AU",))).json()["allowed"] is False
    p = hs.propose({"kind": "amend", "target_id": "HR-05", "proposed_row": new})
    assert p.status_code == 201, p.text
    hs.approve(p.json()["proposal"])
    r = hs.rule("c-au", "client", client_facts(country="AU", region=None, targets=("AU",))).json()
    assert r["allowed"] is True, r["unmet_lines"]


def test_counsel_questions_are_never_verified_directly(hs):
    row = {**_row(hs, "CQ-01"), "status": "verified", "verified_at": "2026-09-26"}
    r = hs.propose({"kind": "amend", "target_id": "CQ-01", "proposed_row": row, "evidence": _ev(row["source_url"])})
    assert r.status_code == 422 and "counsel" in r.json()["detail"]


# --- decisions (B.6) ---------------------------------------------------------------------------

def test_decisions_are_atomic_stale_proposal_aborts_the_whole_call(hs):
    row = _row(hs, "US-FTC-437-01")
    a = hs.propose({"kind": "amend", "target_id": row["id"], "proposed_row": {**row, "status": "unverified", "verified_at": None,
                                                                              "title": "A"}}).json()["proposal"]
    b = hs.propose({"kind": "amend", "target_id": row["id"], "proposed_row": {**row, "status": "unverified", "verified_at": None,
                                                                              "title": "B"}}).json()["proposal"]
    hs.approve(a)
    other = hs.propose({"kind": "retire", "target_id": "US-FTC-5-01"}).json()["proposal"]
    r = hs.decide([{"proposal_id": other["proposal_id"], "content_sha256": other["content_sha256"], "decision": "approve"},
                   {"proposal_id": b["proposal_id"], "content_sha256": b["content_sha256"], "decision": "approve"}])
    assert r.status_code == 409 and "stale" in r.json()["detail"]
    assert hs.svc.version_number == 2 and hs.svc.current.by_id()["US-FTC-5-01"]["status"] == "verified"
    assert hs.svc.proposals[other["proposal_id"]]["status"] == "open"


def test_decision_shape_limits(hs):
    assert hs.decide([]).status_code == 422
    fake = {"proposal_id": "prop-x", "content_sha256": "a" * 64, "decision": "approve"}
    assert hs.decide([fake]).status_code == 404
    assert hs.decide([fake, fake]).status_code == 422
    assert hs.decide([fake] * 201).status_code == 422


def test_rejection_is_kept_with_its_note_and_no_version(hs):
    row = _row(hs, "US-FTC-437-01")
    p = hs.propose({"kind": "amend", "target_id": row["id"], "proposed_row": {**row, "status": "unverified",
                                                                              "verified_at": None}}).json()["proposal"]
    r = hs.decide([{"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "reject",
                    "note": "keep as is"}])
    assert r.status_code == 200 and r.json()["register_version"] == 1
    assert hs.svc.proposals[p["proposal_id"]]["status"] == "rejected"
    assert hs.svc.proposals[p["proposal_id"]]["note"] == "keep as is"
    assert hs.ledger.of_type("register_proposal_rejected")


def test_versions_chain_and_history(hs):
    hs.approve(hs.memo_supersede("CQ-01"))
    vs = hs.get("/compliance/v1/register/versions").json()
    assert [v["version"] for v in vs] == [1, 2]
    assert vs[0]["prev_version_sha256"] is None and vs[1]["prev_version_sha256"] == vs[0]["version_sha256"]
    one = hs.get("/compliance/v1/register/CQ-01").json()
    assert [x["status"] for x in one["history"]] == ["unverified", "superseded"]
    assert hs.get("/compliance/v1/register/NOPE-9").status_code == 404
    page = hs.get("/compliance/v1/register", gate="payout", status="unverified").json()
    assert page["register_version"] == 2 and all(r["effective_status"] == "unverified" for r in page["rows"])
    assert hs.get("/compliance/v1/register", page=2).json()["rows"]
    assert hs.get("/compliance/v1/register", gate="nope").status_code == 422


def test_control_catalog_changes_only_through_an_approved_control_proposal(hs):
    c14 = next(c for c in SEED_CONTROLS if c["control_id"] == "C-14")
    p = hs.propose({"kind": "control", "target_id": "C-14", "proposed_row": {**c14, "sla_hours": 48}})
    assert p.status_code == 201, p.text
    assert hs.get("/compliance/v1/controls/C-14").json()["sla_hours"] == 72
    hs.approve(p.json()["proposal"])
    assert hs.get("/compliance/v1/controls/C-14").json()["sla_hours"] == 48
    assert hs.svc.version_number == 1  # rows unchanged: no new register version


# --- i01 re-verification drafting and C-01 -----------------------------------------------------

def test_reverify_is_drafted_from_a_fresh_snapshot_and_needs_andre(hs):
    import test_watcher as tw
    tw.enable_watcher(hs)
    url = _row(hs, "PLT-TT-01")["source_url"]
    hs.clock.at = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
    out = hs.run_controls()
    assert out["results"]["C-01"]["result"] == "fail"  # platform rows expire 2026-10-26 with no reverify open
    hs.ports.fetcher.pages = {url: b"<p>TikTok branded content policy</p>"}
    tw.run(hs)
    out = hs.run_controls()
    drafted = [hs.svc.proposals[p] for p in out["reverify_proposals_drafted"]]
    tt = [p for p in drafted if p["target_id"] in ("PLT-TT-01", "PLT-TT-02")]
    assert tt and tt[0]["proposed_row"]["verified_at"] == "2026-10-20"
    assert tt[0]["proposed_row"]["expires_at"] == "2026-11-19"
    assert hs.svc.current.by_id()["PLT-TT-01"]["expires_at"] == "2026-10-26"  # nothing extended without Andre
    hs.approve(*tt)
    assert hs.svc.current.by_id()["PLT-TT-01"]["expires_at"] == "2026-11-19"


# --- controls (A.4) -------------------------------------------------------------------------------

def test_trust_center_exposes_only_four_fields(hs):
    tc = hs.get("/compliance/v1/trust-center").json()
    assert len(tc) == 17
    for c in tc:
        assert set(c) == {"control_id", "title", "status", "last_passed_at"}


def test_controls_fed_by_unverified_obligations_are_red_whatever_the_results(hs):
    body = {"result": "pass", "tested_at": iso(NOW), "evidence": []}
    hs.post("/compliance/v1/controls/C-07/results", {"request_id": rid(), **body}, caller="creative_production")
    hs.post("/compliance/v1/controls/C-16/results", {"request_id": rid(), **body}, andre=ANDRE_TOKEN)
    for cid in ("C-07", "C-12", "C-16"):
        c = hs.get(f"/compliance/v1/controls/{cid}").json()
        assert c["status"] == "red" and c["reason"] == "obligation_not_in_force", cid
    c08 = hs.post("/compliance/v1/controls/C-08/results", {"request_id": rid(), **body}, caller="people_43").json()
    assert c08["control"]["status"] == "green"
    assert hs.ledger.of_type("control_status_changed")


def test_future_tested_at_is_refused(hs):
    r = hs.post("/compliance/v1/controls/C-08/results", {"request_id": rid(), "result": "pass",
                                                          "tested_at": iso(NOW + timedelta(hours=2)), "evidence": []},
                caller="people_43")
    assert r.status_code == 422


def test_failing_result_turns_green_red(hs):
    body = {"tested_at": iso(NOW), "evidence": []}
    hs.post("/compliance/v1/controls/C-08/results", {"request_id": rid(), "result": "pass", **body}, caller="people_43")
    hs.post("/compliance/v1/controls/C-08/results", {"request_id": rid(), "result": "fail", **body}, caller="people_43")
    assert hs.get("/compliance/v1/controls/C-08").json()["status"] == "red"


def test_c05_unit():
    now = NOW
    good = {"ruling_id": "a", "allowed": True, "evaluated_at": iso(now), "disclosure_evidence_ref": "t"}
    bad = {**good, "ruling_id": "b", "disclosure_evidence_ref": None}
    old = {**bad, "evaluated_at": iso(now - timedelta(days=8))}
    assert c05([good, old], now)[0] is True
    assert c05([good, bad], now)[0] is False


def test_c11_red_when_the_ledger_does_not_verify(hs):
    hs.ledger.verify_ok = False
    assert hs.run_controls()["results"]["C-11"]["result"] == "fail"
    r = hs.rule("c", "client", client_facts()).json()
    assert "control_red:C-11" in {u["code"] for u in r["unmet"]}


# --- resolver (C.5) -------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def params():
    import json
    from helpers import SEED_PATH
    return Params.from_rows({r["id"]: r for r in json.loads(SEED_PATH.read_bytes())["rows"]})


@pytest.mark.parametrize("j, cls", [
    ({"declared_country": "GB", "declared_region": None, "attested": True, "attestation_ref": "a"}, "operate"),
    ({"declared_country": "US", "declared_region": "US-TX", "attested": True, "attestation_ref": "a"}, "operate"),
    ({"declared_country": "US", "declared_region": None, "attested": True, "attestation_ref": "a"}, "missing"),
    ({"declared_country": "UA", "declared_region": None, "attested": True, "attestation_ref": "a"}, "missing"),
    ({"declared_country": "UA", "declared_region": "UA-30", "attested": True, "attestation_ref": "a"}, "refuse"),
    ({"declared_country": "CA", "declared_region": None, "attested": True, "attestation_ref": "a"}, "refuse"),
    ({"declared_country": "CA", "declared_region": "CA-BC", "attested": True, "attestation_ref": "a"}, "operate"),
    ({"declared_country": "DE", "declared_region": None, "attested": True, "attestation_ref": "a"}, "conditional"),
    ({"declared_country": "US", "declared_region": "CA-ON", "attested": True, "attestation_ref": "a"}, "missing"),
    ({"declared_country": "US", "declared_region": "US-CA", "attested": False, "attestation_ref": "a"}, "missing"),
    ({"declared_country": "KP", "declared_region": None, "attested": True, "attestation_ref": "a"}, "refuse"),
    ({}, "missing"),
])
def test_resolver_person(params, j, cls):
    assert resolve_person(j, params, "p").cls == cls


def test_resolver_targets(params):
    assert resolve_target("CA", params).cls == "refuse"
    assert "HR-05" in resolve_target("CA", params).cites
    assert resolve_target("CA-QC", params).cls == "refuse" and "CA-QC-LAW25" in resolve_target("CA-QC", params).cites
    assert resolve_target("IT", params).cls == "conditional"
    assert "US-OFAC-04" in resolve_target("CU", params).cites


def test_resolve_route_records_and_notes_signal_mismatch(hs):
    r = hs.post("/compliance/v1/jurisdictions/resolve", {"request_id": rid(), "targets": ["GB", "BR"],
                                                         "person": {"declared_country": "US", "declared_region": "US-NY",
                                                                    "attested": True, "attestation_ref": "a"},
                                                         "network_country_signal": "CN"}, caller="onboarding").json()
    assert [a["class"] for a in r["answers"]] == ["operate", "operate", "refuse"]
    assert r["note"] and hs.ledger.of_type("jurisdiction_resolved")


# --- publish types (A.3) -----------------------------------------------------------------------------

def test_email_campaign_canspam_and_canada(hs):
    good = {"ad_identified": True, "postal_address_present": True, "opt_out_mechanism_present": True,
            "opt_out_honor_business_days": 10, "sender_vendor_monitored": True}
    ok = hs.review("zbm_work", "e1", publish_facts("email_campaign", canspam=good, recipient_countries=["US"],
                                                   recipients_cold=False, platforms=("email",))).json()
    assert ok["allowed"] is True, ok["unmet_lines"]
    bad = hs.review("zbm_work", "e2", publish_facts("email_campaign", canspam={**good, "opt_out_honor_business_days": 11},
                                                    recipient_countries=["US"], recipients_cold=False)).json()
    assert ("US-FTC-CANSPAM", "canspam_elements") in unmet_codes(bad)
    ca = hs.review("zbm_work", "e3", publish_facts("email_campaign", canspam=good, recipient_countries=["CA"],
                                                   recipients_cold=True, targets=("CA-ON",))).json()
    assert ("HR-08", "no_cold_outbound_ca") in unmet_codes(ca) and ("CA-CASL", "rule_not_in_force") in unmet_codes(ca)


@pytest.mark.parametrize("asset, extra, oid", [
    ("sms_campaign", {"sms": {"consent_artifacts_complete": True, "quiet_hours_local": "08:00-20:00", "max_per_24h": 3,
                              "opt_out_immediate": True}, "recipient_countries": ["US"]}, "US-FCC-TCPA-01"),
    ("subscription_checkout", {"subscription": {"terms_before_consent": True, "affirmative_unchecked_consent": True,
                                                "online_cancel_as_easy": True}}, "US-ROSCA"),
    ("chatbot", {"genai_disclosed_at_outset": True}, "US-UT-AIPA"),
])
def test_day_one_blocked_asset_types_cite_their_unverified_rows(hs, asset, extra, oid):
    r = hs.review("zbm_work", f"w-{asset}", publish_facts(asset, **extra)).json()
    assert r["allowed"] is False and (oid, "rule_not_in_force") in unmet_codes(r)


@pytest.mark.parametrize("flag, oid, code", [
    ("child_directed", "US-FTC-COPPA", "flag_blocks"),
    ("political_content", "US-POLITICAL", "rule_not_in_force"),
    ("audience_data_sale", "CQ-10", "rule_not_in_force"),
    ("child_access_likely", "US-CA-AADC", "rule_not_in_force"),
])
def test_flags_that_hold_pending_counsel(hs, flag, oid, code):
    hs.post("/compliance/v1/accessibility/checks", {"request_id": rid(), "asset_ref": "s", "asset_type": "site",
                                                    "content_sha256": "d" * 64, "owner_id": "o"}, caller="creative_production")
    r = hs.review("zbm_work", f"w-{flag}", publish_facts(flags={flag: True})).json()
    assert (oid, code) in unmet_codes(r)


def test_video_needs_captions_coverage_and_overlay_free_scan(hs):
    hs.ports.accessibility.covers_captions = False
    hs.post("/compliance/v1/accessibility/checks", {"request_id": rid(), "asset_ref": "v", "asset_type": "ad_video",
                                                    "content_sha256": "d" * 64, "owner_id": "o"}, caller="creative_production")
    r = hs.review("zbm_work", "v1", publish_facts("ad_video", platforms=("youtube",))).json()
    hr09 = [u for u in r["unmet"] if u["obligation_id"] == "HR-09"]
    assert hr09 and "captions" in hr09[0]["message"]


def test_a11y_result_is_for_the_exact_content_hash_and_expires(hs):
    hs.post("/compliance/v1/accessibility/checks", {"request_id": rid(), "asset_ref": "s", "asset_type": "site",
                                                    "content_sha256": "d" * 64, "owner_id": "o"}, caller="creative_production")
    assert hs.review("zbm_work", "s1", publish_facts()).json()["allowed"] is True
    other = hs.review("zbm_work", "s2", publish_facts(asset_content_sha256="e" * 64)).json()
    assert ("HR-09", "accessibility_pass") in unmet_codes(other)
    hs.clock.advance(days=31)
    hs.run_controls()
    stale = hs.review("zbm_work", "s3", publish_facts()).json()
    assert ("HR-09", "accessibility_pass") in unmet_codes(stale)


def test_payout_for_foreign_and_entity_payees(hs):
    hs.approve(*[hs.memo_supersede(c) for c in ("CQ-01", "CQ-03", "CQ-11")])
    hs.activate_brand()
    s = hs.screen("clipper-gb", country="GB", region=None)
    hs.ports.finance.form_kind = "w8ben"
    r = hs.rule("clipper-gb", "zbc_creator", creator_facts(s["screen_id"], country="GB", region=None, tax_form_kind="w8ben",
                                                           network_country_signal="GB")).json()
    assert r["allowed"] is True, r["unmet_lines"]
    pay = hs.review("zbc_clip", rid(), clip_facts(clipper="clipper-gb")).json()
    assert ("US-IRS-W8-VALID", "rule_not_in_force") in unmet_codes(pay)
    assert ("CQ-04", "rule_not_in_force") in unmet_codes(pay)
    assert ("US-IRS-TIN", "tin_match") not in unmet_codes(pay)


def test_entity_payee_needs_screened_owners(hs):
    hs.ports.finance.form_kind = "w9"
    s = hs.screen("clipper-co")
    missing = hs.rule("clipper-co", "zbc_creator", creator_facts(s["screen_id"], payee_type="entity")).json()
    assert ("US-OFAC-03", "fact_missing:owner_screen_ids") in unmet_codes(missing)
    o = hs.screen("owner-1", role="owner", owner_of="clipper-co")
    ok = hs.rule("clipper-co", "zbc_creator", creator_facts(s["screen_id"], payee_type="entity",
                                                            owner_screen_ids=[o["screen_id"]])).json()
    assert ok["allowed"] is True, ok["unmet_lines"]


# --- audit export (C.9) -----------------------------------------------------------------------------

def test_audit_export_orders_records_redacts_names_and_pages(hs):
    s = hs.screen("clipper-1")
    hs.rule("c", "client", client_facts())
    page = hs.get("/compliance/v1/audit/export").json()
    recs = page["records"]
    assert [r["seq"] for r in recs] == sorted(r["seq"] for r in recs)
    kinds = {r["kind"] for r in recs}
    assert {"proposal", "decision", "control_result", "screen", "ruling"} <= kinds
    scr = [r for r in recs if r["kind"] == "screen"][0]
    assert "legal_name" not in scr["record"] and scr["record"]["input_sha256"]
    assert all(isinstance(r["ledger_event_ids"], list) for r in recs)
    assert page["ledger_event_id"] and hs.ledger.of_type("audit_export_issued")
    assert "Name of clipper-1" not in str(page)
    assert s["screen_id"] in str(page)
    later = hs.get("/compliance/v1/audit/export", cursor=recs[-1]["seq"]).json()
    assert later["records"] == [] or later["records"][0]["seq"] > recs[-1]["seq"]
    assert hs.get("/compliance/v1/audit/export", since="not-a-date").status_code == 422
