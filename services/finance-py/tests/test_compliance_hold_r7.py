"""AEGIS r7 (ADR 0024): the sanctions marker, the audit routes and the OFAC exits.

V1     GET /fin/v1/audit/export and /fin/v1/audit/evidence show a Compliance hold generically to every caller but
       Andre's token: no sanctions marker, no sanctions_since, no OFAC release details, no compliance_hold_sanctions
       event type.
n3cv   Converting an unverified hold that already carries the sanctions marker keeps it.
Exits  `delisting` is its own OFAC document kind; a place-based payment that is REJECTED (not blocked) leaves through
       `ofac_rejected_return`: the payment was returned to the sender (a refund of this payment, after it) and the
       filed 501.604 rejected-transaction report is hashed.
"""

from __future__ import annotations

import json

from helpers import ANDRE_TOKEN
from test_compliance_gate_adr0024 import _stripe_hr
from test_compliance_hold_r4 import _allow, _f7r, _hold, _job, _paid, _release
from test_compliance_hold_r5 import AUTH, _pay_then, _sanctioned
from test_compliance_hold_r6 import _refuse_plain, _second_hold
from test_stripe_incoming import sent
from test_sweep_followup import _charge

REPORT = "d" * 64                   # SHA-256 of the filed 501.604 report confirmation


def _andre_get(hr, path, **params):
    return hr.client.get(path, params=params, headers={**hr.headers(None), "X-Andre-Approval-Token": ANDRE_TOKEN})


def _upgraded(hr):
    """A refused hold that the hourly job upgraded to sanctions (so a compliance_hold_sanctions event exists)."""
    inv, pi, out = _pay_then(hr, lambda: _refuse_plain(hr))
    _sanctioned(hr)
    assert _job(hr)["holds_upgraded"] == 1
    return inv, pi


# --- V1 -----------------------------------------------------------------------------------------------------------------

def test_the_audit_export_hides_the_sanctions_marker_from_other_callers(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi = _upgraded(hr)
    r = hr.get("/fin/v1/audit/export", caller="onboarding")
    assert r.status_code == 200, r.text
    text = json.dumps(r.json())
    for word in ("sanctions", "sanctions_since", "ofac", '"held_compliance":', '"held_compliance_history":'):
        assert word not in text.lower(), word
    assert '"held": true' in text                                      # the hold itself is still visible
    full = json.dumps(_andre_get(hr, "/fin/v1/audit/export").json())
    assert '"sanctions": true' in full and "sanctions_since" in full


def test_the_audit_evidence_hides_the_sanctions_event_from_other_callers(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi = _upgraded(hr)
    r = hr.get("/fin/v1/audit/evidence", caller="onboarding")
    assert r.status_code == 200, r.text
    assert "compliance_hold_sanctions" not in r.text and "ofac" not in r.text.lower()
    assert any(e["event_type"] == "compliance_hold_event" for e in r.json()["events"])
    r = hr.get("/fin/v1/audit/evidence", caller="onboarding", event_type="compliance_hold_sanctions")
    assert r.status_code == 200 and r.json()["total"] == 0          # the filter cannot probe for it either
    full = _andre_get(hr, "/fin/v1/audit/evidence")
    assert full.status_code == 200 and "compliance_hold_sanctions" in full.text


def test_an_ofac_release_is_generic_in_the_export_for_other_callers(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _sanctioned(hr))
    doc = "7f" * 32                  # a hash no fixture uses, so finding it means it leaked
    hr.ok(_release(hr, inv, basis="ofac_authorization", ofac_authorization_sha256=doc, ofac_document_kind="license"))
    text = json.dumps(hr.get("/fin/v1/audit/export", caller="onboarding").json()).lower()
    assert "ofac" not in text and "sanctions" not in text and doc not in text
    assert doc in json.dumps(_andre_get(hr, "/fin/v1/audit/export").json())


# --- n3cv ---------------------------------------------------------------------------------------------------------------

def test_converting_an_unverified_hold_keeps_its_sanctions_marker(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _sanctioned(hr))
    first, second = _second_hold(hr, inv, kind="unverified", sanctions=False)
    assert second["kind"] == "unverified" and second["sanctions"] is True
    _refuse_plain(hr)                                       # Compliance now says a plain (non-sanctions) REFUSE
    assert _job(hr)["holds_converted"] == 1
    h = _hold(hr, inv)
    assert h["kind"] == "refused" and h["sanctions"] is True
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", _charge(hr, pi))
    r = _release(hr, inv, basis="refund", entry_id=_f7r(hr, first["receipt_id"])[0]["entry_id"])
    assert r.status_code == 409


# --- the OFAC exits -----------------------------------------------------------------------------------------------------

def test_a_delisting_is_its_own_document_kind(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _sanctioned(hr))
    r = _release(hr, inv, basis="ofac_authorization", ofac_authorization_sha256=AUTH, ofac_document_kind="delisting")
    assert r.status_code == 200, r.text
    assert hr.svc.db["invoices"][inv["invoice_id"]]["held_compliance_history"][-1]["ofac_document_kind"] == "delisting"


def test_a_rejected_payment_returned_to_the_sender_leaves_with_its_501_604_report(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _sanctioned(hr))
    h = _hold(hr, inv)
    r = _release(hr, inv, basis="ofac_rejected_return", ofac_authorization_sha256=REPORT,
                 ofac_document_kind="rejected_transaction_report")
    assert r.status_code == 409 and "refund" in r.text, r.text              # not returned yet
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", _charge(hr, pi))
    entry = _f7r(hr, h["receipt_id"])[0]["entry_id"]
    r = _release(hr, inv, basis="ofac_rejected_return", entry_id=entry, ofac_authorization_sha256=REPORT,
                 ofac_document_kind="license")
    assert r.status_code == 409                                              # the document must be the 501.604 report
    r = _release(hr, inv, basis="ofac_rejected_return", entry_id=entry, ofac_document_kind="rejected_transaction_report")
    assert r.status_code == 409                                              # and its hash is required
    r = _release(hr, inv, basis="ofac_rejected_return", entry_id=entry, ofac_authorization_sha256=REPORT,
                 ofac_document_kind="rejected_transaction_report")
    assert r.status_code == 200, r.text
    hist = hr.svc.db["invoices"][inv["invoice_id"]]["held_compliance_history"][-1]
    assert hist["basis"] == "ofac_rejected_return" and hist["returned_to_sender"] is True
    assert hist["entry_id"] == entry and hist["ofac_authorization_sha256"] == REPORT
    assert _hold(hr, inv) is None


def test_the_rejected_return_ground_is_for_sanctions_holds_only(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _paid(hr, then="refuse")
    h = _hold(hr, inv)
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", _charge(hr, pi))
    r = _release(hr, inv, basis="ofac_rejected_return", entry_id=_f7r(hr, h["receipt_id"])[0]["entry_id"],
                 ofac_authorization_sha256=REPORT, ofac_document_kind="rejected_transaction_report")
    assert r.status_code == 409 and _hold(hr, inv) is not None


def test_the_rejected_transaction_report_does_not_release_a_blocked_hold_without_a_return(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _sanctioned(hr))
    _allow(hr)
    r = _release(hr, inv, basis="ofac_authorization", ofac_authorization_sha256=REPORT,
                 ofac_document_kind="rejected_transaction_report")
    assert r.status_code == 409 and _hold(hr, inv) is not None              # a 501.604 report is the rejected exit
