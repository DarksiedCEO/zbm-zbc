"""War room driver: onboarding-py (Onboarding, ADR 0004) through tests/conftest.py ``make_service`` / ``client_for``
(onboarding-py keeps its harness in conftest.py) and the application body of tests/test_sweep_d.py ``_app``.

Each case: a fresh in-memory service with every department a passing fake (``all_fakes=True``: nothing is paid,
filed or sent) and a FakeLedgerClient, then the case's applications and payments. State: the 1099 running total kept
for the first applicant's person key, and the payment events in the fake ledger."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

C = None
APP = None


def prepare_env(env) -> None:
    for k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN", "DETECTION_SERVICE_URL", "DETECTION_SERVICE_TOKEN"):
        env.pop(k, None)


def import_harness() -> None:
    global C, APP
    import conftest
    from test_sweep_d import _app
    C, APP = conftest, _app


def setup(tmp):
    svc = C.make_service(all_fakes=True)
    return SimpleNamespace(svc=svc, c=C.client_for(svc), steps=[], first_key=None)


def teardown(ctx) -> None:
    close = getattr(ctx.svc, "close", None)
    if close:
        close()


def application(ctx, creator_id, legal_name):
    r = ctx.c.post("/zbc/creators/applications", json=APP(creator_id=creator_id, legal_name=legal_name))
    if ctx.first_key is None and r.status_code == 201:
        ctx.first_key = ctx.svc._creator(creator_id).person_key
    return r


def payment(ctx, creator_id, request_id, amount_usd):
    return ctx.c.post(f"/zbc/creators/{creator_id}/payments", json={"request_id": request_id,
                                                                     "amount_usd": amount_usd})


def payment_repeat(ctx, creator_id, request_id, amount_usd, times):
    r = None
    for _ in range(int(times)):
        r = payment(ctx, creator_id, request_id, amount_usd)
    return r


ACTIONS = {"application": application, "payment": payment, "payment_repeat": payment_repeat}


def observe(ctx) -> dict:
    paid = sum((v for (k, _y), v in ctx.svc._paid.items() if k == ctx.first_key), Decimal("0.00"))
    events = [e for e in ctx.svc.ledger.events if e.get("event_type") == "creator_payment_tracked"]
    return {"first_person_total_usd": f"{paid:.2f}", "payment_events": len(events),
            "person_keys": len({k for (k, _y) in ctx.svc._paid})}
