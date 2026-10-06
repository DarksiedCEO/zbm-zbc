"""Lead intake (four sources), dedupe, scoring, routing and the pipeline (ADR 0013 decisions 6-8, 18)."""

from __future__ import annotations

import json

import pytest

from helpers import FakeSource, Harness, rid


# ------------------------------------------------------------------ sources

@pytest.mark.parametrize("caller,kind,interest", [("hub", "site_form", ("social",)),
                                                   ("hub", "zbc_campaign_inquiry", ()),
                                                   ("onboarding", "site_form", ("tv",)),
                                                   ("detection", "rr_scan", ())])
def test_inbound_sources_accepted_from_their_callers(h, caller, kind, interest):
    lead = h.lead(caller=caller, kind=kind, interest=interest)
    assert lead["source"] == "inbound" and lead["evidence"][0]["kind"] == kind
    assert lead["brand"] == ("zbc" if kind == "zbc_campaign_inquiry" else "zbm")


@pytest.mark.parametrize("caller,source,kind", [("detection", "inbound", "site_form"), ("hub", "inbound", "rr_scan"),
                                                ("sales_agent", "inbound", "site_form"),
                                                ("hub", "referral", "referral_note"),
                                                ("onboarding", "inbound", "zbc_campaign_inquiry")])
def test_a_caller_cannot_present_another_callers_source(h, caller, source, kind):
    r = h.post("/sales/v1/leads", {"request_id": rid(), "source": source, "contact": {"name": "A", "email": "a@b.test"},
                                   "evidence": {"kind": kind, "ref": "x", "captured_at": "2026-10-06T10:00:00Z"},
                                   **({"referrer": {"name": "R", "ref": "r1"}} if source == "referral" else {})}, caller)
    assert r.status_code == 403 and r.json()["detail"] in ("SOURCE_NOT_ALLOWED", "CALLER_NOT_ALLOWED")


def test_referral_and_partner_record_the_referrer(h):
    lead = h.lead(caller="dashboard", source="referral", kind="referral_note", referrer={"name": "Sam", "ref": "p-1"})
    assert lead["referrer"] == {"name": "Sam", "ref": "p-1"}
    lead = h.lead(caller="dashboard", source="partner", kind="partner_note", email="b@other.test", phone=None,
                  referrer={"name": "Agency X", "ref": "partner-9"})
    assert lead["source"] == "partner"


def test_referral_without_a_referrer_refused_and_inbound_with_one_refused(h):
    h.lead(caller="dashboard", source="referral", kind="referral_note", code=422)
    h.lead(referrer={"name": "Sam", "ref": "p-1"}, code=422)


@pytest.mark.parametrize("source", ["public_data", "paid_provider"])
def test_public_data_and_paid_provider_never_over_the_api(h, source):
    r = h.post("/sales/v1/leads", {"request_id": rid(), "source": source, "contact": {"name": "A", "email": "a@b.test"},
                                   "evidence": {"kind": "site_form", "ref": "x", "captured_at": "2026-10-06T10:00:00Z"}},
               "hub")
    assert r.status_code == 422


@pytest.mark.parametrize("source", ["public_data", "paid_provider"])
def test_import_ports_not_wired_answer_503(h, source):
    h.refused(h.post("/sales/v1/leads/import", {"request_id": rid(), "source": source}, "sales_agent"), 503,
              "SOURCE_NOT_WIRED")


def test_import_through_a_wired_port_keeps_source_and_evidence_and_refuses_bad_records(tmp_path):
    from helpers import wired_ports
    good = {"contact": {"name": "Pat", "email": "pat@shop-one.test"}, "product_interest": ["social"],
            "evidence": {"kind": "public_record", "ref": "pr-1", "captured_at": "2026-10-01T00:00:00Z"}}
    wrong_kind = {**good, "evidence": {**good["evidence"], "kind": "provider_record"}}
    with_dob = {**good, "contact": {**good["contact"], "dob": "1990-01-01"}}
    ports = wired_ports()
    ports.sources["public_data"] = FakeSource([good, wrong_kind, with_dob, {"nonsense": 1}])
    h = Harness(tmp_path, ports=ports)
    body = {"request_id": rid(), "source": "public_data"}
    r = h.ok(h.post("/sales/v1/leads/import", body, "sales_agent"))
    assert len(r["created"]) == 1 and r["refused"] == 3
    lead = h.ok(h.get(f"/sales/v1/leads/{r['created'][0]}"))
    assert lead["source"] == "public_data" and lead["evidence"][0]["kind"] == "public_record"
    assert "I1" not in lead["score"]["rules"]
    again = h.ok(h.post("/sales/v1/leads/import", body, "sales_agent"))
    assert again["created"] == [] and again["duplicates"] == 1     # a retried import adds nothing


# ------------------------------------------------------------------ dedupe

def test_dedupe_by_normalised_email(h):
    a = h.lead(email="Jane+promo@Acme-Shop.test", phone=None)
    b = h.lead(email="jane@acme-shop.test", phone=None, caller="detection", kind="rr_scan")
    assert b["duplicate"] is True and b["lead_id"] == a["lead_id"] and len(b["evidence"]) == 2
    assert "I6" in b["score"]["rules"]
    assert len(h.svc.leads) == 1 and len(h.svc.contacts) == 1


def test_dedupe_by_normalised_phone(h):
    a = h.lead(email=None, phone="(310) 555-0100")
    b = h.lead(email=None, phone="+1 310 555 0100")
    assert b["lead_id"] == a["lead_id"] and b["duplicate"] is True


def test_dedupe_account_by_domain(h):
    a = h.lead(email="one@acme-shop.test", phone=None)
    b = h.lead(email="two@acme-shop.test", phone=None, account={"name": "ACME"})
    assert a["lead_id"] != b["lead_id"] and a["account_id"] == b["account_id"]


def test_free_mail_is_not_a_company_domain(h):
    a = h.lead(email="x@gmail.com", phone=None, account={"name": "Solo Shop"})
    b = h.lead(email="y@gmail.com", phone=None, account={"name": "Other Shop"})
    assert a["account_id"] != b["account_id"] and "F1" not in a["score"]["rules"]


def test_no_raw_email_or_phone_reaches_the_ledger(h):
    h.lead(email="secret.person@acme-shop.test", phone="+13105550199")
    blob = json.dumps(h.ledger.events)
    assert "secret.person" not in blob and "5550199" not in blob


@pytest.mark.parametrize("field,value", [("dob", "1990-01-01"), ("ssn", "123"), ("card_number", "4111"),
                                         ("bank_account", "x"), ("date_of_birth", "x")])
def test_fields_about_people_beyond_the_minimum_are_refused(h, field, value):
    r = h.post("/sales/v1/leads", {"request_id": rid(), "source": "inbound",
                                   "contact": {"name": "A", "email": "a@b.test", field: value},
                                   "evidence": {"kind": "site_form", "ref": "x", "captured_at": "2026-10-06T10:00:00Z"}},
               "hub")
    assert r.status_code == 422 and r.json()["detail"][0]["type"] == "forbidden_field"
    assert value not in r.text


@pytest.mark.parametrize("contact,code", [({"name": "A"}, "CONTACT_CHANNEL_REQUIRED"),
                                          ({"name": "A", "email": "not-an-email"}, "EMAIL_INVALID"),
                                          ({"name": "A", "phone": "12"}, "PHONE_INVALID"),
                                          ({"name": "A", "email": "a@b.test", "time_zone": "Mars/Base"},
                                           "TIME_ZONE_INVALID")])
def test_contact_validation(h, contact, code):
    r = h.post("/sales/v1/leads", {"request_id": rid(), "source": "inbound", "contact": contact,
                                   "evidence": {"kind": "site_form", "ref": "x", "captured_at": "2026-10-06T10:00:00Z"}},
               "hub")
    h.refused(r, 422, code)
    assert "not-an-email" not in r.text


# ------------------------------------------------------------------ scoring and routing

def test_scoring_rules_are_deterministic_and_named(h):
    body = {"request_id": rid(), "source": "inbound", "contact": {"name": "Rae", "email": "rae@store.test"},
            "evidence": {"kind": "rr_scan", "ref": "scan-1", "captured_at": "2026-10-06T10:00:00Z", "scan_findings": 4},
            "account": {"name": "Store", "industry": "retail", "employees_band": "51-200", "revenue_band": "10m_50m"},
            "signals": {"monthly_ad_spend_band": "25k_100k", "timeline": "30_days", "budget_stated": True}}
    lead = h.ok(h.post("/sales/v1/leads", body, "detection"), 201)
    s = lead["score"]
    assert (s["fit"], s["intent"]) == (50, 43)       # fit 10+12+12+10+8 capped at 50; intent 15+15+8+5
    assert s["grade"] == "A" and lead["status"] == "qualified"
    assert s["rules"] == ["F1", "F2", "F3", "F5", "F4", "I1", "I2", "I4", "I5"]


def test_low_score_lead_is_new_not_qualified(h):
    lead = h.lead(email="z@gmail.com", phone=None, account={"name": "Tiny"}, signals={}, interest=("social",))
    assert lead["score"]["total"] == 15 and lead["status"] == "new"


def test_scan_findings_only_on_a_scan(h):
    body = {"request_id": rid(), "source": "inbound", "contact": {"name": "A", "email": "a@b.test"},
            "evidence": {"kind": "site_form", "ref": "x", "captured_at": "2026-10-06T10:00:00Z", "scan_findings": 3}}
    h.refused(h.post("/sales/v1/leads", body, "hub"), 422, "INVALID")


@pytest.mark.parametrize("brand,interest,kind", [("zbm", (), "zbc_campaign_inquiry"),
                                                 (None, ("social", "clipping_campaign"), "site_form"),
                                                 ("zbc", ("tv",), "site_form"), ("zbc", (), "rr_scan")])
def test_routing_contradictions_refused(h, brand, interest, kind):
    caller = "detection" if kind == "rr_scan" else "hub"
    h.refused(h.post("/sales/v1/leads", {"request_id": rid(), "source": "inbound", "brand": brand,
                                         "product_interest": list(interest),
                                         "contact": {"name": "A", "email": "a@b.test"},
                                         "evidence": {"kind": kind, "ref": "x",
                                                      "captured_at": "2026-10-06T10:00:00Z"}} if brand else
                     {"request_id": rid(), "source": "inbound", "product_interest": list(interest),
                      "contact": {"name": "A", "email": "a@b.test"},
                      "evidence": {"kind": kind, "ref": "x", "captured_at": "2026-10-06T10:00:00Z"}}, caller),
              422, "BRAND_PRODUCT_MISMATCH")


def test_unrouted_lead_gets_a_routing_task_and_cannot_convert(h):
    lead = h.lead(interest=())
    assert lead["queue"] == "unrouted" and lead["brand"] is None
    tasks = h.ok(h.get("/sales/v1/tasks?status=open"))
    assert [t["kind"] for t in tasks] == ["route_lead"] and tasks[0]["target"] == f"lead:{lead['lead_id']}"
    h.refused(h.post(f"/sales/v1/leads/{lead['lead_id']}/convert", {"request_id": rid()}, "sales_agent"), 409,
              "LEAD_UNROUTED")


def test_zbc_inquiry_routes_to_clipping(h):
    lead = h.lead(kind="zbc_campaign_inquiry", interest=())
    assert (lead["brand"], lead["product_lines"], lead["queue"]) == ("zbc", ["clipping_campaign"], "zbc_sales")


# ------------------------------------------------------------------ pipeline

def test_convert_owner_stage_activity(h):
    lead = h.lead()
    h.ok(h.post(f"/sales/v1/leads/{lead['lead_id']}/owner", {"request_id": rid(), "owner": "agent-7"}, "sales_agent"))
    opp = h.ok(h.post(f"/sales/v1/leads/{lead['lead_id']}/convert", {"request_id": rid()}, "sales_agent"), 201)
    assert opp["owner"] == "agent-7" and opp["stage"] == "qualified"
    assert h.ok(h.get(f"/sales/v1/leads/{lead['lead_id']}"))["status"] == "converted"
    opp = h.ok(h.post(f"/sales/v1/opportunities/{opp['opportunity_id']}/stage",
                      {"request_id": rid(), "stage": "meeting"}, "sales_agent"))
    assert opp["stage"] == "meeting"
    h.ok(h.post("/sales/v1/activities", {"request_id": rid(), "target_kind": "opportunity",
                                         "target_id": opp["opportunity_id"], "kind": "meeting_booked",
                                         "note": "Tuesday"}, "sales_agent"), 201)
    assert h.ok(h.get(f"/sales/v1/opportunities/{opp['opportunity_id']}"))["activities"][0]["kind"] == "meeting_booked"


def test_closed_won_only_through_a_won_proposal(h):
    opp = h.opportunity()
    r = h.post(f"/sales/v1/opportunities/{opp['opportunity_id']}/stage", {"request_id": rid(), "stage": "closed_won"},
               "sales_agent")
    assert r.status_code == 422


def test_convert_needs_a_qualified_lead(h):
    lead = h.lead(email="z@gmail.com", phone=None, account={"name": "Tiny"}, signals={}, interest=("social",))
    h.refused(h.post(f"/sales/v1/leads/{lead['lead_id']}/convert", {"request_id": rid()}, "sales_agent"), 409,
              "LEAD_NOT_QUALIFIED")


def test_disqualify_then_no_convert(h):
    lead = h.lead()
    h.ok(h.post(f"/sales/v1/leads/{lead['lead_id']}/disqualify", {"request_id": rid(), "reason_code": "no_budget"},
                "sales_agent"))
    h.refused(h.post(f"/sales/v1/leads/{lead['lead_id']}/convert", {"request_id": rid()}, "sales_agent"), 409,
              "LEAD_NOT_QUALIFIED")


def test_stale_lead_aging_job(h):
    lead = h.lead()
    h.clock.advance(days=31)
    r = h.ok(h.job("stale-leads"))
    assert r["aged"] == [lead["lead_id"]]
    assert h.ok(h.get(f"/sales/v1/leads/{lead['lead_id']}"))["status"] == "stale"
    assert h.ok(h.job("stale-leads"))["aged"] == []


def test_stale_job_does_not_age_a_recent_lead(h):
    h.lead()
    h.clock.advance(days=29)
    assert h.ok(h.job("stale-leads"))["aged"] == []


def test_intake_is_idempotent_and_refuses_a_changed_body(h):
    body = {"request_id": rid(), "source": "inbound", "contact": {"name": "A", "email": "a@b.test"},
            "evidence": {"kind": "site_form", "ref": "x", "captured_at": "2026-10-06T10:00:00Z"}}
    a = h.ok(h.post("/sales/v1/leads", body, "hub"), 201)
    b = h.ok(h.post("/sales/v1/leads", body, "hub"), 201)
    assert a["lead_id"] == b["lead_id"] and len(h.svc.leads) == 1
    h.refused(h.post("/sales/v1/leads", {**body, "contact": {"name": "B", "email": "a@b.test"}}, "hub"), 409,
              "REQUEST_ID_REUSED")
    # the same request id from another caller is another request
    other = h.post("/sales/v1/leads", body, "onboarding")
    assert other.status_code == 201


def test_task_close(h):
    lead = h.lead(interest=())
    tid = h.ok(h.get("/sales/v1/tasks"))[0]["task_id"]
    h.ok(h.post(f"/sales/v1/tasks/{tid}/close", {"request_id": rid(), "outcome": "done"}, "sales_agent"))
    h.refused(h.post(f"/sales/v1/tasks/{tid}/close", {"request_id": rid(), "outcome": "done"}, "sales_agent"), 409,
              "TASK_CLOSED")
    assert lead



def test_evidence_from_the_future_refused(h):
    body = {"request_id": rid(), "source": "inbound", "contact": {"name": "A", "email": "a@b.test"},
            "evidence": {"kind": "site_form", "ref": "x", "captured_at": "2027-01-01T00:00:00Z"}}
    h.refused(h.post("/sales/v1/leads", body, "hub"), 422, "INVALID")
