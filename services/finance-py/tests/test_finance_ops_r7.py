"""AEGIS wave B round 7 (scratchpad/night/aegis-finance-ops-waveB-r7.md), failing first with AEGIS's repros (H2-H9).
The lesson: fail closed must never become fail PERMANENT; every restore state has a bounded, audited, Andre-only way
out.

R7-1  one money movement, one item, one booking (the in-flight op and its posting; a reversed attempt follows the
      moved one); a second booking of a movement or of a bank reference is refused; Andre can reverse a booking.
R7-2  (a) the evidence summaries carry ids of the full allowed length; (b) an item that cannot be derived is booked by
      hand with the accounts FIXED by its type; (c) an unbooked moved item fences only its counterparty: the post
      phase ends for everyone else.
R7-3/4/5  the 4.12(g) exception and the lift need the bank's own balances to equal the books (less the money moved out
      and not booked); an unattributed bank line since the backup stops the exception; weekend statement days count.
R7-6  a parked rail event still waiting keeps the post phase on, alerts after 72 h, and Andre applies or discards it.
R7-7  a client receipt the ledger shows sent is never sent again, on every send path, with or without a restore.
Mutants: a held item's own payables; not_moved against a rail ``paid`` event; a refund correction covers its transfer.
"""

from __future__ import annotations

import hashlib
import logging

import mail as MAIL
import restore_evidence as RE
from fakes import RAIL_SIG
from helpers import ANDRE_TOKEN, Harness, rid
from intelligences import i10_evidence_audit as i10
from intelligences import i04_payout_run as I4
from ledger import derived_id, payload_sha256
from service import rid as rid_own
from test_restore_reimport import durable, lift, refeed, snapshot, statement_days
from test_restore_rebuild import _full_reimport, _restore_cycle, _void
from test_stripe_incoming import keys, rr_invoice  # noqa: F401

DISP = "/fin/v1/restore/items/{}/disposition"


def _items(z, out):
    return z.svc.db["breaks"][out["reimport_break_id"]]["items"]


def _disp(z, it, **body):
    return z.post(DISP.format(it["event_id"]), {"request_id": rid(), "reason": "checked", **body}, andre=ANDRE_TOKEN)


def _correct(z, it, ref, **kw):
    return z.post(f"/fin/v1/restore/items/{it['event_id']}/correction", {"request_id": rid(), "provider_ref": ref, **kw},
                  andre=ANDRE_TOKEN)


def _money(z, out):
    return [i for i in _items(z, out).values() if i["kind"] in ("money_out", "in_flight")]


def _funding_retried_after_backup(keys, tmp_path):  # noqa: F811
    """AEGIS H3a: funding approved before the backup (bank down: attempt 1 posted and reversed); after the backup
    the rail-sync retry posts attempt 2 and the bank moves the money."""
    x = durable(keys, tmp_path)
    x.fund_campaign()
    x.payee()
    x.accrue()
    x.recon()
    b = x.run()["batch"]
    x.approve(b)
    x.bank.transfer_available = False
    x.fund_batch(b["batch_id"])
    snapshot(tmp_path)
    x.bank.transfer_available = True
    x.ok(x.post("/fin/v1/jobs/rail-sync/run", {"request_id": rid()}, caller="scheduler"))
    z, out = _restore_cycle(x, tmp_path)
    _full_reimport(z, "2026-10-02", "2026-10-02")
    return x, z, out


# --------------------------------------------------------------------------------------------- R7-1

def test_r7_1_one_movement_one_booking_and_a_booking_can_be_reversed(keys, tmp_path):  # noqa: F811
    x, z, out = _funding_retried_after_backup(keys, tmp_path)
    its = [i for i in _money(z, out) if i["event_type"] in ("treasury_op", "treasury_posting_committed")]
    assert len(its) == 2
    primary = [i for i in its if i["status"] != "follows_movement"]
    follower = [i for i in its if i["status"] == "follows_movement"]
    assert len(primary) == 1 and len(follower) == 1 and follower[0]["primary"] == primary[0]["event_id"]
    assert _disp(z, follower[0], disposition="moved", provider_ref="bank-ref-1").status_code == 409
    z.ok(_disp(z, primary[0], disposition="moved", provider_ref="bank-ref-1"))
    lift(z)
    assert _correct(z, follower[0], "bank-ref-1").status_code == 409         # never booked on its own
    r = z.post(f"/fin/v1/restore/items/{primary[0]['event_id']}/manual-booking",
               {"request_id": rid(), "provider_ref": "bank-ref-1", "amount": "1.00", "counterparty": "x",
                "reason": "x", "evidence_sha256": "e" * 64, "transfer_kind": "funding"}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "derived" in r.text                     # nothing typed when it derives
    c = z.ok(_correct(z, primary[0], "bank-ref-1"), 201)
    assert _correct(z, primary[0], "bank-ref-1").status_code == 409          # once
    frc = [e for e in z.svc.entries if e["memo_code"] == "FRC"]
    assert len(frc) == 1
    # the way back: Andre reverses the booking (exact reversal, linked), the item is open for booking again
    assert z.post(f"/fin/v1/restore/items/{primary[0]['event_id']}/correction/reversal",
                  {"request_id": rid(), "reason": "x"}).status_code in (401, 403)
    rv = z.ok(z.post(f"/fin/v1/restore/items/{primary[0]['event_id']}/correction/reversal",
                     {"request_id": rid(), "reason": "booked on the wrong reference"}, andre=ANDRE_TOKEN), 201)
    assert rv["reversal_entry"]["reverses_entry_id"] == c["entry"]["entry_id"]
    it = _items(z, out)[primary[0]["event_id"]]
    assert not it.get("corrected_by") and it["reversed_bookings"][0]["entry_id"] == c["entry"]["entry_id"]
    assert not _items(z, out)[follower[0]["event_id"]].get("corrected_by")
    z.ok(_correct(z, primary[0], "bank-ref-1"), 201)                          # booked again, once
    assert z.post(f"/fin/v1/restore/items/{follower[0]['event_id']}/correction/reversal",
                  {"request_id": rid(), "reason": "x"}, andre=ANDRE_TOKEN).status_code == 409


def test_r7_1_a_reversed_attempt_derives_nothing_and_follows(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    x.fund_campaign()
    x.payee()
    x.accrue()
    x.recon()
    b = x.run()["batch"]
    x.approve(b)
    snapshot(tmp_path)
    x.bank.transfer_available = False
    x.fund_batch(b["batch_id"])                                              # attempt 1: reversed
    x.bank.transfer_available = True
    x.ok(x.post("/fin/v1/jobs/rail-sync/run", {"request_id": rid()}, caller="scheduler"))   # attempt 2: moved
    z, out = _restore_cycle(x, tmp_path)
    posts = sorted([i for i in _money(z, out) if i["event_type"] == "treasury_posting_committed"],
                   key=lambda i: i["order"])
    assert posts[0].get("reversed_on_ledger") and z.svc._derive_money(posts[0]) is None
    assert posts[0]["status"] == "follows_movement" and posts[1]["status"] == "needs_review"


# --------------------------------------------------------------------------------------------- R7-2a

def _evidence_item(event_type, rk, payload, summary, seq, ref):
    psha = payload_sha256({**payload, "rk": rk, "seq": seq})
    return {"event_id": i10.evidence_id(rk, event_type, psha), "payload_sha256": psha, "summary": summary,
            "event_type": event_type, "ref": ref, "kind": "money_out"}


def test_r7_2a_max_length_ids_round_trip_for_every_event_type():
    """Payee and campaign ids up to 128 characters (models.Id), client ids up to 100 (CLIENT_ID_RE); the ids Finance
    mints (batch, item, op, refund) are service.rid's fixed 34 characters; amounts up to 12 integer digits."""
    hr = Harness().ready()
    svc = hr.svc
    payee, camp, client = "p" * 128, "c" * 128, "k" * 100
    period = "2026-W53"
    batch = rid_own("bat", "trolley", period, "r")
    item, refund, op = rid_own("itm", batch, payee), rid_own("rfd", "x"), rid_own("trx", "y")
    key, seq, net = I4.idempotency_key(batch, payee, period), 999999999, "999999999999.99"
    s = RE.item_summary(net, period, payee, "trolley", batch, seq)
    assert len(s) <= RE.SUMMARY_MAX
    it = _evidence_item("item_submitted", derived_id("isub", item),
                        RE.item_payload(item, net, key, payee, "trolley", batch), s, seq, item)
    d = svc._derive_money(it)
    assert d and d["payee"] == payee and d["batch"] == batch and d["key"] == key
    s = RE.refund_paid_summary(net, camp, client, seq)
    assert len(s) <= RE.SUMMARY_MAX
    it = _evidence_item("refund_paid", derived_id("rfdpd", refund), RE.refund_paid_payload(refund, net, camp, client),
                        s, seq, refund)
    d = svc._derive_money(it)
    assert d and d["campaign"] == camp and d["client"] == client
    for dr, cr, kind in ((("1040", None), ("1020", None), "funding"),
                         (("2050", f"client:{client}"), ("1020", None), "refund_payment")):
        s = RE.xfer_summary(kind, 999999, "F6p", net, dr, cr, refund, seq)
        assert len(s) <= RE.SUMMARY_MAX
        it = _evidence_item("treasury_posting_committed", derived_id("trxp", op, 999999),
                            RE.xfer_payload(op, 999999, kind, "F6p", net, dr, cr, refund), s, seq, op)
        assert svc._derive_money(it) is not None


# --------------------------------------------------------------------------------------------- R7-2b / R7-2c

def _payout_after_backup(keys, tmp_path, payees=("clip-a",)):  # noqa: F811
    x = durable(keys, tmp_path)
    x.fund_campaign()
    for p in payees:
        x.payee(p)
        x.accrue(sid=f"sub-{p}", clipper=p)
    x.recon()
    snapshot(tmp_path)
    b = x.run()["batch"]
    x.approve(b)
    x.fund_batch(b["batch_id"])
    x.clock.advance(hours=13)
    x.recon()
    x.release(b["batch_id"])
    for c in [c for c in x.stripe.calls if c[0] == "submit"]:
        x.stripe.paid(c[1])
    pays = {p["payee_id"]: p for p in x.svc.db["payables"].values()}
    z, out = _restore_cycle(x, tmp_path)
    _full_reimport(z, "2026-10-02", "2026-10-02")
    return x, z, out, b, pays


def test_r7_2b_an_underivable_item_is_booked_by_hand_with_fixed_accounts(keys, tmp_path):  # noqa: F811
    x, z, out, b, pays = _payout_after_backup(keys, tmp_path)
    bid = out["reimport_break_id"]
    (sub,) = [i for i in _money(z, out) if i["event_type"] == "item_submitted"]
    old = {**sub, "summary": "Payout item submitted to stripe: 29.01"}       # an old-format summary: not derivable
    z.svc.db["breaks"][bid] = {**z.svc.db["breaks"][bid], "items": {**z.svc.db["breaks"][bid]["items"],
                                                                     sub["event_id"]: old}}
    assert z.svc._derive_money(old) is None
    amt = pays["clip-a"]["current_amount"]
    z.ok(_disp(z, old, disposition="moved", provider_ref="rail-1", moved_amount=amt, counterparty="clip-a"))
    for i in _money(z, out):
        if i["status"] == "needs_review":
            z.ok(_disp(z, i, disposition="moved", provider_ref=f"ref-{i['event_type']}"))
    lift(z)
    assert _correct(z, old, "rail-1", payable_ids=[pays["clip-a"]["payable_id"]]).status_code == 409
    url = f"/fin/v1/restore/items/{sub['event_id']}/manual-booking"
    body = {"request_id": rid(), "provider_ref": "rail-1", "amount": amt, "counterparty": "clip-a",
            "reason": "rail statement shows po_1 paid", "evidence_sha256": "e" * 64, "rail": "stripe",
            "payable_ids": [pays["clip-a"]["payable_id"]]}
    assert z.post(url, {**body, "lines": []}, andre=ANDRE_TOKEN).status_code == 422      # accounts never chosen
    assert z.post(url, {**body, "request_id": rid(), "amount": "1.00"}, andre=ANDRE_TOKEN).status_code == 409
    assert z.post(url, {**body, "request_id": rid(), "counterparty": "clip-zz"}, andre=ANDRE_TOKEN).status_code == 409
    assert z.post(url, body).status_code in (401, 403)
    r = z.ok(z.post(url, body, andre=ANDRE_TOKEN), 201)
    assert {(ln["account"], ln["subledger"]) for ln in r["entry"]["lines"]} == {("2020", "payee:clip-a"), ("1040", None)}
    assert "clip-a" not in z.svc._subject_fences()
    assert z.svc.db["payables"][pays["clip-a"]["payable_id"]]["status"] == "settled"


def test_r7_2c_an_unbooked_payee_does_not_hold_up_anyone_else(keys, tmp_path):  # noqa: F811
    x, z, out, b, pays = _payout_after_backup(keys, tmp_path)
    for i in _money(z, out):
        if i["status"] == "needs_review":
            z.ok(_disp(z, i, disposition="moved", provider_ref=f"ref-{i['event_type']}"))
    lift(z)
    rec = z.recon()
    assert rec["post_restore_clean"] is True                                 # explained by the unbooked items only
    z.payee("clip-c")
    z.accrue(sid="sub-c", clipper="clip-c")
    z.recon()
    b2 = z.run()["batch"]
    assert {it["payee_id"] for it in b2["items"]} == {"clip-c"}     # clip-a is held alone (PAYEE_HOLD)
    assert any("RESTORE_FENCED" in r["message"] for e in b2.get("excluded", []) if e["payee_id"] == "clip-a"
               for r in e["reasons"])
    z.approve(b2)
    z.fund_batch(b2["batch_id"])
    z.clock.advance(hours=13)
    z.recon()
    z.release(b2["batch_id"])
    items = [i for i in z.svc.db["items"].values() if i["batch_id"] == b2["batch_id"]]
    assert {i["payee_id"]: i["status"] for i in items}.get("clip-c") in ("submitted", "paid")
    assert "clip-a" in z.svc._subject_fences()                                # still fenced until booked


def _accrued_after_backup(keys, tmp_path):  # noqa: F811
    """AEGIS H7b: the accrual, run, funding, release and payment all after the backup: no payable is restored."""
    x = durable(keys, tmp_path)
    x.fund_campaign()
    x.payee()
    x.recon()
    snapshot(tmp_path)
    x.accrue()
    x.recon()
    b = x.run()["batch"]
    x.approve(b)
    x.fund_batch(b["batch_id"])
    x.clock.advance(hours=13)
    x.recon()
    x.release(b["batch_id"])
    for c in [c for c in x.stripe.calls if c[0] == "submit"]:
        x.stripe.paid(c[1])
    (p,) = x.svc.db["payables"].values()
    z, out = _restore_cycle(x, tmp_path)
    _full_reimport(z, "2026-10-02", "2026-10-02")
    assert not z.svc.db["payables"]
    for i in _money(z, out):
        if i["status"] == "needs_review":
            z.ok(_disp(z, i, disposition="moved", provider_ref=f"ref-{i['event_type']}"))
    lift(z)
    (sub,) = [i for i in _money(z, out) if i["event_type"] == "item_submitted"]
    return z, out, sub, p


def test_r7_8_a_resent_handoff_recreates_the_payable_and_the_correction_books_it(keys, tmp_path):  # noqa: F811
    z, out, sub, p = _accrued_after_backup(keys, tmp_path)
    assert _correct(z, sub, "ref-item_submitted", payable_ids=[p["payable_id"]]).status_code == 409
    z.ok(z.handoff("sub-1"))                                     # the clipper network re-sends the handoff
    z.ok(_correct(z, sub, "ref-item_submitted", payable_ids=[p["payable_id"]]), 201)
    assert "clip-a" not in z.svc._subject_fences()


def test_r7_8_without_a_resend_the_manual_booking_names_the_submissions(keys, tmp_path):  # noqa: F811
    z, out, sub, p = _accrued_after_backup(keys, tmp_path)
    old = {**sub, "summary": "Payout item submitted to stripe"}   # and the summary cannot be derived either
    bid = out["reimport_break_id"]
    z.svc.db["breaks"][bid] = {**z.svc.db["breaks"][bid], "items": {**z.svc.db["breaks"][bid]["items"],
                                                                     sub["event_id"]: old}}
    url = f"/fin/v1/restore/items/{sub['event_id']}/manual-booking"
    body = {"request_id": rid(), "provider_ref": "ref-item_submitted", "amount": p["current_amount"],
            "counterparty": "clip-a", "reason": "rail statement", "evidence_sha256": "e" * 64, "rail": "stripe"}
    r = z.post(url, body, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "submission_ids" in r.text
    z.ok(z.post(url, {**body, "request_id": rid(), "submission_ids": ["sub-1"]}, andre=ANDRE_TOKEN), 201)
    assert "clip-a" not in z.svc._subject_fences()
    z.ok(z.handoff("sub-1"))                                     # re-sent later: settled by the booking, never paid
    assert z.svc.db["payables"][p["payable_id"]]["status"] == "settled"
    assert z.svc.bal("2020", "payee:clip-a") == 0                # accrued once, booked once
    rv = z.post(f"/fin/v1/restore/items/{sub['event_id']}/correction/reversal",
                {"request_id": rid(), "reason": "wrong amount"}, andre=ANDRE_TOKEN)
    z.ok(rv, 201)
    assert z.svc.db["payables"][p["payable_id"]]["status"] == "returned"
    assert "clip-a" in z.svc._subject_fences()


# --------------------------------------------------------------------------------------------- R7-3 / R7-4 / R7-5

def _lost_ach(keys, tmp_path, token=True):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "700.00", methods=("ach",))
    snapshot(tmp_path)
    x.bank.deposit("zbm", "1010", "700.00")
    line = {"txn_ref_sha256": "c" * 64, "entity": "zbm", "account": "1010", "direction": "credit",
            "amount": "700.00", "value_date": "2026-10-02"}
    if token:
        line["reference_token"] = a["invoice_id"]
    x.ok(x.post("/fin/v1/bank/events", {"request_id": rid(), "lines": [line]}, caller="bank_feed"))
    z, out = _restore_cycle(x, tmp_path, with_refeed=False)
    _full_reimport(z, "2026-10-02", "2026-10-02")
    return z, out, a, line


def test_r7_3_the_exception_and_the_lift_need_the_banks_own_balance(keys, tmp_path):  # noqa: F811
    z, out, a, line = _lost_ach(keys, tmp_path)
    refeed(z)                                                    # the statement omits the line, as the books do
    r = _void(z, z.svc.db["invoices"][a["invoice_id"]])
    assert r.status_code == 409 and "bank" in r.text             # AEGIS H4a: the bank's own balance shows it
    lift(z, status=409)


def test_r7_4_an_unattributed_bank_line_stops_the_exception(keys, tmp_path):  # noqa: F811
    z, out, a, line = _lost_ach(keys, tmp_path, token=False)
    z.ok(z.post("/fin/v1/bank/events", {"request_id": rid(), "lines": [{**line, "txn_ref_sha256": "d" * 64}]},
                caller="bank_feed"))                             # re-fed: an unapplied receipt
    refeed(z)
    r = _void(z, z.svc.db["invoices"][a["invoice_id"]])
    assert r.status_code == 409 and "unattributed" in r.text


def test_r7_5_a_weekend_statement_day_is_checked(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    snapshot(tmp_path)
    rr_invoice(x, "700.00", methods=("ach",))                    # lost: issued after the backup
    x.clock.advance(days=3)                                      # Fri Oct 2 -> Mon Oct 5
    z, out = _restore_cycle(x, tmp_path, with_refeed=False)
    brk = z.svc.db["breaks"][out["reimport_break_id"]]
    days = statement_days(z, brk["since"], brk["opened_on"])
    assert any(d["day"] == "2026-10-03" for d in days)
    sat = [d for d in days if d["day"] == "2026-10-03" and d["entity"] == "zbm"][0]
    sat.update(credit_lines=1, credit_total="700.00")            # a Saturday line Finance never booked
    r = z.post("/fin/v1/restore/bank-refeed", {"request_id": rid(), "since": brk["since"], "until": brk["opened_on"],
                                               "statement_ref": "s", "statement_days": days}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "2026-10-03" in r.text
    without = [d for d in days if d["day"] != "2026-10-03"]
    r = z.post("/fin/v1/restore/bank-refeed", {"request_id": rid(), "since": brk["since"], "until": brk["opened_on"],
                                               "statement_ref": "s", "statement_days": without}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "missing" in r.text           # every calendar day is required


# --------------------------------------------------------------------------------------------- R7-6

def test_r7_6_a_parked_event_waiting_blocks_the_phase_alerts_and_andre_decides(keys, tmp_path, caplog):  # noqa: F811
    x = durable(keys, tmp_path)
    x.fund_campaign()
    x.payee()
    x.accrue()
    x.recon()
    b = x.run()["batch"]
    x.approve(b)
    x.fund_batch(b["batch_id"])
    x.clock.advance(hours=13)
    x.recon()
    x.release(b["batch_id"])
    (item,) = [i for i in x.svc.db["items"].values() if i["batch_id"] == b["batch_id"]]
    snapshot(tmp_path)
    a = rr_invoice(x, "100.00")
    x.ok(_void(x, x.svc.db["invoices"][a["invoice_id"]]))
    z, out = _restore_cycle(x, tmp_path)
    ev = {"event_id": "evt-ret-only", "type": "returned", "signature": RAIL_SIG, "item_id": item["item_id"]}
    z.ok(z.post("/fin/v1/rails/stripe/events", {"request_id": rid(), "events": [ev]}, caller="rail_gateway"))
    _full_reimport(z, "2026-10-02", "2026-10-02")
    for it in [i for i in _items(z, out).values() if i["status"] == "needs_review"]:
        z.ok(_disp(z, it, disposition="lost"))
    lift(z)
    first = z.svc.db["rail_events"]["stripe|evt-ret-only"]["waiting_since"]
    z.clock.advance(hours=1)
    z.post("/fin/v1/jobs/rail-sync/run", {"request_id": rid()}, caller="scheduler")
    assert z.svc.db["rail_events"]["stripe|evt-ret-only"]["waiting_since"] == first   # since, not last tried
    assert z.recon()["post_restore_clean"] is False                           # still waiting: not clean
    z.clock.advance(hours=73)
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        z.svc._watch_tick()
    assert any("rail_event_parked_72h" in m.getMessage() for m in caplog.records)
    url = "/fin/v1/rail-events/stripe/evt-ret-only/disposition"
    assert z.post(url, {"request_id": rid(), "action": "apply", "reason": "x"}, andre=ANDRE_TOKEN).status_code == 409
    z.ok(z.post(url, {"request_id": rid(), "action": "discard", "reason": "the rail confirms no return"},
                andre=ANDRE_TOKEN))
    assert z.recon()["post_restore_clean"] is True


# --------------------------------------------------------------------------------------------- R7-7

def test_r7_7_a_receipt_the_ledger_shows_sent_is_never_sent_again():
    from test_client_mail import harness
    fx = MAIL.FixtureClientMail()
    hr = harness(fx)
    rec = {"client_receipt_id": "fin-crc-7", "status": "pending_send", "client_id": "zbm-client-7", "attempts": 0,
           "created_at": "2026-10-02T00:00:00Z", "entity": "zbm", "invoice_id": None, "what_you_bought": "x",
           "items": [], "period": None, "amount_paid": "1.00", "currency": "usd", "paid_on": "2026-10-02",
           "method": "ach"}
    hr.svc.db["client_receipts"]["fin-crc-7"] = rec
    hr.ledger.record_event("fin-crcs-" + hashlib.sha256(b"x").hexdigest()[:40], "finance", "client_receipt_sent",
                           i10.ACTOR, "fin-crc-7", {"x": 1}, "Client receipt sent")
    # (the ledger is read once per process, at the first send: both sends were recorded before this process)
    hr.ledger.record_event("fin-crcs-" + hashlib.sha256(b"y").hexdigest()[:40], "finance", "client_receipt_sent",
                           i10.ACTOR, "fin-crc-8", {"x": 2}, "Client receipt sent")
    hr.post("/fin/v1/client-receipts/fin-crc-7/send", {"request_id": rid()}, caller="scheduler")
    assert fx.delivered == [] and hr.svc.db["client_receipts"]["fin-crc-7"]["status"] == "sent"
    hr.svc.db["client_receipts"]["fin-crc-8"] = {**rec, "client_receipt_id": "fin-crc-8"}
    hr.ok(hr.post("/fin/v1/jobs/client-mail/run", {"request_id": rid()}, caller="ops_cron"))
    assert fx.delivered == [] and hr.svc.db["client_receipts"]["fin-crc-8"]["status"] == "sent"


# --------------------------------------------------------------------------------------------- the mutants

def test_r7_a_held_payout_item_books_only_its_own_payables(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    x.fund_campaign()
    x.payee()
    x.accrue()
    x.recon()
    b = x.run()["batch"]
    x.approve(b)
    x.fund_batch(b["batch_id"])
    x.accrue(sid="sub-2")                                         # the same payee, the same amount, NOT in the item
    x.clock.advance(hours=13)
    x.recon()
    snapshot(tmp_path)
    x.release(b["batch_id"])
    for c in [c for c in x.stripe.calls if c[0] == "submit"]:
        x.stripe.paid(c[1])
    z, out = _restore_cycle(x, tmp_path)
    _full_reimport(z, "2026-10-02", "2026-10-02")
    (sub,) = [i for i in _money(z, out) if i["event_type"] == "item_submitted"]
    held = z.svc.db["items"][sub["ref"]]
    other = [p for p in z.svc.db["payables"].values() if p["payable_id"] not in held["payable_ids"]
             and p["status"] in ("accrued", "returned")]
    assert other
    for i in _money(z, out):
        if i["status"] == "needs_review":
            z.ok(_disp(z, i, disposition="moved", provider_ref=f"ref-{i['event_type']}"))
    lift(z)
    r = _correct(z, sub, "ref-item_submitted", payable_ids=[other[0]["payable_id"]])
    assert r.status_code == 409 and "own" in r.text


def test_r7_b_not_moved_is_refused_against_the_rails_paid_event():
    hr = Harness().ready()
    hr.svc._lockdown = {"state": "on", "detected": False, "build_incomplete": False}
    hr.svc.db["breaks"]["fin-brk-R"] = {"break_id": "fin-brk-R", "leg": "restore_reimport_required", "status": "open",
                                       "items": {"rail:stripe:e1": {"event_id": "rail:stripe:e1", "kind": "in_flight",
                                                                    "ref": "fin-itm-L", "event_type": "rail_paid",
                                                                    "status": "needs_review"}}}
    r = hr.post(DISP.format("rail:stripe:e1"), {"request_id": rid(), "disposition": "not_moved", "reason": "x",
                                                "provider_ref": "s", "statement_date": "2026-10-02"}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "paid" in r.text
