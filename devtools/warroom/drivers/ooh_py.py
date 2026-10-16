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


def submitted(ctx):
    authorized(ctx)
    return submit(ctx)


def funded_sibling(ctx):
    """Campaign A funded by transfer "boa-ach-12345"; campaign B (the case's target) client-approved, unfunded."""
    a = ctx.h.to_proposal_sent()
    ctx.h.ok(ctx.h.approve(a))
    ctx.h.ok(ctx.h.funds(a, a["sell"], ref="boa-ach-12345"))
    return approved_unfunded(ctx)


def fund(ctx, ref):
    return ctx.h.funds(ctx.c, ctx.c["sell"], ref=ref)


def fund_amount(ctx, amount):
    return ctx.h.funds(ctx.c, amount)


def two_quotes_one_unit(ctx):
    """One held unit with two vendor quotes; returns the attempt to price both in one sell price. The campaign is then
    taken, on the first quote only, to FUNDS_VERIFIED."""
    h = ctx.h
    cid = h.campaign()
    h.plan("acme", cid)
    uid, q1 = h.held_unit("acme", cid, amount="2500.00")
    q2 = h.quote("acme", cid, [uid], "2500.00")
    h.ok(h.go("acme", cid, "AVAILS_REQUESTED"))
    h.ok(h.commercial("acme", cid, "20.00"))
    both = h.price("acme", cid, "CLIENT_SELL_PRICE", "6000.00", andre=True, quote_refs=[q1, q2])
    sp = h.ok(h.price("acme", cid, "CLIENT_SELL_PRICE", "3000.00", andre=True, quote_refs=[q1]), 201)
    h.ok(h.price("acme", cid, "APPROVED_BUY_LIMIT", "5000.00", andre=True), 201)
    h.ok(h.go("acme", cid, "QUOTES_VALIDATED"))
    h.ok(h.go("acme", cid, "PROPOSAL_SENT", andre=True))
    ctx.c = {"tid": "acme", "cid": cid, "uid": uid, "qid": q1, "q2": q2, "sell_id": sp["price_id"], "sell": "3000.00",
             "amount": "2500.00"}
    h.ok(h.approve(ctx.c))
    h.ok(h.funds(ctx.c, "6000.00"))
    v = h.view("acme", cid)
    ctx.c.update(approval_id=v["client_approval"]["approval_id"], terms_id=v["commercial"]["terms_id"])
    return both


def authorize_both_quotes(ctx):
    return ctx.h.authorize(ctx.c, exact_quote_refs=[ctx.c["qid"], ctx.c["q2"]], authorized_amount="5000.00")


def face_registered(ctx):
    """Face lamar/face-101 registered on campaign A; campaign B is the case's target."""
    h = ctx.h
    a = h.campaign()
    r = h.post(f"/tenants/acme/campaigns/{a}/inventory",
               {"request_id": H.rid(), "vendor": "lamar", "kind": "static", "market": "los-angeles",
                "vendor_unit_ref": "face-101"})
    ctx.c = {"tid": "acme", "cid": h.campaign()}
    return r


def add_face(ctx, ref):
    return ctx.h.post(f"/tenants/acme/campaigns/{ctx.c['cid']}/inventory",
                      {"request_id": H.rid(), "vendor": "lamar", "kind": "static", "market": "los-angeles",
                       "vendor_unit_ref": ref})


def dispute(ctx):
    return ctx.h.go("acme", ctx.c["cid"], "DISPUTED")


def outcome(ctx, outcome):
    return ctx.h.outcome(ctx.c, outcome)


ACTIONS = {"approved_unfunded": approved_unfunded, "funded": funded, "authorized": authorized,
           "vendor_text_quote": vendor_text_quote, "authorize": authorize, "authorize_as": authorize_as,
           "submit": submit, "submitted": submitted, "funded_sibling": funded_sibling, "fund": fund, "fund_amount": fund_amount,
           "two_quotes_one_unit": two_quotes_one_unit, "authorize_both_quotes": authorize_both_quotes,
           "face_registered": face_registered, "add_face": add_face, "dispute": dispute, "outcome": outcome}


def observe(ctx) -> dict:
    h = ctx.h
    if ctx.c is None:
        return {}
    c = h.view("acme", ctx.c["cid"])
    auths = [h.svc.authorizations[a] for a in c["authorization_ids"]]
    return {"campaign_state": c["state"], "resume_to": c["resume_to"],
            "funding_state": c["exposure"]["funding_state"], "funds_count": len(c["funds"]),
            "vendor_commitment": c["exposure"]["vendor_commitment"],
            "markup_pct": (c["commercial"] or {}).get("markup_pct"),
            "authorizations": len(auths), "live_authorizations": sum(1 for a in auths if a["status"] == "AUTHORIZED"),
            "orders": len(h.svc.orders),
            "lamar_faces": sum(1 for u in h.svc.inventory.values() if u["vendor"] == "lamar"
                               and u["state"] != "WITHDRAWN"),
            "funds_classes": sorted({f["evidence_class"] for f in c["funds"]})}
