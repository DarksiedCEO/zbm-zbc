"""AEGIS round 1 on 2d398f8 (Oct 6 2026, BLOCKING). One regression test per finding and per reviewer probe
(scratchpad aegis-nbd/test_probes.py, test_p1c.py, test_p6.py); each fails on 2d398f8 and passes after the fix.

H1  a STOP reply was refused 422 when from_email did not parse: nothing held, nothing suppressed.
H2  the agent could mark a delivered bid lost, dropping it from the counterparty aggregate.
M1  a payout whose answer was lost was requeued; a refund then clawed it back although Finance may hold it.
M2  a raw nine-digit id fit in partner_key, counterparty.ref and request_id, and separators beat the scan.
M3  an oversized value overflowed the group aggregate: 500 on views, a stalled submission queue.
Lows: body addresses held only for unknown senders and not after NFKC; org_key did not fold confusables; the window
dropped deals that were still open. Port: 8480 collided with department 11.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from decimal import Decimal

import config as config_mod
from helpers import fin_id, Harness, RecordingPayouts, base_env, rid, wired_ports
from intelligences import i02_identity, i07_deal_threshold, i12_tax_refs
from ports import Delivery


def _sent_message(h):
    c = h.contact()
    t = h.template()
    m = h.ok(h.queue(c, t), 201)
    h.ok(h.job("send-queue"))
    return c, t, m


# --------------------------------------------------------------------------------------------------- H1

def test_h1_stop_from_an_idn_sender_is_recorded_held_and_suppressed(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    c, t, m = _sent_message(h)
    r = h.post("/replies", {"request_id": rid(), "message_id": m["message_id"],
                            "from_email": "pat@xn--80ak6aa92e.xn--p1ai", "text": "STOP emailing me"},
               caller="provider_events")
    ans = h.ok(r, 201)
    assert ans["held"] is True and ans["suppressed"] is True
    h.refused(h.queue(c, t), 403, "SUPPRESSED")


def test_h1_unparseable_senders_never_refuse_the_reply(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    c, t, m = _sent_message(h)
    for frm in ('"pat lee"@westagency.test', "pät@westagency.test", "a" * 65 + "@westagency.test", "not an email"):
        ans = h.ok(h.post("/replies", {"request_id": rid(), "message_id": m["message_id"], "from_email": frm,
                                       "text": "STOP emailing me"}, caller="provider_events"), 201)
        assert ans["held"] is True and ans["suppressed"] is True, frm   # through message_id
    assert h.ok(h.get(f"/contacts/{c['contact_id']}"))["suppressed"] is True


def test_h1_a_reply_with_nothing_resolvable_is_still_recorded_for_andre(h):
    for body in ({"text": "hello"}, {"message_id": "nb-msg-" + "0" * 40, "text": "STOP"},
                 {"from_email": "über@éxample", "text": "remove me"}):
        ans = h.ok(h.post("/replies", {"request_id": rid(), **body}, caller="provider_events"), 201)
        assert ans["held"] is True
        assert ans["task_id"] in {t["task_id"] for t in h.ok(h.get("/tasks?status=open"))}
        assert ans["hold_id"] in {x["hold_id"] for x in h.ok(h.get("/holds?status=active"))}


def test_h1_idn_addresses_parse_and_are_stored_hashed(h):
    assert i02_identity.email("Pat@XN--80AK6AA92E.XN--P1AI") == "pat@xn--80ak6aa92e.xn--p1ai"
    c = h.contact(email="pat@xn--80ak6aa92e.xn--p1ai", verify=False)
    assert c["email_hash"].startswith("email:") and "xn--" not in str(c)
    assert b"xn--80ak6aa92e" not in b"".join(e["payload_sha256"].encode() for e in h.ledger.events)


# --------------------------------------------------------------------------------------------------- H2 (probe P1)

def test_h2_agent_cannot_mark_a_delivered_bid_lost_and_it_stays_in_the_aggregate(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    a = h.pursuit(value="6000.00")
    h.bid(a["pursuit_id"])
    h.ok(h.submit(h.ready_response(a["pursuit_id"])), 201)
    assert h.ok(h.job("submission-queue"))["submitted"] == 1
    h.refused(h.post(f"/pursuits/{a['pursuit_id']}/lost", {"request_id": rid(), "reason_code": "other"}), 403,
              "ANDRE_APPROVAL_REQUIRED")
    h.refused(h.post(f"/pursuits/{a['pursuit_id']}/lost", {"request_id": rid(), "reason_code": "other"},
                     caller="dashboard"), 403, "ANDRE_APPROVAL_REQUIRED")
    assert h.ok(h.post(f"/pursuits/{a['pursuit_id']}/lost", {"request_id": rid(), "reason_code": "other"},
                       andre=True))["stage"] == "lost"
    b = h.pursuit(value="6000.00")
    assert b["deal_gate"]["aggregate"] == "12000.00" and b["deal_gate"]["needs_andre"] is True
    h.bid(b["pursuit_id"])
    rb = h.approve_response(h.response(b["pursuit_id"], [{"custom": "Second half of the same job."}]))
    h.refused(h.submit(rb), 409, "DEAL_APPROVAL_REQUIRED")


def test_h2_a_sending_submission_also_makes_lost_andre_only(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    p = h.pursuit()
    h.bid(p["pursuit_id"])
    s = h.ok(h.submit(h.ready_response(p["pursuit_id"])), 201)
    h.svc.submissions[s["submission_id"]]["status"] = "sending"        # outcome unknown: may have gone out
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/lost", {"request_id": rid(), "reason_code": "price"}), 403,
              "ANDRE_APPROVAL_REQUIRED")


# --------------------------------------------------------------------------------------------------- M1 (probes P3a/b)

class FlakyPayouts:
    """Finance takes the request but the first answer is lost (a timeout)."""
    wired = True

    def __init__(self, during=None):
        self.calls = []
        self.during = during
        self.status_answer = ("unknown", None)

    def request_payout(self, payout_id, payload):
        self.calls.append((payout_id, dict(payload)))
        if self.during and len(self.calls) == 1:
            self.during()
        if len(self.calls) == 1:
            raise TimeoutError("answer lost")
        return Delivery("delivered", f"fin:payout-{len(self.calls)}")

    def payout_status(self, payout_id):
        return Delivery(*self.status_answer)


def test_m1_lost_answer_stays_sending_and_a_refund_is_a_shortfall(tmp_path):
    fp = FlakyPayouts()
    h = Harness(tmp_path, ports=wired_ports(payouts=fp))
    did = h.won_deal(value="8000.00", rate="10.00")["deal_id"]
    h.ok(h.money_event(did, "payment", "8000.00"))
    h.ok(h.job("payout-request"))
    pay = h.ok(h.get("/payouts"))
    assert [(p["status"], p["amount"]) for p in pay] == [("sending", "800.00")]
    d = h.ok(h.money_event(did, "refund", "8000.00"))
    assert d["commission"]["shortfall"] == "800.00"
    assert any(t["kind"] == "clawback_shortfall" for t in h.ok(h.get("/tasks?status=open")))
    assert h.ok(h.get("/payouts"))[0]["amount"] == "800.00"            # never cut while Finance may hold it


def test_m1_refund_during_send_then_exception_never_resends(tmp_path):
    holder = {}

    def during():
        holder["refund"] = holder["h"].money_event(holder["did"], "refund", "8000.00").status_code

    fp = FlakyPayouts(during=during)
    h = Harness(tmp_path, ports=wired_ports(payouts=fp))
    holder["h"] = h
    holder["did"] = h.won_deal(value="8000.00", rate="10.00")["deal_id"]
    h.ok(h.money_event(holder["did"], "payment", "8000.00"))
    t = threading.Thread(target=lambda: holder.setdefault("job", h.job("payout-request")))
    t.start()
    t.join()
    h.ok(h.job("payout-request"))
    assert holder["refund"] == 200
    assert [c[1]["amount"] for c in fp.calls] == ["800.00"]


def test_m1_reconcile_refused_requeues_after_applying_the_shortfall(tmp_path):
    fp = FlakyPayouts()
    h = Harness(tmp_path, ports=wired_ports(payouts=fp))
    did = h.won_deal(value="8000.00", rate="10.00")["deal_id"]
    h.ok(h.money_event(did, "payment", "8000.00"))
    h.ok(h.job("payout-request"))
    h.ok(h.money_event(did, "refund", "2000.00"))                       # shortfall 200 while it is in flight
    fp.status_answer = ("refused", None)
    out = h.ok(h.job("payout-request"))
    assert out["refused"] == 1
    d = h.ok(h.get(f"/partner-deals/{did}"))
    assert d["commission"]["shortfall"] == "0.00" and d["commission"]["settled"] == "600.00"
    assert h.ok(h.get("/payouts"))[0]["status"] == "queued"
    h.ok(h.job("payout-request"))
    assert [c[1]["amount"] for c in fp.calls] == ["800.00", "600.00"]  # the requeued request, reduced, sent again


def test_m1_reconcile_with_finance_and_unknown(tmp_path):
    fp = FlakyPayouts()
    h = Harness(tmp_path, ports=wired_ports(payouts=fp))
    did = h.won_deal()["deal_id"]
    h.ok(h.money_event(did, "payment", "100.00"))
    h.ok(h.job("payout-request"))
    assert h.ok(h.job("payout-request"))["unknown"] == 1 and len(fp.calls) == 1
    fp.status_answer = ("with_finance", "fin:payout-held")
    assert h.ok(h.job("payout-request"))["with_finance"] == 1
    assert h.ok(h.get("/payouts"))[0]["status"] == "with_finance"


def test_m1_a_wired_port_saying_unavailable_is_unknown_not_requeued(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(payouts=RecordingPayouts("unavailable")))
    did = h.won_deal()["deal_id"]
    h.ok(h.money_event(did, "payment", "100.00"))
    h.ok(h.job("payout-request"))
    h.ok(h.job("payout-request"))
    assert h.ok(h.get("/payouts"))[0]["status"] == "sending" and len(h.ports.payouts.calls) == 1


# --------------------------------------------------------------------------------------------------- M2 (probe P4)

def test_m2_separator_bypasses_are_caught():
    for s in ("x1-23456789", "acme-1234-56789", "a1-2-3-4-5-6-7-8-9", "p12345-6789", "１２３４"
              "５６７８９", "1 2 3 4 5 6 7 8 9", "123 456 789"):
        assert i12_tax_refs.raw_tax_id(s), s


def test_m2_no_nine_digit_id_in_keys_refs_or_request_ids(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    smuggled_key = "-".join(("west", "1234", "56789"))          # a fake test tax id, built from parts (gitleaks)
    r = h.post("/partners", {"request_id": rid(), "partner_key": smuggled_key, "kind": "referral",
                             "brands": ["zbm"], "name": "West", "domain": "west.test"})
    assert r.status_code == 422
    p = h.partner(key="okpartner")
    for rq, ref in (("123-45-6789", "crm:abc"), (rid(), "crm:1-23456789")):
        r2 = h.post("/partner-deals", {"request_id": rq, "partner_id": p["partner_id"], "brand": "zbm",
                                       "counterparty": {"ref": ref, "name": "C", "domain": "c.test"},
                                       "deal_value": "100.00"})
        assert r2.status_code == 422, (rq, ref)
    r3 = h.post("/finance/events", {"request_id": "1234567890", "finance_event_id": fin_id(),
                                    "deal_id": "nb-pdl-" + "0" * 40, "kind": "payment", "amount": "1.00",
                                    "currency": "USD"}, caller="finance_31")
    assert r3.status_code == 422
    lines = b"\n".join(h.svc.log._lines)
    assert b"1-23456789" not in lines and b"123-45-6789" not in lines and b"1234-56789" not in lines


# --------------------------------------------------------------------------------------------------- M3 (probe P1c)

def test_m3_values_above_the_cap_are_refused(h):
    for v in ("999999999999999.99", "1000000000.01"):
        h.refused(h.post("/pursuits", {"request_id": rid(), "brand": "zbm", "kind": "formal_pitch", "title": "x",
                                       "counterparty": {"ref": "org:acme", "name": "Acme Inc", "domain": "acme.test"},
                                       "value": v}), 422, "VALUE_INVALID")
    assert h.pursuit(value="1000000000.00", kind="formal_pitch", deadline=None)["deal_gate"]["needs_andre"] is True
    p = h.partner()
    r = h.post("/partner-deals", {"request_id": rid(), "partner_id": p["partner_id"], "brand": "zbm",
                                  "counterparty": {"ref": "org:z", "name": "Zed", "domain": "zed.test"},
                                  "deal_value": "1000000000.01"})
    h.refused(r, 422, "VALUE_INVALID")


def test_m3_an_overflowing_group_fails_closed_and_never_stalls_the_queue(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    other = h.pursuit(value="6000.00", ref="org:other", name="Other", domain="other.test")
    h.bid(other["pursuit_id"])
    victim = h.pursuit(value="9000.00")
    h.bid(victim["pursuit_id"])
    h.ok(h.submit(h.approve_response(h.response(victim["pursuit_id"], [{"custom": "victim text"}]))), 201)
    h.ok(h.submit(h.ready_response(other["pursuit_id"])), 201)
    for _ in range(2):                                     # legacy rows past today's cap (the 2d398f8 path)
        big = h.pursuit(value="5.00", kind="formal_pitch", deadline=None)
        h.svc.pursuits[big["pursuit_id"]]["value"] = "999999999999999.99"
    v = h.ok(h.get(f"/pursuits/{victim['pursuit_id']}"))
    assert v["deal_gate"]["needs_andre"] is True and v["deal_gate"]["approved"] is False
    assert v["deal_gate"]["overflow"] is True and v["deal_gate"]["binding_sha256"] is None
    assert h.get("/pursuits").status_code == 200
    h.refused(h.post(f"/pursuits/{victim['pursuit_id']}/deal-approval",
                     {"request_id": rid(), "binding_sha256": "0" * 64}, andre=True), 409, "DEAL_APPROVAL_STALE")
    out = h.ok(h.job("submission-queue"))
    assert out["held"] == 1 and out["submitted"] == 1                 # the unrelated bid still goes out


def test_m3_one_failing_item_is_recorded_and_the_queue_carries_on(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    ids = []
    for i in range(2):
        p = h.pursuit(ref=f"org:q{chr(97 + i)}", name=f"Q{chr(97 + i)}", domain=f"q{chr(97 + i)}.test")
        h.bid(p["pursuit_id"])
        ids.append(h.ok(h.submit(h.ready_response(p["pursuit_id"])) if i == 0 else
                        h.submit(h.approve_response(h.response(p["pursuit_id"], [{"custom": "two"}]))), 201))
    h.svc.responses[ids[0]["response_id"]]["versions"]["1"]["doc"] = None       # a corrupt item
    out = h.ok(h.job("submission-queue"))
    assert out["errors"] == [ids[0]["submission_id"]] and out["submitted"] == 1


# --------------------------------------------------------------------------------------------------- lows (probe p6, P1b)

def test_low_body_addresses_held_even_for_a_known_sender(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    _sent_message(h)
    sam = h.contact(email="sam@other.test")
    h.ok(h.post("/replies", {"request_id": rid(), "from_email": "pat@westagency.test",
                             "text": "I left; write to sam@other.test instead"}, caller="provider_events"), 201)
    view = h.ok(h.get(f"/contacts/{sam['contact_id']}"))
    assert view["held"] is True and view["suppressed"] is False


def test_low_fullwidth_body_address_is_held(h):
    sam = h.contact(email="sam@other.test")
    h.ok(h.post("/replies", {"request_id": rid(), "from_email": "nobody@unknown.test",
                             "text": "contact ｓａｍ＠ｏｔｈｅｒ．"
                                     "ｔｅｓｔ"}, caller="provider_events"), 201)
    assert h.ok(h.get(f"/contacts/{sam['contact_id']}"))["held"] is True


def test_low_org_key_folds_confusables(h):
    assert i02_identity.org_key("Аcme Inc") == i02_identity.org_key("Acme Inc") == "acme"
    h.pursuit(value="6000.00")
    b = h.pursuit(value="6000.00", ref="org:cyr", name="Аcme Inc", domain="acme-cyr.test")
    assert b["deal_gate"]["aggregate"] == "12000.00"


def test_low_window_applies_to_closed_deals_only():
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    deals = {"new": {"keys": ["ref:a"], "value": "6000.00", "opened_at": "2026-10-01T00:00:00Z",
                     "status": "identified"},
             "old_open": {"keys": ["ref:a"], "value": "6000.00", "opened_at": "2023-01-01T00:00:00Z",
                          "status": "responding"},
             "old_won": {"keys": ["ref:a"], "value": "50.00", "opened_at": "2023-01-01T00:00:00Z", "status": "won"},
             "recent_won": {"keys": ["ref:a"], "value": "1.00", "opened_at": "2026-09-01T00:00:00Z",
                            "status": "won"}}
    total, members = i07_deal_threshold.aggregate("new", deals, now, 365)
    assert total == Decimal("12001.00") and members == ["new", "old_open", "recent_won"]


# --------------------------------------------------------------------------------------------------- port

def test_port_no_longer_collides_with_department_11():
    assert config_mod.load(base_env()).port == 8490
