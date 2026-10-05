"""Media billing (ADR 0009 amendment, Oct 5 2026; founder decisions M1-M8; rule FIN-31).

ZBM buys media as principal, every buy prepaid by ACH or wire, the vendor paid only from cleared money, the fee a
markup on the vendor cost (15% default, set per buy), the client shown a breakout or one blended price, revenue and
cost posted together when the media has run. Card only on Revenue Recovery-only invoices, and still off (D11).
Every matched payment produces a client receipt that the scheduler sends through the client-mail port.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from helpers import ANDRE_TOKEN, Harness, rid
from intelligences import i01_journal as J

LEGAL = {"doc_id": "io-1", "version": 1, "doc_sha256": "c" * 64, "acceptance_id": "acc-io-1"}


def buy_body(**over):
    body = {"request_id": rid("mb"), "client_id": "zbm-client-1", "media_type": "out_of_home",
            "vendor_ref": "vendor-billboard-co", "description": "Digital billboard, I-405 northbound, 4 weeks",
            "flight_start": "2026-10-05", "flight_end": "2026-11-01", "media_cost": "10000.00",
            "display": "breakout", "legal_ref": LEGAL}
    body.update(over)
    return body


def create(hr, **over):
    return hr.ok(hr.post("/fin/v1/media-buys", buy_body(**over), andre=ANDRE_TOKEN), 201)


def issue(hr, inv):
    return hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision",
                         {"request_id": rid(), "content_sha256": inv["content_sha256"], "decision": "approve"},
                         andre=ANDRE_TOKEN))["invoice"]


def ref(tag="x"):
    return hashlib.sha256(f"vendor-pay-{tag}-{rid()}".encode()).hexdigest()


def pay_vendor(hr, buy_id, amount, paid_on="2026-10-07", tag="x"):
    return hr.post(f"/fin/v1/media-buys/{buy_id}/vendor-payments",
                   {"request_id": rid(), "amount": amount, "paid_on": paid_on, "method": "ach",
                    "payment_ref_sha256": ref(tag)}, andre=ANDRE_TOKEN)


def deliver(hr, buy_id, on="2026-11-02"):
    return hr.post(f"/fin/v1/media-buys/{buy_id}/delivery",
                   {"request_id": rid(), "delivered_on": on, "evidence_refs": ["pop-affidavit-1"]}, andre=ANDRE_TOKEN)


def buy(hr, buy_id):
    return hr.ok(hr.get(f"/fin/v1/media-buys/{buy_id}", caller="scheduler"))


def prepaid(hr, **over):
    """A buy created, invoiced, issued and paid; the clock at the payment day (Fri Oct 2 2026, LA)."""
    r = create(hr, **over)
    inv = issue(hr, r["invoice"])
    hr.receive("zbm", "1010", inv["total"], inv["invoice_id"])
    return r["media_buy"]["buy_id"], inv


def to_clear_day(hr):
    hr.clock.advance(days=5)                  # Fri Oct 2 -> Wed Oct 7 (3 business days, FIN_MEDIA_RELEASE_HOLD_BD)


def memos(hr, entity="zbm"):
    return [e["memo_code"] for e in hr.svc.entries if e["entity"] == entity]


# ------------------------------------------------------------------------------------------- the whole buy

def test_full_buy_principal_prepaid_collect_before_pay_revenue_with_cost(hr):
    r = create(hr)
    b, inv = r["media_buy"], r["invoice"]
    # M4: 15% default markup, one half-up quantize; M1: cost and fee both stored
    assert (b["media_cost"], b["markup_pct"], b["fee"], b["total"]) == ("10000.00", "15.00", "1500.00", "11500.00")
    assert [(l["line_code"], l["amount"]) for l in inv["lines"]] == [("media_spend", "10000.00"),
                                                                      ("media_fee", "1500.00")]
    assert inv["kind"] == "media_prepayment" and inv["payment_methods"] == ["ach", "wire"] and inv["status"] == "draft"
    assert [x["amount"] for x in inv["client_lines"]] == ["10000.00", "1500.00"]           # M5 breakout
    assert memos(hr) == []                                                                # a draft posts nothing

    inv = issue(hr, inv)
    assert memos(hr) == ["F12"]
    assert hr.bal("1100", "client:zbm-client-1", "zbm") == Decimal("11500.00")
    assert hr.bal("2120", f"buy:{b['buy_id']}", "zbm") == Decimal("11500.00")

    hr.receive("zbm", "1010", "11500.00", inv["invoice_id"])
    got = buy(hr, b["buy_id"])
    assert got["status"] == "prepaid" and got["prepayment"]["value_date"] == "2026-10-02"
    assert memos(hr) == ["F12", "F11a"]

    # M3: same day -> refused, nothing posted
    r = pay_vendor(hr, b["buy_id"], "10000.00", paid_on="2026-10-02")
    assert r.status_code == 409 and "COLLECT_BEFORE_PAY" in r.text and "2026-10-07" in r.text
    assert "record the vendor payment after that" in r.text      # the hold itself, not only the paid_on check
    assert memos(hr) == ["F12", "F11a"]

    to_clear_day(hr)
    hr.ok(pay_vendor(hr, b["buy_id"], "10000.00"))
    assert buy(hr, b["buy_id"])["status"] == "vendor_paid"
    assert hr.bal("1150", f"buy:{b['buy_id']}", "zbm") == Decimal("10000.00")

    # revenue only once the flight has run
    r = deliver(hr, b["buy_id"], on="2026-10-07")
    assert r.status_code == 409 and "flight runs until 2026-11-01" in r.text
    hr.clock.advance(days=27)                                                           # Tue Nov 3
    done = hr.ok(deliver(hr, b["buy_id"]))["media_buy"]
    assert done["status"] == "delivered" and done["delivered"]["evidence_refs"] == ["pop-affidavit-1"]
    assert memos(hr) == ["F12", "F11a", "F12v", "F12r"]
    f12r = [e for e in hr.svc.entries if e["memo_code"] == "F12r"][0]
    assert {(l["account"], l["debit"], l["credit"]) for l in f12r["lines"]} == {
        ("2120", "11500.00", "0.00"), ("4120", "0.00", "11500.00"), ("5110", "10000.00", "0.00"),
        ("1150", "0.00", "10000.00")}
    assert hr.bal("4120", entity="zbm") == Decimal("11500.00") and hr.bal("5110", entity="zbm") == Decimal("10000.00")
    assert hr.bal("2120", entity="zbm") == 0 and hr.bal("1150", entity="zbm") == 0
    assert hr.bal("1010", entity="zbm") == Decimal("1500.00")                           # the fee is what ZBM keeps
    hr.assert_books_balance()


def test_blended_display_shows_one_line_but_books_keep_cost_and_fee(hr):
    r = create(hr, display="blended", markup_pct="25.00", media_cost="8000.00")
    inv = r["invoice"]
    assert inv["client_lines"] == [{"description": "Digital billboard, I-405 northbound, 4 weeks", "amount": "10000.00"}]
    assert [(l["line_code"], l["amount"]) for l in inv["lines"]] == [("media_spend", "8000.00"),
                                                                      ("media_fee", "2000.00")]
    assert r["media_buy"]["fee"] == "2000.00" and r["media_buy"]["markup_pct"] == "25.00"


def test_markup_rounds_half_up_once(hr):
    b = create(hr, media_cost="333.33", markup_pct="15.00")["media_buy"]
    assert (b["fee"], b["total"]) == ("50.00", "383.33")                                 # 49.9995 -> 50.00


@pytest.mark.parametrize("pct", ["100.01", "500.00"])
def test_markup_above_the_cap_is_refused(hr, pct):
    r = hr.post("/fin/v1/media-buys", buy_body(markup_pct=pct), andre=ANDRE_TOKEN)
    assert r.status_code == 422 and "AMOUNT_OUT_OF_RANGE" in r.text
    assert hr.svc.db["media_buys"] == {}


def test_markup_cap_and_default_are_settings():
    x = Harness(env={"FIN_MEDIA_MAX_MARKUP_PCT": "300.00", "FIN_MEDIA_DEFAULT_MARKUP_PCT": "20.00"}).ready()
    b = create(x, markup_pct="250.00")["media_buy"]
    assert b["fee"] == "25000.00"
    assert create(x)["media_buy"]["markup_pct"] == "20.00"
    for env in ({"FIN_MEDIA_MAX_MARKUP_PCT": "500.01"},
                {"FIN_MEDIA_DEFAULT_MARKUP_PCT": "120.00"},
                {"FIN_MEDIA_RELEASE_HOLD_BD": "1"}):
        with pytest.raises(RuntimeError):
            Harness(env=env)


@pytest.mark.parametrize("over", [{"flight_end": "2026-10-01"}, {"description": "Billboard plus a surcharge"},
                                  {"vendor_ref": "acct-0123456789012"}, {"media_cost": "0.00"},
                                  {"markup_pct": "15.5"}, {"media_type": "carrier_pigeon"}])
def test_bad_buys_are_refused_whole(hr, over):
    r = hr.post("/fin/v1/media-buys", buy_body(**over), andre=ANDRE_TOKEN)
    assert r.status_code == 422, (over, r.text)
    assert hr.svc.db["media_buys"] == {} and hr.svc.db["invoices"] == {}


def test_no_invoice_issues_until_the_tax_row_is_verified():
    x = Harness().ready(counsel=("FIN-CQ-01",))                       # FIN-CQ-11 NOT verified (M8)
    inv = create(x)["invoice"]
    assert inv["tax_treatment"]["status"] == "unverified"
    r = x.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision",
               {"request_id": rid(), "content_sha256": inv["content_sha256"], "decision": "approve"}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "TAX_TREATMENT_UNVERIFIED" in r.text
    assert memos(x) == []


def test_media_buy_needs_the_rules_in_force(h):
    r = h.post("/fin/v1/media-buys", buy_body(), andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "RULES_NOT_IN_FORCE" in r.text


# ------------------------------------------------------------------------------------------- collect before pay

def test_vendor_payment_refused_on_an_unpaid_buy(hr):
    r = create(hr)
    issue(hr, r["invoice"])
    to_clear_day(hr)
    p = pay_vendor(hr, r["media_buy"]["buy_id"], "100.00")
    assert p.status_code == 409 and "COLLECT_BEFORE_PAY" in p.text
    assert hr.bal("1150", entity="zbm") == 0


def test_vendor_paid_before_the_money_cleared_is_refused_even_when_recorded_later(hr):
    bid, _ = prepaid(hr)
    to_clear_day(hr)
    r = pay_vendor(hr, bid, "1000.00", paid_on="2026-10-06")
    assert r.status_code == 409 and "before the client's money cleared" in r.text


def test_vendor_payment_never_above_the_vendor_cost_and_never_twice(hr):
    bid, _ = prepaid(hr)
    to_clear_day(hr)
    assert pay_vendor(hr, bid, "10000.01").status_code == 409
    body = {"request_id": rid(), "amount": "4000.00", "paid_on": "2026-10-07", "method": "check",
            "payment_ref_sha256": ref("a")}
    hr.ok(hr.post(f"/fin/v1/media-buys/{bid}/vendor-payments", body, andre=ANDRE_TOKEN))
    assert buy(hr, bid)["status"] == "vendor_partially_paid"
    dup = hr.post(f"/fin/v1/media-buys/{bid}/vendor-payments", {**body, "request_id": rid()}, andre=ANDRE_TOKEN)
    assert dup.status_code == 409
    assert pay_vendor(hr, bid, "6000.01").status_code == 409
    hr.clock.advance(days=27)                                        # the flight has ended (Nov 3)
    r = deliver(hr, bid)
    assert r.status_code == 409 and "vendor is paid in full" in r.text   # not delivered until paid in full
    assert hr.bal("4120", entity="zbm") == 0
    hr.ok(pay_vendor(hr, bid, "6000.00", paid_on="2026-11-03", tag="b"))
    assert buy(hr, bid)["status"] == "vendor_paid"
    assert hr.bal("1150", f"buy:{bid}", "zbm") == Decimal("10000.00")


def test_future_paid_on_is_refused(hr):
    bid, _ = prepaid(hr)
    to_clear_day(hr)
    r = pay_vendor(hr, bid, "1.00", paid_on="2026-10-08")
    assert r.status_code == 409 and "future" in r.text


def test_hold_is_a_setting():
    x = Harness(env={"FIN_MEDIA_RELEASE_HOLD_BD": "5"}).ready()
    bid, _ = prepaid(x)
    to_clear_day(x)                                                   # 3 business days: not enough at 5
    r = pay_vendor(x, bid, "1.00")
    assert r.status_code == 409 and "2026-10-09" in r.text


# ------------------------------------------------------------------------------------------- returns, cancel, reject

def test_ach_return_before_the_vendor_is_paid_puts_the_buy_back_and_it_can_be_paid_again(hr):
    bid, inv = prepaid(hr)
    rct = [r for r in hr.svc.db["receipts"].values() if r["invoice_id"] == inv["invoice_id"]][0]["receipt_id"]
    out = hr.ok(hr.post(f"/fin/v1/receipts/{rct}/return",
                        {"request_id": rid(), "return_ref_sha256": "d" * 64, "return_code": "R01",
                         "value_date": "2026-10-05"}, caller="bank_feed"))
    assert out["vendor_exposure"] == "0.00"
    b = buy(hr, bid)
    assert b["status"] == "awaiting_payment" and b["prepayment"] is None
    assert hr.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    assert memos(hr) == ["F12", "F11a", "F12x"]
    assert hr.bal("1100", "client:zbm-client-1", "zbm") == Decimal("11500.00")
    to_clear_day(hr)
    assert pay_vendor(hr, bid, "1.00").status_code == 409             # nothing collected any more
    hr.receive("zbm", "1010", inv["total"], inv["invoice_id"])
    assert buy(hr, bid)["status"] == "prepaid"


def test_ach_return_after_the_vendor_was_paid_flags_exposure_and_stops_further_vendor_payments(hr):
    bid, inv = prepaid(hr)
    to_clear_day(hr)
    hr.ok(pay_vendor(hr, bid, "2500.00"))
    rct = [r for r in hr.svc.db["receipts"].values() if r["invoice_id"] == inv["invoice_id"]][0]["receipt_id"]
    out = hr.ok(hr.post(f"/fin/v1/receipts/{rct}/return",
                        {"request_id": rid(), "return_ref_sha256": "e" * 64, "return_code": "R10",
                         "value_date": "2026-10-07"}, andre=ANDRE_TOKEN))
    assert out["vendor_exposure"] == "2500.00"
    assert buy(hr, bid)["status"] == "payment_returned"
    brk = [b for b in hr.svc.db["breaks"].values() if b["leg"] == "media_exposure"]
    assert len(brk) == 1 and brk[0]["difference"] == "2500.00" and brk[0]["status"] == "open"
    assert pay_vendor(hr, bid, "1.00", tag="y").status_code == 409
    assert deliver(hr, bid).status_code == 409


def test_cancel_an_issued_unpaid_buy_reverses_it_and_a_paid_one_cannot_be_cancelled(hr):
    r = create(hr)
    inv = issue(hr, r["invoice"])
    bid = r["media_buy"]["buy_id"]
    out = hr.ok(hr.post(f"/fin/v1/media-buys/{bid}/cancel", {"request_id": rid()}, andre=ANDRE_TOKEN))
    assert out["media_buy"]["status"] == "cancelled" and out["entry_id"]
    assert memos(hr) == ["F12", "F12c"]
    assert hr.bal("1100", entity="zbm") == 0 and hr.bal("2120", entity="zbm") == 0
    assert hr.svc.db["invoices"][inv["invoice_id"]]["status"] == "void"
    bid2, _ = prepaid(hr)
    r = hr.post(f"/fin/v1/media-buys/{bid2}/cancel", {"request_id": rid()}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "refunding a media prepayment is not built" in r.text


def test_cancel_a_draft_posts_nothing_and_rejecting_the_invoice_cancels_the_buy(hr):
    r = create(hr)
    out = hr.ok(hr.post(f"/fin/v1/media-buys/{r['media_buy']['buy_id']}/cancel", {"request_id": rid()},
                        andre=ANDRE_TOKEN))
    assert out["entry_id"] is None and memos(hr) == []
    r2 = create(hr)
    inv = r2["invoice"]
    hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision",
                  {"request_id": rid(), "content_sha256": inv["content_sha256"], "decision": "reject"},
                  andre=ANDRE_TOKEN))
    assert buy(hr, r2["media_buy"]["buy_id"])["status"] == "cancelled"


# ------------------------------------------------------------------------------------------- card policy (FIN-31)

def _zbm_invoice(hr, codes, methods):
    body = {"request_id": rid(), "entity": "zbm", "client_id": "zbm-client-1", "kind": "service",
            "lines": [{"line_code": c, "quantity": 1, "unit_price": "500.00"} for c in codes],
            "payment_methods": methods, "legal_ref": LEGAL}
    return hr.post("/fin/v1/invoices", body, caller="onboarding")


@pytest.mark.parametrize("codes", [["creative_services"], ["production_services"],
                                   ["revenue_recovery_services", "creative_services"]])
def test_card_refused_permanently_on_anything_but_revenue_recovery(hr, codes):
    r = _zbm_invoice(hr, codes, ["ach", "card"])
    assert r.status_code == 422 and "CARD_NOT_ALLOWED" in r.text and "fin/FIN-31/" in r.text


def test_card_on_revenue_recovery_only_is_still_off_until_counsel_and_an_adapter(hr):
    r = _zbm_invoice(hr, ["revenue_recovery_services"], ["card"])
    assert r.status_code == 422 and "CARD_DISABLED" in r.text and "CARD_NOT_ALLOWED" not in r.text
    ok = hr.ok(_zbm_invoice(hr, ["revenue_recovery_services"], ["ach"]), 201)["invoice"]
    assert ok["lines"][0]["line_code"] == "revenue_recovery_services"


@pytest.mark.parametrize("body_over", [{"kind": "media_prepayment"},
                                       {"lines": [{"line_code": "media_spend", "quantity": 1, "unit_price": "9.00"}]},
                                       {"lines": [{"line_code": "media_fee", "quantity": 1, "unit_price": "9.00"}]}])
def test_callers_can_never_draft_a_media_invoice(hr, body_over):
    body = {"request_id": rid(), "entity": "zbm", "client_id": "c", "kind": "service",
            "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": "9.00"}],
            "payment_methods": ["ach"], "legal_ref": LEGAL, **body_over}
    assert hr.post("/fin/v1/invoices", body, caller="onboarding").status_code == 422


# ------------------------------------------------------------------------------------------- journal guards

def _entry(entity, memo, lines):
    return J.build("je-x", entity, "2026-10-02", "2026-10-02T17:00:00Z", lines, memo, {"kind": "t", "id": "x"}, "k",
                   None, "ev", None, None)


def test_media_accounts_post_only_from_media_flows_and_only_for_zbm():
    amt = Decimal("10.00")
    bad = _entry("zbm", "F11", [J.dr("1100", amt, "client:c"), J.cr("4120", amt)])
    assert any("media flow" in x["message"] for x in J.validate(bad, set(), {}))
    corr = _entry("zbm", "correction", [J.dr("5110", amt), J.cr("1010", amt)])
    assert J.validate(corr, set(), {})
    zbc = _entry("zbc", "F12", [J.dr("1100", amt, "client:c"), J.cr("1010", amt)])
    assert any(x["code"] == "ENTITY_MIX" for x in J.validate(zbc, set(), {}))
    no_sub = _entry("zbm", "F12", [J.dr("1100", amt, "client:c"), J.cr("2120", amt)])
    assert any("buy:" in x["message"] for x in J.validate(no_sub, set(), {}))
    good = _entry("zbm", "F12", [J.dr("1100", amt, "client:c"), J.cr("2120", amt, "buy:b-1")])
    assert J.validate(good, set(), {}) == []


def test_andre_manual_correction_cannot_touch_media_accounts(hr):
    r = hr.post("/fin/v1/journal/zbm/corrections", {"request_id": rid(), "lines": [
        {"account": "4120", "subledger": None, "debit": "5.00", "credit": "0.00"},
        {"account": "1010", "subledger": None, "debit": "0.00", "credit": "5.00"}]}, andre=ANDRE_TOKEN)
    assert r.status_code in (409, 422) and memos(hr) == []


# ------------------------------------------------------------------------------------------- reconciliation L4

def test_reconciliation_l4_matches_media_records_per_buy(hr):
    bid, _ = prepaid(hr)
    to_clear_day(hr)
    hr.ok(pay_vendor(hr, bid, "4000.00"))
    run = hr.recon()
    legs = {l["subject"]: l for l in run["recon"]["legs"] if l["leg"] == "L4" and l["subject"].startswith("zbm:")}
    assert legs["zbm:2120"]["status"] == "matched" and legs["zbm:1150"]["status"] == "matched"
    assert legs[f"zbm:2120:buy:{bid}"]["expected"] == "11500.00"
    assert legs[f"zbm:1150:buy:{bid}"]["expected"] == "4000.00"
    assert all(l["status"] == "matched" for l in legs.values())
    # a media record that disagrees with the journal is a break, per buy (never totals only)
    b = hr.svc.db["media_buys"][bid]
    hr.svc.db["media_buys"][bid] = {**b, "vendor_paid": "3999.99"}
    run = hr.recon()
    bad = [l for l in run["recon"]["legs"] if l["subject"] == f"zbm:1150:buy:{bid}"][0]
    assert bad["status"] == "break" and bad["difference"] == "-0.01"
    hr.svc.db["media_buys"][bid] = b


# ------------------------------------------------------------------------------------------- client receipts

def _receipt_for(hr, invoice_id):
    return [r for r in hr.svc.db["client_receipts"].values() if r["invoice_id"] == invoice_id][0]


def test_media_payment_makes_a_receipt_that_says_what_was_bought_and_the_scheduler_sends_it(hr):
    bid, inv = prepaid(hr, display="blended")
    rec = _receipt_for(hr, inv["invoice_id"])
    assert rec["status"] == "pending_send" and rec["amount_paid"] == "11500.00" and rec["entity"] == "zbm"
    assert rec["what_you_bought"] == "Out-of-home: Digital billboard, I-405 northbound, 4 weeks"
    assert rec["items"] == [{"description": "Digital billboard, I-405 northbound, 4 weeks", "amount": "11500.00"}]
    assert rec["period"] == {"flight_start": "2026-10-05", "flight_end": "2026-11-01"}
    crid = rec["client_receipt_id"]
    out = hr.ok(hr.post(f"/fin/v1/client-receipts/{crid}/send", {"request_id": rid()}, caller="scheduler"))
    assert out["sent"] is True and out["client_receipt"]["status"] == "sent"
    (client, view), = hr.f["client_mail"].sent
    assert client == "zbm-client-1" and view["amount_paid"] == "11500.00" and "client_id" not in view
    again = hr.ok(hr.post(f"/fin/v1/client-receipts/{crid}/send", {"request_id": rid()}, caller="scheduler"))
    assert again["sent"] is False and len(hr.f["client_mail"].sent) == 1          # never sent twice
    assert hr.ok(hr.get(f"/fin/v1/client-receipts/{crid}"))["status"] == "sent"


def test_a_refused_or_missing_mail_adapter_leaves_the_receipt_pending(hr):
    _, inv = prepaid(hr)
    crid = _receipt_for(hr, inv["invoice_id"])["client_receipt_id"]
    hr.f["client_mail"].accept = False
    out = hr.ok(hr.post(f"/fin/v1/client-receipts/{crid}/send", {"request_id": rid()}, caller="scheduler"))
    assert out["sent"] is False and out["client_receipt"]["status"] == "pending_send"
    assert out["client_receipt"]["attempts"] == 1
    assert any(e["event_type"] == "client_receipt_send_failed" for e in hr.ledger.events)


def test_the_production_stand_in_sends_nothing():
    from ports import NotBuiltClientMail, Ports, STAND_INS
    assert isinstance(Ports().client_mail, NotBuiltClientMail) and NotBuiltClientMail in STAND_INS
    assert NotBuiltClientMail().send_receipt("c", {}) is False


def test_service_and_zbc_deposit_payments_get_receipts_too(hr):
    zbc_inv = hr.fund_campaign()
    rec = _receipt_for(hr, zbc_inv["invoice_id"])
    assert rec["entity"] == "zbc" and rec["items"] == [{"description": "Campaign deposit", "amount": "1000.00"}]
    inv = hr.ok(_zbm_invoice(hr, ["revenue_recovery_services"], ["ach"]), 201)["invoice"]
    inv = issue(hr, inv)
    hr.receive("zbm", "1010", "500.00", inv["invoice_id"])
    assert _receipt_for(hr, inv["invoice_id"])["what_you_bought"] == "Revenue Recovery services"


# ------------------------------------------------------------------------------------------- auth, idempotency, restart

def test_media_routes_are_andre_only_and_receipt_send_is_scheduler_only(hr):
    bid, _ = prepaid(hr)
    for path in ("/fin/v1/media-buys", f"/fin/v1/media-buys/{bid}/vendor-payments", f"/fin/v1/media-buys/{bid}/delivery",
                 f"/fin/v1/media-buys/{bid}/cancel"):
        for caller in ("scheduler", "onboarding", "bank_feed"):
            assert hr.post(path, {"request_id": rid()}, caller=caller).status_code == 403, (path, caller)
    crid = next(iter(hr.svc.db["client_receipts"]))
    assert hr.post(f"/fin/v1/client-receipts/{crid}/send", {"request_id": rid()}, andre=ANDRE_TOKEN).status_code == 403
    assert hr.post(f"/fin/v1/client-receipts/{crid}/send", {"request_id": rid()}, caller="onboarding").status_code == 403


def test_media_buy_is_idempotent_and_a_changed_body_conflicts(hr):
    body = buy_body()
    a = hr.ok(hr.post("/fin/v1/media-buys", body, andre=ANDRE_TOKEN), 201)
    b = hr.ok(hr.post("/fin/v1/media-buys", body, andre=ANDRE_TOKEN), 201)
    assert a["media_buy"]["buy_id"] == b["media_buy"]["buy_id"] and len(hr.svc.db["media_buys"]) == 1
    assert hr.post("/fin/v1/media-buys", {**body, "media_cost": "9.00"}, andre=ANDRE_TOKEN).status_code == 409


def test_media_state_survives_a_restart(tmp_path):
    x = Harness(data_dir=str(tmp_path / "fin")).ready()
    bid, inv = prepaid(x)
    to_clear_day(x)
    x.ok(pay_vendor(x, bid, "3000.00"))
    y = Harness(data_dir=str(tmp_path / "fin"), ledger=x.ledger, clock=x.clock, fakes=x.f)
    assert buy(y, bid)["status"] == "vendor_partially_paid" and buy(y, bid)["vendor_paid"] == "3000.00"
    assert y.bal("1150", f"buy:{bid}", "zbm") == Decimal("3000.00")
    assert _receipt_for(y, inv["invoice_id"])["status"] == "pending_send"
    y.ok(pay_vendor(y, bid, "7000.00", tag="z"))
    assert buy(y, bid)["status"] == "vendor_paid"
