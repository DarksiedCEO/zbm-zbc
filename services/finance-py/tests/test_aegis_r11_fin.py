"""AEGIS r11 (finance-py): R11-1, R11-2, R11-3, R11-4, R11-5 and the stuck approved refund. Adapted from AEGIS's
probes (scratchpad/r11/probes/test_r11_probe_fin.py: P2, P2b, P3, P5, P8).

- R11-1: Andre's ``receipts/{id}/apply`` reads the RECEIPT's own hold and its source client: held money is never
  applied to any invoice (another client's above all) until Andre releases the receipt's hold on its grounds; an
  unheld receipt whose source client Compliance does not ALLOW is refused; the generic break paths never close a
  Compliance break.
- R11-2: holds on unapplied money count in /health ``compliance_holds_open``, the hourly job re-reads them (release an
  unverified one on ALLOW, convert, upgrade the ground) and Andre releases one with the same bases and OFAC document
  kinds as an invoice hold.
- R11-3: Finance's audit export and evidence are Andre-only (the hold rk / event id let any caller recover the real
  hold event type); every caller identity is refused.
- R11-4: a funded brand Compliance no longer allows opens a DURABLE founder alert (an open high-severity
  ``compliance_hold`` break, ids only, recorded with the job's state change) that the founder-alert dispatcher of ADR
  0027 reads; it closes when the brand is allowed again. No in-memory "already pushed" state.
- R11-5: a hold booked while the release gathers its inputs still excludes the item (local holds re-read under the
  lock).
- Stuck approved refund: Andre cancels it with a reason (the deposit goes back to 2010; a new proposal can follow).
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from helpers import ANDRE_TOKEN, Harness, rid


def _match(hr, client="client-1"):
    hr.cmp.activation_available = True
    hr.cmp.activations = {client: {"allowed": False, "jurisdiction": "US-CA", "reasons": ["hold open: sanctions_match"],
                                   "sanctions": True, "ground": "match"}}


def _plain(hr, client="client-1"):
    hr.cmp.activation_available = True
    hr.cmp.activations = {client: {"allowed": False, "jurisdiction": "US-CA", "reasons": ["outside"],
                                   "sanctions": False}}


def _allow(hr):
    hr.cmp.activation_available = True
    hr.cmp.activations = {}


def _job(hr):
    return hr.ok(hr.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="scheduler"))["summary"]


def _two_clients(hr):
    hr.profile(doc_id=hr.rate_card())
    inv1 = hr.deposit_invoice()                                   # client-1, 1000.00
    hr.profile(campaign="camp-9", client="client-9", doc_id=hr.rate_card(campaign="camp-9"))
    inv9 = hr.deposit_invoice(campaign="camp-9", client="client-9", amount="999.00")
    return inv1, inv9


def _apply(hr, rct, inv):
    return hr.post(f"/fin/v1/receipts/{rct}/apply", {"request_id": rid(), "invoice_id": inv["invoice_id"]},
                   andre=ANDRE_TOKEN)


def _release(hr, rct, **body):
    return hr.post(f"/fin/v1/receipts/{rct}/compliance-hold/release", {"request_id": rid(), **body}, andre=ANDRE_TOKEN)


def _held_unapplied(hr, inv9, setup=_match):
    setup(hr, "client-9")
    r = hr.receive("zbc", "1020", "1000.00", inv9["invoice_id"])   # wrong amount: unapplied, attributable to client-9
    return r["results"][0]


# --- R11-1 ------------------------------------------------------------------------------------------------------------------

def test_r11_1_p2_held_unapplied_money_is_never_applied_to_another_clients_invoice():
    hr = Harness().ready()
    inv1, inv9 = _two_clients(hr)
    res = _held_unapplied(hr, inv9)
    assert res["status"] == "unapplied_held_compliance"
    rct = res["receipt_id"]
    a = _apply(hr, rct, inv1)
    assert a.status_code == 409 and "held by Compliance" in a.text, a.text
    assert hr.svc.db["profiles"]["camp-1"]["status"] == "approved"
    assert hr.svc.db["invoices"][inv1["invoice_id"]]["status"] == "issued"
    rc = hr.svc.db["receipts"][rct]
    assert rc["status"] == "unapplied" and rc["held_compliance"]
    assert all(b["status"] == "open" for b in hr.svc.db["breaks"].values() if b.get("receipt_id") == rct)
    hr.payee()
    assert hr.ok(hr.handoff())["allowed"] is False                      # nobody accrues from it


def test_r11_1_held_money_is_not_applied_to_its_own_clients_invoice_either():
    hr = Harness().ready()
    inv1, inv9 = _two_clients(hr)
    hr.profile(campaign="camp-9b", client="client-9", doc_id=hr.rate_card(campaign="camp-9b"))
    other9 = hr.deposit_invoice(campaign="camp-9b", client="client-9", amount="1000.00")    # issued while allowed
    res = _held_unapplied(hr, inv9)
    a = _apply(hr, res["receipt_id"], other9)
    assert a.status_code == 409 and "held by Compliance" in a.text, a.text


def test_r11_1_an_unheld_receipt_whose_source_client_is_refused_now_is_not_applied_elsewhere():
    hr = Harness().ready()
    inv1, inv9 = _two_clients(hr)
    r = hr.receive("zbc", "1020", "1000.00", inv9["invoice_id"])     # client-9 allowed then: plain unapplied
    rct = r["results"][0]["receipt_id"]
    assert r["results"][0]["status"] == "unapplied"
    _match(hr, "client-9")                                           # the source client is a match now
    a = _apply(hr, rct, inv1)
    assert a.status_code == 409, a.text
    assert ("client_activation", "client-9") in hr.cmp.calls
    assert hr.svc.db["invoices"][inv1["invoice_id"]]["status"] == "issued"


def test_r11_1_the_generic_break_resolution_never_closes_a_held_receipts_breaks():
    hr = Harness().ready()
    inv1, inv9 = _two_clients(hr)
    res = _held_unapplied(hr, inv9)
    for b in [b for b in hr.svc.db["breaks"].values() if b.get("receipt_id") == res["receipt_id"]]:
        r = hr.post(f"/fin/v1/breaks/{b['break_id']}/resolution",
                    {"request_id": rid(), "explanation_code": "misapplied_receipt",
                     "entry_id": hr.svc.entries[-1]["entry_id"]}, andre=ANDRE_TOKEN)
        assert r.status_code == 409, (b["leg"], r.text)
        assert hr.svc.db["breaks"][b["break_id"]]["status"] == "open"


def test_r11_1_after_andres_release_the_money_can_be_applied():
    hr = Harness().ready()
    inv1, inv9 = _two_clients(hr)
    res = _held_unapplied(hr, inv9, setup=_plain)
    _allow(hr)
    hr.ok(_release(hr, res["receipt_id"], basis="compliance_allow"))
    assert "held_compliance" not in hr.svc.db["receipts"][res["receipt_id"]]
    hr.ok(_apply(hr, res["receipt_id"], inv1))
    assert hr.svc.db["profiles"]["camp-1"]["status"] == "funded"


# --- R11-2 ------------------------------------------------------------------------------------------------------------------

def test_r11_2_p2b_an_unapplied_hold_counts_in_health():
    hr = Harness().ready()
    hr.profile(doc_id=hr.rate_card())
    inv = hr.deposit_invoice()
    _match(hr)
    r = hr.receive("zbc", "1020", "1001.00", inv["invoice_id"])
    assert r["results"][0]["status"] == "unapplied_held_compliance"
    assert hr.client.get("/health").json()["compliance_holds_open"] == 1


def test_r11_2_the_job_releases_an_unverified_unapplied_hold_on_allow_and_converts_on_refuse():
    hr = Harness().ready()
    inv1, inv9 = _two_clients(hr)
    hr.cmp.activation_available = False
    a = hr.receive("zbc", "1020", "1000.00", inv9["invoice_id"])["results"][0]
    b = hr.receive("zbc", "1020", "1001.00", inv9["invoice_id"])["results"][0]
    assert a["status"] == b["status"] == "unapplied_held_unverified"
    _allow(hr)
    s = _job(hr)
    assert s["holds_released"] >= 2, s
    assert not hr.svc.db["receipts"][a["receipt_id"]].get("held_compliance")
    hr2 = Harness().ready()
    _, inv9b = _two_clients(hr2)
    hr2.cmp.activation_available = False
    c = hr2.receive("zbc", "1020", "1000.00", inv9b["invoice_id"])["results"][0]
    _match(hr2, "client-9")
    _job(hr2)
    h = hr2.svc.db["receipts"][c["receipt_id"]]["held_compliance"]
    assert h["kind"] == "refused" and h["sanctions_ground"] == "match"


def test_r11_2_the_job_upgrades_a_refused_unapplied_hold_to_a_match():
    hr = Harness().ready()
    _, inv9 = _two_clients(hr)
    res = _held_unapplied(hr, inv9, setup=_plain)
    _match(hr, "client-9")
    _job(hr)
    h = hr.svc.db["receipts"][res["receipt_id"]]["held_compliance"]
    assert h["sanctions_ground"] == "match"
    assert "OFAC" in hr.svc.db["breaks"][h["break_id"]]["note"]


@pytest.mark.parametrize("kind", ["license", "unblocking_report", "compliance_release", "delisting"])
def test_r11_2_andre_releases_a_sanctions_unapplied_hold_only_with_an_ofac_document(kind):
    hr = Harness().ready()
    _, inv9 = _two_clients(hr)
    res = _held_unapplied(hr, inv9)
    _allow(hr)
    r = _release(hr, res["receipt_id"], basis="compliance_allow")
    assert r.status_code == 409 and "SANCTIONS" in r.text, r.text
    r = _release(hr, res["receipt_id"], basis="ofac_authorization", ofac_authorization_sha256="d" * 64,
                 ofac_document_kind="rejected_transaction_report")
    assert r.status_code == 409
    hr.ok(_release(hr, res["receipt_id"], basis="ofac_authorization", ofac_authorization_sha256="d" * 64,
                   ofac_document_kind=kind))
    rc = hr.svc.db["receipts"][res["receipt_id"]]
    assert not rc.get("held_compliance") and rc["held_compliance_history"][-1]["basis"] == "ofac_authorization"
    assert hr.client.get("/health").json()["compliance_holds_open"] == 0


def test_r11_2_the_receipt_release_is_andre_only_and_refuses_a_plain_hold_while_still_refused():
    hr = Harness().ready()
    _, inv9 = _two_clients(hr)
    res = _held_unapplied(hr, inv9, setup=_plain)
    r = hr.post(f"/fin/v1/receipts/{res['receipt_id']}/compliance-hold/release",
                {"request_id": rid(), "basis": "compliance_allow"}, caller="scheduler")
    assert r.status_code in (401, 403)
    assert _release(hr, res["receipt_id"], basis="compliance_allow").status_code == 409        # still refused


# --- R11-3 ------------------------------------------------------------------------------------------------------------------

def test_r11_3_finance_audit_routes_refuse_every_caller_identity():
    from helpers import CALLERS
    hr = Harness().ready()
    assert CALLERS
    for path in ("/fin/v1/audit/evidence", "/fin/v1/audit/export"):
        for caller in sorted(CALLERS):
            r = hr.get(path, caller=caller)
            assert r.status_code in (401, 403), (path, caller, r.status_code)
        assert hr.get(path, caller=None, andre=ANDRE_TOKEN).status_code == 200


def test_r11_3_p8_no_caller_recovers_a_hold_event_type():
    """AEGIS P3/P8: recomputing ``evidence_id(rk, type, payload_sha256)`` needs the row; no caller gets one."""
    hr = Harness().ready()
    hr.profile(doc_id=hr.rate_card())
    inv = hr.deposit_invoice()
    _plain(hr)
    hr.receive("zbc", "1020", "1000.00", inv["invoice_id"])
    _match(hr)
    _job(hr)
    for caller in ("onboarding", "scheduler", "seo_02", "rail_gateway", "bank_feed"):
        assert hr.get("/fin/v1/audit/evidence", caller=caller, limit=1000).status_code in (401, 403)


# --- R11-4 ------------------------------------------------------------------------------------------------------------------

def _alerts(hr):
    return [b for b in hr.svc.db["breaks"].values() if b.get("kind") == "founder_alert"]


def test_r11_4_a_funded_brand_refused_opens_one_durable_founder_alert_and_closes_on_allow(tmp_path):
    hr = Harness(data_dir=str(tmp_path / "d")).ready()
    hr.fund_campaign()
    _match(hr)
    _job(hr)
    _job(hr)
    al = _alerts(hr)
    assert len(al) == 1 and al[0]["status"] == "open" and al[0]["leg"] == "compliance_hold"
    assert al[0]["severity"] == "high" and al[0]["subject"] == "client:client-1" and "OFAC" in al[0]["note"]
    assert hr.client.get("/health").json()["compliance_holds_open"] == 1
    other = hr.get("/fin/v1/breaks", caller="onboarding").text.lower()
    assert "ofac" not in other and "sanction" not in other
    # durable: a restart keeps it (it is in the log, not in memory)
    hr2 = Harness(data_dir=str(tmp_path / "d"), ledger=hr.ledger, clock=hr.clock, fakes=hr.f)
    assert len(_alerts(hr2)) == 1
    _job(hr2)
    assert len(_alerts(hr2)) == 1
    _allow(hr2)
    _job(hr2)
    assert _alerts(hr2)[0]["status"] == "resolved"
    assert hr2.client.get("/health").json()["compliance_holds_open"] == 0


def test_r11_4_the_alert_carries_ids_only_and_no_push_is_relied_on():
    hr = Harness().ready()
    hr.fund_campaign()
    _plain(hr)
    _job(hr)
    al = _alerts(hr)[0]
    assert "OFAC" not in al["note"] and al["category"] == "compliance_hold"
    assert not hr.f["push"].pushed
    assert not hasattr(hr.svc, "_funded_alerted")


def test_r11_4_a_held_refund_is_a_durable_high_severity_break_not_a_push(tmp_path):
    from test_compliance_unapplied_r10 import _held_paid
    from test_compliance_bank_rtm import refuse
    from test_compliance_gate_adr0024 import _stripe_hr
    from test_money_redteam_rtm import charge_obj
    from test_stripe_incoming import sent
    hr = _stripe_hr(tmp_path)
    inv, pi = _held_paid(hr, refuse)
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", charge_obj(hr, pi))
    held = hr.svc.db["invoices"][inv["invoice_id"]]["held_compliance"]
    b = hr.svc.db["breaks"][held["break_id"]]
    assert b["status"] == "open" and b["severity"] == "high"
    assert not hr.f["push"].pushed


# --- R11-5 ------------------------------------------------------------------------------------------------------------------

def test_r11_5_p5_a_hold_booked_while_the_release_gathers_excludes_the_item():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.payee()
    hr.accrue()
    hr.recon()
    b = hr.run()["batch"]
    hr.approve(b)
    hr.fund_batch(b["batch_id"])
    inv2 = hr.deposit_invoice(amount="500.00")
    hr.clock.advance(hours=13)
    hr.recon()
    real = hr.svc._gather_gates

    def gather_then_hold(g, rail, payees):
        out = real(g, rail, payees)                                # Compliance said ALLOW for client-1
        _match(hr)                                                 # now a match, and its wire is booked + held
        r = hr.receive("zbc", "1020", "500.00", inv2["invoice_id"])
        assert r["results"][0]["status"] == "matched_held_compliance"
        return out
    hr.svc._gather_gates = gather_then_hold
    hr.bank.deposit("zbc", "1020", "500.00")
    hr.bank.deposit = lambda *a, **k: None
    rel = hr.ok(hr.post(f"/fin/v1/payout-batches/{b['batch_id']}/release", {"request_id": rid()}, caller="scheduler"))
    assert rel["batch"]["items"][0]["status"] == "excluded_at_release", rel
    assert "F4d" not in [e["memo_code"] for e in hr.svc.entries]


# --- the stuck approved refund ---------------------------------------------------------------------------------------------

def test_a_refund_refused_at_execution_is_cancelled_by_andre_with_a_reason():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.recon()
    rf = hr.ok(hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler"), 201)["refund"]
    real = hr.svc._execute_transfer

    def match_then_execute(tid):
        _match(hr)
        return real(tid)
    hr.svc._execute_transfer = match_then_execute
    hr.ok(hr.post(f"/fin/v1/refunds/{rf['refund_id']}/decision",
                  {"request_id": rid(), "content_sha256": rf["content_sha256"], "decision": "approve"},
                  andre=ANDRE_TOKEN))
    hr.svc._execute_transfer = real
    assert hr.svc.db["refunds"][rf["refund_id"]]["status"] == "approved"
    assert hr.bal("2050", "client:client-1") == Decimal("1000.00")
    bad = hr.post(f"/fin/v1/refunds/{rf['refund_id']}/cancel", {"request_id": rid(), "reason": ""}, andre=ANDRE_TOKEN)
    assert bad.status_code == 422
    r = hr.ok(hr.post(f"/fin/v1/refunds/{rf['refund_id']}/cancel",
                      {"request_id": rid(), "reason": "client refused at payment; deposit kept until Compliance "
                                                      "allows"}, andre=ANDRE_TOKEN))
    assert r["refund"]["status"] == "cancelled"
    assert hr.bal("2050", "client:client-1") == 0 and hr.bal("2010", "campaign:camp-1") == Decimal("1000.00")
    assert hr.post(f"/fin/v1/refunds/{rf['refund_id']}/cancel", {"request_id": rid(), "reason": "again"},
                   caller="scheduler").status_code in (401, 403)


def test_a_paid_refund_is_never_cancelled():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.recon()
    rf = hr.ok(hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler"), 201)["refund"]
    hr.ok(hr.post(f"/fin/v1/refunds/{rf['refund_id']}/decision",
                  {"request_id": rid(), "content_sha256": rf["content_sha256"], "decision": "approve"},
                  andre=ANDRE_TOKEN))
    r = hr.post(f"/fin/v1/refunds/{rf['refund_id']}/cancel", {"request_id": rid(), "reason": "too late"},
                andre=ANDRE_TOKEN)
    assert r.status_code == 409, r.text


def test_a_refund_whose_payment_outcome_is_unknown_is_never_cancelled(hr):
    from test_aegis_c0869c4 import refund_unknown
    rf, tid, _st = refund_unknown(hr)                       # approved; the bank's answer is unknown: money may have moved
    assert hr.svc.db["refunds"][rf["refund_id"]]["status"] == "approved"
    r = hr.post(f"/fin/v1/refunds/{rf['refund_id']}/cancel", {"request_id": rid(), "reason": "stuck"}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "settled first" in r.text, r.text
    assert hr.svc.db["refunds"][rf["refund_id"]]["status"] == "approved"
