#!/usr/bin/env python3
"""
Client Fix lane (28) live run: the REAL ledger-rust binary and clientfix-py's production entrypoint
(``cd src && python3 -m api``) over real HTTP, with a durable data directory, in production mode.

usage: LEDGER_BIN=/path/to/ledger-rust/target/release/server python3 devtools/live_run.py

Proves, with every port a stand-in (nothing is wired, so no client system can be touched): start-up integrity against
the real ledger; a NOT_BUILT setting (the Anthropic key) refuses start; a second process on the data directory
refuses; a connection takes a vault reference only (a password field and a token-shaped value are refused); the flow
finding -> quote -> client acceptance in a hub session -> Finance payment of exactly the quote; no plan before
payment; engaging the fire team answers MODEL_NOT_WIRED; an out-of-allowlist operation and a model-typed ``before``
are refused; a plan needs the store's own values, so with no transport it is refused CONNECTOR_NOT_WIRED and nothing
is recorded or applied; a tenant crossover is refused; revoking the connection cancels the open item and the job settles into a dated
report and a refund proposal; the refund needs Andre's token and exact hash and then stays queued (Finance not
wired); a restart keeps everything; a truncated log is detected; no client value, vault reference or amount reaches
the ledger; GET /ledger/verify valid.

No check depends on the wall-clock hour or the machine's time zone. Exit 0 only if every check holds. Kills only the
PIDs it started. Ports are free ports from bind(0). Tokens are derived here (sha256 of a live-run label), never
secret-shaped literals.
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
import uuid
from pathlib import Path

import httpx

HERE = Path(__file__).resolve()
SVC = HERE.parents[1]


def derived(label: str) -> str:
    return hashlib.sha256(f"clientfix-py live-run only {label}".encode()).hexdigest()


LEDGER_TOKEN = derived("ledger token")
TOKEN = derived("service token")
ANDRE = derived("andre approval token")
CALLERS = {c: derived(f"caller {c}") for c in ("hub", "dashboard", "clientfix_agent", "fire_team", "orchestrator",
                                               "scheduler", "finance_31", "compliance_38")}
CLIENT_A = "onb-" + derived("client A")[:32]
CLIENT_B = "onb-" + derived("client B")[:32]
SHOP = "live-zbest.myshopify.com"
PRODUCT = "gid://shopify/Product/424242"
TITLE = "Live Run Hoodie Title"
VAULT_REF = "vault:delivery_28.cfx-live-" + derived("vault ref")[:16]
LOG: list[str] = []
PROCS: list[subprocess.Popen] = []
CHECKS: list[tuple[str, bool]] = []


def say(msg: str) -> None:
    line = f"[{time.monotonic():10.2f}] {msg}"
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
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code in (200, 503):
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


def rid() -> str:
    return str(uuid.uuid4())


class Api:
    def __init__(self, base: str):
        self.base = base

    def h(self, caller, andre=False, session=None):
        h = {"Authorization": f"Bearer {TOKEN}", "X-CFX-Caller-Token": CALLERS[caller]}
        if andre:
            h["X-Andre-Approval-Token"] = ANDRE
        if session:
            h["X-CFX-Client-Session"] = session
        return h

    def post(self, path, body, caller="dashboard", andre=False, session=None):
        return httpx.post(self.base + "/cfx/v1" + path, json=body, headers=self.h(caller, andre, session), timeout=30)

    def get(self, path, caller="dashboard", session=None):
        return httpx.get(self.base + "/cfx/v1" + path, headers=self.h(caller, session=session), timeout=30)

    def tick(self, name):
        return self.post(f"/ticks/{name}", {"request_id": rid()}, caller="scheduler")


def _main(work: Path) -> int:
    ledger_bin = os.environ["LEDGER_BIN"]
    pl, ps = free_port(), free_port()
    L, S = f"http://127.0.0.1:{pl}", f"http://127.0.0.1:{ps}"
    lh = {"Authorization": f"Bearer {LEDGER_TOKEN}"}
    data = work / "clientfix"
    env = {"CFX_SERVICE_TOKEN": TOKEN, "CFX_CALLER_TOKENS": json.dumps(CALLERS), "CFX_ANDRE_APPROVAL_TOKEN": ANDRE,
           "CFX_DATA_DIR": str(data), "CFX_PORT": str(ps), "LEDGER_SERVICE_URL": L, "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN}
    try:
        (work / "ledger").mkdir()
        start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(pl),
                             "LEDGER_LOG_PATH": str(work / "ledger" / "ledger.jsonl")}, str(work / "ledger"), "ledger",
              work)
        say(f"ledger health: {wait_health(L + '/health')}")

        # --- a NOT_BUILT setting refuses start (before anything else touches the data directory) ----------------
        bad = subprocess.run([sys.executable, "-m", "api"], env={**os.environ, **env,
                                                                 "CFX_ANTHROPIC_API_KEY_REF": "vault:x.y"},
                             cwd=str(SVC / "src"), capture_output=True, timeout=30)
        check("the Anthropic key setting is NOT_BUILT and refuses start",
              bad.returncode != 0 and b"CFX_ANTHROPIC_API_KEY_REF" in bad.stderr)

        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "service", work)
        wait_health(S + "/health")
        a = Api(S)
        st = a.get("/status").json()
        check("durable, production mode, integrity verified against the real ledger",
              st["in_memory"] is False and st["non_production"] is False and st["integrity"]["ok"] is True
              and st["andre_approvals_configured"] is True)
        check("every port is a stand-in", not any(st["ports_wired"].values()))
        second = subprocess.run([sys.executable, "-m", "api"], env={**os.environ, **env, "CFX_PORT": str(free_port())},
                                cwd=str(SVC / "src"), capture_output=True, timeout=30)
        check("a second process on the same data directory refuses",
              second.returncode != 0 and b"another clientfix-py process" in second.stderr)

        # --- connections: vault references only ----------------------------------------------------------------
        scopes = ["read_products", "write_products", "read_content", "write_content", "read_online_store_pages",
                  "write_online_store_pages", "read_online_store_navigation", "write_online_store_navigation"]
        base = {"client_id": CLIENT_A, "connector": "shopify", "account_ref": SHOP, "scopes": scopes}
        pw = a.post("/connections", {"request_id": rid(), **base, "password": "hunter2"}, caller="hub")
        tok = a.post("/connections", {"request_id": rid(), **base, "token_ref": "shpat_" + "0a" * 16}, caller="hub")
        conn = a.post("/connections", {"request_id": rid(), **base, "token_ref": VAULT_REF}, caller="hub")
        check("a password field and a token value are refused; a vault reference is accepted and never shown back",
              pw.status_code == 422 and tok.status_code == 422 and conn.status_code == 201
              and "token_ref" not in conn.json() and conn.json()["has_token_ref"] is True)
        cid = conn.json()["connection_id"]

        # --- finding, quote, client session, payment --------------------------------------------------------------
        f = a.post("/findings", {"request_id": rid(), "finding_id": "rr:live-1", "agent_id": "platform-integration",
                                 "leak_category": "platform_integration_gap", "client_id": CLIENT_A,
                                 "check_code": "product_seo_missing",
                                 "resource": {"connection_id": cid, "target": PRODUCT}}, caller="orchestrator")
        cross = a.post("/findings", {"request_id": rid(), "finding_id": "rr:live-x", "agent_id": "a",
                                     "client_id": CLIENT_B, "check_code": "product_seo_missing",
                                     "resource": {"connection_id": cid, "target": PRODUCT}}, caller="orchestrator")
        check("a finding for another client on this client's connection is a tenant mismatch",
              f.status_code == 201 and cross.status_code == 409 and cross.json()["detail"] == "TENANT_MISMATCH")
        j = a.post("/jobs", {"request_id": rid(), "client_id": CLIENT_A,
                             "items": [{"finding_id": "rr:live-1", "price": "175.00"}]}, caller="clientfix_agent").json()
        jid = j["job_id"]
        sess = a.post("/client-sessions", {"request_id": rid(), "client_id": CLIENT_A}, caller="hub").json()
        acc = a.post(f"/jobs/{jid}/quote/accept", {"request_id": rid(), "sha256": j["quote_sha256"]}, caller="hub",
                     session=sess["session_token"])
        item = a.get(f"/jobs/{jid}").json()["items"][0]
        plan = {"request_id": rid(), "team": j["team"],
                "items": [{"item_id": item["item_id"], "connection_id": cid,
                           "ops": [{"op": "shopify.product.update", "target": PRODUCT, "field": "seo.title",
                                    "after": TITLE}]}]}
        early = a.post(f"/jobs/{jid}/plan", plan, caller="fire_team")
        fev = "fin-evt-" + derived("finance event")[:40]
        wrong = a.post("/finance/events", {"request_id": rid(), "finance_event_id": "fin-evt-" + derived("x")[:40],
                                           "job_id": jid, "kind": "payment_confirmed", "amount": "174.99",
                                           "currency": "USD", "quote_sha256": j["quote_sha256"]}, caller="finance_31")
        paid = a.post("/finance/events", {"request_id": rid(), "finance_event_id": fev, "job_id": jid,
                                          "kind": "payment_confirmed", "amount": "175.00", "currency": "USD",
                                          "quote_sha256": j["quote_sha256"]}, caller="finance_31")
        check("quote accepted in a client session; no plan before payment; only the exact amount pays",
              acc.status_code == 200 and early.status_code == 409 and early.json()["detail"] == "PAYMENT_REQUIRED"
              and wrong.status_code == 200 and wrong.json()["payment"] is None          # recorded, never pays the job
              and [o["amount"] for o in wrong.json()["orphan_payments"]] == ["174.99"]  # (AEGIS round 3 L3: refunded)
              and paid.status_code == 200 and paid.json()["status"] == "paid")

        # --- fire team, plan, approval, apply ---------------------------------------------------------------------
        eng = a.post(f"/jobs/{jid}/engage", {"request_id": rid()}, caller="clientfix_agent")
        check("engaging the fire team answers MODEL_NOT_WIRED (no Anthropic key)",
              eng.status_code == 503 and eng.json()["detail"] == "MODEL_NOT_WIRED")
        bad_op = dict(plan, request_id=rid(), items=[dict(plan["items"][0], ops=[dict(plan["items"][0]["ops"][0],
                                                                                    op="shopify.theme.write")])])
        refused_op = a.post(f"/jobs/{jid}/plan", bad_op, caller="fire_team")
        with_before = dict(plan, request_id=rid(), items=[dict(plan["items"][0], ops=[dict(plan["items"][0]["ops"][0],
                                                                                         before="model-typed")])])
        typed_before = a.post(f"/jobs/{jid}/plan", with_before, caller="fire_team")
        n_before = len(httpx.get(L + "/ledger/entries", headers=lh, timeout=60).json())
        planned = a.post(f"/jobs/{jid}/plan", {**plan, "request_id": rid()}, caller="fire_team")
        ap = a.post(f"/jobs/{jid}/apply", {"request_id": rid()}, caller="scheduler")
        n_after = len(httpx.get(L + "/ledger/entries", headers=lh, timeout=60).json())
        check("an out-of-allowlist op is refused; a plan carrying a model-typed before value is refused",
              refused_op.status_code == 422 and refused_op.json()["detail"] == "OP_NOT_ALLOWED"
              and typed_before.status_code == 422)
        check("a plan needs the store's own current values: with no transport it is refused CONNECTOR_NOT_WIRED, "
              "nothing can be approved or applied, and nothing is recorded",
              planned.status_code == 503 and planned.json()["detail"] == "CONNECTOR_NOT_WIRED"
              and ap.status_code == 409 and ap.json()["detail"] == "PLAN_REQUIRED" and n_before == n_after)

        # --- revocation, report, refund -----------------------------------------------------------------------------
        rv = a.post(f"/connections/{cid}/revoke", {"request_id": rid()}, caller="hub")
        job = a.get(f"/jobs/{jid}").json()
        check("revoking the connection cancels the open item; the job settles into a dated report and a refund",
              rv.status_code == 200 and rv.json()["status"] == "revoked"
              and job["items"][0]["status"] == "cancelled_revoked" and job["report"] is not None
              and job["status"] == "refund_pending")
        # AEGIS round 4 L1: the orphaned 174.99 payment above has its own refund; select THIS job's unfixed refund
        # by kind and job id, never by list position (refund ids are hashes, so the order is not fixed)
        def unfixed_refund() -> dict:
            return next(x for x in a.get("/refunds").json() if x["kind"] == "unfixed" and x["job_id"] == jid)
        r = unfixed_refund()
        no_andre = a.post(f"/refunds/{r['refund_id']}/approve", {"request_id": rid(), "sha256": r["refund_sha256"]})
        bad_hash = a.post(f"/refunds/{r['refund_id']}/approve", {"request_id": rid(), "sha256": "1" * 64}, andre=True)
        ok = a.post(f"/refunds/{r['refund_id']}/approve", {"request_id": rid(), "sha256": r["refund_sha256"]},
                    andre=True)
        tick = a.tick("refunds").json()
        check("the refund needs Andre's token and exact hash, then stays queued (Finance not wired)",
              r["amount"] == "175.00" and no_andre.status_code == 403 and bad_hash.status_code == 409
              and ok.status_code == 200 and tick.get("not_wired") == 1
              and unfixed_refund()["status"] == "queued")

        # --- restart, truncation ------------------------------------------------------------------------------------
        stop(sp, "service")
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "service", work)
        wait_health(S + "/health")
        again = a.get(f"/jobs/{jid}").json()
        check("a restart keeps everything and re-verifies against the ledger",
              again["status"] == "refund_pending" and a.get("/status").json()["integrity"]["ok"] is True
              and a.get(f"/connections/{cid}").json()["status"] == "revoked")
        evd = a.get("/audit/evidence?limit=1000", caller="compliance_38").json()
        check("the evidence view marks each logical action committed against the real ledger",
              evd["committed"] > 0 and evd["rule"] == "unanchored evidence = attempted, not done")
        stop(sp, "service")
        path = data / "clientfix_log.jsonl"
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(lines[:-2]))
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "service", work)
        hs = wait_health(S + "/health")
        w = a.post("/client-sessions", {"request_id": rid(), "client_id": CLIENT_A}, caller="hub")
        check("a truncated log is detected against the ledger and nothing takes effect",
              hs["status"] == "degraded" and w.status_code == 503)
        path.write_bytes(b"".join(lines))
        stop(sp, "service")

        # --- the ledger ---------------------------------------------------------------------------------------------
        ents = httpx.get(L + "/ledger/entries", headers=lh, timeout=60).json()
        mine = [x for x in ents if x.get("department") == "clientfix"]
        types = sorted({x["event_type"] for x in mine})
        say(f"ledger: {len(ents)} entries, {len(mine)} from clientfix; types: {', '.join(types)}")
        check("connections, findings, quotes, payments, revocations, reports and refunds are typed "
              "events on the ledger",
              {"log_anchor", "connection_registered", "finding_added", "job_created", "quote_accepted",
               "payment_confirmed", "connection_revoked", "report_issued",
               "refund_proposed", "refund_approved", "session_opened"} <= set(types))
        blob = json.dumps(ents)
        check("no client value, shop, vault reference, session token or amount on the ledger",
              TITLE not in blob and SHOP not in blob and VAULT_REF not in blob and "175.00" not in blob
              and sess["session_token"] not in blob)
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
    work = Path(tempfile.mkdtemp(prefix="clientfix-live-", dir=keep_in))
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
