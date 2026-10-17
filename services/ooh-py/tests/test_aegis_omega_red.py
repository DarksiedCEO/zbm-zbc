"""AEGIS Ω RED at d1b0de9: the reliability defect F1 and the test-truth gaps B1-B8.

F1: a ledger answer lost after the ledger recorded the anchor, then a retry that cannot reach the ledger, must leave
the outcome UNKNOWN (the pending line is kept and rolled forward), never "not recorded" (the line dropped, its seq
reused, and every later write refused for good).

B1-B8: each locked rule below is pinned at its exact boundary, with a control that the same request is accepted on
the right side of it, so the refusal is the rule and not something else."""
from __future__ import annotations

import pytest

from helpers import ANDRE, FakeLedger, Harness, at, rid
from ledger import LedgerConflict, LedgerNotRecorded, LedgerRecordError
from test_aegis_ooh_r1 import _back_to_proposal_sent

WRONG = "0" * 64


@pytest.fixture
def acme(h):
    h.tenant("acme")
    return h


# ---------------------------------------------------------------- F1: lost answer, then an unreachable ledger

class _ScriptedLedger(FakeLedger):
    """Plays a scripted fault on the next log anchors; otherwise the ordinary fake ledger."""

    def __init__(self):
        super().__init__()
        self.script: list[str] = []

    def record_event(self, event_id, department, event_type, *a, **k):
        if event_type == "log_anchor" and self.script:
            step = self.script.pop(0)
            if step == "record_then_lose":
                super().record_event(event_id, department, event_type, *a, **k)
                raise LedgerRecordError("response lost after the ledger recorded it")
            if step == "unreachable":
                raise LedgerNotRecorded("ledger unreachable: ConnectError")
            if step == "lose_unrecorded":
                raise LedgerRecordError("response lost; nothing recorded")
        return super().record_event(event_id, department, event_type, *a, **k)


def _recover(h):
    for _ in range(3):
        h.svc._last_integrity_try = 0
        h.svc.verify_integrity(force=True, always=True)
    return h.svc.integrity


def test_f1_lost_answer_then_unreachable_ledger_rolls_forward_and_writes_resume(tmp_path):
    led = _ScriptedLedger()
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"), ledger=led)
    h.tenant("acme")
    led.script = ["record_then_lose", "unreachable"]
    r = h.post("/tenants", {"request_id": rid(), "tenant_id": "orphan-1"}, andre=True)
    assert r.status_code == 503, r.text
    assert _recover(h)["ok"] is True, h.svc.integrity
    assert "orphan-1" in h.svc.tenants                      # the anchored line was rolled forward, not dropped
    h.ok(h.post("/tenants", {"request_id": rid(), "tenant_id": "after-1"}, andre=True), 201)
    assert len(led.of_type("log_anchor")) == len(h.svc.log)
    h2 = h.restart()
    assert h2.svc.integrity["ok"] is True, h2.svc.integrity
    h2.ok(h2.post("/tenants", {"request_id": rid(), "tenant_id": "after-2"}, andre=True), 201)
    assert len(led.of_type("log_anchor")) == len(h2.svc.log)


def test_f1_lost_answer_then_unreachable_ledger_nothing_recorded_also_recovers(tmp_path):
    """The other branch of the same fault: the ledger never held it. Rolling forward re-records it once."""
    led = _ScriptedLedger()
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"), ledger=led)
    h.tenant("acme")
    led.script = ["lose_unrecorded", "unreachable"]
    assert h.post("/tenants", {"request_id": rid(), "tenant_id": "maybe-1"}, andre=True).status_code == 503
    assert _recover(h)["ok"] is True, h.svc.integrity
    h.ok(h.post("/tenants", {"request_id": rid(), "tenant_id": "after-3"}, andre=True), 201)
    assert len(led.of_type("log_anchor")) == len(h.svc.log)


def _recorder_raising(*errors):
    class Rec:
        calls = 0

        def record(self, *a):
            Rec.calls += 1
            e = errors[Rec.calls - 1]
            if e is not None:
                raise e
    return Rec()


@pytest.mark.parametrize("first, second, expect", [
    (LedgerRecordError("lost"), LedgerNotRecorded("unreachable"), "unknown"),     # F1: stays unknown
    (LedgerRecordError("lost"), LedgerRecordError("lost again"), "unknown"),
    (LedgerNotRecorded("refused"), None, False),                                  # certain first: no retry
])
def test_f1_record_twice_outcome(acme, first, second, expect):
    svc = acme.svc
    saved = svc.rec
    svc.rec = rec = _recorder_raising(first, second)
    try:
        with pytest.raises(LedgerRecordError) as ei:
            svc._record_twice("evd-x", "log_anchor", "andre", "log:x", {}, "s")
    finally:
        svc.rec = saved
    assert ei.value.took_effect == expect
    assert type(rec).calls == (1 if expect is False else 2)


def test_f1_conflict_is_not_retried(acme):
    svc = acme.svc
    saved = svc.rec
    svc.rec = rec = _recorder_raising(LedgerConflict("409"))
    try:
        with pytest.raises(LedgerConflict):
            svc._record_twice("evd-x", "log_anchor", "andre", "log:x", {}, "s")
    finally:
        svc.rec = saved
    assert type(rec).calls == 1


# ---------------------------------------------------------------- B1: a wrong Andre token is refused, not "presented"

def _wrong(h):
    x = h.headers("dashboard")
    x["X-Andre-Approval-Token"] = WRONG
    return x


def _right(h):
    x = h.headers("dashboard")
    x["X-Andre-Approval-Token"] = ANDRE
    return x


def test_b1_wrong_andre_token_on_buy_limit_price(acme):
    h = acme
    cid = h.campaign("acme")
    h.plan("acme", cid)
    body = {"price_type": "APPROVED_BUY_LIMIT", "amount": "9000.00", "currency": "USD"}
    url = f"/ooh/v1/tenants/acme/campaigns/{cid}/prices"
    h.refused(h.client.post(url, json={"request_id": rid(), **body}, headers=_wrong(h)), 403, "ANDRE_APPROVAL_INVALID")
    h.ok(h.client.post(url, json={"request_id": rid(), **body}, headers=_right(h)), 201)


def test_b1_wrong_andre_token_on_plan_approval(acme):
    h = acme
    cid = h.campaign("acme")
    for to in ("BRIEF_VALIDATED", "PLAN_DRAFTED"):
        h.ok(h.go("acme", cid, to))
    url = f"/ooh/v1/tenants/acme/campaigns/{cid}/transitions"

    def body():
        c = h.view("acme", cid)
        return {"request_id": rid(), "to": "PLAN_APPROVED", "expected_state": c["state"],
                "expected_version": c["version"], "reason": "TEST_STEP"}
    h.refused(h.client.post(url, json=body(), headers=_wrong(h)), 403, "ANDRE_APPROVAL_INVALID")
    assert h.view("acme", cid)["state"] == "PLAN_DRAFTED"
    h.ok(h.client.post(url, json=body(), headers=_right(h)))
    assert h.view("acme", cid)["state"] == "PLAN_APPROVED"


def test_b1_wrong_andre_token_on_readiness(acme):
    h = acme
    url = "/ooh/v1/markets/los-angeles/readiness"
    body = {"flag": "PLANNING_COVERAGE", "value": "MET", "evidence_ref": "readiness-la-planning"}
    h.refused(h.client.post(url, json={"request_id": rid(), **body}, headers=_wrong(h)), 403, "ANDRE_APPROVAL_INVALID")
    h.ok(h.client.post(url, json={"request_id": rid(), **body}, headers=_right(h)))


def test_b1_wrong_andre_token_on_kill_switch_release(acme):
    h = acme
    h.ok(h.switch("capability:purchase", True))
    url = "/ooh/v1/kill-switches"
    body = {"switch": "capability:purchase", "engaged": False}
    h.refused(h.client.post(url, json={"request_id": rid(), **body}, headers=_wrong(h)), 403, "ANDRE_APPROVAL_INVALID")
    h.ok(h.client.post(url, json={"request_id": rid(), **body}, headers=_right(h)))


# ---------------------------------------------------------------- B2 / B3: Andre-only routes without his token

def test_b2_funds_reconfirmation_needs_andre(acme):
    h = acme
    ctx = h.funded()
    _back_to_proposal_sent(h, ctx)
    h.ok(h.approve(ctx))
    url = f"/tenants/acme/campaigns/{ctx['cid']}/funds-reconfirmations"

    def body():
        return {"request_id": rid(), "expected_version": h.view("acme", ctx["cid"])["version"],
                "approved_amount": ctx["sell"], "currency": "USD"}
    before = h.view("acme", ctx["cid"])["state"]
    h.refused(h.post(url, body(), caller="dashboard", andre=False), 403, "ANDRE_APPROVAL_REQUIRED")
    h.refused(h.post(url, body(), caller="ooh_agent"), 403)
    assert h.view("acme", ctx["cid"])["state"] == before
    h.ok(h.post(url, body(), andre=True))
    assert h.view("acme", ctx["cid"])["state"] == "FUNDS_VERIFIED"


def test_b3_order_outcome_needs_andre(acme):
    h = acme
    ctx = h.authorized()
    h.ok(h.submit(ctx))
    url = f"/tenants/acme/campaigns/{ctx['cid']}/authorizations/{ctx['aid']}/outcome"

    def body():
        return {"request_id": rid(), "outcome": "ACCEPTED", "vendor_ref": "lamar-io-9001"}
    h.refused(h.post(url, body(), caller="dashboard", andre=False), 403, "ANDRE_APPROVAL_REQUIRED")
    h.refused(h.post(url, body(), caller="ooh_agent"), 403)
    h.ok(h.post(url, body(), andre=True))


# ---------------------------------------------------------------- B4-B6: money and time limits at the boundary

def test_b4_ten_thousand_is_allowed_one_cent_more_is_refused(acme):
    h = acme
    h.ok(h.authorize(h.funded("acme", amount="10000.00", ref="b4-at")), 201)
    h.refused(h.authorize(h.funded("acme", amount="10000.01", ref="b4-over")), 409, "SPEND_LIMIT_EXCEEDED")


def test_b5_buy_limit_plus_one_cent_is_refused(acme):
    h = acme
    ctx = h.to_proposal_sent("acme", amount="5000.01", ref="b5-face")
    cid = ctx["cid"]
    h.ok(h.price("acme", cid, "APPROVED_BUY_LIMIT", "5000.00", andre=True), 201)
    h.ok(h.approve(ctx))
    h.ok(h.funds(ctx, ctx["sell"]))
    c = h.view("acme", cid)
    ctx.update(approval_id=c["client_approval"]["approval_id"], terms_id=c["commercial"]["terms_id"])
    h.finance_verify(ctx)
    ctx["contract_id"] = h.contract(ctx)
    h.refused(h.authorize(ctx), 409, "AMOUNT_EXCEEDS_APPROVAL")


def test_b5_control_buy_limit_equal_to_the_quote_is_allowed(acme):
    h = acme
    ctx = h.funded("acme", amount="5000.00", ref="b5-ctl")       # the helper sets APPROVED_BUY_LIMIT = the quote
    h.ok(h.authorize(ctx), 201)


def test_b6_authority_of_exactly_seven_days_is_allowed_one_second_more_is_refused(acme):
    h = acme
    ctx = h.funded("acme")
    h.refused(h.authorize(ctx, expires_at=at(days=7, seconds=1)), 409, "AUTHORITY_TOO_LONG")
    h.ok(h.authorize(ctx, expires_at=at(days=7)), 201)


# ---------------------------------------------------------------- B7: the purchase is bound to the client's approval

def test_b7_a_quote_the_client_never_approved_is_refused(acme):
    h = acme
    ctx = h.funded(amount="5000.00")
    tid, cid = "acme", ctx["cid"]
    uid2 = h.unit(tid, cid, ref="b7-extra-face")
    for to in ("VERIFIED", "AVAILABILITY_REQUESTED"):
        h.ok(h.move(tid, cid, uid2, to))
    qid2 = h.quote(tid, cid, [uid2], "1000.00")
    for to in ("QUOTED", "HOLD_REQUESTED"):
        h.ok(h.move(tid, cid, uid2, to))
    h.confirm_hold(tid, cid, uid2)
    ctx2 = dict(ctx, uid=uid2, qid=qid2, amount="1000.00")
    ctx2["contract_id"] = h.contract(ctx2, total="1000.00")
    h.refused(h.authorize(ctx2), 409, "CLIENT_APPROVAL_MISMATCH")
    h.ok(h.authorize(ctx), 201)                                   # control: the approved quote still buys


# ---------------------------------------------------------------- B8: the approved contract must match exactly

def _two_quote_campaign(h):
    cid = h.campaign("acme")
    h.plan("acme", cid)
    u1, q1 = h.held_unit("acme", cid, amount="5000.00", ref="b8-1")
    u2, q2 = h.held_unit("acme", cid, amount="5000.00", ref="b8-2")
    h.ok(h.go("acme", cid, "AVAILS_REQUESTED"))
    h.ok(h.commercial("acme", cid, "20.00"))
    sp = h.ok(h.price("acme", cid, "CLIENT_SELL_PRICE", "12000.00", andre=True, quote_refs=[q1, q2]), 201)
    h.ok(h.price("acme", cid, "APPROVED_BUY_LIMIT", "5000.00", andre=True), 201)
    h.ok(h.go("acme", cid, "QUOTES_VALIDATED"))
    h.ok(h.go("acme", cid, "PROPOSAL_SENT", andre=True))
    ctx = {"tid": "acme", "cid": cid, "uid": u1, "qid": q1, "sell_id": sp["price_id"], "sell": "12000.00",
           "amount": "5000.00"}
    h.ok(h.approve(ctx))
    h.ok(h.funds(ctx, "12000.00"))
    c = h.view("acme", cid)
    ctx.update(approval_id=c["client_approval"]["approval_id"], terms_id=c["commercial"]["terms_id"])
    h.finance_verify(ctx)
    return ctx, (u2, q2)


def test_b8_a_contract_for_one_quote_cannot_buy_another(acme):
    h = acme
    ctx, (u2, q2) = _two_quote_campaign(h)
    ctx["contract_id"] = h.contract(ctx)                          # approved for Q1 only
    h.refused(h.authorize(dict(ctx, uid=u2, qid=q2)), 409, "CONTRACT_MISMATCH")
    h.ok(h.authorize(ctx), 201)                                   # control: Q1 under its own contract


def test_b8_a_contract_for_another_total_is_refused_at_the_draft(acme):
    """A total, vendor or currency that differs from the quote never reaches the purchase gate: the draft refuses the
    total, and the authorization refuses the amount, vendor and currency before the contract is compared."""
    h = acme
    ctx, _ = _two_quote_campaign(h)
    base = f"/tenants/acme/campaigns/{ctx['cid']}/contracts"
    body = {"request_id": rid(), "vendor": "lamar", "quote_refs": [ctx["qid"]], "total": "4999.99", "currency": "USD",
            "io_doc_ref": "lamar-io-draft-1", "cancellation_terms_ref": "lamar-cxl-30d", "payment_terms_days": 30,
            "vendor_payment_method": "ach"}
    h.refused(h.post(base, body), 409, "CONTRACT_TOTAL_MISMATCH")
    ctx["contract_id"] = h.contract(ctx)
    h.refused(h.authorize(ctx, authorized_amount="4999.99"), 409, "QUOTE_ORDER_MISMATCH")
    h.refused(h.authorize(ctx, vendor="outfront"), 409, "VENDOR_MISMATCH")
