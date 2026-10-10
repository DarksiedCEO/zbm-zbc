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
  - only ports 80 and 443 (REFUSED_PORT otherwise);
  - the body is read RAW and decoded here, incrementally, never more than ``max_bytes`` decoded bytes (zlib
    ``max_length`` per step): at most one content coding, gzip or deflate; stacked or other codings are refused
    UNSUPPORTED_ENCODING; a MemoryError becomes RESOURCE_LIMIT, never an exception out of ``fetch``;
  - one HARD overall deadline over connect + TLS + headers + body for every hop (a socket backend bounds every
    socket operation by the time left), and the kill-switch guard is checked before every socket operation;
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
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpcore
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

STATES = ("OK", "REFUSED_URL", "REFUSED_PORT", "REFUSED_ADDRESS", "DNS_FAILED", "CONNECT_FAILED", "TLS_FAILED",
          "TIMEOUT", "TOO_LARGE", "UNSUPPORTED_ENCODING", "RESOURCE_LIMIT", "TOO_MANY_REDIRECTS", "BLOCKED_BY_ROBOTS",
          "PROTOCOL_ERROR", "KILLED")
# Only the web's standard ports are fetched (AEGIS L2): a URL or redirect naming any other port (an admin console, a
# database, a service on a non-web port of a public host) is refused REFUSED_PORT.
DEFAULT_PORTS = (80, 443)
# At most ONE content coding, and only these (AEGIS H1): each is decoded incrementally against the byte budget.
ENCODINGS = ("gzip", "x-gzip", "deflate")

_BLOCKED_V6 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"),
               ipaddress.ip_network("2002::/16"), ipaddress.ip_network("2001::/32"),
               ipaddress.ip_network("fec0::/10"))                      # deprecated site-local (AEGIS L1)
_BLOCKED_V4 = (ipaddress.ip_network("192.88.99.0/24"),)                # 6to4 relay anycast (AEGIS L1)


def public_address(ip: str) -> bool:
    """True only for a globally routable unicast address (and no embedded non-public IPv4)."""
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv4Address) and any(a in n for n in _BLOCKED_V4):
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


# ---------------------------------------------------------------------------------------------- hard deadline
# AEGIS M1: httpx's timeouts are per phase and per read, so a server trickling its HEADERS a byte at a time kept a
# fetch alive far past its budget. Every socket operation of a fetch now goes through this network backend, which
# bounds each connect / TLS handshake / read / write by the time left until the fetch's overall deadline and checks
# the kill-switch guard before each one: the whole fetch (connect + headers + body, every hop) ends at the deadline,
# and an engaged switch stops it at its next socket operation. The deadline and guard are per thread (a fetch runs
# on its caller's thread), so one backend instance serves every concurrent fetch.

class _Budget(threading.local):
    deadline: Optional[float] = None
    guard: Optional[Callable] = None


_BUDGET = _Budget()


def _left(timeout: Optional[float], exc: type) -> Optional[float]:
    d = _BUDGET.deadline
    if d is None:
        return timeout
    left = d - time.monotonic()
    if left <= 0:
        raise exc("the fetch's overall deadline has passed")
    return left if timeout is None else min(timeout, left)


def _guard_check() -> None:
    g = _BUDGET.guard
    if g is not None:
        g(capability="fetch", provider="web")


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(self, inner):
        self._s = inner

    def read(self, max_bytes: int, timeout: Optional[float] = None) -> bytes:
        _guard_check()
        return self._s.read(max_bytes, _left(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: Optional[float] = None) -> None:
        _guard_check()
        self._s.write(buffer, _left(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self._s.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return _DeadlineStream(self._s.start_tls(ssl_context, server_hostname, _left(timeout, httpcore.ConnectTimeout)))

    def get_extra_info(self, info: str):
        return self._s.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self):
        self._b = httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        _guard_check()
        return _DeadlineStream(self._b.connect_tcp(host, port, _left(timeout, httpcore.ConnectTimeout), local_address,
                                                   socket_options))

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are never used")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


_BACKEND = _DeadlineBackend()


def install_deadline_backend(transport: httpx.HTTPTransport) -> httpx.HTTPTransport:
    """Route ``transport``'s socket I/O through the deadline backend. httpx 0.28 has no public parameter for the
    network backend, so the pool's attribute is set; if httpx ever moves it, this refuses (the service will not run
    without a hard deadline) instead of silently fetching without one."""
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise RuntimeError("cannot install the fetch deadline backend on this httpx version; refusing to fetch")
    pool._network_backend = _BACKEND
    return transport


# ---------------------------------------------------------------------------------------------- bounded decoding

class _Decoder:
    """Raw body bytes -> decoded bytes, never more than ``limit`` decoded bytes in memory (AEGIS H1). Each step asks
    zlib for at most the remaining budget + 1 bytes (``max_length``), so a compression bomb costs at most the budget."""

    def __init__(self, encoding: Optional[str], limit: int):
        self.enc, self.limit, self.n, self.parts, self.d = encoding, limit, 0, [], None

    def _add(self, b: bytes) -> None:
        self.n += len(b)
        if self.n > self.limit:
            raise _Refused("TOO_LARGE", f"body larger than {self.limit} bytes (decoded)")
        self.parts.append(b)

    def feed(self, data: bytes) -> None:
        if not data:
            return
        if self.enc is None:
            self._add(data)
            return
        if self.d is None:
            if self.enc in ("gzip", "x-gzip"):
                wbits = 16 + zlib.MAX_WBITS
            else:   # "deflate" is zlib-wrapped by the RFC; some servers send raw deflate
                zlib_header = len(data) >= 2 and (data[0] & 0x0F) == 8 and ((data[0] << 8) | data[1]) % 31 == 0
                wbits = zlib.MAX_WBITS if zlib_header else -zlib.MAX_WBITS
            self.d = zlib.decompressobj(wbits)
        buf = data
        try:
            while buf and not self.d.eof:
                self._add(self.d.decompress(buf, self.limit - self.n + 1))
                buf = self.d.unconsumed_tail
        except zlib.error:
            raise _Refused("PROTOCOL_ERROR", "the body could not be decoded") from None

    def body(self) -> bytes:
        return b"".join(self.parts)


class Fetcher:
    connected = True

    def __init__(self, timeout_s: int = 10, max_bytes: int = 2 * 1024 * 1024, max_redirects: int = 5,
                 bot_info_url: Optional[str] = None, resolver: Callable = system_resolver,
                 policy: Callable = default_policy, transport: Optional[httpx.BaseTransport] = None,
                 verify: object = True, ports: tuple = DEFAULT_PORTS):
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        info = f"; +{bot_info_url}" if bot_info_url else ""
        self.user_agent = f"Mozilla/5.0 (compatible; {PRODUCT_TOKEN}/{VERSION}{info})"
        self.resolver = resolver
        self.policy = policy
        self.transport = install_deadline_backend(transport) if isinstance(transport, httpx.HTTPTransport) \
            else transport
        self.verify = verify
        self.ports = tuple(ports)

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
        if port not in self.ports:
            raise _Refused("REFUSED_PORT", "only the standard web ports are fetched")
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

    def _one(self, url: str, target: tuple, ua: str, deadline: float, accept: str
             ) -> tuple[int, dict, Optional[bytes], str]:
        """One request to the address ``target`` already vetted (resolved once per hop: no second answer is used)."""
        scheme, host, port, path, addrs = target
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
            transport = self.transport or install_deadline_backend(httpx.HTTPTransport(verify=self.verify,
                                                                                       trust_env=False))
            with httpx.Client(timeout=t, transport=transport, follow_redirects=False, trust_env=False) as client:
                with client.stream("GET", target, headers=headers, extensions=ext) as resp:
                    kept = {k: resp.headers.get(k)[:2000] for k in KEPT_HEADERS if resp.headers.get(k) is not None}
                    if 300 <= resp.status_code < 400 and resp.headers.get("location"):
                        return resp.status_code, kept, None, url
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        raise _Refused("TOO_LARGE", f"declared body larger than {self.max_bytes} bytes")
                    codings = [c.strip().lower() for c in (resp.headers.get("content-encoding") or "").split(",")
                               if c.strip() and c.strip().lower() != "identity"]
                    if len(codings) > 1 or (codings and codings[0] not in ENCODINGS):
                        raise _Refused("UNSUPPORTED_ENCODING", "only one content coding, gzip or deflate, is accepted")
                    dec = _Decoder(codings[0] if codings else None, self.max_bytes)
                    raw = 0
                    for chunk in resp.iter_raw():               # raw bytes: httpx decodes nothing (AEGIS H1)
                        raw += len(chunk)
                        if raw > self.max_bytes:
                            raise _Refused("TOO_LARGE", f"body larger than {self.max_bytes} bytes (on the wire)")
                        if time.monotonic() > deadline:
                            raise _Refused("TIMEOUT", "the response did not complete within the deadline")
                        dec.feed(chunk)
                    return resp.status_code, kept, dec.body(), url
        except _Refused:
            raise
        except httpx.TimeoutException:
            raise _Refused("TIMEOUT", "timed out") from None
        except MemoryError:
            raise _Refused("RESOURCE_LIMIT", "the response exhausted memory; refused") from None
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
        prev = (_BUDGET.deadline, _BUDGET.guard)
        if prev[0] is not None:                   # a nested fetch (robots.txt) never outlives the outer deadline
            deadline = min(deadline, prev[0])
        _BUDGET.deadline, _BUDGET.guard = deadline, guard
        current = url
        try:
            for hop in range(self.max_redirects + 1):
                if guard is not None:
                    guard(capability="fetch", provider="web")
                target = self._target(current)            # the URL and its addresses first, robots second
                if honor_robots and robots_cache is not None and not self._robots_allows(current, robots_cache,
                                                                                         guard):
                    raise _Refused("BLOCKED_BY_ROBOTS", f"robots.txt disallows this path for {PRODUCT_TOKEN}")
                status, headers, body, _ = self._one(current, target, user_agent, deadline, accept)
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
        except MemoryError:
            res.state, res.detail, res.final_url = "RESOURCE_LIMIT", "memory exhausted; refused", current
            return res
        finally:
            _BUDGET.deadline, _BUDGET.guard = prev

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
