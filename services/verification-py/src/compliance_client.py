"""
V&I → Compliance (38) thin client (spec §D.3): ``GET /compliance/v1/register/{id}``.

Caller name ``verification_integrity`` (it already exists in compliance-py, ADR 0006 decision 2). Wired only
when VI_COMPLIANCE_URL, VI_COMPLIANCE_TOKEN and VI_COMPLIANCE_CALLER_TOKEN are all set; otherwise
``ports.NotWiredCompliance`` answers unavailable. Pattern of creative-py ``shared/compliance38.py`` after
AEGIS wave 14 (N14-7): a 10 s TOTAL WALL-CLOCK budget per call, both attempts included, however slowly the
bytes arrive (the exchange runs in a daemon thread and the caller waits at most the remaining budget — a
server that drips one byte every few seconds cannot stretch the call past it, AEGIS N16-1); one retry on a
transport error or 5xx while budget remains; 1 MiB answer cap; a compressed answer is refused; redirects are
not followed; any parse failure or an answer that does not name the requested row → unavailable (never
"verified").

Cache: one answer per obligation id for at most ``ttl_s`` (default 3600 s, spec "≤ 1 h"); a cached answer is
dropped as soon as any fresher answer reports a different ``register_version`` (the cache is per
register version). The row's ``verified_at`` / ``expires_at`` travel with it so the service re-judges a
cached "verified" row against its expiry at the time of use (N16-1).
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date
from typing import Optional

import httpx

from ports import RegisterRow

TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
_ID = re.compile(r"[A-Z0-9][A-Z0-9-]{1,39}")
STATUSES = ("verified", "unverified", "expired", "superseded")


def _run_bounded(fn, seconds: float):
    """Run ``fn(cancel)`` in a daemon thread and wait at most ``seconds`` (wall clock). Returns its result,
    ("error", <name>, None) if it raised, or None when the deadline passed (the thread is told to stop and
    abandoned; whatever it reads later is discarded)."""
    box: dict = {}
    cancel = threading.Event()

    def run():
        try:
            box["r"] = fn(cancel)
        except BaseException as exc:  # noqa: BLE001 - nothing escapes: fail closed
            box["r"] = ("error", type(exc).__name__, None)

    t = threading.Thread(target=run, name="vi-compliance38-call", daemon=True)
    t.start()
    t.join(max(0.0, seconds))
    if t.is_alive():
        cancel.set()
        return None
    return box.get("r", ("error", "no answer", None))


def _exchange(url: str, headers: dict, transport, remaining: float, cancel: threading.Event):
    try:
        with httpx.Client(timeout=httpx.Timeout(max(0.01, remaining)), transport=transport,
                          follow_redirects=False) as c:
            with c.stream("GET", url, headers={**headers, "Accept-Encoding": "identity"}) as resp:
                enc = resp.headers.get("content-encoding", "identity").strip().lower()
                if enc not in ("", "identity"):
                    return "error", "encoded answer", None
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    return "too_large", resp.status_code, None
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    if cancel.is_set():
                        return "error", "deadline", None
                    buf += chunk
                    if len(buf) > MAX_RESPONSE_BYTES:
                        return "too_large", resp.status_code, None
                return "ok", resp.status_code, bytes(buf)
    except httpx.HTTPError as exc:
        return "error", type(exc).__name__, None


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
            res = _run_bounded(lambda cancel, r=remaining: _exchange(url, self._headers, self._transport, r, cancel),
                               remaining)
            if res is None:
                break                                   # the wall-clock budget is spent: unavailable
            kind, status, body = res
            if kind == "error":
                continue
            if kind == "too_large":
                return RegisterRow(False, oid)
            if status >= 500:
                continue
            if status != 200:
                return RegisterRow(False, oid)
            return _parse(body, oid)
        return RegisterRow(False, oid)


def _day(v) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str) or len(v) != 10:
        raise ValueError("date")
    date.fromisoformat(v)
    return v


def _parse(body: bytes, oid: str) -> RegisterRow:
    try:
        data = json.loads(body)
        row = data["row"]
        version = data["register_version"]
        rid, status, params = row["id"], row["effective_status"], row.get("parameters") or {}
        verified_at, expires_at = _day(row.get("verified_at")), _day(row.get("expires_at"))
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - any shape problem: unavailable, never verified
        return RegisterRow(False, oid)
    if (rid != oid or status not in STATUSES or isinstance(version, bool) or not isinstance(version, int)
            or version < 1 or not isinstance(params, dict)):
        return RegisterRow(False, oid)
    return RegisterRow(True, oid, version, status, params, None, verified_at, expires_at)
