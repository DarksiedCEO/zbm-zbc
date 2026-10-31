"""AEGIS r9 R9-1 (ADR 0024): every route that sends a client's money OUT asks Compliance at that moment.

A deposit refund (scheduler proposal, Andre's approval, Andre's re-payment) is refused while the client is not a
current ALLOW (refused or unreachable) or any payment of the client is held; on a sanctions ground Andre's token gets
an OFAC warning (do not return blocked property; runbook step G), other callers a generic reason. AEGIS's probe:
deposit approved while allowed -> the client becomes an OFAC match -> the bank wire is booked -> the scheduler
proposes the refund -> Andre approves: refused.
"""

from __future__ import annotations

from helpers import ANDRE_TOKEN, Harness, rid


def _match(hr, client="client-1"):
    hr.cmp.activations = {client: {"allowed": False, "jurisdiction": "US-CA", "reasons": ["hold open: sanctions_match"],
                                   "sanctions": True, "ground": "match"}}


def _allow(hr):
    hr.cmp.activation_available = True
    hr.cmp.activations = {}


def _decide(hr, rf):
    return hr.post(f"/fin/v1/refunds/{rf['refund_id']}/decision",
                   {"request_id": rid(), "content_sha256": rf["content_sha256"], "decision": "approve"},
                   andre=ANDRE_TOKEN)


def test_aegis_r9_probe_a_sanctioned_clients_deposit_is_never_refunded():
    hr = Harness().ready()
    hr.profile(doc_id=hr.rate_card())
    inv = hr.deposit_invoice()                                  # approved while ALLOW
    _match(hr)                                                  # then an OFAC match
    hr.receive("zbc", "1020", "1000.00", inv["invoice_id"])     # the wire is booked (held, RTM-01)
    hr.recon()
    r = hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler")
    assert r.status_code == 409, r.text
    assert "OFAC" not in r.text and "sanction" not in r.text.lower()       # the scheduler gets a generic reason
    assert not hr.svc.db["refunds"]


def test_a_refund_proposed_while_allowed_is_refused_at_approval_with_an_ofac_warning_for_andre():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.recon()
    rf = hr.ok(hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler"), 201)["refund"]
    _match(hr)
    r = _decide(hr, rf)
    assert r.status_code == 409 and "OFAC" in r.text and "step G" in r.text, r.text
    assert hr.svc.db["refunds"][rf["refund_id"]]["status"] == "proposed"
    assert not [t for t in hr.svc.db["treasury_ops"].values() if t["kind"] == "refund_payment"]


def test_a_refund_is_refused_while_compliance_is_unreachable_and_paid_on_allow():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.recon()
    rf = hr.ok(hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler"), 201)["refund"]
    hr.cmp.activation_available = False
    r = _decide(hr, rf)
    assert r.status_code == 409 and "compliance_38" in r.text, r.text
    _allow(hr)
    assert hr.ok(_decide(hr, rf))["refund"]["status"] == "paid"


def test_a_refund_is_refused_while_any_payment_of_the_client_is_held():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.recon()
    with hr.svc.lock:                                          # some payment of this client is held by Compliance
        some = next(iter(i for i in hr.svc.db["invoices"].values() if i["client_id"] == "client-1"))
        hr.svc.db["invoices"][some["invoice_id"]] = {**some, "held_compliance": {"kind": "unverified",
                                                                                "sanctions": False}}
    r = hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler")
    assert r.status_code == 409 and "held" in r.text, r.text
