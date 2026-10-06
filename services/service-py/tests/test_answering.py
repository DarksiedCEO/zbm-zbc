"""Andre's rule (ADR 0014 decisions 12-15): routine answered right away from approved answers; money, contracts and
complaints go to Andre; security to Cybersecurity; privacy to Compliance + Legal, never answered by the bot."""

from __future__ import annotations

import pytest

from helpers import Harness, RecordingAlerts, RecordingHandoff, rid
from ports import Ports


def _handoffs(h, ticket_id):
    return {x["department"]: x for x in h.ok(h.get(f"/svc/v1/tickets/{ticket_id}"))["handoffs"]}


def _alert_codes(h):
    return [a["code"] for a in h.ok(h.get("/svc/v1/alerts"))]


@pytest.mark.parametrize("text", ["I want a refund", "what are your hours? also I want a refund",
                                  "refund", "Can I get my money back for the hours you were closed?"])
def test_a_refund_is_never_auto_answered_even_when_an_article_matches(h, text):
    h.article()
    r = h.ok(h.chat(text), 201)
    assert r["action"] == "escalated" and "answer" not in r
    t = h.ok(h.get(f"/svc/v1/tickets/{r['ticket_id']}"))
    assert t["status"] == "escalated" and t["queue"] == "andre" and "money" in t["categories"]
    assert not [m for m in t["messages"] if m["dir"] == "out"]
    assert _handoffs(h, r["ticket_id"])["finance_31"]["status"] == "not_wired"
    assert "ESCALATION_MONEY" in _alert_codes(h)
    assert h.ledger.of_type("escalation_opened") and h.ledger.of_type("alert_raised")


def test_my_lawyer_goes_to_andre_and_legal(h):
    r = h.ok(h.chat("My lawyer will contact you about the contract"), 201)
    hof = _handoffs(h, r["ticket_id"])
    assert set(hof) == {"legal_37"} and hof["legal_37"]["kind"] == "litigation_threat"
    assert "ESCALATION_CONTRACT" in _alert_codes(h)


def test_legal_handoff_is_delivered_when_the_port_is_wired(tmp_path):
    ports = Ports.default()
    legal = ports.handoffs["legal_37"] = RecordingHandoff()
    h = Harness(tmp_path, ports=ports)
    r = h.ok(h.chat("DMCA notice: you used my copyrighted song"), 201)
    assert len(legal.calls) == 1
    req = legal.calls[0]
    assert req.kind == "ip_claim" and req.ticket_id == r["ticket_id"]
    assert "song" not in repr(req)                                   # refs and codes only, never the body
    hof = _handoffs(h, r["ticket_id"])["legal_37"]
    assert hof["status"] == "delivered" and hof["reference"] == "ref-1"


def test_a_refused_or_failing_handoff_is_retried_by_the_job(tmp_path):
    ports = Ports.default()
    legal = ports.handoffs["legal_37"] = RecordingHandoff("unavailable")
    h = Harness(tmp_path, ports=ports)
    r = h.ok(h.chat("I will sue"), 201)
    assert _handoffs(h, r["ticket_id"])["legal_37"]["status"] == "unavailable"
    legal.status = "delivered"
    h.ok(h.job("handoff-retries"))
    assert _handoffs(h, r["ticket_id"])["legal_37"]["status"] == "delivered" and len(legal.calls) == 2


def test_a_raising_port_is_unavailable_never_an_error(tmp_path):
    class Boom:
        wired = True

        def handoff(self, req):
            raise RuntimeError("secret internal detail")
    ports = Ports.default()
    ports.handoffs["finance_31"] = Boom()
    h = Harness(tmp_path, ports=ports)
    r = h.chat("refund please")
    assert r.status_code == 201
    assert _handoffs(h, r.json()["ticket_id"])["finance_31"]["status"] == "unavailable"


@pytest.mark.parametrize("text", ["this is the worst service ever", "what the fuck", "WHERE IS MY REPORT ALREADY",
                                  "I want to make a complaint!!!"])
def test_complaints_go_to_andre(h, text):
    h.article()
    r = h.ok(h.chat(text), 201)
    assert r["action"] == "escalated"
    assert "ESCALATION_COMPLAINT" in _alert_codes(h)
    assert _handoffs(h, r["ticket_id"]) == {}                      # complaints: Andre only


def test_repeated_contacts_become_a_complaint(h):
    h.article()
    assert h.ok(h.chat("what are your hours"), 201)["action"] == "answered"
    assert h.ok(h.chat("what are your hours"), 201)["action"] == "answered"
    r = h.ok(h.chat("what are your hours"), 201)                  # third in 24 hours
    assert r["action"] == "escalated" and "ESCALATION_COMPLAINT" in _alert_codes(h)


def test_security_goes_to_cybersecurity_and_is_p1(h):
    r = h.ok(h.chat("I think someone hacked my portal account"), 201)
    assert set(_handoffs(h, r["ticket_id"])) == {"cybersecurity_22"}
    t = h.ok(h.get(f"/svc/v1/tickets/{r['ticket_id']}"))
    assert t["priority"] == "p1" and "ESCALATION_SECURITY" in _alert_codes(h)


@pytest.mark.parametrize("text", ["Please delete my data", "delete my account and all my personal information",
                                  "GDPR right to be forgotten request"])
def test_privacy_requests_route_to_compliance_and_legal_and_are_never_answered(h, text):
    h.article()
    r = h.ok(h.chat(text), 201)
    assert r["action"] == "escalated" and "answer" not in r
    hof = _handoffs(h, r["ticket_id"])
    assert set(hof) >= {"compliance_38", "legal_37"} and hof["legal_37"]["kind"] == "privacy_request"
    assert "ESCALATION_PRIVACY" in _alert_codes(h)


def test_a_follow_up_on_an_escalated_ticket_is_never_bot_answered(h):
    h.article()
    r1 = h.ok(h.chat("I want a refund"), 201)
    r2 = h.ok(h.chat("also, what are your hours?"), 201)
    assert r2["ticket_id"] == r1["ticket_id"] and r2["action"] == "queued_for_human"
    t = h.ok(h.get(f"/svc/v1/tickets/{r1['ticket_id']}"))
    assert t["status"] == "escalated" and t["queue"] == "andre"   # never demoted


def test_email_answer_is_queued_visibly_when_no_provider_is_wired(h):
    h.article()
    r = h.ok(h.email("Hello, what are your opening hours?"), 201)
    assert r["action"] == "answered" and "answer" not in r
    out = h.ok(h.get("/svc/v1/outbound?status=queued"))
    assert len(out) == 1 and out[0]["channel"] == "email" and out[0]["origin"] == "kb"
    tick = h.ok(h.job("outbound-tick"))
    assert tick["not_wired"] == 1 and tick["sent"] == 0
    assert h.ok(h.get("/svc/v1/outbound?status=queued"))                          # still queued, visibly


def test_email_to_the_wrong_brand_identity_is_refused(h):
    r = h.post("/svc/v1/inbound/email", {"request_id": rid(), "brand": "zbc", "to_address": "support@zbestmedia.test",
                                         "from_address": "a@b.test", "text": "hours?"}, caller="email_gateway")
    assert r.status_code == 422 and r.json()["detail"] == "WRONG_BRAND_IDENTITY"


def test_email_inbound_refused_when_the_brand_has_no_support_identity(tmp_path):
    h = Harness(tmp_path, SVC_SUPPORT_EMAIL_ZBC=None)
    r = h.email("hours?", brand="zbc")
    assert r.status_code == 503 and r.json()["detail"] == "BRAND_EMAIL_NOT_CONFIGURED"


def test_one_conversation_across_channels(h):
    cid = h.contact(email="owner@acme.test", phone="+13105551234", timezone="America/Los_Angeles")
    r1 = h.ok(h.chat("I need help with my campaign"), 201)
    r2 = h.ok(h.email("following up by email on my campaign"), 201)
    assert r1["contact_id"] == r2["contact_id"] == cid and r1["ticket_id"] == r2["ticket_id"]


def test_a_ticket_id_of_another_contact_is_refused(h):
    r = h.ok(h.chat("I need help", ref="client:one"), 201)
    other = h.chat("me too", ref="client:two", ticket_id=r["ticket_id"])
    assert other.status_code == 404 and other.json()["detail"] == "TICKET_NOT_FOUND"


def test_chat_thread_view_is_scoped_to_the_contact(h):
    h.article()
    r = h.ok(h.chat("what are your hours", ref="client:one"), 201)
    th = h.ok(h.get(f"/svc/v1/chat/threads/{r['ticket_id']}?contact_ref=client:one&brand=zbm", caller="hub"))
    assert [m["from"] for m in th["messages"]] == ["contact", "team"]
    assert h.get(f"/svc/v1/chat/threads/{r['ticket_id']}?contact_ref=client:two&brand=zbm",
                 caller="hub").status_code == 404
    assert h.get(f"/svc/v1/chat/threads/{r['ticket_id']}?contact_ref=client:one&brand=zbm",
                 caller="email_gateway").status_code == 403


def test_andre_reply_needs_his_token_and_is_recorded(h):
    r = h.ok(h.chat("I need help with an invoice"), 201)
    path = f"/svc/v1/tickets/{r['ticket_id']}/reply"
    assert h.post(path, {"request_id": rid(), "text": "On it."}).status_code == 403
    rep = h.ok(h.post(path, {"request_id": rid(), "text": "On it, Andre here."}, andre=True), 201)
    t = h.ok(h.get(f"/svc/v1/tickets/{r['ticket_id']}"))
    assert t["status"] == "pending_customer" and t["answered_by"] == "andre"
    assert [m for m in t["messages"] if m["message_id"] == rep["message_id"]][0]["origin"] == "human"
    assert h.ledger.of_type("answer_queued")


def test_alerts_are_delivered_when_the_alert_port_is_wired(tmp_path):
    ports = Ports.default()
    alerts = ports.alerts = RecordingAlerts()
    h = Harness(tmp_path, ports=ports)
    h.ok(h.chat("refund my money, this is a scam"), 201)
    codes = sorted(a.code for a in alerts.sent)
    assert codes == ["ESCALATION_COMPLAINT", "ESCALATION_MONEY"]
    assert all("refund" not in repr(a) for a in alerts.sent)       # codes and ids only
    assert all(a["delivery"] == "delivered" for a in h.ok(h.get("/svc/v1/alerts")))
