"""AEGIS round 2 attacks on the wave B fixes (scratchpad/night/aegis-waveB-r2-evidence/test_aegis_r2_attack.py), kept
as regressions. At a7da86e3, 9 of them failed (attacks.log): A1, A1b, A1c, A2, A4, A5, A7, A11 and A6 (a test-design
issue, replaced by A6c). Changes from AEGIS's file, each because the fix changed the contract: A3 also asserts the
restarted window ends; A4 reconciles with the acknowledgement once the guard refuses; A8 asserts the 1999 date and
the 0.01 credit; A11 asserts the item goes to Andre; A6 is dropped (A6c replaces it)."""

from __future__ import annotations

from helpers import ANDRE_TOKEN, rid
from intelligences import i10_evidence_audit as i10
from ledger import payload_sha256
from svc_credits import credit_note_payload, credit_note_summary
from test_credit_notes import CLIENT, credit
from test_reconcile_anchor import RECONCILE, _plan
from test_restore_reimport import durable, lift, refeed, pay, reimport, restart, restore, sim_balance, snapshot
from test_restore_rebuild import _full_reimport, _restore_cycle, _state, _void
from test_stripe_incoming import checkout, keys, rr_invoice  # noqa: F401


# ---------------------------------------------------------------- A1 attempted (uncommitted) evidence is rebuilt

def test_a1_a_credit_note_that_never_took_effect_is_not_rebuilt(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    pay(x, a)
    snapshot(tmp_path)
    # the ledger records the credit note's evidence, then refuses the log anchor: Finance answers 503, nothing booked
    x.ledger.fail_on_type = i10.ANCHOR_TYPE
    r = credit(x, x.svc.db["invoices"][a["invoice_id"]], "300.00", refund_due_from="2026-10-02")
    x.ledger.fail_on_type = None
    print("A1 credit under anchor failure:", r.status_code, r.text[:200])
    assert r.status_code == 503
    assert not x.svc.db["credit_notes"]
    assert x.ledger.of_type("credit_note_issued"), "evidence of the attempt is on the ledger"
    # Andre then makes something else happen after the backup so a restore break opens (a void of another invoice)
    b = rr_invoice(x, "500.00")
    x.ok(_void(x, b))
    before = _state(x, [a["invoice_id"]])
    z, out = _restore_cycle(x, tmp_path)
    dry, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    print("A1 plan:", [y["action"] for y in dry["plan"]["actions"]], "failed", real["failed"])
    print("A1 credit notes after rebuild:", {c["number"]: c["amount"] for c in z.svc.db["credit_notes"].values()})
    print("A1 2050 after:", z.bal("2050", f"client:{CLIENT}", "zbm"), "before restore:", before[2]["2050"])
    assert not z.svc.db["credit_notes"], "a credit note that never existed was rebuilt (phantom refund owed)"


def test_a1b_a_retried_credit_note_after_a_503_is_rebuilt_twice(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    pay(x, a)
    snapshot(tmp_path)
    x.ledger.fail_on_type = i10.ANCHOR_TYPE
    assert credit(x, x.svc.db["invoices"][a["invoice_id"]], "300.00", refund_due_from="2026-10-02").status_code == 503
    x.ledger.fail_on_type = None
    # Andre retries (new request: a new form submission)
    x.ok(credit(x, x.svc.db["invoices"][a["invoice_id"]], "300.00", refund_due_from="2026-10-02"), 201)
    before = _state(x, [a["invoice_id"]])
    z, out = _restore_cycle(x, tmp_path)
    dry, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    print("A1b plan:", [y["action"] for y in dry["plan"]["actions"]], "failed", real["failed"],
          "resolved", real["ready_to_lift"])
    after = _state(z, [a["invoice_id"]])
    print("A1b before 2050", before[2]["2050"], "after 2050", after[2]["2050"], "cns", len(after[1]))
    assert after == before


# ---------------------------------------------------------------- A2 forged ledger event (authenticity)

def test_a2_a_forged_credit_note_event_written_with_the_finance_ledger_identity_is_rebuilt(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    pay(x, a)
    snapshot(tmp_path)
    b = rr_invoice(x, "500.00")
    x.ok(_void(x, b))                                   # a genuine post-backup money event so a break opens
    # forge: anyone holding a token that may write department "finance" computes a self-consistent event
    cnid = "fin-cn-" + "F" * 26
    seq = 7
    from ledger import derived_id
    rk = derived_id("cn", cnid)
    payload = credit_note_payload(cnid, "CN-2026-0099", a["invoice_id"], "900.00", "900.00", "nonconforming",
                                  "2026-10-01")
    p = {**payload, "rk": rk, "seq": seq}
    eid = i10.evidence_id(rk, "credit_note_issued", payload_sha256(p))
    x.ledger.record_event(eid, "finance", "credit_note_issued", "andre", cnid, p,
                          credit_note_summary(cnid, "CN-2026-0099", a["invoice_id"], "900.00", "900.00",
                                              "nonconforming", "2026-10-01", seq))
    z, out = _restore_cycle(x, tmp_path)
    dry, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    print("A2 plan:", [y["action"] for y in dry["plan"]["actions"]], "failed", real["failed"])
    print("A2 credit notes:", {c["number"]: (c["amount"], c.get("rebuilt_from_ledger")) for c in
                               z.svc.db["credit_notes"].values()}, "2050", z.bal("2050", f"client:{CLIENT}", "zbm"))
    assert not z.svc.db["credit_notes"], "a forged ledger event became a 900.00 refund obligation"


# ---------------------------------------------------------------- A3 quiesce: expiry and restart

def test_a3_an_expired_quiesce_window_reopens_writes(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    q = x.ok(x.post("/fin/v1/backup/quiesce", {"request_id": rid(), "minutes": 5}, andre=ANDRE_TOKEN))
    print("A3 writes_refused_until:", q["writes_refused_until"])
    x.clock.advance(minutes=30)
    r = checkout(x, a["invoice_id"])
    print("A3 checkout 30 min after the window ended:", r.status_code, r.text[:160])
    y = restart(x, tmp_path)
    r2 = checkout(y, a["invoice_id"])
    print("A3 after restart:", r2.status_code, r2.text[:160])
    x.clock.advance(days=3)
    r3 = checkout(y, a["invoice_id"])
    print("A3 three days later:", r3.status_code)
    assert r.status_code == 200, "writes_refused_until passed but Finance is still paused (no end, no alert)"
    assert r2.status_code == 200 and r3.status_code == 200                  # AEGIS R2-6: a restart does not re-pause


def test_a3b_the_reconcile_exemption_on_a_live_quiesced_finance(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    rr_invoice(x, "1000.00")
    q = x.ok(x.post("/fin/v1/backup/quiesce", {"request_id": rid(), "minutes": 30}, andre=ANDRE_TOKEN))
    r = x.client.get(RECONCILE, headers=x.headers(andre=ANDRE_TOKEN))
    print("A3b GET reconcile live:", r.status_code, r.text[:200])
    body = {"request_id": rid(), "head_sha256": (r.json() or {}).get("head_sha256") if r.status_code == 200 else "0" * 64,
            "void_lines": [], "void_event_ids": []}
    r2 = x.post(RECONCILE, body, andre=ANDRE_TOKEN)
    print("A3b POST reconcile live while quiesced:", r2.status_code, r2.text[:200])
    res = x.ok(x.post("/fin/v1/backup/resume", {"request_id": rid(), "marker_id": q["marker"]["marker_id"]},
                      andre=ANDRE_TOKEN))
    print("A3b resume consistent:", res["consistent"])
    assert r2.status_code != 200 or res["consistent"] is False


# ---------------------------------------------------------------- A4 contract deadlines lost by a restore

def test_a4_a_sow_ended_deadline_opened_after_the_backup_survives_a_restore(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    snapshot(tmp_path)
    out = x.ok(x.post(f"/fin/v1/invoices/{a['invoice_id']}/sow-ended",
                      {"request_id": rid(), "sow_ended_on": "2026-10-02"}, andre=ANDRE_TOKEN), 201)
    ack = x.ok(x.post(f"/fin/v1/clients/{CLIENT}/acknowledgements",
                      {"request_id": rid(), "topic": "cancellation", "received_on": "2026-10-02"},
                      andre=ANDRE_TOKEN), 201)
    restore(tmp_path)
    y = restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    plan = _plan(y)
    print("A4 money_at_risk:", [m["event_type"] for m in plan["money_at_risk"]])
    body = {"request_id": rid(), "head_sha256": plan["head_sha256"], "void_lines": plan["voidable"]["lines"],
            "void_event_ids": plan["voidable"]["event_ids"]}
    r = y.post(RECONCILE, body, andre=ANDRE_TOKEN)
    print("A4 reconcile WITHOUT acknowledgement:", r.status_code, r.text[:200])
    assert r.status_code == 409 and "RESTORE_GUARD" in r.text                # obligations count like money (R2-4)
    y.ok(y.post(RECONCILE, {**body, "request_id": rid(), "money_events_acknowledged": True}, andre=ANDRE_TOKEN))
    z = restart(x, tmp_path)
    brk = [b for b in z.svc.db["breaks"].values() if b["leg"] == "restore_reimport_required"][0]
    kinds = sorted(i["kind"] for i in brk["items"].values() if i["status"] == "needs_review")
    assert kinds == ["deadline", "deadline", "mail"], kinds                   # listed for Andre, with ids
    open_legs = sorted(b["leg"] for b in z.svc.db["breaks"].values() if b["status"] != "resolved")
    print("A4 open breaks after restore:", open_legs)
    assert out["break_id"] in z.svc.db["breaks"] or "restore_reimport_required" in open_legs, \
        "the MSA 4.12(g) deadline vanished with no signal"
    assert ack["break_id"] in z.svc.db["breaks"] or "restore_reimport_required" in open_legs


# ---------------------------------------------------------------- A5 the restore break closed by hand

def test_a5_the_restore_break_closed_with_any_journal_entry_without_coverage(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    b = rr_invoice(x, "1100.00")
    snapshot(tmp_path)
    pay(x, a)
    x.clock.advance(days=2)
    pay(x, b)
    z, out = _restore_cycle(x, tmp_path)
    _dry, real = _full_reimport(z, "2026-10-04", "2026-10-04")
    bid = out["reimport_break_id"]
    assert z.svc.db["breaks"][bid]["status"] == "open"
    eid = z.svc.entries[-1]["entry_id"]
    r = z.post(f"/fin/v1/breaks/{bid}/resolution", {"request_id": rid(), "explanation_code": "unknown",
                                                     "entry_id": eid}, andre=ANDRE_TOKEN)
    print("A5 resolve restore break with an unrelated entry:", r.status_code, r.text[:200])
    print("A5 invoice A:", z.svc.db["invoices"][a["invoice_id"]]["status"], "1060", z.bal("1060", None, "zbm"),
          "stripe", sim_balance(z))
    assert r.status_code >= 400, "the coverage rule is bypassed by the generic break resolution"


# ---------------------------------------------------------------- A6 time zone / mid-day backup

def test_a6b_a_restored_backup_taken_inside_its_quiesce_window_boots_paused_and_reconcile_works(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    q = x.ok(x.post("/fin/v1/backup/quiesce", {"request_id": rid(), "minutes": 30}, andre=ANDRE_TOKEN))
    snapshot(tmp_path)                                  # the backup holds the open marker
    x.ok(x.post("/fin/v1/backup/resume", {"request_id": rid(), "marker_id": q["marker"]["marker_id"]},
                andre=ANDRE_TOKEN))
    pay(x, a)
    restore(tmp_path)
    y = restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    plan = _plan(y)
    body = {"request_id": rid(), "head_sha256": plan["head_sha256"], "void_lines": plan["voidable"]["lines"],
            "void_event_ids": plan["voidable"]["event_ids"], "money_events_acknowledged": True}
    r = y.post(RECONCILE, body, andre=ANDRE_TOKEN)
    print("A6b reconcile on a paused restore:", r.status_code, r.text[:200])
    z = restart(x, tmp_path)
    print("A6b quiesced after reconcile:", z.svc.quiesced())
    rr = reimport(z)
    print("A6b reimport while paused:", rr.status_code, rr.text[:160])
    res = z.post("/fin/v1/backup/resume", {"request_id": rid(), "marker_id": q["marker"]["marker_id"]},
                 andre=ANDRE_TOKEN)
    print("A6b resume:", res.status_code, res.text[:200])
    assert r.status_code == 200 and res.status_code == 200
    refeed(z)                                          # AEGIS r5 R5-4
    _d, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    assert real["ready_to_lift"]


# ---------------------------------------------------------------- A7 refund_due_from

def test_a7_refund_due_from_before_the_invoice_and_before_payment(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    pay(x, a)
    inv = x.svc.db["invoices"][a["invoice_id"]]
    r = credit(x, inv, "100.00", refund_due_from="1990-01-01")
    print("A7 due_from 1990:", r.status_code, r.text[:300])
    r2 = credit(x, x.svc.db["invoices"][a["invoice_id"]], "100.00", refund_due_from="2026-10-03")
    print("A7 due_from tomorrow:", r2.status_code)
    r3 = credit(x, x.svc.db["invoices"][a["invoice_id"]], "100.00", refund_due_from=None)
    print("A7 missing due_from:", r3.status_code, r3.text[:160])
    x.clock.advance(hours=8)                            # 2026-10-03 01:00Z = Oct 2 18:00 PT: still Oct 2 in LA
    r4 = credit(x, x.svc.db["invoices"][a["invoice_id"]], "50.00", refund_due_from="2026-10-03")
    print("A7 due_from = UTC today but LA tomorrow:", r4.status_code)
    assert r2.status_code in (400, 409, 422) and r3.status_code == 409 and r4.status_code in (400, 409, 422)
    assert r.status_code in (400, 409, 422), "a due date 36 years before the invoice is accepted"


# ---------------------------------------------------------------- A8 sow-ended route

def test_a8_sow_ended_who_replay_and_dates(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    p = f"/fin/v1/invoices/{a['invoice_id']}/sow-ended"
    body = {"request_id": rid(), "sow_ended_on": "2026-10-02"}
    for who in ("onboarding", "scheduler", "ops_cron"):
        r = x.post(p, body, caller=who)
        print("A8 caller", who, r.status_code)
        assert r.status_code in (401, 403)
    r = x.post(p, body)                                  # service token, no andre
    print("A8 service token:", r.status_code)
    assert r.status_code in (401, 403)
    first = x.ok(x.post(p, body, andre=ANDRE_TOKEN), 201)
    again = x.post(p, body, andre=ANDRE_TOKEN)
    print("A8 replay same request:", again.status_code, again.json() == first)
    assert again.status_code in (200, 201) and again.json() == first
    other = x.post(p, {**body, "sow_ended_on": "2026-09-01"}, andre=ANDRE_TOKEN)
    print("A8 same request_id, other body:", other.status_code)
    assert other.status_code == 409
    second = x.post(p, {"request_id": rid(), "sow_ended_on": "2026-10-02"}, andre=ANDRE_TOKEN)
    print("A8 second deadline while open:", second.status_code)
    assert second.status_code == 409
    fut = x.post(f"/fin/v1/invoices/{rr_invoice(x, '10.00')['invoice_id']}/sow-ended",
                 {"request_id": rid(), "sow_ended_on": "2026-10-05"}, andre=ANDRE_TOKEN)
    assert fut.status_code in (400, 422)
    # a 0.01 credit note closes the "credit it down to the work done" deadline
    x.ok(credit(x, x.svc.db["invoices"][a["invoice_id"]], "0.01", refund_due_from=None), 201)
    b = x.svc.db["breaks"][first["break_id"]]
    print("A8 deadline after a 0.01 credit note:", b["status"], b.get("resolution"))
    assert b["status"] == "open"                                             # R2-8
    old = x.post(f"/fin/v1/invoices/{rr_invoice(x, '10.00')['invoice_id']}/sow-ended",
                 {"request_id": rid(), "sow_ended_on": "1999-01-01"}, andre=ANDRE_TOKEN)
    print("A8 SOW ended 1999 (before the invoice):", old.status_code)
    assert old.status_code == 422                                            # R2-7


# ---------------------------------------------------------------- A9 duplicate replays of the re-import

def test_a9_reimport_replayed_and_rerun_never_rebuilds_twice(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    pay(x, a)
    snapshot(tmp_path)
    x.ok(credit(x, x.svc.db["invoices"][a["invoice_id"]], "300.00", refund_due_from="2026-10-02"), 201)
    before = _state(x, [a["invoice_id"]])
    z, out = _restore_cycle(x, tmp_path)
    dry = z.ok(reimport(z))
    body = {"request_id": rid(), "since": "2026-10-02", "until": "2026-10-02", "dry_run": False,
            "plan_sha256": dry["plan"]["plan_sha256"]}
    r1 = z.ok(z.post("/fin/v1/stripe/reimport", body, andre=ANDRE_TOKEN))
    r2 = z.ok(z.post("/fin/v1/stripe/reimport", body, andre=ANDRE_TOKEN))          # same request replayed
    w = restart(z, tmp_path)
    r3 = w.ok(reimport(w))
    print("A9 r1==r2", r1 == r2, "plan after restart", r3["plan"]["actions"])
    assert _state(w, [a["invoice_id"]]) == before and r3["plan"]["actions"] == []
    # a second restore from the same backup and a second rebuild
    z2, out2 = _restore_cycle(w, tmp_path)
    _d, real = _full_reimport(z2, "2026-10-02", "2026-10-02")
    print("A9 second restore:", real["failed"], real["ready_to_lift"], len(z2.svc.db["credit_notes"]))
    assert _state(z2, [a["invoice_id"]]) == before


# ---------------------------------------------------------------- A10 contact key

def test_a10_contact_key_handling(tmp_path):
    import config as config_mod
    from helpers import base_env
    env = base_env()
    k = tmp_path / "k"
    k.write_text("AB" * 32)
    try:
        config_mod.load({**env, "FIN_CONTACT_HASH_KEY_FILE": str(k)})
        print("A10 uppercase key accepted")
    except RuntimeError as e:
        print("A10 uppercase key:", str(e)[:120])
    k.write_text("")
    try:
        config_mod.load({**env, "FIN_CONTACT_HASH_KEY_FILE": str(k)})
        print("A10 empty key file accepted")
    except Exception as e:  # noqa: BLE001
        print("A10 empty key file:", type(e).__name__, str(e)[:120])
    env2 = {k_: v for k_, v in env.items() if k_ != "FIN_CONTACT_HASH_KEY_FILE"}
    s = config_mod.load(env2)
    print("A10 no key, mail off: contact_hash_key", s.contact_hash_key)
    assert "contact_hash_key" not in repr(s) or "ab" * 8 not in repr(s)


def test_a11_a_rebuild_that_cannot_succeed_deadlocks_new_stripe_pages(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    c = rr_invoice(x, "700.00")                          # an ordinary invoice from before the backup
    snapshot(tmp_path)
    b = rr_invoice(x, "500.00")                          # issued after the backup...
    x.ok(_void(x, b))                                    # ...and voided the same day
    z, out = _restore_cycle(x, tmp_path)
    for day in ("2026-10-02",):
        dry, real = _full_reimport(z, day, day)
        print("A11 run", day, [(r["action"], r["result"], r.get("why", "")[:70]) for r in real["results"]],
              "clean", real["clean"])
    # AEGIS r3 (the coordinator's direction): during the restore lockdown NO money goes out, for any invoice;
    # the lockdown never deadlocks: Andre settles the lost item and lifts it
    r = checkout(z, c["invoice_id"])
    r2 = credit(z, z.svc.db["invoices"][c["invoice_id"]], "10.00", refund_due_from=None)
    assert r.status_code == 409 and "RESTORE_LOCKDOWN" in r.text
    assert r2.status_code == 201, r2.text              # AEGIS r4 R4-8: crediting an UNPAID untouched invoice stays open
    brk = z.svc.db["breaks"][out["reimport_break_id"]]
    (it,) = [i for i in brk["items"].values() if i["kind"] == "void"]
    assert it["status"] == "needs_review" and "not in the restored books" in it["why"]
    lift(z, status=409)                                                      # an item is not settled
    d = z.ok(z.post(f"/fin/v1/restore/items/{it['event_id']}/disposition",
                    {"request_id": rid(), "disposition": "lost", "reason": "invoice issued and voided after the backup"},
                    andre=ANDRE_TOKEN))
    assert d["ready_to_lift"] is True
    lift(z)
    assert z.svc.db["breaks"][out["reimport_break_id"]]["status"] == "resolved"
    assert checkout(z, c["invoice_id"]).status_code == 200                   # the untouched invoice is payable
    assert checkout(z, b["invoice_id"]).status_code in (404, 409)            # the lost invoice never is
    z.ok(credit(z, z.svc.db["invoices"][c["invoice_id"]], "10.00", refund_due_from=None), 201)


def test_a1c_a_void_that_never_took_effect_is_rebuilt(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    snapshot(tmp_path)
    x.ledger.fail_on_type = i10.ANCHOR_TYPE
    assert _void(x, a).status_code == 503
    x.ledger.fail_on_type = None
    assert x.svc.db["invoices"][a["invoice_id"]]["status"] == "issued"
    pay_b = rr_invoice(x, "300.00")
    pay(x, pay_b)                                        # unrelated money so the restore break opens
    z, out = _restore_cycle(x, tmp_path)
    _d, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    print("A1c invoice A after rebuild:", z.svc.db["invoices"][a["invoice_id"]]["status"], "resolved",
          real["ready_to_lift"])
    assert z.svc.db["invoices"][a["invoice_id"]]["status"] == "issued", "a void that never happened was rebuilt"


def test_a6c_late_evening_backup_with_resume(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    x.clock.advance(hours=13, minutes=30)              # 2026-10-03 06:30Z = Oct 2 23:30 PT
    q = x.ok(x.post("/fin/v1/backup/quiesce", {"request_id": rid(), "minutes": 30}, andre=ANDRE_TOKEN))
    snapshot(tmp_path)
    x.ok(x.post("/fin/v1/backup/resume", {"request_id": rid(), "marker_id": q["marker"]["marker_id"]},
                andre=ANDRE_TOKEN))
    x.clock.advance(hours=1)                           # Oct 3 00:30 PT
    pay(x, a)
    z, out = _restore_cycle(x, tmp_path)
    z.ok(z.post("/fin/v1/backup/resume", {"request_id": rid(), "marker_id": q["marker"]["marker_id"]},
                andre=ANDRE_TOKEN))
    brk = z.svc.db["breaks"][out["reimport_break_id"]]
    print("A6c since", brk["since"], "opened_on", brk["opened_on"])
    _d, r1 = _full_reimport(z, "2026-10-03", "2026-10-03")
    print("A6c Oct3 only resolved:", r1["ready_to_lift"], "clean", r1["clean"])
    _d, r2 = _full_reimport(z, "2026-10-02", "2026-10-02")
    print("A6c +Oct2 resolved:", r2["ready_to_lift"])
    assert brk["since"] == "2026-10-02" and not r1["ready_to_lift"] and r2["ready_to_lift"]
    assert z.bal("1060", None, "zbm") == sim_balance(z)


def test_a10b_key_formats(tmp_path):
    import os
    import config as config_mod
    from helpers import base_env
    env = base_env()
    for label, val in (("upper", "AB" * 32), ("short", "ab" * 31), ("newline", "ab" * 32 + "\n"), ("empty", "")):
        k = tmp_path / label
        k.write_text(val)
        os.chmod(k, 0o600)
        try:
            config_mod.load({**env, "FIN_CONTACT_HASH_KEY_FILE": str(k)})
            print("A10b", label, "accepted")
        except Exception as e:  # noqa: BLE001
            print("A10b", label, type(e).__name__, str(e)[:110])
    try:
        rk = tmp_path / "rk"
        rk.write_text("re_x")
        os.chmod(rk, 0o600)
        e2 = {k: v for k, v in env.items() if k != "FIN_CONTACT_HASH_KEY_FILE"}
        config_mod.load({**e2, "RESEND_API_KEY_FILE": str(rk), "MAIL_FROM": "billing@zbestmedia.com"})
        print("A10b mail on without key: accepted")
    except Exception as e:  # noqa: BLE001
        print("A10b mail on without key:", type(e).__name__, str(e)[:110])
