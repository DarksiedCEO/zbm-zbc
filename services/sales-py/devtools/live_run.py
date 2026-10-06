#!/usr/bin/env python3
"""
Lead Generation (26) + Sales (27) live run: the REAL ledger-rust binary and sales-py's production entrypoint
(``cd src && python3 -m api``) over real HTTP, with a durable data directory and a PII hash key file. Modeled on
services/security-py/devtools/live_run.py.

usage: LEDGER_BIN=/path/to/ledger-rust/target/release/server python3 devtools/live_run.py

Proves: start-up integrity against the real ledger; inbound, referral and duplicate leads; imports refused while no
source is wired; Andre's template approval (and a wrong token refused); cold email queued but not sent while no
provider is wired; SMS refused without consent and in quiet hours, accepted with consent in the window; an opt-out
reply suppressing across both brands; no quote without an approved price; auto-approval at or under $10,000 and
Andre's approval above it; sending refused while Legal is a stand-in; a restart that keeps everything; a forged
pending line left inert; a second process on the same data directory refused; a truncated log detected; typed events
on the ledger and no raw email on it; GET /ledger/verify valid. Exit 0 only if every check holds. Kills only the
PIDs it started.
"""

from __future__ import annotations

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
sys.path[:0] = [str(SVC / "src")]

import store as store_mod  # noqa: E402
from intelligences import i07_quiet_hours as quiet  # noqa: E402

LEDGER_TOKEN = "live-ledger-token-" + "l" * 24
TOKEN = "live-sales-service-token-" + "s" * 20
ANDRE = "live-andre-approval-token-" + "a" * 20
CALLERS = {c: f"live-sales-caller-{c}-" + "c" * 24 for c in ("hub", "detection", "dashboard", "scheduler",
                                                             "sales_agent", "provider_events", "compliance_38")}
# NANP zones with an area code the service knows (S3-L1: texts and calls only to +1 numbers in its table)
AREA_FOR = {"Pacific/Pago_Pago": "684", "Pacific/Honolulu": "808", "America/Anchorage": "907",
            "America/Los_Angeles": "310", "America/Denver": "303", "America/Chicago": "312", "America/New_York": "212",
            "America/Halifax": "902", "America/St_Johns": "709", "Pacific/Guam": "671"}
ZONES = tuple(AREA_FOR)
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
    end = time.time() + timeout
    while time.time() < end:
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


# A zone is only picked if the service's own rule gives the same answer for this long (the SMS checks run within
# seconds of the pick).
ZONE_MARGIN = timedelta(minutes=3)
# A valid zone for contacts whose phone the window checks do not depend on.
NEUTRAL_ZONE = "America/New_York"
# Window checks that the real clock cannot exercise at this moment (reported, never counted as passed).
NOT_EXERCISABLE: list[str] = []


def _zone_holds(z: str, inside: bool, at: datetime) -> bool:
    """The service's own quiet-hours rule (i07.allowed, with the number this run will use), at ``at`` and for the
    whole margin after it. Never a copy of the window: the run tests what the service decides."""
    phone = phone_for(z, "0000")
    return all(quiet.allowed(at + timedelta(minutes=m), z, phone) is inside
               for m in range(0, int(ZONE_MARGIN.total_seconds() // 60) + 1))


def _pick(at: datetime) -> tuple[str | None, str | None]:
    day = next((z for z in ZONES if _zone_holds(z, True, at)), None)
    night = next((z for z in ZONES if _zone_holds(z, False, at)), None)
    return day, night


def zones_for_now() -> tuple[str | None, str | None]:
    """A zone inside 08:00-21:00 and one outside it, each stable for ZONE_MARGIN, or None for a side in a real gap.

    NANP has daily gaps (checked minute by minute against i07.allowed and AREA_ZONES): no zone inside about
    11:00-12:00 UTC on standard time (area code 709 also covers Goose Bay, Atlantic), and every zone inside about
    22:00-23:30 UTC (to 00:30 on standard time); the 3-minute margin opens each gap 3 minutes early. The run uses the
    real clock, so in a gap the matching check is reported as not exercisable; pytest covers the window on a fixed
    clock. A skip must never hide a bug (AEGIS F1), so the picker fails the run unless: every zone gets a definite
    answer from the service, the two gaps are not both open (they never overlap), and a side missing now is found
    within 3 hours either way (real gaps last at most about 2h33m)."""
    now = datetime.now(timezone.utc)
    unknown = [z for z in ZONES if quiet.allowed(now, z, phone_for(z, "0000")) is None]
    if unknown:
        raise RuntimeError(f"the service gives no quiet-hours answer for {unknown}: a bug, not a gap")
    day, night = _pick(now)
    if day is None and night is None:
        raise RuntimeError("neither a daytime nor a night zone: impossible on a real clock, so a bug")
    for side, found in (("daytime", day), ("night", night)):
        if found is None:
            near = [now + timedelta(minutes=m) for m in range(-180, 181, 5)]
            if not any(_pick(t)[0 if side == "daytime" else 1] for t in near):
                raise RuntimeError(f"no {side} zone within 3 hours of {now:%H:%M}Z: a bug, not a gap")
    return day, night


def not_exercisable(name: str, why: str) -> None:
    NOT_EXERCISABLE.append(name)
    say(f"  NOT EXERCISABLE NOW: {name} ({why}; covered by pytest on a fixed clock)")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=sales-py live run::not exercisable at this hour: {name} ({why})", flush=True)


class Api:
    def __init__(self, base: str):
        self.base = base

    def h(self, caller, andre=None):
        h = {"Authorization": f"Bearer {TOKEN}", "X-SALES-Caller-Token": CALLERS[caller]}
        if andre:
            h["X-Andre-Approval-Token"] = andre
        return h

    def post(self, path, body, caller="dashboard", andre=None):
        return httpx.post(self.base + "/sales/v1" + path, json=body, headers=self.h(caller, andre), timeout=30)

    def get(self, path, caller="dashboard"):
        return httpx.get(self.base + "/sales/v1" + path, headers=self.h(caller), timeout=30)


def verify(a: "Api", lead: dict) -> None:
    """A person at the console verifies the names merge fields render (AEGIS S2-H1)."""
    a.post(f"/accounts/{lead['account_id']}/display-name", {"request_id": rid(), "display_name": "Live Shop"})
    a.post(f"/contacts/{lead['contact_id']}/first-name", {"request_id": rid(), "first_name": "Live"})


def rid() -> str:
    return "live-" + uuid.uuid4().hex


def phone_for(zone: str, local: str) -> str:
    """A +1 number whose area code is in the zone (the area code's zone is checked too, S2-L1 / S3-L1)."""
    return f"+1{AREA_FOR[zone]}555{local}"


def lead_body(email, phone, tz, kind="site_form", source="inbound", interest=("revenue_recovery",), **extra):
    contact = {"name": "Live Person", "email": email, "time_zone": tz}
    if phone:
        contact["phone"] = phone
    return {"request_id": rid(), "source": source, "product_interest": list(interest), "contact": contact,
            "account": {"name": "Live Shop", "domain": email.split("@")[1], "industry": "ecommerce",
                        "employees_band": "11-50"},
            "evidence": {"kind": kind, "ref": "ev-" + uuid.uuid4().hex[:10],
                         "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()},
            "signals": {"requested_call": True, "timeline": "now"}, **extra}


def _main(work: Path) -> int:
    ledger_bin = os.environ["LEDGER_BIN"]
    pl, ps = free_port(), free_port()
    L, S = f"http://127.0.0.1:{pl}", f"http://127.0.0.1:{ps}"
    lh = {"Authorization": f"Bearer {LEDGER_TOKEN}"}
    etc = work / "etc"
    etc.mkdir(mode=0o700)
    data = work / "sales"
    env = {"SALES_SERVICE_TOKEN": TOKEN, "SALES_CALLER_TOKENS": json.dumps(CALLERS),
           "SALES_DATA_DIR": str(data), "SALES_ANDRE_APPROVAL_TOKEN": ANDRE,
           "SALES_PII_HASH_KEY_FILE": secret_file(etc / "pii.key", os.urandom(32).hex().encode()),
           "SALES_OUTREACH_DOMAIN": "zbm-outreach.example", "SALES_ZBM_DOMAIN": "zbestmedia.com",
           "SALES_ZBC_DOMAIN": "zbestclips.com",
           "SALES_POSTAL_ADDRESS": "123 Example Street, Los Angeles, CA 90001", "SALES_PORT": str(ps),
           "LEDGER_SERVICE_URL": L, "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN}
    for k in [k for k in os.environ if k.startswith("SALES_")]:
        os.environ.pop(k, None)
    try:
        (work / "ledger").mkdir()
        start([ledger_bin], {"LEDGER_SERVICE_TOKEN": LEDGER_TOKEN, "LEDGER_PORT": str(pl),
                             "LEDGER_LOG_PATH": str(work / "ledger" / "ledger.jsonl")}, str(work / "ledger"), "ledger",
              work)
        say(f"ledger health: {wait_health(L + '/health')}")
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "sales", work)
        wait_health(S + "/health")
        a = Api(S)
        st = a.get("/status").json()
        check("durable, integrity verified against the real ledger, every port a stand-in",
              st["in_memory"] is False and st["integrity"]["ok"] is True and not any(st["ports_wired"].values()))

        # --- a second process on the same data directory --------------------------------------------------------
        env2 = {**env, "SALES_PORT": str(free_port())}
        p2 = start([sys.executable, "-m", "api"], env2, str(SVC / "src"), "sales-second", work)
        try:
            code = p2.wait(timeout=20)
        except subprocess.TimeoutExpired:
            code = None
        check("a second process on the same data directory refuses to start", code not in (None, 0))

        # --- leads ------------------------------------------------------------------------------------------------
        day_zone, night_zone = zones_for_now()
        say(f"recipient zones: daytime {day_zone}, night {night_zone}")
        lead_zone = day_zone or NEUTRAL_ZONE
        night_lead_zone = night_zone or NEUTRAL_ZONE
        r = a.post("/leads", lead_body("buyer@live-shop.example", phone_for(lead_zone, "0123"), lead_zone), "hub")
        lead = r.json()
        check("an inbound site-form lead is scored and routed", r.status_code == 201 and lead["brand"] == "zbm"
              and lead["status"] == "qualified")
        r = a.post("/leads", lead_body("Buyer+x@live-shop.example", None, lead_zone, kind="rr_scan"), "detection")
        check("a scan for the same person is merged into the open lead",
              r.status_code == 201 and r.json().get("duplicate") is True and r.json()["lead_id"] == lead["lead_id"])
        r = a.post("/leads", lead_body("ref@other.example", None, lead_zone, source="referral", kind="referral_note",
                                       referrer={"name": "Partner P", "ref": "p-1"}), "dashboard")
        check("a referral keeps its referrer", r.status_code == 201 and r.json()["referrer"]["ref"] == "p-1")
        r = a.post("/leads/import", {"request_id": rid(), "source": "paid_provider"}, "sales_agent")
        check("paid-provider import refused while not wired",
              r.status_code == 503 and r.json()["detail"] == "SOURCE_NOT_WIRED")
        r = a.post("/leads", {**lead_body("x@y.example", None, lead_zone), "contact": {"name": "X", "email":
                                                                                       "x@y.example",
                                                                                       "dob": "1990-01-01"}}, "hub")
        check("a date of birth is refused", r.status_code == 422)
        opp = a.post(f"/leads/{lead['lead_id']}/convert", {"request_id": rid()}, "sales_agent").json()

        # --- templates and email --------------------------------------------------------------------------------
        t = a.post("/templates", {"request_id": rid(), "brand": "zbm", "channel": "email", "name": "intro",
                                  "subject": "An idea for {{company}}", "body": "Hi {{first_name}}, a quick idea."},
                   "sales_agent").json()
        sha = t["versions"][0]["content_sha256"]
        r = a.post(f"/templates/{t['template_id']}/versions/1/approve", {"request_id": rid(), "content_sha256": sha},
                   "dashboard", "wrong-" + "w" * 30)
        check("a wrong Andre token is refused", r.status_code == 403)
        r = a.post(f"/templates/{t['template_id']}/versions/1/approve", {"request_id": rid(), "content_sha256": sha},
                   "dashboard", ANDRE)
        check("Andre approves the template by its hash", r.status_code == 200)
        r = a.post("/templates", {"request_id": rid(), "brand": "zbm", "channel": "email", "name": "bait",
                                  "subject": "Re: your invoice", "body": "x"}, "sales_agent")
        check("a deceptive subject is refused", r.status_code == 422 and r.json()["detail"] == "SUBJECT_DECEPTIVE")
        r = a.post("/outreach/email", {"request_id": rid(), "contact_id": lead["contact_id"],
                                       "template_id": t["template_id"], "version": 1}, "sales_agent")
        check("a name typed into a form never renders: unverified merge fields refused (S2-H1)",
              r.status_code == 403 and r.json()["detail"] == "MERGE_FIELD_REFUSED")
        verify(a, lead)
        msg = a.post("/outreach/email", {"request_id": rid(), "contact_id": lead["contact_id"],
                                         "template_id": t["template_id"], "version": 1}, "sales_agent").json()
        r = a.post("/jobs/send-queue/run", {"request_id": rid()}, "scheduler").json()
        check("cold email is queued and stays queued while no provider is wired",
              msg["status"] == "queued" and r["not_wired"] == 1 and r["sent"] == 0)

        # --- sms ---------------------------------------------------------------------------------------------------
        st_tpl = a.post("/templates", {"request_id": rid(), "brand": "zbm", "channel": "sms", "name": "sms1",
                                       "body": "Hi {{first_name}}, following up."}, "sales_agent").json()
        a.post(f"/templates/{st_tpl['template_id']}/versions/1/approve",
               {"request_id": rid(), "content_sha256": st_tpl["versions"][0]["content_sha256"]}, "dashboard", ANDRE)
        sms = {"request_id": rid(), "contact_id": lead["contact_id"], "template_id": st_tpl["template_id"],
               "version": 1}
        r = a.post("/outreach/sms", sms, "sales_agent")
        check("a cold text without consent is refused", r.status_code == 403 and r.json()["detail"] ==
              "CONSENT_REQUIRED")
        r = a.post("/consents", {"request_id": rid(), "contact_id": lead["contact_id"], "channel": "sms",
                                 "brand": "zbm", "source": "web_form",
                                 "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                                 "consent_text_version": "sms-v1", "consent_text_sha256": "c" * 64}, "hub")
        check("express consent recorded", r.status_code == 201)
        if day_zone:
            r = a.post("/outreach/sms", {**sms, "request_id": rid()}, "sales_agent")
            check("with consent, inside the recipient's window, the text is queued", r.status_code == 201)
            if r.status_code != 201:
                say(f"    answer: {r.status_code} {r.text[:120]}")
        else:
            not_exercisable("with consent, inside the recipient's window, the text is queued",
                            "no NANP zone is inside 08:00-21:00 right now")
        night = a.post("/leads", lead_body("night@night-shop.example", phone_for(night_lead_zone, "0199"),
                                           night_lead_zone), "hub").json()
        verify(a, night)
        a.post("/consents", {"request_id": rid(), "contact_id": night["contact_id"], "channel": "sms", "brand": "zbm",
                             "source": "web_form",
                             "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                             "consent_text_version": "sms-v1", "consent_text_sha256": "c" * 64}, "hub")
        if night_zone:
            r = a.post("/outreach/sms", {**sms, "request_id": rid(), "contact_id": night["contact_id"]},
                       "sales_agent")
            check("a text at night in the recipient's zone is refused",
                  r.status_code == 403 and r.json()["detail"] == "QUIET_HOURS")
        else:
            not_exercisable("a text at night in the recipient's zone is refused",
                            "every NANP zone is inside 08:00-21:00 right now")

        r = a.post("/replies", {"request_id": rid(), "channel": "sms", "from_phone": phone_for(night_lead_zone, "0199"),
                                "text": "Opt me out"}, "provider_events").json()
        r2 = a.post("/outreach/sms", {**sms, "request_id": rid(), "contact_id": night["contact_id"]}, "sales_agent")
        check("any SMS reply holds the number until Andre releases it (S2-C1, S3-C1)",
              r["held"] is True and r2.status_code == 403 and r2.json()["detail"] == "PHONE_HOLD")

        # --- opt-out across brands ----------------------------------------------------------------------------------
        r = a.post("/replies", {"request_id": rid(), "channel": "email", "message_id": msg["message_id"],
                                "text": "Please unsubscribe me"}, "provider_events").json()
        zbc = a.post("/templates", {"request_id": rid(), "brand": "zbc", "channel": "email", "name": "clips",
                                    "subject": "Clips for {{company}}", "body": "Hi {{first_name}}."},
                     "sales_agent").json()
        a.post(f"/templates/{zbc['template_id']}/versions/1/approve",
               {"request_id": rid(), "content_sha256": zbc["versions"][0]["content_sha256"]}, "dashboard", ANDRE)
        r2 = a.post("/outreach/email", {"request_id": rid(), "contact_id": lead["contact_id"],
                                        "template_id": zbc["template_id"], "version": 1}, "sales_agent")
        check("an unsubscribe reply suppresses at once, for the sister brand too",
              r["suppressed"] is True and r2.status_code == 403 and r2.json()["detail"] == "SUPPRESSED")

        # --- prices and proposals ----------------------------------------------------------------------------------
        line = "zbm.revenue_recovery_engagement"
        r = a.post("/proposals", {"request_id": rid(), "opportunity_id": opp["opportunity_id"],
                                  "lines": [{"line_id": line}]}, "sales_agent")
        check("no approved price, no quote", r.status_code == 409 and r.json()["detail"] == "PRICE_NOT_APPROVED")
        r = a.post(f"/pricebook/zbm/lines/{line}/approve", {"request_id": rid(), "version": 1, "price": 2500.0},
                   "dashboard", ANDRE)
        check("a float price is refused", r.status_code == 422)
        r = a.post(f"/pricebook/zbm/lines/{line}/approve", {"request_id": rid(), "version": 1, "price": "2500.00"},
                   "dashboard", ANDRE)
        check("Andre approves a price", r.status_code == 200)
        small = a.post("/proposals", {"request_id": rid(), "opportunity_id": opp["opportunity_id"],
                                      "lines": [{"line_id": line, "quantity": 4}]}, "sales_agent").json()
        check("a $10,000.00 list-price proposal is auto-approved", small["status"] == "approved"
              and small["total"] == "10000.00" and small["payment_methods"] == ["ach"])
        big = a.post("/proposals", {"request_id": rid(), "opportunity_id": opp["opportunity_id"],
                                    "lines": [{"line_id": line, "quantity": 5}]}, "sales_agent").json()
        check("over $10,000 waits for Andre", big["status"] == "pending_andre")
        third = a.post("/proposals", {"request_id": rid(), "opportunity_id": opp["opportunity_id"],
                                      "lines": [{"line_id": line, "quantity": 1}]}, "sales_agent").json()
        check("a deal split into pieces under $10,000 still waits for Andre (S1-H2)",
              third["status"] == "pending_andre" and third["needs_andre"] == ["OPPORTUNITY_TOTAL_OVER_MAX"])
        r = a.post(f"/contacts/{lead['contact_id']}/time-zone", {"request_id": rid(), "time_zone": "America/Denver"},
                   "sales_agent")
        check("the agent cannot change a recipient's time zone (S1-M1)",
              r.status_code == 403 and r.json()["detail"] == "CALLER_NOT_ALLOWED")
        r = a.post(f"/proposals/{big['proposal_id']}/send", {"request_id": rid(), "contract_kind": "client_msa",
                                                            "contract_ref": "msa-1"}, "sales_agent")
        check("a pending proposal cannot be sent", r.status_code == 409)
        r = a.post(f"/proposals/{big['proposal_id']}/approve", {"request_id": rid(),
                                                               "content_sha256": big["content_sha256"]},
                   "dashboard", ANDRE)
        check("Andre approves the exact proposal", r.status_code == 200 and r.json()["approved_by"] == "andre")
        r = a.post(f"/proposals/{small['proposal_id']}/send", {"request_id": rid(), "contract_kind": "client_msa",
                                                              "contract_ref": "msa-1"}, "sales_agent")
        check("sending refused while Legal is a stand-in", r.status_code == 503 and
              r.json()["detail"] == "LEGAL_UNAVAILABLE")

        # --- restart ---------------------------------------------------------------------------------------------
        stop(sp, "sales")
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "sales", work)
        wait_health(S + "/health")
        st = a.get("/status").json()
        check("after a restart the log re-verifies against the ledger", st["integrity"]["ok"] is True)
        r2 = a.post("/outreach/email", {"request_id": rid(), "contact_id": lead["contact_id"],
                                        "template_id": t["template_id"], "version": 1}, "sales_agent")
        check("the suppression survives the restart", r2.status_code == 403 and r2.json()["detail"] == "SUPPRESSED")
        check("proposals and prices survive the restart",
              a.get(f"/proposals/{big['proposal_id']}", "sales_agent").json()["status"] == "approved")

        # --- a forged pending line ------------------------------------------------------------------------------
        stop(sp, "sales")
        lines = [ln for ln in (data / "sales_log.jsonl").read_bytes().split(b"\n") if ln]
        rl = store_mod.RecordLog(None)
        rl._lines = lines
        forged_key = "phone:" + "9" * 64 + "|sms|zbc"
        _, fl = rl.prepare("consent_granted", json.loads(lines[-1])["at"],
                           {"key": forged_key, "contact_id": night["contact_id"], "channel": "sms", "brand": "zbc",
                            "source": "web_form", "captured_at": "2026-10-01T00:00:00Z",
                            "consent_text_version": "x", "consent_text_sha256": "d" * 64, "actor": "hub"})
        (data / "pending.line").write_bytes(fl)
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "sales", work)
        wait_health(S + "/health")
        st = a.get("/status").json()
        check("a forged pending line is set aside, never anchored or applied",
              st["integrity"]["ok"] is True and (data / "pending.discarded").exists())

        # --- a truncated log --------------------------------------------------------------------------------------
        stop(sp, "sales")
        path = data / "sales_log.jsonl"
        kept = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(kept[:-2]))
        sp = start([sys.executable, "-m", "api"], env, str(SVC / "src"), "sales", work)
        hs = wait_health(S + "/health")
        r = a.post("/suppressions", {"request_id": rid(), "email": "z@z.example", "reason": "manual"}, "hub")
        check("a truncated log is detected and nothing is written",
              hs["status"] == "degraded" and r.status_code == 503)
        stop(sp, "sales")

        # --- the ledger ---------------------------------------------------------------------------------------------
        ents = httpx.get(L + "/ledger/entries", headers=lh, timeout=60).json()
        mine = [x for x in ents if x.get("department") == "sales"]
        types = sorted({x["event_type"] for x in mine})
        say(f"ledger: {len(ents)} entries, {len(mine)} from sales; types: {', '.join(types)}")
        check("typed events are on the ledger", {"log_anchor", "consent_granted", "suppression_added",
                                                  "phone_hold_applied", "account_display_name_verified",
                                                  "template_approved", "price_approved", "proposal_approved",
                                                  "founder_approval_refused"} <= set(types))
        check("no raw email on the ledger", "live-shop.example" not in json.dumps(ents)
              and "buyer@" not in json.dumps(ents))
        v = httpx.get(L + "/ledger/verify", headers=lh, timeout=120)
        check("ledger verifies valid", v.status_code == 200 and v.json().get("valid") is True)
        failed = [n for n, ok in CHECKS if not ok]
        say(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed" + (f"; FAILED: {failed}" if failed else "")
            + (f"; not exercisable at this hour: {NOT_EXERCISABLE}" if NOT_EXERCISABLE else ""))
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
    work = Path(tempfile.mkdtemp(prefix="sales-live-", dir=keep_in))
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
