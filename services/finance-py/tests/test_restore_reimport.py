"""CH-3 / CH-4 (red team chaos): backups, restores and the Stripe re-import.

The red team's restore scenario R1, reproduced: Finance's volume is restored to a backup taken BEFORE two invoices were
paid (and a refund made), while the evidence ledger and Stripe keep going. Before this fix the reconcile plan voided
the evidence of the paid invoices as a "cleanup", and the payments were acknowledged to Stripe and lost from the
books. Now:
- the plan lists ``money_at_risk`` and the reconcile refuses without Andre's explicit acknowledgement (RESTORE_GUARD),
  then opens a break that stays open until a Stripe re-import ran;
- a replayed Checkout event for a page Finance lost is answered 2xx but opens a break and logs an ALERT (CH-4);
- the Stripe re-import (dry run, then real) books every missing payment, refund and dispute exactly once, re-links the
  lost page, and leaves 1060 equal to Stripe's balance.
"""

from __future__ import annotations

import logging
import os
import shutil
from decimal import Decimal

import pytest

from clock import FixedClock
from helpers import ANDRE_TOKEN, NOW, Harness, rid
from stripe_incoming import StripeIncoming
from stripe_sim import TEST_KEY, TEST_WHSEC, SimStripe
from test_stripe_incoming import CANCEL, SUCCESS, checkout, keys, rr_invoice, sent  # noqa: F401
from test_reconcile_anchor import RECONCILE, _plan

TODAY = "2026-10-02"


def durable(keys, tmp_path, name="d", **env):  # noqa: F811
    clock = FixedClock(NOW)
    sim = SimStripe(clock)
    port = StripeIncoming(TEST_KEY, TEST_WHSEC, False, SUCCESS, CANCEL, transport=sim.transport(), clock=clock)
    hr = Harness(env={**keys, "FIN_CARD_PREPAYMENTS": "1", **env}, clock=clock, stripe_in=port,
                 data_dir=str(tmp_path / name)).ready()
    hr.sim, hr.port, hr.env_extra = sim, port, {**keys, "FIN_CARD_PREPAYMENTS": "1", **env}
    return hr


def restart(x, tmp_path, name="d", **env):
    y = Harness(env={**x.env_extra, **env}, data_dir=str(tmp_path / name), ledger=x.ledger, clock=x.clock,
                fakes=x.f, stripe_in=x.port)
    y.sim, y.port, y.env_extra = x.sim, x.port, x.env_extra
    return y


def snapshot(tmp_path, name="d", to="backup"):
    src, dst = tmp_path / name, tmp_path / to
    dst.mkdir()
    for p in src.iterdir():
        if p.is_file() and not p.name.endswith(".lock") and "lock" not in p.name:
            shutil.copy2(p, dst / p.name)


def restore(tmp_path, name="d", frm="backup"):
    for p in (tmp_path / frm).iterdir():
        shutil.copy2(p, tmp_path / name / p.name)


def pay(hr, inv, cs=None, method="us_bank_account"):
    cs = cs or hr.ok(checkout(hr, inv["invoice_id"]))["checkout"]
    pi = hr.sim.pay(cs["session_id"], method=method)
    return cs, pi, sent(hr, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})


def sim_balance(hr):
    return (Decimal(hr.sim.balance_cents()) / 100).quantize(Decimal("0.01"))


def reimport(hr, dry=True, plan=None, since=TODAY, until=TODAY):
    body = {"request_id": rid(), "since": since, "until": until, "dry_run": dry}
    if plan:
        body["plan_sha256"] = plan
    return hr.post("/fin/v1/stripe/reimport", body, andre=ANDRE_TOKEN)


def statement_days(hr, since, until, extra=()):
    """The bank statement's per-day credit figures for every calendar day and bank account. In these tests the
    statement is what the fake bank feed delivered, i.e. the lines Finance booked, plus ``extra`` lines the bank has
    that Finance has not been fed: (day, entity, account, amount)."""
    from datetime import date, timedelta
    from decimal import Decimal
    out = {}
    d, end = date.fromisoformat(str(since)), date.fromisoformat(str(until))
    while d <= end:
        for ent, acct in (("zbc", "1020"), ("zbc", "1010"), ("zbm", "1010")):    # every calendar day (AEGIS r7)
            out[(d.isoformat(), ent, acct)] = [0, Decimal("0.00")]
        d += timedelta(days=1)
    for r in hr.svc.db["receipts"].values():
        k = (str(r.get("value_date") or "")[:10], r.get("entity"), r.get("into_account"))
        if r.get("bank_txn_ref_sha256") and k in out:
            out[k][0] += 1
            out[k][1] += Decimal(r["amount"])
    for day, ent, acct, amt in extra:
        out[(day, ent, acct)][0] += 1
        out[(day, ent, acct)][1] += Decimal(amt)
    closing = {(e, a): hr.svc.ports.bank.balance(e, a).balance for e, a in (("zbc", "1020"), ("zbc", "1010"),
                                                                              ("zbm", "1010"))}
    return [{"day": k[0], "entity": k[1], "account": k[2], "credit_lines": v[0], "credit_total": f"{v[1]:.2f}",
             "closing_balance": closing[(k[1], k[2])]} for k, v in sorted(out.items())]


def refeed(hr, extra=(), status=200):
    """AEGIS r5 R5-4 / r6 R6-3: Andre confirms the bank lines since the backup were fed again, with the statement's
    per-day figures (for every open restore break)."""
    for b in hr.svc._restore_open():
        r = hr.post("/fin/v1/restore/bank-refeed", {"request_id": rid(), "since": b["since"],
                                                     "until": b["opened_on"], "statement_ref": "stmt-2026-10",
                                                     "statement_days": statement_days(hr, b["since"], b["opened_on"],
                                                                                      extra)}, andre=ANDRE_TOKEN)
        assert r.status_code == status, (r.status_code, r.text)


def lift(hr, reason="re-import complete and every restore item settled", status=200, with_refeed=True):
    """AEGIS r3: Andre lifts the restore lockdown (refused, 409, until everything is done)."""
    if with_refeed:
        refeed(hr)
    r = hr.post("/fin/v1/restore/lockdown/lift", {"request_id": rid(), "reason": reason}, andre=ANDRE_TOKEN)
    assert r.status_code == status, (r.status_code, r.text)
    return r.json()


def test_ch3_restore_never_silently_voids_paid_invoices_and_the_reimport_rebuilds_them(keys, tmp_path, caplog):  # noqa: F811
    x = durable(keys, tmp_path)
    a = rr_invoice(x, "1000.00")
    _cs_a, pi_a, _ = pay(x, a)                                       # A: paid BEFORE the backup
    b = rr_invoice(x, "1100.00")
    cs_b = x.ok(checkout(x, b["invoice_id"]))["checkout"]             # B: its page exists before the backup
    c = rr_invoice(x, "1200.00")                                      # C: issued before; its page comes after
    # --- the coordinated backup: quiesce, snapshot, resume
    q = x.ok(x.post("/fin/v1/backup/quiesce", {"request_id": rid(), "minutes": 30}, andre=ANDRE_TOKEN))
    snapshot(tmp_path)
    assert x.ok(x.post("/fin/v1/backup/resume", {"request_id": rid(), "marker_id": q["marker"]["marker_id"]},
                       andre=ANDRE_TOKEN))["consistent"] is True
    # --- after the backup: B and C are paid, A is partly refunded in the Dashboard
    _, pi_b, rb = pay(x, b, cs_b)
    cs_c, pi_c, rc = pay(x, c, method="card")
    assert rb["status"] == rc["status"] == "matched"
    x.sim.refund(pi_a, 25000)
    sent(x, "charge.refunded", {"id": x.sim.pis[pi_a]["latest_charge"], "object": "charge", "payment_intent": pi_a})
    before = {k: x.svc.db["invoices"][i["invoice_id"]]["status"] for k, i in (("a", a), ("b", b), ("c", c))}
    assert before == {"a": "paid", "b": "paid", "c": "paid"}
    assert x.bal("1060", None, "zbm") == sim_balance(x)
    # --- the restore: Finance's volume goes back to the backup; the ledger and Stripe do not
    restore(tmp_path)
    with pytest.raises(RuntimeError, match="refusing to start"):
        restart(x, tmp_path)
    y = restart(x, tmp_path, FIN_RECONCILE_MODE="1")
    plan = _plan(y)
    assert not plan["fatal"] and plan["voidable"]["event_ids"]
    risky = {m["event_type"] for m in plan["money_at_risk"]}
    assert "journal_entry_posted" in risky                                         # the payments' journal entries
    body = {"request_id": rid(), "head_sha256": plan["head_sha256"], "void_lines": plan["voidable"]["lines"],
            "void_event_ids": plan["voidable"]["event_ids"]}
    r = y.post(RECONCILE, body, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "RESTORE_GUARD" in r.text                     # never a silent cleanup
    out = y.ok(y.post(RECONCILE, {**body, "request_id": rid(), "money_events_acknowledged": True}, andre=ANDRE_TOKEN))
    assert out["stripe_reimport_required"] is True and out["reimport_break_id"]
    z = restart(x, tmp_path)
    assert {k: z.svc.db["invoices"][i["invoice_id"]]["status"] for k, i in (("a", a), ("b", b), ("c", c))} == \
        {"a": "paid", "b": "issued", "c": "issued"}                               # what the backup knew
    brk = z.svc.db["breaks"][out["reimport_break_id"]]
    assert brk["status"] == "open" and brk["leg"] == "restore_reimport_required" and brk["since"] == TODAY
    assert z.bal("1060", None, "zbm") != sim_balance(z)                            # the books are behind Stripe
    # the backup was taken inside its pause window: the restored Finance stays paused until the marker is resumed
    # (AEGIS F6, fail closed); resuming it says the window is not a live one any more
    assert z.post("/fin/v1/invoices", {"request_id": rid()}, caller="onboarding").status_code == 503
    assert z.ok(z.post("/fin/v1/backup/resume", {"request_id": rid(), "marker_id": q["marker"]["marker_id"]},
                       andre=ANDRE_TOKEN))["consistent"] is False
    # --- CH-4: Stripe redelivers C's completed page; Finance lost that page: 2xx, but a break and an ALERT
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        r = sent(z, "checkout.session.completed", {"id": cs_c["session_id"], "object": "checkout.session"})
    assert r["status"] == "unknown_session_break"
    ub = [v for v in z.svc.db["breaks"].values() if v["leg"] == "stripe_unknown_session"]
    assert len(ub) == 1 and ub[0]["status"] == "open" and ub[0]["session_id"] == cs_c["session_id"]
    assert any("ALERT finance: stripe_unknown_session" in rec.getMessage() for rec in caplog.records)
    # --- the re-import: a dry run, then the real run naming its plan
    dry = z.ok(reimport(z))
    acts = [(a_["action"], a_.get("payment_intent")) for a_ in dry["plan"]["actions"]]
    assert ("relink_page", pi_c) in acts and ("book_payment", pi_b) in acts and ("book_payment", pi_c) in acts
    assert ("book_refund", pi_a) in acts
    assert z.svc.db["invoices"][b["invoice_id"]]["status"] == "issued"            # a dry run books nothing
    assert reimport(z, dry=False).status_code == 409                              # no plan named
    assert reimport(z, dry=False, plan="f" * 64).status_code == 409               # not the dry run's plan
    refeed(z)                                         # AEGIS r5: Andre confirms the bank lines were fed again
    real = z.ok(reimport(z, dry=False, plan=dry["plan"]["plan_sha256"]))
    assert real["failed"] == 0 and out["reimport_break_id"] in real["ready_to_lift"]
    assert {k: z.svc.db["invoices"][i["invoice_id"]]["status"] for k, i in (("a", a), ("b", b), ("c", c))} == \
        {"a": "paid", "b": "paid", "c": "paid"}
    assert z.svc.db["invoices"][a["invoice_id"]]["refunded"] == "250.00"
    assert z.bal("1060", None, "zbm") == sim_balance(z)                           # the books equal Stripe again
    assert z.svc.db["breaks"][ub[0]["break_id"]]["status"] == "resolved"
    # idempotent: nothing left to do, nothing booked twice
    again = z.ok(reimport(z))
    assert again["plan"]["actions"] == []
    assert z.ok(z.get("/fin/v1/integrity"))["status"] == "green"
    # AEGIS r3: the lockdown stays on until Andre lifts it, explicitly
    assert z.svc.restore_lockdown() is not None and z.ok(z.get("/health"))["status"] == "degraded"
    lift(z)
    assert z.svc.restore_lockdown() is None and z.svc.db["breaks"][out["reimport_break_id"]]["status"] == "resolved"


def test_ch3_quiesce_refuses_writes_and_resume_reports_consistency(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    q = x.ok(x.post("/fin/v1/backup/quiesce", {"request_id": rid(), "minutes": 5}, andre=ANDRE_TOKEN))
    m = q["marker"]
    assert m["log_head_seq"] == len(x.svc.log) and x.ledger.of_type("backup_marker_recorded")
    r = x.post("/fin/v1/invoices", {"request_id": rid()}, caller="onboarding")
    assert r.status_code == 503 and "QUIESCED" in r.text
    payload, sig = x.sim.event("payout.paid", {"id": "po_0001sim", "object": "payout"})
    assert x.post("/fin/v1/stripe/events", {"request_id": rid(), "payload": payload, "signature": sig},
                  caller="rail_gateway").status_code == 503                           # Stripe retries later
    assert x.get("/fin/v1/breaks").status_code == 200                                 # reads still answer
    assert x.post("/fin/v1/backup/quiesce", {"request_id": rid()}, andre=ANDRE_TOKEN).status_code == 503
    out = x.ok(x.post("/fin/v1/backup/resume", {"request_id": rid(), "marker_id": m["marker_id"]}, andre=ANDRE_TOKEN))
    assert out["consistent"] is True
    assert x.post("/fin/v1/invoices", {"request_id": rid()}, caller="onboarding").status_code == 422   # open again
    # a window that ran out before resume is not a consistent backup
    q = x.ok(x.post("/fin/v1/backup/quiesce", {"request_id": rid(), "minutes": 5}, andre=ANDRE_TOKEN))
    x.clock.advance(minutes=6)
    assert x.post("/fin/v1/invoices", {"request_id": rid()}, caller="onboarding").status_code == 422   # expired
    assert x.ok(x.post("/fin/v1/backup/resume", {"request_id": rid(), "marker_id": q["marker"]["marker_id"]},
                       andre=ANDRE_TOKEN))["consistent"] is False
    assert x.post("/fin/v1/backup/quiesce", {"request_id": rid()}, caller="onboarding").status_code in (401, 403)


def test_ch3_reimport_is_andre_only_bounded_and_refuses_a_partial_listing(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    body = {"request_id": rid(), "since": TODAY, "until": TODAY, "dry_run": True}
    assert x.post("/fin/v1/stripe/reimport", body, caller="onboarding").status_code in (401, 403)
    assert x.post("/fin/v1/stripe/reimport", body, caller="ops_cron").status_code == 403
    assert reimport(x, since="2026-06-01", until=TODAY).status_code == 422          # more than 92 days
    assert reimport(x, since="2026-10-03", until="2026-10-03").status_code == 422   # the future
    assert reimport(x, since=TODAY, until="2026-10-01").status_code == 422          # backwards
    x.sim.fail_next = [500]
    assert reimport(x).status_code == 503                                             # nothing planned
    assert x.ok(reimport(x))["plan"]["actions"] == []


def test_ch4_a_paid_page_finance_does_not_know_opens_a_break_but_answers_2xx(keys, tmp_path, caplog):  # noqa: F811
    x = durable(keys, tmp_path)
    inv = rr_invoice(x, "300.00")
    cs = x.ok(checkout(x, inv["invoice_id"]))["checkout"]
    x.sim.pay(cs["session_id"])
    del x.svc.db["stripe_sessions"][cs["session_id"]]          # lost (a restore, another environment, a lost commit)
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        r = x.post("/fin/v1/stripe/events", dict(zip(("payload", "signature"),
                                                      x.sim.event("checkout.session.completed",
                                                                  {"id": cs["session_id"],
                                                                   "object": "checkout.session"})),
                                                  request_id=rid()), caller="rail_gateway")
    assert r.status_code == 200 and r.json()["status"] == "unknown_session_break"
    (b,) = [v for v in x.svc.db["breaks"].values() if v["leg"] == "stripe_unknown_session"]
    assert b["difference"] == "300.00" and b["severity"] == "high" and b["owner"] == "andre"
    assert any("stripe_unknown_session" in rec.getMessage() for rec in caplog.records)
    # an unpaid page Finance does not know stays the quiet "unknown_session" it was
    other = x.sim.event("checkout.session.expired", {"id": "cs_test_9999abc", "object": "checkout.session"})
    r = x.post("/fin/v1/stripe/events", {"request_id": rid(), "payload": other[0], "signature": other[1]},
               caller="rail_gateway")
    assert r.status_code == 200 and r.json()["status"] == "unknown_session"
