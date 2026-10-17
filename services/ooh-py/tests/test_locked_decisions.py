"""The founder's locked decisions (Oct 16 2026 round 2) that the waves 0-1 code did not yet enforce: clients pay by
ACH only; the $10,000 single-purchase limit and the 7-day authorization are ceilings the environment can only lower;
principal vs agent stays UNDETERMINED, and purchases stay blocked, until a CPA or counsel confirms."""

from __future__ import annotations

import pytest

import config as config_mod
from helpers import Harness, at, base_env, rid


# ---------------------------------------------------------------------------------------------- limits

@pytest.mark.parametrize("name,value", [("OOH_MAX_PURCHASE_AMOUNT", "10000.01"), ("OOH_MAX_PURCHASE_AMOUNT", "25000.00"),
                                        ("OOH_AUTHORITY_MAX_DAYS", "8"), ("OOH_AUTHORITY_MAX_DAYS", "30")])
def test_the_environment_cannot_raise_the_locked_limits(name, value):
    with pytest.raises(RuntimeError, match=name):
        config_mod.load(base_env(**{name: value}))


def test_the_environment_may_lower_the_locked_limits():
    s = config_mod.load(base_env(OOH_MAX_PURCHASE_AMOUNT="2500.00", OOH_AUTHORITY_MAX_DAYS="3"))
    assert s.max_purchase_amount == "2500.00" and s.authority_max_days == 3
    s = config_mod.load(base_env())
    assert s.max_purchase_amount == "10000.00" and s.authority_max_days == 7


def test_an_authorization_longer_than_seven_days_is_refused(h):
    h.tenant("acme")
    ctx = h.funded()
    h.refused(h.authorize(ctx, expires_at=at(days=7, minutes=1)), 409, "AUTHORITY_TOO_LONG")
    h.ok(h.authorize(ctx, expires_at=at(days=7)), 201)


def test_status_shows_the_ceilings(h):
    s = h.ok(h.get("/status"))
    assert s["limits"]["max_purchase_ceiling"] == "10000.00" and s["limits"]["authority_days_ceiling"] == 7


# ---------------------------------------------------------------------------------------------- client funds: ACH only

@pytest.mark.parametrize("method", ["check", "card", "wire", "cash"])
def test_client_funds_are_ach_only(h, method):
    h.tenant("acme")
    ctx = h.to_proposal_sent()
    h.ok(h.approve(ctx))
    r = h.funds(ctx, ctx["sell"], method=method)
    assert r.status_code == 422, r.text
    assert h.view("acme", ctx["cid"])["funds"] == []


# ---------------------------------------------------------------------------------------------- principal / agent

def undetermined(h):
    h.tenant("acme")
    h.determine("UNDETERMINED", ref="withdrawn-for-test")
    return h


def test_without_a_confirmation_a_deal_cannot_be_principal_or_agent(h):
    undetermined(h)
    assert h.ok(h.get("/principal-agent"))["purchases_blocked"] is True
    cid = h.campaign()
    for pa in ("PRINCIPAL", "AGENT"):
        h.refused(h.commercial("acme", cid, pa=pa), 409, "PRINCIPAL_AGENT_NOT_CONFIRMED")
    c = h.ok(h.commercial("acme", cid, pa="UNDETERMINED"))
    assert c["commercial"]["principal_agent_state"] == "UNDETERMINED"


@pytest.mark.parametrize("caller", ["ooh_agent", "dashboard", "compliance_38", "scheduler"])
def test_only_andre_records_the_confirmation(h, caller):
    undetermined(h)
    r = h.post("/principal-agent-determinations", {"request_id": rid(), "determination": "PRINCIPAL",
                                                   "confirmed_by": "COUNSEL", "confirmation_ref": "memo-1"},
               caller=caller)
    assert r.status_code == 403
    assert h.ok(h.get("/principal-agent"))["principal_agent_state"] == "UNDETERMINED"


def test_the_confirmation_names_a_cpa_or_counsel_and_a_reference(h):
    undetermined(h)
    for bad in ({"confirmed_by": "ANDRE"}, {"confirmed_by": "AGENT_RUNTIME"}, {"confirmation_ref": ""}):
        body = {"request_id": rid(), "determination": "PRINCIPAL", "confirmed_by": "CPA", "confirmation_ref": "m-1",
                **bad}
        assert h.post("/principal-agent-determinations", body, andre=True).status_code == 422, bad
    v = h.determine("PRINCIPAL", confirmed_by="CPA", ref="cpa-letter-7")
    assert v["determination"]["confirmed_by"] == "CPA" and v["determination"]["recorded_by"] == "andre"
    assert v["purchases_blocked"] is False
    assert h.ledger.of_type("principal_agent_determination_recorded_by_andre")


def test_a_deal_follows_the_confirmed_determination_only(h):
    h.tenant("acme")
    h.determine("AGENT", confirmed_by="COUNSEL", ref="counsel-agent-1")
    cid = h.campaign()
    h.refused(h.commercial("acme", cid, pa="PRINCIPAL"), 409, "PRINCIPAL_AGENT_NOT_CONFIRMED")
    assert h.ok(h.commercial("acme", cid, pa="AGENT"))["commercial"]["principal_agent_state"] == "AGENT"


def test_a_withdrawn_confirmation_blocks_authorization_and_submission(h):
    h.tenant("acme")
    ctx = h.authorized()                                 # bought as PRINCIPAL under counsel's confirmation
    h.determine("UNDETERMINED", ref="counsel-withdrew")
    h.refused(h.submit(ctx), 409, "PRINCIPAL_AGENT_NOT_CONFIRMED")
    h.refused(h.authorize(ctx), 409, "PRINCIPAL_AGENT_NOT_CONFIRMED")
    h.determine("AGENT", ref="counsel-changed")          # a changed determination blocks too
    h.refused(h.submit(ctx), 409, "PRINCIPAL_AGENT_NOT_CONFIRMED")
    h.determine("PRINCIPAL", ref="counsel-restored")
    h.ok(h.submit(ctx))


def test_undetermined_campaigns_never_reach_a_purchase(h):
    undetermined(h)
    cid = h.campaign()
    h.plan("acme", cid)
    uid, qid = h.held_unit("acme", cid)
    h.ok(h.go("acme", cid, "AVAILS_REQUESTED"))
    h.ok(h.commercial("acme", cid, pa="UNDETERMINED"))
    sp = h.ok(h.price("acme", cid, "CLIENT_SELL_PRICE", "6000.00", andre=True, quote_refs=[qid]), 201)
    h.ok(h.price("acme", cid, "APPROVED_BUY_LIMIT", "5000.00", andre=True), 201)
    h.ok(h.go("acme", cid, "QUOTES_VALIDATED"))
    h.ok(h.go("acme", cid, "PROPOSAL_SENT", andre=True))
    x = {"tid": "acme", "cid": cid, "uid": uid, "qid": qid, "sell_id": sp["price_id"], "sell": "6000.00",
         "amount": "5000.00"}
    h.ok(h.approve(x))
    h.ok(h.funds(x, "6000.00"))
    v = h.view("acme", cid)
    x.update(approval_id=v["client_approval"]["approval_id"], terms_id=v["commercial"]["terms_id"])
    h.refused(h.authorize(x), 409, "PRINCIPAL_AGENT_UNDETERMINED")
    h.determine("PRINCIPAL", ref="counsel-late")         # a later confirmation does not rewrite this deal's model
    h.refused(h.authorize(x), 409, "PRINCIPAL_AGENT_UNDETERMINED")


def test_the_determination_replays_identically(hd):
    hd.tenant("acme")
    hd.determine("AGENT", confirmed_by="CPA", ref="cpa-2")
    before = hd.svc.snapshot()
    h2 = hd.restart()
    assert h2.svc.snapshot() == before and h2.svc.pa_determination["determination"] == "AGENT"
    h2.determine("UNDETERMINED", ref="withdrawn")
    h3 = h2.restart()
    assert h3.svc.pa_determination is None


def test_a_fresh_service_starts_undetermined(tmp_path):
    h = Harness(tmp_path)
    v = h.ok(h.get("/principal-agent"))
    assert v == {"principal_agent_state": "UNDETERMINED", "determination": None, "purchases_blocked": True}
