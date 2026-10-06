"""Commissions: a percentage of what the client ACTUALLY paid (a Finance event), Decimal-exact with finance-py's
half-up cent rounding on the cumulative base, capped at the won value; refunds and chargebacks claw back unpaid
commission first; payouts are requests to Finance through a port, never Stripe, never actually paid here."""

from decimal import Decimal

import money
from helpers import fin_id, Harness, RecordingPayouts, rid, wired_ports
from intelligences import i11_commission


DUP = "fin-evt-" + "ab" * 20


def _h(tmp_path, **over):
    return Harness(tmp_path, ports=wired_ports(**over))


def test_nothing_accrues_before_a_payment(tmp_path):
    h = _h(tmp_path)
    d = h.won_deal()
    assert d["commission"]["accrued"] == "0.00"
    assert h.ok(h.job("payout-request"))["requested"] == 0


def test_payment_events_only_from_finance_and_only_on_won_deals(tmp_path):
    h = _h(tmp_path)
    p = h.partner()
    d = h.deal(p["partner_id"])
    r = h.post("/finance/events", {"request_id": rid(), "finance_event_id": fin_id(), "deal_id": d["deal_id"],
                                   "kind": "payment", "amount": "10.00", "currency": "USD"})
    h.refused(r, 403, "CALLER_NOT_ALLOWED")
    h.refused(h.money_event(d["deal_id"], "payment", "10.00"), 409, "DEAL_NOT_WON")


def test_commission_on_paid_amount(tmp_path):
    h = _h(tmp_path)
    d = h.won_deal(value="8000.00", rate="10.00")
    d = h.ok(h.money_event(d["deal_id"], "payment", "2500.00"))
    assert d["commission"]["accrued"] == "250.00" and d["commission"]["unpaid"] == "250.00"


def test_half_up_rounding_matches_finance_q(tmp_path):
    h = _h(tmp_path)
    d = h.won_deal(rate="12.50")
    d = h.ok(h.money_event(d["deal_id"], "payment", "0.04"))           # 0.005 -> 0.01 (half up)
    assert d["commission"]["accrued"] == "0.01"
    assert money.commission_total(Decimal("0.03"), Decimal("12.50")) == Decimal("0.00")   # 0.00375 -> 0.00


def test_cumulative_base_prevents_rounding_drift(tmp_path):
    h = _h(tmp_path)
    d = h.won_deal(rate="50.00")
    for _ in range(3):
        d = h.ok(h.money_event(d["deal_id"], "payment", "0.01"))
    assert d["commission"]["accrued"] == "0.02"          # 0.015 on 0.03 -> 0.02, not 3 x 0.01


def test_base_capped_at_won_value(tmp_path):
    h = _h(tmp_path)
    d = h.won_deal(value="1000.00", rate="10.00")
    h.ok(h.money_event(d["deal_id"], "payment", "900.00"))
    d = h.ok(h.money_event(d["deal_id"], "payment", "500.00"))
    assert d["commission"]["base"] == "1000.00" and d["commission"]["accrued"] == "100.00"


def test_floats_and_bad_amounts_refused(tmp_path):
    h = _h(tmp_path)
    d = h.won_deal()
    for bad in (10.0, 10, "10", "0.00", "-5.00", "1e2", "10.001"):
        r = h.post("/finance/events", {"request_id": rid(), "finance_event_id": fin_id(), "deal_id": d["deal_id"],
                                       "kind": "payment", "amount": bad, "currency": "USD"}, caller="finance_31")
        assert r.status_code == 422, bad
    r = h.post("/finance/events", {"request_id": rid(), "finance_event_id": fin_id(), "deal_id": d["deal_id"],
                                   "kind": "payment", "amount": "1.00", "currency": "EUR"}, caller="finance_31")
    assert r.status_code == 422


def test_finance_event_id_is_unique(tmp_path):
    h = _h(tmp_path)
    d = h.won_deal()
    h.ok(h.money_event(d["deal_id"], "payment", "100.00", ev=DUP))
    d2 = h.ok(h.money_event(d["deal_id"], "payment", "100.00", ev=DUP))     # same facts: once
    assert d2["commission"]["client_paid"] == "100.00"
    h.refused(h.money_event(d["deal_id"], "payment", "999.00", ev=DUP), 409, "FINANCE_EVENT_REUSED")
    h.refused(h.money_event(d["deal_id"], "refund", "100.00", ev=DUP), 409, "FINANCE_EVENT_REUSED")


def test_refund_claws_back_unpaid(tmp_path):
    h = _h(tmp_path)
    d = h.won_deal(rate="10.00")
    h.ok(h.money_event(d["deal_id"], "payment", "1000.00"))
    d = h.ok(h.money_event(d["deal_id"], "refund", "400.00"))
    assert d["commission"]["accrued"] == "60.00" and d["commission"]["unpaid"] == "60.00"
    d = h.ok(h.money_event(d["deal_id"], "chargeback", "600.00"))
    assert d["commission"]["accrued"] == "0.00" and d["commission"]["shortfall"] == "0.00"
    assert h.ledger.of_type("commission_clawback")
    d = h.ok(h.money_event(d["deal_id"], "refund", "50.00"))              # net below zero: base stays 0.00
    assert d["commission"]["base"] == "0.00"


def test_clawback_cuts_queued_payout_before_finance_takes_it(tmp_path):
    h = _h(tmp_path, payouts=RecordingPayouts("not_wired"))
    h.ports.payouts.wired = False
    d = h.won_deal(rate="10.00")
    h.ok(h.money_event(d["deal_id"], "payment", "1000.00"))
    out = h.ok(h.job("payout-request"))
    assert out["requested"] == 1 and out["not_wired"] == 1
    pay = h.ok(h.get("/payouts"))[0]
    assert pay["status"] == "queued" and pay["amount"] == "100.00" and "tax_info_ref" not in pay
    d = h.ok(h.money_event(d["deal_id"], "refund", "250.00"))
    pay = h.ok(h.get("/payouts"))[0]
    assert pay["amount"] == "75.00" and d["commission"]["settled"] == "75.00" and d["commission"]["unpaid"] == "0.00"
    h.ok(h.money_event(d["deal_id"], "chargeback", "750.00"))
    pay = h.ok(h.get("/payouts"))[0]
    assert pay["status"] == "cancelled" and pay["amount"] == "0.00"


def test_clawback_after_finance_took_it_is_a_shortfall_task(tmp_path):
    h = _h(tmp_path)
    d = h.won_deal(rate="10.00")
    h.ok(h.money_event(d["deal_id"], "payment", "1000.00"))
    out = h.ok(h.job("payout-request"))
    assert out["with_finance"] == 1
    call = h.ports.payouts.calls[0][1]
    assert call["amount"] == "100.00" and call["currency"] == "USD" and call["tax_info_ref"].startswith("vault:tax:")
    d = h.ok(h.money_event(d["deal_id"], "refund", "1000.00"))
    assert d["commission"]["shortfall"] == "100.00" and d["commission"]["unpaid"] == "-100.00"
    assert any(t["kind"] == "clawback_shortfall" for t in h.ok(h.get("/tasks?status=open")))
    h.ok(h.money_event(d["deal_id"], "payment", "1000.00"))
    assert h.ok(h.job("payout-request"))["requested"] == 0     # already settled: never paid twice


def test_payout_needs_payee_and_finance_confirms_paid(tmp_path):
    h = _h(tmp_path)
    p = h.partner()
    h.rate(p["partner_id"])
    d = h.deal(p["partner_id"])
    h.ok(h.post(f"/partner-deals/{d['deal_id']}/won", {"request_id": rid(), "agreement_kind": "referral_agreement"},
                andre=True))
    h.ok(h.money_event(d["deal_id"], "payment", "100.00"))
    assert h.ok(h.job("payout-request"))["payee_missing"] == 1
    h.ok(h.payee(p["partner_id"]))
    assert h.ok(h.job("payout-request"))["with_finance"] == 1
    pay = h.ok(h.get("/payouts"))[0]
    h.refused(h.post(f"/finance/payouts/{pay['payout_id']}/paid", {"request_id": rid(), "finance_ref": fin_id("pay")}),
              403)
    paid = h.ok(h.post(f"/finance/payouts/{pay['payout_id']}/paid", {"request_id": rid(), "finance_ref": fin_id("pay")},
                       caller="finance_31"))
    assert paid["status"] == "paid"
    h.refused(h.post(f"/finance/payouts/{pay['payout_id']}/paid", {"request_id": rid(), "finance_ref": fin_id("pay")},
                     caller="finance_31"), 409, "PAYOUT_NOT_WITH_FINANCE")


def test_default_ports_never_pay(h):
    assert h.svc.ports.payouts.wired is False
    from ports import NotWiredPayouts
    assert NotWiredPayouts().request_payout("x", {}).status == "not_wired"


def test_commission_unit_vectors():
    st = i11_commission.fresh()
    r = i11_commission.apply(st, "payment", "333.33", "33.33", "1000.00", [])
    assert r["state"]["accrued"] == "111.10"                 # 111.099889 -> 111.10
    r = i11_commission.apply(r["state"], "payment", "0.01", "33.33", "1000.00", [])
    assert r["state"]["accrued"] == "111.10"                 # 333.34 * 0.3333 = 111.103222 -> 111.10
    r = i11_commission.apply(r["state"], "refund", "333.34", "33.33", "1000.00", [])
    assert r["state"]["accrued"] == "0.00" and r["accrued_delta"] == "-111.10"
