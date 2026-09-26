"""
Fix wave 5, NEW-3 (MED, CONFIRMED): unauthenticated, unbounded request
heads and no idle / partial-head / slow-body deadlines — tested against the
real service process (`python3 -m api`) over real sockets.

The finding: `python3 -m api` ran uvicorn with its defaults, i.e. the
httptools parser, which has no request-head size limit (one 100-200 MB
header was buffered in full: 53 -> 415 MB RSS), and no request-head or
request-body deadline (10/10 idle and partial-head sockets still open after
60 s; a slow-drip body held its connection indefinitely). All of this is
reachable without the service token.

What must hold (values in src/http_limits.py, documented in the README):
  - an oversized request head is refused while it is being read (400), the
    connection is closed, and the server's RSS stays flat;
  - a connection that has not delivered a complete request head within
    REQUEST_HEAD_TIMEOUT_S of connecting is closed (idle-from-connect and
    trickled partial heads alike);
  - an idle keep-alive connection is closed after KEEP_ALIVE_TIMEOUT_S;
  - a request body that has not fully arrived within the body deadline is
    answered 408 when the app is reading it, and the connection is closed
    even when the app never reads it (an early 401);
  - at most MAX_OPEN_CONNECTIONS sockets are held; extras are closed at once;
  - /health keeps answering throughout.

Ports: FULFILLMENT_TEST_PORT_RANGE when set (this wave: 20140-20159).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from _procinfo import rss_kib
from test_live_server import SRC, TOKEN, _free_port

import http_limits

MIB = 1024 * 1024
BODY_TIMEOUT_UNDER_TEST = 3.0  # narrowed via FULFILLMENT_BODY_READ_TIMEOUT_S (may only narrow)
SLACK_S = 2.5


def _start(env_extra: dict[str, str] | None = None):
    port = _free_port()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(SRC),
           "FULFILLMENT_SERVICE_TOKEN": TOKEN, "FULFILLMENT_PORT": str(port), **(env_extra or {})}
    # DEVNULL, not PIPE: the refused requests below each log a warning, and
    # an unread pipe would eventually block the server.
    proc = subprocess.Popen([sys.executable, "-m", "api"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError("service exited early")
        try:
            if _health(port, timeout=0.5)[0] == 200:
                return proc, port
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise AssertionError("service did not start within 15s")


def _stop(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture(scope="module")
def server():
    proc, port = _start({"FULFILLMENT_BODY_READ_TIMEOUT_S": str(BODY_TIMEOUT_UNDER_TEST)})
    yield proc, port
    _stop(proc)


# Fix wave 16: portable (Linux /proc, macOS/BSD ps); measures only the server pid.
_rss_kib = rss_kib


def _read_status(s: socket.socket) -> int | None:
    data = b""
    try:
        while b"\r\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except (ConnectionResetError, TimeoutError, socket.timeout):
        pass
    return int(data[9:12]) if data.startswith(b"HTTP/1.1 ") else None


def _health(port: int, timeout: float = 5.0) -> tuple[int | None, float]:
    t0 = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
        return _read_status(s), time.monotonic() - t0


def _closed_by_peer(s: socket.socket) -> bool:
    """True once the server has closed the connection (EOF or reset)."""
    s.setblocking(False)
    try:
        return s.recv(65536) == b""
    except BlockingIOError:
        return False
    except (ConnectionResetError, BrokenPipeError):
        return True
    finally:
        s.setblocking(True)


class _HealthProbe:
    """Polls /health every 0.25 s in the background; records (status, seconds)."""

    def __init__(self, port: int):
        self.port = port
        self.results: list[tuple[int | None, float]] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.results.append(_health(self.port))
            except OSError as exc:
                self.results.append((None, float("inf")))
                _ = exc
            self._stop.wait(0.25)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=10)

    def assert_healthy(self):
        assert self.results, "no /health probes ran"
        bad = [r for r in self.results if r[0] != 200 or r[1] >= 1.0]
        assert not bad, f"/health not responsive: {bad[:5]} of {len(self.results)}"


def test_documented_values():
    assert http_limits.MAX_HEADER_BYTES == 16 * 1024
    assert http_limits.REQUEST_HEAD_TIMEOUT_S == 10.0
    assert http_limits.KEEP_ALIVE_TIMEOUT_S == 5
    assert http_limits.BODY_READ_TIMEOUT_S == 30.0
    assert http_limits.LIMIT_CONCURRENCY == 128
    assert http_limits.MAX_OPEN_CONNECTIONS == 256


def test_oversized_header_is_refused_while_read_and_rss_stays_flat(server):
    proc, port = server
    before = _rss_kib(proc.pid)
    with _HealthProbe(port) as probe:
        s = socket.create_connection(("127.0.0.1", port), timeout=30)
        sent = 0
        try:
            s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\nX-Pad: ")
            chunk = b"a" * MIB
            try:
                while sent < 150 * MIB:
                    s.sendall(chunk)
                    sent += len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            status = _read_status(s)
        finally:
            s.close()
        time.sleep(0.5)
    after = _rss_kib(proc.pid)
    assert status in (400, 431, None), status
    assert sent < 150 * MIB, "the whole 150 MB header was accepted"
    assert after - before < 20 * 1024, f"RSS grew {before} -> {after} KiB"
    probe.assert_healthy()


def test_idle_and_partial_head_sockets_are_closed_within_the_head_deadline(server):
    _, port = server
    bound = http_limits.REQUEST_HEAD_TIMEOUT_S + SLACK_S
    idle = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(10)]
    partial = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(10)]
    head = b"GET /health HTTP/1.1\r\nHost: t\r\nX-Slow: " + b"a" * 64
    t0 = time.monotonic()
    closed_at: dict[int, float] = {}
    try:
        with _HealthProbe(port) as probe:
            i = 0
            while time.monotonic() - t0 < bound + 3 and len(closed_at) < 20:
                for n, s in enumerate(idle + partial):
                    if n in closed_at:
                        continue
                    if n >= 10 and i < len(head):
                        try:
                            s.sendall(head[i:i + 1])  # slowloris: one byte per 0.25 s, never finishing
                        except (BrokenPipeError, ConnectionResetError):
                            closed_at[n] = time.monotonic() - t0
                            continue
                    if _closed_by_peer(s):
                        closed_at[n] = time.monotonic() - t0
                i += 1
                time.sleep(0.25)
        probe.assert_healthy()
    finally:
        for s in idle + partial:
            s.close()
    assert len(closed_at) == 20, f"only {len(closed_at)}/20 hostile sockets were closed within {bound + 3:.1f}s"
    worst = max(closed_at.values())
    assert worst <= bound, f"a socket was held {worst:.1f}s (bound {bound:.1f}s)"
    assert min(closed_at.values()) >= http_limits.REQUEST_HEAD_TIMEOUT_S - 1, "closed before the documented deadline"


def test_idle_keep_alive_connection_is_closed(server):
    _, port = server
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\n\r\n")
        assert _read_status(s) == 200
        t0 = time.monotonic()
        while not _closed_by_peer(s):
            assert time.monotonic() - t0 < http_limits.KEEP_ALIVE_TIMEOUT_S + SLACK_S, "idle keep-alive held open"
            time.sleep(0.1)
    finally:
        s.close()


def _slow_body_socket(port: int, authed: bool) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    auth = b"Authorization: Bearer " + TOKEN.encode() + b"\r\n" if authed else b""
    s.sendall(b"POST /agents/missed-call-detection/detect HTTP/1.1\r\nHost: t\r\n" + auth
              + b"Content-Type: application/json\r\nContent-Length: 100000\r\n\r\n{")
    return s


def test_slow_body_gets_408_and_the_connection_is_closed(server):
    _, port = server
    s = _slow_body_socket(port, authed=True)
    t0 = time.monotonic()
    got = b""
    try:
        with _HealthProbe(port) as probe:
            while time.monotonic() - t0 < BODY_TIMEOUT_UNDER_TEST + SLACK_S + 3:
                try:
                    s.sendall(b" ")  # one byte per 0.25 s: never finishes 100 KB
                except (BrokenPipeError, ConnectionResetError):
                    break
                s.setblocking(False)
                try:
                    chunk = s.recv(65536)
                    if chunk == b"":
                        break
                    got += chunk
                except BlockingIOError:
                    pass
                except ConnectionResetError:
                    break
                finally:
                    s.setblocking(True)
                time.sleep(0.25)
            elapsed = time.monotonic() - t0
        probe.assert_healthy()
    finally:
        s.close()
    assert got.startswith(b"HTTP/1.1 408"), got[:80]
    assert elapsed <= BODY_TIMEOUT_UNDER_TEST + SLACK_S, f"slow body held {elapsed:.1f}s"


def test_slow_body_the_app_never_reads_is_still_closed(server):
    """No token: the route answers 401 before reading the body. Before the
    fix, the rest of the body could then be trickled forever (each byte
    reset uvicorn's keep-alive timer)."""
    _, port = server
    s = _slow_body_socket(port, authed=False)
    bound = BODY_TIMEOUT_UNDER_TEST + http_limits.BODY_DEADLINE_GRACE_S + SLACK_S
    t0 = time.monotonic()
    try:
        while True:
            try:
                s.sendall(b" ")
            except (BrokenPipeError, ConnectionResetError):
                break
            if _closed_by_peer(s):
                break
            assert time.monotonic() - t0 < bound, f"unread slow body held open > {bound:.1f}s"
            time.sleep(0.25)
    finally:
        s.close()


def test_connection_count_is_bounded_and_health_recovers(server):
    _, port = server
    extra = 40
    socks = []
    try:
        for _ in range(http_limits.MAX_OPEN_CONNECTIONS + extra):
            socks.append(socket.create_connection(("127.0.0.1", port), timeout=5))
        time.sleep(http_limits.OVER_CAP_CLOSE_S + 0.5)
        # Fix wave 6, N7: every socket over the cap is answered 503 and closed
        # within OVER_CAP_CLOSE_S (it used to be aborted with no response, and
        # this test asserted a bare EOF). Not held for 10 s either way.
        refused = 0
        for s in socks:
            s.setblocking(False)
            try:
                data = s.recv(65536)
            except BlockingIOError:
                continue
            except ConnectionResetError:
                refused += 1
                continue
            finally:
                s.setblocking(True)
            if data.startswith(b"HTTP/1.1 503"):
                refused += 1
                assert _closed_by_peer(s), "over-cap socket answered 503 but still held"
        assert refused >= extra, f"only {refused} of {len(socks)} refused; cap {http_limits.MAX_OPEN_CONNECTIONS}"
        # while saturated, /health gets a prompt answer or refusal, never a hang
        try:
            status, took = _health(port, timeout=3)
            assert took < 1.0
            assert status in (200, 503, None)
        except (ConnectionResetError, BrokenPipeError):
            pass
        # the held sockets never sent a head: all gone after the head deadline
        time.sleep(http_limits.REQUEST_HEAD_TIMEOUT_S + 1)
        assert all(_closed_by_peer(s) for s in socks)
    finally:
        for s in socks:
            s.close()
    status, took = _health(port)
    assert status == 200 and took < 1.0


def test_body_timeout_env_may_only_narrow():
    for bad in ("0", "-1", "31", "nan", "abc"):
        port = _free_port()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(SRC),
               "FULFILLMENT_SERVICE_TOKEN": TOKEN, "FULFILLMENT_PORT": str(port),
               "FULFILLMENT_BODY_READ_TIMEOUT_S": bad}
        r = subprocess.run([sys.executable, "-m", "api"], env=env, capture_output=True, timeout=20)
        assert r.returncode != 0, bad
        assert b"FULFILLMENT_BODY_READ_TIMEOUT_S" in r.stderr, r.stderr[-400:]
