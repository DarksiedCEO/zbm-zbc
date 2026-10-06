"""Texts and calls (TCPA; ADR 0013 decision 12): only with recorded express consent for that channel and brand, only
08:00-21:00 recipient-local, unknown time zone refused; revocation is global and suppresses."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from helpers import CONSENT_SHA, rid


def sms(h, contact_id, t, request_id=None):
    return h.post("/sales/v1/outreach/sms", {"request_id": request_id or rid(), "contact_id": contact_id,
                                             "template_id": t["template_id"], "version": 1}, "sales_agent")


def voice(h, contact_id, brand="zbm"):
    return h.post("/sales/v1/outreach/voice", {"request_id": rid(), "contact_id": contact_id, "brand": brand,
                                               "purpose": "book_call"}, "sales_agent")


def sms_template(h, brand="zbm"):
    return h.template(brand=brand, channel="sms", name=f"sms_{brand}_{uuid.uuid4().hex[:8]}", subject=None,
                      body="Hi {{first_name}}, following up on your request. Want a quick call?")


def test_cold_sms_without_consent_refused(w):
    lead = w.lead()
    w.refused(sms(w, lead["contact_id"], sms_template(w)), 403, "CONSENT_REQUIRED")


def test_cold_call_without_consent_refused(w):
    w.refused(voice(w, w.lead()["contact_id"]), 403, "CONSENT_REQUIRED")


def test_sms_with_consent_is_sent_with_stop_language(w):
    lead = w.lead()
    w.ok(w.consent(lead["contact_id"]), 201)
    w.ok(sms(w, lead["contact_id"], sms_template(w)), 201)
    assert w.ok(w.job("send-queue"))["sent"] == 1
    _, to, msg = w.ports.sms.sent[0]
    assert to == "+13105550100" and msg["body"].startswith("Z Best Media: Hi Jane") and \
        msg["body"].endswith("Reply STOP to opt out.")
    assert w.ledger.of_type("consent_granted") and w.ledger.of_type("outreach_send")


def test_consent_to_one_brand_is_not_consent_to_the_other(w):
    lead = w.lead()
    w.ok(w.consent(lead["contact_id"], brand="zbm"), 201)
    w.refused(sms(w, lead["contact_id"], sms_template(w, "zbc")), 403, "CONSENT_REQUIRED")


def test_consent_for_sms_is_not_consent_for_calls(w):
    lead = w.lead()
    w.ok(w.consent(lead["contact_id"], channel="sms"), 201)
    w.refused(voice(w, lead["contact_id"]), 403, "CONSENT_REQUIRED")
    w.ok(w.consent(lead["contact_id"], channel="voice"), 201)
    w.ok(voice(w, lead["contact_id"]), 201)


@pytest.mark.parametrize("utc_hour,tz,ok", [(5, "America/Los_Angeles", False),   # 22:00 PDT
                                            (4, "America/Los_Angeles", True),    # 21:00 is out: 4 UTC = 21 PDT
                                            (14, "America/Los_Angeles", False),  # 07:00 PDT
                                            (15, "America/Los_Angeles", True),   # 08:00 PDT
                                            (2, "America/New_York", False),      # 22:00 EDT
                                            (21, "Europe/London", False)])       # 22:00 BST
def test_quiet_hours_by_recipient_time_zone(tmp_path, utc_hour, tz, ok):
    from clock import FixedClock
    from helpers import Harness, wired_ports
    h = Harness(tmp_path, ports=wired_ports(), clock=FixedClock(datetime(2026, 10, 7, utc_hour, tzinfo=timezone.utc)))
    lead = h.lead(tz=tz, phone="+13105550100" if tz.startswith("America/") else "+442071838750")
    h.ok(h.consent(lead["contact_id"]), 201)
    r = sms(h, lead["contact_id"], sms_template(h))
    if utc_hour == 4:
        h.refused(r, 403, "QUIET_HOURS")              # 21:00 sharp is outside 08:00-21:00
    elif ok:
        assert r.status_code == 201
    else:
        h.refused(r, 403, "QUIET_HOURS")


def test_unknown_time_zone_refused(w):
    lead = w.lead(tz=None)
    w.ok(w.consent(lead["contact_id"]), 201)
    w.refused(sms(w, lead["contact_id"], sms_template(w)), 403, "TIME_ZONE_UNKNOWN")
    w.ok(w.consent(lead["contact_id"], channel="voice"), 201)
    w.refused(voice(w, lead["contact_id"]), 403, "TIME_ZONE_UNKNOWN")
    w.ok(w.post(f"/sales/v1/contacts/{lead['contact_id']}/time-zone", {"request_id": rid(),
                                                                         "time_zone": "America/Chicago"}, "hub"))
    w.ok(sms(w, lead["contact_id"], sms_template(w)), 201)


def test_queued_text_waits_for_the_window_at_send_time(w):
    lead = w.lead()
    w.ok(w.consent(lead["contact_id"]), 201)
    w.ok(sms(w, lead["contact_id"], sms_template(w)), 201)
    w.clock.advance(hours=11)                          # 22:00 in Los Angeles
    r = w.ok(w.job("send-queue"))
    assert r["deferred_quiet_hours"] == 1 and w.ports.sms.sent == []
    w.clock.advance(hours=11)                          # 09:00 next day
    assert w.ok(w.job("send-queue"))["sent"] == 1


def test_revocation_covers_every_channel_and_brand_and_cancels_the_queue(w):
    lead = w.lead()
    cid = lead["contact_id"]
    for ch in ("sms", "voice"):
        for b in ("zbm", "zbc"):
            w.ok(w.consent(cid, channel=ch, brand=b), 201)
    queued = w.ok(sms(w, cid, sms_template(w)), 201)
    w.ok(w.post("/sales/v1/consents/revoke", {"request_id": rid(), "contact_id": cid, "source": "form"}, "hub"))
    c = w.ok(w.get(f"/sales/v1/contacts/{cid}"))
    assert not any(c["consent"].values()) and c["phone_suppressed"] is True
    assert w.svc.messages[queued["message_id"]]["status"] == "cancelled"
    w.refused(voice(w, cid, "zbc"), 403, "SUPPRESSED")
    w.refused(w.consent(cid), 403, "SUPPRESSED")       # a later "consent" cannot undo the opt-out
    assert w.ledger.of_type("consent_revoked")


def test_stop_reply_revokes_and_suppresses_immediately(w):
    lead = w.lead()
    cid = lead["contact_id"]
    w.ok(w.consent(cid), 201)
    w.ok(sms(w, cid, sms_template(w)), 201)
    w.ok(w.job("send-queue"))
    mid = w.ports.sms.sent[0][0]
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "sms", "message_id": mid, "text": "STOP"},
                    "provider_events"), 201)
    assert r["class"] == "unsubscribe" and r["suppressed"] is True
    w.refused(sms(w, cid, sms_template(w)), 403, "SUPPRESSED")


def test_stop_from_an_unknown_number_still_suppresses_it(w):
    w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "sms", "from_phone": "+1 (310) 555-0100",
                                      "text": "stop"}, "provider_events"), 201)
    lead = w.lead()
    w.refused(w.consent(lead["contact_id"]), 403, "SUPPRESSED")


def test_consent_in_the_future_refused(w):
    lead = w.lead()
    r = w.post("/sales/v1/consents", {"request_id": rid(), "contact_id": lead["contact_id"], "channel": "sms",
                                      "brand": "zbm", "source": "web_form", "captured_at": "2027-01-01T00:00:00Z",
                                      "consent_text_version": "v1", "consent_text_sha256": CONSENT_SHA}, "hub")
    w.refused(r, 422, "CONSENT_IN_FUTURE")


def test_consent_needs_the_consent_text_hash(w):
    lead = w.lead()
    r = w.post("/sales/v1/consents", {"request_id": rid(), "contact_id": lead["contact_id"], "channel": "sms",
                                      "brand": "zbm", "source": "web_form", "captured_at": "2026-10-06T17:00:00Z",
                                      "consent_text_version": "v1"}, "hub")
    assert r.status_code == 422


def test_consent_only_from_capture_points(w):
    lead = w.lead()
    for caller in ("sales_agent", "detection", "scheduler"):
        r = w.post("/sales/v1/consents", {"request_id": rid(), "contact_id": lead["contact_id"], "channel": "sms",
                                          "brand": "zbm", "source": "web_form", "captured_at": "2026-10-06T17:00:00Z",
                                          "consent_text_version": "v1", "consent_text_sha256": CONSENT_SHA}, caller)
        w.refused(r, 403, "CALLER_NOT_ALLOWED")


def test_ledger_down_no_consent_change(w):
    lead = w.lead()
    w.ledger.fail = True
    w.refused(w.consent(lead["contact_id"]), 503, "LEDGER_UNAVAILABLE")
    w.ledger.fail = False
    assert not w.ok(w.get(f"/sales/v1/contacts/{lead['contact_id']}"))["consent"]["sms:zbm"]


def test_consent_revoked_between_queue_and_send_cancels(w):
    lead = w.lead()
    w.ok(w.consent(lead["contact_id"]), 201)
    msg = w.ok(sms(w, lead["contact_id"], sms_template(w)), 201)
    w.svc.consents[next(iter(w.svc.consents))].append({"event": "revoked", "at": "x", "source": "test"})
    r = w.ok(w.job("send-queue"))
    assert r["cancelled"] == 1 and w.ports.sms.sent == []
    assert w.svc.messages[msg["message_id"]]["reason"] == "CONSENT_REQUIRED"


def test_voice_not_wired_stays_queued(h):
    lead = h.lead()
    h.ok(h.consent(lead["contact_id"], channel="voice"), 201)
    h.ok(voice(h, lead["contact_id"]), 201)
    assert h.ok(h.job("send-queue"))["not_wired"] == 1
