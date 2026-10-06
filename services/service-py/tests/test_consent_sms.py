"""Consent registry and the outbound channel rules (ADR 0014 decisions 5-7): SMS only with recorded express consent,
STOP revokes at once, quiet hours 08:00-21:00 recipient-local, unknown time zone refuses; proactive email needs
consent; every send recorded on the ledger before the provider is called."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import channels
from clock import FixedClock
from helpers import T0, Harness, RecordingSender, rid
from ports import Ports


def _wired(tmp_path, clock=None, **env):
    ports = Ports.default()
    senders = {c: RecordingSender() for c in ("email", "sms", "chat")}
    ports.senders.update(senders)
    return Harness(tmp_path, ports=ports, clock=clock, **env), senders


def _sms_ticket(h, text="I need help with my campaign"):
    return h.ok(h.sms(text), 201)["ticket_id"]


# --------------------------------------------------------------------------------------------------- registry

def test_consent_is_recorded_with_source_time_and_text_hash_never_the_text(h):
    cid = h.contact(phone="+13105551234", timezone="America/Los_Angeles")
    c = h.ok(h.consent(cid), 201)
    view = h.ok(h.get(f"/svc/v1/contacts/{cid}/consents", caller="hub"))[0]
    assert view["status"] == "active" and view["source"] == "portal_form" and view["express"] is True
    assert view["captured_at"] == "2026-10-01T10:00:00Z" and len(view["consent_text_sha256"]) == 64
    assert c["consent_text_sha256"] == view["consent_text_sha256"]
    assert "agree" not in str(h.ledger.events)
    assert h.ledger.of_type("consent_changed")


def test_implied_consent_is_refused(h):
    cid = h.contact(phone="+13105551234", timezone="America/Los_Angeles")
    r = h.post("/svc/v1/consents", {"request_id": rid(), "contact_id": cid, "channel": "sms", "source": "portal_form",
                                    "consent_text": "x", "captured_at": "2026-10-01T10:00:00Z", "express": False},
               caller="hub")
    assert r.status_code == 422


def test_consent_needs_an_address_and_a_past_time(h):
    cid = h.contact()
    assert h.consent(cid).json()["detail"] == "NO_ADDRESS"
    cid2 = h.contact("client:b", phone="+13105559999")
    r = h.post("/svc/v1/consents", {"request_id": rid(), "contact_id": cid2, "channel": "sms", "source": "site_form",
                                    "consent_text": "yes", "captured_at": "2027-01-01T00:00:00Z", "express": True},
               caller="hub")
    assert r.json()["detail"] == "CONSENT_IN_FUTURE"


def test_only_hub_or_onboarding_may_record_consent(h):
    cid = h.contact(phone="+13105551234")
    r = h.post("/svc/v1/consents", {"request_id": rid(), "contact_id": cid, "channel": "sms", "source": "portal_form",
                                    "consent_text": "yes", "captured_at": "2026-10-01T10:00:00Z", "express": True},
               caller="sms_gateway")
    assert r.status_code == 403 and r.json()["detail"] == "CALLER_NOT_ALLOWED"


# --------------------------------------------------------------------------------------------------- SMS

def test_sms_reply_without_consent_is_refused(h):
    tid = _sms_ticket(h)
    r = h.post(f"/svc/v1/tickets/{tid}/reply", {"request_id": rid(), "text": "Hi!"}, andre=True)
    assert r.status_code == 409 and r.json()["detail"] == "SMS_CONSENT_REQUIRED"
    assert not h.ok(h.get("/svc/v1/outbound"))


def test_sms_routine_question_without_consent_is_not_auto_answered(h):
    h.article()
    r = h.ok(h.sms("what are your hours"), 201)
    assert r["action"] == "queued_for_human"


def test_sms_with_consent_is_sent_and_recorded_first(tmp_path):
    h, senders = _wired(tmp_path)
    h.article()
    cid = h.contact(phone="+13105551234", timezone="America/Los_Angeles")
    h.ok(h.consent(cid), 201)
    r = h.ok(h.sms("what are your hours"), 201)
    assert r["action"] == "answered" and r["contact_id"] == cid
    tick = h.ok(h.job("outbound-tick"))
    assert tick["sent"] == 1
    msg = senders["sms"].sent[0]
    assert msg.to == "+13105551234" and msg.sender == "+13105550100"
    assert msg.text.endswith("Reply STOP to opt out.") and msg.text.startswith("We are open")
    assert h.ledger.of_type("message_sent")[-1]["subject_id"] == msg.message_id


def test_stop_revokes_immediately_and_cancels_queued_sms(tmp_path):
    h, senders = _wired(tmp_path, clock=FixedClock(datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)))  # 22:00 LA
    h.article()
    cid = h.contact(phone="+13105551234", timezone="America/Los_Angeles")
    h.ok(h.consent(cid), 201)
    h.ok(h.sms("what are your hours"), 201)
    assert h.ok(h.job("outbound-tick"))["waiting"] == 1          # quiet hours: waits, not sent early
    r = h.ok(h.sms("STOP"), 201)
    assert r["action"] == "opted_out"
    assert h.ok(h.get(f"/svc/v1/contacts/{cid}/consents", caller="hub"))[0]["status"] == "revoked"
    out = [m for m in h.ok(h.get("/svc/v1/outbound")) if m["origin"] == "kb"]
    assert out[0]["status"] == "cancelled" and out[0]["reason"] == "CONSENT_REVOKED"
    assert r["confirmation_message_id"]
    h.clock.at = T0
    assert h.ok(h.job("outbound-tick"))["sent"] == 1                 # only the one opt-out confirmation
    assert len(senders["sms"].sent) == 1 and senders["sms"].sent[0].message_id == r["confirmation_message_id"]
    assert "unsubscribed" in senders["sms"].sent[0].text and "Reply STOP" not in senders["sms"].sent[0].text


@pytest.mark.parametrize("word", ["stop", "Stop.", "UNSUBSCRIBE", "cancel", "quit", "End", "STOPALL"])
def test_stop_keywords(word):
    assert channels.is_stop(word)


@pytest.mark.parametrize("text", ["what are your hours", "I need help with my campaign", "yes please",
                                  "send me the report", "thanks!"])
def test_not_an_opt_out(text):
    assert not channels.is_opt_out(text)


def test_stop_from_an_unknown_number_records_a_revocation(h):
    r = h.ok(h.sms("STOP", frm="+13105550001"), 201)
    assert r["action"] == "opted_out"
    assert h.ok(h.get(f"/svc/v1/contacts/{r['contact_id']}/consents", caller="hub"))[0]["status"] == "revoked"


def test_a_new_consent_after_stop_is_needed_to_text_again(tmp_path):
    h, senders = _wired(tmp_path)
    cid = h.contact(phone="+13105551234", timezone="America/Los_Angeles")
    h.ok(h.consent(cid), 201)
    h.ok(h.sms("STOP"), 201)
    tid = _sms_ticket(h, "I need help")
    assert h.post(f"/svc/v1/tickets/{tid}/reply", {"request_id": rid(), "text": "hi"},
                  andre=True).json()["detail"] == "SMS_CONSENT_REQUIRED"
    h.clock.advance(minutes=5)
    h.ok(h.consent(cid, captured_at="2026-10-06T18:03:00Z"), 201)     # captured after the STOP
    h.ok(h.post(f"/svc/v1/tickets/{tid}/reply", {"request_id": rid(), "text": "hi"}, andre=True), 201)


@pytest.mark.parametrize("utc_hour,allowed", [(14, False), (15, True), (18, True), (3, True), (4, False)])
def test_quiet_hours_are_recipient_local(utc_hour, allowed):
    # America/Los_Angeles is UTC-7 on 2026-10-06: 15:00 UTC = 08:00 local, 04:00 UTC = 21:00 local
    now = datetime(2026, 10, 6, utc_hour, 0, tzinfo=timezone.utc)
    assert channels.within_sms_hours(now, "America/Los_Angeles") is allowed


def test_quiet_hours_in_the_recipients_zone_not_ours(tmp_path):
    # 18:00 UTC: 11:00 in LA, 03:00 next day in Tokyo
    h, senders = _wired(tmp_path)
    cid = h.contact(phone="+13105551234", timezone="Asia/Tokyo")
    h.ok(h.consent(cid), 201)
    tid = _sms_ticket(h)
    rep = h.ok(h.post(f"/svc/v1/tickets/{tid}/reply", {"request_id": rid(), "text": "hello"}, andre=True), 201)
    assert rep["waiting_for"] == "quiet_hours"
    assert h.ok(h.job("outbound-tick"))["waiting"] == 1 and not senders["sms"].sent


def test_unknown_time_zone_refuses_sms(tmp_path):
    h, senders = _wired(tmp_path)
    cid = h.contact(phone="+13105551234")                         # no time zone recorded
    h.ok(h.consent(cid), 201)
    tid = _sms_ticket(h)
    r = h.post(f"/svc/v1/tickets/{tid}/reply", {"request_id": rid(), "text": "hello"}, andre=True)
    assert r.status_code == 409 and r.json()["detail"] == "TIMEZONE_UNKNOWN"


def test_an_invalid_time_zone_is_refused_at_the_door(h):
    r = h.post("/svc/v1/contacts", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:x",
                                    "timezone": "Mars/Olympus_Mons"}, caller="hub")
    assert r.status_code == 422 and r.json()["detail"] == "TIMEZONE_UNKNOWN"


def test_consent_revoked_by_dashboard(tmp_path):
    h, senders = _wired(tmp_path)
    cid = h.contact(phone="+13105551234", timezone="America/Los_Angeles")
    h.ok(h.consent(cid), 201)
    h.ok(h.post("/svc/v1/consents/revoke", {"request_id": rid(), "contact_id": cid, "channel": "sms"}))
    assert h.ok(h.get(f"/svc/v1/contacts/{cid}/consents", caller="hub"))[0]["revoked_via"] == "dashboard"


# --------------------------------------------------------------------------------------------------- email, chat

def test_email_reply_on_their_own_ticket_needs_no_consent_but_revocation_stops_it(tmp_path):
    h, senders = _wired(tmp_path)
    r = h.ok(h.email("I need help with my campaign"), 201)
    h.ok(h.post(f"/svc/v1/tickets/{r['ticket_id']}/reply", {"request_id": rid(), "text": "Happy to help."},
                andre=True), 201)
    assert h.ok(h.job("outbound-tick"))["sent"] == 1
    sent = senders["email"].sent[0]
    assert sent.sender == "support@zbestmedia.test" and sent.to == "owner@acme.test" and sent.subject.startswith("Re:")
    h.ok(h.post("/svc/v1/consents/revoke", {"request_id": rid(), "contact_id": r["contact_id"], "channel": "email"},
                caller="hub"))
    rr = h.post(f"/svc/v1/tickets/{r['ticket_id']}/reply", {"request_id": rid(), "text": "again"}, andre=True)
    assert rr.status_code == 409 and rr.json()["detail"] == "EMAIL_CONSENT_REVOKED"


def test_zbc_email_goes_out_from_the_zbc_identity(tmp_path):
    h, senders = _wired(tmp_path)
    r = h.ok(h.email("help with my clips", brand="zbc"), 201)
    h.ok(h.post(f"/svc/v1/tickets/{r['ticket_id']}/reply", {"request_id": rid(), "text": "Sure."}, andre=True), 201)
    h.ok(h.job("outbound-tick"))
    assert senders["email"].sent[0].sender == "help@zbestclips.test"


def test_phone_is_never_an_outbound_channel(h):
    r = h.ok(h.post("/svc/v1/calls", {"request_id": rid(), "brand": "zbm", "from_number": "+13105551234",
                                      "started_at": "2026-10-06T17:00:00Z", "duration_seconds": 30,
                                      "outcome": "voicemail", "voicemail_ref": "vm/1"}, caller="voice_gateway"), 201)
    rr = h.post(f"/svc/v1/tickets/{r['ticket_id']}/reply", {"request_id": rid(), "text": "hi"}, andre=True)
    assert rr.status_code == 422 and rr.json()["detail"] == "CHANNEL_NOT_OUTBOUND"


# --------------------------------------------------------------------------------------------------- sending

def test_ledger_down_nothing_is_sent(tmp_path):
    h, senders = _wired(tmp_path)
    r = h.ok(h.email("I need help"), 201)
    h.ok(h.post(f"/svc/v1/tickets/{r['ticket_id']}/reply", {"request_id": rid(), "text": "ok"}, andre=True), 201)
    h.ledger.fail_types = {"message_sending"}
    assert h.job("outbound-tick").status_code == 503
    assert not senders["email"].sent
    assert h.ok(h.get("/svc/v1/outbound?status=queued"))
    h.ledger.fail_types = set()
    assert h.ok(h.job("outbound-tick"))["sent"] == 1 and len(senders["email"].sent) == 1


def test_ledger_down_no_answer_no_reply_no_consent(h):
    h.article()
    cid = h.contact(phone="+13105551234")
    h.ledger.fail = True
    assert h.chat("what are your hours").status_code == 503
    assert h.consent(cid).status_code == 503
    h.ledger.fail = False
    assert not h.ok(h.get(f"/svc/v1/contacts/{cid}/consents", caller="hub"))
    assert not h.ok(h.get("/svc/v1/tickets"))


def test_a_failing_provider_is_retried_then_given_up(tmp_path):
    h, senders = _wired(tmp_path)
    senders["email"].result = "failed"
    r = h.ok(h.email("I need help"), 201)
    h.ok(h.post(f"/svc/v1/tickets/{r['ticket_id']}/reply", {"request_id": rid(), "text": "ok"}, andre=True), 201)
    for _ in range(5):
        assert h.ok(h.job("outbound-tick"))["failed"] == 1
    last = h.ok(h.job("outbound-tick"))
    assert last["cancelled"] == 1 and h.ok(h.get("/svc/v1/outbound"))[0]["reason"] == "PROVIDER_FAILED"
    t = h.ok(h.get(f"/svc/v1/tickets/{r['ticket_id']}"))
    assert t["status"] == "open" and t["queue"] == "andre"          # the answer will not go: back to Andre


def test_an_answer_whose_article_was_edited_before_sending_is_cancelled(tmp_path):
    h, senders = _wired(tmp_path)
    h.article()
    r = h.ok(h.email("what are your opening hours"), 201)
    h.ok(h.post("/svc/v1/kb/articles", {"request_id": rid(), "item_id": "hours", "brands": ["zbm"],
                                        "channels": ["email"], "title": "t", "answer": "changed",
                                        "rules": {"any": ["hours"]}}), 201)
    assert h.ok(h.job("outbound-tick"))["cancelled"] == 1 and not senders["email"].sent
    assert h.ok(h.get(f"/svc/v1/tickets/{r['ticket_id']}"))["queue"] == "andre"


def test_a_send_whose_result_could_not_be_recorded_is_never_sent_twice(tmp_path):
    h, senders = _wired(tmp_path)
    r = h.ok(h.email("I need help"), 201)
    h.ok(h.post(f"/svc/v1/tickets/{r['ticket_id']}/reply", {"request_id": rid(), "text": "ok"}, andre=True), 201)
    real = h.svc._commit

    def fail_result(kind, data, actor):
        if kind == "outbound_result":
            raise __import__("errors").Unavailable("LEDGER_UNAVAILABLE")
        return real(kind, data, actor)
    h.svc._commit = fail_result
    assert h.job("outbound-tick").status_code == 503
    h.svc._commit = real
    assert h.ok(h.job("outbound-tick"))["sent"] == 1
    assert len(senders["email"].sent) == 1
    assert h.ok(h.get("/svc/v1/outbound"))[-1]["status"] == "sent"


def test_a_message_whose_body_is_gone_is_cancelled_not_stuck(tmp_path):
    h, senders = _wired(tmp_path)
    r = h.ok(h.email("I need help"), 201)
    rep = h.ok(h.post(f"/svc/v1/tickets/{r['ticket_id']}/reply", {"request_id": rid(), "text": "a unique reply"},
                      andre=True), 201)
    h.svc.bodies._mem.pop(h.svc.messages[rep["message_id"]]["body_sha256"])
    assert h.ok(h.job("outbound-tick"))["cancelled"] == 1 and not senders["email"].sent


def test_a_changed_phone_number_no_longer_finds_the_old_contact(h):
    cid = h.contact(phone="+13105551234")
    h.ok(h.post("/svc/v1/contacts", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:acme",
                                     "phone": "+13105559876"}, caller="hub"), 201)
    assert h.ok(h.sms("hello", frm="+13105559876"), 201)["contact_id"] == cid
    assert h.ok(h.sms("hello", frm="+13105551234"), 201)["contact_id"] != cid
