#!/usr/bin/env python3
"""
New Business Development (12) live run: the REAL ledger-rust binary and bizdev-py's production entrypoint
(``cd src && python3 -m api``) over real HTTP, with a durable data directory, in production mode.

usage: LEDGER_BIN=/path/to/ledger-rust/target/release/server python3 devtools/live_run.py

Proves: start-up integrity against the real ledger; a bid needs Andre, a response needs his approval by hash, an
approved submission stays queued (no port); a submission past its stored deadline is refused (a deadline a few
seconds out, waited past: elapsed time only); a government bid cannot be submitted until every item is attested and
its sensitive items are flagged; a split deal is aggregated over the threshold; a raw SSN is refused; agreements and
partner wins are refused LEGAL_UNAVAILABLE; outreach stays queued and a reply holds the contact; a NOT_BUILT setting
refuses start; a second process on the data directory refuses; a restart keeps everything and re-verifies; a
truncated log is detected; nothing personal, no amount and no name on the ledger; GET /ledger/verify valid.

No check depends on the wall-clock hour or the time zone of the machine (the sales-py live run broke on one): the
only time-based check waits past a deadline set a few seconds ahead, a lower bound on elapsed time. Exit 0 only if
every check holds. Kills only the PIDs it started. Ports are free ports from bind(0). Tokens are derived here
(sha256 of a live-run label), never secret-shaped literals.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve()
SVC = HERE.parents[1]


def derived(label: str) -> str:
    return hashlib.sha256(f"bizdev-py live-run only {label}".encode()).hexdigest()


LEDGER_TOKEN = derived("ledger token")
TOKEN = derived("service token")
ANDRE = derived("andre approval token")
CALLERS = {c: derived(f"caller {c}") for c in ("dashboard", "bizdev_agent", "scheduler", "provider_events", "hub",
                                               "finance_31", "compliance_38")}
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


def iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat() \
        .replace("+00:00", "Z")


def rid() -> str:
    """A UUID: the one opaque request-id shape the service accepts (ADR 0016 decision 27)."""
    return str(uuid.uuid4())


def fin_id(prefix: str = "evt") -> str:
    """A finance-py generated id (``ledger.derived_id``: fin-<prefix>-<40 hex>)."""
    return f"fin-{prefix}-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


class Api:
    def __init__(self, base: str):
        self.base = base

    def h(self, caller, andre=False):
        h = {"Authorization": f"Bearer {TOKEN}", "X-NBD-Caller-Token": CALLERS[caller]}
        if andre:
            h["X-Andre-Approval-Token"] = ANDRE
        return h

    def post(self, path, body, caller="bizdev_agent", andre=False):
        if andre:
            caller = "dashboard"
        return httpx.post(self.base + "/nbd/v1" + path, json=body, headers=self.h(caller, andre), timeout=30)

    def get(self, path, caller="dashboard"):
        return httpx.get(self.base + "/nbd/v1" + path, headers=self.h(caller), timeout=30)

    def job(self, name):
        return self.post(f"/jobs/{name}/run", {"request_id": rid()}, caller="scheduler")


def pursuit(a: Api, kind="rfp", value="5000.00", ref="org:live-acme", name="Live Acme Inc", domain="liveacme.test",
            deadline=None, **extra):
    body = {"request_id": rid(), "brand": "zbm", "kind": kind, "title": "Live OOH RFP",
            "counterparty": {"ref": ref, "name": name, "domain": domain}, "value": value,
            "deadline": deadline or iso_in(30 * 86400), **extra}
    return a.post("/pursuits", body)


def bid(a: Api, pid: str):
    crit = {c: "yes" for c in ("scope_fit", "capacity", "deadline_feasible", "compliance_feasible", "relationship",
                               "price_competitive", "payment_terms_acceptable")}
    q = a.post(f"/pursuits/{pid}/qualification", {"request_id": rid(), **crit}).json()["qualification"]
    body = {"request_id": rid(), "decision": "bid", "qualification_sha256": q["qualification_sha256"]}
    return a.post(f"/pursuits/{pid}/bid-decision", body, andre=True)


def approved_block(a: Api, key: str):
    b = a.post("/blocks", {"request_id": rid(), "block_key": key, "brand": "zbm", "title": "About",
                           "text": "Z Best Media runs outdoor and digital campaigns."}).json()
    v = b["versions"][0]
    a.post(f"/blocks/{b['block_id']}/versions/1/approve", {"request_id": rid(), "content_sha256":
                                                           v["content_sha256"]}, andre=True)
    return b


def approved_response(a: Api, pid: str, block: dict):
    r = a.post("/responses", {"request_id": rid(), "pursuit_id": pid,
                              "parts": [{"block_id": block["block_id"], "version": 1},
                                        {"custom": "Twelve digital boards for six weeks."}]}).json()
    v = r["versions"][0]
    a.post(f"/responses/{r['response_id']}/approve", {"request_id": rid(), "version": 1,
                                                      "content_sha256": v["content_sha256"],
                                                      "acknowledged_flags": v["flags"]}, andre=True)
    return r, v


def _main(work: Path) -> int:
    ledger_bin = os.environ["LEDGER_BIN"]
    pl, ps = free_port(), free_port()
    L, S = f"http://127.0.0.1:{pl}", f"http://127.0.0.1:{ps}"
    lh = {"Authorization": f"Bearer {LEDGER_TOKEN}"}
    data = work / "bizdev"
    etc = work / "etc"
    etc.mkdir(mode=0o700)
    key_file = etc / "pii.key"
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, os.urandom(32).hex().encode())
    os.close(fd)
    env = {"NBD_SERVICE_TOKEN": TOKEN, "NBD_CALLER_TOKENS": json.dumps(CALLERS), "NBD_ANDRE_APPROVAL_TOKEN": ANDRE,
           "NBD_DATA_DIR": str(data), "NBD_PII_HASH_KEY_FILE": str(key_file), "NBD_PORT": str(ps),
           "NBD_OUTREACH_DOMAIN": "zbm-partners.test", "NBD_ZBM_DOMAIN": "zbestmedia.test",
           "NBD_ZBC_DOMAIN": "zbestclips.test", "NBD_POSTAL_ADDRESS": "123 Live Street, Los Angeles, CA 90001",
           "LEDGER_SERVICE_URL": L, "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN}
    try:
        (work / "ledger").mkdir()
        start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(pl),
                             "LEDGER_LOG_PATH": str(work / "ledger" / "ledger.jsonl")}, str(work / "ledger"), "ledger",
              work)
        say(f"ledger health: {wait_health(L + '/health')}")

        # --- a NOT_BUILT setting refuses start (before anything else touches the data directory) ----------------
        bad = subprocess.run([sys.executable, "-m", "api"], env={**os.environ, **env,
                                                                 "NBD_EMAIL_PROVIDER": "smtp"},
                             cwd=str(SVC / "src"), capture_output=True, timeout=30)
        check("a NOT_BUILT provider setting refuses start", bad.returncode != 0 and b"NBD_EMAIL_PROVIDER" in bad.stderr)

        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "service", work)
        wait_health(S + "/health")
        a = Api(S)
        st = a.get("/status").json()
        check("durable, production mode, integrity verified against the real ledger",
              st["in_memory"] is False and st["non_production"] is False and st["integrity"]["ok"] is True
              and st["andre_approvals_configured"] is True)
        check("every port is a stand-in", not any(st["ports_wired"].values()))
        second = subprocess.run([sys.executable, "-m", "api"], env={**os.environ, **env, "NBD_PORT": str(free_port())},
                                cwd=str(SVC / "src"), capture_output=True, timeout=30)
        check("a second process on the same data directory refuses",
              second.returncode != 0 and b"another bizdev-py process" in second.stderr)

        # --- a pursuit: Andre bids and approves; the submission stays queued ------------------------------------
        p = pursuit(a).json()
        pid = p["pursuit_id"]
        crit = {c: "yes" for c in ("scope_fit", "capacity", "deadline_feasible", "compliance_feasible", "relationship",
                                   "price_competitive", "payment_terms_acceptable")}
        q = a.post(f"/pursuits/{pid}/qualification", {"request_id": rid(), **crit}).json()["qualification"]
        body = {"request_id": rid(), "decision": "bid", "qualification_sha256": q["qualification_sha256"]}
        r1 = a.post(f"/pursuits/{pid}/bid-decision", body)
        r2 = a.post(f"/pursuits/{pid}/bid-decision", {**body, "request_id": rid()}, andre=True)
        check("the agent cannot decide to bid; Andre can",
              r1.status_code == 403 and r2.status_code == 200 and r2.json()["stage"] == "responding")
        block = approved_block(a, "about-live")
        r = a.post("/responses", {"request_id": rid(), "pursuit_id": pid,
                                  "parts": [{"block_id": block["block_id"], "version": 1},
                                            {"custom": "Twelve digital boards for six weeks."}]}).json()
        v = r["versions"][0]
        sub_early = a.post(f"/responses/{r['response_id']}/submit",
                           {"request_id": rid(), "version": 1, "content_sha256": v["content_sha256"]})
        wrong = a.post(f"/responses/{r['response_id']}/approve", {"request_id": rid(), "version": 1,
                                                                  "content_sha256": "0" * 64,
                                                                  "acknowledged_flags": []}, andre=True)
        a.post(f"/responses/{r['response_id']}/approve", {"request_id": rid(), "version": 1,
                                                          "content_sha256": v["content_sha256"],
                                                          "acknowledged_flags": v["flags"]}, andre=True)
        sub = a.post(f"/responses/{r['response_id']}/submit",
                     {"request_id": rid(), "version": 1, "content_sha256": v["content_sha256"]})
        tick = a.job("submission-queue").json()
        check("a response needs Andre's approval of its exact hash before it can be submitted",
              sub_early.status_code == 409 and sub_early.json()["detail"] == "RESPONSE_NOT_APPROVED"
              and wrong.status_code == 409 and sub.status_code == 201)
        check("an approved submission stays queued (submission port not wired)",
              tick.get("not_wired") == 1 and a.get("/submissions?status=queued", caller="bizdev_agent").json())
        queued_sub = sub.json()["submission_id"]

        # --- a deadline a few seconds out: waited past, the submission is refused --------------------------------
        near = pursuit(a, ref="org:live-near", name="Near Co", domain="near.test", deadline=iso_in(10)).json()
        bid(a, near["pursuit_id"])
        nr, nv = approved_response(a, near["pursuit_id"], block)
        deadline = datetime.fromisoformat(near["deadline"].replace("Z", "+00:00"))
        while datetime.now(timezone.utc) <= deadline + timedelta(seconds=1):     # elapsed time only, never the hour
            time.sleep(0.25)
        late = a.post(f"/responses/{nr['response_id']}/submit",
                      {"request_id": rid(), "version": 1, "content_sha256": nv["content_sha256"]})
        check("a submission past its stored deadline is refused",
              late.status_code == 409 and late.json()["detail"] == "DEADLINE_PASSED")

        # --- a government bid: nothing auto-attested; sensitive items flagged to Andre ---------------------------
        g = pursuit(a, kind="government_bid", ref="gov:live-county", name="Live County", domain="livecounty.gov").json()
        bid(a, g["pursuit_id"])
        gr, gv = approved_response(a, g["pursuit_id"], block)
        gsub = a.post(f"/responses/{gr['response_id']}/submit",
                      {"request_id": rid(), "version": 1, "content_sha256": gv["content_sha256"]})
        tasks = a.get("/tasks?status=open").json()
        check("a government bid is refused until Andre attests every item; nothing is pre-attested",
              gsub.status_code == 409 and gsub.json()["detail"] == "CHECKLIST_INCOMPLETE"
              and all(i["attested_at"] is None for i in g["checklist"]))
        check("conflict-of-interest, gift, lobbying and contingent-fee items are flagged to Andre",
              {"conflict_of_interest_disclosure", "gifts_gratuities_certification", "lobbying_certification_disclosure",
               "contingent_fee_representation"} <= {t["code"] for t in tasks if t["kind"] == "checklist_sensitive"})
        for item in g["checklist"]:
            a.post(f"/pursuits/{g['pursuit_id']}/checklist/{item['item_id']}/attest",
                   {"request_id": rid(), "item_sha256": item["item_sha256"]}, andre=True)
        gsub = a.post(f"/responses/{gr['response_id']}/submit",
                      {"request_id": rid(), "version": 1, "content_sha256": gv["content_sha256"]})
        check("once Andre attested each item by hash, the bid is queued", gsub.status_code == 201)

        # --- splitting a deal does not get around the threshold ---------------------------------------------------
        s1 = pursuit(a, value="6000.00", ref="org:split-1", name="Split Co", domain="split.test").json()
        bid(a, s1["pursuit_id"])
        s1r, s1v = approved_response(a, s1["pursuit_id"], block)
        s2 = pursuit(a, value="6000.00", ref="org:split-2", name="SPLIT, Incorporated", domain="www.split.test").json()
        blocked = a.post(f"/responses/{s1r['response_id']}/submit",
                         {"request_id": rid(), "version": 1, "content_sha256": s1v["content_sha256"]})
        check("two $6,000 deals with one counterparty aggregate over $10,000 and both need Andre",
              s2["deal_gate"]["aggregate"] == "12000.00" and s2["deal_gate"]["needs_andre"] is True
              and blocked.status_code == 409 and blocked.json()["detail"] == "DEAL_APPROVAL_REQUIRED")

        # --- partners: tax refs only, rates by Andre, Legal a stand-in -------------------------------------------
        pt = a.post("/partners", {"request_id": rid(), "partner_key": "live-west", "kind": "referral",
                                  "brands": ["zbm"], "name": "Live West Agency", "domain": "livewest.test"}).json()
        ssn = a.post(f"/partners/{pt['partner_id']}/payee", {"request_id": rid(), "finance_payee_ref": "fin:payee-live",
                                                             "tax_info_ref": "vault:tax:123-45-6789-abcdefgh"},
                     andre=True)
        check("a raw SSN in a partner's tax reference is refused 422 and not echoed",
              ssn.status_code == 422 and ssn.json()["detail"] == "TAX_ID_RAW_REFUSED" and "6789" not in ssn.text)
        prop = a.post(f"/partners/{pt['partner_id']}/rate", {"request_id": rid(), "version": 1,
                                                             "rate_pct": "12.50"}).json()
        agent_appr = a.post(f"/partners/{pt['partner_id']}/rate/approve",
                            {"request_id": rid(), "version": 1,
                             "binding_sha256": prop["rate_proposed"]["binding_sha256"]})
        appr = a.post(f"/partners/{pt['partner_id']}/rate/approve",
                      {"request_id": rid(), "version": 1, "binding_sha256": prop["rate_proposed"]["binding_sha256"]},
                      andre=True)
        check("only Andre approves a partner's rate", agent_appr.status_code == 403 and appr.status_code == 200)
        agr = a.post(f"/partners/{pt['partner_id']}/agreements", {"request_id": rid(), "kind": "referral_agreement"})
        deal = a.post("/partner-deals", {"request_id": rid(), "partner_id": pt["partner_id"], "brand": "zbm",
                                         "counterparty": {"ref": "org:live-client", "name": "Live Client",
                                                          "domain": "liveclient.test"},
                                         "deal_value": "8000.00"}).json()
        won = a.post(f"/partner-deals/{deal['deal_id']}/won", {"request_id": rid(),
                                                               "agreement_kind": "referral_agreement"}, andre=True)
        check("agreements and partner wins are refused LEGAL_UNAVAILABLE while Legal is a stand-in",
              agr.status_code == 503 and agr.json()["detail"] == "LEGAL_UNAVAILABLE"
              and won.status_code == 503 and won.json()["detail"] == "LEGAL_UNAVAILABLE")
        pay = a.post("/finance/events", {"request_id": rid(), "finance_event_id": fin_id(),
                                         "deal_id": deal["deal_id"], "kind": "payment", "amount": "100.00",
                                         "currency": "USD"}, caller="finance_31")
        flt = a.post("/finance/events", {"request_id": rid(), "finance_event_id": fin_id(),
                                         "deal_id": deal["deal_id"], "kind": "payment", "amount": 100.0,
                                         "currency": "USD"}, caller="finance_31")
        check("no commission without a won deal; a float amount is refused",
              pay.status_code == 409 and pay.json()["detail"] == "DEAL_NOT_WON" and flt.status_code == 422)

        # --- outreach: approved template by hash, stays queued, a reply holds -------------------------------------
        t = a.post("/templates", {"request_id": rid(), "template_key": "live-intro", "brand": "zbm",
                                  "subject": "A referral partnership idea",
                                  "body": "Hi {{first_name}}, a partnership between {{company}} and us?"}).json()
        tv = t["versions"][0]
        a.post(f"/templates/{t['template_id']}/versions/1/approve", {"request_id": rid(),
                                                                     "content_sha256": tv["content_sha256"]},
               andre=True)
        c = a.post("/contacts", {"request_id": rid(), "brand": "zbm", "email": "pat@livewest.test",
                                 "name": "Pat Live", "partner_id": pt["partner_id"]}).json()
        a.post(f"/contacts/{c['contact_id']}/merge-fields", {"request_id": rid(), "first_name": "Pat",
                                                             "company": "Live West"}, caller="dashboard")
        qbody = {"contact_id": c["contact_id"], "template_id": t["template_id"], "version": 1,
                 "content_sha256": tv["content_sha256"]}
        m = a.post("/outreach/email", {"request_id": rid(), **qbody})
        send = a.job("send-queue").json()
        check("outreach email queues from an approved template and stays queued (email port not wired)",
              m.status_code == 201 and send.get("not_wired") == 1)
        rep = a.post("/replies", {"request_id": rid(), "message_id": m.json()["message_id"],
                                  "text": "Sounds good, tell me more"}, caller="provider_events").json()
        again = a.post("/outreach/email", {"request_id": rid(), **qbody})
        check("any reply holds further outreach until Andre decides",
              rep.get("held") is True and again.status_code == 403 and again.json()["detail"] == "CONTACT_HELD")
        stop_idn = a.post("/replies", {"request_id": rid(), "message_id": m.json()["message_id"],
                                       "from_email": "pat@xn--80ak6aa92e.xn--p1ai", "text": "STOP"},
                          caller="provider_events")
        weird = a.post("/replies", {"request_id": rid(), "from_email": '"pat lee"@livewest.test', "text": "remove me"},
                       caller="provider_events")
        check("a STOP from an IDN or unparseable sender is recorded, held and suppressed through the message",
              stop_idn.status_code == 201 and stop_idn.json()["suppressed"] is True
              and weird.status_code == 201 and weird.json()["held"] is True)

        # --- restart ---------------------------------------------------------------------------------------------
        stop(sp, "service")
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "service", work)
        wait_health(S + "/health")
        st = a.get("/status").json()
        subs = {s["submission_id"]: s for s in a.get("/submissions", caller="bizdev_agent").json()}
        again = a.post("/outreach/email", {"request_id": rid(), **qbody})
        check("after a restart the log re-verifies and every record survives",
              st["integrity"]["ok"] is True and subs.get(queued_sub, {}).get("status") == "queued"
              and again.status_code == 403 and again.json()["detail"] == "SUPPRESSED")
        integ = a.get("/audit/integrity", caller="compliance_38").json()
        check("the integrity route reports the ledger's real verdict", integ["ledger_valid"] is True
              and integ["integrity"]["ok"] is True)

        # --- a truncated log is caught ----------------------------------------------------------------------------
        stop(sp, "service")
        path = data / "bizdev_log.jsonl"
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(lines[:-2]))
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "service", work)
        hs = wait_health(S + "/health")
        r = pursuit(a, ref="org:after-truncate", name="After Co", domain="after.test")
        check("a truncated log is detected against the ledger and nothing takes effect",
              hs["status"] == "degraded" and r.status_code == 503)
        path.write_bytes(b"".join(lines))
        stop(sp, "service")

        # --- the ledger -----------------------------------------------------------------------------------------
        ents = httpx.get(L + "/ledger/entries", headers=lh, timeout=60).json()
        mine = [x for x in ents if x.get("department") == "bizdev"]
        types = sorted({x["event_type"] for x in mine})
        say(f"ledger: {len(ents)} entries, {len(mine)} from bizdev; types: {', '.join(types)}")
        check("bids, approvals, attestations, submissions, rates and holds are typed events on the ledger",
              {"log_anchor", "pursuit_opened", "bid_decided", "block_approved", "response_approved",
               "submission_queued", "checklist_attested", "rate_approved", "template_approved", "reply_holds_applied",
               "contact_created"} <= set(types))
        blob = json.dumps(ents)
        check("no email, name, amount, rate or reply text on the ledger",
              "pat@livewest.test" not in blob and "Live West" not in blob and "6000.00" not in blob
              and "12.50" not in blob and "Sounds good" not in blob and "Live Acme" not in blob)
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
    work = Path(tempfile.mkdtemp(prefix="bizdev-live-", dir=keep_in))
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
