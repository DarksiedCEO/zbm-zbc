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
    config.addinivalue_line("markers", "sink_last: runs after every other test (it reads what the whole run wrote)")


# Sink jobs A-D: every record the suite commits is observed by the sink guard. ``SINK_SEEN`` keeps, for the whole run,
# the sinks whose records passed and, per test, the (sink, path) pairs the guard REFUSED. There is no legacy list any
# more (sink job D): tests/test_sink_guard.py reads this LAST and fails when a refusal happened anywhere but in a test
# that provokes one on purpose (``EXPECTED_SINK_REFUSALS`` there). Paths and sink names only: never a value.
SINK_SEEN = {"passed": set(), "violations": {}, "full_run": False, "test": "(outside a test)"}


def _sink_observer(kind, found, bad):
    if not bad:
        SINK_SEEN["passed"].add(kind)
    else:
        SINK_SEEN["violations"].setdefault(SINK_SEEN["test"], set()).update((kind, p) for p in bad)


@pytest.fixture(autouse=True)
def _sink_current_test(request):
    name = getattr(request.node, "originalname", None) or request.node.name
    SINK_SEEN["test"] = f"{Path(str(request.node.fspath)).name}::{name}"
    yield
    SINK_SEEN["test"] = "(outside a test)"


def _install_sink_observer():
    import sink_guard
    sink_guard.OBSERVER = _sink_observer


_install_sink_observer()


def pytest_collection_modifyitems(session, config, items):
    last = [i for i in items if i.get_closest_marker("sink_last")]
    if last:
        items[:] = [i for i in items if not i.get_closest_marker("sink_last")] + last
    files = {Path(str(i.fspath)).name for i in items}
    every = {p.name for p in TESTS.glob("test_*.py")}
    opt = config.option
    SINK_SEEN["full_run"] = bool(
        files >= every and not getattr(opt, "keyword", "") and not getattr(opt, "markexpr", "")
        and not getattr(opt, "lf", False) and not getattr(opt, "failedfirst", False)
        and not getattr(opt, "maxfail", 0) and not getattr(opt, "deselect", None)
        and not getattr(opt, "numprocesses", None) and not hasattr(config, "workerinput"))


@pytest.fixture
def srv():
    """The in-process HTTP fixture server (tests using it are marked ``local_http``)."""
    from fixture_server import FixtureServer
    s = FixtureServer()
    try:
        yield s
    finally:
        s.close()
