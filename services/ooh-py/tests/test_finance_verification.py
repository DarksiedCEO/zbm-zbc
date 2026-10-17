"""Locked (founder, Oct 16 2026 round 2): "Finance (dept 31) must verify funds cleared." Andre's gate records what he
observed; a purchase also needs Finance's own verification for the current client approval (ADR 0022)."""

from __future__ import annotations

import pytest

from helpers import rid


@pytest.fixture
def acme(h):
    h.tenant("acme")
    return h


def unverified(h):
    """FUNDS_VERIFIED through Andre's gate, contract approved, but no Finance (31) verification."""
    ctx = h.to_proposal_sent()
    h.ok(h.approve(ctx))
    h.ok(h.funds(ctx, ctx["sell"]))
    c = h.view("acme", ctx["cid"])
    ctx.update(approval_id=c["client_approval"]["approval_id"], terms_id=c["commercial"]["terms_id"])
    ctx["contract_id"] = h.contract(ctx)
    return ctx


def fv_body(ctx, h, **over):
    ap = h.view("acme", ctx["cid"])["client_approval"]
    return {"request_id": rid(), "approval_id": ap["approval_id"], "verified_amount": ap["amount"], "currency": "USD",
            "finance_ref": "fin31-ver-1", "cleared": True, **over}


def test_andres_gate_alone_never_authorizes_a_purchase(acme):
    h = acme
    ctx = unverified(h)
    assert {f["evidence_class"] for f in h.view("acme", ctx["cid"])["funds"]} == {"OBSERVED"}
    h.refused(h.authorize(ctx), 409, "FUNDS_NOT_FINANCE_VERIFIED")
    c = h.finance_verify(ctx)
    fv = c["finance_verification"]
    assert fv["evidence_class"] == "FINANCIALLY_VERIFIED" and fv["source"] == "FINANCE_31"
    assert h.ledger.of_type("client_funds_verified_by_finance_31")[-1]["actor"] == "finance_31"
    h.ok(h.authorize(ctx), 201)


@pytest.mark.parametrize("caller,andre", [("dashboard", True), ("dashboard", False), ("ooh_agent", False),
                                          ("compliance_38", False), ("scheduler", False)])
def test_only_finance_verifies(acme, caller, andre):
    h = acme
    ctx = unverified(h)
    r = h.post(f"/tenants/acme/campaigns/{ctx['cid']}/finance-verifications", fv_body(ctx, h), caller=caller,
               andre=andre)
    assert r.status_code == 403, r.text
    assert h.view("acme", ctx["cid"])["finance_verification"] is None


def test_a_verification_covers_the_current_approval_in_full(acme):
    h = acme
    ctx = unverified(h)
    path = f"/tenants/acme/campaigns/{ctx['cid']}/finance-verifications"
    h.refused(h.post(path, fv_body(ctx, h, approval_id="ooh-cap-" + "0" * 40), caller="finance_31"), 409,
              "CLIENT_APPROVAL_MISMATCH")
    h.refused(h.post(path, fv_body(ctx, h, currency="EUR"), caller="finance_31"), 409, "FUNDS_CURRENCY_MISMATCH")
    assert h.post(path, fv_body(ctx, h, verified_amount="0.00"), caller="finance_31").status_code == 422
    assert h.post(path, fv_body(ctx, h, cleared=False), caller="finance_31").status_code == 422
    h.finance_verify(ctx, amount="5999.99")                              # one cent short of 6000.00
    h.refused(h.authorize(ctx), 409, "FUNDS_NOT_FINANCE_VERIFIED")
    h.finance_verify(ctx, amount="6000.00")
    h.ok(h.authorize(ctx), 201)


def test_a_change_request_clears_the_verification(acme):
    h = acme
    ctx = h.funded()
    assert h.view("acme", ctx["cid"])["finance_verification"] is not None
    h.ok(h.go("acme", ctx["cid"], "CHANGE_REQUESTED", andre=True))
    assert h.view("acme", ctx["cid"])["finance_verification"] is None


def test_the_gate_rechecks_finance_at_submission(acme):
    h = acme
    ctx = h.authorized()
    h.svc.campaigns[ctx["cid"]]["finance_verification"] = None         # as if withdrawn (no route lowers it)
    h.refused(h.submit(ctx), 409, "FUNDS_NOT_FINANCE_VERIFIED")


def test_a_verification_before_client_approval_is_refused(acme):
    h = acme
    ctx = h.to_proposal_sent()
    body = {"request_id": rid(), "approval_id": "ooh-cap-" + "1" * 40, "verified_amount": "6000.00",
            "currency": "USD", "finance_ref": "fin31-early", "cleared": True}
    h.refused(h.post(f"/tenants/acme/campaigns/{ctx['cid']}/finance-verifications", body, caller="finance_31"), 409,
              "STATE_NOT_ELIGIBLE")


def test_finance_verification_replays_identically(hd):
    hd.tenant("acme")
    ctx = hd.funded()
    before = hd.svc.snapshot()
    h2 = hd.restart()
    assert h2.svc.snapshot() == before
    assert h2.view("acme", ctx["cid"])["finance_verification"]["finance_ref"].startswith("fin31-")
