"""AEGIS r6 (ADR 0024): the sanctions marker on a held payment.

N2  The hourly job re-reads Compliance for every held client: a hold is upgraded to sanctions when Compliance now says
    so, never downgraded. A refund release re-reads Compliance and refuses when the refusal is now sanctions-grounded.
N3  The marker is sticky: a later hold on the same invoice inherits it (with an open OFAC break), an unverified hold
    carrying it is never released on an ALLOW, and no checkout is made on an invoice that carries a hold.
N4  (see test_compliance_gate_adr0024) a refusal whose sanctions flag is missing or malformed counts as sanctions.
N5  An OFAC release names the kind of OFAC document (license, unblocking report, Compliance Release).
N6  A sanctions hold refuses the other grounds even with an OFAC SHA attached (c1a); the ground is re-checked inside
    the lock (c1f).
Vis Only Andre's token sees the sanctions marker; other callers see `held`.
"""

from __future__ import annotations

from helpers import ANDRE_TOKEN, rid
from test_compliance_gate_adr0024 import _stripe_hr
from test_compliance_hold_r4 import CLIENT, _allow, _f7r, _hold, _job, _paid, _release
from test_compliance_hold_r5 import AUTH, _pay_then, _sanctioned
from test_stripe_incoming import sent
from test_sweep_followup import _charge


def _refuse_plain(hr, client=CLIENT):
    hr.f["compliance"].activation_available = True
    hr.f["compliance"].activations = {client: {"allowed": False, "sanctions": False,
                                               "reasons": ["HR-14: the OFAC search is older than 90 days"]}}


def _second_hold(hr, inv, kind="unverified", sanctions=False):
    from service import Op
    svc = hr.svc
    first = _hold(hr, inv)
    with svc.lock:
        op = Op(svc, f"test-second-{rid()}", "intel_02_receivables", inv["invoice_id"])
        svc._open_compliance_hold(op, inv["invoice_id"], kind, "again", "fin-rct-second", "5.00",
                                  first["payment_entry_id"], "card (Stripe)", "2026-10-02", sanctions=sanctions)
        svc._commit(op)
    return first, _hold(hr, inv)


# --- N2 -----------------------------------------------------------------------------------------------------------------

def test_the_job_upgrades_a_refused_hold_to_sanctions_and_never_downgrades(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _refuse_plain(hr))
    assert _hold(hr, inv)["sanctions"] is False
    _sanctioned(hr)                                              # Andre records an OFAC match later
    s = _job(hr)
    h = _hold(hr, inv)
    assert h["sanctions"] is True and s["holds_upgraded"] == 1, s
    assert "SANCTIONS" in hr.svc.db["breaks"][h["break_id"]]["note"] and \
        hr.svc.db["breaks"][h["break_id"]]["status"] == "open"
    _refuse_plain(hr)                                            # Compliance later says otherwise: no downgrade
    _job(hr)
    assert _hold(hr, inv)["sanctions"] is True
    _allow(hr)
    _job(hr)
    assert _hold(hr, inv)["sanctions"] is True                   # a refused hold is never released by the job


def test_a_refund_release_re_reads_compliance(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _refuse_plain(hr))
    h = _hold(hr, inv)
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", _charge(hr, pi))
    entry = _f7r(hr, h["receipt_id"])[0]["entry_id"]
    hr.f["compliance"].activation_available = False
    r = _release(hr, inv, basis="refund", entry_id=entry)
    assert r.status_code == 409 and "compliance_38" in r.text, r.text      # cannot confirm: refused
    _sanctioned(hr)
    r = _release(hr, inv, basis="refund", entry_id=entry)
    assert r.status_code == 409 and "SANCTIONS" in r.text, r.text
    assert _hold(hr, inv)["sanctions"] is True                              # and the hold is upgraded
    _refuse_plain(hr)
    r = _release(hr, inv, basis="refund", entry_id=entry)
    assert r.status_code == 409                                             # sticky: never refunded out now


def test_a_refund_release_goes_through_when_compliance_confirms_no_sanctions(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _refuse_plain(hr))
    h = _hold(hr, inv)
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", _charge(hr, pi))
    r = _release(hr, inv, basis="refund", entry_id=_f7r(hr, h["receipt_id"])[0]["entry_id"])
    assert r.status_code == 200, r.text


# --- N3 -----------------------------------------------------------------------------------------------------------------

def test_a_later_hold_inherits_the_sanctions_marker_with_an_open_ofac_break(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _sanctioned(hr))
    first, second = _second_hold(hr, inv, kind="unverified", sanctions=False)
    assert second["sanctions"] is True and second["kind"] == "unverified"
    b = hr.svc.db["breaks"][second["break_id"]]
    assert b["status"] == "open" and "SANCTIONS" in b["note"]
    _allow(hr)
    assert _job(hr)["holds_released"] == 0 and _hold(hr, inv)["sanctions"] is True   # never released on an ALLOW


def test_no_checkout_on_an_invoice_that_carries_a_hold(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _sanctioned(hr))
    hr.sim.refund(pi)                                            # refunded anyway at Stripe: the invoice is issued again
    sent(hr, "charge.refunded", _charge(hr, pi))
    assert hr.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    _allow(hr)
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()}, caller="onboarding")
    assert r.status_code == 409 and "held by Compliance" in r.text, r.text


# --- N5 / N6 --------------------------------------------------------------------------------------------------------------

def test_an_ofac_release_names_the_kind_of_ofac_document(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _sanctioned(hr))
    r = _release(hr, inv, basis="ofac_authorization", ofac_authorization_sha256=AUTH)
    assert r.status_code in (409, 422) and _hold(hr, inv) is not None
    r = _release(hr, inv, basis="ofac_authorization", ofac_authorization_sha256=AUTH, ofac_document_kind="memo")
    assert r.status_code == 422
    r = _release(hr, inv, basis="ofac_authorization", ofac_authorization_sha256=AUTH,
                 ofac_document_kind="unblocking_report")
    assert r.status_code == 200, r.text
    hist = hr.svc.db["invoices"][inv["invoice_id"]]["held_compliance_history"][-1]
    assert hist["ofac_document_kind"] == "unblocking_report" and hist["ofac_authorization_sha256"] == AUTH
    ev = hr.ledger.of_type("compliance_hold_released")[-1]
    assert "unblocking_report" in str(ev)


def test_a_sanctions_hold_refuses_other_grounds_even_with_an_ofac_sha(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _sanctioned(hr))
    h = _hold(hr, inv)
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", _charge(hr, pi))
    entry = _f7r(hr, h["receipt_id"])[0]["entry_id"]
    r = _release(hr, inv, basis="refund", entry_id=entry, ofac_authorization_sha256=AUTH,
                 ofac_document_kind="license")
    assert r.status_code == 409 and "SANCTIONS" in r.text, r.text
    _allow(hr)
    r = _release(hr, inv, basis="compliance_allow", ofac_authorization_sha256=AUTH, ofac_document_kind="license")
    assert r.status_code == 409 and "SANCTIONS" in r.text, r.text
    assert _hold(hr, inv) is not None


def test_the_ground_is_re_checked_inside_the_lock(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _refuse_plain(hr))
    fc = hr.f["compliance"]
    _allow(hr)
    real = fc.client_activation

    def answer(cid):
        a = real(cid)
        with hr.svc.lock:                       # the hold becomes a sanctions hold while the route reads Compliance
            i = hr.svc.db["invoices"][inv["invoice_id"]]
            hr.svc.db["invoices"][inv["invoice_id"]] = {**i, "held_compliance": {**i["held_compliance"],
                                                                                  "sanctions": True}}
        return a

    fc.client_activation = answer
    r = _release(hr, inv, basis="compliance_allow")
    assert r.status_code == 409 and "SANCTIONS" in r.text, r.text
    assert _hold(hr, inv) is not None


# --- visibility ---------------------------------------------------------------------------------------------------------

def test_only_andre_sees_the_sanctions_marker(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _sanctioned(hr))
    other = hr.ok(hr.get(f"/fin/v1/invoices/{inv['invoice_id']}", caller="onboarding"))
    assert other["held"] is True and "held_compliance" not in other and "sanctions" not in str(other).lower()
    mine = hr.ok(hr.client.get(f"/fin/v1/invoices/{inv['invoice_id']}",
                               headers={**hr.headers(None), "X-Andre-Approval-Token": ANDRE_TOKEN}))
    assert mine["held_compliance"]["sanctions"] is True
    brk = hr.ok(hr.get("/fin/v1/breaks", caller="onboarding"))
    assert "SANCTIONS" not in str(brk) and "sanctions" not in str(brk).lower()
    brk = hr.ok(hr.client.get("/fin/v1/breaks", headers={**hr.headers(None), "X-Andre-Approval-Token": ANDRE_TOKEN}))
    assert "SANCTIONS" in str(brk)
