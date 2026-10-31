"""AEGIS r10 R10-4 and the RTM-05 / RTM-08c partials: money of a client Compliance does not ALLOW never becomes plain
unapplied cash, and a refund or bank return of held money is loud for Andre.

- R10-4: a wire carrying a refused client's invoice token with the wrong amount, or a second Stripe payment on a
  refused client's paid invoice, is booked as unapplied cash AND held: the receipt carries the hold (with its OFAC
  ground on a sanctions refusal) and a Compliance break; a Dashboard refund of it is an OFAC-held refund (the hold's
  break goes high severity with the OFAC note, Andre is pushed), never a routine refund.
- RTM-05: a refund of a held payment keeps the invoice paid-held (it re-opens only when Andre releases the hold on its
  refund), Andre is pushed on a sanctions ground, a failed refund updates the hold's note, and a bank return of a held
  deposit marks the hold the same way.
- RTM-08c: the refund's own ``stripe_refund`` break carries the OFAC note too (Andre's token only).
"""

from __future__ import annotations

import json

from helpers import ANDRE_TOKEN, Harness, rid
from test_compliance_bank_rtm import bank_line, refuse
from test_compliance_gate_adr0024 import _stripe_hr
from test_money_redteam_rtm import _paid, charge_obj
from test_stripe_incoming import checkout, rr_invoice, send, sent

CLIENT = "zbm-client-7"


def _plain(hr, client=CLIENT):
    refuse(hr, client=client, sanctions=False, ground=None)


def _pushed(hr, kind):
    return [b for k, b in hr.f["push"].pushed if k == kind]


def _rc(hr, rid_):
    return hr.svc.db["receipts"][rid_]


def _visible_breaks(hr):
    return hr.get("/fin/v1/breaks", caller="onboarding").text


# --- R10-4: the bank path --------------------------------------------------------------------------------------------------

def test_r10_4_a_sanctioned_clients_wire_of_the_wrong_amount_is_held_not_plain_unapplied(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr, "700.00", methods=("ach", "wire"))
    refuse(hr)
    r = hr.ok(bank_line(hr, "zbm", "1010", "701.00", inv["invoice_id"]))
    res = r["results"][0]
    assert res["status"] == "unapplied_held_compliance", r
    rc = _rc(hr, res["receipt_id"])
    assert rc["status"] == "unapplied" and rc["held_compliance"]["sanctions_ground"] == "match"
    brk = hr.svc.db["breaks"][rc["held_compliance"]["break_id"]]
    assert brk["leg"] == "compliance_hold" and "OFAC" in brk["note"]
    assert any(b["leg"] == "unapplied_cash" and b.get("receipt_id") == res["receipt_id"]
               for b in hr.svc.db["breaks"].values())                   # still unapplied cash for reconciliation
    low = _visible_breaks(hr).lower()
    assert "ofac" not in low and "sanction" not in low
    assert hr.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"


def test_r10_4_a_plain_refusal_and_a_sanctions_refusal_answer_the_bank_feed_alike(tmp_path):
    out = []
    for setup in (refuse, _plain):
        (tmp_path / setup.__name__).mkdir()
        hr = _stripe_hr(tmp_path / setup.__name__)
        inv = rr_invoice(hr, "700.00", methods=("ach", "wire"))
        setup(hr)
        out.append(hr.ok(bank_line(hr, "zbm", "1010", "701.00", inv["invoice_id"]))["results"][0]["status"])
    assert out[0] == out[1] == "unapplied_held_compliance"


def test_r10_4_an_allowed_clients_wrong_amount_is_plain_unapplied(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr, "700.00", methods=("ach", "wire"))
    r = hr.ok(bank_line(hr, "zbm", "1010", "701.00", inv["invoice_id"]))
    assert r["results"][0]["status"] == "unapplied"
    assert "held_compliance" not in _rc(hr, r["results"][0]["receipt_id"])


def test_r10_4_compliance_down_holds_an_attributable_wrong_amount_as_unverified(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr, "700.00", methods=("ach", "wire"))
    hr.cmp.activation_available = False
    r = hr.ok(bank_line(hr, "zbm", "1010", "701.00", inv["invoice_id"]))
    assert r["results"][0]["status"] == "unapplied_held_unverified"


# --- R10-4: the Stripe path and the Dashboard refund ------------------------------------------------------------------------

def _second_payment(hr):
    inv, cs, pi = _paid(hr, method="card")
    refuse(hr)                                                   # the client is now an OFAC match
    pi2 = hr.sim.pay(cs["session_id"], method="card", force=True)
    out = sent(hr, "payment_intent.succeeded", {"id": pi2, "object": "payment_intent"})
    return inv, pi2, out


def test_r10_4_a_second_stripe_payment_from_a_sanctioned_client_is_held(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi2, out = _second_payment(hr)
    assert out["status"] == "unapplied_held_compliance", out
    rc = next(r for r in hr.svc.db["receipts"].values() if (r.get("stripe") or {}).get("payment_intent") == pi2)
    assert rc["held_compliance"]["sanctions_ground"] == "match"


def test_r10_4_a_dashboard_refund_of_held_unapplied_money_is_an_ofac_held_refund(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi2, out = _second_payment(hr)
    hr.sim.refund(pi2)
    r = sent(hr, "charge.refunded", charge_obj(hr, pi2))
    assert r["status"] == "refund_booked_held", r
    rc = next(r for r in hr.svc.db["receipts"].values() if (r.get("stripe") or {}).get("payment_intent") == pi2)
    brk = hr.svc.db["breaks"][rc["held_compliance"]["break_id"]]
    assert brk["severity"] == "high" and "OFAC" in brk["note"] and "rejected" in brk["note"], brk
    assert hr.ledger.of_type("compliance_hold_sanctions_refunded")
    assert _pushed(hr, "compliance_held_refund")
    low = _visible_breaks(hr).lower()
    assert "ofac" not in low and "sanction" not in low


# --- RTM-05 ---------------------------------------------------------------------------------------------------------------

def _held_paid(hr, setup):
    inv = rr_invoice(hr, "1200.00")
    cs = hr.ok(checkout(hr, inv["invoice_id"]))["checkout"]
    pi = hr.sim.pay(cs["session_id"], method="card")
    setup(hr)
    sent(hr, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    return inv, pi


def test_rtm05_a_refunded_held_invoice_stays_paid_held_until_andre_releases_it(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi = _held_paid(hr, _plain)
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", charge_obj(hr, pi))
    i = hr.svc.db["invoices"][inv["invoice_id"]]
    assert i["status"] == "paid" and i["held_compliance"] and i["refunded"] == "1200.00", i   # not silently issued
    f7r = [e for e in hr.svc.entries if e["memo_code"] == "F7r"][-1]
    from test_compliance_hold_r4 import _release
    hr.ok(_release(hr, inv, basis="refund", entry_id=f7r["entry_id"]))
    assert hr.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"      # re-opened by Andre's release


def test_rtm05_andre_is_pushed_when_a_sanctions_held_payment_is_refunded(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi = _held_paid(hr, refuse)
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", charge_obj(hr, pi))
    pushed = _pushed(hr, "compliance_held_refund")
    assert len(pushed) == 1 and "OFAC" in json.dumps(pushed[0])


def test_rtm05_a_plain_held_refund_pushes_andre_without_the_ofac_line(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi = _held_paid(hr, _plain)
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", charge_obj(hr, pi))
    pushed = _pushed(hr, "compliance_held_refund")
    assert len(pushed) == 1 and "OFAC" not in json.dumps(pushed[0])


def test_rtm05_f2_a_failed_refund_of_a_held_payment_keeps_it_paid_held_and_rewrites_the_note(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi = _held_paid(hr, refuse)
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", charge_obj(hr, pi))
    ch = hr.sim.charges[hr.sim.pis[pi]["latest_charge"]]
    ch["amount_refunded"] = 0
    hr.sim._txn(120000, 0, ch["id"], "refund_failure")
    r = send(hr, "refund.failed", {"id": "re_0009sim", "object": "refund", "payment_intent": pi})
    assert r.status_code == 200 and r.json()["status"] == "refund_reversed", r.text
    i = hr.svc.db["invoices"][inv["invoice_id"]]
    assert i["status"] == "paid" and i["held_compliance"]["sanctions"] is True
    note = hr.svc.db["breaks"][i["held_compliance"]["break_id"]]["note"]
    assert "left the account" not in note and "back in the Stripe balance" in note and "OFAC" in note, note


def test_rtm05_a_bank_return_of_a_sanctions_held_deposit_is_marked_for_andre():
    hr = Harness().ready()
    hr.profile(doc_id=hr.rate_card())
    inv = hr.deposit_invoice()
    hr.cmp.activations = {"client-1": {"allowed": False, "jurisdiction": "US-CA", "reasons": ["sanctions_match"],
                                       "sanctions": True, "ground": "match"}}
    hr.receive("zbc", "1020", "1000.00", inv["invoice_id"])
    held = hr.svc.db["invoices"][inv["invoice_id"]]["held_compliance"]
    hr.ok(hr.post(f"/fin/v1/receipts/{held['receipt_id']}/return",
                  {"request_id": rid(), "return_ref_sha256": "c" * 64, "return_code": "R01", "value_date": "2026-10-03"},
                  caller="bank_feed"))
    brk = hr.svc.db["breaks"][held["break_id"]]
    assert brk["severity"] == "high" and "OFAC" in brk["note"] and "returned" in brk["note"], brk
    assert hr.ledger.of_type("compliance_hold_sanctions_refunded")
    assert _pushed(hr, "compliance_held_refund")
    low = hr.get("/fin/v1/breaks", caller="onboarding").text.lower()
    assert "ofac" not in low and "sanction" not in low


# --- RTM-08c ---------------------------------------------------------------------------------------------------------------

def test_rtm08c_the_refund_break_of_a_sanctions_held_payment_carries_the_ofac_note_for_andre_only(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi = _held_paid(hr, refuse)
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", charge_obj(hr, pi))
    notes = [b.get("note") for b in hr.svc.db["breaks"].values() if b["leg"] == "stripe_refund"]
    assert any("OFAC" in (n or "") for n in notes), notes            # the red team's repro assertion
    mine = hr.ok(hr.get("/fin/v1/breaks", andre=ANDRE_TOKEN, caller=None)).get("breaks")
    assert any(b["leg"] == "stripe_refund" and "OFAC" in (b.get("note") or "") for b in mine)
    low = _visible_breaks(hr).lower()
    assert "ofac" not in low and "sanction" not in low
