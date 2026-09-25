"""
Fix wave 6, N7 (INFO): connections over MAX_OPEN_CONNECTIONS were aborted
without a response (curl exit 000, `Connection reset by peer`) instead of
being answered. Between LIMIT_CONCURRENCY and MAX_OPEN_CONNECTIONS held
sockets, uvicorn answers every new request 503 (including /health) for as
long as the sockets are held (<= REQUEST_HEAD_TIMEOUT_S if they never send a
head). Above the cap the service now writes a minimal 503 and closes.

What must hold, against the real process (`python3 -m api`):
  - a connection made while MAX_OPEN_CONNECTIONS are held gets
    `HTTP/1.1 503`, `Connection: close`, and is then closed — never a bare
    reset;
  - it is not held: it is closed as soon as its request bytes arrive, or
    after OVER_CAP_CLOSE_S if none arrive;
  - held sockets are still bounded and /health recovers.

Ports: FULFILLMENT_TEST_PORT_RANGE when set (this wave: 20320-20339).
"""

from __future__ import annotations

import socket
import time

import pytest

from test_fix5_http_limits_live import _closed_by_peer, _health, _read_status, _start, _stop

import http_limits


@pytest.fixture(scope="module")
def server():
    proc, port = _start()
    yield proc, port
    _stop(proc)


def _recv_all(s: socket.socket, timeout: float = 3.0) -> bytes:
    s.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except (ConnectionResetError, TimeoutError, socket.timeout):
        pass
    return data


def test_over_cap_connection_is_answered_503_then_closed(server):
    _, port = server
    held = []
    try:
        for _ in range(http_limits.MAX_OPEN_CONNECTIONS):
            held.append(socket.create_connection(("127.0.0.1", port), timeout=5))
        time.sleep(0.5)
        assert sum(_closed_by_peer(s) for s in held) == 0, "sockets within the cap were closed"

        statuses = []
        for _ in range(5):
            t0 = time.monotonic()
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
                try:
                    s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                raw = _recv_all(s)
            took = time.monotonic() - t0
            statuses.append((int(raw[9:12]) if raw.startswith(b"HTTP/1.1 ") else None, took, raw[:200]))
        assert all(st == 503 for st, _, _ in statuses), statuses
        assert all(b"connection: close" in raw.lower() for _, _, raw in statuses), statuses
        assert all(took < 1.0 for _, took, _ in statuses), statuses  # closed once the request arrived, not held

        # a silent over-cap connection is closed after OVER_CAP_CLOSE_S, not held
        silent = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            t0 = time.monotonic()
            raw = _recv_all(silent, timeout=http_limits.OVER_CAP_CLOSE_S + 2)
            took = time.monotonic() - t0
        finally:
            silent.close()
        assert raw.startswith(b"HTTP/1.1 503"), raw[:80]
        assert took <= http_limits.OVER_CAP_CLOSE_S + 1.0, f"silent over-cap socket held {took:.1f}s"
    finally:
        for s in held:
            s.close()
    time.sleep(0.5)
    status, took = _health(port)
    assert status == 200 and took < 1.0


def test_between_limit_concurrency_and_cap_health_is_503_until_release(server):
    """Documents the trade-off exactly: with >= LIMIT_CONCURRENCY sockets held
    (below the hard cap), a new request is answered 503, then 200 again
    within REQUEST_HEAD_TIMEOUT_S once the holders send nothing."""
    _, port = server
    held = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(http_limits.LIMIT_CONCURRENCY)]
    try:
        time.sleep(0.3)
        status, took = _health(port, timeout=3)
        assert status == 503 and took < 1.0, (status, took)
        time.sleep(http_limits.REQUEST_HEAD_TIMEOUT_S + 1)
        assert all(_closed_by_peer(s) for s in held)
        status, took = _health(port, timeout=3)
        assert status == 200 and took < 1.0, (status, took)
    finally:
        for s in held:
            s.close()
