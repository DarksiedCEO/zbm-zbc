"""An in-process HTTP fixture server on 127.0.0.1 (an OS-assigned port, never a fixed one) for the fetch, agent and
audit tests: no internet. Routes are keyed by (host, path); the host is the request's Host header without the port, so
one server stands in for several sites. A route is a function ``(handler) -> None`` that writes the response, or a
tuple ``(status, headers, body)``."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from primitives import fetch as fetch_mod

NAMES = ("site.test", "other.test", "www.site.test", "competitor.test")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):          # quiet
        pass

    def do_GET(self):  # noqa: N802
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        self.server.seen.append({"host": host, "path": self.path, "ua": self.headers.get("User-Agent")})
        route = self.server.routes.get((host, self.path.split("?")[0])) or self.server.routes.get(("*", self.path))
        if route is None:
            self._send(404, {"Content-Type": "text/plain"}, b"not found")
            return
        if callable(route):
            try:
                route(self)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        status, headers, body = route
        self._send(status, headers, body)

    def _send(self, status: int, headers: dict, body: bytes):
        try:
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            if "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class FixtureServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.routes = {}
        self.httpd.seen = []
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def routes(self) -> dict:
        return self.httpd.routes

    @property
    def seen(self) -> list:
        return self.httpd.seen

    def url(self, host: str = "site.test", path: str = "/") -> str:
        return f"http://{host}:{self.port}{path}"

    def html(self, host: str, path: str, body: str, status: int = 200, headers=None):
        self.routes[(host, path)] = (status, {"Content-Type": "text/html; charset=utf-8", **(headers or {})},
                                     body.encode("utf-8"))

    def text(self, host: str, path: str, body: str, ctype: str = "text/plain", status: int = 200):
        self.routes[(host, path)] = (status, {"Content-Type": ctype}, body.encode("utf-8"))

    def redirect(self, host: str, path: str, location: str, status: int = 301):
        self.routes[(host, path)] = (status, {"Location": location, "Content-Type": "text/plain"}, b"")

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # --- name resolution and the address policy the tests use
    def resolver(self, extra: dict | None = None) -> Callable:
        table = {n: ["127.0.0.1"] for n in NAMES}
        table.update(extra or {})

        def resolve(host: str, port: int):
            if host in table:
                return list(table[host])
            return fetch_mod.system_resolver(host, port)    # numeric spellings, localhost: offline
        return resolve

    def policy(self) -> Callable:
        """Allow exactly the fixture's NAMED hosts on its port; everything else is judged by the production policy
        (so 127.0.0.1, localhost, [::1], 169.254.169.254, decimal spellings are refused as in production)."""
        def allow(host: str, ip: str, port: int) -> bool:
            if host in NAMES and ip == "127.0.0.1" and port == self.port:
                return True
            return fetch_mod.default_policy(host, ip, port)
        return allow

    def fetcher(self, **kw) -> fetch_mod.Fetcher:
        args = {"timeout_s": 1, "max_bytes": 256 * 1024, "max_redirects": 3, "resolver": self.resolver(),
                "policy": self.policy()}
        args.update(kw)
        return fetch_mod.Fetcher(**args)


def drip(seconds_between: float, total: int):
    """A route that sends headers, then one byte every ``seconds_between`` seconds."""
    def route(h):
        h.send_response(200)
        h.send_header("Content-Type", "text/html")
        h.send_header("Content-Length", str(total))
        h.end_headers()
        for _ in range(total):
            h.wfile.write(b"x")
            h.wfile.flush()
            time.sleep(seconds_between)
    return route


def stall(seconds: float):
    def route(h):
        time.sleep(seconds)
        h._send(200, {"Content-Type": "text/html"}, b"<html></html>")
    return route


def chunked(chunk: bytes, count: int, ctype: str = "text/html", encoding: str | None = None):
    def route(h):
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.send_header("Transfer-Encoding", "chunked")
        if encoding:
            h.send_header("Content-Encoding", encoding)
        h.end_headers()
        for _ in range(count):
            h.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        h.wfile.write(b"0\r\n\r\n")
    return route


def by_ua(bot_body: str, human_body: str):
    """A cloaking route: one page for our crawler, another for a browser."""
    def route(h):
        ua = h.headers.get("User-Agent") or ""
        body = bot_body if fetch_mod.PRODUCT_TOKEN in ua else human_body
        h._send(200, {"Content-Type": "text/html; charset=utf-8"}, body.encode())
    return route
