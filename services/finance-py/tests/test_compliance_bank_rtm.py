"""Money red team (RTM-01, Oct 30-31 2026): the BANK path goes through the same Compliance gate and hold as Stripe.

A wire / ACH statement line, Andre's manual application of an unapplied receipt, a ZBC campaign deposit, and a media
vendor payment: for a client Compliance does not currently ALLOW (refused, sanctions with its ground, or unreachable)
the cash is booked truthfully but HELD: no client receipt, no campaign funded, no vendor paid. Adapted from the red
team's repros (scratchpad/audit/redteam-money/test_rtm_money.py: rtm01a, rtm01b, rtm01c).
"""

from __future__ import annotations

import hashlib

from helpers import ANDRE_TOKEN, rid
from test_compliance_gate_adr0024 import _stripe_hr
from test_compliance_hold_r4 import _job, _release
from test_media_billing import create, issue, pay_vendor
from test_stripe_incoming import rr_invoice


def refuse(hr, client="zbm-client-7", sanctions=True, ground="match"):
    hr.cmp.activation_available = True
    hr.cmp.activations = {client: {"allowed": False, "jurisdiction": "US-CA", "reasons": ["OFAC SDN match"],
                                   "sanctions": sanctions, "ground": ground}}


def bank_line(hr, entity, account, amount, token, caller="bank_feed"):
    line = {"txn_ref_sha256": hashlib.sha256(rid("txn").encode()).hexdigest(), "entity": entity, "account": account,
            "direction": "credit", "amount": amount, "value_date": "2026-10-02", "reference_token": token}
    hr.bank.deposit(entity, account, amount)
    return hr.post("/fin/v1/bank/events", {"request_id": rid(), "lines": [line]}, caller=caller)


def _receipts_for(hr, inv_id):
    return [c for c in hr.svc.db["client_receipts"].values() if c["invoice_id"] == inv_id]


def test_rtm01a_a_wire_from_a_sanctioned_client_is_booked_but_held(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr, "1200.00", methods=("ach", "wire"))
    refuse(hr)
    r = hr.ok(bank_line(hr, "zbm", "1010", "1200.00", inv["invoice_id"]))
    assert r["results"][0]["status"] == "matched_held_compliance", r
    i = hr.svc.db["invoices"][inv["invoice_id"]]
    assert i["status"] == "paid" and i["held_compliance"]["sanctions_ground"] == "match"
    assert _receipts_for(hr, inv["invoice_id"]) == []
    other = hr.ok(hr.get(f"/fin/v1/invoices/{inv['invoice_id']}", caller="onboarding"))
    assert other["held"] is True and "held_compliance" not in other


def test_rtm01a_a_wire_while_compliance_is_down_is_held_unverified_and_the_job_settles_it(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr, "1200.00", methods=("ach", "wire"))
    hr.cmp.activation_available = False
    r = hr.ok(bank_line(hr, "zbm", "1010", "1200.00", inv["invoice_id"]))
    assert r["results"][0]["status"] == "matched_held_unverified"
    hr.cmp.activation_available = True
    hr.cmp.activations = {}
    assert _job(hr)["holds_released"] == 1
    assert hr.svc.db["invoices"][inv["invoice_id"]].get("held_compliance") is None
    assert len(_receipts_for(hr, inv["invoice_id"])) == 1                  # the held receipt goes out now


def test_an_allowed_clients_wire_is_unchanged(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr, "1200.00", methods=("ach", "wire"))
    r = hr.ok(bank_line(hr, "zbm", "1010", "1200.00", inv["invoice_id"]))
    assert r["results"][0]["status"] == "matched" and len(_receipts_for(hr, inv["invoice_id"])) == 1


def test_rtm01b_no_vendor_is_paid_from_a_sanctioned_clients_wire(tmp_path):
    hr = _stripe_hr(tmp_path)
    m = create(hr)
    inv = issue(hr, m["invoice"])
    refuse(hr, client="zbm-client-1")
    hr.ok(bank_line(hr, "zbm", "1010", inv["total"], inv["invoice_id"]))
    hr.clock.advance(days=4)
    r = pay_vendor(hr, m["media_buy"]["buy_id"], "10000.00", paid_on="2026-10-06", tag="rtm01b")
    assert r.status_code == 409 and "Compliance" in r.text, r.text


def test_a_vendor_payment_reads_the_clients_activation_at_the_moment_of_payment(tmp_path):
    hr = _stripe_hr(tmp_path)
    m = create(hr)
    inv = issue(hr, m["invoice"])
    hr.ok(bank_line(hr, "zbm", "1010", inv["total"], inv["invoice_id"]))        # paid while ALLOW: not held
    assert hr.svc.db["invoices"][inv["invoice_id"]].get("held_compliance") is None
    hr.clock.advance(days=4)
    refuse(hr, client="zbm-client-1", sanctions=False, ground=None)           # refused afterwards
    r = pay_vendor(hr, m["media_buy"]["buy_id"], "10000.00", paid_on="2026-10-06", tag="live")
    assert r.status_code == 409 and "COMPLIANCE_NOT_ACTIVATED" in r.text, r.text
    hr.cmp.activation_available = False                                         # unreachable: refused too
    r = pay_vendor(hr, m["media_buy"]["buy_id"], "10000.00", paid_on="2026-10-06", tag="live2")
    assert r.status_code == 409 and "compliance_38" in r.text, r.text
    hr.cmp.activation_available = True
    hr.cmp.activations = {}
    hr.ok(pay_vendor(hr, m["media_buy"]["buy_id"], "10000.00", paid_on="2026-10-06", tag="live3"))


def test_rtm01c_a_refused_brands_deposit_does_not_fund_the_campaign(tmp_path):
    hr = _stripe_hr(tmp_path)
    hr.profile(doc_id=hr.rate_card())
    inv = hr.deposit_invoice()
    refuse(hr, client="client-1")
    res = hr.ok(bank_line(hr, "zbc", "1020", inv["total"], inv["invoice_id"]))
    assert res["results"][0]["status"] == "matched_held_compliance"
    assert hr.svc.db["profiles"]["camp-1"]["status"] != "funded"
    assert _receipts_for(hr, inv["invoice_id"]) == []
    # released on a current ALLOW: the campaign is funded and the receipt goes out then
    refuse(hr, client="client-1", sanctions=False, ground=None)
    hr.cmp.activations = {}
    hold = hr.svc.db["invoices"][inv["invoice_id"]]["held_compliance"]
    assert hold["sanctions"] is True                                            # a match: never released on an ALLOW
    r = _release(hr, inv, basis="compliance_allow")
    assert r.status_code == 409


def test_a_refused_brands_deposit_released_on_allow_funds_the_campaign(tmp_path):
    hr = _stripe_hr(tmp_path)
    hr.profile(doc_id=hr.rate_card())
    inv = hr.deposit_invoice()
    refuse(hr, client="client-1", sanctions=False, ground=None)
    hr.ok(bank_line(hr, "zbc", "1020", inv["total"], inv["invoice_id"]))
    assert hr.svc.db["profiles"]["camp-1"]["status"] != "funded"
    hr.cmp.activations = {}
    hr.ok(_release(hr, inv, basis="compliance_allow"))
    assert hr.svc.db["profiles"]["camp-1"]["status"] == "funded"
    assert len(_receipts_for(hr, inv["invoice_id"])) == 1


def test_andre_applying_an_unapplied_receipt_for_a_refused_brand_holds_it(tmp_path):
    hr = _stripe_hr(tmp_path)
    hr.profile(doc_id=hr.rate_card())
    inv = hr.deposit_invoice()
    res = hr.ok(bank_line(hr, "zbc", "1020", inv["total"], "no-such-token"))   # lands unapplied
    rct = res["results"][0]["receipt_id"]
    assert res["results"][0]["status"] == "unapplied"
    refuse(hr, client="client-1", sanctions=True, ground="place")
    r = hr.post(f"/fin/v1/receipts/{rct}/apply", {"request_id": rid(), "invoice_id": inv["invoice_id"]},
                andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text
    i = hr.svc.db["invoices"][inv["invoice_id"]]
    assert i["held_compliance"]["sanctions_ground"] == "place"
    assert hr.svc.db["profiles"]["camp-1"]["status"] != "funded" and _receipts_for(hr, inv["invoice_id"]) == []
