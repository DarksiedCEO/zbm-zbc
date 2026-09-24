import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# api.py fails closed (raises at import time) if FULFILLMENT_SERVICE_TOKEN
# isn't set, so tests need a value in place before `from api import app`
# runs anywhere. Fixed test-only value, never used outside tests.
TEST_SERVICE_TOKEN = "test-shared-secret-do-not-use-in-production"
os.environ.setdefault("FULFILLMENT_SERVICE_TOKEN", TEST_SERVICE_TOKEN)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_outbound_gate(monkeypatch):
    """Fix wave 1, F3: api._GATE keeps per-number attempt history for the
    life of the process (that is the point of it). Give every test its own
    gate so one test's calls don't count against another's numbers."""
    import api

    if hasattr(api, "_build_gate"):
        monkeypatch.setattr(api, "_GATE", api._build_gate())
    yield
