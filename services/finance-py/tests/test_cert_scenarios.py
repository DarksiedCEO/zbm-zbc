"""Spec §F scenario certification tests S1-S14."""

from __future__ import annotations

import random
from decimal import Decimal

from helpers import ANDRE_TOKEN, Harness, rid
from money import WIRE_PATTERN


def test_s1_rules_not_approved_every_action_refused(h):
    r = h.ok(h.handoff())
    assert not r["allowed"] and "RULES_NOT_IN_FORCE" in r["reason"]
    p = h.ok(h.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "clip-a", "kind": "clipper",
                                        "declared_country": "US"}, caller="clipper_network"))
    assert not p["allowed"] and any("RULES_NOT_IN_FORCE" in u for u in p["unmet"])
    for path, body, kw in [
        ("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, {"caller": "scheduler"}),
        ("/fin/v1/reconciliations/run", {"request_id": rid()}, {"caller": "scheduler"}),
        ("/fin/v1/treasury/sweeps", {"request_id": rid(), "amount": "1.00"}, {"caller": "scheduler"}),
        ("/fin/v1/rate-cards/proposals", {"request_id": rid(), "campaign_id": "c", "creator_rate_per_1000":
            {t: "1.00" for t in ("T0", "T1", "T2", "T3")}, "max_paid_views_per_clip": 10,
                                          "effective_at": "2026-09-01T00:00:00Z"}, {"andre": ANDRE_TOKEN}),
        ("/fin/v1/jobs/accrual/run", {"request_id": rid()}, {"caller": "scheduler"}),
    ]:
        r = h.post(path, body, **kw)
        assert r.status_code == 409, (path, r.text)
        assert r.json()["reasons"][0]["code"] == "RULES_NOT_IN_FORCE" and r.json()["reasons"][0]["rule_id"] == "FIN-00"
    rd = h.ok(h.get("/fin/v1/clients/client-1/billing-readiness", caller="onboarding"))
    assert not rd["allowed"] and any("RULES_NOT_IN_FORCE" in u for u in rd["unmet"])


def _clean(hr):
    hr.fund_campaign()
    hr.payee()
    return hr.accrue()


def test_s2_clean_accrual_one_balanced_entry(hr):
    p = _clean(hr)
    assert p["amount"] == "29.01" and p["revenue_amount"] == "49.38" and p["paid_views"] == 12345
    assert p["status"] == "accrued" and len(p["entry_ids"]) == 1
    e = hr.svc.entries_by_id[p["entry_ids"][0]]
    assert e["memo_code"] == "F2" and len(e["lines"]) == 4
    assert hr.bal("2020", "payee:clip-a") == Decimal("29.01")
    assert hr.bal("2010", "campaign:camp-1") == Decimal("950.62")
    assert hr.bal("4010") == Decimal("49.38") and hr.bal("5010") == Decimal("29.01")
    hr.assert_books_balance()


def test_s3_weekly_run_approve_release_paid_recon_green(hr):
    _clean(hr)
    b, rel = hr.full_payout()
    it = rel["batch"]["items"][0]
    assert it["status"] == "submitted" and it["net"] == "29.01"
    hr.pay_items(rel["batch"])
    bb = hr.batch(b["batch_id"])
    assert bb["status"] == "settled" and bb["items"][0]["status"] == "paid"
    flows = [e["memo_code"] for e in hr.svc.entries]
    for f in ("F1", "F2", "F4a", "F4d", "F4e"):
        assert f in flows
    assert hr.bal("2020", "payee:clip-a") == 0 and hr.bal("2030") == 0
    r = hr.recon()
    assert r["fc01"], [l for l in r["recon"]["legs"] if l["status"] != "matched"]
    assert not [b for b in hr.ok(hr.get("/fin/v1/breaks"))["breaks"] if b["status"] == "open"]
    hr.assert_books_balance()


def _week(hr, days=7):
    hr.clock.advance(days=days)


def test_s4_below_minimum_carried_forward_then_paid_next_week(hr):
    hr.fund_campaign()
    hr.payee()
    hr.accrue("s-small", views=2000)                       # 4.70
    hr.recon()
    r = hr.run()
    assert r["run_empty"] and r["batch"] is None and not r["blocked"]
    ex = r["excluded"][0]
    assert ex["payee_id"] == "clip-a" and [x["code"] for x in ex["reasons"]] == ["BELOW_MINIMUM"]
    assert hr.ok(hr.get(f"/fin/v1/payables/{ex['payable_ids'][0]}"))["status"] == "accrued"
    _week(hr)
    hr.accrue("s-small-2", views=3000)                     # 7.05 -> 11.75
    b, rel = hr.full_payout()
    assert rel["batch"]["items"][0]["net"] == "11.75" and rel["batch"]["items"][0]["status"] == "submitted"
    assert len(rel["batch"]["items"][0]["payable_ids"]) == 2


def test_s5_backup_withholding_24_percent(hr):
    hr.fund_campaign()
    hr.payee()
    hr.ok(hr.post("/fin/v1/payees/clip-a/tax/b-notices", {"request_id": rid(), "cp2100_received_on": "2026-09-30",
                                                          "first_b_notice_sent_on": "2026-10-01",
                                                          "start_withholding": True}, andre=ANDRE_TOKEN))
    hr.accrue()
    b, rel = hr.full_payout()
    it = rel["batch"]["items"][0]
    assert (it["gross"], it["withheld"], it["net"]) == ("29.01", "6.96", "22.05")
    assert hr.bal("2040") == Decimal("6.96")
    hr.pay_items(rel["batch"])
    assert hr.bal("2020", "payee:clip-a") == 0
    hr.assert_books_balance()


def test_s6_revision_before_release_f5_and_after_release_f5a_breach_until_top_up(hr):
    hr.fund_campaign()
    hr.payee()
    hr.accrue()
    hr.vi.add_clawback("sub-1", -2345)                     # 12,345 -> 10,000
    hr.ok(hr.post("/fin/v1/jobs/clawback-sync/run", {"request_id": rid()}, caller="scheduler"))
    p = hr.ok(hr.get(f"/fin/v1/payables/{hr.svc.pay_by_sub['sub-1']}"))
    assert p["current_amount"] == "23.50" and p["adjustments"][0]["payable_delta"] == "5.51"
    assert "F5" in [e["memo_code"] for e in hr.svc.entries]
    assert hr.bal("2020", "payee:clip-a") == Decimal("23.50")
    # after release: sweep the margin first so a clawback after payment breaks the invariant
    b, rel = hr.full_payout()
    hr.pay_items(rel["batch"])
    hr.recon()
    sw = hr.ok(hr.get("/fin/v1/treasury"))["sweepable"]
    op = hr.ok(hr.post("/fin/v1/treasury/sweeps", {"request_id": rid(), "amount": sw}, caller="scheduler"), 201)["operation"]
    hr.ok(hr.post(f"/fin/v1/treasury/sweeps/{op['op_id']}/decision", {"request_id": rid(), "content_sha256":
                                                                      op["content_sha256"], "decision": "approve"},
                  andre=ANDRE_TOKEN))
    assert hr.ok(hr.get("/fin/v1/treasury"))["journal"]["gap"] == "0.00"
    hr.vi.add_clawback("sub-1", -1000)                     # 10,000 -> 9,000 after it was paid
    hr.clock.advance(days=7)
    hr.ok(hr.post("/fin/v1/jobs/clawback-sync/run", {"request_id": rid()}, caller="scheduler"))
    assert "F5a" in [e["memo_code"] for e in hr.svc.entries]
    assert hr.bal("1200", "payee:clip-a") == Decimal("2.35")
    assert hr.ok(hr.get("/fin/v1/treasury"))["journal"]["ok"] is False
    assert hr.ledger.of_type("treasury_breach")
    hr.recon()
    r = hr.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, caller="scheduler")
    assert r.status_code == 409 and "TREASURY_BREACH" in [x["code"] for x in r.json()["reasons"]]
    assert "FC-03" not in [c["control_id"] for c in hr.ok(hr.get("/fin/v1/controls"))["controls"]
                           if c["status"] == "green"]
    gap = Decimal(hr.ok(hr.get("/fin/v1/treasury"))["journal"]["gap"])
    assert gap == Decimal("-4.00")                        # revenue returned to the deposit, creator already paid
    hr.ok(hr.post("/fin/v1/treasury/top-ups", {"request_id": rid(), "amount": f"{-gap:.2f}",
                                                "reason_code": "clawback_after_release"}, andre=ANDRE_TOKEN))
    assert "F5c" in [e["memo_code"] for e in hr.svc.entries]
    hr.accrue("sub-2", views=6000)                        # 14.10 of new earnings: nets the 2.35 receivable
    hr.recon()
    r = hr.run()
    assert r["batch"], r["excluded"]
    it = r["batch"]["items"][0]
    assert (it["gross"], it["netted"], it["net"]) == ("14.10", "2.35", "11.75")
    hr.assert_books_balance()


def test_s7_over_budget_certification_holds_nothing_posts(hr):
    hr.fund_campaign(budget="10.00")
    hr.payee()
    n = len(hr.svc.entries)
    r = hr.ok(hr.handoff())
    assert not r["allowed"] and "OVER_BUDGET" in r["reason"]
    p = hr.ok(hr.get(f"/fin/v1/payables/{r['reference']}"))
    assert p["status"] == "over_budget_hold" and p["entry_ids"] == []
    assert len(hr.svc.entries) == n and hr.ledger.of_type("payable_over_budget_hold")


def test_s8_refund_of_unspent_deposit_only_without_open_dispute(hr):
    inv = hr.fund_campaign()
    hr.payee()
    hr.accrue()
    hr.recon()
    d = hr.ok(hr.post("/fin/v1/disputes", {"request_id": rid(), "kind": "invoice_dispute", "invoice_id":
                                           inv["invoice_id"], "amount": "100.00"}, andre=ANDRE_TOKEN), 201)["dispute"]
    r = hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler")
    assert r.status_code == 409 and "DISPUTE_OPEN" in [x["code"] for x in r.json()["reasons"]]
    hr.ok(hr.post(f"/fin/v1/disputes/{d['dispute_id']}/outcome", {"request_id": rid(), "outcome": "withdrawn"},
                  andre=ANDRE_TOKEN))
    rf = hr.ok(hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler"), 201)["refund"]
    assert rf["amount"] == "950.62"
    res = hr.ok(hr.post(f"/fin/v1/refunds/{rf['refund_id']}/decision", {"request_id": rid(), "content_sha256":
                                                                       rf["content_sha256"], "decision": "approve"},
                        andre=ANDRE_TOKEN))
    assert res["refund"]["status"] == "paid"
    assert hr.bal("2010", "campaign:camp-1") == 0 and hr.bal("2050") == 0
    assert [e["memo_code"] for e in hr.svc.entries][-2:] == ["F6", "F6p"]
    hr.assert_books_balance()


def _zbm_invoice(hr, kind="service", code="creative_services", **extra):
    body = {"request_id": rid(), "entity": "zbm", "client_id": "zbm-client-1", "kind": kind,
            "lines": [{"line_code": code, "quantity": 2, "unit_price": "250.00"}], "payment_methods": ["ach"],
            "legal_ref": {"doc_id": "msa-1", "version": 1, "doc_sha256": "b" * 64, "acceptance_id": "acc-z"}, **extra}
    return hr.post("/fin/v1/invoices", body, caller="onboarding")


def test_s9_zbm_invoice_posts_only_zbm_accounts(hr):
    inv = hr.ok(_zbm_invoice(hr), 201)["invoice"]
    zbc_before = len([e for e in hr.svc.entries if e["entity"] == "zbc"])
    hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision", {"request_id": rid(), "content_sha256":
                                                                     inv["content_sha256"], "decision": "approve"},
                  andre=ANDRE_TOKEN))
    hr.receive("zbm", "1010", "500.00", inv["invoice_id"])
    zbm = [e for e in hr.svc.entries if e["entity"] == "zbm"]
    assert [e["memo_code"] for e in zbm] == ["F11", "F11a"]
    for e in zbm:
        assert {l["account"] for l in e["lines"]} <= {"1010", "1100", "4110"}
    assert len([e for e in hr.svc.entries if e["entity"] == "zbc"]) == zbc_before
    assert hr.bal("4110", entity="zbm") == Decimal("500.00") and hr.bal("1100", entity="zbm") == 0
    hr.assert_books_balance()


def test_s10_close_checklist_lock_and_correction_in_open_period(hr):
    hr.recon()
    for t in ("preclose_review", "subledgers_closed", "balance_sheet_recs", "restricted_recon_signed",
              "exception_aging", "rollforward_2020_1200"):
        r = hr.ok(hr.post(f"/fin/v1/close/zbc/2026-09/tasks/{t}", {"request_id": rid()}, caller="scheduler"))
        assert r["status"] == "done", r
    lock = hr.ok(hr.post("/fin/v1/close/zbc/2026-09/approve", {"request_id": rid()}, andre=ANDRE_TOKEN))
    assert lock["locked"] and lock["statements"] == "draft"
    bad = hr.post("/fin/v1/journal/zbc/corrections", {"request_id": rid(), "effective_date": "2026-09-30", "lines": [
        {"account": "1010", "debit": "5.00", "credit": "0.00"}, {"account": "3000", "debit": "0.00", "credit": "5.00"}]},
        andre=ANDRE_TOKEN)
    assert bad.status_code == 422 and "PERIOD_LOCKED" in bad.text
    e = hr.ok(hr.post("/fin/v1/journal/zbc/corrections", {"request_id": rid(), "effective_date": "2026-10-02", "lines": [
        {"account": "1010", "debit": "5.00", "credit": "0.00"}, {"account": "3000", "debit": "0.00", "credit": "5.00"}]},
        andre=ANDRE_TOKEN), 201)["entry"]
    rev = hr.ok(hr.post("/fin/v1/journal/zbc/corrections", {"request_id": rid(), "effective_date": "2026-10-02",
                                                            "reverses_entry_id": e["entry_id"]}, andre=ANDRE_TOKEN), 201)
    assert rev["entry"]["reverses_entry_id"] == e["entry_id"] and rev["entry"]["period"] == "2026-10"
    hr.assert_books_balance()


def test_s11_1099_tracker_and_fc08(hr):
    hr.fund_campaign(budget="5000.00")
    hr.payee()
    hr.accrue(views=893617)                                # 2,100.00
    hr.recon()
    r = hr.run()
    assert r["run_empty"] and r["exceptions_opened"]      # first payout above 500.00 -> exception queue
    ex = r["exceptions_opened"][0]
    assert "LIMIT_EXCEEDED" in [x["code"] for x in r["excluded"][0]["reasons"]]
    hr.ok(hr.post(f"/fin/v1/exceptions/{ex}/decision", {"request_id": rid(), "decision": "approve"}, andre=ANDRE_TOKEN))
    b = hr.run()["batch"]
    hr.approve(b)
    hr.fund_batch(b["batch_id"])
    hr.clock.advance(hours=13)
    hr.recon()
    rel = hr.release(b["batch_id"])
    hr.pay_items(rel["batch"])
    f = hr.ok(hr.get("/fin/v1/tax/1099/2026", caller=None, andre=ANDRE_TOKEN))
    rec = f["records"][0]
    assert rec["box1_nonemployee_comp"] == "2100.00" and rec["form_1099_required"] is True and rec["status"] == "required"
    hr.cmp.rows_status["US-IRS-1099NEC"] = "unverified"
    f = hr.ok(hr.get("/fin/v1/tax/1099/2026", caller=None, andre=ANDRE_TOKEN))
    assert f["records"][0]["form_1099_required"] is None and f["records"][0]["status"] == "rule_not_in_force"
    hr.clock.at = hr.clock.at.replace(month=12, day=1)
    fc8 = [c for c in hr.ok(hr.get("/fin/v1/controls"))["controls"] if c["control_id"] == "FC-08"][0]
    assert fc8["status"] == "red" and "2026-12-01" in fc8["why"]
    hr.ok(hr.post("/fin/v1/tax/readiness", {"request_id": rid(), "tcc_obtained_at": "2026-10-10",
                                            "iris_test_passed_at": "2026-11-01", "ftb_swift_ready_at": "2026-11-02"},
                  andre=ANDRE_TOKEN))
    fc8 = [c for c in hr.ok(hr.get("/fin/v1/controls"))["controls"] if c["control_id"] == "FC-08"][0]
    assert fc8["status"] == "green"


def test_s12_subscription_invoice_missing_any_arl_field_refused(hr):
    full = {"consent_artifact_ref": "consent-1", "cancel_medium": "same_medium_and_online",
            "annual_reminder_due": "2027-09-01", "price_change_notice_days": 14, "trial_days": 0,
            "trial_reminder_days": 0}
    for f in full:
        rec = {k: v for k, v in full.items() if k != f}
        r = _zbm_invoice(hr, kind="subscription", code="subscription_fee", recurring=rec)
        assert r.status_code == 422 and "RECURRING_INCOMPLETE" in r.text, f
    r = _zbm_invoice(hr, kind="subscription", code="subscription_fee")
    assert r.status_code == 422 and "RECURRING_INCOMPLETE" in r.text
    inv = hr.ok(_zbm_invoice(hr, kind="subscription", code="subscription_fee", recurring=full), 201)["invoice"]
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision", {"request_id": rid(), "content_sha256":
                                                                  inv["content_sha256"], "decision": "approve"},
                andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "FIN-CQ-10" in r.text          # ARL counsel row unverified -> no issue


def test_s13_card_fee_line_or_surcharge_text_refused(hr):
    r = hr.post("/fin/v1/invoices", {"request_id": rid(), "entity": "zbm", "client_id": "c", "kind": "service",
                                     "lines": [{"line_code": "card_fee", "quantity": 1, "unit_price": "3.00"}],
                                     "payment_methods": ["ach"], "legal_ref": {"doc_id": "m", "version": 1,
                                                                               "doc_sha256": "b" * 64,
                                                                               "acceptance_id": "a"}}, caller="onboarding")
    assert r.status_code == 422
    for text in ("3% surcharge", "Card fee", "convenience fee", "Processing-Fee"):
        r = hr.post("/fin/v1/invoices", {"request_id": rid(), "entity": "zbm", "client_id": "c", "kind": "service",
                                         "lines": [{"line_code": "creative_services", "quantity": 1,
                                                    "unit_price": "3.00", "description": text}],
                                         "payment_methods": ["ach"], "legal_ref": {"doc_id": "m", "version": 1,
                                                                                   "doc_sha256": "b" * 64,
                                                                                   "acceptance_id": "a"}},
                    caller="onboarding")
        assert r.status_code == 422 and "SURCHARGE_REFUSED" in r.text, text
    r = hr.post("/fin/v1/invoices", {"request_id": rid(), "entity": "zbm", "client_id": "c", "kind": "service",
                                     "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": "3.00"}],
                                     "payment_methods": ["card"], "legal_ref": {"doc_id": "m", "version": 1,
                                                                                "doc_sha256": "b" * 64,
                                                                                "acceptance_id": "a"}},
                caller="onboarding")
    assert r.status_code == 422 and "CARD_NOT_ALLOWED" in r.text   # media/non-RR work: ACH or wire only (FIN-31)


def test_s14_precision_half_up_once_per_payable(hr):
    import money as M
    assert M.fmt(M.payable_amount(2500, Decimal("0.01"))) == "0.03"            # half-even would give 0.02
    hr.fund_campaign(rate="0.01", client_rate="0.02", budget="100.00")
    hr.payee()
    p = hr.accrue(views=2500)
    assert p["amount"] == "0.03" and p["revenue_amount"] == "0.05"


def test_s14_ten_thousand_random_payables_sum_and_balance():
    """10,000 payables through the real accrual path (service calls, record-first, fakes), one weekly run: the sum
    of the items equals the batch total, the journal balances to zero, every amount is a §1 string."""
    import time
    hr = Harness().ready()
    rnd = random.Random(20260926)
    rates = ["0.01", "0.37", "1.00", "2.35", "3.99"]
    for i, rate in enumerate(rates):
        hr.fund_campaign(campaign=f"camp-{i}", client=f"client-{i}", budget="999999.00", rate=rate,
                         client_rate=f"{Decimal(rate) * 2:.2f}")
    payees = [f"clip-{i:02d}" for i in range(50)]
    for pid in payees:
        hr.payee(pid)
    c0 = time.process_time()      # wave 25 (M2): the S14 budget is CPU work, not a starved wall clock
    total_expected = Decimal("0.00")
    per_payee: dict = {}
    for n in range(10_000):
        sid, pid, ci = f"s{n}", payees[n % 50], rnd.randrange(5)
        views = rnd.randrange(0, 1000)
        hr.vi.certify(sid, pid, f"camp-{ci}", views, "2026-09-20T12:00:00Z")
        hr.cmp.rule(f"r{n}", sid)
        r = hr.svc.accept_handoff("creative_production", f"h{n}", sid, {
            "submission_id": sid, "eligible": True, "blockers": [], "clip_review_outcome": "pass",
            "verification": {"verified": True, "reason": "", "attestation_id": None},
            "compliance": {"allowed": True, "reason": "", "reference": f"r{n}"}, "note": None})
        assert r["allowed"], r
        amt = Decimal(views) * Decimal(rates[ci]) / 1000
        q = amt.quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
        total_expected += q
        per_payee[pid] = per_payee.get(pid, Decimal("0.00")) + q
    hr.recon()
    b = hr.run()["batch"]
    assert b["totals"]["gross"] == f"{total_expected:.2f}"
    assert sum(Decimal(i["net"]) for i in b["items"]) == Decimal(b["totals"]["net"])
    for it in b["items"]:
        assert Decimal(it["gross"]) == per_payee[it["payee_id"]]
        for k in ("gross", "netted", "withheld", "net"):
            assert WIRE_PATTERN.fullmatch(it[k])
    for e in hr.svc.entries:
        for l in e["lines"]:
            assert WIRE_PATTERN.fullmatch(l["debit"]) and WIRE_PATTERN.fullmatch(l["credit"])
    hr.assert_books_balance()
    assert time.process_time() - c0 < 600
