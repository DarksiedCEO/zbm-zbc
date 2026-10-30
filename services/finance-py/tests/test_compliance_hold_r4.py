"""AEGIS r4 (ADR 0024): a Stripe payment from a client Compliance does not currently ALLOW is booked as received but
HELD until a valid release.

R4-1  Compliance unreachable at booking holds the payment too (no client receipt, no vendor payment, the invoice status
      SEO reads says held); the hourly job re-asks Compliance: ALLOW releases it, REFUSE converts it to a refused hold.
R4-2  A hold is released only (a) by a Stripe refund of at least the held amount, booked AFTER the payment (journal
      order, not timestamps), or (b) by Andre with a current Compliance ALLOW; a generic break resolution is refused.
R4-3  The post-creation re-check records a page expired only after Stripe confirms.
INFO  A compliance hold is the client's alone: it does not turn FC-02 red for every payout.
"""

from __future__ import annotations

import pytest

from helpers import ANDRE_TOKEN, rid
from test_compliance_gate_adr0024 import _flip_after, _stripe_hr
from test_media_billing import create, issue, pay_vendor
from test_stripe_incoming import rr_invoice, sent
from test_sweep_followup import _charge

CLIENT = "zbm-client-7"


def _paid(hr, *, then, inv=None):
    """The client pays on a page; when the payment is booked Compliance answers ``then`` (allow / refuse / down)."""
    inv = inv or rr_invoice(hr)
    cs = hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()},
                       caller="onboarding"))["checkout"]
    pi = hr.sim.pay(cs["session_id"])
    fc = hr.f["compliance"]
    if then == "refuse":
        fc.activations = {inv["client_id"]: {"allowed": False, "reasons": ["hold open: sanctions_match"]}}
    elif then == "down":
        fc.activation_available = False
    out = sent(hr, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    return inv, pi, out


def _hold(hr, inv):
    return hr.svc.db["invoices"][inv["invoice_id"]].get("held_compliance")


def _receipts(hr, inv):
    return [c for c in hr.svc.db["client_receipts"].values() if c["invoice_id"] == inv["invoice_id"]]


def _release(hr, inv, **body):
    return hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/compliance-hold/release", {"request_id": rid(), **body},
                   andre=ANDRE_TOKEN)


def _job(hr):
    return hr.ok(hr.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="scheduler"))["summary"]


def _allow(hr, client=CLIENT):
    hr.f["compliance"].activation_available = True
    hr.f["compliance"].activations = {client: {"allowed": True, "jurisdiction": "US-CA"}}


# --- R4-1: unreachable at booking is a hold too ----------------------------------------------------------------------

def test_a_payment_booked_while_compliance_is_down_is_held(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _paid(hr, then="down")
    assert out["status"] == "matched_held_unverified"
    i = hr.svc.db["invoices"][inv["invoice_id"]]
    assert i["status"] == "paid" and i["held_compliance"]["kind"] == "unverified"
    assert hr.svc.db["breaks"][i["held_compliance"]["break_id"]]["leg"] == "compliance_unverified"
    assert _receipts(hr, inv) == []
    st = hr.ok(hr.get(f"/fin/v1/invoices/{inv['invoice_id']}/status", caller="seo_02"))
    assert st["status"] == "paid" and st["held"] is True


def test_an_unverified_media_prepayment_never_pays_the_vendor(tmp_path):
    hr = _stripe_hr(tmp_path)
    m = create(hr)
    inv = issue(hr, m["invoice"])
    _paid(hr, then="down", inv=inv)
    hr.clock.advance(days=4)
    r = pay_vendor(hr, m["media_buy"]["buy_id"], "10000.00", paid_on="2026-10-06")
    assert r.status_code == 409 and "Compliance" in r.text, r.text


def test_the_job_releases_an_unverified_hold_when_compliance_allows(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, _ = _paid(hr, then="down")
    bid = _hold(hr, inv)["break_id"]
    _allow(hr)
    s = _job(hr)
    assert s["holds_released"] == 1 and s["holds_converted"] == 0, s
    assert _hold(hr, inv) is None
    assert hr.svc.db["breaks"][bid]["status"] == "resolved"
    assert len(_receipts(hr, inv)) == 1                                  # the receipt goes out now
    assert len(hr.ledger.of_type("compliance_hold_released")) == 1
    assert hr.ok(hr.get(f"/fin/v1/invoices/{inv['invoice_id']}/status", caller="seo_02"))["held"] is False


def test_the_job_converts_an_unverified_hold_to_refused_on_refuse(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, _ = _paid(hr, then="down")
    old = _hold(hr, inv)["break_id"]
    hr.f["compliance"].activation_available = True
    hr.f["compliance"].activations = {CLIENT: {"allowed": False, "reasons": ["jurisdiction US-CA is refuse"]}}
    s = _job(hr)
    assert s["holds_converted"] == 1 and s["holds_released"] == 0, s
    h = _hold(hr, inv)
    assert h["kind"] == "refused" and h["break_id"] != old
    assert hr.svc.db["breaks"][h["break_id"]]["leg"] == "compliance_hold"
    assert hr.svc.db["breaks"][old]["status"] == "resolved"
    assert _receipts(hr, inv) == []


def test_the_job_leaves_an_unverified_hold_while_compliance_stays_down(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, _ = _paid(hr, then="down")
    s = _job(hr)
    assert s["holds_released"] == 0 and s["holds_converted"] == 0 and _hold(hr, inv)["kind"] == "unverified"


# --- R4-2: what releases a hold ----------------------------------------------------------------------------------------

def _f7r(hr, rct_id):
    return [e for e in hr.svc.entries if e["memo_code"] == "F7r" and e["source"].get("id") == rct_id]


def test_a_full_refund_after_the_payment_releases_a_refused_hold(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, _ = _paid(hr, then="refuse")
    h = _hold(hr, inv)
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", _charge(hr, pi))
    refund = _f7r(hr, h["receipt_id"])
    assert len(refund) == 1
    r = _release(hr, inv, basis="refund", entry_id=refund[0]["entry_id"])
    assert r.status_code == 200, r.text
    assert _hold(hr, inv) is None and hr.svc.db["breaks"][h["break_id"]]["status"] == "resolved"
    assert _receipts(hr, inv) == []                                       # refunded: no receipt


def test_citing_the_payments_own_entry_or_a_partial_refund_does_not_release(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, _ = _paid(hr, then="refuse")
    h = _hold(hr, inv)
    r = _release(hr, inv, basis="refund", entry_id=h["payment_entry_id"])
    assert r.status_code == 409 and "refund" in r.text, r.text
    hr.sim.refund(pi, 10000)                                             # 100.00 of 1200.00
    sent(hr, "charge.refunded", _charge(hr, pi))
    part = _f7r(hr, h["receipt_id"])[0]
    r = _release(hr, inv, basis="refund", entry_id=part["entry_id"])
    assert r.status_code == 409 and "held amount" in r.text, r.text
    r = _release(hr, inv, basis="refund", entry_id=None)
    assert r.status_code in (409, 422)
    assert _hold(hr, inv)["kind"] == "refused"


def test_a_refund_entry_of_another_receipt_does_not_release(tmp_path):
    hr = _stripe_hr(tmp_path)
    other, opi, _ = _paid(hr, then="allow")
    inv, pi, _ = _paid(hr, then="refuse")
    hr.sim.refund(opi)
    sent(hr, "charge.refunded", _charge(hr, opi))
    other_rct = next(r for r in hr.svc.db["receipts"].values() if r["stripe"]["payment_intent"] == opi)
    e = _f7r(hr, other_rct["receipt_id"])[0]
    r = _release(hr, inv, basis="refund", entry_id=e["entry_id"])
    assert r.status_code == 409 and _hold(hr, inv) is not None


def test_andre_releases_only_with_a_current_compliance_allow(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, _ = _paid(hr, then="refuse")
    r = _release(hr, inv, basis="compliance_allow")
    assert r.status_code == 409 and "COMPLIANCE_NOT_ACTIVATED" in r.text, r.text
    hr.f["compliance"].activation_available = False
    r = _release(hr, inv, basis="compliance_allow")
    assert r.status_code == 409 and "compliance_38" in r.text
    _allow(hr)
    r = _release(hr, inv, basis="compliance_allow")
    assert r.status_code == 200, r.text
    assert r.json()["ledger_event_ids"] and _hold(hr, inv) is None and len(_receipts(hr, inv)) == 1
    r = _release(hr, inv, basis="compliance_allow")
    assert r.status_code == 409                                            # nothing held any more


def test_the_release_route_is_andre_only(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, _ = _paid(hr, then="refuse")
    for caller in ("onboarding", "scheduler", "seo_02"):
        r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/compliance-hold/release",
                    {"request_id": rid(), "basis": "compliance_allow"}, caller=caller)
        assert r.status_code in (401, 403), (caller, r.status_code)


def test_a_compliance_break_is_never_resolved_by_the_generic_route(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, _ = _paid(hr, then="refuse")
    h = _hold(hr, inv)
    r = hr.post(f"/fin/v1/breaks/{h['break_id']}/resolution",
                {"request_id": rid(), "explanation_code": "unknown", "entry_id": h["payment_entry_id"]},
                andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "compliance-hold/release" in r.text, r.text
    assert hr.svc.db["breaks"][h["break_id"]]["status"] == "open"


# --- INFO-3: a client's hold does not freeze everyone's payouts ----------------------------------------------------

def test_a_compliance_hold_does_not_turn_fc02_red(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, _ = _paid(hr, then="refuse")
    assert hr.svc._open_breaks() == []
    assert not any("FC-02" in r["message"] for r in hr.svc._control_block_reasons("run"))


# --- R4-3: the post-creation re-check never records an unconfirmed expiry ------------------------------------------

def test_the_recheck_keeps_the_page_open_when_stripe_does_not_confirm(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr)
    fc = hr.f["compliance"]
    _flip_after(hr, 1, "refuse")
    flipped = fc.client_activation

    def answer(client_id):
        a = flipped(client_id)
        if not a.allowed:
            hr.sim.fail_next = [500] * 10                 # Stripe cannot expire the page just made
        return a

    fc.client_activation = answer
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()}, caller="onboarding")
    assert r.status_code == 409 and "Stripe did not confirm the page just made is closed" in r.text, r.text
    sessions = list(hr.svc.db["stripe_sessions"].values())
    assert len(sessions) == 1 and sessions[0]["status"] == "open"
    assert "url" not in r.text or sessions[0]["url"] not in r.text
    fc.client_activation = flipped                        # Stripe works again; the client stays refused
    hr.sim.fail_next = []
    assert _job(hr)["closed"] == 1
    assert hr.svc.db["stripe_sessions"][sessions[0]["session_id"]]["status"] == "expired"


@pytest.mark.parametrize("held", [True, False])
def test_the_seo_contract_reads_the_held_flag(tmp_path, held):
    """finance-py answers `held`; seo-py's own parser (loaded by path) turns a held invoice into INVOICE_HELD."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "seo_finance_client", Path(__file__).resolve().parents[2] / "seo-py" / "src" / "finance_client.py")
    fc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fc)
    hr = _stripe_hr(tmp_path)
    inv, pi, _ = _paid(hr, then="refuse" if held else "allow")
    st = hr.ok(hr.get(f"/fin/v1/invoices/{inv['invoice_id']}/status", caller="seo_02"))
    facts = fc.parse_invoice(st, inv["invoice_id"])
    assert fc.assess(facts, CLIENT) == ("INVOICE_HELD" if held else fc.PAID)
