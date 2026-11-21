"""Bounded local HTTP controls; no real Finance/provider invocation."""
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import housekeeping_runner as R

NOW = datetime(2026, 11, 1, 9, 59, tzinfo=timezone.utc)


def env(origin="https://finance.example.test", **extra):
    return {"FIN_HOUSEKEEPING_ENABLED": "1", "FIN_HOUSEKEEPING_ORIGIN": origin,
            "FIN_HOUSEKEEPING_SERVICE_TOKEN": secrets.token_hex(32),
            "FIN_HOUSEKEEPING_CALLER_TOKEN": secrets.token_hex(32), **extra}


def response(request_id):
    return {"job": "stripe-sessions", "request_id": request_id, "day": "2026-11-01", "already_ran": False,
            "summary": {key: 0 for key in R.COUNTERS}}


@pytest.fixture
def server():
    state = {"calls": [], "mode": "ok", "status": 200, "secret": secrets.token_hex(32)}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            state["calls"].append((self.path, dict(self.headers), json.loads(raw)))
            body = response(json.loads(raw)["request_id"])
            mode = state["mode"]
            if mode == "degraded": body["summary"]["activation_unknown"] = body["summary"]["activation_checked"] = 1
            if mode == "failed": body["summary"].update(checked=1, failed=1)
            if mode == "key": body["summary"][state["secret"]] = 1
            if mode == "bool": body["summary"]["closed"] = True
            if mode == "mismatch": body["request_id"] = state["secret"]
            raw = json.dumps(body).encode()
            if mode == "malformed": raw = b'{broken'
            if mode == "oversize": raw = b' ' * (R.MAX_RESPONSE + 1)
            if mode == "empty": raw = b''
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            if state["status"] == 302: self.send_header("Location", state["origin"] + "/redirected")
            self.end_headers()
            try:
                if mode == "trickle":
                    for _ in range(100):
                        self.wfile.write(b' '); self.wfile.flush(); time.sleep(0.1)
                else:
                    self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["origin"] = f"http://127.0.0.1:{srv.server_port}"
    thread = threading.Thread(target=srv.serve_forever)
    thread.start()
    yield state
    srv.shutdown(); srv.server_close(); thread.join(5)
    assert not thread.is_alive()


def test_disabled_never_spawns(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("disabled spawned"))
    r = R.run({}, NOW)
    assert r["status"] == "disabled" and r["attempts"] == 0


@pytest.mark.parametrize("origin", ["http://example.test", "https://user@finance.test", "https://x.test/path",
    "https://x.test?x=1", "https://x.test#fragment", "https://x.test:65536", "https://x.test:0",
    "https://x..test", "https://-x.test", "https://x.test\n", "ftp://x.test", "https://x.test\\evil"])
def test_invalid_origin_never_spawns(origin, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("invalid spawned"))
    assert R.run(env(origin), NOW)["status"] == "invalid_settings"


@pytest.mark.parametrize("setting,value", [("FIN_HOUSEKEEPING_ENABLED", "true"),
    ("FIN_HOUSEKEEPING_CALLER_TOKEN", "short"), ("FIN_HOUSEKEEPING_CALLER_TOKEN", "${{missing_reference}}"),
    ("FIN_HOUSEKEEPING_DEADLINE_SECONDS", "0"), ("FIN_HOUSEKEEPING_DEADLINE_SECONDS", "121"),
    ("FIN_HOUSEKEEPING_DEADLINE_SECONDS", "NaN")])
def test_invalid_settings(setting, value, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("invalid spawned"))
    e = env(); e[setting] = value
    assert R.run(e, NOW)["status"] == "invalid_settings"


def test_hour_ids_aware_normalized_and_dst():
    a = R.request_id(NOW)
    assert a == R.request_id(NOW.replace(minute=0))
    assert a == R.request_id(NOW.astimezone(timezone(timedelta(hours=-8))))
    assert a != R.request_id(NOW - timedelta(hours=1))
    with pytest.raises(ValueError): R.request_id(NOW.replace(tzinfo=None))


def test_success_real_worker_proxy_disabled_and_no_secret_argv(server, monkeypatch):
    e = env(server["origin"])
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    original = subprocess.Popen
    commands = []
    def observe(*args, **kwargs):
        commands.append(args[0]); return original(*args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", observe)
    r = R.run(e, NOW)
    assert r["status"] == "completed" and r["attempts"] == 1
    assert len(server["calls"]) == 1 and server["calls"][0][0] == R.JOB_PATH
    assert server["calls"][0][1]["Authorization"] == "Bearer " + e["FIN_HOUSEKEEPING_SERVICE_TOKEN"]
    assert e["FIN_HOUSEKEEPING_SERVICE_TOKEN"] not in json.dumps([commands, r])
    assert e["FIN_HOUSEKEEPING_CALLER_TOKEN"] not in json.dumps([commands, r])


@pytest.mark.parametrize("mode,status", [("degraded", "degraded"), ("failed", "degraded"),
    ("key", "outcome_unknown"), ("bool", "outcome_unknown"), ("mismatch", "outcome_unknown"),
    ("malformed", "outcome_unknown"), ("oversize", "outcome_unknown"), ("empty", "outcome_unknown")])
def test_response_controls(server, mode, status):
    server["mode"] = mode
    result = R.run(env(server["origin"]), NOW)
    assert result["status"] == status and R.exit_code(result) != 0
    assert server["secret"] not in json.dumps(result) and len(server["calls"]) == 1


@pytest.mark.parametrize("code", [201, 204, 302, 401, 403, 409, 500, 503])
def test_http_not_completed_and_no_redirect_retry(server, code):
    server["status"] = code
    result = R.run(env(server["origin"]), NOW)
    assert result["status"] == ("refused" if code in (401, 403) else "outcome_unknown")
    assert len(server["calls"]) == 1 and R.exit_code(result) != 0


def test_trickle_deadline_kills_owned_worker(server, monkeypatch):
    server["mode"] = "trickle"
    original = subprocess.Popen
    children = []
    def capture(*args, **kwargs):
        p = original(*args, **kwargs); children.append(p); return p
    monkeypatch.setattr(subprocess, "Popen", capture)
    start = time.monotonic()
    r = R.run(env(server["origin"], FIN_HOUSEKEEPING_DEADLINE_SECONDS="1"), NOW)
    assert r["status"] == "outcome_unknown" and time.monotonic() - start < 3
    assert len(children) == 1 and children[0].poll() is not None
    with pytest.raises(ProcessLookupError): os.kill(children[0].pid, 0)
    assert len(server["calls"]) == 1


def test_dns_is_inside_owned_worker_deadline(monkeypatch):
    # Same worker, delay actual DNS entry before any socket connect; no remote address is contacted.
    code = ("import runpy,socket,time; n=runpy.run_path(" + repr(R.__file__) + "); "
            "socket.getaddrinfo=lambda *a,**k: time.sleep(10); n['_worker_main']()")
    monkeypatch.setattr(R, "_worker_command", lambda: [sys.executable, "-I", "-B", "-c", code])
    original = subprocess.Popen
    children = []
    def capture(*args, **kwargs):
        p = original(*args, **kwargs); children.append(p); return p
    monkeypatch.setattr(subprocess, "Popen", capture)
    start = time.monotonic()
    r = R.run(env(FIN_HOUSEKEEPING_DEADLINE_SECONDS="1"), NOW)
    assert r["status"] == "outcome_unknown" and time.monotonic() - start < 3
    assert len(children) == 1 and children[0].poll() is not None
    with pytest.raises(ProcessLookupError): os.kill(children[0].pid, 0)


@pytest.mark.parametrize("mode", ["duplicate", "nonfinite", "invalid_day", "missing_counter", "extra_key",
                                  "negative", "oversized_counter", "bad_total", "bad_type", "contradictory_activation"])
def test_strict_response_parser(mode):
    rid = R.request_id(NOW)
    body = response(rid)
    if mode == "invalid_day": body["day"] = "20261101"
    if mode == "missing_counter": body["summary"].pop("failed")
    if mode == "extra_key": body[secrets.token_hex(32)] = 1
    if mode == "negative": body["summary"]["holds_changed"] = -1
    if mode == "oversized_counter": body["summary"]["holds_changed"] = 1_000_000_001
    if mode == "bad_total": body["summary"]["checked"] = 1
    if mode == "bad_type": body["summary"]["checked"] = "0"
    if mode == "contradictory_activation": body["summary"].update(refused_clients=1, activation_unknown=1, activation_checked=1)
    raw = json.dumps(body).encode()
    if mode == "duplicate": raw = raw.replace(b'"closed": 0', b'"closed": 0, "closed": 0')
    if mode == "nonfinite": raw = raw.replace(b'"holds_changed": 0', b'"holds_changed": NaN')
    with pytest.raises(ValueError): R._completed(raw, rid)


@pytest.mark.parametrize("mode", ["empty", "oversize", "unexpected", "exit"])
def test_bad_worker_output_is_unknown(mode, monkeypatch):
    if mode == "empty": code = "pass"
    elif mode == "oversize": code = "print('x'*5000)"
    elif mode == "unexpected": code = "print('{\"unexpected\":true}')"
    else: code = "raise SystemExit(2)"
    monkeypatch.setattr(R, "_worker_command", lambda: [sys.executable, "-I", "-B", "-c", code])
    assert R.run(env(), NOW)["status"] == "outcome_unknown"


def test_unavailable_owned_local_port_unknown_no_retry():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        # Keep ownership but never listen; worker cannot connect to another service on this port.
        r = R.run(env(f"http://127.0.0.1:{port}"), NOW)
    assert r["status"] == "outcome_unknown" and r["attempts"] == 1


def test_same_hour_runs_are_at_least_once_not_deduplicated(server):
    e = env(server["origin"])
    a, b = R.run(e, NOW), R.run(e, NOW.replace(minute=0))
    assert a["status"] == b["status"] == "completed"
    assert len(server["calls"]) == 2
    assert server["calls"][0][2] == server["calls"][1][2]


@pytest.mark.parametrize("bad", ["same", "newline", "space", "template", "long"])
def test_credentials_are_strict_and_repr_private(bad):
    e = env()
    if bad == "same": e[PREFIX_KEY] = e["FIN_HOUSEKEEPING_SERVICE_TOKEN"]
    if bad == "newline": e[PREFIX_KEY] += "\n"
    if bad == "space": e[PREFIX_KEY] += " "
    if bad == "template": e[PREFIX_KEY] += "{{x}}"
    if bad == "long": e[PREFIX_KEY] = "x" * 4097
    assert R.run(e, NOW)["status"] == "invalid_settings"
    valid = env()
    assert valid[PREFIX_KEY] not in repr(R._settings(valid))


PREFIX_KEY = "FIN_HOUSEKEEPING_CALLER_TOKEN"


def test_disabled_cli_never_opens_finance_volume(tmp_path):
    volume = tmp_path / "finance-volume"
    volume.mkdir()
    marker = volume / "owned-marker"
    marker.write_bytes(b"unchanged")
    completed = subprocess.run([sys.executable, "-I", "-B", R.__file__],
                               env={"PATH": os.defpath, "FIN_DATA_DIR": str(volume)},
                               capture_output=True, timeout=5, check=False)
    assert completed.returncode == 0 and not completed.stderr
    assert json.loads(completed.stdout)["status"] == "disabled"
    assert list(volume.iterdir()) == [marker] and marker.read_bytes() == b"unchanged"
