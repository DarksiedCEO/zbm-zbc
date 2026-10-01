"""Details: business-day calendar, filings windows, envelopes, the Compliance thin client, holds, obligations
entered from a memo, document-version rules."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from bizdays import BusinessCalendar, HolidaysUnknown
from builders import MSA_TEXT, MSA_VARS, playbook
from clock import FixedClock
from compliance_client import HttpCompliance
from helpers import COUNSEL_REF, Harness, b64, rid, sha
from intelligences import i08_filings as i08

SEED = Path(__file__).resolve().parents[1] / "seed"
CAL = BusinessCalendar((SEED / "us_federal_holidays.json").read_bytes())


# --- calendar ------------------------------------------------------------------------------------------------------

def test_holiday_seed_observance_shifts():
    hol = CAL.holidays
    assert date(2026, 7, 3) in hol and date(2026, 7, 4) not in hol          # Saturday -> Friday
    assert date(2027, 12, 24) in hol and date(2027, 12, 31) in hol          # Christmas 2027 and New Year 2028 (Saturdays)
    assert date(2027, 6, 18) in hol                                          # Juneteenth 2027 is a Saturday
    assert date(2028, 1, 17) in hol                                          # MLK: third Monday
    assert len([d for d in hol if d.year == 2026]) == 11


def test_business_days_outside_the_seeded_range_fail_closed(hr):
    with pytest.raises(HolidaysUnknown):
        CAL.add(date(2030, 12, 27), 10)
    x = Harness(clock=FixedClock(datetime(2030, 12, 27, 20, tzinfo=timezone.utc)))
    x.approve_rules()
    n = x.ok(x.post("/legal/v1/takedowns", {"request_id": rid(), "target": {"kind": "platform_post",
                    "post_ref_sha256": "a" * 64, "platform": "tiktok"},
                    "elements": {e: True for e in ("signature", "work_identified", "material_located", "contact",
                                                   "good_faith_statement", "perjury_statement")}}, caller="hub"), 201)
    r = x.post(f"/legal/v1/takedowns/{n['notice_id']}/counter-notice", {"request_id": rid()}, caller="hub")
    assert r.status_code == 409 and r.json()["detail"] == "HOLIDAYS_UNKNOWN"


def test_backward_business_days():
    assert CAL.add(date(2026, 12, 7), -10) == date(2026, 11, 20)


# --- filings -------------------------------------------------------------------------------------------------------

def test_filing_windows():
    assert i08.sos_window(1, 2027) == (date(2026, 8, 1), date(2027, 1, 31))   # SOS example: January -> Aug 1 .. Jan 31
    assert i08.sos_window(7, 2027) == (date(2027, 2, 1), date(2027, 7, 31))
    d = i08.compute("tm_section_8", {"registration_date": "2027-03-10"}, 3)
    assert (d["window_opens"], d["window_closes"]) == ("2032-03-10", "2033-03-10") and "325" in d["fee_reference"]
    d = i08.compute("tm_section_9", {"registration_date": "2028-02-29"}, 3)
    assert (d["window_opens"], d["window_closes"]) == ("2037-02-28", "2038-02-28")
    assert i08.compute("sos_statement_of_information", {"formation_month": 1, "due_year": 2027}, 3)["fee_reference"] == \
        "UNVERIFIED"
    with pytest.raises(ValueError):
        i08.compute("fbn_statement", {}, 3)


def test_trademark_filing_ready_needs_cq27_and_lapse_opens_a_matter(he):
    f = he.ok(he.apost("/legal/v1/filings", {"request_id": rid(), "entity": "zbm", "kind": "tm_statement_of_use",
                                             "window_closes": "2026-10-10"}), 201)
    r = he.apost(f"/legal/v1/filings/{f['filing_id']}/ready", {"request_id": rid()})
    assert r.status_code == 409 and r.json()["reasons"][0]["cq_id"] == "CQ-27"
    he.verify_cq("CQ-27")
    assert he.ok(he.apost(f"/legal/v1/filings/{f['filing_id']}/ready", {"request_id": rid()}))["status"] == "ready"
    he.clock.advance(days=11)
    assert he.job("filings")["summary"]["lapsed"] == 1
    fl = [x for x in he.ok(he.get("/legal/v1/filings"))["items"] if x["filing_id"] == f["filing_id"]][0]
    m = he.ok(he.get(f"/legal/v1/matters/{fl['matter_id']}"))
    assert m["route"] == "counsel_standard" and m["kind"] == "filing_lapsed"


def test_jobs_run_once_per_day(hr):
    a = hr.job("filings")
    b = hr.job("filings")
    assert a["already_ran"] is False and b["already_ran"] is True
    hr.clock.advance(days=1)
    assert hr.job("filings")["already_ran"] is False


# --- envelopes -----------------------------------------------------------------------------------------------------

def test_envelope_unavailable_by_default_and_excluded_doc_types(he):
    v = he.approve_doc("clipper_agreement", "clipper agreement text")
    r = he.ok(he.apost("/legal/v1/envelopes", {"request_id": rid(), "doc_id": "clipper_agreement", "version": "1.0",
                                               "party_ref": "clipper:c1", "signer_refs": ["c1"]}))
    assert r["created"] is False and r["reasons"][0]["code"] == "ESIGN_UNAVAILABLE"
    he.approve_doc("privacy_policy", "privacy policy text", entity="zbm")
    r = he.apost("/legal/v1/envelopes", {"request_id": rid(), "doc_id": "privacy_policy", "version": "1.0",
                                         "party_ref": "client:c1", "signer_refs": ["c1"]})
    assert r.status_code == 409 and r.json()["detail"] == "ESIGN_DOC_TYPE_EXCLUDED"
    _ = v


def test_envelope_completion_with_a_wired_provider_stores_the_certificate():
    x = Harness(wired=True)
    x.approve_rules()
    x.engage()
    v = x.approve_doc("clipper_agreement", "clipper agreement text")
    e = x.ok(x.apost("/legal/v1/envelopes", {"request_id": rid(), "doc_id": "clipper_agreement", "version": "1.0",
                                             "party_ref": "clipper:c1", "signer_refs": ["c1"]}))
    assert e["created"] is True
    cert = b"provider audit certificate"
    done = x.ok(x.post("/legal/v1/esign/events", {"request_id": rid(), "envelope_id": e["envelope_id"],
                                                   "status": "completed", "signed_document_sha256": v["sha256"],
                                                   "certificate_b64": b64(cert)}, caller="esign_gateway"))
    assert done["evidence_sufficient"] is True and done["method"] == "esign_envelope"
    assert x.svc.blobs.get(sha(cert)) == cert
    e2 = x.ok(x.apost("/legal/v1/envelopes", {"request_id": rid(), "doc_id": "clipper_agreement", "version": "1.0",
                                              "party_ref": "clipper:c2", "signer_refs": ["c2"]}))
    bad = x.ok(x.post("/legal/v1/esign/events", {"request_id": rid(), "envelope_id": e2["envelope_id"],
                                                  "status": "completed", "signed_document_sha256": "0" * 64,
                                                  "certificate_b64": b64(b"c2")}, caller="esign_gateway"))
    assert bad["evidence_sufficient"] is False and bad["reasons"][0]["code"] == "SIGNED_HASH_MISMATCH"


# --- documents ---------------------------------------------------------------------------------------------------

def test_document_version_rules(he):
    he.upload("sow", "sow 1", entity="zbm")
    # fix 18 (AEGIS N17-5): the number is Legal's. A caller-supplied version is refused (before: 1.0 again -> 409,
    # 0.9 -> 409, and any caller could pick 9999.9999 and freeze the document)
    assert he.apost("/legal/v1/documents/sow/versions", {"request_id": rid(), "version": "1.0", "entity": "zbm",
                                                         "text": "again"}).status_code == 422
    assert he.upload("sow", "again", "1.1", entity="zbm")["version"] == "1.1"      # next minor, assigned
    r = he.apost("/legal/v1/documents/sow/versions", {"request_id": rid(), "bump": "major", "entity": "zbm",
                                                      "text": "Hello {{name}}"})
    assert r.status_code == 422                                          # placeholder without a schema entry
    assert he.apost("/legal/v1/documents/unknown_doc/versions", {"request_id": rid(),
                                                                 "entity": "zbm", "text": "x"}).status_code == 404
    assert he.post("/legal/v1/documents/soi_zbc/versions", {"request_id": rid(), "entity": "zbc",
                                                            "variables": {}}, caller="scheduler").status_code == 404
    assert he.post("/legal/v1/documents/sow/versions", {"request_id": rid(), "entity": "zbm", "text": "x"},
                   caller="scheduler").status_code == 403                # only Andre uploads
    r = he.ok(he.get("/legal/v1/documents/nope_doc/current"))
    assert r["available"] is False and "DOCUMENT_UNKNOWN" in r["reason"]
    assert he.client.get("/legal/v1/documents/sow/versions/1.0/text",
                         headers=he.headers("scheduler")).status_code == 403


def test_template_fill_needs_an_approved_template_and_typed_variables(he):
    r = he.post("/legal/v1/documents/client_msa/versions", {"request_id": rid(), "entity": "zbm", "party_ref": "client:a",
                "variables": {"client_name": "A", "end_date": "2027-01-01"}}, caller="scheduler")
    assert r.status_code == 409 and r.json()["detail"] == "NO_APPROVED_TEMPLATE"
    he.approve_doc("client_msa", MSA_TEXT, "1.0", "zbm", [("MSA-RENEW-01", "standard")], MSA_VARS)
    for bad in ({"client_name": "A"}, {"client_name": "A", "end_date": "soon"},
                {"client_name": "A", "end_date": "2027-01-01", "extra": "x"}, {"client_name": "A\nB", "end_date": "2027-01-01"}):
        r = he.post("/legal/v1/documents/client_msa/versions", {"request_id": rid(), "entity": "zbm",
                    "party_ref": "client:acme", "variables": bad}, caller="scheduler")
        assert r.status_code == 422, bad
    r = he.post("/legal/v1/documents/client_msa/versions", {"request_id": rid(), "entity": "zbm",
                "variables": {"client_name": "Acme Co", "end_date": "2027-01-01"}}, caller="scheduler")
    assert r.status_code == 422                                          # fix 18: a fill names its party
    ok = he.fill("client_msa", {"client_name": "Acme Co", "end_date": "2027-01-01"}, "client:acme", expect="1.1")
    assert ok["sha256"] == sha(MSA_TEXT.replace("{{client_name}}", "Acme Co").replace("{{end_date}}", "2027-01-01"))
    assert ok["review_label"] == "counsel_template:client_msa@1.0"


def test_counsel_review_packages_and_records_not_delivered(he):
    he.upload("clipper_agreement", "draft")
    r = he.ok(he.apost("/legal/v1/documents/clipper_agreement/versions/1.0/counsel-review",
                       {"request_id": rid(), "question_text": "Is the clause list complete?"}))
    assert r["delivered"] is False and r["status"] == "counsel_review"
    assert he.ledger.of_type("counsel_review_packaged") and he.ledger.of_type("crossing_counsel_channel_requested")


def test_retire_and_supersede(he):
    v1 = he.approve_doc("terms", "terms v1", entity="zbm")
    v2 = he.approve_doc("terms", "terms v2", "2.0", entity="zbm")
    assert he.ok(he.get("/legal/v1/documents/terms/current"))["current_version"] == "2.0"
    he.ok(he.apost("/legal/v1/documents/terms/versions/2.0/decision", {"request_id": rid(), "decision": "retire",
                                                                        "version_sha256": v2["sha256"]}))
    assert he.ok(he.get("/legal/v1/documents/terms/current"))["current_version"] == "1.0"
    _ = v1


def test_future_effective_date_is_not_current_until_then(he):
    he.approve_doc("sow", "sow", entity="zbm", effective_at="2026-10-05T00:00:00Z")
    assert he.ok(he.get("/legal/v1/documents/sow/current"))["current_version"] is None
    he.clock.advance(days=4)
    assert he.ok(he.get("/legal/v1/documents/sow/current"))["current_version"] == "1.0"


# --- obligations entered from a memo, waive ---------------------------------------------------------------------------

def test_counterparty_paper_obligation_only_from_a_memo_and_waive(he):
    body = {"request_id": rid(), "memo_id": "lg-mem-00000000000000000000000000", "doc_id": "cp_vendor_msa",
            "version": "1.0", "party": "zbm", "counterparty_ref": "counterparty:vendor-1",
            "obligation_code": "insurance_notice_of_claim", "due": "2026-12-01", "alert_lead_days": 10,
            "owner_department": "andre"}
    r = he.apost("/legal/v1/obligations", body)
    assert r.status_code == 409 and r.json()["detail"] == "MEMO_DOES_NOT_CITE"
    memo = he.memo(cites={"doc_versions": ["cp_vendor_msa@1.0"]})
    o = he.ok(he.apost("/legal/v1/obligations", {**body, "request_id": rid(), "memo_id": memo["memo_id"]}), 201)
    w = he.ok(he.apost(f"/legal/v1/obligations/{o['obligation_id']}/waive", {"request_id": rid()}))
    assert w["status"] == "waived"


# --- holds -------------------------------------------------------------------------------------------------------------

def test_hold_with_wired_ports_freezes_and_notices_and_renotices():
    x = Harness(wired=True)
    x.approve_rules()
    x.engage()
    x.approve_doc("lit_hold_notice", "Preserve everything about the matter.", entity="zbm")
    m = x.ok(x.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "r",
                                           "kind": "data_incident", "custodians": ["andre", "clipper:cn-clp-1"],
                                           "systems": ["email", "drive", "finance_log", "legal_store"]}, caller="hub"), 201)
    assert m["route"] == "counsel_same_day"
    hold = x.svc.holds[m["hold_ids"][0]]
    assert hold["freeze"] == {"email": "frozen", "drive": "frozen", "finance_log": "frozen", "legal_store": "frozen"}
    assert hold["sent_at"] and hold["notice_template"]["doc_id"] == "lit_hold_notice"
    ack = x.ok(x.post(f"/legal/v1/holds/{hold['hold_id']}/acknowledgments", {"request_id": rid(),
                                                                            "custodian": "clipper:cn-clp-1"}, caller="hub"))
    assert ack["acknowledged"] == 1
    assert x.post(f"/legal/v1/holds/{hold['hold_id']}/acknowledgments", {"request_id": rid(), "custodian": "stranger"},
                  caller="hub").status_code == 422
    x.clock.advance(days=90)
    assert x.job("holds-renotice")["summary"] == {"renoticed": 1}


def test_hold_with_stand_ins_records_not_frozen_and_alerts(hr):
    m = hr.ok(hr.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "r",
                                             "kind": "agency_letter"}, caller="hub"), 201)
    hold = hr.svc.holds[m["hold_ids"][0]]
    assert hold["freeze"]["email"] == "not_frozen" and hold["freeze"]["legal_store"] == "frozen"
    assert hold["notice_template"] is None and hold["sent_at"] is None
    assert hr.ledger.of_type("crossing_push_requested")


def test_triage_matrix(hr):
    def t(kind, **kw):
        return hr.ok(hr.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "r",
                                                    "kind": kind, **kw}, caller="hub"), 201)
    assert (t("ip_claim")["route"], t("ip_claim")["hold_ids"]) == ("counsel_standard", [])
    r = t("ip_claim", facts={"deadline_stated": True})
    assert r["likelihood"] == "L2" and r["hold_ids"]
    assert t("contract_dispute", disputed_amount_usd="4999.99")["route"] == "playbook_lane"
    assert t("contract_dispute", disputed_amount_usd="5000.00")["severity"] == "S4"
    amb = t("contract_dispute")
    assert amb["route"] == "counsel_same_day" and amb["hold_ids"]              # amount unknown -> ambiguous -> escalate
    assert t("routine_contract")["route"] == "playbook_lane"
    ca = t("ip_claim", facts={"class_action_threat": True})
    assert ca["route"] == "counsel_same_day" and ca["hold_ids"]
    d = t("privacy_request", facts={"dsar": True})
    assert d["deadlines"]["dsar_respond_by"] == "2026-11-15" and d["deadlines"]["dsar_max"] == "2026-12-30"
    assert d["deadlines"]["dsar_confirm_by"] == "2026-10-16"                  # 10 business days, Columbus Day skipped
    assert "DSAR_CLOCK_UNVERIFIED" in {x["code"] for x in d["reasons"]}


# --- the Compliance thin client ---------------------------------------------------------------------------------------

def _client(handler, timeout=10.0):
    return HttpCompliance("http://compliance.test", "svc-token", "caller-token", transport=httpx.MockTransport(handler),
                          timeout=timeout)


def test_thin_client_created_refused_unavailable():
    seen = []

    def ok(req):
        seen.append(req)
        return httpx.Response(201, json={"proposal": {"proposal_id": "prop-ABC"}})
    a = _client(ok).create_proposal("lg37-x", {"kind": "supersede", "target_id": "CQ-11"})
    assert (a.status, a.compliance_proposal_id) == ("created", "prop-ABC")
    assert seen[0].headers["X-Compliance-Caller-Token"] == "caller-token"
    assert json.loads(seen[0].content)["request_id"] == "lg37-x"
    assert _client(lambda r: httpx.Response(422, json={"detail": "x"})).create_proposal("r", {}).status == "refused"
    calls = []

    def flaky(req):
        calls.append(1)
        return httpx.Response(503)
    assert _client(flaky).create_proposal("r", {}).status == "unavailable" and len(calls) == 2
    assert _client(lambda r: httpx.Response(201, text="not json")).create_proposal("r", {}).status == "unavailable"
    assert _client(lambda r: httpx.Response(201, json={"proposal": {"proposal_id": "a b"}})).create_proposal(
        "r", {}).status == "unavailable"
    assert _client(lambda r: httpx.Response(302, headers={"location": "http://evil"})).create_proposal(
        "r", {}).status == "unavailable"


def test_thin_client_row_reads():
    good = {"register_version": 3, "row": {"id": "CQ-11-M", "effective_status": "verified"}}
    assert _client(lambda r: httpx.Response(200, json=good)).row("CQ-11-M").effective_status == "verified"
    other = {"register_version": 3, "row": {"id": "OTHER", "effective_status": "verified"}}
    assert _client(lambda r: httpx.Response(200, json=other)).row("CQ-11-M").available is False
    assert _client(lambda r: httpx.Response(200, json=good)).row("bad id").available is False


def test_thin_client_wall_clock_deadline():
    """Wave 25 (scout B M2): ordered by state, not by a wall-clock bound a starved runner can break. The peer does not
    answer until the test lets it: the 1.0 s deadline must return `unavailable` while the peer still holds the request
    (0f017a7: a 3 s peer and `< 2.0`)."""
    import threading
    inside, release = threading.Event(), threading.Event()

    def slow(req):
        inside.set()
        release.wait(120)
        return httpx.Response(201, json={"proposal": {"proposal_id": "late"}})
    got = {}
    t = threading.Thread(target=lambda: got.update(a=_client(slow, timeout=1.0).create_proposal("r", {})))
    t.start()
    t.join(60)                                        # a bound on a stall, never on the answer's speed
    returned_while_held = not t.is_alive() and inside.is_set() and not release.is_set()
    release.set()
    t.join(60)
    assert returned_while_held, "the 1.0 s deadline waited for the peer's answer"
    assert got["a"].status == "unavailable"


# --- memos and the register ----------------------------------------------------------------------------------------

def test_memo_view_and_register_rows(he):
    m = he.memo(cites={"cq_ids": ["CQ-21", "VI-CQ-05"]}, answers=[{"cq_id": "CQ-21", "resolution": "verified_rule"},
                                                                    {"cq_id": "VI-CQ-05", "resolution": "blocks_stay"}])
    v = he.ok(he.get(f"/legal/v1/memos/{m['memo_id']}"))
    assert v["memo_sha256"] == m["memo_sha256"] and "content_b64" not in v and v["entered_by"] == "andre"
    assert he.ok(he.get("/legal/v1/register/VI-CQ-05"))["status"] == "unverified"
    rows = he.ok(he.get("/legal/v1/register"))["rows"]
    assert len(rows) == 56 and {r["cq_id"] for r in rows} >= {"CQ-01", "CQ-27", "VI-CQ-06", "CN-CQ-08", "FIN-CQ-15"}
    vi = he.memo(cites={"cq_ids": ["VI-CQ-01"]}, answers=[{"cq_id": "VI-CQ-01", "resolution": "verified_rule"}])
    assert vi["effects"][0]["effect"] == "verified"
    assert he.ledger.of_type("crossing_verification_integrity_requested")     # the owning service is told (stand-in)
    assert he.apost("/legal/v1/memos", {"request_id": rid(), "counsel_ref": COUNSEL_REF, "memo_date": "2027-01-01",
                                        "content_b64": b64(b"future"), "cites": {}}).status_code == 422


def test_playbook_view_and_version_order(he):
    playbook(he)
    pb = he.ok(he.get("/legal/v1/playbooks/client_msa"))["playbook"]
    assert pb["version"] == "1.0" and all("standard_text" not in c for c in pb["clauses"])
    memo = he.memo(cites={"clause_ids": ["MSA-RENEW-01", "MSA-PAY-01", "MSA-CCPA-01"]})
    from builders import MSA_CLAUSES
    r = he.apost("/legal/v1/playbooks/proposals", {"request_id": rid(), "counsel_memo_id": memo["memo_id"],
                 "playbook": {"playbook_id": "pb", "doc_type": "client_msa", "version": "1.0", "clauses": MSA_CLAUSES}})
    assert r.status_code == 409
    bad = [dict(MSA_CLAUSES[0], escalation={"fallback_1": "agent", "fallback_2": "agent", "unmatched": "counsel"})]
    r = he.apost("/legal/v1/playbooks/proposals", {"request_id": rid(), "counsel_memo_id": memo["memo_id"],
                 "playbook": {"playbook_id": "pb", "doc_type": "client_msa", "version": "2.0", "clauses": bad}})
    assert r.status_code == 422 and "always counsel" in r.text


def test_engagement_renewal_keeps_the_counsel_ref_and_memos_follow_the_current_letter(he):
    assert he.memo(content=b"before expiry")["memo_id"]
    he.clock.advance(days=91)                                                  # the letter's review_by passed
    r = he.apost("/legal/v1/memos", {"request_id": rid(), "counsel_ref": COUNSEL_REF, "memo_date": "2026-12-31",
                                     "content_b64": b64(b"after expiry"), "cites": {}})
    assert r.status_code == 409 and r.json()["detail"] == "ENGAGEMENT_NOT_APPROVED"
    he.engage(version="1.1")                                                   # same counsel, a new countersigned version
    assert he.memo(content=b"after renewal", memo_date="2026-12-31")["memo_id"]


def test_fills_keep_using_the_template_after_an_instance_is_approved_and_old_instances_stay_acceptable(he):
    from builders import executed_msa
    fill, acc = executed_msa(he)                                    # 1.0 template, 1.1 acme instance (approved)
    b = he.fill("client_msa", {"client_name": "Beta LLC", "end_date": "2027-12-31"}, "client:beta", expect="1.2")
    assert b["review_label"] == "counsel_template:client_msa@1.0"
    r = he.post("/legal/v1/documents/client_msa/versions", {"request_id": rid(), "entity": "zbm", "party_ref": "client:c",
                "variables": {"client_name": "You must sign this today", "end_date": "2027-12-31"}}, caller="scheduler")
    assert r.status_code == 422 and r.json()["detail"] == "ADVICE_TEXT_BLOCKED"
    memo = he.memo(cites={"doc_versions": ["client_msa@1.2"]})
    he.ok(he.apost("/legal/v1/documents/client_msa/versions/1.2/counsel-signoff",
                   {"request_id": rid(), "counsel_ref": COUNSEL_REF, "signed_on": "2026-10-01", "doc_sha256": b["sha256"],
                    "memo_id": memo["memo_id"], "memo_sha256": memo["memo_sha256"]}))
    he.ok(he.apost("/legal/v1/documents/client_msa/versions/1.2/decision",
                   {"request_id": rid(), "decision": "approve", "version_sha256": b["sha256"]}))
    a2 = he.clickwrap("client_msa", "1.1", fill["sha256"], party="client:acme", caller="onboarding")
    assert a2["evidence_sufficient"] is True                        # 1.1 is still approved and in force
    a3 = he.clickwrap("client_msa", "1.2", b["sha256"], party="client:beta", caller="onboarding")
    assert a3["evidence_sufficient"] is True
    # fix 18 (AEGIS N17-4): acme's instance is acme's; another client accepting it is refused (before: sufficient)
    r = he.clickwrap("client_msa", "1.1", fill["sha256"], party="client:acme2", caller="onboarding", code=409)
    assert r["detail"] == "INSTANCE_NOT_BOUND"
