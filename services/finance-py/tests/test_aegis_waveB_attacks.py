"""AEGIS wave B attack tests kept as regressions (scratchpad/night/aegis-waveB-evidence/test_aegis_attack.py: the race,
the refund that pays a credit note and leaves a remainder, and the replayed request id). The restore repros are in
test_restore_rebuild.py, as assertions."""
from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal

import pytest

from helpers import ANDRE_TOKEN, rid
from intelligences import i01_journal as J
from test_credit_notes import CLIENT, ar, balanced, credit, dashboard_refund, owed_back, sim_1060, st, stripe_paid  # noqa: F401

from test_stripe_incoming import checkout, keys, rr_invoice, sent  # noqa: F401

def test_race_two_credit_notes_cannot_promise_back_more_than_paid(st):
    inv, pi = stripe_paid(st, "1200.00")
    barrier = threading.Barrier(2, timeout=10)
    real = st.cmp.client_activation

    def slow(cid):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return real(cid)
    st.cmp.client_activation = slow
    out = []

    def go():
        try:
            out.append(("ok", st.svc.issue_credit_note(rid(), inv["invoice_id"],
                                                       {"request_id": "x", "content_sha256": inv["content_sha256"],
                                                        "amount": "1000.00", "reason_code": "other",
                                                        "refund_due_from": date(2026, 10, 2)})))
        except Exception as exc:  # noqa: BLE001
            out.append(("err", type(exc).__name__))
    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    print("RACE", [o[0] if o[0] == "ok" else o for o in out])
    assert sorted(o[0] for o in out) == ["err", "ok"]
    assert owed_back(st) == Decimal("1000.00")
    balanced(st)

def test_full_refund_pays_pending_credit_note_and_the_rest_is_absorbed_by_a_second(st):
    inv, pi = stripe_paid(st, "1200.00")
    cn1 = st.ok(credit(st, inv, "400.00", reason="nonconforming"), 201)["credit_note"]
    dashboard_refund(st, pi)                                  # Andre refunds everything at Stripe
    i = st.svc.db["invoices"][inv["invoice_id"]]
    print("AFTER FULL REFUND", i["status"], i["credited"], ar(st), owed_back(st))
    assert st.svc.db["credit_notes"][cn1["credit_note_id"]]["status"] == "refunded"
    assert owed_back(st) == 0 and ar(st) == Decimal("800.00")
    cn2 = st.ok(credit(st, i, "800.00", reason="other"), 201)["credit_note"]
    assert (cn2["to_ar"], cn2["to_refund"]) == ("800.00", "0.00")
    assert ar(st) == 0 and owed_back(st) == 0
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "credited"
    assert credit(st, st.svc.db["invoices"][inv["invoice_id"]], "0.01").status_code == 409
    assert st.bal("1060", None, "zbm") == sim_1060(st)
    balanced(st)

def test_same_request_id_replayed_is_one_credit_note(st):
    inv, pi = stripe_paid(st, "1200.00")
    body = {"request_id": rid(), "content_sha256": inv["content_sha256"], "amount": "100.00", "reason_code": "other",
            "refund_due_from": "2026-10-02"}                         # AEGIS F4 (required since)
    a = st.post(f"/fin/v1/invoices/{inv['invoice_id']}/credit-notes", body, andre=ANDRE_TOKEN)
    b = st.post(f"/fin/v1/invoices/{inv['invoice_id']}/credit-notes", body, andre=ANDRE_TOKEN)
    assert a.status_code == 201 and b.status_code in (200, 201) and a.json()["credit_note"] == b.json()["credit_note"]
    assert len(st.svc.db["credit_notes"]) == 1 and owed_back(st) == Decimal("100.00")

