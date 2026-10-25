#!/usr/bin/env python3
"""Render the client SEO audit report of a completed audit (ADR 0017, section "Client report").

usage:
  python3 devtools/render_report.py fetch --api URL --tenant TID --audit AID --format html|markdown --out PATH \\
      --service-token-file F --caller-token-file F --andre-token-file F [--request-id UUID]
  python3 devtools/render_report.py preview AUDIT.json --tenant TID --format html|markdown --out PATH \\
      [--brand-name NAME] [--brand-accent '#RRGGBB']

``fetch`` is the delivery path: it asks the service (``POST /seo/v1/tenants/{tid}/audits/{aid}/client-report``, the
dashboard caller with Andre's approval token), which records the render on the ledger BEFORE answering; the document
is written only when its SHA-256 matches the one the service recorded. Re-running with the same ``--request-id``
returns the same document (or REPORT_CHANGED when the retained view changed since). Tokens are read from files
(0600 recommended), never from the command line.

``preview`` renders offline from a saved audit view (the body of ``GET /seo/v1/tenants/{tid}/audits/{aid}``). Nothing
is recorded, so the document carries a PREVIEW banner and is not for delivery. Exit 2 when the audit cannot be
rendered (not completed, another tenant's, malformed) or the service refuses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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


def fetch(a: argparse.Namespace, client=None) -> tuple[bytes, dict]:
    import httpx
    if not TENANT.fullmatch(a.tenant) or not AUDIT.fullmatch(a.audit):
        raise ValueError("tenant or audit id malformed")
    headers = {"Authorization": f"Bearer {_token(a.service_token_file)}",
               "X-SEO-Caller-Token": _token(a.caller_token_file),
               "X-Andre-Approval-Token": _token(a.andre_token_file)}
    body = {"request_id": a.request_id or str(uuid.uuid4()), "format": a.format}
    own = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=False, trust_env=False)
    try:
        r = client.post(f"{a.api.rstrip('/')}/seo/v1/tenants/{a.tenant}/audits/{a.audit}/client-report",
                        json=body, headers=headers)
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
        raise ValueError("the document does not match the SHA-256 the service recorded")
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
    Path(a.out).write_bytes(data)
    print(json.dumps({**meta, "out": a.out, "bytes": len(data)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
