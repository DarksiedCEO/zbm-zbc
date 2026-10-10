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
    if k.startswith("SEO_") or k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
        os.environ.pop(k, None)

from helpers import Harness  # noqa: E402


@pytest.fixture
def h(tmp_path):
    """In memory, non-production, fixed clock (T0), the fetcher pointed at nothing unless a test wires one."""
    return Harness(tmp_path)


@pytest.fixture
def hd(tmp_path):
    """As ``h`` with a durable data directory (restarts keep state)."""
    return Harness(tmp_path, data_dir=str(tmp_path / "data"))


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """No network in tests: any socket connect raises, except to 127.0.0.1 for a test that started a local fixture
    server (marked ``local_http``)."""
    import socket
    if request.node.get_closest_marker("local_http"):
        real_connect, real_connect_ex, real_create = socket.socket.connect, socket.socket.connect_ex, \
            socket.create_connection

        def only_loopback(fn):
            def wrapped(*a, **k):
                addr = a[1] if len(a) > 1 and isinstance(a[0], socket.socket) else a[0]
                if not (isinstance(addr, tuple) and addr[0] == "127.0.0.1"):
                    raise RuntimeError("network access attempted in a test (not allowed)")
                return fn(*a, **k)
            return wrapped
        monkeypatch.setattr(socket.socket, "connect", only_loopback(real_connect))
        monkeypatch.setattr(socket.socket, "connect_ex", only_loopback(real_connect_ex))
        monkeypatch.setattr(socket, "create_connection", only_loopback(real_create))
        return

    def refuse(*a, **k):
        raise RuntimeError("network access attempted in a test (not allowed)")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def pytest_configure(config):
    config.addinivalue_line("markers", "local_http: the test talks to an in-process HTTP fixture on 127.0.0.1")


@pytest.fixture
def srv():
    """The in-process HTTP fixture server (tests using it are marked ``local_http``)."""
    from fixture_server import FixtureServer
    s = FixtureServer()
    try:
        yield s
    finally:
        s.close()
