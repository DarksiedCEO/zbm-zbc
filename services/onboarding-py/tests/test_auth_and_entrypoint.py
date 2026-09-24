"""Auth, docs and bind conventions (BUILD_CONTRACTS.md section 0), proven on
the module-level app AND on a real process bound to a real socket."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api import app
from conftest import TEST_SERVICE_TOKEN

SRC = Path(__file__).resolve().parents[1] / "src"
anon = TestClient(app)
auth = TestClient(app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})


def test_health_is_open():
    assert anon.get("/health").json() == {"status": "ok", "service": "onboarding-py", "data_source": "non-live"}


@pytest.mark.parametrize("method,path", [
    ("get", "/intelligences"), ("post", "/onboarding/clients"), ("get", "/onboarding/clients/x"),
    ("post", "/onboarding/clients/x/messages"), ("post", "/zbc/creators/applications"), ("get", "/playbook"),
    ("post", "/onboarding/clients/x/access/credentials"), ("get", "/onboarding/escalations"),
])
def test_every_route_rejects_missing_wrong_malformed_and_non_ascii_tokens(method, path):
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": TEST_SERVICE_TOKEN}):
        assert getattr(TestClient(app, headers=headers), method)(path).status_code == 401
    # raw bytes: httpx refuses non-ASCII str headers client-side
    assert getattr(TestClient(app, headers={"Authorization": b"Bearer caf\xc3\xa9"}), method)(path).status_code == 401


def test_correct_token_accepted():
    assert auth.get("/intelligences").status_code == 200


def test_docs_redoc_openapi_disabled():
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert anon.get(p).status_code == 404
        assert auth.get(p).status_code == 404


def test_default_app_uses_fail_closed_stand_ins():
    # No ledger configured in the test env: a mutating call is refused with 503.
    r = auth.post("/onboarding/clients", json={"client_id": "c1", "business_name": "B", "time_zone": "UTC",
                                               "signer": {"name": "A B", "email": "a@b.co"}})
    assert r.status_code == 503 and r.json()["proceeded"] is False


def test_refuses_to_start_without_token():
    env = {k: v for k, v in os.environ.items() if k != "ONBOARDING_SERVICE_TOKEN"}
    p = subprocess.run([sys.executable, "-c", "import api"], cwd=SRC, env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode != 0 and "ONBOARDING_SERVICE_TOKEN is not set" in p.stderr


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port):
    addrs = set()
    for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        parts = line.split()
        ip, p = parts[1].split(":")
        if int(p, 16) == port and parts[3] == "0A":
            addrs.add(ip)
    return addrs


def _start(extra):
    port = _free_port()
    env = dict(os.environ, ONBOARDING_SERVICE_TOKEN="entrypoint-test-token", ONBOARDING_PORT=str(port), **extra)
    proc = subprocess.Popen([sys.executable, "-m", "api"], cwd=SRC, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    host = extra.get("ONBOARDING_BIND_ADDR", "127.0.0.1")
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"http://{host}:{port}/health", timeout=0.5).status_code == 200:
                return proc, host, port
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    raise AssertionError("server did not start")


@pytest.mark.skipif(not Path("/proc/net/tcp").exists(), reason="needs Linux /proc")
def test_entrypoint_binds_loopback_by_default_and_honours_override():
    proc, host, port = _start({})
    try:
        assert _listening(port) == {"0100007F"}  # 127.0.0.1, not 00000000 (0.0.0.0)
        assert httpx.get(f"http://{host}:{port}/intelligences").status_code == 401
        assert httpx.get(f"http://{host}:{port}/docs").status_code == 404
    finally:
        proc.terminate()
        proc.wait(10)
    proc, host, port = _start({"ONBOARDING_BIND_ADDR": "127.0.0.2"})
    try:
        assert _listening(port) == {"0200007F"}
    finally:
        proc.terminate()
        proc.wait(10)
