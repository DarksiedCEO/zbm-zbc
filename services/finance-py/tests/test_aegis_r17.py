"""AEGIS round 17 findings on finance-py (N17-1, -2, -3, -9, -10, -11, -12, -13). Each test failed on
integration-2026-09-24 @ 680c289 (evidence in the fix-18 report) and passes after the fix."""

from __future__ import annotations

import hashlib
import json
import random
from decimal import Decimal

import httpx
import pytest

import money as M
from helpers import ANDRE_TOKEN, SECOND_TOKEN, Harness, rid
from ports import (BankBalance, BankTransfer, Certification, ClawbackPage, ComplianceRuling, HoldsAnswer, JurisdictionAnswer,
                   RailAccount, RailBalance, RailSubmit, RegisterRow, SanctionsAnswer, TaxAgentAnswer)


def _codes(r):
    return sorted({x["code"] for x in r.json().get("reasons", [])})


def _restart(x, data_dir):
    return Harness(data_dir=data_dir, ledger=x.ledger, clock=x.clock, fakes=x.f, env=None)


def _same_cert(x, sid_old, sid_new, clipper="clip-a", campaign="camp-1"):
    """V&I answers for ``sid_new`` with the certification id it gave ``sid_old`` (a replayed / colliding id)."""
    c = x.vi.certify(sid_new, clipper, campaign, 100000, "2026-09-21T12:00:00Z")
    c["certification_id"] = x.vi.certs[sid_old]["certification_id"]
    x.cmp.rule(f"cmp-rul-{sid_new}", sid_new)
    return x.handoff(sid_new, clipper=clipper, certify=False, rule=False)


# ================================================================== N17-1 payable identity

def test_n17_1_settled_payable_is_never_rebound_to_another_submission(hr):
    hr.fund_campaign()
    hr.payee("clip-a")
    p1 = hr.accrue("sub-1", views=100000)
    b, _ = hr.full_payout()
    hr.pay_items(hr.batch(b["batch_id"]))
    assert hr.ok(hr.get(f"/fin/v1/payables/{p1['payable_id']}"))["status"] == "settled"
    r = _same_cert(hr, "sub-1", "sub-2")
    assert r.status_code == 200 and r.json()["allowed"] is False
    assert "PAYABLE_IDENTITY_CONFLICT" in _codes(r)
    after = hr.ok(hr.get(f"/fin/v1/payables/{p1['payable_id']}"))
    assert (after["submission_id"], after["status"]) == ("sub-1", "settled")       # never rewritten, never accrued
    assert hr.ledger.of_type("payable_identity_conflict")
    assert any(f["kind"] == "certification_rebound" for f in hr.ok(hr.get("/fin/v1/integrity"))["findings"])
    hr.clock.advance(days=7)
    rec = hr.recon()
    assert rec["fc01"], [l for l in rec["recon"]["legs"] if l["status"] not in ("matched", "not_in_use")]
    run = hr.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, caller="scheduler")
    assert not (run.json().get("batch") or {}).get("items")                       # nothing paid twice


def test_n17_1_replayed_certification_cannot_redirect_a_payout_to_another_clipper(hr):
    hr.fund_campaign()
    hr.payee("clip-a")
    hr.payee("clip-b")
    p1 = hr.accrue("sub-1", clipper="clip-a", views=100000)
    r = _same_cert(hr, "sub-1", "sub-2", clipper="clip-b")
    assert r.json()["allowed"] is False and "PAYABLE_IDENTITY_CONFLICT" in _codes(r)
    p = hr.ok(hr.get(f"/fin/v1/payables/{p1['payable_id']}"))
    assert (p["payee_id"], p["submission_id"], p["status"]) == ("clip-a", "sub-1", "accrued")
    assert hr.bal("2020", "payee:clip-a") == Decimal("235.00") and hr.bal("2020", "payee:clip-b") == 0
    hr.recon()
    items = hr.run()["batch"]["items"]
    assert [(i["payee_id"], i["net"]) for i in items] == [("clip-a", "235.00")]


def test_n17_1_a_code_path_cannot_move_a_payable_backwards_or_rebind_it(hr):
    from service import IntegrityRefused, Op
    hr.fund_campaign()
    hr.payee("clip-a")
    p = hr.accrue("sub-1", views=100000)
    p = {**hr.svc.db["payables"][p["payable_id"]], "status": "settled"}
    hr.svc.db["payables"][p["payable_id"]] = p
    op = Op(hr.svc, "t", "t", "t")
    with pytest.raises(IntegrityRefused):
        op.put("payables", p["payable_id"], {**p, "status": "accrued"})
    with pytest.raises(IntegrityRefused):
        op.put("payables", p["payable_id"], {**p, "payee_id": "clip-b"})
    with pytest.raises(IntegrityRefused):
        op.put("payables", p["payable_id"], {**p, "certification_id": ["x"]})
    op.put("payables", p["payable_id"], {**p, "status": "returned"})           # paid, sent back: owed again


def test_n17_1_reconciliation_and_integrity_compare_per_payable_not_totals(hr):
    hr.fund_campaign()
    hr.payee("clip-a")
    hr.payee("clip-b")
    p = hr.accrue("sub-1", views=100000)
    hr.recon()
    # a record/journal disagreement that nets to zero in the totals: the payable now names clip-b
    hr.svc.db["payables"][p["payable_id"]] = {**hr.svc.db["payables"][p["payable_id"]], "payee_id": "clip-b"}
    rec = hr.recon()["recon"]
    bad = {l["subject"] for l in rec["legs"] if l["status"] not in ("matched", "not_in_use")}
    assert "zbc:2020" not in bad                                                    # the total still ties ...
    assert {"zbc:2020:payee:clip-a", "zbc:2020:payee:clip-b"} <= bad              # ... the sub-ledgers do not
    integ = hr.ok(hr.get("/fin/v1/integrity"))
    assert integ["status"] == "red" and any("accrual entry does not post 2020" in x for x in integ["problems"])


def test_n17_1_replay_of_an_old_rebinding_line_is_total_and_turns_integrity_red(tmp_path):
    from service import Op
    d = str(tmp_path / "d")
    x = Harness(data_dir=d).ready()
    x.fund_campaign()
    x.payee("clip-a")
    p = x.accrue("sub-1", views=100000)
    op = Op(x.svc, "legacy", "t", "t")          # what the pre-fix code wrote: the same payable, another submission
    op.ops.append(("put", {"coll": "payables", "id": p["payable_id"],
                           "rec": {**x.svc.db["payables"][p["payable_id"]], "submission_id": "sub-2"}}))
    x.svc._commit(op)
    y = _restart(x, d)
    integ = y.ok(y.get("/fin/v1/integrity"))
    assert integ["status"] == "red" and any("rebound" in pr for pr in integ["problems"])


# ================================================================== N17-2 sweeps

def _sweep_setup():
    h = Harness().ready()
    h.fund_campaign(budget="1000.00")
    h.payee("clip-a")
    h.accrue("sub-1", views=250000)          # revenue 1000.00, creator 587.50 -> sweepable 412.50
    h.recon()
    return h


def _propose(h, amount):
    return h.post("/fin/v1/treasury/sweeps", {"request_id": rid(), "amount": amount}, caller="scheduler")


def _decide(h, op_):
    return h.post(f"/fin/v1/treasury/sweeps/{op_['op_id']}/decision",
                  {"request_id": rid(), "content_sha256": op_["content_sha256"], "decision": "approve"}, andre=ANDRE_TOKEN)


def test_n17_2_sweepable_subtracts_sweeps_already_proposed_or_approved():
    h = _sweep_setup()
    a = h.ok(_propose(h, "400.00"), 201)["operation"]
    r = _propose(h, "400.00")
    assert r.status_code == 409 and "TREASURY_BREACH" in _codes(r)
    h.bank.transfer_available = False
    assert h.ok(_decide(h, a))["operation"]["status"] == "approved"             # bank down: not executed
    assert h.ok(h.get("/fin/v1/treasury"))["sweepable"] == "12.50"
    assert _propose(h, "400.00").status_code == 409
    h.bank.transfer_available = True
    h.clock.advance(days=1)
    h.ok(h.post("/fin/v1/jobs/rail-sync/run", {"request_id": rid()}, caller="scheduler"))
    assert [t[3] for t in h.bank.transfers] == ["400.00"]
    assert h.bank.balances[("zbc", "1020")] == Decimal("600.00") and h.bal("1020") == Decimal("600.00")


def test_n17_2_posting_is_committed_and_anchored_before_the_bank_is_asked():
    h = _sweep_setup()
    a = h.ok(_propose(h, "100.00"), 201)["operation"]
    seen = {}
    real = h.bank.transfer

    def spy(entity, src, dst, amount, key):
        t = h.svc.db["treasury_ops"][key]
        seen["status"] = t["status"]
        seen["entry"] = h.svc.entries_by_id.get(t.get("entry_id") or "")
        seen["anchored"] = any(e["event_type"] == "treasury_posting_committed" for e in h.ledger.events)
        seen["log_has_entry"] = any(k == "journal" and r["entry_id"] == t.get("entry_id")
                                    for rec in h.svc.log.iter_records() for k, r in rec["data"]["ops"])
        return real(entity, src, dst, amount, key)
    h.bank.transfer = spy
    assert h.ok(_decide(h, a))["operation"]["status"] == "done"
    assert seen["status"] == "executing" and seen["entry"]["memo_code"] == "F8"
    assert seen["anchored"] and seen["log_has_entry"]


def test_n17_2_bank_refusal_reverses_the_posting_by_a_recorded_reversal():
    h = _sweep_setup()
    a = h.ok(_propose(h, "100.00"), 201)["operation"]
    h.bank.transfer = lambda *a_, **k: BankTransfer("refused")
    t = h.ok(_decide(h, a))
    assert t["operation"]["status"] == "approved" and t["transfer"]["transfer"] == "refused"
    f8 = [e for e in h.svc.entries if e["memo_code"] == "F8"]
    assert len(f8) == 2 and f8[1]["reverses_entry_id"] == f8[0]["entry_id"]
    assert h.bal("1020") == Decimal("1000.00") and h.bal("1010") == 0
    assert h.ledger.of_type("treasury_posting_reversed")


def test_n17_2_unknown_bank_outcome_keeps_the_posting_opens_a_break_and_retries_with_the_same_key():
    h = _sweep_setup()
    a = h.ok(_propose(h, "100.00"), 201)["operation"]
    real = h.bank.transfer

    def boom(*a_, **k):
        raise RuntimeError("timeout after send")
    h.bank.transfer = boom
    t = h.ok(_decide(h, a))
    assert t["operation"]["status"] == "bank_unknown"
    assert any(b.get("kind") == "bank_state_unknown" for b in h.svc.db["breaks"].values())
    h.bank.transfer = real
    h.clock.advance(days=1)
    h.ok(h.post("/fin/v1/jobs/rail-sync/run", {"request_id": rid()}, caller="scheduler"))
    assert h.svc.db["treasury_ops"][a["op_id"]]["status"] == "done"
    assert len([e for e in h.svc.entries if e["memo_code"] == "F8"]) == 1            # never posted twice
    assert h.bank.transfers[0][4] == a["op_id"]


def test_n17_2_restricted_pool_invariant_is_rechecked_at_execution():
    h = _sweep_setup()
    a = h.ok(_propose(h, "400.00"), 201)["operation"]
    h.bank.transfer_available = False
    h.ok(_decide(h, a))
    rcpt = [r for r in h.svc.db["receipts"].values() if r["status"] == "matched"][0]
    h.bank.deposit("zbc", "1020", "-1000.00")
    h.ok(h.post(f"/fin/v1/receipts/{rcpt['receipt_id']}/return", {"request_id": rid(), "return_ref_sha256": "c" * 64,
                                                                    "return_code": "R01", "value_date": "2026-10-02"},
                caller="bank_feed"))
    h.bank.transfer_available = True
    h.clock.advance(days=1)
    h.ok(h.post("/fin/v1/jobs/rail-sync/run", {"request_id": rid()}, caller="scheduler"))
    t = h.svc.db["treasury_ops"][a["op_id"]]
    assert t["status"] == "refused_at_execution" and "TREASURY_BREACH" in {x["code"] for x in t["reasons"]}
    assert h.bank.transfers == [] and h.bal("1010") == 0
    f8 = [e for e in h.svc.entries if e["memo_code"] == "F8"]
    assert len(f8) == 2 and f8[1]["reverses_entry_id"] == f8[0]["entry_id"]      # only the bank-down attempt, reversed


# ================================================================== N17-3 adapter answers

def _vi_http(overrides):
    from clients import HttpVerification

    def handler(req):
        sid = req.url.path.split("/")[4]
        base = {"submission_id": sid, "certification_id": "vi-cert-" + sid, "status": "certified",
                "certified_views": 100000, "campaign_id": "camp-1", "clipper_id": "clip-a", "platform": "tiktok",
                "window": {"create_time": "2026-09-20T12:00:00Z"}, "certified_at": "2026-09-20T12:00:00Z",
                "reasons": [], "rules_version": 1}
        base.update(overrides)
        return httpx.Response(200, json=base)
    return HttpVerification("http://vi.invalid", "t" * 40, "c" * 40, transport=httpx.MockTransport(handler))


MALFORMED_CERTS = {
    "cert_id_list": {"certification_id": ["evil", 1]}, "cert_id_dict": {"certification_id": {"a": 1}},
    "cert_id_int": {"certification_id": 7}, "views_float": {"certified_views": 100000.0},
    "views_str": {"certified_views": "100000"}, "views_bool": {"certified_views": True},
    "views_negative": {"certified_views": -5}, "views_huge": {"certified_views": 10 ** 13},
    "campaign_list": {"campaign_id": ["camp-1"]}, "clipper_int": {"clipper_id": 7},
    "create_time_int": {"window": {"create_time": 12345}}, "create_time_garbage": {"window": {"create_time": "soon"}},
    "status_list": {"status": ["certified"]}, "status_int": {"status": 1}, "rules_version_str": {"rules_version": "1"},
    "reasons_not_list": {"reasons": "x"}, "window_list": {"window": ["x"]}, "submission_list": {"submission_id": ["s"]},
    "certified_at_int": {"certified_at": 5}, "platform_dict": {"platform": {"x": 1}},
}


@pytest.mark.parametrize("shape", sorted(MALFORMED_CERTS))
def test_n17_3_every_malformed_certification_is_refused_recorded_and_replay_is_total(tmp_path, shape):
    d = str(tmp_path / "d")
    x = Harness(data_dir=d).ready()
    x.fund_campaign()
    x.payee("clip-a")
    x.svc.ports.vi = _vi_http(MALFORMED_CERTS[shape])
    x.cmp.rule("cmp-rul-sub-x", "sub-x")
    r = x.handoff("sub-x", certify=False, rule=False)
    assert r.status_code == 200 and r.json()["allowed"] is False, r.text
    assert not x.svc.db["payables"]
    y = _restart(x, d)                                   # the log replays: nothing malformed was anchored
    assert y.ok(y.get("/fin/v1/integrity"))["status"] == "green"
    assert not y.svc.db["payables"]


def test_n17_3_malformed_answers_from_every_port_fail_closed_and_are_recorded(tmp_path):
    d = str(tmp_path / "d")
    x = Harness(data_dir=d).ready()
    x.fund_campaign()
    x.payee("clip-a")
    x.accrue("sub-1", views=100000)
    x.recon()
    f = x.f
    f["vi"].certification = lambda sid: Certification(True, sid, ["x"], "certified", 1.5, "camp-1", "clip-a")
    f["compliance"].ruling = lambda rid_: ComplianceRuling(True, rid_, "payout", "sub-1", "yes", "2026-10-02T00:00:00Z")
    f["compliance"].holds = lambda s: HoldsAnswer(True, [1, 2])
    f["compliance"].sanctions_status = lambda s, r: SanctionsAnswer(True, "scr", "clear", "v", 1, 7, "fresh")
    f["compliance"].row = lambda o: RegisterRow(True, o, ["verified"])
    f["compliance"].jurisdiction = lambda c, r: JurisdictionAnswer(True, 3)
    f["tax"].status = lambda pid: TaxAgentAnswer(True, "w9", "yes", None, "matched")
    f["rails"]["stripe"].account_status = lambda ref: RailAccount(True, ref, "verified", 1)
    f["rails"]["stripe"].balance = lambda: RailBalance(True, 1000.0, "2026-10-02T17:00:00Z")
    f["bank"].balance = lambda e, a: BankBalance(True, "01.00", "2026-10-02T17:00:00Z")
    f["vault"].contact_ref_valid = lambda ref: "yes"
    r = x.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, caller="scheduler")
    assert r.status_code in (201, 409) and not (r.json().get("batch") or {}).get("items"), r.text
    kinds = {e["payload"]["port"] for e in x.ledger.of_type("adapter_answer_refused")}
    assert {"verification_integrity", "compliance_38", "tax_agent", "rail_stripe", "bank_feed", "vault"} <= kinds
    _restart(x, d)


def test_n17_3_malformed_rail_submit_answer_is_a_transport_error_never_a_posting(hr):
    hr.fund_campaign()
    hr.payee("clip-a")
    hr.accrue("sub-1", views=100000)
    hr.recon()
    b = hr.run()["batch"]
    hr.approve(b)
    hr.fund_batch(b["batch_id"])
    hr.clock.advance(hours=13)
    hr.recon()
    hr.stripe.submit = lambda key, ref, amount, item_id: RailSubmit("accepted", ["po_1"])
    rel = hr.release(b["batch_id"])
    assert [x["status"] for x in rel["results"] if "status" in x][0] == "submitting"
    assert not [e for e in hr.svc.entries if e["memo_code"] == "F4d"]
    assert hr.ledger.of_type("adapter_answer_refused")


def test_n17_3_malformed_clawback_page_is_refused_whole():
    h = Harness().ready()
    h.fund_campaign()
    h.payee("clip-a")
    h.accrue("sub-1", views=100000)
    good = {"clawback_id": "c1", "certification_id": h.vi.certs["sub-1"]["certification_id"], "views_delta": -10,
            "cause": "platform_revision_down", "rule_id": "VI-05"}
    bad = {**good, "clawback_id": "c2", "views_delta": "-10"}
    h.vi.clawbacks = lambda cursor: ClawbackPage(True, (good, bad), None)
    out = h.ok(h.post("/fin/v1/jobs/clawback-sync/run", {"request_id": rid()}, caller="scheduler"))
    assert out["summary"]["available"] is False and out["summary"]["applied"] == 0      # no partial apply
    assert not h.svc.db["clawbacks"]


def test_fix18_a_compliance_row_unavailable_during_a_run_excludes_the_payee_never_a_500(hr):
    """New defect found while sweeping N17-3: a non-empty Compliance-row reason list was unpacked as an OFAC triple
    when the gate inputs were hashed, so any unavailable row turned the whole payout run into a 500."""
    hr.fund_campaign()
    hr.payee("clip-a")
    hr.accrue("sub-1", views=100000)
    hr.recon()
    hr.cmp.rows_available = False
    r = hr.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, caller="scheduler")
    assert r.status_code in (201, 409), r.text
    assert not (r.json().get("batch") or {}).get("items")


# ================================================================== N17-9 absolute liabilities, deposit returns

def test_n17_9_treasury_liabilities_are_absolute_per_sub_ledger():
    import intelligences.i08_treasury as T
    b = {("zbc", "1020", None): Decimal("1000.00"),
         ("zbc", "2010", "campaign:c1"): Decimal("1000.00"),        # debit balance: c1 driven negative
         ("zbc", "2010", "campaign:c2"): Decimal("-1000.00"),       # c2 owed 1000.00
         ("zbc", "2020", "payee:p"): Decimal("-587.50")}
    p = T.position(b)
    assert p["liabilities"] == Decimal("1587.50") and p["gap"] == Decimal("-587.50")


def test_n17_9_reversing_a_consumed_deposit_by_correction_is_refused_not_masked():
    h = Harness().ready()
    h.fund_campaign("camp-1", "client-1", budget="1000.00")
    h.fund_campaign("camp-2", "client-2", budget="1000.00")
    h.payee("clip-a")
    h.accrue("sub-1", views=250000)
    f1 = [e for e in h.svc.entries if e["memo_code"] == "F1" and any(l["subledger"] == "campaign:camp-1"
                                                                       for l in e["lines"])][0]
    r = h.post("/fin/v1/journal/zbc/corrections", {"request_id": rid(), "reverses_entry_id": f1["entry_id"],
                                                   "effective_date": "2026-10-02"}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "TREASURY_BREACH" in _codes(r)


def _return(h, receipt_id, who="bank_feed"):
    return h.post(f"/fin/v1/receipts/{receipt_id}/return", {"request_id": rid(), "return_ref_sha256": "d" * 64,
                                                            "return_code": "R01", "value_date": "2026-10-02"},
                  caller=who)


def test_n17_9_deposit_return_before_any_accrual_reduces_the_unearned_balance_only():
    h = Harness().ready()
    h.fund_campaign()
    rc = [r for r in h.svc.db["receipts"].values() if r["status"] == "matched"][0]
    h.bank.deposit("zbc", "1020", "-1000.00")
    out = h.ok(_return(h, rc["receipt_id"]))
    assert (out["to_2010"], out["shortfall"], out["shortfall_id"]) == ("1000.00", "0.00", None)
    assert h.bal("2010", "campaign:camp-1") == 0 and h.bal("1020") == 0
    assert h.svc.db["profiles"]["camp-1"]["status"] == "approved"                 # no longer funded
    assert h.recon()["fc01"]


def test_n17_9_deposit_return_after_accrual_opens_a_shortfall_that_blocks_runs_until_andre_tops_up():
    h = Harness().ready()
    h.fund_campaign()
    h.payee("clip-a")
    h.accrue("sub-1", views=100000)                   # revenue 400.00 earned, creator 235.00 owed
    rc = [r for r in h.svc.db["receipts"].values() if r["status"] == "matched"][0]
    h.bank.deposit("zbc", "1020", "-1000.00")
    out = h.ok(_return(h, rc["receipt_id"]))
    assert (out["to_2010"], out["shortfall"]) == ("600.00", "400.00")
    assert h.bal("1100", "client:client-1") == Decimal("400.00") and h.bal("2010", "campaign:camp-1") == 0
    assert h.ledger.of_type("deposit_shortfall_opened")
    h.recon()
    r = h.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, caller="scheduler")
    assert r.status_code == 409 and "DEPOSIT_SHORTFALL" in _codes(r)
    low = h.post("/fin/v1/treasury/top-ups", {"request_id": rid(), "amount": "100.00",
                                               "shortfall_id": out["shortfall_id"]}, andre=ANDRE_TOKEN)
    assert low.status_code == 409
    h.bank.deposit("zbc", "1010", "400.00")
    t = h.ok(h.post("/fin/v1/treasury/top-ups", {"request_id": rid(), "amount": "400.00",
                                                  "reason_code": "deposit_return_shortfall",
                                                  "shortfall_id": out["shortfall_id"]}, andre=ANDRE_TOKEN))
    assert t["operation"]["status"] == "done"
    assert h.svc.db["shortfalls"][out["shortfall_id"]]["status"] == "closed"
    h.recon()
    r = h.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, caller="scheduler")
    assert "DEPOSIT_SHORTFALL" not in _codes(r)


def test_n17_9_only_a_matched_deposit_can_be_returned_and_only_by_the_bank_feed_or_andre(hr):
    hr.fund_campaign()
    rc = [r for r in hr.svc.db["receipts"].values() if r["status"] == "matched"][0]
    assert _return(hr, rc["receipt_id"], who="scheduler").status_code == 403
    assert _return(hr, "fin-rct-NOPE").status_code == 404
    hr.bank.deposit("zbc", "1020", "-1000.00")
    hr.ok(_return(hr, rc["receipt_id"]))
    assert _return(hr, rc["receipt_id"]).status_code == 409


# ================================================================== N17-10 own ids never look-alikes

def test_n17_10_finance_generated_ids_are_never_refused_200k():
    import models
    from ledger import derived_id
    from service import rid as frid
    rng = random.Random(17)
    prefixes = ["bat", "brk", "cb", "dec", "dsp", "exc", "hof", "int", "inv", "itm", "je", "off", "pay", "prop", "rc",
                "rcp", "rct", "rec", "rfd", "trx", "sft", "dec2"]
    refused = []
    for i in range(200_000):
        p = prefixes[i % len(prefixes)]
        v = frid(p, rng.random(), i) if i % 4 else derived_id(p, rng.random(), i)
        if models.sensitive_value(v):
            refused.append(v)
    assert refused == []


def test_n17_10_the_exemption_is_only_for_a_whole_own_id():
    import models
    assert models.sensitive_value("DE89370400440532013000") == "iban_shape"
    assert models.sensitive_value("x DE89370400440532013000") == "iban_shape"
    assert models.sensitive_value("4111111111111111") in ("card_number_shape", "bank_account_or_phone_number_shape")


# ================================================================== N17-11 extreme rate cards

@pytest.mark.parametrize("field,value", [("rate", "9999999.00"), ("rate", "1000.01"), ("cap", 10 ** 12 + 1)])
def test_n17_11_extreme_rate_card_is_a_422_with_reason(hr, field, value):
    rates = {t: (value if field == "rate" else "2.35") for t in ("T0", "T1", "T2", "T3")}
    r = hr.post("/fin/v1/rate-cards/proposals", {"request_id": rid(), "campaign_id": "c1", "creator_rate_per_1000": rates,
                                                 "max_paid_views_per_clip": value if field == "cap" else 10,
                                                 "effective_at": "2026-11-01T00:00:00Z"}, andre=ANDRE_TOKEN)
    assert r.status_code == 422, r.text
    assert field == "cap" or "AMOUNT_OUT_OF_RANGE" in r.text


def test_n17_11_views_above_the_cap_are_refused_never_a_500():
    h = Harness(env={"FIN_MAX_CERTIFIED_VIEWS": "1000000"}).ready()
    h.fund_campaign(rate="1000.00", cap=1_000_000)
    h.payee("clip-a")
    r = h.handoff("sub-1", views=10 ** 12)
    assert r.status_code == 200 and r.json()["allowed"] is False
    assert h.ledger.of_type("adapter_answer_refused")


def test_n17_11_a_money_error_anywhere_is_a_422_not_a_500(hr, monkeypatch):
    import svc_books

    def boom(*a, **k):
        raise M.MoneyError("money out of range")
    monkeypatch.setattr(svc_books.BooksMixin, "treasury_view", boom, raising=False)
    monkeypatch.setattr(type(hr.svc), "treasury_view", boom)
    r = hr.get("/fin/v1/treasury")
    assert r.status_code == 422 and r.json()["detail"] == "AMOUNT_OUT_OF_RANGE"


# ================================================================== N17-12 withholding basis

def _withholding(h):
    h.fund_campaign(budget="1000.00")
    h.payee("clip-a")
    h.accrue("sub-1", views=100000)                                   # 235.00
    b, _ = h.full_payout()
    h.pay_items(h.batch(b["batch_id"]))
    h.vi.add_clawback("sub-1", -50000)                                # receivable 117.50
    h.ok(h.post("/fin/v1/jobs/clawback-sync/run", {"request_id": rid()}, caller="scheduler"))
    h.svc.db["tax"]["clip-a"] = {**(h.svc.db["tax"].get("clip-a") or {"payee_id": "clip-a"}),
                                 "backup_withholding": {"flag": True}}
    h.clock.advance(days=7)
    h.accrue("sub-2", views=100000, create_time="2026-09-22T12:00:00Z")
    h.recon()
    it = h.run()["batch"]["items"][0]
    return it["gross"], it["netted"], it["withheld"], it["net"]


def test_n17_12_backup_withholding_defaults_to_24pct_of_gross():
    assert _withholding(Harness().ready()) == ("235.00", "117.50", "56.40", "61.10")


def test_n17_12_gross_minus_netting_applies_only_with_the_cpa_row_verified():
    h = Harness(env={"FIN_WITHHOLDING_BASIS": "gross_minus_netting"}).ready()
    assert _withholding(h) == ("235.00", "117.50", "56.40", "61.10")            # FIN-CQ-16 unverified: gross
    h2 = Harness(env={"FIN_WITHHOLDING_BASIS": "gross_minus_netting"}).ready(
        counsel=("FIN-CQ-01", "FIN-CQ-11", "FIN-CQ-16"))
    assert _withholding(h2) == ("235.00", "117.50", "28.20", "89.30")


def test_n17_12_config_accepts_only_the_two_bases():
    import config
    from helpers import base_env
    with pytest.raises(RuntimeError):
        config.load(base_env(FIN_WITHHOLDING_BASIS="net"))
    assert config.load(base_env()).withholding_basis == "gross"


# ================================================================== N17-13 second approver: own request, own identity

def _dual():
    x = Harness(env={"FIN_SECOND_APPROVER_TOKEN": SECOND_TOKEN, "FIN_DUAL_HUMAN_THRESHOLD": "100.00"}).ready()
    x.fund_campaign()
    x.payee()
    x.accrue("sub-1", views=100000)
    x.recon()
    return x, x.run()["batch"]


def test_n17_13_two_tokens_on_one_request_are_refused():
    x, b = _dual()
    body = {"request_id": rid(), "content_sha256": b["content_sha256"], "decision": "approve"}
    r = x.post(f"/fin/v1/payout-batches/{b['batch_id']}/decision", body, andre=ANDRE_TOKEN, second=SECOND_TOKEN)
    assert r.status_code == 403
    r = x.post(f"/fin/v1/payout-batches/{b['batch_id']}/second-approval", {**body, "request_id": rid()},
               andre=ANDRE_TOKEN, second=SECOND_TOKEN)
    assert r.status_code == 403
    assert x.batch(b["batch_id"])["status"] == "proposed"


def test_n17_13_second_approval_is_a_separate_request_with_its_own_actor_in_either_order():
    for andre_first in (True, False):
        x, b = _dual()
        body = {"content_sha256": b["content_sha256"], "decision": "approve"}
        a = lambda: x.ok(x.post(f"/fin/v1/payout-batches/{b['batch_id']}/decision", {**body, "request_id": rid()},
                                andre=ANDRE_TOKEN))
        s = lambda: x.ok(x.post(f"/fin/v1/payout-batches/{b['batch_id']}/second-approval",
                                {**body, "request_id": rid()}, second=SECOND_TOKEN))
        first, second = (a, s) if andre_first else (s, a)
        r1 = first()
        assert r1["status"] in ("awaiting_second_approver", "awaiting_andre")
        assert x.batch(b["batch_id"])["status"] == "proposed"
        r2 = second()
        assert r2["status"] == "approved" and r2["approval"]["second_approver"] == "second_approver"
        ev = x.ledger.of_type("batch_second_approved")
        assert len(ev) == 1 and ev[0]["actor"] == "second_approver"
        assert x.ledger.of_type("batch_approved_by_andre")[0]["actor"] == "andre"


def test_n17_13_second_approval_needs_the_second_token_and_the_right_hash():
    x, b = _dual()
    body = {"request_id": rid(), "content_sha256": b["content_sha256"], "decision": "approve"}
    assert x.post(f"/fin/v1/payout-batches/{b['batch_id']}/second-approval", body).status_code == 403
    assert x.post(f"/fin/v1/payout-batches/{b['batch_id']}/second-approval", {**body, "request_id": rid()},
                  second=ANDRE_TOKEN).status_code == 403
    assert x.post(f"/fin/v1/payout-batches/{b['batch_id']}/second-approval",
                  {**body, "request_id": rid(), "content_sha256": "0" * 64}, second=SECOND_TOKEN).status_code == 409


# ================================================================== N17-6 swept into Finance: IP addresses

@pytest.mark.parametrize("value", ["10.0.0.1", "fe80::1", "client:192.168.1.1", "r-10.0.0.1:8080", "%31%30.0.0.1",
                                   "::ffff:1.2.3.4"])
def test_n17_6_finance_refuses_an_ip_address_in_any_string_field(hr, value):
    r = hr.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "clip-z", "kind": "clipper",
                                   "declared_country": "US", "owner_subject_ids": [value]}, caller="clipper_network")
    assert r.status_code == 422 and "ip_address_shape" in r.text


# ================================================================== wave-17 class sweep: stale-base weakening

def test_fix18_rate_card_weakening_is_rechecked_against_the_version_in_force_at_decision(hr):
    """New defect found in the stale-base sweep: a rate-card proposal's weakening flag was computed only against the
    version published when it was DRAFTED. Two proposals drafted on v1 -- A lowers the rate, B keeps v1's rate --
    and A published first: B then RAISES the rate against the version in force, yet it was approved without the
    weakening acknowledgment."""
    doc = hr.rate_card("camp-1", rate="2.35", effective_at="2026-11-01T00:00:00Z")

    def prop(rate):
        return hr.ok(hr.post("/fin/v1/rate-cards/proposals", {"request_id": rid(), "doc_id": doc, "campaign_id": "camp-1",
                                                               "creator_rate_per_1000": {t: rate for t in ("T0", "T1", "T2", "T3")},
                                                               "max_paid_views_per_clip": 1_000_000,
                                                               "effective_at": "2026-11-02T00:00:00Z"},
                             andre=ANDRE_TOKEN), 201)["proposal"]
    a, b = prop("2.00"), prop("2.35")
    assert not a["weakening"] and not b["weakening"]
    dec = lambda p, **kw: hr.post("/fin/v1/rate-cards/decisions", {"request_id": rid(), "decisions": [
        {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve", **kw}]},
        andre=ANDRE_TOKEN)
    assert dec(a).status_code == 200
    r = dec(b)
    assert r.status_code == 422 and "creator_rate_raised" in r.text
    assert dec(b, acknowledge_weakening=True).status_code == 200
