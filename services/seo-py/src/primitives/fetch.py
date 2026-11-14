"""
Primitive: fetch (ADR 0017 decision 11). The one outbound web client of this service.

SSRF-safe by construction:
  - only ``http`` and ``https``; no userinfo; the host is an IP literal or, after IDNA, plain LDH labels (anything
    else, a control character or a space among them, is refused before any resolver call);
  - the host is resolved HERE, every resolved address is checked (loopback, private, link-local incl. the cloud
    metadata address 169.254.169.254, CGNAT, multicast, reserved, unspecified, IPv4-mapped / 6to4 / NAT64 IPv6
    embedding any of those: refused), and the connection is made to the CHECKED address (the URL carries the IP
    literal, Host and TLS SNI carry the name), so a DNS answer that changes between check and connect (rebinding)
    is never used; the checked addresses are tried IPv4 first, at most MAX_ADDRESS_ATTEMPTS, the next only when no
    connection to the previous one was made (AEGIS A2 H-1: a host with no outbound IPv6 still reaches dual-stack sites);
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
import logging
import re
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
from primitives import safe_url as safe_url_mod

# review c0c09bd1: a fetched URL (an operator's audit path, a cited URL) is never written to a log line: httpx /
# httpcore log every request URL at INFO / DEBUG, so their loggers are held at WARNING
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

PRODUCT_TOKEN = "ZBM-SEO-Audit"
VERSION = "0.1"
ROBOTS_MAX_BYTES = 500 * 1024              # RFC 9309: crawlers must parse at least 500 KiB
# AEGIS Wave-1 M-S1, round 2 M2: this crawler's own robots.txt checks in one run (one robots cache per origin) share
# this COUNTED work (primitives/robots.py ``Work``; no clock, so a slow network never spends it); a path left
# undecided once it is spent is not fetched, with its own state ROBOTS_CHECK_INCOMPLETE (never BLOCKED_BY_ROBOTS).
ROBOTS_CHECK_WORK = robots_mod.STEP_WORK_UNITS
KEPT_HEADERS = ("content-type", "content-length", "location", "x-robots-tag", "last-modified", "link",
                "cache-control", "vary", "server-timing", "retry-after")
# A browser-like identity used ONLY by access-diff to compare what a person's browser would be served. Labelled
# as such in every report; never used for crawling.
HUMAN_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36")

STATES = ("OK", "REFUSED_URL", "REFUSED_PORT", "REFUSED_ADDRESS", "DNS_FAILED", "CONNECT_FAILED", "TLS_FAILED",
          "TIMEOUT", "TOO_LARGE", "UNSUPPORTED_ENCODING", "RESOURCE_LIMIT", "TOO_MANY_REDIRECTS", "BLOCKED_BY_ROBOTS",
          "ROBOTS_CHECK_INCOMPLETE", "PROTOCOL_ERROR", "KILLED", "REDIRECT_TOO_LONG")
# B-FT-04: a Location header is used WHOLE up to this many octets (RFC 9110 recommends at least 8,000); a longer one
# is never followed (REDIRECT_TOO_LONG). A URL this service fetches is at most URL_MAX characters.
LOCATION_MAX = 8192
URL_MAX = 16_384
# B-RB-08 / B-SE-03: robots.txt is read to its parse limit of DECODED octets; the bytes on the wire are bounded only
# by this (and the deadline), so a body that decodes small (gzip of empty members) is read, never refused
ROBOTS_WIRE_MAX = 64 * 1024 * 1024
DECODE_STEP = 1024 * 1024        # A-11: the most one zlib call returns (output is streamed in steps of this size)
# Only the web's standard ports are fetched (AEGIS L2): a URL or redirect naming any other port (an admin console, a
# database, a service on a non-web port of a public host) is refused REFUSED_PORT.
DEFAULT_PORTS = (80, 443)
# At most ONE content coding, and only these (AEGIS H1): each is decoded incrementally against the byte budget.
ENCODINGS = ("gzip", "x-gzip", "deflate")

_BLOCKED_V6 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"),
               ipaddress.ip_network("2002::/16"), ipaddress.ip_network("2001::/32"),
               ipaddress.ip_network("fec0::/10"))                      # deprecated site-local (AEGIS L1)
_BLOCKED_V4 = (ipaddress.ip_network("192.88.99.0/24"),)                # 6to4 relay anycast (AEGIS L1)
# AEGIS A2 H-1: a host's vetted addresses are tried in turn (IPv4 first, families alternating), at most this many, and
# the next one only when no TCP connection to the previous one was made (nothing was sent to it).
MAX_ADDRESS_ATTEMPTS = 4


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
    refused_redirect: Optional[dict] = None              # WR-F014: {"status", "location"} of a refused redirect
    # TOO_LARGE (AEGIS Wave-1 M-O4): the decoded bytes read before the fetch was stopped (the body is not kept):
    # {"bytes_read": n, "sha256": of those bytes, "declared": Content-Length or None}
    partial: Optional[dict] = None
    truncated: bool = False                              # the body was cut at the caller's ``truncate_at``
    # BLOCKED_BY_ROBOTS: why ("rule": a rule in the file disallows ZBM-SEO-Audit, stored in its safe form; or
    # "unreachable": robots.txt answered 5xx / could not be fetched, RFC 9309 complete disallow)
    robots: Optional[dict] = None
    status_seen: bool = False                            # a response status line arrived before the fetch stopped

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
        """What a report may carry: no body, and every URL in its safe form (primitives/safe_url.py: templated path,
        no query, fragment or userinfo; AEGIS Wave-1 M-S2). ``x_robots_tag`` is reduced to the directives and bot
        tokens this service knows (``safe_url.safe_x_robots``)."""
        return {"url": safe_url_mod.safe_url(self.url), "final_url": safe_url_mod.safe_url(self.final_url),
                "state": self.state, "status": self.status, "content_type": safe_url_mod.safe_ctype(self.content_type),
                "redirects": [{"url": safe_url_mod.safe_url(r.get("url")), "status": r.get("status")}
                              for r in self.redirects], "detail": self.detail,
                "bytes": len(self.body) if self.body is not None else None, "truncated": self.truncated,
                "x_robots_tag": safe_url_mod.safe_x_robots(self.headers.get("x-robots-tag"))}


class SinkStop(Exception):
    """Raised by a streamed fetch's sink: it has read what it needs (or refuses the rest); the fetch ends there
    with the status it got (the body is the sink's)."""


class _Refused(Exception):
    def __init__(self, state: str, detail: str, partial: Optional[dict] = None, robots: Optional[dict] = None):
        super().__init__(detail)
        self.state = state
        self.detail = detail
        self.partial = partial
        self.robots = robots


class _RefusedRedirect(_Refused):
    """WR-F014: a 3xx whose Location httpx could not parse or encode (a raw-Unicode or malformed host). The hop and
    its target are recorded, never dropped, so a caller can classify where the redirect pointed."""

    def __init__(self, state: str, detail: str, status: int, location: str):
        super().__init__(state, detail)
        self.status, self.location = status, location


class _NotConnected(_Refused):
    """No TCP connection to this address was made, so nothing was sent to it: the next vetted address may be tried."""


def _refused_redirect(seen: dict, detail: str) -> None:
    """Raise _RefusedRedirect when the response that failed was a redirect with a Location (WR-F014)."""
    status, loc = seen.get("status"), seen.get("location")
    if isinstance(status, int) and 300 <= status < 400 and isinstance(loc, str) and loc:
        raise _RefusedRedirect("REFUSED_URL", detail, status, loc[:2000]) from None


def _connect_order(addrs: list[str]) -> list[str]:
    """IPv4 first, then the families alternate (RFC 8305 section 4 style), each family in the resolver's order."""
    v4 = [a for a in addrs if ":" not in a]
    v6 = [a for a in addrs if ":" in a]
    out = [a for pair in zip(v4, v6) for a in pair] + v4[len(v6):] + v6[len(v4):]
    return list(dict.fromkeys(out))


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
    connected: bool = False          # a TCP connection of the current address attempt is up (AEGIS A2 H-1)
    truncate_at: Optional[int] = None   # read at most this many decoded bytes and keep them (robots.txt, RFC 9309 2.5)
    truncated: bool = False             # the body was cut at ``truncate_at``
    sink: Optional[Callable] = None     # streamed fetch: decoded bytes go here, the body is not kept (sitemaps)
    max_bytes: Optional[int] = None     # this fetch's decoded cap when it is not the fetcher's (sitemaps, Q-07)
    status_seen: bool = False


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
        stream = self._b.connect_tcp(host, port, _left(timeout, httpcore.ConnectTimeout), local_address, socket_options)
        _BUDGET.connected = True
        return _DeadlineStream(stream)

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

    def __init__(self, encoding: Optional[str], limit: int, keep_prefix: bool = False, sink: Optional[Callable] = None):
        self.enc, self.limit, self.n, self.parts, self.d = encoding, limit, 0, [], None
        self.keep_prefix = keep_prefix
        self.sink = sink                        # streamed: decoded bytes go to the sink, nothing is kept here
        # B-OS-01 (rv-wave1v2 #5): a streamed read keeps no bytes, so the size and SHA-256 of what was decoded and
        # handed on are counted here, for the quarantine record of a stop (never an empty-body hash)
        self.sunk, self.sunk_n = hashlib.sha256(), 0

    def _add(self, b: bytes) -> None:
        self.n += len(b)
        if self.n > self.limit:
            if self.keep_prefix:                # keep exactly the first ``limit`` bytes, then stop
                self.parts.append(b[:len(b) - (self.n - self.limit)])
            raise _Refused("TOO_LARGE", f"body larger than {self.limit} bytes (decoded)")
        if self.sink is not None:
            if b:
                self.sunk.update(b)
                self.sunk_n += len(b)
                self.sink(b)
            return
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
            while True:
                # A-11 (rv-wave1v2 #4): each zlib call returns at most DECODE_STEP bytes (and never more than the
                # budget + 1), so one call never holds the whole remaining budget in memory; output zlib still holds
                # (a full step with all input consumed) is drained before the next chunk
                out = self.d.decompress(buf, min(DECODE_STEP, self.limit - self.n + 1))
                self._add(out)
                buf = self.d.unconsumed_tail
                if not self.d.eof:
                    if not buf and len(out) < DECODE_STEP:
                        break
                    continue
                # AEGIS N2: a gzip body may hold several members; each is decoded, all within the same budget
                rest = self.d.unused_data
                if not rest or self.enc not in ("gzip", "x-gzip"):
                    break
                self.d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                buf = rest
        except zlib.error:
            raise _Refused("PROTOCOL_ERROR", "the body could not be decoded") from None

    def end(self) -> None:
        """The body is over: a content coding that did not reach its end (a truncated gzip / deflate stream) is
        PROTOCOL_ERROR, never an empty or partial body (B-FT-05, A-09)."""
        if self.d is not None and not self.d.eof:
            raise _Refused("PROTOCOL_ERROR", "the body could not be decoded (truncated coding)")

    def body(self) -> bytes:
        return b"".join(self.parts)

    def read_so_far(self) -> dict:
        """The ``partial`` of a stop (B-OS-01 ``read_before_stop``): size and SHA-256 of the decoded bytes read."""
        if self.sink is not None:
            return {"bytes_read": self.sunk_n, "sha256": self.sunk.hexdigest(), "declared": None}
        got = self.body()
        return {"bytes_read": len(got), "sha256": hashlib.sha256(got).hexdigest(), "declared": None}


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
        if not isinstance(url, str) or len(url) > URL_MAX or any(c in url for c in "\r\n\t\x00 "):
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
        if _is_ip_literal(host):
            if "%" in host:                       # a scoped (zone id) address is never a public one; never resolved
                raise _Refused("REFUSED_URL", "an IPv6 zone id is refused")
            host = str(ipaddress.ip_address(host.strip("[]")))
        else:
            try:
                host = host.encode("idna").decode("ascii").lower()
            except UnicodeError:
                raise _Refused("REFUSED_URL", "host is not a valid domain name") from None
            # AEGIS SEO Wave 3 round 2 (host strictness): after IDNA a name is plain LDH labels or it is refused here,
            # before any resolver call: no whitespace or control character (``127.0.0.1\x0bevil``; glibc's inet_aton
            # reads ``0177.0.0.1\x0b`` as 127.0.0.1), no Unicode space that IDNA maps to an ASCII space, no empty
            # label. A real IDN is xn-- labels and passes. An underscore is refused too: it is not a host name
            # character (RFC 952 / RFC 1123 section 2.1); DNS carries it only in service labels (``_dmarc``,
            # ``_sip._tcp``) that are never a web origin, and a site published under one is reported REFUSED_URL
            # (visible in the report), not fetched.
            if not _ldh_host(host):
                raise _Refused("REFUSED_URL", "host is not a valid domain name")
        if _numeric_host_not_canonical(host):
            raise _Refused("REFUSED_URL", "a numeric host must be a canonical IP address")
        if port is None:                          # B-FT-01: an explicit port 0 is not the default port
            port = 443 if scheme == "https" else 80
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
        """One request to an address of ``target``, every one already vetted (resolved once per hop: no second answer
        is used). AEGIS A2 H-1: the addresses are tried IPv4 first, families alternating, at most MAX_ADDRESS_ATTEMPTS;
        the next only when no TCP connection to the previous one was made (refused, unreachable, connect timeout:
        nothing was sent) and only while the fetch's one deadline has time left. Each attempt's connect timeout is a
        share of the time left, so every address tried gets a turn and the deadline is never exceeded. An HTTP answer
        of any status, a TLS failure, or any failure after the connection is up ends the request: no other address."""
        order = _connect_order(target[4])[:MAX_ADDRESS_ATTEMPTS]
        last: Optional[_Refused] = None
        for i, ip in enumerate(order):
            left = deadline - time.monotonic()
            if i and left <= 0:
                break
            connect_s = min(self.timeout_s, left / (len(order) - i)) if left > 0 else self.timeout_s
            try:
                return self._attempt(url, target, ip, ua, deadline, accept, connect_s)
            except _NotConnected as exc:
                last = exc
        raise _Refused(last.state, last.detail, last.partial)

    def _attempt(self, url: str, target: tuple, ip: str, ua: str, deadline: float, accept: str, connect_s: float
                 ) -> tuple[int, dict, Optional[bytes], str]:
        """One request to the vetted address ``ip`` of ``target`` (the URL carries the IP literal; Host and SNI the
        name). Raises _NotConnected when no TCP connection was made."""
        scheme, host, port, path, _addrs = target
        lit = f"[{ip}]" if ":" in ip else ip
        default = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
        target = f"{scheme}://{lit}:{port}{path}"
        name = f"[{host}]" if ":" in host else host               # an IPv6 literal is bracketed in Host
        host_header = name if default else f"{name}:{port}"
        headers = {"Host": host_header, "User-Agent": ua, "Accept": accept,
                   "Accept-Encoding": "gzip, deflate", "Connection": "close"}
        ext = {"sni_hostname": host} if scheme == "https" else {}
        t = httpx.Timeout(self.timeout_s, connect=connect_s)
        _BUDGET.connected = False
        try:
            transport = self.transport or install_deadline_backend(httpx.HTTPTransport(verify=self.verify,
                                                                                       trust_env=False))
            seen: dict = {}

            def _saw(r):                                   # runs before httpx parses the Location (WR-F014)
                seen["status"], seen["location"] = r.status_code, r.headers.get("location")
            with httpx.Client(timeout=t, transport=transport, follow_redirects=False, trust_env=False,
                              event_hooks={"response": [_saw]}) as client:
                with client.stream("GET", target, headers=headers, extensions=ext) as resp:
                    _BUDGET.status_seen = True
                    kept = {k: resp.headers.get(k)[:2000] for k in KEPT_HEADERS if resp.headers.get(k) is not None}
                    xr = resp.headers.get_list("x-robots-tag")
                    if xr:                                # each header line on its own (safe_url.HEADER_SEP)
                        kept["x-robots-tag"] = safe_url_mod.HEADER_SEP.join(v[:2000] for v in xr[:50])
                    if 300 <= resp.status_code < 400 and resp.headers.get("location"):
                        loc = resp.headers.get("location")
                        if len(loc.encode("utf-8", "surrogateescape")) > LOCATION_MAX:
                            raise _Refused("REDIRECT_TOO_LONG", f"the Location header is longer than {LOCATION_MAX} "
                                                                "octets; not followed")
                        kept["location"] = loc            # B-FT-04: the whole value is followed, never a cut one
                        return resp.status_code, kept, None, url
                    declared = resp.headers.get("content-length")
                    cut = _BUDGET.truncate_at
                    # a streamed read (sink) is bounded by its own wire and decoded caps and measures the body
                    # itself, so a declared length does not refuse it unread (B-DE-03: a plain over-limit sitemap
                    # is measured TOO_LARGE, never refused before a byte is read)
                    if cut is None and _BUDGET.sink is None and declared and declared.isdigit() and \
                            int(declared) > (_BUDGET.max_bytes or self.max_bytes):
                        raise _Refused("TOO_LARGE", f"declared body larger than {self.max_bytes} bytes",
                                       {"bytes_read": 0, "sha256": hashlib.sha256(b"").hexdigest(),
                                        "declared": int(declared) if len(declared) < 19 else None})
                    codings = [c.strip().lower() for c in (resp.headers.get("content-encoding") or "").split(",")
                               if c.strip() and c.strip().lower() != "identity"]
                    if len(codings) > 1 or (codings and codings[0] not in ENCODINGS):
                        raise _Refused("UNSUPPORTED_ENCODING", "only one content coding, gzip or deflate, is accepted")
                    sink = _BUDGET.sink
                    limit = cut if cut is not None else (_BUDGET.max_bytes or self.max_bytes)
                    dec = _Decoder(codings[0] if codings else None, limit, keep_prefix=cut is not None, sink=sink)
                    raw = 0
                    try:
                      try:
                        for chunk in resp.iter_raw():           # raw bytes: httpx decodes nothing (AEGIS H1)
                            raw += len(chunk)
                            wire_max = max(self.max_bytes, ROBOTS_WIRE_MAX) if cut is not None else \
                                (_BUDGET.max_bytes or self.max_bytes)
                            if raw > wire_max:
                                if sink is not None:
                                    # B-DE-03 (rv-wave1v2 #3): a streamed read measures up to its wire cap, whatever
                                    # the chunking: the octets within the cap go to the sink first (which may stop
                                    # the read itself, e.g. a plain sitemap over the protocol limit)
                                    dec.feed(chunk[:len(chunk) - (raw - wire_max)])
                                raise _Refused("TOO_LARGE", "body larger than this service reads (on the wire)")
                            if time.monotonic() > deadline:
                                raise _Refused("TIMEOUT", "the response did not complete within the deadline")
                            dec.feed(chunk)
                        dec.end()
                      except SinkStop:
                        return resp.status_code, kept, b"", url
                    except _Refused as r:
                        if r.state == "TOO_LARGE" and cut is not None and dec.n > cut:
                            _BUDGET.truncated = True            # the first ``cut`` bytes are the body (RFC 9309 2.5)
                            return resp.status_code, kept, dec.body(), url
                        if r.state == "TOO_LARGE" and r.partial is None:
                            r.partial = dec.read_so_far()       # what was read before the stop (bounded)
                            if declared and declared.isdigit() and len(declared) < 19:
                                r.partial["declared"] = int(declared)
                        raise
                    return resp.status_code, kept, dec.body(), url
        except _Refused:
            raise
        except httpx.InvalidURL:
            # WR-F008: httpx parses a redirect's Location itself (``response.next_request``) even when it does not
            # follow it, and raises InvalidURL (not an HTTPError) for one it cannot parse (``javascript:alert(1)``)
            loc = (seen if "seen" in locals() else {}).get("location") or ""
            if len(loc.encode("utf-8", "surrogateescape")) > LOCATION_MAX:
                raise _Refused("REDIRECT_TOO_LONG", f"the Location header is longer than {LOCATION_MAX} octets; "
                                                    "not followed") from None
            _refused_redirect(seen if "seen" in locals() else {}, "the redirect target is not a valid URL")
            raise _Refused("REFUSED_URL", "the redirect target is not a valid URL") from None
        except httpx.StreamError:
            raise _Refused("PROTOCOL_ERROR", "protocol error: StreamError") from None
        except UnicodeError:
            # AEGIS SEO Wave 3 L3: a redirect target httpx cannot encode or IDNA-encode (a full-width host
            # ``１２７.０.０.１``, ``xn--``) is a URL problem, not a connection failure. Nothing was connected to: the
            # request goes to the CHECKED address only, and a Location is followed only after _target re-checks it
            _refused_redirect(seen if "seen" in locals() else {}, "the redirect target is not a valid URL")
            raise _Refused("REFUSED_URL", "the target or redirect target is not a valid URL") from None
        except httpx.TimeoutException as exc:
            if isinstance(exc, httpx.ConnectTimeout) and not _BUDGET.connected:
                raise _NotConnected("TIMEOUT", "timed out") from None
            raise _Refused("TIMEOUT", "timed out") from None
        except MemoryError:
            raise _Refused("RESOURCE_LIMIT", "the response exhausted memory; refused") from None
        except httpx.ConnectError as exc:
            if isinstance(exc.__cause__, ssl.SSLError) or "SSL" in str(exc) or "certificate" in str(exc).lower():
                raise _Refused("TLS_FAILED", "TLS handshake or certificate check failed") from None
            if not _BUDGET.connected:
                raise _NotConnected("CONNECT_FAILED", "connection failed") from None
            raise _Refused("CONNECT_FAILED", "connection failed") from None
        except httpx.RemoteProtocolError as exc:
            if "location header" in str(exc).lower():       # httpx refuses to parse the redirect target itself
                _refused_redirect(seen if "seen" in locals() else {}, "the redirect target is not a valid URL")
                raise _Refused("REFUSED_URL", "the redirect target is not a valid URL") from None
            raise _Refused("PROTOCOL_ERROR", "protocol error: RemoteProtocolError") from None
        except (httpx.DecodingError, httpx.LocalProtocolError) as exc:
            raise _Refused("PROTOCOL_ERROR", f"protocol error: {type(exc).__name__}") from None
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise _Refused("CONNECT_FAILED", f"request failed: {type(exc).__name__}") from None

    # ------------------------------------------------------------------ public

    def fetch(self, url: str, *, ua: str = "bot", robots_cache: Optional[dict] = None, honor_robots: bool = True,
              guard: Optional[Callable] = None, accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,"
                                                              "text/plain;q=0.8,*/*;q=0.5",
              truncate_at: Optional[int] = None, sink: Optional[Callable] = None,
              max_bytes: Optional[int] = None) -> FetchResult:
        """GET ``url`` with redirects followed by hand. ``ua``: "bot" (ours) or "human" (access-diff only).
        ``guard`` is the kill-switch check (raises to stop). ``truncate_at``: keep the first that many decoded bytes
        of a longer body and stop reading (``truncated``), instead of refusing it TOO_LARGE. Never raises for a
        network outcome."""
        user_agent = self.user_agent if ua == "bot" else HUMAN_UA
        res = FetchResult(url=url, state="OK", user_agent=ua)
        deadline = time.monotonic() + 2 * self.timeout_s
        prev = (_BUDGET.deadline, _BUDGET.guard, _BUDGET.truncate_at, _BUDGET.truncated, _BUDGET.sink,
                _BUDGET.max_bytes)
        if prev[0] is not None:                   # a nested fetch (robots.txt) never outlives the outer deadline
            deadline = min(deadline, prev[0])
        _BUDGET.deadline, _BUDGET.guard = deadline, guard
        _BUDGET.truncate_at, _BUDGET.truncated = None, False
        _BUDGET.status_seen = False
        current = url
        try:
            for hop in range(self.max_redirects + 1):
                if guard is not None:
                    guard(capability="fetch", provider="web")
                target = self._target(current)            # the URL and its addresses first, robots second
                if honor_robots and robots_cache is not None:
                    _BUDGET.sink, _BUDGET.max_bytes = None, None     # the nested robots fetch keeps its own
                    self._robots_check(current, robots_cache, guard)
                _BUDGET.truncate_at, _BUDGET.truncated = truncate_at, False
                _BUDGET.sink, _BUDGET.max_bytes = sink, max_bytes
                _BUDGET.status_seen = False
                status, headers, body, _ = self._one(current, target, user_agent, deadline, accept)
                res.truncated = _BUDGET.truncated
                _BUDGET.truncate_at = None
                if 300 <= status < 400 and headers.get("location"):
                    try:
                        nxt = urljoin(current, headers["location"])
                    except ValueError:                    # WR-F008: an unparseable target is refused, never raised
                        raise _Refused("REFUSED_URL", "the redirect target is not a valid URL") from None
                    res.redirects.append({"url": current, "status": status})
                    current = nxt
                    if hop == self.max_redirects:
                        raise _Refused("TOO_MANY_REDIRECTS", f"more than {self.max_redirects} redirects")
                    continue
                res.final_url, res.status, res.headers, res.body = current, status, headers, body
                res.content_type, res.charset = _content_type(headers)
                return res
            raise _Refused("TOO_MANY_REDIRECTS", f"more than {self.max_redirects} redirects")
        except _RefusedRedirect as r:
            # WR-F014: the refused redirect is recorded APART (``refused_redirect``: {status, location}; the target is
            # never fetched). ``redirects`` and ``final_url`` keep their meaning (followed hops; where the fetch
            # stopped), so every existing reader of a FetchResult is unchanged.
            res.state, res.detail, res.final_url = r.state, r.detail, current
            res.refused_redirect = {"status": r.status, "location": r.location}
            return res
        except _Refused as r:
            res.state, res.detail, res.final_url, res.partial = r.state, r.detail, current, r.partial
            res.robots = r.robots
            res.status_seen = bool(getattr(_BUDGET, "status_seen", False))
            return res
        except Killed as k:                               # a kill switch engaged (or the service closed)
            res.state, res.detail, res.final_url = "KILLED", k.code, current
            return res
        except MemoryError:
            res.state, res.detail, res.final_url = "RESOURCE_LIMIT", "memory exhausted; refused", current
            return res
        finally:
            (_BUDGET.deadline, _BUDGET.guard, _BUDGET.truncate_at, _BUDGET.truncated, _BUDGET.sink,
             _BUDGET.max_bytes) = prev

    def _robots_check(self, url: str, cache: dict, guard: Optional[Callable]) -> None:
        """Raise unless robots.txt lets this crawler fetch ``url``. Three outcomes, three states (round 2 M2/M3):
        a rule IN THE FILE disallows it, or robots.txt is unreachable (RFC 9309: complete disallow) ->
        BLOCKED_BY_ROBOTS with ``robots`` saying which; the check could not be completed within this run's counted
        work, or robots.txt was larger than this service reads at all -> ROBOTS_CHECK_INCOMPLETE (this service's own
        refusal: no claim about the file). A file cut at the parse limit is honoured as read (RFC 9309 2.5)."""
        p = urlsplit(url)
        origin = f"{p.scheme.lower()}://{(p.netloc or '').lower()}"
        if origin not in cache:
            rb = self.robots(origin, guard=guard)
            if rb["fetch"]["state"] == "KILLED":          # B-FT-08: a switch during the robots fetch is KILLED
                raise Killed(rb["fetch"]["detail"] or "KILLED_GLOBAL")
            cache[origin] = rb
        rb = cache[origin]
        path = urlunsplit(("", "", p.path or "/", p.query, ""))
        if rb["parsed"] is None:
            if rb["default_allow"]:
                return
            if rb["status_class"] == "incomplete":
                raise _Refused("ROBOTS_CHECK_INCOMPLETE", "robots.txt could not be decoded within this service's "
                                                          "bounds; not checked, so not fetched",
                               robots={"reason": "not_decodable"})
            raise _Refused("BLOCKED_BY_ROBOTS", "robots.txt could not be fetched (RFC 9309: complete disallow)",
                           robots={"reason": "unreachable"})
        work = rb.get("check_work")
        if work is None:
            work = rb["check_work"] = robots_mod.Work(ROBOTS_CHECK_WORK)
        d = rb["parsed"].decide(PRODUCT_TOKEN, path, work)
        if d["allowed"] is None:
            raise _Refused("ROBOTS_CHECK_INCOMPLETE", "robots.txt could not be checked within this run's counted "
                                                      "work; not fetched (no claim about the file)",
                           robots={"reason": "work", "units": work.limit})
        if d["allowed"] is False:
            raise _Refused("BLOCKED_BY_ROBOTS", f"robots.txt disallows this path for {PRODUCT_TOKEN}",
                           robots={"reason": "rule"})

    def robots(self, origin: str, guard: Optional[Callable] = None) -> dict:
        """RFC 9309 section 2.3.1: 2xx -> parse (the first ROBOTS_MAX_BYTES; a longer file is read up to there and
        marked truncated, section 2.5); 4xx -> unavailable, allow all; 5xx or unreachable -> disallow all. Redirects
        followed (up to max_redirects, every hop checked). The text is decoded UTF-8 with ``surrogateescape``: an
        octet that is not valid UTF-8 is kept as itself for matching (robots.canon), never replaced."""
        r = self.fetch(origin + "/robots.txt", honor_robots=False, guard=guard, accept="text/plain,*/*;q=0.5",
                       truncate_at=ROBOTS_MAX_BYTES)
        out = {"origin": origin, "fetch": r.summary(), "parsed": None, "default_allow": False, "status_class": None}
        if r.state in ("TOO_LARGE", "PROTOCOL_ERROR", "RESOURCE_LIMIT", "UNSUPPORTED_ENCODING") and r.status_seen:
            # B-RB-08 / B-SE-03: the server answered, but the body could not be DECODED within this service's
            # bounds (over ROBOTS_WIRE_MAX on the wire, a corrupt or unsupported coding): incomplete, this service's
            # own limit, never "unreachable"
            out["status_class"] = "incomplete"
            out["partial"] = r.partial                   # size / SHA-256 of what was read, for the quarantine record
            return out
        if r.state != "OK":
            out["status_class"] = "unreachable"
            return out
        if 200 <= r.status < 300:
            out["text"] = r.body[:ROBOTS_MAX_BYTES].decode("utf-8", errors="surrogateescape")
            out["parsed"] = robots_mod.parse(out["text"], truncated=r.truncated or len(r.body) > ROBOTS_MAX_BYTES)
            out["status_class"] = "ok"
            out["text_sha256"] = hashlib.sha256(r.body).hexdigest()
            out["truncated"] = out["parsed"].truncated
        elif 400 <= r.status < 500 and r.status != 429:
            # Q-06 (owner default): 429 is treated like 5xx (Google documents it so), every other 4xx is unavailable
            out["default_allow"] = True
            out["status_class"] = "unavailable"
        else:
            out["status_class"] = "unreachable"
        return out


_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _ldh_host(host: str) -> bool:
    """Letters-digits-hyphen labels only (RFC 1123 host names, after IDNA): 1-63 characters a label, no leading or
    trailing hyphen, at most 253 characters in all, no empty label."""
    return 0 < len(host) <= 253 and all(_LABEL.fullmatch(lb) for lb in host.split("."))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _numeric_host_not_canonical(host: str) -> bool:
    """True for a host whose last label is a number (decimal, or 0x hex) but which is not a canonical IP address.

    Such a host is an IPv4 address in a legacy spelling (``0177.0.0.1``, ``127.1``, ``2130706433``, ``0x7f000001``)
    and what it resolves to is up to the platform's C library: glibc reads ``0177`` as octal (127.0.0.1) while the
    macOS CI runner resolved it to a different, public address. An attacker-chosen URL must not mean different
    addresses on different hosts, and no real site is addressed this way, so these spellings are refused before any
    resolution (the WHATWG URL rule: a host whose last label is numeric is an IPv4 address, never a domain name)."""
    if _is_ip_literal(host):
        return False
    last = host.rsplit(".", 1)[-1].lower()
    if not last or not last.isascii():
        return False
    return last.isdigit() or (last.startswith("0x") and all(c in "0123456789abcdef" for c in last[2:]))
