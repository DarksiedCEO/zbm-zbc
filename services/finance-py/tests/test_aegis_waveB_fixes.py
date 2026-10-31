"""AEGIS wave B review, F4 and the Lows (F6, F7, F8, F9), failing first.

F4  the refund deadline counts from the day the refund became due (MSA 4.10), not the credit note's date; the client
    email shows the same date.
F6  a backup pause survives a restart: a restarted Finance stays paused until Andre resumes the marker.
F7  void after a full Stripe refund is refused (a credit note instead); the ops_cron middleware is tested on its own;
    stripe_webhooks.py names a test that exists.
F8  the billing contact's hash on the ledger is an HMAC with a per-deployment key (FIN_CONTACT_HASH_KEY), never a
    bare SHA-256; no key: no contact is recorded, and mail cannot be switched on.
F9  the 2-business-day acknowledgement (MSA 4.12(f)) and the 10-business-day cancel-or-credit (MSA 4.12(g)) each
    get a deadline and a break, and an overdue one turns high and logs an ALERT.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from pathlib import Path

import pytest

import config as config_mod
import mail as MAIL
from helpers import ANDRE_TOKEN, Harness, base_env, rid
from test_client_mail import CONTACT, harness, run_mail, set_contact
from test_credit_notes import CLIENT, ach_invoice, credit, dashboard_refund, stripe_paid, void
from test_restore_reimport import durable, restart
from test_stripe_incoming import keys, make  # noqa: F401  (keys: a fixture)

HASH_KEY = "ab" * 32


@pytest.fixture
def st(keys):  # noqa: F811
    return make(keys, FIN_CARD_PREPAYMENTS="1")


# --------------------------------------------------------------------------------------------- F4

def test_f4_the_refund_is_due_10_business_days_after_it_became_due_not_after_the_credit_note(st):
    inv, _pi = stripe_paid(st, "1200.00")
    st.clock.advance(days=6)                                    # Thursday Oct 8: Andre credits a SOW that ended Oct 1
    out = st.ok(credit(st, st.svc.db["invoices"][inv["invoice_id"]], "400.00", reason="sow_ended_prepaid",
                       refund_due_from="2026-10-01"), 201)
    cn = out["credit_note"]
    assert cn["refund_due_from"] == "2026-10-01" and cn["refund_due_by"] == "2026-10-15"    # not Oct 22
    brk = st.svc.db["breaks"][cn["break_id"]]
    assert brk["due_by"] == "2026-10-15"
    (m,) = [m for m in st.svc.db["mail_outbox"].values() if m["kind"] == "credit_note"]
    assert m["data"]["refund_due_by"] == "2026-10-15"
    assert "2026-10-15" in MAIL.render("credit_note", m["data"])[1]


def test_f4_a_refund_owing_credit_note_must_say_when_the_refund_became_due(st):
    inv, _pi = stripe_paid(st, "1200.00")
    i = st.svc.db["invoices"][inv["invoice_id"]]
    r = credit(st, i, "100.00", refund_due_from=None)
    assert r.status_code == 409 and "refund_due_from" in r.text
    assert credit(st, i, "100.00", refund_due_from="2026-10-03").status_code == 422      # the future
    st.clock.advance(days=20)
    late = st.ok(credit(st, i, "100.00", refund_due_from="2026-10-01"), 201)["credit_note"]
    assert late["refund_due_by"] == "2026-10-15" and st.svc.db["breaks"][late["break_id"]]["severity"] == "high"
    assert st.svc.db["breaks"][late["break_id"]]["late"] is True


def test_f4_an_unpaid_reduction_needs_no_refund_date():
    hr = Harness().ready()
    inv = ach_invoice(hr)
    assert hr.ok(credit(hr, inv, "100.00", refund_due_from=None), 201)["credit_note"]["refund_due_by"] is None


# --------------------------------------------------------------------------------------------- F6

def test_f6_a_restarted_finance_stays_paused_until_the_marker_is_resumed(keys, tmp_path):  # noqa: F811
    x = durable(keys, tmp_path)
    q = x.ok(x.post("/fin/v1/backup/quiesce", {"request_id": rid(), "minutes": 5}, andre=ANDRE_TOKEN))
    y = restart(x, tmp_path)
    y.clock.advance(minutes=30)                                  # even past the window: fail closed until resumed
    r = y.post("/fin/v1/invoices", {"request_id": rid()}, caller="onboarding")
    assert r.status_code == 503 and "QUIESCED" in r.text
    out = y.ok(y.post("/fin/v1/backup/resume", {"request_id": rid(), "marker_id": q["marker"]["marker_id"]},
                      andre=ANDRE_TOKEN))
    assert out["consistent"] is False                            # a restart happened inside the window
    assert y.post("/fin/v1/invoices", {"request_id": rid()}, caller="onboarding").status_code == 422


# --------------------------------------------------------------------------------------------- F7

def test_f7_void_after_a_full_stripe_refund_is_refused_and_a_credit_note_settles_it(st):
    inv, pi = stripe_paid(st, "1200.00")
    dashboard_refund(st, pi)
    i = st.svc.db["invoices"][inv["invoice_id"]]
    assert i["status"] == "issued" and i["refunded"] == "1200.00"
    r = void(st, i)
    assert r.status_code == 409 and "credit note" in r.text
    out = st.ok(credit(st, i, "1200.00", reason="cancelled_before_work"), 201)
    assert out["invoice"]["status"] == "credited"
    assert not [b for b in st.svc.db["breaks"].values() if b["leg"] == "stripe_refund" and b["status"] == "open"]


def test_f7_the_ops_cron_middleware_alone_refuses_every_other_route():
    hr = Harness().ready()
    hr.app.add_api_route("/fin/v1/zz-probe", lambda: {"ok": True}, methods=["GET", "POST"])     # no auth at all
    assert hr.client.get("/fin/v1/zz-probe", headers=hr.headers("onboarding")).status_code == 200
    assert hr.client.get("/fin/v1/zz-probe", headers=hr.headers("ops_cron")).status_code == 403
    assert hr.client.post("/fin/v1/zz-probe", headers=hr.headers("ops_cron")).status_code == 403
    assert hr.client.get("/health", headers=hr.headers("ops_cron")).status_code == 200


def test_f7_the_webhook_list_cites_a_test_that_exists():
    src = (Path(__file__).resolve().parents[1] / "src" / "stripe_webhooks.py").read_text()
    for name in re.findall(r"tests/(test_[a-z0-9_]+\.py)", src):
        assert (Path(__file__).resolve().parent / name).exists(), name
    assert re.findall(r"tests/(test_[a-z0-9_]+\.py)", src)


# --------------------------------------------------------------------------------------------- F8

def _key_file(tmp_path, name, value):
    p = tmp_path / name
    p.write_text(value + "\n")
    os.chmod(p, 0o600)
    return str(p)


def test_f8_the_contact_hash_on_the_ledger_is_an_hmac_with_the_deployment_key():
    hr = harness()
    set_contact(hr)
    (ev,) = hr.ledger.of_type("client_contact_recorded")
    want = hmac.new(bytes.fromhex(HASH_KEY), CONTACT.encode(), hashlib.sha256).hexdigest()
    assert ev["payload"]["email_hmac_sha256"] == want
    assert hashlib.sha256(CONTACT.encode()).hexdigest() not in json.dumps(hr.ledger.events)
    assert CONTACT not in json.dumps(hr.ledger.events)


def test_f8_no_key_no_contact_and_mail_cannot_be_on_without_it(tmp_path):
    hr = Harness(env={"FIN_CONTACT_HASH_KEY_FILE": "__unset__"}).ready()
    r = hr.put(f"/fin/v1/clients/{CLIENT}/billing-contact", {"request_id": rid(), "email": CONTACT})
    assert r.status_code == 409 and "FIN_CONTACT_HASH_KEY" in r.text
    env = base_env(RESEND_API_KEY_FILE=_key_file(tmp_path, "r", "re_live_FinanceSimulatedResendKey0000"),
                   MAIL_FROM="billing@zbestmedia.com", FIN_CONTACT_HASH_KEY_FILE="__unset__")
    with pytest.raises(RuntimeError, match="FIN_CONTACT_HASH_KEY"):
        config_mod.load(env)
    with pytest.raises(RuntimeError, match="FIN_CONTACT_HASH_KEY_FILE"):
        config_mod.load(base_env(FIN_CONTACT_HASH_KEY_FILE=_key_file(tmp_path, "k", "not-hex-and-too-short")))


# --------------------------------------------------------------------------------------------- F9

def test_f9_an_acknowledgement_has_a_deadline_closed_when_it_is_sent(caplog):
    hr = harness(MAIL.FixtureClientMail())
    set_contact(hr)
    out = hr.ok(hr.post(f"/fin/v1/clients/{CLIENT}/acknowledgements",
                        {"request_id": rid(), "topic": "refund_request", "received_on": "2026-10-02"},
                        andre=ANDRE_TOKEN), 201)
    brk = hr.svc.db["breaks"][out["break_id"]]
    assert brk["leg"] == "acknowledgement_due" and brk["due_by"] == "2026-10-06" and brk["status"] == "open"
    run_mail(hr)
    assert hr.svc.db["breaks"][out["break_id"]]["status"] == "resolved"


def test_f9_an_unsent_acknowledgement_turns_overdue_and_alerts(caplog):
    hr = harness(MAIL.FixtureClientMail())                     # no billing contact: nothing can be sent
    out = hr.ok(hr.post(f"/fin/v1/clients/{CLIENT}/acknowledgements",
                        {"request_id": rid(), "topic": "cancellation", "received_on": "2026-10-02"},
                        andre=ANDRE_TOKEN), 201)
    hr.clock.advance(days=5)                                     # Wednesday Oct 7: past Tuesday Oct 6
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        run_mail(hr)
    b = hr.svc.db["breaks"][out["break_id"]]
    assert b["status"] == "open" and b["severity"] == "high" and b["overdue"] is True
    assert any("ALERT finance: deadline_overdue" in r.getMessage() for r in caplog.records)


def test_f9_sow_end_opens_a_cancel_or_credit_deadline_that_a_void_or_credit_note_closes(caplog):
    hr = Harness().ready()
    inv = ach_invoice(hr)
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/sow-ended", {"request_id": rid(), "sow_ended_on": "2026-10-01"},
                andre=ANDRE_TOKEN)
    out = hr.ok(r, 201)
    b = hr.svc.db["breaks"][out["break_id"]]
    assert b["leg"] == "cancel_or_credit_due" and b["due_by"] == "2026-10-15"
    assert hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/sow-ended", {"request_id": rid(), "sow_ended_on": "2026-10-01"},
                   caller="onboarding").status_code in (401, 403)
    hr.ok(credit(hr, inv, "300.00"), 201)
    assert hr.svc.db["breaks"][out["break_id"]]["status"] == "resolved"
    # one that runs late alerts
    inv2 = ach_invoice(hr, "500.00")
    out2 = hr.ok(hr.post(f"/fin/v1/invoices/{inv2['invoice_id']}/sow-ended",
                         {"request_id": rid(), "sow_ended_on": "2026-10-01"}, andre=ANDRE_TOKEN), 201)
    hr.clock.advance(days=14)
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        hr.ok(hr.post("/fin/v1/jobs/client-mail/run", {"request_id": rid()}, caller="ops_cron"))
    assert hr.svc.db["breaks"][out2["break_id"]]["severity"] == "high"
    assert any("deadline_overdue" in r.getMessage() for r in caplog.records)
