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
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
TOKEN = "live-test-token-not-a-secret"


def _free_port() -> int:
    """An OS-assigned free port, or — when FULFILLMENT_TEST_PORT_RANGE="LO-HI"
    is set (fix wave 1: engineers run in assigned port ranges) — the first
    free port in that range, checked on both loopback addresses used here."""
    rng = os.environ.get("FULFILLMENT_TEST_PORT_RANGE")
    if not rng:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
    lo, hi = (int(x) for x in rng.split("-"))
    for port in range(lo, hi + 1):
        try:
            for host in ("127.0.0.1", "127.0.0.2"):
                with socket.socket() as s:
                    s.bind((host, port))
            return port
        except OSError:
            continue
    raise AssertionError(f"no free port in FULFILLMENT_TEST_PORT_RANGE={rng}")


def _listening_addrs(port: int) -> set[str]:
    """Hex local addresses in LISTEN state (st=0A) for `port`, from /proc/net/tcp."""
    out = set()
    for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        fields = line.split()
        addr, port_hex = fields[1].split(":")
        if int(port_hex, 16) == port and fields[3] == "0A":
            out.add(addr)
    return out


def _start(env_extra: dict[str, str]) -> tuple[subprocess.Popen, str, int]:
    port = _free_port()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(SRC),
           "FULFILLMENT_SERVICE_TOKEN": TOKEN, "FULFILLMENT_PORT": str(port), **env_extra}
    proc = subprocess.Popen([sys.executable, "-m", "api"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    host = env_extra.get("FULFILLMENT_BIND_ADDR", "127.0.0.1")
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"service exited early: {proc.stdout.read().decode(errors='replace')}")
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return proc, host, port
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise AssertionError("service did not start listening within 15s")


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


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
    assert _listening_addrs(port) == {"0100007F"}  # 127.0.0.1, not 00000000 (0.0.0.0)


def test_bind_addr_env_override_is_honored():
    proc, host, port = _start({"FULFILLMENT_BIND_ADDR": "127.0.0.2"})
    try:
        assert _listening_addrs(port) == {"0200007F"}
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
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(SRC),
           "FULFILLMENT_SERVICE_TOKEN": TOKEN, "FULFILLMENT_CONTACT_WINDOW": "06:00-23:00"}
    r = subprocess.run([sys.executable, "-c", "import api"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "FULFILLMENT_CONTACT_WINDOW" in r.stderr
