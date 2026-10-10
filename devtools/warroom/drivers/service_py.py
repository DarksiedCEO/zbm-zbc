"""War room driver: service-py (Customer Service & Success, ADR 0014) through tests/helpers.py ``Harness``.

Each case: a fresh in-memory, non-production harness (fake ledger, fixed clock, the service's own default ports:
nothing is sent), the sender's contact with recorded email + SMS consent, a second contact ("client:other") whose
identifiers must never appear in a response, then the case's inbound message. State read back through the API
(``GET /svc/v1/contacts/{id}``) and the harness's alert record."""

from __future__ import annotations

from types import SimpleNamespace

SENDER_EMAIL, SENDER_PHONE = "owner@acme.test", "+13105551234"
OTHER = {"ref": "client:other", "email": "other.person@other.test", "phone": "+13105559876"}
H = None


def prepare_env(env) -> None:
    for k in list(env):
        if k.startswith("SVC_") or k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
            env.pop(k, None)


def import_harness() -> None:
    global H
    import helpers
    H = helpers


def setup(tmp):
    h = H.Harness(tmp)
    return SimpleNamespace(h=h, steps=[], cid=None, other=None)


def teardown(ctx) -> None:
    ctx.h.svc.close()


def _consents(h, cid) -> dict:
    return {c["channel"]: c["status"] for c in h.ok(h.get(f"/svc/v1/contacts/{cid}"))["consents"]}


# ------------------------------------------------------------------------------------------------ actions

def sender_with_consents(ctx, ref="client:acme"):
    h = ctx.h
    ctx.cid = h.contact(ref, email=SENDER_EMAIL, phone=SENDER_PHONE, timezone="America/Los_Angeles")
    h.ok(h.consent(ctx.cid, channel="email"), 201)
    h.ok(h.consent(ctx.cid, channel="sms"), 201)
    ctx.before = _consents(h, ctx.cid)


def other_contact(ctx):
    h = ctx.h
    ctx.other = h.contact(OTHER["ref"], email=OTHER["email"], phone=OTHER["phone"], timezone="America/New_York")
    h.ok(h.consent(ctx.other, channel="email"), 201)
    h.ok(h.consent(ctx.other, channel="sms"), 201)
    ctx.other_before = _consents(h, ctx.other)


def inbound_email(ctx, text, subject="Re: hello"):
    return ctx.h.email(text, frm=SENDER_EMAIL, subject=subject)


def inbound_sms(ctx, text):
    return ctx.h.sms(text, frm=SENDER_PHONE)


def inbound_chat(ctx, text, ref="client:acme"):
    return ctx.h.chat(text, ref=ref)


ACTIONS = {"sender_with_consents": sender_with_consents, "other_contact": other_contact,
           "inbound_email": inbound_email, "inbound_sms": inbound_sms, "inbound_chat": inbound_chat}


def observe(ctx) -> dict:
    h = ctx.h
    state = {"consents_before": getattr(ctx, "before", {}), "alert_codes": [], "foreign_identifiers": []}
    if ctx.cid:
        state["consents_after"] = _consents(h, ctx.cid)
    # one sender per case (the other contact never writes), so every alert raised is about this case's message
    state["alert_codes"] = sorted({a.get("code") for a in h.svc.alerts.values()})
    if ctx.other:
        state["other_before"] = ctx.other_before
        state["other_after"] = _consents(h, ctx.other)
        state["foreign_identifiers"] = [OTHER["email"], OTHER["phone"], OTHER["phone"][2:], ctx.other]
    return state
