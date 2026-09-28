"""Fix wave 21, lead ruling L1 (the ledger-rust N20-M-1 class, swept to every Python service): an answer
written while the client is still sending its request body is followed by a GRACEFUL close — FIN, a bounded
drain of the unread bytes, then close — so the client reads the whole answer and EOF and its socket reports no
error. Before, uvicorn closed at once with unread bytes in the socket, the kernel answered RST, and the client
saw ECONNRESET/EPIPE (often before it could read the answer at all).

The server here is the service's own protocol class under uvicorn, in a child process, with a tiny ASGI app
(the paths exercised are uvicorn's and the protocol's, not the service's routes):
  - uvicorn's own limit_concurrency 503 (Connection: close), the rest of a 40 KiB body arriving after it;
  - a 400 uvicorn writes itself (unparseable Content-Length), 30 KiB arriving after it;
  - an app answer with Connection: close written before the body, the rest of 40 KiB arriving after it.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

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


MODULE = "serve"
PROTOCOL = "HeadDeadlineH11Protocol"
SRC = Path(__file__).resolve().parents[1] / "src"

CHILD = r'''
import asyncio, socket, sys
import uvicorn
import importlib
proto = getattr(importlib.import_module(sys.argv[1]), sys.argv[2])

async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    if scope["path"] == "/hold":
        await asyncio.sleep(30)
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


def _server(limit: int):
    env = dict(os.environ, PYTHONPATH=str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    p = subprocess.Popen([sys.executable, "-c", CHILD, MODULE, PROTOCOL, str(limit)], cwd=SRC, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    port = int(p.stdout.readline())
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            time.sleep(0.05)
    return p, port


def _exchange(port: int, first: bytes, later: bytes) -> tuple[bytes, int, str]:
    """Send ``first``, wait until the server has answered (and, before wave 21, closed), send ``later`` — the rest
    of a body still on its way — then read to EOF; returns (answer, SO_ERROR, how the read ended)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        s.sendall(first)
        time.sleep(0.3)
        try:
            s.sendall(later)
        except OSError:
            pass                     # recorded through SO_ERROR / the read below
        time.sleep(0.2)
        out, how = b"", "eof"
        while True:
            try:
                chunk = s.recv(65536)
            except OSError as exc:
                how = type(exc).__name__
                break
            if not chunk:
                break
            out += chunk
        return out, s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR), how
    finally:
        s.close()


@pytest.fixture
def one_slot():
    p, port = _server(limit=1)
    yield port
    p.kill()
    p.wait()


@pytest.fixture
def open_server():
    p, port = _server(limit=100)
    yield port
    p.kill()
    p.wait()


def test_limit_concurrency_503_with_the_body_sent_closes_gracefully(one_slot):
    hold = socket.create_connection(("127.0.0.1", one_slot), timeout=10)     # the one slot, held
    try:
        time.sleep(0.2)
        head = b"POST /x HTTP/1.1\r\nHost: t\r\nContent-Length: %d\r\n\r\n" % (40 * 1024)
        out, err, how = _exchange(one_slot, head + b"x" * 1024, b"x" * (39 * 1024))
        assert out.startswith(b"HTTP/1.1 503") and how == "eof" and err == 0, (out[:40], err, how)
    finally:
        hold.close()


def test_uvicorn_400_with_bytes_after_the_head_closes_gracefully(open_server):
    head = b"POST /x HTTP/1.1\r\nHost: t\r\nContent-Length: abc\r\n\r\n"
    out, err, how = _exchange(open_server, head + b"y" * 1024, b"y" * (30 * 1024))
    assert out.startswith(b"HTTP/1.1 400") and how == "eof" and err == 0, (out[:40], err, how)


def test_app_answer_with_connection_close_before_the_body_is_read_closes_gracefully(open_server):
    head = b"POST /x HTTP/1.1\r\nHost: t\r\nContent-Length: %d\r\n\r\n" % (40 * 1024)
    out, err, how = _exchange(open_server, head + b"z" * 1024, b"z" * (39 * 1024))
    assert out.startswith(b"HTTP/1.1 401") and out.endswith(b"denied") and how == "eof" and err == 0, (out[-40:], err, how)


def test_the_drain_is_bounded_a_client_that_keeps_sending_is_cut(one_slot):
    """The drain is bounded (DRAIN_MAX_BYTES / DRAIN_TIMEOUT_S): a client still sending long after the answer is
    closed on, by design — it is not read forever."""
    import importlib
    sys.path.insert(0, str(SRC))
    try:
        mod = importlib.import_module(MODULE)
    finally:
        sys.path.remove(str(SRC))
    assert mod.DRAIN_MAX_BYTES == 64 * 1024 and mod.DRAIN_TIMEOUT_S == 1.0
    hold = socket.create_connection(("127.0.0.1", one_slot), timeout=10)
    s = socket.create_connection(("127.0.0.1", one_slot), timeout=10)
    try:
        time.sleep(0.2)
        s.sendall(b"POST /x HTTP/1.1\r\nHost: t\r\nContent-Length: 100000000\r\n\r\n")
        t0, cut = time.monotonic(), None
        while time.monotonic() - t0 < 10:
            try:
                s.sendall(b"x" * 65536)
            except OSError as exc:
                cut = type(exc).__name__
                break
        assert cut is not None and time.monotonic() - t0 < 5, (cut, time.monotonic() - t0)
    finally:
        s.close()
        hold.close()
