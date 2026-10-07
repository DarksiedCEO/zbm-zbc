"""Regression tests for the Oct 6 2026 backend bug sweep (project doc ``bug-sweep-2026-10-06``), finance-py findings.

Each test is one of the sweep's reproducing probes (``scratchpad/sweep-B/fin_*.py``, ``x_settings_repr.py``) turned
into an assertion, plus the store / lock / verify items of the same fix. Every test here FAILS on integration
5d49ee9 and passes after the fix (ADR 0009 amendment "bug sweep Oct 6 2026").

  B-F1  bank receipt whose anchor failed is permanently unbookable (time in the journal evidence payload)
  B-F2  a payout Stripe accepted is never booked: permanent 409, paid webhook acked-and-ignored, one stuck item blocks
        the others, a 503 says took_effect=false after money moved
  B-F3  Stripe checkout payment whose anchor failed is never booked
  B-F4  a top-up replayed after a restart re-creates the treasury op: the bank moves $400, the journal nets to $0
  X-6   a Stripe Dashboard refund (charge.refunded) is acked as already booked
  X-8   repr(Settings) prints every bearer token
  X-10  release_locks grows without bound
  F-3/E-5  one fsync error bricks the log; no single-writer lock; no inert close()
  verify() of the ledger chain runs under the service lock
"""

from __future__ import annotations

import hashlib
import os
import threading
from decimal import Decimal

import pytest

import config as config_mod
import fakes
from helpers import ANDRE_TOKEN, Harness, rid
from intelligences import i01_journal as J
from ledger import derived_id
from stripe_sim import TEST_KEY, TEST_WHSEC
from test_media_billing import pay_vendor
from test_stripe_incoming import CANCEL, SUCCESS, _secret, checkout, make, media_paid_by_stripe, memos, rr_invoice, \
    send, sent
from test_stripe_incoming import bal as zbm_bal


@pytest.fixture
def skeys(tmp_path):
    return {"FIN_STRIPE_INCOMING": "1", "FIN_STRIPE_SECRET_KEY_FILE": _secret(tmp_path, "sk", TEST_KEY),
            "FIN_STRIPE_WEBHOOK_SECRET_FILE": _secret(tmp_path, "wh", TEST_WHSEC),
            "FIN_STRIPE_SUCCESS_URL": SUCCESS, "FIN_STRIPE_CANCEL_URL": CANCEL}


# --------------------------------------------------------------------------------------------- helpers

def spy(h, fail):
    """Wrap the fake ledger: ``fail(event_type, payload)`` -> True makes that record fail (nothing recorded)."""
    orig = h.ledger.record_event

    def record_event(event_id, department, event_type, actor, subject_id, payload, summary):
        if fail(event_type, payload):
            raise fakes.LedgerNotRecorded("test: simulated ledger outage")
        return orig(event_id, department, event_type, actor, subject_id, payload, summary)

    h.ledger.record_event = record_event
    return orig


def once_after(trigger, target="local_log_appended"):
    """Fail the first ``target`` record that follows a record for which ``trigger(type, payload)`` held."""
    st = {"seen": False, "done": False}

    def fail(et, p):
        if not st["done"] and st["seen"] and et == target:
            st["done"] = True
            return True
        if trigger(et, p):
            st["seen"] = True
        return False
    return fail


def is_f4d(et, p):
    return et == "journal_entry_posted" and p.get("flow") == "F4d"


def payout_ready(h, payees=("clip-a",)):
    """Funded campaign, payees with one payable each, a run Andre approved and funded, clock past the release delay."""
    h.fund_campaign()
    for i, p in enumerate(payees, 1):
        h.payee(p)
        h.accrue(f"sub-{i}", clipper=p)
    h.recon()
    b = h.run()["batch"]
    h.approve(b)
    h.fund_batch(b["batch_id"])
    h.clock.advance(hours=13)
    h.recon()
    return b


def release(h, bid):
    return h.post(f"/fin/v1/payout-batches/{bid}/release", {"request_id": rid()}, caller="scheduler")


def f4d_entries(h, iid):
    return [e for e in h.svc.entries if e["memo_code"] == "F4d" and e["source"]["id"] == iid]


# --------------------------------------------------------------------------------------------- B-F1

def bank_line(inv_id, ref=b"txn-probe"):
    return {"txn_ref_sha256": hashlib.sha256(ref).hexdigest(), "entity": "zbc", "account": "1020",
            "direction": "credit", "amount": "1000.00", "value_date": "2026-10-02", "reference_token": inv_id}


def test_bf1_bank_receipt_books_after_its_anchor_failed(hr):
    """fin_p1_orphan / fin_p1c: the first commit's anchor fails; the retry a second later used to get a permanent 409
    on journal_entry_posted (its payload carried entry_sha256, which covers posted_at)."""
    doc = hr.rate_card()
    hr.profile(doc_id=doc)
    inv = hr.deposit_invoice()
    hr.bank.deposit("zbc", "1020", "1000.00")
    body = {"request_id": "probe-bank-1", "lines": [bank_line(inv["invoice_id"])]}
    hr.ledger.fail_on_type = "local_log_appended"
    assert hr.post("/fin/v1/bank/events", body, caller="bank_feed").status_code == 503
    hr.ledger.fail_on_type = None
    hr.clock.advance(seconds=1)
    r = hr.post("/fin/v1/bank/events", body, caller="bank_feed")
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["status"] == "matched"
    assert hr.bal("1020") == Decimal("1000.00") and len(hr.svc.entries) == 1
    hr.clock.advance(minutes=1)
    assert hr.post("/fin/v1/bank/events", body, caller="bank_feed").status_code == 200      # the stored answer
    assert len(hr.svc.entries) == 1


def test_bf1_journal_evidence_carries_no_clock_and_names_its_action(hr):
    """The journal evidence payload binds what is posted, its request key and its log line -- never posted_at."""
    doc = hr.rate_card()
    hr.profile(doc_id=doc)
    inv = hr.deposit_invoice()
    hr.ok(hr.post("/fin/v1/bank/events", {"request_id": rid(), "lines": [bank_line(inv["invoice_id"])]},
                  caller="bank_feed"))
    e = hr.svc.entries[-1]
    ev = hr.ledger.by_id[e["ledger_event_id"]]
    p = ev["payload"]
    assert "entry_sha256" not in p and "posted_at" not in p
    assert p["rk"] == derived_id("je", "zbc", e["idempotency_key"]) and p["seq"] == len(hr.svc.log)


# --------------------------------------------------------------------------------------------- B-F2

def test_bf2_payout_accepted_by_the_rail_is_booked_after_its_anchor_failed(hr):
    """fin_p2_payout: the rail accepted, the F4d line's anchor failed. Every later drive got a permanent 409 on
    journal_entry_posted; the item stayed ``submitting`` with the money gone and nothing booked."""
    b = payout_ready(hr)
    spy(hr, once_after(is_f4d))
    r = release(hr, b["batch_id"])
    assert r.status_code == 200, r.text
    iid = b["item_ids"][0]
    it = hr.svc.db["items"][iid]
    assert it["status"] == "submitted", it
    assert len(f4d_entries(hr, iid)) == 1
    assert hr.bal("2030", f"item:{iid}") == Decimal(it["net"])
    assert len(hr.stripe.payouts) == 1                               # one payout at the rail: the same key
    hr.stripe.paid(it["idempotency_key"])
    hr.rail_event("paid", iid)
    assert hr.svc.db["items"][iid]["status"] == "paid" and hr.bal("2030", f"item:{iid}") == 0


def test_bf2_restart_after_a_retried_booking_starts_and_shows_the_attempt(tmp_path):
    """The failed attempt stays on the ledger: it is ``attempted`` in /audit/evidence (never a second effect) and a
    restart is not refused as a ghost ruling (its action was committed under another log line)."""
    d = str(tmp_path / "fin")
    h = Harness(data_dir=d).ready()
    b = payout_ready(h)
    spy(h, once_after(is_f4d))
    assert release(h, b["batch_id"]).status_code == 200
    iid = b["item_ids"][0]
    ev = h.ok(h.get("/fin/v1/audit/evidence", event_type="journal_entry_posted"))
    rk = derived_id("je", "zbc", f"F4d|{iid}")
    rows = [x for x in ev["events"] if x["status"] == "committed" and x["rk"] == rk]
    assert len(rows) == 1
    assert ev["counts"]["attempted"] >= 1
    h2 = Harness(data_dir=d, ledger=h.ledger, clock=h.clock, fakes=h.f)
    assert h2.svc.db["items"][iid]["status"] == "submitted"
    assert h2.ok(h2.get("/fin/v1/integrity"))["status"] == "green"


def test_bf2_one_stuck_item_never_blocks_the_others(hr):
    """The first item's booking can never be recorded now; the second item is still submitted and booked."""
    b = payout_ready(hr, ("clip-a", "clip-b"))
    stuck, other = b["item_ids"][0], b["item_ids"][1]
    spy(hr, lambda et, p: is_f4d(et, p) and p.get("source_id") == stuck)
    r = release(hr, b["batch_id"])
    assert r.status_code == 200, r.text
    assert hr.svc.db["items"][other]["status"] == "submitted" and len(f4d_entries(hr, other)) == 1
    assert hr.svc.db["items"][stuck]["status"] == "submitting"
    res = {x["item_id"]: x for x in r.json()["results"] if x.get("outcome")}
    assert res[stuck]["outcome"] == "unknown_reconciling" and res[stuck]["took_effect"] == "unknown"


def test_bf2_a_release_after_money_moved_never_says_took_effect_false(hr):
    """The ledger goes down right after the rail accepted: the answer names the item's outcome as unknown and
    reconciling (it was a 503 saying ``took_effect: false``)."""
    b = payout_ready(hr)
    down = {"on": False}

    def fail(et, p):
        if is_f4d(et, p):
            down["on"] = True
        return down["on"]
    spy(hr, fail)
    r = release(hr, b["batch_id"])
    assert r.json().get("took_effect") is not False, r.text
    if r.status_code == 503:
        assert r.json()["took_effect"] == "unknown" and r.json()["outcome"] == "unknown_reconciling"
    else:
        assert r.status_code == 200
        res = [x for x in r.json()["results"] if x["item_id"] == b["item_ids"][0]]
        assert res and all(x.get("outcome") == "unknown_reconciling" and x["took_effect"] == "unknown" for x in res)
    assert len(hr.stripe.payouts) == 1


def test_bf2_a_503_after_the_bank_moved_money_says_unknown(hr):
    """A top-up the bank accepted whose booking cannot be recorded: 503 with took_effect "unknown", never false."""
    hr.bank.deposit("zbc", "1010", "1000.00")
    down = {"on": False}
    orig_transfer = hr.bank.transfer

    def transfer(*a, **k):
        out = orig_transfer(*a, **k)
        down["on"] = True
        return out
    hr.bank.transfer = transfer
    spy(hr, lambda et, p: down["on"])
    r = hr.post("/fin/v1/treasury/top-ups", {"request_id": rid(), "amount": "50.00"}, andre=ANDRE_TOKEN)
    assert r.status_code == 503
    assert r.json()["took_effect"] == "unknown" and r.json()["outcome"] == "unknown_reconciling"
    assert len(hr.bank.transfers) == 1


def test_bf2_paid_event_for_a_submitting_item_is_held_and_booked_by_our_own_reconcile(hr):
    """fin_p2b: the rail's ``paid`` event for an item still ``submitting`` was acked as ``ignored_submitting`` and
    lost. It is held; a redelivery while still submitting re-holds it (never ``duplicate``); the rail-sync job books
    the acceptance and then applies the held event."""
    b = payout_ready(hr)
    iid = b["item_ids"][0]
    down = {"on": True}
    spy(hr, lambda et, p: down["on"] and is_f4d(et, p))
    assert release(hr, b["batch_id"]).status_code == 200
    it = hr.svc.db["items"][iid]
    assert it["status"] == "submitting"
    hr.stripe.paid(it["idempotency_key"])
    ev = {"event_id": "evt-paid-1", "type": "paid", "signature": fakes.RAIL_SIG, "item_id": iid}
    r = hr.ok(hr.post("/fin/v1/rails/stripe/events", {"request_id": rid(), "events": [ev]}, caller="rail_gateway"))
    assert r["results"][0]["status"] == "held_submitting"
    r = hr.ok(hr.post("/fin/v1/rails/stripe/events", {"request_id": rid(), "events": [ev]}, caller="rail_gateway"))
    assert r["results"][0]["status"] == "held_submitting"
    down["on"] = False
    hr.ok(hr.post("/fin/v1/jobs/rail-sync/run", {"request_id": rid()}, caller="scheduler"))
    assert hr.svc.db["items"][iid]["status"] == "paid"
    assert [e["memo_code"] for e in hr.svc.entries if e["source"].get("id") == iid].count("F4e") == 1
    assert hr.bal("2030", f"item:{iid}") == 0 and hr.bal("2020", "payee:clip-a") == 0


def test_bf2_held_paid_event_is_booked_by_the_rails_redelivery(hr):
    """The held event's own replay fails once (ledger down for F4e): the rail's redelivery of the same event books it."""
    b = payout_ready(hr)
    iid = b["item_ids"][0]
    state = {"f4d": True, "f4e": True}

    def fail(et, p):
        if et == "journal_entry_posted" and p.get("flow") == "F4d" and state["f4d"]:
            return True
        if et == "journal_entry_posted" and p.get("flow") == "F4e" and state["f4e"] and not state["f4d"]:
            state["f4e"] = False
            return True
        return False
    spy(hr, fail)
    release(hr, b["batch_id"])
    it = hr.svc.db["items"][iid]
    hr.stripe.paid(it["idempotency_key"])
    ev = {"event_id": "evt-paid-2", "type": "paid", "signature": fakes.RAIL_SIG, "item_id": iid}
    assert hr.ok(hr.post("/fin/v1/rails/stripe/events", {"request_id": rid(), "events": [ev]},
                         caller="rail_gateway"))["results"][0]["status"] == "held_submitting"
    state["f4d"] = False
    hr.release(b["batch_id"])                                    # drives the item: F4d booked, the replay fails
    assert hr.svc.db["items"][iid]["status"] == "submitted"
    r = hr.ok(hr.post("/fin/v1/rails/stripe/events", {"request_id": rid(), "events": [ev]}, caller="rail_gateway"))
    assert r["results"][0]["status"] == "paid"
    assert hr.svc.db["items"][iid]["status"] == "paid"


# --------------------------------------------------------------------------------------------- B-F3

def test_bf3_stripe_payment_books_on_redelivery_after_its_anchor_failed(skeys):
    """fin_p3_stripe: the checkout.session.completed commit's anchor failed; every redelivery was 409/503 and the
    client's paid invoice was never booked."""
    st = make(skeys, FIN_CARD_PREPAYMENTS="1")
    inv = rr_invoice(st, "1200.00")
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    st.sim.pay(cs["session_id"], method="us_bank_account")
    st.ledger.fail_on_type = "local_log_appended"
    assert send(st, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"}).status_code \
        == 503
    st.ledger.fail_on_type = None
    st.clock.advance(minutes=1)
    r = send(st, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    assert r.status_code == 200 and r.json()["status"] == "matched", r.text
    assert memos(st).count("F13") == 1
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "paid"


# --------------------------------------------------------------------------------------------- B-F4

def test_bf4_topup_replay_after_restart_never_rebuilds_the_operation(tmp_path):
    """fin_p4_topup: the stored answer lived only in memory; after a restart the replay re-created the op (attempts
    0) and re-used the already reversed posting key: the bank moved $400 while the journal netted to zero."""
    d = str(tmp_path / "fin")
    h = Harness(data_dir=d).ready()
    h.bank.deposit("zbc", "1010", "1000.00")
    h.bank.transfer_available = False                      # attempt 1: the bank is down -> the posting is reversed
    body = {"request_id": "topup-1", "amount": "400.00"}
    first = h.ok(h.post("/fin/v1/treasury/top-ups", body, andre=ANDRE_TOKEN))
    tid = first["operation"]["op_id"]
    assert first["operation"]["status"] == "approved"
    h.bank.transfer_available = True
    h2 = Harness(data_dir=d, ledger=h.ledger, clock=h.clock, fakes=h.f)
    h2.clock.advance(minutes=2)
    r = h2.post("/fin/v1/treasury/top-ups", body, andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text
    op = h2.svc.db["treasury_ops"][tid]
    assert r.json()["operation"]["status"] == op["status"] == "approved"
    assert op["attempts"] == 1                              # never reset
    assert len(h2.bank.transfers) == 0                      # the replay moved nothing
    h2.ok(h2.post("/fin/v1/jobs/rail-sync/run", {"request_id": rid()}, caller="scheduler"))
    op = h2.svc.db["treasury_ops"][tid]
    assert op["status"] == "done" and op["attempts"] == 2
    assert len(h2.bank.transfers) == 1
    assert h2.bal("1020") == h2.bank.balances[("zbc", "1020")] == Decimal("400.00")


def test_bf4_existing_treasury_op_is_never_recreated(hr):
    """A request that would build an op id that already exists is refused, never written over."""
    from service import Op
    hr.bank.deposit("zbc", "1010", "1000.00")
    t = hr.ok(hr.post("/fin/v1/treasury/top-ups", {"request_id": "topup-x", "amount": "10.00"},
                      andre=ANDRE_TOKEN))["operation"]
    op = Op(hr.svc, "t", "andre", "treasury")
    with pytest.raises(Exception) as ei:
        hr.svc._new_treasury_op(op, "top_up", Decimal("10.00"), None, "andre", "topup-x")
    assert "never re-created" in str(ei.value)
    assert hr.svc.db["treasury_ops"][t["op_id"]]["status"] == "done"


# --------------------------------------------------------------------------------------------- X-6

def charge_of(st, pi):
    return st.sim.pis[pi]["latest_charge"]


def test_x6_dashboard_refund_is_booked_from_amount_refunded(skeys):
    st = make(skeys, FIN_CARD_PREPAYMENTS="1")
    inv = rr_invoice(st, "1200.00")
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    pi = st.sim.pay(cs["session_id"], method="us_bank_account")
    sent(st, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    before = zbm_bal(st, "1060")
    st.sim.refund(pi, 30000)                                          # $300 refunded in the Stripe Dashboard
    obj = {"id": charge_of(st, pi), "object": "charge", "payment_intent": pi}
    assert sent(st, "charge.refunded", obj)["status"] == "refund_booked"
    assert zbm_bal(st, "1060") == before - Decimal("300.00")
    assert zbm_bal(st, "1100", "client:zbm-client-7") == Decimal("300.00")
    assert memos(st).count("F7r") == 1
    assert any(b["leg"] == "stripe_refund" and b["status"] == "open" for b in st.svc.db["breaks"].values())
    assert sent(st, "charge.refunded", obj)["status"] == "payment_already_booked"     # booked once
    assert memos(st).count("F7r") == 1
    st.sim.refund(pi)                                                 # the rest: a full refund
    assert sent(st, "charge.refunded", obj)["status"] == "refund_booked"
    assert memos(st).count("F7r") == 2 and zbm_bal(st, "1100", "client:zbm-client-7") == Decimal("1200.00")
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    for e in st.svc.entries:
        d, c = J.totals(e)
        assert d == c


def test_x6_any_refund_on_a_media_prepayment_blocks_the_vendor_payment(skeys):
    st = make(skeys, FIN_CARD_PREPAYMENTS="1")
    bid, _, pi = media_paid_by_stripe(st)
    st.sim.refund(pi, 100)                                            # $1.00
    sent(st, "charge.refunded", {"id": charge_of(st, pi), "object": "charge", "payment_intent": pi})
    st.clock.advance(days=4)
    r = pay_vendor(st, bid, "10000.00", paid_on="2026-10-06")
    assert r.status_code == 409 and "refunded at" in r.text, r.text


# --------------------------------------------------------------------------------------------- X-8

def test_x8_settings_repr_never_prints_a_token():
    """x_settings_repr: Settings was a plain dataclass; repr() printed every bearer token in clear."""
    tok = "test-x8-service-do-not-use-" + "s" * 12
    andre = "test-x8-andre-do-not-use-" + "a" * 12
    ledger = "test-x8-ledger-do-not-use-" + "l" * 12
    s = config_mod.load({"FIN_SERVICE_TOKEN": tok, "FIN_ANDRE_APPROVAL_TOKEN": andre,
                         "LEDGER_SERVICE_URL": "http://127.0.0.1:1", "LEDGER_SERVICE_TOKEN": ledger})
    text = repr(s) + str(s)
    assert tok not in text and andre not in text and ledger not in text


# --------------------------------------------------------------------------------------------- X-10

def test_x10_release_locks_stay_bounded(hr):
    for i in range(50):
        assert release(hr, f"fin-bat-NOPE{i}").status_code == 404
    assert len(hr.svc.release_locks) == 0
    b = payout_ready(hr)
    rel = hr.release(b["batch_id"])
    hr.pay_items(rel["batch"])
    assert hr.batch(b["batch_id"])["status"] == "settled"
    assert release(hr, b["batch_id"]).status_code == 409
    assert b["batch_id"] not in hr.svc.release_locks


# --------------------------------------------------------------------------------------------- store (F-3/E-5)

def test_store_one_fsync_error_never_bricks_the_log(tmp_path, monkeypatch):
    import store as store_mod
    d = str(tmp_path / "fin")
    h = Harness(data_dir=d).ready()
    real = os.fsync
    hit = {"n": 0}

    def flaky(fd):
        hit["n"] += 1
        if hit["n"] == 1:
            raise OSError(5, "simulated EIO on fsync")
        return real(fd)
    monkeypatch.setattr(store_mod.os, "fsync", flaky)
    r = h.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "clip-q", "kind": "clipper",
                                  "declared_country": "US", "callback_contact_ref": "vault:contact-clip-q"},
               caller="clipper_network")
    assert r.status_code == 503 and hit["n"] >= 1
    monkeypatch.setattr(store_mod.os, "fsync", real)
    h.payee("clip-r")
    assert h.svc.log.verify()                         # the file holds exactly what memory holds
    with open(os.path.join(d, store_mod.LOG_NAME), "rb") as fh:
        assert fh.read().count(b"\n") == len(h.svc.log)


def test_store_an_empty_line_refuses_start(tmp_path):
    import store as store_mod
    d = str(tmp_path / "fin")
    h = Harness(data_dir=d).ready()
    getattr(h.svc, "close", lambda: None)()          # stop the instance (5d49ee9 had no close())
    p = os.path.join(d, store_mod.LOG_NAME)
    with open(p, "rb") as fh:
        raw = fh.read().split(b"\n")
    raw.insert(2, b"")
    with open(p, "wb") as fh:
        fh.write(b"\n".join(raw))
    with pytest.raises(Exception) as ei:
        Harness(data_dir=d, ledger=h.ledger, clock=h.clock, fakes=h.f)
    assert "empty line" in str(ei.value)


def test_store_never_writes_through_a_symlinked_log(tmp_path):
    import store as store_mod
    d = str(tmp_path / "fin")
    h = Harness(data_dir=d).ready()
    p = os.path.join(d, store_mod.LOG_NAME)
    target = str(tmp_path / "elsewhere.jsonl")
    os.replace(p, target)
    os.symlink(target, p)
    size = os.path.getsize(target)
    r = h.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "clip-z", "kind": "clipper",
                                  "declared_country": "US", "callback_contact_ref": "vault:contact-clip-z"},
               caller="clipper_network")
    assert r.status_code == 503
    assert os.path.getsize(target) == size


def test_single_writer_a_second_live_instance_on_one_directory_refuses(tmp_path):
    import api
    d = str(tmp_path / "fin")
    h = Harness(data_dir=d).ready()
    with pytest.raises(Exception) as ei:
        api.build_service(config_mod.load(h.env), h.clock, None, h.ledger)
    assert "data-directory claim" in str(ei.value) or "already holds this data directory" in str(ei.value)
    h.svc.close()
    assert h.svc.closed
    r = h.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "clip-y", "kind": "clipper",
                                  "declared_country": "US", "callback_contact_ref": "vault:contact-clip-y"},
               caller="clipper_network")
    assert r.status_code == 503                                       # a closed instance is inert
    h2 = Harness(data_dir=d, ledger=h.ledger, clock=h.clock, fakes=h.f)  # the directory is free again
    assert h2.svc.rules_version == h.svc.rules_version


# --------------------------------------------------------------------------------------------- verify outside the lock

def test_ledger_verify_runs_outside_the_service_lock(hr):
    seen = {}
    real = hr.ledger.verify

    def probe(got):
        ok = hr.svc.lock.acquire(blocking=False)
        got.append(ok)
        if ok:
            hr.svc.lock.release()

    def verify():
        got: list = []
        t = threading.Thread(target=probe, args=(got,))
        t.start()
        t.join(5)
        seen["free"] = bool(got and got[0])
        return real()
    hr.ledger.verify = verify
    assert hr.ok(hr.get("/fin/v1/integrity"))["status"] == "green"
    assert seen["free"] is True
