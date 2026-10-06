"""AEGIS round 3 (Oct 5 2026, BLOCKING): regressions with the reviewer's cases (scratchpad aegis-sales-r3/repro)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from clock import FixedClock
from helpers import Harness, rid, wired_ports
from intelligences import i10_replies

CASES = ["yes stop", "ok remove me", "sounds good, never again", "interested? no", "👍", "ok 🛑", "yes ❌",
         "ok 🖕", "Yes 👎", "y e s", "уеѕ", "ok​stop", "", " ", "ok no", "sure not",
         "Out of office. I did not sign up for this", "I'm driving - delete my number",
         "auto reply: please do not text this number again", "out of office no me escribas",
         "I'm driving. Opt me out", "out of office wrong number", "auto reply never contact me",
         "im driving, who is this? dont message me again", "Out of office. Lose this number.",
         "auto reply: this number is no longer in service", "I'm driving, not interested",
         "Yes", "interested", "call me", "Please don't message me again", "Opt me out", "unsub",
         "never text me again", "delete my number", "S T O P", "ЅTOP", "St0p", "I did not sign up for this",
         "no me escribas", "who is this", "hmm maybe later"]
EMAIL_CASES = ["Opt me out", "delete my number", "no me escribas", "I did not sign up for this",
               "Please quit texting my cell", "Yes, interested", "👍"]


def q_sms(h, cid, t):
    return h.post("/sales/v1/outreach/sms", {"request_id": rid(), "contact_id": cid, "template_id": t["template_id"],
                                             "version": 1}, "sales_agent")


def q_voice(h, cid, brand="zbm"):
    return h.post("/sales/v1/outreach/voice", {"request_id": rid(), "contact_id": cid, "brand": brand,
                                               "purpose": "follow_up"}, "sales_agent")


def texted(w, brands=("zbm",), **kw):
    lead = w.vlead(**kw)
    for b in brands:
        w.ok(w.consent(lead["contact_id"], brand=b), 201)
        w.ok(w.consent(lead["contact_id"], channel="voice", brand=b), 201)
    t = w.template(channel="sms", name="s", subject=None, body="Hi {{first_name}}, quick question?")
    w.ok(q_sms(w, lead["contact_id"], t), 201)
    w.ok(w.job("send-queue"))
    return lead, t, w.ports.sms.sent[0][0]


# ------------------------------------------------------------------ S3-C1 / S3-C2 / S3-H1

@pytest.mark.parametrize("text", CASES)
def test_s3_c1_every_sms_reply_ends_with_one_text_sent(w, text):
    lead, t, mid = texted(w, brands=("zbm", "zbc"))
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "sms", "message_id": mid,
                                          "from_phone": "+13105550100", "text": text}, "provider_events"), 201)
    assert r["held"] or r["suppressed"]
    for brand in ("zbm", "zbc"):
        assert q_sms(w, lead["contact_id"], t).status_code == 403
        assert q_voice(w, lead["contact_id"], brand).status_code == 403
    w.ok(w.job("send-queue"))
    assert len(w.ports.sms.sent) == 1 and w.ports.voice.sent == []


@pytest.mark.parametrize("text", EMAIL_CASES)
def test_s3_c2_an_email_reply_holds_texts_and_calls_too(w, text):
    lead, t, _ = texted(w)
    te = w.template(name="e")
    em = w.ok(w.post("/sales/v1/outreach/email", {"request_id": rid(), "contact_id": lead["contact_id"],
                                                  "template_id": te["template_id"], "version": 1}, "sales_agent"), 201)
    w.ok(w.job("send-queue"))
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "message_id": em["message_id"],
                                          "from_email": "jane@acme-shop.test", "text": text}, "provider_events"), 201)
    assert r["held"] or r["suppressed"]
    assert q_sms(w, lead["contact_id"], t).status_code == 403
    w.ok(w.job("send-queue"))
    assert len(w.ports.sms.sent) == 1


def test_s3_h1_a_voice_reply_holds(w):
    lead, t, _ = texted(w)
    w.ok(q_voice(w, lead["contact_id"]), 201)
    w.ok(w.job("send-queue"))
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "voice",
                                          "message_id": w.ports.voice.sent[0][0], "text": "yes"}, "provider_events"),
             201)
    assert r["held"] is True
    w.refused(q_sms(w, lead["contact_id"], t), 403, "PHONE_HOLD")


def test_s3_c1_a_reply_from_another_number_holds_that_number_and_the_contacts(w):
    lead, t, mid = texted(w)
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "sms", "message_id": mid,
                                          "from_phone": "+12125550123", "text": "ok"}, "provider_events"), 201)
    hold = w.svc.phone_holds[next(iter(w.svc.phone_holds))]
    assert len(hold["hashes"]) == 2 and r["held"]


@pytest.mark.parametrize("text", [
    "I'm driving with Do Not Disturb While Driving turned on. I'll see your message when I get where I'm going.",
    "I’m driving with Do Not Disturb While Driving turned on. I’ll see your message when I get where "
    "I’m going.",
    "  I am currently out of the office.  ", "OUT OF OFFICE"])
def test_s3_c1_only_the_exact_auto_reply_texts_do_not_hold(w, text):
    lead, t, mid = texted(w)
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "sms", "message_id": mid,
                                          "from_phone": "+13105550100", "text": text}, "provider_events"), 201)
    assert r["held"] is False and r["class"] == "out_of_office"
    w.ok(q_sms(w, lead["contact_id"], t), 201)


@pytest.mark.parametrize("text", ["I'm driving with Do Not Disturb While Driving turned on. I'll see your message when "
                                  "I get where I'm going. STOP", "out of office!", "out  of office", "Out of office."])
def test_s3_c1_an_auto_reply_with_anything_changed_holds(text):
    assert i10_replies.exact_auto_reply(text) is False


def test_s3_c1_the_hold_survives_a_restart_and_only_andre_releases_it(tmp_path):
    w = Harness(tmp_path, data_dir=str(tmp_path / "d"), ports=wired_ports())
    lead, t, mid = texted(w)
    task_id = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "sms", "message_id": mid,
                                                "text": "yes"}, "provider_events"), 201)["task_id"]
    w2 = w.restart()
    w2.refused(q_sms(w2, lead["contact_id"], t), 403, "PHONE_HOLD")
    w2.ok(w2.andre(f"/sales/v1/tasks/{task_id}/decision", {"request_id": rid(), "decision": "not_an_opt_out"}))
    w2.ok(q_sms(w2, lead["contact_id"], t), 201)


# ------------------------------------------------------------------ S3-M1

def test_s3_m1_texts_and_calls_to_non_nanp_numbers_refused(tmp_path):
    w = Harness(tmp_path, clock=FixedClock(datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)), ports=wired_ports())
    lead = w.vlead(tz="America/Los_Angeles", phone="+447700900123")      # 03:00 in the UK
    w.ok(w.consent(lead["contact_id"]), 201)
    w.ok(w.consent(lead["contact_id"], channel="voice"), 201)
    t = w.template(channel="sms", name="s", subject=None, body="Hi {{first_name}}")
    w.refused(q_sms(w, lead["contact_id"], t), 403, "NON_NANP_NOT_SUPPORTED")
    w.refused(q_voice(w, lead["contact_id"]), 403, "NON_NANP_NOT_SUPPORTED")


# ------------------------------------------------------------------ S3-M2

def test_s3_m2_the_real_acceptance_is_on_the_ledger(w):
    w.ok(w.price("zbm.revenue_recovery_engagement", price="2500.00"))
    lead = w.lead()
    opp = w.ok(w.post(f"/sales/v1/leads/{lead['lead_id']}/convert", {"request_id": rid()}, "sales_agent"), 201)
    p = w.ok(w.post("/sales/v1/proposals", {"request_id": rid(), "opportunity_id": opp["opportunity_id"],
                                            "lines": [{"line_id": "zbm.revenue_recovery_engagement"}]},
                    "sales_agent"), 201)
    w.ok(w.post(f"/sales/v1/proposals/{p['proposal_id']}/send", {"request_id": rid(), "contract_kind": "client_msa",
                                                                  "contract_ref": "msa-1"}, "sales_agent"))
    acceptance = {"kind": "esign_envelope", "ref": "env-1"}
    w.ok(w.post(f"/sales/v1/proposals/{p['proposal_id']}/won", {"request_id": rid(), "acceptance": acceptance},
                "sales_agent"))
    payload = w.ledger.of_type("proposal_won")[-1]["_payload"]
    assert payload["acceptance"] == acceptance and payload["by"] == "sales_agent"
    won = [r for r in w.svc.log.records if r["kind"] == "proposal_won"][-1]
    assert won["data"]["acceptance"] == acceptance


def test_s3_m2_andre_won_records_no_acceptance(w):
    w.ok(w.price("zbm.revenue_recovery_engagement", price="2500.00"))
    opp = w.opportunity()
    p = w.ok(w.post("/sales/v1/proposals", {"request_id": rid(), "opportunity_id": opp["opportunity_id"],
                                            "lines": [{"line_id": "zbm.revenue_recovery_engagement"}]},
                    "sales_agent"), 201)
    w.ok(w.post(f"/sales/v1/proposals/{p['proposal_id']}/send", {"request_id": rid(), "contract_kind": "client_msa",
                                                                  "contract_ref": "msa-1"}, "sales_agent"))
    w.ok(w.andre(f"/sales/v1/proposals/{p['proposal_id']}/won", {"request_id": rid()}))
    payload = w.ledger.of_type("proposal_won")[-1]["_payload"]
    assert payload["acceptance"] is None and payload["by"] == "andre"


# ------------------------------------------------------------------ S3-L1

@pytest.mark.parametrize("utc,phone,tz,expect", [
    (datetime(2026, 10, 7, 0, 30, tzinfo=timezone.utc), "+19025550100", "America/Toronto", "QUIET_HOURS"),
    (datetime(2026, 10, 7, 0, 30, tzinfo=timezone.utc), "+17095550100", "America/Toronto", "QUIET_HOURS"),
    (datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc), "+16715550100", "America/New_York", "QUIET_HOURS"),
    (datetime(2026, 12, 7, 1, 30, tzinfo=timezone.utc), "+17875550100", "America/New_York", "QUIET_HOURS"),
    (datetime(2026, 10, 7, 0, 30, tzinfo=timezone.utc), "+12425550100", "America/New_York", None),
    (datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc), "+13105550100", "America/Los_Angeles", "QUIET_HOURS"),
    (datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc), "+19995550100", "America/New_York", "AREA_CODE_UNKNOWN"),
    (datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc), "+16845550100", "America/New_York", None),
])
def test_s3_l1_area_code_zones_across_nanp(tmp_path, utc, phone, tz, expect):
    w = Harness(tmp_path, clock=FixedClock(utc), ports=wired_ports())
    lead = w.vlead(tz=tz, phone=phone)
    w.ok(w.consent(lead["contact_id"]), 201)
    t = w.template(channel="sms", name="s", subject=None, body="Hi {{first_name}}")
    r = q_sms(w, lead["contact_id"], t)
    if expect:
        w.refused(r, 403, expect)
    else:
        assert r.status_code == 201


@pytest.mark.parametrize("phone", ["+1310555010", "+131055501000", "+11105550100", "+13101550100"])
def test_s3_l1_a_plus_one_number_must_be_twelve_characters_and_well_formed(w, phone):
    w.lead(phone=phone, email=None, code=422)


def test_s3_l1_all_nanp_zone_intersection_is_tiny_so_unknown_codes_are_refused():
    from zoneinfo import ZoneInfo
    from intelligences import i07_quiet_hours as q
    day = datetime(2026, 10, 7, tzinfo=timezone.utc)
    ok = [m for m in range(0, 24 * 60, 15)
          if all(q._inside(day.replace(hour=0) + __import__("datetime").timedelta(minutes=m), ZoneInfo(z))
                 for z in q.NANP_ZONES)]
    assert len(ok) * 15 <= 120                     # at most two hours a day across every NANP zone
    assert q.phone_problem("+19995550100") == "AREA_CODE_UNKNOWN"


def test_s3_m1_send_time_also_refuses_a_non_nanp_number(w):
    lead, t, _ = texted(w)
    queued = w.ok(q_sms(w, lead["contact_id"], t), 201)
    w.svc.contacts[lead["contact_id"]]["phone"] = "+447700900123"          # state changed after queueing
    w.ok(w.job("send-queue"))
    assert len(w.ports.sms.sent) == 1 and w.svc.messages[queued["message_id"]]["reason"] == "NON_NANP_NOT_SUPPORTED"


@pytest.mark.parametrize("phone,code", [("+1310555010", "PHONE_INVALID"), ("+131055501000", "PHONE_INVALID"),
                                        ("+447700900123", "NON_NANP_NOT_SUPPORTED"), (None, "NON_NANP_NOT_SUPPORTED"),
                                        ("+13105550100", None)])
def test_s3_l1_phone_problem_layer(phone, code):
    from intelligences import i07_quiet_hours as q
    assert q.phone_problem(phone) == code
