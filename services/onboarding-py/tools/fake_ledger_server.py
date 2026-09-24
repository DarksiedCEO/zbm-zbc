"""
A SMALL FAKE of ledger-rust's ``POST /ledger/events`` (BUILD_CONTRACTS.md
section 2), for local runs of onboarding-py only. Prefer the real
ledger-rust (see the README's live-run section); this fake validates
exactly like it (event.rs) so it is never looser. Standard library
only. NOT the real ledger: in-memory, no persistence, a simple SHA-256
chain for show — the real events endpoint is being built in ledger-rust by
the Decimal/ledger workstream.

Implements the contract's observable behaviour:
  201 new event | 200 identical retry | 409 same event_id, different content
  400 validation failure (unknown fields rejected) | 401 bad/missing token
  GET /ledger/entries (bearer) lists entries; GET /health is open.

Usage:
  LEDGER_SERVICE_TOKEN=... python3 tools/fake_ledger_server.py --port 18290
Binds 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIELDS = {
    "event_id": re.compile(r"[A-Za-z0-9._:-]{1,128}"),
    "department": re.compile(r"[a-z0-9_]{1,64}"),
    "event_type": re.compile(r"[a-z0-9_]{1,64}"),
    "actor": re.compile(r"[a-z0-9_]{1,64}"),
    "subject_id": re.compile(r"[A-Za-z0-9._:-]{1,128}"),
    "payload_sha256": re.compile(r"[0-9a-f]{64}"),
}
# Same as ledger-rust (event.rs): Rust's char::is_control is Unicode Cc —
# C0, DEL AND C1 (U+0080-U+009F). Lone surrogates are not Unicode scalar
# values, so serde rejects them too.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")

TOKEN = os.environ.get("LEDGER_SERVICE_TOKEN", "")
ENTRIES: list[dict] = []
BY_ID: dict[str, dict] = {}
LOCK = threading.Lock()


def validate(body) -> str | None:
    if not isinstance(body, dict):
        return "body must be an object"
    expected = set(FIELDS) | {"summary"}
    if set(body) != expected:
        return "unknown or missing fields"
    for k, rx in FIELDS.items():
        # fullmatch: Python's "$" would accept a trailing newline; Rust does not.
        if not isinstance(body[k], str) or not rx.fullmatch(body[k]):
            return f"invalid {k}"
    s = body["summary"]
    if not isinstance(s, str) or not 1 <= len(s) <= 280 or CONTROL.search(s):
        return "invalid summary"
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        return

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        h = self.headers.get("Authorization", "")
        if not h.startswith("Bearer "):
            return False
        try:
            return hmac.compare_digest(h[7:].encode("utf-8", "surrogateescape"), TOKEN.encode())
        except Exception:  # noqa: BLE001
            return False

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok", "service": "fake-ledger (onboarding live-run tool)"})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/ledger/entries":
            with LOCK:
                return self._send(200, list(ENTRIES))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/ledger/events":
            return self._send(404, {"error": "not found"})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except Exception:  # noqa: BLE001
            return self._send(400, {"error": "invalid json"})
        err = validate(body)
        if err:
            return self._send(400, {"error": err})
        with LOCK:
            existing = BY_ID.get(body["event_id"])
            if existing is not None:
                same = all(existing[k] == body[k] for k in body)
                return self._send(200 if same else 409, existing if same else {"error": "event_id exists with different content"})
            prev = ENTRIES[-1]["hash"] if ENTRIES else "0" * 64
            entry = {"seq": len(ENTRIES) + 1, "kind": "event", **body,
                     "recorded_at": datetime.now(timezone.utc).isoformat(), "prev_hash": prev}
            entry["hash"] = hashlib.sha256((prev + json.dumps(body, sort_keys=True)).encode()).hexdigest()
            ENTRIES.append(entry)
            BY_ID[body["event_id"]] = entry
        self._send(201, entry)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18290)
    args = ap.parse_args()
    if not TOKEN:
        raise SystemExit("LEDGER_SERVICE_TOKEN is not set; refusing to start")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
