"""
An in-memory web for the Wave-1 certification tests (no socket: httpx.MockTransport behind the real Fetcher, so the
real URL checks, robots.txt handling, redirects and byte budgets run). Hosts resolve to documentation-range public
addresses; nothing leaves the process.
"""

from __future__ import annotations

import gzip
import secrets
from datetime import timedelta
from typing import Optional

import httpx

from agents import RunContext, osei
from clock import FixedClock
from helpers import T0
from primitives import fetch as fetch_mod

ADDRS = {"site.test": "192.0.2.10", "www.site.test": "192.0.2.10", "other.test": "192.0.2.11"}


def _resp(status: int, body: bytes, headers: dict) -> httpx.Response:
    """A streamed (not pre-read) response, as a server's: the fetcher reads it raw."""
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


class Web:
    def __init__(self):
        self.routes: dict = {}
        self.requests: list = []

    def resolve(self, host: str, port: int) -> list:
        if host in ADDRS:
            return [ADDRS[host]]
        raise OSError("NXDOMAIN (test web)")

    def handle(self, request):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        path = request.url.raw_path.decode("ascii", "replace").split("?")[0]
        self.requests.append((host, path))
        r = self.routes.get((host, path))
        if r is None:
            return _resp(404, b"not found", {"content-type": "text/plain"})
        if callable(r):
            return r(request)
        status, body, headers = r
        return _resp(status, body if isinstance(body, bytes) else body.encode(), headers)

    def route(self, path: str, body, status: int = 200, ctype: str = "text/html; charset=utf-8",
              host: str = "site.test", **headers):
        self.routes[(host, path)] = (status, body, {"content-type": ctype, **{k.replace("_", "-"): v
                                                                             for k, v in headers.items()}})

    def fetcher(self, **kw) -> fetch_mod.Fetcher:
        args = {"timeout_s": 2, "max_bytes": 256 * 1024, "max_redirects": 3, "resolver": self.resolve,
                "policy": lambda host, ip, port: ip in ADDRS.values(), "transport": httpx.MockTransport(self.handle)}
        args.update(kw)
        return fetch_mod.Fetcher(**args)


def page(title: str = "Z Best Media | Revenue recovery", head: str = "", body: str = "", lang: str = "en",
         canonical: Optional[str] = "http://site.test/", ld: Optional[str] = None, h1: str = "Z Best Media") -> str:
    canon = f"<link rel='canonical' href='{canonical}'>" if canonical else ""
    ld_tag = f"<script type='application/ld+json'>{ld}</script>" if ld else ""
    text = body or ("<p>" + "Revenue recovery for consumer brands. " * 30 + "</p>")
    h1_tag = f"<h1>{h1}</h1>" if h1 is not None else ""
    return (f"<!doctype html><html lang='{lang}'><head><title>{title}</title>"
            f"<meta name='description' content='Revenue recovery.'>{canon}{ld_tag}{head}</head>"
            f"<body>{h1_tag}<h2>What we do</h2>{text}</body></html>")


ORG_LD = ('{"@context": "https://schema.org", "@type": "Organization", "name": "Z Best Media", '
          '"url": "http://site.test/", "sameAs": ["https://example.org/zbm"]}')


def sitemap(locs, lastmod: Optional[str] = "2026-10-01") -> str:
    lm = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
    urls = "".join(f"<url><loc>{x}</loc>{lm}</url>" for x in locs)
    return f"<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>{urls}" \
           "</urlset>"


def index(locs) -> str:
    kids = "".join(f"<sitemap><loc>{x}</loc></sitemap>" for x in locs)
    return f"<?xml version='1.0'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>{kids}" \
           "</sitemapindex>"


def site(web: Web, robots: str = "User-agent: *\nAllow: /\nSitemap: http://site.test/sitemap.xml\n",
         llms: Optional[str] = "# Z\n\n> R.\n\n## Pages\n- [Home](http://site.test/): start\n") -> Web:
    web.route("/robots.txt", robots, ctype="text/plain")
    web.route("/sitemap.xml", sitemap(["http://site.test/", "http://site.test/about"]), ctype="application/xml")
    if llms is not None:
        web.route("/llms.txt", llms, ctype="text/plain")
    web.route("/", page(ld=ORG_LD))
    web.route("/about", page(title="About Z Best Media", canonical="http://site.test/about", ld=ORG_LD))
    return web


def ctx(web: Web, paths=("/",), path_key: Optional[bytes] = None, **fk) -> RunContext:
    clock = FixedClock(T0)
    return RunContext(fetcher=web.fetcher(**fk), renderer=None, guard=lambda **k: None, clock=clock,
                      osei=osei.Osei(clock), domain="site.test", scheme="http", paths=list(paths), path_key=path_key)


def marker() -> str:
    """A synthetic planted value made at run time (never a real secret or personal datum)."""
    return "zq" + secrets.token_hex(6)


def gz(text: str) -> bytes:
    return gzip.compress(text.encode())


def day(n: float) -> timedelta:
    return timedelta(days=n)
