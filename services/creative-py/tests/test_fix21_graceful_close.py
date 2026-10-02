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
import select
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
PROTOCOL = "_HeadDeadlineH11Protocol"
SRC = Path(__file__).resolve().parents[1] / "src"

CHILD = r"""
import asyncio, socket, sys
import uvicorn
import importlib
proto = getattr(importlib.import_module(sys.argv[1]), sys.argv[2])
if sys.argv[4] != "default":
    mixin = next(c for c in proto.__mro__ if c.__name__ == "GracefulCloseMixin")
    sys.modules[mixin.__module__].DRAIN_TIMEOUT_S = float(sys.argv[4])

class Announcing(proto):
    def connection_made(self, transport):
        super().connection_made(transport)
        sys.stdout.write("C\n")
        sys.stdout.flush()

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
cfg = uvicorn.Config(app, http=Announcing, limit_concurrency=int(sys.argv[3]), log_level="warning",
                     h11_max_incomplete_event_size=16 * 1024, timeout_keep_alive=5, lifespan="off")
server = uvicorn.Server(cfg)

async def main():
    task = asyncio.ensure_future(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            return await task
        await asyncio.sleep(0.01)
    sys.stdout.write("%d\n" % sock.getsockname()[1])
    sys.stdout.flush()
    await task

asyncio.run(main())
"""

# Fix wave 25 (scout X3): synchronised on events, not sleeps. The child announces its port once uvicorn has started
# and every connection it accepts ("C" on its stdout); a test waits for those, and for the server's ANSWER before it
# sends the rest of a body — before, a 0.3 s sleep stood in for "the server has answered", so on a slow server the
# late bytes arrived first and the answer-then-body case (the regression this file guards) was not exercised at all.
HANG_GUARD_S = 60          # waits for an EVENT give up after this; never a measurement


class _Child:
    def __init__(self, limit: int, drain_s: str = "30"):
        env = dict(os.environ, PYTHONPATH=str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
        self.p = subprocess.Popen([sys.executable, "-c", CHILD, MODULE, PROTOCOL, str(limit), drain_s], cwd=SRC,
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.buf = b""
        try:
            self.port = int(self._line())
        except BaseException:
            self.close()          # never an orphaned server (scout X3: this helper used to leave it running)
            raise

    def _line(self) -> bytes:
        deadline = time.monotonic() + HANG_GUARD_S
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("the server child said nothing")
            ready, _, _ = select.select([self.p.stdout], [], [], left)
            if ready:
                chunk = os.read(self.p.stdout.fileno(), 4096)
                if not chunk:
                    raise RuntimeError(f"the server child exited ({self.p.wait()})")
                self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line

    def accepted(self, n: int = 1) -> None:
        for _ in range(n):
            assert self._line() == b"C"

    def connect(self) -> socket.socket:
        s = socket.create_connection(("127.0.0.1", self.port), timeout=HANG_GUARD_S)
        self.accepted()
        return s

    def close(self) -> None:
        self.p.kill()
        self.p.wait()
        self.p.stdout.close()


def _read_answer(s: socket.socket) -> bytes:
    """Reads one HTTP response: its head and, when it has a Content-Length, its body; never past it. (A response
    without Content-Length — uvicorn's own 400 — is delimited by the close: its head is what proves the answer.)"""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = s.recv(1)
        if not chunk:
            raise AssertionError(f"the connection ended before a whole response head: {data!r}")
        data += chunk
    head = data.decode("latin-1").lower()
    lengths = [h.split(":", 1)[1] for h in head.split("\r\n") if h.startswith("content-length:")]
    if not lengths:
        return data
    length = int(lengths[0])
    body = b""
    while len(body) < length:
        chunk = s.recv(length - len(body))
        if not chunk:
            raise AssertionError("the connection ended inside the response body")
        body += chunk
    return data + body


def _exchange(child: _Child, first: bytes, later: bytes) -> tuple[bytes, int, str]:
    """Send ``first``, wait for the server's whole ANSWER, then send ``later`` — the rest of a body still on its
    way — then read to EOF; returns (answer, SO_ERROR, how the read after the answer ended)."""
    s = child.connect()
    try:
        s.sendall(first)
        answer = _read_answer(s)
        delimited = b"content-length:" in answer.lower()
        try:
            s.sendall(later)
        except OSError:
            pass                     # recorded through SO_ERROR / the read below
        how = "eof"
        while True:
            try:
                chunk = s.recv(65536)
            except OSError as exc:
                how = type(exc).__name__
                break
            if not chunk:
                break
            if delimited:
                how = "extra bytes"
            else:
                answer += chunk      # the rest of a close-delimited answer
        return answer, s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR), how
    finally:
        s.close()


@pytest.fixture
def one_slot():
    child = _Child(limit=1)
    yield child
    child.close()


@pytest.fixture
def open_server():
    child = _Child(limit=100)
    yield child
    child.close()


def test_limit_concurrency_503_with_the_body_sent_closes_gracefully(one_slot):
    hold = one_slot.connect()                                 # the one slot, held (the server accepted it)
    try:
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


def _module():
    import importlib
    sys.path.insert(0, str(SRC))
    try:
        return importlib.import_module(MODULE)
    finally:
        sys.path.remove(str(SRC))


def test_the_drain_is_bounded_in_bytes_a_client_that_keeps_sending_is_cut(one_slot):
    """The drain is bounded (DRAIN_MAX_BYTES): a client still sending long after the answer is closed on, by design —
    it is not read forever. The child's drain WINDOW is 30 s here, so the cut can only come from the byte bound; it
    is measured in bytes: the client got its error before it had sent DRAIN_MAX_BYTES plus what the two kernels'
    socket buffers can hold (asserted with a 64 MiB ceiling, far above any loopback buffer, far below "forever")."""
    mod = _module()
    assert mod.DRAIN_MAX_BYTES == 64 * 1024 and mod.DRAIN_TIMEOUT_S == 1.0
    hold = one_slot.connect()
    s = one_slot.connect()
    try:
        s.sendall(b"POST /x HTTP/1.1\r\nHost: t\r\nContent-Length: 100000000\r\n\r\n")
        assert _read_answer(s).startswith(b"HTTP/1.1 503")    # answered; the drain has started
        sent, cut, ceiling = 0, None, 64 * 1024 * 1024
        while sent < ceiling:
            try:
                sent += s.send(b"x" * 65536)
            except OSError as exc:
                cut = type(exc).__name__
                break
        assert cut is not None, f"{sent} bytes accepted after the answer: the drain is not bounded in bytes"
        # The server's close, not the client's own hang guard (fix wave 25, E-C review): a server that simply stopped
        # reading would block this send until the socket timeout, and a TimeoutError is not a cut.
        assert cut in ("ConnectionResetError", "BrokenPipeError", "ConnectionAbortedError"), (cut, sent)
    finally:
        s.close()
        hold.close()


def test_the_drain_is_bounded_in_time_a_slow_sender_is_cut():
    """The drain WINDOW (DRAIN_TIMEOUT_S, the module's default here): a client trickling a byte every 20 ms — far
    under the byte bound — is cut once the window ends. The cut is an event (its send fails); the only time
    assertion is a LOWER bound (not before the window) measured from before the request, which load cannot break."""
    child = _Child(limit=100, drain_s="default")
    try:
        mod = _module()
        s = child.connect()
        # The clock starts BEFORE the request is sent (fix wave 25, E-C): the server cannot answer — so its drain
        # window cannot start — before it has the request, so "cut >= window after this stamp" holds however late
        # this process gets to read the answer. (Stamped after the read, a test process starved between the
        # server's answer and its own read measured less than the window and failed a correct server.)
        sent_at = time.monotonic()
        s.sendall(b"POST /x HTTP/1.1\r\nHost: t\r\nContent-Length: 100000000\r\n\r\n")
        assert _read_answer(s).startswith(b"HTTP/1.1 401")
        cut = None
        while time.monotonic() - sent_at < HANG_GUARD_S:
            try:
                s.send(b"x")
            except OSError as exc:
                cut = type(exc).__name__
                break
            time.sleep(0.02)
        took = time.monotonic() - sent_at
        assert cut is not None, "a slow sender was never cut: the drain window does not end"
        assert took >= mod.DRAIN_TIMEOUT_S, f"cut {took:.2f} s after the request, before the {mod.DRAIN_TIMEOUT_S} s window"
        s.close()
    finally:
        child.close()
