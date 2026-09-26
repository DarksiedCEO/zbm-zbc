"""
DEVTOOLS ONLY — never the production entrypoint (that is ``cd src && python3 -m api``, where every dependency is
its fail-closed stand-in).

Runs verification-py with the real ledger, the real record-first plumbing, the real hardened launcher and the
real data directory, but with the TEST fakes from ``tests/fakes.py`` in place of the vault, the platform
adapters, the hasher, the media intake, the age provider, Compliance, Legal, Finance, People and Clipper
Network, and with a settable clock. That lets a live run walk a clip through 30+ simulated days in minutes.

Extra routes (service bearer required), mounted only here:
  POST /devtools/advance   {"days", "hours", "minutes"}       move the clock forward
  POST /devtools/account   {"platform", "account_id", "professional", "followers"}   next OAuth account
  POST /devtools/video     {"platform", "post_ref", "patch": {...}}   create/patch a fake platform video
  POST /devtools/age       {"result", "estimated_age_low", "card_kind", "dob_consistent"}   next provider answer
  GET  /devtools/now
The clock offset and the fakes' state are saved in VI_DEVTOOLS_STATE_FILE after every request, so a restart of
this server continues the same simulated world (that is how the live run's restart leg works).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve()
SRC = HERE.parents[1] / "src"
TESTS = HERE.parents[1] / "tests"
sys.path[:0] = [str(SRC), str(TESTS)]

import config as config_mod  # noqa: E402
import serve  # noqa: E402
from fakes import (FakeAdapter, FakeAgeProvider, FakeClipperNetwork, FakeCompliance, FakeFinance, FakeHasher,  # noqa: E402
                   FakeLegal, FakeMediaIntake, FakeOEmbed, FakePeople, FakeVault)
from fastapi import Body, Depends  # noqa: E402
from ports import AgeProviderAnswer  # noqa: E402
from service import Ports  # noqa: E402


class DevClock:
    def __init__(self, offset_s: float = 0.0):
        self.offset_s = offset_s

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self.offset_s)

    def today(self):
        return self.now().date()


def _import_api_inertly():
    saved = {k: os.environ.pop(k, None) for k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN", "VI_DATA_DIR")}
    try:
        import api
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    return api


def _save(path: Path, clock: DevClock, vault: FakeVault, adapters: dict) -> None:
    def vid(v):
        return {k: (x.hex() if isinstance(x, bytes) else x) for k, x in v.items()}
    doc = {"offset_s": clock.offset_s, "vault": {"n": vault.n, "refs": vault.refs},
           "adapters": {p: {"accounts": a.accounts, "queue": a.account_queue,
                            "videos": {r: vid(v) for r, v in a.videos.items()}} for p, a in adapters.items()}}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, path)


def _load(path: Path, clock: DevClock, vault: FakeVault, adapters: dict) -> None:
    if not path.exists():
        return
    doc = json.loads(path.read_text())
    clock.offset_s = doc["offset_s"]
    vault.n, vault.refs = doc["vault"]["n"], doc["vault"]["refs"]
    for p, st in doc["adapters"].items():
        a = adapters[p]
        a.accounts, a.account_queue = st["accounts"], st["queue"]
        a.videos = {r: {k: (bytes.fromhex(x) if k == "cover" else x) for k, x in v.items()} for r, v in st["videos"].items()}


def main() -> None:
    api = _import_api_inertly()
    settings = config_mod.load()
    state = Path(os.environ["VI_DEVTOOLS_STATE_FILE"])
    clock = DevClock()
    vault = FakeVault()
    adapters = {p: FakeAdapter(p) for p in ("youtube", "tiktok", "instagram", "x")}
    age = FakeAgeProvider()
    _load(state, clock, vault, adapters)
    ports = Ports(vault=vault, adapters=adapters, oembed=FakeOEmbed(), hasher=FakeHasher(), media=FakeMediaIntake(),
                  age=age, compliance=FakeCompliance(), legal=FakeLegal(), finance=FakeFinance(), people=FakePeople(),
                  clipper_network=FakeClipperNetwork())
    svc = api.build_service(settings, clock, ports)
    app = api.create_app(svc, settings)
    auth = Depends(api.make_require_auth(settings.service_token))

    @app.middleware("http")
    async def persist(request, call_next):
        resp = await call_next(request)
        _save(state, clock, vault, adapters)
        return resp

    @app.get("/devtools/now", dependencies=[auth])
    def now() -> dict:
        return {"now": clock.now().isoformat(), "offset_s": clock.offset_s}

    @app.post("/devtools/advance", dependencies=[auth])
    def advance(b: dict = Body(...)) -> dict:
        clock.offset_s += timedelta(days=b.get("days", 0), hours=b.get("hours", 0),
                                    minutes=b.get("minutes", 0)).total_seconds()
        return {"now": clock.now().isoformat()}

    @app.post("/devtools/account", dependencies=[auth])
    def account(b: dict = Body(...)) -> dict:
        adapters[b["platform"]].next_account(b["account_id"], b.get("professional", True), b.get("followers", 1000))
        return {"queued": b["account_id"]}

    @app.post("/devtools/video", dependencies=[auth])
    def video(b: dict = Body(...)) -> dict:
        a = adapters[b["platform"]]
        v = a.videos.setdefault(b["post_ref"], {"video_id": "vid-" + b["post_ref"][-10:],
                                                "create_time": int(clock.now().timestamp()),
                                                "values": {"views": 0, "likes": 0, "comments": 0, "shares": 0},
                                                "caption": "caption #ad", "cover": b"cover-" + b["post_ref"].encode()})
        patch = dict(b.get("patch") or {})
        if "values" in patch:
            v["values"].update(patch.pop("values"))
        v.update(patch)
        return {"post_ref": b["post_ref"], "values": v["values"]}

    @app.post("/devtools/age", dependencies=[auth])
    def age_answer(b: dict = Body(...)) -> dict:
        age.answer = AgeProviderAnswer(b.get("result", "adult"), b.get("estimated_age_low"), b.get("card_kind"),
                                       b.get("dob_consistent", True), "fake-age-provider", "prov-ref-live")
        return {"ok": True}

    serve.run(app, host=os.environ.get("VI_BIND_ADDR", "127.0.0.1"), port=int(os.environ["VI_PORT"]))


if __name__ == "__main__":
    main()
