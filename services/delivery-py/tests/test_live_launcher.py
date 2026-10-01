"""L2 (spec §F Live): the hardened launcher (serve.py) started as a real process on a port from 18800-18849 (or
``DLV_TEST_PORT_RANGE``) with a
clean, allowlisted environment: loopback bind (via _procinfo), the request-head cap and deadline, the concurrency
bound, /health shape, and the bind-address override. Skipped with the reason printed when no port is free."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from _procinfo import NO_OVERRIDE_ADDR_REASON, listening_addrs, override_bind_addr, rss_kib, url_host
from helpers import SERVICE_ROOT, base_env, child_python_args, free_live_port, make_repo

HTTP = httpx.Client(trust_env=False)          # never a proxy between the test and 127.0.0.x
SRC = SERVICE_ROOT / "src"
PYTHON = sys.executable      # wave 22: the interpreter running the suite (the service's venv, wherever it was built)
# Wave 21 (N20-D-1): a live run's log never rewrites the committed docs/evidence/dept28/live-launcher-run*.log files
# (frozen artefacts). Wave 24 (E6, N23-D-9): nor does it go anywhere in the source tree (it went to an untracked
# docs/evidence/dept28/_runs/): it is written under the session's temp dir (DLV_LIVE_LOG_DIR to keep it elsewhere).
LOG_DIR = Path(os.environ.get("DLV_LIVE_LOG_DIR") or os.path.join(tempfile.gettempdir(), "dlv-live-runs"))


def _free_port() -> int:
    """A port of the assigned live range (default 18800-18849; ``DLV_TEST_PORT_RANGE`` overrides — wave 21)."""
    return free_live_port()


def _start(tmp: str, extra: dict | None = None, host: str = "127.0.0.1"):
    port = _free_port()
    repo, _ = make_repo(tmp)
    env = base_env(tmp, repo, llm="none")
    env.update({"DLV_PORT": str(port), "DLV_BIND_ADDR": host, **(extra or {})})
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    log_path = Path(tmp) / "serve.log"
    log = open(log_path, "wb")
    proc = subprocess.Popen([PYTHON, *child_python_args(), "-m", "zbm_delivery.api"], cwd=str(SRC), env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 150
    while True:
        try:
            if HTTP.get(f"http://{url_host(host)}:{port}/health", timeout=1).status_code == 200:
                return proc, port, env, log_path
        except httpx.HTTPError:
            pass
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            log.close()
            raise RuntimeError("delivery-py did not start:\n" + log_path.read_text()[-3000:])
        time.sleep(0.2)


def _stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = str(tmp_path_factory.mktemp("live"))
    proc, port, env, log_path = _start(tmp)
    try:
        yield proc, port, env, log_path
    finally:
        _stop(proc)


def test_l2_bind_is_loopback_only_and_health_shape(server):
    proc, port, env, _ = server
    addrs = listening_addrs(port)
    assert addrs and addrs <= {"127.0.0.1"}, addrs
    r = HTTP.get(f"http://127.0.0.1:{port}/health", timeout=3)
    body = r.json()
    assert body["status"] == "ok" and body["ledger"] == "unconfigured" and body["llm"] == "unconfigured"
    assert body["sandbox"] == "unavailable"                       # this box has no Docker daemon (proven live)
    assert body["deerflow_commit"] == "345f08be00c8a9495079b732a39b46aa9af1584e" and body["in_memory"] is False
    # the ledger is unconfigured: a run is 503 with a reason; nothing is issued
    hdr = {"Authorization": f"Bearer {env['DLV_SERVICE_TOKEN']}", "X-DLV-Caller-Token": json.loads(env["DLV_CALLER_TOKENS"])["aegis"]}
    doc = {"request_id": "req-live-1", "source": {"kind": "aegis_review", "ref": "r", "sha256": "a" * 64}, "base_ref": "integration-2026-09-24",
           "base_sha": "0" * 40, "service": "toy-py", "findings": [{"id": "N1-1", "severity": "high", "title": "t",
                                                                    "file": "services/toy-py/src/toy/calc.py", "line": 6, "reproduction": "r",
                                                                    "expected": "e", "observed": "o"}]}
    r = HTTP.post(f"http://127.0.0.1:{port}/dlv/v1/fix-runs", json=doc, headers=hdr, timeout=10)
    assert r.status_code == 503 and r.json()["reasons"][0]["code"] == "SANDBOX_UNAVAILABLE"
    assert HTTP.get(f"http://127.0.0.1:{port}/dlv/v1/policy", headers=hdr, timeout=3).json()["policy_version"] == 1
    assert HTTP.get(f"http://127.0.0.1:{port}/docs", timeout=3).status_code == 404


def test_l2_oversized_header_is_refused_and_memory_stays_flat(server):
    proc, port, _, _ = server
    before = rss_kib(proc.pid)
    status = None
    sent = 0
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\nX-Big: ")
        chunk = b"a" * (1024 * 1024)
        try:
            for _ in range(200):
                s.sendall(chunk)
                sent += 1
        except OSError:
            pass
        try:
            s.settimeout(3)
            head = s.recv(4096)
            if head.startswith(b"HTTP/1.1 "):
                status = int(head[9:12])
        except OSError:
            pass
    assert status in (None, 400, 431), status
    assert sent < 200
    after = rss_kib(proc.pid)
    assert after - before < 64 * 1024, (before, after)
    assert HTTP.get(f"http://127.0.0.1:{port}/health", timeout=3).status_code == 200


def test_l2_idle_head_is_closed_within_the_deadline(server):
    _, port, _, _ = server
    with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
        s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\n")
        t0 = time.monotonic()
        s.settimeout(14)
        try:
            data = s.recv(16)
        except socket.timeout:
            pytest.fail("half-sent head was not closed within the deadline")
        assert data == b"" or data.startswith(b"HTTP/1.1 4")
        assert time.monotonic() - t0 <= 13


def test_l2_bind_override(tmp_path):
    host = override_bind_addr()
    if host is None:
        pytest.skip(NO_OVERRIDE_ADDR_REASON)
    proc, port, _, _ = _start(str(tmp_path), host=host)
    try:
        addrs = listening_addrs(port)
        assert addrs == {host}, addrs
    finally:
        _stop(proc)


def test_l2_refuses_to_start_with_a_stray_env_name(tmp_path):
    repo, _ = make_repo(str(tmp_path))
    env = base_env(str(tmp_path), repo, llm="none")
    env["OPENAI_API_KEY"] = "sk-nope"
    r = subprocess.run([PYTHON, *child_python_args(), "-m", "zbm_delivery.api"], cwd=str(SRC), env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode != 0 and "DLV_ENV_ALLOWLIST" in r.stderr


def test_l2_live_log_is_written(server, tmp_path):
    """The live-run log (spec brief): the exchange above, captured from the process, written to LOG_DIR (wave 24: the
    session's temp dir, never the source tree). Wave 21 (N20-D-1): it used to overwrite the tracked
    docs/evidence/dept28/live-launcher-run.log."""
    proc, port, env, log_path = server
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = LOG_DIR / "live-launcher-run.log"
    hdr = {"Authorization": f"Bearer {env['DLV_SERVICE_TOKEN']}", "X-DLV-Caller-Token": json.loads(env["DLV_CALLER_TOKENS"])["scheduler"]}
    lines = [f"# live launcher run — pid {proc.pid} port {port} — python {sys.version.split()[0]}", ""]
    for path, h in (("/health", {}), ("/dlv/v1/policy", hdr), ("/dlv/v1/audit/export", hdr), ("/dlv/v1/reconcile", hdr)):
        r = HTTP.get(f"http://127.0.0.1:{port}{path}", headers=h, timeout=5)
        lines.append(f"GET {path} -> {r.status_code} {r.text[:600]}")
    lines.append("")
    lines.append("listening: " + ", ".join(sorted(listening_addrs(port))))
    lines.append("server stdout/stderr tail:")
    lines.append(log_path.read_text()[-2000:])
    out.write_text("\n".join(lines) + "\n")
    print(f"live-run log: {out}")
    assert out.exists()


def test_l2_a_sigterm_stop_leaves_no_temp_dir_of_the_service(tmp_path):
    """Wave 24 (E6 sweep, found by the suite's /tmp check): uvicorn re-raises the SIGTERM it captured once its graceful
    shutdown is done; with the default disposition the process then died OF the signal (-15) and no atexit handler
    ran — every stop left the service's own temp dirs (gitport's dlv-git-*, the in-memory home, the sandbox base)
    in TMPDIR, in production as in the suite. Now the stop is a normal exit (143): the dirs are removed."""
    own_tmp = tmp_path / "svc-tmp"
    own_tmp.mkdir()
    (tmp_path / "w").mkdir()
    proc, port, _, _ = _start(str(tmp_path / "w"), extra={"TMPDIR": str(own_tmp)})
    try:
        assert HTTP.get(f"http://127.0.0.1:{port}/health", timeout=5).status_code == 200
        assert any(p.name.startswith("dlv-git-") for p in own_tmp.iterdir()), list(own_tmp.iterdir())
    finally:
        _stop(proc)
    assert proc.returncode == 143, proc.returncode
    assert sorted(p.name for p in own_tmp.iterdir()) == []


def test_l2_service_children_get_the_suites_temp_dir():
    """The other half: a child the suite has to SIGKILL (a hung start, say) runs no exit handler at all — its temp
    dirs must land under the session's temp root, which the suite removes, not in the host's /tmp (TMPDIR is on
    DLV_ENV_ALLOWLIST)."""
    env = base_env(tempfile.mkdtemp(), "/nonexistent-repo")
    assert env.get("TMPDIR") == tempfile.gettempdir(), env.get("TMPDIR")
