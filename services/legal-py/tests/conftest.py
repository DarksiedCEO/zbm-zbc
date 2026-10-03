import os
import sys

# wave 26b (E-B XS-PYC): this suite imports other services' code by file path (and their src through sys.path);
# without PYTHONDONTWRITEBYTECODE that wrote __pycache__/ into compliance-py, creative-py, onboarding-py and
# verification-py. No import from here on writes bytecode (the hygiene wrapper sets the variable as well).
sys.dont_write_bytecode = True
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
for p in (SRC, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# api.py refuses to start without LEGAL_SERVICE_TOKEN (fail closed). Test-only values; the ledger env is
# deliberately unset (the module-level app gets the unconfigured ledger).
os.environ.setdefault("LEGAL_SERVICE_TOKEN", "test-legal-service-token-do-not-use-0123")
for k in list(os.environ):
    if k.startswith("LEGAL_") and k != "LEGAL_SERVICE_TOKEN" or k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
        os.environ.pop(k, None)

from helpers import Harness  # noqa: E402


@pytest.fixture
def h():
    return Harness()


@pytest.fixture
def hr():
    """Harness with the Legal rules seed approved by Andre."""
    x = Harness()
    x.approve_rules()
    return x


@pytest.fixture
def he():
    """Rules approved and counsel engaged (an approved engagement letter with the AI clause)."""
    x = Harness()
    x.approve_rules()
    x.engage()
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
