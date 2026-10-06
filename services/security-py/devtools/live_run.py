#!/usr/bin/env python3
"""
Cybersecurity (22) live run: the REAL ledger-rust binary and security-py's production entrypoint
(``cd src && python3 -m api``) over real HTTP, with a durable data directory and the non-production local master
key. A software passkey (tests/helpers.Authenticator) performs real WebAuthn ceremonies.

usage: LEDGER_BIN=/path/to/ledger-rust/target/release/server python3 devtools/live_run.py

Proves: start-up integrity against the real ledger; passkey enrolment and approval; store, use, rotate; refusals;
the release recorded on the ledger and no value on it; a service credential verified with the published keys;
a freeze; Legal's preservation answer; a restart that keeps everything and re-verifies; a truncated log detected
after a restart; GET /ledger/verify valid. Exit 0 only if every check holds. Kills only the PIDs it started.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

HERE = Path(__file__).resolve()
SVC = HERE.parents[1]
sys.path[:0] = [str(SVC / "src"), str(SVC / "tests")]

from helpers import Authenticator  # noqa: E402
import tokens  # noqa: E402

LEDGER_TOKEN = "live-ledger-token-" + "l" * 24
TOKEN = "live-sec-service-token-" + "s" * 20
CALLERS = {c: f"live-sec-caller-{c}-" + "c" * 24 for c in ("finance_31", "legal_37", "dashboard", "scheduler",
                                                           "onboarding")}
ENROLL = __import__("secrets").token_urlsafe(48)
RP_ID, ORIGIN = "zbm.test", "https://console.zbm.test"
LOG: list[str] = []
PROCS: list[subprocess.Popen] = []
CHECKS: list[tuple[str, bool]] = []


def say(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG.append(line)
    print(line, flush=True)


def check(name: str, ok: bool) -> None:
    CHECKS.append((name, bool(ok)))
    say(f"  CHECK {'PASS' if ok else 'FAIL'}: {name}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_health(url: str, timeout: float = 25.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                return r.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


def start(cmd, env, cwd, name, logdir) -> subprocess.Popen:
    out = open(logdir / f"{name}.log", "ab")
    p = subprocess.Popen(cmd, env={**os.environ, **env}, cwd=cwd, stdout=out, stderr=subprocess.STDOUT)
    PROCS.append(p)
    say(f"started {name} pid={p.pid}")
    return p


def stop(p: subprocess.Popen, name: str) -> None:
    p.terminate()
    p.wait(timeout=10)
    say(f"stopped {name} pid={p.pid} (exit {p.returncode})")


def secret_file(path: Path, content: bytes) -> str:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, content)
    os.close(fd)
    return str(path)


class Api:
    def __init__(self, base: str):
        self.base = base

    def h(self, caller):
        return {"Authorization": f"Bearer {TOKEN}", "X-SEC-Caller-Token": CALLERS[caller]}

    def post(self, path, body, caller="dashboard"):
        return httpx.post(self.base + path, json=body, headers=self.h(caller), timeout=30)

    def get(self, path, caller="dashboard"):
        return httpx.get(self.base + path, headers=self.h(caller), timeout=30)

    def approved(self, action, target, body, key):
        ch = self.post("/sec/v1/approvals/challenges", {"action": action, "target": target, "body": body}).json()
        return {**body, "approval": key.assert_(ch)}


def rid() -> str:
    return "live-" + uuid.uuid4().hex


def _main(work: Path) -> int:
    ledger_bin = os.environ["LEDGER_BIN"]
    pl, ps = free_port(), free_port()
    L, S = f"http://127.0.0.1:{pl}", f"http://127.0.0.1:{ps}"
    lh = {"Authorization": f"Bearer {LEDGER_TOKEN}"}
    secrets_dir = work / "etc"
    secrets_dir.mkdir(mode=0o700)
    env = {"SEC_SERVICE_TOKEN": TOKEN, "SEC_CALLER_TOKENS": json.dumps(CALLERS), "SEC_NON_PRODUCTION": "1",
           "SEC_DATA_DIR": str(work / "security"), "SEC_KMS": "local_file",
           "SEC_LOCAL_MASTER_KEY_FILE": secret_file(secrets_dir / "master.key", base64.b64encode(os.urandom(32))),
           "SEC_ANDRE_ENROLL_TOKEN_FILE": secret_file(secrets_dir / "enroll.token", ENROLL.encode()),
           "SEC_WEBAUTHN_RP_ID": RP_ID, "SEC_WEBAUTHN_ORIGINS": ORIGIN, "SEC_PORT": str(ps),
           "LEDGER_SERVICE_URL": L, "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN}
    try:
        (work / "ledger").mkdir()
        start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(pl),
                             "LEDGER_LOG_PATH": str(work / "ledger" / "ledger.jsonl")}, str(work / "ledger"), "ledger",
              work)
        say(f"ledger health: {wait_health(L + '/health')}")
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "security", work)
        wait_health(S + "/health")
        a = Api(S)
        hs = a.get("/sec/v1/status").json()
        say(f"security-py status: integrity={hs['integrity']['ok']} in_memory={hs['in_memory']} "
            f"vault={hs['vault_available']}")
        check("durable, integrity verified against the real ledger, vault available",
              hs["in_memory"] is False and hs["integrity"]["ok"] is True and hs["vault_available"] is True)

        # --- Andre's passkeys ------------------------------------------------------------------------------------
        k1 = Authenticator(-7, rp_id=RP_ID, origin=ORIGIN)
        r = a.post("/sec/v1/passkeys/enroll/options", {"enroll_token": "wrong-" + "x" * 30})
        check("a wrong enroll token is refused", r.status_code == 403)
        opts = a.post("/sec/v1/passkeys/enroll/options", {"enroll_token": ENROLL}).json()
        r = a.post("/sec/v1/passkeys/enroll", k1.register(opts))
        check("first passkey enrolled with the one-time token", r.status_code == 201)
        k2 = Authenticator(-8, rp_id=RP_ID, origin=ORIGIN)
        opts = a.post("/sec/v1/passkeys/enroll/options", a.approved("PASSKEY_ENROLL", "", {}, k1)).json()
        check("second passkey enrolled with the first one's approval",
              a.post("/sec/v1/passkeys/enroll", k2.register(opts)).status_code == 201)
        check("the token no longer enrols anything",
              a.post("/sec/v1/passkeys/enroll/options", {"enroll_token": ENROLL}).status_code == 403)

        # --- vault ----------------------------------------------------------------------------------------------
        ref = "vault:finance_31.stripe_secret"
        body = {"request_id": rid(), "owner": "finance_31", "name": "stripe_secret", "kind": "api_key",
                "value": "sk_test_LIVE_RUN_VALUE_0001", "readers": ["finance_31"], "purposes": ["stripe_api"]}
        r = a.post("/sec/v1/secrets/andre", {**body, "approval": {"challenge_id": "ch-x", "credential_id": "AAAA",
                                                                  "client_data_json": "AAAA",
                                                                  "authenticator_data": "AAAA", "signature": "AAAA"}})
        check("storing for a department without a real passkey approval -> 403", r.status_code == 403)
        r = a.post("/sec/v1/secrets/andre", a.approved("SECRET_STORE", ref, body, k1))
        check("Andre stores Finance's Stripe key with his passkey", r.status_code == 201 and "value" not in r.json())
        r = a.post(f"/sec/v1/secrets/{ref}/use", {"purpose": "stripe_api"}, caller="finance_31")
        check("Finance reads it for its declared purpose", r.status_code == 200 and
              r.json()["value"] == "sk_test_LIVE_RUN_VALUE_0001" and r.headers["cache-control"] == "no-store")
        check("Legal cannot see it exists",
              a.post(f"/sec/v1/secrets/{ref}/use", {"purpose": "stripe_api"}, caller="legal_37").status_code == 404)
        check("the dashboard never gets a value",
              a.post(f"/sec/v1/secrets/{ref}/use", {"purpose": "stripe_api"}).status_code == 403)
        r = a.post(f"/sec/v1/secrets/{ref}/rotate", {"request_id": rid(), "value": "sk_test_LIVE_RUN_VALUE_0002"},
                   caller="finance_31")
        check("Finance rotates its key", r.status_code == 200 and r.json()["version"] == 2)
        sealed = sorted(os.listdir(work / "security" / "sealed"))
        check("only the current version is on disk, sealed", len([s for s in sealed if s.endswith(".v1")]) == 1
              and not any(b"sk_test_LIVE" in (work / "security" / "sealed" / s).read_bytes() for s in sealed))

        # --- identity ---------------------------------------------------------------------------------------------
        t = a.post("/sec/v1/identity/tokens", {"audience": "legal_37"}, caller="finance_31").json()
        jwks = tokens.jwks_map(a.get("/sec/v1/identity/jwks", caller="legal_37").json()["keys"])
        v = tokens.verify(t["token"], jwks, "legal_37", int(time.time()))
        check("a Finance credential for Legal verifies with the published key", v.subject == "finance_31")

        # --- freeze, holds ------------------------------------------------------------------------------------------
        fb = {"request_id": rid(), "target_kind": "caller", "target_id": "onboarding", "reason_code": "LIVE_RUN"}
        f = a.post("/sec/v1/freezes", a.approved("FREEZE", "caller:onboarding", fb, k2)).json()
        r = a.post("/sec/v1/secrets", {"request_id": rid(), "name": "x", "kind": "api_key", "value": "v"},
                   caller="onboarding")
        check("a frozen caller can do nothing", r.status_code == 403 and r.json()["detail"] == "CALLER_FROZEN")
        r = a.post(f"/sec/v1/freezes/{f['freeze_id']}/lift",
                   a.approved("LIFT_FREEZE", f["freeze_id"], {"request_id": rid()}, k1))
        check("the freeze is lifted with a passkey", r.status_code == 200 and r.json()["status"] == "lifted")
        r = a.post("/sec/v1/holds", {"request_id": rid(), "hold_id": "lg-hld-live", "systems": ["email"]},
                   caller="legal_37").json()
        check("Legal's hold is answered honestly: email is not connected", r["delivered"] is False)

        # --- restart ----------------------------------------------------------------------------------------------
        stop(sp, "security")
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "security", work)
        wait_health(S + "/health")
        check("after a restart the log re-verifies against the ledger",
              a.get("/sec/v1/status").json()["integrity"]["ok"] is True)
        r = a.post(f"/sec/v1/secrets/{ref}/use", {"purpose": "stripe_api"}, caller="finance_31")
        check("the rotated key survives the restart", r.json().get("value") == "sk_test_LIVE_RUN_VALUE_0002")
        r = a.post(f"/sec/v1/secrets/{ref}/rotate",
                   a.approved("SECRET_ROTATE", ref, {"request_id": rid(), "value": "sk_test_LIVE_RUN_VALUE_0003"}, k1))
        check("passkey counters carried across the restart (approval still works)", r.status_code == 200)

        # --- a truncated log is caught --------------------------------------------------------------------------------
        stop(sp, "security")
        path = work / "security" / "security_log.jsonl"
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(lines[:-2]))
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "security", work)
        hs = wait_health(S + "/health")
        r = a.post(f"/sec/v1/secrets/{ref}/use", {"purpose": "stripe_api"}, caller="finance_31")
        check("a truncated log is detected against the ledger and nothing is released",
              hs["status"] == "degraded" and r.status_code == 503)
        stop(sp, "security")

        # --- the ledger ---------------------------------------------------------------------------------------------
        ents = httpx.get(L + "/ledger/entries", headers=lh, timeout=60).json()
        mine = [x for x in ents if x.get("department") == "cybersecurity"]
        types = sorted({x["event_type"] for x in mine})
        say(f"ledger: {len(ents)} entries, {len(mine)} from cybersecurity; types: {', '.join(types)}")
        check("every release and credential is on the ledger",
              {"secret_released", "credential_issued", "log_anchor"} <= set(types))
        check("no secret value anywhere on the ledger", "sk_test_LIVE" not in json.dumps(ents))
        v = httpx.get(L + "/ledger/verify", headers=lh, timeout=120)
        check("ledger verifies valid", v.status_code == 200 and v.json().get("valid") is True)
        failed = [n for n, ok in CHECKS if not ok]
        say(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
        return 0 if not failed else 1
    finally:
        for p in PROCS:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        (work / "live_run.txt").write_text("\n".join(LOG) + "\n")


def main() -> int:
    keep_in = os.environ.get("LIVE_WORK_DIR") or None
    work = Path(tempfile.mkdtemp(prefix="security-live-", dir=keep_in))
    try:
        return _main(work)
    finally:
        if keep_in:
            print(f"work dir kept (LIVE_WORK_DIR): {work}", flush=True)
        else:
            shutil.rmtree(work)
            print(f"work dir {work} removed (set LIVE_WORK_DIR=<dir> to keep the run's logs)", flush=True)


if __name__ == "__main__":
    sys.exit(main())
