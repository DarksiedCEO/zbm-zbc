"""ADR 0028: the real launcher (``cd src && python3 -m api``): it refuses to start without its secrets, and when it
runs, uvicorn sends no Server header and logs no request path (the path holds the token)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from helpers import ENV

SRC = Path(__file__).resolve().parents[1] / "src"
OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open     # never through a proxy


def _env(**over):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PORTAL_")}
    env.update(ENV)
    env.update({k: v for k, v in over.items() if v is not None})
    for k, v in over.items():
        if v is None:
            env.pop(k, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


@pytest.mark.parametrize("missing", ["PORTAL_LEGAL_SERVICE_TOKEN", "PORTAL_LEGAL_CALLER_TOKEN", "PORTAL_EVIDENCE_KEY"])
def test_the_launcher_refuses_to_start_without_a_secret(missing):
    p = subprocess.run([sys.executable, "-m", "api"], cwd=SRC, env=_env(**{missing: None}), capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 1 and f"RuntimeError: {missing} is not set" in p.stderr


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.live
def test_the_launcher_serves_health_without_a_server_header_or_an_access_log():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "api"], cwd=SRC, env=_env(PORTAL_PORT=str(port)),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline, headers = time.time() + 30, None
        while time.time() < deadline:
            try:
                with OPEN(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    assert r.status == 200 and json.loads(r.read())["status"] == "ok"
                    headers = {k.lower(): v for k, v in r.headers.items()}
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        assert headers is not None, "the launcher never answered /health"
        assert "server" not in headers
        assert headers["content-security-policy"].startswith("default-src 'none'")
        try:
            OPEN(f"http://127.0.0.1:{port}/accept/{'K' * 43}", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code in (404, 503)          # Legal is not running: a 503; never anything else
    finally:
        proc.terminate()
        out, _ = proc.communicate(timeout=20)
    assert "K" * 43 not in out and "/accept/" not in out
    assert proc.returncode in (0, -15)
