"""AEGIS r13 (finance-py): R13-1, R13-2 (finance part), R13-4, R13-5, R13-6.

- R13-1: the founder alert's id is random (never derivable from the client id, not even across a re-opening), its
  record has a fixed set of fields, a non-Andre break row of a Compliance leg is built from an ALLOW-LIST of fields, and
  every non-Andre caller's break list is the same for a client sanctioned from the start and a plain refusal.
- R13-2: each money-held condition alerts on its own (a 2050 balance, an open refund, held unapplied money as the
  only link to a client), and the rail funding execution re-reads Finance's own holds under the lock.
- R13-4: the GET-write invariant also follows an aliased service object and ``getattr(svc, "...")``.
- R13-5: no non-Andre row carries ``checked_at`` (a field only founder alerts had).
- R13-6: an allow -> refuse flip re-opens the client's same alert record instead of creating a new one.
"""

from __future__ import annotations

import json
import re

import pytest

from helpers import ANDRE_TOKEN, CALLERS, Harness, rid
from service import rid as fin_rid


def _match(hr, client="client-1"):
    hr.cmp.activation_available = True
    hr.cmp.activations = {client: {"allowed": False, "jurisdiction": "US-CA", "reasons": ["hold open: sanctions_match"],
                                   "sanctions": True, "ground": "match"}}


def _plain(hr, client="client-1"):
    hr.cmp.activation_available = True
    hr.cmp.activations = {client: {"allowed": False, "jurisdiction": "US-CA", "reasons": ["outside"],
                                   "sanctions": False}}


def _allow(hr):
    hr.cmp.activation_available = True
    hr.cmp.activations = {}


def _job(hr):
    return hr.ok(hr.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="scheduler"))["summary"]


def _alerts(hr, status="open"):
    return [b for b in hr.svc.db["breaks"].values() if b.get("kind") == "founder_alert"
            and (status is None or b["status"] == status)]


# --- R13-1 ------------------------------------------------------------------------------------------------------------------

def test_r13_1_the_alert_id_is_random_never_derived_from_the_client():
    ids = []
    for _ in range(2):                                       # the same client in two worlds: two unrelated ids
        hr = Harness().ready()
        hr.fund_campaign()
        _match(hr)
        _job(hr)
        ids.append(_alerts(hr)[0]["break_id"])
    assert ids[0] != ids[1], "the alert id is derived from the client"
    derivable = {fin_rid("brk", "founder_alert", *parts) for parts in
                 (("client-1",), ("funded_client", "client-1"), ("client", "client-1"), ("client:client-1",))}
    assert not set(ids) & derivable


ALERT_FIELDS = {"break_id", "leg", "kind", "category", "severity", "subject", "difference", "opened_at", "opened_on",
                "owner", "explanation_code", "status", "resolution", "note", "checked_at"}


def test_r13_1_the_founder_alert_record_has_exactly_the_reviewed_fields():
    """A new field on the alert (a ``ground``, say) must be reviewed for what it tells non-Andre readers."""
    hr = Harness().ready()
    hr.fund_campaign()
    _match(hr)
    _job(hr)
    assert set(_alerts(hr)[0]) == ALERT_FIELDS, sorted(set(_alerts(hr)[0]) ^ ALERT_FIELDS)


ROW_ALLOW = {"break_id", "leg", "subject", "difference", "opened_at", "opened_on", "owner", "explanation_code",
             "status", "receipt_id", "note", "resolution", "aging", "clears_by"}


def test_r13_1_non_andre_compliance_rows_are_built_from_an_allow_list():
    hr = Harness().ready()
    hr.fund_campaign()
    inv2 = hr.deposit_invoice(amount="500.00")
    _match(hr)
    hr.receive("zbc", "1020", "501.00", inv2["invoice_id"])       # a held receipt (its breaks)
    _job(hr)
    with hr.svc.lock:                                              # planted extra fields on every Compliance break
        for k, b in list(hr.svc.db["breaks"].items()):
            if b.get("leg") in ("compliance_hold", "compliance_unverified"):
                hr.svc.db["breaks"][k] = {**b, "ground": "match", "sanctions": True, "x_anything": 1}
    for caller in sorted(CALLERS):
        r = hr.get("/fin/v1/breaks", caller=caller)
        if r.status_code != 200:
            continue
        for row in r.json()["breaks"]:
            if row["leg"] == "compliance_hold":
                assert set(row) <= ROW_ALLOW, (caller, sorted(set(row) - ROW_ALLOW))
                assert "checked_at" not in row                     # R13-5


_ID = re.compile(r"(fin-[a-z0-9]+-)[0-9A-Za-z]+|[0-9a-f]{40,64}|r-\d+")


def _break_views(setup):
    hr = Harness().ready()
    hr.fund_campaign()
    inv2 = hr.deposit_invoice(amount="500.00")
    setup(hr)
    hr.receive("zbc", "1020", "501.00", inv2["invoice_id"])
    _job(hr)
    _job(hr)
    return {c: _ID.sub(lambda m: (m.group(1) or "") + "X", hr.get("/fin/v1/breaks", caller=c).text)
            for c in sorted(CALLERS)}


def test_r13_1_every_non_andre_break_list_is_the_same_for_a_sanctioned_from_start_and_a_plain_refusal():
    s, n = _break_views(_match), _break_views(_plain)
    assert s == n, [c for c in s if s[c] != n[c]]


# --- R13-2 ------------------------------------------------------------------------------------------------------------------

def test_m1_a_2050_balance_alone_alerts():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.recon()
    rf = hr.ok(hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler"), 201)["refund"]
    real = hr.svc._execute_transfer

    def m_then(tid):
        _match(hr)
        return real(tid)
    hr.svc._execute_transfer = m_then
    hr.ok(hr.post(f"/fin/v1/refunds/{rf['refund_id']}/decision",
                  {"request_id": rid(), "content_sha256": rf["content_sha256"], "decision": "approve"},
                  andre=ANDRE_TOKEN))
    hr.svc._execute_transfer = real
    with hr.svc.lock:                                              # only the 2050 balance links the client now
        hr.svc.db["refunds"][rf["refund_id"]] = {**hr.svc.db["refunds"][rf["refund_id"]], "status": "closed_test"}
    assert hr.bal("2050", "client:client-1") > 0 and hr.bal("2010", "campaign:camp-1") == 0
    _job(hr)
    assert len(_alerts(hr)) == 1


def test_m4_an_open_refund_alone_alerts():
    hr = Harness().ready()
    with hr.svc.lock:                                              # an open refund is the client's only link
        hr.svc.db["refunds"]["rf-only"] = {"refund_id": "rf-only", "client_id": "client-7", "campaign_id": "camp-7",
                                            "status": "proposed", "amount": "10.00"}
    _match(hr, "client-7")
    _job(hr)
    assert len(_alerts(hr)) == 1 and _alerts(hr)[0]["subject"] == "client:client-7"


def test_m6_held_unapplied_money_alone_links_a_client_without_a_campaign(tmp_path):
    from test_compliance_bank_rtm import bank_line, refuse
    from test_compliance_gate_adr0024 import _stripe_hr
    from test_stripe_incoming import rr_invoice
    hr = _stripe_hr(tmp_path)
    inv = rr_invoice(hr, "700.00", methods=("ach", "wire"))        # zbm-client-7: no campaign, no profile
    refuse(hr)
    r = hr.ok(bank_line(hr, "zbm", "1010", "701.00", inv["invoice_id"]))
    assert r["results"][0]["status"] == "unapplied_held_compliance"
    hr.ok(hr.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="scheduler"))
    al = _alerts(hr)
    assert len(al) == 1 and al[0]["subject"] == "client:zbm-client-7", al


def test_x2_a_hold_booked_before_the_funding_executes_stops_it():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.payee()
    hr.accrue()
    hr.recon()
    b = hr.run()["batch"]
    hr.approve(b)
    inv2 = hr.deposit_invoice(amount="500.00")
    op = hr.ok(hr.post("/fin/v1/treasury/funding", {"request_id": rid(), "batch_id": b["batch_id"]},
                       caller="scheduler"), 201)["operation"]
    real = hr.svc._transfer_client_reasons
    state = {}

    def read_then_hold(op_id, body, request_id, full):
        out = real(op_id, body, request_id, full)
        if body is None and not state:                             # the execution's read said ALLOW ...
            state["x"] = 1
            _match(hr)                                             # ... then the client's wire is booked and held
            state["wire"] = hr.receive("zbc", "1020", "500.00", inv2["invoice_id"])["results"][0]["status"]
            _allow(hr)
        return out
    hr.svc._transfer_client_reasons = read_then_hold
    r = hr.ok(hr.post(f"/fin/v1/treasury/funding/{op['op_id']}/decision",
                      {"request_id": rid(), "content_sha256": op["content_sha256"], "decision": "approve"},
                      andre=ANDRE_TOKEN))
    hr.svc._transfer_client_reasons = real
    assert state["wire"] == "matched_held_compliance"
    assert r["transfer"]["status"] == "refused_at_execution", r
    assert "F4a" not in [e["memo_code"] for e in hr.svc.entries]


# --- R13-6 ------------------------------------------------------------------------------------------------------------------

def test_r13_6_an_allow_refuse_flip_reopens_the_same_alert_record():
    hr = Harness().ready()
    hr.fund_campaign()
    _match(hr)
    _job(hr)
    first = _alerts(hr)[0]["break_id"]
    for _ in range(3):
        _allow(hr)
        _job(hr)
        assert not _alerts(hr)
        _plain(hr)
        _job(hr)
    al = _alerts(hr, status=None)
    assert [a["break_id"] for a in al] == [first] and al[0]["status"] == "open"


# --- R13-4 ------------------------------------------------------------------------------------------------------------------

def test_r13_4_the_get_write_scan_follows_aliases_and_getattr():
    import ast
    import textwrap

    from test_money_out_invariant_r10 import _handler_calls
    src = textwrap.dedent('''
        def h(batch_id):
            s = svc
            t = s
            a = getattr(svc, "release_batch")
            b = getattr(t, "decide_batch")
            return s.list_batches(None), t.get_batch(batch_id), a(), b()
    ''')
    assert _handler_calls(ast.parse(src)) >= {"release_batch", "decide_batch", "list_batches", "get_batch"}
    assert json.dumps(sorted(_handler_calls(ast.parse("def g():\n    return getattr(svc, name)()\n")))) == '["*"]'
