"""Tickets: status machine, priority, SLA timers and breach alerts (ADR 0014 decisions 8-10); phone plumbing
(decision 4)."""

from __future__ import annotations

from datetime import timedelta

import pytest

import config as config_mod
from helpers import Harness, base_env, rid


def _ticket(h, text="I need help with my campaign"):
    return h.ok(h.chat(text), 201)["ticket_id"]


def _status(h, tid, status, **kw):
    return h.post(f"/svc/v1/tickets/{tid}/status", {"request_id": rid(), "status": status}, **kw)


# --------------------------------------------------------------------------------------------------- status machine

def test_status_machine(h):
    tid = _ticket(h)
    assert _status(h, tid, "closed").json()["detail"] == "TRANSITION_NOT_ALLOWED"
    h.ok(_status(h, tid, "resolved"))
    h.ok(_status(h, tid, "closed"))
    for s in ("open", "resolved", "pending_customer", "escalated"):
        assert _status(h, tid, s).status_code == 409
    assert h.ok(h.get(f"/svc/v1/tickets/{tid}"))["status"] == "closed"


def test_a_message_after_close_opens_a_new_ticket_and_after_resolve_reopens(h):
    tid = _ticket(h)
    h.ok(_status(h, tid, "resolved"))
    again = h.ok(h.chat("one more thing please"), 201)
    assert again["ticket_id"] == tid
    t = h.ok(h.get(f"/svc/v1/tickets/{tid}"))
    assert t["status"] == "open" and t["reopened"] == 1
    h.ok(_status(h, tid, "resolved"))
    h.ok(_status(h, tid, "closed"))
    assert h.ok(h.chat("new issue now"), 201)["ticket_id"] != tid


def test_reopened_twice_is_a_complaint(h):
    tid = _ticket(h)
    for _ in range(2):
        h.ok(_status(h, tid, "resolved"))
        h.clock.advance(days=2)
        r = h.ok(h.chat("following up on this"), 201)
    assert r["action"] == "escalated"


def test_only_the_dashboard_changes_status(h):
    tid = _ticket(h)
    assert _status(h, tid, "resolved", caller="hub").status_code == 403


def test_tickets_are_per_brand(h):
    a = h.ok(h.chat("help please", brand="zbm"), 201)
    b = h.ok(h.chat("help please", brand="zbc"), 201)
    assert a["ticket_id"] != b["ticket_id"]
    assert [t["brand"] for t in h.ok(h.get("/svc/v1/tickets?brand=zbc"))] == ["zbc"]


# --------------------------------------------------------------------------------------------------- SLA

def test_sla_targets_follow_priority(h):
    tid = _ticket(h)
    t = h.ok(h.get(f"/svc/v1/tickets/{tid}"))
    assert t["priority"] == "p3" and t["first_due"] == "2026-10-07T02:00:00Z"          # 8 h
    r = h.ok(h.post(f"/svc/v1/tickets/{tid}/priority", {"request_id": rid(), "priority": "p1"}))
    assert r["first_due"] == "2026-10-06T19:00:00Z" and r["resolution_due"] == "2026-10-07T02:00:00Z"


def test_first_response_breach_alerts_andre_once(h):
    tid = _ticket(h)
    assert h.ok(h.job("sla-sweep"))["breaches"] == 0
    h.clock.advance(hours=8, minutes=1)
    assert h.ok(h.job("sla-sweep"))["breaches"] == 1
    assert h.ok(h.job("sla-sweep"))["breaches"] == 0                 # once
    t = h.ok(h.get(f"/svc/v1/tickets/{tid}"))
    assert t["breaches"] == ["first_response"]
    alerts = [a for a in h.ok(h.get("/svc/v1/alerts")) if a["code"] == "SLA_FIRST_RESPONSE_BREACHED"]
    assert len(alerts) == 1 and alerts[0]["delivery"] == "not_wired" and alerts[0]["subject"] == tid
    assert h.ledger.of_type("sla_breached")


def test_a_first_response_in_time_is_no_breach_but_resolution_still_counts(h):
    h.article()
    tid = h.ok(h.chat("what are your hours"), 201)["ticket_id"]
    h.clock.advance(days=3, minutes=1)
    h.ok(h.job("sla-sweep"))
    assert h.ok(h.get(f"/svc/v1/tickets/{tid}"))["breaches"] == ["resolution"]


def test_resolved_tickets_do_not_breach(h):
    tid = _ticket(h)
    h.ok(_status(h, tid, "resolved"))
    h.clock.advance(days=30)
    assert h.ok(h.job("sla-sweep"))["breaches"] == 0


def test_escalation_raises_priority_and_tightens_targets(h):
    tid = h.ok(h.chat("refund"), 201)["ticket_id"]
    t = h.ok(h.get(f"/svc/v1/tickets/{tid}"))
    assert t["priority"] == "p2" and t["first_due"] == "2026-10-06T22:00:00Z"


@pytest.mark.parametrize("over,msg", [
    ({"SVC_SLA_P1_FIRST_RESPONSE_MINUTES": "1"}, "from 5 to 2880"),
    ({"SVC_SLA_P2_RESOLUTION_MINUTES": "99999"}, "from 60 to 20160"),
    ({"SVC_SLA_P1_FIRST_RESPONSE_MINUTES": "600"}, "first-response target must not exceed"),
    ({"SVC_SLA_P1_RESOLUTION_MINUTES": "5000"}, "looser for p1 than for p2"),
])
def test_sla_settings_are_bounded(over, msg):
    with pytest.raises(RuntimeError, match=msg):
        config_mod.load(base_env(**over))


def test_sla_settings_configurable_within_bounds(tmp_path):
    h = Harness(tmp_path, SVC_SLA_P3_FIRST_RESPONSE_MINUTES="300")
    tid = _ticket(h)
    assert h.ok(h.get(f"/svc/v1/tickets/{tid}"))["first_due"] == "2026-10-06T23:00:00Z"


def test_job_idempotent_and_scheduler_only(h):
    body = {"request_id": rid()}
    a = h.ok(h.post("/svc/v1/jobs/sla-sweep/run", body, caller="scheduler"))
    b = h.ok(h.post("/svc/v1/jobs/sla-sweep/run", body, caller="scheduler"))
    assert b["already_ran"] is True and b["breaches"] == a["breaches"]
    assert h.post("/svc/v1/jobs/sla-sweep/run", {"request_id": rid()}).status_code == 403
    assert h.post("/svc/v1/jobs/nope/run", {"request_id": rid()}, caller="scheduler").status_code == 422


# --------------------------------------------------------------------------------------------------- phone

def _call(h, outcome="voicemail", frm="+13105551234", **extra):
    return h.post("/svc/v1/calls", {"request_id": rid(), "brand": "zbm", "from_number": frm,
                                    "started_at": "2026-10-06T17:00:00Z", "duration_seconds": 42,
                                    "outcome": outcome, **extra}, caller="voice_gateway")


def test_voicemail_becomes_a_ticket_in_andres_queue_with_an_alert(h):
    r = h.ok(_call(h, voicemail_ref="vm/abc", transcript_ref="tr/abc"), 201)
    t = h.ok(h.get(f"/svc/v1/tickets/{r['ticket_id']}"))
    assert t["channel"] == "phone" and t["queue"] == "andre" and t["status"] == "open"
    assert "VOICEMAIL_RECEIVED" in [a["code"] for a in h.ok(h.get("/svc/v1/alerts"))]
    assert h.svc.calls[r["call_id"]]["transcript_ref"] == "tr/abc"


def test_a_call_from_a_known_number_joins_the_conversation(h):
    cid = h.contact(phone="+13105551234")
    tid = h.ok(h.chat("help with my campaign"), 201)["ticket_id"]
    r = h.ok(_call(h, "answered"), 201)
    assert r["contact_id"] == cid and r["ticket_id"] == tid


def test_call_handoff_to_andre(h):
    r = h.ok(_call(h, "answered"), 201)
    h.ok(h.post(f"/svc/v1/calls/{r['call_id']}/handoff", {"request_id": rid()}, caller="voice_gateway"))
    t = h.ok(h.get(f"/svc/v1/tickets/{r['ticket_id']}"))
    assert t["status"] == "escalated" and t["queue"] == "andre"
    assert "CALL_HANDOFF" in [a["code"] for a in h.ok(h.get("/svc/v1/alerts"))]


def test_call_record_refs_only(h):
    r = _call(h, transcript="the whole transcript text")
    assert r.status_code == 422


def test_routing_rules_need_andre_and_decide_in_hours(h):
    body = {"request_id": rid(), "timezone": "America/Los_Angeles", "days": ["mon", "tue", "wed", "thu", "fri"],
            "open_hour": 9, "close_hour": 18, "in_hours": "ring_andre"}
    assert h.put("/svc/v1/phone/routing/zbm", body).status_code == 403
    assert h.ok(h.post("/svc/v1/calls/route", {"brand": "zbm"}, caller="voice_gateway"))["action"] == "voicemail"
    h.ok(h.put("/svc/v1/phone/routing/zbm", body, andre=True))
    d = h.ok(h.post("/svc/v1/calls/route", {"brand": "zbm"}, caller="voice_gateway"))     # Tue 11:00 LA
    assert d == {"brand": "zbm", "action": "ring_andre", "reason": "IN_HOURS", "voice_provider_wired": False}
    h.clock.advance(hours=8)                                                                # 19:00 LA
    assert h.ok(h.post("/svc/v1/calls/route", {"brand": "zbm"}, caller="voice_gateway"))["action"] == "voicemail"
    assert h.ok(h.post("/svc/v1/calls/route", {"brand": "zbc"}, caller="voice_gateway"))["reason"] == \
        "NO_ROUTING_RULES"


def test_voice_provider_setting_refuses_start():
    with pytest.raises(RuntimeError, match="SVC_VOICE_PROVIDER"):
        config_mod.load(base_env(SVC_VOICE_PROVIDER="twilio"))


def test_call_ticket_breach_counts_like_any_other(h):
    r = h.ok(_call(h, "missed"), 201)
    h.clock.at = h.clock.at + timedelta(hours=9)
    h.ok(h.job("sla-sweep"))
    assert h.ok(h.get(f"/svc/v1/tickets/{r['ticket_id']}"))["breaches"] == ["first_response"]
