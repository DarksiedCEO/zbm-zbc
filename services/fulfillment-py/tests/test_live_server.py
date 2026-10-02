"""
Real-socket tests: spawn the service as a real OS process through its own
entrypoint (`python3 -m api`), talk to it over TCP, and read the kernel's
socket table to prove what address it actually bound.

This is the layer where the Revenue Recovery bind/auth bugs hid behind a
green TestClient suite (root README findings #3, #4, #7). Added in the
Sep 24 2026 audit; before it, fulfillment-py had no entrypoint that
controlled its own bind address and no test above TestClient.
"""

from __future__ import annotations

import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from _procinfo import NO_OVERRIDE_ADDR_REASON, listening_addrs, override_bind_addr, port_free
from conftest import child_env

SRC = Path(__file__).resolve().parents[1] / "src"
TOKEN = "live-test-token-not-a-secret"


def _free_port() -> int:
    """An OS-assigned free port, or — when FULFILLMENT_TEST_PORT_RANGE="LO-HI"
    is set (fix wave 1: engineers run in assigned port ranges) — the first
    free port in that range, checked on every loopback address used here
    (127.0.0.1 plus the override address this OS supports, fix wave 16)."""
    rng = os.environ.get("FULFILLMENT_TEST_PORT_RANGE")
    if not rng:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
    lo, hi = (int(x) for x in rng.split("-"))
    hosts = ["127.0.0.1"] + [h for h in (override_bind_addr(),) if h]
    for port in range(lo, hi + 1):
        if all(port_free(host, port) for host in hosts):
            return port
    raise AssertionError(f"no free port in FULFILLMENT_TEST_PORT_RANGE={rng}")


# Fix wave 16: the kernel's socket table, read portably (Linux /proc/net/tcp
# and tcp6; macOS/BSD lsof). Returns text addresses, IPv4 and IPv6.
_listening_addrs = listening_addrs


_LOGS: dict[int, "tempfile._TemporaryFileWrapper"] = {}  # pid -> the server's captured output


def _announced_bind(log) -> bool:
    """Fix wave 25: whether the child ITSELF has announced that it is listening.
    uvicorn logs "Uvicorn running on" right after its bind, before the loop
    serves anything; a child that lost the port to another process logs the
    EADDRINUSE error and exits instead. Read with pread: the child writes at
    the file offset it shares with this descriptor, which must not move."""
    fd = log.fileno()
    return b"Uvicorn running on" in os.pread(fd, os.fstat(fd).st_size, 0)


def _start(env_extra: dict[str, str], _attempts: int = 5) -> tuple[subprocess.Popen, str, int]:
    """Fix wave 25: an answer on the port is this child's only once the child
    has announced its own bind (_announced_bind) and is still running — two
    test processes sharing FULFILLMENT_TEST_PORT_RANGE can pick the same free
    port, and the one whose child lost the bind used to take the winner's
    answer for its own server. A child that lost the port is retried on a
    fresh one, up to ``_attempts`` times (the N20-M-3 race)."""
    host = env_extra.get("FULFILLMENT_BIND_ADDR", "127.0.0.1")
    for attempt in range(_attempts):
        port = _free_port()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(SRC), **child_env(),
               "FULFILLMENT_SERVICE_TOKEN": TOKEN, "FULFILLMENT_PORT": str(port), **env_extra}
        # Fix wave 8: this piped stdout to PIPE and never drained it, so uvicorn's
        # access log (one line per request) filled the 64 KiB pipe after a few
        # hundred requests and blocked the server mid-test. A temp file is
        # unbounded and still lets an early exit be reported with its output.
        log = tempfile.TemporaryFile(mode="w+b")
        proc = subprocess.Popen([sys.executable, "-m", "api"], env=env, stdout=log, stderr=subprocess.STDOUT)
        _LOGS[proc.pid] = log
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                log.seek(0)
                output = log.read().decode(errors="replace")
                _close_log(proc)
                if "address already in use" in output.lower() and attempt + 1 < _attempts:
                    time.sleep(0.2)
                    break
                raise AssertionError(f"service exited early: {output}")
            if not _announced_bind(log):
                time.sleep(0.05)
                continue
            try:
                with socket.create_connection((host, port), timeout=0.2):
                    pass
            except OSError:
                time.sleep(0.1)
                continue
            if proc.poll() is None:
                return proc, host, port
        else:
            proc.kill()
            proc.wait()
            _close_log(proc)
            raise AssertionError("service did not start listening within 15s")
    raise AssertionError("service could not bind a free port")


def _close_log(proc: subprocess.Popen) -> None:
    log = _LOGS.pop(proc.pid, None)
    if log is not None:
        log.close()


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    _close_log(proc)


def _get(host: str, port: int, path: str, auth: str | bytes | None = None) -> int:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.putrequest("GET", path)
    if auth is not None:
        conn.putheader("Authorization", auth)
    conn.endheaders()
    status = conn.getresponse().status
    conn.close()
    return status


@pytest.fixture(scope="module")
def default_server():
    proc, host, port = _start({})
    yield host, port
    _stop(proc)


def test_entrypoint_binds_loopback_by_default(default_server):
    _, port = default_server
    assert _listening_addrs(port) == {"127.0.0.1"}  # not 0.0.0.0 / :: (all interfaces)


def test_bind_addr_env_override_is_honored():
    # Fix wave 16: 127.0.0.2 exists on Linux only; macOS gets ::1 instead.
    # Either way the socket table must show exactly the requested address.
    addr = override_bind_addr()
    if addr is None:
        pytest.skip(NO_OVERRIDE_ADDR_REASON)
    proc, host, port = _start({"FULFILLMENT_BIND_ADDR": addr})
    try:
        assert host == addr
        assert _listening_addrs(port) == {addr}
        assert _get(host, port, "/health") == 200
    finally:
        _stop(proc)


def test_live_health_open_and_routes_authenticated(default_server):
    host, port = default_server
    assert _get(host, port, "/health") == 200
    assert _get(host, port, "/fixtures/call-events") == 401
    assert _get(host, port, "/fixtures/call-events", "Bearer wrong") == 401
    assert _get(host, port, "/fixtures/call-events", f"Bearer {TOKEN}") == 200


def test_live_non_ascii_token_is_401_not_500(default_server):
    host, port = default_server
    assert _get(host, port, "/fixtures/call-events", "Bearer café".encode("utf-8")) == 401


def test_live_docs_are_disabled(default_server):
    host, port = default_server
    for path in ["/docs", "/redoc", "/openapi.json"]:
        assert _get(host, port, path) == 404


def test_malformed_contact_window_refuses_to_start():
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(SRC), **child_env(),
           "FULFILLMENT_SERVICE_TOKEN": TOKEN, "FULFILLMENT_CONTACT_WINDOW": "06:00-23:00"}
    r = subprocess.run([sys.executable, "-c", "import api"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "FULFILLMENT_CONTACT_WINDOW" in r.stderr


def test_a_server_started_here_survives_thousands_of_logged_requests(default_server):
    """Fix wave 8 (harness): 1 500 requests write ~150 KB of access log; with
    stdout on an undrained PIPE (64 KiB) the server blocked around the 500th."""
    host, port = default_server
    # Fix wave 25 (R-HYGIENE L1): no wall-clock bound — a server blocked on its log pipe answers nothing, and the
    # connection's 5 s timeout raises (it used to also assert < 60 s overall, which a slow box could trip without a
    # block and a block could never reach).
    conn = http.client.HTTPConnection(host, port, timeout=5)
    for _ in range(1500):
        conn.request("GET", "/health")
        assert conn.getresponse().read()
    conn.close()
