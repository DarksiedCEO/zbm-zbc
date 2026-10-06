"""Outreach: email only, from Andre-approved templates matched by hash, CAN-SPAM footer and one-click unsubscribe,
suppression shared across both brands, any reply holds further automatic outreach until Andre decides."""

from datetime import timedelta

from helpers import POSTAL, Harness, RecordingSender, rid, wired_ports


def _wired(tmp_path, **env):
    return Harness(tmp_path, ports=wired_ports(), **env)


def test_queue_requires_named_approved_hash(h):
    c = h.contact()
    t = h.template(approve=False)
    h.refused(h.queue(c, t), 403, "TEMPLATE_NOT_APPROVED")
    v = t["versions"][0]
    t = h.ok(h.post(f"/templates/{t['template_id']}/versions/1/approve",
                    {"request_id": rid(), "content_sha256": v["content_sha256"]}, andre=True))
    h.refused(h.queue(c, t, sha="0" * 64), 403, "TEMPLATE_HASH_MISMATCH")
    assert h.ok(h.queue(c, t), 201)["status"] == "queued"


def test_template_approval_andre_only_exact_hash(h):
    t = h.template(approve=False)
    v = t["versions"][0]
    url = f"/templates/{t['template_id']}/versions/1/approve"
    h.refused(h.post(url, {"request_id": rid(), "content_sha256": v["content_sha256"]}), 403)
    h.refused(h.post(url, {"request_id": rid(), "content_sha256": "f" * 64}, andre=True), 409,
              "TEMPLATE_HASH_MISMATCH")


def test_tampered_template_in_memory_is_caught(h):
    c = h.contact()
    t = h.template()
    h.svc.templates[t["template_id"]]["versions"]["1"]["body"] = "Hi {{first_name}}, wire money now."
    h.refused(h.queue(c, t), 403, "TEMPLATE_HASH_MISMATCH")


def test_deceptive_subjects_and_unknown_placeholders_refused(h):
    for subject in ("RE: our call", "URGENT: action required", "Your invoice is past due", "YOU HAVE WON"):
        r = h.post("/templates", {"request_id": rid(), "template_key": "bad-" + rid()[2:10], "brand": "zbm",
                                  "subject": subject, "body": "Hello."})
        h.refused(r, 422, "SUBJECT_DECEPTIVE")
    h.refused(h.post("/templates", {"request_id": rid(), "template_key": "bad-ph", "brand": "zbm",
                                    "subject": "Hello", "body": "Hi {{ssn_last4}}"}), 422, "PLACEHOLDER_UNKNOWN")


def test_merge_values_only_from_console(h):
    c = h.contact(verify=False)
    t = h.template()
    h.refused(h.queue(c, t), 403, "MERGE_FIELD_REFUSED")
    h.refused(h.post(f"/contacts/{c['contact_id']}/merge-fields", {"request_id": rid(), "first_name": "Pat"}), 403)
    h.refused(h.post(f"/contacts/{c['contact_id']}/merge-fields", {"request_id": rid(), "company": "evil.com"},
                     caller="dashboard"), 422)


def test_template_brand_must_match_contact(h):
    c = h.contact(brand="zbc")
    t = h.template()
    h.refused(h.queue(c, t), 403, "TEMPLATE_BRAND_MISMATCH")


def test_no_phone_no_sms(h):
    r = h.post("/contacts", {"request_id": rid(), "brand": "zbm", "email": "a@b.test", "name": "A",
                             "phone": "+13105551234"})
    h.refused(r, 422)
    assert "+13105551234" not in r.text
    assert h.client.post("/nbd/v1/outreach/sms", json={}, headers=h.headers("bizdev_agent")).status_code == 404
    assert not any(k in h.svc.ports.wired() for k in ("sms", "voice"))


def test_outreach_not_configured_refuses(tmp_path):
    h = Harness(tmp_path, NBD_OUTREACH_DOMAIN=None, NBD_POSTAL_ADDRESS=None)
    c = h.contact()
    t = h.template()
    h.refused(h.queue(c, t), 403, "OUTREACH_NOT_CONFIGURED")


def test_send_tick_stays_queued_without_port(h):
    h.ok(h.queue(h.contact(), h.template()), 201)
    assert h.ok(h.job("send-queue"))["not_wired"] == 1
    assert not h.ledger.of_type("outreach_send")


def test_wired_send_carries_can_spam_footer(tmp_path):
    h = _wired(tmp_path)
    m = h.ok(h.queue(h.contact(), h.template()), 201)
    assert h.ok(h.job("send-queue"))["sent"] == 1
    mid, to, msg = h.ports.email.sent[0]
    assert to == "pat@westagency.test" and mid == m["message_id"]
    assert POSTAL in msg["body"] and "unsubscribe" in msg["body"].lower()
    assert msg["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert msg["from"].endswith("@zbm-partners.test>") and "Hi Pat" in msg["body"]
    ev = h.ledger.of_type("outreach_send")[0]
    assert "pat@" not in str(ev["_payload"]) and ev["_payload"]["to_hash"].startswith("email:")


def test_daily_cap_counts_by_clock_date(tmp_path):
    h = _wired(tmp_path, NBD_DAILY_SEND_CAP="1")
    t = h.template()
    for i in range(2):
        h.ok(h.queue(h.contact(email=f"p{i}@x.test"), t), 201)
    out = h.ok(h.job("send-queue"))
    assert out["sent"] == 1 and out["capped"] == 1
    h.clock.at = h.clock.at.replace(hour=23, minute=59)           # same date: still capped
    assert h.ok(h.job("send-queue"))["capped"] == 1
    h.clock.advance(minutes=2)                                    # next UTC date
    assert h.ok(h.job("send-queue"))["sent"] == 1


def test_any_reply_holds_until_andre_decides(tmp_path):
    h = _wired(tmp_path)
    c = h.contact()
    t = h.template()
    m1 = h.ok(h.queue(c, t), 201)
    h.ok(h.job("send-queue"))
    queued = h.ok(h.queue(c, h.template(key="follow-up", subject="Following up")), 201)
    ans = h.ok(h.post("/replies", {"request_id": rid(), "message_id": m1["message_id"],
                                   "text": "Yes! Interested, let's talk."}, caller="provider_events"), 201)
    assert ans["held"] is True and ans["class"] == "interested" and ans["suppressed"] is False
    by_id = {m["message_id"]: m for m in h.ok(h.get("/outreach/messages"))}
    assert by_id[queued["message_id"]]["status"] == "cancelled"
    assert by_id[queued["message_id"]]["reason"] == "CONTACT_HELD"
    h.refused(h.queue(c, t), 403, "CONTACT_HELD")
    h.refused(h.post(f"/holds/{ans['hold_id']}/decision", {"request_id": rid(), "decision": "resume"},
                     caller="dashboard"), 403)
    h.ok(h.post(f"/holds/{ans['hold_id']}/decision", {"request_id": rid(), "decision": "resume"}, andre=True))
    assert h.ok(h.queue(c, t), 201)["status"] == "queued"
    h.refused(h.post(f"/holds/{ans['hold_id']}/decision", {"request_id": rid(), "decision": "opt_out"}, andre=True),
              409, "HOLD_CLOSED")


def test_out_of_office_also_holds(tmp_path):
    h = _wired(tmp_path)
    c = h.contact()
    m = h.ok(h.queue(c, h.template()), 201)
    h.ok(h.job("send-queue"))
    ans = h.ok(h.post("/replies", {"request_id": rid(), "message_id": m["message_id"],
                                   "text": "I am currently out of the office."}, caller="provider_events"), 201)
    assert ans["held"] is True and ans["class"] == "out_of_office"


def test_opt_out_wording_suppresses_across_brands(tmp_path):
    h = _wired(tmp_path)
    c = h.contact()
    m = h.ok(h.queue(c, h.template()), 201)
    h.ok(h.job("send-queue"))
    ans = h.ok(h.post("/replies", {"request_id": rid(), "message_id": m["message_id"],
                                   "text": "Please r.e.m.o.v.e me -- not interested"}, caller="provider_events"), 201)
    assert ans["suppressed"] is True
    h.ok(h.post(f"/holds/{ans['hold_id']}/decision", {"request_id": rid(), "decision": "resume"}, andre=True))
    h.refused(h.queue(c, h.template(key="second")), 403, "SUPPRESSED")
    c2 = h.contact(email="other@westagency.test", brand="zbc")
    assert c2["suppressed"] is False                         # suppression is per address, never per domain


def test_opt_out_decision_suppresses(tmp_path):
    h = _wired(tmp_path)
    c = h.contact()
    m = h.ok(h.queue(c, h.template()), 201)
    h.ok(h.job("send-queue"))
    ans = h.ok(h.post("/replies", {"request_id": rid(), "message_id": m["message_id"], "text": "who is this?"},
                      caller="provider_events"), 201)
    h.ok(h.post(f"/holds/{ans['hold_id']}/decision", {"request_id": rid(), "decision": "opt_out"}, andre=True))
    assert h.ok(h.get(f"/contacts/{c['contact_id']}"))["suppressed"] is True


def test_unresolved_reply_holds_named_contact_never_suppresses_it(tmp_path):
    h = _wired(tmp_path)
    c = h.contact(email="named@client.test")
    ans = h.ok(h.post("/replies", {"request_id": rid(), "from_email": "stranger@else.test",
                                   "text": "STOP emailing named@client.test"}, caller="provider_events"), 201)
    view = h.ok(h.get(f"/contacts/{c['contact_id']}"))
    assert view["held"] is True and view["suppressed"] is False
    assert ans["suppressed"] is True                     # the sender's own address is suppressed
    h.refused(h.post("/replies", {"request_id": rid(), "text": "hello"}, caller="provider_events"), 422,
              "REPLY_SENDER_REQUIRED")


def test_reply_text_never_stored(tmp_path):
    h = _wired(tmp_path)
    c = h.contact()
    m = h.ok(h.queue(c, h.template()), 201)
    h.ok(h.job("send-queue"))
    h.ok(h.post("/replies", {"request_id": rid(), "message_id": m["message_id"], "text": "secret-reply-phrase-991"},
                caller="provider_events"), 201)
    assert "secret-reply-phrase-991" not in b"".join(h.svc.log._lines).decode()


def test_unsubscribe_token_and_bounces(tmp_path):
    h = _wired(tmp_path)
    c = h.contact()
    m = h.ok(h.queue(c, h.template()), 201)
    h.ok(h.job("send-queue"))
    token = h.svc.unsubscribe_token(m["message_id"])
    h.refused(h.post("/unsubscribe", {"request_id": rid(), "token": token[:-1] + ("0" if token[-1] != "0" else "1")},
                     caller="hub"), 404, "UNSUBSCRIBE_TOKEN_UNKNOWN")
    h.ok(h.post("/unsubscribe", {"request_id": rid(), "token": token}, caller="hub"))
    assert h.ok(h.get(f"/contacts/{c['contact_id']}"))["suppressed"] is True
    c2 = h.contact(email="bounce@x.test")
    m2 = h.ok(h.queue(c2, h.template(key="other")), 201)
    h.ok(h.job("send-queue"))
    h.ok(h.post("/events/email", {"request_id": rid(), "message_id": m2["message_id"], "event": "hard_bounce"},
                caller="provider_events"))
    assert h.ok(h.get(f"/contacts/{c2['contact_id']}"))["suppressed"] is True


def test_suppression_is_by_address_for_both_brands(tmp_path):
    h = _wired(tmp_path)
    h.ok(h.post("/suppressions", {"request_id": rid(), "email": "Pat+deals@WestAgency.test"}, caller="dashboard"), 201)
    c = h.contact(email="pat@westagency.test", brand="zbc")
    h.refused(h.queue(c, h.template(brand="zbc", key="zbc-intro")), 403, "SUPPRESSED")
    assert all("pat" not in str(x) for x in h.ok(h.get("/suppressions")))


def test_suppressed_after_queue_is_cancelled_at_send(tmp_path):
    h = _wired(tmp_path)
    c = h.contact()
    h.ok(h.queue(c, h.template()), 201)
    h.svc.suppression[c["email_hash"]] = {"hash": c["email_hash"], "reason": "manual", "at": "x", "by": "x"}
    assert h.ok(h.job("send-queue"))["cancelled"] == 1
    assert h.ports.email.sent == []


def test_provider_failure_marks_failed(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(email=RecordingSender("failed")))
    h.ok(h.queue(h.contact(), h.template()), 201)
    assert h.ok(h.job("send-queue"))["failed"] == 1


def test_queue_cap_per_caller(tmp_path):
    h = Harness(tmp_path, NBD_QUEUE_MAX_PER_CALLER="1")
    t = h.template()
    h.ok(h.queue(h.contact(email="a@x.test"), t), 201)
    h.refused(h.queue(h.contact(email="b@x.test"), t), 429, "QUEUE_FULL")


def test_contact_dedupe_by_canonical_email(h):
    h.contact(email="Pat+x@westagency.test", verify=False)
    h.refused(h.post("/contacts", {"request_id": rid(), "brand": "zbm", "email": "pat@WESTAGENCY.test",
                                   "name": "Pat"}), 409, "CONTACT_EXISTS")
    assert timedelta  # noqa: B018
