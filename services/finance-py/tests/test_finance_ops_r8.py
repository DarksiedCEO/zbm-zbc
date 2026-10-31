"""AEGIS wave B round 8 (scratchpad/night/aegis-finance-ops-waveB-r8.md), failing first with AEGIS's repros. The models
of production use the REAL stand-in ports (``NotWiredBank``, ``NotWiredRail``), as ``ports.Ports`` wires them.

R8-1  with no bank feed, the re-feed takes Andre's declared closing balances (statement reference + evidence SHA-256),
      recorded as declared; the lift, the 4.12(g) exception and the post-lift reconciliation use them.
R8-2  an old-format refund and its transfer are ONE movement (one reference or two).
R8-3  a manual booking is bound to the item: counterparty, rail, amount, held balance, certified submissions.
R8-4  a held payout item's own (batched) payables are booked without waiting for the approval to expire.
R8-5  a mistaken ``moved`` is undone by Andre (no active booking) and disposed of again.
R8-6  a booking resolves only the breaks it explains.
R8-7  a refund of a campaign created after the backup: the documented exit ends with clean books.
R8-8  ``_movement_booked`` is the only guard across two restore breaks.
R8-9  the sent-mail index reads the ledger once, not per send.
Plus the surviving mutants of AEGIS r8 section 4.
"""

from __future__ import annotations

import contextlib
import hashlib

import pytest

import restore_evidence as RE
from helpers import ANDRE_TOKEN, Harness, rid
from intelligences import i04_payout_run as I4
from ports import NotWiredBank, NotWiredRail
from test_restore_reimport import durable, lift, pay, refeed, snapshot, statement_days
from test_restore_rebuild import _full_reimport, _restore_cycle, _void
from test_stripe_incoming import keys, rr_invoice  # noqa: F401

DISP = "/fin/v1/restore/items/{}/disposition"


def _items(z, out):
    return z.svc.db["breaks"][out["reimport_break_id"]]["items"]


def _money(z, out):
    return [i for i in _items(z, out).values() if i["kind"] in ("money_out", "in_flight")]


def _disp(z, it, **body):
    return z.post(DISP.format(it["event_id"]), {"request_id": rid(), "reason": "checked", **body}, andre=ANDRE_TOKEN)


def _correct(z, it, ref, **kw):
    return z.post(f"/fin/v1/restore/items/{it['event_id']}/correction", {"request_id": rid(), "provider_ref": ref, **kw},
                  andre=ANDRE_TOKEN)


def _manual(z, it, ref, amount, cp, **kw):
    return z.post(f"/fin/v1/restore/items/{it['event_id']}/manual-booking",
                  {"request_id": rid(), "provider_ref": ref, "amount": amount, "counterparty": cp,
                   "reason": "statement", "evidence_sha256": "e" * 64, **kw}, andre=ANDRE_TOKEN)


def _reverse(z, it):
    return z.post(f"/fin/v1/restore/items/{it['event_id']}/correction/reversal",
                  {"request_id": rid(), "reason": "booked in error"}, andre=ANDRE_TOKEN)


def _undo(z, it, andre=ANDRE_TOKEN):
    return z.post(f"/fin/v1/restore/items/{it['event_id']}/disposition/undo",
                  {"request_id": rid(), "reason": "the rail never paid it"}, andre=andre)


def _set_summary(z, out, it, summary):
    bid = out["reimport_break_id"]
    b = z.svc.db["breaks"][bid]
    z.svc.db["breaks"][bid] = {**b, "items": {**b["items"], it["event_id"]: {**it, "summary": summary}}}
    return z.svc.db["breaks"][bid]["items"][it["event_id"]]


@contextlib.contextmanager
def old_formats():
    """The summary formats of f6562e07 (events written before r7): AEGIS r8's deviation (b) model."""
    mp = pytest.MonkeyPatch()

    def item_old(net, period, payee, rail, batch, seq):
        key = I4.idempotency_key(batch, payee, period)
        return f"Payout item submitted to {rail}: {net} payee {payee} batch {batch} key {key} line {seq}"

    def xfer_old(kind, attempt, flow, amount, dr, cr, ref_id, seq):
        return (f"{kind} posted (attempt {attempt}) {flow} {amount} dr {dr[0]} {dr[1] or '-'} cr {cr[0]} "
                f"{cr[1] or '-'} ref {ref_id or '-'} line {seq}")

    def refund_old(amount, campaign, client, seq):
        return f"Refund paid: {amount} campaign {campaign} client {client} line {seq}"

    mp.setattr(RE, "item_summary", item_old)
    mp.setattr(RE, "xfer_summary", xfer_old)
    mp.setattr(RE, "refund_paid_summary", refund_old)
    try:
        yield
    finally:
        mp.undo()


def _production_ports(z):
    z.svc.ports.bank = NotWiredBank()                                   # ports.Ports: what production wires
    z.svc.ports.rails = {"stripe": NotWiredRail("stripe"), "trolley": NotWiredRail("trolley")}


# --------------------------------------------------------------------------------------------- R8-1

@pytest.mark.parametrize("trigger", ["stripe_payment", "void"])
def test_r8_1_production_wiring_restore_lift_reconcile_and_payout(keys, tmp_path, trigger):  # noqa: F811
    x = durable(keys, tmp_path)
    x.fund_campaign()
    x.payee()
    x.accrue()
    a = rr_invoice(x, "1000.00")
    c = rr_invoice(x, "300.00")
    snapshot(tmp_path)
    if trigger == "stripe_payment":
        pay(x, a)                                                         # the first paid client, after the backup
    else:
        x.ok(_void(x, x.svc.db["invoices"][a["invoice_id"]]))
    z, out = _restore_cycle(x, tmp_path, with_refeed=False)
    brk = z.svc.db["breaks"][out["reimport_break_id"]]
    days = statement_days(z, brk["since"], brk["opened_on"])           # Andre's statement (read from the fake bank)
    _production_ports(z)
    _full_reimport(z, brk["since"], brk["opened_on"])
    for it in [i for i in _items(z, out).values() if i["status"] == "needs_review"]:
        z.ok(_disp(z, it, disposition="lost"))
    body = {"request_id": rid(), "since": brk["since"], "until": brk["opened_on"], "statement_ref": "stmt-10",
            "statement_days": days}
    r = z.post("/fin/v1/restore/bank-refeed", body, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "evidence_sha256" in r.text           # declared figures need the statement's evidence
    wrong = [{**d, "closing_balance": "1.00"} if d["day"] == brk["opened_on"] else d for d in days]
    r = z.post("/fin/v1/restore/bank-refeed", {**body, "request_id": rid(), "statement_days": wrong,
                                               "evidence_sha256": "5" * 64}, andre=ANDRE_TOKEN)
    assert r.status_code == 409                                           # a declared balance the books contradict
    rf = z.ok(z.post("/fin/v1/restore/bank-refeed", {**body, "request_id": rid(), "evidence_sha256": "5" * 64},
                     andre=ANDRE_TOKEN))["bank_refeed"]
    assert rf["balance_source"] == "declared_by_andre" and rf["evidence_sha256"] == "5" * 64
    ev = [e for e in z.ledger.events if e["event_type"] == "restore_bank_refeed_confirmed"]
    assert ev and "declared" in str(ev[-1])
    z.ok(_void(z, z.svc.db["invoices"][c["invoice_id"]]))                # MSA 4.12(g) during the lockdown
    lift(z, with_refeed=False)
    rec = z.recon()
    legs = {(lg["leg"], lg["subject"]): lg for lg in rec["recon"]["legs"]}
    assert legs[("L1", "zbc:1020")]["status"] == "matched" and "declared by Andre" in legs[("L1", "zbc:1020")]["note"]
    assert rec["post_restore_clean"] is True and z.svc.restore_post_phase() is None
    # the payout path: nothing restore-related holds it; what refuses it is that no bank / rail is wired
    z.svc._money_out_refusal("payout", "x", counterparties=("clip-a",))
    r = z.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, caller="scheduler")
    assert "RESTORE" not in r.text and "POST_RESTORE" not in r.text
    assert r.status_code in (200, 201) or "not wired" in r.text or "unavailable" in r.text, r.text
    # after the phase the declared figures are no longer used: the bank leg is unavailable, as before any restore
    assert z.svc._declared_balance("zbc", "1020") is None


# --------------------------------------------------------------------------------------------- R8-2

def _old_refund(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    x.fund_campaign()
    x.fund_campaign("camp-2", "client-2")
    x.payee()
    x.accrue()
    x.recon()
    snapshot(tmp_path)
    with old_formats():
        rf = x.ok(x.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler"), 201)["refund"]
        x.ok(x.post(f"/fin/v1/refunds/{rf['refund_id']}/decision",
                    {"request_id": rid(), "content_sha256": rf["content_sha256"], "decision": "approve"},
                    andre=ANDRE_TOKEN))
    n1 = len(x.bank.transfers)
    z, out = _restore_cycle(x, tmp_path, with_refeed=False)
    brk = z.svc.db["breaks"][out["reimport_break_id"]]
    _full_reimport(z, brk["since"], brk["opened_on"])
    its = {i["event_type"]: i for i in _money(z, out)}
    return x, z, out, rf, its, n1


@pytest.mark.parametrize("refs", ["one", "two"])
def test_r8_2_an_old_format_refund_and_its_transfer_are_one_movement(keys, tmp_path, refs):  # noqa: F811
    x, z, out, rf, its, n1 = _old_refund(keys, tmp_path)
    rp, tx = its["refund_paid"], its["treasury_posting_committed"]
    assert z.svc._derive_money(rp) is None and z.svc._derive_money(tx) is None        # both old: nothing derives
    assert tx["status"] == "follows_movement" and tx["primary"] == rp["event_id"]
    amt = rf["amount"]
    z.ok(_disp(z, rp, disposition="moved", provider_ref="bank-ref-9", moved_amount=amt, counterparty="camp-1"))
    r = _disp(z, tx, disposition="moved", provider_ref="bank-ref-9" if refs == "one" else "bank-ref-9b",
              moved_amount=amt, counterparty="client-1")
    assert r.status_code == 409                                              # it follows its refund
    z.ok(_manual(z, rp, "bank-ref-9", amt, "camp-1"), 201)
    assert _manual(z, tx, "bank-ref-9b", amt, "client-1", rail="stripe", transfer_kind="funding").status_code == 409
    assert len([e for e in z.svc.entries if e["memo_code"] == "FRC"]) == 1
    lift(z)                                                                  # never permanent
    assert f'{z.svc.bal("1020"):.2f}' == x.bank.balance("zbc", "1020").balance
    assert len(z.bank.transfers) == n1


# --------------------------------------------------------------------------------------------- R8-3

def test_r8_3_a_manual_refund_is_bound_to_the_campaign_and_its_held_balance(keys, tmp_path):  # noqa: F811
    x, z, out, rf, its, n1 = _old_refund(keys, tmp_path)
    rp, amt = its["refund_paid"], rf["amount"]
    r = _disp(z, rp, disposition="moved", provider_ref="bank-ref-9", moved_amount=amt, counterparty="camp-2")
    assert r.status_code == 409 and "camp-1" in r.text                      # AEGIS C2: the lost event names camp-1
    r = _disp(z, rp, disposition="moved", provider_ref="bank-ref-9", moved_amount="50000.00", counterparty="camp-1")
    assert r.status_code == 409                                              # more than the event shows
    bare = _set_summary(z, out, rp, "Refund paid")                          # nothing left to read from the event
    z.ok(_disp(z, bare, disposition="moved", provider_ref="bank-ref-9", moved_amount="50000.00",
               counterparty="camp-1"))
    r = _manual(z, bare, "bank-ref-9", "50000.00", "camp-1")
    assert r.status_code == 409 and "holds" in r.text                       # AEGIS C3: the campaign's held balance
    assert not [e for e in z.svc.entries if e["memo_code"] == "FRC"]


def test_r8_3_the_counterparty_must_be_the_dispositions(keys, tmp_path):  # noqa: F811
    x, z, out, rf, its, n1 = _old_refund(keys, tmp_path)
    bare = _set_summary(z, out, its["refund_paid"], "Refund paid")
    z.ok(_disp(z, bare, disposition="moved", provider_ref="bank-ref-9", moved_amount="10.00", counterparty="camp-1"))
    r = _manual(z, bare, "bank-ref-9", "10.00", "camp-2")
    assert r.status_code == 409 and "disposition" in r.text


def _old_payout(keys, tmp_path, accrue_after=False):  # noqa: F811
    x = durable(keys, tmp_path)
    x.fund_campaign()
    x.payee()
    if not accrue_after:
        x.accrue()
    x.recon()
    snapshot(tmp_path)
    with old_formats():
        if accrue_after:
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
    for i in _money(z, out):
        if i["status"] == "needs_review":
            extra = {} if i["event_type"] == "batch_release_started" else {
                "moved_amount": p["current_amount"], "counterparty": "clip-a"}
            z.ok(_disp(z, i, disposition="moved", provider_ref=f"ref-{i['event_type']}", **extra))
    (sub,) = [i for i in _money(z, out) if i["event_type"] == "item_submitted"]
    return x, z, out, sub, p


def test_r8_3_a_manual_payout_is_bound_to_its_rail_and_certified_submissions(keys, tmp_path):  # noqa: F811
    x, z, out, sub, p = _old_payout(keys, tmp_path, accrue_after=True)
    amt = p["current_amount"]
    assert _manual(z, sub, "ref-item_submitted", amt, "clip-a", submission_ids=["sub-1"]).status_code == 409  # no rail
    r = _manual(z, sub, "ref-item_submitted", amt, "clip-a", rail="trolley", submission_ids=["sub-1"])
    assert r.status_code == 409 and "stripe" in r.text                      # AEGIS E3: the event was on stripe
    r = _manual(z, sub, "ref-item_submitted", amt, "clip-a", rail="stripe", submission_ids=["invented-1"])
    assert r.status_code == 409 and "not certified" in r.text               # AEGIS C4
    bare = _set_summary(z, out, sub, "Payout item submitted")
    z.ok(_undo(z, bare))
    z.ok(_disp(z, bare, disposition="moved", provider_ref="ref-item_submitted", moved_amount="9000.00",
               counterparty="clip-a"))
    r = _manual(z, bare, "ref-item_submitted", "9000.00", "clip-a", rail="stripe", submission_ids=["sub-1"])
    assert r.status_code == 409 and "worth" in r.text                       # AEGIS C4: at most what they are worth
    assert not [e for e in z.svc.entries if e["memo_code"] == "FRC" and e["source"]["id"] == sub["event_id"]]


# --------------------------------------------------------------------------------------------- R8-4

def test_r8_4_a_held_items_own_batched_payables_are_booked_now(keys, tmp_path):  # noqa: F811
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
    x.release(b["batch_id"])
    for c in [c for c in x.stripe.calls if c[0] == "submit"]:
        x.stripe.paid(c[1])
    z, out = _restore_cycle(x, tmp_path)
    _full_reimport(z, "2026-10-02", "2026-10-02")
    (sub,) = [i for i in _money(z, out) if i["event_type"] == "item_submitted"]
    held = z.svc.db["items"][sub["ref"]]
    assert {z.svc.db["payables"][p]["status"] for p in held["payable_ids"]} == {"batched"}
    for i in _money(z, out):
        if i["status"] == "needs_review":
            z.ok(_disp(z, i, disposition="moved", provider_ref=f"ref-{i['event_type']}"))
    lift(z)
    z.ok(_correct(z, sub, "ref-item_submitted", payable_ids=list(held["payable_ids"])), 201)   # no 2-day wait
    assert {z.svc.db["payables"][p]["status"] for p in held["payable_ids"]} == {"settled"}
    assert "clip-a" not in z.svc._subject_fences()
    z.clock.advance(days=3)                                                 # the approval expires later: no effect
    z.recon()
    assert {z.svc.db["payables"][p]["status"] for p in held["payable_ids"]} == {"settled"}


# --------------------------------------------------------------------------------------------- R8-5

def test_r8_5_a_mistaken_moved_is_undone_and_disposed_of_again(keys, tmp_path):  # noqa: F811
    from test_finance_ops_r7 import _payout_after_backup
    x, z, out, b, pays = _payout_after_backup(keys, tmp_path)
    for i in _money(z, out):
        if i["status"] == "needs_review":
            z.ok(_disp(z, i, disposition="moved", provider_ref=f"ref-{i['event_type']}"))
    (sub,) = [i for i in _money(z, out) if i["event_type"] == "item_submitted"]
    lift(z)
    z.ok(_correct(z, sub, "ref-item_submitted", payable_ids=[pays["clip-a"]["payable_id"]]), 201)
    assert _undo(z, sub).status_code == 409                                  # a booking is active: reverse it first
    z.ok(_reverse(z, sub), 201)
    assert _undo(z, sub, andre=None).status_code in (401, 403)
    z.ok(_undo(z, sub))
    it = _items(z, out)[sub["event_id"]]
    assert it["status"] == "needs_review" and it["undone_dispositions"][0]["provider_ref"] == "ref-item_submitted"
    assert "clip-a" in z.svc._subject_fences()                               # still fenced until disposed of again
    ev = [e for e in z.ledger.events if e["event_type"] == "restore_disposition_undone"]
    assert ev
    # the rail did pay it: not_moved is refused; recorded moved again with the right reference, booked once
    r = _disp(z, sub, disposition="not_moved", provider_ref="s", statement_date="2026-10-02")
    assert r.status_code == 409
    z.ok(_disp(z, sub, disposition="moved", provider_ref="po-right"))
    z.ok(_correct(z, sub, "po-right", payable_ids=[pays["clip-a"]["payable_id"]]), 201)
    assert "clip-a" not in z.svc._subject_fences()


# --------------------------------------------------------------------------------------------- R8-6 / R8-5 mutant

def _funding_lost(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    x.fund_campaign()
    x.payee()
    x.accrue()
    x.recon()
    b = x.run()["batch"]
    x.approve(b)
    snapshot(tmp_path)
    x.fund_batch(b["batch_id"])
    z, out = _restore_cycle(x, tmp_path)
    _full_reimport(z, "2026-10-02", "2026-10-02")
    for i in _money(z, out):
        if i["status"] == "needs_review":
            z.ok(_disp(z, i, disposition="moved", provider_ref="bank-ref-1"))
    lift(z)
    return z, out


def test_r8_6_a_booking_resolves_only_the_break_it_explains(keys, tmp_path):  # noqa: F811
    from decimal import Decimal
    z, out = _funding_lost(keys, tmp_path)
    z.bank.balances[("zbc", "1020")] -= Decimal("500.00")                 # an unrelated hole at the bank
    rec = z.recon()
    assert rec["post_restore_clean"] is False                                # not explained by the unbooked funding
    assert z.svc._open_breaks()                                             # and it blocks every run (FC-02)
    fund = [i for i in _money(z, out) if i["status"] != "follows_movement"
            and i["event_type"] in ("treasury_posting_committed", "treasury_op")][0]
    c = z.ok(_correct(z, fund, "bank-ref-1"), 201)
    for bid in c["breaks_resolved"]:
        assert abs(Decimal(z.svc.db["breaks"][bid]["difference"])) == Decimal(fund_amount(z, fund))
    still = [b for b in z.svc.db["breaks"].values() if b.get("leg") == "L1" and b["status"] != "resolved"]
    assert still                                                             # the 500.00 is not "resolved" by it


def fund_amount(z, it):
    return z.svc._derive_money(it)["amount"]


# --------------------------------------------------------------------------------------------- R8-7

def test_r8_7_a_refund_of_a_campaign_created_after_the_backup_ends_clean(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    x.fund_campaign()
    x.recon()
    snapshot(tmp_path)
    x.fund_campaign("camp-9", "client-9")
    dep = [r for r in x.svc.db["receipts"].values() if r.get("amount") == "1000.00"][-1]
    rf = x.ok(x.post("/fin/v1/refunds/camp-9", {"request_id": rid()}, caller="scheduler"), 201)["refund"]
    x.ok(x.post(f"/fin/v1/refunds/{rf['refund_id']}/decision",
                {"request_id": rid(), "content_sha256": rf["content_sha256"], "decision": "approve"}, andre=ANDRE_TOKEN))
    z, out = _restore_cycle(x, tmp_path, with_refeed=False)
    brk = z.svc.db["breaks"][out["reimport_break_id"]]
    _full_reimport(z, brk["since"], brk["opened_on"])
    for i in [i for i in _money(z, out) if i["status"] == "needs_review"]:
        z.ok(_disp(z, i, disposition="moved", provider_ref="bank-ref-9"))
    rp = [i for i in _money(z, out) if i["event_type"] == "refund_paid"][0]
    assert _correct(z, rp, "bank-ref-9", refund_campaign_id="camp-9").status_code == 409   # no campaign yet
    # runbook section 5 (R8-7): 1. re-feed the deposit's bank line (it lands unapplied)
    z.ok(z.post("/fin/v1/bank/events", {"request_id": rid(), "lines": [{
        "txn_ref_sha256": dep.get("bank_txn_ref_sha256") or hashlib.sha256(b"dep9").hexdigest(), "entity": "zbc",
        "account": "1020", "direction": "credit", "amount": "1000.00", "value_date": "2026-10-02"}]},
        caller="bank_feed"))
    lift(z)
    # 2. re-enter the campaign (rate card, commercial profile), re-issue its deposit invoice, apply the receipt
    doc = z.rate_card("camp-9")
    z.profile("camp-9", "client-9", "1000.00", "4.00", doc)
    inv = z.deposit_invoice("camp-9", "client-9", "1000.00")
    (rc,) = [r for r in z.svc.db["receipts"].values() if r["status"] == "unapplied"]
    z.ok(z.post(f"/fin/v1/receipts/{rc['receipt_id']}/apply", {"request_id": rid(), "invoice_id": inv["invoice_id"]},
                andre=ANDRE_TOKEN))
    # 3. the derived correction
    z.ok(_correct(z, rp, "bank-ref-9", refund_campaign_id="camp-9"), 201)
    assert not {"camp-9", "client-9"} & z.svc._subject_fences()
    assert z.svc.bal("2010", "campaign:camp-9") == 0
    rec = z.recon()
    assert rec["post_restore_clean"] is True, rec["recon"]["legs"]


# --------------------------------------------------------------------------------------------- R8-8 / reference

def _synthetic(hr, items):
    hr.svc.db["breaks"]["fin-brk-R"] = {"break_id": "fin-brk-R", "leg": "restore_reimport_required",
                                       "status": "resolved", "items": items}


def test_r8_8_one_movement_in_two_restore_breaks_is_booked_once():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.payee()
    hr.accrue()
    hr.recon()
    b = hr.run()["batch"]
    hr.approve(b)
    hr.fund_batch(b["batch_id"])
    (t,) = [t for t in hr.svc.db["treasury_ops"].values() if t["kind"] == "funding"]

    def item(eid, ref):
        return {"event_id": eid, "event_type": "treasury_op", "kind": "in_flight", "ref": t["op_id"],
                "status": "disposed", "disposition": {"disposition": "moved", "provider_ref": ref,
                                                      "moved_amount": t["amount"], "counterparties": []}}
    _synthetic(hr, {"a1": item("a1", "r-1")})
    hr.svc.db["breaks"]["fin-brk-S"] = {"break_id": "fin-brk-S", "leg": "restore_reimport_required",
                                       "status": "resolved", "items": {"a2": item("a2", "r-2")}}
    hr.ok(_correct(hr, {"event_id": "a1"}, "r-1"), 201)
    r = _correct(hr, {"event_id": "a2"}, "r-2")                              # a second restore, another reference
    assert r.status_code == 409 and "already booked" in r.text


def test_r8_one_reference_books_one_movement(keys, tmp_path):  # noqa: F811
    from test_finance_ops_r7 import _payout_after_backup
    x, z, out, b, pays = _payout_after_backup(keys, tmp_path)
    for i in _money(z, out):
        if i["status"] == "needs_review":
            z.ok(_disp(z, i, disposition="moved", provider_ref="same-ref"))
    lift(z)
    (sub,) = [i for i in _money(z, out) if i["event_type"] == "item_submitted"]
    fund = [i for i in _money(z, out) if i["status"] != "follows_movement"
            and i["event_type"] in ("treasury_posting_committed", "treasury_op")][0]
    z.ok(_correct(z, fund, "same-ref"), 201)
    r = _correct(z, sub, "same-ref", payable_ids=[pays["clip-a"]["payable_id"]])
    assert r.status_code == 409 and "reference" in r.text


# --------------------------------------------------------------------------------------------- R8-9

def test_r8_9_the_sent_index_reads_the_ledger_once():
    from test_client_mail import harness
    import mail as MAIL
    fx = MAIL.FixtureClientMail()
    hr = harness(fx)
    calls = []
    real = hr.svc.recorder.client.entries

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)
    hr.svc.recorder.client.entries = counting
    rec = {"client_receipt_id": "fin-crc-1", "status": "pending_send", "client_id": "zbm-client-7", "attempts": 0,
           "created_at": "2026-10-02T00:00:00Z", "entity": "zbm", "invoice_id": None, "what_you_bought": "x",
           "items": [], "period": None, "amount_paid": "1.00", "currency": "usd", "paid_on": "2026-10-02",
           "method": "ach"}
    for n in range(1, 4):
        hr.svc.db["client_receipts"][f"fin-crc-{n}"] = {**rec, "client_receipt_id": f"fin-crc-{n}"}
        hr.post(f"/fin/v1/client-receipts/fin-crc-{n}/send", {"request_id": rid()}, caller="scheduler")
    hr.post("/fin/v1/jobs/client-mail/run", {"request_id": rid()}, caller="ops_cron")
    assert len(calls) == 1
    sent = [k for k, r in hr.svc.db["client_receipts"].items() if r["status"] == "sent"]
    assert set(sent) <= hr.svc._sent_idx                                     # kept current on every send


# --------------------------------------------------------------------------------------------- AEGIS r8 survivors

def test_r8_a_resent_handoff_beyond_the_booking_is_not_settled(keys, tmp_path):  # noqa: F811
    x, z, out, sub, p = _old_payout(keys, tmp_path, accrue_after=True)
    bare = _set_summary(z, out, sub, "Payout item submitted")
    z.ok(_undo(z, bare))
    less = f"{(__import__('decimal').Decimal(p['current_amount']) - 1):.2f}"
    z.ok(_disp(z, bare, disposition="moved", provider_ref="ref-item_submitted", moved_amount=less,
               counterparty="clip-a"))
    lift(z)
    z.ok(_manual(z, bare, "ref-item_submitted", less, "clip-a", rail="stripe", submission_ids=["sub-1"]), 201)
    z.ok(z.handoff("sub-1"))
    assert z.svc.db["payables"][p["payable_id"]]["status"] == "accrued"
    assert [b for b in z.svc.db["breaks"].values() if b["leg"] == "restore_resent_over_booking" and b["status"] == "open"]


def test_r8_b_a_statement_day_outside_the_window_is_refused(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    snapshot(tmp_path)
    rr_invoice(x, "100.00")
    z, out = _restore_cycle(x, tmp_path, with_refeed=False)
    brk = z.svc.db["breaks"][out["reimport_break_id"]]
    days = statement_days(z, brk["since"], brk["opened_on"])
    extra = [{**days[0], "day": "2026-10-09"}]
    r = z.post("/fin/v1/restore/bank-refeed", {"request_id": rid(), "since": brk["since"], "until": brk["opened_on"],
                                               "statement_ref": "s", "statement_days": days + extra}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "outside" in r.text
    refeed(z)


def _synthetic_payout(hr):
    hr.fund_campaign()
    hr.payee()
    hr.accrue()
    (p,) = hr.svc.db["payables"].values()
    _synthetic(hr, {"s1": {"event_id": "s1", "event_type": "item_submitted", "kind": "money_out", "ref": "fin-itm-X",
                           "summary": "x", "status": "disposed",
                           "disposition": {"disposition": "moved", "provider_ref": "po-1", "moved_amount": p["amount"],
                                           "counterparty": "clip-a", "counterparties": ["clip-a"]}}})
    return p


def test_r8_c_manual_booking_payables_xor_submissions_and_a_taken_submission():
    hr = Harness().ready()
    p = _synthetic_payout(hr)
    it = {"event_id": "s1"}
    r = _manual(hr, it, "po-1", p["amount"], "clip-a", rail="stripe", payable_ids=[p["payable_id"]],
                submission_ids=["sub-9"])
    assert r.status_code == 409 and "submissions" in r.text
    hr.svc.db["payables"][p["payable_id"]] = {**p, "status": "settled"}      # sub-1 has a payable (already paid)
    r = _manual(hr, it, "po-1", p["amount"], "clip-a", rail="stripe", submission_ids=["sub-1"])
    assert r.status_code == 409 and "already has" in r.text


def test_r8_d_only_a_restore_correction_is_reversed():
    hr = Harness().ready()
    hr.fund_campaign()
    eid = next(iter(hr.svc.entries_by_id))
    _synthetic(hr, {"s1": {"event_id": "s1", "event_type": "item_submitted", "kind": "money_out", "ref": "fin-itm-X",
                           "status": "disposed", "corrected_by": eid,
                           "disposition": {"disposition": "moved", "provider_ref": "po-1"}}})
    r = _reverse(hr, {"event_id": "s1"})
    assert r.status_code == 409 and "not a restore correction" in r.text
