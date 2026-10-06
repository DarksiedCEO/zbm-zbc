#!/usr/bin/env python3
"""
Customer Service (30) + Client Success (29) live run: the REAL ledger-rust binary and service-py's production
entrypoint (``cd src && python3 -m api``) over real HTTP, with a durable data directory.

usage: LEDGER_BIN=/path/to/ledger-rust/target/release/server python3 devtools/live_run.py

Proves: start-up integrity against the real ledger; an approved answer sent right away and an unapproved one never;
a refund, a lawyer and a privacy request escalated (never answered); SMS refused without consent, STOP revoking it;
an at-risk account alerted with a save plan that accepts only an approved offer; a restart that keeps everything and
re-verifies; a truncated log detected after a restart; nothing personal on the ledger; GET /ledger/verify valid.
Exit 0 only if every check holds. Kills only the PIDs it started. Ports are free ports from bind(0).
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

LEDGER_TOKEN = "live-ledger-token-" + "l" * 24
TOKEN = "live-svc-service-token-" + "s" * 20
ANDRE = "live-andre-approval-token-" + "a" * 20
CALLERS = {c: f"live-svc-caller-{c}-" + "c" * 24 for c in ("hub", "sms_gateway", "email_gateway", "onboarding",
                                                           "dashboard", "scheduler")}
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


def daytime_zone() -> str:
    """A time zone where it is between 10:00 and 18:00 now (SMS quiet hours are recipient-local)."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    for tz in ("America/Los_Angeles", "America/New_York", "Europe/London", "Europe/Berlin", "Asia/Dubai",
               "Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney", "Pacific/Auckland",
               "Pacific/Honolulu", "America/Anchorage", "America/Sao_Paulo", "Atlantic/Azores"):
        if 10 <= datetime.now(timezone.utc).astimezone(ZoneInfo(tz)).hour < 18:
            return tz
    return "UTC"


def rid() -> str:
    return "live-" + uuid.uuid4().hex


class Api:
    def __init__(self, base: str):
        self.base = base

    def h(self, caller, andre=False):
        h = {"Authorization": f"Bearer {TOKEN}", "X-SVC-Caller-Token": CALLERS[caller]}
        if andre:
            h["X-Andre-Approval-Token"] = ANDRE
        return h

    def post(self, path, body, caller="dashboard", andre=False):
        return httpx.post(self.base + path, json=body, headers=self.h(caller, andre), timeout=30)

    def get(self, path, caller="dashboard"):
        return httpx.get(self.base + path, headers=self.h(caller), timeout=30)


def _main(work: Path) -> int:
    ledger_bin = os.environ["LEDGER_BIN"]
    pl, ps = free_port(), free_port()
    L, S = f"http://127.0.0.1:{pl}", f"http://127.0.0.1:{ps}"
    lh = {"Authorization": f"Bearer {LEDGER_TOKEN}"}
    data = work / "service"
    etc = work / "etc"
    etc.mkdir(mode=0o700)
    key_file = etc / "hmac.key"
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, base64.b64encode(os.urandom(32)))
    os.close(fd)
    env = {"SVC_HMAC_KEY_FILE": str(key_file),"SVC_SERVICE_TOKEN": TOKEN, "SVC_CALLER_TOKENS": json.dumps(CALLERS), "SVC_ANDRE_APPROVAL_TOKEN": ANDRE,
           "SVC_DATA_DIR": str(data), "SVC_PORT": str(ps), "SVC_SUPPORT_EMAIL_ZBM": "support@zbestmedia.test",
           "SVC_SUPPORT_EMAIL_ZBC": "support@zbestclips.test", "SVC_SMS_NUMBER_ZBM": "+13105550100",
           "SVC_SMS_NUMBER_ZBC": "+13105550200", "LEDGER_SERVICE_URL": L, "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN}
    try:
        (work / "ledger").mkdir()
        start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(pl),
                             "LEDGER_LOG_PATH": str(work / "ledger" / "ledger.jsonl")}, str(work / "ledger"), "ledger",
              work)
        say(f"ledger health: {wait_health(L + '/health')}")
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "service", work)
        wait_health(S + "/health")
        a = Api(S)
        st = a.get("/svc/v1/status").json()
        say(f"service-py status: integrity={st['integrity']['ok']} in_memory={st['in_memory']} "
            f"andre={st['andre_approvals_configured']}")
        check("durable, production mode, integrity verified against the real ledger",
              st["in_memory"] is False and st["non_production"] is False and st["integrity"]["ok"] is True)

        # --- approved answers ---------------------------------------------------------------------------------
        art = a.post("/svc/v1/kb/articles", {"request_id": rid(), "item_id": "hours", "brands": ["zbm", "zbc"],
                                             "channels": ["chat", "email", "sms"], "title": "Opening hours",
                                             "answer": "We are open Monday to Friday, 9am to 6pm Pacific.",
                                             "rules": {"any": ["hours", "open"]}}).json()
        r = a.post("/svc/v1/chat/messages", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:live1",
                                             "text": "what are your hours?"}, caller="hub").json()
        check("an unapproved article is never used", r["action"] == "queued_for_human" and "answer" not in r)
        r = a.post("/svc/v1/kb/articles/hours/approve", {"request_id": rid(), "version": art["version"],
                                                         "content_sha256": art["content_sha256"]})
        check("the dashboard alone cannot approve", r.status_code == 403)
        r = a.post("/svc/v1/kb/articles/hours/approve", {"request_id": rid(), "version": art["version"],
                                                         "content_sha256": art["content_sha256"]}, andre=True)
        check("Andre approves the exact version", r.status_code == 200)
        r = a.post("/svc/v1/chat/messages", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:live2",
                                             "text": "When are you open?"}, caller="hub").json()
        check("a routine question is answered right away with the approved text",
              r["action"] == "answered" and r["answer"]["text"].startswith("We are open"))
        answered_ticket = r["ticket_id"]

        # --- escalations ----------------------------------------------------------------------------------------
        r = a.post("/svc/v1/chat/messages", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:live3",
                                             "text": "What are your hours? Also I want a refund."}, caller="hub").json()
        check("a refund is never auto-answered", r["action"] == "escalated" and "answer" not in r)
        t = a.get(f"/svc/v1/tickets/{r['ticket_id']}").json()
        check("money goes to Andre with a Finance reference (not wired)",
              t["queue"] == "andre" and [x["status"] for x in t["handoffs"]] == ["not_wired"])
        r = a.post("/svc/v1/inbound/email", {"request_id": rid(), "brand": "zbc", "to_address":
                                             "support@zbestclips.test", "from_address": "creator@live.test",
                                             "subject": "clip", "text": "My lawyer says your clip infringes"},
                   caller="email_gateway").json()
        t = a.get(f"/svc/v1/tickets/{r['ticket_id']}").json()
        check("'my lawyer' routes to Legal (37)", {x["department"] for x in t["handoffs"]} == {"legal_37"})
        r = a.post("/svc/v1/chat/messages", {"request_id": rid(), "brand": "zbc", "contact_ref": "client:live4",
                                             "text": "Please delete my data"}, caller="hub").json()
        t = a.get(f"/svc/v1/tickets/{r['ticket_id']}").json()
        check("a data deletion request goes to Compliance and Legal, unanswered",
              r["action"] == "escalated" and {"compliance_38", "legal_37"} <= {x["department"] for x in t["handoffs"]})

        # --- SMS consent ----------------------------------------------------------------------------------------
        cid = a.post("/svc/v1/contacts", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:sms",
                                          "phone": "+13105551234", "timezone": "America/Los_Angeles"},
                     caller="hub").json()["contact_id"]
        r = a.post("/svc/v1/inbound/sms", {"request_id": rid(), "brand": "zbm", "to_number": "+13105550100",
                                           "from_number": "+13105551234", "text": "I need help"},
                   caller="sms_gateway").json()
        sms_ticket = r["ticket_id"]
        r = a.post(f"/svc/v1/tickets/{sms_ticket}/reply", {"request_id": rid(), "text": "Hi"}, andre=True)
        check("an SMS without recorded consent is refused", r.status_code == 409 and
              r.json()["detail"] == "SMS_CONSENT_REQUIRED")
        a.post("/svc/v1/consents", {"request_id": rid(), "contact_id": cid, "channel": "sms",
                                        "address": "+13105551234", "source": "portal_form",
                                    "consent_text": "I agree to receive texts.", "captured_at": "2026-10-01T10:00:00Z",
                                    "express": True}, caller="hub")
        r = a.post("/svc/v1/inbound/sms", {"request_id": rid(), "brand": "zbm", "to_number": "+13105550100",
                                           "from_number": "+13105551234", "text": "Please stop texting me"},
                   caller="sms_gateway").json()
        cons = a.get(f"/svc/v1/contacts/{cid}/consents", caller="hub").json()
        check("an opt-out phrase revokes SMS consent immediately and queues one confirmation",
              r["action"] == "opted_out" and cons[0]["status"] == "revoked" and r.get("confirmation_message_id"))
        r = a.post("/svc/v1/consents", {"request_id": rid(), "contact_id": cid, "channel": "sms",
                                        "address": "+13105551234",
                                        "source": "portal_form", "consent_text": "I agree to receive texts.",
                                        "captured_at": "2026-10-01T10:00:00Z", "express": True}, caller="hub")
        check("a consent captured before the STOP cannot bring it back", r.status_code == 409)

        # --- client success -------------------------------------------------------------------------------------
        a.post("/svc/v1/accounts", {"request_id": rid(), "account_id": "acct-live", "brand": "zbm",
                                    "primary_contact_id": cid}, caller="onboarding")
        a.post("/svc/v1/offers", {"request_id": rid(), "item_id": "draft-offer", "brand": "zbm", "title": "Draft",
                                  "terms": "Not approved.", "price": "10.00"})
        for i in range(3):
            a.post("/svc/v1/chat/messages", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:sms",
                                             "text": f"this is ridiculous, complaint number {i}"}, caller="hub")
        a.post("/svc/v1/jobs/health-recompute/run", {"request_id": rid()}, caller="scheduler")
        hv = a.get("/svc/v1/accounts/acct-live/health").json()
        say(f"health: score={hv['health']['score']} signals={[s['signal'] for s in hv['health']['signals']]}")
        plan = hv["save_plan"]
        check("an at-risk account gets an explainable score, an alert and a save plan",
              hv["at_risk"] is True and plan is not None and
              "ACCOUNT_AT_RISK" in [x["code"] for x in a.get("/svc/v1/alerts").json()])
        if plan is not None:
            r = a.post(f"/svc/v1/save-plans/{plan['plan_id']}/offer", {"request_id": rid(),
                                                                        "offer_id": "draft-offer"}, andre=True)
        check("a save plan refuses an offer Andre has not approved", plan is not None and r.status_code == 409 and
              r.json()["detail"] == "OFFER_NOT_APPROVED")

        # --- restart --------------------------------------------------------------------------------------------
        stop(sp, "service")
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "service", work)
        wait_health(S + "/health")
        st = a.get("/svc/v1/status").json()
        check("after a restart the log re-verifies against the ledger", st["integrity"]["ok"] is True)
        t = a.get(f"/svc/v1/tickets/{answered_ticket}").json()
        check("tickets, messages and answers survive the restart",
              [m["text"] for m in t["messages"]][-1].startswith("We are open"))
        r = a.post(f"/svc/v1/tickets/{sms_ticket}/reply", {"request_id": rid(), "text": "Hi"}, andre=True)
        check("the STOP revocation survives the restart", r.status_code == 409)
        r = a.post("/svc/v1/chat/messages", {"request_id": rid(), "brand": "zbc", "contact_ref": "client:live5",
                                             "text": "what are your hours"}, caller="hub").json()
        check("the approval survives the restart", r["action"] == "answered")

        # --- sending, end to end (V2-L4): the non-production file sender behind the SMS port ---------------------
        stop(sp, "service")
        outbox = work / "outbox.jsonl"
        sp = start([sys.executable, "-m", "api"], {**env, "SVC_NON_PRODUCTION": "1", "SVC_SMS_PROVIDER": "nonprod_file",
                                                   "SVC_NONPROD_OUTBOX_FILE": str(outbox)},
                   str(SVC / "src"), "service", work)
        wait_health(S + "/health")
        tz = daytime_zone()
        cid2 = a.post("/svc/v1/contacts", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:sender",
                                           "phone": "+13105557777", "timezone": tz}, caller="hub").json()["contact_id"]
        a.post("/svc/v1/consents", {"request_id": rid(), "contact_id": cid2, "channel": "sms",
                                    "address": "+13105557777", "source": "portal_form",
                                    "consent_text": "I agree to receive texts.", "captured_at": "2026-10-01T10:00:00Z",
                                    "express": True}, caller="hub")
        t2 = a.post("/svc/v1/inbound/sms", {"request_id": rid(), "brand": "zbm", "to_number": "+13105550100",
                                            "from_number": "+13105557777", "text": "I need help with my campaign"},
                    caller="sms_gateway").json()["ticket_id"]
        rep = a.post(f"/svc/v1/tickets/{t2}/reply", {"request_id": rid(), "text": "Andre here, calling you today."},
                     andre=True).json()
        tick = a.post("/svc/v1/jobs/outbound-tick/run", {"request_id": rid()}, caller="scheduler").json()
        sent = [json.loads(x) for x in outbox.read_text().splitlines()] if outbox.exists() else []
        mine_sent = [x for x in httpx.get(L + "/ledger/entries", headers=lh, timeout=60).json()
                     if x.get("department") == "service" and x.get("subject_id") == rep["message_id"]]
        say(f"tick={tick} outbox={[x['message_id'] for x in sent]} reply={rep} events={[x['event_type'] for x in mine_sent]}")
        check("outbound-tick sends through the SMS port: message_sending, then the send, then message_sent",
              rep["message_id"] in [x["message_id"] for x in sent] and tick.get("sent") == len(sent)
              and [x["event_type"] for x in mine_sent] == ["message_sending", "message_sent"])
        check("the opt-out confirmation queued earlier went out through the same port, once",
              sum(1 for x in sent if x["to"] == "+13105551234" and "unsubscribed" in x["text"]) == 1)

        # --- a truncated log is caught --------------------------------------------------------------------------
        stop(sp, "service")
        path = data / "service_log.jsonl"
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(lines[:-2]))
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "service", work)
        hs = wait_health(S + "/health")
        r = a.post("/svc/v1/chat/messages", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:live6",
                                             "text": "hello"}, caller="hub")
        check("a truncated log is detected against the ledger and nothing takes effect",
              hs["status"] == "degraded" and r.status_code == 503)
        path.write_bytes(b"".join(lines))
        stop(sp, "service")

        # --- the ledger -----------------------------------------------------------------------------------------
        ents = httpx.get(L + "/ledger/entries", headers=lh, timeout=60).json()
        mine = [x for x in ents if x.get("department") == "service"]
        types = sorted({x["event_type"] for x in mine})
        say(f"ledger: {len(ents)} entries, {len(mine)} from service; types: {', '.join(types)}")
        check("answers, escalations, consent changes, approvals and alerts are on the ledger",
              {"log_anchor", "message_sent", "escalation_opened", "consent_changed", "approval_recorded",
               "alert_raised", "save_plan_started"} <= set(types))
        blob = json.dumps(ents)
        check("no message body, address or phone number on the ledger",
              "refund" not in blob and "creator@live.test" not in blob and "+13105551234" not in blob
              and "We are open" not in blob)
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
    work = Path(tempfile.mkdtemp(prefix="service-live-", dir=keep_in))
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
