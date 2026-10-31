"""AEGIS wave B round 3 (scratchpad/night/aegis-finance-ops-waveB-r3.md), failing first: the restore LOCKDOWN.

R3-1/R3-2  a durable lockdown written BEFORE a restore's reconcile changes anything; the reconcile and its restore
           break are one line (or a 503); a failed ledger read leaves the build incomplete (never empty-and-done);
           while on, no money goes out anywhere (webhooks still book money in), /health is degraded, an ALERT
           repeats; only Andre lifts it, once everything is done; a lost lockdown file is detected at start.
R3-3       (test_finance_ops_r2.py) dispositions only for what cannot be rebuilt, bound to the client.
R3-4       the commit-mark retry queue is the local log: after a restart the owed marks are written.
R3-6       an unauthenticated ledger event never moves the credit note numbering floor.
R3-7       the pause ends on the monotonic clock too; the watch ALERTs without a request.
R3-8       the deadline close keeps the reason text and refuses a done_on before the deadline began.
R3-9/R3-10 the authenticity checks each have a test (no key, unknown key id, MAC, department, mark hash); a
           rotated key stays readable; /health says restore_rebuild_unavailable.
Lows       each date bound on its own; a client email whose credit note is not rebuilt goes to Andre.
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal

import pytest

from errors import Conflict
from helpers import ANDRE_TOKEN, rid
from intelligences import i10_evidence_audit as i10
from ledger import derived_id, payload_sha256
from svc_credits import credit_note_payload, credit_note_summary
from test_credit_notes import CLIENT, ach_invoice, credit
from test_reconcile_anchor import RECONCILE, _plan
from test_restore_reimport import durable, lift, pay, reimport, restart, restore, snapshot
from test_restore_rebuild import _full_reimport, _restore_cycle, _void
from test_stripe_incoming import checkout, keys, rr_invoice  # noqa: F401



def _reconcile(y, ack=True):
    plan = _plan(y)
    return y.post(RECONCILE, {"request_id": rid(), "head_sha256": plan["head_sha256"],
                              "void_lines": plan["voidable"]["lines"], "void_event_ids": plan["voidable"]["event_ids"],
                              "money_events_acknowledged": ack}, andre=ANDRE_TOKEN)


def _lock_file(tmp_path, name="d"):
    p = tmp_path / name / "restore_lockdown.json"
    return json.loads(p.read_text()) if p.exists() else None


def _items(z, out):
    return z.svc.db["breaks"][out["reimport_break_id"]]["items"]


def _scenario(keys, tmp_path):  # noqa: F811
    """AEGIS B11: a paid invoice A; after the backup a credit note on A owing 400.00 and a void of invoice B."""
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    pay(x, a)
    b = rr_invoice(x, "500.00")
    snapshot(tmp_path)
    x.ok(credit(x, x.svc.db["invoices"][a["invoice_id"]], "400.00"), 201)
    x.ok(_void(x, x.svc.db["invoices"][b["invoice_id"]]))
    return x, a, b


# --------------------------------------------------------------------------------------------- R3-1

def test_r3_1_a_failed_read_at_the_scan_leaves_the_build_incomplete_and_the_lockdown_on(keys, tmp_path):  # noqa: F811
    x, a, b = _scenario(keys, tmp_path)
    restore(tmp_path)
    y = restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    real_scan = y.svc._scan_tail
    y.svc._scan_tail = lambda idx: (_ for _ in ()).throw(OSError("one timed-out read"))
    out = y.ok(_reconcile(y))
    y.svc._scan_tail = real_scan
    assert out["reimport_break_id"] and _lock_file(tmp_path)["state"] == "on"
    z = restart(x, tmp_path)
    brk = z.svc.db["breaks"][out["reimport_break_id"]]
    assert brk["build_incomplete"] is True and brk["items"] == {}
    assert z.ok(z.get("/health"))["restore_lockdown"] == {"on": True, "detected": False, "build_incomplete": True,
                                                          "why": None, "post_restore_reconcile_required": False}
    assert checkout(z, b["invoice_id"]).status_code == 409                   # the lost void is never payable
    lift(z, status=409)
    # a re-import reads the ledger again first; while that fails, nothing is planned
    z.svc._scan_tail = lambda idx: (_ for _ in ()).throw(OSError("still down"))
    r = reimport(z, since="2026-10-02", until="2026-10-02")
    assert r.status_code == 503 and "RESTORE_BUILD_INCOMPLETE" in r.text
    z.svc._scan_tail = real_scan
    dry, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    assert sorted(a_["action"] for a_ in dry["plan"]["actions"]) == ["rebuild_credit_note", "rebuild_void"]
    assert real["failed"] == 0 and out["reimport_break_id"] in real["ready_to_lift"]
    assert z.svc.db["breaks"][out["reimport_break_id"]]["status"] == "open"   # never closed automatically
    assert z.bal("2050", f"client:{CLIENT}", "zbm") == Decimal("400.00")
    lift(z)
    assert z.svc.restore_lockdown() is None and _lock_file(tmp_path)["state"] == "post_restore_reconcile_required"
    assert checkout(z, b["invoice_id"]).status_code in (404, 409)            # void: never payable
    assert z.ok(z.get("/health"))["status"] == "ok"


# --------------------------------------------------------------------------------------------- R3-2

def test_r3_2_the_reconcile_and_its_break_are_one_line_and_the_lockdown_is_written_first(keys, tmp_path):  # noqa: F811
    x, a, b = _scenario(keys, tmp_path)
    restore(tmp_path)
    y = restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    y.ledger.fail_on_type = "break_opened"
    r = _reconcile(y)
    y.ledger.fail_on_type = None
    assert r.status_code == 503                                              # no reconcile without its break
    assert _lock_file(tmp_path)["state"] == "on"                             # written ahead, and it stays
    assert not [v for v in y.svc.db["breaks"].values() if v["leg"] == "restore_reimport_required"]
    out = y.ok(_reconcile(y))
    assert out["reimport_break_id"] and out["reimport_break_id"] in y.svc.db["breaks"]
    z = restart(x, tmp_path)
    assert z.svc.restore_lockdown() is not None
    assert checkout(z, b["invoice_id"]).status_code == 409


def test_r3_2_the_lockdown_write_failing_stops_the_reconcile_before_anything_changes(keys, tmp_path,  # noqa: F811
                                                                                     monkeypatch):
    import svc_restore
    x, a, b = _scenario(keys, tmp_path)
    restore(tmp_path)
    y = restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    head = _plan(y)["head_sha256"]
    before = len(y.ledger.of_type(i10.RECONCILE_TYPE))

    def refuse(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(svc_restore.os, "replace", refuse)
    r = _reconcile(y)
    monkeypatch.undo()
    assert r.status_code == 503 and "restore lockdown could not be written" in r.text
    assert _plan(y)["head_sha256"] == head and len(y.ledger.of_type(i10.RECONCILE_TYPE)) == before


def test_r3_2_a_lost_lockdown_file_is_detected_at_start(keys, tmp_path, caplog):  # noqa: F811
    x, a, b = _scenario(keys, tmp_path)
    z, out = _restore_cycle(x, tmp_path)
    z.svc.close()
    (tmp_path / "d" / "restore_lockdown.json").unlink()
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        w = restart(x, tmp_path)
    assert w.svc.lockdown_status()["on"] and w.svc.lockdown_status()["detected"]
    assert any("restore_lockdown on (detected" in m.getMessage() for m in caplog.records)
    assert checkout(w, b["invoice_id"]).status_code == 409


def test_r3_2_a_plain_reconcile_with_nothing_at_risk_leaves_no_lockdown(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    rr_invoice(x, "100.00")
    assert x.svc.restore_lockdown() is None and _lock_file(tmp_path) is None


# --------------------------------------------------------------------------------------------- lockdown scope

def test_r3_lockdown_refuses_all_money_out_books_money_in_and_degrades_health(keys, tmp_path, caplog):  # noqa: F811
    x = durable(keys, tmp_path)
    d = rr_invoice(x, "640.00")
    cs = x.ok(checkout(x, d["invoice_id"]))["checkout"]                      # a page made before the backup
    snapshot(tmp_path)
    b = rr_invoice(x, "500.00")
    x.ok(_void(x, x.svc.db["invoices"][b["invoice_id"]]))
    other = rr_invoice(x, "250.00")                                          # made after the backup: lost
    z, out = _restore_cycle(x, tmp_path)
    assert z.post("/fin/v1/invoices", {"request_id": rid()}, caller="onboarding").status_code == 409
    for path in (f"/fin/v1/invoices/{d['invoice_id']}/stripe-checkout", "/fin/v1/payout-runs",
                 "/fin/v1/refunds/zbm-camp-1", "/fin/v1/payout-batches/fin-bat-X/release",
                 "/fin/v1/media-buys/fin-mb-X/vendor-payments"):
        r = z.post(path, {"request_id": rid()}, andre=ANDRE_TOKEN)
        assert r.status_code == 409 and "RESTORE_LOCKDOWN" in r.text, (path, r.status_code, r.text[:120])
    for what in ("void", "credit note", "Stripe page"):                      # also below the api (service calls)
        with pytest.raises(Conflict, match="RESTORE_LOCKDOWN"):
            z.svc._restore_refusal(what, d["invoice_id"])
    h = z.ok(z.get("/health"))
    assert h["status"] == "degraded" and h["restore_lockdown"]["on"] is True
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        z.svc._lockdown_alert_at = None
        z.svc._watch_tick()                                                  # no request needed
    assert any("ALERT finance: restore_lockdown on" in m.getMessage() for m in caplog.records)
    # money IN is still booked: the client pays the page during the lockdown and the webhook books it
    pay(z, d, cs=cs)
    assert z.svc.db["invoices"][d["invoice_id"]]["status"] == "paid"
    _full_reimport(z, "2026-10-02", "2026-10-02")
    for it in [i for i in _items(z, out).values() if i["status"] == "needs_review"]:
        z.ok(z.post(f"/fin/v1/restore/items/{it['event_id']}/disposition",
                    {"request_id": rid(), "disposition": "lost", "reason": "issued and voided after the backup"},
                    andre=ANDRE_TOKEN))
    assert z.post("/fin/v1/restore/lockdown/lift", {"request_id": rid(), "reason": "x"}).status_code in (401, 403)
    assert z.post("/fin/v1/restore/lockdown/lift", {"request_id": rid(), "reason": "x"},
                  caller="ops_cron").status_code in (401, 403)
    lift(z)
    lift(z, status=409)                                                      # not in lockdown any more


# --------------------------------------------------------------------------------------------- R3-4

def test_r3_4_owed_commit_marks_survive_a_restart_and_are_written(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "900.00")
    x.ledger.fail_on_type = "effect_committed"
    x.ok(credit(x, x.svc.db["invoices"][a["invoice_id"]], "100.00", refund_due_from=None), 201)
    assert x.ok(x.get("/health"))["commit_marks_pending"] == 2
    y = restart(x, tmp_path)                                                 # still failing: queued from the log
    assert y.ok(y.get("/health"))["commit_marks_pending"] == 2 and not y.ledger.of_type("effect_committed")
    y.ledger.fail_on_type = None
    y.ok(y.post("/fin/v1/jobs/client-mail/run", {"request_id": rid()}, caller="ops_cron"))
    assert len(y.ledger.of_type("effect_committed")) == 2
    assert y.ok(y.get("/health"))["commit_marks_pending"] == 0


# --------------------------------------------------------------------------------------------- R3-9 / R3-10

def _forge_cn(x, inv_id, number, amount, seq=7):
    cnid = "fin-cn-" + ("F" * 22 + number[-4:])
    rk = derived_id("cn", cnid)
    p = {**credit_note_payload(cnid, number, inv_id, amount, amount, "nonconforming", "2026-10-02"), "rk": rk,
         "seq": seq}
    eid = i10.evidence_id(rk, "credit_note_issued", payload_sha256(p))
    x.ledger.record_event(eid, "finance", "credit_note_issued", "andre", cnid, p,
                          credit_note_summary(cnid, number, inv_id, amount, amount, "nonconforming", "2026-10-02", seq))
    return eid


def _mark(x, intent, *, kid, mac, payload_mac=None, dept="finance", seq=7, ls="a" * 64):
    mp = {"intent_event_id": intent, "line_seq": seq, "line_sha256": ls, "key_id": kid,
          "mac": payload_mac if payload_mac is not None else mac}
    x.ledger.record_event(derived_id("cmt", intent), dept, "effect_committed", i10.ACTOR, intent, mp,
                          f"Committed {intent} at line {seq} {ls} key {kid} mac {mac}")


def test_r3_10_every_authenticity_check_has_a_test(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    pay(x, a)
    snapshot(tmp_path)
    svc = x.svc
    kid = svc._key_id(svc._evidence_key())
    b = rr_invoice(x, "200.00")                       # (it is in the backup) a genuine post-backup void, so a restore
    x.ok(_void(x, x.svc.db["invoices"][b["invoice_id"]]))                        # has lines to reconcile
    e_dash = _forge_cn(x, a["invoice_id"], "CN-2026-0090", "100.00")
    _mark(x, e_dash, kid="-", mac="-")                                          # (a) unauthenticated
    e_badmac = _forge_cn(x, a["invoice_id"], "CN-2026-0091", "100.00")
    _mark(x, e_badmac, kid=kid, mac="0" * 64)                                    # (b) the MAC does not verify
    e_hash = _forge_cn(x, a["invoice_id"], "CN-2026-0092", "100.00")
    good = svc._commit_mac(e_hash, 7, "a" * 64)
    _mark(x, e_hash, kid=kid, mac=good, payload_mac="1" * 64)                    # (c) summary != payload
    e_unknown = _forge_cn(x, a["invoice_id"], "CN-2026-0093", "100.00")
    _mark(x, e_unknown, kid="abcdefabcdef", mac=good)                            # (d) a key id nobody holds
    e_9000 = _forge_cn(x, a["invoice_id"], "CN-2026-9000", "1.00", seq=99999)    # (e) no mark, a huge number
    # (f) another department's deadline: never part of Finance's restore
    x.ledger.record_event("fin-ddl-" + "e" * 40, "legal", "deadline_recorded", "andre", "fin-brk-OTHER", {"x": 1},
                          "Deadline acknowledgement_due fin-brk-OTHER client zbm-client-7 mail m since 2026-10-02 "
                          "due 2026-10-06")
    z, out = _restore_cycle(x, tmp_path)
    its = _items(z, out)
    assert "fin-ddl-" + "e" * 40 not in its
    why = {e: its[e]["why"] for e in (e_dash, e_badmac, e_hash, e_unknown, e_9000)}
    assert all(its[e]["status"] == "needs_review" for e in why), why
    assert "not authenticated" in why[e_dash] and "MAC does not verify" in why[e_badmac]
    assert "does not hash to its payload" in why[e_hash] and "unknown key id abcdefabcdef" in why[e_unknown]
    assert "no commit mark" in why[e_9000]
    h = z.ok(z.get("/health"))
    assert h["restore_rebuild_unavailable"] is True and "abcdefabcdef" in h["restore_rebuild"]["reason"]
    assert z.svc._cn_number_floor(2026) == 0                                     # R3-6: forged numbers never count


def test_r3_9_a_rotated_key_stays_readable_and_no_key_is_reported(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "900.00")
    pay(x, a)
    snapshot(tmp_path)
    x.ok(credit(x, x.svc.db["invoices"][a["invoice_id"]], "300.00"), 201)
    old = x.svc.cfg.contact_hash_key.hex()
    rotated = tmp_path / "k-rot"
    rotated.write_text("cd" * 32 + "_" + old)                                   # new key first, the old one after
    os.chmod(rotated, 0o600)
    restore(tmp_path)
    y = restart(x, tmp_path, FIN_RECONCILE_MODE="1", FIN_CONTACT_HASH_KEY_FILE=str(rotated))
    out = y.ok(_reconcile(y))
    cn = [i for i in y.svc.db["breaks"][out["reimport_break_id"]]["items"].values() if i["kind"] == "credit_note"][0]
    assert cn["status"] == "pending"                                             # the old key still verifies
    z = restart(x, tmp_path, FIN_CONTACT_HASH_KEY_FILE=str(rotated))
    h = z.ok(z.get("/health"))
    assert h["restore_rebuild_unavailable"] is False and len(h["restore_rebuild"]["old_key_ids"]) == 1
    assert z.svc._key_id(z.svc._evidence_key()) == h["restore_rebuild"]["key_id"]
    # without a key at all: nothing is authenticated, and /health says so
    w = restart(x, tmp_path, FIN_CONTACT_HASH_KEY_FILE="")
    hw = w.ok(w.get("/health"))
    assert hw["restore_rebuild_unavailable"] is True and "not set" in hw["restore_rebuild"]["reason"]
    assert w.svc._mark_ok(cn["event_id"], {})[0] is False


# --------------------------------------------------------------------------------------------- R3-6

def test_r3_6_the_floor_counts_only_authentic_numbers_per_year(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    pay(x, a)
    snapshot(tmp_path)
    x.ok(credit(x, x.svc.db["invoices"][a["invoice_id"]], "100.00"), 201)       # CN-2026-0001, authentic
    _forge_cn(x, a["invoice_id"], "CN-2026-9000", "1.00")
    z, out = _restore_cycle(x, tmp_path)
    assert z.svc.db["breaks"][out["reimport_break_id"]]["cn_number_floor"] == {"2026": 1}
    assert z.svc._cn_number_floor(2027) == 0
    dry, real = _full_reimport(z, "2026-10-02", "2026-10-02")
    for it in [i for i in _items(z, out).values() if i["status"] == "needs_review"]:
        z.ok(z.post(f"/fin/v1/restore/items/{it['event_id']}/disposition",
                    {"request_id": rid(), "disposition": "lost", "reason": "forged, never issued"}, andre=ANDRE_TOKEN))
    lift(z)
    c = rr_invoice(z, "300.00")
    r = z.ok(credit(z, z.svc.db["invoices"][c["invoice_id"]], "10.00", refund_due_from=None), 201)
    assert r["credit_note"]["number"] == "CN-2026-0002"


# --------------------------------------------------------------------------------------------- R3-7

def test_r3_7_the_pause_ends_on_the_monotonic_clock_and_the_watch_alerts_alone(keys, tmp_path, caplog):  # noqa: F811
    x = durable(keys, tmp_path)
    mono = [1000]
    x.svc._monotonic = lambda: mono[0]
    x.ok(x.post("/fin/v1/backup/quiesce", {"request_id": rid(), "minutes": 30}, andre=ANDRE_TOKEN))
    x.clock.advance(minutes=20)
    x.clock.advance(hours=-3)                                                    # the wall clock steps back
    mono[0] += 31 * 60
    assert x.post("/fin/v1/invoices", {"request_id": rid()}, caller="onboarding").status_code != 503
    # restarted inside the window: paused, and the watch ALERTs without any request
    x.ok(x.post("/fin/v1/backup/quiesce", {"request_id": rid(), "minutes": 30}, andre=ANDRE_TOKEN))
    y = restart(x, tmp_path)
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        y.svc._pause_alert_at = None
        y.svc._watch_tick()
    assert any("paused_for_backup" in m.getMessage() for m in caplog.records)
    t = y.svc.start_watch(interval_s=3600)
    assert t.daemon and t.is_alive()
    # the restarted pause is bounded by the monotonic time since start too
    ym = [y.svc._boot_mono]
    y.svc._monotonic = lambda: ym[0]
    ym[0] += 31 * 60
    assert y.svc.quiesced() is None


# --------------------------------------------------------------------------------------------- R3-8 and the Lows

def test_r3_8_the_close_keeps_the_reason_and_done_on_starts_with_the_deadline(keys):  # noqa: F811
    from helpers import Harness
    hr = Harness().ready()
    inv = ach_invoice(hr)
    hr.clock.advance(days=3)
    bid = hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/sow-ended",
                        {"request_id": rid(), "sow_ended_on": "2026-10-04"}, andre=ANDRE_TOKEN), 201)["break_id"]
    body = {"request_id": rid(), "basis": "closed_by_andre", "reason": "client owes the full amount (15.4(a))"}
    r = hr.post(f"/fin/v1/deadlines/{bid}/close", {**body, "done_on": "2026-10-03"}, andre=ANDRE_TOKEN)
    assert r.status_code in (400, 422) and "before this deadline began" in r.text
    hr.ok(hr.post(f"/fin/v1/deadlines/{bid}/close", {**body, "request_id": rid(), "done_on": "2026-10-05"},
                  andre=ANDRE_TOKEN))
    res = hr.svc.db["breaks"][bid]["resolution"]
    assert res["reason"] == body["reason"] and res["reason_sha256"]
    (ev,) = hr.ledger.of_type("deadline_closed_by_andre")
    assert "reason" not in ev["payload"] and body["reason"] not in ev["summary"]


def test_r3_lows_each_date_bound_alone():
    from helpers import Harness
    hr = Harness().ready()
    inv = ach_invoice(hr)
    p = f"/fin/v1/invoices/{inv['invoice_id']}/sow-ended"
    r = hr.post(p, {"request_id": rid(), "sow_ended_on": "2026-10-01"}, andre=ANDRE_TOKEN)   # only "before issue"
    assert r.status_code == 422 and "before the invoice" in r.text
    hr.clock.advance(days=402)
    r = hr.post(p, {"request_id": rid(), "sow_ended_on": "2026-10-02"}, andre=ANDRE_TOKEN)   # only "400 days"
    assert r.status_code == 422 and "400" in r.text


def test_r3_lows_an_email_whose_credit_note_is_not_rebuilt_goes_to_andre(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "900.00")
    pay(x, a)
    snapshot(tmp_path)
    cn = x.ok(credit(x, x.svc.db["invoices"][a["invoice_id"]], "200.00"), 201)["credit_note"]
    (intent,) = [e for e in x.ledger.events if e["event_type"] == "credit_note_issued" and e["subject_id"] ==
                 cn["credit_note_id"]]
    gone = derived_id("cmt", intent["event_id"])                     # the credit note's mark is lost; its email's is not
    x.ledger.events[:] = [e for e in x.ledger.events if e["event_id"] != gone]
    x.ledger.by_id.pop(gone, None)
    z, out = _restore_cycle(x, tmp_path)
    its = _items(z, out)
    (c,) = [i for i in its.values() if i["kind"] == "credit_note"]
    (m,) = [i for i in its.values() if i["kind"] == "mail" and i.get("parent") == cn["credit_note_id"]]
    assert c["status"] == "needs_review" and m["status"] == "needs_review" and "not rebuilt" in m["why"]
