"""Client offboarding across the services (ADR 0027 decision 3; MSA 15.5; runbook docs/runbooks/client-offboarding.md).
Standard library only; Andre runs it on his own machine through the tunnels. It never prints a token.

    python3 devtools/client_offboarding.py plan --config ~/zbm/offboarding.json --client <client id> --out ~/zbm/offb/<id>-plan.json
    python3 devtools/client_offboarding.py run  --config ~/zbm/offboarding.json --plan ~/zbm/offb/<id>-plan.json --out ~/zbm/offb/<id>-certificate.json

``plan`` asks each service for its DRY RUN and writes one plan file (mode 0600): what is held where, what is deleted,
withdrawn, closed or kept (and for how long), the blockers, and the steps no service can do (laptop, Gmail, Neon...).
``run`` executes it in order: Compliance first (closes the activation, certifies), then Finance LAST with Compliance's
certificate hash (deletes the billing contact, withdraws unsent mail, certifies), so the deletion certificate Finance
records on the ledger covers both. Each service refuses a plan that changed since the dry run; the tool then stops.

SEO, Legal, detection and delivery have no offboarding route: the plan lists what SEO holds for the client (read
from GET /seo/v1/tenants with the dashboard caller) and the manual steps for all four.

The config file (mode 0600) names each service's tunnel URL and the curl header files that carry its tokens (the same
``*.hdr`` files docs/deploy/railway.md uses: one ``Name: value`` line each, mode 0600):

    {"finance":    {"url": "http://127.0.0.1:18410", "header_files": ["~/.zbm-fin/auth.hdr", "~/.zbm-fin/andre.hdr"]},
     "compliance": {"url": "http://127.0.0.1:18380", "header_files": ["~/.zbm-cmp/auth.hdr", "~/.zbm-cmp/andre.hdr"]},
     "seo":        {"url": "http://127.0.0.1:18500", "header_files": ["~/.zbm-seo/auth.hdr", "~/.zbm-seo/dashboard.hdr"]}}

The certificate file holds hashes and counts only (no client id, no name, no address).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

CLIENT_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
URL_RE = re.compile(r"(http://(127\.0\.0\.1|localhost|\[::1\]|[a-z0-9-]{1,63}\.railway\.internal)|https://[A-Za-z0-9.-]{1,253})"
                    r"(:[0-9]{1,5})?")
HEADER_RE = re.compile(r"([A-Za-z0-9-]{1,64}):[ \t]*([\x21-\x7e][\x20-\x7e]{0,1023})")
MANUAL = {
    "detection": "Revenue Recovery store data: remove the client's store from RR_STORE_CONNECTIONS and unset its "
                 "RR_STORE_SECRET_* in Railway (detection keeps no volume); finding records on the ledger hold order "
                 "references: client-offboarding.md B.3",
    "delivery": "delivery-py is not deployed: it holds nothing for clients in production",
    "legal": "acceptance records and contract terms: kept 7 years (MSA 15.5(b)); Legal stores references, not names",
    "seo": "unbind the Search Console property (POST /seo/v1/tenants/{tid}/gsc-property {\"property\": null}), pause "
           "its schedules, ask the client to remove ZBM's service account; audit records stay in seo's append-only "
           "log (no per-client erase yet): client-offboarding.md B.4",
    "working_copies": "laptop (~/client-reports, Downloads), Gmail attachments, Drive, AI-tool chats, token files: "
                      "client-offboarding.md B.5",
    "neon_lead": "the lead row in Neon (zbest-sites), if the lead is no longer needed: client-offboarding.md B.5.7",
}


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _private_file(path: str) -> str:
    path = os.path.expanduser(path)
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        raise SystemExit(f"client_offboarding: cannot read {path}") from None
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.geteuid():
        raise SystemExit(f"client_offboarding: {path} must be a regular file you own, mode 600")
    return path


def load_config(path: str) -> dict:
    with open(_private_file(path), encoding="utf-8") as fh:
        cfg = json.load(fh)
    out = {}
    for svc in ("finance", "compliance", "seo"):
        c = cfg.get(svc)
        if c is None:
            if svc == "seo":
                continue
            raise SystemExit(f"client_offboarding: the config has no {svc}")
        url = str(c.get("url", "")).rstrip("/")
        if not URL_RE.fullmatch(url):
            raise SystemExit(f"client_offboarding: {svc} url must be a tunnel (http://127.0.0.1:<port>), a "
                             "*.railway.internal http URL or an https URL")
        headers = {}
        for hf in c.get("header_files") or []:
            with open(_private_file(hf), encoding="utf-8") as fh:
                for line in fh.read().splitlines():
                    if not line.strip():
                        continue
                    m = HEADER_RE.fullmatch(line.strip())
                    if not m:
                        raise SystemExit(f"client_offboarding: {hf} is not 'Name: value' lines")
                    headers[m.group(1)] = m.group(2)
        out[svc] = {"url": url, "headers": headers}
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):           # a redirect would carry the tokens elsewhere
        return None


def http(method: str, url: str, headers: dict, body: Optional[bytes]):
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={**headers, "Content-Type": "application/json",
                                          "User-Agent": "zbm-offboarding/1"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(req, timeout=60) as resp:
            raw, status = resp.read(4 * 1024 * 1024), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(256 * 1024), exc.code
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 0, {"detail": f"unreachable ({type(exc).__name__})"}
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def _rid(kind: str) -> str:
    return f"offb-{kind}-{int(time.time())}-{os.getpid()}"


def _offb(call, cfg, svc: str, path: str, client: str, body: dict):
    return call("POST", f"{cfg[svc]['url']}{path.format(client=client)}", cfg[svc]["headers"],
                json.dumps({"request_id": _rid(svc[:3]), **body}).encode())


PATHS = {"finance": "/fin/v1/clients/{client}/offboarding", "compliance": "/compliance/v1/clients/{client}/offboarding"}


def _write_private(path: str, doc: dict) -> None:
    path = os.path.expanduser(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(path, 0o600)


def cmd_plan(args, call, out) -> int:
    cfg = load_config(args.config)
    client = args.client
    plan = {"version": 1, "client_id": client, "lane": args.lane, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                                              time.gmtime()),
            "services": {}, "manual": dict(MANUAL)}
    blocked = False
    for svc in ("compliance", "finance"):
        body = {"dry_run": True, **({"lane": args.lane} if svc == "compliance" else {})}
        status, doc = _offb(call, cfg, svc, PATHS[svc], client, body)
        if status != 200 or not isinstance(doc, dict) or "plan_sha256" not in doc:
            print(f"{svc}: dry run failed (HTTP {status}): {str((doc or {}).get('detail', ''))[:300]}", file=out)
            return 1
        plan["services"][svc] = {k: doc.get(k) for k in ("plan_sha256", "held", "blockers", "warnings")}
        blocked = blocked or bool(doc.get("blockers"))
    if "seo" in cfg:
        status, doc = call("GET", f"{cfg['seo']['url']}/seo/v1/tenants", cfg["seo"]["headers"], None)
        if status == 200 and isinstance(doc, list):
            mine = [t for t in doc if isinstance(t, dict) and t.get("finance_client_id") == client]
            plan["services"]["seo"] = {"tenants": [{"tenant_id": t.get("tenant_id"), "domains": len(t.get("domains") or []),
                                                    "gsc_property_bound": bool(t.get("gsc_property")),
                                                    "audits": t.get("audits", 0)} for t in mine],
                                       "action": "manual (see manual.seo)"}
        else:
            plan["services"]["seo"] = {"error": f"tenants not readable (HTTP {status})", "action": "manual"}
    _write_private(args.out, plan)
    for svc, p in plan["services"].items():
        for h in p.get("held") or []:
            ret = f"  {h['retention']}" if h.get("retention") else ""
            print(f"{svc:<11} {h.get('kind', ''):<30} {h.get('count', 0):>5}  {h.get('action', '')}{ret}", file=out)
        for t in p.get("tenants") or []:
            print(f"{svc:<11} tenant {t['tenant_id']}: {t['audits']} audit(s), Search Console bound: "
                  f"{t['gsc_property_bound']}", file=out)
        for b in p.get("blockers") or []:
            print(f"{svc:<11} BLOCKED: {b}", file=out)
        for w in p.get("warnings") or []:
            print(f"{svc:<11} warning: {w}", file=out)
    for k, v in MANUAL.items():
        print(f"manual      {k}: {v}", file=out)
    print(("BLOCKED: resolve the blockers, then plan again" if blocked else
           f"plan written to {args.out}; read it, then: run --plan {args.out}"), file=out)
    return 0


def cmd_run(args, call, out) -> int:
    cfg = load_config(args.config)
    with open(_private_file(args.plan), encoding="utf-8") as fh:
        plan = json.load(fh)
    client = plan.get("client_id", "")
    if not CLIENT_RE.fullmatch(client):
        raise SystemExit("client_offboarding: the plan names no valid client id")
    if any(p.get("blockers") for p in plan["services"].values()):
        print("the plan has blockers: resolve them and plan again", file=out)
        return 1
    certs = {}
    for svc in ("compliance", "finance"):
        body = {"dry_run": False, "plan_sha256": plan["services"][svc]["plan_sha256"]}
        if svc == "compliance":
            body["lane"] = plan.get("lane", "client")
        else:
            body["peer_certificates"] = [{"service": "compliance",
                                          "certificate_sha256": certs["compliance"]["certificate_sha256"]}]
        status, doc = _offb(call, cfg, svc, PATHS[svc], client, body)
        if status != 200 or not isinstance(doc, dict) or "certificate_sha256" not in doc:
            print(f"{svc}: the run was refused (HTTP {status}): {str((doc or {}).get('detail', ''))[:300]}; nothing "
                  "after it ran" + (". Compliance already closed the activation and certified; plan again and run "
                                    "Finance" if svc == "finance" else ""), file=out)
            return 1
        if sha(doc["certificate"]) != doc["certificate_sha256"]:
            print(f"{svc}: the certificate does not match its hash; stop and investigate", file=out)
            return 1
        certs[svc] = doc
        print(f"{svc}: done, certificate {doc['certificate_sha256'][:16]}...", file=out)
    combined = {"version": 1, "client_id_sha256": hashlib.sha256(client.encode()).hexdigest(),
                "ledger_certificate_sha256": certs["finance"]["certificate_sha256"],
                "certificates": {k: v["certificate"] for k, v in certs.items()},
                "manual_steps_pending": sorted(MANUAL)}
    _write_private(args.out, combined)
    print(f"certificate written to {args.out}. The ledger holds Finance's certificate "
          f"{certs['finance']['certificate_sha256']} (covering Compliance's). Now do the manual steps and tick them "
          "in the client file (runbook section C).", file=out)
    return 0


def main(argv=None, http: Callable = http, out=sys.stdout) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--config", required=True)
    p.add_argument("--client", required=True)
    p.add_argument("--lane", default="client", choices=("client", "zbc_brand"))
    p.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--plan", required=True)
    r.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "plan":
        if not CLIENT_RE.fullmatch(args.client):
            raise SystemExit("client_offboarding: --client must be 1-100 characters of [A-Za-z0-9._-]")
        return cmd_plan(args, http, out)
    return cmd_run(args, http, out)


if __name__ == "__main__":
    sys.exit(main())
