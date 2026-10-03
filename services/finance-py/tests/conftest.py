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

# api.py refuses to start without FIN_SERVICE_TOKEN (fail closed). Test-only values; the ledger env is deliberately
# unset (the module-level app gets the unconfigured ledger).
os.environ.setdefault("FIN_SERVICE_TOKEN", "test-fin-service-token-do-not-use-0123456")
for k in list(os.environ):
    if k.startswith("FIN_") and k != "FIN_SERVICE_TOKEN" or k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
        os.environ.pop(k, None)

import helpers  # noqa: E402
from helpers import Harness  # noqa: E402

HARNESSES = helpers.HARNESSES


@pytest.fixture(autouse=True)
def _g9_books_balance():
    """G9: every journal entry of every harness a test built balances, and each entity's trial balance is 0.00."""
    HARNESSES.clear()
    yield
    from intelligences import i01_journal as J
    for x in list(HARNESSES):
        for e in x.svc.entries:
            d, c = J.totals(e)
            assert d == c and d > 0, e["entry_id"]
        for ent in ("zbc", "zbm"):
            assert J.trial_balance(x.svc.balances, ent)["difference"] == "0.00"
    HARNESSES.clear()


@pytest.fixture
def h():
    return Harness()


@pytest.fixture
def hr():
    """Harness with the rules seed approved, FIN-CQ-01 and FIN-CQ-11 verified (memo rows) and the access review
    attested."""
    return Harness().ready()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No network in tests: any socket connect raises."""
    import socket

    def refuse(*a, **k):
        raise RuntimeError("network access attempted in a test (not allowed)")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
