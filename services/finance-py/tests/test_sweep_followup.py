"""Regression tests for the AEGIS conditions on fix-finance 5a56a3a (ADR 0009 "Sweep follow-up -- AEGIS conditions of
5a56a3a: fixed"). Each test fails on 5a56a3a and passes after the fix.

  M1  store.py ignored a short pwrite: a partial line stayed on disk
  M2  retry_transfers could ask the bank twice: no age limit, no in-flight guard
  M3  _drive_item resubmitted on Trolley an item whose ``paid`` / ``failed`` rail event was held
  M4  replay_held_rail_events scanned every rail event ever received, under the service lock
  L1  the ghost-ruling exemption ignored ledger position
  L2  Stripe refunds: an unapplied receipt refunded in full, a partial refund, a refund on a charged-back receipt
"""

from __future__ import annotations

import os
import threading
from decimal import Decimal

import pytest

import fakes
from helpers import ANDRE_TOKEN, Harness, rid
from intelligences import i01_journal as J
from intelligences import i10_evidence_audit as i10
from ports import BankTransfer, RailLookup
from test_cert_attacks import _trolley
from test_stripe_incoming import CANCEL, SUCCESS, _secret, bal, checkout, make, memos, paid_rr, rr_invoice, sent
from stripe_sim import TEST_KEY, TEST_WHSEC
from test_sweep_fixes import is_f4d, payout_ready, spy


@pytest.fixture
def skeys(tmp_path):
    return {"FIN_STRIPE_INCOMING": "1", "FIN_STRIPE_SECRET_KEY_FILE": _secret(tmp_path, "sk", TEST_KEY),
            "FIN_STRIPE_WEBHOOK_SECRET_FILE": _secret(tmp_path, "wh", TEST_WHSEC),
            "FIN_STRIPE_SUCCESS_URL": SUCCESS, "FIN_STRIPE_CANCEL_URL": CANCEL}


def balanced(h):
    for e in h.svc.entries:
        d, c = J.totals(e)
        assert d == c, e["entry_id"]
    for ent in ("zbc", "zbm"):
        assert h.ok(h.get(f"/fin/v1/journal/{ent}/trial-balance"))["difference"] == "0.00"


# --------------------------------------------------------------------------------------------- M1

def test_m1_a_short_write_is_cut_back_and_refused(tmp_path, monkeypatch):
    import store as store_mod
    d = str(tmp_path / "fin")
    h = Harness(data_dir=d).ready()
    path = os.path.join(d, store_mod.LOG_NAME)
    before = os.path.getsize(path)
    lines_before = len(h.svc.log)
    real = os.pwrite

    def short(fd, data, offset):
        return real(fd, data[: len(data) // 2], offset)       # the disk took half the line
    monkeypatch.setattr(store_mod.os, "pwrite", short)
    r = h.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "clip-s", "kind": "clipper",
                                  "declared_country": "US", "callback_contact_ref": "vault:contact-clip-s"},
               caller="clipper_network")
    assert r.status_code == 503, r.text
    assert os.path.getsize(path) == before                   # no partial line left on disk
    assert len(h.svc.log) == lines_before and h.svc.log.verify()
    monkeypatch.setattr(store_mod.os, "pwrite", real)
    h.payee("clip-t")                                         # the log is not bricked
    assert h.svc.log.verify()
    with open(path, "rb") as fh:
        raw = fh.read()
    assert raw.endswith(b"\n") and raw.count(b"\n") == len(h.svc.log)


def test_m1_store_unit_short_write_raises_store_write_error(tmp_path, monkeypatch):
    import store as store_mod
    log = store_mod.RecordLog(str(tmp_path))
    log.append("x", "2026-10-07T00:00:00Z", {"a": 1})
    size = os.path.getsize(log.path)
    monkeypatch.setattr(store_mod.os, "pwrite", lambda fd, data, off: os.write(fd, b"") or 7)
    try:
        log.append("x", "2026-10-07T00:00:01Z", {"a": 2})
    except store_mod.StoreWriteError as exc:
        assert "short write" in str(exc)
    else:
        raise AssertionError("a short write must raise StoreWriteError")
    assert os.path.getsize(log.path) == size and len(log) == 1 and log.verify()


# --------------------------------------------------------------------------------------------- M2

def _sweep(h, amount="100.00"):
    h.fund_campaign(budget="1000.00")
    h.payee("clip-a")
    h.accrue("sub-1", views=250000)
    h.recon()
    op = h.ok(h.post("/fin/v1/treasury/sweeps", {"request_id": rid(), "amount": amount}, caller="scheduler"),
              201)["operation"]
    return op


def _decide(h, op):
    return h.post(f"/fin/v1/treasury/sweeps/{op['op_id']}/decision",
                  {"request_id": rid(), "content_sha256": op["content_sha256"], "decision": "approve"},
                  andre=ANDRE_TOKEN)


def _counting_bank(h):
    calls = []
    real = h.bank.transfer

    def transfer(*a, **k):
        calls.append(a)
        return real(*a, **k)
    h.bank.transfer = transfer
    return calls, real


def test_m2_unknown_transfer_past_the_window_is_never_asked_again(hr):
    op = _sweep(hr)

    def boom(*a, **k):
        raise RuntimeError("timeout after send")
    hr.bank.transfer = boom
    assert hr.ok(_decide(hr, op))["operation"]["status"] == "bank_unknown"
    calls, _ = _counting_bank(hr)
    hr.clock.advance(hours=24)                                 # past RETRY_WINDOW_H (23 h)
    hr.ok(hr.post("/fin/v1/jobs/rail-sync/run", {"request_id": rid()}, caller="scheduler"))
    assert calls == []                                         # the bank is never asked past the window
    t = hr.svc.db["treasury_ops"][op["op_id"]]
    assert t["status"] == "bank_unknown" and t["retry_window_expired"] is True
    brk = [b for b in hr.svc.db["breaks"].values() if b.get("kind") == "bank_retry_window_expired"]
    assert len(brk) == 1 and brk[0]["owner"] == "andre" and brk[0]["difference"] == "100.00"
    assert hr.ledger.of_type("break_opened")
    hr.clock.advance(hours=24)
    hr.ok(hr.post("/fin/v1/jobs/rail-sync/run", {"request_id": rid()}, caller="scheduler"))
    assert calls == [] and len([b for b in hr.svc.db["breaks"].values()
                                if b.get("kind") == "bank_retry_window_expired"]) == 1
    assert len([e for e in hr.svc.entries if e["memo_code"] == "F8"]) == 1          # the posting is kept, once


def test_m2_within_the_window_the_same_key_is_asked_again(hr):
    op = _sweep(hr)
    real = hr.bank.transfer

    def boom(*a, **k):
        raise RuntimeError("timeout after send")
    hr.bank.transfer = boom
    hr.ok(_decide(hr, op))
    hr.bank.transfer = real
    calls, _ = _counting_bank(hr)
    hr.clock.advance(hours=22)
    hr.ok(hr.post("/fin/v1/jobs/rail-sync/run", {"request_id": rid()}, caller="scheduler"))
    assert len(calls) == 1 and calls[0][4] == op["op_id"]
    assert hr.svc.db["treasury_ops"][op["op_id"]]["status"] == "done"


def test_m2_concurrent_retry_runs_never_ask_the_bank_twice(hr):
    """Two rail-sync retries race the request path's own bank call: the bank is asked exactly once."""
    op = _sweep(hr)
    real = hr.bank.transfer
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow(*a, **k):
        calls.append(a)
        entered.set()
        assert release.wait(10)
        return real(*a, **k)
    hr.bank.transfer = slow
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("decide", _decide(hr, op)))
    t.start()
    assert entered.wait(10)                                   # the request path is inside the bank call
    assert hr.svc.db["treasury_ops"][op["op_id"]]["status"] == "executing"
    racers = [threading.Thread(target=lambda i=i: out.setdefault(f"r{i}", hr.svc.retry_transfers()))
              for i in range(2)]
    for r in racers:
        r.start()
    for r in racers:
        r.join(10)
    assert not any(r.is_alive() for r in racers)
    assert out["r0"] == 0 and out["r1"] == 0                  # skipped: the call is in flight
    release.set()
    t.join(10)
    assert out["decide"].status_code == 200, out["decide"].text
    assert len(calls) == 1
    assert hr.svc.db["treasury_ops"][op["op_id"]]["status"] == "done"
    assert op["op_id"] not in hr.svc.xfer_in_flight
    assert hr.svc.retry_transfers() == 0 and len(calls) == 1


def test_m2_in_flight_guard_is_released_when_the_bank_call_raises(hr):
    op = _sweep(hr)
    hr.bank.transfer = lambda *a, **k: BankTransfer("refused")
    hr.ok(_decide(hr, op))
    assert hr.svc.db["treasury_ops"][op["op_id"]]["status"] == "approved"
    assert op["op_id"] not in hr.svc.xfer_in_flight


# --------------------------------------------------------------------------------------------- M3

def _stuck_trolley_item(hr):
    """A Trolley item the rail accepted whose F4d booking cannot be recorded: it stays ``submitting``."""
    b = _trolley(hr)
    tr = hr.f["rails"]["trolley"]
    down = {"on": True}
    spy(hr, lambda et, p: down["on"] and is_f4d(et, p))
    hr.release(b["batch_id"])
    iid = b["item_ids"][0]
    assert hr.svc.db["items"][iid]["status"] == "submitting"
    return b, iid, tr, down


def _not_found(tr):
    def lookup(key, item_id):
        tr.calls.append(("lookup", key, item_id))
        return RailLookup(True, False)                      # Trolley's lookup does not see it (idempotency UNVERIFIED)
    tr.lookup = lookup


def _held(hr, iid, etype):
    ev = {"event_id": rid("evt"), "type": etype, "signature": fakes.RAIL_SIG, "item_id": iid}
    r = hr.ok(hr.post("/fin/v1/rails/trolley/events", {"request_id": rid(), "events": [ev]}, caller="rail_gateway"))
    assert r["results"][0]["status"] == "held_submitting"


def test_m3_a_held_paid_event_is_acceptance_never_a_resubmit(hr):
    b, iid, tr, down = _stuck_trolley_item(hr)
    it = hr.svc.db["items"][iid]
    tr.paid(it["idempotency_key"])
    _held(hr, iid, "paid")
    _not_found(tr)
    down["on"] = False
    submits = len([c for c in tr.calls if c[0] == "submit"])
    hr.ok(hr.post("/fin/v1/jobs/rail-sync/run", {"request_id": rid()}, caller="scheduler"))
    assert len([c for c in tr.calls if c[0] == "submit"]) == submits          # never sent to the rail again
    assert len(tr.payouts) == 1
    assert hr.svc.db["items"][iid]["status"] == "paid"
    flows = [e["memo_code"] for e in hr.svc.entries if e["source"].get("id") == iid]
    assert flows.count("F4d") == 1 and flows.count("F4e") == 1
    assert hr.bal("2030", f"item:{iid}") == 0
    assert hr.ledger.of_type("item_accepted_from_rail_event")
    balanced(hr)


def test_m3_a_held_failed_event_is_a_break_never_a_resubmit(hr):
    b, iid, tr, down = _stuck_trolley_item(hr)
    _held(hr, iid, "failed")
    _not_found(tr)
    down["on"] = False
    submits = len([c for c in tr.calls if c[0] == "submit"])
    for _ in range(2):
        hr.ok(hr.post("/fin/v1/jobs/rail-sync/run", {"request_id": rid()}, caller="scheduler"))
    assert len([c for c in tr.calls if c[0] == "submit"]) == submits
    assert hr.svc.db["items"][iid]["status"] == "submitting"
    brk = [x for x in hr.svc.db["breaks"].values() if x.get("kind") == "rail_failed_unconfirmed"]
    assert len(brk) == 1 and brk[0]["owner"] == "andre" and brk[0]["lookup"] == "not_found"


def test_m3_a_held_failed_event_the_rail_confirms_books_the_failure(hr):
    b, iid, tr, down = _stuck_trolley_item(hr)
    _held(hr, iid, "failed")
    down["on"] = False
    submits = len([c for c in tr.calls if c[0] == "submit"])
    hr.ok(hr.post("/fin/v1/jobs/rail-sync/run", {"request_id": rid()}, caller="scheduler"))
    assert len([c for c in tr.calls if c[0] == "submit"]) == submits
    assert hr.svc.db["items"][iid]["status"] == "failed"
    flows = [e["memo_code"] for e in hr.svc.entries if e["source"].get("id") == iid]
    assert flows.count("F4d") == 1 and flows.count("F4f") == 1
    balanced(hr)


# --------------------------------------------------------------------------------------------- M4

class NoScanDict(dict):
    """``db["rail_events"]`` that counts keyed reads and refuses a scan."""

    def __init__(self, *a):
        super().__init__(*a)
        self.reads = 0

    def __getitem__(self, k):
        self.reads += 1
        return super().__getitem__(k)

    def get(self, k, default=None):
        self.reads += 1
        return super().get(k, default)

    def _scan(self, *a, **k):
        raise AssertionError("rail_events scanned under the lock")
    items = values = keys = __iter__ = _scan


def test_m4_held_events_are_found_by_index_not_by_scanning(tmp_path):
    d = str(tmp_path / "fin")
    h = Harness(data_dir=d).ready()
    b = payout_ready(h)
    iid = b["item_ids"][0]
    down = {"on": True}
    spy(h, lambda et, p: down["on"] and is_f4d(et, p))
    h.release(b["batch_id"])
    h.stripe.paid(h.svc.db["items"][iid]["idempotency_key"])
    ev = {"event_id": "evt-m4", "type": "paid", "signature": fakes.RAIL_SIG, "item_id": iid}
    h.ok(h.post("/fin/v1/rails/stripe/events", {"request_id": rid(), "events": [ev]}, caller="rail_gateway"))
    assert h.svc.held_by_item == {iid: {"stripe|evt-m4"}}
    h2 = Harness(data_dir=d, ledger=h.ledger, clock=h.clock, fakes=h.f)           # rebuilt on replay
    assert h2.svc.held_by_item == {iid: {"stripe|evt-m4"}}
    noisy = NoScanDict(h2.svc.db["rail_events"])
    for n in range(5000):                                     # a long history of settled events
        dict.__setitem__(noisy, f"stripe|old-{n}", {"event_id": f"old-{n}", "rail": "stripe", "type": "paid",
                                                    "item_id": f"fin-itm-old{n}", "status": "paid", "held": False})
    h2.svc.db["rail_events"] = noisy
    down["on"] = False
    h2.svc.drive_open_items("m4")
    assert h2.svc.db["items"][iid]["status"] == "paid"
    assert noisy.reads <= 10, noisy.reads                     # O(held events), independent of the 5000
    assert h2.svc.held_by_item == {}
    noisy.reads = 0
    assert h2.svc.replay_held_rail_events("m4b") == 0 and noisy.reads == 0


# --------------------------------------------------------------------------------------------- L1

EPOCH = "ab" * 8


def _anchor(seq, sha):
    return {"event_id": i10.anchor_id(EPOCH, seq, sha), "event_type": i10.ANCHOR_TYPE, "department": "finance",
            "payload_sha256": "0" * 64}


def _ruling(rk, et, psha):
    return {"event_id": i10.evidence_id(rk, et, psha), "event_type": et, "department": "finance", "payload_sha256": psha}


def _assess(entries, actions):
    lines = [(1, "1" * 64, True), (2, "2" * 64, True)]
    return i10.assess(entries, EPOCH, lines, set(), 0, strict=False, committed_actions=actions)


RK, ET, PSHA = "fin-je-" + "c" * 40, "journal_entry_posted", "d" * 64


def test_l1_an_attempt_recorded_before_its_committing_anchor_is_not_a_ghost():
    entries = [_anchor(1, "1" * 64), _ruling(RK, ET, PSHA), _anchor(2, "2" * 64)]
    a = _assess(entries, {(RK, ET, 2)})
    assert not a.voidable and not a.void_event_ids


def test_l1_a_matching_ruling_after_the_committing_anchor_is_still_a_ghost():
    """Negative: the same (rk, type, payload) recorded AFTER the line that committed the action is a second effect."""
    entries = [_anchor(1, "1" * 64), _anchor(2, "2" * 64), _ruling(RK, ET, PSHA)]
    a = _assess(entries, {(RK, ET, 2)})
    assert a.voidable and i10.evidence_id(RK, ET, PSHA) in a.void_event_ids


def test_l1_an_action_without_a_position_or_an_anchor_exempts_nothing():
    entries = [_anchor(1, "1" * 64), _ruling(RK, ET, PSHA), _anchor(2, "2" * 64)]
    for actions in ({(RK, ET)}, {(RK, ET, 3)}, {(RK, "payable_accrued", 2)}):
        a = _assess(entries, actions)
        assert i10.evidence_id(RK, ET, PSHA) in a.void_event_ids, actions


# --------------------------------------------------------------------------------------------- L2

def _charge(st, pi):
    return {"id": st.sim.pis[pi]["latest_charge"], "object": "charge", "payment_intent": pi}


def _rct(st, pi):
    return next(r for r in st.svc.db["receipts"].values() if (r.get("stripe") or {}).get("payment_intent") == pi)


def test_l2_unapplied_receipt_partly_then_fully_refunded(skeys):
    st = make(skeys, FIN_CARD_PREPAYMENTS="1")
    inv = rr_invoice(st, "1200.00")
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    pi = st.sim.pay(cs["session_id"], amount=100000)                    # 1000.00: unapplied
    assert sent(st, "checkout.session.completed", {"id": cs["session_id"]})["status"] == "unapplied"
    st.sim.refund(pi, 40000)
    assert sent(st, "charge.refunded", _charge(st, pi))["status"] == "refund_booked"
    assert _rct(st, pi)["status"] == "unapplied" and bal(st, "2070") == Decimal("600.00")
    st.sim.refund(pi)
    assert sent(st, "charge.refunded", _charge(st, pi))["status"] == "refund_booked"
    rc = _rct(st, pi)
    assert rc["status"] == "refunded" and rc["stripe"]["refunded"] == "1000.00"
    assert bal(st, "2070") == 0 and memos(st).count("F7r") == 2
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    balanced(st)


def test_l2_partial_refund_of_a_matched_payment_has_its_own_status(skeys):
    st = make(skeys, FIN_CARD_PREPAYMENTS="1")
    inv, cs, pi, _ = paid_rr(st, "1200.00")
    st.sim.refund(pi, 30000)
    sent(st, "charge.refunded", _charge(st, pi))
    rc = _rct(st, pi)
    assert rc["status"] == "partially_refunded" and rc["stripe"]["refunded"] == "300.00"
    i = st.svc.db["invoices"][inv["invoice_id"]]
    assert i["status"] == "paid" and i["refunded"] == "300.00"
    assert bal(st, "1100", "client:zbm-client-7") == Decimal("300.00")
    # the client receipt states the full amount paid: withdrawn, never sent
    crs = [c for c in st.svc.db["client_receipts"].values() if c["receipt_id"] == rc["receipt_id"]]
    assert crs and all(c["status"] in ("withdrawn", "withdrawn_after_send") for c in crs)
    st.sim.refund(pi)                                                   # the rest
    sent(st, "charge.refunded", _charge(st, pi))
    assert _rct(st, pi)["status"] == "refunded"
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    assert bal(st, "1100", "client:zbm-client-7") == Decimal("1200.00")
    balanced(st)


def test_l2_a_payment_failure_after_a_partial_refund_is_booked_against_the_client(skeys):
    st = make(skeys, FIN_CARD_PREPAYMENTS="1")
    inv, cs, pi, _ = paid_rr(st, "1200.00")
    st.sim.refund(pi, 20000)
    sent(st, "charge.refunded", _charge(st, pi))
    st.sim.fail_after_success(pi)
    assert sent(st, "charge.failed", _charge(st, pi))["status"] == "payment_failed_after_success"
    assert _rct(st, pi)["status"] == "returned" and bal(st, "2070") == 0
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    balanced(st)


def test_l2_refund_on_a_charged_back_receipt_is_booked_not_refused(skeys):
    st = make(skeys, FIN_CARD_PREPAYMENTS="1")
    inv, cs, pi, _ = paid_rr(st, "1000.00", method="card")
    du = st.sim.dispute(pi)
    sent(st, "charge.dispute.funds_withdrawn", {"id": du})
    st.sim.close_dispute(du, won=False)
    sent(st, "charge.dispute.closed", {"id": du})
    assert _rct(st, pi)["status"] == "charged_back"
    st.sim.refund(pi, 5000)                                            # $50 refunded at Stripe on top
    assert sent(st, "charge.refunded", _charge(st, pi))["status"] == "refund_booked"
    rc = _rct(st, pi)
    assert rc["status"] == "charged_back" and rc["stripe"]["refunded"] == "50.00"
    assert bal(st, "1100", "client:zbm-client-7") == Decimal("1050.00")
    assert any(b["leg"] == "stripe_refund" and b["status"] == "open" for b in st.svc.db["breaks"].values())
    assert sent(st, "charge.refunded", _charge(st, pi))["status"] == "payment_already_booked"
    assert memos(st).count("F7r") == 1
    balanced(st)
