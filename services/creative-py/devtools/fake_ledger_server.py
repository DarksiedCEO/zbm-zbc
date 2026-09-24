"""
DEV-ONLY stand-in for ledger-rust's `POST /ledger/events` (BUILD_CONTRACTS
section 2), for live smoke runs of creative-py when ledger-rust isn't
running. Stdlib only. Binds 127.0.0.1. Not a ledger: in-memory, no
persistence, a simple sha256 chain for show.

    FAKE_LEDGER_TOKEN=... python3 devtools/fake_ledger_server.py [port]   # default 18390
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("FAKE_LEDGER_TOKEN") or sys.exit("FAKE_LEDGER_TOKEN must be set")
# Same rules as ledger-rust src/event.rs `EventInput::validate`: fullmatch
# (a trailing newline is invalid), ASCII-only ids, summary 1-280 Unicode
# scalar values with no control character (C0, DEL or C1 — Rust
# `char::is_control`), and no lone surrogate (invalid JSON for serde).
FIELDS = {
    "event_id": re.compile(r"[A-Za-z0-9._:-]{1,128}"),
    "department": re.compile(r"[a-z0-9_]{1,64}"),
    "event_type": re.compile(r"[a-z0-9_]{1,64}"),
    "actor": re.compile(r"[a-z0-9_]{1,64}"),
    "subject_id": re.compile(r"[A-Za-z0-9._:-]{1,128}"),
    "payload_sha256": re.compile(r"[0-9a-f]{64}"),
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
ENTRIES: list[dict] = []


def validate(body) -> str | None:
    """None if ledger-rust would accept `body`, else the error."""
    if not isinstance(body, dict) or set(body) != set(FIELDS) | {"summary"}:
        return "fields must be exactly the contract fields"
    for k, rx in FIELDS.items():
        if not isinstance(body[k], str) or not rx.fullmatch(body[k]):
            return f"invalid {k}"
    s = body["summary"]
    if not isinstance(s, str) or not 1 <= len(s) <= 280 or _CONTROL.search(s):
        return "invalid summary"
    return None


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self) -> bool:
        h = self.headers.get("Authorization", "")
        try:
            return h.startswith("Bearer ") and hmac.compare_digest(h[7:], TOKEN)
        except TypeError:
            return False

    def do_GET(self):  # noqa: N802
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/ledger/entries":
            return self._send(200, ENTRIES)
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/ledger/events":
            return self._send(404, {"error": "not found"})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except Exception:
            return self._send(400, {"error": "invalid json"})
        err = validate(body)
        if err:
            return self._send(400, {"error": err})
        for e in ENTRIES:
            if e["event_id"] == body["event_id"]:
                same = all(e[k] == body[k] for k in body)
                return self._send(200 if same else 409, e if same else {"error": "event_id conflict"})
        prev = ENTRIES[-1]["hash"] if ENTRIES else "0" * 64
        entry = {"seq": len(ENTRIES) + 1, "kind": "event", **body,
                 "recorded_at": datetime.now(timezone.utc).isoformat(), "prev_hash": prev}
        entry["hash"] = hashlib.sha256((prev + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        ENTRIES.append(entry)
        return self._send(201, entry)

    def log_message(self, fmt, *args):  # quieter
        sys.stderr.write("fake-ledger " + (fmt % args) + "\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18390
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
