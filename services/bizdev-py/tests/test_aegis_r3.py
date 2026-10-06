"""AEGIS round 3 on 63bef23 (Oct 6 2026, BLOCKING: one High). Regression tests from the reviewer's probes
(scratchpad aegis-nbd/test_r3.py, test_r3b.py); each fails on 63bef23 and passes after the fix.

R3-H1  a second reply was folded into the first reply's hold; Andre's decision on the first released the contact.
Low    unknown ticks were counted and written for ever, and the counter in state_sha256 could starve a reconcile.
Info   uppercase UUID request ids were refused.
Info   not_delivered / not_paid did not ask the port first.
"""

from __future__ import annotations

import uuid

from helpers import Harness, RecordingPayouts, RecordingSubmission, fin_id, rid, wired_ports


def _sent(h):
    c = h.contact()
    t = h.template()
    m = h.ok(h.queue(c, t), 201)
    h.ok(h.job("send-queue"))
    return c, t, m


def _reply(h, m, text):
    return h.ok(h.post("/replies", {"request_id": rid(), "message_id": m["message_id"], "text": text},
                       caller="provider_events"), 201)


# --------------------------------------------------------------------------------------------------- R3-H1

def test_h1_reviewer_sequence_a_stale_decision_never_releases_the_contact(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    c, t, m = _sent(h)
    r1 = _reply(h, m, "Thanks, let me think")
    seen_by_andre = h.hold_state(r1["hold_id"])                     # Andre opens T1 and looks
    h.clock.advance(days=1)
    r2 = _reply(h, m, "Actually please do not reach out again to our team")
    assert r2["hold_id"] != r1["hold_id"]                           # every reply has its own hold
    assert r2["task_id"] != r1["task_id"]                           # a new day: a new task
    h.refused(h.decide(r1["hold_id"], "resume", state=seen_by_andre), 409, "STATE_HASH_MISMATCH")
    assert h.ok(h.get(f"/contacts/{c['contact_id']}"))["held"] is True
    h.refused(h.queue(c, t), 403, "CONTACT_HELD")
    # with the current state, the decision covers BOTH holds and closes BOTH tasks: nothing orphaned
    assert sorted(next(x for x in h.ok(h.get("/holds")) if x["hold_id"] == r1["hold_id"])["group"]) == \
        sorted([r1["hold_id"], r2["hold_id"]])
    h.ok(h.decide(r1["hold_id"], "resume"))
    holds = {x["hold_id"]: x["status"] for x in h.ok(h.get("/holds"))}
    assert holds[r1["hold_id"]] == holds[r2["hold_id"]] == "lifted"
    assert not [x for x in h.ok(h.get("/tasks?status=open")) if x["kind"] == "review_reply"]


def test_h1_same_day_replies_share_one_task_linked_to_every_hold(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    c, t, m = _sent(h)
    r1 = _reply(h, m, "one")
    r2 = _reply(h, m, "two")
    assert r1["task_id"] == r2["task_id"] and r1["hold_id"] != r2["hold_id"]
    task = next(x for x in h.ok(h.get("/tasks")) if x["task_id"] == r1["task_id"])
    assert sorted(task["hold_ids"]) == sorted([r1["hold_id"], r2["hold_id"]])
    h.ok(h.decide(r2["hold_id"], "opt_out"))
    assert h.ok(h.get(f"/contacts/{c['contact_id']}"))["suppressed"] is True
    assert next(x for x in h.ok(h.get("/tasks")) if x["task_id"] == r1["task_id"])["status"] == "closed"


def test_h1_a_reply_after_andre_looked_makes_his_decision_stale_same_day(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    c, t, m = _sent(h)
    r1 = _reply(h, m, "maybe")
    seen = h.hold_state(r1["hold_id"])
    _reply(h, m, "no, stop")
    h.refused(h.decide(r1["hold_id"], "resume", state=seen), 409, "STATE_HASH_MISMATCH")
    assert h.ok(h.get(f"/contacts/{c['contact_id']}"))["held"] is True


def test_h1_decision_needs_a_state_hash(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    c, t, m = _sent(h)
    r1 = _reply(h, m, "hello")
    h.refused(h.post(f"/holds/{r1['hold_id']}/decision", {"request_id": rid(), "decision": "resume"}, andre=True), 422)


# --------------------------------------------------------------------------------------------------- Low: ticks

def _stuck(tmp_path, **env):
    sub = RecordingSubmission(TimeoutError("lost"))
    h = Harness(tmp_path, ports=wired_ports(submission=sub), **env)
    p = h.pursuit(kind="formal_pitch", deadline=None)
    h.bid(p["pursuit_id"])
    h.ok(h.submit(h.ready_response(p["pursuit_id"])), 201)
    return h


def test_low_ticks_stop_once_the_stuck_task_is_open(tmp_path):
    h = _stuck(tmp_path, NBD_UNKNOWN_TICKS_BEFORE_TASK="3")
    for _ in range(3):
        h.ok(h.job("submission-queue"))
    lines = len(h.svc.log)
    s = h.ok(h.get("/submissions"))[0]
    for _ in range(4):
        h.ok(h.job("submission-queue"))
    assert len(h.svc.log) - lines == 4                               # only the job_ran lines; no tick lines
    s2 = h.ok(h.get("/submissions"))[0]
    assert s2["unknown_ticks"] == 3 and s2["state_sha256"] == s["state_sha256"]
    h.ok(h.post(f"/submissions/{s['submission_id']}/reconcile",
                {"request_id": rid(), "outcome": "not_delivered", "state_sha256": s["state_sha256"]}, andre=True))


def test_low_payout_ticks_stop_too(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(payouts=RecordingPayouts("unavailable")), NBD_UNKNOWN_TICKS_BEFORE_TASK="2")
    did = h.won_deal()["deal_id"]
    h.ok(h.money_event(did, "payment", "100.00"))
    for _ in range(2):
        h.ok(h.job("payout-request"))
    p = h.ok(h.get("/payouts"))[0]
    lines = len(h.svc.log)
    for _ in range(3):
        h.ok(h.job("payout-request"))
    assert len(h.svc.log) - lines == 3
    p2 = h.ok(h.get("/payouts"))[0]
    assert p2["unknown_ticks"] == 2 and p2["state_sha256"] == p["state_sha256"]


# --------------------------------------------------------------------------------------------------- Info: case

def test_info_uppercase_request_ids_are_one_request(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    d = h.won_deal()
    rq = str(uuid.uuid4())
    body = {"finance_event_id": fin_id(), "deal_id": d["deal_id"], "kind": "payment", "amount": "100.00",
            "currency": "USD"}
    h.ok(h.post("/finance/events", {"request_id": rq.upper(), **body}, caller="finance_31"))
    again = h.ok(h.post("/finance/events", {"request_id": rq, **body}, caller="finance_31"))
    assert again["commission"]["client_paid"] == "100.00"            # the same request, answered once
    h.ok(h.post(f"/partners/{h.partner(key='mixed')['partner_id']}/rate",
                {"request_id": uuid.uuid4().hex.upper(), "version": 1, "rate_pct": "10.00"}))


# --------------------------------------------------------------------------------------------------- Info: port

def test_info_not_delivered_asks_the_port_first(tmp_path):
    h = _stuck(tmp_path)
    h.ok(h.job("submission-queue"))
    s = h.ok(h.get("/submissions"))[0]
    h.ports.submission.status_answer = ("accepted", "sub-ref-late")
    r = h.post(f"/submissions/{s['submission_id']}/reconcile",
               {"request_id": rid(), "outcome": "not_delivered", "state_sha256": s["state_sha256"]}, andre=True)
    h.refused(r, 409, "PORT_SAYS_DELIVERED")
    assert h.ok(h.get("/submissions"))[0]["status"] == "submitted"   # the port's answer applied


def test_info_not_delivered_stands_when_the_port_is_unknown(tmp_path):
    h = _stuck(tmp_path)
    h.ok(h.job("submission-queue"))
    s = h.ok(h.get("/submissions"))[0]
    asked = len(getattr(h.ports.submission, "reconciled", []))
    out = h.ok(h.post(f"/submissions/{s['submission_id']}/reconcile",
                      {"request_id": rid(), "outcome": "not_delivered", "state_sha256": s["state_sha256"]},
                      andre=True))
    assert out["status"] == "not_delivered"
    assert len(h.ports.submission.reconciled) == asked + 1            # the port was asked once, said unknown


def test_info_not_paid_asks_the_port_first(tmp_path):
    pay = RecordingPayouts("unavailable")
    h = Harness(tmp_path, ports=wired_ports(payouts=pay))
    did = h.won_deal()["deal_id"]
    h.ok(h.money_event(did, "payment", "100.00"))
    h.ok(h.job("payout-request"))
    p = h.ok(h.get("/payouts"))[0]
    pay.status_answer, pay.status_ref = "with_finance", fin_id("pay")
    r = h.post(f"/payouts/{p['payout_id']}/reconcile",
               {"request_id": rid(), "outcome": "not_paid", "state_sha256": p["state_sha256"]}, andre=True)
    h.refused(r, 409, "PORT_SAYS_DELIVERED")
    assert h.ok(h.get("/payouts"))[0]["status"] == "with_finance"
    assert len(pay.calls) == 1                                       # never requeued, never resent
