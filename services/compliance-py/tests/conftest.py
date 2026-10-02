import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
for p in (SRC, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# api.py refuses to start without COMPLIANCE_SERVICE_TOKEN (fail closed). Test-only
# values; the ledger env is deliberately unset (module-level app = unconfigured ledger).
# Fix wave 25 (D1): always the test token, never one the shell exports (test_fix25_test_token.py).
os.environ["COMPLIANCE_SERVICE_TOKEN"] = "test-compliance-service-token-do-not-use"
for k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN", "COMPLIANCE_CALLER_TOKENS", "COMPLIANCE_ANDRE_APPROVAL_TOKEN",
          "COMPLIANCE_DATA_DIR", "COMPLIANCE_WATCHER_ENABLED"):
    os.environ.pop(k, None)

from helpers import Harness  # noqa: E402


@pytest.fixture
def h():
    return Harness()


@pytest.fixture
def hs():
    """Harness with the seed approved and Compliance's internal controls run."""
    x = Harness()
    x.approve_seed()
    x.run_controls()
    return x


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Spec H.27: no network in tests. Any socket connect raises."""
    import socket

    def refuse(*a, **k):
        raise RuntimeError("network access attempted in a test (not allowed)")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
