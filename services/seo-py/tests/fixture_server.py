"""An in-process HTTP fixture server on 127.0.0.1 (an OS-assigned port, never a fixed one) for the fetch, agent and
audit tests: no internet. Routes are keyed by (host, path); the host is the request's Host header without the port, so
one server stands in for several sites. A route is a function ``(handler) -> None`` that writes the response, or a
tuple ``(status, headers, body)``."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import httpx

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
            if host in NAMES and ip == "127.0.0.1" and port in (self.port, 80):
                return True
            return fetch_mod.default_policy(host, ip, port)
        return allow

    def site_fetcher(self, **kw) -> fetch_mod.Fetcher:
        """A fetcher for the audit-level tests: ``http://site.test/`` (port 80) reaches this server — a test-only
        transport rewrites the port of a request to the CHECKED loopback address. Production code is unchanged."""
        return self.fetcher(transport=PortRewrite(self.port), **kw)

    def fetcher(self, **kw) -> fetch_mod.Fetcher:
        args = {"timeout_s": 1, "max_bytes": 256 * 1024, "max_redirects": 3, "resolver": self.resolver(),
                "policy": self.policy(), "ports": (80, 443, self.port)}
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


class PortRewrite(httpx.HTTPTransport):
    def __init__(self, port: int):
        super().__init__()
        self.port = port

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "127.0.0.1" and request.url.port in (None, 80):
            request.url = request.url.copy_with(port=self.port)
        return super().handle_request(request)


ORG_LD = ('{"@context":"https://schema.org","@type":"LocalBusiness","name":"Z Best Media","url":"http://site.test/",'
          '"telephone":"(562) 248-6617","sameAs":["https://social.example/zbm"],"address":{"@type":"PostalAddress",'
          '"streetAddress":"5318 East 2nd Street","addressLocality":"Long Beach","addressRegion":"CA"}}')
BODY_TEXT = "<p>" + ("Z Best Media helps brands recover revenue across marketplaces. " * 12) + "</p>"


def home_html(title: str = "Z Best Media | Revenue recovery", ld: str | None = ORG_LD, extra_head: str = "",
              body: str = BODY_TEXT, canonical: str | None = "http://site.test/") -> str:
    canon = f"<link rel='canonical' href='{canonical}'>" if canonical else ""
    ld_tag = f"<script type='application/ld+json'>{ld}</script>" if ld else ""
    return (f"<!doctype html><html lang='en'><head><title>{title}</title>"
            f"<meta name='description' content='Revenue recovery for brands.'>{canon}{ld_tag}{extra_head}</head>"
            f"<body><h1>Z Best Media</h1><h2>What we do</h2>{body}</body></html>")


def install_site(srv, robots: str | None = "User-agent: *\nAllow: /\nSitemap: http://site.test/sitemap.xml\n",
                 sitemap: str | None = None, llms: str | None = "# Z Best Media\n\n> Revenue recovery.\n\n## Pages\n"
                 "- [Home](http://site.test/): start here\n", home: str | None = None):
    if robots is not None:
        srv.text("site.test", "/robots.txt", robots)
    if sitemap is None:
        sitemap = ("<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                   "<url><loc>http://site.test/</loc><lastmod>2026-10-01</lastmod></url>"
                   "<url><loc>http://site.test/about</loc><lastmod>2026-09-12T10:00:00Z</lastmod></url></urlset>")
    srv.text("site.test", "/sitemap.xml", sitemap, ctype="application/xml")
    if llms is not None:
        srv.text("site.test", "/llms.txt", llms)
    srv.html("site.test", "/", home if home is not None else home_html())
    srv.html("site.test", "/about", home_html(title="About Z Best Media", canonical="http://site.test/about"))
