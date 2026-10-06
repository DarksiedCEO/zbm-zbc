#!/usr/bin/env python3
"""
Influencer & Partnership Marketing (11) live run: the REAL ledger-rust binary and influencer-py's production entrypoint
(``cd src && python3 -m api``) over real HTTP, with a durable data directory and a PII hash key file. Structured like
services/sales-py/devtools/live_run.py.

usage: LEDGER_BIN=/path/to/ledger-rust/target/release/server python3 devtools/live_run.py

No check depends on the wall-clock hour or the day of the week (influencer outreach has no quiet-hours rule, and nothing
here reads the time except the log lines' own timestamps).

Proves: start-up integrity against the real ledger; a second process on the data directory refused; the email
confirmation round trip (an application or a tax reference changes nothing until the token mailed to the address on
record comes back; a stranger's application attaches no handle); the 18+ rule (an adult application kept, a minor and a
missing attestation refused, nothing kept); replies never refused for their sender fields; a date of birth and raw tax ids
refused; discovery ports not wired; Andre's template approval (a wrong token refused and recorded); outreach email
queued but not sent while no provider is wired; a DM sent only as Andre approved it (and staying queued: no DM
provider); any reply holding outreach until Andre decides; an opt-out suppressing both brands and DMs; the brief's FTC
section; the $5,000 rule with a split deal caught; floats refused; content without the disclosure refused, Andre's
content approval by hash, live refused without a contract; Legal and Finance stand-ins refusing; a restart that keeps
everything; a forged pending line left inert; a truncated log detected; typed events on the ledger, nothing personal on
it; GET /ledger/verify valid. Exit 0 only if every check holds. Kills only the PIDs it started.
"""

from __future__ import annotations

import hashlib
import hmac
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
sys.path[:0] = [str(SVC / "src")]

import store as store_mod  # noqa: E402


def _derived(label: str) -> str:
    return hashlib.sha256(f"influencer-py live-run only {label}".encode()).hexdigest()


LEDGER_TOKEN = "ledger-" + _derived("ledger token")
TOKEN = "svc-" + _derived("service token")
ANDRE = "andre-" + _derived("andre token")
CALLERS = {c: f"{c}-" + _derived(c) for c in ("hub", "dashboard", "influencer_agent", "scheduler",
                                              "provider_events", "compliance_38")}
ATTEST_SHA = hashlib.sha256(b"I confirm I am 18 or older.").hexdigest()
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


def wait_health(url: str, timeout: int = 25) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            r = httpx.get(url, timeout=1)
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


def rid() -> str:
    return "live-" + uuid.uuid4().hex


class Api:
    def __init__(self, base: str):
        self.base = base

    def h(self, caller, andre=None):
        h = {"Authorization": f"Bearer {TOKEN}", "X-INF-Caller-Token": CALLERS[caller]}
        if andre:
            h["X-Andre-Approval-Token"] = andre
        return h

    def post(self, path, body, caller="dashboard", andre=None):
        return httpx.post(self.base + "/inf/v1" + path, json=body, headers=self.h(caller, andre), timeout=30)

    def get(self, path, caller="dashboard"):
        return httpx.get(self.base + "/inf/v1" + path, headers=self.h(caller), timeout=30)


def detail(r) -> str:
    try:
        return r.json().get("detail")
    except ValueError:
        return ""


def token(key: bytes, conf_id: str) -> str:
    """The live run plays the creator's mailbox: the confirmation link's token (the email port is not wired)."""
    return f"{conf_id}." + hmac.new(key, f"confirm\x00{conf_id}".encode(), hashlib.sha256).hexdigest()[:32]


def application(email, handle, adult=True, **extra):
    body = {"request_id": rid(), "display_name": "Live Creator", "email": email,
            "handles": [{"platform": "instagram", "handle": handle}], "niches": ["gaming"], "follower_band": "mid",
            "country": "US", "attestation_text_version": "age-v1", "attestation_text_sha256": ATTEST_SHA, **extra}
    if adult is not None:
        body["adult_18_plus"] = adult
    return body


def _main(work: Path) -> int:
    ledger_bin = os.environ["LEDGER_BIN"]
    pl, ps = free_port(), free_port()
    L, S = f"http://127.0.0.1:{pl}", f"http://127.0.0.1:{ps}"
    lh = {"Authorization": f"Bearer {LEDGER_TOKEN}"}
    etc = work / "etc"
    etc.mkdir(mode=0o700)
    data = work / "influencer"
    pii = os.urandom(32)
    env = {"INF_SERVICE_TOKEN": TOKEN, "INF_CALLER_TOKENS": json.dumps(CALLERS), "INF_DATA_DIR": str(data),
           "INF_ANDRE_APPROVAL_TOKEN": ANDRE,
           "INF_PII_HASH_KEY_FILE": secret_file(etc / "pii.key", pii.hex().encode()),
           "INF_OUTREACH_DOMAIN": "zb-creators.example", "INF_ZBM_DOMAIN": "zbestmedia.com",
           "INF_ZBC_DOMAIN": "zbestclips.com", "INF_POSTAL_ADDRESS": "123 Example Street, Los Angeles, CA 90001",
           "INF_PORT": str(ps), "LEDGER_SERVICE_URL": L, "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN}
    for k in [k for k in os.environ if k.startswith("INF_")]:
        os.environ.pop(k, None)
    try:
        (work / "ledger").mkdir()
        start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(pl),
                             "LEDGER_LOG_PATH": str(work / "ledger" / "ledger.jsonl")}, str(work / "ledger"), "ledger",
              work)
        say(f"ledger health: {wait_health(L + '/health')}")
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "influencer", work)
        wait_health(S + "/health")
        a = Api(S)
        st = a.get("/status").json()
        check("durable, integrity verified against the real ledger, every port a stand-in",
              st["in_memory"] is False and st["integrity"]["ok"] is True and not any(st["ports_wired"].values())
              and st["andre_approvals_configured"] is True)

        env2 = {**env, "INF_PORT": str(free_port())}
        p2 = start([sys.executable, "-m", "api"], env2, str(SVC / "src"), "influencer-second", work)
        try:
            code = p2.wait(timeout=20)
        except subprocess.TimeoutExpired:
            code = None
        check("a second process on the same data directory refuses to start", code not in (None, 0))

        # --- creators and the 18+ rule ----------------------------------------------------------------------------
        r = a.post("/applications", application("creator@live-creator.example", "@live.creator"), "hub")
        app = r.json()
        check("an application changes nothing about identity until the address is confirmed (AEGIS R1-M1/M2)",
              r.status_code == 201 and app["adult_attested"] is False and app["handles"] == []
              and app["confirmation_status"] == "pending")
        r = a.post("/confirmations", {"request_id": rid(), "token": token(pii, app["confirmation_id"])[:-1] + "x"},
                   "hub")
        r2 = a.post("/confirmations", {"request_id": rid(), "token": token(pii, app["confirmation_id"])}, "hub")
        inf = a.get(f"/influencers/{app['influencer_id']}").json()
        check("a wrong token is refused; the right one attests the creator with the flag only",
              r.status_code == 404 and r2.status_code == 200 and inf["adult_attested"] is True
              and inf["email_confirmed"] is True and [h["handle"] for h in inf["handles"]] == ["live.creator"])
        r = a.post("/applications", application("Creator@live-creator.example", "@stranger"), "hub")
        inf2 = a.get(f"/influencers/{inf['influencer_id']}").json()
        check("a stranger's application with the creator's address attaches no handle",
              r.status_code == 201 and [h["handle"] for h in inf2["handles"]] == ["live.creator"])
        r = a.post("/applications", application("kid@live-creator.example", "@kid", adult=False), "hub")
        r2 = a.post("/applications", application("none@live-creator.example", "@none", adult=None), "hub")
        n_inf = len(a.get("/influencers").json())
        check("a declared minor and a missing attestation are refused and nothing is kept",
              r.status_code == 422 and detail(r) == "MINOR_REFUSED" and r2.status_code == 422
              and detail(r2) == "AGE_ATTESTATION_REQUIRED" and n_inf == 1)
        r = a.post("/applications", application("dob@live-creator.example", "@dob", date_of_birth="2001-01-01"), "hub")
        check("a date of birth is refused", r.status_code == 422 and detail(r) == "FORBIDDEN_FIELD")
        r = a.post("/tax-profiles", {"request_id": rid(), "influencer_id": inf["influencer_id"], "tax_form": "w9",
                                     "tax_ref": "stripe:acct_LIVEabcdefghijklmn", "legal_form": "individual",
                                     "country": "US", "ssn": "000-00-0000"}, "hub")
        r2 = a.post("/tax-profiles", {"request_id": rid(), "influencer_id": inf["influencer_id"], "tax_form": "w9",
                                      "tax_ref": "vault:123-45-6789", "legal_form": "individual", "country": "US"},
                    "hub")
        check("a raw TIN is refused by key and by shape (422 TAX_ID_REFUSED)",
              r.status_code == 422 and detail(r) == "TAX_ID_REFUSED" and r2.status_code == 422
              and detail(r2) == "TAX_ID_REFUSED")
        r = a.post("/discovery/import", {"request_id": rid(), "source": "paid_database"}, "influencer_agent")
        r2 = a.post("/discovery/import", {"request_id": rid(), "source": "public_profile"}, "influencer_agent")
        check("discovery through the paid database and public profiles is not wired",
              r.status_code == 503 and detail(r) == "SOURCE_NOT_WIRED" and r2.status_code == 503
              and detail(r2) == "SOURCE_NOT_WIRED")

        # --- templates, email, DMs ---------------------------------------------------------------------------------
        t = a.post("/templates", {"request_id": rid(), "brand": "zbc", "name": "intro",
                                  "subject": "A collab idea, {{first_name}}",
                                  "body": "Hi {{first_name}}, we would love to work with you."},
                   "influencer_agent").json()
        sha = t["versions"][0]["content_sha256"]
        url = f"/templates/{t['template_id']}/versions/1/approve"
        r = a.post(url, {"request_id": rid(), "content_sha256": sha}, "dashboard", "wrong-" + "w" * 40)
        check("a wrong Andre token is refused", r.status_code == 403 and detail(r) == "ANDRE_APPROVAL_INVALID")
        r = a.post(url, {"request_id": rid(), "content_sha256": sha}, "dashboard", ANDRE)
        check("Andre approves the template by its hash", r.status_code == 200)
        r = a.post("/templates", {"request_id": rid(), "brand": "zbm", "name": "bait", "subject": "Re: your payment",
                                  "body": "x"}, "influencer_agent")
        check("a deceptive subject is refused", r.status_code == 422 and detail(r) == "SUBJECT_DECEPTIVE")
        email_body = {"request_id": rid(), "influencer_id": inf["influencer_id"], "template_id": t["template_id"],
                      "version": 1}
        r = a.post("/outreach/email", email_body, "influencer_agent")
        check("an unverified first name never renders", r.status_code == 403 and detail(r) == "MERGE_FIELD_REFUSED")
        a.post(f"/influencers/{inf['influencer_id']}/first-name", {"request_id": rid(), "first_name": "Live"})
        msg = a.post("/outreach/email", {**email_body, "request_id": rid()}, "influencer_agent").json()
        job = a.post("/jobs/send-queue/run", {"request_id": rid()}, "scheduler").json()
        check("outreach email is queued and stays queued while no provider is wired",
              msg["status"] == "queued" and job["not_wired"] >= 1 and job["sent"] == 0
              and a.get("/outreach/messages?status=queued", "influencer_agent").json()[0]["status"] == "queued")
        dr = a.post("/dm-drafts", {"request_id": rid(), "influencer_id": inf["influencer_id"],
                                   "platform": "instagram", "brand": "zbc",
                                   "text": "Hi! Loved your last stream. Open to a paid collab?"},
                    "influencer_agent").json()
        r = a.post(f"/dm-drafts/{dr['draft_id']}/approve", {"request_id": rid(), "content_sha256": "0" * 64},
                   "dashboard", ANDRE)
        r2 = a.post(f"/dm-drafts/{dr['draft_id']}/approve", {"request_id": rid(),
                                                             "content_sha256": dr["content_sha256"]},
                    "influencer_agent", ANDRE)
        check("a DM is approved only by Andre and only for its exact hash",
              r.status_code == 409 and r2.status_code == 403)
        r = a.post(f"/dm-drafts/{dr['draft_id']}/approve", {"request_id": rid(),
                                                            "content_sha256": dr["content_sha256"]},
                   "dashboard", ANDRE)
        job = a.post("/jobs/send-queue/run", {"request_id": rid()}, "scheduler").json()
        check("an approved DM stays queued: there is no DM provider",
              r.status_code == 200 and job["sent"] == 0 and any(
                  m["channel"] == "dm" and m["status"] == "queued"
                  for m in a.get("/outreach/messages", "influencer_agent").json()))

        # --- replies hold, opt-outs suppress ------------------------------------------------------------------------
        rep = a.post("/replies", {"request_id": rid(), "channel": "instagram", "from_handle": "@live.creator",
                                  "text": "yes interested!"}, "provider_events").json()
        r = a.post("/outreach/email", {**email_body, "request_id": rid()}, "influencer_agent")
        msgs = [m for m in a.get("/outreach/messages?status=queued", "influencer_agent").json()
                if m.get("purpose") != "confirmation"]          # a confirmation mail is not outreach
        check("any reply (even 'yes') holds every further outreach until Andre decides",
              rep["held"] is True and r.status_code == 403 and detail(r) == "REPLY_HOLD" and not msgs)
        r = a.post(f"/holds/{rep['hold_id']}/decision", {"request_id": rid(), "decision": "continue"}, "dashboard",
                   ANDRE)
        r2 = a.post("/outreach/email", {**email_body, "request_id": rid()}, "influencer_agent")
        check("Andre's decision lifts the hold", r.status_code == 200 and r2.status_code == 201)
        other = a.post("/applications", application("other@live-creator.example", "@other.creator"), "hub").json()
        a.post("/confirmations", {"request_id": rid(), "token": token(pii, other["confirmation_id"])}, "hub")
        a.post(f"/influencers/{other['influencer_id']}/first-name", {"request_id": rid(), "first_name": "Other"})
        rep = a.post("/replies", {"request_id": rid(), "channel": "email", "from_email": "other@live-creator.example",
                                  "text": "Please unsubscribe me"}, "provider_events").json()
        zbm = a.post("/templates", {"request_id": rid(), "brand": "zbm", "name": "zbm-intro",
                                    "subject": "Working together, {{first_name}}?", "body": "Hi {{first_name}}."},
                     "influencer_agent").json()
        a.post(f"/templates/{zbm['template_id']}/versions/1/approve",
               {"request_id": rid(), "content_sha256": zbm["versions"][0]["content_sha256"]}, "dashboard", ANDRE)
        r = a.post("/outreach/email", {"request_id": rid(), "influencer_id": other["influencer_id"],
                                       "template_id": zbm["template_id"], "version": 1}, "influencer_agent")
        r2 = a.post("/dm-drafts", {"request_id": rid(), "influencer_id": other["influencer_id"],
                                   "platform": "instagram", "brand": "zbc", "text": "Hi"}, "influencer_agent")
        check("an opt-out suppresses both brands and DMs at once",
              rep["suppressed"] is True and r.status_code == 403 and detail(r) == "SUPPRESSED"
              and r2.status_code == 403 and detail(r2) == "SUPPRESSED")

        # --- campaigns, briefs, the $5,000 rule ----------------------------------------------------------------------
        c = a.post("/campaigns", {"request_id": rid(), "brand": "zbc", "name": "Live launch", "kind": "influencer"},
                   "influencer_agent").json()
        b = a.post("/briefs", {"request_id": rid(), "campaign_id": c["campaign_id"], "title": "Launch",
                               "text": "Play the new mode for ten minutes and share what you honestly think.",
                               "disclosure": "#ad"}, "influencer_agent").json()
        check("every brief carries its disclosure and the fixed FTC section",
              "16 CFR Part 255" in b["rendered"] and '"#ad"' in b["rendered"])
        a.post(f"/briefs/{b['brief_id']}/approve", {"request_id": rid(), "content_sha256": b["content_sha256"]},
               "dashboard", ANDRE)
        deal_body = {"influencer_id": inf["influencer_id"], "campaign_id": c["campaign_id"], "brief_id": b["brief_id"],
                     "deliverables": [{"platform": "instagram", "kind": "reel", "quantity": 1}]}
        r = a.post("/deals", {"request_id": rid(), **deal_body, "fee": 5000.0}, "influencer_agent")
        check("a float fee is refused", r.status_code == 422)
        d1 = a.post("/deals", {"request_id": rid(), **deal_body, "fee": "5000.00"}, "influencer_agent").json()
        d2 = a.post("/deals", {"request_id": rid(), **deal_body, "fee": "1.00"}, "influencer_agent").json()
        check("$5,000.00 is approved here; a $1.00 more for the same person, ever, goes to Andre",
              d1["status"] == "approved" and d2["status"] == "pending_andre"
              and "INFLUENCER_TOTAL_OVER_LIMIT" in d2["needs_andre"])
        r = a.post(f"/deals/{d2['deal_id']}/approve", {"request_id": rid(), "content_sha256": d2["content_sha256"]},
                   "dashboard", ANDRE)
        mc = a.get("/material-connections", "compliance_38").json()
        check("Andre approves the deal by hash; material connections are recorded",
              r.status_code == 200 and r.json()["approved_by"] == "andre" and len(mc) == 2
              and {x["disclosure"] for x in mc} == {"#ad"})

        # --- content, contracts, payouts ----------------------------------------------------------------------------
        media = [hashlib.sha256(b"live-media").hexdigest()]
        content_body = {"deal_id": d1["deal_id"], "platform": "instagram", "media_sha256": media,
                        "platform_label_on": True}
        r = a.post("/contents", {"request_id": rid(), **content_body, "caption": "Best mode ever #gaming"},
                   "influencer_agent")
        r2 = a.post("/contents", {"request_id": rid(), **content_body, "caption": "Best mode #gaming #fun #ad"},
                    "influencer_agent")
        check("content without an up-front disclosure is refused (FTC, fail closed)",
              detail(r) == "DISCLOSURE_MISSING" and detail(r2) == "DISCLOSURE_NOT_PROMINENT")
        content = a.post("/contents", {"request_id": rid(), **content_body, "caption": "#ad Best mode ever #gaming"},
                         "influencer_agent").json()
        r = a.post(f"/contents/{content['content_id']}/approve", {"request_id": rid(),
                                                                  "content_sha256": content["content_sha256"]},
                   "dashboard", ANDRE)
        r2 = a.post(f"/contents/{content['content_id']}/live", {"request_id": rid(),
                                                                "content_sha256": content["content_sha256"],
                                                                "post_ref": "ig-post-1"}, "influencer_agent")
        check("Andre approves the final content by hash; it cannot count live without a contract",
              r.status_code == 200 and r2.status_code == 409 and detail(r2) == "CONTRACT_NOT_IN_FORCE")
        r = a.post(f"/deals/{d1['deal_id']}/contract", {"request_id": rid()}, "influencer_agent")
        check("sending a contract is refused while Legal is a stand-in",
              r.status_code == 503 and detail(r) == "LEGAL_UNAVAILABLE")
        r = a.post("/tax-profiles", {"request_id": rid(), "influencer_id": inf["influencer_id"], "tax_form": "w9",
                                     "tax_ref": "stripe:acct_LIVEabcdefghijklmn", "legal_form": "individual",
                                     "country": "US"}, "hub")
        before = a.get(f"/influencers/{inf['influencer_id']}").json()["tax_profile"]
        r1 = a.post("/confirmations", {"request_id": rid(), "token": token(pii, r.json()["conf_id"])}, "hub")
        after = a.get(f"/influencers/{inf['influencer_id']}").json()["tax_profile"]
        check("a tax REFERENCE takes effect only once confirmed from the address on record",
              r.status_code == 201 and before is None and r1.status_code == 200 and after["tax_form"] == "w9")
        r2 = a.post(f"/payees/{inf['influencer_id']}/verify", {"request_id": rid()}, "influencer_agent")
        r3 = a.post("/payouts", {"request_id": rid(), "deal_id": d1["deal_id"], "amount": "100.00",
                                 "content_ids": [content["content_id"]]}, "influencer_agent")
        check("verification and payouts refused while Finance is a stand-in",
              r2.status_code == 503 and detail(r2) == "FINANCE_UNAVAILABLE"
              and r3.status_code == 409 and not a.get("/payouts").json())
        r = a.post("/replies", {"request_id": rid(), "channel": "email", "message_id": "unknown-message",
                                "from_email": "Other <other@live-creator.example>", "text": "stop"},
                   "provider_events")
        r2 = a.post("/replies", {"request_id": rid(), "channel": "email", "from_handle": "@nobody",
                                 "text": "stop"}, "provider_events")
        r3 = a.post("/replies", {"channel": "email", "message_id": "<123456789@mail.example>",
                                 "from_email": "other@live-creator.example", "text": "STOP\n" + "> quoted\n" * 3000,
                                 "provider_extra": True}, "provider_events")
        reps = [a.post("/applications", application("flood@live-creator.example", "@flood"), "hub").json()
                for _ in range(3)]
        more = [a.post("/applications", application("flood@live-creator.example", "@flood"), "hub") for _ in range(3)]
        check("AEGIS R2: a long, oddly shaped reply lands; one open confirmation per address; applications rate-limited",
              r3.status_code == 201 and len({x["confirmation_id"] for x in reps}) == 1
              and [x.status_code for x in more] == [201, 201, 429] and detail(more[2]) == "APPLICATION_RATE_LIMITED")
        other_app = a.post("/applications", application("flood2@live-creator.example", "@flood2"), "hub").json()
        stranger = a.post("/applications", application("flood2@live-creator.example", "@stranger2"), "hub")
        r5 = a.post("/confirmations", {"request_id": rid(), "token": token(pii, other_app["confirmation_id"])}, "hub")
        flood2 = a.get(f"/influencers/{other_app['influencer_id']}").json()
        r6 = a.post("/jobs/hold-expiry/run", {"request_id": rid()}, "scheduler")
        check("AEGIS R3: a stranger's application never invalidates the creator's open link; hold expiry runs",
              stranger.status_code == 201 and r5.status_code == 200
              and [h["handle"] for h in flood2["handles"]] == ["flood2"] and r6.status_code == 200)
        check("a reply is never refused for its sender fields; one that resolves nothing is kept for Andre",
              r.status_code == 201 and r.json()["suppressed"] is True and r2.status_code == 201
              and r2.json()["held"] is True and r2.json()["influencer_id"] is None)

        # --- restart ------------------------------------------------------------------------------------------------
        stop(sp, "influencer")
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "influencer", work)
        wait_health(S + "/health")
        st = a.get("/status").json()
        r = a.post("/outreach/email", {"request_id": rid(), "influencer_id": other["influencer_id"],
                                       "template_id": zbm["template_id"], "version": 1}, "influencer_agent")
        check("after a restart the log re-verifies and the suppression, deals and approvals survive",
              st["integrity"]["ok"] is True and detail(r) == "SUPPRESSED"
              and a.get(f"/deals/{d2['deal_id']}").json()["status"] == "approved")

        # --- a forged pending line ------------------------------------------------------------------------------
        stop(sp, "influencer")
        lines = [ln for ln in (data / "influencer_log.jsonl").read_bytes().split(b"\n") if ln]
        rl = store_mod.RecordLog(None)
        rl._lines = lines
        _, fl = rl.prepare("hold_decided", json.loads(lines[-1])["at"],
                           {"hold_id": rep["hold_id"], "decision": "continue", "hashes": [], "actor": "andre"})
        (data / "pending.line").write_bytes(fl)
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "influencer", work)
        wait_health(S + "/health")
        st = a.get("/status").json()
        holds = {x["hold_id"]: x["status"] for x in a.get("/holds", "influencer_agent").json()}
        check("a forged pending line is set aside, never anchored or applied",
              st["integrity"]["ok"] is True and (data / "pending.discarded").exists()
              and holds[rep["hold_id"]] == "active")
        r = a.post("/jobs/integrity/run", {"request_id": rid()}, "scheduler")
        check("the integrity job reads the ledger's own verdict", r.status_code == 200
              and r.json()["ledger_valid"] is True and r.json()["integrity"]["ok"] is True)

        # --- a truncated log ------------------------------------------------------------------------------------
        stop(sp, "influencer")
        path = data / "influencer_log.jsonl"
        kept = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(kept[:-2]))
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "influencer", work)
        hs = wait_health(S + "/health")
        r = a.post("/suppressions", {"request_id": rid(), "email": "z@z.example"}, "hub")
        check("a truncated log is detected and nothing is written",
              hs["status"] == "degraded" and r.status_code == 503 and detail(r) == "INTEGRITY_UNVERIFIED")
        stop(sp, "influencer")

        # --- the ledger ---------------------------------------------------------------------------------------------
        ents = httpx.get(L + "/ledger/entries", headers=lh, timeout=60).json()
        mine = [x for x in ents if x.get("department") == "influencer"]
        types = sorted({x["event_type"] for x in mine})
        say(f"ledger: {len(ents)} entries, {len(mine)} from influencer; types: {', '.join(types)}")
        check("typed events are on the ledger", {
            "log_anchor", "influencer_recorded", "age_attestation_recorded", "template_approved", "dm_approved",
            "outreach_hold_applied", "hold_decided", "suppression_added", "brief_approved", "deal_recorded",
            "deal_approved", "material_connection_recorded", "content_approved", "tax_profile_recorded",
            "founder_approval_refused", "first_name_verified", "confirmation_requested",
            "email_confirmed"} <= set(types))
        blob = json.dumps(ents)
        check("nothing personal on the ledger (no email, handle, name or tax reference)",
              "live-creator.example" not in blob and "live.creator" not in blob and "Live Creator" not in blob
              and "acct_LIVE" not in blob)
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
    work = Path(tempfile.mkdtemp(prefix="influencer-live-", dir=keep_in))
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
