"""War room driver: ooh-py (Out-of-Home & DOOH, ADR 0019) through tests/helpers.py ``Harness``.

Each case: a fresh in-memory, non-production harness (FakeLedger, fixed clock, every port NOT_CONNECTED: nothing can
be ordered, charged or sent) with one client tenant, then the case's campaign set-up (client-approved and unfunded,
or funded, or authorized) built through the API by Andre, then the case's attack. State is read back through the
service: the campaign's state and cash exposure, its authorizations, and the duplicate-order registry."""

from __future__ import annotations

from types import SimpleNamespace

H = None


def prepare_env(env) -> None:
    for k in list(env):
        if k.startswith("OOH_") or k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
            env.pop(k, None)


def import_harness() -> None:
    global H
    import helpers
    H = helpers


def setup(tmp):
    h = H.Harness(tmp)
    h.tenant("acme")
    return SimpleNamespace(h=h, steps=[], c=None)


def teardown(ctx) -> None:
    ctx.h.svc.close()


def approved_unfunded(ctx):
    ctx.c = ctx.h.to_proposal_sent()
    r = ctx.h.approve(ctx.c)
    v = ctx.h.view("acme", ctx.c["cid"])
    ctx.c.update(approval_id=v["client_approval"]["approval_id"], terms_id=v["commercial"]["terms_id"])
    return r


def funded(ctx):
    ctx.c = ctx.h.funded()
    return {"status": 200, "body": {"state": ctx.h.view("acme", ctx.c["cid"])["state"]}}


def authorized(ctx):
    ctx.c = ctx.h.authorized()
    return {"status": 201, "body": ctx.c["auth"]}


def vendor_text_quote(ctx, text):
    uid = ctx.h.unit("acme", ctx.c["cid"])
    return ctx.h.price("acme", ctx.c["cid"], "VENDOR_QUOTE", "1.00", vendor="lamar", inventory_ids=[uid],
                       valid_until=H.at(days=3), vendor_text=text)


def authorize(ctx, **over):
    return ctx.h.authorize(ctx.c, **over)


def authorize_as(ctx, caller):
    """The authorization body Andre would sign, sent by another caller (with no approval token)."""
    return ctx.h.post(f"/tenants/acme/campaigns/{ctx.c['cid']}/authorizations", ctx.h.auth_body(ctx.c),
                      caller=caller)


def submit(ctx):
    ctx.c["aid"] = ctx.c["auth"]["authorization_id"]
    return ctx.h.submit(ctx.c)


ACTIONS = {"approved_unfunded": approved_unfunded, "funded": funded, "authorized": authorized,
           "vendor_text_quote": vendor_text_quote, "authorize": authorize, "authorize_as": authorize_as,
           "submit": submit}


def observe(ctx) -> dict:
    h = ctx.h
    if ctx.c is None:
        return {}
    c = h.view("acme", ctx.c["cid"])
    auths = [h.svc.authorizations[a] for a in c["authorization_ids"]]
    return {"campaign_state": c["state"], "funding_state": c["exposure"]["funding_state"],
            "vendor_commitment": c["exposure"]["vendor_commitment"], "markup_pct": c["commercial"]["markup_pct"],
            "authorizations": len(auths), "live_authorizations": sum(1 for a in auths if a["status"] == "AUTHORIZED"),
            "orders": len(h.svc.orders),
            "funds_classes": sorted({f["evidence_class"] for f in c["funds"]})}
