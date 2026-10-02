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
# Fix wave 25 (D1): always the test token, never one the shell exports (test_fix25_test_token.py).
os.environ["FULFILLMENT_SERVICE_TOKEN"] = TEST_SERVICE_TOKEN


import pytest  # noqa: E402


def child_env() -> dict[str, str]:
    """Fix wave 24, F5 (AEGIS N23-S-9): the bytecode settings of every Python
    child process a test starts (the live servers included) — merged into the
    environments the tests build from scratch. PYTHONDONTWRITEBYTECODE=1
    always (a test child never writes bytecode into the source tree);
    PYTHONPYCACHEPREFIX when the parent has one."""
    env = {"PYTHONDONTWRITEBYTECODE": "1"}
    if os.environ.get("PYTHONPYCACHEPREFIX"):
        env["PYTHONPYCACHEPREFIX"] = os.environ["PYTHONPYCACHEPREFIX"]
    return env


@pytest.fixture(autouse=True)
def _fresh_outbound_gate(monkeypatch):
    """Fix wave 1, F3: api._GATE keeps per-number attempt history for the
    life of the process (that is the point of it). Give every test its own
    gate so one test's calls don't count against another's numbers."""
    import api

    if hasattr(api, "_build_gate"):
        monkeypatch.setattr(api, "_GATE", api._build_gate())
    yield
