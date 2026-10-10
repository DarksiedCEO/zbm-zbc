"""War room driver: sales-py (Sales & Lead Generation, ADR 0013) through tests/helpers.py ``Harness``.

Each case: a fresh in-memory, non-production harness with every port a recording fake (``wired_ports``: nothing is
sent), the replying lead (verified, email + phone), a second lead whose identifiers must never appear in a response,
then the case's reply on the case's channel. State read back through ``GET /sales/v1/contacts/{id}`` and the
open-task queue."""

from __future__ import annotations

from types import SimpleNamespace

SENDER_EMAIL, SENDER_PHONE = "jane@acme-shop.test", "+13105550100"
OTHER_EMAIL, OTHER_PHONE = "olivia@other-shop.test", "+13105550199"
H = None


def prepare_env(env) -> None:
    for k in list(env):
        if k.startswith("SALES_") or k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
            env.pop(k, None)


def import_harness() -> None:
    global H
    import helpers
    H = helpers


def setup(tmp):
    return SimpleNamespace(h=H.Harness(tmp, ports=H.wired_ports()), steps=[], lead=None, other=None)


def teardown(ctx) -> None:
    close = getattr(ctx.h.svc, "close", None)
    if close:
        close()


def replying_lead(ctx):
    ctx.lead = ctx.h.vlead(email=SENDER_EMAIL, phone=SENDER_PHONE)
    ctx.h.ok(ctx.h.consent(ctx.lead["contact_id"]), 201)


def other_lead(ctx):
    ctx.other = ctx.h.vlead(email=OTHER_EMAIL, phone=OTHER_PHONE,
                            account={"name": "Other Shop", "domain": "other-shop.test", "industry": "ecommerce",
                                     "employees_band": "11-50", "revenue_band": "1m_10m"})


def reply(ctx, text, channel="email"):
    body = {"request_id": H.rid(), "channel": channel, "text": text}
    if channel == "email":
        body["from_email"] = SENDER_EMAIL
    else:
        body["from_phone"] = SENDER_PHONE
    return ctx.h.post("/sales/v1/replies", body, caller="provider_events")


ACTIONS = {"replying_lead": replying_lead, "other_lead": other_lead, "reply": reply}


def observe(ctx) -> dict:
    h = ctx.h
    state: dict = {"foreign_identifiers": []}
    if ctx.lead:
        c = h.ok(h.get(f"/sales/v1/contacts/{ctx.lead['contact_id']}"))
        state["email_suppressed"] = bool(c.get("email_suppressed"))
        state["phone_suppressed"] = bool(c.get("phone_suppressed"))
        state["open_tasks"] = sorted(t["kind"] for t in h.ok(h.get("/sales/v1/tasks?status=open")))
    if ctx.other:
        o = h.ok(h.get(f"/sales/v1/contacts/{ctx.other['contact_id']}"))
        state["other_suppressed"] = bool(o.get("email_suppressed") or o.get("phone_suppressed"))
        state["foreign_identifiers"] = [OTHER_EMAIL, OTHER_PHONE, OTHER_PHONE[2:], ctx.other["contact_id"]]
    return state
