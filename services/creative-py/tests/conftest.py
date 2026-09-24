import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

# api.py fails closed (raises at import time) without CREATIVE_SERVICE_TOKEN.
# Fixed test-only values, never used outside tests. The ledger env vars are
# deliberately NOT set: the module-level app must default to "ledger not
# configured" and every test that needs a ledger injects a fake.
TEST_SERVICE_TOKEN = "test-shared-secret-do-not-use-in-production"
TEST_FOUNDER_TOKEN = "test-andre-approval-token-do-not-use"
os.environ.setdefault("CREATIVE_SERVICE_TOKEN", TEST_SERVICE_TOKEN)
os.environ.pop("LEDGER_SERVICE_URL", None)
os.environ.pop("LEDGER_SERVICE_TOKEN", None)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock():
    from shared.clock import FixedClock

    return FixedClock(NOW)


@pytest.fixture
def ledger():
    from shared.ledger import FakeLedgerClient

    return FakeLedgerClient()


@pytest.fixture
def registry():
    from shared.registry import seeded_registry

    return seeded_registry()


@pytest.fixture
def rights():
    from shared.rights import RightsRegistry

    return RightsRegistry()


@pytest.fixture
def actors():
    from shared.actors import ActorRegistry

    return ActorRegistry()


@pytest.fixture
def recorder(ledger):
    from shared.ledger import EvidenceRecorder

    return EvidenceRecorder(ledger)


class Api:
    """A TestClient bound to an app built with injected fakes."""

    def __init__(self, *, ledger, clock, departments=None, actors=None, founder_token=TEST_FOUNDER_TOKEN, **build_kw):
        from fastapi.testclient import TestClient

        from api import build_app

        self.ledger = ledger
        self.clock = clock
        self.app = build_app(service_token=TEST_SERVICE_TOKEN, ledger=ledger, founder_token=founder_token,
                             clock=clock, departments=departments, actors=actors, **build_kw)
        self.client = TestClient(self.app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})
        self.zbm = self.app.state.zbm
        self.zbc = self.app.state.zbc

    def post(self, path, json=None, andre=None, **kw):
        headers = {"X-Andre-Approval-Token": andre} if andre is not None else {}
        return self.client.post(path, json=json, headers=headers, **kw)

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def put(self, path, json=None, **kw):
        return self.client.put(path, json=json, **kw)


@pytest.fixture
def make_api(ledger, clock):
    def _make(**kw):
        return Api(ledger=kw.pop("ledger", ledger), clock=kw.pop("clock", clock), **kw)

    return _make


@pytest.fixture
def api(make_api):
    return make_api()
