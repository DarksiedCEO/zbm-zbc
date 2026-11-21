"""AEGIS r10 R10-1 (ADR 0024): a brand's deposit is never spent while Compliance does not ALLOW that brand.

A ZBC campaign is funded while its brand is ALLOW; Compliance then reports an OFAC SDN match. From then on the brand's
unearned deposit is blocked property: no creator accrual is posted against it, no payout run proposes it, Andre's
approval is refused with the OFAC warning, the rail is not funded for it and the release excludes it. Compliance being
unreachable refuses the same way (fail closed). AEGIS's probe ``test_funded_then_sanctioned_accrual_and_payout`` is
reproduced step by step below.
"""

from __future__ import annotations

from decimal import Decimal

from helpers import ANDRE_TOKEN, SECOND_TOKEN, Harness, rid


def _match(hr, client="client-1"):
    hr.cmp.activations = {client: {"allowed": False, "jurisdiction": "US-CA", "reasons": ["hold open: sanctions_match"],
                                   "sanctions": True, "ground": "match"}}


def _refuse_plain(hr, client="client-1"):
    hr.cmp.activations = {client: {"allowed": False, "jurisdiction": "US-CA", "reasons": ["outside the service area"],
                                   "sanctions": False}}


def _allow(hr):
    hr.cmp.activation_available = True
    hr.cmp.activations = {}


def _generic(text: str) -> None:
    assert "OFAC" not in text and "sanction" not in text.lower() and "match" not in text.lower(), text


def _accrued(hr):
    hr.fund_campaign()
    hr.payee()
    return hr.accrue()


def _approve(hr, b):
    return hr.post(f"/fin/v1/payout-batches/{b['batch_id']}/decision",
                   {"request_id": rid(), "content_sha256": b["content_sha256"], "decision": "approve"},
                   andre=ANDRE_TOKEN)


def test_aegis_r10_1_probe_a_sanctioned_brands_deposit_pays_no_creator():
    hr = Harness().ready()
    hr.fund_campaign()                                          # funded while ALLOW
    hr.payee()
    _match(hr)                                                  # then an OFAC SDN match
    r = hr.ok(hr.handoff())                                     # step 3: the accrual is refused
    assert r["allowed"] is False and "COMPLIANCE_NOT_ACTIVATED" in r["reason"], r
    _generic(r["reason"])
    assert hr.bal("2010", "campaign:camp-1") == Decimal("1000.00")
    assert hr.bal("4010") == 0
    assert all(p["status"] != "accrued" for p in hr.svc.db["payables"].values())
    hr.recon()
    run = hr.run()                                              # step 4: nothing to propose
    assert run["batch"] is None and run["run_empty"]
    assert ("client_activation", "client-1") in hr.cmp.calls


def test_an_accrual_job_retry_accrues_only_once_the_brand_is_allowed_again():
    hr = Harness().ready()
    hr.fund_campaign()
    hr.payee()
    hr.cmp.activation_available = False                        # Compliance unreachable: fail closed
    r = hr.ok(hr.handoff())
    assert r["allowed"] is False and "compliance_38" in r["reason"], r
    hr.ok(hr.post("/fin/v1/jobs/accrual/run", {"request_id": rid()}, caller="scheduler"))
    assert hr.bal("2010", "campaign:camp-1") == Decimal("1000.00")
    _allow(hr)
    hr.clock.advance(hours=25)
    hr.ok(hr.post("/fin/v1/jobs/accrual/run", {"request_id": rid()}, caller="scheduler"))
    assert hr.bal("2010", "campaign:camp-1") == Decimal("950.62")


def test_a_payout_run_excludes_payables_of_a_brand_that_is_no_longer_allowed():
    hr = Harness().ready()
    p = _accrued(hr)
    _match(hr)
    hr.recon()
    run = hr.run()
    assert run["batch"] is None
    ex = [e for e in run["excluded"] if p["payable_id"] in e["payable_ids"]]
    assert ex and "COMPLIANCE_NOT_ACTIVATED" in [r["code"] for r in ex[0]["reasons"]], run
    import json
    _generic(json.dumps(run))


def test_andres_approval_is_refused_with_the_ofac_warning_after_a_match():
    hr = Harness().ready()
    _accrued(hr)
    hr.recon()
    b = hr.run()["batch"]                                       # proposed while ALLOW
    _match(hr)
    r = _approve(hr, b)
    assert r.status_code == 409 and "OFAC" in r.text and "step G" in r.text, r.text
    assert hr.batch(b["batch_id"])["status"] == "proposed"


def test_andres_approval_is_refused_while_compliance_is_unreachable_then_allowed():
    hr = Harness().ready()
    _accrued(hr)
    hr.recon()
    b = hr.run()["batch"]
    hr.cmp.activation_available = False
    r = _approve(hr, b)
    assert r.status_code == 409 and "compliance_38" in r.text, r.text
    _allow(hr)
    assert hr.ok(_approve(hr, b))["status"] == "approved"


def test_rail_funding_is_refused_after_a_match_at_proposal_and_at_andres_decision():
    hr = Harness().ready()
    _accrued(hr)
    hr.recon()
    b = hr.run()["batch"]
    hr.approve(b)
    op = hr.ok(hr.post("/fin/v1/treasury/funding", {"request_id": rid(), "batch_id": b["batch_id"]},
                       caller="scheduler"), 201)["operation"]     # proposed while ALLOW
    _match(hr)
    r = hr.post("/fin/v1/treasury/funding", {"request_id": rid(), "batch_id": b["batch_id"]}, caller="scheduler")
    assert r.status_code == 409, r.text
    _generic(r.text)
    r = hr.post(f"/fin/v1/treasury/funding/{op['op_id']}/decision",
                {"request_id": rid(), "content_sha256": op["content_sha256"], "decision": "approve"}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "OFAC" in r.text, r.text
    assert hr.svc.db["treasury_ops"][op["op_id"]]["status"] == "proposed"
    assert "F4a" not in [e["memo_code"] for e in hr.svc.entries]


def test_rail_funding_approved_while_allowed_is_refused_at_execution_after_a_match():
    hr = Harness().ready()
    _accrued(hr)
    hr.recon()
    b = hr.run()["batch"]
    hr.approve(b)
    op = hr.ok(hr.post("/fin/v1/treasury/funding", {"request_id": rid(), "batch_id": b["batch_id"]},
                       caller="scheduler"), 201)["operation"]
    real = hr.svc._execute_transfer
    seen = {}

    def match_then_execute(tid):                                # the match lands between approval and execution
        _match(hr)
        seen["res"] = real(tid)
        return seen["res"]
    hr.svc._execute_transfer = match_then_execute
    hr.ok(hr.post(f"/fin/v1/treasury/funding/{op['op_id']}/decision",
                  {"request_id": rid(), "content_sha256": op["content_sha256"], "decision": "approve"},
                  andre=ANDRE_TOKEN))
    assert seen["res"]["status"] == "refused_at_execution", seen
    assert "F4a" not in [e["memo_code"] for e in hr.svc.entries]


def test_the_release_excludes_an_item_whose_brand_was_matched_after_approval_and_funding():
    hr = Harness().ready()
    _accrued(hr)
    hr.recon()
    b = hr.run()["batch"]
    hr.approve(b)
    hr.fund_batch(b["batch_id"])
    _match(hr)
    hr.clock.advance(hours=13)
    hr.recon()
    rel = hr.release(b["batch_id"])
    it = rel["batch"]["items"][0]
    assert it["status"] == "excluded_at_release", it
    assert "COMPLIANCE_NOT_ACTIVATED" in [r["code"] for r in it["reasons"]]
    import json
    _generic(json.dumps(rel))
    assert "F4d" not in [e["memo_code"] for e in hr.svc.entries]


def test_a_plain_refusal_excludes_exactly_like_a_sanctions_refusal_for_non_andre_callers():
    import json
    out = []
    for refuse in (_match, _refuse_plain):
        hr = Harness().ready()
        _accrued(hr)
        refuse(hr)
        hr.recon()
        run = hr.run()
        out.append(json.dumps([e["reasons"] for e in run["excluded"]], sort_keys=True))
    assert out[0] == out[1]


def test_another_brands_refusal_does_not_block_this_brands_creators():
    hr = Harness().ready()
    _accrued(hr)
    _match(hr, client="client-2")
    hr.recon()
    b = hr.run()["batch"]
    assert b is not None and b["totals"]["count"] == 1


def test_the_second_approver_is_refused_too():
    hr = Harness(env={"FIN_SECOND_APPROVER_TOKEN": SECOND_TOKEN, "FIN_DUAL_HUMAN_THRESHOLD": "1.00"}).ready()
    _accrued(hr)
    hr.recon()
    b = hr.run()["batch"]
    _match(hr)
    r = hr.post(f"/fin/v1/payout-batches/{b['batch_id']}/second-approval",
                {"request_id": rid(), "content_sha256": b["content_sha256"], "decision": "approve"},
                second=SECOND_TOKEN)
    assert r.status_code == 409, r.text
    _generic(r.text)


# --- AEGIS r10 mutant o4: the deposit re-payment (``repay_refund``) asks Compliance --------------------------------------

def _not_moved_refund(hr):
    from test_aegis_c0869c4 import refund_unknown, settle
    rf, tid, _st = refund_unknown(hr)
    hr.ok(settle(hr, tid, "not_moved"))
    return rf


def _repay(hr, rf):
    return hr.post(f"/fin/v1/refunds/{rf['refund_id']}/repay",
                   {"request_id": rid(), "content_sha256": rf["content_sha256"]}, andre=ANDRE_TOKEN)


def test_o4_a_refund_is_not_paid_again_after_the_client_becomes_an_ofac_match(hr):
    rf = _not_moved_refund(hr)
    ops = set(hr.svc.db["treasury_ops"])
    _match(hr)
    r = _repay(hr, rf)
    assert r.status_code == 409 and "OFAC" in r.text and "step G" in r.text, r.text
    assert set(hr.svc.db["treasury_ops"]) == ops                       # no new payment operation at all
    assert hr.svc.db["refunds"][rf["refund_id"]]["status"] == "payment_not_moved"
    assert hr.bal("2050", "client:client-1") == Decimal(rf["amount"])


def test_o4_a_refund_is_not_paid_again_while_compliance_is_unreachable(hr):
    rf = _not_moved_refund(hr)
    ops = set(hr.svc.db["treasury_ops"])
    hr.cmp.activation_available = False
    r = _repay(hr, rf)
    assert r.status_code == 409 and "compliance_38" in r.text, r.text
    assert set(hr.svc.db["treasury_ops"]) == ops
    _allow(hr)
    assert hr.ok(_repay(hr, rf))["payment"]["status"] == "done"


def test_a_refund_payment_attempt_re_asks_compliance_at_execution(hr):
    """The approval asked; a match landing before the transfer attempt still stops it (every attempt asks)."""
    rf = _not_moved_refund(hr)
    real = hr.svc._execute_transfer
    seen = {}

    def match_then_execute(tid):
        _match(hr)
        seen["res"] = real(tid)
        return seen["res"]
    hr.svc._execute_transfer = match_then_execute
    hr.ok(_repay(hr, rf))
    assert seen["res"]["status"] == "refused_at_execution", seen
    _generic(str(seen["res"]))
    assert hr.bal("2050", "client:client-1") == Decimal(rf["amount"])
