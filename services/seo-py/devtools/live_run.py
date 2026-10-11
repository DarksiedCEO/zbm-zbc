#!/usr/bin/env python3
"""
Search & Answer Intelligence (2) live run: the REAL ledger-rust binary, seo-py's production entrypoint
(``cd src && python3 -m api``) and Finance (31)'s production entrypoint (finance-py, the same command) over real HTTP,
with durable data directories, in production mode.

usage: LEDGER_BIN=/path/to/ledger-rust/target/release/server python3 devtools/live_run.py

Proves: a NOT_BUILT setting refuses start; start-up integrity against the real ledger and the seeded own-properties
tenant and entity record; every port but the fetcher NOT_CONNECTED; a second process on the data directory refuses;
tenants and domains are Andre's, a domain belongs to one tenant; the hub sees its own tenant only (another tenant
answers the same 404 as a missing one); a kill switch engaged over the API stops the crawler at the fetch guard
before any DNS lookup or socket; only Andre releases it; the audit is recorded first and its report's SHA-256 is on
the ledger; a client audit needs an invoice id and Andre, and the invoice is verified with the real Finance (31)
process (a draft invoice is refused INVOICE_NOT_PAID, an unknown one INVOICE_NOT_FOUND, another client's
INVOICE_TENANT_MISMATCH, none of them overridable; Finance stopped is FINANCE_UNAVAILABLE, which only Andre may
override, recorded; a reused invoice is refused); Finance (31) sees status, never the report; personal-data
keys are refused; a scheduled re-audit runs its slot once and a second tick is a no-op; the department view counts
the recorded runs; a restart keeps everything and re-verifies; the evidence view marks each action committed exactly
once; a truncated log is detected; no domain or NAP in the clear on the ledger; GET /ledger/verify valid.

No outbound network: this run never crawls a real site (CI has none to crawl, and a live run must not depend on the
internet). The web provider switch is engaged before the first audit, so every fetch stops at the kill-switch guard,
which runs before name resolution. The crawler itself is proven against an in-process fixture in the test suite.
A PAID invoice is not reachable through Finance's production entrypoint without its bank feed and Legal (both
fail-closed stand-ins there); that path is finance-py's contract test (tests/test_seo_invoice_contract.py).

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
    return hashlib.sha256(f"seo-py live-run only {label}".encode()).hexdigest()


LEDGER_TOKEN = derived("ledger token")
TOKEN = derived("service token")
ANDRE = derived("andre approval token")
CALLERS = {c: derived(f"caller {c}") for c in ("dashboard", "seo_agent", "scheduler", "hub", "finance_31",
                                               "compliance_38")}
TENANT_TOKENS = {t: derived(f"tenant {t}") for t in ("zbm", "acme-live", "globex-live")}
FIN_TOKEN = derived("finance service token")
FIN_ANDRE = derived("finance andre approval token")
FIN_CALLERS = {c: derived(f"finance caller {c}") for c in ("onboarding", "seo_02")}
FIN_SRC = SVC.parent / "finance-py" / "src"
ACME_PARTY = "acme-party-live"
OWN_DOMAIN = "zbestmedia-live.test"
CLIENT_DOMAIN = "acme-live.test"
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
    out.close()
    PROCS.append(p)
    say(f"started {name} pid={p.pid}")
    return p


def stop(p: subprocess.Popen, name: str) -> None:
    p.terminate()
    p.wait(timeout=10)
    say(f"stopped {name} pid={p.pid} (exit {p.returncode})")


def rid() -> str:
    return str(uuid.uuid4())


def invoice_id(label: str) -> str:
    """A Finance (31) invoice id shape (``fin-inv-`` + 26 Crockford base32), derived from a label."""
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    n = int.from_bytes(hashlib.sha256(label.encode()).digest()[:17], "big")
    return "fin-inv-" + "".join(alphabet[(n >> (5 * i)) & 31] for i in range(26))


class Api:
    def __init__(self, base: str):
        self.base = base

    def h(self, caller, andre=False, tenant=None):
        h = {"Authorization": f"Bearer {TOKEN}", "X-SEO-Caller-Token": CALLERS[caller]}
        if andre:
            h["X-Andre-Approval-Token"] = ANDRE
        if tenant:
            h["X-SEO-Tenant-Token"] = TENANT_TOKENS[tenant]
        return h

    def post(self, path, body, caller="seo_agent", andre=False):
        if andre:
            caller = "dashboard"
        return httpx.post(self.base + "/seo/v1" + path, json=body, headers=self.h(caller, andre), timeout=60)

    def get(self, path, caller="dashboard", tenant=None):
        return httpx.get(self.base + "/seo/v1" + path, headers=self.h(caller, tenant=tenant), timeout=60)

    def job(self, name):
        return self.post(f"/jobs/{name}/run", {"request_id": rid()}, caller="scheduler")


class Fin:
    def __init__(self, base: str):
        self.base = base

    def h(self, caller=None, andre=False):
        hd = {"Authorization": f"Bearer {FIN_TOKEN}"}
        if caller:
            hd["X-FIN-Caller-Token"] = FIN_CALLERS[caller]
        if andre:
            hd["X-Andre-Approval-Token"] = FIN_ANDRE
        return hd

    def get(self, path, caller="onboarding"):
        return httpx.get(self.base + path, headers=self.h(caller), timeout=30)

    def post(self, path, body, caller=None, andre=False):
        return httpx.post(self.base + path, json=body, headers=self.h(caller, andre), timeout=30)

    def approve_rules(self) -> int:
        seed = [p for p in self.get("/fin/v1/rules").json()["open_proposals"] if p["kind"] == "seed"][0]
        return self.post("/fin/v1/rules/decisions", {"request_id": f"seo-live-{uuid.uuid4().hex}", "decisions": [
            {"proposal_id": seed["proposal_id"], "content_sha256": seed["content_sha256"], "decision": "approve"}]},
            andre=True).status_code

    def draft(self, client: str):
        body = {"request_id": f"seo-live-{uuid.uuid4().hex}", "entity": "zbm", "client_id": client, "kind": "service",
                "lines": [{"line_code": "strategy_services", "quantity": 1, "unit_price": "2000.00"}],
                "payment_methods": ["ach"], "legal_ref": {"doc_id": "msa-live", "version": 1, "doc_sha256": "d" * 64,
                                                          "acceptance_id": "acc-live"}}
        return self.post("/fin/v1/invoices", body, caller="onboarding")


def _env(work: Path, ps: int, ledger_url: str) -> dict:
    etc = work / "etc"
    etc.mkdir(mode=0o700)
    key_file = etc / "log-hash.key"
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, os.urandom(32).hex().encode())
    os.close(fd)
    data = work / "seo"
    data.mkdir(mode=0o700)
    return {"SEO_SERVICE_TOKEN": TOKEN, "SEO_CALLER_TOKENS": json.dumps(CALLERS),
            "SEO_TENANT_TOKENS": json.dumps(TENANT_TOKENS), "SEO_ANDRE_APPROVAL_TOKEN": ANDRE,
            "SEO_DATA_DIR": str(data), "SEO_LOG_HASH_KEY_FILE": str(key_file), "SEO_PORT": str(ps),
            "LEDGER_SERVICE_URL": ledger_url, "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN}


def _fin_env(work: Path, pf: int, ledger_url: str) -> dict:
    (work / "finance").mkdir(mode=0o700)
    return {"FIN_SERVICE_TOKEN": FIN_TOKEN, "FIN_ANDRE_APPROVAL_TOKEN": FIN_ANDRE,
            "FIN_CALLER_TOKENS": json.dumps(FIN_CALLERS), "FIN_DATA_DIR": str(work / "finance"), "FIN_PORT": str(pf),
            "LEDGER_SERVICE_URL": ledger_url, "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "PYTHONDONTWRITEBYTECODE": "1"}


def _main(work: Path) -> int:
    ledger_bin = os.environ["LEDGER_BIN"]
    pl, ps, pf = free_port(), free_port(), free_port()
    L, S, F = f"http://127.0.0.1:{pl}", f"http://127.0.0.1:{ps}", f"http://127.0.0.1:{pf}"
    lh = {"Authorization": f"Bearer {LEDGER_TOKEN}"}
    env = {**_env(work, ps, L), "SEO_FINANCE_URL": F, "SEO_FINANCE_TOKEN": FIN_TOKEN,
           "SEO_FINANCE_CALLER_TOKEN": FIN_CALLERS["seo_02"]}
    fenv = _fin_env(work, pf, L)
    data = Path(env["SEO_DATA_DIR"])
    src = str(SVC / "src")
    try:
        (work / "ledger").mkdir()
        start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(pl),
                             "LEDGER_LOG_PATH": str(work / "ledger" / "ledger.jsonl")}, str(work / "ledger"), "ledger",
              work)
        say(f"ledger health: {wait_health(L + '/health')}")
        fp = start([sys.executable, "-m", "api"], fenv, str(FIN_SRC), "finance", work)
        wait_health(F + "/health")
        fin = Fin(F)
        check("Finance (31) runs from its production entrypoint and Andre approves its rules",
              fin.approve_rules() == 200)

        # --- settings that must refuse start (before anything touches the data directory) ---------------------------
        bad = subprocess.run([sys.executable, "-m", "api"], env={**os.environ, **env, "SEO_RENDERER": "chromium"},
                             cwd=src, capture_output=True, timeout=30)
        check("a NOT_BUILT setting (a renderer) refuses start", bad.returncode != 0 and b"SEO_RENDERER" in bad.stderr)
        nokey = {k: v for k, v in env.items() if k != "SEO_LOG_HASH_KEY_FILE"}
        bad = subprocess.run([sys.executable, "-m", "api"], env={**{k: v for k, v in os.environ.items()
                                                                    if k != "SEO_LOG_HASH_KEY_FILE"}, **nokey},
                             cwd=src, capture_output=True, timeout=30)
        check("production without the log hash key refuses start",
              bad.returncode != 0 and b"SEO_LOG_HASH_KEY_FILE" in bad.stderr)

        sp = start([sys.executable, "-m", "api"], env, src, "service", work)
        wait_health(S + "/health")
        a = Api(S)
        st = a.get("/status").json()
        check("durable, production mode, integrity verified against the real ledger, Andre configured",
              st["in_memory"] is False and st["non_production"] is False and st["integrity"]["ok"] is True
              and st["andre_approvals_configured"] is True)
        ports = st["ports"]
        check("only the fetcher and Finance are connected; render, every answer engine and first-party source are not; "
              "invoices are verified with Finance", ports["fetch"] == "connected" and ports["render"] == "NOT_CONNECTED"
              and ports["finance"] == "connected" and st["invoice_verification"]["mode"] == "finance"
              and set(ports["answer_engines"].values()) == {"NOT_CONNECTED"}
              and set(ports["first_party"].values()) == {"NOT_CONNECTED"} and ports["clientfix"] == "NOT_CONNECTED")
        second = subprocess.run([sys.executable, "-m", "api"], env={**os.environ, **env, "SEO_PORT": str(free_port())},
                                cwd=src, capture_output=True, timeout=30)
        check("a second process on the same data directory refuses",
              second.returncode != 0 and b"another seo-py process" in second.stderr)
        ent = a.get("/tenants/zbm/entity").json()
        check("the own-properties tenant and Andre's NAP are seeded, each field with its provenance",
              ent.get("fields", {}).get("telephone", {}).get("value") == "(562) 248-6617"
              and all(f.get("provenance") for f in ent.get("fields", {}).values()))

        # --- tenants and domains: Andre's ---------------------------------------------------------------------------
        agent_t = a.post("/tenants", {"request_id": rid(), "tenant_id": "acme-live", "kind": "client"})
        t1 = a.post("/tenants", {"request_id": rid(), "tenant_id": "acme-live", "kind": "client"}, andre=True)
        t2 = a.post("/tenants", {"request_id": rid(), "tenant_id": "globex-live", "kind": "client"}, andre=True)
        check("only Andre creates a tenant", agent_t.status_code == 403 and t1.status_code == 201
              and t2.status_code == 201)
        d0 = a.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [OWN_DOMAIN]}, andre=True)
        d1 = a.post("/tenants/acme-live/domains", {"request_id": rid(), "domains": [CLIENT_DOMAIN]}, andre=True)
        taken = a.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [OWN_DOMAIN, "www." + CLIENT_DOMAIN]},
                       andre=True)
        check("a client's domain (or its www. twin) cannot also be registered to the own-properties tenant",
              d0.status_code == 200 and d1.status_code == 200 and taken.status_code == 409
              and taken.json()["detail"] == "DOMAIN_TAKEN")
        mine = a.get("/tenants/acme-live", caller="hub", tenant="acme-live")
        other = a.get("/tenants/globex-live", caller="hub", tenant="acme-live")
        missing = a.get("/tenants/nobody-live", caller="hub", tenant="acme-live")
        check("the hub sees its own tenant; another tenant answers exactly like a missing one",
              mine.status_code == 200 and other.status_code == 404 and missing.status_code == 404
              and other.json() == missing.json())

        # --- the kill switch stops the crawler at the fetch guard (no DNS, no socket) ------------------------------
        on = a.post("/kill-switches", {"request_id": rid(), "switch": "provider:web", "engaged": True},
                    caller="compliance_38")
        off = a.post("/kill-switches", {"request_id": rid(), "switch": "provider:web", "engaged": False},
                     caller="dashboard")
        check("Compliance engages a switch over the API (recorded); only Andre releases it",
              on.status_code == 200 and on.json()["recorded"] is True and on.json()["providers"]["web"] is True
              and off.status_code == 403 and off.json()["detail"] == "ANDRE_APPROVAL_REQUIRED")
        unauth = a.post("/tenants/zbm/audits", {"request_id": rid(), "domain": "example.com", "paths": ["/"]})
        check("an audit of a domain nobody registered is refused before anything is recorded",
              unauth.status_code == 403 and unauth.json()["detail"] == "DOMAIN_NOT_AUTHORIZED")
        au = a.post("/tenants/zbm/audits", {"request_id": rid(), "domain": OWN_DOMAIN, "paths": ["/", "/about"]})
        rep = au.json().get("report") or {}
        outs = rep.get("summary", {}).get("agent_outcomes", {})
        selene = next((x for x in rep.get("agents", []) if x["agent"] == "selene"), {})
        check("an own-properties audit completes with the crawler KILLED at the guard and no page observed",
              au.status_code == 201 and au.json()["status"] == "completed" and outs.get("selene") == "KILLED"
              and selene.get("findings") == [] and all(p["fetch"]["state"] == "KILLED" for p in
                                                       rep.get("pages", {}).values()))
        check("the report says what was NOT_CONNECTED and why, and claims no effect",
              "answer_engine:openai" in rep.get("not_connected", []) and "render" in rep.get("not_connected", [])
              and all(f.get("effect_class") is None for e in rep.get("agents", []) for f in e["findings"]))
        own_aid = au.json()["audit_id"]

        # --- a client's paid audit: invoice id, Andre, and Finance (31) verifies the invoice ------------------------
        body = {"domain": CLIENT_DOMAIN, "paths": ["/"]}
        draft = fin.draft(ACME_PARTY)
        other = fin.draft("someone-else-live")
        check("Finance drafts invoices for the client and for someone else", draft.status_code == 201
              and other.status_code == 201)
        draft_iid, other_iid = draft.json()["invoice"]["invoice_id"], other.json()["invoice"]["invoice_id"]
        no_inv = a.post("/tenants/acme-live/audits", {"request_id": rid(), **body}, andre=True)
        no_andre = a.post("/tenants/acme-live/audits", {"request_id": rid(), **body, "invoice_id": draft_iid})
        own_inv = a.post("/tenants/zbm/audits", {"request_id": rid(), "domain": OWN_DOMAIN, "paths": ["/"],
                                                 "invoice_id": invoice_id("zbm")})
        check("a client audit needs a Finance (31) invoice id and Andre; an own audit refuses an invoice id",
              no_inv.status_code == 409 and no_inv.json()["detail"] == "INVOICE_REQUIRED"
              and no_andre.status_code == 403 and no_andre.json()["detail"] == "ANDRE_APPROVAL_REQUIRED"
              and own_inv.status_code == 422 and own_inv.json()["detail"] == "INVOICE_NOT_FOR_OWN_TENANT")
        unbound = a.post("/tenants/acme-live/audits", {"request_id": rid(), **body, "invoice_id": draft_iid},
                         andre=True)
        bind = a.post("/tenants/acme-live/finance-client", {"request_id": rid(), "finance_client_id": ACME_PARTY},
                      andre=True)
        check("a tenant not bound to a Finance client is refused; Andre binds it",
              unbound.status_code == 409 and unbound.json()["detail"] == "TENANT_FINANCE_CLIENT_UNBOUND"
              and bind.status_code == 200 and bind.json()["finance_client_id"] == ACME_PARTY)
        unpaid = a.post("/tenants/acme-live/audits", {"request_id": rid(), **body, "invoice_id": draft_iid,
                                                      "invoice_override": "ANDRE_CONFIRMED_PAYMENT"}, andre=True)
        unknown = a.post("/tenants/acme-live/audits", {"request_id": rid(), **body,
                                                       "invoice_id": invoice_id("acme unknown")}, andre=True)
        theirs = a.post("/tenants/acme-live/audits", {"request_id": rid(), **body, "invoice_id": other_iid},
                        andre=True)
        check("the real Finance answers: a draft is INVOICE_NOT_PAID (not overridable), an unknown id "
              "INVOICE_NOT_FOUND, another client's INVOICE_TENANT_MISMATCH",
              unpaid.status_code == 409 and unpaid.json() == {"detail": "INVOICE_NOT_PAID", "override_allowed": False}
              and unknown.status_code == 409 and unknown.json()["detail"] == "INVOICE_NOT_FOUND"
              and theirs.status_code == 409 and theirs.json()["detail"] == "INVOICE_TENANT_MISMATCH")
        stop(fp, "finance")
        over_iid = invoice_id("acme override")
        down = a.post("/tenants/acme-live/audits", {"request_id": rid(), **body, "invoice_id": over_iid}, andre=True)
        none_yet = a.get("/tenants/acme-live/audits").json()
        check("Finance stopped: the paid run is refused FINANCE_UNAVAILABLE, override allowed; no refused attempt "
              "recorded an audit", down.status_code == 503 and down.json() == {"detail": "FINANCE_UNAVAILABLE",
                                                                            "override_allowed": True}
              and none_yet == [])
        cl = a.post("/tenants/acme-live/audits", {"request_id": rid(), **body, "invoice_id": over_iid,
                                                  "invoice_override": "FINANCE_OUTAGE"}, andre=True)
        v = cl.json().get("invoice_verification") or {}
        check("Andre's override of an unverifiable invoice runs the audit and says so",
              cl.status_code == 201 and cl.json()["status"] == "completed" and cl.json()["andre_approved"] is True
              and v.get("verdict") == "OVERRIDDEN" and v.get("cause") == "FINANCE_UNAVAILABLE")
        replay = a.post("/tenants/acme-live/audits", {"request_id": rid(), **body, "invoice_id": over_iid,
                                                      "invoice_override": "FINANCE_OUTAGE"}, andre=True)
        check("the same invoice cannot pay for a second run, override or not",
              replay.status_code == 409 and replay.json()["detail"] == "INVOICE_ALREADY_USED")
        client_aid = cl.json()["audit_id"]
        fin = a.get(f"/tenants/acme-live/audits/{client_aid}", caller="finance_31")
        hub_other = a.get(f"/tenants/zbm/audits/{own_aid}", caller="hub", tenant="acme-live")
        check("Finance (31) reads the audit's status, never the report; the hub cannot read another tenant's audit",
              fin.status_code == 200 and "report" not in fin.json() and fin.json()["status"] == "completed"
              and hub_other.status_code == 404)
        pii = a.post("/tenants/zbm/audits", {"request_id": rid(), "domain": OWN_DOMAIN, "paths": ["/"],
                                             "meta": {"client_ip": "203.0.113.9"}})
        check("a personal-data key anywhere in a body is refused 422 and not echoed",
              pii.status_code == 422 and "203.0.113.9" not in pii.text)

        # --- a scheduled re-audit: one run per slot ----------------------------------------------------------------
        sch = a.post("/tenants/zbm/schedules", {"request_id": rid(), "domain": OWN_DOMAIN, "paths": ["/"],
                                                "every_days": 7})
        sid = sch.json()["schedule_id"]
        t1 = a.job("schedule-tick").json()
        t2 = a.job("schedule-tick").json()
        sv = a.get(f"/tenants/zbm/schedules/{sid}").json()
        check("a schedule's due slot runs once; the next tick finds it done",
              sch.status_code == 201 and t1.get("ran") == 1 and t2.get("ran") == 0 and t2.get("already_done", 0) >= 1
              and sv["slots"].get("0", {}).get("status") == "completed")
        dep = a.get("/department", caller="compliance_38").json()
        card = dep.get("agents", {}).get("selene", {}).get("scorecard", {})
        check("the department view counts recorded runs only (Selene ran three audits, all KILLED)",
              card.get("runs") == 3 and card.get("outcomes", {}).get("KILLED") == 3)

        # --- restart ----------------------------------------------------------------------------------------------
        stop(sp, "service")
        sp = start([sys.executable, "-m", "api"], env, src, "service", work)
        wait_health(S + "/health")
        st = a.get("/status").json()
        audits = {x["audit_id"]: x for x in a.get("/tenants/zbm/audits").json()}
        again = a.post("/kill-switches", {"request_id": rid(), "switch": "provider:web", "engaged": False},
                       andre=True)
        check("after a restart the log re-verifies, every record survives, and the switch held until Andre released it",
              st["integrity"]["ok"] is True and audits.get(own_aid, {}).get("status") == "completed"
              and st["kill_switches"]["providers"]["web"] is True
              and again.status_code == 200 and again.json()["providers"]["web"] is False)
        integ = a.get("/audit/integrity", caller="compliance_38").json()
        check("the integrity route reports the ledger's real verdict",
              integ["ledger_valid"] is True and integ["integrity"]["ok"] is True)
        evd = a.get("/audit/evidence?limit=1000", caller="compliance_38").json()
        # an internal record (a report, the seed) has no request id: its key is the subject plus the request key
        done = [(e["event_type"], e["subject_id"], e["rk"]) for e in evd["evidence"] if e["status"] == "committed"]
        say(f"evidence view: {evd['committed']} committed, {evd['attempted']} attempted")
        check("the evidence view marks each logical action committed exactly once against the real ledger",
              evd["committed"] > 0 and evd["total"] <= 1000 and len(done) == len(set(done)) == evd["committed"]
              and evd["rule"] == "unanchored evidence = attempted, not done")

        # --- a truncated log is caught ----------------------------------------------------------------------------
        stop(sp, "service")
        path = data / "seo_log.jsonl"
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(lines[:-2]))
        sp = start([sys.executable, "-m", "api"], env, src, "service", work)
        hs = wait_health(S + "/health")
        r = a.post("/kill-switches", {"request_id": rid(), "switch": "provider:web", "engaged": False}, andre=True)
        check("a truncated log is detected against the ledger and nothing that needs the log takes effect",
              hs["status"] == "degraded" and r.status_code == 503)
        path.write_bytes(b"".join(lines))
        stop(sp, "service")

        # --- the ledger -------------------------------------------------------------------------------------------
        ents = httpx.get(L + "/ledger/entries", headers=lh, timeout=60).json()
        mine_l = [x for x in ents if x.get("department") == "seo"]
        types = sorted({x["event_type"] for x in mine_l})
        say(f"ledger: {len(ents)} entries, {len(mine_l)} from seo; seo types: {', '.join(types)}")
        check("tenants, domains, switches, audit requests, approvals, invoice overrides, reports and schedules are "
              "typed events", {"log_anchor", "tenant_created", "tenant_domains_set", "tenant_finance_client_set",
                               "kill_switch_set", "audit_requested", "audit_approved_by_andre",
                               "invoice_verification_overridden_by_andre", "audit_report_recorded",
                               "schedule_created"} <= set(types))
        reports = [x for x in mine_l if x["event_type"] == "audit_report_recorded"]
        check("every completed audit (two one-off, one own and one client, plus one scheduled) has its report "
              "recorded once", len({x["subject_id"] for x in reports}) == 3)
        blob = json.dumps(mine_l)
        check("no domain, NAP, Finance client id or page content in the clear in seo's ledger events",
              OWN_DOMAIN not in blob and CLIENT_DOMAIN not in blob and "5318 East 2nd" not in blob
              and "248-6617" not in blob and ACME_PARTY not in blob)
        say(f"finance events on the shared ledger: {sum(1 for x in ents if x.get('department') == 'finance')}")
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
                    p.wait(timeout=5)
        (work / "live_run.txt").write_text("\n".join(LOG) + "\n")


def main() -> int:
    keep_in = os.environ.get("LIVE_WORK_DIR") or None
    work = Path(tempfile.mkdtemp(prefix="seo-live-", dir=keep_in))
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
