"""AEGIS r10 R10-1 (job part), R10-2, R10-5, R10-6: the hourly hold job.

- R10-6: the job re-checks bank-path holds even when Stripe incoming is off (it used to return zeros).
- R10-1: it also asks Compliance about every client with a funded campaign and pushes Andre (only Andre) when one is
  no longer allowed, with the OFAC line on a sanctions ground. Accruals and payouts are refused meanwhile anyway.
- R10-2: what a non-Andre reader sees is the same for a plain refused hold (N), a refused hold the job upgrades to a
  sanctions match (U) and a hold that was a sanctions match from the start (S): the job summary, the number and shape
  of the exported log lines, the (generic) audit evidence and /integrity. Every refused hold the job re-reads writes
  one hold record, one break record and one hold event, whether or not its ground changed, and match holds are
  re-read like the others.
- R10-5: releasing a held deposit the bank already returned does not mark the campaign funded with nothing in it.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from helpers import ANDRE_TOKEN, Harness, rid


def _match(hr, client="client-1"):
    hr.cmp.activation_available = True
    hr.cmp.activations = {client: {"allowed": False, "jurisdiction": "US-CA", "reasons": ["hold open: sanctions_match"],
                                   "sanctions": True, "ground": "match"}}


def _refuse_plain(hr, client="client-1"):
    hr.cmp.activation_available = True
    hr.cmp.activations = {client: {"allowed": False, "jurisdiction": "US-CA", "reasons": ["outside the service area"],
                                   "sanctions": False}}


def _allow(hr):
    hr.cmp.activation_available = True
    hr.cmp.activations = {}


def _job(hr):
    return hr.ok(hr.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="scheduler"))["summary"]


def _deposit(hr):
    hr.profile(doc_id=hr.rate_card())
    return hr.deposit_invoice()


def _hold(hr, inv):
    return hr.svc.db["invoices"][inv["invoice_id"]].get("held_compliance")


# --- R10-6 ---------------------------------------------------------------------------------------------------------------

def test_r10_6_an_unverified_bank_hold_is_released_by_the_job_with_stripe_incoming_off():
    hr = Harness().ready()
    assert not hr.svc.cfg.stripe_incoming
    inv = _deposit(hr)
    hr.cmp.activation_available = False
    r = hr.receive("zbc", "1020", "1000.00", inv["invoice_id"])
    assert r["results"][0]["status"] == "matched_held_unverified", r
    _allow(hr)
    s = _job(hr)
    assert s["holds_released"] == 1, s
    assert _hold(hr, inv) is None and hr.svc.db["profiles"]["camp-1"]["status"] == "funded"


def test_r10_6_a_refused_bank_hold_is_upgraded_to_a_match_with_stripe_incoming_off():
    hr = Harness().ready()
    inv = _deposit(hr)
    _refuse_plain(hr)
    hr.receive("zbc", "1020", "1000.00", inv["invoice_id"])
    assert _hold(hr, inv)["kind"] == "refused" and not _hold(hr, inv)["sanctions"]
    _match(hr)
    _job(hr)
    assert _hold(hr, inv)["sanctions_ground"] == "match"


# --- R10-1: the job re-checks clients with funded campaigns and tells Andre ---------------------------------------------

def test_r10_1_the_job_pushes_andre_when_a_funded_brand_becomes_a_match():
    hr = Harness().ready()
    hr.fund_campaign()
    _match(hr)
    s = _job(hr)
    assert ("client_activation", "client-1") in hr.cmp.calls
    pushed = [b for k, b in hr.f["push"].pushed if k == "compliance_funded_client_refused"]
    assert len(pushed) == 1 and "OFAC" in json.dumps(pushed[0]), hr.f["push"].pushed
    assert "OFAC" not in json.dumps(s) and "sanction" not in json.dumps(s).lower()
    _job(hr)                                                     # once per state, not every hour
    assert len([1 for k, _ in hr.f["push"].pushed if k == "compliance_funded_client_refused"]) == 1


def test_r10_1_a_plain_refusal_of_a_funded_brand_is_pushed_without_the_ofac_line():
    hr = Harness().ready()
    hr.fund_campaign()
    _refuse_plain(hr)
    _job(hr)
    pushed = [b for k, b in hr.f["push"].pushed if k == "compliance_funded_client_refused"]
    assert len(pushed) == 1 and "OFAC" not in json.dumps(pushed[0])


# --- R10-2: N vs U vs S look the same to every non-Andre reader ------------------------------------------------------------

_ID = re.compile(r"(fin-[a-z0-9]+-)[0-9A-Za-z]+|[0-9a-f]{40,64}|r-\d+|rule-\d+")


def _norm(x):
    return _ID.sub(lambda m: (m.group(1) or "") + "X", json.dumps(x, sort_keys=True))


def _visible(hr, summary):
    lines = []
    cur = 0
    while cur is not None:
        page = hr.ok(hr.get("/fin/v1/audit/export", caller="onboarding", cursor=cur))
        for e in page["records"]:
            lines.append((e["kind"], tuple((r["kind"], r["record"].get("coll") if isinstance(r["record"], dict)
                                            else None,
                                            tuple(sorted((r["record"].get("rec") or {}) if isinstance(r["record"], dict)
                                                         and isinstance(r["record"].get("rec"), dict) else ())))
                                           for r in e["records"]), len(e["ledger_event_ids"])))
        cur = page["next_cursor"]
    ev = hr.ok(hr.get("/fin/v1/audit/evidence", caller="onboarding", limit=1000))
    events = Counter((e["event_type"], e["status"]) for e in ev["events"])
    integ = hr.ok(hr.get("/fin/v1/integrity", caller="onboarding"))
    breaks = _norm(hr.ok(hr.get("/fin/v1/breaks", caller="onboarding")))
    inv = _norm(hr.ok(hr.get(f"/fin/v1/invoices/{next(iter(hr.svc.db['invoices']))}", caller="onboarding")))
    return {"summary": summary, "lines": lines, "events": events, "log_lines": integ.get("log_lines"),
            "breaks": breaks, "invoice": inv}


def _world(kind):
    hr = Harness().ready()
    inv = _deposit(hr)
    (_match if kind == "S" else _refuse_plain)(hr)
    hr.receive("zbc", "1020", "1000.00", inv["invoice_id"])
    if kind == "U":
        _match(hr)
    s = _job(hr)
    s2 = _job(hr)                                                # and a second hour
    assert _hold(hr, inv)["sanctions_ground"] == (None if kind == "N" else "match")
    return _visible(hr, [s, s2])


def test_r10_2_a_sanctions_upgrade_and_a_match_from_the_start_look_like_a_plain_refusal():
    n, u, s = _world("N"), _world("U"), _world("S")
    for k in n:
        assert n[k] == u[k], ("N vs U differ", k, n[k], u[k])
        assert n[k] == s[k], ("N vs S differ", k, n[k], s[k])


def test_r10_2_andre_still_sees_the_upgrade():
    hr = Harness().ready()
    inv = _deposit(hr)
    _refuse_plain(hr)
    hr.receive("zbc", "1020", "1000.00", inv["invoice_id"])
    _match(hr)
    _job(hr)
    ev = hr.ok(hr.get("/fin/v1/audit/evidence", andre=ANDRE_TOKEN, limit=1000))
    assert "compliance_hold_sanctions" in {e["event_type"] for e in ev["events"]}


# --- R10-5 ---------------------------------------------------------------------------------------------------------------

def test_r10_5_a_returned_held_deposit_does_not_fund_an_empty_campaign():
    hr = Harness().ready()
    inv = _deposit(hr)
    hr.cmp.activation_available = False
    hr.receive("zbc", "1020", "1000.00", inv["invoice_id"])          # held unverified
    rct = next(r for r in hr.svc.db["receipts"].values() if r.get("invoice_id") == inv["invoice_id"])
    hr.ok(hr.post(f"/fin/v1/receipts/{rct['receipt_id']}/return",
                  {"request_id": rid(), "return_ref_sha256": "c" * 64, "return_code": "R01", "value_date": "2026-10-03"},
                  caller="bank_feed"))
    assert hr.bal("2010", "campaign:camp-1") == 0
    _allow(hr)
    _job(hr)                                                         # the hold is released on ALLOW
    assert hr.svc.db["profiles"]["camp-1"]["status"] != "funded"
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/compliance-hold/release",
                {"request_id": rid(), "basis": "compliance_allow"}, andre=ANDRE_TOKEN)
    assert hr.svc.db["profiles"]["camp-1"]["status"] != "funded", r.text
