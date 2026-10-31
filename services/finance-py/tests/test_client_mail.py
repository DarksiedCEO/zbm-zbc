"""Client email (ADR 0026; red team OPS-H3, RTM-12): the Resend adapter, what a message may contain, who receives it,
the outbox with retries and backoff, the alert on permanent failure, and the cron caller's narrow reach.

No message ever leaves the machine: the Resend adapter is driven through an httpx.MockTransport."""

from __future__ import annotations

import json
import logging
import os

import httpx
import pytest

import api
import config as config_mod
import mail as MAIL
from helpers import ANDRE_TOKEN, Harness, base_env, make_fakes, rid
from ports import MailOutcome, NotBuiltClientMail, Ports
from test_credit_notes import CLIENT, ach_invoice, credit

CONTACT = "ap@client-seven.example"


def harness(adapter=None) -> Harness:
    from clock import FixedClock
    from helpers import NOW
    clock = FixedClock(NOW)
    f = make_fakes(clock)
    f["client_mail"] = adapter if adapter is not None else MAIL.FixtureClientMail()
    return Harness(clock=clock, fakes=f).ready()


def set_contact(hr, client=CLIENT, email=CONTACT):
    return hr.ok(hr.put(f"/fin/v1/clients/{client}/billing-contact", {"request_id": rid(), "email": email,
                                                                       "name": "Accounts payable"}))


def run_mail(hr):
    return hr.ok(hr.post("/fin/v1/jobs/client-mail/run", {"request_id": rid()}, caller="ops_cron"))["summary"]


def paid(hr, price="2000.00"):
    inv = ach_invoice(hr, price)
    hr.receive("zbm", "1010", price, inv["invoice_id"])
    return hr.svc.db["invoices"][inv["invoice_id"]]


# --------------------------------------------------------------------------------------------- what a message holds

BAD = ("<img", "<script", "<a ", "href", "src=", "http://", "https://", "pixel", "track", "beacon", "<link",
       "url(", "@import")


@pytest.mark.parametrize("kind,data", [
    ("receipt", {"invoice_id": "fin-inv-1", "what_you_bought": "SEO Technical Audit",
                 "items": [{"description": "SEO Technical Audit", "amount": "2000.00"}], "period": None,
                 "amount_paid": "2000.00", "currency": "USD", "paid_on": "2026-10-02", "method": "ACH or wire"}),
    ("credit_note", {"credit_note_number": "CN-2026-0001", "invoice_id": "fin-inv-1", "amount": "400.00",
                     "issued_on": "2026-10-02", "refund_amount": "400.00", "refund_due_by": "2026-10-16",
                     "amount_still_due": "0.00"}),
    ("invoice_cancelled", {"invoice_id": "fin-inv-1", "amount_cancelled": "1000.00", "cancelled_on": "2026-10-02"}),
    ("refund_sent", {"credit_note_number": "CN-2026-0001", "invoice_id": "fin-inv-1", "refund_amount": "400.00",
                     "refunded_on": "2026-10-05"}),
    ("acknowledgement", {"topic": "refund_request", "received_on": "2026-10-02", "invoice_id": "fin-inv-1",
                         "reply_by": None}),
])
def test_every_kind_is_plain_text_and_simple_html_with_no_tracking_and_no_internal_fields(kind, data):
    poison = {**data, "hold_reason": "OFAC SDN match", "sanctions": True, "sanctions_ground": "match",
              "held_compliance": {"reason": "Compliance refused: sanctions"}, "note": "internal: client is shady"}
    subject, text, page = MAIL.render(kind, poison)
    for part in (subject, text, page):
        low = part.lower()
        for bad in BAD:
            assert bad not in low, (kind, bad)
        for word in ("ofac", "sanction", "hold", "compliance", "shady", "internal"):
            assert word not in low, (kind, word)
    assert page.startswith("<!doctype html>") and "billing@zbestmedia.com" in text
    assert MAIL.render(kind, data) == (subject, text, page)        # the extra fields changed nothing


def test_unknown_kinds_are_refused():
    with pytest.raises(ValueError):
        MAIL.render("marketing", {})


# --------------------------------------------------------------------------------------------- the Resend adapter

def resend(handler):
    return MAIL.ResendClientMail("re_test_FinanceSimulatedResendKey0000", "Z Best Media <billing@zbestmedia.com>",
                                 transport=httpx.MockTransport(handler))


def msg(to=CONTACT, kind="invoice_cancelled", key="fin-mail-ABC"):
    return MAIL.message(kind, CLIENT, to, {"invoice_id": "fin-inv-1", "amount_cancelled": "10.00",
                                           "cancelled_on": "2026-10-02"}, key)


def test_resend_sends_one_message_to_the_contact_on_file_only_with_an_idempotency_key():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, json={"id": "4ef9a417-02e9-4d39-ad75-9611e0fcc33c"})
    out = resend(handler).deliver(msg())
    assert out == MailOutcome("sent", provider_id="4ef9a417-02e9-4d39-ad75-9611e0fcc33c")
    (req,) = seen
    assert req.method == "POST" and str(req.url) == "https://api.resend.com/emails"
    assert req.headers["authorization"] == "Bearer re_test_FinanceSimulatedResendKey0000"
    assert req.headers["idempotency-key"] == "fin-mail-ABC"
    body = json.loads(req.content)
    assert body["to"] == [CONTACT] and body["from"] == "Z Best Media <billing@zbestmedia.com>"
    assert body["reply_to"] == "billing@zbestmedia.com"
    assert set(body) == {"from", "to", "subject", "text", "html", "reply_to"}         # no cc, bcc, tags, headers
    assert "re_test_" not in repr(resend(handler))


def test_resend_without_a_recipient_makes_no_request():
    calls = []
    out = resend(lambda r: calls.append(r) or httpx.Response(200, json={"id": "x"})).deliver(msg(to=None))
    assert out.outcome == "no_recipient" and calls == []


@pytest.mark.parametrize("status,name,outcome", [
    (409, "concurrent_idempotent_requests", "retry"), (409, "invalid_idempotent_request", "permanent"),
    (429, "rate_limit_exceeded", "retry"), (500, "application_error", "retry"), (503, "service_unavailable", "retry"),
    (422, "missing_required_field", "permanent"), (403, "validation_error", "permanent"),
    (401, "validation_error", "permanent"), (400, "validation_error", "permanent"),
])
def test_resend_errors_are_retry_or_permanent(status, name, outcome):
    out = resend(lambda r: httpx.Response(status, json={"statusCode": status, "name": name, "message": "x"})) \
        .deliver(msg())
    assert out.outcome == outcome and out.code.startswith(str(status))


def test_resend_network_failure_and_a_200_without_an_id_are_retried():
    def boom(req):
        raise httpx.ConnectError("no route", request=req)
    assert resend(boom).deliver(msg()).outcome == "retry"
    assert resend(lambda r: httpx.Response(200, text="<html>proxy</html>")).deliver(msg()).outcome == "permanent"


# --------------------------------------------------------------------------------------------- configuration

def _key_file(tmp_path, value="re_live_FinanceSimulatedResendKey0000"):
    p = tmp_path / "resend"
    p.write_text(value + "\n")
    os.chmod(p, 0o600)
    return str(p)


def test_mail_is_off_by_default_and_needs_both_settings(tmp_path):
    s = config_mod.load(base_env())
    assert s.resend_api_key is None and s.mail_from is None
    assert isinstance(api.build_ports(s).client_mail, NotBuiltClientMail)
    assert isinstance(Ports().client_mail, NotBuiltClientMail)
    s = config_mod.load(base_env(MAIL_FROM="Z Best Media <billing@zbestmedia.com>"))   # IaC sets it; no key: off
    assert s.mail_from and s.resend_api_key is None
    assert isinstance(api.build_ports(s).client_mail, NotBuiltClientMail)
    with pytest.raises(RuntimeError, match="MAIL_FROM"):
        config_mod.load(base_env(RESEND_API_KEY_FILE=_key_file(tmp_path)))
    s = config_mod.load(base_env(RESEND_API_KEY_FILE=_key_file(tmp_path), MAIL_FROM="billing@zbestmedia.com"))
    assert "re_live" not in repr(s)
    port = api.build_ports(s).client_mail
    assert isinstance(port, MAIL.ResendClientMail) and port.sender == "billing@zbestmedia.com"


@pytest.mark.parametrize("sender", ["andre@zbestmedia.com", "billing@zbestmedia.co", "billing@evil.example",
                                    "Z Best <billing@zbestmedia.com>, x@y.z", "billing@zbestmedia.com\nBcc: x@y.z"])
def test_mail_from_is_billing_at_zbestmedia_only(tmp_path, sender):
    with pytest.raises(RuntimeError, match="MAIL_FROM"):
        config_mod.load(base_env(RESEND_API_KEY_FILE=_key_file(tmp_path), MAIL_FROM=sender))


def test_a_key_that_is_not_a_resend_key_refuses_start(tmp_path):
    with pytest.raises(RuntimeError, match="RESEND_API_KEY_FILE"):
        config_mod.load(base_env(RESEND_API_KEY_FILE=_key_file(tmp_path, "xx_notaresendkey0000000000"),
                                 MAIL_FROM="billing@zbestmedia.com"))


# --------------------------------------------------------------------------------------------- the outbox and the job

def test_receipts_wait_for_a_billing_contact_then_go_to_it_and_only_it():
    hr = harness()
    inv = paid(hr)
    s = run_mail(hr)
    assert s["sent"] == 0 and hr.f["client_mail"].delivered == []
    crc = [r for r in hr.svc.db["client_receipts"].values() if r["invoice_id"] == inv["invoice_id"]][0]
    assert crc["status"] == "pending_send" and crc["attempts"] == 0 and crc["waiting_for"] == "a billing contact"
    brk = [b for b in hr.svc.db["breaks"].values() if b["leg"] == "client_mail_no_contact"]
    assert len(brk) == 1 and brk[0]["status"] == "open"
    assert run_mail(hr)["sent"] == 0                         # waiting: untouched, no new attempt
    set_contact(hr)
    assert [b for b in hr.svc.db["breaks"].values() if b["leg"] == "client_mail_no_contact"][0]["status"] == "resolved"
    hr.clock.advance(minutes=16)
    assert run_mail(hr)["sent"] == 1
    (m,) = hr.f["client_mail"].delivered
    assert m["to"] == CONTACT and m["kind"] == "receipt" and "2000.00" in m["text"]
    assert hr.svc.db["client_receipts"][crc["client_receipt_id"]]["status"] == "sent"
    # the contact is in Finance's store, the ledger has only its hash
    assert CONTACT not in json.dumps(hr.ledger.events) and hr.ledger.of_type("client_contact_recorded")
    assert run_mail(hr)["sent"] == 0                         # never twice


def test_credit_notes_and_acknowledgements_are_queued_with_the_booking_and_sent_by_the_job():
    hr = harness()
    set_contact(hr)
    inv = ach_invoice(hr)
    out = hr.ok(credit(hr, inv, "100.00"), 201)
    assert hr.f["client_mail"].delivered == []               # the booking never waits on mail
    ack = hr.ok(hr.post(f"/fin/v1/clients/{CLIENT}/acknowledgements",
                        {"request_id": rid(), "topic": "billing_dispute", "received_on": "2026-10-01",
                         "invoice_id": inv["invoice_id"]}, andre=ANDRE_TOKEN), 201)
    assert ack["acknowledge_by"] == "2026-10-05" and ack["late"] is False          # 2 business days (MSA 4.12(f))
    assert run_mail(hr)["sent"] == 2
    kinds = sorted(m["kind"] for m in hr.f["client_mail"].delivered)
    assert kinds == ["acknowledgement", "credit_note"]
    cn_mail = [m for m in hr.f["client_mail"].delivered if m["kind"] == "credit_note"][0]
    assert out["credit_note"]["number"] in cn_mail["subject"] and "900.00" in cn_mail["text"]
    ack_mail = [m for m in hr.f["client_mail"].delivered if m["kind"] == "acknowledgement"][0]
    assert "2026-10-23" in ack_mail["text"]        # 15 business days (Columbus Day skipped) to resolve (policy 8.3)
    assert all(m["to"] == CONTACT for m in hr.f["client_mail"].delivered)


def test_retries_back_off_then_fail_for_good_with_a_break_and_an_alert(caplog):
    hr = harness()
    set_contact(hr)
    inv = ach_invoice(hr)
    hr.ok(credit(hr, inv, "100.00"), 201)
    fx = hr.f["client_mail"]
    fx.outcomes = [MailOutcome("retry", code="503:service_unavailable")] * 7
    (mid,) = list(hr.svc.db["mail_outbox"])
    waits = []
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        for i in range(7):
            run_mail(hr)
            rec = hr.svc.db["mail_outbox"][mid]
            assert rec["attempts"] == i + 1
            if i < 6:
                assert rec["status"] == "pending"
                assert run_mail(hr)["sent"] == 0 and hr.svc.db["mail_outbox"][mid]["attempts"] == i + 1  # backoff
                waits.append(15 * 2 ** i if 15 * 2 ** i < 480 else 480)
                hr.clock.advance(minutes=min(15 * 2 ** i, 480) + 1)
    assert waits == [15, 30, 60, 120, 240, 480] and sum(waits) < 24 * 60          # inside Resend's 24 h key window
    rec = hr.svc.db["mail_outbox"][mid]
    assert rec["status"] == "send_failed"
    brk = [b for b in hr.svc.db["breaks"].values() if b["leg"] == "client_mail_failed"]
    assert len(brk) == 1 and brk[0]["severity"] == "high"
    assert any("ALERT finance: client_mail_failed" in r.getMessage() for r in caplog.records)
    assert CONTACT not in caplog.text
    # Andre fixes the cause and requeues it
    assert hr.post(f"/fin/v1/client-mail/{mid}/requeue", {"request_id": rid()}, caller="ops_cron").status_code == 403
    hr.ok(hr.post(f"/fin/v1/client-mail/{mid}/requeue", {"request_id": rid()}, andre=ANDRE_TOKEN))
    assert run_mail(hr)["sent"] == 1 and hr.svc.db["mail_outbox"][mid]["status"] == "sent"
    assert hr.svc.db["breaks"][brk[0]["break_id"]]["status"] == "resolved"


def test_a_permanent_refusal_fails_at_once():
    hr = harness()
    set_contact(hr)
    paid(hr)
    hr.f["client_mail"].outcomes = [MailOutcome("permanent", code="403:validation_error")]
    assert run_mail(hr)["failed_for_good"] == 1
    assert [b for b in hr.svc.db["breaks"].values() if b["leg"] == "client_mail_failed"]


def test_an_adapter_that_raises_never_touches_the_books():
    class Exploding:
        requires_contact = True

        def send_receipt(self, client_id, receipt):
            raise RuntimeError("boom")

        def deliver(self, m):
            raise RuntimeError("boom")
    hr = harness(Exploding())
    set_contact(hr)
    inv = paid(hr)
    tb = hr.ok(hr.get("/fin/v1/journal/zbm/trial-balance"))
    s = run_mail(hr)
    assert s["sent"] == 0 and s["waiting"] == 1
    assert hr.ok(hr.get("/fin/v1/journal/zbm/trial-balance")) == tb
    assert hr.svc.db["invoices"][inv["invoice_id"]]["status"] == "paid"


def test_with_mail_off_the_job_touches_nothing():
    hr = harness(NotBuiltClientMail())
    paid(hr)
    n = len(hr.ledger.events)
    s = run_mail(hr)
    assert s["mail_off"] is True and s["waiting"] == 1
    assert not [e for e in hr.ledger.events[n:] if "client_receipt" in e["event_type"]
                or "client_mail" in e["event_type"] or e["event_type"].startswith("crossing_")]


def test_billing_contact_is_andre_only_and_validated():
    hr = harness()
    body = {"request_id": rid(), "email": CONTACT}
    assert hr.client.put(f"/fin/v1/clients/{CLIENT}/billing-contact", json=body,
                         headers=hr.headers(caller="onboarding")).status_code in (401, 403)
    for bad in ("not-an-email", "a@b", "x@y.z\nbcc@evil.example", "a,b@c.example"):
        r = hr.put(f"/fin/v1/clients/{CLIENT}/billing-contact", {"request_id": rid(), "email": bad})
        assert r.status_code == 422, bad
