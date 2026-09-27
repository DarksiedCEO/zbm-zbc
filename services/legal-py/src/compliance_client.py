"""
Legal → Compliance (38) thin client (spec §C.5, §E.2): ``POST /compliance/v1/register/proposals`` as caller
``legal_37`` and ``GET /compliance/v1/register/{id}``.

Pattern of verification-py ``compliance_client.py`` after AEGIS round 16 (N16-1): a 10 s TOTAL WALL-CLOCK budget
per call, both attempts included, however slowly the bytes arrive (the exchange runs in a daemon thread and the
caller waits at most the remaining budget); one retry on a transport error or 5xx while budget remains (a POST
retry is safe: Compliance's idempotency is ``request_id`` + body, and Legal derives the request id from the
memo and the row); 1 MiB answer cap; a compressed answer is refused; redirects are not followed. Any failure is
``unavailable`` (the proposal stays ``pending_delivery`` and is retried by the scheduler), never "created".
The service never calls this while holding its lock.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

import httpx

from ports import ComplianceRow, ProposalAnswer

TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
_ID = re.compile(r"[A-Z0-9][A-Z0-9-]{1,39}")
STATUSES = ("verified", "unverified", "expired", "superseded")


def _run_bounded(fn, seconds: float):
    box: dict = {}
    cancel = threading.Event()

    def run():
        try:
            box["r"] = fn(cancel)
        except BaseException as exc:  # noqa: BLE001 - nothing escapes: fail closed
            box["r"] = ("error", type(exc).__name__, None)

    t = threading.Thread(target=run, name="legal-compliance38-call", daemon=True)
    t.start()
    t.join(max(0.0, seconds))
    if t.is_alive():
        cancel.set()
        return None
    return box.get("r", ("error", "no answer", None))


def _exchange(method: str, url: str, headers: dict, body: Optional[dict], transport, remaining: float,
              cancel: threading.Event):
    try:
        with httpx.Client(timeout=httpx.Timeout(max(0.01, remaining)), transport=transport,
                          follow_redirects=False) as c:
            kw = {"json": body} if body is not None else {}
            with c.stream(method, url, headers={**headers, "Accept-Encoding": "identity"}, **kw) as resp:
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


class HttpCompliance:
    def __init__(self, base_url: str, service_token: str, caller_token: str,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = TIMEOUT_S):
        if not (base_url and service_token and caller_token):
            raise ValueError("HttpCompliance needs a URL, the service token and the caller token")
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {service_token}", "X-Compliance-Caller-Token": caller_token}
        self._transport = transport
        self._timeout = timeout

    def _call(self, method: str, path: str, body: Optional[dict]):
        deadline = time.monotonic() + self._timeout
        url = f"{self._base}{path}"
        last = None
        for _attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            res = _run_bounded(lambda cancel, r=remaining: _exchange(method, url, self._headers, body, self._transport,
                                                                     r, cancel), remaining)
            if res is None:
                break
            kind, status, data = res
            if kind == "error":
                continue
            if kind == "too_large":
                return ("too_large", status, None)
            last = (kind, status, data)
            if status >= 500:
                continue
            return last
        return last

    def create_proposal(self, request_id: str, body: dict) -> ProposalAnswer:
        res = self._call("POST", "/compliance/v1/register/proposals", {"request_id": request_id, **body})
        if res is None or res[0] != "ok" or res[1] >= 500:
            return ProposalAnswer("unavailable", None, res[1] if res else None)
        status, data = res[1], res[2]
        if status in (200, 201):
            try:
                pid = json.loads(data)["proposal"]["proposal_id"]
                if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", pid):
                    raise ValueError
            except Exception:  # noqa: BLE001 - an answer we cannot read is not proof of creation
                return ProposalAnswer("unavailable", None, status)
            return ProposalAnswer("created", pid, status)
        if 400 <= status < 500:
            return ProposalAnswer("refused", None, status)
        return ProposalAnswer("unavailable", None, status)      # a redirect or any other answer: retried, never "created"

    def row(self, obligation_id: str) -> ComplianceRow:
        if not isinstance(obligation_id, str) or not _ID.fullmatch(obligation_id):
            return ComplianceRow(False, str(obligation_id)[:40])
        res = self._call("GET", f"/compliance/v1/register/{obligation_id}", None)
        if res is None or res[0] != "ok" or res[1] != 200:
            return ComplianceRow(False, obligation_id)
        try:
            data = json.loads(res[2])
            row, version = data["row"], data["register_version"]
            if row["id"] != obligation_id or row["effective_status"] not in STATUSES or isinstance(version, bool) \
                    or not isinstance(version, int):
                raise ValueError
        except Exception:  # noqa: BLE001
            return ComplianceRow(False, obligation_id)
        return ComplianceRow(True, obligation_id, row["effective_status"], version)
