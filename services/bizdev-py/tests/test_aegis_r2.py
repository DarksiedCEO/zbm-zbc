"""AEGIS round 2 on 5e16d09 (Oct 6 2026, NOT BLOCKING). One regression test per item and per reviewer probe
(scratchpad aegis-nbd/test_r2.py); each fails on 5e16d09 and passes after the fix.

N1  the eight-digit cap over-refused: UUID request ids, finance-py's generated ids, CRM refs.
N2  a submission (or payout) could stay ``sending`` for ever with nobody told; no way for Andre to settle it.
L1  only the first five body addresses were held, so a contact named sixth was missed.
L2  every reply opened its own task and hold; holds were scanned linearly.
L3  a payout refused for ever was resent for ever; the shortfall field disagreed with its open task.
L4  ``münchen.de`` and ``xn--mnchen-3ya.de`` were different counterparties (and the Unicode form was refused).
"""

from __future__ import annotations

import uuid

from helpers import Harness, RecordingPayouts, RecordingSubmission, fin_id, rid, wired_ports

DIGITY_UUID = "12345678-1234-4123-8123-123456789012"


# --------------------------------------------------------------------------------------------------- N1

def test_n1_uuid_request_ids_are_accepted_on_partner_and_finance_routes(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    d = h.won_deal()
    r = h.post("/finance/events", {"request_id": DIGITY_UUID, "finance_event_id": fin_id(), "deal_id": d["deal_id"],
                                   "kind": "payment", "amount": "100.00", "currency": "USD"}, caller="finance_31")
    h.ok(r)
    p = h.partner(key="hub")
    h.ok(h.post(f"/partners/{p['partner_id']}/rate", {"request_id": uuid.uuid4().hex, "version": 1,
                                                       "rate_pct": "10.00"}))


def test_n1_finance_py_generated_ids_are_accepted(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    d = h.won_deal()
    h.ok(h.money_event(d["deal_id"], "payment", "100.00", ev="fin-evt-" + "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"))
    cb = h.money_event(d["deal_id"], "chargeback", "100.00", ev="fin-evt-" + "0123456789ABCDEFGHJKMNPQRS")
    assert h.ok(cb)["commission"]["accrued"] == "0.00"                 # the clawback goes through
    for bad in ("fin:ev-1", "fin-evt-short", "fin-EVT-" + "a" * 40, "fin-evt-" + "I" * 26):
        h.refused(h.money_event(d["deal_id"], "payment", "1.00", ev=bad), 422)


def test_n1_crm_refs_are_scanned_not_capped(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    p = h.partner(key="hub")
    cp = {"ref": "hubspot:12345678901", "name": "Client", "domain": "client.test"}
    h.ok(h.post("/partner-deals", {"request_id": rid(), "partner_id": p["partner_id"], "brand": "zbm",
                                   "counterparty": cp, "deal_value": "100.00"}), 201)
    h.ok(h.post("/pursuits", {"request_id": rid(), "brand": "zbm", "kind": "formal_pitch", "title": "t",
                              "counterparty": cp, "value": "100.00"}), 201)
    for bad_ref in ("crm:123456789", "crm:12-3456789", "crm:x1-23456789"):
        for path, extra in (("/partner-deals", {"partner_id": p["partner_id"], "deal_value": "1.00"}),
                            ("/pursuits", {"kind": "formal_pitch", "title": "t", "value": "1.00"})):
            r = h.post(path, {"request_id": rid(), "brand": "zbm", **extra,
                              "counterparty": {**cp, "ref": bad_ref}})
            assert r.status_code == 422, (path, bad_ref)


def test_n1_names_are_scanned_not_capped(h):
    p = h.ok(h.post("/partners", {"request_id": rid(), "partner_key": "route-66", "kind": "referral",
                                  "brands": ["zbm"], "name": "Route 66 Diner 1234567", "domain": "route66.test"}), 201)
    assert p["name"] == "Route 66 Diner 1234567"
    r = h.post("/partners", {"request_id": rid(), "partner_key": "taxco", "kind": "referral", "brands": ["zbm"],
                             "name": "Tax Co 123 45 6789", "domain": "taxco.test"})
    assert r.status_code == 422 and "6789" not in r.text


def test_n1_request_ids_have_one_opaque_shape(h):
    for bad in ("r-abc", "123-45-6789", "ABCDEF0123456789", "0123456789abcde", "a" * 65, "west-1234"):
        r = h.post("/partners", {"request_id": bad, "partner_key": "shape", "kind": "referral", "brands": ["zbm"],
                                 "name": "Shape", "domain": "shape.test"})
        assert r.status_code == 422, bad
    for good in (str(uuid.uuid4()), uuid.uuid4().hex, "0123456789abcdef"):
        h.ok(h.post("/partners", {"request_id": good, "partner_key": "s-" + uuid.uuid4().hex[:8].replace("0", "a"),
                                  "kind": "referral", "brands": ["zbm"], "name": "Shape", "domain": "shape.test"}),
             201)


def test_n1_payout_paid_takes_a_finance_id(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    d = h.won_deal()
    h.ok(h.money_event(d["deal_id"], "payment", "100.00"))
    h.ok(h.job("payout-request"))
    pay = h.ok(h.get("/payouts"))[0]
    paid = h.ok(h.post(f"/finance/payouts/{pay['payout_id']}/paid", {"request_id": rid(),
                                                                      "finance_ref": fin_id("pay")},
                       caller="finance_31"))
    assert paid["status"] == "paid"


# --------------------------------------------------------------------------------------------------- N2

def _stuck_pitch(tmp_path, **env):
    sub = RecordingSubmission(TimeoutError("lost"))
    h = Harness(tmp_path, ports=wired_ports(submission=sub), **env)
    p = h.pursuit(kind="formal_pitch", deadline=None)
    h.bid(p["pursuit_id"])
    r = h.ready_response(p["pursuit_id"])
    s = h.ok(h.submit(r), 201)
    return h, p, r, s


def test_n2_a_stuck_pitch_without_a_deadline_reaches_andre_after_n_ticks(tmp_path):
    h, p, r, s = _stuck_pitch(tmp_path, NBD_UNKNOWN_TICKS_BEFORE_TASK="3")
    for _ in range(2):
        h.ok(h.job("submission-queue"))
    assert not [t for t in h.ok(h.get("/tasks")) if t["kind"] == "stuck_unknown"]
    for _ in range(3):
        h.ok(h.job("submission-queue"))
    stuck = [t for t in h.ok(h.get("/tasks?status=open")) if t["kind"] == "stuck_unknown"]
    assert len(stuck) == 1 and stuck[0]["code"] == "SUBMISSION_STUCK"
    assert h.ok(h.get("/submissions"))[0]["unknown_ticks"] == 5


def test_n2_andre_reconciles_a_stuck_submission(tmp_path):
    h, p, r, s = _stuck_pitch(tmp_path, NBD_UNKNOWN_TICKS_BEFORE_TASK="1")
    h.ok(h.job("submission-queue"))
    sub = h.ok(h.get("/submissions"))[0]
    url = f"/submissions/{sub['submission_id']}/reconcile"
    body = {"request_id": rid(), "outcome": "not_delivered", "state_sha256": sub["state_sha256"]}
    h.refused(h.post(url, body, caller="dashboard"), 403)
    h.refused(h.post(url, {**body, "state_sha256": "0" * 64}, andre=True), 409, "STATE_HASH_MISMATCH")
    out = h.ok(h.post(url, body, andre=True))
    assert out["status"] == "not_delivered"
    assert not [t for t in h.ok(h.get("/tasks?status=open")) if t["target"] == f"submission:{sub['submission_id']}"]
    h.ports.submission.status = "accepted"
    h.ok(h.submit(r), 201)                                              # resubmittable after not_delivered
    h.refused(h.post(url, {**body, "request_id": rid()}, andre=True), 409, "SUBMISSION_NOT_SENDING")


def test_n2_reconcile_delivered_moves_the_pursuit_on(tmp_path):
    h, p, r, s = _stuck_pitch(tmp_path)
    h.ok(h.job("submission-queue"))
    sub = h.ok(h.get("/submissions"))[0]
    h.ok(h.post(f"/submissions/{sub['submission_id']}/reconcile",
                {"request_id": rid(), "outcome": "delivered", "state_sha256": sub["state_sha256"]}, andre=True))
    assert h.ok(h.get(f"/pursuits/{p['pursuit_id']}"))["stage"] == "submitted"
    h.refused(h.submit(r), 409)                                         # delivered: never submitted again


def test_n2_stuck_payout_task_and_andre_reconcile(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(payouts=RecordingPayouts("unavailable")), NBD_UNKNOWN_TICKS_BEFORE_TASK="2")
    did = h.won_deal(value="8000.00", rate="10.00")["deal_id"]
    h.ok(h.money_event(did, "payment", "1000.00"))
    h.ok(h.job("payout-request"))                                       # sent, unknown: tick 1
    h.ok(h.job("payout-request"))                                       # reconcile unknown: tick 2
    stuck = [t for t in h.ok(h.get("/tasks?status=open")) if t["kind"] == "stuck_unknown"]
    assert len(stuck) == 1 and stuck[0]["code"] == "PAYOUT_STUCK"
    pay = h.ok(h.get("/payouts"))[0]
    url = f"/payouts/{pay['payout_id']}/reconcile"
    h.refused(h.post(url, {"request_id": rid(), "outcome": "paid", "state_sha256": pay["state_sha256"]},
                     caller="dashboard"), 403)
    h.ok(h.money_event(did, "refund", "500.00"))                        # shortfall 50 while it is stuck
    h.refused(h.post(url, {"request_id": rid(), "outcome": "not_paid", "state_sha256": "0" * 64}, andre=True), 409,
              "STATE_HASH_MISMATCH")
    pay = h.ok(h.get("/payouts"))[0]
    out = h.ok(h.post(url, {"request_id": rid(), "outcome": "not_paid", "state_sha256": pay["state_sha256"]},
                      andre=True))
    assert out["status"] == "queued" and out["amount"] == "50.00"       # requeued after the shortfall
    assert not [t for t in h.ok(h.get("/tasks?status=open")) if t["target"] == f"payout:{pay['payout_id']}"]


def test_n2_reconcile_paid(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(payouts=RecordingPayouts("unavailable")))
    did = h.won_deal()["deal_id"]
    h.ok(h.money_event(did, "payment", "100.00"))
    h.ok(h.job("payout-request"))
    pay = h.ok(h.get("/payouts"))[0]
    out = h.ok(h.post(f"/payouts/{pay['payout_id']}/reconcile",
                      {"request_id": rid(), "outcome": "paid", "state_sha256": pay["state_sha256"]}, andre=True))
    assert out["status"] == "paid"


# --------------------------------------------------------------------------------------------------- L1

def test_l1_every_contact_named_in_a_body_is_held(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    c = h.contact()
    t = h.template()
    body = " ".join(f"x{j}@spam{j}.test" for j in range(8)) + " pat@westagency.test"
    h.ok(h.post("/replies", {"request_id": rid(), "from_email": "spam@evil.test", "text": body},
                caller="provider_events"), 201)
    assert h.ok(h.get(f"/contacts/{c['contact_id']}"))["held"] is True
    h.refused(h.queue(c, t), 403, "CONTACT_HELD")
    hold = h.ok(h.get("/holds?status=active"))[0]
    assert len(hold["hashes"]) == 1 + 5 + 1                           # sender, five strangers, the contact


# --------------------------------------------------------------------------------------------------- L2

def test_l2_one_task_and_one_hold_per_sender_per_day(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    for _ in range(5):
        h.ok(h.post("/replies", {"request_id": rid(), "from_email": "spam1@evil.test", "text": "buy now"},
                    caller="provider_events"), 201)
    assert len([t for t in h.ok(h.get("/tasks")) if t["kind"] == "review_reply"]) == 1
    assert len(h.ok(h.get("/holds"))) == 1
    h.clock.advance(days=1)
    h.ok(h.post("/replies", {"request_id": rid(), "from_email": "spam1@evil.test", "text": "again"},
                caller="provider_events"), 201)
    assert len([t for t in h.ok(h.get("/tasks")) if t["kind"] == "review_reply"]) == 2
    assert h.svc.hold_by_hash                                          # indexed by hash


# --------------------------------------------------------------------------------------------------- L3

def test_l3_a_payout_refused_n_times_is_held_with_a_task(tmp_path):
    pay = RecordingPayouts("refused")
    h = Harness(tmp_path, ports=wired_ports(payouts=pay), NBD_PAYOUT_MAX_REFUSALS="3")
    did = h.won_deal()["deal_id"]
    h.ok(h.money_event(did, "payment", "100.00"))
    for _ in range(6):
        h.ok(h.job("payout-request"))
    assert len(pay.calls) == 3
    p = h.ok(h.get("/payouts"))[0]
    assert p["status"] == "held" and p["refusals"] == 3
    assert any(t["kind"] == "payout_refused" for t in h.ok(h.get("/tasks?status=open")))


def test_l3_shortfall_field_and_task_agree(tmp_path):
    pay = RecordingPayouts()
    h = Harness(tmp_path, ports=wired_ports(payouts=pay))
    did = h.won_deal(value="8000.00", rate="10.00")["deal_id"]
    h.ok(h.money_event(did, "payment", "5000.00"))
    h.ok(h.job("payout-request"))                                       # 500 with Finance
    d = h.ok(h.money_event(did, "refund", "5000.00"))
    assert d["commission"]["shortfall"] == "500.00"
    assert [t for t in h.ok(h.get("/tasks?status=open")) if t["kind"] == "clawback_shortfall"]
    d = h.ok(h.money_event(did, "payment", "8000.00"))                  # the accrual is restored (800)
    assert d["commission"]["shortfall"] == "0.00" and d["commission"]["unpaid"] == "300.00"
    assert not [t for t in h.ok(h.get("/tasks?status=open")) if t["kind"] == "clawback_shortfall"]
    h.ok(h.job("payout-request"))
    total = sum(float(p["amount"]) for p in h.ok(h.get("/payouts")) if p["status"] == "with_finance")
    assert total == 800.0


# --------------------------------------------------------------------------------------------------- L4

def test_l4_unicode_and_punycode_domains_are_one_key(h):
    h.pursuit(value="6000.00", ref="org:m1", name="Stadtwerke", domain="münchen.de")
    b = h.pursuit(value="6000.00", ref="org:m2", name="Other", domain="xn--mnchen-3ya.de")
    assert b["deal_gate"]["aggregate"] == "12000.00"
    h.contact(email="info@münchen.de", verify=False)
    h.refused(h.post("/contacts", {"request_id": rid(), "brand": "zbm", "email": "info@xn--mnchen-3ya.de",
                                   "name": "Info"}), 409, "CONTACT_EXISTS")
