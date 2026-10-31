"""AEGIS wave B round 5 (scratchpad/night/aegis-finance-ops-waveB-r5.md), failing first: AFTER the lift.

R5-1  after the lift money out waits for a clean reconciliation run AFTER the lift (post_restore_reconcile_required);
      money recorded as moved fences its SUBJECT (counterparty and amount): a refund re-proposed under a new id is
      refused.
R5-2  a restore correction (memo FRC, Andre, bound to a moved item, its provider reference and amount) books what
      moved, settles the payables or the refund, resolves the reconciliation breaks; end to end: a lost payout ->
      moved -> corrections -> a clean reconciliation -> payouts and refunds work again, nothing paid twice.
R5-3  not_moved needs evidence (the rail is looked up; bank money needs an attestation); a restore-touched item is
      looked up at the rail before any submit.
R5-4  the MSA 4.12(g) exception needs the invoice untouched by pages after the backup, the Stripe window covered and
      the bank re-feed confirmed; the lift needs the re-feed too.
R5-5  rail events during a lockdown are parked (held) and applied at the lift; destination_changed applies at once;
      an event for an item the restored books lack becomes a restore item.
R5-6  vendor payments of a fenced media buy are refused.
R5-7  mutant gaps (drive fence, exception fence, lift effects, refund_paid, reissue kinds, reissued amount).
R5-8  no client email while locked; mail the ledger shows as sent after the backup is never sent again.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import mail as MAIL
from errors import Conflict
from helpers import ANDRE_TOKEN, Harness, rid
from test_credit_notes import ach_invoice, void
from test_finance_ops_r4 import _locked_harness
from test_restore_reimport import durable, lift, pay, refeed, snapshot
from test_restore_rebuild import _full_reimport, _restore_cycle, _void
from test_stripe_incoming import keys, rr_invoice  # noqa: F401

DISP = "/fin/v1/restore/items/{}/disposition"


def _items(z, out):
    return z.svc.db["breaks"][out["reimport_break_id"]]["items"]


def _moved(z, it, amount, counterparty, ref):
    return z.ok(z.post(DISP.format(it["event_id"]), {"request_id": rid(), "disposition": "moved", "reason": "seen",
                                                    "provider_ref": ref, "moved_amount": amount,
                                                    "counterparty": counterparty}, andre=ANDRE_TOKEN))


def _correct(z, it, ref, lines, **kw):
    return z.post(f"/fin/v1/restore/items/{it['event_id']}/correction",
                  {"request_id": rid(), "entity": "zbc", "provider_ref": ref, "lines": lines, **kw}, andre=ANDRE_TOKEN)


def _ln(account, sub, debit="0.00", credit="0.00"):
    return {"account": account, "subledger": sub, "debit": debit, "credit": credit}


# --------------------------------------------------------------------------------------------- R5-1 / R5-2 payout

def test_r5_2_a_lost_payout_is_booked_by_corrections_and_payouts_work_again_nothing_twice(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    x.fund_campaign()
    x.payee()
    x.accrue()
    x.recon()
    snapshot(tmp_path)                                                       # the backup BEFORE the run
    b = x.run()["batch"]
    x.approve(b)
    x.fund_batch(b["batch_id"])
    x.clock.advance(hours=13)
    x.recon()
    x.release(b["batch_id"])
    subs = [c for c in x.stripe.calls if c[0] == "submit"]
    x.stripe.paid(subs[0][1])                                                # the rail paid the creator
    (pay_,) = x.svc.db["payables"].values()
    amt, payee = pay_["current_amount"], pay_["payee_id"]
    z, out = _restore_cycle(x, tmp_path)
    brk = z.svc.db["breaks"][out["reimport_break_id"]]
    _full_reimport(z, brk["since"], brk["opened_on"])
    its = {i["event_type"]: i for i in _items(z, out).values() if i["kind"] == "money_out"}
    assert {"treasury_posting_committed", "batch_release_started", "item_submitted"} <= set(its)
    _moved(z, its["treasury_posting_committed"], amt, b["batch_id"], "bank-ref-1")
    _moved(z, its["batch_release_started"], amt, b["batch_id"], "rail-ref-1")
    _moved(z, its["item_submitted"], amt, payee, "rail-ref-1")
    lift(z)
    assert z.svc.lockdown_status()["post_restore_reconcile_required"] is True
    rec = z.recon()
    assert rec["post_restore_clean"] is False                               # the books are behind the bank and rail
    # money out stays refused until a clean reconciliation after the lift
    with pytest.raises(Conflict, match="POST_RESTORE_RECONCILE_REQUIRED"):
        z.svc._money_out_refusal("bank transfer", "x")
    # the corrections: the funding transfer, then the payout (its payables settled)
    bad = _correct(z, its["item_submitted"], "rail-ref-WRONG", [_ln("2020", f"payee:{payee}", debit=amt),
                                                                _ln("1040", None, credit=amt)])
    assert bad.status_code == 409                                            # not the recorded reference
    bad = _correct(z, its["item_submitted"], "rail-ref-1", [_ln("2020", f"payee:{payee}", debit="1.00"),
                                                            _ln("1040", None, credit="1.00")])
    assert bad.status_code == 409                                            # not the moved amount
    c1 = z.ok(_correct(z, its["treasury_posting_committed"], "bank-ref-1",
                       [_ln("1040", None, debit=amt), _ln("1020", None, credit=amt)]), 201)
    c2 = z.ok(_correct(z, its["item_submitted"], "rail-ref-1",
                       [_ln("2020", f"payee:{payee}", debit=amt), _ln("1040", None, credit=amt)],
                       payable_ids=[pay_["payable_id"]]), 201)
    assert c1["breaks_resolved"] or c2["breaks_resolved"]
    assert _correct(z, its["item_submitted"], "rail-ref-1", [_ln("2020", f"payee:{payee}", debit=amt),
                                                              _ln("1040", None, credit=amt)]).status_code == 409
    assert z.svc.db["payables"][pay_["payable_id"]]["status"] == "settled"
    rec = z.recon()
    assert rec["post_restore_clean"] is True, ([l for l in rec["recon"]["legs"] if l["status"] != "matched"], [b for b in z.svc.db["breaks"].values() if b["status"] != "resolved"])
    assert not z.svc.lockdown_status()["post_restore_reconcile_required"]
    # the lost batch is never released again; a NEW payout works
    r = z.post(f"/fin/v1/payout-batches/{b['batch_id']}/release", {"request_id": rid()}, caller="scheduler")
    assert r.status_code in (404, 409)
    z.accrue(sid="sub-2")
    z.recon()
    b2 = z.run()["batch"]
    z.approve(b2)
    z.fund_batch(b2["batch_id"])
    z.clock.advance(hours=13)
    z.recon()
    z.release(b2["batch_id"])
    new = [c for c in z.stripe.calls if c[0] == "submit"][len(subs):]
    assert new and all(c[1] != subs[0][1] for c in new)                     # only the new payout went out
    assert len({c[3] for c in new}) == len(new)


# --------------------------------------------------------------------------------------------- R5-1 refund

def test_r5_1_a_lost_refund_is_fenced_by_subject_and_booked_by_a_correction(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    x.fund_campaign()
    x.payee()
    x.accrue()
    x.recon()
    snapshot(tmp_path)
    rf = x.ok(x.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler"), 201)["refund"]
    x.ok(x.post(f"/fin/v1/refunds/{rf['refund_id']}/decision",
                {"request_id": rid(), "content_sha256": rf["content_sha256"], "decision": "approve"}, andre=ANDRE_TOKEN))
    n1 = len(x.bank.transfers)
    z, out = _restore_cycle(x, tmp_path)
    brk = z.svc.db["breaks"][out["reimport_break_id"]]
    _full_reimport(z, brk["since"], brk["opened_on"])
    its = {i["event_type"]: i for i in _items(z, out).values() if i["kind"] in ("money_out", "in_flight")}
    assert "refund_paid" in its                                              # R5-7: refund_paid is money out
    for it in its.values():
        _moved(z, it, rf["amount"], "camp-1", "bank-ref-9")
    lift(z)
    r = z.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler")
    assert r.status_code in (409, 422) and "RESTORE_FENCED" in r.text       # a new id, the same refund: refused
    c = z.ok(_correct(z, its["refund_paid"], "bank-ref-9",
                      [_ln("2010", "campaign:camp-1", debit=rf["amount"]), _ln("1020", None, credit=rf["amount"])],
                      refund_campaign_id="camp-1"), 201)
    rec = z.recon()
    assert rec["post_restore_clean"] is True, (c, rec["recon"]["legs"])
    assert len(z.bank.transfers) == n1                                       # paid once, ever


# --------------------------------------------------------------------------------------------- R5-3

def test_r5_3_not_moved_is_looked_up_at_the_rail_and_a_touched_item_is_looked_up_first(keys, tmp_path):  # noqa: F811
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
    snapshot(tmp_path)
    x.release(b["batch_id"])                                                 # sent after the backup
    z, out = _restore_cycle(x, tmp_path)
    its = [i for i in _items(z, out).values() if i["event_type"] in ("item_submitted", "batch_release_started")]
    for it in its:
        r = z.post(DISP.format(it["event_id"]), {"request_id": rid(), "disposition": "not_moved", "reason": "x"},
                   andre=ANDRE_TOKEN)
        assert r.status_code == 409 and "record it as moved" in r.text       # the rail has it
    # bank money: not_moved needs Andre's attestation
    hr = _locked_harness()
    hr.svc.db["breaks"]["fin-brk-R"] = {"break_id": "fin-brk-R", "leg": "restore_reimport_required", "status": "open",
                                       "items": {"e1": {"event_id": "e1", "kind": "money_out", "ref": "fin-trx-1",
                                                        "event_type": "treasury_posting_committed",
                                                        "status": "needs_review"}}}
    r = hr.post(DISP.format("e1"), {"request_id": rid(), "disposition": "not_moved", "reason": "x"}, andre=ANDRE_TOKEN)
    assert r.status_code in (400, 422) and "attestation" in r.text


def test_r5_3_a_restore_touched_item_is_looked_up_before_any_submit():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.payee()
    hr.accrue()
    hr.recon()
    b = hr.run()["batch"]
    hr.approve(b)
    hr.fund_batch(b["batch_id"])
    hr.clock.advance(hours=13)
    hr.recon()
    hr.svc.db["breaks"]["fin-brk-OLD"] = {"break_id": "fin-brk-OLD", "leg": "restore_reimport_required",
                                         "status": "resolved", "items": {"e1": {
                                             "event_id": "e1", "kind": "money_out", "ref": b["batch_id"],
                                             "event_type": "batch_release_started", "status": "disposed",
                                             "disposition": {"disposition": "not_moved"}}}}
    hr.release(b["batch_id"])
    calls = [c[0] for c in hr.stripe.calls if c[0] in ("lookup", "submit")]
    assert calls[:2] == ["lookup", "submit"]                                 # looked up first, then sent once


# --------------------------------------------------------------------------------------------- R5-4

def test_r5_4_the_unpaid_exception_needs_untouched_covered_refed_and_the_lift_needs_the_refeed(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    paid_later = rr_invoice(x, "700.00")
    plain = rr_invoice(x, "300.00")
    snapshot(tmp_path)
    pay(x, paid_later)                                                       # page made and paid after the backup
    z, out = _restore_cycle(x, tmp_path, with_refeed=False)
    assert z.svc.db["invoices"][paid_later["invoice_id"]]["status"] == "issued"   # the restored view
    r = void(z, z.svc.db["invoices"][plain["invoice_id"]])
    assert r.status_code == 409 and "not covered" in r.text                  # the window first
    _full_reimport(z, "2026-10-02", "2026-10-02")
    r = void(z, z.svc.db["invoices"][plain["invoice_id"]])
    assert r.status_code == 409 and "fed again" in r.text                    # then the bank re-feed
    lift(z, status=409, with_refeed=False)
    refeed(z)
    z.ok(void(z, z.svc.db["invoices"][plain["invoice_id"]]))
    # an invoice a page touched after the backup is never voided under the exception (it was paid)
    hr = Harness().ready()
    inv = ach_invoice(hr, "200.00")
    hr.svc.db["breaks"]["fin-brk-R"] = {"break_id": "fin-brk-R", "leg": "restore_reimport_required", "status": "open",
                                       "items": {}, "affected_invoices": [inv["invoice_id"]], "covered": [],
                                       "since": "2026-10-02", "opened_on": "2026-10-02"}
    r = void(hr, hr.svc.db["invoices"][inv["invoice_id"]])
    assert r.status_code == 409 and "after the backup" in r.text


# --------------------------------------------------------------------------------------------- R5-5

def test_r5_5_rail_events_are_parked_during_the_lockdown_and_applied_at_the_lift(keys, tmp_path):  # noqa: F811
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
    x.ok(_void(x, x.svc.db["invoices"][a["invoice_id"]]))                    # something to restore
    z, out = _restore_cycle(x, tmp_path)
    assert z.svc.db["items"][item["item_id"]]["status"] == "submitted"
    r = z.rail_event("paid", item["item_id"])
    assert r["results"][0]["status"] == "held_submitting"                    # parked
    assert z.svc.db["items"][item["item_id"]]["status"] == "submitted"       # nothing applied yet
    ref = z.svc.db["payees"]["clip-a"]["rail_account_ref"]
    z.rail_event("destination_changed", account_ref=ref, new_destination_fingerprint="fp-new-destination")
    assert z.svc.db["payees"]["clip-a"].get("hold") or z.svc.db["payees"]["clip-a"].get("holds") \
        or z.svc.db["payees"]["clip-a"].get("status") in ("hold", "on_hold")  # applied at once
    z.rail_event("paid", "fin-itm-LOSTBYTHERESTORE")
    lost = [i for i in _items(z, out).values() if i.get("ref") == "fin-itm-LOSTBYTHERESTORE"]
    assert lost and lost[0]["status"] == "needs_review"
    z.ok(z.post(DISP.format(lost[0]["event_id"]), {"request_id": rid(), "disposition": "moved", "reason": "rail",
                                                  "provider_ref": "po_9", "moved_amount": "1.00",
                                                  "counterparty": "clip-a"}, andre=ANDRE_TOKEN))
    _full_reimport(z, "2026-10-02", "2026-10-02")
    for it in [i for i in _items(z, out).values() if i["status"] == "needs_review"]:
        z.ok(z.post(DISP.format(it["event_id"]), {"request_id": rid(), "disposition": "lost", "reason": "x"},
                    andre=ANDRE_TOKEN))
    lift(z)
    assert z.svc.db["items"][item["item_id"]]["status"] == "paid"            # applied at the lift


# --------------------------------------------------------------------------------------------- R5-6 / R5-7

def test_r5_6_and_r5_7_fences_and_bindings_below_the_api():
    hr = Harness().ready()
    svc = hr.svc
    svc.db["breaks"]["fin-brk-OLD"] = {
        "break_id": "fin-brk-OLD", "leg": "restore_reimport_required", "status": "resolved", "fenced": ["fin-mb-1",
                                                                                                      "fin-itm-9"],
        "items": {"e1": {"event_id": "e1", "kind": "money_out", "ref": "fin-mb-1", "status": "disposed",
                         "disposition": {"disposition": "moved", "counterparty": "fin-mb-1", "moved_amount": "50.00"}}}}
    with pytest.raises(Conflict, match="RESTORE_FENCED"):
        svc._money_out_refusal("vendor payment", "fin-mb-1", counterparties=("fin-mb-1",), amount=Decimal("50.00"))
    r = hr.post("/fin/v1/media-buys/fin-mb-1/vendor-payments",
                {"request_id": rid(), "amount": "50.00", "paid_on": "2026-10-02", "method": "ach",
                 "payment_ref_sha256": "a" * 64}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "RESTORE_FENCED" in r.text               # R5-6
    # R5-7: drive_open_items never drives a fenced item
    svc.db["items"]["fin-itm-9"] = {"item_id": "fin-itm-9", "status": "submitting", "batch_id": "fin-bat-9",
                                   "rail": "stripe", "idempotency_key": "k9", "payee_id": "clip-a", "net": "1.00",
                                   "first_submitted_at": "2026-10-01T00:00:00Z"}
    assert svc.drive_open_items("x")["items"] == []
    assert svc._drive_item("x", svc.db["items"]["fin-itm-9"])["fenced"] is True   # also when called directly
    assert not [c for c in hr.stripe.calls if c[0] in ("lookup", "submit")]
    # R5-7: the exception refuses a fenced invoice
    inv = ach_invoice(hr, "200.00")
    svc.db["breaks"]["fin-brk-OLD"]["fenced"].append(inv["invoice_id"])
    assert svc._lockdown_exception_refusal(inv["invoice_id"]) == "money moved on this invoice after the backup"
    # R5-7: reissued_as needs at least the item's amount
    svc.db["credit_notes"]["fin-cn-S"] = {"credit_note_id": "fin-cn-S", "invoice_id": inv["invoice_id"],
                                         "amount": "10.00", "to_refund": "0.00"}
    with pytest.raises(Conflict, match="at least the same amount"):
        svc._check_reissued_as({"kind": "credit_note", "invoice_id": inv["invoice_id"], "amount": "600.00",
                                "to_refund": "0.00", "order": 0}, "fin-cn-S")


def test_r5_7_reissue_kinds_and_lift_effects(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    snapshot(tmp_path)
    x.ok(x.post("/fin/v1/clients/zbm-client-7/acknowledgements",
                {"request_id": rid(), "topic": "refund_request", "received_on": "2026-10-02"}, andre=ANDRE_TOKEN), 201)
    z, out = _restore_cycle(x, tmp_path)
    (ddl,) = [i for i in _items(z, out).values() if i["kind"] == "deadline"]
    r = z.post(DISP.format(ddl["event_id"]), {"request_id": rid(), "disposition": "reissue_after_lift", "reason": "x"},
               andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "only a credit note or a void" in r.text
    for it in [i for i in _items(z, out).values() if i["status"] == "needs_review"]:
        z.ok(z.post(DISP.format(it["event_id"]), {"request_id": rid(), "disposition": "lost", "reason": "x"},
                    andre=ANDRE_TOKEN))
    _full_reimport(z, "2026-10-02", "2026-10-02")
    # a void made during the lockdown on an invoice a restore item touches stops the lift
    bid = out["reimport_break_id"]
    z.svc.db["invoices"]["fin-inv-T"] = {"invoice_id": "fin-inv-T", "voided_during_lockdown": True, "status": "void"}
    items = dict(z.svc.db["breaks"][bid]["items"])
    items["x"] = {"event_id": "x", "kind": "void", "invoice_id": "fin-inv-T", "status": "disposed"}
    z.svc.db["breaks"][bid] = {**z.svc.db["breaks"][bid], "items": items}
    r = z.post("/fin/v1/restore/lockdown/lift", {"request_id": rid(), "reason": "x"}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "touches a restore item" in r.text


# --------------------------------------------------------------------------------------------- R5-8

def test_r5_8_no_client_mail_while_locked_and_mail_sent_after_the_backup_is_never_resent():
    hr = _locked_harness()
    fx = MAIL.FixtureClientMail()
    hr.svc.ports.client_mail = fx
    hr.svc.db["mail_outbox"]["fin-mail-1"] = {"mail_id": "fin-mail-1", "kind": "acknowledgement",
                                             "client_id": "zbm-client-7", "ref": "r", "data": {}, "status": "pending",
                                             "created_at": "2026-10-02T00:00:00Z", "attempts": 0}
    out = hr.ok(hr.post("/fin/v1/jobs/client-mail/run", {"request_id": rid()}, caller="ops_cron"))
    assert out["summary"].get("restore_lockdown") is True and fx.delivered == []
    hr.svc._lockdown = None
    hr.svc.db["breaks"]["fin-brk-OLD"] = {"break_id": "fin-brk-OLD", "leg": "restore_reimport_required",
                                         "status": "resolved", "mail_sent": ["fin-mail-1"]}
    hr.ok(hr.post("/fin/v1/jobs/client-mail/run", {"request_id": rid()}, caller="ops_cron"))
    assert fx.delivered == [] and hr.svc.db["mail_outbox"]["fin-mail-1"]["status"] == "sent"
    assert hr.svc.db["mail_outbox"]["fin-mail-1"]["sent_before_restore"] is True
