"""
Fix wave 22 (lead ruling G8; AEGIS N21-C-4): the blocking-client residual of the bounded graceful close, pinned.

The graceful close (fix wave 21, L1; src/graceful_close.py) answers, sends FIN, then reads and discards the client's
remaining bytes for at most 64 KiB / 1 s before it closes. A client that answers "503" by READING (it sends while it
reads, stops at the answer and reads it to Content-Length — what test_live_128_senders does since wave 21, ruled a
legitimate client in round 21) gets the whole answer and a clean close. A client that writes a multi-megabyte body
with one blocking ``sendall`` and reads nothing until it has sent everything cannot: once the bounded drain ends the
server closes and the kernel resets the connection, so the ``sendall`` fails and the 503 is never read. That is the
design (an unbounded drain is a resource an attacker holds for free; ledger-rust makes the same trade), and it is
pinned here so that a change to it — either way — is a visible decision, not a drift.
"""

from __future__ import annotations

import select
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import child_env

SRC = Path(__file__).resolve().parents[1] / "src"
BODY = 3_900_000

CHILD = r'''
import asyncio, socket, sys
import uvicorn, http_limits

async def app(scope, receive, send):
    if scope["type"] == "http":
        await asyncio.sleep(30)

sock = socket.socket(); sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1], flush=True)
uvicorn.Server(uvicorn.Config(app, http=http_limits.DeadlineH11Protocol, limit_concurrency=2, log_level="error",
                              lifespan="off")).run(sockets=[sock])
'''

_REAL_CONNECT = socket.socket.connect
_REAL_CREATE = socket.create_connection


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch):
    def only_loopback(fn):
        def wrapped(*a, **k):
            addr = a[1] if len(a) > 1 and isinstance(a[0], socket.socket) else a[0]
            if not (isinstance(addr, tuple) and addr[0] == "127.0.0.1"):
                raise RuntimeError("network access attempted in a test (not allowed)")
            return fn(*a, **k)
        return wrapped
    monkeypatch.setattr(socket.socket, "connect", only_loopback(_REAL_CONNECT))
    monkeypatch.setattr(socket, "create_connection", only_loopback(_REAL_CREATE))


@pytest.fixture
def one_slot_held():
    p = subprocess.Popen([sys.executable, "-c", CHILD], cwd=SRC, stdout=subprocess.PIPE, text=True,
                         env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(SRC), **child_env()})
    try:
        port = int(p.stdout.readline())
        for _ in range(100):                                                  # until uvicorn listens
            try:
                hold = socket.create_connection(("127.0.0.1", port), timeout=10)  # uvicorn counts it: the next is 503
                break
            except ConnectionRefusedError:
                time.sleep(0.05)
        time.sleep(0.3)
        yield port
        hold.close()
    finally:
        p.kill()
        p.wait()


def _data() -> bytes:
    return b"POST /x HTTP/1.1\r\nHost: t\r\nContent-Length: %d\r\n\r\n" % (4 * 1024 * 1024) + b" " * BODY


def test_a_blocking_sendall_client_with_megabytes_unsent_is_reset_after_the_bounded_drain(one_slot_held):
    s = socket.create_connection(("127.0.0.1", one_slot_held), timeout=10)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
    try:
        with pytest.raises((BrokenPipeError, ConnectionResetError)):
            s.sendall(_data())
    finally:
        s.close()


def test_a_reading_client_gets_the_503_and_a_clean_close(one_slot_held):
    s = socket.create_connection(("127.0.0.1", one_slot_held), timeout=10)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
    s.setblocking(False)
    data, sent, buf, t0 = _data(), 0, b"", time.monotonic()
    try:
        while time.monotonic() - t0 < 10:
            r, w, _ = select.select([s], [s] if sent < len(data) else [], [], 1)
            if r:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                head, sep, body = buf.partition(b"\r\n\r\n")
                if sep and b"content-length: 19" in head.lower() and len(body) >= 19:
                    break
            elif w:
                try:
                    sent += s.send(data[sent:sent + 65536])
                except BlockingIOError:
                    pass
        # the whole answer is read; a send still in flight when the drain's bound ended may be refused afterwards
        # (EPIPE on the socket), which costs this client nothing — it has its answer
        assert buf.startswith(b"HTTP/1.1 503") and buf.endswith(b"Service Unavailable"), buf[:80]
    finally:
        s.close()
