"""Money red team (Oct 30-31 2026), the Stripe-side findings, adapted from its repros
(scratchpad/audit/redteam-money/test_rtm_money.py).

RTM-02  A refund that later fails (amount_refunded goes down) is booked back; refund.* events are read back; nothing
        422-loops: an unexpected-but-valid state opens a break and answers 2xx.
RTM-03  Checkout pins adaptive_pricing[enabled]=false; a payment presented in another currency is booked as unapplied
        cash with a break (or a break only), answered 2xx, never a 503 loop.
RTM-05  A Dashboard refund of a sanctions-held payment opens a high-severity OFAC-relevant break (Andre-only), keeps
        the hold and the invoice held, and is never booked as a routine refund.
RTM-08  A dispute larger than the charge opens a break and answers 2xx; nothing is booked.
RTM-09  An invoice above the configured maximum (FIN_MAX_INVOICE_TOTAL, default 250,000.00) is refused.
"""

from __future__ import annotations

from decimal import Decimal

from helpers import ANDRE_TOKEN, rid
from test_compliance_gate_adr0024 import _stripe_hr
from test_compliance_hold_r8 import _ground
from test_stripe_incoming import bal, checkout, rr_invoice, send, sent


def charge_obj(hr, pi):
    return {"id": hr.sim.pis[pi]["latest_charge"], "object": "charge", "payment_intent": pi}


def _paid(hr, price="1200.00", method="us_bank_account"):
    inv = rr_invoice(hr, price)
    cs = hr.ok(checkout(hr, inv["invoice_id"]))["checkout"]
    pi = hr.sim.pay(cs["session_id"], method=method)
    sent(hr, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    return inv, cs, pi


# --- RTM-02 -------------------------------------------------------------------------------------------------------------

def test_rtm02_a_failed_refund_is_booked_back(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, cs, pi = _paid(hr)
    hr.sim.refund(pi, 50000)
    assert sent(hr, "charge.refunded", charge_obj(hr, pi))["status"] == "refund_booked"
    s1060 = bal(hr, "1060")
    ch = hr.sim.charges[hr.sim.pis[pi]["latest_charge"]]
    ch["amount_refunded"] = 0                                         # the ACH refund failed at the bank
    hr.sim._txn(50000, 0, ch["id"], "refund_failure")
    r = send(hr, "charge.refund.updated", {"id": "re_0001sim", "object": "refund", "payment_intent": pi,
                                          "status": "failed"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "refund_reversed"
    assert bal(hr, "1060") == s1060 + Decimal("500.00")
    rc = next(x for x in hr.svc.db["receipts"].values() if (x.get("stripe") or {}).get("payment_intent") == pi)
    assert rc["status"] == "matched" and rc["stripe"]["refunded"] == "0.00"
    i = hr.svc.db["invoices"][inv["invoice_id"]]
    assert i["status"] == "paid" and i.get("refunded") in (None, "0.00")
    assert any(b["leg"] == "stripe_refund_reversed" for b in hr.svc.db["breaks"].values())


def test_rtm02_refund_events_are_read_back_not_ignored(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, cs, pi = _paid(hr)
    hr.sim.refund(pi, 50000)
    r = sent(hr, "refund.updated", {"id": "re_0002sim", "object": "refund", "payment_intent": pi})
    assert r["status"] == "refund_booked"


def test_rtm02_a_full_refund_that_fails_reopens_nothing_and_books_back(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, cs, pi = _paid(hr)
    hr.sim.refund(pi)                                                  # full refund: the invoice is issued again
    sent(hr, "charge.refunded", charge_obj(hr, pi))
    assert hr.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    ch = hr.sim.charges[hr.sim.pis[pi]["latest_charge"]]
    ch["amount_refunded"] = 0
    hr.sim._txn(120000, 0, ch["id"], "refund_failure")
    r = send(hr, "refund.failed", {"id": "re_0003sim", "object": "refund", "payment_intent": pi})
    assert r.status_code == 200 and r.json()["status"] == "refund_reversed", r.text
    assert hr.svc.db["invoices"][inv["invoice_id"]]["status"] == "paid"


# --- RTM-03 -------------------------------------------------------------------------------------------------------------

def test_rtm03_checkout_pins_adaptive_pricing_off(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr, "1200.00")
    hr.ok(checkout(hr, inv["invoice_id"]))
    form = [r for r in hr.sim.requests if r["path"] == "/v1/checkout/sessions"][-1]["form"]
    assert form.get("adaptive_pricing[enabled]") == "false", form


def test_rtm03_a_payment_presented_in_another_currency_is_held_and_flagged_not_retried(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr, "1200.00")
    cs = hr.ok(checkout(hr, inv["invoice_id"]))["checkout"]
    pi = hr.sim.pay(cs["session_id"], method="card")
    hr.sim.pis[pi].update(currency="eur", amount=110000, amount_received=110000)
    r = send(hr, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    assert r.status_code == 200, r.text
    assert any(b["leg"] == "stripe_currency_mismatch" for b in hr.svc.db["breaks"].values())
    assert hr.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"          # never applied
    assert not [c for c in hr.svc.db["client_receipts"].values() if c["invoice_id"] == inv["invoice_id"]]
    r2 = send(hr, "payment_intent.succeeded", {"id": pi, "object": "payment_intent"})
    assert r2.status_code == 200


# --- RTM-05 -------------------------------------------------------------------------------------------------------------

def test_rtm05_a_refund_of_a_sanctions_held_payment_is_loud_and_keeps_the_hold(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr, "1200.00")
    cs = hr.ok(checkout(hr, inv["invoice_id"]))["checkout"]
    pi = hr.sim.pay(cs["session_id"], method="card")
    _ground(hr, "match")
    sent(hr, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    hr.sim.refund(pi)
    r = sent(hr, "charge.refunded", charge_obj(hr, pi))
    assert r["status"] == "refund_booked_held"
    brk = [b for b in hr.svc.db["breaks"].values() if b["leg"] == "stripe_refund_sanctions"]
    assert len(brk) == 1 and brk[0]["severity"] == "high" and "OFAC" in brk[0]["note"]
    i = hr.svc.db["invoices"][inv["invoice_id"]]
    assert i["held_compliance"]["sanctions"] is True and i["status"] == "paid"      # not routine 'issued'
    assert hr.ledger.of_type("compliance_hold_sanctions_refunded")
    other = hr.get("/fin/v1/breaks", caller="onboarding").text
    assert "OFAC" not in other and "sanction" not in other.lower()


# --- RTM-08 -------------------------------------------------------------------------------------------------------------

def test_rtm08_a_dispute_larger_than_the_charge_opens_a_break_and_books_nothing(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, cs, pi = _paid(hr, method="card")
    before = bal(hr, "1300")
    du = hr.sim.dispute(pi, amount=500000)
    r = send(hr, "charge.dispute.created", {"id": du, "object": "dispute", "payment_intent": pi})
    assert r.status_code == 200, r.text
    assert bal(hr, "1300") == before
    assert any(b["leg"] == "stripe_dispute_anomaly" for b in hr.svc.db["breaks"].values())


# --- RTM-09 -------------------------------------------------------------------------------------------------------------

def test_rtm09_an_invoice_above_the_maximum_is_refused(tmp_path):
    hr = _stripe_hr(tmp_path)
    from test_media_billing import LEGAL
    for price, ok in (("250000.00", True), ("250000.01", False), ("1000000000000.00", False)):
        body = {"request_id": rid(), "entity": "zbm", "client_id": "zbm-client-7", "kind": "service",
                "lines": [{"line_code": "revenue_recovery_services", "quantity": 1, "unit_price": price}],
                "payment_methods": ["ach", "wire"], "legal_ref": LEGAL}
        r = hr.post("/fin/v1/invoices", body, caller="onboarding")
        assert (r.status_code == 201) is ok, (price, r.status_code, r.text[:200])
        if not ok:
            assert "INVOICE_ABOVE_MAXIMUM" in r.text or "maximum" in r.text


# --- RTM-06: Andre sees the rule-expiry warning on billing readiness ----------------------------------------------------

def test_rtm06_billing_readiness_shows_andre_the_rules_about_to_expire(tmp_path):
    hr = _stripe_hr(tmp_path)
    hr.cmp.rule_expiry_answer = {"available": True, "within_30_days": ["US-OFAC-04"], "within_14_days": [],
                                 "within_7_days": [], "next": {"id": "US-OFAC-04", "expires_at": "2026-12-25"}}
    mine = hr.client.get("/fin/v1/clients/zbm-client-7/billing-readiness",
                         headers={**hr.headers(None), "X-Andre-Approval-Token": ANDRE_TOKEN})
    assert mine.status_code == 200, mine.text
    assert mine.json()["compliance_rule_expiry"]["within_30_days"] == ["US-OFAC-04"]
    other = hr.ok(hr.get("/fin/v1/clients/zbm-client-7/billing-readiness", caller="onboarding"))
    assert "compliance_rule_expiry" not in other
    hr.cmp.rule_expiry_answer = None                                     # Compliance unreachable: says so
    mine = hr.client.get("/fin/v1/clients/zbm-client-7/billing-readiness",
                         headers={**hr.headers(None), "X-Andre-Approval-Token": ANDRE_TOKEN}).json()
    assert mine["compliance_rule_expiry"] == {"available": False}


def test_rtm06_the_client_reads_compliance_readiness(tmp_path):
    from test_compliance_gate_adr0024 import cmp_client
    doc = {"expiry_warnings": {"within_30_days": ["US-OFAC-04"], "within_14_days": [], "within_7_days": [],
                               "client_activation_rules": {"within_30_days": ["US-OFAC-04"], "within_14_days": [],
                                                           "within_7_days": []},
                               "next_client_activation_rule": {"id": "US-OFAC-04", "expires_at": "2026-12-25"}}}
    a = cmp_client({"/compliance/v1/readiness": (200, doc)}).rule_expiry()
    assert a == {"available": True, "within_30_days": ["US-OFAC-04"], "within_14_days": [], "within_7_days": [],
                 "next": {"id": "US-OFAC-04", "expires_at": "2026-12-25"}}
    assert cmp_client({"/compliance/v1/readiness": (503, {})}).rule_expiry() is None
    assert cmp_client({"/compliance/v1/readiness": (200, {"x": 1})}).rule_expiry() is None
