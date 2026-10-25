#!/usr/bin/env python3
"""Render the client SEO audit report of a completed audit (ADR 0017, section "Client report").

usage:
  python3 devtools/render_report.py fetch --api URL --tenant TID --audit AID --format html|markdown --out PATH \\
      --service-token-file F --caller-token-file F --andre-token-file F [--request-id UUID]
  python3 devtools/render_report.py preview AUDIT.json --tenant TID --format html|markdown --out PATH \\
      [--brand-name NAME] [--brand-accent '#RRGGBB']

``fetch`` is the delivery path: it asks the service (``POST /seo/v1/tenants/{tid}/audits/{aid}/client-report``, the
dashboard caller with Andre's approval token), which records the render on the ledger BEFORE answering. The document
is written (mode 0600, also when the file existed; never through a symlink) only when (1) its SHA-256 and size match
the answer's own ``document_sha256`` / ``bytes`` (a truncated or altered transfer) and (2) the service's list of
recorded renders (``GET .../client-reports``, which holds a render only after the ledger accepted its event) has this
``report_id`` with the same ``document_sha256``. It does not read the ledger itself. ``--api`` must be https, except
for a loopback host (127.0.0.0/8, ::1, localhost): the request carries the service, caller and approval tokens.
Re-running with the same ``--request-id`` returns the same document (or REPORT_CHANGED when the retained view changed
since). Tokens are read from files (0600 recommended), never from the command line.

``preview`` renders offline from a saved audit view (the body of ``GET /seo/v1/tenants/{tid}/audits/{aid}``). Nothing
is recorded, so the document carries a PREVIEW banner and is not for delivery. Exit 2 when the audit cannot be
rendered (not completed, another tenant's, malformed) or the service refuses.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import client_report  # noqa: E402

TENANT = re.compile(r"[a-z][a-z0-9-]{1,39}")
AUDIT = re.compile(r"seo-aud-[0-9a-f]{40}")


def _token(path: str) -> str:
    tok = Path(path).read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[\x21-\x7e]{32,512}", tok):
        raise ValueError("a token file must hold 32..512 printable ASCII characters")
    return tok


def api_base(url: str) -> str:
    """The service's base URL: https, or http only to a loopback host (AEGIS M-3: the request carries three secrets).
    No credentials, query or fragment."""
    from urllib.parse import urlsplit
    if not isinstance(url, str) or not re.fullmatch(r"[\x21-\x7e]{1,2048}", url):
        # AEGIS N-3: control characters, spaces and non-ASCII refused here, before urlsplit strips or httpx trips
        raise ValueError("--api must be printable ASCII without spaces")
    try:
        u = urlsplit(url)
        host, _port = (u.hostname or ""), u.port
    except ValueError:
        raise ValueError("--api is not a URL") from None
    if "?" in url or "#" in url or "@" in u.netloc or not host:
        raise ValueError("--api must be scheme://host[:port][/path] without credentials, query or fragment")
    if u.scheme == "https":
        return url.rstrip("/")
    if u.scheme == "http":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        if loopback:
            return url.rstrip("/")
    raise ValueError("--api must be https (plain http only to a loopback host): the request carries the tokens")


def fetch(a: argparse.Namespace, client=None) -> tuple[bytes, dict]:
    import httpx
    if not TENANT.fullmatch(a.tenant) or not AUDIT.fullmatch(a.audit):
        raise ValueError("tenant or audit id malformed")
    base = api_base(a.api)
    headers = {"Authorization": f"Bearer {_token(a.service_token_file)}",
               "X-SEO-Caller-Token": _token(a.caller_token_file),
               "X-Andre-Approval-Token": _token(a.andre_token_file)}
    body = {"request_id": a.request_id or str(uuid.uuid4()), "format": a.format}
    own = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=False, trust_env=False)
    try:
        r = client.post(f"{base}/seo/v1/tenants/{a.tenant}/audits/{a.audit}/client-report", json=body, headers=headers)
        listed = client.get(f"{base}/seo/v1/tenants/{a.tenant}/audits/{a.audit}/client-reports",
                            headers={k: v for k, v in headers.items() if k != "X-Andre-Approval-Token"}) \
            if r.status_code == 200 else None
    finally:
        if own:
            client.close()
    if r.status_code != 200:
        detail = None
        try:
            detail = r.json().get("detail")
        except ValueError:
            pass
        raise PermissionError(f"the service refused ({r.status_code} {detail if isinstance(detail, str) else ''})")
    rep = r.json()
    data = rep["content"].encode("utf-8")
    if hashlib.sha256(data).hexdigest() != rep["document_sha256"] or len(data) != rep["bytes"]:
        raise ValueError("the document does not match the SHA-256 in the service's answer")
    rows = listed.json() if listed is not None and listed.status_code == 200 else None
    if not isinstance(rows, list) or not any(isinstance(x, dict) and x.get("report_id") == rep["report_id"]
                                             and x.get("document_sha256") == rep["document_sha256"] for x in rows):
        raise ValueError("the service's recorded renders do not list this document")
    meta = {k: rep[k] for k in ("report_id", "audit_id", "format", "document_sha256", "audit_report_sha256",
                                "renderer_version")} | {"request_id": body["request_id"], "recorded": True}
    return data, meta


def preview(a: argparse.Namespace) -> tuple[bytes, dict]:
    audit = json.loads(Path(a.audit_json).read_bytes())
    branding = client_report.Branding(name=a.brand_name or client_report.DEFAULT_BRANDING.name,
                                      accent=a.brand_accent or client_report.DEFAULT_BRANDING.accent)
    data, _ = client_report.render(audit, a.tenant, a.format, branding, preview=True)
    return data, {"audit_id": audit.get("audit_id"), "format": a.format,
                  "document_sha256": client_report.document_sha256(data), "recorded": False}


def main(argv: Optional[list] = None, client=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="render through the service (recorded on the ledger)")
    f.add_argument("--api", required=True)
    f.add_argument("--audit", required=True)
    f.add_argument("--service-token-file", required=True)
    f.add_argument("--caller-token-file", required=True)
    f.add_argument("--andre-token-file", required=True)
    f.add_argument("--request-id")
    p = sub.add_parser("preview", help="render offline from a saved audit view (not recorded; PREVIEW banner)")
    p.add_argument("audit_json")
    p.add_argument("--brand-name")
    p.add_argument("--brand-accent")
    for s in (f, p):
        s.add_argument("--tenant", required=True)
        s.add_argument("--format", choices=sorted(client_report.FORMATS), required=True)
        s.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    try:
        data, meta = fetch(a, client) if a.cmd == "fetch" else preview(a)
    except (client_report.ReportInputError, PermissionError, ValueError, KeyError, TypeError, OSError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    try:
        # AEGIS N-2: never through a symlink, and 0600 even when the file already existed (O_CREAT's mode applies
        # only to a new file)
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as e:
        print(f"refused: cannot write --out ({e.strerror})", file=sys.stderr)
        return 2
    with os.fdopen(fd, "wb") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(data)
    print(json.dumps({**meta, "out": a.out, "bytes": len(data)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
