#!/usr/bin/env python3
"""
Revenue Recovery store connector live run (ADR 0020): the REAL ledger-rust binary, detection-py's supported launcher
(``cd src && python3 serve.py``) and the REAL orchestrator-go binary (built here with ``go build``), over real HTTP on
loopback. No Shopify credential exists, so this proves the NOT_CONNECTED posture end to end.

usage: LEDGER_BIN=/path/to/ledger-rust/target/release/server python3 devtools/live_run.py

detection-py runs under an audit hook (``sys.addaudithook``) that writes every ``socket.connect`` and
``socket.getaddrinfo`` the process attempts to a file; the run fails if there is even one. Proves:
  1. a client with no registration: status NOT_CONNECTED, sync and snapshot 409 NOT_CONNECTED;
  2. orchestrator-go's scan of that client: 409 with reason NOT_CONNECTED, and the ledger did not change;
  3. a registered client (a placeholder credential file, never a real token) while detection-py has no ledger: sync
     LEDGER_UNAVAILABLE (record-first: nothing is read without the request on the ledger); snapshot NOT_SYNCED; the
     orchestrator's scan 409 NOT_SYNCED;
  4. the kill switch file: sync and snapshot STORE_READS_DISABLED at once, no restart;
  5. the fixture scan still works (data_source fixtures, non_live_data_source true) and GET /ledger/verify is valid;
  6. detection-py attempted no outbound connection and no name lookup other than its own loopback bind address.

Kills only the PIDs it started. Ports are free ports from bind(0). Tokens are derived here (sha256 of a live-run
label), never secret-shaped literals. Exit 0 only if every check holds.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve()
SVC = HERE.parents[1]
REPO = HERE.parents[3]


def derived(label: str) -> str:
    return hashlib.sha256(f"detection-py rr-store live-run only {label}".encode()).hexdigest()


LEDGER_TOKEN = derived("ledger token")
DETECTION_TOKEN = derived("detection token")
ORCH_TOKEN = derived("orchestrator token")
PROCS: list[subprocess.Popen] = []
CHECKS: list[tuple[str, bool, str]] = []

AUDIT_BOOT = r"""
import sys, runpy, os
_log = open(os.environ["RR_LIVE_AUDIT_FILE"], "a", buffering=1)
def _hook(event, args):
    if event in ("socket.connect", "socket.getaddrinfo"):
        _log.write(f"{event} {args[1] if event == 'socket.connect' else args[0]!r}\n")
sys.addaudithook(_hook)
import socket as _s
_probe = _s.socket()
try:  # positive control: a loopback connect to a closed port must appear in the audit file
    _probe.connect(("127.0.0.1", int(os.environ["RR_LIVE_AUDIT_PROBE_PORT"])))
except OSError:
    pass
finally:
    _probe.close()
sys.argv = ["serve.py"] + sys.argv[1:]
runpy.run_path("serve.py", run_name="__main__")
"""


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""), flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(url: str, proc: subprocess.Popen, what: str, deadline_s: float = 60.0) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise SystemExit(f"{what} exited early ({proc.returncode})")
        try:
            if httpx.get(url, timeout=1.0, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise SystemExit(f"{what} did not become healthy")


def start(cmd: list[str], env: dict, cwd: Path, log: Path) -> subprocess.Popen:
    p = subprocess.Popen(cmd, env=env, cwd=str(cwd), stdout=open(log, "ab"), stderr=subprocess.STDOUT)
    PROCS.append(p)
    return p


def main() -> int:
    ledger_bin = os.environ.get("LEDGER_BIN")
    if not ledger_bin or not os.access(ledger_bin, os.X_OK):
        print("LEDGER_BIN must name the ledger-rust release server binary", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="rr-store-live-"))
    base_env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "GOPATH", "GOCACHE",
                                                             "GOMODCACHE", "GOFLAGS", "GOTELEMETRY")}
    base_env["NO_PROXY"] = base_env["no_proxy"] = "127.0.0.1,localhost"
    try:
        # ---- ledger-rust
        lp = free_port()
        led_env = {**base_env, "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_LOG_PATH": str(work / "ledger.jsonl"),
                   "LEDGER_PORT": str(lp), "LEDGER_BIND_ADDR": "127.0.0.1"}
        led = start([ledger_bin], led_env, work, work / "ledger.log")
        lurl = f"http://127.0.0.1:{lp}"
        wait_health(f"{lurl}/health", led, "ledger-rust")
        lh = {"Authorization": f"Bearer {LEDGER_TOKEN}"}

        def ledger_entries() -> int:
            return len(httpx.get(f"{lurl}/ledger/entries", headers=lh, timeout=10, trust_env=False).json())

        # ---- detection-py: one registered placeholder connection, no detection ledger, a kill-switch path
        placeholder = work / "lr-connected.token"
        placeholder.write_text(derived("placeholder credential, not a Shopify token") + "\n")
        os.chmod(placeholder, 0o600)
        conns = work / "connections.json"
        conns.write_text(json.dumps({"version": 1, "connections": [{
            "client_id": "lr-connected", "platform": "shopify", "shop": "zbm-live-run-placeholder",
            "credential": {"kind": "static_token", "token_file": str(placeholder)},
            "approval": {"approved_by": "andre", "approved_on": "2026-10-24", "approval_ref": "live-run-placeholder"}}]}))
        os.chmod(conns, 0o600)
        kill = work / "KILL"
        audit = work / "detection-audit.log"
        audit.write_text("")
        dp, probe_port = free_port(), free_port()
        det_env = {**base_env, "ZBM_SERVICE_TOKEN": DETECTION_TOKEN, "RR_STORE_CONNECTIONS_FILE": str(conns),
                   "RR_STORE_KILL_SWITCH_FILE": str(kill), "RR_LIVE_AUDIT_FILE": str(audit),
                   "RR_LIVE_AUDIT_PROBE_PORT": str(probe_port),
                   "PYTHONDONTWRITEBYTECODE": "1"}
        det = start([sys.executable, "-c", AUDIT_BOOT, "--port", str(dp)], det_env, SVC / "src", work / "det.log")
        durl = f"http://127.0.0.1:{dp}"
        wait_health(f"{durl}/health", det, "detection-py")
        dh = {"Authorization": f"Bearer {DETECTION_TOKEN}"}

        # ---- orchestrator-go
        orch_bin = work / "orchestrator"
        b = subprocess.run(["go", "build", "-o", str(orch_bin), "./cmd/orchestrator"],
                           cwd=str(REPO / "services" / "orchestrator-go"), env=base_env, capture_output=True, text=True,
                           timeout=600)
        if b.returncode != 0:
            print(b.stderr, file=sys.stderr)
            return 2
        op = free_port()
        orch_env = {**base_env, "DETECTION_SERVICE_URL": durl, "DETECTION_SERVICE_TOKEN": DETECTION_TOKEN,
                    "LEDGER_SERVICE_URL": lurl, "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN,
                    "ORCHESTRATOR_SERVICE_TOKEN": ORCH_TOKEN, "ORCHESTRATOR_PORT": str(op),
                    "ORCHESTRATOR_BIND_ADDR": "127.0.0.1",
                    # ADR 0023: no Railway edge here (no X-Real-IP), so the client address is the TCP peer
                    "ORCHESTRATOR_CLIENT_IP_SOURCE": "remote-addr",
                    # L-2 (final go-live AEGIS): fixture scans are refused unless this dev/test flag is set; step 5
                    # runs one.
                    "ORCHESTRATOR_ALLOW_FIXTURE_SCANS": "1"}
        orch = start([str(orch_bin)], orch_env, work, work / "orch.log")
        ourl = f"http://127.0.0.1:{op}"
        wait_health(f"{ourl}/health", orch, "orchestrator-go")
        oh = {"Authorization": f"Bearer {ORCH_TOKEN}"}
        c = httpx.Client(timeout=30, trust_env=False)

        # 1. no registration
        st = c.get(f"{durl}/stores/acme/status", headers=dh).json()
        check("1 status of an unregistered client is NOT_CONNECTED", st.get("connected") is False
              and st.get("reason") == "NOT_CONNECTED", json.dumps(st))
        for path in ("/stores/sync", "/stores/snapshot"):
            r = c.post(f"{durl}{path}", json={"client_id": "acme"}, headers=dh)
            check(f"1 {path} of an unregistered client is 409 NOT_CONNECTED",
                  r.status_code == 409 and r.json()["detail"]["reason"] == "NOT_CONNECTED", r.text)

        # 2. orchestrator scan of that client
        before = ledger_entries()
        r = c.post(f"{ourl}/revenue-recovery/scan?client_id=acme", headers=oh)
        check("2 orchestrator scan of an unregistered client is 409 NOT_CONNECTED",
              r.status_code == 409 and r.json().get("reason") == "NOT_CONNECTED", r.text)
        check("2 the refused scan wrote nothing to the ledger", ledger_entries() == before)

        # 3. registered placeholder, no detection ledger: record-first refuses the read
        r = c.post(f"{durl}/stores/sync", json={"client_id": "lr-connected"}, headers=dh)
        check("3 sync without a ledger is 503 LEDGER_UNAVAILABLE (record-first)",
              r.status_code == 503 and r.json()["detail"]["reason"] == "LEDGER_UNAVAILABLE", r.text)
        r = c.post(f"{durl}/stores/snapshot", json={"client_id": "lr-connected"}, headers=dh)
        check("3 snapshot of a never-synced client is 409 NOT_SYNCED",
              r.status_code == 409 and r.json()["detail"]["reason"] == "NOT_SYNCED", r.text)
        r = c.post(f"{ourl}/revenue-recovery/scan?client_id=lr-connected", headers=oh)
        check("3 orchestrator scan of a never-synced client is 409 NOT_SYNCED",
              r.status_code == 409 and r.json().get("reason") == "NOT_SYNCED", r.text)
        st = c.get(f"{durl}/stores/lr-connected/status", headers=dh).json()
        check("3 status shows the registration and never the credential",
              st.get("connected") is True and st.get("credential_kind") == "static_token"
              and str(placeholder) not in json.dumps(st) and derived("placeholder credential, not a Shopify token")
              not in json.dumps(st), json.dumps(st))

        # 4. kill switch, no restart
        kill.write_text("stop")
        r1 = c.post(f"{durl}/stores/sync", json={"client_id": "lr-connected"}, headers=dh)
        r2 = c.post(f"{durl}/stores/snapshot", json={"client_id": "lr-connected"}, headers=dh)
        check("4 kill switch file: sync and snapshot 503 STORE_READS_DISABLED",
              r1.status_code == 503 and r1.json()["detail"]["reason"] == "STORE_READS_DISABLED"
              and r2.status_code == 503 and r2.json()["detail"]["reason"] == "STORE_READS_DISABLED", r1.text + r2.text)
        kill.unlink()

        # 5. fixture scan unchanged; ledger verifies
        r = c.post(f"{ourl}/revenue-recovery/scan", headers=oh)
        body = r.json() if r.status_code == 200 else {}
        check("5 fixture scan still 200 with data_source fixtures, non-live",
              r.status_code == 200 and body.get("data_source") == "fixtures" and body.get("non_live_data_source") is True
              and "store_snapshot_id" not in body, r.text[:300])
        v = c.get(f"{lurl}/ledger/verify", headers=lh).json()
        check("5 GET /ledger/verify valid", v.get("valid") is True, json.dumps(v))

        # 6. no outbound attempt from detection-py
        lines = [ln for ln in audit.read_text().splitlines() if ln.strip()]
        loopback = ("'127.0.0.1'", "('127.0.0.1',", "'localhost'", "'::1'", "('::1',")
        outbound = [ln for ln in lines if not any(t in ln for t in loopback)]
        print(f"    detection-py socket audit: {len(lines)} event(s), all loopback: {not outbound}", flush=True)
        check("6 the socket audit hook works (its loopback positive control was recorded)",
              any(f"('127.0.0.1', {probe_port})" in ln for ln in lines), "; ".join(lines[:5]))
        check("6 detection-py attempted no outbound connection and no non-loopback name lookup", outbound == [],
              "; ".join(outbound[:5]))
    finally:
        for p in PROCS:
            if p.poll() is None:
                p.terminate()
        for p in PROCS:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(work, ignore_errors=True)
    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\nlive run: {len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 0 if CHECKS and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
