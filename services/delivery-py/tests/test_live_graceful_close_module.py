"""Fix wave 22 (lead rulings G5/G6; AEGIS N21-C-1, N21-C-3): the graceful-close module shared by the ten Python
services. This file is byte-identical in every service's tests/ (it finds its own service's module and protocol).

  - the module is THE shared one: its sha256 is pinned here, and every sibling copy this checkout holds is identical;
  - reads are bounded: one buffer per event-loop thread; a read hands the parser at most READ_BUFFER_BYTES, and a
    drained read allocates nothing;
  - the concurrency slot is given back before the drain (before: a connection answered with Connection: close held
    its slot for the whole drain — up to 1 s — and the next request got uvicorn's 503);
  - at most <PREFIX>_DRAINS_MAX drains at once; past the cap an answered socket is closed at once;
  - the drain count is bookkept (a drain that ends leaves the set).
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import re
import socket
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

import pytest

PINNED_SHA256 = "c41d0a484f5c54cd9e38f60a139d850593f775d3652886860874f5bbc213cea3"
SERVICE = Path(__file__).resolve().parents[1]
SRC = SERVICE / "src"
SERVICES = SERVICE.parent
_COPY = next(p for p in (SRC / "graceful_close.py", SRC / "zbm_delivery" / "graceful_close.py") if p.exists())
_IMPORT = "graceful_close" if _COPY.parent == SRC else "zbm_delivery.graceful_close"
_SERVE = next((m for m, f in (("zbm_delivery.serve", SRC / "zbm_delivery" / "serve.py"), ("http_limits", SRC / "http_limits.py"),
                              ("serve", SRC / "serve.py")) if f.exists()))
_SERVE_FILE = {"zbm_delivery.serve": SRC / "zbm_delivery" / "serve.py", "http_limits": SRC / "http_limits.py",
               "serve": SRC / "serve.py"}[_SERVE]
_PROTOCOL = re.search(r"^class (_?\w+)\(GracefulCloseMixin, H11Protocol\):", _SERVE_FILE.read_text(), re.M).group(1)
_ENV = re.search(r'drains_max_from_env\("([A-Z_]+)"\)', _SERVE_FILE.read_text()).group(1)

_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_CREATE = socket.create_connection


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch):
    """This module talks to a server it started on 127.0.0.1; the suite's no-network rule stays in force for
    every other address."""
    def only_loopback(fn):
        def wrapped(*a, **k):
            addr = a[1] if len(a) > 1 and isinstance(a[0], socket.socket) else a[0]
            if not (isinstance(addr, tuple) and addr[0] == "127.0.0.1"):
                raise RuntimeError("network access attempted in a test (not allowed)")
            return fn(*a, **k)
        return wrapped
    monkeypatch.setattr(socket.socket, "connect", only_loopback(_REAL_CONNECT))
    monkeypatch.setattr(socket.socket, "connect_ex", only_loopback(_REAL_CONNECT_EX))
    monkeypatch.setattr(socket, "create_connection", only_loopback(_REAL_CREATE))


def _module():
    sys.path.insert(0, str(SRC))
    try:
        return importlib.import_module(_IMPORT)
    finally:
        sys.path.remove(str(SRC))


# ====================================================================== one module, pinned

def test_the_module_is_the_shared_one_pinned_and_identical_to_its_siblings():
    digest = hashlib.sha256(_COPY.read_bytes()).hexdigest()
    assert digest == PINNED_SHA256, f"{_COPY} differs from the pinned shared module"
    siblings = sorted(SERVICES.glob("*/src/graceful_close.py")) + sorted(SERVICES.glob("*/src/*/graceful_close.py"))
    assert _COPY in siblings
    differ = [str(p) for p in siblings if hashlib.sha256(p.read_bytes()).hexdigest() != digest]
    assert differ == [], differ
    this = (SERVICE / "tests" / Path(__file__).name).read_bytes()
    for other in SERVICES.glob(f"*/tests/{Path(__file__).name}"):
        assert other.read_bytes() == this, f"{other} is not byte-identical to this test"


def test_the_drain_cap_is_configured_from_the_services_env_and_refuses_nonsense(monkeypatch):
    gc = _module()
    assert gc.DRAINS_MAX == 512 and gc.DRAIN_MAX_BYTES == 64 * 1024 and gc.DRAIN_TIMEOUT_S == 1.0
    monkeypatch.delenv(_ENV, raising=False)
    assert gc.drains_max_from_env(_ENV) == 512
    monkeypatch.setenv(_ENV, "7")
    assert gc.drains_max_from_env(_ENV) == 7
    for bad in ("0", "-1", "x", "1.5"):
        monkeypatch.setenv(_ENV, bad)
        with pytest.raises(RuntimeError):
            gc.drains_max_from_env(_ENV)


# ====================================================================== bounded reads, allocation-free drains

class _Transport:
    def __init__(self):
        self.protocol, self.closed, self.eof, self.reading = None, False, False, True

    def set_protocol(self, p):
        self.protocol = p

    def is_closing(self):
        return self.closed

    def can_write_eof(self):
        return True

    def write_eof(self):
        self.eof = True

    def resume_reading(self):
        self.reading = True

    def close(self):
        self.closed = True


def _proto():
    gc = _module()

    class Base:
        def __init__(self):
            self.got, self.connections, self.cycle, self.server_state = [], set(), None, type("S", (), {})()
            self.loop = asyncio.new_event_loop()

        def connection_made(self, t):
            self.transport = t
            self.connections.add(self)

        def data_received(self, data):
            self.got.append(data)

        def eof_received(self):
            return None

        def connection_lost(self, exc):
            self.connections.discard(self)

    class P(gc.GracefulCloseMixin, Base):
        pass
    return gc, P


def test_reads_are_bounded_and_go_through_the_threads_one_buffer():
    gc, P = _proto()
    t = _Transport()
    p = P()
    p.connection_made(t)
    reader = t.protocol
    assert isinstance(reader, asyncio.BufferedProtocol)
    buf = reader.get_buffer(262144)
    assert len(buf) == gc.READ_BUFFER_BYTES <= 64 * 1024 and buf is gc.read_buffer()
    buf[:5] = b"hello"
    reader.buffer_updated(5)
    assert p.got == [b"hello"]
    p2 = P()
    t2 = _Transport()
    p2.connection_made(t2)
    assert t2.protocol.get_buffer(-1).obj is buf.obj           # one buffer per thread, whatever the connection
    p.loop.close()
    p2.loop.close()


def test_a_drained_read_allocates_nothing_and_the_slot_is_free_before_the_drain():
    gc, P = _proto()
    t = _Transport()
    p = P()
    p.connection_made(t)
    reader = t.protocol
    p.cycle = type("C", (), {"response_complete": True, "body": b"x" * 150_000})()
    p.transport.close()                                        # uvicorn's close → the graceful close
    assert t.eof and not t.closed and p not in p.connections   # FIN sent, draining, the slot already given back
    assert p.cycle.body == b"" and p in gc.draining(p.server_state)
    tracemalloc.start()
    try:
        base, _ = tracemalloc.get_traced_memory()
        for _ in range(3):
            reader.get_buffer(-1)
            reader.buffer_updated(gc.READ_BUFFER_BYTES)
        grown, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert grown - base < 4096 and p.got == [] and not t.closed, grown - base
    for _ in range(3):                                         # past DRAIN_MAX_BYTES (64 KiB): closed
        reader.buffer_updated(gc.READ_BUFFER_BYTES)
    assert t.closed
    reader.connection_lost(None)
    assert p not in gc.draining(p.server_state)
    p.loop.close()


def test_past_the_drain_cap_an_answered_socket_is_closed_at_once():
    gc, P = _proto()
    P.drains_max = 2
    state = type("S", (), {})()
    ps = []
    for _ in range(3):
        t = _Transport()
        p = P()
        p.server_state = state
        p.connection_made(t)
        p.transport.close()
        ps.append((p, t))
    assert [t.closed for _, t in ps] == [False, False, True] and [t.eof for _, t in ps] == [True, True, False]
    assert len(gc.draining(state)) == 2
    for p, _ in ps:
        p.loop.close()


# ====================================================================== live: the service's own protocol under uvicorn

CHILD = r'''
import asyncio, socket, sys
import uvicorn
import importlib
proto = getattr(importlib.import_module(sys.argv[1]), sys.argv[2])

async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 401,
                "headers": [(b"content-type", b"text/plain"), (b"content-length", b"6"), (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": b"denied"})

sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1], flush=True)
cfg = uvicorn.Config(app, http=proto, limit_concurrency=int(sys.argv[3]), log_level="warning",
                     h11_max_incomplete_event_size=16 * 1024, timeout_keep_alive=5, lifespan="off")
uvicorn.Server(cfg).run(sockets=[sock])
'''


def _server(limit: int, env_extra: dict | None = None):
    env = dict(os.environ, PYTHONPATH=str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""), **(env_extra or {}))
    p = subprocess.Popen([sys.executable, "-c", CHILD, _SERVE, _PROTOCOL, str(limit)], cwd=SRC, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        port = int(p.stdout.readline())
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.05)
        time.sleep(0.2)
    except BaseException:
        p.kill()                  # never an orphaned server (fix wave 22, G3 class)
        p.wait()
        raise
    return p, port


def _answered(port: int) -> socket.socket:
    """A connection whose request was answered (401, Connection: close) and read to EOF; left open (the server
    is draining it)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"GET /x HTTP/1.1\r\nHost: t\r\n\r\n")
    got = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        got += chunk
    assert got.startswith(b"HTTP/1.1 401"), got[:40]
    return s


def _server_closed(s: socket.socket) -> bool:
    """Whether the server has closed its side for good (a send after its close is answered RST → EPIPE)."""
    try:
        s.send(b"x")
        time.sleep(0.15)
        s.send(b"x")
        time.sleep(0.05)
        s.send(b"x")
        return False
    except OSError:
        return True


def test_live_a_draining_connection_does_not_hold_a_concurrency_slot():
    """limit_concurrency=2 (uvicorn counts the requesting connection itself): one connection answered with
    Connection: close and draining (its client silent, socket open), then a new request — served, not 503."""
    p, port = _server(limit=2)
    try:
        a = _answered(port)
        b = socket.create_connection(("127.0.0.1", port), timeout=5)
        b.sendall(b"GET /y HTTP/1.1\r\nHost: t\r\n\r\n")
        got = b.recv(64)
        b.close()
        assert got.startswith(b"HTTP/1.1 401"), got
        assert not _server_closed(a)                           # a is still draining (within DRAIN_TIMEOUT_S)
        a.close()
    finally:
        p.kill()
        p.wait()


def test_live_past_the_drain_cap_the_answered_socket_is_closed_at_once():
    p, port = _server(limit=100, env_extra={_ENV: "2"})
    try:
        socks = [_answered(port) for _ in range(3)]
        closed = [_server_closed(s) for s in socks]
        for s in socks:
            s.close()
        assert closed == [False, False, True], closed
    finally:
        p.kill()
        p.wait()
