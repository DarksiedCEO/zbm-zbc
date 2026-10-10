"""
Primitive: fetch (ADR 0017 decision 11). The one outbound web client of this service.

SSRF-safe by construction:
  - only ``http`` and ``https``; no userinfo; an IDNA host;
  - the host is resolved HERE, every resolved address is checked (loopback, private, link-local incl. the cloud
    metadata address 169.254.169.254, CGNAT, multicast, reserved, unspecified, IPv4-mapped / 6to4 / NAT64 IPv6
    embedding any of those: refused), and the connection is made to the CHECKED address (the URL carries the IP
    literal, Host and TLS SNI carry the name), so a DNS answer that changes between check and connect (rebinding)
    is never used;
  - numeric host spellings (``2130706433``, ``0x7f.1``, ``017700000001``) resolve to their address and are refused
    by the same check;
  - redirects are followed by hand, at most ``max_redirects``, every hop checked again;
  - the body is streamed and capped at ``max_bytes`` DECODED bytes (a compression bomb stops at the cap); an overall
    deadline as well as httpx's per-phase timeouts (a server dripping a byte at a time is cut off);
  - robots.txt (RFC 9309) is fetched and honoured for this crawler's own product token before any other path on
    that origin; the crawler always identifies itself (``ZBM-SEO-Audit``).
Anything fetched is DATA: nothing in a response is ever executed, followed as an instruction, or written anywhere
except as bounded extracts in a report.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from primitives import Killed
from primitives import robots as robots_mod

PRODUCT_TOKEN = "ZBM-SEO-Audit"
VERSION = "0.1"
ROBOTS_MAX_BYTES = 500 * 1024              # RFC 9309: crawlers must parse at least 500 KiB
KEPT_HEADERS = ("content-type", "content-length", "location", "x-robots-tag", "last-modified", "link",
                "cache-control", "vary", "server-timing", "retry-after")
# A browser-like identity used ONLY by access-diff to compare what a person's browser would be served. Labelled
# as such in every report; never used for crawling.
HUMAN_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36")

STATES = ("OK", "REFUSED_URL", "REFUSED_ADDRESS", "DNS_FAILED", "CONNECT_FAILED", "TLS_FAILED", "TIMEOUT",
          "TOO_LARGE", "TOO_MANY_REDIRECTS", "BLOCKED_BY_ROBOTS", "PROTOCOL_ERROR", "KILLED")

_BLOCKED_V6 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"),
               ipaddress.ip_network("2002::/16"), ipaddress.ip_network("2001::/32"))


def public_address(ip: str) -> bool:
    """True only for a globally routable unicast address (and no embedded non-public IPv4)."""
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address):
        if a.ipv4_mapped is not None:
            return public_address(str(a.ipv4_mapped))
        if any(a in n for n in _BLOCKED_V6):
            return False                 # 6to4 / Teredo / NAT64: an embedded IPv4 we will not reason about
    return bool(a.is_global and not a.is_multicast and not a.is_reserved and not a.is_unspecified)


def default_policy(host: str, ip: str, port: int) -> bool:
    return public_address(ip)


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos})


@dataclass
class FetchResult:
    url: str
    state: str
    final_url: Optional[str] = None
    status: Optional[int] = None
    headers: dict = field(default_factory=dict)
    body: Optional[bytes] = None
    content_type: Optional[str] = None
    charset: Optional[str] = None
    redirects: list = field(default_factory=list)        # [{"url", "status"}] in order
    detail: Optional[str] = None
    user_agent: str = "bot"                              # "bot" (ours) or "human" (access-diff only)

    @property
    def ok(self) -> bool:
        return self.state == "OK"

    def text(self) -> str:
        if self.body is None:
            return ""
        enc = self.charset or "utf-8"
        try:
            return self.body.decode(enc, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def summary(self) -> dict:
        """What a report may carry: no body."""
        return {"url": self.url, "final_url": self.final_url, "state": self.state, "status": self.status,
                "content_type": self.content_type, "redirects": list(self.redirects), "detail": self.detail,
                "bytes": len(self.body) if self.body is not None else None,
                "x_robots_tag": self.headers.get("x-robots-tag")}


class _Refused(Exception):
    def __init__(self, state: str, detail: str):
        super().__init__(detail)
        self.state = state
        self.detail = detail


def _content_type(headers: dict) -> tuple[Optional[str], Optional[str]]:
    raw = headers.get("content-type")
    if not raw:
        return None, None
    parts = [p.strip() for p in raw.split(";")]
    ctype = parts[0].lower() or None
    charset = None
    for p in parts[1:]:
        k, _, v = p.partition("=")
        if k.strip().lower() == "charset":
            charset = v.strip().strip('"').lower()[:40] or None
    return ctype, charset


class Fetcher:
    connected = True

    def __init__(self, timeout_s: int = 10, max_bytes: int = 2 * 1024 * 1024, max_redirects: int = 5,
                 bot_info_url: Optional[str] = None, resolver: Callable = system_resolver,
                 policy: Callable = default_policy, transport: Optional[httpx.BaseTransport] = None,
                 verify: object = True):
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        info = f"; +{bot_info_url}" if bot_info_url else ""
        self.user_agent = f"Mozilla/5.0 (compatible; {PRODUCT_TOKEN}/{VERSION}{info})"
        self.resolver = resolver
        self.policy = policy
        self.transport = transport
        self.verify = verify

    @classmethod
    def from_settings(cls, settings) -> "Fetcher":
        return cls(timeout_s=settings.fetch_timeout_s, max_bytes=settings.fetch_max_bytes,
                   max_redirects=settings.fetch_max_redirects, bot_info_url=settings.bot_info_url)

    # ------------------------------------------------------------------ URL and address checks

    def _target(self, url: str) -> tuple[str, str, int, str, list[str]]:
        """(scheme, host, port, path+query, vetted addresses) or _Refused."""
        if not isinstance(url, str) or len(url) > 2048 or any(c in url for c in "\r\n\t\x00 "):
            raise _Refused("REFUSED_URL", "malformed URL")
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise _Refused("REFUSED_URL", "malformed URL") from None
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise _Refused("REFUSED_URL", "only http and https are fetched")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise _Refused("REFUSED_URL", "credentials in a URL are refused")
        host = (parts.hostname or "").rstrip(".")
        if not host:
            raise _Refused("REFUSED_URL", "no host")
        try:
            host = host.encode("idna").decode("ascii").lower() if not _is_ip_literal(host) else host
        except UnicodeError:
            raise _Refused("REFUSED_URL", "host is not a valid domain name") from None
        port = port or (443 if scheme == "https" else 80)
        try:
            addrs = list(self.resolver(host, port))
        except (OSError, UnicodeError, ValueError):
            raise _Refused("DNS_FAILED", "the host does not resolve") from None
        if not addrs:
            raise _Refused("DNS_FAILED", "the host does not resolve")
        bad = [a for a in addrs if not self.policy(host, a, port)]
        if bad:
            raise _Refused("REFUSED_ADDRESS", "the host resolves to a non-public address; refused (SSRF guard)")
        path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
        return scheme, host, port, path, addrs

    # ------------------------------------------------------------------ one request (no redirects)

    def _one(self, url: str, ua: str, deadline: float, accept: str) -> tuple[int, dict, Optional[bytes], str]:
        scheme, host, port, path, addrs = self._target(url)
        ip = addrs[0]
        lit = f"[{ip}]" if ":" in ip else ip
        default = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
        target = f"{scheme}://{lit}:{port}{path}"
        host_header = host if default else f"{host}:{port}"
        headers = {"Host": host_header, "User-Agent": ua, "Accept": accept,
                   "Accept-Encoding": "gzip, deflate", "Connection": "close"}
        ext = {"sni_hostname": host} if scheme == "https" else {}
        t = httpx.Timeout(self.timeout_s)
        try:
            with httpx.Client(timeout=t, transport=self.transport, verify=self.verify, follow_redirects=False,
                              trust_env=False) as client:
                with client.stream("GET", target, headers=headers, extensions=ext) as resp:
                    kept = {k: resp.headers.get(k)[:2000] for k in KEPT_HEADERS if resp.headers.get(k) is not None}
                    if 300 <= resp.status_code < 400 and resp.headers.get("location"):
                        return resp.status_code, kept, None, url
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        raise _Refused("TOO_LARGE", f"declared body larger than {self.max_bytes} bytes")
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise _Refused("TOO_LARGE", f"body larger than {self.max_bytes} bytes (decoded)")
                        if time.monotonic() > deadline:
                            raise _Refused("TIMEOUT", "the response did not complete within the deadline")
                        chunks.append(chunk)
                    return resp.status_code, kept, b"".join(chunks), url
        except _Refused:
            raise
        except httpx.TimeoutException:
            raise _Refused("TIMEOUT", "timed out") from None
        except httpx.ConnectError as exc:
            if isinstance(exc.__cause__, ssl.SSLError) or "SSL" in str(exc) or "certificate" in str(exc).lower():
                raise _Refused("TLS_FAILED", "TLS handshake or certificate check failed") from None
            raise _Refused("CONNECT_FAILED", "connection failed") from None
        except httpx.RemoteProtocolError as exc:
            if "location header" in str(exc).lower():       # httpx refuses to parse the redirect target itself
                raise _Refused("REFUSED_URL", "the redirect target is not a valid URL") from None
            raise _Refused("PROTOCOL_ERROR", "protocol error: RemoteProtocolError") from None
        except (httpx.DecodingError, httpx.LocalProtocolError) as exc:
            raise _Refused("PROTOCOL_ERROR", f"protocol error: {type(exc).__name__}") from None
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise _Refused("CONNECT_FAILED", f"request failed: {type(exc).__name__}") from None

    # ------------------------------------------------------------------ public

    def fetch(self, url: str, *, ua: str = "bot", robots_cache: Optional[dict] = None, honor_robots: bool = True,
              guard: Optional[Callable] = None, accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,"
                                                              "text/plain;q=0.8,*/*;q=0.5") -> FetchResult:
        """GET ``url`` with redirects followed by hand. ``ua``: "bot" (ours) or "human" (access-diff only).
        ``guard`` is the kill-switch check (raises to stop). Never raises for a network outcome."""
        user_agent = self.user_agent if ua == "bot" else HUMAN_UA
        res = FetchResult(url=url, state="OK", user_agent=ua)
        deadline = time.monotonic() + 2 * self.timeout_s
        current = url
        try:
            for hop in range(self.max_redirects + 1):
                if guard is not None:
                    guard(capability="fetch", provider="web")
                if honor_robots and robots_cache is not None and not self._robots_allows(current, robots_cache,
                                                                                         guard):
                    raise _Refused("BLOCKED_BY_ROBOTS", f"robots.txt disallows this path for {PRODUCT_TOKEN}")
                status, headers, body, _ = self._one(current, user_agent, deadline, accept)
                if 300 <= status < 400 and headers.get("location"):
                    nxt = urljoin(current, headers["location"])
                    res.redirects.append({"url": current, "status": status})
                    current = nxt
                    if hop == self.max_redirects:
                        raise _Refused("TOO_MANY_REDIRECTS", f"more than {self.max_redirects} redirects")
                    continue
                res.final_url, res.status, res.headers, res.body = current, status, headers, body
                res.content_type, res.charset = _content_type(headers)
                return res
            raise _Refused("TOO_MANY_REDIRECTS", f"more than {self.max_redirects} redirects")
        except _Refused as r:
            res.state, res.detail, res.final_url = r.state, r.detail, current
            return res
        except Killed as k:                               # a kill switch engaged (or the service closed)
            res.state, res.detail, res.final_url = "KILLED", k.code, current
            return res

    def _robots_allows(self, url: str, cache: dict, guard: Optional[Callable]) -> bool:
        p = urlsplit(url)
        origin = f"{p.scheme.lower()}://{(p.netloc or '').lower()}"
        if origin not in cache:
            cache[origin] = self.robots(origin, guard=guard)
        rb = cache[origin]
        path = urlunsplit(("", "", p.path or "/", p.query, ""))
        return rb["parsed"].allowed(PRODUCT_TOKEN, path) if rb["parsed"] is not None else rb["default_allow"]

    def robots(self, origin: str, guard: Optional[Callable] = None) -> dict:
        """RFC 9309 section 2.3.1: 2xx -> parse (first ROBOTS_MAX_BYTES); 4xx -> unavailable, allow all; 5xx or
        unreachable -> disallow all. Redirects followed (up to max_redirects, every hop checked)."""
        r = self.fetch(origin + "/robots.txt", honor_robots=False, guard=guard, accept="text/plain,*/*;q=0.5")
        out = {"origin": origin, "fetch": r.summary(), "parsed": None, "default_allow": False, "status_class": None}
        if r.state == "TOO_LARGE":
            out["status_class"] = "too_large"          # cannot read the whole file: treat as unreachable (fail closed)
            return out
        if r.state != "OK":
            out["status_class"] = "unreachable"
            return out
        if 200 <= r.status < 300:
            out["parsed"] = robots_mod.parse(r.body[:ROBOTS_MAX_BYTES].decode("utf-8", errors="replace"))
            out["status_class"] = "ok"
            out["text_sha256"] = hashlib.sha256(r.body).hexdigest()
        elif 400 <= r.status < 500:
            out["default_allow"] = True
            out["status_class"] = "unavailable"
        else:
            out["status_class"] = "unreachable"
        return out


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False
