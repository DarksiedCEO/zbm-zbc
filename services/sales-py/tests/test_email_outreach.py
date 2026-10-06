"""Cold email (CAN-SPAM; ADR 0013 decisions 10-11): separate outreach domain, Andre-approved templates bound to their
hash, postal address and one-click unsubscribe in every message, global append-only suppression, warm-up pace,
recorded before sent."""

from __future__ import annotations

import pytest

from helpers import ANDRE, CALLERS, FakeSender, Harness, rid, wired_ports


def queue(h, contact_id, t, version=1, request_id=None):
    return h.post("/sales/v1/outreach/email", {"request_id": request_id or rid(), "contact_id": contact_id,
                                               "template_id": t["template_id"], "version": version}, "sales_agent")


def test_queue_and_send_with_footer_unsubscribe_and_honest_from(w):
    lead = w.vlead()
    t = w.template()
    msg = w.ok(queue(w, lead["contact_id"], t), 201)
    assert msg["status"] == "queued" and msg["from_domain"] == "zbm-outreach.test"
    r = w.ok(w.job("send-queue"))
    assert r["sent"] == 1
    mid, to, sent = w.ports.email.sent[0]
    assert to == "jane@acme-shop.test" and mid == msg["message_id"]
    assert sent["from"] == "Z Best Media <hello@zbm-outreach.test>"
    assert "123 Test Street, Suite 4, Los Angeles, CA 90001" in sent["body"]
    assert sent["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert sent["headers"]["List-Unsubscribe"].startswith("<https://zbm-outreach.test/u/sl-msg-")
    assert sent["subject"] == "Quick idea for Acme Shop" and sent["body"].startswith("Hi Jane,")
    assert w.ok(w.get("/sales/v1/outreach/messages?status=sent", "sales_agent"))[0]["provider_ref"] == "prov-1"


def test_send_is_on_the_ledger_before_the_provider_is_called(w):
    lead = w.vlead()
    msg = w.ok(queue(w, lead["contact_id"], w.template()), 201)
    seen = {}
    real = w.ports.email.send

    def spy(message_id, to, m):
        seen["recorded"] = [e for e in w.ledger.of_type("outreach_send") if e["subject_id"] == f"msg:{message_id}"]
        return real(message_id, to, m)
    w.ports.email.send = spy
    w.ok(w.job("send-queue"))
    assert len(seen["recorded"]) == 1 and msg["message_id"] in seen["recorded"][0]["subject_id"]


def test_ledger_down_means_nothing_is_sent(w):
    lead = w.vlead()
    w.ok(queue(w, lead["contact_id"], w.template()), 201)
    w.ledger.fail = True
    r = w.job("send-queue")
    assert r.status_code == 503 and r.json()["detail"] == "LEDGER_UNAVAILABLE"
    assert w.ports.email.sent == []
    w.ledger.fail = False
    assert w.ok(w.job("send-queue"))["sent"] == 1


def test_not_wired_provider_leaves_the_message_queued_visibly(h):
    lead = h.vlead()
    h.ok(queue(h, lead["contact_id"], h.template()), 201)
    r = h.ok(h.job("send-queue"))
    assert r["not_wired"] == 1 and r["sent"] == 0
    assert h.ok(h.get("/sales/v1/status"))["queued"] == 1
    assert h.ledger.of_type("outreach_send") == []


def test_no_outreach_domain_no_cold_email(tmp_path):
    h = Harness(tmp_path, SALES_OUTREACH_DOMAIN=None, SALES_POSTAL_ADDRESS=None)
    lead = h.vlead()
    h.refused(queue(h, lead["contact_id"], h.template()), 403, "OUTREACH_NOT_CONFIGURED")


def test_template_not_approved_refused(w):
    lead = w.vlead()
    w.refused(queue(w, lead["contact_id"], w.template(approve=False)), 403, "TEMPLATE_NOT_APPROVED")


def test_template_edited_after_approval_is_refused_at_queue_and_at_send(w):
    lead = w.vlead()
    t = w.template()
    w.ok(queue(w, lead["contact_id"], t), 201)                     # queued against the approved hash
    w.ok(w.post(f"/sales/v1/templates/{t['template_id']}/versions/1/edit",
                {"request_id": rid(), "subject": "Quick idea for {{company}}", "body": "New words, never approved."},
                "sales_agent"))
    w.refused(queue(w, lead["contact_id"], t), 403, "TEMPLATE_HASH_MISMATCH")
    r = w.ok(w.job("send-queue"))
    assert r["cancelled"] == 1 and w.ports.email.sent == []
    assert w.ok(w.get("/sales/v1/outreach/messages"))[0]["reason"] == "TEMPLATE_HASH_MISMATCH"


def test_template_tampered_in_memory_is_caught_by_the_hash(w):
    lead = w.vlead()
    t = w.template()
    w.svc.templates[t["template_id"]]["versions"]["1"]["body"] = "tampered"
    w.refused(queue(w, lead["contact_id"], t), 403, "TEMPLATE_HASH_MISMATCH")


def test_andre_approves_only_the_hash_he_names(w):
    t = w.template(approve=False)
    r = w.andre(f"/sales/v1/templates/{t['template_id']}/versions/1/approve", {"request_id": rid(),
                                                                                "content_sha256": "0" * 64})
    w.refused(r, 409, "TEMPLATE_HASH_MISMATCH")
    sha = t["versions"][0]["content_sha256"]
    for token in (None, "wrong-" + "x" * 40, CALLERS["dashboard"]):
        r = w.post(f"/sales/v1/templates/{t['template_id']}/versions/1/approve",
                   {"request_id": rid(), "content_sha256": sha}, "dashboard", token)
        assert r.status_code == 403
    r = w.post(f"/sales/v1/templates/{t['template_id']}/versions/1/approve",
               {"request_id": rid(), "content_sha256": sha}, "sales_agent", ANDRE)
    w.refused(r, 403, "CALLER_NOT_ALLOWED")                                   # Andre's token only via the console
    assert w.ledger.of_type("founder_approval_refused")


@pytest.mark.parametrize("subject", ["Re: our call", "FWD: invoice", "URGENT: read this", "Your invoice is ready",
                                     "Action required on your account", "You've won a free audit",
                                     "Guaranteed results", "BIG NEWS FOR YOU", "Hello!!", "Verify your store",
                                     "Payment failed", "Final notice"])
def test_deceptive_subjects_refused(w, subject):
    r = w.post("/sales/v1/templates", {"request_id": rid(), "brand": "zbm", "channel": "email", "name": "bad",
                                       "subject": subject, "body": "Hi"}, "sales_agent")
    w.refused(r, 422, "SUBJECT_DECEPTIVE")


def test_unknown_merge_field_refused(w):
    w.refused(w.post("/sales/v1/templates", {"request_id": rid(), "brand": "zbm", "channel": "email", "name": "x",
                                             "subject": "Hi", "body": "Hi {{ssn_last4}}"}, "sales_agent"),
              422, "PLACEHOLDER_UNKNOWN")


def test_only_the_sales_agent_queues_outreach(w):
    lead = w.vlead()
    t = w.template()
    for caller in ("dashboard", "hub", "scheduler"):
        r = w.post("/sales/v1/outreach/email", {"request_id": rid(), "contact_id": lead["contact_id"],
                                                "template_id": t["template_id"], "version": 1}, caller)
        w.refused(r, 403, "CALLER_NOT_ALLOWED")


def test_one_click_unsubscribe_then_resend_refused_across_both_brands(w):
    lead = w.vlead()
    zbm = w.template()
    zbc = w.template(brand="zbc", name="clips", subject="Clips for {{company}}",
                     body="Hi {{first_name}}, we run clipping campaigns.")
    w.ok(queue(w, lead["contact_id"], zbm), 201)
    w.ok(w.job("send-queue"))
    token = w.ports.email.sent[0][2]["headers"]["List-Unsubscribe"].rsplit("/", 1)[1].rstrip(">")
    queued_zbc = w.ok(queue(w, lead["contact_id"], zbc), 201)
    w.ok(w.post("/sales/v1/unsubscribe", {"request_id": rid(), "token": token}, "hub"))
    assert w.ok(w.get("/sales/v1/outreach/messages?status=cancelled"))[0]["message_id"] == queued_zbc["message_id"]
    w.refused(queue(w, lead["contact_id"], zbm), 403, "SUPPRESSED")
    w.refused(queue(w, lead["contact_id"], zbc), 403, "SUPPRESSED")
    assert w.ledger.of_type("suppression_added")
    w.ok(w.job("send-queue"))
    assert len(w.ports.email.sent) == 1


def test_forged_unsubscribe_token_refused(w):
    w.refused(w.post("/sales/v1/unsubscribe", {"request_id": rid(), "token": "sl-msg-" + "0" * 40 + "." + "0" * 32},
                     "hub"), 404, "UNSUBSCRIBE_TOKEN_UNKNOWN")


def test_suppression_is_append_only_there_is_no_way_to_remove(w):
    fastapi_app = w.client.app.app.app                       # InputLimits(NoStore(FastAPI))
    routes = [(r.path, sorted(getattr(r, "methods", []))) for r in fastapi_app.routes if "suppression" in r.path]
    assert routes and all("DELETE" not in m for _, m in routes)
    assert not any("remove" in p or "delete" in p or "lift" in p for p, _ in routes)
    lead = w.vlead()
    w.ok(w.post("/sales/v1/suppressions", {"request_id": rid(), "contact_id": lead["contact_id"],
                                           "reason": "manual"}), 201)
    r = w.client.delete("/sales/v1/suppressions", headers=w.headers())
    assert r.status_code == 405
    assert len(w.ok(w.get("/sales/v1/suppressions"))) == 2


def test_hub_suppression_by_raw_email_matches_a_contact_later(w):
    w.ok(w.post("/sales/v1/suppressions", {"request_id": rid(), "email": "JANE@acme-shop.test",
                                           "reason": "unsubscribe"}, "hub"), 201)
    lead = w.vlead()
    w.refused(queue(w, lead["contact_id"], w.template()), 403, "SUPPRESSED")
    assert "jane@" not in str(w.ok(w.get("/sales/v1/suppressions")))


@pytest.mark.parametrize("event", ["hard_bounce", "complaint"])
def test_hard_bounce_and_complaint_suppress(w, event):
    lead = w.vlead()
    t = w.template()
    msg = w.ok(queue(w, lead["contact_id"], t), 201)
    w.ok(w.job("send-queue"))
    w.ok(w.post("/sales/v1/events/email", {"request_id": rid(), "message_id": msg["message_id"], "event": event},
                "provider_events"))
    w.refused(queue(w, lead["contact_id"], t), 403, "SUPPRESSED")


def test_soft_bounce_does_not_suppress_and_unsent_message_events_refused(w):
    lead = w.vlead()
    t = w.template()
    msg = w.ok(queue(w, lead["contact_id"], t), 201)
    w.refused(w.post("/sales/v1/events/email", {"request_id": rid(), "message_id": msg["message_id"],
                                                "event": "complaint"}, "provider_events"), 409, "MESSAGE_NOT_SENT")
    w.ok(w.job("send-queue"))
    w.ok(w.post("/sales/v1/events/email", {"request_id": rid(), "message_id": msg["message_id"],
                                           "event": "soft_bounce"}, "provider_events"))
    w.ok(queue(w, lead["contact_id"], t), 201)


def _leads(h, n):
    return [h.vlead(email=f"p{i}@shop{i}.test", phone=None, account={"name": f"Shop {i}"})["contact_id"]
            for i in range(n)]


def test_daily_cap_starts_low_and_holds_the_rest_in_the_queue(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), SALES_WARMUP_SCHEDULE="3,5,8")
    t = h.template()
    for cid in _leads(h, 5):
        h.ok(queue(h, cid, t), 201)
    r = h.ok(h.job("send-queue"))
    assert (r["sent"], r["capped"]) == (3, 2)
    assert h.ok(h.job("send-queue"))["sent"] == 0                 # same day: still capped
    h.clock.advance(days=1)
    assert h.ok(h.job("warmup-reset"))["step"] == 2
    r = h.ok(h.job("send-queue"))
    assert r["sent"] == 2


def test_warmup_advances_once_a_day_and_holds_on_complaints(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), SALES_WARMUP_SCHEDULE="2,4,8")
    assert h.ok(h.job("warmup-reset"))["held"] == "NOT_STARTED"
    t = h.template()
    cids = _leads(h, 2)
    msgs = [h.ok(queue(h, c, t), 201) for c in cids]
    h.ok(h.job("send-queue"))
    h.ok(h.post("/sales/v1/events/email", {"request_id": rid(), "message_id": msgs[0]["message_id"],
                                           "event": "complaint"}, "provider_events"))
    h.clock.advance(days=1)
    r = h.ok(h.job("warmup-reset"))
    assert r["held"] == "HELD_BY_YESTERDAY" and r["step"] == 1 and r["cap_today"] == 2


def test_warmup_is_bounded_by_the_daily_cap(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), SALES_WARMUP_SCHEDULE="5,10", SALES_DAILY_SEND_CAP="7")
    t = h.template()
    h.ok(queue(h, _leads(h, 1)[0], t), 201)
    h.ok(h.job("send-queue"))
    h.clock.advance(days=1)
    assert h.ok(h.job("warmup-reset"))["cap_today"] == 7
    assert h.ok(h.job("warmup-reset"))["held"] == "ALREADY_ADVANCED_TODAY"


def test_provider_failure_is_recorded_and_not_retried(tmp_path):
    ports = wired_ports(email=FakeSender("failed"))
    h = Harness(tmp_path, ports=ports)
    h.ok(queue(h, h.vlead()["contact_id"], h.template()), 201)
    assert h.ok(h.job("send-queue"))["failed"] == 1
    assert h.ok(h.job("send-queue"))["failed"] == 0 and len(ports.email.sent) == 1


def test_queue_is_idempotent_and_a_changed_body_is_409(w):
    lead = w.vlead()
    t = w.template()
    request_id = rid()
    a = w.ok(queue(w, lead["contact_id"], t, request_id=request_id), 201)
    b = w.ok(queue(w, lead["contact_id"], t, request_id=request_id), 201)
    assert a["message_id"] == b["message_id"] and len(w.svc.messages) == 1
    t2 = w.template(name="second", subject="Another idea for {{company}}")
    w.refused(queue(w, lead["contact_id"], t2, request_id=request_id), 409, "REQUEST_ID_REUSED")


def test_send_tick_is_idempotent_per_request(w):
    w.ok(queue(w, w.vlead()["contact_id"], w.template()), 201)
    request_id = rid()
    assert w.ok(w.job("send-queue", request_id))["sent"] == 1
    again = w.ok(w.job("send-queue", request_id))
    assert again["already_ran"] is True and len(w.ports.email.sent) == 1


def test_cancel_a_queued_message(w):
    msg = w.ok(queue(w, w.vlead()["contact_id"], w.template()), 201)
    w.ok(w.post(f"/sales/v1/outreach/messages/{msg['message_id']}/cancel", {"request_id": rid()}, "sales_agent"))
    w.ok(w.job("send-queue"))
    assert w.ports.email.sent == []


def test_contact_without_email_cannot_be_emailed(w):
    lead = w.vlead(email=None)
    w.refused(queue(w, lead["contact_id"], w.template()), 422, "CONTACT_NO_EMAIL")
