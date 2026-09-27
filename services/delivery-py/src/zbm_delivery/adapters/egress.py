"""
Egress adapter — the only outbound client (spec §C.4, D4; DF-NET-04/06/08; ADR 0006 decision 11).

``EgressClient`` wraps ONE ``httpx.Client`` built once (``trust_env=False``, our CA bundle from ``SSL_CERT_FILE``
when set, ``timeout=httpx.Timeout(10.0)`` default, ``follow_redirects=False``, ``max_redirects=0``). Every request:

- host must be in the allowlist (exact host match, https only, port 443 unless the entry names a port) — else
  ``EgressRefused`` BEFORE any DNS lookup (the URL is parsed, the host compared, nothing else happens);
- is recorded ``crossing_egress_requested {host, purpose, body_sha256}`` first — a failed record refuses the call;
- ``timeout`` may only be shortened, except ``purpose="llm"`` which uses ``DLV_EGRESS_LLM_READ_TIMEOUT_S`` for the
  read phase and 10 s for connect;
- round 18 R6: a TOTAL per-call deadline (connect + every read, wall clock) enforced by this client while the body is
  streamed — ``default_timeout_s`` for ordinary calls, ``llm_read_timeout_s`` for LLM calls, and never more than the
  ``deadline_s`` the caller passes (the run's remaining wall clock); the body is read in chunks against the cap
  (never buffered past it); ``abort(run_id)`` closes every response in flight for that run from another thread;
- one retry on transport error / 5xx with the same idempotency header for non-LLM calls, none for LLM calls;
- the response body is capped (8 MiB for LLM answers, 1 MiB otherwise) — larger → ``EgressFailed``.

The proxy: when ``HTTPS_PROXY`` is set the client is built with it explicitly (still ``trust_env=False``); the
allowlist is enforced on the TARGET host regardless. No other module constructs an ``httpx.Client`` (G13) except
``ledger.py`` (a separate 5 s client, as in finance-py). No secret is ever placed in an exception text.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
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
        self._inflight: dict[int, tuple[str, Any]] = {}      # id -> (run_id, response) for abort()
        self._lock = threading.Lock()
        self._aborted: set[int] = set()

    def close(self) -> None:
        self._client.close()

    def abort(self, run_id: str) -> int:
        """Close every in-flight response of ``run_id`` (R6: a cancel or the watchdog interrupts the LLM call). Returns
        how many were closed."""
        with self._lock:
            victims = [(k, r) for k, (rid, r) in self._inflight.items() if rid == run_id]
            for k, _ in victims:
                self._aborted.add(k)
        for _, resp in victims:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        return len(victims)

    def allowed(self, url: str) -> tuple[str, int]:
        host, port = parse_target(url)
        if host not in self.allow or self.allow[host] != port:
            raise EgressRefused(f"host not on the egress allowlist (or wrong port): {host[:80]}")
        return host, port

    def request(self, method: str, url: str, *, purpose: str, headers: Optional[dict] = None,
                body: Optional[bytes] = None, timeout: Optional[float] = None, run_id: str = "-",
                deadline_s: Optional[float] = None) -> httpx.Response:
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
            total = self.llm_read_timeout_s
            attempts = 1
            cap = LLM_CAP
        else:
            total = min(float(timeout), self.default_timeout_s) if timeout else self.default_timeout_s
            attempts = 2
            cap = OTHER_CAP
        if deadline_s is not None:
            total = max(0.0, min(total, float(deadline_s)))
        hdrs = dict(headers or {})
        hdrs.setdefault("Idempotency-Key", idem)
        last: Optional[Exception] = None
        started = time.monotonic()
        for attempt in range(attempts):
            remaining = total - (time.monotonic() - started)
            if remaining <= 0:
                raise EgressFailed("egress total deadline exceeded before the request")
            read_s = min(remaining, self.llm_read_timeout_s if purpose == "llm" else total)
            t = httpx.Timeout(connect=min(CONNECT_S, remaining), read=read_s, write=min(CONNECT_S, remaining), pool=min(CONNECT_S, remaining))
            try:
                resp = self._stream(method, url, hdrs, body, t, cap, started, total, run_id)
            except httpx.TransportError as exc:
                last = EgressFailed(f"transport error: {type(exc).__name__}")
                continue
            if resp.status_code >= 500 and attempt + 1 < attempts:
                last = EgressFailed(f"HTTP {resp.status_code}")
                continue
            return resp
        raise last or EgressFailed("egress failed")

    def _stream(self, method, url, hdrs, body, t, cap, started, total, run_id) -> httpx.Response:
        """Send, then read the body chunk by chunk against the cap and the total deadline (R6)."""
        req = self._client.build_request(method, url, headers=hdrs, content=body, timeout=t)
        resp = self._client.send(req, stream=True)
        key = id(resp)
        with self._lock:
            self._inflight[key] = (run_id, resp)
        chunks: list[bytes] = []
        size = 0
        try:
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > cap:
                    raise EgressFailed("response larger than the cap")
                if time.monotonic() - started > total:
                    raise EgressFailed("egress total deadline exceeded while reading the response")
                with self._lock:
                    if key in self._aborted:
                        raise EgressFailed("egress aborted (run interrupted)")
                chunks.append(chunk)
        except httpx.TransportError as exc:
            with self._lock:
                aborted = key in self._aborted
            raise EgressFailed("egress aborted (run interrupted)" if aborted else f"transport error: {type(exc).__name__}") from None
        except (httpx.StreamError, RuntimeError) as exc:
            with self._lock:
                aborted = key in self._aborted
            if aborted:
                raise EgressFailed("egress aborted (run interrupted)") from None
            if isinstance(exc, EgressFailed):
                raise
            raise EgressFailed(f"stream error: {type(exc).__name__}") from None
        finally:
            with self._lock:
                self._inflight.pop(key, None)
                self._aborted.discard(key)
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        # rebuild a fully-read response the callers use as before (.status_code, .content, .json())
        out = httpx.Response(resp.status_code, headers=resp.headers, content=b"".join(chunks), request=req)
        return out
