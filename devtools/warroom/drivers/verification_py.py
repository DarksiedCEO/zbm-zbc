"""War room driver: verification-py (Verification & Integrity, ADR 0007) through tests/helpers.py ``Harness``.

Each case: a fresh in-memory harness (every port a fake from tests/fakes.py, FakeLedgerClient, fixed clock) with the
rules seed approved by Andre (the ``hr`` fixture), then the case's steps: a clean clip registered and approved, a
second registration of the same post under a chaos URL; or connections; or a minor identity and a look-alike
email."""

from __future__ import annotations

from types import SimpleNamespace

H = None
AgeProviderAnswer = None


def prepare_env(env) -> None:
    env["VI_SERVICE_TOKEN"] = "test-vi-service-token-do-not-use-0123"    # always the test value, never one inherited
    for k in list(env):
        if k.startswith("VI_") and k != "VI_SERVICE_TOKEN" or k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
            env.pop(k, None)


def import_harness() -> None:
    global H, AgeProviderAnswer
    import helpers
    from ports import AgeProviderAnswer as A
    H, AgeProviderAnswer = helpers, A


def setup(tmp):
    h = H.Harness()
    h.approve_rules()
    return SimpleNamespace(h=h, steps=[], refs={})


def teardown(ctx) -> None:
    ctx.h.svc.close()
    H.HARNESSES.clear()


def _r(resp) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:2000]
    return {"status": resp.status_code, "body": body}


# ------------------------------------------------------------------------------------------------ actions

def clean_clip(ctx, sid="d1", clipper="clip-a"):
    ctx.refs[sid] = ctx.h.clean_clip(sid, clipper)


def onboard(ctx, clipper, platform="tiktok"):
    ctx.h.onboard(clipper, platform)


def register(ctx, sid, clipper, post_ref, campaign_id="camp-2"):
    return ctx.h.register(sid, clipper, post_ref=post_ref, campaign_id=campaign_id)


def connect(ctx, clipper, platform="tiktok", account_id=None, gap_minutes=0):
    if gap_minutes:
        ctx.h.clock.advance(minutes=int(gap_minutes))
    out = ctx.h.connect(clipper, platform, account_id=account_id)
    return {"status": 200, "body": out}


def minor_identity(ctx, clipper="kid", email="kid.name@gmail.com"):
    h = ctx.h
    h.connect(clipper)
    h.identity(clipper, email)
    h.age.answer = AgeProviderAnswer("minor", None, None, True, "p", "r")
    h.ok(h.age_check(clipper, dob="2012-01-01"))
    h.age.answer = AgeProviderAnswer("adult", None, None, True, "p", "r2")   # the provider now says "adult"


def identity_then_age(ctx, clipper, email):
    """The identity check, then the age check; an identity the service refuses (4xx) is reported as that refusal
    (no identity, no attestation: the look-alike got nowhere)."""
    h = ctx.h
    r = h.post("/vi/v1/identity/checks", {"request_id": H.rid("id"), "clipper_id": clipper, "email": email},
               caller="clipper_network")
    if r.status_code >= 400:
        return _r(r)
    return _r(h.age_check(clipper))


ACTIONS = {"clean_clip": clean_clip, "onboard": onboard, "register": lambda ctx, **kw: _r(register(ctx, **kw)),
           "connect": connect, "minor_identity": minor_identity, "identity_then_age": identity_then_age}


def observe(ctx) -> dict:
    h = ctx.h
    subs = getattr(h.svc, "submissions", {}) or {}
    return {"submissions": len(subs),
            "open_holds": sorted(f"{x['subject_kind']}:{x['subject_id']}" for x in h.ok(h.get("/vi/v1/holds"))
                                 if x["status"] == "open")}
