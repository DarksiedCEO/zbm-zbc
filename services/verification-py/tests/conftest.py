import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
for p in (SRC, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# api.py refuses to start without VI_SERVICE_TOKEN (fail closed). Test-only values; the ledger env is
# deliberately unset (the module-level app gets the unconfigured ledger).
os.environ.setdefault("VI_SERVICE_TOKEN", "test-vi-service-token-do-not-use-0123")
for k in list(os.environ):
    if k.startswith("VI_") and k != "VI_SERVICE_TOKEN" or k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
        os.environ.pop(k, None)

from helpers import Harness  # noqa: E402


@pytest.fixture
def h():
    return Harness()


@pytest.fixture
def hr():
    """Harness with the rules seed approved by Andre."""
    x = Harness()
    x.approve_rules()
    return x


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No network in tests: any socket connect raises."""
    import socket

    def refuse(*a, **k):
        raise RuntimeError("network access attempted in a test (not allowed)")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
