"""AEGIS wave B round 2: the new routes and the mutants N8, N10 and N11, failing first.

R2-2  Andre disposes of a restore item that cannot be rebuilt: Andre only, ``reissued`` names what was re-issued,
      and the restore break closes once every item is rebuilt or disposed.
R2-3  the generic break resolution refuses the restore break and every deadline leg.
R2-8  cancel-or-credit closes on a void or a full credit, or on Andre's explicit close with a reason.
R2-9  with mail off (or sent by hand) Andre marks an acknowledgement sent; its queued email is never sent after.
R2-10 refund totals on a restore come only from verified ``stripe_refund_booked`` events.
R2-1  an effect whose commit mark could not be written alerts, the job writes the mark later, and a restore before
      that treats the effect as unproven (an item for Andre, never booked).
N8    a second open sow-ended deadline on the same invoice is refused (test_aegis_waveB_r2.py A8).
N10   the re-import closes a window only when a fresh plan for it is empty.
N11   credit note numbers go on from the highest number, never from the count.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from helpers import ANDRE_TOKEN, Harness, rid
from test_client_mail import harness, run_mail, set_contact
from test_credit_notes import CLIENT, ach_invoice, credit, dashboard_refund, stripe_paid
from test_restore_reimport import durable, lift, pay, snapshot
from test_restore_rebuild import _full_reimport, _restore_cycle
from test_stripe_incoming import keys, make, rr_invoice  # noqa: F401

import mail as MAIL


def _resolve(hr, bid):
    return hr.post(f"/fin/v1/breaks/{bid}/resolution", {"request_id": rid(), "explanation_code": "unknown"},
                   andre=ANDRE_TOKEN)


def _close(hr, bid, basis, **kw):
    return hr.post(f"/fin/v1/deadlines/{bid}/close", {"request_id": rid(), "basis": basis,
                                                     "reason": "handled with the client by phone", **kw},
                   andre=ANDRE_TOKEN)


def _ack(hr):
    return hr.ok(hr.post(f"/fin/v1/clients/{CLIENT}/acknowledgements",
                         {"request_id": rid(), "topic": "refund_request", "received_on": "2026-10-02"},
                         andre=ANDRE_TOKEN), 201)


def _sow(hr, inv):
    return hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/sow-ended",
                         {"request_id": rid(), "sow_ended_on": "2026-10-02"}, andre=ANDRE_TOKEN), 201)


# --------------------------------------------------------------------------------------------- R2-3

def test_r2_3_the_generic_resolution_refuses_the_restore_break_and_every_deadline(keys):  # noqa: F811
    hr = harness(MAIL.FixtureClientMail())
    ack = _ack(hr)["break_id"]
    sow = _sow(hr, ach_invoice(hr))["break_id"]
    st = make(keys, FIN_CARD_PREPAYMENTS="1")
    inv, _ = stripe_paid(st)
    st.ok(credit(st, inv, "200.00"), 201)
    (refund,) = [b["break_id"] for b in st.svc.db["breaks"].values() if b["leg"] == "credit_note_refund_due"]
    for h, bid in ((hr, ack), (hr, sow), (st, refund)):
        r = _resolve(h, bid)
        assert r.status_code == 409 and "closes only by its own rule" in r.text, (bid, r.text)
        assert h.svc.db["breaks"][bid]["status"] == "open"
    # the restore break: a copy of an open one, so only the leg differs from the deadlines above
    hr.svc.db["breaks"]["fin-brk-RESTORE0000000000000000000"] = {
        **hr.svc.db["breaks"][sow], "break_id": "fin-brk-RESTORE0000000000000000000",
        "leg": "restore_reimport_required"}
    assert _resolve(hr, "fin-brk-RESTORE0000000000000000000").status_code == 409


# --------------------------------------------------------------------------------------------- R2-8 / R2-9

def test_r2_9_andre_marks_an_acknowledgement_sent_by_hand_and_its_email_never_goes(keys):  # noqa: F811
    fx = MAIL.FixtureClientMail()
    hr = harness(fx)
    set_contact(hr)
    out = _ack(hr)
    bid = out["break_id"]
    assert hr.post(f"/fin/v1/deadlines/{bid}/close", {"request_id": rid(), "basis": "acknowledged_outside_finance",
                                                     "reason": "x"}).status_code in (401, 403)
    assert _close(hr, bid, "closed_by_andre").status_code == 409                     # the wrong basis for this leg
    assert _close(hr, bid, "acknowledged_outside_finance", done_on="2026-10-09").status_code in (400, 422)
    r = hr.ok(_close(hr, bid, "acknowledged_outside_finance", done_on="2026-10-02"))
    b = hr.svc.db["breaks"][bid]
    assert b["status"] == "resolved" and b["resolution"]["by"] == "andre" and b["met_late"] is False
    assert hr.svc.db["mail_outbox"][out["mail_id"]]["status"] == "cancelled_sent_outside"
    assert hr.ledger.of_type("deadline_closed_by_andre") and r["ledger_event_ids"]
    run_mail(hr)
    assert fx.delivered == []                                                       # never sent twice
    assert _close(hr, bid, "acknowledged_outside_finance").status_code == 409        # already closed


def test_r2_8_a_partial_credit_leaves_cancel_or_credit_open_until_andre_closes_it(keys):  # noqa: F811
    hr = Harness().ready()
    inv = ach_invoice(hr)
    bid = _sow(hr, inv)["break_id"]
    hr.ok(credit(hr, inv, "400.00", refund_due_from=None), 201)                    # credited down to the work done
    assert hr.svc.db["breaks"][bid]["status"] == "open"
    assert _close(hr, bid, "acknowledged_outside_finance").status_code == 409
    hr.ok(_close(hr, bid, "closed_by_andre"))
    assert hr.svc.db["breaks"][bid]["status"] == "resolved"
    # a refund deadline is never closed by hand: it closes when the refund is paid
    st = make(keys, FIN_CARD_PREPAYMENTS="1")
    pinv, _ = stripe_paid(st)
    st.ok(credit(st, pinv, "200.00"), 201)
    (refund,) = [b["break_id"] for b in st.svc.db["breaks"].values() if b["leg"] == "credit_note_refund_due"]
    assert _close(st, refund, "closed_by_andre").status_code == 409


# --------------------------------------------------------------------------------------------- N11

def test_n11_credit_note_numbers_go_on_from_the_highest_not_the_count():
    hr = Harness().ready()
    inv = ach_invoice(hr)
    hr.ok(credit(hr, inv, "100.00", refund_due_from=None), 201)
    hr.ok(credit(hr, hr.svc.db["invoices"][inv["invoice_id"]], "100.00", refund_due_from=None), 201)
    numbers = sorted(c["number"] for c in hr.svc.db["credit_notes"].values())
    assert numbers == ["CN-2026-0001", "CN-2026-0002"]
    first = [k for k, c in hr.svc.db["credit_notes"].items() if c["number"] == "CN-2026-0001"][0]
    del hr.svc.db["credit_notes"][first]                         # a gap (e.g. one not yet rebuilt after a restore)
    assert hr.svc._next_credit_note_number() == "CN-2026-0003"    # count + 1 would reuse 0002
    hr.svc.db["breaks"]["fin-brk-FLOOR00000000000000000000"] = {"break_id": "fin-brk-FLOOR00000000000000000000",
                                                                "leg": "restore_reimport_required",
                                                                "status": "resolved",
                                                                "cn_number_floor": {"2026": 9, "2025": 70}}
    assert hr.svc._next_credit_note_number() == "CN-2026-0010"    # never below a number the ledger saw (that year)


# --------------------------------------------------------------------------------------------- N10

def test_n10_a_window_closes_only_when_a_fresh_plan_for_it_is_empty(keys, tmp_path, monkeypatch):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "800.00")
    snapshot(tmp_path)
    pay(x, a)                                                     # a payment the restored books do not have
    z, out = _restore_cycle(x, tmp_path)
    monkeypatch.setattr(z.svc, "_reimport_one", lambda request_id, action: "booked")   # claims it, books nothing
    dry, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    assert dry["plan"]["actions"] and real["failed"] == 0
    assert real["clean"] is False and out["reimport_break_id"] not in real["ready_to_lift"]
    b = z.svc.db["breaks"][out["reimport_break_id"]]
    assert b["status"] == "open" and b["covered"] == []


# --------------------------------------------------------------------------------------------- R2-2

def test_r3_3_dispositions_only_for_what_cannot_be_rebuilt_bound_to_the_client(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    paid_inv = rr_invoice(x, "900.00")
    pay(x, paid_inv)
    snapshot(tmp_path)
    x.ok(credit(x, x.svc.db["invoices"][paid_inv["invoice_id"]], "600.00"), 201)   # rebuildable: verified + marked
    x.ok(x.post(f"/fin/v1/clients/{CLIENT}/acknowledgements",                      # a deadline: never rebuilt
                {"request_id": rid(), "topic": "refund_request", "received_on": "2026-10-02"}, andre=ANDRE_TOKEN), 201)
    z, out = _restore_cycle(x, tmp_path)
    items = z.svc.db["breaks"][out["reimport_break_id"]]["items"]
    (cn,) = [i for i in items.values() if i["kind"] == "credit_note"]
    (ddl,) = [i for i in items.values() if i["kind"] == "deadline"]
    assert cn["status"] == "pending" and ddl["status"] == "needs_review" and ddl["client_id"] == CLIENT
    lost = {"request_id": rid(), "disposition": "lost", "reason": "typo"}
    # a verified, rebuildable refund obligation is never disposed of (AEGIS B5/B5b)
    assert z.post(f"/fin/v1/restore/items/{cn['event_id']}/disposition", lost, andre=ANDRE_TOKEN).status_code == 409
    p = f"/fin/v1/restore/items/{ddl['event_id']}/disposition"
    body = {"disposition": "reissued", "reason": "acknowledged again by phone"}
    assert z.post(p, {**body, "request_id": rid(), "reissued_as": paid_inv["invoice_id"]}).status_code in (401, 403)
    assert z.post(p, {**body, "request_id": rid(), "reissued_as": out["reimport_break_id"]},
                  andre=ANDRE_TOKEN).status_code == 409                               # never a break id
    r = z.post(p, {**body, "request_id": rid(), "reissued_as": paid_inv["invoice_id"]}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "only a lost credit note or void" in r.text     # AEGIS r4 R4-7: bound to kind
    assert z.post(p, {**body, "request_id": rid(), "disposition": "moved"}, andre=ANDRE_TOKEN).status_code == 409
    assert z.post("/fin/v1/restore/items/fin-ddl-0000000000000000000000000000000000000000/disposition",
                  {**lost, "request_id": rid()}, andre=ANDRE_TOKEN).status_code == 404
    d = z.ok(z.post(p, {"request_id": rid(), "disposition": "lost", "reason": "re-acknowledged by phone"},
                    andre=ANDRE_TOKEN))
    assert d["ready_to_lift"] is False                                             # coverage still missing
    assert z.post(p, {**lost, "request_id": rid()}, andre=ANDRE_TOKEN).status_code == 409   # disposed of once only
    assert z.ledger.of_type("restore_item_disposed")
    for m in [i for i in items.values() if i["kind"] == "mail" and i["status"] == "needs_review"]:
        z.ok(z.post(f"/fin/v1/restore/items/{m['event_id']}/disposition",
                    {"request_id": rid(), "disposition": "lost", "reason": "acknowledged by phone instead"},
                    andre=ANDRE_TOKEN))
    _, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    assert real["failed"] == 0 and out["reimport_break_id"] in real["ready_to_lift"]
    assert z.bal("2050", f"client:{CLIENT}", "zbm") == Decimal("600.00")            # the obligation came back
    lift(z)


# --------------------------------------------------------------------------------------------- R2-1 commit marks

def test_r2_1_an_unwritten_commit_mark_alerts_and_the_job_writes_it(caplog):
    hr = Harness().ready()
    inv = ach_invoice(hr)
    hr.ledger.fail_on_type = "effect_committed"
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        hr.ok(credit(hr, inv, "100.00", refund_due_from=None), 201)             # the effect stands
    hr.ledger.fail_on_type = None
    assert any("commit_mark_unwritten" in r.getMessage() for r in caplog.records)
    assert not hr.ledger.of_type("effect_committed") and len(hr.svc._unmarked) == 2   # the note and its email
    run_mail(hr)
    assert len(hr.ledger.of_type("effect_committed")) == 2 and hr.svc._unmarked == []


def test_r2_1_a_restore_before_the_mark_is_written_asks_andre_and_books_nothing(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "900.00")
    snapshot(tmp_path)
    x.ledger.fail_on_type = "effect_committed"
    x.ok(credit(x, x.svc.db["invoices"][a["invoice_id"]], "100.00", refund_due_from=None), 201)
    x.ledger.fail_on_type = None
    z, out = _restore_cycle(x, tmp_path)                          # the pending retry was in memory: gone
    _, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    assert not z.svc.db["credit_notes"] and not any(r["action"] == "rebuild_credit_note" for r in real["results"])
    (it,) = [i for i in z.svc.db["breaks"][out["reimport_break_id"]]["items"].values() if i["kind"] == "credit_note"]
    assert it["status"] == "needs_review" and "possibly committed" in it["why"]   # its line's anchor exists
    assert z.svc.db["breaks"][out["reimport_break_id"]]["status"] == "open"


# --------------------------------------------------------------------------------------------- R2-10

def test_r2_10_refund_totals_come_only_from_verified_events(keys):  # noqa: F811
    st = make(keys, FIN_CARD_PREPAYMENTS="1")
    inv, pi = stripe_paid(st)
    dashboard_refund(st, pi, 30000)
    entries = st.svc.recorder.client.entries()
    ((i, e),) = [(n, e) for n, e in enumerate(entries) if e["event_type"] == "stripe_refund_booked"]
    rct = e["subject_id"]
    assert st.svc._refund_steps({"refunds": {rct: [(i, e)]}}, rct, pi) == [(i, "300.00")]
    forged = {**e, "summary": e["summary"].replace("total refunded 300.00", "total refunded 1200.00")}
    assert st.svc._refund_steps({"refunds": {rct: [(i, forged)]}}, rct, pi) == []
    assert st.svc._refund_steps({"refunds": {rct: [(i, {**e, "summary": "Stripe refund booked"})]}}, rct, pi) == []
