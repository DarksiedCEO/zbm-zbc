"""
The in-process service for conformance tests: a durable data dir in a temp folder, a generated vault key, the
in-memory web (tests/cert_web.py: real Fetcher, httpx.MockTransport), and every output collected for the D-05
scan: API answers, ledger events, every data-dir file, every Python log record at DEBUG, and every request sent
(host, path) so D-02m can check where planted values went.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time

from cert_web import Web, page, sitemap  # noqa: F401  (re-exported for tests)
from helpers import Harness, rid
from ports import Ports

GOOGLE_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
OWN = "site.test"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:  # noqa: BLE001 - a broken record is still captured by its template
            self.lines.append(str(record.msg))


class Svc:
    """One service over one Web. ``tenant`` is created with ``site.test`` registered."""

    def __init__(self, tmp_path, web: Web, tenant: str = "zbm", durable: bool = True, **env):
        self.web = web
        self.tmp = tmp_path
        ports = Ports.default()
        ports.fetcher = web.fetcher()
        self.cap = _Capture()
        root = logging.getLogger()
        self._old_level = root.level
        root.addHandler(self.cap)
        root.setLevel(logging.DEBUG)
        self.h = Harness(tmp_path, data_dir=str(tmp_path / "data") if durable else None, ports=ports, **env)
        self.answers: list = []                         # D-02e/i-l: stored and returned forms
        self.displays: list = []                        # D-02g/h: owner displays (resolved; not scanned for D-05)
        self.statuses: list = []
        self.walls: list = []
        if tenant:
            self.h.ok(self.h.post(f"/tenants/{tenant}/domains", {"request_id": rid(), "domains": [OWN]}, andre=True))

    def close(self):
        logging.getLogger().removeHandler(self.cap)
        logging.getLogger().setLevel(self._old_level)
        self.h.svc.close()

    # ------------------------------------------------------------------ calls (every answer kept for the scan)

    def _keep(self, path, resp, t0):
        self.walls.append(time.monotonic() - t0)
        self.statuses.append(resp.status_code)
        owner_display = "resolve=evidence" in path or path.endswith("/client-report")
        (self.displays if owner_display else self.answers).append(resp.text)
        return resp

    def post(self, path, body, **kw):
        t0 = time.monotonic()
        return self._keep(path, self.h.post(path, body, **kw), t0)

    def get(self, path, **kw):
        t0 = time.monotonic()
        return self._keep(path, self.h.get(path, **kw), t0)

    def audit(self, paths=("/",), tenant="zbm", domain=OWN):
        r = self.post(f"/tenants/{tenant}/audits", {"request_id": rid(), "domain": domain, "scheme": "http",
                                                    "paths": list(paths)})
        assert r.status_code == 201, (r.status_code, r.text[:300])
        return r.json()

    def audit_resolved(self, aid, tenant="zbm"):
        return self.get(f"/tenants/{tenant}/audits/{aid}?resolve=evidence", caller="hub", tenant=tenant)

    def log_ingest_resolved(self, iid, tenant="zbm", caller="hub"):
        return self.get(f"/tenants/{tenant}/log-ingests/{iid}?resolve=evidence", caller=caller,
                        tenant=tenant if caller == "hub" else None)

    def client_report(self, aid, fmt="markdown", tenant="zbm"):
        r = self.post(f"/tenants/{tenant}/audits/{aid}/client-report", {"request_id": rid(), "format": fmt},
                      caller="dashboard", andre=True)
        return r

    def log_ingest(self, lines, tenant="zbm", fmt="combined"):
        """Create an ingest, send ``lines`` in line-aligned chunks of at most 80,000 bytes, finish it."""
        iid = self.h.ok(self.post(f"/tenants/{tenant}/log-ingests", {"request_id": rid(), "domain": OWN,
                                                                      "scheme": "http", "format": fmt}), 201)
        iid = iid["ingest_id"]
        chunks, cur = [], b""
        for ln in lines:
            b = ln.encode("utf-8", "surrogateescape") + b"\n"
            if cur and len(cur) + len(b) > 80_000:
                chunks.append(cur)
                cur = b""
            cur += b
        chunks.append(cur)
        for k, c in enumerate(chunks):
            self.h.ok(self.post(f"/tenants/{tenant}/log-ingests/{iid}/chunks",
                                {"request_id": rid(), "seq": k + 1, "data_b64": base64.b64encode(c).decode(),
                                 "last": k == len(chunks) - 1}))
        return self.h.ok(self.post(f"/tenants/{tenant}/log-ingests/{iid}/finish", {"request_id": rid()}))

    # ------------------------------------------------------------------ outputs for the D-05 scan

    def stored_outputs(self) -> str:
        """D-02a-e, i-m surfaces: every answer, the ledger, the Compliance export, every data-dir file (log, vault
        lines), every log record, and the request lines sent to hosts other than the audited site."""
        parts = list(self.answers)
        parts.append(json.dumps(self.h.ledger.events, default=str))
        exp = self.h.get("/audit/export", caller="compliance_38")
        parts.append(exp.text)
        data = self.tmp / "data"
        if data.exists():
            for root, _, files in os.walk(data):
                for f in files:
                    with open(os.path.join(root, f), "rb") as fh:
                        parts.append(fh.read().decode("utf-8", "replace"))
        parts.extend(self.cap.lines)
        parts.extend(f"{h}{p}" for h, p in self.web.requests if h not in (OWN, "www." + OWN))
        return "\n".join(parts)


def clf(ip, path, status=200, ua=GOOGLE_UA, ref="-"):
    return f'{ip} - - [10/Oct/2026:13:55:36 -0700] "GET {path} HTTP/1.1" {status} 2326 "{ref}" "{ua}"'
