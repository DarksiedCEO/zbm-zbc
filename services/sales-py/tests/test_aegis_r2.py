"""AEGIS round 2 (Oct 5 2026, BLOCKING): regressions with the reviewer's cases (scratchpad aegis-sales-r2/repro)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import config as config_mod
from clock import FixedClock
from helpers import Harness, base_env, rid, wired_ports

REVIEWER_TEXTS = ["Please don't message me again", "Opt me out", "unsub", "never text me again", "delete my number",
                  "S T O P", "ЅTOP", "St0p", "I did not sign up for this", "no me escribas"]


def q_email(h, cid, t):
    return h.post("/sales/v1/outreach/email", {"request_id": rid(), "contact_id": cid,
                                               "template_id": t["template_id"], "version": 1}, "sales_agent")


def q_sms(h, cid, t):
    return h.post("/sales/v1/outreach/sms", {"request_id": rid(), "contact_id": cid, "template_id": t["template_id"],
                                             "version": 1}, "sales_agent")


def texted(w, brands=("zbm",)):
    lead = w.vlead()
    for b in brands:
        w.ok(w.consent(lead["contact_id"], brand=b), 201)
    t = w.template(channel="sms", name="s", subject=None, body="Hi {{first_name}}, quick question?")
    w.ok(q_sms(w, lead["contact_id"], t), 201)
    w.ok(w.job("send-queue"))
    return lead, t, w.ports.sms.sent[0][0]


def reply(w, mid, text, channel="sms"):
    return w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": channel, "message_id": mid,
                                             "from_phone": "+13105550100", "text": text}, "provider_events"), 201)


# ------------------------------------------------------------------ S2-C1

@pytest.mark.parametrize("text", REVIEWER_TEXTS)
def test_s2_c1_every_reviewer_case_ends_with_no_second_text(w, text):
    lead, t, mid = texted(w)
    r = reply(w, mid, text)
    assert r["held"] is True or r["suppressed"] is True
    q = q_sms(w, lead["contact_id"], t)
    assert q.status_code == 403 and q.json()["detail"] in ("PHONE_HOLD", "SUPPRESSED")
    w.ok(w.job("send-queue"))
    assert len(w.ports.sms.sent) == 1


def test_s2_c1_the_hold_is_on_the_ledger_before_the_log_line_and_covers_both_brands(w):
    lead, t, mid = texted(w, brands=("zbm", "zbc"))
    reply(w, mid, "who is this")
    ev = w.ledger.of_type("phone_hold_applied")
    assert len(ev) == 1
    last_anchor = w.ledger.of_type("log_anchor")[-1]
    assert ev[0]["seq"] < last_anchor["seq"]                        # recorded first, then the line is anchored
    zbc = w.template(brand="zbc", channel="sms", name="z", subject=None, body="Hi {{first_name}}.")
    w.refused(q_sms(w, lead["contact_id"], zbc), 403, "PHONE_HOLD")
    w.refused(w.post("/sales/v1/outreach/voice", {"request_id": rid(), "contact_id": lead["contact_id"],
                                                   "brand": "zbm", "purpose": "book_call"}, "sales_agent"), 403,
              "PHONE_HOLD")


def test_s2_c1_queued_texts_are_cancelled_by_the_hold(w):
    lead, t, mid = texted(w)
    queued = w.ok(q_sms(w, lead["contact_id"], t), 201)
    reply(w, mid, "hmm")
    assert w.svc.messages[queued["message_id"]]["status"] == "cancelled"
    w.ok(w.job("send-queue"))
    assert len(w.ports.sms.sent) == 1


def test_s2_c1_send_time_also_respects_a_hold(w):
    """Defence in depth: a queued text to a held number is not sent even if the queue was not swept."""
    lead, t, _ = texted(w)
    queued = w.ok(q_sms(w, lead["contact_id"], t), 201)
    w.svc.phone_holds["h"] = {"hold_id": "h", "hashes": [w.svc.contacts[lead["contact_id"]]["phone_hash"]],
                              "status": "active", "task_id": "x", "reply_id": "y"}
    w.ok(w.job("send-queue"))
    assert len(w.ports.sms.sent) == 1 and w.svc.messages[queued["message_id"]]["reason"] == "PHONE_HOLD"


@pytest.mark.parametrize("text,cls", [("Yes", "interested"), ("yes please!", "interested"),
                                      ("Sounds good", "interested"), ("Out of office until Monday", "out_of_office")])
def test_s2_c1_only_a_narrow_positive_reply_keeps_texting_open(w, text, cls):
    lead, t, mid = texted(w)
    r = reply(w, mid, text)
    assert r["held"] is False and r["class"] == cls
    w.ok(q_sms(w, lead["contact_id"], t), 201)


@pytest.mark.parametrize("text", ["yes, but stop texting", "interested? no. never text me again", "yes I did not ask",
                                  "Out of office. Stop texting me"])
def test_s2_c1_positive_words_mixed_with_anything_else_still_hold(w, text):
    _, _, mid = texted(w)
    r = reply(w, mid, text)
    assert r["held"] or r["suppressed"]


def test_s2_c1_only_andre_lifts_a_hold_and_only_by_deciding_the_task(w):
    lead, t, mid = texted(w)
    task_id = reply(w, mid, "Opt me out")["task_id"]
    w.refused(w.post(f"/sales/v1/tasks/{task_id}/close", {"request_id": rid(), "outcome": "done"}, "sales_agent"),
              403, "HOLD_NEEDS_ANDRE_DECISION")
    w.refused(w.post(f"/sales/v1/tasks/{task_id}/close", {"request_id": rid(), "outcome": "done"}, "dashboard"),
              403, "HOLD_NEEDS_ANDRE_DECISION")
    r = w.post(f"/sales/v1/tasks/{task_id}/decision", {"request_id": rid(), "decision": "not_an_opt_out"}, "dashboard")
    w.refused(r, 403, "ANDRE_APPROVAL_REQUIRED")
    w.refused(q_sms(w, lead["contact_id"], t), 403, "PHONE_HOLD")
    w.ok(w.andre(f"/sales/v1/tasks/{task_id}/decision", {"request_id": rid(), "decision": "not_an_opt_out"}))
    assert w.ledger.of_type("phone_hold_decided")
    w.ok(q_sms(w, lead["contact_id"], t), 201)


def test_s2_c1_lifting_never_restores_a_revoked_consent(w):
    lead, t, mid = texted(w, brands=("zbm", "zbc"))
    task_id = reply(w, mid, "hmm")["task_id"]
    w.ok(w.post("/sales/v1/consents/revoke", {"request_id": rid(), "contact_id": lead["contact_id"],
                                              "source": "call"}, "dashboard"))
    w.ok(w.andre(f"/sales/v1/tasks/{task_id}/decision", {"request_id": rid(), "decision": "not_an_opt_out"}))
    c = w.ok(w.get(f"/sales/v1/contacts/{lead['contact_id']}"))
    assert c["phone_held"] is False and c["phone_suppressed"] is True and not any(c["consent"].values())
    w.refused(q_sms(w, lead["contact_id"], t), 403, "SUPPRESSED")


def test_s2_c1_andre_may_decide_it_was_an_opt_out(w):
    lead, t, mid = texted(w)
    task_id = reply(w, mid, "delete my number")["task_id"]
    w.ok(w.andre(f"/sales/v1/tasks/{task_id}/decision", {"request_id": rid(), "decision": "opt_out"}))
    c = w.ok(w.get(f"/sales/v1/contacts/{lead['contact_id']}"))
    assert c["phone_suppressed"] is True and not any(c["consent"].values())


def test_s2_c1_voice_replies_hold_too(w):
    lead = w.vlead()
    w.ok(w.consent(lead["contact_id"], channel="voice"), 201)
    w.ok(w.post("/sales/v1/outreach/voice", {"request_id": rid(), "contact_id": lead["contact_id"], "brand": "zbm",
                                             "purpose": "book_call"}, "sales_agent"), 201)
    w.ok(w.job("send-queue"))
    r = reply(w, w.ports.voice.sent[0][0], "no me llames", channel="voice")
    assert r["held"] is True


@pytest.mark.parametrize("text", ["S T O P", "ЅTOP", "St0p", "ＳＴＯＰ"])
def test_s2_c1_label_folds_confusables_and_spaced_letters(text):
    from intelligences import i10_replies
    assert i10_replies.classify(text, "sms") == "unsubscribe"


# ------------------------------------------------------------------ S2-H1

@pytest.mark.parametrize("company", ["Acc0unt Suspended", "Acme Paym3nt Overdue", "Acme - Verify Acct Now",
                                     "Acme Security Notice", "evil-site dot com", "Acme call 310-555-0199",
                                     "Acme reply YES to 99999", "Acme. Wire 5000 to us today"])
def test_s2_h1_a_name_typed_into_a_form_never_renders(w, company):
    lead = w.lead(account={"name": company, "domain": "acme-shop.test"})
    for t in (w.template(name="a"), w.template(name="b", body="Hi {{first_name}}, we help {{company}} recover revenue.")):
        w.refused(q_email(w, lead["contact_id"], t), 403, "MERGE_FIELD_REFUSED")
    w.ok(w.job("send-queue"))
    assert w.ports.email.sent == []


def test_s2_h1_the_verified_display_name_is_what_renders(w):
    lead = w.lead(account={"name": "Acc0unt Suspended", "domain": "acme-shop.test"})
    w.verify(lead, display_name="Acme Shop", first_name="Jane")
    assert w.ledger.of_type("account_display_name_verified") and w.ledger.of_type("contact_first_name_verified")
    w.ok(q_email(w, lead["contact_id"], w.template()), 201)
    w.ok(w.job("send-queue"))
    sent = w.ports.email.sent[0][2]
    assert sent["subject"] == "Quick idea for Acme Shop" and "Acc0unt" not in sent["body"]


def test_s2_h1_only_the_console_verifies_and_the_rules_still_apply(w):
    lead = w.lead()
    for caller in ("sales_agent", "hub"):
        r = w.post(f"/sales/v1/accounts/{lead['account_id']}/display-name",
                   {"request_id": rid(), "display_name": "Acme"}, caller)
        w.refused(r, 403, "CALLER_NOT_ALLOWED")
    w.refused(w.post(f"/sales/v1/accounts/{lead['account_id']}/display-name",
                     {"request_id": rid(), "display_name": "http://x.example"}), 422, "MERGE_FIELD_REFUSED")


def test_s2_h1_first_name_unverified_refused_but_a_generic_greeting_works(w):
    lead = w.lead()
    w.post(f"/sales/v1/accounts/{lead['account_id']}/display-name", {"request_id": rid(), "display_name": "Acme"})
    w.refused(q_email(w, lead["contact_id"], w.template(name="fn")), 403, "MERGE_FIELD_REFUSED")
    generic = w.template(name="generic", subject="A quick idea for {{company}}", body="Hello, a quick idea for you.")
    w.ok(q_email(w, lead["contact_id"], generic), 201)


def test_s2_h1_sms_uses_verified_values_only(w):
    lead = w.lead()
    w.ok(w.consent(lead["contact_id"]), 201)
    t = w.template(channel="sms", name="s", subject=None, body="Hi {{first_name}}, quick question?")
    w.refused(q_sms(w, lead["contact_id"], t), 403, "MERGE_FIELD_REFUSED")


# ------------------------------------------------------------------ S2-L1

@pytest.mark.parametrize("utc,tz", [(datetime(2026, 10, 7, 4, 30, tzinfo=timezone.utc), "Pacific/Honolulu"),
                                    (datetime(2026, 10, 7, 13, 30, tzinfo=timezone.utc), "America/New_York")])
def test_s2_l1_the_area_codes_zone_is_also_checked(tmp_path, utc, tz):
    w = Harness(tmp_path, clock=FixedClock(utc), ports=wired_ports())
    lead = w.vlead(tz=tz)                                  # a 310 (Los Angeles) number
    w.ok(w.consent(lead["contact_id"]), 201)
    t = w.template(channel="sms", name="s", subject=None, body="Hi {{first_name}}")
    w.refused(q_sms(w, lead["contact_id"], t), 403, "QUIET_HOURS")
    w.ok(w.job("send-queue"))
    assert w.ports.sms.sent == []


@pytest.mark.parametrize("tz", ["America/Noronha", "America/Nuuk", "America/Sao_Paulo", "America/Mexico_City",
                                "America/Argentina/Buenos_Aires"])
def test_s2_l1_non_nanp_zones_refused_for_a_plus_one_number(w, tz):
    w.lead(tz=tz, code=422)


def test_s2_l1_unknown_area_code_needs_eastern_and_pacific_daytime(tmp_path):
    early = FixedClock(datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc))   # 10:30 ET, 07:30 PT
    w = Harness(tmp_path, clock=early, ports=wired_ports())
    lead = w.vlead(tz="America/New_York", phone="+19995550100")
    w.ok(w.consent(lead["contact_id"]), 201)
    t = w.template(channel="sms", name="s", subject=None, body="Hi {{first_name}}")
    w.refused(q_sms(w, lead["contact_id"], t), 403, "QUIET_HOURS")
    early.advance(hours=1)                                                   # 11:30 ET, 08:30 PT
    w.ok(q_sms(w, lead["contact_id"], t), 201)


# ------------------------------------------------------------------ S2-L2

@pytest.mark.parametrize("over", [{"SALES_ZBM_DOMAIN": None}, {"SALES_ZBC_DOMAIN": None},
                                  {"SALES_OUTREACH_DOMAIN": "go.zbestclips.test"},
                                  {"SALES_OUTREACH_DOMAIN": "zbestmedia.test"}])
def test_s2_l2_brand_domains_are_pinned_and_both_required(over):
    with pytest.raises(RuntimeError, match="SALES_ZBM_DOMAIN"):
        config_mod.load(base_env(**over))


# ------------------------------------------------------------------ S2 DoS

def test_s2_queue_is_capped_per_caller(tmp_path):
    w = Harness(tmp_path, ports=wired_ports(), SALES_QUEUE_MAX_PER_CALLER="2")
    t = w.template()
    cids = [w.vlead(email=f"p{i}@shop{i}.test", phone=None, account={"name": f"Shop {i}"})["contact_id"]
            for i in range(3)]
    w.ok(q_email(w, cids[0], t), 201)
    w.ok(q_email(w, cids[1], t), 201)
    w.refused(q_email(w, cids[2], t), 429, "QUEUE_FULL")
    w.ok(w.job("send-queue"))                                    # sent ones no longer count
    w.ok(q_email(w, cids[2], t), 201)


def test_s2_queue_cap_is_bounded():
    with pytest.raises(RuntimeError, match="SALES_QUEUE_MAX_PER_CALLER"):
        config_mod.load(base_env(SALES_QUEUE_MAX_PER_CALLER="20001"))


@pytest.mark.parametrize("text", ["Out of office. Stop texting me", "auto reply: unsubscribe", "I'm driving, quit it"])
def test_s2_c1_positive_reply_never_admits_an_opt_out_word(text):
    from intelligences import i10_replies
    assert i10_replies.positive_reply(text, "sms") is None
