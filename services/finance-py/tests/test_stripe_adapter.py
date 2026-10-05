"""The Stripe incoming adapter on its own: signature scheme, money conversion, request shape, error mapping, and the
strict check of what it hands the service (ADR 0009 amendment, Oct 5 2026)."""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal

import httpx
import pytest

import answers as A
from clock import FixedClock
from helpers import NOW
from ports import RailBalance, StripeCheckout, StripeDispute, StripePayment, StripePayout, StripeSession
from stripe_incoming import (API_VERSION, StripeIncoming, form_pairs, from_cents, to_cents, verify_signature)
from stripe_sim import TEST_KEY, TEST_WHSEC, SimStripe

T = int(NOW.timestamp())


def sig(body: bytes, t: int = T, secret: str = TEST_WHSEC) -> str:
    return hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def adapter(sim=None, handler=None):
    clock = FixedClock(NOW)
    sim = sim or SimStripe(clock)
    tr = httpx.MockTransport(handler) if handler else sim.transport()
    return StripeIncoming(TEST_KEY, TEST_WHSEC, False, "https://x.example/ok", "https://x.example/no", transport=tr,
                          clock=clock), sim


# ------------------------------------------------------------------------------------------------- signatures

def test_signature_scheme():
    body = b'{"id":"evt_1"}'
    good = sig(body)
    assert verify_signature(body, f"t={T},v1={good}", TEST_WHSEC, T)
    assert verify_signature(body, f"t={T},v1={'0' * 64},v1={good}", TEST_WHSEC, T)          # rolling secrets
    assert verify_signature(body, f"t={T},v0={'1' * 64},v1={good}", TEST_WHSEC, T)          # other schemes ignored
    assert verify_signature(body, f"t={T},v1={good}", TEST_WHSEC, T + 300)                   # at the tolerance
    for header, now in [
        (f"t={T},v1={good}", T + 301), (f"t={T},v1={good}", T - 301),
        (f"t={T},v1={sig(body, secret='whsec_other')}", T), (f"t={T + 1},v1={good}", T),
        (f"v1={good}", T), (f"t={T}", T), (f"t={T},t={T},v1={good}", T), (f"t=-{T},v1={good}", T),
        (f"t={T},v1={good.upper()}", T), (f"t={T},v1={good[:-1]}", T), ("", T), (None, T),
        (f"t={T},v1={good}," + "x" * 1100, T),
    ]:
        assert not verify_signature(body, header, TEST_WHSEC, now), header
    assert not verify_signature(body + b" ", f"t={T},v1={good}", TEST_WHSEC, T)
    assert not verify_signature(body, f"t={T},v1={good}", "", T)


# ------------------------------------------------------------------------------------------------- money and forms

def test_cents_are_exact():
    assert to_cents("0.01") == 1 and to_cents("1200.00") == 120000 and to_cents("999999.99") == 99999999
    assert from_cents(0) == "0.00" and from_cents(5) == "0.05" and from_cents(-1500) == "-15.00"
    assert from_cents(99999999) == "999999.99"
    for bad in ("1.5", "1.005", "-1.00", "01.00", "1e3", 12, None, "1,000.00"):
        with pytest.raises(ValueError):
            to_cents(bad)
    for bad in (1.5, "100", True, None):
        with pytest.raises(ValueError):
            from_cents(bad)
    assert all(Decimal(from_cents(n)) * 100 == n for n in range(-1000, 1000, 7))


def test_form_pairs_use_stripes_bracket_syntax():
    got = form_pairs({"a": 1, "b": {"c": "x", "d": [1, 2]}, "e": [{"f": {"g": 3}}], "h": None, "i": True})
    assert got == [("a", "1"), ("b[c]", "x"), ("b[d][0]", "1"), ("b[d][1]", "2"), ("e[0][f][g]", "3"), ("i", "true")]


# ------------------------------------------------------------------------------------------------- checkout create

def test_create_checkout_request_and_answer():
    ad, sim = adapter()
    ans = ad.create_checkout("fin-inv-ABC", "1234.56", ("us_bank_account",), "fin-chk-1", T + 3600, "Invoice X")
    assert ans.outcome == "created" and ans.url.startswith("https://checkout.stripe.com/")
    assert A.check(ans, StripeCheckout("unavailable")) is ans
    req = sim.requests[0]
    assert req["headers"]["stripe-version"] == API_VERSION and req["headers"]["idempotency-key"] == "fin-chk-1"
    assert req["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert req["form"]["line_items[0][price_data][unit_amount]"] == "123456"
    assert "TEST" not in repr(ad) and TEST_KEY not in repr(ad)


@pytest.mark.parametrize("status,outcome", [(400, "rejected"), (401, "rejected"), (402, "rejected"),
                                            (429, "transport_error"), (500, "transport_error"),
                                            (503, "transport_error")])
def test_create_checkout_error_mapping(status, outcome):
    ad, sim = adapter()
    sim.fail_next = [status]
    assert ad.create_checkout("fin-inv-1", "10.00", ("card",), "k", T + 3600, "x").outcome == outcome


def test_create_checkout_refuses_bad_input_without_calling_stripe():
    ad, sim = adapter()
    for args in [("bad id!", "10.00", ("card",)), ("i", "10", ("card",)), ("i", "10.00", ()),
                 ("i", "10.00", ("paypal",))]:
        assert ad.create_checkout(*args, "k", T + 3600, "x").outcome == "rejected"
    assert not sim.requests


def test_create_checkout_answer_must_match_the_request():
    def handler(request):
        return httpx.Response(200, json={"id": "cs_test_1", "object": "checkout.session", "url":
                                         "https://checkout.stripe.com/c/pay/cs_test_1", "expires_at": T + 3600,
                                         "livemode": True, "amount_total": 1000, "client_reference_id": "i",
                                         "currency": "usd"})
    ad, _ = adapter(handler=handler)                                       # livemode differs: refused
    assert ad.create_checkout("i", "10.00", ("card",), "k", T + 3600, "x").outcome == "rejected"


def test_transport_failures_are_unavailable_never_a_pass():
    def boom(request):
        raise httpx.ConnectError("down")
    ad, _ = adapter(handler=boom)
    assert ad.create_checkout("i", "10.00", ("card",), "k", T + 3600, "x").outcome == "transport_error"
    assert not ad.payment("pi_123").available and not ad.dispute("du_1").available
    assert not ad.payout("po_1").available and not ad.session("cs_1").available and not ad.balance().available

    def redirect(request):
        return httpx.Response(302, headers={"location": "https://evil.example/"})
    ad, _ = adapter(handler=redirect)
    assert not ad.payment("pi_123").available

    def huge(request):
        return httpx.Response(200, content=b"{" + b" " * (1024 * 1024 + 10) + b"}")
    ad, _ = adapter(handler=huge)
    assert not ad.balance().available


# ------------------------------------------------------------------------------------------------- reads

def test_reads_map_stripe_objects():
    ad, sim = adapter()
    cs = ad.create_checkout("fin-inv-1", "100.00", ("us_bank_account", "card"), "k", T + 3600, "x")
    pi = sim.pay(cs.session_id, method="card")
    p = ad.payment(pi)
    assert (p.status, p.amount_received, p.method, p.gross, p.fee, p.invoice_id, p.charge_status) == \
        ("succeeded", "100.00", "card", "100.00", "3.20", "fin-inv-1", "succeeded")
    assert A.check(p, StripePayment(False)) is p
    s = ad.session(cs.session_id)
    assert s.status == "complete" and s.payment_intent == pi and A.check(s, StripeSession(False)) is s
    du = sim.dispute(pi)
    d = ad.dispute(du)
    assert d.status == "needs_response" and d.txns[0]["amount"] == "-100.00" and d.txns[0]["fee"] == "15.00"
    assert A.check(d, StripeDispute(False)).txns == d.txns
    po = sim.payout(9680, "paid")
    o = ad.payout(po)
    assert (o.status, o.amount) == ("paid", "96.80") and A.check(o, StripePayout(False)) is o
    b = ad.balance()
    assert b.available and b.balance == "-115.00"        # +96.80 charge net, -115.00 dispute net, -96.80 payout
    assert A.check(b, RailBalance(False)) is b
    assert ad.payment("pi_doesnotexist").found is False
    assert ad.payment("../../v1/balance").found is False and len(sim.requests) == 7   # never put into a URL


def test_a_200_that_is_not_the_object_asked_for_is_unavailable():
    def other(request):
        return httpx.Response(200, json={"id": "pi_someoneelse", "object": "payment_intent", "status": "succeeded"})
    ad, _ = adapter(handler=other)
    assert not ad.payment("pi_mine").available
    assert not ad.session("cs_mine").available


def test_the_strict_check_refuses_incomplete_or_odd_answers():
    for bad in [StripePayment(True, found=True, payment_intent="pi_1"),                      # no status
                StripePayment(True, found=True, payment_intent="pi_1", status="succeeded", amount_received="1.0",
                              currency="usd"),
                StripePayment(True, found=True, payment_intent="pi_1", status="weird", amount_received="1.00",
                              currency="usd"),
                StripePayment(True, found=True, payment_intent="../x", status="succeeded", amount_received="1.00",
                              currency="usd")]:
        with pytest.raises(A.Malformed):
            A.check(bad, StripePayment(False))
    with pytest.raises(A.Malformed):
        A.check(StripeCheckout("created", session_id="cs_1", url="https://evil.example/pay"), StripeCheckout("x"))
    with pytest.raises(A.Malformed):
        A.check(StripeDispute(True, found=True, dispute_id="du_1", status="lost", amount="1.00",
                              txns=({"txn_id": "txn_1", "amount": "-1.00"},)), StripeDispute(False))
