"""Client offboarding and data deletion, Finance's side (ADR 0027 decision 3; MSA 15.5; red team OPS-H8).

- Redaction by design: the billing contact (wave B) lives only in a deletable side store; the hash-chained log and the
  ledger never hold the address, the name or a plain hash of either (verified line by line).
- ``POST /fin/v1/clients/{id}/offboarding`` (Andre only): a dry run lists what Finance holds for the client and what
  happens to each kind (delete / withdraw / keep 7 years); the real run names the dry run's ``plan_sha256``, deletes the
  contact, withdraws unsent client email, keeps the money records, and writes a hash-only deletion certificate to the
  ledger (with the other services' certificate hashes)."""

from __future__ import annotations

import hashlib
import json
import os

import mail as MAIL
from helpers import ANDRE_TOKEN, NOW, Harness, make_fakes, rid
from test_client_mail import paid, run_mail
from test_credit_notes import CLIENT, ach_invoice

EMAIL = "ap@client-seven.example"
NAME = "Accounts Payable Desk"
OFFB = f"/fin/v1/clients/{CLIENT}/offboarding"


def harness(data_dir=None) -> Harness:
    from clock import FixedClock
    clock = FixedClock(NOW)
    f = make_fakes(clock)
    f["client_mail"] = MAIL.FixtureClientMail()
    return Harness(clock=clock, fakes=f, data_dir=data_dir).ready()


def set_contact(hr, client=CLIENT, email=EMAIL, name=NAME):
    return hr.ok(hr.put(f"/fin/v1/clients/{client}/billing-contact",
                        {"request_id": rid(), "email": email, "name": name}))


def forms(*values):
    out = set()
    for v in values:
        out |= {v, v.lower(), hashlib.sha256(v.encode()).hexdigest(), hashlib.sha256(v.lower().encode()).hexdigest()}
    return out


def assert_no_pii(hr, data_dir=None):
    ledger = json.dumps(hr.ledger.events)              # the test double keeps payloads; the real ledger gets hashes
    for f in forms(EMAIL, NAME):
        assert f not in ledger, f
    if data_dir:
        for name in os.listdir(data_dir):
            p = os.path.join(data_dir, name)
            if os.path.isfile(p) and name.endswith(".jsonl"):
                raw = open(p, encoding="utf-8").read()
                for f in forms(EMAIL, NAME):
                    assert f not in raw, (name, f)


def dry(hr, client=CLIENT):
    return hr.ok(hr.post(f"/fin/v1/clients/{client}/offboarding", {"request_id": rid(), "dry_run": True},
                         andre=ANDRE_TOKEN))


def run(hr, plan, client=CLIENT, **over):
    return hr.post(f"/fin/v1/clients/{client}/offboarding",
                   {"request_id": rid(), "dry_run": False, "plan_sha256": plan, **over}, andre=ANDRE_TOKEN)


# --------------------------------------------------------------------------------------------- redaction by design

def test_the_billing_contact_never_reaches_the_log_or_the_ledger(tmp_path):
    hr = harness(str(tmp_path))
    set_contact(hr)
    paid(hr)
    hr.ok(hr.post(f"/fin/v1/clients/{CLIENT}/acknowledgements", {"request_id": rid(), "topic": "refund_request",
                                                                 "received_on": "2026-10-01"}, andre=ANDRE_TOKEN), 201)
    assert run_mail(hr)["sent"] == 2
    assert {m["to"] for m in hr.f["client_mail"].delivered} == {EMAIL}
    assert_no_pii(hr, str(tmp_path))
    vault = [n for n in os.listdir(tmp_path / "pii")]
    assert len(vault) == 1 and oct(os.stat(tmp_path / "pii" / vault[0]).st_mode & 0o777) == "0o600"
    assert CLIENT not in vault[0]                                   # the file name is a hash too


def test_the_contact_survives_a_restart(tmp_path):
    hr = harness(str(tmp_path))
    set_contact(hr)
    hr2 = Harness(clock=hr.clock, fakes=hr.f, data_dir=str(tmp_path), ledger=hr.ledger)
    assert hr2.svc._contact(CLIENT) == EMAIL


# --------------------------------------------------------------------------------------------- the dry run

def test_the_dry_run_lists_everything_and_changes_nothing(tmp_path):
    hr = harness(str(tmp_path))
    set_contact(hr)
    paid(hr)
    ach_invoice(hr, "300.00")                                     # issued, unpaid
    log_before = (tmp_path / "fin_log.jsonl").read_bytes()
    events = len(hr.ledger.events)
    p = dry(hr)
    assert (tmp_path / "fin_log.jsonl").read_bytes() == log_before and len(hr.ledger.events) == events
    held = {h["kind"]: h for h in p["held"]}
    assert held["billing_contact"]["count"] == 1 and held["billing_contact"]["action"] == "delete"
    assert held["invoices"]["count"] == 2 and held["invoices"]["action"] == "keep_7_years"
    assert held["journal_entries"]["count"] >= 2 and held["journal_entries"]["action"] == "keep_7_years"
    assert held["unsent_client_email"]["action"] == "withdraw"
    assert p["blockers"] == [] and any("unpaid" in w for w in p["warnings"])
    assert len(p["plan_sha256"]) == 64 and p["dry_run"] is True
    assert EMAIL not in json.dumps(p) and NAME not in json.dumps(p)


def test_a_compliance_hold_or_an_open_dispute_blocks_the_real_run():
    hr = harness()
    set_contact(hr)
    inv = paid(hr)
    hr.svc.db["invoices"][inv["invoice_id"]] = {**hr.svc.db["invoices"][inv["invoice_id"]],
                                                "held_compliance": {"kind": "refused", "sanctions": True}}
    p = dry(hr)
    assert p["blockers"] and "Compliance" in p["blockers"][0]
    r = run(hr, p["plan_sha256"])
    assert r.status_code == 409 and "blocked" in r.text


def test_an_open_stripe_dispute_on_the_clients_payment_blocks():
    from service import rid as frid
    hr = harness()
    set_contact(hr)
    inv = paid(hr)
    rct = frid("rct", "stripe", "pi_test_offb")
    hr.svc.db["receipts"][rct] = {"receipt_id": rct, "invoice_id": inv["invoice_id"], "status": "matched"}
    hr.svc.db["stripe_disputes"]["dp_test_offb"] = {"dispute_id": "dp_test_offb", "payment_intent": "pi_test_offb",
                                                    "finalized": False}
    p = dry(hr)
    assert any("dispute" in b for b in p["blockers"])
    hr.svc.db["stripe_disputes"]["dp_test_offb"]["finalized"] = True
    assert dry(hr)["blockers"] == []


# --------------------------------------------------------------------------------------------- the real run

def test_the_real_run_deletes_the_contact_keeps_the_books_and_certifies(tmp_path):
    hr = harness(str(tmp_path))
    set_contact(hr)
    paid(hr)
    tb = hr.ok(hr.get("/fin/v1/journal/zbm/trial-balance"))
    invoices = json.dumps({k: v for k, v in hr.svc.db["invoices"].items()}, sort_keys=True)
    p = dry(hr)
    out = hr.ok(run(hr, p["plan_sha256"], peer_certificates=[{"service": "compliance", "certificate_sha256": "a" * 64}]))
    cert = out["certificate"]
    assert out["certificate_sha256"] == hashlib.sha256(
        json.dumps(cert, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert cert["deleted"]["billing_contact"] == 1 and cert["withdrawn"]["unsent_client_email"] >= 1
    assert cert["kept"]["invoices"]["retention"] == "7 years after the end of the tax year (MSA 15.5(a))"
    assert cert["peer_certificates"] == [{"service": "compliance", "certificate_sha256": "a" * 64}]
    assert cert["client_id_sha256"] == hashlib.sha256(CLIENT.encode()).hexdigest() and CLIENT not in json.dumps(cert)
    assert cert["pii_left_in_log"] == 0
    assert os.listdir(tmp_path / "pii") == []
    assert hr.svc._contact(CLIENT) is None
    assert hr.ok(hr.get("/fin/v1/journal/zbm/trial-balance")) == tb
    assert json.dumps({k: v for k, v in hr.svc.db["invoices"].items()}, sort_keys=True) == invoices
    (ev,) = hr.ledger.of_type("client_offboarded")
    assert ev["payload"]["certificate_sha256"] == out["certificate_sha256"]
    assert_no_pii(hr, str(tmp_path))
    assert not [m for m in hr.svc.db["mail_outbox"].values() if m["client_id"] == CLIENT
                and m["status"] in ("pending", "send_failed")]
    # a restart keeps it deleted
    hr2 = Harness(clock=hr.clock, fakes=hr.f, data_dir=str(tmp_path), ledger=hr.ledger)
    assert hr2.svc._contact(CLIENT) is None


def test_the_real_run_needs_the_dry_runs_plan_and_refuses_a_stale_one():
    hr = harness()
    set_contact(hr)
    r = hr.post(OFFB, {"request_id": rid(), "dry_run": False}, andre=ANDRE_TOKEN)
    assert r.status_code == 422
    p = dry(hr)
    paid(hr)                                                      # the state moved after the dry run
    r = run(hr, p["plan_sha256"])
    assert r.status_code == 409 and "plan" in r.text


def test_a_retry_of_the_same_request_answers_the_same_and_deletes_once():
    hr = harness()
    set_contact(hr)
    p = dry(hr)
    body = {"request_id": rid(), "dry_run": False, "plan_sha256": p["plan_sha256"]}
    a = hr.ok(hr.post(OFFB, body, andre=ANDRE_TOKEN))
    b = hr.ok(hr.post(OFFB, body, andre=ANDRE_TOKEN))
    assert a == b and len(hr.ledger.of_type("client_offboarded")) == 1


def test_only_andre_offboards():
    hr = harness()
    for caller in ("onboarding", "ops_cron", "ops_alerts", "scheduler"):
        assert hr.post(OFFB, {"request_id": rid(), "dry_run": True}, caller=caller).status_code == 403, caller


def test_a_client_finance_never_knew_has_an_empty_plan():
    hr = harness()
    p = dry(hr, client="zbm-client-unknown")
    assert all(h["count"] == 0 for h in p["held"]) and p["blockers"] == []
