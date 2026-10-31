"""AEGIS wave B round 4 (scratchpad/night/aegis-finance-ops-waveB-r4.md), failing first: the lockdown as an ALLOWLIST.

R4-1/R4-2/R4-4/R4-11  every state-changing route and every job is either allowlisted or refused while locked (a walk
                      over the running app), after authentication; the service choke points refuse too.
R4-3                  money that went OUT after the backup is an item for Andre and its subject is fenced: a lost
                      payout release can never be released again.
R4-5/R4-6             no lockdown clear while a restore break is open; a corrupt file fails closed and says why; an
                      unreadable backup point refuses the reconcile, and a break without its ledger position is
                      located again.
R4-7                  ``reissued`` is bound to the item's kind, invoice and amount.
R4-8                  MSA 4.12(g) stays meetable: Andre voids or credits an UNPAID untouched invoice during a lockdown
                      (flagged, listed at the lift); a lockdown past 2 business days escalates, daily.
R4-9/R4-10/R4-13      lift replay tells the real state; a once-reconciled healthy data dir is not locked; round-2 int
                      floors still count.
R4-12                 tests for the surviving mutants (R4-2, R4-4, R4-6, R4-21, R4-22, R4-23, R4-25, R4-28, N15, M2b).
"""

from __future__ import annotations

import logging
import os

import pytest
from fastapi.routing import APIRoute

import api as api_mod
import config as config_mod
from errors import Conflict
from helpers import ANDRE_TOKEN, CALLERS, Harness, rid
from svc_recon import JOBS
from test_credit_notes import CLIENT, ach_invoice, credit, void
from test_finance_ops_r3 import _reconcile, _scenario
from test_reconcile_anchor import _plan
from test_restore_reimport import durable, lift, pay, reimport, restart, restore, snapshot
from test_restore_rebuild import _full_reimport, _restore_cycle, _void
from test_stripe_incoming import keys, rr_invoice  # noqa: F401

LOCKED = "RESTORE_LOCKDOWN"
EXPECTED_ALLOWED = {
    "/fin/v1/stripe/events", "/fin/v1/bank/events", "/fin/v1/rails/x1/events",           # money in, rail status
    "/fin/v1/reconcile", "/fin/v1/stripe/reimport", "/fin/v1/restore/items/x1/disposition",
    "/fin/v1/restore/items/x1/correction", "/fin/v1/restore/items/x1/correction/reversal",
    "/fin/v1/restore/items/x1/manual-booking", "/fin/v1/restore/bank-refeed", "/fin/v1/restore/lockdown/lift",
    "/fin/v1/deadlines/x1/close", "/fin/v1/clients/x1/acknowledgements", "/fin/v1/invoices/x1/sow-ended",
    "/fin/v1/invoices/x1/void", "/fin/v1/invoices/x1/credit-notes",                      # service-checked (4.12(g))
    "/fin/v1/backup/quiesce", "/fin/v1/backup/resume", "/fin/v1/jobs/client-mail/run"}


def _settle(z, out, disp="lost", amount="29.01"):
    for it in [i for i in z.svc.db["breaks"][out["reimport_break_id"]]["items"].values()
               if i["status"] == "needs_review"]:
        body = {"request_id": rid(), "disposition": disp, "reason": "checked: bank ref BR-1"}
        if it["kind"] in ("money_out", "in_flight"):
            body.update(disposition="moved", provider_ref="BR-1")
            if z.svc._derive_money(it) is None and it["event_type"] not in ("batch_release_started",
                                                                           "media_vendor_payment_recorded"):
                body.update(moved_amount=amount, counterparty=it["ref"])
        z.ok(z.post(f"/fin/v1/restore/items/{it['event_id']}/disposition", body, andre=ANDRE_TOKEN))


def _locked_harness():
    hr = Harness().ready()
    hr.svc._lockdown = {"state": "on", "detected": False, "build_incomplete": False, "opened_at": "2026-10-02T17:00:00Z"}
    return hr


# --------------------------------------------------------------------------------------------- R4-1/2/4/11

def test_r4_every_write_route_and_job_is_allowlisted_or_refused_while_locked():
    hr = _locked_harness()
    allowed, refused = [], []
    for r in hr.app.routes:
        if not isinstance(r, APIRoute) or not (set(r.methods) - {"GET", "HEAD"}):
            continue
        paths = [r.path.replace("{job}", j) for j in JOBS] if "{job}" in r.path else [r.path]
        for p in paths:
            concrete = p
            while "{" in concrete:
                a, b = concrete.index("{"), concrete.index("}")
                concrete = concrete[:a] + "x1" + concrete[b + 1:]
            if p == "/fin/v1/backup/quiesce":
                allowed.append(p)                                     # allowlisted; not sent (it would pause)
                continue
            for method in set(r.methods) - {"GET", "HEAD"}:
                resp = hr.client.request(method, concrete, json={"request_id": rid()},
                                         headers=hr.headers(caller="scheduler", andre=ANDRE_TOKEN))
                gated = resp.status_code == 409 and LOCKED in resp.text and "only money in" in resp.text
                if api_mod.LOCKDOWN_ALLOWED.fullmatch(concrete):
                    assert not gated, (method, concrete)
                    allowed.append(concrete)
                else:
                    assert gated, (method, concrete, resp.status_code, resp.text[:120])
                    refused.append(concrete)
    # every money-out route AEGIS named is among the refused, and the jobs too (except the alert job)
    for must in ("/fin/v1/refunds/x1/repay", "/fin/v1/treasury/funding", "/fin/v1/treasury/funding/x1/decision",
                 "/fin/v1/treasury/sweeps", "/fin/v1/payout-batches/x1/release", "/fin/v1/payout-runs",
                 "/fin/v1/jobs/rail-sync/run", "/fin/v1/invoices/x1/stripe-checkout", "/fin/v1/invoices",
                 "/fin/v1/media-buys/x1/vendor-payments", "/fin/v1/reconciliations/run"):
        assert must in refused, must
    # AEGIS r5 R5-7: an independent, hand-written allowlist: widening the code's list fails here
    assert sorted(set(allowed)) == sorted(EXPECTED_ALLOWED), sorted(set(allowed) ^ EXPECTED_ALLOWED)
    assert len(refused) > 40
    # R4-11: authentication first (no lockdown oracle for the unauthenticated)
    assert hr.post("/fin/v1/payout-runs", {"request_id": rid()}, bearer=None).status_code == 401
    assert hr.post("/fin/v1/invoices/x1/void", {"request_id": rid()}, bearer="wrong").status_code == 401


def test_r4_the_service_choke_points_refuse_below_the_api():
    hr = _locked_harness()
    svc = hr.svc
    with pytest.raises(Conflict, match=LOCKED):
        svc._money_out_refusal("bank transfer", "t1")
    svc.db["treasury_ops"]["t1"] = {"op_id": "t1", "status": "approved", "kind": "refund_payment", "ref_id": "r1"}
    with pytest.raises(Conflict, match=LOCKED):
        svc._execute_transfer("t1")
    assert svc.retry_transfers() == 0
    assert svc.drive_open_items("x")["restore_lockdown"] is True
    with pytest.raises(Conflict, match=LOCKED):
        svc.repay_refund(rid(), "r1", {"request_id": rid()})
    with pytest.raises(Conflict, match=LOCKED):
        svc._restore_refusal("Stripe page", "fin-inv-X")
    # N15: the checkout itself refuses below the api (a lost void is never payable)
    inv = ach_invoice(Harness().ready())          # any issued invoice id shape
    svc.db["invoices"][inv["invoice_id"]] = inv
    with pytest.raises(Conflict, match=LOCKED):
        svc._stripe_checkout_make("onboarding", rid(), inv["invoice_id"], ("k",), "h")
    # R4-2 mutant: a restore break alone (lockdown flag off) still refuses
    svc._lockdown = None
    svc.db["breaks"]["fin-brk-R"] = {"break_id": "fin-brk-R", "leg": "restore_reimport_required", "status": "open"}
    assert svc.restore_locked()
    with pytest.raises(Conflict, match=LOCKED):
        svc._restore_refusal("void", "fin-inv-X")
    with pytest.raises(Conflict, match=LOCKED):
        svc._money_out_refusal("bank transfer", "t1")


# --------------------------------------------------------------------------------------------- R4-3

def test_r4_3_a_payout_released_after_the_backup_is_an_item_and_is_never_released_again(keys, tmp_path):  # noqa: F811
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
    x.release(b["batch_id"])                                                 # money out AFTER the backup
    n_sub = len([c for c in x.stripe.calls if c[0] == "submit"])
    z, out = _restore_cycle(x, tmp_path)
    brk = z.svc.db["breaks"][out["reimport_break_id"]]
    money = [i for i in brk["items"].values() if i["kind"] == "money_out"]
    assert {i["event_type"] for i in money} >= {"batch_release_started", "item_submitted"}
    assert b["batch_id"] in brk["fenced"]
    _full_reimport(z, brk["since"], brk["opened_on"])
    lift(z, status=409)                                                      # the money items are open
    it = money[0]
    bad = z.post(f"/fin/v1/restore/items/{it['event_id']}/disposition",
                 {"request_id": rid(), "disposition": "lost", "reason": "x"}, andre=ANDRE_TOKEN)
    assert bad.status_code == 409                                            # money: moved / not_moved only
    _settle(z, out)                                                          # Andre: moved (rail reference)
    lift(z)
    # AEGIS r6 R6-9: the post-restore phase ends first (a clean reconciliation), so this proves the id fence alone
    rec = z.recon()
    assert rec["post_restore_clean"] is True and z.svc.restore_post_phase() is None, rec["recon"]["legs"]
    before = z.svc.db["batches"][b["batch_id"]]["status"]
    r = z.post(f"/fin/v1/payout-batches/{b['batch_id']}/release", {"request_id": rid()}, caller="scheduler")
    assert r.status_code == 409 and "RESTORE_FENCED" in r.text
    assert len([c for c in z.stripe.calls if c[0] == "submit"]) == n_sub      # never submitted again
    assert z.svc.db["batches"][b["batch_id"]]["status"] == before            # refused before anything changed
    z2 = restart(x, tmp_path)                                                # R4-21: a restart after the lift
    assert not z2.svc.restore_locked() and b["batch_id"] in z2.svc._fenced()


def test_r4_3_not_moved_releases_the_fence_and_in_flight_transfers_are_items(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    snapshot(tmp_path)
    a = rr_invoice(x, "300.00")
    x.ok(_void(x, x.svc.db["invoices"][a["invoice_id"]]))
    restore(tmp_path)
    y = restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    y.svc.db["treasury_ops"]["fin-trx-T1"] = {"op_id": "fin-trx-T1", "status": "approved", "kind": "funding",
                                              "amount": "10.00", "rail": "stripe"}
    out = y.ok(_reconcile(y))
    its = y.svc.db["breaks"][out["reimport_break_id"]]["items"]
    assert its["inflight:fin-trx-T1"]["kind"] == "in_flight" and "fin-trx-T1" in y.svc._fenced()
    y.svc.reconcile_mode = False
    y.svc.reconcile_required = []
    y.ok(y.post("/fin/v1/restore/items/inflight:fin-trx-T1/disposition",
                {"request_id": rid(), "disposition": "not_moved", "reason": "bank shows nothing",
                 "provider_ref": "stmt-1002", "statement_date": "2026-10-02"}, andre=ANDRE_TOKEN))
    assert "fin-trx-T1" not in y.svc._fenced()


# --------------------------------------------------------------------------------------------- R4-5 / R4-4 / R4-22

def test_r4_5_a_corrupt_file_fails_closed_and_an_open_break_always_locks(keys, tmp_path):  # noqa: F811
    x, a, b = _scenario(keys, tmp_path)
    z, out = _restore_cycle(x, tmp_path)
    z.svc.close()
    (tmp_path / "d" / "restore_lockdown.json").write_text("{garbage")
    w = restart(x, tmp_path)
    st = w.ok(w.get("/health"))["restore_lockdown"]
    assert st["on"] is True and "could not be read" in st["why"]              # R4-4: fail closed, and why
    w.svc._lockdown = {"state": "clear"}                                      # even with the flag off...
    assert w.svc.restore_locked()                                             # ...the open break locks
    w.svc._lockdown_clear_after_reconcile()                                   # R4-5: never cleared with a break open
    assert w.svc._lockdown == {"state": "clear"} and w.svc.restore_locked()
    w.svc.close()
    (tmp_path / "d" / "restore_lockdown.json").unlink()
    v = restart(x, tmp_path)
    assert v.svc.restore_lockdown() is not None                               # re-entered from the open break


def test_r4_22_a_plain_reconcile_never_clears_a_real_lockdown():
    hr = _locked_harness()
    hr.svc._lockdown_clear_after_reconcile()
    assert hr.svc.restore_lockdown() is not None


# --------------------------------------------------------------------------------------------- R4-6

def test_r4_6_an_unreadable_backup_point_refuses_the_reconcile_and_a_lost_position_is_found(keys, tmp_path):  # noqa: F811
    x, a, b = _scenario(keys, tmp_path)
    restore(tmp_path)
    y = restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    real_rp, client = y.svc.restore_point, y.svc.recorder.client
    real_entries = client.entries

    def rp_once():
        client.entries = lambda *a_, **k: (_ for _ in ()).throw(OSError("one timeout"))
        try:
            return real_rp()
        finally:
            client.entries = real_entries
    y.svc.restore_point = rp_once
    head = _plan(y)["head_sha256"]
    r = _reconcile(y)
    y.svc.restore_point = real_rp
    assert r.status_code == 503 and _plan(y)["head_sha256"] == head            # nothing done
    out = y.ok(_reconcile(y))
    z = restart(x, tmp_path)
    bid = out["reimport_break_id"]
    z.svc.db["breaks"][bid] = {**z.svc.db["breaks"][bid], "ledger_index": None, "build_incomplete": True,
                               "items": {}}
    z.ok(reimport(z, since="2026-10-02", until="2026-10-02"))                 # located again, then listed
    brk = z.svc.db["breaks"][bid]
    assert brk["ledger_index"] is not None and not brk["build_incomplete"] and brk["items"]


# --------------------------------------------------------------------------------------------- R4-7 / R4-8 / R4-25

def test_r4_7_reissued_is_bound_to_the_item_and_r4_8_unpaid_voids_stay_possible(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    paid_inv = rr_invoice(x, "900.00")
    pay(x, paid_inv)
    unpaid = rr_invoice(x, "400.00")
    other = rr_invoice(x, "250.00")
    snapshot(tmp_path)
    x.ledger.fail_on_type = "effect_committed"                               # committed, marks never written
    x.ok(credit(x, x.svc.db["invoices"][paid_inv["invoice_id"]], "600.00"), 201)
    x.ok(_void(x, x.svc.db["invoices"][unpaid["invoice_id"]]))
    x.ledger.fail_on_type = None
    z, out = _restore_cycle(x, tmp_path)                                     # (the log held no mark queue: lost)
    its = z.svc.db["breaks"][out["reimport_break_id"]]["items"]
    (cn,) = [i for i in its.values() if i["kind"] == "credit_note"]
    (vd,) = [i for i in its.values() if i["kind"] == "void"]
    assert cn["status"] == vd["status"] == "needs_review"
    p_cn = f"/fin/v1/restore/items/{cn['event_id']}/disposition"
    for ref in (other["invoice_id"], paid_inv["invoice_id"], out["reimport_break_id"]):
        r = z.post(p_cn, {"request_id": rid(), "disposition": "reissued", "reason": "x", "reissued_as": ref},
                   andre=ANDRE_TOKEN)
        assert r.status_code == 409, ref                                       # never an unrelated id
    _full_reimport(z, "2026-10-02", "2026-10-02")                            # R5-4: the window covered first
    # R4-8: an UNPAID untouched invoice is voided during the lockdown (4.12(g)); a touched one is not
    assert void(z, z.svc.db["invoices"][unpaid["invoice_id"]]).status_code == 409   # the restore touches it
    z.ok(void(z, z.svc.db["invoices"][other["invoice_id"]]))
    assert z.svc.db["invoices"][other["invoice_id"]]["voided_during_lockdown"] is True
    r = credit(z, z.svc.db["invoices"][paid_inv["invoice_id"]], "10.00")       # would owe a refund: refused
    assert r.status_code == 409 and LOCKED in r.text
    # the lost void: Andre cannot void it again while it is touched, so it is re-issued after the lift
    z.ok(z.post(f"/fin/v1/restore/items/{vd['event_id']}/disposition",
                {"request_id": rid(), "disposition": "reissue_after_lift", "reason": "void again after"},
                andre=ANDRE_TOKEN))
    z.ok(z.post(p_cn, {"request_id": rid(), "disposition": "reissue_after_lift", "reason": "MSA 4.10 refund owed"},
                andre=ANDRE_TOKEN))
    _settle(z, out)
    _full_reimport(z, "2026-10-02", "2026-10-02")
    res = lift(z)
    assert {"kind": "void", "invoice_id": other["invoice_id"]} in res["lockdown_effects"]
    due = [b for b in z.svc.db["breaks"].values() if b["leg"] == "restore_reissue_due"]
    assert len(due) == 2 and {d["item"]["kind"] for d in due} == {"credit_note", "void"}     # R4-25
    assert [d for d in due if d["item"]["kind"] == "credit_note"][0]["item"]["to_refund"] == "600.00"


def test_r4_8_the_unpaid_exception_needs_a_complete_list():
    hr = Harness().ready()
    inv = ach_invoice(hr, "300.00")
    hr.svc._lockdown = {"state": "on", "detected": False, "build_incomplete": True}
    r = void(hr, hr.svc.db["invoices"][inv["invoice_id"]])
    assert r.status_code == 409 and "not complete" in r.text
    hr.svc._lockdown = {"state": "on", "detected": True, "build_incomplete": False}
    assert void(hr, hr.svc.db["invoices"][inv["invoice_id"]]).status_code == 409
    hr.svc._lockdown = {"state": "on", "detected": False, "build_incomplete": False}
    hr.ok(void(hr, hr.svc.db["invoices"][inv["invoice_id"]]))
    assert hr.svc.db["invoices"][inv["invoice_id"]]["voided_during_lockdown"] is True


def test_r4_8_a_long_lockdown_escalates_daily(caplog):
    hr = _locked_harness()
    hr.svc._lockdown["opened_at"] = "2026-10-02T17:00:00Z"                    # Friday
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        hr.svc._watch_tick()
        assert not any("restore_lockdown_long" in m.getMessage() for m in caplog.records)
        hr.clock.advance(days=4)                                              # Tuesday: 2 business days later
        hr.svc._watch_tick()
        hr.svc._lockdown_alert_at = None
        hr.svc._watch_tick()                                                  # same day: once
        assert sum("restore_lockdown_long" in m.getMessage() for m in caplog.records) == 1
        hr.clock.advance(days=1)
        hr.svc._lockdown_alert_at = None
        hr.svc._watch_tick()
        assert sum("restore_lockdown_long" in m.getMessage() for m in caplog.records) == 2


# --------------------------------------------------------------------------------------------- R4-9 / R4-28 / R4-6'

def test_r4_9_the_lift_writes_first_and_a_replay_tells_the_real_state(keys, tmp_path, monkeypatch):  # noqa: F811
    import svc_restore
    x, a, b = _scenario(keys, tmp_path)
    z, out = _restore_cycle(x, tmp_path)
    bid = out["reimport_break_id"]
    _full_reimport(z, "2026-10-02", "2026-10-02")
    _settle(z, out)
    # R4-28: an incomplete list alone refuses the lift
    z.svc.db["breaks"][bid] = {**z.svc.db["breaks"][bid], "build_incomplete": True}
    r = z.post("/fin/v1/restore/lockdown/lift", {"request_id": rid(), "reason": "x"}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "incomplete" in r.text
    z.svc.db["breaks"][bid] = {**z.svc.db["breaks"][bid], "build_incomplete": False}
    # R4-6 mutant: reconcile mode alone refuses the lift
    z.svc.reconcile_required = ["x"]
    assert z.post("/fin/v1/restore/lockdown/lift", {"request_id": rid(), "reason": "x"},
                  andre=ANDRE_TOKEN).status_code == 409
    z.svc.reconcile_required = []
    req = rid()
    monkeypatch.setattr(svc_restore.os, "replace", lambda *a_, **k: (_ for _ in ()).throw(OSError("full")))
    r1 = z.post("/fin/v1/restore/lockdown/lift", {"request_id": req, "reason": "done"}, andre=ANDRE_TOKEN)
    monkeypatch.undo()
    assert r1.status_code == 503 and z.svc.restore_locked() and z.svc.db["breaks"][bid]["status"] == "open"
    ok = z.ok(z.post("/fin/v1/restore/lockdown/lift", {"request_id": req, "reason": "done"}, andre=ANDRE_TOKEN))
    assert ok["lifted"] is True and not z.svc.restore_locked()
    z.svc._lockdown = {"state": "on"}                                         # a later lockdown
    again = z.ok(z.post("/fin/v1/restore/lockdown/lift", {"request_id": req, "reason": "done"}, andre=ANDRE_TOKEN))
    assert again["lifted"] is False and again["locked"] is True


# --------------------------------------------------------------------------------------------- R4-10 / R4-13 / R4-23

def test_r4_10_a_once_reconciled_healthy_data_dir_is_not_locked(keys, tmp_path):  # noqa: F811
    from store import StoreWriteError
    x = durable(keys, tmp_path)
    rr_invoice(x, "100.00")
    real = x.svc.log.append_prepared
    x.svc.log.append_prepared = lambda *a_, **k: (_ for _ in ()).throw(StoreWriteError("disk hiccup"))
    x.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "clip-z", "kind": "clipper", "declared_country": "US",
                              "callback_contact_ref": "vault:contact-clip-z"}, caller="clipper_network")
    x.svc.log.append_prepared = real
    y = restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    y.ok(_reconcile(y, ack=False))
    y.svc.close()
    p = tmp_path / "d" / "restore_lockdown.json"
    if p.exists():
        p.unlink()                                                            # a data dir from before the file
    w = restart(x, tmp_path)
    assert not w.svc.restore_locked() and w.ok(w.get("/health"))["status"] == "ok"


def test_r4_13_a_round_2_int_floor_still_counts():
    hr = Harness().ready()
    hr.svc.db["breaks"]["fin-brk-OLD"] = {"break_id": "fin-brk-OLD", "leg": "restore_reimport_required",
                                         "status": "resolved", "cn_number_floor": 7}
    assert hr.svc._next_credit_note_number() == "CN-2026-0008"


@pytest.mark.parametrize("value", ["ab" * 32 + "_" + "cd" * 32 + "_" + "ef" * 32 + "_" + "12" * 32,
                                   "ab" * 32 + "_" + "ab" * 32])
def test_r4_23_at_most_two_old_keys_each_once(tmp_path, value):
    kf = tmp_path / "k"
    kf.write_text(value)
    os.chmod(kf, 0o600)
    with pytest.raises(RuntimeError, match="FIN_CONTACT_HASH_KEY_FILE"):
        config_mod._contact_keys({"FIN_CONTACT_HASH_KEY_FILE": str(kf)})


# --------------------------------------------------------------------------------------------- M2b

def test_m2b_an_issued_invoice_with_a_receipt_is_never_voided():
    hr = Harness().ready()
    inv = ach_invoice(hr, "1000.00")
    hr.receive("zbm", "1010", "100.00", inv["invoice_id"])
    # a receipt tied to the invoice while it is still issued (e.g. a payment held for Compliance)
    (rc,) = hr.svc.db["receipts"].values()
    hr.svc.db["receipts"][rc["receipt_id"]] = {**rc, "invoice_id": inv["invoice_id"]}
    cur = hr.svc.db["invoices"][inv["invoice_id"]]
    assert cur["status"] == "issued" and not cur.get("paid_at")
    r = void(hr, cur)
    assert r.status_code == 409 and "money was paid" in r.text
    assert CLIENT and CALLERS
