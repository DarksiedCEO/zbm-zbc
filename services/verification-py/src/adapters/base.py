"""
Shared plumbing for the real platform adapters (spec §C.1, G7).

The real adapters are BUILT (httpx; tested against a mock transport, never the network) but run only when
the token vault is wired AND ``VI_<PLATFORM>_APP_CREDENTIALS_REF`` is set — neither is possible in this
build, so ``config`` refuses to start with either set, and every platform uses ``NotWiredAdapter``.

Guard rails every adapter goes through (``Http.call``):
- only the (method, URL prefix) pairs an adapter declares in ``ALLOWED`` (documented endpoints from the
  research notes; anything else raises before a byte is sent — G7);
- https only, no redirects followed, no cookies, 10 s timeout, 5 MB response cap;
- the token appears only in the ``Authorization`` header of that one request and is never in an
  exception, answer or log line (the raw response body is hashed, never kept);
- a 429 is reported as ``rate_limited`` (the service backs off and records ``platform_rate_limited``).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Optional

import httpx

TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


def caption_sha256(text: Optional[str]) -> Optional[str]:
    """§C.3: caption hash over NFKC, whitespace-collapsed text (no case folding: a changed letter is a change)."""
    if text is None:
        return None
    norm = " ".join(unicodedata.normalize("NFKC", text).split())
    return hashlib.sha256(norm.encode("utf-8", "surrogatepass")).hexdigest()


class CallRefused(Exception):
    """An adapter tried a call outside its declared allow-list (never sent)."""


@dataclass
class Reply:
    status: int
    body: bytes
    sha256: str

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return None


class Http:
    def __init__(self, allowed: tuple, transport: Optional[httpx.BaseTransport] = None):
        self.allowed = allowed
        self.transport = transport

    def check(self, method: str, url: str) -> None:
        if not url.startswith("https://"):
            raise CallRefused("https only")
        if not any(method == m and url.startswith(prefix) for m, prefix in self.allowed):
            raise CallRefused(f"{method} to an undeclared endpoint refused")

    def call(self, method: str, url: str, token: Optional[str] = None, params: Optional[dict] = None,
             json_body: Optional[dict] = None) -> Reply:
        self.check(method, url)
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(timeout=TIMEOUT_S, transport=self.transport, follow_redirects=False) as client:
                with client.stream(method, url, params=params, json=json_body, headers=headers) as resp:
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            return Reply(-1, b"", hashlib.sha256(b"").hexdigest())
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    return Reply(resp.status_code, body, hashlib.sha256(body).hexdigest())
        except httpx.HTTPError as exc:
            # only the exception TYPE survives (an httpx message can quote the request)
            raise TransportFailed(type(exc).__name__) from None


class TransportFailed(Exception):
    pass


def as_int(v) -> Optional[int]:
    """Platform counts arrive as ints or decimal strings (YouTube); anything else is 'unavailable'."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int) and v >= 0:
        return v
    if isinstance(v, str) and v.isdigit() and len(v) <= 19:
        return int(v)
    return None
