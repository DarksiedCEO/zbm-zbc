"""Regression tests for AEGIS re-review of fix-finance c0869c4 (ADR 0009 "Sweep follow-up 3").

  H-N1  refund-payment treasury ops had no content_sha256: settling one crashed (500) and the treasury view showed
        no hash; a refund stuck at bank_unknown could never be settled. Ported from the reviewer's probe
        ``aegis-fin/v2/.../test_probe_v2r.py``; plus a settle test over every op kind that can reach bank_unknown
  L-N2  refunded + charged back above the payment was booked silently
  L1    the ghost exemption bound used the LAST committing line of an rk; it is the first
  M1    a truncate that fails after a failed write left the log refusing writes with no visible reason
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from helpers import ANDRE_TOKEN, Harness, rid
from intelligences import i10_evidence_audit as i10
from stripe_sim import TEST_KEY, TEST_WHSEC
from test_aegis_f751017 import booked, op_of, retry_at, scripted_bank
from test_stripe_incoming import CANCEL, SUCCESS, _secret, make, paid_rr, sent
from test_sweep_followup import _charge, _decide, _rct, _sweep, balanced


NOT_MOVED_REF = "stmt-2026-10-07#L42"


def settle(h, tid, outcome, sha=None, request_id=None, bank_ref=NOT_MOVED_REF):
    body = {"request_id": request_id or rid(), "content_sha256": sha or view_sha(h, tid), "outcome": outcome}
    if bank_ref is not None:
        body["bank_ref"] = bank_ref
    return h.post(f"/fin/v1/treasury/operations/{tid}/settlement", body, andre=ANDRE_TOKEN)


def view_sha(h, tid):
    ops = h.ok(h.get("/fin/v1/treasury"))["open_operations"]
    return next(o["content_sha256"] for o in ops if o["op_id"] == tid)


# --------------------------------------------------------------------------------------------- H-N1 refunds

def refund_unknown(h):
    """A refund whose payment's first ask moved the money and lost the answer: bank_unknown."""
    h.fund_campaign()
    h.payee()
    h.accrue()
    h.recon()
    rf = h.ok(h.post("/fin/v1/refunds/camp-1", {"request_id": rid()}, caller="scheduler"), 201)["refund"]
    st = scripted_bank(h, ["raise"])
    h.post(f"/fin/v1/refunds/{rf['refund_id']}/decision",
           {"request_id": rid(), "content_sha256": rf["content_sha256"], "decision": "approve"}, andre=ANDRE_TOKEN)
    t = next(x for x in h.svc.db["treasury_ops"].values() if x["kind"] == "refund_payment")
    assert t["status"] == "bank_unknown"
    return rf, t["op_id"], st


def test_hn1_refund_payment_ops_carry_their_hash_and_the_view_shows_it(hr):
    rf, tid, _ = refund_unknown(hr)
    t = op_of(hr, tid)
    assert t["content_sha256"] == hr.svc.treasury_op_sha({k: v for k, v in t.items() if k != "content_sha256"})
    assert view_sha(hr, tid) == t["content_sha256"]


def test_hn1_settle_refund_moved_pays_the_refund(hr):
    rf, tid, st = refund_unknown(hr)
    assert settle(hr, tid, "moved", sha="a" * 64).status_code == 409             # wrong hash: refused, no 500
    r = hr.ok(settle(hr, tid, "moved"))
    assert r["operation"]["status"] == "done"
    assert hr.svc.db["refunds"][rf["refund_id"]]["status"] == "paid"
    assert hr.bal("2050", "client:client-1") == 0 and booked(hr, tid, "1020") == -Decimal(rf["amount"])
    assert len(st["moves"]) == 1
    balanced(hr)


def test_hn1_settle_refund_not_moved_reverses_and_andre_can_pay_again(hr):
    rf, tid, st = refund_unknown(hr)
    r = hr.ok(settle(hr, tid, "not_moved"))
    assert r["operation"]["status"] == "not_moved" and r["reversal_entry_id"]
    rfd = hr.svc.db["refunds"][rf["refund_id"]]
    assert rfd["status"] == "payment_not_moved"
    assert booked(hr, tid, "1020") == 0
    assert hr.bal("2050", "client:client-1") == Decimal(rf["amount"])         # the client is still owed
    assert tid not in [o["op_id"] for o in hr.ok(hr.get("/fin/v1/treasury"))["open_operations"]]
    assert hr.recon()["recon"]                                                  # L4 still expects the refund
    assert all(leg["status"] in ("matched", "not_in_use") for leg in hr.recon()["recon"]["legs"]
               if leg["subject"].startswith("zbc:2010"))
    bad = hr.post(f"/fin/v1/refunds/{rf['refund_id']}/repay", {"request_id": rid(), "content_sha256": "b" * 64},
                  andre=ANDRE_TOKEN)
    assert bad.status_code == 409
    other = hr.post(f"/fin/v1/refunds/{rf['refund_id']}/repay",
                    {"request_id": rid(), "content_sha256": rf["content_sha256"]}, caller="scheduler")
    assert other.status_code == 403
    rp = hr.ok(hr.post(f"/fin/v1/refunds/{rf['refund_id']}/repay",
                       {"request_id": rid(), "content_sha256": rf["content_sha256"]}, andre=ANDRE_TOKEN))
    assert rp["payment"]["status"] == "done" and rp["payment"]["op_id"] != tid   # a NEW key, never the old one
    assert hr.svc.db["refunds"][rf["refund_id"]]["status"] == "paid"
    assert hr.bal("2050", "client:client-1") == 0
    balanced(hr)


def test_hn1_settled_refund_survives_a_restart(tmp_path):
    d = str(tmp_path / "fin")
    h = Harness(data_dir=d).ready()
    rf, tid, _ = refund_unknown(h)
    h.ok(settle(h, tid, "not_moved"))
    h2 = Harness(data_dir=d, ledger=h.ledger, clock=h.clock, fakes=h.f)
    assert h2.svc.db["treasury_ops"][tid]["status"] == "not_moved"
    assert h2.svc.db["refunds"][rf["refund_id"]]["status"] == "payment_not_moved"
    assert h2.ok(h2.get("/fin/v1/integrity"))["status"] == "green"


def test_hn1_a_legacy_refund_op_without_a_hash_is_settled_by_its_computed_hash(hr):
    rf, tid, _ = refund_unknown(hr)
    legacy = {k: v for k, v in op_of(hr, tid).items() if k != "content_sha256"}
    hr.svc.db["treasury_ops"][tid] = legacy                       # a record written before c0869c4
    sha = view_sha(hr, tid)
    assert sha == hr.svc.treasury_op_sha(legacy)
    assert hr.ok(settle(hr, tid, "moved", sha=sha))["operation"]["status"] == "done"


# --------------------------------------------------------------------------------------------- H-N1 every op kind

def unknown_sweep(h):
    op = _sweep(h)
    scripted_bank(h, ["raise"])
    h.ok(_decide(h, op))
    return op["op_id"], "1010"


def unknown_topup(h):
    h.fund_campaign()
    h.payee("clip-a")
    h.accrue("sub-1", views=100000)
    rc = [r for r in h.svc.db["receipts"].values() if r["status"] == "matched"][0]
    h.bank.deposit("zbc", "1020", "-1000.00")
    out = h.ok(h.post(f"/fin/v1/receipts/{rc['receipt_id']}/return",
                      {"request_id": rid(), "return_ref_sha256": "c" * 64, "return_code": "R01",
                       "value_date": "2026-10-02"}, caller="bank_feed"))
    h.bank.deposit("zbc", "1010", "400.00")
    scripted_bank(h, ["raise"])
    r = h.post("/fin/v1/treasury/top-ups", {"request_id": rid(), "amount": "400.00",
                                            "reason_code": "deposit_return_shortfall",
                                            "shortfall_id": out["shortfall_id"]}, andre=ANDRE_TOKEN)
    assert r.status_code in (200, 503), r.text
    tid = next(t["op_id"] for t in h.svc.db["treasury_ops"].values() if t["kind"] == "top_up")
    h.shortfall_id = out["shortfall_id"]
    return tid, "1020"


def unknown_funding(h):
    h.fund_campaign()
    h.payee("clip-a")
    h.accrue("sub-1")
    h.recon()
    b = h.run()["batch"]
    h.approve(b)
    op = h.ok(h.post("/fin/v1/treasury/funding", {"request_id": rid(), "batch_id": b["batch_id"]},
                     caller="scheduler"), 201)["operation"]
    scripted_bank(h, ["raise"])
    h.post(f"/fin/v1/treasury/funding/{op['op_id']}/decision",
           {"request_id": rid(), "content_sha256": op["content_sha256"], "decision": "approve"}, andre=ANDRE_TOKEN)
    return op["op_id"], "1040"


def unknown_refund(h):
    rf, tid, _ = refund_unknown(h)
    return tid, "2050"


KINDS = {"sweep": unknown_sweep, "top_up": unknown_topup, "funding": unknown_funding, "refund_payment": unknown_refund}


@pytest.mark.parametrize("kind", sorted(KINDS))
@pytest.mark.parametrize("outcome", ["moved", "not_moved"])
def test_hn1_every_treasury_kind_settles_both_ways(hr, kind, outcome):
    tid, acct = KINDS[kind](hr)
    t = op_of(hr, tid)
    assert t["kind"] == kind and t["status"] == "bank_unknown" and t["content_sha256"]
    before = booked(hr, tid, acct)
    assert before != 0
    r = settle(hr, tid, outcome)
    assert r.status_code == 200, r.text
    t = op_of(hr, tid)
    if outcome == "moved":
        assert t["status"] == "done" and booked(hr, tid, acct) == before
        if kind == "top_up":
            assert hr.svc.db["shortfalls"][hr.shortfall_id]["status"] == "closed"
    else:
        assert t["status"] == "not_moved" and booked(hr, tid, acct) == 0
        if kind == "top_up":
            assert hr.svc.db["shortfalls"][hr.shortfall_id]["status"] == "open"
    assert tid not in [o["op_id"] for o in hr.ok(hr.get("/fin/v1/treasury"))["open_operations"]]
    assert settle(hr, tid, outcome, sha=t["content_sha256"]).status_code == 409        # closed: nothing to settle
    retry_at(hr, 1)
    assert op_of(hr, tid)["status"] == t["status"]
    balanced(hr)


# --------------------------------------------------------------------------------------------- L-N2

@pytest.fixture
def skeys(tmp_path):
    return {"FIN_STRIPE_INCOMING": "1", "FIN_STRIPE_SECRET_KEY_FILE": _secret(tmp_path, "sk", TEST_KEY),
            "FIN_STRIPE_WEBHOOK_SECRET_FILE": _secret(tmp_path, "wh", TEST_WHSEC),
            "FIN_STRIPE_SUCCESS_URL": SUCCESS, "FIN_STRIPE_CANCEL_URL": CANCEL}


def over(st):
    return [b for b in st.svc.db["breaks"].values() if b["leg"] == "stripe_over_recovery"]


def test_ln2_refund_after_a_full_chargeback_opens_an_over_recovery_break(skeys):
    st = make(skeys, FIN_CARD_PREPAYMENTS="1")
    inv, cs, pi, _ = paid_rr(st, "1000.00", method="card")
    du = st.sim.dispute(pi)
    sent(st, "charge.dispute.funds_withdrawn", {"id": du})
    st.sim.close_dispute(du, won=False)
    sent(st, "charge.dispute.closed", {"id": du})
    assert not over(st)
    st.sim.refund(pi, 5000)
    sent(st, "charge.refunded", _charge(st, pi))
    b = over(st)
    assert len(b) == 1 and b[0]["difference"] == "50.00" and b[0]["owner"] == "andre"
    sent(st, "charge.refunded", _charge(st, pi))                                  # redelivery: still one
    assert len(over(st)) == 1
    balanced(st)


def test_ln2_lost_chargeback_after_a_refund_above_the_rest_opens_the_break(skeys):
    st = make(skeys, FIN_CARD_PREPAYMENTS="1")
    inv, cs, pi, _ = paid_rr(st, "1000.00", method="card")
    st.sim.refund(pi, 40000)
    sent(st, "charge.refunded", _charge(st, pi))
    du = st.sim.dispute(pi, amount=70000)                                        # 400 + 700 > 1000
    sent(st, "charge.dispute.created", {"id": du})
    st.sim.close_dispute(du, won=False)
    sent(st, "charge.dispute.closed", {"id": du})
    b = over(st)
    assert len(b) == 1 and b[0]["difference"] == "100.00"
    assert _rct(st, pi)["stripe"]["charged_back"] == "700.00"
    balanced(st)


def test_ln2_exact_full_reversal_opens_no_over_recovery_break(skeys):
    st = make(skeys, FIN_CARD_PREPAYMENTS="1")
    inv, cs, pi, _ = paid_rr(st, "1000.00", method="card")
    st.sim.refund(pi, 30000)
    sent(st, "charge.refunded", _charge(st, pi))
    du = st.sim.dispute(pi, amount=70000)
    sent(st, "charge.dispute.created", {"id": du})
    st.sim.close_dispute(du, won=False)
    sent(st, "charge.dispute.closed", {"id": du})
    assert not over(st)


# --------------------------------------------------------------------------------------------- L1, M1

EPOCH = "ab" * 8
RK, ET, PSHA = "fin-je-" + "c" * 40, "journal_entry_posted", "d" * 64


def _anchor(seq, sha):
    return {"event_id": i10.anchor_id(EPOCH, seq, sha), "event_type": i10.ANCHOR_TYPE, "department": "finance",
            "payload_sha256": "0" * 64}


def test_l1_the_first_committing_line_bounds_the_exemption():
    """The rk is named by line 1 and again by line 3: a ruling between them is a second effect (a ghost)."""
    ruling = {"event_id": i10.evidence_id(RK, ET, PSHA), "event_type": ET, "department": "finance",
              "payload_sha256": PSHA}
    lines = [(1, "1" * 64, True), (2, "2" * 64, True), (3, "3" * 64, True)]
    entries = [_anchor(1, "1" * 64), _anchor(2, "2" * 64), ruling, _anchor(3, "3" * 64)]
    a = i10.assess(entries, EPOCH, lines, set(), 0, strict=False, committed_actions={(RK, ET, 1), (RK, ET, 3)})
    assert ruling["event_id"] in a.void_event_ids


def test_m1_a_truncate_that_fails_is_reported_not_silent(tmp_path, monkeypatch):
    import store as store_mod
    d = str(tmp_path / "fin")
    h = Harness(data_dir=d).ready()
    real_w, real_t = os.pwrite, os.ftruncate
    monkeypatch.setattr(store_mod.os, "pwrite", lambda fd, data, off: real_w(fd, data[:5], off))
    monkeypatch.setattr(store_mod.os, "ftruncate", lambda fd, n: (_ for _ in ()).throw(OSError(5, "EIO")))
    r = h.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "clip-s", "kind": "clipper",
                                  "declared_country": "US", "callback_contact_ref": "vault:contact-clip-s"},
               caller="clipper_network")
    assert r.status_code == 503
    monkeypatch.setattr(store_mod.os, "pwrite", real_w)
    monkeypatch.setattr(store_mod.os, "ftruncate", real_t)
    assert h.ok(h.client.get("/health"))["log_write_fault"] is True
    integ = h.ok(h.get("/fin/v1/integrity"))
    assert integ["status"] == "red" and any("LOCAL_LOG_WRITE_FAULT" in p for p in integ["problems"])
    r = h.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "clip-u", "kind": "clipper",
                                  "declared_country": "US", "callback_contact_ref": "vault:contact-clip-u"},
               caller="clipper_network")
    assert r.status_code == 503                                     # still fail closed




# --------------------------------------------------------------------------------------------- M-N3 (AEGIS 25290ee)

@pytest.mark.parametrize("bad", [None, "", "x" * 129, "stmt line 4", "<b>ref</b>", "ref\n2"])
def test_mn3_not_moved_needs_valid_bank_evidence(hr, bad):
    rf, tid, _ = refund_unknown(hr)
    r = settle(hr, tid, "not_moved", bank_ref=bad)
    assert r.status_code in (409, 422), r.text
    if r.status_code == 409:
        assert "BANK_EVIDENCE_REQUIRED" in r.text
    assert op_of(hr, tid)["status"] == "bank_unknown"
    assert hr.svc.db["refunds"][rf["refund_id"]]["status"] == "approved"
    assert not hr.ledger.of_type("treasury_settled_by_andre")


def test_mn3_not_moved_without_bank_ref_at_the_service_is_refused_with_a_reason(hr):
    """The model already refuses a missing ref (422); the service refuses too, with a reason (defence in depth)."""
    from service import Refused
    rf, tid, _ = refund_unknown(hr)
    with pytest.raises(Refused) as ei:
        hr.svc.settle_treasury(rid(), tid, {"content_sha256": view_sha(hr, tid), "outcome": "not_moved",
                                            "bank_ref": None, "note": None})
    assert "BANK_EVIDENCE_REQUIRED" in str(ei.value.body)


def test_mn3_moved_may_omit_bank_ref(hr):
    rf, tid, _ = refund_unknown(hr)
    assert hr.ok(settle(hr, tid, "moved", bank_ref=None))["operation"]["status"] == "done"


def test_mn3_bank_ref_is_recorded_shown_and_required_for_repay(hr):
    rf, tid, _ = refund_unknown(hr)
    hr.ok(settle(hr, tid, "not_moved"))
    ev = [e for e in hr.ledger.of_type("treasury_settled_by_andre")]
    assert len(ev) == 1 and ev[0]["payload"]["bank_ref"] == NOT_MOVED_REF and ev[0]["payload"]["outcome"] == "not_moved"
    shown = {o["op_id"]: o for o in hr.ok(hr.get("/fin/v1/treasury"))["settled_operations"]}
    assert shown[tid]["settled_bank_ref"] == NOT_MOVED_REF and shown[tid]["settled_outcome"] == "not_moved"
    assert hr.svc.db["refunds"][rf["refund_id"]]["not_moved_bank_ref"] == NOT_MOVED_REF
    rp = hr.ok(hr.post(f"/fin/v1/refunds/{rf['refund_id']}/repay",
                       {"request_id": rid(), "content_sha256": rf["content_sha256"]}, andre=ANDRE_TOKEN))
    assert rp["attested"] == {"not_moved_op_id": tid, "bank_ref": NOT_MOVED_REF}
    assert rp["refund"]["repaid_on_bank_ref"] == NOT_MOVED_REF
    assert hr.ledger.of_type("refund_approved")[-1]["payload"]["not_moved_bank_ref"] == NOT_MOVED_REF


def test_mn3_repay_refused_when_the_settlement_has_no_recorded_bank_ref(hr):
    """A refund left payment_not_moved by a settlement recorded without evidence (e.g. before 25290ee M-N3)."""
    rf, tid, _ = refund_unknown(hr)
    hr.ok(settle(hr, tid, "not_moved"))
    r = hr.svc.db["refunds"][rf["refund_id"]]
    hr.svc.db["refunds"][rf["refund_id"]] = {k: v for k, v in r.items() if k != "not_moved_bank_ref"}
    t = op_of(hr, tid)
    hr.svc.db["treasury_ops"][tid] = {**t, "settled_bank_ref": None}
    rp = hr.post(f"/fin/v1/refunds/{rf['refund_id']}/repay",
                 {"request_id": rid(), "content_sha256": rf["content_sha256"]}, andre=ANDRE_TOKEN)
    assert rp.status_code == 409 and "BANK_EVIDENCE_REQUIRED" in rp.text
    assert hr.svc.db["refunds"][rf["refund_id"]]["status"] == "payment_not_moved"
