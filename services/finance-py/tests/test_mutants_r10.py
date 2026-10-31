"""AEGIS r10 surviving mutants pinned (o4 and f2 are in test_compliance_payouts_r10 / test_compliance_unapplied_r10).

- b4: a client the bank batch did not ask Compliance about (an invoice issued between the read and the booking) is
  held unverified, never treated as ALLOW.
- f1: only a release on a current ALLOW funds a held campaign deposit (an OFAC-authorization release does not).
- h4 / h5: a Compliance break's severity and note hash never reach a non-Andre reader (breaks list, audit export),
  even after a held refund marked it high severity.
"""

from __future__ import annotations

import json

from helpers import ANDRE_TOKEN, Harness, rid
from test_compliance_bank_rtm import bank_line, refuse
from test_compliance_gate_adr0024 import _stripe_hr
from test_money_redteam_rtm import charge_obj
from test_stripe_incoming import checkout, rr_invoice, sent


def test_b4_a_client_not_asked_before_the_booking_is_held_unverified(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr, "1200.00", methods=("ach", "wire"))
    race_id = "fin-inv-RACE0000000000000000000000"
    svc = hr.svc
    real_idem = svc._idem
    calls = {"n": 0}

    def idem(principal, request_id, route, body):
        out = real_idem(principal, request_id, route, body)
        if route == "bank/events":
            calls["n"] += 1
            if calls["n"] == 2:                  # under the booking lock: an invoice issued since the Compliance read
                svc.db["invoices"][race_id] = {**svc.db["invoices"][inv["invoice_id"]], "invoice_id": race_id,
                                               "client_id": "zbm-client-race"}
        return out
    svc._idem = idem
    r = hr.ok(bank_line(hr, "zbm", "1010", "1200.00", race_id))
    assert r["results"][0]["status"] == "matched_held_unverified", r
    assert ("client_activation", "zbm-client-race") not in hr.cmp.calls
    assert not [c for c in svc.db["client_receipts"].values() if c["invoice_id"] == race_id]


def test_f1_an_ofac_authorization_release_does_not_fund_the_campaign():
    hr = Harness().ready()
    hr.profile(doc_id=hr.rate_card())
    inv = hr.deposit_invoice()
    hr.cmp.activations = {"client-1": {"allowed": False, "jurisdiction": "US-CA", "reasons": ["sanctions_match"],
                                       "sanctions": True, "ground": "match"}}
    hr.receive("zbc", "1020", "1000.00", inv["invoice_id"])
    assert hr.svc.db["invoices"][inv["invoice_id"]]["held_compliance"]["sanctions"] is True
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/compliance-hold/release",
                {"request_id": rid(), "basis": "ofac_authorization", "ofac_authorization_sha256": "d" * 64,
                 "ofac_document_kind": "license"}, andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text
    assert hr.svc.db["profiles"]["camp-1"]["status"] == "approved"           # released, but not funded


def _held_refund(tmp_path, sanctions: bool):
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr, "1200.00")
    cs = hr.ok(checkout(hr, inv["invoice_id"]))["checkout"]
    pi = hr.sim.pay(cs["session_id"], method="card")
    refuse(hr, sanctions=sanctions, ground="match" if sanctions else None)
    sent(hr, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", charge_obj(hr, pi))
    return hr


def _export_breaks(hr, **who):
    out = []
    cur = 0
    while cur is not None:
        page = (hr.redacted_get("/fin/v1/audit/export", cursor=cur).json() if who.get("caller")
                else hr.ok(hr.get("/fin/v1/audit/export", cursor=cur, **who)))
        for e in page["records"]:
            for r in e["records"]:
                rec = r["record"].get("rec") if isinstance(r["record"], dict) else None
                if r["record"].get("coll") == "breaks" and isinstance(rec, dict):
                    out.append(rec)
        cur = page["next_cursor"]
    return out


def test_h4_h5_no_compliance_break_severity_or_note_hash_outside_andres_token(tmp_path):
    hr = _held_refund(tmp_path, sanctions=True)
    held = [b for b in hr.svc.db["breaks"].values() if b["leg"] == "compliance_hold"]
    assert held and held[0]["severity"] == "high"                              # Andre's record is marked
    rows = hr.ok(hr.get("/fin/v1/breaks", caller="onboarding"))["breaks"]
    for b in rows:                                                              # h5: the breaks list
        if b["leg"] in ("compliance_hold", "stripe_refund"):
            assert "severity" not in b, b
    exported = _export_breaks(hr, caller="onboarding")                         # h4: the audit export
    assert exported
    for b in exported:
        if b.get("leg") in ("compliance_hold", "stripe_refund"):
            assert "severity" not in b and "note_sha256" not in b and "note" not in b, b
    mine = _export_breaks(hr, andre=ANDRE_TOKEN, caller=None)
    assert any(b.get("severity") == "high" for b in mine)


def test_h4_h5_a_sanctions_and_a_plain_held_refund_read_the_same_outside_andres_token(tmp_path):
    views = []
    for s in (True, False):
        (tmp_path / str(s)).mkdir()
        hr = _held_refund(tmp_path / str(s), sanctions=s)
        rows = hr.ok(hr.get("/fin/v1/breaks", caller="onboarding"))["breaks"]
        views.append(sorted(json.dumps(sorted(b)) + b["leg"] for b in rows))
    assert views[0] == views[1]
