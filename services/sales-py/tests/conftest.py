import os
import sys

sys.dont_write_bytecode = True
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
for p in (SRC, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

for k in list(os.environ):
    if k.startswith("SALES_") or k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
        os.environ.pop(k, None)

from helpers import Harness, wired_ports  # noqa: E402


@pytest.fixture
def h(tmp_path):
    """In memory, non-production, every port a stand-in (nothing wired)."""
    return Harness(tmp_path)


@pytest.fixture
def w(tmp_path):
    """As ``h`` with every port wired to a recording fake."""
    return Harness(tmp_path, ports=wired_ports())


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No network in tests: any socket connect raises."""
    import socket

    def refuse(*a, **k):
        raise RuntimeError("network access attempted in a test (not allowed)")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
