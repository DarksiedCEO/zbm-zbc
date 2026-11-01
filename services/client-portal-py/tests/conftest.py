import sys

# no import from here on writes bytecode (the hygiene wrapper sets PYTHONDONTWRITEBYTECODE as well)
sys.dont_write_bytecode = True
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
for p in (SRC, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """No network in tests (any socket connect raises), except tests marked ``live`` (they start the real launcher
    on 127.0.0.1 and talk to it)."""
    if request.node.get_closest_marker("live"):
        return
    import socket

    def refuse(*a, **k):
        raise RuntimeError("network access attempted in a test (not allowed)")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def pytest_configure(config):
    config.addinivalue_line("markers", "live: starts the real launcher on 127.0.0.1 (no outside network)")
