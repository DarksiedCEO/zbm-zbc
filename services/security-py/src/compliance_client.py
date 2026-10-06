"""
Cybersecurity (22) -> Compliance (38) thin client: ``POST /compliance/v1/controls/C-14/results`` as caller
``cybersecurity_22`` (Compliance's control C-14, "Security controls from Cybersecurity (22)", owned by us).

legal-py's ``compliance_client.py`` pattern (itself verification-py's after AEGIS round 16): a 10 s TOTAL
wall-clock budget per call, one retry on a transport error or 5xx while budget remains (safe: Compliance's
idempotency is request_id + body), a 1 MiB answer cap, no compressed answer, no redirects. Any failure is
``unavailable`` (the report is retried by the next job run), never "delivered". Never called under the lock.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

import httpx

from ports import ControlPush

TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024


def _run_bounded(fn, seconds: float):
    box: dict = {}
    cancel = threading.Event()

    def run():
        try:
            box["r"] = fn(cancel)
        except BaseException as exc:  # noqa: BLE001 - nothing escapes: fail closed
            box["r"] = ("error", type(exc).__name__, None)

    t = threading.Thread(target=run, name="sec22-compliance38-call", daemon=True)
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

    def push_control_result(self, control_id: str, request_id: str, result: str, tested_at: str,
                            evidence: list[dict]) -> ControlPush:
        if not re.fullmatch(r"C-[0-9]{2,3}", control_id):
            return ControlPush("refused", None)
        body = {"request_id": request_id, "result": result, "tested_at": tested_at, "evidence": evidence}
        res = self._call("POST", f"/compliance/v1/controls/{control_id}/results", body)
        if res is None or res[0] != "ok" or res[1] >= 500:
            return ControlPush("unavailable", res[1] if res else None)
        status, data = res[1], res[2]
        if status in (200, 201):
            try:
                if json.loads(data)["control"]["control_id"] != control_id:
                    raise ValueError
            except Exception:  # noqa: BLE001 - an answer we cannot read is not proof of delivery
                return ControlPush("unavailable", status)
            return ControlPush("delivered", status)
        if 400 <= status < 500:
            return ControlPush("refused", status)
        return ControlPush("unavailable", status)
