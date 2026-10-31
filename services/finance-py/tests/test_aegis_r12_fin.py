"""AEGIS r12 (finance-py): R12-1, R12-2, R12-3, R12-5. Adapted from AEGIS's probes (scratchpad/r12/probes: P9, P10,
P14, P16) and its surviving mutants (j2, j3, j5, r1-r4, fa2-fa4, t3-t6).

- R12-1: nothing a non-Andre caller reads differs between a plain refusal and a sanctions upgrade of a client whose
  money ZBC holds: ``/fin/v1/integrity`` carries no counters for them, every founder alert is re-written each hourly run
  whatever its ground (the same log lines), and a founder alert in the non-Andre break list names no client.
- R12-2: a founder alert covers every client whose money ZBC holds (an unspent deposit, a proposed or refused refund,
  a 2010 or 2050 balance, held unapplied money), derived from balances and holds, not from the campaign's status.
- R12-3: a refund payment (and a vendor payment) re-reads Finance's own holds of the client under the lock.
- R12-5: the surviving guards are pinned.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest

from helpers import ANDRE_TOKEN, CALLERS, Harness, rid


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


def _health(hr):
    return hr.client.get("/health").json()["compliance_holds_open"]


# --- R12-1 ------------------------------------------------------------------------------------------------------------------

def test_r12_1_integrity_carries_no_counter_for_a_non_andre_caller():
    hr = Harness().ready()
    for caller in sorted(CALLERS):
        r = hr.get("/fin/v1/integrity", caller=caller)
        if r.status_code == 200:
            assert set(r.json()) <= {"status", "checked_at"}, (caller, r.json())
    full = hr.ok(hr.get("/fin/v1/integrity", caller=None, andre=ANDRE_TOKEN))
    assert "log_lines" in full and "journal_entries" in full


_ID = re.compile(r"(fin-[a-z0-9]+-)[0-9A-Za-z]+|[0-9a-f]{40,64}|r-\d+")


def _per_run(funded: bool, unapplied: bool, invoice_hold: bool, upgrade: bool):
    """AEGIS P16: log-line deltas per hourly run (Andre's count) and every non-Andre read, upgrade before run 3."""
    hr = Harness().ready()
    if funded:
        hr.fund_campaign()
    else:
        hr.profile(doc_id=hr.rate_card())
    inv2 = hr.deposit_invoice(amount="500.00")
    _plain(hr)
    if unapplied:
        hr.receive("zbc", "1020", "501.00", inv2["invoice_id"])
    if invoice_hold:
        hr.receive("zbc", "1020", "500.00", inv2["invoice_id"])
    _job(hr)
    _job(hr)
    deltas, reads = [], []
    for k in range(4):
        if k == 2 and upgrade:
            _match(hr)
        a = len(hr.svc.log)
        _job(hr)
        deltas.append(len(hr.svc.log) - a)
        seen = {}
        for caller in sorted(CALLERS):
            for path in ("/fin/v1/integrity", "/fin/v1/breaks", "/health"):
                r = hr.get(path, caller=caller)
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
                seen[(caller, path)] = (r.status_code, _ID.sub(lambda m: (m.group(1) or "") + "X",
                                                              json.dumps(body, sort_keys=True)))
        reads.append(seen)
    return deltas, reads


@pytest.mark.parametrize("unapplied,invoice_hold", [(False, False), (True, False), (False, True)])
def test_r12_1_p16_a_sanctions_upgrade_writes_and_shows_the_same_as_a_plain_refusal(unapplied, invoice_hold):
    n_d, n_r = _per_run(True, unapplied, invoice_hold, upgrade=False)
    u_d, u_r = _per_run(True, unapplied, invoice_hold, upgrade=True)
    assert n_d == u_d, ("log-line deltas differ", n_d, u_d)
    for k, (a, b) in enumerate(zip(n_r, u_r)):
        diff = [cell for cell in a if a[cell] != b[cell]]
        assert not diff, (k, diff[:5])


def test_r12_1_a_founder_alert_names_no_client_outside_andres_token():
    hr = Harness().ready()
    hr.fund_campaign()
    _match(hr)
    _job(hr)
    al = _alerts(hr)[0]
    assert al["subject"] == "client:client-1"                      # Andre's record
    assert "client-1" not in al["break_id"]
    for caller in ("onboarding", "scheduler", "seo_02", "creative_production"):
        txt = hr.get("/fin/v1/breaks", caller=caller).text
        assert "client-1" not in txt and "founder_alert" not in txt, (caller, txt[:300])
    mine = hr.ok(hr.get("/fin/v1/breaks", caller=None, andre=ANDRE_TOKEN))["breaks"]
    assert any(b.get("subject") == "client:client-1" and "OFAC" in (b.get("note") or "") for b in mine)


# --- R12-2 ------------------------------------------------------------------------------------------------------------------

def test_r12_2_p10a_a_proposed_refund_then_a_match_alerts_andre():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.recon()
    hr.ok(hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler"), 201)
    assert hr.svc.db["profiles"]["camp-1"]["status"] != "funded"
    _match(hr)
    _job(hr)
    assert len(_alerts(hr)) == 1 and _health(hr) >= 1


def test_r12_2_p10b_a_refused_refund_and_its_cancellation_keep_the_alert_until_allowed():
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
    assert hr.bal("2050", "client:client-1") == Decimal("1000.00")
    _job(hr)
    assert len(_alerts(hr)) == 1                                    # 2050 holds the client's money
    hr.ok(hr.post(f"/fin/v1/refunds/{rf['refund_id']}/cancel", {"request_id": rid(), "reason": "stuck"},
                  andre=ANDRE_TOKEN))
    _job(hr)
    assert len(_alerts(hr)) == 1                                    # 2010 holds it again
    _allow(hr)
    _job(hr)
    assert not _alerts(hr) and _health(hr) == 0


def test_r12_2_an_alert_stays_open_after_the_deposit_returned_while_unapplied_money_is_still_held():
    hr = Harness().ready()
    inv1 = hr.fund_campaign()
    _match(hr)
    hr.receive("zbc", "1020", "1001.00", inv1["invoice_id"])        # held unapplied money of the same client
    _job(hr)
    assert len(_alerts(hr)) == 1
    rct = next(r for r in hr.svc.db["receipts"].values() if r.get("invoice_id") == inv1["invoice_id"])
    hr.ok(hr.post(f"/fin/v1/receipts/{rct['receipt_id']}/return",
                  {"request_id": rid(), "return_ref_sha256": "c" * 64, "return_code": "R01", "value_date": "2026-10-03"},
                  caller="bank_feed"))
    assert hr.bal("2010", "campaign:camp-1") == 0
    _job(hr)
    assert len(_alerts(hr)) == 1                                    # still money of a matched client (held receipt)


def test_r12_2_an_alert_closes_when_zbc_holds_none_of_the_clients_money():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.recon()
    _job(hr)
    _match(hr)
    _job(hr)
    assert len(_alerts(hr)) == 1
    _allow(hr)
    rf = hr.ok(hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler"), 201)["refund"]
    hr.ok(hr.post(f"/fin/v1/refunds/{rf['refund_id']}/decision",
                  {"request_id": rid(), "content_sha256": rf["content_sha256"], "decision": "approve"},
                  andre=ANDRE_TOKEN))                                # paid: nothing held any more
    _match(hr)
    _job(hr)
    assert not _alerts(hr), _alerts(hr)


# --- R12-3 ------------------------------------------------------------------------------------------------------------------

def test_r12_3_p14_a_hold_booked_during_the_refund_payments_read_stops_the_payment():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.recon()
    inv2 = hr.deposit_invoice(amount="500.00")
    rf = hr.ok(hr.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler"), 201)["refund"]
    real = hr.svc._transfer_client_reasons
    state = {}

    def gather_then_hold(op_id, body, request_id, full):
        out = real(op_id, body, request_id, full)
        if body is None and not state:
            state["x"] = 1
            _match(hr)
            state["wire"] = hr.receive("zbc", "1020", "500.00", inv2["invoice_id"])["results"][0]["status"]
        return out
    hr.svc._transfer_client_reasons = gather_then_hold
    hr.ok(hr.post(f"/fin/v1/refunds/{rf['refund_id']}/decision",
                  {"request_id": rid(), "content_sha256": rf["content_sha256"], "decision": "approve"},
                  andre=ANDRE_TOKEN))
    assert state["wire"] == "matched_held_compliance"
    t = next(x for x in hr.svc.db["treasury_ops"].values() if x["kind"] == "refund_payment")
    assert t["status"] == "refused_at_execution", t
    assert "F6p" not in [e["memo_code"] for e in hr.svc.entries]


# --- R12-5: the surviving guards ----------------------------------------------------------------------------------------------

def _two(hr):
    hr.profile(doc_id=hr.rate_card())
    inv1 = hr.deposit_invoice()
    hr.profile(campaign="camp-9", client="client-9", doc_id=hr.rate_card(campaign="camp-9"))
    inv9 = hr.deposit_invoice(campaign="camp-9", client="client-9", amount="999.00")
    return inv1, inv9


@pytest.mark.parametrize("setup", [_match, _plain])
def test_j2_a_refused_or_sanctions_receipt_hold_is_never_released_by_the_job_on_allow(setup):
    hr = Harness().ready()
    _, inv9 = _two(hr)
    setup(hr, "client-9")
    rct = hr.receive("zbc", "1020", "1000.00", inv9["invoice_id"])["results"][0]["receipt_id"]
    _allow(hr)
    _job(hr)
    assert hr.svc.db["receipts"][rct].get("held_compliance"), "released by the job on an ALLOW"


def test_j3_the_job_reads_compliance_again_right_before_releasing_a_receipt_hold():
    hr = Harness().ready()
    _, inv9 = _two(hr)
    hr.cmp.activation_available = False
    rct = hr.receive("zbc", "1020", "1000.00", inv9["invoice_id"])["results"][0]["receipt_id"]
    _allow(hr)
    fc = hr.cmp
    real = fc.client_activation
    seen = {"n": 0}

    def flip(cid):
        if cid == "client-9":
            seen["n"] += 1
            if seen["n"] >= 2:                                        # the second read (just before the release)
                _match(hr, "client-9")
        return real(cid)
    fc.client_activation = flip
    _job(hr)
    fc.client_activation = real
    assert hr.svc.db["receipts"][rct].get("held_compliance"), "released on a stale ALLOW"


def test_j5_an_unreachable_compliance_changes_no_receipt_hold():
    hr = Harness().ready()
    _, inv9 = _two(hr)
    hr.cmp.activation_available = False
    rct = hr.receive("zbc", "1020", "1000.00", inv9["invoice_id"])["results"][0]["receipt_id"]
    before = json.dumps(hr.svc.db["receipts"][rct], sort_keys=True)
    _job(hr)
    assert json.dumps(hr.svc.db["receipts"][rct], sort_keys=True) == before


def test_r1_r2_a_receipt_release_refuses_a_hold_that_became_sanctions():
    hr = Harness().ready()
    _, inv9 = _two(hr)
    _plain(hr, "client-9")
    rct = hr.receive("zbc", "1020", "1000.00", inv9["invoice_id"])["results"][0]["receipt_id"]
    _match(hr, "client-9")                                           # now a match: neither compliance_allow nor refund
    for basis in ("compliance_allow", "refund"):
        r = hr.post(f"/fin/v1/receipts/{rct}/compliance-hold/release",
                    {"request_id": rid(), "basis": basis, "entry_id": hr.svc.entries[-1]["entry_id"]},
                    andre=ANDRE_TOKEN)
        assert r.status_code == 409 and "SANCTIONS" in r.text, (basis, r.text)
    assert hr.svc.db["receipts"][rct]["held_compliance"]["sanctions_ground"] == "match"


def test_r1_the_ground_is_re_checked_under_the_lock():
    hr = Harness().ready()
    _, inv9 = _two(hr)
    _plain(hr, "client-9")
    rct = hr.receive("zbc", "1020", "1000.00", inv9["invoice_id"])["results"][0]["receipt_id"]
    _allow(hr)
    real = hr.svc._activation

    def act_then_upgrade(g, cid):
        a = real(g, cid)
        with hr.svc.lock:                                            # the hold became sanctions after the read
            rc = hr.svc.db["receipts"][rct]
            hr.svc.db["receipts"][rct] = {**rc, "held_compliance": {**rc["held_compliance"], "sanctions": True,
                                                                     "sanctions_ground": "match"}}
        return a
    hr.svc._activation = act_then_upgrade
    r = hr.post(f"/fin/v1/receipts/{rct}/compliance-hold/release", {"request_id": rid(), "basis": "compliance_allow"},
                andre=ANDRE_TOKEN)
    hr.svc._activation = real
    assert r.status_code == 409 and hr.svc.db["receipts"][rct].get("held_compliance"), r.text


def _stripe_held_unapplied(tmp_path, sanctions=False):
    from test_compliance_bank_rtm import refuse
    from test_compliance_gate_adr0024 import _stripe_hr
    from test_money_redteam_rtm import _paid
    from test_stripe_incoming import sent
    hr = _stripe_hr(tmp_path)
    inv, cs, pi = _paid(hr, method="card")
    refuse(hr, sanctions=sanctions, ground="match" if sanctions else None)
    pi2 = hr.sim.pay(cs["session_id"], method="card", force=True)
    out = sent(hr, "payment_intent.succeeded", {"id": pi2, "object": "payment_intent"})
    assert out["status"] == "unapplied_held_compliance"
    rc = next(r for r in hr.svc.db["receipts"].values() if (r.get("stripe") or {}).get("payment_intent") == pi2)
    return hr, pi, pi2, rc["receipt_id"]


def test_r3_r4_a_refund_release_needs_this_receipts_own_refund_covering_the_hold(tmp_path):
    from test_money_redteam_rtm import charge_obj
    from test_stripe_incoming import sent
    hr, pi, pi2, rct = _stripe_held_unapplied(tmp_path)
    hr.f["compliance"].activations = {}                             # not sanctions; the client allowed again
    hr.sim.refund(pi, 10000)                                        # a refund of ANOTHER receipt
    sent(hr, "charge.refunded", charge_obj(hr, pi))
    other = [e for e in hr.svc.entries if e["memo_code"] == "F7r"][-1]
    r = hr.post(f"/fin/v1/receipts/{rct}/compliance-hold/release",
                {"request_id": rid(), "basis": "refund", "entry_id": other["entry_id"]}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "of this money" in r.text, r.text
    hr.sim.refund(pi2, 10000)                                       # a partial refund of this receipt
    sent(hr, "charge.refunded", charge_obj(hr, pi2))
    mine = [e for e in hr.svc.entries if e["memo_code"] == "F7r" and (e.get("source") or {}).get("id") == rct][-1]
    r = hr.post(f"/fin/v1/receipts/{rct}/compliance-hold/release",
                {"request_id": rid(), "basis": "refund", "entry_id": mine["entry_id"]}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "do not cover" in r.text, r.text
    assert hr.svc.db["receipts"][rct].get("held_compliance")


def test_fa2_an_outage_never_rewrites_an_open_alerts_ofac_note():
    hr = Harness().ready()
    hr.fund_campaign()
    _match(hr)
    _job(hr)
    note = _alerts(hr)[0]["note"]
    assert "OFAC" in note
    hr.cmp.activation_available = False
    _job(hr)
    al = _alerts(hr)
    assert len(al) == 1 and al[0]["note"] == note


def test_fa3_the_alert_note_follows_the_ground_for_andre():
    hr = Harness().ready()
    hr.fund_campaign()
    _plain(hr)
    _job(hr)
    assert "OFAC" not in _alerts(hr)[0]["note"]
    _match(hr)
    _job(hr)
    assert "OFAC" in _alerts(hr)[0]["note"]


def test_fa4_an_open_alert_is_re_read_and_closed_once_nothing_is_held(tmp_path):
    hr = Harness().ready()
    hr.fund_campaign()
    _match(hr)
    _job(hr)
    assert len(_alerts(hr)) == 1
    with hr.svc.lock:                                                # the client's money is no longer held anywhere
        for k in list(hr.svc.db["profiles"]):
            hr.svc.db["profiles"][k] = {**hr.svc.db["profiles"][k], "client_id": "client-other"}
    hr.svc._client_holds_money = lambda cid: False                   # noqa: E731 - only the alert's own re-read remains
    _allow(hr)
    _job(hr)
    assert not _alerts(hr)


def _held_receipt_of_client_1(hr):
    inv2 = hr.deposit_invoice(amount="500.00")
    _plain(hr)
    hr.receive("zbc", "1020", "501.00", inv2["invoice_id"])          # held unapplied money of client-1
    _allow(hr)                                                       # Compliance allows again; the hold stays
    return inv2


def test_t3_held_unapplied_money_of_the_brand_stops_an_accrual():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.payee()
    _held_receipt_of_client_1(hr)
    r = hr.ok(hr.handoff())
    assert r["allowed"] is False and "COMPLIANCE_NOT_ACTIVATED" in r["reason"], r


def test_t4_the_accrual_re_reads_the_brands_holds_under_the_lock():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.payee()
    _held_receipt_of_client_1(hr)
    real = hr.svc._campaign_outflow
    hr.svc._campaign_outflow = lambda g, c, full: {x: [] for x in c}   # the read saw nothing: only the lock's re-read
    r = hr.ok(hr.handoff())
    hr.svc._campaign_outflow = real
    assert r["allowed"] is False, r


def test_t5_held_unapplied_money_of_the_brand_excludes_it_from_a_run():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.payee()
    hr.accrue()
    _held_receipt_of_client_1(hr)
    real = hr.svc._campaign_outflow
    hr.svc._campaign_outflow = lambda g, c, full: {x: [] for x in c}   # the read saw nothing: only the lock's re-read
    hr.recon()
    r = hr.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, caller="scheduler")
    hr.svc._campaign_outflow = real
    body = r.json()                                                  # blocked (the unapplied cash break) or empty
    assert body.get("batch") is None, body
    assert any("COMPLIANCE_NOT_ACTIVATED" in [x["code"] for x in e["reasons"]] for e in body["excluded"]), body


def test_t6_held_unapplied_money_of_the_brand_refuses_andres_approval():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.payee()
    hr.accrue()
    hr.recon()
    b = hr.run()["batch"]
    _held_receipt_of_client_1(hr)
    real = hr.svc._campaigns_reasons
    hr.svc._campaigns_reasons = lambda g, c, full: []               # the read saw nothing: only the lock's re-read
    r = hr.post(f"/fin/v1/payout-batches/{b['batch_id']}/decision",
                {"request_id": rid(), "content_sha256": b["content_sha256"], "decision": "approve"},
                andre=ANDRE_TOKEN)
    hr.svc._campaigns_reasons = real
    assert r.status_code == 409, r.text


def _stuck_refund(hr):
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
    tid = next(t for t in hr.svc.db["treasury_ops"].values() if t["kind"] == "refund_payment")["op_id"]
    return rf, tid


@pytest.mark.parametrize("change", [{"outcome_unknown": True}, {"status": "bank_unknown"}, {"status": "executing"},
                                    {"status": "done"}])
def test_c1_c2_a_refund_is_never_cancelled_once_its_payment_may_have_moved(change):
    hr = Harness().ready()
    rf, tid = _stuck_refund(hr)
    with hr.svc.lock:
        hr.svc.db["treasury_ops"][tid] = {**hr.svc.db["treasury_ops"][tid], **change}
    r = hr.post(f"/fin/v1/refunds/{rf['refund_id']}/cancel", {"request_id": rid(), "reason": "x"}, andre=ANDRE_TOKEN)
    assert r.status_code == 409, (change, r.text)
    assert hr.svc.db["refunds"][rf["refund_id"]]["status"] == "approved"


def test_r12_3_a_vendor_payment_re_reads_the_clients_held_unapplied_money(tmp_path):
    from test_compliance_bank_rtm import bank_line, refuse
    from test_compliance_gate_adr0024 import _stripe_hr
    from test_media_billing import create, issue, pay_vendor
    hr = _stripe_hr(tmp_path)
    m = create(hr)
    inv = issue(hr, m["invoice"])
    hr.ok(bank_line(hr, "zbm", "1010", inv["total"], inv["invoice_id"]))        # paid while ALLOW: not held
    hr.clock.advance(days=4)
    refuse(hr, client="zbm-client-1", sanctions=False, ground=None)
    r = hr.ok(bank_line(hr, "zbm", "1010", "1.00", inv["invoice_id"]))         # more money of the client: held
    assert r["results"][0]["status"] == "unapplied_held_compliance"
    hr.cmp.activations = {}                                                     # Compliance allows again; held stays
    r = pay_vendor(hr, m["media_buy"]["buy_id"], "10000.00", paid_on="2026-10-06", tag="held-rcpt")
    assert r.status_code == 409 and "held by Compliance" in r.text, r.text
