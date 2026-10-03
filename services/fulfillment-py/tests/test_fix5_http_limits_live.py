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

import pytest

from _procinfo import rss_kib
from test_live_server import SRC, TOKEN, _announced_bind, _free_port

import http_limits
from conftest import child_env

MIB = 1024 * 1024
BODY_TIMEOUT_UNDER_TEST = 3.0  # narrowed via FULFILLMENT_BODY_READ_TIMEOUT_S (may only narrow)
SLACK_S = 2.5


def _start(env_extra: dict[str, str] | None = None, _attempts: int = 5):
    """Start the service on a free port. Fix wave 21: the output goes to a
    temp file (not DEVNULL: an early exit is reported with it; not PIPE: an
    unread pipe would block the server), and a start that lost the port to
    another process between the free-port check and the bind (EADDRINUSE,
    the N20-M-3 race) is retried on a fresh port, up to ``_attempts`` times.
    Fix wave 25: /health answering is this child's answer only once the child
    has announced its own bind (test_live_server._announced_bind) — the race's
    loser used to take the winner's 200 for its own server and return a
    process about to exit."""
    import tempfile
    for attempt in range(_attempts):
        port = _free_port()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(SRC), **child_env(),
               "FULFILLMENT_SERVICE_TOKEN": TOKEN, "FULFILLMENT_PORT": str(port), **(env_extra or {})}
        log = tempfile.TemporaryFile(mode="w+b")
        proc = subprocess.Popen([sys.executable, "-m", "api"], env=env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                log.seek(0)
                output = log.read().decode(errors="replace")
                log.close()
                if "address already in use" in output.lower() and attempt + 1 < _attempts:
                    time.sleep(0.2)
                    break
                raise AssertionError(f"service exited early (port {port}): {output[-2000:]}")
            if not _announced_bind(log):
                time.sleep(0.05)
                continue
            try:
                if _health(port, timeout=0.5)[0] == 200 and proc.poll() is None:
                    log.close()          # the child keeps its own descriptor
                    return proc, port
            except OSError:
                time.sleep(0.1)
        else:
            proc.kill()
            proc.wait()
            log.close()
            raise AssertionError("service did not start within 15s")
    raise AssertionError("service could not bind a free port")


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


def _closed_before_witness(s: socket.socket, witness: socket.socket, attempts: int, poke=None) -> tuple[bool, bool]:
    """Fix wave 26b (W25-EA-7): polls every 0.25 s (at most `attempts` times, a hang guard) until `s` or `witness` is
    closed by the server; returns (s closed, witness still open AFTER s was seen closed). `s` is checked first and the
    witness after it, so (True, True) proves `s` was closed while the witness was still open. `poke(s)` runs before
    each check (a trickled byte); an OSError from it counts as `s` closed."""
    for _ in range(attempts):
        closed = False
        if poke is not None:
            try:
                poke(s)
            except (BrokenPipeError, ConnectionResetError):
                closed = True
        if closed or _closed_by_peer(s):
            return True, not _closed_by_peer(witness)
        if _closed_by_peer(witness):
            return False, False
        time.sleep(0.25)
    return False, not _closed_by_peer(witness)


def test_idle_keep_alive_connection_is_closed(server):
    """Fix wave 26b (W25-EA-7): was `elapsed < KEEP_ALIVE_TIMEOUT_S + SLACK_S` — a wall-clock bound against a named
    constant (it moves with the constant, and load can cross it). Now an ORDER: a witness socket that connects just
    before and sends nothing can only be closed by the head deadline (REQUEST_HEAD_TIMEOUT_S, 10 s from its connect);
    the idle keep-alive (KEEP_ALIVE_TIMEOUT_S, 5 s after its response) must be closed while the witness is still
    open. A keep-alive not enforced is closed by nothing earlier than the head deadline, i.e. after the witness. Both
    timers run on the server's own loop clock, so load delays them alike."""
    _, port = server
    assert http_limits.KEEP_ALIVE_TIMEOUT_S < http_limits.REQUEST_HEAD_TIMEOUT_S   # the order the test relies on
    witness = socket.create_connection(("127.0.0.1", port), timeout=5)
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\n\r\n")
        assert _read_status(s) == 200
        closed, witness_open = _closed_before_witness(s, witness, int((http_limits.REQUEST_HEAD_TIMEOUT_S + 20) / 0.25))
        assert closed, "idle keep-alive held open (not even the head deadline closed it)"
        assert witness_open, "the idle keep-alive was closed only after the head deadline closed the witness"
    finally:
        s.close()
        witness.close()


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
    # fix wave 25 (R-HYGIENE L1): was `elapsed <= BODY_TIMEOUT_UNDER_TEST + SLACK_S` (a wall-clock upper bound). The
    # 408's detail names the rule that cut the body: only the narrowed body deadline (3 s) says "not received within
    # 3s" — the 30 s default would say 30s, the min-rate rule "stalled ... slower than", the projection "cannot
    # complete". The loop above gives up at BODY_TIMEOUT_UNDER_TEST + SLACK_S + 3 s, so a held body still fails.
    assert f"not received within {BODY_TIMEOUT_UNDER_TEST:g}s".encode() in got, got[:400]
    print(f"slow body cut after {elapsed:.1f}s (printed only)")


def test_slow_body_the_app_never_reads_is_still_closed(server):
    """No token: the route answers 401 before reading the body. Before the
    fix, the rest of the body could then be trickled forever (each byte
    reset uvicorn's keep-alive timer)."""
    # Fix wave 26b (W25-EA-7): was `elapsed < BODY_TIMEOUT_UNDER_TEST + BODY_DEADLINE_GRACE_S + SLACK_S` (a wall-clock
    # bound against named constants). Now an ORDER against a witness that connects first and sends nothing — only
    # the head deadline closes it, REQUEST_HEAD_TIMEOUT_S (10 s) from its connect; the unread body's deadline is
    # BODY_TIMEOUT_UNDER_TEST + BODY_DEADLINE_GRACE_S (8 s) from its head. Before the fix nothing closed a trickled
    # unread body (each byte reset uvicorn's keep-alive timer), so it would outlive the witness.
    _, port = server
    assert BODY_TIMEOUT_UNDER_TEST + http_limits.BODY_DEADLINE_GRACE_S < http_limits.REQUEST_HEAD_TIMEOUT_S
    witness = socket.create_connection(("127.0.0.1", port), timeout=5)
    s = _slow_body_socket(port, authed=False)
    try:
        closed, witness_open = _closed_before_witness(s, witness, int((http_limits.REQUEST_HEAD_TIMEOUT_S + 20) / 0.25),
                                                      poke=lambda sock: sock.sendall(b" "))
        assert closed, "unread slow body held open (past the head deadline and 20 s more)"
        assert witness_open, "the unread slow body was closed only after the head deadline closed the witness"
    finally:
        s.close()
        witness.close()


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
            status, _took = _health(port, timeout=3)  # the 3 s client timeout is the "never a hang" guard
            # fix wave 25 (E-A successor, R-HYGIENE L1): `took < 1.0` dropped (wall clock under load)
            assert status in (200, 503, None)
        except (ConnectionResetError, BrokenPipeError):
            pass
        # the held sockets never sent a head: all gone after the head deadline
        time.sleep(http_limits.REQUEST_HEAD_TIMEOUT_S + 1)
        assert all(_closed_by_peer(s) for s in socks)
    finally:
        for s in socks:
            s.close()
    status, _took = _health(port)
    assert status == 200  # fix wave 25 (E-A successor, R-HYGIENE L1): `took < 1.0` dropped; served after the flood


def test_body_timeout_env_may_only_narrow():
    for bad in ("0", "-1", "31", "nan", "abc"):
        port = _free_port()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(SRC), **child_env(),
               "FULFILLMENT_SERVICE_TOKEN": TOKEN, "FULFILLMENT_PORT": str(port),
               "FULFILLMENT_BODY_READ_TIMEOUT_S": bad}
        r = subprocess.run([sys.executable, "-m", "api"], env=env, capture_output=True, timeout=20)
        assert r.returncode != 0, bad
        assert b"FULFILLMENT_BODY_READ_TIMEOUT_S" in r.stderr, r.stderr[-400:]
