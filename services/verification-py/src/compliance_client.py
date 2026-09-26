"""
V&I → Compliance (38) thin client (spec §D.3): ``GET /compliance/v1/register/{id}``.

Caller name ``verification_integrity`` (it already exists in compliance-py, ADR 0006 decision 2). Wired only
when VI_COMPLIANCE_URL, VI_COMPLIANCE_TOKEN and VI_COMPLIANCE_CALLER_TOKEN are all set; otherwise
``ports.NotWiredCompliance`` answers unavailable. Pattern of creative-py ``shared/compliance38.py``: 10 s
TOTAL budget per call, one retry on a transport error or 5xx, 1 MiB answer cap, any parse failure or an
answer that does not name the requested row → unavailable (never "verified").

Cache: one answer per obligation id for at most ``ttl_s`` (default 3600 s, spec "≤ 1 h"); a cached answer is
dropped as soon as any fresher answer reports a different ``register_version`` (the cache is per
register version).
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

import httpx

from ports import RegisterRow

TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
_ID = re.compile(r"[A-Z0-9][A-Z0-9-]{1,39}")
STATUSES = ("verified", "unverified", "expired", "superseded")


class HttpComplianceRegister:
    def __init__(self, base_url: str, service_token: str, caller_token: str,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = TIMEOUT_S, ttl_s: float = 3600.0,
                 monotonic=time.monotonic):
        if not (base_url and service_token and caller_token):
            raise ValueError("HttpComplianceRegister needs a URL, the service token and the caller token")
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {service_token}", "X-Compliance-Caller-Token": caller_token}
        self._transport = transport
        self._timeout = timeout
        self._ttl = ttl_s
        self._mono = monotonic
        self._cache: dict[str, tuple[float, RegisterRow]] = {}
        self._lock = threading.Lock()

    def row(self, obligation_id: str) -> RegisterRow:
        if not isinstance(obligation_id, str) or not _ID.fullmatch(obligation_id):
            return RegisterRow(False, str(obligation_id)[:40])
        now = self._mono()
        with self._lock:
            hit = self._cache.get(obligation_id)
            if hit and now - hit[0] <= self._ttl:
                return hit[1]
        ans = self._fetch(obligation_id)
        if ans.available:
            with self._lock:
                stale = [k for k, (_, r) in self._cache.items() if r.register_version != ans.register_version]
                for k in stale:
                    del self._cache[k]
                self._cache[obligation_id] = (now, ans)
        return ans

    def _fetch(self, oid: str) -> RegisterRow:
        deadline = time.monotonic() + self._timeout
        url = f"{self._base}/compliance/v1/register/{oid}"
        for _attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                with httpx.Client(timeout=remaining, transport=self._transport, follow_redirects=False) as c:
                    with c.stream("GET", url, headers=self._headers) as resp:
                        chunks, size = [], 0
                        for chunk in resp.iter_bytes():
                            size += len(chunk)
                            if size > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                                return RegisterRow(False, oid)
                            chunks.append(chunk)
                        status = resp.status_code
                        body = b"".join(chunks)
            except httpx.HTTPError:
                continue
            if status >= 500:
                continue
            if status != 200:
                return RegisterRow(False, oid)
            return _parse(body, oid)
        return RegisterRow(False, oid)


def _parse(body: bytes, oid: str) -> RegisterRow:
    try:
        data = json.loads(body)
        row = data["row"]
        version = data["register_version"]
        rid, status, params = row["id"], row["effective_status"], row.get("parameters") or {}
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - any shape problem: unavailable, never verified
        return RegisterRow(False, oid)
    if (rid != oid or status not in STATUSES or isinstance(version, bool) or not isinstance(version, int)
            or version < 1 or not isinstance(params, dict)):
        return RegisterRow(False, oid)
    return RegisterRow(True, oid, version, status, params)
