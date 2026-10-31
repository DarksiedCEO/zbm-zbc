"""AEGIS wave B review (NOT_CERTIFIED, Oct 30-31 2026), F1 and F2, with AEGIS's own repros
(scratchpad/night/aegis-waveB-evidence/test_aegis_attack.py) turned into assertions.

F1: the restore break records the BACKUP's point in time (the restored log's last line, before the reconcile is
appended), and only a re-import that covers the whole window from that day to the day the break opened, and leaves
nothing missing, closes it.
F2: credit notes and voids are not in Stripe. The re-import rebuilds them from the evidence ledger (their ledger events
carry everything needed, checked against the payload SHA-256 the ledger holds), in ledger order with the payments and
refunds, so the state after restore + re-import equals the state before. Numbering continues; a voided invoice never
becomes payable again; anything that cannot be rebuilt keeps the break open.
"""

from __future__ import annotations

from decimal import Decimal

from helpers import ANDRE_TOKEN, rid
from test_credit_notes import CLIENT, credit
from test_reconcile_anchor import RECONCILE, _plan
from test_restore_reimport import lift, durable, pay, reimport, restart, restore, sim_balance, snapshot
from test_stripe_incoming import checkout, keys, rr_invoice, sent  # noqa: F401  (keys: a fixture)


def _restore_cycle(x, tmp_path):
    restore(tmp_path)
    y = restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    plan = _plan(y)
    body = {"request_id": rid(), "head_sha256": plan["head_sha256"], "void_lines": plan["voidable"]["lines"],
            "void_event_ids": plan["voidable"]["event_ids"], "money_events_acknowledged": True}
    out = y.ok(y.post(RECONCILE, body, andre=ANDRE_TOKEN))
    return restart(x, tmp_path), out


def _full_reimport(z, since, until):
    dry = z.ok(reimport(z, since=since, until=until))
    return dry, z.ok(reimport(z, dry=False, plan=dry["plan"]["plan_sha256"], since=since, until=until))


def _void(hr, inv):
    return hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/void",
                   {"request_id": rid(), "content_sha256": inv["content_sha256"], "reason_code": "sow_ended_unpaid"},
                   andre=ANDRE_TOKEN)


def _state(hr, ids):
    keep = ("status", "credited", "credited_ar", "refund_due", "refunded", "refunded_via_credit")
    invs = {i: {k: hr.svc.db["invoices"][i].get(k) for k in keep} for i in ids}
    cns = {c: {k: v for k, v in r.items() if k in ("number", "invoice_id", "amount", "to_ar", "to_refund",
                                                    "refund_remaining", "status", "refund_due_by", "reason_code")}
           for c, r in hr.svc.db["credit_notes"].items()}
    bal = {a: hr.bal(a, None, "zbm") for a in ("1060", "1100", "2050", "2070", "4110")}
    return invs, cns, bal


# --------------------------------------------------------------------------------------------- F1

def test_f1_the_break_dates_the_backup_and_a_one_day_reimport_does_not_close_a_multi_day_restore(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    b = rr_invoice(x, "1100.00")
    snapshot(tmp_path)                                 # backup on Oct 2
    pay(x, a)                                          # paid Oct 2, after the backup
    x.clock.advance(days=2)
    pay(x, b)                                          # paid Oct 4
    z, out = _restore_cycle(x, tmp_path)
    brk = z.svc.db["breaks"][out["reimport_break_id"]]
    assert brk["since"] == "2026-10-02" and brk["opened_on"] == "2026-10-04"      # the backup's day, not today
    assert brk["restore_point"]["log_head_seq"] >= 1 and brk["restore_point"]["at"].startswith("2026-10-02")
    # AEGIS's repro: a re-import of a single day does not close it
    _dry, real = _full_reimport(z, "2026-10-04", "2026-10-04")
    assert out["reimport_break_id"] not in real["ready_to_lift"]
    assert z.svc.db["breaks"][out["reimport_break_id"]]["status"] == "open"
    _dry, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    assert out["reimport_break_id"] not in real["ready_to_lift"]                 # Oct 3 is still not covered
    _dry, real = _full_reimport(z, "2026-10-03", "2026-10-03")
    assert out["reimport_break_id"] in real["ready_to_lift"]                     # the runs now cover Oct 2..4
    assert {z.svc.db["invoices"][i["invoice_id"]]["status"] for i in (a, b)} == {"paid"}
    assert z.bal("1060", None, "zbm") == sim_balance(z)


def test_f1_a_window_that_still_misses_money_does_not_close(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    snapshot(tmp_path)
    pay(x, a)
    z, out = _restore_cycle(x, tmp_path)
    z.sim.fail_next = []
    dry = z.ok(reimport(z))
    # Stripe moves money in the window between the dry run and the real run: the real run is refused (plan changed)
    z.sim.dispute(list(z.sim.pis)[0])
    assert reimport(z, dry=False, plan=dry["plan"]["plan_sha256"]).status_code == 409
    assert z.svc.db["breaks"][out["reimport_break_id"]]["status"] == "open"
    _dry, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    assert out["reimport_break_id"] in real["ready_to_lift"]
    assert z.bal("1060", None, "zbm") == sim_balance(z)


# --------------------------------------------------------------------------------------------- F2

def test_f2_a_credit_note_survives_a_restore_with_its_number_and_its_refund_obligation(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    pay(x, a)
    snapshot(tmp_path)
    cn = x.ok(credit(x, x.svc.db["invoices"][a["invoice_id"]], "300.00", reason="nonconforming",
                     refund_due_from="2026-10-02"), 201)["credit_note"]
    before = _state(x, [a["invoice_id"]])
    z, out = _restore_cycle(x, tmp_path)
    assert cn["credit_note_id"] not in z.svc.db["credit_notes"]                   # lost by the restore...
    dry, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    assert [x_["action"] for x_ in dry["plan"]["actions"]] == ["rebuild_credit_note"]
    assert real["failed"] == 0 and out["reimport_break_id"] in real["ready_to_lift"]
    assert _state(z, [a["invoice_id"]]) == before                                 # ...and rebuilt exactly
    assert z.bal("2050", f"client:{CLIENT}", "zbm") == Decimal("300.00")
    due = [b for b in z.svc.db["breaks"].values() if b["leg"] == "credit_note_refund_due" and b["status"] == "open"]
    assert len(due) == 1 and due[0]["due_by"] == "2026-10-16"
    # the credit note's email was never sent before the restore: it is queued again, once (AEGIS R2-4)
    assert len([m for m in z.svc.db["mail_outbox"].values() if m["ref"] == cn["credit_note_id"]
                and m["status"] == "pending"]) == 1
    assert credit(z, z.svc.db["invoices"][a["invoice_id"]], "50.00").status_code == 409   # AEGIS r3: lockdown
    lift(z)
    nxt = z.ok(credit(z, z.svc.db["invoices"][a["invoice_id"]], "50.00", refund_due_from="2026-10-02"), 201)
    assert cn["number"] == "CN-2026-0001" and nxt["credit_note"]["number"] == "CN-2026-0002"


def test_f2_a_void_survives_a_restore_and_the_invoice_never_becomes_payable(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    snapshot(tmp_path)
    x.ok(_void(x, a))
    z, out = _restore_cycle(x, tmp_path)
    assert z.svc.db["invoices"][a["invoice_id"]]["status"] == "issued"          # restored state...
    r = checkout(z, a["invoice_id"])
    assert r.status_code == 409 and "restore" in r.text.lower()                  # ...is not payable meanwhile
    assert credit(z, z.svc.db["invoices"][a["invoice_id"]], "1.00").status_code == 409
    _dry, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    assert real["failed"] == 0
    assert z.svc.db["invoices"][a["invoice_id"]]["status"] == "void"
    assert z.bal("1100", f"client:{CLIENT}", "zbm") == 0
    assert checkout(z, a["invoice_id"]).status_code == 409


def test_f2_backup_then_credit_note_and_void_then_restore_and_reimport_equals_before(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1200.00")
    b = rr_invoice(x, "800.00")
    snapshot(tmp_path)
    _cs, pi, _ = pay(x, a)                                          # after the backup: payment
    x.ok(credit(x, x.svc.db["invoices"][a["invoice_id"]], "400.00", refund_due_from="2026-10-02"), 201)
    x.sim.refund(pi, 40000)                                         # the refund the credit note promised
    sent(x, "charge.refunded", {"id": x.sim.pis[pi]["latest_charge"], "object": "charge", "payment_intent": pi})
    x.ok(_void(x, b))
    x.ok(credit(x, x.svc.db["invoices"][a["invoice_id"]], "100.00", refund_due_from="2026-10-02"), 201)
    before = _state(x, [a["invoice_id"], b["invoice_id"]])
    assert before[0][a["invoice_id"]]["refund_due"] == "100.00"
    z, out = _restore_cycle(x, tmp_path)
    dry, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    assert [y["action"] for y in dry["plan"]["actions"]] == [
        "relink_page", "book_payment", "rebuild_credit_note", "book_refund", "rebuild_void", "rebuild_credit_note"]
    assert real["failed"] == 0 and out["reimport_break_id"] in real["ready_to_lift"]
    assert _state(z, [a["invoice_id"], b["invoice_id"]]) == before
    assert z.bal("1060", None, "zbm") == sim_balance(z)
    assert z.ok(z.get("/fin/v1/integrity"))["status"] == "green"
    assert z.ok(reimport(z))["plan"]["actions"] == []                              # nothing left, nothing twice


def test_f2_evidence_that_does_not_verify_is_not_rebuilt_and_the_break_stays_open(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    pay(x, a)
    snapshot(tmp_path)
    x.ok(credit(x, x.svc.db["invoices"][a["invoice_id"]], "300.00", refund_due_from="2026-10-02"), 201)
    ev = x.ledger.of_type("credit_note_issued")[0]
    ev["summary"] = ev["summary"].replace("300.00", "30.00")       # a summary that no longer matches its payload
    z, out = _restore_cycle(x, tmp_path)
    items = z.svc.db["breaks"][out["reimport_break_id"]]["items"]
    (cn_item,) = [i for i in items.values() if i["kind"] == "credit_note"]
    assert cn_item["status"] == "needs_review" and "hash" in cn_item["why"]
    _dry, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    assert out["reimport_break_id"] not in real["ready_to_lift"]
    assert not z.svc.db["credit_notes"]
    assert z.svc.db["breaks"][out["reimport_break_id"]]["status"] == "open"


# --------------------------------------------------------------------------------------------- AEGIS regressions

def test_aegis_reimport_is_idempotent_across_a_restart_and_a_webhook_replay(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    b = rr_invoice(x, "1100.00")
    snapshot(tmp_path)
    cs, pi_b, _ = pay(x, b)
    z, _out = _restore_cycle(x, tmp_path)
    _full_reimport(z, "2026-10-02", "2026-10-02")
    n_entries = len(z.svc.entries)
    w = restart(z, tmp_path)
    assert w.ok(reimport(w))["plan"]["actions"] == []
    sent(w, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    sent(w, "payment_intent.succeeded", {"id": pi_b, "object": "payment_intent"})
    assert len(w.svc.entries) == n_entries and w.bal("1060", None, "zbm") == sim_balance(w)
    assert len([r for r in w.svc.db["receipts"].values() if r.get("invoice_id") == b["invoice_id"]]) == 1
