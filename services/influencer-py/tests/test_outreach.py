"""Outreach (ADR 0015 decisions 10-11): email from Andre-approved templates only, CAN-SPAM complete, from the separate
outreach domain; one suppression list across both brands; platform DMs only as Andre approved them, by hash."""

from __future__ import annotations

import pytest

from helpers import ANDRE, OUTREACH, POSTAL, Harness, rid, wired_ports


# ------------------------------------------------------------------------------------------------ templates

@pytest.mark.parametrize("subject", ["Re: our chat", "URGENT: action required", "Your invoice is ready",
                                     "You've won a collab", "FREE COLLAB OFFER", "Collab!!", "Verify your account"])
def test_deceptive_subjects_refused(h, subject):
    h.code(h.post("/templates", {"request_id": rid(), "brand": "zbm", "name": "t", "subject": subject,
                                 "body": "hi"}, caller="influencer_agent"), 422, "SUBJECT_DECEPTIVE")


def test_unknown_placeholders_refused(h):
    h.code(h.post("/templates", {"request_id": rid(), "brand": "zbm", "name": "t", "subject": "Hi {{handle}}",
                                 "body": "x"}, caller="influencer_agent"), 422, "PLACEHOLDER_UNKNOWN")


def test_andre_approves_the_exact_hash_and_only_andre(h):
    t = h.template(approve=False)
    v = t["versions"][0]
    url = f"/templates/{t['template_id']}/versions/1/approve"
    h.code(h.post(url, {"request_id": rid(), "content_sha256": v["content_sha256"]}), 403, "ANDRE_APPROVAL_REQUIRED")
    h.code(h.post(url, {"request_id": rid(), "content_sha256": v["content_sha256"]}, andre="wrong-" + "w" * 40), 403,
           "ANDRE_APPROVAL_INVALID")
    h.code(h.post(url, {"request_id": rid(), "content_sha256": v["content_sha256"]}, caller="influencer_agent",
                  andre=True), 403, "CALLER_NOT_ALLOWED")
    h.code(h.post(url, {"request_id": rid(), "content_sha256": "0" * 64}, andre=True), 409, "CONTENT_HASH_MISMATCH")
    assert len(h.ledger.of_type("founder_approval_refused")) == 2
    h.ok(h.post(url, {"request_id": rid(), "content_sha256": v["content_sha256"]}, andre=True))
    h.code(h.post(url, {"request_id": rid(), "content_sha256": v["content_sha256"]}, andre=True), 409,
           "ALREADY_APPROVED")


def test_andre_token_not_configured_refuses_every_approval(tmp_path):
    h = Harness(tmp_path, INF_ANDRE_APPROVAL_TOKEN=None)
    t = h.template(approve=False)
    h.code(h.post(f"/templates/{t['template_id']}/versions/1/approve",
                  {"request_id": rid(), "content_sha256": t["versions"][0]["content_sha256"]}, andre=ANDRE), 403,
           "ANDRE_TOKEN_NOT_CONFIGURED")


def test_a_new_version_needs_its_own_approval(w):
    inf = w.creator()
    t = w.template()
    t2 = w.ok(w.post(f"/templates/{t['template_id']}/versions", {"request_id": rid(), "subject": "Hi {{first_name}}",
                                                                 "body": "Changed copy."}, caller="influencer_agent"),
              201)
    assert t2["versions"][1]["status"] == "draft"
    w.code(w.email(inf, t, version=2), 403, "TEMPLATE_NOT_APPROVED")
    w.ok(w.email(inf, t, version=1), 201)


def test_a_tampered_approved_template_never_sends(w):
    inf = w.creator()
    t = w.template()
    m = w.ok(w.email(inf, t), 201)
    w.svc.templates[t["template_id"]]["versions"]["1"]["body"] = "Wire money to us"
    w.code(w.email(inf, t), 403, "TEMPLATE_HASH_MISMATCH")
    out = w.ok(w.job("send-queue"))
    assert out["cancelled"] == 1 and w.svc.messages[m["message_id"]]["reason"] == "TEMPLATE_HASH_MISMATCH"
    assert not w.ports.email.sent


# ------------------------------------------------------------------------------------------------ email

def test_email_stays_queued_while_no_provider_is_wired(h):
    inf = h.creator()
    t = h.template()
    m = h.ok(h.email(inf, t), 201)
    out = h.ok(h.job("send-queue"))
    assert m["status"] == "queued" and out["not_wired"] == 1 and out["sent"] == 0
    assert h.svc.messages[m["message_id"]]["status"] == "queued"


def test_email_is_can_spam_complete_and_from_the_outreach_domain(w):
    inf = w.creator()
    t = w.template()
    w.ok(w.email(inf, t), 201)
    out = w.ok(w.job("send-queue"))
    assert out["sent"] == 1
    mid, to, msg = w.ports.email.sent[0]
    assert to == "creator@example.test" and msg["from"] == f"Z Best Media <creators@{OUTREACH}>"
    assert POSTAL in msg["body"] and "unsubscribe:" in msg["body"]
    assert msg["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert msg["headers"]["List-Unsubscribe"].startswith(f"<https://{OUTREACH}/u/{mid}.")
    assert msg["subject"] == "Working together, Casey?"
    assert w.ledger.of_type("outreach_send")[0]["_payload"]["to_hash"].startswith("email:")


def test_outreach_not_configured_refuses(tmp_path):
    h = Harness(tmp_path, INF_OUTREACH_DOMAIN=None, INF_POSTAL_ADDRESS=None)
    app = h.ok(h.application(), 201)
    assert app["confirmation_status"] == "undeliverable" and not h.svc.messages     # nothing can be confirmed
    h.code(h.confirm(app["confirmation_id"]), 409, "CONFIRMATION_USED")
    p = h.prospect()
    h.ok(h.post(f"/influencers/{p['influencer_id']}/first-name", {"request_id": rid(), "first_name": "Al"}))
    t = h.template()
    h.code(h.email(p, t), 403, "OUTREACH_NOT_CONFIGURED")


def test_a_name_typed_into_a_form_never_renders(w):
    inf = w.creator(first_name=None)
    t = w.template()
    w.code(w.email(inf, t), 403, "MERGE_FIELD_REFUSED")


def test_only_the_agent_queues_email(w):
    inf = w.creator()
    t = w.template()
    for caller in ("dashboard", "hub", "scheduler"):
        w.code(w.post("/outreach/email", {"request_id": rid(), "influencer_id": inf["influencer_id"],
                                          "template_id": t["template_id"], "version": 1}, caller=caller),
               403, "CALLER_NOT_ALLOWED")


def test_daily_cap(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), INF_DAILY_SEND_CAP="2")
    t = h.template()
    for i in range(3):
        inf = h.creator(email=f"c{i}@example.test", handles=((("x", f"@c{i}"),)))
        h.ok(h.email(inf, t), 201)
    out = h.ok(h.job("send-queue"))
    assert out["sent"] == 2 and out["capped"] == 1
    h.clock.advance(days=1)
    assert h.ok(h.job("send-queue"))["sent"] == 1


def test_queue_cap_per_caller(tmp_path):
    h = Harness(tmp_path, INF_QUEUE_MAX_PER_CALLER="1")
    t = h.template()
    a = h.creator()
    b = h.creator(email="b@example.test", handles=(("x", "@bb"),))
    h.ok(h.email(a, t), 201)
    h.code(h.email(b, t), 429, "QUEUE_FULL")


# ------------------------------------------------------------------------------------------------ suppression

def test_the_one_click_link_suppresses_every_channel_for_both_brands(w):
    inf = w.creator()
    t = w.template()
    zbc = w.template(brand="zbc", name="clips")
    m = w.ok(w.email(inf, t), 201)
    token = w.svc.unsubscribe_token(m["message_id"])
    w.code(w.post("/unsubscribe", {"request_id": rid(), "token": token[:-1] + "0"}, caller="hub"), 404,
           "UNSUBSCRIBE_TOKEN_UNKNOWN")
    w.ok(w.post("/unsubscribe", {"request_id": rid(), "token": token}, caller="hub"))
    assert w.svc.messages[m["message_id"]]["status"] == "cancelled"
    w.code(w.email(inf, zbc), 403, "SUPPRESSED")
    w.code(w.post("/dm-drafts", {"request_id": rid(), "influencer_id": inf["influencer_id"], "platform": "instagram",
                                 "brand": "zbc", "text": "Hi"}, caller="influencer_agent"), 403, "SUPPRESSED")


def test_suppression_is_append_only_and_has_no_removal_route(w):
    inf = w.creator()
    w.ok(w.post("/suppressions", {"request_id": rid(), "influencer_id": inf["influencer_id"]}), 201)
    import api
    routes = {(r.path, tuple(sorted(r.methods))) for r in api.create_app(w.svc, w.settings).routes}
    assert {p for p, _ in routes if "suppress" in p} == {"/inf/v1/suppressions"}
    assert ("/inf/v1/suppressions", ("DELETE",)) not in routes
    w.code(w.post("/suppressions", {"request_id": rid()}), 422, "TARGET_REQUIRED")
    assert len(w.ok(w.get("/suppressions", caller="compliance_38"))) == 2


def test_a_suppressed_handle_blocks_the_influencer_everywhere(w):
    inf = w.creator()
    t = w.template()
    w.ok(w.post("/suppressions", {"request_id": rid(), "handle": {"platform": "instagram",
                                                                   "handle": "@Creator.One"}}, caller="hub"), 201)
    w.code(w.email(inf, t), 403, "SUPPRESSED")


def test_complaint_suppresses_everything_and_hard_bounce_the_address(w):
    a = w.creator()
    b = w.creator(email="b@example.test", handles=(("x", "@bb"),))
    t = w.template()
    ma = w.ok(w.email(a, t), 201)
    mb = w.ok(w.email(b, t), 201)
    w.code(w.post("/events/email", {"request_id": rid(), "message_id": ma["message_id"], "event": "complaint"},
                  caller="provider_events"), 409, "MESSAGE_NOT_SENT")
    w.ok(w.job("send-queue"))
    w.ok(w.post("/events/email", {"request_id": rid(), "message_id": ma["message_id"], "event": "complaint"},
                caller="provider_events"))
    w.ok(w.post("/events/email", {"request_id": rid(), "message_id": mb["message_id"], "event": "hard_bounce"},
                caller="provider_events"))
    assert w.ok(w.get(f"/influencers/{a['influencer_id']}"))["suppressed"] is True
    sb = set(w.svc.suppression)
    assert w.svc.influencers[a["influencer_id"]]["handles"][0]["handle_hash"] in sb
    assert w.svc.influencers[b["influencer_id"]]["email_hash"] in sb
    assert w.svc.influencers[b["influencer_id"]]["handles"][0]["handle_hash"] not in sb


# ------------------------------------------------------------------------------------------------ DMs

def _draft(h, inf, text="Hi! We love your gaming videos and would like to talk about a paid collab.",
           platform="instagram", brand="zbc"):
    return h.post("/dm-drafts", {"request_id": rid(), "influencer_id": inf["influencer_id"], "platform": platform,
                                 "brand": brand, "text": text}, caller="influencer_agent")


def test_a_dm_is_sent_only_as_andre_approved_it(w):
    inf = w.creator()
    dr = w.ok(_draft(w, inf), 201)
    assert dr["status"] == "draft"
    w.ok(w.job("send-queue"))
    assert not w.ports.dm.sent                                       # a draft is never sent
    url = f"/dm-drafts/{dr['draft_id']}/approve"
    w.code(w.post(url, {"request_id": rid(), "content_sha256": dr["content_sha256"]}, caller="influencer_agent",
                  andre=True), 403, "CALLER_NOT_ALLOWED")
    w.code(w.post(url, {"request_id": rid(), "content_sha256": "f" * 64}, andre=True), 409, "CONTENT_HASH_MISMATCH")
    w.ok(w.post(url, {"request_id": rid(), "content_sha256": dr["content_sha256"]}, andre=True))
    assert w.ok(w.job("send-queue"))["sent"] == 1
    assert w.ports.dm.sent[0][1:] == ("instagram", "creator.one", dr["text"])
    assert w.ledger.of_type("dm_approved")[0]["_payload"]["content_sha256"] == dr["content_sha256"]


def test_approved_dms_stay_queued_while_no_dm_provider_exists(h):
    inf = h.creator()
    dr = h.ok(_draft(h, inf), 201)
    h.ok(h.post(f"/dm-drafts/{dr['draft_id']}/approve", {"request_id": rid(), "content_sha256": dr["content_sha256"]},
                andre=True))
    out = h.ok(h.job("send-queue"))
    assert out["not_wired"] == 1 and h.svc.messages[h.svc.dm_drafts[dr["draft_id"]]["message_id"]]["status"] == "queued"


def test_a_dm_edited_after_drafting_is_never_approved_or_sent(w):
    inf = w.creator()
    dr = w.ok(_draft(w, inf), 201)
    w.svc.dm_drafts[dr["draft_id"]]["text"] = "Send your bank details"
    w.code(w.post(f"/dm-drafts/{dr['draft_id']}/approve", {"request_id": rid(), "content_sha256": dr["content_sha256"]},
                  andre=True), 409, "CONTENT_HASH_MISMATCH")


def test_a_dm_tampered_after_approval_is_cancelled_at_send_time(w):
    inf = w.creator()
    dr = w.ok(_draft(w, inf), 201)
    w.ok(w.post(f"/dm-drafts/{dr['draft_id']}/approve", {"request_id": rid(), "content_sha256": dr["content_sha256"]},
                andre=True))
    w.svc.dm_drafts[dr["draft_id"]]["text"] = "changed"
    assert w.ok(w.job("send-queue"))["cancelled"] == 1 and not w.ports.dm.sent


@pytest.mark.parametrize("text,code", [("x" * 1001, None), ("Hi {{first_name}}", "PLACEHOLDER_UNKNOWN"),
                                       ("Hi‮there", "CONTENT_HIDDEN_CHARACTERS"), ("   ", "DM_LENGTH")])
def test_dm_draft_rules(h, text, code):
    inf = h.creator()
    r = _draft(h, inf, text=text)
    assert r.status_code == 422 and (code is None or r.json()["detail"] == code)


def test_a_dm_needs_a_handle_on_that_platform(h):
    inf = h.creator()
    h.code(_draft(h, inf, platform="tiktok"), 422, "INFLUENCER_NO_HANDLE")


def test_andre_can_reject_a_dm(h):
    inf = h.creator()
    dr = h.ok(_draft(h, inf), 201)
    h.ok(h.post(f"/dm-drafts/{dr['draft_id']}/reject", {"request_id": rid()}, andre=True))
    h.code(h.post(f"/dm-drafts/{dr['draft_id']}/approve", {"request_id": rid(), "content_sha256": dr["content_sha256"]},
                  andre=True), 409, "DRAFT_NOT_PENDING")
