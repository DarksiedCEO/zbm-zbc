"""Authenticated loopback-only Compliance protocol fixture for the detection live harness.

This is test infrastructure, never a production Compliance adapter. The real
Compliance service owns rule evaluation; this fixture drives its HTTP contract.
"""
from __future__ import annotations

import hmac
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ComplianceFixture:
    def __init__(self):
        self.service_token = secrets.token_hex(32)
        self.caller_token = secrets.token_hex(32)
        self._modes: dict[str, str] = {}
        self._lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # No request headers or credentials in logs.

            def do_GET(self):
                authenticated = (
                    hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + fixture.service_token)
                    and hmac.compare_digest(self.headers.get("X-Compliance-Caller-Token", ""), fixture.caller_token)
                )
                if not authenticated:
                    status, body = 403, {"detail": "caller refused"}
                else:
                    with fixture._lock:
                        modes = dict(fixture._modes)
                    client = next((cid for cid in modes
                                   if self.path == f"/compliance/v1/clients/{cid}/activation"), None)
                    if client is None:
                        status, body = 404, {"detail": "unknown fixture client"}
                    elif modes[client] == "unavailable":
                        status, body = 503, {"detail": "fixture unavailable"}
                    else:
                        now = datetime.now(timezone.utc)
                        allowed = modes[client] == "allow"
                        expired = modes[client] == "expired"
                        status, body = 200, {
                            "client_id": client, "lane": "client", "decision": "ALLOW" if allowed else "REFUSE",
                            "current": allowed, "ruling_id": "cmp-rul-live" if allowed else None,
                            "evaluated_at": now.isoformat() if allowed else None, "register_version": 1,
                            "jurisdiction": "US-CA" if allowed else None,
                            "valid_until": (now - timedelta(days=1)).isoformat() if expired else
                                           (now + timedelta(days=1)).isoformat() if allowed else None,
                            "reasons": [] if allowed else ["activation expired" if expired else "not activated"],
                            "seed_pinned": True, "sanctions_grounded": None, "sanctions_ground": None,
                        }
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, name="live-compliance", daemon=True)

    def set_mode(self, client_id: str, mode: str) -> None:
        if mode not in {"allow", "refuse", "expired", "unavailable"}:
            raise ValueError("unknown Compliance fixture mode")
        with self._lock:
            self._modes[client_id] = mode

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("Compliance fixture did not stop")


def unexpected_socket_events(events: list[dict], allowed_ports: set[int]) -> list[dict]:
    """Only the precise loopback fixture/probe destinations may be contacted."""
    unexpected = []
    for event in events:
        if event.get("event") == "socket.connect":
            address = event.get("address")
            ok = (isinstance(address, list) and len(address) >= 2 and address[0] == "127.0.0.1"
                  and address[1] in allowed_ports)
        elif event.get("event") == "socket.getaddrinfo":
            ok = event.get("host") == "127.0.0.1" and event.get("port") in allowed_ports
        else:
            ok = False
        if not ok:
            unexpected.append(event)
    return unexpected
