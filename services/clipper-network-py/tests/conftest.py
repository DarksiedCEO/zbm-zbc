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

# api.py refuses to start without CN_SERVICE_TOKEN / CN_IDENTITY_HMAC_KEY (fail closed). Test-only values; the
# ledger env is deliberately unset (the module-level app gets the unconfigured ledger: nothing can be recorded).
os.environ.setdefault("CN_SERVICE_TOKEN", "test-cn-service-token-do-not-use-0000")
os.environ.setdefault("CN_IDENTITY_HMAC_KEY", "test-cn-identity-hmac-key-do-not-use-000")
for k in list(os.environ):
    if k.startswith("CN_") and k not in ("CN_SERVICE_TOKEN", "CN_IDENTITY_HMAC_KEY"):
        os.environ.pop(k, None)
for k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
    os.environ.pop(k, None)

from helpers import Harness  # noqa: E402


@pytest.fixture
def h():
    return Harness()


@pytest.fixture
def hs():
    """Harness with the seed approved."""
    x = Harness()
    x.approve_seed()
    return x


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Spec §G: no network in tests. Any socket connect raises."""
    import socket

    def refuse(*a, **k):
        raise RuntimeError("network access attempted in a test (not allowed)")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
