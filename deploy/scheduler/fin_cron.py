"""
fin-cron: the Railway cron service for Finance (ADR 0026; red team OPS-H1). Runs once per Railway cron tick (hourly),
calls Finance's housekeeping routes over the private network with its OWN caller token (``ops_cron``), prints one
line per call and exits. Standard library only; no state; no secret is ever printed.

Every tick:
- ``POST /fin/v1/jobs/stripe-sessions/run``: closes Stripe pages that must not take money and re-asks Compliance for
  payments held ``unverified`` (the hourly hold re-check the runbook promises);
- ``POST /fin/v1/jobs/client-mail/run``: sends due client receipts, credit-note and refund confirmations and
  acknowledgements (retries with backoff live in Finance).
Once a day, on the tick whose local hour (FIN_CRON_TZ) is FIN_CRON_RECON_HOUR:
- ``POST /fin/v1/reconciliations/run``: the daily reconciliation (FC-01..).

Compliance's own controls run inside Compliance (its in-process scheduler); nothing here calls Compliance.

The same image runs a second cron service, ``ops-alerts`` (ADR 0027), with ``FIN_CRON_JOBS=alerts``: every 5 minutes it
calls only ``POST /fin/v1/jobs/founder-alerts/run`` (Finance's caller ``ops_alerts``), which emails Andre what needs
him, deduplicated and rate-limited inside Finance.

A 503 (Finance quiesced for a backup, Stripe busy) is retried twice with a short wait; anything else that is not 2xx is
a failed call. The process exits 1 if any call failed, so Railway marks the cron run failed.

Environment (all set in .railway/railway.ts):
  FIN_CRON_FINANCE_URL     http://finance.railway.internal:8410 (private; anything but a .railway.internal http URL or
                           an https URL is refused)
  FIN_CRON_SERVICE_TOKEN   Finance's service bearer (a Railway reference to finance.FIN_SERVICE_TOKEN)
  FIN_CRON_CALLER_TOKEN    this service's caller token (Finance knows it as ``ops_cron``)
  FIN_CRON_RECON_HOUR      0-23, default 7
  FIN_CRON_TZ              IANA zone, default America/Los_Angeles
  FIN_CRON_JOBS            housekeeping (default: the hourly jobs and the daily reconciliation) or alerts (only the
                           founder alerts; the ops-alerts service)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

HOURLY = ("/fin/v1/jobs/stripe-sessions/run", "/fin/v1/jobs/client-mail/run")
DAILY = ("/fin/v1/reconciliations/run",)
ALERTS = ("/fin/v1/jobs/founder-alerts/run",)
MODES = ("housekeeping", "alerts")
TIMEOUT_S = 120
RETRY_WAIT_S = (10, 30)
_URL = re.compile(r"(http://[a-z0-9-]{1,63}\.railway\.internal|https://[A-Za-z0-9.-]{1,253})(:[0-9]{1,5})?")
_TOKEN = re.compile(r"[\x21-\x7e]{32,512}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):           # a redirect would carry the tokens elsewhere
        return None


def settings(env) -> dict:
    url = (env.get("FIN_CRON_FINANCE_URL") or "").strip().rstrip("/")
    if not _URL.fullmatch(url):
        raise SystemExit("fin-cron: FIN_CRON_FINANCE_URL must be http://<service>.railway.internal:<port> or https://")
    bearer = (env.get("FIN_CRON_SERVICE_TOKEN") or "").strip()
    caller = (env.get("FIN_CRON_CALLER_TOKEN") or "").strip()
    for name, v in (("FIN_CRON_SERVICE_TOKEN", bearer), ("FIN_CRON_CALLER_TOKEN", caller)):
        if not _TOKEN.fullmatch(v) or "${{" in v or "{{" in v:
            raise SystemExit(f"fin-cron: {name} is missing, too short or an unresolved reference")
    if bearer == caller:
        raise SystemExit("fin-cron: the service token and the caller token must differ")
    raw_hour = (env.get("FIN_CRON_RECON_HOUR") or "7").strip()
    if not raw_hour.isdigit() or not 0 <= int(raw_hour) <= 23:
        raise SystemExit("fin-cron: FIN_CRON_RECON_HOUR must be 0-23")
    tz = (env.get("FIN_CRON_TZ") or "America/Los_Angeles").strip()
    try:
        ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        raise SystemExit("fin-cron: FIN_CRON_TZ is not a time zone") from None
    mode = (env.get("FIN_CRON_JOBS") or "housekeeping").strip()
    if mode not in MODES:
        raise SystemExit("fin-cron: FIN_CRON_JOBS must be housekeeping or alerts")
    return {"url": url, "bearer": bearer, "caller": caller, "hour": int(raw_hour), "tz": tz, "mode": mode}


def http_post(url: str, headers: dict, body: bytes) -> tuple[int, Optional[dict]]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    # no proxy from the environment (the tokens go only to Finance), no redirect (NoRedirect)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read(1024 * 1024)
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(64 * 1024), exc.code
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, None
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def run(env, now: Optional[datetime] = None, post: Callable = http_post, sleep: Callable = time.sleep,
        out=sys.stdout) -> int:
    s = settings(env)
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(s["tz"]))
    if s["mode"] == "alerts":
        paths = list(ALERTS)
    else:
        paths = list(HOURLY) + (list(DAILY) if local.hour == s["hour"] else [])
    stamp = now.strftime("%Y%m%dT%H%MZ")
    failed = 0
    for path in paths:
        rid = f"cron-{path.strip('/').split('/')[-2 if path.endswith('/run') else -1]}-{stamp}"[:128]
        headers = {"Authorization": f"Bearer {s['bearer']}", "X-FIN-Caller-Token": s["caller"],
                   "Content-Type": "application/json", "User-Agent": "zbm-fin-cron/1"}
        body = json.dumps({"request_id": rid}).encode()
        status, doc = 0, None
        for attempt in range(len(RETRY_WAIT_S) + 1):
            status, doc = post(s["url"] + path, headers, body)
            if status != 503 or attempt == len(RETRY_WAIT_S):
                break
            sleep(RETRY_WAIT_S[attempt])
        ok = 200 <= status < 300
        failed += 0 if ok else 1
        summary = (doc or {}).get("summary") if isinstance(doc, dict) else None
        line = {"call": path, "status": status, "ok": ok, "request_id": rid}
        if isinstance(summary, dict):
            line["summary"] = {k: v for k, v in summary.items() if isinstance(v, (int, bool))}
        if not ok:
            line["level"] = "ALERT"
        print(("ALERT fin-cron: " if not ok else "fin-cron: ") + json.dumps(line, sort_keys=True), file=out,
              flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(os.environ))
