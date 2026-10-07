"""Regression tests for AEGIS REVISE of fix-finance f751017 (ADR 0009 "Sweep follow-up 2 -- AEGIS REVISE of f751017").

  C1    after an unknown bank outcome a later refused / unavailable answer reversed the posting and a new attempt
        re-sent the key past the bank's window (double payment), or left money moved but booked net 0
  M-N1  an operation whose bank outcome is unknown could never close; Andre now settles it (recorded, idempotent)
  M-N2  a missing window anchor counted as inside the window; a clock behind the anchor extended it
  L-N1  a partial refund plus a lost chargeback for the rest left the receipt / invoice as if still paid

C1 ports the reviewer's probe (``aegis-fin/new/.../test_probe_m2b.py``): a bank that de-duplicates a key for 24 h
from first sight and really moves money on each move.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from helpers import ANDRE_TOKEN, rid
from ports import BankTransfer
from test_stripe_incoming import CANCEL, SUCCESS, _secret, bal, make, memos, paid_rr, sent
from stripe_sim import TEST_KEY, TEST_WHSEC
from test_sweep_followup import _charge, _decide, _rct, _sweep, balanced


def scripted_bank(h, script):
    """A bank whose idempotency window is 24 h from first sight of a key. ``script``: per call, "ok", "raise"
    (moves the money, then the answer is lost) or "unavailable" (moves nothing)."""
    real = h.bank.transfer
    st = {"first": {}, "moves": []}

    def transfer(entity, src, dst, amount, key):
        now = h.clock.now()
        act = script.pop(0) if script else "ok"
        if act == "unavailable":
            return BankTransfer("unavailable")
        if act == "refused":
            return BankTransfer("refused")
        seen = st["first"].get(key)
        if seen is None or now - seen >= timedelta(hours=24):
            st["first"][key] = now
            st["moves"].append(now)
            real(entity, src, dst, amount, f"{key}#{len(st['moves'])}")
        if act == "raise":
            raise RuntimeError("timeout after send")
        return BankTransfer("accepted", "bt-ok")
    h.bank.transfer = transfer
    return st


def booked(h, tid, acct):
    n = Decimal(0)
    for e in h.svc.entries:
        if e["source"].get("id") == tid:
            for ln in e["lines"]:
                if ln["account"] == acct:
                    n += Decimal(ln["debit"]) - Decimal(ln["credit"])
    return n


def retry_at(h, hours):
    h.clock.advance(hours=hours)
    h.svc.retry_transfers()


def op_of(h, tid):
    return h.svc.db["treasury_ops"][tid]


# --------------------------------------------------------------------------------------------- C1

@pytest.mark.parametrize("later", ["unavailable", "refused"])
def test_c1_sweep_unknown_then_refusal_is_never_reversed_nor_resent(hr, later):
    """Probe a: raise (moved), +22 h re-ask answered unavailable/refused, +3 h: one move, booked 100.00, bank_unknown."""
    op = _sweep(hr)
    st = scripted_bank(hr, ["raise", later])
    hr.ok(_decide(hr, op))
    retry_at(hr, 22)
    t = op_of(hr, op["op_id"])
    assert t["status"] == "bank_unknown" and t["outcome_unknown"] and t["last_outcome"] == later
    retry_at(hr, 3)
    assert len(st["moves"]) == 1
    assert booked(hr, op["op_id"], "1010") == Decimal("100.00")           # money moved, books say moved
    assert not [e for e in hr.svc.entries if e.get("reverses_entry_id") and e["source"].get("id") == op["op_id"]]
    t = op_of(hr, op["op_id"])
    assert t["status"] == "bank_unknown" and t["attempts"] == 1 and t["retry_window_expired"]
    kinds = {b.get("kind") for b in hr.svc.db["breaks"].values() if b["subject"] == f"transfer:{op['op_id']}"}
    assert {"bank_state_unknown", "bank_retry_window_expired"} <= kinds


def test_c1_sweep_unknown_then_repeated_unavailable_never_books_zero(hr):
    """Probe b: money moved once; the books must never say nothing moved."""
    op = _sweep(hr)
    st = scripted_bank(hr, ["raise", "unavailable", "unavailable", "unavailable"])
    hr.ok(_decide(hr, op))
    retry_at(hr, 1)
    retry_at(hr, 1)
    assert len(st["moves"]) == 1 and booked(hr, op["op_id"], "1010") == Decimal("100.00")
    assert op_of(hr, op["op_id"])["status"] == "bank_unknown"


def test_c1_sweep_never_ends_refused_at_execution_after_money_moved(hr):
    """Probe c: +22 h unavailable, +3 h recon then retry: one move, never ``refused_at_execution`` with 1010 at 0."""
    op = _sweep(hr, amount="10.00")
    st = scripted_bank(hr, ["raise", "unavailable"])
    hr.ok(_decide(hr, op))
    retry_at(hr, 22)
    hr.clock.advance(hours=3)
    hr.recon()
    hr.svc.retry_transfers()
    t = op_of(hr, op["op_id"])
    assert len(st["moves"]) == 1 and t["status"] == "bank_unknown"
    assert booked(hr, op["op_id"], "1010") == Decimal("10.00")


def test_c1_topup_is_never_paid_twice(hr):
    """Probe d: a top-up whose first ask moved money then lost the answer; re-ask unavailable; +25 h: one move."""
    hr.fund_campaign(budget="1000.00")
    hr.bank.deposit("zbc", "1010", "400.00")
    st = scripted_bank(hr, ["raise", "unavailable"])
    r = hr.post("/fin/v1/treasury/top-ups", {"request_id": rid(), "amount": "50.00",
                                             "reason_code": "clawback_after_release"}, andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text
    tid = r.json()["operation"]["op_id"]
    retry_at(hr, 22)
    retry_at(hr, 3)
    assert len(st["moves"]) == 1, "bank moved the top-up twice"
    assert booked(hr, tid, "1020") == Decimal("50.00")
    assert op_of(hr, tid)["status"] == "bank_unknown"


def test_c1_a_clean_refusal_still_reverses_and_retries_within_the_window(hr):
    """Unchanged: a refusal with no earlier unknown answer is a clean "nothing moved" (reversal, new attempt)."""
    op = _sweep(hr)
    st = scripted_bank(hr, ["refused"])
    t = hr.ok(_decide(hr, op))
    assert t["operation"]["status"] == "approved"
    first = op_of(hr, op["op_id"])["first_asked_at"]
    retry_at(hr, 2)
    t = op_of(hr, op["op_id"])
    assert t["status"] == "done" and t["attempts"] == 2 and t["first_asked_at"] == first
    assert len(st["moves"]) == 1 and booked(hr, op["op_id"], "1010") == Decimal("100.00")


def test_c1_the_window_runs_from_the_first_ask_ever_across_attempts(hr):
    """Clean refusals (nothing moved) keep their first ask; an unknown answer on a later attempt is bounded by the
    window from that FIRST ask, never from the later attempt's posting."""
    op = _sweep(hr)
    st = scripted_bank(hr, ["refused", "raise"])
    hr.ok(_decide(hr, op))
    first = op_of(hr, op["op_id"])["first_asked_at"]
    retry_at(hr, 20)                                          # attempt 2: moves the money, answer lost
    t = op_of(hr, op["op_id"])
    assert t["attempts"] == 2 and t["first_asked_at"] == first and t["status"] == "bank_unknown"
    retry_at(hr, 4)                                           # 24 h after the first ask, 4 h after attempt 2's
    t = op_of(hr, op["op_id"])
    assert t["retry_window_expired"] and t["status"] == "bank_unknown"
    assert len(st["moves"]) == 1 and booked(hr, op["op_id"], "1010") == Decimal("100.00")


# --------------------------------------------------------------------------------------------- M-N1

def _settle(h, tid, outcome, andre=ANDRE_TOKEN, request_id=None, **kw):
    t = op_of(h, tid)
    body = {"request_id": request_id or rid(), "content_sha256": t["content_sha256"], "outcome": outcome, **kw}
    return h.post(f"/fin/v1/treasury/operations/{tid}/settlement", body, andre=andre)


def _unknown_sweep(h):
    op = _sweep(h)
    scripted_bank(h, ["raise"])
    h.ok(_decide(h, op))
    retry_at(h, 24)                                           # window passed: Finance stops asking
    assert op_of(h, op["op_id"])["retry_window_expired"]
    return op["op_id"]


def test_mn1_andre_settles_moved_the_posting_stands_and_the_op_closes(hr):
    tid = _unknown_sweep(hr)
    rq = rid()
    r = hr.ok(_settle(hr, tid, "moved", request_id=rq, bank_ref="stmt-line-17"))
    t = op_of(hr, tid)
    assert r["operation"]["status"] == t["status"] == "done" and t["settled_by"] == "andre"
    assert booked(hr, tid, "1010") == Decimal("100.00")
    assert all(b["status"] == "resolved" for b in hr.svc.db["breaks"].values()
               if b["subject"] == f"transfer:{tid}")
    assert hr.ledger.of_type("treasury_settled_by_andre")
    assert hr.ok(_settle(hr, tid, "moved", request_id=rq, bank_ref="stmt-line-17")) == r     # idempotent replay
    assert _settle(hr, tid, "not_moved").status_code == 409                                 # closed now
    assert tid not in [o["op_id"] for o in hr.ok(hr.get("/fin/v1/treasury"))["open_operations"]]
    balanced(hr)


def test_mn1_andre_settles_not_moved_the_posting_is_reversed_and_released(hr):
    tid = _unknown_sweep(hr)
    r = hr.ok(_settle(hr, tid, "not_moved"))
    t = op_of(hr, tid)
    assert t["status"] == "not_moved" and r["reversal_entry_id"]
    assert booked(hr, tid, "1010") == 0 and booked(hr, tid, "1020") == 0
    assert tid not in [o["op_id"] for o in hr.ok(hr.get("/fin/v1/treasury"))["open_operations"]]
    hr.svc.retry_transfers()
    assert op_of(hr, tid)["status"] == "not_moved"                      # never asked again
    balanced(hr)


def test_mn1_only_andre_settles_and_only_an_unknown_operation(hr):
    tid = _unknown_sweep(hr)
    assert _settle(hr, tid, "moved", andre="not-andre-token-xxxxxxxxxxxxxxxx").status_code == 403
    r = hr.post(f"/fin/v1/treasury/operations/{tid}/settlement",
                {"request_id": rid(), "content_sha256": op_of(hr, tid)["content_sha256"], "outcome": "moved"},
                caller="scheduler")
    assert r.status_code in (401, 403)
    bad = hr.post(f"/fin/v1/treasury/operations/{tid}/settlement",
                  {"request_id": rid(), "content_sha256": "0" * 64, "outcome": "moved"}, andre=ANDRE_TOKEN)
    assert bad.status_code == 409
    assert op_of(hr, tid)["status"] == "bank_unknown" and not hr.ledger.of_type("treasury_settled_by_andre")
    hr.ok(_settle(hr, tid, "moved"))
    assert _settle(hr, tid, "moved").status_code == 409                 # done: nothing left to settle


def test_mn1_a_settled_operation_survives_a_restart(tmp_path):
    from helpers import Harness
    d = str(tmp_path / "fin")
    h = Harness(data_dir=d).ready()
    tid = _unknown_sweep(h)
    h.ok(_settle(h, tid, "moved"))
    h2 = Harness(data_dir=d, ledger=h.ledger, clock=h.clock, fakes=h.f)
    assert h2.svc.db["treasury_ops"][tid]["status"] == "done"
    assert h2.ok(h2.get("/fin/v1/integrity"))["status"] == "green"


# --------------------------------------------------------------------------------------------- M-N2

def test_mn2_an_operation_with_no_window_anchor_is_expired_not_reasked(hr):
    op = _sweep(hr)
    scripted_bank(hr, ["raise"])
    hr.ok(_decide(hr, op))
    tid = op["op_id"]
    legacy = {k: v for k, v in op_of(hr, tid).items() if k not in ("first_asked_at", "posted_at")}
    hr.svc.db["treasury_ops"][tid] = legacy                           # a record written before 420b488
    calls = []
    hr.bank.transfer = lambda *a, **k: calls.append(a) or BankTransfer("accepted", "x")
    hr.svc.retry_transfers()
    assert calls == []
    t = op_of(hr, tid)
    assert t["retry_window_expired"] and t["status"] == "bank_unknown"
    assert any(b.get("kind") == "bank_retry_window_expired" for b in hr.svc.db["breaks"].values())


def test_mn2_a_second_attempt_without_first_asked_at_never_uses_its_own_posted_at(hr):
    op = _sweep(hr)
    scripted_bank(hr, ["raise"])
    hr.ok(_decide(hr, op))
    tid = op["op_id"]
    hr.svc.db["treasury_ops"][tid] = {**{k: v for k, v in op_of(hr, tid).items() if k != "first_asked_at"},
                                      "attempts": 2}
    calls = []
    hr.bank.transfer = lambda *a, **k: calls.append(a) or BankTransfer("accepted", "x")
    hr.svc.retry_transfers()
    assert calls == [] and op_of(hr, tid)["retry_window_expired"]


def test_mn2_a_clock_behind_the_anchor_never_reasks(hr):
    op = _sweep(hr)
    scripted_bank(hr, ["raise"])
    hr.ok(_decide(hr, op))
    calls = []
    hr.bank.transfer = lambda *a, **k: calls.append(a) or BankTransfer("accepted", "x")
    hr.clock.advance(hours=-30)                                       # the clock jumped back
    hr.svc.retry_transfers()
    assert calls == [] and op_of(hr, op["op_id"])["status"] == "bank_unknown"
    hr.clock.advance(hours=30 + 24)                                   # back past the real window
    hr.svc.retry_transfers()
    assert calls == [] and op_of(hr, op["op_id"])["retry_window_expired"]


# --------------------------------------------------------------------------------------------- L-N1

@pytest.fixture
def skeys(tmp_path):
    return {"FIN_STRIPE_INCOMING": "1", "FIN_STRIPE_SECRET_KEY_FILE": _secret(tmp_path, "sk", TEST_KEY),
            "FIN_STRIPE_WEBHOOK_SECRET_FILE": _secret(tmp_path, "wh", TEST_WHSEC),
            "FIN_STRIPE_SUCCESS_URL": SUCCESS, "FIN_STRIPE_CANCEL_URL": CANCEL}


def test_ln1_partial_refund_then_lost_chargeback_of_the_rest_is_fully_reversed(skeys):
    st = make(skeys, FIN_CARD_PREPAYMENTS="1")
    inv, cs, pi, _ = paid_rr(st, "1000.00", method="card")
    st.sim.refund(pi, 30000)
    sent(st, "charge.refunded", _charge(st, pi))
    du = st.sim.dispute(pi, amount=70000)
    sent(st, "charge.dispute.created", {"id": du})
    st.sim.close_dispute(du, won=False)
    assert sent(st, "charge.dispute.closed", {"id": du})["status"] == "dispute_lost"
    assert _rct(st, pi)["status"] == "charged_back"
    i = st.svc.db["invoices"][inv["invoice_id"]]
    assert i["status"] == "issued" and i["paid_at"] is None
    assert bal(st, "1100", "client:zbm-client-7") == Decimal("1000.00") and bal(st, "1300") == 0
    assert not [b for b in st.svc.db["breaks"].values() if b["leg"] == "dispute_receivable"]
    assert memos(st).count("F7r") == 1 and memos(st).count("F7l") == 1
    balanced(st)


def test_ln1_partly_lost_chargeback_then_refund_of_the_rest_is_fully_reversed(skeys):
    st = make(skeys, FIN_CARD_PREPAYMENTS="1")
    inv, cs, pi, _ = paid_rr(st, "1000.00", method="card")
    du = st.sim.dispute(pi, amount=70000)
    sent(st, "charge.dispute.created", {"id": du})
    st.sim.close_dispute(du, won=False)
    sent(st, "charge.dispute.closed", {"id": du})
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "paid"           # 700 of 1000 taken back so far
    st.sim.refund(pi, 30000)
    assert sent(st, "charge.refunded", _charge(st, pi))["status"] == "refund_booked"
    assert _rct(st, pi)["status"] == "charged_back"
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "issued"
    assert bal(st, "1100", "client:zbm-client-7") == Decimal("1000.00")
    balanced(st)


def test_ln1_partial_refund_and_a_smaller_chargeback_stay_partial(skeys):
    st = make(skeys, FIN_CARD_PREPAYMENTS="1")
    inv, cs, pi, _ = paid_rr(st, "1000.00", method="card")
    st.sim.refund(pi, 30000)
    sent(st, "charge.refunded", _charge(st, pi))
    du = st.sim.dispute(pi, amount=20000)
    sent(st, "charge.dispute.created", {"id": du})
    st.sim.close_dispute(du, won=False)
    sent(st, "charge.dispute.closed", {"id": du})
    assert _rct(st, pi)["status"] == "partially_refunded"
    assert st.svc.db["invoices"][inv["invoice_id"]]["status"] == "paid"
    assert [b for b in st.svc.db["breaks"].values() if b["leg"] == "dispute_receivable"]
    balanced(st)
