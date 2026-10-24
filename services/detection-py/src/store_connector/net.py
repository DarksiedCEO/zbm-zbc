"""Bounded HTTPS plumbing for the store connector (ADR 0020 section 4).

The pattern is seo-py ``finance_client.py`` (AEGIS SEO Wave 3 M1 and round 2), kept here so this package stays
standalone: the name lookup runs inside the operation's hard deadline on at most ``workers`` resolver threads in the
process (``DnsGate``), every socket operation (connect, TLS, each read and write) is bounded by the time left
(``DeadlineBackend``), and the backend connects only to the resolved addresses.

Added for a client-supplied host (SSRF): a resolved address must be a public unicast address (``is_global`` and not
multicast); an IPv6 address in a NAT64, IPv4-compatible, site-local or 6to4 range is refused, and any IPv4 address
embedded in an IPv6 one (mapped, 6to4, Teredo, NAT64) must itself be public. A name that resolves to a loopback, private, link-local, shared or reserved address is refused as if it
did not resolve, so a hostile DNS answer for ``*.myshopify.com`` cannot point the connector inside our network.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from typing import Callable, Optional

import httpcore


def system_resolve(host: str, port: int) -> list:
    return [i[4][0] for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


class DnsGate:
    """At most ``workers`` resolver threads at once; with every slot taken a lookup is refused (fail closed)."""

    def __init__(self, workers: int = 2):
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.workers = workers
        self._cond = threading.Condition(threading.Lock())
        self._busy = 0

    @property
    def busy(self) -> int:
        with self._cond:
            return self._busy

    def try_take(self) -> Optional["_Slot"]:
        with self._cond:
            if self._busy >= self.workers:
                return None
            self._busy += 1
        return _Slot(self)

    def _give_back(self) -> None:
        with self._cond:
            self._busy -= 1
            self._cond.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: self._busy == 0, timeout)

    def _after_fork_in_child(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._busy = 0


class _Slot:
    def __init__(self, gate: DnsGate):
        self._gate, self._lock, self._held = gate, threading.Lock(), True

    def release(self) -> None:
        with self._lock:
            held, self._held = self._held, False
        if held:
            self._gate._give_back()


DNS_GATE = DnsGate()
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=DNS_GATE._after_fork_in_child)


def ip_literal(host: str) -> Optional[str]:
    try:
        return str(ipaddress.ip_address(host[1:-1] if host.startswith("[") and host.endswith("]") else host))
    except ValueError:
        return None


# AEGIS golive-rr L3: IPv6 ranges that carry (or translate to) an IPv4 address, or that are deprecated site-local.
# Refused whole, whatever ``is_global`` says on this Python: NAT64 well-known (64:ff9b::/96) and local-use
# (64:ff9b:1::/48) prefixes, IPv4-compatible (::/96), site-local (fec0::/10) and 6to4 (2002::/16).
_REFUSED_V6 = tuple(ipaddress.ip_network(n) for n in
                    ("64:ff9b::/96", "64:ff9b:1::/48", "::/96", "fec0::/10", "2002::/16"))


def _embedded_v4(addr: ipaddress.IPv6Address) -> list:
    out = []
    if addr.ipv4_mapped is not None:
        out.append(addr.ipv4_mapped)
    if addr.sixtofour is not None:
        out.append(addr.sixtofour)
    if addr.teredo is not None:
        out.extend(addr.teredo)
    if any(addr in n for n in _REFUSED_V6[:3]):              # NAT64 / IPv4-compatible: the low 32 bits
        out.append(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
    return out


def _public_v4(addr: ipaddress.IPv4Address) -> bool:
    return addr.is_global and not addr.is_multicast


def is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv4Address):
        return _public_v4(addr)
    if getattr(addr, "scope_id", None):
        return False
    if any(addr in n for n in _REFUSED_V6):
        return False
    if not all(_public_v4(v4) for v4 in _embedded_v4(addr)):
        return False
    return addr.is_global and not addr.is_multicast


class Resolver:
    """Resolves INSIDE the deadline; returns public addresses only. An IP literal host is refused outright: the
    connector only ever names ``<handle>.myshopify.com``."""

    _thread = threading.Thread

    def __init__(self, resolve: Callable[[str, int], list] = system_resolve, gate: Optional[DnsGate] = None):
        self._resolve = resolve
        self._gate = gate if gate is not None else DNS_GATE

    def __call__(self, host: str, port: int, timeout: float) -> list:
        if ip_literal(host) is not None:
            raise httpcore.ConnectError("an address literal is never connected to")
        if timeout <= 0:
            raise httpcore.ConnectTimeout("the deadline has passed")
        slot = self._gate.try_take()
        if slot is None:
            raise httpcore.ConnectError("every DNS worker is busy")
        box: dict = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["addrs"] = self._resolve(host, port)
            except BaseException as exc:  # noqa: BLE001 - reported to the waiting caller as a connect error
                box["error"] = exc
            finally:
                slot.release()
                done.set()

        try:
            self._thread(target=work, name="rr-store-dns", daemon=True).start()
        except BaseException:
            slot.release()
            raise httpcore.ConnectError("could not start a DNS lookup") from None
        if not done.wait(timeout):
            raise httpcore.ConnectTimeout("the host did not resolve within the deadline")
        if "error" in box:
            raise httpcore.ConnectError("the host does not resolve")
        out: list = []
        try:
            for a in box.get("addrs") or ():
                ip = ip_literal(a) if isinstance(a, str) else None
                if ip is None:
                    raise httpcore.ConnectError("the host resolved to something that is not an address")
                if not is_public(ip):
                    raise httpcore.ConnectError("the host resolved to a non-public address")
                if ip not in out:
                    out.append(ip)
        except TypeError:
            raise httpcore.ConnectError("the host resolved to something that is not an address") from None
        if not out:
            raise httpcore.ConnectError("the host does not resolve")
        return out


class DeadlineStream(httpcore.NetworkStream):
    def __init__(self, inner, left: Callable[[Optional[float], type], float]):
        self._s, self._left = inner, left

    def read(self, max_bytes: int, timeout: Optional[float] = None) -> bytes:
        return self._s.read(max_bytes, self._left(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: Optional[float] = None) -> None:
        self._s.write(buffer, self._left(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self._s.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return DeadlineStream(self._s.start_tls(ssl_context, server_hostname,
                                                self._left(timeout, httpcore.ConnectTimeout)), self._left)

    def get_extra_info(self, info: str):
        return self._s.get_extra_info(info)


class DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, monotonic: Callable[[], float], end: float, inner=None, resolver: Optional[Resolver] = None):
        self._b = inner or httpcore.SyncBackend()
        self._mono, self._end = monotonic, end
        self._resolver = resolver or Resolver()

    def left(self, timeout: Optional[float], exc: type) -> float:
        left = self._end - self._mono()
        if left <= 0:
            raise exc("the deadline has passed")
        return left if timeout is None else min(timeout, left)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addrs = self._resolver(host, port, self.left(timeout, httpcore.ConnectTimeout))
        last: Optional[Exception] = None
        for ip in addrs:
            left = self.left(timeout, httpcore.ConnectTimeout)
            try:
                inner = self._b.connect_tcp(ip, port, left, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
                continue
            return DeadlineStream(inner, self.left)
        raise last if last is not None else httpcore.ConnectError("the host does not resolve")

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are never used")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
