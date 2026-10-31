"""AEGIS wave B round 6 (scratchpad/night/aegis-finance-ops-waveB-r6.md), failing first with AEGIS's repros.

R6-1  a restore correction is DERIVED from the lost event (lines, amount, entity, counterparty); Andre names only
      the reference and what it settled: a payout its payables (mandatory, the payee's, adding up to the net), a
      refund its campaign (mandatory); the counterparty's fence ends only when that debt is settled (G4c, G3, G2).
R6-2  a later reconcile with nothing at risk never clears the post-restore phase (G8).
R6-3  the bank re-feed confirmation is checked day by day against the bank lines Finance booked; a bank line since
      the backup that refers to an invoice stops the 4.12(g) exception (G6b).
R6-4  not_moved for ANY payout is looked up at the rail by its idempotency key (from the lost event); not found ->
      only after 24 h, a second lookup and Andre's statement reference and date; the rail unreachable -> 503 (G5b).
R6-5  parked rail events replay in arrival order; one not applicable yet stays held (G7, both id orders).
R6-6  any money to a fenced counterparty is refused, whatever the amount (the batch is fenced by id only).
R6-7  client receipts the ledger shows sent after the backup are never sent again.
R6-8  the four surviving mutants: the counterparty fence at the choke points, the correction's amount check, the
      lookup when the rail is down, the payables' payee check.
"""

from __future__ import annotations

import pytest

from errors import Conflict
from fakes import RAIL_SIG
from helpers import ANDRE_TOKEN, Harness, rid
from test_credit_notes import ach_invoice
from test_restore_reimport import durable, lift, refeed, snapshot
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


def _feed(hr, entity, account, amount, token):
    """Feed one bank statement line to Finance (the money is already at the bank)."""
    import hashlib
    line = {"txn_ref_sha256": hashlib.sha256(rid("txn").encode()).hexdigest(), "entity": entity, "account": account,
            "direction": "credit", "amount": amount, "value_date": "2026-10-02", "reference_token": token}
    return hr.ok(hr.post("/fin/v1/bank/events", {"request_id": rid(), "lines": [line]}, caller="bank_feed"))


def _two_payee_payout(keys, tmp_path, paid=True):  # noqa: F811
    """AEGIS G4c: payables of clip-a and clip-b; run, funding and release AFTER the backup; the rail pays."""
    x = durable(keys, tmp_path)
    x.fund_campaign()
    x.payee()
    x.payee("clip-b")
    x.accrue()
    x.accrue(sid="sub-b", clipper="clip-b")                                 # the same amount as clip-a's
    x.recon()
    snapshot(tmp_path)
    b = x.run()["batch"]
    x.approve(b)
    x.fund_batch(b["batch_id"])
    x.clock.advance(hours=13)
    x.recon()
    x.release(b["batch_id"])
    if paid:
        for c in [c for c in x.stripe.calls if c[0] == "submit"]:
            x.stripe.paid(c[1])
    pays = {p["payee_id"]: p for p in x.svc.db["payables"].values()}
    z, out = _restore_cycle(x, tmp_path)
    brk = z.svc.db["breaks"][out["reimport_break_id"]]
    _full_reimport(z, brk["since"], brk["opened_on"])
    return x, z, out, b, pays


# --------------------------------------------------------------------------------------------- R6-1

def test_r6_1_a_correction_is_derived_and_bound_to_the_obligation(keys, tmp_path):  # noqa: F811
    x, z, out, b, pays = _two_payee_payout(keys, tmp_path)
    subs = [i for i in _items(z, out).values() if i["event_type"] == "item_submitted"]
    by_payee = {z.svc._derive_money(i)["payee"]: i for i in subs}
    assert set(by_payee) == {"clip-a", "clip-b"}
    for i in _items(z, out).values():
        if i["kind"] in ("money_out", "in_flight"):
            z.ok(_disp(z, i, disposition="moved", provider_ref=f"ref-{i['event_type']}"))
    lift(z)
    ia = by_payee["clip-a"]
    ref = f"ref-{ia['event_type']}"
    # G4c / G3: lines, an entity or an expense account can never be typed
    r = _correct(z, ia, ref, lines=[{"account": "5030", "subledger": None, "debit": "29.01", "credit": "0.00"}])
    assert r.status_code == 422
    r = _correct(z, ia, ref, entity="zbm")
    assert r.status_code == 422
    assert _correct(z, ia, ref).status_code == 409                           # no payables named
    assert pays["clip-b"]["current_amount"] == pays["clip-a"]["current_amount"]
    r = _correct(z, ia, ref, payable_ids=[pays["clip-b"]["payable_id"]])
    assert r.status_code == 409 and "payee" in r.text                        # another payee's, same amount (R6-8)
    assert "clip-a" in z.svc._subject_fences() and "clip-b" in z.svc._subject_fences()
    z.ok(_correct(z, ia, ref, payable_ids=[pays["clip-a"]["payable_id"]]), 201)
    assert "clip-a" not in z.svc._subject_fences()                           # its debt is settled
    assert "clip-b" in z.svc._subject_fences()                                # not booked yet: still fenced
    # the counterparty fence holds at every choke point, any amount (R6-6, R6-8)
    z.svc._lockdown = {"state": "lifted"}                                    # (look past the post phase)
    for amount in (pays["clip-b"]["current_amount"], "1.00", None):
        with pytest.raises(Conflict, match="RESTORE_FENCED"):
            z.svc._money_out_refusal("rail submit", "fin-itm-new", counterparties=("clip-b",), amount=amount)


def test_r6_1_the_moved_amount_must_be_the_lost_events(keys, tmp_path):  # noqa: F811
    x, z, out, b, pays = _two_payee_payout(keys, tmp_path)
    ia = [i for i in _items(z, out).values() if i["event_type"] == "item_submitted"][0]
    z.ok(_disp(z, ia, disposition="moved", provider_ref="ref-1"))
    bid = out["reimport_break_id"]
    tampered = {**ia, "disposition": {**z.svc.db["breaks"][bid]["items"][ia["event_id"]]["disposition"],
                                      "moved_amount": "999.99"}}
    z.svc.db["breaks"][bid] = {**z.svc.db["breaks"][bid],
                               "items": {**z.svc.db["breaks"][bid]["items"], ia["event_id"]: tampered}}
    payee = z.svc._derive_money(ia)["payee"]
    r = _correct(z, ia, "ref-1", payable_ids=[pays[payee]["payable_id"]])
    assert r.status_code == 409 and "moved" in r.text                        # R6-8: the amount check


# --------------------------------------------------------------------------------------------- R6-2

def test_r6_2_a_later_plain_reconcile_never_ends_the_post_phase():
    hr = Harness().ready()
    hr.svc._lockdown = {"state": "post_restore_reconcile_required", "ack_ledger_len": 0}
    hr.svc._lockdown_clear_after_reconcile()
    assert hr.svc.restore_post_phase() is not None
    with pytest.raises(Conflict, match="POST_RESTORE_RECONCILE_REQUIRED"):
        hr.svc._money_out_refusal("bank transfer", "t1")


# --------------------------------------------------------------------------------------------- R6-3

def test_r6_3_the_refeed_is_checked_against_booked_bank_lines_and_the_exception_rechecks(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "700.00", methods=("ach",))
    snapshot(tmp_path)
    x.receive("zbm", "1010", "700.00", a["invoice_id"])                      # ACH paid after the backup
    z, out = _restore_cycle(x, tmp_path, with_refeed=False)
    _full_reimport(z, "2026-10-02", "2026-10-02")
    refeed(z, extra=[("2026-10-02", "zbm", "1010", "700.00")], status=409)    # the statement shows it: not fed yet
    assert _void(z, z.svc.db["invoices"][a["invoice_id"]]).status_code == 409
    assert z.post("/fin/v1/restore/bank-refeed", {"request_id": rid(), "since": "2026-10-02", "until": "2026-10-02",
                                                  "statement_ref": "s", "statement_days": [
                                                      {"day": "2026-10-02", "entity": "zbc", "account": "1020",
                                                       "credit_lines": 0, "credit_total": "0.00",
                                                       "closing_balance": "0.00"}]},
                  andre=ANDRE_TOKEN).status_code == 409                       # every account of every business day
    _feed(z, "zbm", "1010", "700.00", a["invoice_id"])                       # Andre feeds the line (bank unchanged)
    refeed(z)
    r = _void(z, z.svc.db["invoices"][a["invoice_id"]])
    assert r.status_code == 409                                               # paid: never voided
    lift(z)


def test_r6_3_a_bank_line_naming_the_invoice_stops_the_exception():
    hr = Harness().ready()
    inv = ach_invoice(hr, "200.00")
    hr.svc.db["receipts"]["fin-rct-U"] = {"receipt_id": "fin-rct-U", "invoice_id": None, "status": "unapplied",
                                         "reference_token": inv["invoice_id"], "value_date": "2026-10-02",
                                         "amount": "50.00", "entity": "zbm", "into_account": "1010"}
    hr.svc.db["breaks"]["fin-brk-R"] = {"break_id": "fin-brk-R", "leg": "restore_reimport_required", "status": "open",
                                       "items": {}, "covered": [["2026-10-02", "2026-10-02"]],
                                       "since": "2026-10-02", "opened_on": "2026-10-02",
                                       "bank_refeed": {"since": "2026-10-02", "until": "2026-10-02"}}
    assert hr.svc._lockdown_exception_refusal(inv["invoice_id"]) == "a bank line since the backup refers to this " \
                                                                      "invoice"


# --------------------------------------------------------------------------------------------- R6-4

def test_r6_4_not_moved_for_any_payout_is_looked_up_and_waits_24h(keys, tmp_path):  # noqa: F811
    x, z, out, b, pays = _two_payee_payout(keys, tmp_path)
    subs = [i for i in _items(z, out).values() if i["event_type"] == "item_submitted"]
    assert all(i["ref"] not in z.svc.db["items"] for i in subs)               # the restored books never held them
    n0 = len([c for c in z.stripe.calls if c[0] == "lookup"])
    r = _disp(z, subs[0], disposition="not_moved", provider_ref="stmt", statement_date="2026-10-02")
    assert r.status_code == 409 and "record it as moved" in r.text            # G5b: the rail has it
    assert len([c for c in z.stripe.calls if c[0] == "lookup"]) == n0 + 1
    z.stripe.lookup_available = False
    assert _disp(z, subs[0], disposition="not_moved", provider_ref="s",
                 statement_date="2026-10-02").status_code == 503              # R6-8: never accepted blind
    z.stripe.lookup_available = True
    z.stripe.payouts.clear()                                                 # suppose the rail does not have it
    first = z.ok(_disp(z, subs[0], disposition="not_moved", provider_ref="s", statement_date="2026-10-02"))
    assert first["status"] == "pending_not_moved"
    again = z.ok(_disp(z, subs[0], disposition="not_moved", provider_ref="s", statement_date="2026-10-02"))
    assert again["status"] == "pending_not_moved"                             # not before 24 h
    z.clock.advance(hours=25)
    assert _disp(z, subs[0], disposition="not_moved", reason="x").status_code in (400, 422)   # the statement
    done = z.ok(_disp(z, subs[0], disposition="not_moved", provider_ref="s", statement_date="2026-10-02"))
    assert done["status"] == "disposed"
    ev = _items(z, out)[subs[0]["event_id"]]["disposition"]["evidence"]
    assert len(ev["rail_lookups"]) == 2 and ev["attestation"]["provider_ref"] == "s"


# --------------------------------------------------------------------------------------------- R6-5

@pytest.mark.parametrize("ids", [("evt-B-paid", "evt-A-returned"), ("evt-A-paid", "evt-B-returned")])
def test_r6_5_parked_events_replay_in_arrival_order(keys, tmp_path, ids):  # noqa: F811
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
    for eid, t in zip(ids, ("paid", "returned")):                            # the rail's order: paid, then returned
        ev = {"event_id": eid, "type": t, "signature": RAIL_SIG, "item_id": item["item_id"]}
        z.ok(z.post("/fin/v1/rails/stripe/events", {"request_id": rid(), "events": [ev]}, caller="rail_gateway"))
    _full_reimport(z, "2026-10-02", "2026-10-02")
    for it in [i for i in _items(z, out).values() if i["status"] == "needs_review"]:
        z.ok(_disp(z, it, disposition="lost"))
    lift(z)
    assert z.svc.db["items"][item["item_id"]]["status"] == "returned"


def test_r6_5_an_event_not_applicable_yet_stays_held():
    hr = Harness().ready()
    hr.svc.db["items"]["fin-itm-1"] = {"item_id": "fin-itm-1", "status": "submitted", "payable_ids": [],
                                      "batch_id": "b", "payee_id": "clip-a", "net": "1.00", "rail": "stripe"}
    hr.svc.db["rail_events"]["stripe|e1"] = {"event_id": "e1", "rail": "stripe", "type": "returned",
                                            "item_id": "fin-itm-1", "status": "held_submitting", "held": True,
                                            "restore_parked": True, "parked_seq": 1}
    hr.svc.held_by_item["fin-itm-1"] = {"stripe|e1"}
    hr.svc.replay_held_rail_events("t")
    r = hr.svc.db["rail_events"]["stripe|e1"]
    assert r["held"] is True and r["last_try"].startswith("ignored_")


# --------------------------------------------------------------------------------------------- R6-7

def test_r6_7_client_receipts_sent_after_the_backup_are_not_sent_again():
    hr = Harness().ready()
    hr.svc.db["client_receipts"]["fin-crc-1"] = {"client_receipt_id": "fin-crc-1", "status": "pending_send",
                                                "client_id": "c", "created_at": "2026-10-02T00:00:00Z"}
    hr.svc.db["breaks"]["fin-brk-OLD"] = {"break_id": "fin-brk-OLD", "leg": "restore_reimport_required",
                                         "status": "resolved", "mail_sent": ["fin-crc-1"]}
    assert hr.svc._restore_mail_dedupe("t") == 1
    assert hr.svc.db["client_receipts"]["fin-crc-1"]["status"] == "sent"


# --------------------------------------------------------------------------------------------- R6-8 choke points

def test_r6_8_the_counterparty_fence_holds_at_the_bank_and_rail_choke_points():
    hr = Harness().ready()
    svc = hr.svc
    svc.db["breaks"]["fin-brk-OLD"] = {"break_id": "fin-brk-OLD", "leg": "restore_reimport_required",
                                      "status": "resolved", "items": {"e1": {
                                          "event_id": "e1", "kind": "money_out", "ref": "fin-x", "status": "disposed",
                                          "event_type": "item_submitted",
                                          "disposition": {"disposition": "moved", "counterparties": ["clip-a", "camp-9"]}}}}
    svc.db["items"]["fin-itm-2"] = {"item_id": "fin-itm-2", "status": "approved", "batch_id": "fin-bat-2",
                                   "payee_id": "clip-a", "net": "3.00", "rail": "stripe"}
    with pytest.raises(Conflict, match="RESTORE_FENCED"):
        svc._submit_item("fin-itm-2", "scheduler")
    svc.db["refunds"]["fin-rfd-2"] = {"refund_id": "fin-rfd-2", "campaign_id": "camp-9", "client_id": "client-9",
                                     "amount": "8.00", "status": "approved"}
    svc.db["treasury_ops"]["fin-trx-2"] = {"op_id": "fin-trx-2", "status": "approved", "kind": "refund_payment",
                                          "ref_id": "fin-rfd-2", "amount": "8.00"}
    with pytest.raises(Conflict, match="RESTORE_FENCED"):
        svc._execute_transfer("fin-trx-2")
    assert not [c for c in hr.stripe.calls if c[0] == "submit"]


def test_r6_4_a_release_without_any_known_key_cannot_be_not_moved():
    hr = _bare_locked()
    hr.svc.db["breaks"]["fin-brk-R"] = {"break_id": "fin-brk-R", "leg": "restore_reimport_required", "status": "open",
                                       "items": {"e1": {"event_id": "e1", "kind": "money_out", "ref": "fin-bat-L",
                                                        "event_type": "batch_release_started",
                                                        "status": "needs_review"}}}
    r = hr.post(DISP.format("e1"), {"request_id": rid(), "disposition": "not_moved", "reason": "x",
                                    "provider_ref": "s", "statement_date": "2026-10-02"}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "idempotency key" in r.text


def _bare_locked():
    hr = Harness().ready()
    hr.svc._lockdown = {"state": "on", "detected": False, "build_incomplete": False}
    return hr
