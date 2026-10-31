"""AEGIS r8 (ADR 0024): the money path stays conservative. Every sanctions hold is BLOCKED by default.

1  Finance stores Compliance's sanctions ground on the hold: "match" (OFAC list match) or "place" (comprehensively
   sanctioned place or target). It is sticky, and match wins. A missing or unknown ground on a sanctions refusal is
   "match" (the strictest).
2  `ofac_rejected_return` only for ground "place", and only with the hash of OFAC's written guidance that the funds be
   rejected (document kind ofac_guidance), besides the refund entry and the 501.604 report hash. Never for "match".
4  Non-Andre callers never see sanctions, OFAC or match details: neutral event ids, generic Compliance reasons.
5  Pins: rr6 (the refund is after the payment), rr8 (the refund is this payment's), rr10 (the document kind is part of
   the idempotency key), v1e (returned_to_sender hidden).
"""

from __future__ import annotations

import json
import re

from helpers import ANDRE_TOKEN, rid
from test_compliance_gate_adr0024 import _stripe_hr
from test_compliance_hold_r4 import CLIENT, _f7r, _hold, _job, _paid, _release
from test_compliance_hold_r5 import _pay_then
from test_compliance_hold_r6 import _refuse_plain
from test_compliance_hold_r7 import REPORT, _andre_get
from test_stripe_incoming import rr_invoice, sent
from test_sweep_followup import _charge

GUIDANCE = "e" * 64                  # SHA-256 of OFAC's written guidance that the funds be rejected


def _ground(hr, ground, client=CLIENT):
    hr.f["compliance"].activation_available = True
    reason = "hold open: sanctions_match (hold-x)" if ground == "match" else "jurisdiction CU is refuse"
    hr.f["compliance"].activations = {client: {"allowed": False, "sanctions": True, "ground": ground,
                                               "reasons": [reason]}}


def _refund(hr, pi):
    hr.sim.refund(pi)
    sent(hr, "charge.refunded", _charge(hr, pi))


def _rejected(hr, inv, entry, **over):
    body = {"basis": "ofac_rejected_return", "entry_id": entry, "ofac_authorization_sha256": REPORT,
            "ofac_document_kind": "rejected_transaction_report", "ofac_guidance_sha256": GUIDANCE}
    body.update(over)
    return _release(hr, inv, **{k: v for k, v in body.items() if v is not None})


# --- 1: the ground on the hold ----------------------------------------------------------------------------------------

def test_the_hold_stores_the_sanctions_ground(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _ground(hr, "place"))
    assert _hold(hr, inv)["sanctions_ground"] == "place"
    (tmp_path / "b").mkdir()
    hr2 = _stripe_hr(tmp_path / "b")
    inv2, pi2, out2 = _pay_then(hr2, lambda: _ground(hr2, "match"))
    assert _hold(hr2, inv2)["sanctions_ground"] == "match"


def test_a_missing_ground_on_a_sanctions_refusal_is_a_match(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _ground(hr, None))
    assert _hold(hr, inv)["sanctions"] is True and _hold(hr, inv)["sanctions_ground"] == "match"


def test_match_wins_and_the_ground_is_sticky(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _ground(hr, "place"))
    _ground(hr, "match")                                      # an OFAC match recorded later
    _job(hr)
    assert _hold(hr, inv)["sanctions_ground"] == "match"
    _ground(hr, "place")
    _job(hr)
    assert _hold(hr, inv)["sanctions_ground"] == "match"      # never back to place


def test_a_non_sanctions_hold_upgraded_by_the_job_takes_the_ground(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _refuse_plain(hr))
    _ground(hr, "place")
    assert _job(hr)["holds_upgraded"] == 1
    assert _hold(hr, inv)["sanctions_ground"] == "place"


# --- 2: the rejected exit is for a place, with OFAC's written guidance --------------------------------------------------

def test_a_rejected_return_is_refused_for_a_match_even_with_every_document(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _ground(hr, "match"))
    h = _hold(hr, inv)
    _refund(hr, pi)
    r = _rejected(hr, inv, _f7r(hr, h["receipt_id"])[0]["entry_id"])
    assert r.status_code == 409 and "blocked" in r.text, r.text
    assert _hold(hr, inv) is not None


def test_a_rejected_return_for_a_place_needs_ofacs_written_guidance(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _ground(hr, "place"))
    h = _hold(hr, inv)
    _refund(hr, pi)
    entry = _f7r(hr, h["receipt_id"])[0]["entry_id"]
    r = _rejected(hr, inv, entry, ofac_guidance_sha256=None)
    assert r.status_code == 409 and "guidance" in r.text, r.text
    r = _rejected(hr, inv, entry)
    assert r.status_code == 200, r.text
    hist = hr.svc.db["invoices"][inv["invoice_id"]]["held_compliance_history"][-1]
    assert hist["ofac_guidance_sha256"] == GUIDANCE and hist["ofac_authorization_sha256"] == REPORT
    assert {d["kind"] for d in hist["documents"]} == {"rejected_transaction_report", "ofac_guidance"}


# --- 5: pins ------------------------------------------------------------------------------------------------------------

def test_rr6_a_refund_entry_before_the_payment_in_the_journal_is_refused(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _paid(hr, then="refuse")
    h = _hold(hr, inv)
    _refund(hr, pi)
    e = _f7r(hr, h["receipt_id"])[0]
    svc = hr.svc
    with svc.lock:                                   # the journal shows the refund BEFORE the payment it refunds
        svc.entries.remove(e)
        pay_at = next(i for i, x in enumerate(svc.entries) if x["entry_id"] == h["payment_entry_id"])
        svc.entries.insert(pay_at, e)
    r = _release(hr, inv, basis="refund", entry_id=e["entry_id"])
    assert r.status_code == 409 and "after the payment" in r.text, r.text


def test_rr8_a_refund_of_another_payment_does_not_release_even_when_this_one_is_refunded(tmp_path):
    hr = _stripe_hr(tmp_path)
    a, pa, _ = _paid(hr, then="refuse")
    hr.f["compliance"].activations = {}                                  # the next client pays normally ...
    b = rr_invoice(hr)
    cs = hr.ok(hr.post(f"/fin/v1/invoices/{b['invoice_id']}/stripe-checkout", {"request_id": rid()},
                       caller="onboarding"))["checkout"]
    pb = hr.sim.pay(cs["session_id"])
    _refuse_plain(hr)                                                     # ... and is held too
    sent(hr, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    _refund(hr, pa)
    _refund(hr, pb)
    hb = _hold(hr, b)
    r = _release(hr, a, basis="refund", entry_id=_f7r(hr, hb["receipt_id"])[0]["entry_id"])
    assert r.status_code == 409 and "this held payment" in r.text, r.text
    assert _hold(hr, a) is not None


def test_rr10_a_replay_with_another_document_kind_is_a_conflict(tmp_path):
    hr = _stripe_hr(tmp_path)
    inv, pi, out = _pay_then(hr, lambda: _ground(hr, "match"))
    req = rid()
    body = {"request_id": req, "basis": "ofac_authorization", "ofac_authorization_sha256": "7f" * 32,
            "ofac_document_kind": "license"}
    url = f"/fin/v1/invoices/{inv['invoice_id']}/compliance-hold/release"
    assert hr.post(url, body, andre=ANDRE_TOKEN).status_code == 200
    r = hr.post(url, {**body, "ofac_document_kind": "delisting"}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "different body" in r.text, r.text


# --- 4 and v1e: nothing about sanctions, OFAC or a match reaches a non-Andre caller -------------------------------------

LEAK = re.compile(r"sanction|ofac|holdsan|\bmatch\b|returned_to_sender|guidance", re.I)
STATIC = ("Sanctions list", "OFAC")        # static control titles and rule texts every caller may read


def _scan(text: str) -> list[str]:
    for s in STATIC:
        text = text.replace(s, "")
    return [text[max(0, m.start() - 60):m.end() + 40] for m in LEAK.finditer(text)]


def test_no_non_andre_response_names_sanctions_ofac_or_a_match(tmp_path):
    hr = _stripe_hr(tmp_path)
    # state 1: a place hold returned as rejected; state 2: a refused hold the job upgrades to a match
    inv, pi, out = _pay_then(hr, lambda: _ground(hr, "place"))
    h = _hold(hr, inv)
    _refund(hr, pi)
    hr.ok(_rejected(hr, inv, _f7r(hr, h["receipt_id"])[0]["entry_id"]))
    hr.f["compliance"].activations = {}
    inv2 = rr_invoice(hr, "300.00")
    cs = hr.ok(hr.post(f"/fin/v1/invoices/{inv2['invoice_id']}/stripe-checkout", {"request_id": rid()},
                       caller="onboarding"))["checkout"]
    hr.sim.pay(cs["session_id"])
    _refuse_plain(hr)
    sent(hr, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    hr.f["compliance"].activations = {}
    inv3 = rr_invoice(hr, "200.00")                       # issued while the client was still allowed
    _ground(hr, "match")
    assert _job(hr)["holds_upgraded"] == 1
    # what onboarding (and every general caller) can read now
    reads = [hr.get("/fin/v1/audit/export", caller="onboarding"),
             hr.get("/fin/v1/audit/evidence", caller="onboarding"),
             hr.get(f"/fin/v1/invoices/{inv['invoice_id']}", caller="onboarding"),
             hr.get(f"/fin/v1/invoices/{inv2['invoice_id']}", caller="onboarding"),
             hr.get("/fin/v1/breaks", caller="onboarding"),
             hr.get(f"/fin/v1/clients/{CLIENT}/billing-readiness", caller="onboarding"),
             hr.post(f"/fin/v1/invoices/{inv3['invoice_id']}/stripe-checkout", {"request_id": rid()},
                     caller="onboarding"),
             hr.get(f"/fin/v1/invoices/{inv2['invoice_id']}/status", caller="seo_02")]
    for r in reads:
        assert r.status_code in (200, 409), (r.request.url, r.status_code, r.text[:200])
        hits = _scan(r.text)
        assert not hits, (str(r.request.url), hits[:3])
    # page through the whole export
    cur = 0
    while cur is not None:
        page = hr.get("/fin/v1/audit/export", caller="onboarding", cursor=cur).json()
        assert not _scan(json.dumps(page)), _scan(json.dumps(page))[:3]
        cur = page["next_cursor"]
    # Andre's token sees it all
    full = _andre_get(hr, "/fin/v1/audit/export").text
    assert "returned_to_sender" in full and "sanctions_ground" in full
