"""War room driver: clipper-network-py (Clipper Network, ADR 0008) through tests/helpers.py ``Harness``.

Each case: a fresh in-memory harness (passing fakes for every port, FakeLedgerClient, fixed clock) with the rules
seed approved by Andre (the ``hs`` fixture), then an application whose display name is the case's chaos value."""

from __future__ import annotations

import itertools
from types import SimpleNamespace

H = None
_n = itertools.count(1)


def prepare_env(env) -> None:
    # the war room's own test values, always: a token or key already in the environment (a real one) is never used
    env["CN_SERVICE_TOKEN"] = "test-cn-service-token-do-not-use-0000"
    env["CN_IDENTITY_HMAC_KEY"] = "test-cn-identity-hmac-key-do-not-use-000"
    for k in list(env):
        if k.startswith("CN_") and k not in ("CN_SERVICE_TOKEN", "CN_IDENTITY_HMAC_KEY"):
            env.pop(k, None)
    for k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
        env.pop(k, None)


def import_harness() -> None:
    global H
    import helpers
    H = helpers


def setup(tmp):
    h = H.Harness()
    h.approve_seed()
    return SimpleNamespace(h=h, steps=[])


def teardown(ctx) -> None:
    ctx.h.svc.close()
    H.HARNESSES.clear()


def apply(ctx, display_name):
    return ctx.h.apply(f"applicant{next(_n)}@example.com", display_name=display_name)


ACTIONS = {"apply": apply}


def observe(ctx) -> dict:
    return {}
