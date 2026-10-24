"""A fake Google for the Search Console connector's tests and the war room: the token endpoint and the two Search
Console methods behind httpx.MockTransport, a test-only RSA key generated at run time (never written to the
repository), an injected clock, and a fake socket layer for the production deadline backend. No network."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from datetime import date, timedelta
from urllib.parse import parse_qs, unquote

import httpcore
import httpx

_KEY_CACHE: dict = {}
SA_EMAIL = "seo-reader@zbm-seo-test.iam.gserviceaccount.com"
SA_KEY_ID = "0123456789abcdef0123456789abcdef01234567"


def rsa_key():
    """One 2048-bit RSA key per test process, generated here (never stored in the repository)."""
    if "k" not in _KEY_CACHE:
        from cryptography.hazmat.primitives.asymmetric import rsa
        _KEY_CACHE["k"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _KEY_CACHE["k"]


def pem() -> str:
    from cryptography.hazmat.primitives import serialization
    return rsa_key().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()).decode("ascii")


def key_json(**over) -> dict:
    d = {"type": "service_account", "project_id": "zbm-seo-test", "private_key_id": SA_KEY_ID, "private_key": pem(),
         "client_email": SA_EMAIL, "client_id": "1" * 21, "token_uri": "https://oauth2.googleapis.com/token"}
    d.update(over)
    return {k: v for k, v in d.items() if v is not None}


def write_key_file(path, mode: int = 0o600, **over) -> str:
    path.write_text(json.dumps(key_json(**over)))
    os.chmod(path, mode)
    return str(path)


def pem_fragments() -> list:
    """Pieces of the private key that must never appear anywhere: PEM body lines and the private exponent."""
    lines = [ln for ln in pem().splitlines() if ln and "-----" not in ln]
    d = rsa_key().private_numbers().d
    return lines[1:4] + [str(d)[:40], format(d, "x")[:40], "PRIVATE KEY"]


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Clock:
    """Injected monotonic and wall clocks; ``sleep`` only moves time (and records the pause)."""

    def __init__(self, wall: float = 1_791_000_000.0):
        self.t = 1000.0
        self.w = wall
        self.sleeps: list = []

    def mono(self) -> float:
        return self.t

    def wall(self) -> float:
        return self.w + (self.t - 1000.0)

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s

    def advance(self, s: float) -> None:
        self.t += s


class Drip(httpx.SyncByteStream):
    """A body trickled a few bytes at a time; every chunk moves the injected clock."""

    def __init__(self, data: bytes, clock: Clock, step_s: float, chunk: int = 4):
        self.data, self.clock, self.step, self.chunk = data, clock, step_s, chunk
        self.sent = 0

    def __iter__(self):
        for i in range(0, len(self.data), self.chunk):
            self.clock.advance(self.step)
            self.sent += 1
            yield self.data[i:i + self.chunk]


class Endless(httpx.SyncByteStream):
    """A body that never ends (a huge answer): 64 KiB chunks for as long as anyone reads."""

    def __init__(self):
        self.sent = 0

    def __iter__(self):
        while True:
            self.sent += 1
            yield b"[" * 65536


class FakeGoogle:
    """oauth2.googleapis.com/token and www.googleapis.com/webmasters/v3/{sites, .../searchAnalytics/query}.
    ``script`` queues answers (status, json-or-bytes[, headers]) or callables for the next API calls;
    ``token_script`` the same for the token endpoint."""

    def __init__(self, clock: Clock | None = None, prop: str = "sc-domain:acme.test",
                 level: str = "siteFullUser"):
        self.clock = clock or Clock()
        self.requests: list = []
        self.script: list = []
        self.token_script: list = []
        self.sites = [{"siteUrl": prop, "permissionLevel": level}]
        self.tokens: list = []
        self.dates_clicks = (5, 50)               # per date: clicks, impressions (+ position 7.5)
        self.pages = [("https://acme.test/blog/post-1", 30, 300, 4.0), ("https://acme.test/blog/post-2", 10, 200, 9.0),
                      ("https://www.acme.test/u/jane.doe@example.com", 1, 10, 3.0),
                      ("https://elsewhere.test/x", 2, 20, 12.0)]
        self.queries = [("acme pricing", 20, 100, 2.0), ("how to clean a rug", 10, 150, 8.0),
                        ("rug cleaner near me", 5, 60, 5.0), ("jane.doe@example.com", 1, 3, 1.0),
                        ("call 555 010 0199", 1, 2, 1.0), ("blue rug", 3, 40, 11.0)]
        self.on_request: Callable | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        self.requests.append({"method": request.method, "host": request.url.host, "path": request.url.raw_path.decode(),
                              "body": body, "headers": dict(request.headers)})
        if self.on_request is not None:
            self.on_request(self, request)
        if request.url.host == "oauth2.googleapis.com":
            if self.token_script:
                return self._scripted(self.token_script.pop(0), request)
            tok = f"fake-access-token-{len(self.tokens) + 1:04d}-" + "t" * 24
            self.tokens.append(tok)
            return httpx.Response(200, json={"access_token": tok, "expires_in": 3600, "token_type": "Bearer"})
        if self.script:
            return self._scripted(self.script.pop(0), request)
        auth = request.headers.get("authorization", "")
        if not self.tokens or auth != f"Bearer {self.tokens[-1]}":
            return httpx.Response(401, json={"error": {"code": 401, "message": "unauthorized",
                                                       "errors": [{"reason": "authError"}]}})
        path = request.url.raw_path.decode()
        if request.method == "GET" and path == "/webmasters/v3/sites":
            return httpx.Response(200, json={"siteEntry": self.sites})
        if request.method == "POST" and path.endswith("/searchAnalytics/query"):
            prop = unquote(path.split("/sites/", 1)[1].rsplit("/searchAnalytics", 1)[0])
            if prop not in {s["siteUrl"] for s in self.sites if s["permissionLevel"] != "siteUnverifiedUser"}:
                return httpx.Response(403, json={"error": {"code": 403, "message": "no",
                                                           "errors": [{"reason": "insufficientPermissions"}]}})
            return httpx.Response(200, json=self.query(json.loads(body)))
        return httpx.Response(404, json={"error": {"code": 404, "message": "not found"}})

    def _scripted(self, item, request):
        if callable(item):
            return item(request)
        status, payload, *rest = item
        headers = rest[0] if rest else {}
        if isinstance(payload, (bytes, httpx.SyncByteStream)):
            return httpx.Response(status, content=payload if isinstance(payload, bytes) else None,
                                  stream=payload if isinstance(payload, httpx.SyncByteStream) else None,
                                  headers=headers)
        return httpx.Response(status, json=payload, headers=headers)

    def query(self, q: dict) -> dict:
        dims = q["dimensions"]
        start, end = date.fromisoformat(q["startDate"]), date.fromisoformat(q["endDate"])
        if dims == ["date"]:
            rows = []
            d = start
            while d <= end:
                c, i = self.dates_clicks
                rows.append({"keys": [d.isoformat()], "clicks": c, "impressions": i, "ctr": c / i if i else 0,
                             "position": 7.5})
                d += timedelta(days=1)
        else:
            src = self.pages if dims == ["page"] else self.queries
            rows = [{"keys": [k], "clicks": c, "impressions": i, "ctr": c / i if i else 0, "position": p}
                    for k, c, i, p in src]
        sr, lim = q.get("startRow", 0), q.get("rowLimit", 1000)
        return {"rows": rows[sr:sr + lim], "responseAggregationType": q["aggregationType"]}

    def form(self, i: int = 0) -> dict:
        tok = [r for r in self.requests if r["host"] == "oauth2.googleapis.com"][i]
        return {k: v[0] for k, v in parse_qs(tok["body"].decode()).items()}

    def api_requests(self) -> list:
        return [r for r in self.requests if r["host"] == "www.googleapis.com"]


# ---------------------------------------------------------------------- the socket layer, faked

class FakeStream(httpcore.NetworkStream):
    """A fake TCP/TLS stream that answers one canned HTTP/1.1 response, ``chunk`` bytes per read, moving the clock
    ``step_s`` per read. ``start_tls`` returns itself (no ssl_object: HTTP/1.1)."""

    def __init__(self, response: bytes, clock: Clock, step_s: float = 0.0, chunk: int = 65536, log: list | None = None):
        self.resp, self.clock, self.step, self.chunk, self.log = response, clock, step_s, chunk, log
        self.pos = 0
        self.written = b""

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if self.log is not None:
            self.log.append(("read", timeout))
        self.clock.advance(self.step)
        out = self.resp[self.pos:self.pos + min(max_bytes, self.chunk)]
        self.pos += len(out)
        return out

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.written += buffer

    def close(self) -> None:
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        if self.log is not None:
            self.log.append(("tls", server_hostname))
        return self

    def get_extra_info(self, info: str):
        return None


class FakeNetwork(httpcore.NetworkBackend):
    """Hands out FakeStreams; records every connect (host must already be an IP literal: the deadline backend
    resolves first)."""

    def __init__(self, responses: list, clock: Clock, step_s: float = 0.0, chunk: int = 65536):
        self.responses, self.clock, self.step, self.chunk = list(responses), clock, step_s, chunk
        self.connects: list = []
        self.log: list = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connects.append((host, port, timeout))
        return FakeStream(self.responses.pop(0), self.clock, self.step, self.chunk, self.log)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("no")

    def sleep(self, seconds: float) -> None:
        pass


def http_response(status: int, payload, headers: dict | None = None) -> bytes:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    h = {"Content-Type": "application/json", "Content-Length": str(len(body)), **(headers or {})}
    head = f"HTTP/1.1 {status} X\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
    return head.encode() + body
