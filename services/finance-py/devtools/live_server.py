"""
DEVTOOLS ONLY — never the production entrypoint (that is ``cd src && python3 -m api``, where every dependency is its
fail-closed stand-in).

Runs finance-py with the real ledger, the real record-first plumbing, the real hardened launcher and a real data
directory, but with the TEST fakes from ``tests/fakes.py`` in place of V&I, Compliance, Clipper Network, Legal, the
rails, the bank, the tax agent, the GL, the vault, People and push, and with a settable clock — so a live run can walk
money through a prepayment, a certification, a weekly payout, a release delay and a later clawback in minutes.

Extra routes (service bearer required), mounted only here:
  POST /devtools/advance            {"days", "hours", "minutes"}           move the clock forward
  POST /devtools/vi/certify         {"submission_id", "clipper_id", "campaign_id", "views", "create_time"}
  POST /devtools/vi/clawback        {"submission_id", "views_delta"}
  POST /devtools/compliance/rule    {"ruling_id", "subject_id"}
  POST /devtools/bank/deposit       {"entity", "account", "amount"}      the bank side of a receipt
  POST /devtools/rail/paid          {"rail", "idempotency_key"}          the rail pays (funds leave the platform)
  GET  /devtools/now
The fakes and the clock offset are pickled to FIN_DEVTOOLS_STATE_FILE after every request, so a restart of this
server continues the same simulated world (that is how the live run's restart leg works).
"""

from __future__ import annotations

import os
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve()
SRC = HERE.parents[1] / "src"
TESTS = HERE.parents[1] / "tests"
sys.path[:0] = [str(SRC), str(TESTS)]

import config as config_mod  # noqa: E402
import serve  # noqa: E402
from fastapi import Body, Depends  # noqa: E402
from ports import Ports  # noqa: E402


class DevClock:
    def __init__(self, offset_s: float = 0.0):
        self.offset_s = offset_s

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self.offset_s)

    def today(self):
        return self.now().date()


def _import_api_inertly():
    saved = {k: os.environ.pop(k, None) for k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN", "FIN_DATA_DIR")}
    try:
        import api
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    return api


def main() -> None:
    api = _import_api_inertly()              # before helpers (which imports api): the module-level app stays inert
    from helpers import make_fakes
    settings = config_mod.load()
    state = Path(os.environ["FIN_DEVTOOLS_STATE_FILE"])
    if state.exists():
        world = pickle.loads(state.read_bytes())
    else:
        clock = DevClock()
        world = {"clock": clock, "f": make_fakes(clock)}
        world["f"]["rails"]["trolley"].balance_available = False
    clock, f = world["clock"], world["f"]
    ports = Ports(vi=f["vi"], compliance=f["compliance"], cn=f["cn"], legal=f["legal"], rails=f["rails"],
                  bank=f["bank"], tax=f["tax"], gl=f["gl"], vault=f["vault"], people=f["people"], push=f["push"],
                  client_mail=f["client_mail"])
    svc = api.build_service(settings, clock, ports)
    app = api.create_app(svc, settings)
    auth = Depends(api.make_require_auth(settings.service_token))

    @app.middleware("http")
    async def persist(request, call_next):
        resp = await call_next(request)
        tmp = state.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps(world))
        os.replace(tmp, state)
        return resp

    @app.get("/devtools/now", dependencies=[auth])
    def now() -> dict:
        return {"now": clock.now().isoformat(), "offset_s": clock.offset_s}

    @app.post("/devtools/advance", dependencies=[auth])
    def advance(b: dict = Body(...)) -> dict:
        clock.offset_s += timedelta(days=b.get("days", 0), hours=b.get("hours", 0),
                                    minutes=b.get("minutes", 0)).total_seconds()
        return {"now": clock.now().isoformat()}

    @app.post("/devtools/vi/certify", dependencies=[auth])
    def certify(b: dict = Body(...)) -> dict:
        c = f["vi"].certify(b["submission_id"], b["clipper_id"], b["campaign_id"], int(b["views"]), b["create_time"],
                            certified_at=clock.now().replace(microsecond=0).isoformat().replace("+00:00", "Z"))
        return {"certification_id": c["certification_id"], "status": c["status"]}

    @app.post("/devtools/vi/clawback", dependencies=[auth])
    def clawback(b: dict = Body(...)) -> dict:
        return f["vi"].add_clawback(b["submission_id"], int(b["views_delta"]))

    @app.post("/devtools/compliance/rule", dependencies=[auth])
    def rule(b: dict = Body(...)) -> dict:
        f["compliance"].rule(b["ruling_id"], b["subject_id"])
        return {"ruling_id": b["ruling_id"]}

    @app.post("/devtools/bank/deposit", dependencies=[auth])
    def deposit(b: dict = Body(...)) -> dict:
        f["bank"].deposit(b["entity"], b["account"], b["amount"])
        return {"ok": True}

    @app.post("/devtools/rail/paid", dependencies=[auth])
    def rail_paid(b: dict = Body(...)) -> dict:
        p = f["rails"][b.get("rail", "stripe")].paid(b["idempotency_key"])
        return {"rail_ref": p["rail_ref"], "status": p["status"]}

    serve.run(app, host=os.environ.get("FIN_BIND_ADDR", "127.0.0.1"), port=int(os.environ["FIN_PORT"]))


if __name__ == "__main__":
    main()
