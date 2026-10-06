"""AEGIS round 1 (Oct 5 2026, BLOCKING): one regression per finding, each the reviewer's scenario
(scratchpad aegis-sales-r1/repro/test_aegis_r1.py)."""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone

import pytest

import config as config_mod
from clock import FixedClock
from helpers import ANDRE, Harness, base_env, rid, secret_file, wired_ports
from intelligences import i10_replies


def q_email(h, cid, t):
    return h.post("/sales/v1/outreach/email", {"request_id": rid(), "contact_id": cid,
                                               "template_id": t["template_id"], "version": 1}, "sales_agent")


def sms_q(h, cid, t):
    return h.post("/sales/v1/outreach/sms", {"request_id": rid(), "contact_id": cid, "template_id": t["template_id"],
                                             "version": 1}, "sales_agent")


# ------------------------------------------------------------------ S1-H1 merge fields

def test_s1_h1_merge_field_cannot_inject_a_deceptive_subject(w):
    lead = w.vlead(account={"name": "URGENT: account suspended - verify your password", "domain": "acme-shop.test"})
    t = w.template()
    r = q_email(w, lead["contact_id"], t)
    assert r.status_code == 403 and r.json()["detail"] == "MERGE_FIELD_REFUSED"
    w.ok(w.job("send-queue"))
    assert w.ports.email.sent == []


def test_s1_h1_letters_only_value_that_makes_the_subject_deceptive_is_refused(w):
    lead = w.vlead(account={"name": "Final notice", "domain": "acme-shop.test"})
    w.refused(q_email(w, lead["contact_id"], w.template()), 403, "SUBJECT_DECEPTIVE")


@pytest.mark.parametrize("company,code", [("Claim at http://evil.example/x", "MERGE_FIELD_REFUSED"),
                                          ("www.evil.example", "MERGE_FIELD_REFUSED"),
                                          ("me@evil.example", "MERGE_FIELD_REFUSED"),
                                          ("evil.example", "URL_NOT_APPROVED"),
                                          ("A" * 41, "MERGE_FIELD_REFUSED")])
def test_s1_h1_merge_field_cannot_inject_a_link(w, company, code):
    lead = w.vlead(account={"name": company, "domain": "acme-shop.test"})
    t = w.template(body="Hi {{first_name}}, we help {{company}} recover revenue.")
    w.refused(q_email(w, lead["contact_id"], t), 403, code)


def test_s1_h1_checked_again_at_send_time(w, monkeypatch):
    """A message queued before the rule existed (or past a queue-time gap) is still stopped at send time."""
    lead = w.vlead(account={"name": "Claim at http://evil.example/x", "domain": "acme-shop.test"})
    t = w.template()
    with monkeypatch.context() as mp:
        mp.setattr(type(w.svc), "_merge_problem", lambda self, t, v, c: None)
        msg = w.ok(q_email(w, lead["contact_id"], t), 201)
    r = w.ok(w.job("send-queue"))
    assert r["cancelled"] == 1 and w.ports.email.sent == []
    assert w.svc.messages[msg["message_id"]]["reason"] == "MERGE_FIELD_REFUSED"


def test_s1_h1_a_field_the_template_does_not_use_is_not_judged(w):
    lead = w.vlead(account={"name": "Shop @ http://x.example", "domain": "acme-shop.test"})
    t = w.template(name="nocompany", subject="A quick idea", body="Hi {{first_name}}, a quick idea for you.")
    w.ok(q_email(w, lead["contact_id"], t), 201)


# ------------------------------------------------------------------ S1-H2 split proposals

def test_s1_h2_split_proposals_are_judged_as_the_sum(w):
    w.ok(w.price("zbm.revenue_recovery_monthly", price="2500.00"))
    opp = w.opportunity()
    parts = []
    for _ in range(3):
        p = w.ok(w.post("/sales/v1/proposals", {"request_id": rid(), "opportunity_id": opp["opportunity_id"],
                                                "lines": [{"line_id": "zbm.revenue_recovery_monthly",
                                                           "quantity": 4}]}, "sales_agent"), 201)
        parts.append(p)
    assert parts[0]["status"] == "approved"
    assert [p["status"] for p in parts[1:]] == ["pending_andre", "pending_andre"]
    assert parts[1]["needs_andre"] == ["OPPORTUNITY_TOTAL_OVER_MAX"]


def test_s1_h2_a_lost_proposal_does_not_count(w):
    w.ok(w.price("zbm.revenue_recovery_monthly", price="2500.00"))
    opp = w.opportunity()
    body = {"opportunity_id": opp["opportunity_id"], "lines": [{"line_id": "zbm.revenue_recovery_monthly",
                                                                "quantity": 4}]}
    first = w.ok(w.post("/sales/v1/proposals", {"request_id": rid(), **body}, "sales_agent"), 201)
    w.ok(w.post(f"/sales/v1/proposals/{first['proposal_id']}/lost", {"request_id": rid(), "reason_code": "price"},
                "sales_agent"))
    assert w.ok(w.post("/sales/v1/proposals", {"request_id": rid(), **body}, "sales_agent"), 201)["status"] == \
        "approved"


# ------------------------------------------------------------------ S1-H3 opt-out wording

@pytest.mark.parametrize("text", ["please cancel", "Cancel these texts", "quit texting me", "end", "wrong number",
                                  "leave me alone", "lose my number", "unsubscribed", "ＳＴＯＰ", "S.T.O.P",
                                  "remove", "take me off your list", "STOP", "alto", "parar", "cancelar", "baja",
                                  "No más mensajes", "no more texts", "Opt-out", "stop texting me please"])
def test_s1_h3_sms_opt_out_wording(text):
    assert i10_replies.classify(text, "sms") == "unsubscribe"
    assert i10_replies.classify(text, "voice") == "unsubscribe"


def test_s1_h3_cancel_reply_stops_texts_on_every_channel_and_brand(w):
    lead = w.vlead()
    for ch in ("sms", "voice"):
        for b in ("zbm", "zbc"):
            w.ok(w.consent(lead["contact_id"], channel=ch, brand=b), 201)
    t = w.template(channel="sms", name="sms2", subject=None, body="Hi {{first_name}}, quick question?")
    w.ok(sms_q(w, lead["contact_id"], t), 201)
    w.ok(w.job("send-queue"))
    mid = w.ports.sms.sent[0][0]
    r = w.ok(w.post("/sales/v1/replies", {"request_id": rid(), "channel": "sms", "message_id": mid,
                                          "from_phone": "+13105550100", "text": "Please cancel these texts"},
                    "provider_events"), 201)
    assert r["class"] == "unsubscribe" and r["suppressed"] is True
    c = w.ok(w.get(f"/sales/v1/contacts/{lead['contact_id']}"))
    assert c["email_suppressed"] and c["phone_suppressed"] and not any(c["consent"].values())
    w.refused(sms_q(w, lead["contact_id"], t), 403, "SUPPRESSED")


# ------------------------------------------------------------------ S1-M1 time zone

def test_s1_m1_agent_cannot_move_a_time_zone_to_dodge_quiet_hours(tmp_path):
    clock = FixedClock(datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc))     # 23:00 in Los Angeles
    w = Harness(tmp_path, clock=clock, ports=wired_ports())
    lead = w.vlead()
    w.ok(w.consent(lead["contact_id"]), 201)
    t = w.template(channel="sms", name="sms1", subject=None, body="Hi {{first_name}}, quick question?")
    w.refused(sms_q(w, lead["contact_id"], t), 403, "QUIET_HOURS")
    r = w.post(f"/sales/v1/contacts/{lead['contact_id']}/time-zone", {"request_id": rid(), "time_zone": "Asia/Tokyo"},
               "sales_agent")
    w.refused(r, 403, "CALLER_NOT_ALLOWED")
    r = w.post(f"/sales/v1/contacts/{lead['contact_id']}/time-zone", {"request_id": rid(), "time_zone": "Asia/Tokyo"},
               "dashboard")
    w.refused(r, 422, "TIME_ZONE_PHONE_MISMATCH")                    # a +1 number lives in an American zone
    w.refused(sms_q(w, lead["contact_id"], t), 403, "QUIET_HOURS")
    assert w.ok(w.job("send-queue"))["sent"] == 0 and w.ports.sms.sent == []


def test_s1_m1_allowed_change_is_a_typed_ledger_event(w):
    lead = w.vlead()
    w.ok(w.post(f"/sales/v1/contacts/{lead['contact_id']}/time-zone",
                {"request_id": rid(), "time_zone": "Pacific/Honolulu"}, "onboarding"))
    assert w.ledger.of_type("contact_time_zone_set")


def test_s1_m1_nanp_number_with_a_foreign_zone_refused_at_intake(w):
    w.lead(tz="Asia/Tokyo", code=422)


# ------------------------------------------------------------------ S1-M2 warm-up per domain

def test_s1_m2_a_new_outreach_domain_restarts_the_warmup(tmp_path):
    d = str(tmp_path / "data")
    clock = FixedClock(datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc))
    kw = dict(SALES_WARMUP_SCHEDULE="1,2,4")
    h = Harness(tmp_path, data_dir=d, clock=clock, ports=wired_ports(), **kw)
    t = h.template()
    for day in range(3):
        for i in range(4):
            lead = h.vlead(email=f"p{day}{i}@shop{day}{i}.test", phone=None,
                          account={"name": f"Shop {day}{i}", "domain": f"shop{day}{i}.test"})
            h.ok(q_email(h, lead["contact_id"], t), 201)
        h.ok(h.job("send-queue"))
        clock.advance(days=1)
        h.ok(h.job("warmup-reset"))
    assert h.svc._cap_today() == 4
    h2 = h.restart(SALES_OUTREACH_DOMAIN="fresh-cold.test")
    assert h2.svc._cap_today() == 1                      # domain B starts at day 1
    assert h2.restart(SALES_OUTREACH_DOMAIN="zbm-outreach.test").svc._cap_today() == 4


# ------------------------------------------------------------------ S1-M3 registrable domains

@pytest.mark.parametrize("outreach,zbm,zbc", [("go.zbestmedia.com", "www.zbestmedia.com", "zbestclips.com"),
                                               ("mail.zbm.co.uk", "zbm.co.uk", "zbestclips.com"),
                                               ("zbestclips.com", "zbestmedia.com", "zbestclips.com")])
def test_s1_m3_sibling_subdomains_share_a_registrable_domain(outreach, zbm, zbc):
    with pytest.raises(RuntimeError, match="registrable"):
        config_mod.load(base_env(SALES_OUTREACH_DOMAIN=outreach, SALES_ZBM_DOMAIN=zbm, SALES_ZBC_DOMAIN=zbc))


@pytest.mark.parametrize("missing", ["SALES_ZBM_DOMAIN", "SALES_ZBC_DOMAIN"])
def test_s1_m3_both_brand_domains_must_be_listed(missing):
    with pytest.raises(RuntimeError, match="SALES_ZBM_DOMAIN and SALES_ZBC_DOMAIN are not both set"):
        config_mod.load(base_env(**{"SALES_OUTREACH_DOMAIN": "zbm-outreach.com", missing: None}))


def test_s1_m3_two_level_suffix_is_not_one_registrable_domain():
    s = config_mod.load(base_env(SALES_OUTREACH_DOMAIN="zbm-outreach.co.uk", SALES_ZBM_DOMAIN="zbestmedia.co.uk",
                                 SALES_ZBC_DOMAIN="zbestclips.com"))
    assert s.outreach_domain == "zbm-outreach.co.uk"


# ------------------------------------------------------------------ S1-M4 one-click unsubscribe

def test_s1_m4_one_click_unsubscribe_stops_texts_too(w):
    lead = w.vlead()
    w.ok(w.consent(lead["contact_id"]), 201)
    w.ok(q_email(w, lead["contact_id"], w.template()), 201)
    w.ok(w.job("send-queue"))
    tok = w.svc.unsubscribe_token(w.ports.email.sent[0][0])
    w.ok(w.post("/sales/v1/unsubscribe", {"request_id": rid(), "token": tok}, "hub"))
    c = w.ok(w.get(f"/sales/v1/contacts/{lead['contact_id']}", "sales_agent"))
    assert c["email_suppressed"] and c["phone_suppressed"] and not any(c["consent"].values())
    st = w.template(channel="sms", name="sms3", subject=None, body="Hi {{first_name}}")
    w.refused(sms_q(w, lead["contact_id"], st), 403, "SUPPRESSED")
    w.ok(w.job("send-queue"))
    assert w.ports.sms.sent == []


def test_s1_m4_complaint_is_an_opt_out_everywhere(w):
    lead = w.vlead()
    w.ok(w.consent(lead["contact_id"]), 201)
    msg = w.ok(q_email(w, lead["contact_id"], w.template()), 201)
    w.ok(w.job("send-queue"))
    w.ok(w.post("/sales/v1/events/email", {"request_id": rid(), "message_id": msg["message_id"],
                                           "event": "complaint"}, "provider_events"))
    assert w.ok(w.get(f"/sales/v1/contacts/{lead['contact_id']}"))["phone_suppressed"] is True


# ------------------------------------------------------------------ S1-M5 key format

def test_s1_m5_generated_hex_and_base64_keys_are_accepted(tmp_path):
    refused = 0
    for i in range(200):
        raw = os.urandom(32)
        for enc in (raw.hex().encode(), base64.b64encode(raw), base64.urlsafe_b64encode(raw).rstrip(b"=")):
            p = secret_file(tmp_path, "k", enc)
            try:
                config_mod.load(base_env(SALES_PII_HASH_KEY_FILE=p))
            except RuntimeError:
                refused += 1
    assert refused == 0


@pytest.mark.parametrize("content", [b"a" * 64, os.urandom(16).hex().encode(), b"correct horse battery staple x"])
def test_s1_m5_weak_or_short_keys_refused(tmp_path, content):
    with pytest.raises(RuntimeError, match="generated key"):
        config_mod.load(base_env(SALES_PII_HASH_KEY_FILE=secret_file(tmp_path, "k", content)))


def test_s1_m5_the_decoded_bytes_are_the_key(tmp_path):
    raw = os.urandom(32)
    a = config_mod.load(base_env(SALES_PII_HASH_KEY_FILE=secret_file(tmp_path, "a", raw.hex().encode())))
    b = config_mod.load(base_env(SALES_PII_HASH_KEY_FILE=secret_file(tmp_path, "b", base64.b64encode(raw))))
    assert a.pii_key.reveal() == b.pii_key.reveal() == raw


# ------------------------------------------------------------------ S1-L1 won

def _sent_proposal(w):
    w.ok(w.price("zbm.revenue_recovery_engagement", price="2500.00"))
    p = w.ok(w.post("/sales/v1/proposals", {"request_id": rid(), "opportunity_id": w.opportunity()["opportunity_id"],
                                            "lines": [{"line_id": "zbm.revenue_recovery_engagement"}]},
                    "sales_agent"), 201)
    w.ok(w.post(f"/sales/v1/proposals/{p['proposal_id']}/send", {"request_id": rid(), "contract_kind": "client_msa",
                                                                  "contract_ref": "msa-1"}, "sales_agent"))
    return p


def test_s1_l1_agent_alone_cannot_mark_won(w):
    p = _sent_proposal(w)
    w.refused(w.post(f"/sales/v1/proposals/{p['proposal_id']}/won", {"request_id": rid()}, "sales_agent"), 403,
              "ACCEPTANCE_REQUIRED")
    w.ports.legal.acceptance = "not_found"
    w.refused(w.post(f"/sales/v1/proposals/{p['proposal_id']}/won",
                     {"request_id": rid(), "acceptance": {"kind": "esign_envelope", "ref": "env-1"}}, "sales_agent"),
              403, "ACCEPTANCE_NOT_CONFIRMED")
    assert w.ports.onboarding.calls == [] and w.ports.finance.calls == []
    w.ports.legal.acceptance = "accepted"
    won = w.ok(w.post(f"/sales/v1/proposals/{p['proposal_id']}/won",
                      {"request_id": rid(), "acceptance": {"kind": "esign_envelope", "ref": "env-1"}}, "sales_agent"))
    assert won["status"] == "won" and w.ports.legal.acceptance_checks[-1][1] == p["content_sha256"]


def test_s1_l1_andre_may_mark_won_and_the_legal_stand_in_refuses_agents(tmp_path):
    ports = wired_ports()
    h = Harness(tmp_path, ports=ports)
    p = _sent_proposal(h)
    ports.legal = __import__("ports").NotWiredLegal()
    h.refused(h.post(f"/sales/v1/proposals/{p['proposal_id']}/won",
                     {"request_id": rid(), "acceptance": {"kind": "legal_acceptance", "ref": "a-1"}}, "sales_agent"),
              503, "LEGAL_UNAVAILABLE")
    h.refused(h.post(f"/sales/v1/proposals/{p['proposal_id']}/won", {"request_id": rid()}, "sales_agent", "wrong" * 8),
              403, "CALLER_NOT_ALLOWED")
    assert h.ok(h.andre(f"/sales/v1/proposals/{p['proposal_id']}/won", {"request_id": rid()}))["status"] == "won"
    assert h.ledger.of_type("proposal_won")
    assert ANDRE


# ------------------------------------------------------------------ S1-L3 durable log needs a real key

def test_s1_l3_non_production_with_a_data_dir_needs_a_real_key(tmp_path):
    with pytest.raises(RuntimeError, match="required whenever SALES_DATA_DIR"):
        config_mod.load(base_env(SALES_DATA_DIR=str(tmp_path / "d")))
