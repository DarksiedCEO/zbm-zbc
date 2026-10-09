"""Auth, docs and bind conventions (BUILD_CONTRACTS.md section 0), proven on
the module-level app AND on a real process bound to a real socket."""

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api import app
from _procinfo import NO_OVERRIDE_ADDR_REASON, listening_addrs, override_bind_addr, url_host
from conftest import TEST_SERVICE_TOKEN

SRC = Path(__file__).resolve().parents[1] / "src"
anon = TestClient(app)
auth = TestClient(app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})


def test_health_is_open():
    h = anon.get("/health").json()
    assert {k: h[k] for k in ("status", "service", "data_source")} == {"status": "ok", "service": "onboarding-py",
                                                                       "data_source": "non-live"}
    # bug sweep D: the local evidence log's state (no data dir configured here: in memory)
    assert h["in_memory"] is True and h["evidence_lines_owed"] == 0 and h["log_write_fault"] is False


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


# Fix wave 16: the kernel's socket table, read portably (Linux /proc/net/tcp
# and tcp6; macOS/BSD lsof), as text addresses. This test used to be skipped
# on macOS ("needs Linux /proc") and now runs there.
_listening = listening_addrs


def _start(extra):
    # Fix wave 26b (scout C5-6): the child is this test's only once IT holds the port (conftest.start_live, the
    # shared owner-checked helper); a 200 on a picked port used to be taken for its answer, whoever sent it.
    from conftest import start_live

    host = extra.get("ONBOARDING_BIND_ADDR", "127.0.0.1")

    def launch(port):
        env = dict(os.environ, ONBOARDING_SERVICE_TOKEN="entrypoint-test-token", ONBOARDING_PORT=str(port), **extra)
        return subprocess.Popen([sys.executable, "-m", "api"], cwd=SRC, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

    proc, port = start_live(launch, host=host)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"http://{url_host(host)}:{port}/health", timeout=0.5).status_code == 200:
                return proc, host, port
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    proc.wait()
    raise AssertionError("server did not start")


def test_entrypoint_binds_loopback_by_default_and_honours_override():
    proc, host, port = _start({})
    try:
        assert _listening(port) == {"127.0.0.1"}  # not 0.0.0.0 / :: (all interfaces)
        assert httpx.get(f"http://{host}:{port}/intelligences").status_code == 401
        assert httpx.get(f"http://{host}:{port}/docs").status_code == 404
    finally:
        proc.terminate()
        proc.wait(10)
    # 127.0.0.2 exists on Linux only; macOS proves the override with ::1.
    addr = override_bind_addr()
    if addr is None:
        pytest.skip(NO_OVERRIDE_ADDR_REASON + " (the default-bind half above passed)")
    proc, host, port = _start({"ONBOARDING_BIND_ADDR": addr})
    try:
        assert host == addr
        assert _listening(port) == {addr}
    finally:
        proc.terminate()
        proc.wait(10)
