import os
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True
SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
for p in (SRC, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

for k in list(os.environ):
    if k.startswith("CFX_") or k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
        os.environ.pop(k, None)

from helpers import Harness  # noqa: E402


@pytest.fixture
def h(tmp_path):
    """In memory, non-production, fixed clock (T0), every port a recording fake (transport to recorded platforms)."""
    return Harness(tmp_path)


@pytest.fixture
def hd(tmp_path):
    """As ``h`` with a durable data directory (restarts keep state)."""
    return Harness(tmp_path, data_dir=str(tmp_path / "data"))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No network in tests: any socket connect raises (the shared live graceful-close modules install their own
    loopback-only guard on top, for the server they start on 127.0.0.1)."""
    import socket

    def refuse(*a, **k):
        raise RuntimeError("network access attempted in a test (not allowed)")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
