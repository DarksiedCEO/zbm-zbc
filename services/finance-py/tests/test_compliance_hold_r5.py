"""AEGIS r5 (ADR 0024): payments held by Compliance.

C1    A hold grounded in sanctions is never refunded or released on an ALLOW: OFAC blocking rules (31 CFR 501.603 /
      501.604) apply, and only Andre's release recording an OFAC authorization or reference clears it.
C2    Finance's /health counts open Compliance holds (the private heartbeat check stops pinging while one is open).
R5-1  An ALLOW release after a refund prepares no client receipt for refunded money.
R5-2  A second hold on the same invoice closes the first hold's break and keeps it in history.
R5-3  The hourly job re-reads Compliance immediately before each release.
R5-4  The race guards: a hold released or changed between the two locks is not released twice (409 / skipped).
"""

from __future__ import annotations

from helpers import rid
from test_compliance_gate_adr0024 import _stripe_hr
from test_compliance_hold_r4 import CLIENT, _allow, _f7r, _hold, _job, _paid, _receipts, _release
from test_stripe_incoming import sent
from test_sweep_followup import _charge

AUTH = "c" * 64                       # SHA-256 of the OFAC authorization document Andre keeps


def _sanctioned(hr, client=CLIENT):
    hr.f["compliance"].activation_available = True
    hr.f["compliance"].activations = {client: {"allowed": False, "sanctions": True,
                                               "reasons": ["hold open: sanctions_match (hold-x)"]}}


# --- C1 -----------------------------------------------------------------------------------------------------------------

def _pay_then(hr, setup):
    from test_stripe_incoming import rr_invoice
    inv = rr_invoice(hr)
    cs = hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()},
                       caller="onboarding"))["checkout"]
    pi = hr.sim.pay(cs["session_id"])
    setup()
    out = sent(hr, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    return inv, pi, out


def test_a_sanctions_hold_is_marked_and_never_released_by_refund_or_allow(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _sanctioned(hr))
    h = _hold(hr, inv)
    assert out["status"] == "matched_held_compliance" and h["kind"] == "refused" and h["sanctions"] is True
    hr.sim.refund(pi)                                       # even if the money was returned at Stripe
    sent(hr, "charge.refunded", _charge(hr, pi))
    r = _release(hr, inv, basis="refund", entry_id=_f7r(hr, h["receipt_id"])[0]["entry_id"])
    assert r.status_code == 409 and "OFAC" in r.text, r.text
    _allow(hr)
    r = _release(hr, inv, basis="compliance_allow")
    assert r.status_code == 409 and "OFAC" in r.text, r.text
    assert _hold(hr, inv) is not None


def test_only_an_ofac_reference_releases_a_sanctions_hold(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _sanctioned(hr))
    r = _release(hr, inv, basis="ofac_authorization")
    assert r.status_code in (409, 422), r.text
    r = _release(hr, inv, basis="ofac_authorization", ofac_authorization_sha256=AUTH, ofac_document_kind="license")
    assert r.status_code == 200, r.text
    assert _hold(hr, inv) is None and _receipts(hr, inv) == []
    hist = hr.svc.db["invoices"][inv["invoice_id"]]["held_compliance_history"][-1]
    assert hist["basis"] == "ofac_authorization" and hist["ofac_authorization_sha256"] == AUTH
    assert hr.ledger.of_type("compliance_hold_released")


def test_an_ofac_reference_does_not_release_a_non_sanctions_hold(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _paid(hr, then="refuse")
    assert _hold(hr, inv)["sanctions"] is False
    r = _release(hr, inv, basis="ofac_authorization", ofac_authorization_sha256=AUTH, ofac_document_kind="license")
    assert r.status_code == 409 and _hold(hr, inv) is not None


def test_an_unverified_hold_the_job_finds_sanctioned_becomes_a_sanctions_hold(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _paid(hr, then="down")
    _sanctioned(hr)
    assert _job(hr)["holds_changed"] == 1
    h = _hold(hr, inv)
    assert h["kind"] == "refused" and h["sanctions"] is True


# --- C2 -----------------------------------------------------------------------------------------------------------------

def test_health_counts_open_compliance_holds(tmp_path):
    hr = _stripe_hr(tmp_path)
    assert hr.client.get("/health").json()["compliance_holds_open"] == 0
    inv, pi, out = _paid(hr, then="refuse")
    body = hr.client.get("/health").json()
    assert body["compliance_holds_open"] == 1
    assert inv["invoice_id"] not in hr.client.get("/health").text and CLIENT not in hr.client.get("/health").text
    _allow(hr)
    hr.ok(_release(hr, inv, basis="compliance_allow"))
    assert hr.client.get("/health").json()["compliance_holds_open"] == 0


# --- R5-1 ---------------------------------------------------------------------------------------------------------------

def test_an_allow_release_after_a_refund_prepares_no_receipt(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _paid(hr, then="refuse")
    hr.sim.refund(pi, 10000)                                          # partly refunded
    sent(hr, "charge.refunded", _charge(hr, pi))
    _allow(hr)
    hr.ok(_release(hr, inv, basis="compliance_allow"))
    assert _hold(hr, inv) is None and _receipts(hr, inv) == []
    assert hr.ledger.of_type("client_receipt_created") == []


# --- R5-2 ---------------------------------------------------------------------------------------------------------------

def test_a_second_hold_closes_the_first_break_and_keeps_it_in_history(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _paid(hr, then="refuse")
    first = _hold(hr, inv)
    svc = hr.svc
    with svc.lock:                                                   # a second held payment on the same invoice
        from service import Op
        op = Op(svc, "test-second-hold", "intel_02_receivables", inv["invoice_id"])
        svc._open_compliance_hold(op, inv["invoice_id"], "refused", "again", "fin-rct-second", "5.00",
                                  first["payment_entry_id"], "card (Stripe)", "2026-10-02", sanctions=False)
        svc._commit(op)
    second = _hold(hr, inv)
    assert second["receipt_id"] == "fin-rct-second"
    assert svc.db["breaks"][first["break_id"]]["status"] == "resolved"
    hist = svc.db["invoices"][inv["invoice_id"]]["held_compliance_history"]
    assert hist[-1]["receipt_id"] == first["receipt_id"] and hist[-1]["basis"] == "superseded_by_new_hold"


# --- R5-3 / R5-4 ----------------------------------------------------------------------------------------------------------

def test_the_job_re_reads_compliance_before_each_release(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _paid(hr, then="down")
    fc = hr.f["compliance"]
    _allow(hr)
    real = fc.client_activation
    n = {"reads": 0}

    def answer(cid):
        n["reads"] += 1
        if n["reads"] >= 2:                     # the client is refused between the sweep read and the release
            fc.activations = {cid: {"allowed": False, "reasons": ["hold open: sanctions_match"], "sanctions": True}}
        return real(cid)

    fc.client_activation = answer
    s = _job(hr)
    assert s["holds_released"] == 0 and n["reads"] >= 2, (s, n)
    assert _hold(hr, inv)["kind"] in ("unverified", "refused")


def test_a_hold_released_while_the_route_read_compliance_is_not_released_twice(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _paid(hr, then="refuse")
    fc = hr.f["compliance"]
    _allow(hr)
    real = fc.client_activation

    def answer(cid):
        a = real(cid)
        fc.client_activation = real                               # once
        hr.ok(_release(hr, inv, basis="compliance_allow"))         # another release lands between the two locks
        return a

    fc.client_activation = answer
    r = _release(hr, inv, basis="compliance_allow")
    assert r.status_code == 409 and "no Compliance hold" in r.text, r.text
    assert len(hr.ledger.of_type("compliance_hold_released")) == 1


def test_the_job_skips_a_hold_released_while_it_read_compliance(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _paid(hr, then="down")
    fc = hr.f["compliance"]
    _allow(hr)
    real = fc.client_activation
    n = {"reads": 0}

    def answer(cid):
        n["reads"] += 1
        if n["reads"] == 2:                                       # just before the job's release step
            fc.client_activation = real
            hr.ok(_release(hr, inv, basis="compliance_allow"))    # Andre releases it first
        return real(cid)

    fc.client_activation = answer
    s = _job(hr)
    assert s["holds_released"] == 0, s
    assert len(hr.ledger.of_type("compliance_hold_released")) == 1
    assert len(_receipts(hr, inv)) == 1
