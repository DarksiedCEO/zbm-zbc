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

from test_fix5_http_limits_live import _closed_by_peer, _health, _start, _stop

import http_limits


@pytest.fixture(scope="module")
def server():
    proc, port = _start()
    yield proc, port
    _stop(proc)


def _recv_until_closed(s: socket.socket, timeout: float) -> tuple[bytes, bool]:
    """Everything the server sends, and whether the SERVER ended the connection (EOF or reset) — False when only our
    own `timeout` ended the wait, i.e. the socket was held open."""
    s.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return data, True
            data += chunk
    except ConnectionResetError:
        return data, True
    except (TimeoutError, socket.timeout):
        return data, False


def _over_cap_probe(port: int) -> tuple[bytes, bool]:
    """One /health on a fresh connection: (everything the server sent, whether the SERVER closed it). Connection:
    close, so a 200 (not yet at a limit) ends too."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        try:
            s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        return _recv_until_closed(s, 30.0)


def _is_over_cap_answer(raw: bytes) -> bool:
    """The over-cap path's own 503 (http_limits._OVER_CAP_RESPONSE, written at connection_made): it alone carries
    Retry-After — uvicorn's limit_concurrency 503 (LIMIT_CONCURRENCY..MAX_OPEN_CONNECTIONS held) does not."""
    return raw.startswith(b"HTTP/1.1 503") and b"\r\nretry-after: 1\r\n" in raw.lower()


def _wait_until_over_cap(port: int, attempts: int = 200) -> list:
    """Fix wave 26b (W25-EA-6): probes until one is answered by the over-cap path, i.e. until the server has accepted
    every held connection (only then is a new one over MAX_OPEN_CONNECTIONS); every answer seen is returned. Was a
    fixed `time.sleep(0.5)` for "the server has accepted them"."""
    seen = []
    for _ in range(attempts):
        raw, _ = _over_cap_probe(port)
        seen.append(raw[:12])
        if _is_over_cap_answer(raw):
            return seen
        time.sleep(0.02)   # pacing between probes; the loop ends on the server's answer
    raise AssertionError(f"no over-cap answer after {attempts} probes: {seen[-5:]}")


def test_over_cap_connection_is_answered_503_then_closed(server):
    _, port = server
    held = []
    try:
        for _ in range(http_limits.MAX_OPEN_CONNECTIONS):
            held.append(socket.create_connection(("127.0.0.1", port), timeout=5))
        # Fix wave 26b (W25-EA-6): was `time.sleep(0.5)` then the check below. The check now runs once the server
        # has provably accepted all MAX_OPEN_CONNECTIONS held sockets (a probe got the over-cap answer).
        _wait_until_over_cap(port)
        assert sum(_closed_by_peer(s) for s in held) == 0, "sockets within the cap were closed"

        statuses = []
        for _ in range(5):
            t0 = time.monotonic()
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
                try:
                    s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                raw, closed = _recv_until_closed(s, 30.0)
            took = time.monotonic() - t0
            statuses.append((int(raw[9:12]) if raw.startswith(b"HTTP/1.1 ") else None, closed, raw[:200], took))
        assert all(st == 503 for st, _, _, _ in statuses), statuses
        assert all(_is_over_cap_answer(raw) for _, _, raw, _ in statuses), statuses   # the over-cap path's 503
        assert all(b"connection: close" in raw.lower() for _, _, raw, _ in statuses), statuses
        # fix wave 25 (R-HYGIENE L1): was `took < 1.0` (wall clock). Closed once the request arrived, not held: the
        # SERVER ended each connection (EOF/reset) after its 503 — a held socket ends only by our 30 s timeout.
        assert all(closed for _, closed, _, _ in statuses), statuses

        # a silent over-cap connection is closed after OVER_CAP_CLOSE_S, not held
        silent = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            t0 = time.monotonic()
            raw, closed = _recv_until_closed(silent, http_limits.REQUEST_HEAD_TIMEOUT_S - 1)
            took = time.monotonic() - t0
        finally:
            silent.close()
        # fix wave 25 (R-HYGIENE L1): was `took <= OVER_CAP_CLOSE_S + 1.0`. Only the over-cap path answers a socket
        # that sent nothing (503) and closes it; the head timeout (REQUEST_HEAD_TIMEOUT_S, 10 s) would close it
        # without a 503 and after our wait (REQUEST_HEAD_TIMEOUT_S - 1) has ended.
        assert raw.startswith(b"HTTP/1.1 503"), raw[:80]
        assert closed, f"silent over-cap socket held open ({took:.1f}s, printed only)"

        # Fix wave 26b (W25-EA-6): was "close every held socket, `time.sleep(0.5)`, /health must be 200". Now the
        # first held socket (the OLDEST: its head deadline, REQUEST_HEAD_TIMEOUT_S from its connect, comes first) is
        # kept open as a witness, the others are closed, and /health is probed until it is 200. The witness still
        # open afterwards proves the release came from our closes, not from the head deadline (which would have
        # closed the witness first) — state ordering, where the old form needed the server to react within 0.5 s.
        witness = held[0]
        for s in held[1:]:
            s.close()
        seen = _health_until(port, 200)
        assert seen[-1] == 200, seen
        assert not _closed_by_peer(witness), ("the cap was released only by the head deadline (the oldest held "
                                              "socket was closed first), not by our closes", seen)
    finally:
        for s in held:
            s.close()
    status, _ = _health(port)
    assert status == 200  # fix wave 25 (R-HYGIENE L1): the wall-clock `took < 1.0` dropped; the cap was released


def _health_until(port: int, want: int, attempts: int = 200) -> list:
    """Fix wave 26b (W25-EA-6): /health on fresh connections until it answers `want`, at most `attempts` round
    trips; every status seen is returned (the caller asserts the last one)."""
    seen = []
    for _ in range(attempts):
        status, _ = _health(port, timeout=3)
        seen.append(status)
        if status == want:
            break
        time.sleep(0.02)   # pacing between probes; the loop ends on the server's answer
    return seen


def _until_all_closed_by_peer(socks: list, attempts: int) -> bool:
    """Fix wave 26b (W25-EA-6): True as soon as the server has closed every socket in `socks`, polled every 0.1 s at
    most `attempts` times (a hang guard; the loop ends when the server acts)."""
    for _ in range(attempts):
        if all(_closed_by_peer(s) for s in socks):
            return True
        time.sleep(0.1)
    return False


def test_between_limit_concurrency_and_cap_health_is_503_until_release(server):
    """Documents the trade-off exactly: with >= LIMIT_CONCURRENCY sockets held
    (below the hard cap), a new request is answered 503, then 200 again
    within REQUEST_HEAD_TIMEOUT_S once the holders send nothing."""
    _, port = server
    held = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(http_limits.LIMIT_CONCURRENCY)]
    try:
        # Fix wave 26b (W25-EA-6): was `time.sleep(0.3)` then ONE /health that had to be 503 (the server might not
        # have accepted the held sockets yet on a loaded box). Now /health until the server, having accepted them,
        # answers 503 — only 200 (not yet at the limit) may come first; a server that never refuses fails here.
        seen = _health_until(port, 503)
        # fix wave 25 (R-HYGIENE L1): `took < 1.0` dropped — a 503 is produced only by the over-limit path (a queued
        # request would be answered 200 or not at all)
        assert seen[-1] == 503 and set(seen) <= {200, 503}, seen
        # was `time.sleep(REQUEST_HEAD_TIMEOUT_S + 1)` then "all closed": now until the server has closed them all
        # (the head deadline), with a hang guard of REQUEST_HEAD_TIMEOUT_S + 20 s of polling
        assert _until_all_closed_by_peer(held, int((http_limits.REQUEST_HEAD_TIMEOUT_S + 20) / 0.1)), \
            [_closed_by_peer(s) for s in held].count(False)
        status, took = _health(port, timeout=3)
        assert status == 200, (status, took)
    finally:
        for s in held:
            s.close()
