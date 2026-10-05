"""Stripe incoming: ZBM client payments through ZBM's Stripe account (ADR 0009 amendment, Oct 5 2026; founder
M6/M9/M10/M11; rule FIN-31).

Driven end to end through the REAL adapter (``stripe_incoming.StripeIncoming``) talking to a simulated Stripe
(``stripe_sim.SimStripe``) over an httpx transport, and through signed webhook events, because api.stripe.com is not
reachable from the build sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal

import pytest

import config as config_mod
from clock import FixedClock
from helpers import ANDRE_TOKEN, NOW, Harness, rid
from intelligences import i01_journal as J
from stripe_incoming import API_VERSION, StripeIncoming
from stripe_sim import TEST_KEY, TEST_WHSEC, SimStripe
from test_media_billing import LEGAL, buy, create, issue, pay_vendor

SUCCESS, CANCEL = "https://zbestmedia.com/pay/thanks", "https://zbestmedia.com/pay/cancelled"


def _secret(tmp_path, name, value, mode=0o600):
    p = tmp_path / name
    p.write_text(value + "\n")
    os.chmod(p, mode)
    return str(p)


@pytest.fixture
def keys(tmp_path):
    return {"FIN_STRIPE_INCOMING": "1", "FIN_STRIPE_SECRET_KEY_FILE": _secret(tmp_path, "sk", TEST_KEY),
            "FIN_STRIPE_WEBHOOK_SECRET_FILE": _secret(tmp_path, "wh", TEST_WHSEC),
            "FIN_STRIPE_SUCCESS_URL": SUCCESS, "FIN_STRIPE_CANCEL_URL": CANCEL}


def make(keys, **env):
    clock = FixedClock(NOW)
    sim = SimStripe(clock)
    port = StripeIncoming(TEST_KEY, TEST_WHSEC, False, SUCCESS, CANCEL, transport=sim.transport(), clock=clock)
    hr = Harness(env={**keys, **env}, clock=clock, stripe_in=port).ready()
    hr.sim = sim
    return hr


@pytest.fixture
def st(keys):
    return make(keys, FIN_CARD_PREPAYMENTS="1")


def rr_invoice(hr, price="1200.00", methods=("ach", "card"), codes=("revenue_recovery_services",)):
    body = {"request_id": rid(), "entity": "zbm", "client_id": "zbm-client-7", "kind": "service",
            "lines": [{"line_code": c, "quantity": 1, "unit_price": price} for c in codes],
            "payment_methods": list(methods), "legal_ref": LEGAL}
    inv = hr.ok(hr.post("/fin/v1/invoices", body, caller="onboarding"), 201)["invoice"]
    return issue(hr, inv)


def checkout(hr, inv_id, caller="onboarding", andre=None):
    return hr.post(f"/fin/v1/invoices/{inv_id}/stripe-checkout", {"request_id": rid()},
                   caller=None if andre else caller, andre=andre)


def send(hr, typ, obj, **kw):
    payload, sig = hr.sim.event(typ, obj, **kw)
    return hr.post("/fin/v1/stripe/events", {"request_id": rid(), "payload": payload, "signature": sig},
                   caller="rail_gateway")


def sent(hr, typ, obj, **kw):
    return hr.ok(send(hr, typ, obj, **kw))


def bal(hr, acct, sub=None):
    return hr.bal(acct, sub, "zbm")


def memos(hr):
    return [e["memo_code"] for e in hr.svc.entries if e["entity"] == "zbm"]


def paid_rr(hr, price="1200.00", method="us_bank_account"):
    inv = rr_invoice(hr, price)
    cs = hr.ok(checkout(hr, inv["invoice_id"]))["checkout"]
    pi = hr.sim.pay(cs["session_id"], method=method)
    r = sent(hr, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    return inv, cs, pi, r


# --------------------------------------------------------------------------------------------- checkout

def test_checkout_offers_ach_and_card_for_revenue_recovery_under_the_cap(st):
    inv = rr_invoice(st, "1200.00")
    out = st.ok(checkout(st, inv["invoice_id"]))
    cs = out["checkout"]
    assert out["reused"] is False and cs["methods"] == ["us_bank_account", "card"] and cs["status"] == "open"
    assert cs["url"].startswith("https://checkout.stripe.com/c/pay/cs_test_")
    req = [r for r in st.sim.requests if r["method"] == "POST"][0]
    f = req["form"]
    assert req["headers"]["stripe-version"] == API_VERSION and req["headers"]["idempotency-key"].startswith("fin-chk-")
    assert req["headers"]["authorization"] == f"Bearer {TEST_KEY}"
    assert f["mode"] == "payment" and f["line_items[0][price_data][unit_amount]"] == "120000"
    assert f["line_items[0][price_data][currency]"] == "usd" and f["client_reference_id"] == inv["invoice_id"]
    assert f["allowed_payment_method_types[0]"] == "us_bank_account" and f["allowed_payment_method_types[1]"] == "card"
    assert f["metadata[invoice_id]"] == inv["invoice_id"] == f["payment_intent_data[metadata][invoice_id]"]
    assert f["success_url"] == SUCCESS and f["cancel_url"] == CANCEL
    assert 23 * 3600 - 5 <= int(f["expires_at"]) - int(NOW.timestamp()) <= 24 * 3600
    assert not any("surcharge" in k or "fee" in k for k in f)                              # FIN-21: no surcharge
    view = st.ok(st.get(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", caller="onboarding"))
    assert [s["session_id"] for s in view["sessions"]] == [cs["session_id"]]


def test_checkout_hands_back_the_open_page_instead_of_a_second_one(st):
    inv = rr_invoice(st)
    a = st.ok(checkout(st, inv["invoice_id"]))
    b = st.ok(checkout(st, inv["invoice_id"], andre=ANDRE_TOKEN))
    assert b["reused"] is True and b["checkout"]["session_id"] == a["checkout"]["session_id"]
    assert sum(r["method"] == "POST" for r in st.sim.requests) == 1


def test_card_is_never_offered_when_the_card_policy_fails(keys):
    off = make(keys, FIN_CARD_PREPAYMENTS="1")
    inv = rr_invoice(off)                                                # drafted while card was on ...
    off.svc.cfg.card_prepayments = False                                 # ... and Andre turned it off since
    assert off.ok(checkout(off, inv["invoice_id"]))["checkout"]["methods"] == ["us_bank_account"]
    hr = make(keys, FIN_CARD_PREPAYMENTS="1")
    big = rr_invoice(hr, "5000.01", methods=("ach",))                    # above the cap: ACH only (M10)
    assert hr.ok(checkout(hr, big["invoice_id"]))["checkout"]["methods"] == ["us_bank_account"]
    m = create(hr)                                                       # media: ACH or wire, never card (M9)
    mi = issue(hr, m["invoice"])
    assert hr.ok(checkout(hr, mi["invoice_id"]))["checkout"]["methods"] == ["us_bank_account"]
    posted = [r["form"] for r in hr.sim.requests if r["method"] == "POST"]
    assert all("allowed_payment_method_types[1]" not in f for f in posted)


def test_checkout_refusals(st, keys):
    wire = rr_invoice(st, methods=("wire",))
    r = checkout(st, wire["invoice_id"])
    assert r.status_code == 409 and "STRIPE_NOT_ALLOWED" in r.text
    draft = st.ok(st.post("/fin/v1/invoices", {
        "request_id": rid(), "entity": "zbm", "client_id": "c", "kind": "service", "payment_methods": ["ach"],
        "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": "10.00"}], "legal_ref": LEGAL},
        caller="onboarding"), 201)["invoice"]
    r = checkout(st, draft["invoice_id"])
    assert r.status_code == 409 and "issued" in r.text
    assert checkout(st, "no-such-invoice").status_code == 404
    assert not st.sim.requests


def test_zbc_deposit_invoice_never_goes_through_stripe(st):
    doc = st.rate_card("camp-9")
    st.profile("camp-9", "client-9", "500.00", "4.00", doc)
    inv = st.deposit_invoice("camp-9", "client-9", "500.00")
    assert inv["status"] == "issued" and inv["entity"] == "zbc"
    r = checkout(st, inv["invoice_id"])
    assert r.status_code == 409 and "ENTITY_MIX" in r.text and "FIN-CQ-02" in r.text
    assert not st.sim.requests


def test_checkout_when_stripe_is_not_wired_refuses(keys):
    hr = Harness().ready()
    inv = rr_invoice(hr, methods=("ach",))
    r = checkout(hr, inv["invoice_id"])
    assert r.status_code == 409 and "DEPENDENCY_UNAVAILABLE:stripe" in r.text
    r = hr.post("/fin/v1/stripe/events", {"request_id": rid(), "payload": "{}", "signature": "t=1,v1=00"},
                caller="rail_gateway")
    assert r.status_code == 409 and "DEPENDENCY_UNAVAILABLE:stripe" in r.text
    assert hr.ok(hr.get("/health", caller=None))["stripe_incoming"] == {"wired": False, "livemode": False}


def test_checkout_survives_stripe_being_down_and_retries_with_the_same_key(st):
    inv = rr_invoice(st)
    st.sim.fail_next = [500]
    r = checkout(st, inv["invoice_id"])
    assert r.status_code == 409 and "transport_error" in r.text
    assert not st.svc.db["stripe_sessions"]
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    keys_used = [r["headers"]["idempotency-key"] for r in st.sim.requests if r["method"] == "POST"]
    assert len(keys_used) == 2 and keys_used[0] == keys_used[1]                    # no second page at Stripe
    assert len(st.sim.sessions) == 1 and cs["session_id"] in st.sim.sessions


def test_checkout_refused_when_stripes_answer_does_not_match(st):
    inv = rr_invoice(st)
    orig = st.sim._create_session

    def wrong_amount(request, form):
        return orig(request, {**form, "line_items[0][price_data][unit_amount]": "1"})
    st.sim._create_session = wrong_amount
    r = checkout(st, inv["invoice_id"])
    assert r.status_code == 409 and "rejected" in r.text and not st.svc.db["stripe_sessions"]


# --------------------------------------------------------------------------------------------- payments

def test_ach_payment_books_only_once_stripe_says_it_succeeded(st):
    inv = rr_invoice(st, "1200.00")
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    pi = st.sim.pay(cs["session_id"], settle=False)                      # ACH: processing for up to 4 business days
    r = sent(st, "checkout.session.completed", {"id": cs["session_id"], "object": "checkout.session"})
    assert r["status"] == "payment_processing" and "F13" not in memos(st)
    st.sim.succeed(pi)                                                    # 0.8%, capped at 5.00
    r = sent(st, "checkout.session.async_payment_succeeded", {"id": cs["session_id"], "object": "checkout.session"})
    assert r["status"] == "matched"
    assert bal(st, "1060") == Decimal("1195.00") and bal(st, "5020") == Decimal("5.00")
    assert bal(st, "1100", "client:zbm-client-7") == 0 and bal(st, "4110") == Decimal("1200.00")
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "paid"
    rc = [x for x in st.svc.db["receipts"].values() if x.get("into_account") == "1060"][0]
    assert rc["method"] == "stripe_ach" and rc["stripe"]["fee"] == "5.00" and rc["status"] == "matched"
    crc = [c for c in st.svc.db["client_receipts"].values() if c["receipt_id"] == rc["receipt_id"]][0]
    assert crc["method"] == "ACH bank debit (Stripe)" and crc["amount_paid"] == "1200.00"
    # the same payment announced again, by another event type: nothing posts twice
    n = len(st.svc.entries)
    assert sent(st, "payment_intent.succeeded", {"id": pi, "object": "payment_intent"})["status"] == \
        "payment_already_booked"
    assert len(st.svc.entries) == n
    assert st.svc.db["stripe_sessions"][cs["session_id"]]["status"] in ("complete", "invoice_paid")


def test_the_same_event_delivered_twice_is_a_duplicate(st):
    inv = rr_invoice(st)
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    st.sim.pay(cs["session_id"])
    payload, sig = st.sim.event("checkout.session.completed", {"id": cs["session_id"]})
    body = {"request_id": rid(), "payload": payload, "signature": sig}
    assert st.ok(st.post("/fin/v1/stripe/events", body, caller="rail_gateway"))["status"] == "matched"
    n = len(st.svc.entries)
    again = st.ok(st.post("/fin/v1/stripe/events", {**body, "request_id": rid()}, caller="rail_gateway"))
    assert again["status"] == "duplicate" and len(st.svc.entries) == n


def test_card_payment_on_revenue_recovery_books_stripes_card_fee(st):
    inv, cs, pi, r = paid_rr(st, "1000.00", method="card")
    assert r["status"] == "matched"
    assert bal(st, "5020") == Decimal("29.30") and bal(st, "1060") == Decimal("970.70")       # 2.9% + 30c
    rc = st.svc.db["receipts"][[k for k, v in st.svc.db["receipts"].items() if v.get("into_account") == "1060"][0]]
    assert rc["method"] == "stripe_card"


def test_a_payment_finance_cannot_match_is_unapplied_and_opens_a_break(st):
    inv = rr_invoice(st, "1200.00")
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    st.sim.pay(cs["session_id"], amount=100000)                          # 1000.00 against a 1200.00 invoice
    r = sent(st, "checkout.session.completed", {"id": cs["session_id"]})
    assert r["status"] == "unapplied"
    assert bal(st, "2070") == Decimal("1000.00") and bal(st, "1100", "client:zbm-client-7") == Decimal("1200.00")
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    brk = [b for b in st.svc.db["breaks"].values() if b["subject"] == "zbm:1060"]
    assert brk and brk[0]["status"] == "open" and brk[0]["difference"] == "1000.00"
    assert not [c for c in st.svc.db["client_receipts"].values() if c["invoice_id"] == inv["invoice_id"]]


def test_a_second_payment_of_a_paid_invoice_is_unapplied(st):
    inv, cs, pi, _ = paid_rr(st)
    pi2 = st.sim.pay(cs["session_id"], force=True)                      # e.g. a second payment made elsewhere
    assert sent(st, "payment_intent.succeeded", {"id": pi2})["status"] == "unapplied"
    assert bal(st, "2070") == Decimal("1200.00")


def test_a_payment_with_no_finance_checkout_is_never_applied(st):
    inv = rr_invoice(st, "1200.00")
    other = rr_invoice(st, "1200.00")
    cs = st.ok(checkout(st, other["invoice_id"]))["checkout"]
    # someone made a payment in the Stripe Dashboard whose metadata names an invoice Finance never sent to Stripe
    pi = st.sim.pay(cs["session_id"], metadata={"invoice_id": inv["invoice_id"]})
    assert sent(st, "payment_intent.succeeded", {"id": pi})["status"] == "unapplied"
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"


def test_stripe_unreadable_answers_503_and_the_retry_books_it(st):
    inv = rr_invoice(st)
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    st.sim.pay(cs["session_id"])
    payload, sig = st.sim.event("checkout.session.completed", {"id": cs["session_id"]})
    st.sim.fail_next = [503]
    r = st.post("/fin/v1/stripe/events", {"request_id": rid(), "payload": payload, "signature": sig},
                caller="rail_gateway")
    assert r.status_code == 503 and r.json()["took_effect"] is False
    assert not st.svc.db["stripe_events"] and "F13" not in memos(st)
    st.sim.fail_next = [200]                                             # a 200 with an error body: still not trusted
    r = st.post("/fin/v1/stripe/events", {"request_id": rid(), "payload": payload, "signature": sig},
                caller="rail_gateway")
    assert r.status_code == 503
    r = st.ok(st.post("/fin/v1/stripe/events", {"request_id": rid(), "payload": payload, "signature": sig},
                      caller="rail_gateway"))
    assert r["status"] == "matched"


def test_succeeded_without_a_readable_balance_transaction_is_retried_not_guessed(st):
    inv = rr_invoice(st)
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    pi = st.sim.pay(cs["session_id"])
    ch = st.sim.charges[st.sim.pis[pi]["latest_charge"]]
    txn = ch["balance_transaction"]
    ch["balance_transaction"] = None
    r = send(st, "payment_intent.succeeded", {"id": pi})
    assert r.status_code == 503 and "F13" not in memos(st)
    ch["balance_transaction"] = txn
    assert sent(st, "payment_intent.succeeded", {"id": pi})["status"] == "matched"


def test_a_payment_that_fails_after_it_succeeded_makes_the_client_owe_again(st):
    inv, cs, pi, _ = paid_rr(st, "1200.00")
    st.sim.fail_after_success(pi, fee_back=500)
    r = sent(st, "charge.failed", {"id": st.sim.pis[pi]["latest_charge"], "object": "charge", "payment_intent": pi})
    assert r["status"] == "payment_failed_after_success"
    assert bal(st, "1060") == 0 and bal(st, "5020") == 0
    assert bal(st, "1100", "client:zbm-client-7") == Decimal("1200.00")
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    crc = [c for c in st.svc.db["client_receipts"].values() if c["invoice_id"] == inv["invoice_id"]][0]
    assert crc["status"].startswith("withdrawn")
    assert sent(st, "payment_intent.payment_failed", {"id": pi})["status"] == "payment_failure_already_booked"


# --------------------------------------------------------------------------------------------- signatures and modes

def test_signatures_are_checked_before_anything_is_read(st):
    inv, cs, pi, _ = paid_rr(st)
    n_req = len(st.sim.requests)
    obj = {"id": pi}
    payload, good = st.sim.event("payment_intent.succeeded", obj)
    cases = [
        (payload, st.sim.sign(payload, secret="whsec_SomeoneElsesSecret00000000")),           # wrong secret
        (payload, st.sim.sign(payload, ts=int(NOW.timestamp()) - 301)),                      # stale
        (payload, st.sim.sign(payload, ts=int(NOW.timestamp()) + 301)),                      # from the future
        (payload.replace(pi, "pi_0000forged"), good),                                       # altered body
        (payload, "v1=" + "0" * 64),                                                          # no timestamp
        (payload, "t=123,v0=" + "0" * 64),                                                    # no v1
    ]
    for body, sig in cases:
        r = st.post("/fin/v1/stripe/events", {"request_id": rid(), "payload": body, "signature": sig},
                    caller="rail_gateway")
        assert r.status_code == 409 and "Stripe-Signature did not verify" in r.text, (sig, r.text)
    assert len(st.sim.requests) == n_req                                  # nothing was read back from Stripe
    assert len(st.svc.db["stripe_events"]) == 1


def test_a_rolled_secret_still_verifies_while_both_are_sent(st):
    inv = rr_invoice(st)
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    st.sim.pay(cs["session_id"])
    payload, sig = st.sim.event("checkout.session.completed", {"id": cs["session_id"]})
    old = st.sim.sign(payload, secret="whsec_OldSecretBeingRolled0000000").split(",")[1]
    r = st.ok(st.post("/fin/v1/stripe/events", {"request_id": rid(), "payload": payload,
                                                "signature": f"{sig.split(',')[0]},{old},{sig.split(',')[1]}"},
                      caller="rail_gateway"))
    assert r["status"] == "matched"


def test_events_from_the_other_mode_and_unknown_types_are_recorded_and_ignored(st):
    inv, cs, pi, _ = paid_rr(st)
    n = len(st.svc.entries)
    assert sent(st, "payment_intent.succeeded", {"id": pi}, livemode=True)["status"] == "ignored_other_mode"
    assert sent(st, "customer.created", {"id": "cus_123abc"})["status"] == "ignored_type"
    assert len(st.svc.entries) == n


def test_only_the_gateway_may_send_events_and_bad_bodies_are_refused(st):
    payload, sig = st.sim.event("customer.created", {"id": "cus_1"})
    body = {"request_id": rid(), "payload": payload, "signature": sig}
    assert st.post("/fin/v1/stripe/events", body, caller="onboarding").status_code == 403
    assert st.post("/fin/v1/stripe/events", body, andre=ANDRE_TOKEN).status_code == 403
    p2 = json.dumps({"id": "not-an-event", "type": "x.y", "livemode": False, "data": {"object": {"id": "cus_1"}}})
    r = st.post("/fin/v1/stripe/events", {"request_id": rid(), "payload": p2, "signature": st.sim.sign(p2)},
                caller="rail_gateway")
    assert r.status_code == 422
    assert st.post("/fin/v1/stripe/events", {"request_id": rid(), "payload": payload}, caller="rail_gateway") \
        .status_code == 422


# --------------------------------------------------------------------------------------------- disputes

def test_card_dispute_withdrawn_then_won(st):
    inv, cs, pi, _ = paid_rr(st, "1000.00", method="card")
    du = st.sim.dispute(pi, fee=1500)
    r = sent(st, "charge.dispute.created", {"id": du, "object": "dispute", "payment_intent": pi})
    assert r["status"] == "dispute_needs_response"
    assert bal(st, "1300") == Decimal("1000.00") and bal(st, "5030") == Decimal("15.00")
    assert bal(st, "1060") == Decimal("970.70") - Decimal("1015.00")
    n = len(st.svc.entries)
    assert sent(st, "charge.dispute.updated", {"id": du})["status"] == "dispute_needs_response"
    assert len(st.svc.entries) == n                                      # the same withdrawal never posts twice
    st.sim.close_dispute(du, won=True, fee_back=1500)
    assert sent(st, "charge.dispute.closed", {"id": du})["status"] == "dispute_won"
    assert bal(st, "1300") == 0 and bal(st, "5030") == 0 and bal(st, "1060") == Decimal("970.70")
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "paid"
    assert st.svc.db["stripe_disputes"][du]["finalized"] is True


def test_dispute_lost_makes_the_client_owe_the_invoice_again(st):
    inv, cs, pi, _ = paid_rr(st, "1000.00", method="card")
    du = st.sim.dispute(pi)
    sent(st, "charge.dispute.funds_withdrawn", {"id": du})
    st.sim.close_dispute(du, won=False)
    assert sent(st, "charge.dispute.closed", {"id": du})["status"] == "dispute_lost"
    assert bal(st, "1300") == 0 and bal(st, "1100", "client:zbm-client-7") == Decimal("1000.00")
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    assert "F7l" in memos(st) and memos(st).count("F7l") == 1
    sent(st, "charge.dispute.closed", {"id": du})
    assert memos(st).count("F7l") == 1


# --------------------------------------------------------------------------------------------- media by Stripe ACH

def media_paid_by_stripe(hr):
    m = create(hr)
    inv = issue(hr, m["invoice"])
    cs = hr.ok(checkout(hr, inv["invoice_id"]))["checkout"]
    assert cs["methods"] == ["us_bank_account"]
    pi = hr.sim.pay(cs["session_id"])
    assert sent(hr, "checkout.session.completed", {"id": cs["session_id"]})["status"] == "matched"
    return m["media_buy"]["buy_id"], inv, pi


def test_media_prepaid_by_stripe_ach_keeps_the_two_day_hold(st):
    bid, inv, pi = media_paid_by_stripe(st)
    b = buy(st, bid)
    assert b["status"] == "prepaid" and b["prepayment"]["value_date"] == "2026-10-02"
    assert bal(st, "2120", f"buy:{bid}") == Decimal("11500.00") and bal(st, "1060") == Decimal("11495.00")
    r = pay_vendor(st, bid, "10000.00", paid_on="2026-10-02")
    assert r.status_code == 409 and "2026-10-06" in r.text
    st.clock.advance(days=4)                                             # Tue Oct 6
    st.ok(pay_vendor(st, bid, "10000.00", paid_on="2026-10-06"))


def test_media_dispute_blocks_the_vendor_payment_and_a_loss_reopens_the_invoice(st):
    bid, inv, pi = media_paid_by_stripe(st)
    du = st.sim.dispute(pi)
    sent(st, "charge.dispute.created", {"id": du})
    assert buy(st, bid)["payment_disputed"] is True
    st.clock.advance(days=4)
    r = pay_vendor(st, bid, "10000.00", paid_on="2026-10-06")
    assert r.status_code == 409 and "disputed the prepayment" in r.text
    st.sim.close_dispute(du, won=False)
    sent(st, "charge.dispute.closed", {"id": du})
    b = buy(st, bid)
    assert b["status"] == "awaiting_payment" and b["payment_disputed"] is False
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    assert bal(st, "1100", "client:zbm-client-1") == Decimal("11500.00")


def test_media_dispute_after_the_vendor_was_paid_opens_an_exposure_break(st):
    bid, inv, pi = media_paid_by_stripe(st)
    st.clock.advance(days=4)
    st.ok(pay_vendor(st, bid, "10000.00", paid_on="2026-10-06"))
    du = st.sim.dispute(pi)
    sent(st, "charge.dispute.created", {"id": du})
    brk = [x for x in st.svc.db["breaks"].values() if x["leg"] == "media_exposure"]
    assert brk and brk[0]["difference"] == "10000.00" and brk[0]["status"] == "open"


# --------------------------------------------------------------------------------------------- payouts and L3

def test_payouts_move_the_stripe_balance_to_the_operating_account(st):
    paid_rr(st, "1200.00")                                               # 1060 = 1195.00 (ACH fee capped at 5)
    po = st.sim.payout(119500, "in_transit")
    assert sent(st, "payout.created", {"id": po, "object": "payout"})["status"] == "payout_in_transit"
    assert bal(st, "1060") == Decimal("1195.00") and bal(st, "1010") == 0
    run = st.ok(st.post("/fin/v1/reconciliations/run", {"request_id": rid()}, caller="scheduler"))
    leg = [l for l in run["recon"]["legs"] if l["subject"] == "zbm:1060"][0]
    assert leg["status"] == "matched", leg                                # in transit is counted, not a break
    st.sim.payouts[po]["status"] = "paid"
    sent(st, "payout.paid", {"id": po})
    assert bal(st, "1060") == 0 and bal(st, "1010") == Decimal("1195.00")
    st.sim.payouts[po]["status"] = "failed"
    sent(st, "payout.failed", {"id": po})
    assert bal(st, "1060") == Decimal("1195.00") and bal(st, "1010") == 0
    sent(st, "payout.failed", {"id": po})
    assert memos(st).count("F13q") == 1


def test_l3_breaks_when_stripe_moved_money_finance_never_saw(st):
    paid_rr(st)
    st.sim.extra_balance = -2500                                         # e.g. a Stripe fee outside any payment
    run = st.ok(st.post("/fin/v1/reconciliations/run", {"request_id": rid()}, caller="scheduler"))
    leg = [l for l in run["recon"]["legs"] if l["subject"] == "zbm:1060"][0]
    assert leg["status"] == "break" and leg["difference"] == "-25.00"


def test_l3_is_not_in_use_when_stripe_is_off():
    hr = Harness().ready()
    run = hr.ok(hr.post("/fin/v1/reconciliations/run", {"request_id": rid()}, caller="scheduler"))
    leg = [l for l in run["recon"]["legs"] if l["subject"] == "zbm:1060"][0]
    assert leg["status"] == "not_in_use"


# --------------------------------------------------------------------------------------------- guards

def test_a_stripe_receipt_is_never_returned_by_hand(st):
    inv, cs, pi, _ = paid_rr(st)
    rct = [k for k, v in st.svc.db["receipts"].items() if v.get("into_account") == "1060"][0]
    r = st.post(f"/fin/v1/receipts/{rct}/return", {"request_id": rid(), "return_code": "R01",
                                                    "return_ref_sha256": "a" * 64, "value_date": "2026-10-02"},
                caller="bank_feed")
    assert r.status_code == 409 and "Stripe payment is never returned by hand" in r.text


def test_i01_keeps_the_stripe_balance_to_stripe_flows():
    def entry(memo, lines, entity="zbm"):
        return {"entity": entity, "period": "2026-10", "memo_code": memo, "lines": lines}
    bad = J.validate(entry("F11a", [J.dr("1060", Decimal("5.00")), J.cr("1100", Decimal("5.00"), "client:c")]),
                     set(), {})
    assert any("1060" in x["message"] for x in bad)
    assert not J.validate(entry("F13", [J.dr("1060", Decimal("5.00")), J.cr("1100", Decimal("5.00"), "client:c")]),
                          set(), {})
    zbc = J.validate(entry("F13", [J.dr("1010", Decimal("5.00")), J.cr("2070", Decimal("5.00"))], "zbc"), set(), {})
    assert any(x["code"] == "ENTITY_MIX" for x in zbc)


# --------------------------------------------------------------------------------------------- configuration

def test_config_stripe_settings_fail_closed(tmp_path, keys):
    ok = config_mod.load({**Harness().env, **keys})
    assert ok.stripe_incoming and not ok.stripe_livemode and ok.stripe_success_url == SUCCESS
    assert "sk_test" not in repr(ok) and "whsec" not in repr(ok) and ok.stripe_secret_key.reveal() == TEST_KEY
    base = Harness().env
    bad = [
        {**keys, "FIN_STRIPE_SECRET_KEY_FILE": "__unset__"},
        {**keys, "FIN_STRIPE_SECRET_KEY_FILE": "relative/path"},
        {**keys, "FIN_STRIPE_SECRET_KEY_FILE": _secret(tmp_path, "sk_open", TEST_KEY, 0o644)},     # group/other read
        {**keys, "FIN_STRIPE_SECRET_KEY_FILE": _secret(tmp_path, "sk_live", "sk_live_" + "x" * 30)},  # live, no switch
        {**keys, "FIN_STRIPE_LIVE": "1"},                                                          # switch, test key
        {**keys, "FIN_STRIPE_WEBHOOK_SECRET_FILE": _secret(tmp_path, "wh_bad", TEST_KEY)},          # not a whsec_
        {**keys, "FIN_STRIPE_SUCCESS_URL": "http://zbestmedia.com/pay"},                           # not https
        {**keys, "FIN_STRIPE_CANCEL_URL": "__unset__"},
        {"FIN_STRIPE_SUCCESS_URL": SUCCESS},                                                       # stray, flag off
    ]
    for over in bad:
        env = {**base, **over}
        env = {k: v for k, v in env.items() if v != "__unset__"}
        with pytest.raises(RuntimeError):
            config_mod.load(env)
    link = tmp_path / "sk_link"
    link.symlink_to(keys["FIN_STRIPE_SECRET_KEY_FILE"])
    with pytest.raises(RuntimeError):
        config_mod.load({**base, **keys, "FIN_STRIPE_SECRET_KEY_FILE": str(link)})
    live = config_mod.load({**base, **keys, "FIN_STRIPE_LIVE": "1",
                            "FIN_STRIPE_SECRET_KEY_FILE": _secret(tmp_path, "sk_live2", "rk_live_" + "y" * 30)})
    assert live.stripe_livemode is True


def test_health_reports_stripe_wiring(st):
    assert st.ok(st.get("/health", caller=None))["stripe_incoming"] == {"wired": True, "livemode": False}


# --------------------------------------------------------------------------------------------- AEGIS review of 0265a45

def _events(hr, typ, obj):
    payload, sig = hr.sim.event(typ, obj)
    return payload, sig


def test_aegis_h1_a_stale_read_never_overwrites_a_newer_one(st):
    """Two deliveries race: A reads 'succeeded', then the charge fails and B arrives. B must wait for A, read the
    failure, and reverse it -- never A's stale success applied after B."""
    import threading

    import httpx
    inv = rr_invoice(st)
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    pi = st.sim.pay(cs["session_id"])
    port = st.svc.ports.stripe_in
    orig = st.sim.handle
    entered, release = threading.Event(), threading.Event()

    def slow(req):
        resp = orig(req)
        if req.method == "GET" and "/payment_intents/" in req.url.path and not entered.is_set():
            entered.set()
            release.wait(10)
        return resp
    port._transport = httpx.MockTransport(slow)
    out = {}
    pa, sa = _events(st, "payment_intent.succeeded", {"id": pi})
    ta = threading.Thread(target=lambda: out.setdefault("a", st.svc.stripe_event("rail_gateway", rid(), pa, sa)))
    ta.start()
    assert entered.wait(10)
    st.sim.fail_after_success(pi, fee_back=500)
    pb, sb = _events(st, "charge.failed", {"id": "ch_x", "payment_intent": pi})
    tb = threading.Thread(target=lambda: out.setdefault("b", st.svc.stripe_event("rail_gateway", rid(), pb, sb)))
    tb.start()
    tb.join(0.5)
    assert tb.is_alive()                                                  # B waits for A
    release.set()
    ta.join(10)
    tb.join(10)
    assert out["a"]["status"] == "matched" and out["b"]["status"] == "payment_failed_after_success"
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    assert bal(st, "1060") == 0 and bal(st, "1100", "client:zbm-client-7") == Decimal("1200.00")


def test_aegis_h2_a_dispute_seen_before_its_payment_still_blocks_the_vendor(st):
    m = create(st)
    inv = issue(st, m["invoice"])
    bid = m["media_buy"]["buy_id"]
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    pi = st.sim.pay(cs["session_id"])                                   # the payment event is delayed / lost
    du = st.sim.dispute(pi)
    assert sent(st, "charge.dispute.created", {"id": du})["status"] == "dispute_needs_response"
    assert "F13" in memos(st) and buy(st, bid)["payment_disputed"] is True
    st.clock.advance(days=4)
    r = pay_vendor(st, bid, "10000.00", paid_on="2026-10-06")
    assert r.status_code == 409 and "disputed the prepayment" in r.text


def test_aegis_m1_a_retry_after_a_lost_answer_gets_the_same_page(st):
    import httpx
    inv = rr_invoice(st)
    port = st.svc.ports.stripe_in
    orig = st.sim.handle
    lost = {"n": 0}

    def lose_first_answer(req):
        resp = orig(req)
        if req.method == "POST" and lost["n"] == 0:
            lost["n"] = 1
            raise httpx.ReadTimeout("answer lost after Stripe made the session")
        return resp
    port._transport = httpx.MockTransport(lose_first_answer)
    assert checkout(st, inv["invoice_id"]).status_code == 409
    assert len(st.sim.sessions) == 1                                     # Stripe DID make it
    st.clock.advance(minutes=5)
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    assert cs["session_id"] in st.sim.sessions and len(st.sim.sessions) == 1


def test_aegis_m2_an_outdated_page_is_closed_before_a_new_one_is_made(st):
    inv = rr_invoice(st, "1000.00")
    old = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    assert old["methods"] == ["us_bank_account", "card"]
    st.svc.cfg.card_prepayments = False                                  # Andre turns card off
    new = st.ok(checkout(st, inv["invoice_id"]))
    assert new["closed"] == [old["session_id"]] and new["checkout"]["methods"] == ["us_bank_account"]
    assert st.sim.sessions[old["session_id"]]["status"] == "expired"
    assert st.svc.db["stripe_sessions"][old["session_id"]]["status"] == "expired"
    with pytest.raises(RuntimeError):
        st.sim.pay(old["session_id"], method="card")                     # Stripe refuses an expired page


def test_aegis_m2_a_page_that_cannot_be_closed_blocks_a_new_one(st):
    inv = rr_invoice(st, "1000.00")
    st.ok(checkout(st, inv["invoice_id"]))
    st.svc.cfg.card_prepayments = False
    st.sim.fail_next = [503]
    r = checkout(st, inv["invoice_id"])
    assert r.status_code == 409 and "could not be closed" in r.text and len(st.sim.sessions) == 1


def test_aegis_m2_daily_job_closes_pages_that_should_no_longer_take_money(st):
    a = rr_invoice(st, "1000.00")
    pa = st.ok(checkout(st, a["invoice_id"]))["checkout"]
    st.receive("zbm", "1010", "1000.00", a["invoice_id"])                # paid by wire instead
    b = rr_invoice(st, "900.00")
    pb = st.ok(checkout(st, b["invoice_id"]))["checkout"]
    st.svc.cfg.card_prepayments = False                                  # b's page offers a card it may not
    c = rr_invoice(st, "800.00", methods=("ach",))
    pc = st.ok(checkout(st, c["invoice_id"]))["checkout"]                # still right: stays open
    out = st.ok(st.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="scheduler"))
    assert out["summary"] == {"closed": 2, "failed": 0, "checked": 2}
    assert st.sim.sessions[pa["session_id"]]["status"] == "expired"
    assert st.sim.sessions[pb["session_id"]]["status"] == "expired"
    assert st.sim.sessions[pc["session_id"]]["status"] == "open"


def test_aegis_m3_answers_from_the_other_mode_are_refused(st):
    inv = rr_invoice(st)
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    pi = st.sim.pay(cs["session_id"])
    st.sim.pis[pi]["livemode"] = True
    r = send(st, "payment_intent.succeeded", {"id": pi})
    assert r.status_code == 422 and "F13" not in memos(st) and not st.svc.db["stripe_events"]


def test_aegis_m3_more_reinstated_than_withdrawn_is_refused_whole(st):
    inv, cs, pi, _ = paid_rr(st, "1000.00", method="card")
    du = st.sim.dispute(pi)
    sent(st, "charge.dispute.created", {"id": du})
    st.sim.disputes[du]["_txns"].append(st.sim._txn(100001, 0, du, "adjustment"))
    n = len(st.svc.entries)
    posted = sum(e["event_type"] == "journal_entry_posted" for e in st.ledger.events)
    r = send(st, "charge.dispute.funds_reinstated", {"id": du})
    assert r.status_code == 422 and "reinstated more" in r.text and len(st.svc.entries) == n
    # refused BEFORE anything posted: no journal event reached the ledger without its local record
    assert sum(e["event_type"] == "journal_entry_posted" for e in st.ledger.events) == posted


def test_aegis_m3_card_is_rechecked_when_the_money_arrives(st):
    inv = rr_invoice(st, "1000.00")
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    st.svc.cfg.card_prepayments = False                                  # turned off; page not yet closed by the job
    pi = st.sim.pay(cs["session_id"], method="card")
    assert sent(st, "payment_intent.succeeded", {"id": pi})["status"] == "unapplied"
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"


def test_aegis_m3_partly_lost_dispute_is_a_receivable_with_its_own_break(st):
    inv, cs, pi, _ = paid_rr(st, "1000.00", method="card")
    du = st.sim.dispute(pi, amount=40000)
    sent(st, "charge.dispute.created", {"id": du})
    st.sim.close_dispute(du, won=False)
    sent(st, "charge.dispute.closed", {"id": du})
    i = st.svc.db["invoices"][inv["invoice_id"]]
    assert i["status"] == "paid" and i["charged_back"] == "400.00"
    assert bal(st, "1100", "client:zbm-client-7") == Decimal("400.00") and bal(st, "1300") == 0
    brk = [b for b in st.svc.db["breaks"].values() if b["leg"] == "dispute_receivable"]
    assert len(brk) == 1 and brk[0]["difference"] == "400.00" and brk[0]["explanation_code"] == "partial_chargeback"


def test_aegis_m3_a_failure_that_costs_a_fee_books_it(st):
    inv, cs, pi, _ = paid_rr(st, "1200.00")                              # fee 5.00
    st.sim.fail_after_success(pi, fee_back=-200)                         # Stripe keeps its fee and charges 2.00 more
    sent(st, "charge.failed", {"id": "ch_x", "payment_intent": pi})
    assert bal(st, "5020") == Decimal("7.00") and bal(st, "1060") == Decimal("-7.00")


def test_aegis_m3_gross_must_equal_the_amount_received(st):
    inv = rr_invoice(st, "1200.00")
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    pi = st.sim.pay(cs["session_id"])
    st.sim.pis[pi]["amount_received"] = 100000
    assert sent(st, "payment_intent.succeeded", {"id": pi})["status"] == "unapplied"


def test_aegis_m3_a_zbc_invoice_is_never_applied_from_stripe(st):
    doc = st.rate_card("camp-8")
    st.profile("camp-8", "client-8", "1200.00", "4.00", doc)
    zbc = st.deposit_invoice("camp-8", "client-8", "1200.00")
    inv = rr_invoice(st, "1200.00")
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    st.svc.db["stripe_sessions"]["cs_test_forged"] = {**st.svc.db["stripe_sessions"][cs["session_id"]],
                                                      "session_id": "cs_test_forged", "invoice_id": zbc["invoice_id"]}
    pi = st.sim.pay(cs["session_id"], metadata={"invoice_id": zbc["invoice_id"]})
    assert sent(st, "payment_intent.succeeded", {"id": pi})["status"] == "unapplied"
    assert st.svc.db["invoices"][zbc["invoice_id"]]["status"] == "issued"
    assert not [e for e in st.svc.entries if e["entity"] == "zbc" and e["memo_code"].startswith("F13")]


def test_aegis_m3_a_payout_whose_amount_changed_is_refused(st):
    paid_rr(st)
    po = st.sim.payout(50000, "in_transit")
    sent(st, "payout.created", {"id": po})
    st.sim.payouts[po].update(amount=60000, status="paid")
    r = send(st, "payout.paid", {"id": po})
    assert r.status_code == 422 and "F13p" not in memos(st)


def test_aegis_l1_won_before_the_money_is_back_keeps_the_vendor_blocked_until_it_is(st):
    bid, inv, pi = media_paid_by_stripe(st)
    du = st.sim.dispute(pi)
    sent(st, "charge.dispute.created", {"id": du})
    st.sim.disputes[du]["status"] = "won"                                # status first, money later
    sent(st, "charge.dispute.closed", {"id": du})
    assert buy(st, bid)["payment_disputed"] is True and st.svc.db["stripe_disputes"][du]["finalized"] is False
    st.sim.disputes[du]["_txns"].append(st.sim._txn(st.sim.disputes[du]["amount"], 0, du, "adjustment"))
    sent(st, "charge.dispute.funds_reinstated", {"id": du})
    assert buy(st, bid)["payment_disputed"] is False and st.svc.db["stripe_disputes"][du]["finalized"] is True


def test_aegis_l3_a_lost_dispute_after_the_vendor_was_paid_opens_one_exposure_break(st):
    bid, inv, pi = media_paid_by_stripe(st)
    st.clock.advance(days=4)
    st.ok(pay_vendor(st, bid, "10000.00", paid_on="2026-10-06"))
    du = st.sim.dispute(pi)
    sent(st, "charge.dispute.created", {"id": du})
    st.sim.close_dispute(du, won=False)
    sent(st, "charge.dispute.closed", {"id": du})
    assert len([b for b in st.svc.db["breaks"].values() if b["leg"] == "media_exposure"]) == 1
    assert buy(st, bid)["status"] == "payment_returned"


def test_aegis_l7_an_unsigned_call_writes_nothing_to_the_ledger(st):
    before = len(st.ledger.events) if hasattr(st.ledger, "events") else None
    r = st.post("/fin/v1/stripe/events", {"request_id": rid(), "payload": '{"id":"evt_1"}', "signature": "t=1,v1=0"},
                caller="rail_gateway")
    assert r.status_code == 409
    if before is not None:
        assert len(st.ledger.events) == before


def test_a_fee_credit_from_stripe_is_booked_not_crashed_on(st):
    inv = rr_invoice(st, "1200.00")
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    pi = st.sim.pay(cs["session_id"], fee=-100)                          # e.g. a promotional fee credit
    assert sent(st, "payment_intent.succeeded", {"id": pi})["status"] == "matched"
    assert bal(st, "1060") == Decimal("1201.00") and bal(st, "5020") == Decimal("-1.00")


# --------------------------------------------------------------------------------------------- AEGIS re-verification of d372d2b

def test_aegis_n1_a_refused_dispute_never_leaves_its_payment_half_written(st):
    inv = rr_invoice(st, "1000.00")
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    pi = st.sim.pay(cs["session_id"], method="card")                     # its payment event never arrived
    du = st.sim.dispute(pi)
    st.sim.disputes[du]["_txns"].append(st.sim._txn(100001, 0, du, "adjustment"))   # an impossible reinstatement
    r = send(st, "charge.dispute.created", {"id": du})
    assert r.status_code == 422
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "paid" and "F13" in memos(st)
    posted = [e["payload"]["entry_id"] for e in st.ledger.events if e["event_type"] == "journal_entry_posted"]
    assert sorted(posted) == sorted(st.svc.entries_by_id)                # every ledger journal event is local


def test_aegis_n2_the_page_job_can_run_again_the_same_day(st):
    inv = rr_invoice(st, "1000.00")
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    st.svc.cfg.card_prepayments = False
    st.sim.fail_next = [503]
    first = st.ok(st.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="scheduler"))
    assert first["summary"]["failed"] == 1 and st.sim.sessions[cs["session_id"]]["status"] == "open"
    st.sim.fail_next = []
    second = st.ok(st.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="scheduler"))
    assert second["already_ran"] is False and second["summary"]["closed"] == 1
    assert st.sim.sessions[cs["session_id"]]["status"] == "expired"


def test_aegis_n3_a_busy_stripe_turn_answers_503_instead_of_queueing(st, monkeypatch):
    import svc_stripe
    monkeypatch.setattr(svc_stripe, "STRIPE_WAIT_S", 0.2)
    inv = rr_invoice(st)
    lk = st.svc._stripe_lock()
    lk.acquire()
    try:
        r = checkout(st, inv["invoice_id"])
        assert r.status_code == 503 and "busy" in r.text and r.json()["took_effect"] is False
        payload, sig = st.sim.event("customer.created", {"id": "cus_1"})
        r = st.post("/fin/v1/stripe/events", {"request_id": rid(), "payload": payload, "signature": sig},
                    caller="rail_gateway")
        assert r.status_code == 503
    finally:
        lk.release()
    assert st.ok(checkout(st, inv["invoice_id"]))["checkout"]["status"] == "open"


def test_aegis_n4_pages_closed_before_a_refusal_are_remembered(st):
    inv = rr_invoice(st, "1000.00")
    a = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    ghost = {**a, "session_id": "cs_test_9999gone", "created_at": "2026-10-02T17:00:01+00:00"}
    st.svc.db["stripe_sessions"]["cs_test_9999gone"] = ghost             # a page Stripe no longer knows
    st.svc.cfg.card_prepayments = False
    r = checkout(st, inv["invoice_id"])
    assert r.status_code == 409 and "could not be closed" in r.text
    assert st.sim.sessions[a["session_id"]]["status"] == "expired"
    assert st.svc.db["stripe_sessions"][a["session_id"]]["status"] == "expired"


def test_aegis_n5_a_fifo_at_the_secret_path_is_refused_not_waited_on(tmp_path, keys):
    import threading
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    out = {}

    def load():
        try:
            config_mod.load({**Harness().env, **keys, "FIN_STRIPE_SECRET_KEY_FILE": str(fifo)})
        except RuntimeError as exc:
            out["err"] = str(exc)
    t = threading.Thread(target=load, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive() and "not a regular file" in out["err"]


def test_aegis_n6_a_paid_page_whose_payment_failed_does_not_block_a_new_page(st):
    inv = rr_invoice(st, "1000.00")
    a = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    pi = st.sim.pay(a["session_id"], settle=False)                       # ACH started ...
    st.sim.pis[pi]["status"] = "requires_payment_method"                 # ... and failed before it succeeded
    st.svc.cfg.card_prepayments = False
    b = st.ok(checkout(st, inv["invoice_id"]))
    assert b["checkout"]["session_id"] != a["session_id"] and b["checkout"]["methods"] == ["us_bank_account"]
    assert st.svc.db["stripe_sessions"][a["session_id"]]["status"] == "payment_failed"


def test_aegis_r1_a_dispute_finance_cannot_book_still_blocks_the_vendor(st):
    m = create(st)
    inv = issue(st, m["invoice"])
    bid = m["media_buy"]["buy_id"]
    cs = st.ok(checkout(st, inv["invoice_id"]))["checkout"]
    pi = st.sim.pay(cs["session_id"])
    du = st.sim.dispute(pi)
    st.sim.disputes[du]["_txns"].append(st.sim._txn(1150001, 0, du, "adjustment"))   # anomalous: refused
    for _ in range(3):
        assert send(st, "charge.dispute.created", {"id": du}).status_code == 422
    assert buy(st, bid)["payment_disputed"] is True
    assert len([b for b in st.svc.db["breaks"].values() if b["leg"] == "stripe_dispute"]) == 1
    crc = [c for c in st.svc.db["client_receipts"].values() if c["invoice_id"] == inv["invoice_id"]][0]
    assert crc["status"].startswith("withdrawn")
    st.clock.advance(days=4)
    r = pay_vendor(st, bid, "10000.00", paid_on="2026-10-06")
    assert r.status_code == 409 and "disputed the prepayment" in r.text
    posted = [e["payload"]["entry_id"] for e in st.ledger.events if e["event_type"] == "journal_entry_posted"]
    assert sorted(posted) == sorted(st.svc.entries_by_id)


# --------------------------------------------------------------------------------------------- bank feed x Stripe payouts

def bank_line(hr, amount, token=None, value_date="2026-10-03", tag=None):
    line = {"txn_ref_sha256": hashlib.sha256((tag or rid("bk")).encode()).hexdigest(), "entity": "zbm",
            "account": "1010", "direction": "credit", "amount": amount, "value_date": value_date}
    if token:
        line["reference_token"] = token
    return hr.ok(hr.post("/fin/v1/bank/events", {"request_id": rid(), "lines": [line]}, caller="bank_feed"))


def test_a_stripe_payout_on_the_bank_statement_is_matched_not_booked_twice(st):
    paid_rr(st, "1200.00")                                               # 1060 = 1195.00
    po = st.sim.payout(119500, "paid")
    sent(st, "payout.paid", {"id": po})
    assert bal(st, "1010") == Decimal("1195.00")
    r = bank_line(st, "1195.00")
    assert r["results"][0]["status"] == "stripe_payout"
    assert bal(st, "1010") == Decimal("1195.00") and bal(st, "2070") == 0       # not counted twice
    assert not [b for b in st.svc.db["breaks"].values() if b["leg"] == "unapplied_cash"]
    assert st.svc.db["stripe_payouts"][po]["bank_receipt_id"] == r["results"][0]["receipt_id"]
    assert bank_line(st, "1195.00")["results"][0]["status"] == "unapplied"    # a second, different credit is not it


def test_the_bank_can_arrive_before_stripes_paid_event(st):
    paid_rr(st, "1200.00")
    po = st.sim.payout(119500, "in_transit")
    sent(st, "payout.created", {"id": po})
    r = bank_line(st, "1195.00")
    assert r["results"][0]["status"] == "stripe_payout"
    assert bal(st, "1010") == Decimal("1195.00") and bal(st, "1060") == 0
    st.sim.payouts[po]["status"] = "paid"
    sent(st, "payout.paid", {"id": po})
    assert bal(st, "1010") == Decimal("1195.00") and memos(st).count("F13p") == 1


def test_payout_matching_by_id_amount_and_window(st):
    paid_rr(st, "1200.00")
    a = st.sim.payout(50000, "in_transit")
    b = st.sim.payout(50000, "in_transit")
    sent(st, "payout.created", {"id": a})
    sent(st, "payout.created", {"id": b})
    assert bank_line(st, "500.00", token=b)["results"][0]["status"] == "stripe_payout"   # exact by payout id
    assert st.svc.db["stripe_payouts"][b]["bank_receipt_id"] and not st.svc.db["stripe_payouts"][a]["bank_receipt_id"]
    assert bank_line(st, "500.00", token="po_0000nosuch")["results"][0]["status"] == "unapplied"
    assert bank_line(st, "500.01")["results"][0]["status"] == "unapplied"                 # amount must match
    assert bank_line(st, "500.00", value_date="2026-10-20")["results"][0]["status"] == "unapplied"   # outside window
    assert bank_line(st, "500.00")["results"][0]["status"] == "stripe_payout"             # a, by amount and window
    assert memos(st).count("F13p") == 2


def test_an_invoice_reference_still_wins_over_a_payout_of_the_same_amount(st):
    paid_rr(st, "1200.00")
    po = st.sim.payout(119500, "in_transit")
    sent(st, "payout.created", {"id": po})
    inv = rr_invoice(st, "1195.00", methods=("ach", "wire"))
    st.bank.deposit("zbm", "1010", "1195.00")
    assert bank_line(st, "1195.00", token=inv["invoice_id"])["results"][0]["status"] == "matched"
    assert not st.svc.db["stripe_payouts"][po]["bank_receipt_id"]


# --------------------------------------------------------------------------------------------- AEGIS review of da054ef

def test_aegis_lh_h1_a_bad_line_refuses_the_batch_before_anything_posts(st):
    paid_rr(st, "1200.00")
    po = st.sim.payout(119500, "in_transit")
    sent(st, "payout.created", {"id": po})
    posted = sum(e["event_type"] == "journal_entry_posted" for e in st.ledger.events)
    good = {"txn_ref_sha256": "a" * 64, "entity": "zbm", "account": "1010", "direction": "credit",
            "amount": "1195.00", "value_date": "2026-10-03"}
    bad = {**good, "txn_ref_sha256": "b" * 64, "account": "1020"}
    r = st.post("/fin/v1/bank/events", {"request_id": rid(), "lines": [good, bad]}, caller="bank_feed")
    assert r.status_code == 422
    assert sum(e["event_type"] == "journal_entry_posted" for e in st.ledger.events) == posted
    st.sim.payouts[po]["status"] = "paid"
    assert sent(st, "payout.paid", {"id": po})["status"] == "payout_paid"      # Stripe ingestion not blocked
    assert bank_line(st, "1195.00", tag="retry")["results"][0]["status"] == "stripe_payout"
    assert bal(st, "1010") == Decimal("1195.00") and memos(st).count("F13p") == 1


def test_aegis_lh_h2_bank_before_stripe_ever_mentions_the_payout_is_never_booked_twice(st):
    paid_rr(st, "1200.00")
    po = st.sim.payout(119500, "paid")                                    # payout.created never delivered
    r = bank_line(st, "1195.00", token=po)
    assert r["results"][0]["status"] == "unapplied" and bal(st, "2070") == Decimal("1195.00")
    sent(st, "payout.paid", {"id": po})
    assert bal(st, "1010") == Decimal("1195.00") and bal(st, "2070") == 0 and bal(st, "1060") == 0
    rc = st.svc.db["receipts"][r["results"][0]["receipt_id"]]
    assert rc["status"] == "stripe_payout" and rc["stripe_payout_id"] == po
    brk = [b for b in st.svc.db["breaks"].values() if b.get("receipt_id") == rc["receipt_id"]]
    assert brk and brk[0]["status"] == "resolved"


def test_aegis_lh_m1_m2_amount_matching_needs_no_reference_and_a_first_seen_date(st):
    paid_rr(st, "1200.00")
    po = st.sim.payout(50000, "in_transit")
    sent(st, "payout.created", {"id": po})
    assert bank_line(st, "500.00", token="fin-inv-TYPO")["results"][0]["status"] == "unapplied"   # M2
    st.svc.db["stripe_payouts"][po] = {k: v for k, v in st.svc.db["stripe_payouts"][po].items()
                                       if k != "first_seen_on"}                                  # a legacy record
    assert bank_line(st, "500.00", value_date="2027-06-01")["results"][0]["status"] == "unapplied"  # M1
    assert bank_line(st, "500.00", token=po)["results"][0]["status"] == "stripe_payout"           # exact id still


def test_aegis_lh_l1_a_payout_failed_after_the_bank_showed_it_opens_a_break(st):
    paid_rr(st, "1200.00")
    po = st.sim.payout(119500, "paid")
    sent(st, "payout.paid", {"id": po})
    bank_line(st, "1195.00")
    st.sim.payouts[po]["status"] = "failed"
    sent(st, "payout.failed", {"id": po})
    assert [b for b in st.svc.db["breaks"].values() if b["leg"] == "stripe_payout" and b["status"] == "open"]


@pytest.mark.parametrize("bad", [True, "1", " 1.0", 1.0])
def test_aegis_lh_l6_contract_versions_are_never_coerced(bad):
    import models as m
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        m.DocRef.model_validate({"doc_id": "client_msa", "version": bad, "doc_sha256": "c" * 64,
                                 "acceptance_id": "a1"})
    assert m.DocRef.model_validate({"doc_id": "x", "version": "1.1", "doc_sha256": "c" * 64,
                                    "acceptance_id": "a1"}).version == "1.1"



# --------------------------------------------------------------------------------------------- AEGIS re-review of d1b2afe

def test_aegis_lh_n2_an_amount_match_is_only_suggested_never_relabelled(st):
    paid_rr(st, "1200.00")
    po = st.sim.payout(119500, "paid")
    r = bank_line(st, "1195.00")                                          # a client ACH with no reference, or the payout
    rct = r["results"][0]["receipt_id"]
    sent(st, "payout.paid", {"id": po})
    rc = st.svc.db["receipts"][rct]
    assert rc["status"] == "unapplied"                                    # not relabelled
    brk = st.svc.db["breaks"][[k for k, b in st.svc.db["breaks"].items() if b.get("receipt_id") == rct][0]]
    assert brk["status"] == "open" and brk["suggested_stripe_payout_id"] == po
    assert bal(st, "1010") == Decimal("2390.00") and bal(st, "1060") == 0  # visible to L2 against the bank


def test_aegis_lh_n1_a_receipt_andre_already_reversed_is_never_reclassified(st):
    paid_rr(st, "1200.00")
    po = st.sim.payout(119500, "paid")
    r = bank_line(st, "1195.00", token=po)
    rct = r["results"][0]["receipt_id"]
    entry = st.svc.entries_by_id[st.svc.db["receipts"][rct]["entry_id"]]
    st.ok(st.post("/fin/v1/journal/zbm/corrections", {"request_id": rid(), "effective_date": "2026-10-02",
                                                        "reverses_entry_id": entry["entry_id"]}, andre=ANDRE_TOKEN), 201)
    sent(st, "payout.paid", {"id": po})
    assert bal(st, "2070") == 0 and bal(st, "1010") == Decimal("1195.00")
    assert st.svc.db["receipts"][rct]["status"] == "unapplied"


def test_aegis_lh_n4_an_explained_break_is_also_closed_on_an_exact_match(st):
    paid_rr(st, "1200.00")
    po = st.sim.payout(119500, "paid")
    r = bank_line(st, "1195.00", token=po)
    rct = r["results"][0]["receipt_id"]
    bid = [k for k, b in st.svc.db["breaks"].items() if b.get("receipt_id") == rct][0]
    st.svc.db["breaks"][bid] = {**st.svc.db["breaks"][bid], "status": "explained"}
    sent(st, "payout.paid", {"id": po})
    assert st.svc.db["breaks"][bid]["status"] == "resolved"
