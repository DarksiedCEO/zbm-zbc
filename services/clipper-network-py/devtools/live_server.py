"""
DEVTOOLS ONLY — never the production entrypoint (that is ``cd src && python3 -m api``).

Runs clipper-network-py exactly as ``api.main()`` does (settings from the
environment, the hardened launcher, the real ledger over HTTP, a disk log),
except that the department ports are the TEST fakes from tests/fakes.py,
driven by a FIXTURE file (``$CN_FIXTURE_FILE``, JSON, re-read on every call)
so a live run can show admission → tiers → enrolment → kit → strike →
suspension → ban → offboarding deterministically and without any network:

  {"clock_offset_days": 0,                                   # added to the system clock
   "certs": {"<clipper_id>": [[cert_id, submission, campaign, platform, status, views], ...]},
   "strikes": [{"clipper_id", "class", "n"}],                # V&I strikes with resolvable evidence
   "finance_open_items": "none"}

The fakes live in tests/ (guardrail G3: no passing fake in src/).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1] / "src"), str(HERE.parents[1] / "tests")]

import config as config_mod  # noqa: E402
import serve  # noqa: E402
from fakes import (FakeHub, FakeMessaging, FakePush, PassingCompliance, PassingCreative, PassingFinance,  # noqa: E402
                   PassingLegal, PassingPeople, PassingVI)
from ports import Certification, Ports  # noqa: E402

FIXTURE = Path(os.environ.get("CN_FIXTURE_FILE", "/nonexistent"))


def fixture() -> dict:
    try:
        return json.loads(FIXTURE.read_bytes())
    except (OSError, ValueError):
        return {}


@dataclass
class OffsetClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(days=fixture().get("clock_offset_days", 0))

    def today(self):
        return self.now().date()


class FixtureVI(PassingVI):
    def certifications(self, clipper_id):
        self.certs[clipper_id] = [Certification(*c) for c in fixture().get("certs", {}).get(clipper_id, [])]
        return super().certifications(clipper_id)

    def strikes(self, cursor):
        for s in fixture().get("strikes", []):
            self.add_strike(s["clipper_id"], s["class"], n=s.get("n", 1))
        return super().strikes(cursor)


class FixtureFinance(PassingFinance):
    def open_items(self, clipper_id):
        self.open_state = fixture().get("finance_open_items", "none")
        return super().open_items(clipper_id)


def _import_api_inertly():
    saved = {k: os.environ.pop(k, None) for k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN", "CN_DATA_DIR")}
    try:
        import api
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    return api


def main() -> None:
    api = _import_api_inertly()
    settings = config_mod.load()
    clock = OffsetClock()
    ports = Ports(vi=FixtureVI(clock.today()), compliance=PassingCompliance(), creative=PassingCreative(),
                  finance=FixtureFinance(), legal=PassingLegal(), people=PassingPeople(), messaging=FakeMessaging(),
                  hub=FakeHub(), push=FakePush())
    svc = api.build_service(settings, clock, ports)
    app = api.create_app(svc, settings)
    serve.run(app, host=os.environ.get("CN_BIND_ADDR", "127.0.0.1"), port=int(os.environ["CN_PORT"]))


if __name__ == "__main__":
    main()
