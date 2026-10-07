"""Inbound replies (ADR 0013 decision 13): deterministic classes; opt-outs suppress at once; the text is never kept."""

from __future__ import annotations

import json
import os

import pytest

from helpers import Harness, rid, wired_ports
from intelligences import i10_replies


@pytest.mark.parametrize("text,cls", [
    ("STOP", "unsubscribe"), ("stop all", "unsubscribe"), ("Unsubscribe", "unsubscribe"), ("cancel", "unsubscribe"),
    ("Please remove me from your list", "unsubscribe"), ("Do not contact me again", "unsubscribe"),
    ("Not interested, thanks", "unsubscribe"), ("no thank you", "unsubscribe"),
    ("I am out of the office until Monday", "out_of_office"), ("Automatic reply: on vacation", "out_of_office"),
    ("Yes, I'm interested. Can we book a time?", "interested"), ("Send me the pricing", "interested"),
    ("Who is this?", "review"), ("What's your address?", "review")])
def test_classifier_table(text, cls):
    assert i10_replies.classify(text) == cls


def setup(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    lead = h.vlead()
    t = h.template()
    h.ok(h.post("/sales/v1/outreach/email", {"request_id": rid(), "contact_id": lead["contact_id"],
                                             "template_id": t["template_id"], "version": 1}, "sales_agent"), 201)
    h.ok(h.job("send-queue"))
    return h, lead, t, h.ports.email.sent[0][0]


def reply(h, mid, text, channel="email"):
    return h.ok(h.post("/sales/v1/replies", {"request_id": rid(), "channel": channel, "message_id": mid,
                                             "text": text}, "provider_events"), 201)


def test_email_unsubscribe_reply_suppresses_both_brands(tmp_path):
    h, lead, _, mid = setup(tmp_path)
    assert reply(h, mid, "please unsubscribe me")["suppressed"] is True
    zbc = h.template(brand="zbc", name="clips", subject="Clips for {{company}}", body="Hi {{first_name}}.")
    h.refused(h.post("/sales/v1/outreach/email", {"request_id": rid(), "contact_id": lead["contact_id"],
                                                  "template_id": zbc["template_id"], "version": 1}, "sales_agent"),
              403, "SUPPRESSED")


def test_interested_reply_holds_the_phone_and_a_person_follows_up(tmp_path):
    h, lead, _, mid = setup(tmp_path)
    r = reply(h, mid, "Sounds good, let's talk Tuesday")
    task = h.ok(h.get("/sales/v1/tasks?status=open"))[0]
    assert r["class"] == "interested" and r["held"] is True and task["kind"] == "review_reply"


def test_interested_from_an_email_only_contact_opens_a_book_call_task(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    lead = h.vlead(phone=None)
    t = h.template()
    h.ok(h.post("/sales/v1/outreach/email", {"request_id": rid(), "contact_id": lead["contact_id"],
                                             "template_id": t["template_id"], "version": 1}, "sales_agent"), 201)
    h.ok(h.job("send-queue"))
    r = reply(h, h.ports.email.sent[0][0], "Sounds good, let's talk Tuesday")
    task = h.ok(h.get("/sales/v1/tasks?status=open"))[0]
    assert r["held"] is False and task["kind"] == "book_call" and task["target"] == f"contact:{lead['contact_id']}"


def test_an_exact_out_of_office_reply_reschedules_a_week_out(tmp_path):
    h, _, _, mid = setup(tmp_path)
    assert reply(h, mid, "  Out of Office ")["held"] is False
    task = h.ok(h.get("/sales/v1/tasks"))[0]
    assert task["kind"] == "reschedule" and task["due_on"] == "2026-10-13"
    assert reply(h, mid, "Out of office until the 20th")["held"] is True


def test_anything_else_goes_to_human_review(tmp_path):
    h, _, _, mid = setup(tmp_path)
    assert reply(h, mid, "Who gave you my email?")["class"] == "review"
    assert h.ok(h.get("/sales/v1/tasks"))[0]["kind"] == "review_reply"


def test_reply_text_is_never_stored_or_exported(tmp_path):
    d = str(tmp_path / "d")
    h = Harness(tmp_path, data_dir=d, ports=wired_ports())
    lead = h.vlead()
    t = h.template()
    h.ok(h.post("/sales/v1/outreach/email", {"request_id": rid(), "contact_id": lead["contact_id"],
                                             "template_id": t["template_id"], "version": 1}, "sales_agent"), 201)
    h.ok(h.job("send-queue"))
    secret = "my private cell is 555 867 5309 call me"
    reply(h, h.ports.email.sent[0][0], secret)
    raw = open(os.path.join(d, "sales_log.jsonl"), "rb").read().decode()
    assert "867 5309" not in raw
    assert "867 5309" not in json.dumps(h.ok(h.get("/sales/v1/audit/export")))
    assert "867 5309" not in json.dumps(h.ledger.events)


def test_reply_without_a_sender_is_recorded_for_a_person(h):
    # sweep A: a reply is never refused (was 422 REPLY_SENDER_REQUIRED); nothing resolves, so a person reviews it
    r = h.ok(h.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "text": "stop"},
                    "provider_events"), 201)
    assert r["class"] == "unsubscribe" and r["suppressed"] is False and r["task_id"]
    assert h.svc.tasks[r["task_id"]]["kind"] == "review_reply" and not h.svc.suppression


def test_only_the_provider_relay_posts_replies(h):
    h.refused(h.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "from_email": "a@b.test",
                                           "text": "stop"}, "sales_agent"), 403, "CALLER_NOT_ALLOWED")


def test_reply_ledger_down_nothing_recorded(tmp_path):
    h, lead, t, mid = setup(tmp_path)
    h.ledger.fail = True
    h.refused(h.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "message_id": mid,
                                           "text": "STOP"}, "provider_events"), 503, "LEDGER_UNAVAILABLE")
    assert not h.svc.suppression


def test_an_opt_out_on_email_also_stops_texts_and_calls(tmp_path):
    h, lead, _, mid = setup(tmp_path)
    h.ok(h.consent(lead["contact_id"]), 201)
    reply(h, mid, "unsubscribe")
    c = h.ok(h.get(f"/sales/v1/contacts/{lead['contact_id']}"))
    assert c["email_suppressed"] and c["phone_suppressed"] and not c["consent"]["sms:zbm"]
