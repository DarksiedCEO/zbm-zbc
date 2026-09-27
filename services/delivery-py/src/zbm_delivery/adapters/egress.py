"""
Egress adapter — the only outbound client (spec §C.4, D4; DF-NET-04/06/08; ADR 0006 decision 11).

``EgressClient`` wraps ONE ``httpx.Client`` built once (``trust_env=False``, our CA bundle from ``SSL_CERT_FILE``
when set, ``timeout=httpx.Timeout(10.0)`` default, ``follow_redirects=False``, ``max_redirects=0``). Every request:

- host must be in the allowlist (exact host match, https only, port 443 unless the entry names a port) — else
  ``EgressRefused`` BEFORE any DNS lookup (the URL is parsed, the host compared, nothing else happens);
- is recorded ``crossing_egress_requested {host, purpose, body_sha256}`` first — a failed record refuses the call;
- ``timeout`` may only be shortened, except ``purpose="llm"`` which uses ``DLV_EGRESS_LLM_READ_TIMEOUT_S`` for the
  read phase and 10 s for connect;
- one retry on transport error / 5xx with the same idempotency header for non-LLM calls, none for LLM calls;
- the response body is capped (8 MiB for LLM answers, 1 MiB otherwise) — larger → ``EgressRefused``.

The proxy: when ``HTTPS_PROXY`` is set the client is built with it explicitly (still ``trust_env=False``); the
allowlist is enforced on the TARGET host regardless. No other module constructs an ``httpx.Client`` (G13) except
``ledger.py`` (a separate 5 s client, as in finance-py). No secret is ever placed in an exception text.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from zbm_delivery.ledger import derived_id

ACTOR = "intel_04_egress"
LLM_CAP = 8 * 1024 * 1024
OTHER_CAP = 1024 * 1024
CONNECT_S = 10.0


class EgressRefused(RuntimeError):
    """Refused before any byte left the box (host not on the allowlist, wrong scheme/port, unrecorded)."""


class EgressFailed(RuntimeError):
    """The request left the box and failed (transport error, timeout, 5xx after the retry, body over the cap)."""


def parse_target(url: str) -> tuple[str, int]:
    """(host, port) of an https URL, or ``EgressRefused``. IP literals, trailing dots and userinfo are refused."""
    if not isinstance(url, str) or len(url) > 2048:
        raise EgressRefused("bad url")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise EgressRefused("only https leaves the box")
    if parts.username or parts.password or "@" in parts.netloc:
        raise EgressRefused("userinfo in url refused")
    host = (parts.hostname or "")
    if not host or host.endswith(".") or host != host.lower() or host.replace(".", "").replace("-", "").isdigit() \
            or ":" in host or "[" in parts.netloc:
        raise EgressRefused("host is not a plain lower-case DNS name")
    try:
        port = parts.port or 443
    except ValueError:
        raise EgressRefused("bad port") from None
    return host, port


class EgressClient:
    def __init__(self, allow_hosts: tuple, *, record, default_timeout_s: float = 10.0, llm_read_timeout_s: float = 60.0,
                 transport: Optional[httpx.BaseTransport] = None, env: Optional[dict] = None):
        env = dict(os.environ) if env is None else env
        self.allow: dict[str, int] = {}
        for h in allow_hosts:
            host, _, port = h.partition(":")
            self.allow[host] = int(port) if port else 443
        self.record = record
        self.default_timeout_s = float(default_timeout_s)
        self.llm_read_timeout_s = float(llm_read_timeout_s)
        verify: Any = env.get("SSL_CERT_FILE") or True
        proxy = env.get("HTTPS_PROXY") or env.get("https_proxy") or None
        kwargs: dict[str, Any] = dict(trust_env=False, verify=verify, timeout=httpx.Timeout(self.default_timeout_s),
                                      follow_redirects=False, max_redirects=0)
        if transport is not None:
            kwargs["transport"] = transport
        elif proxy:
            kwargs["proxy"] = proxy
        self._client = httpx.Client(**kwargs)
        self.requests: int = 0

    def close(self) -> None:
        self._client.close()

    def allowed(self, url: str) -> tuple[str, int]:
        host, port = parse_target(url)
        if host not in self.allow or self.allow[host] != port:
            raise EgressRefused(f"host not on the egress allowlist (or wrong port): {host[:80]}")
        return host, port

    def request(self, method: str, url: str, *, purpose: str, headers: Optional[dict] = None,
                body: Optional[bytes] = None, timeout: Optional[float] = None, run_id: str = "-") -> httpx.Response:
        if method not in ("GET", "POST"):
            raise EgressRefused("method not allowed")
        host, port = self.allowed(url)                                  # before any DNS lookup
        body = body or b""
        body_sha = hashlib.sha256(body).hexdigest()
        self.requests += 1
        idem = secrets.token_hex(16)
        try:
            self.record(derived_id("eg", run_id, self.requests, host, purpose, body_sha), "crossing_egress_requested",
                        ACTOR, run_id if run_id != "-" else "egress",
                        {"host": host, "port": port, "purpose": purpose[:32], "body_sha256": body_sha, "method": method,
                         "seq": self.requests}, f"Egress to {host} ({purpose})")
        except Exception as exc:  # noqa: BLE001 - unrecorded = refused
            raise EgressRefused(f"egress not recorded ({type(exc).__name__}); refused") from None
        if purpose == "llm":
            t = httpx.Timeout(connect=CONNECT_S, read=self.llm_read_timeout_s, write=CONNECT_S, pool=CONNECT_S)
            attempts = 1
            cap = LLM_CAP
        else:
            t_s = min(float(timeout), self.default_timeout_s) if timeout else self.default_timeout_s
            t = httpx.Timeout(t_s)
            attempts = 2
            cap = OTHER_CAP
        hdrs = dict(headers or {})
        hdrs.setdefault("Idempotency-Key", idem)
        last: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                resp = self._client.request(method, url, headers=hdrs, content=body, timeout=t)
            except httpx.TransportError as exc:
                last = EgressFailed(f"transport error: {type(exc).__name__}")
                continue
            if resp.status_code >= 500 and attempt + 1 < attempts:
                last = EgressFailed(f"HTTP {resp.status_code}")
                continue
            if len(resp.content) > cap:
                raise EgressFailed("response larger than the cap")
            return resp
        raise last or EgressFailed("egress failed")
