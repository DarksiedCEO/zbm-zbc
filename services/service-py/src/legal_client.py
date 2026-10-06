"""
Customer Service / Client Success -> Legal (37) thin client: ``POST /legal/v1/requests`` (legal-py's matter intake),
as the caller whose token is ``SVC_LEGAL_CALLER_TOKEN`` (legal-py does not list this department as a caller yet:
ADR 0014 unlock list).

security-py's ``compliance_client.py`` pattern (itself legal-py's / verification-py's after AEGIS round 16): a 10 s
TOTAL wall-clock budget per call, one retry on a transport error or 5xx while budget remains (safe: legal-py's
idempotency is request_id + body, and the request_id is the handoff id), a 1 MiB answer cap, no compressed answer,
no redirects. The request carries refs and codes only, never the message body or a contact detail. Any failure is
``unavailable`` (the handoff-retries job tries again), never "delivered"; an answer that does not echo our request
id or carry a matter id is not proof of delivery. Never called under the lock.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

import httpx

from ports import Handoff, HandoffRequest

TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
LEGAL_KINDS = ("contract_dispute", "litigation_threat", "ip_claim", "privacy_request", "question")
_MATTER = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def _run_bounded(fn, seconds: float):
    box: dict = {}
    cancel = threading.Event()

    def run():
        try:
            box["r"] = fn(cancel)
        except BaseException as exc:  # noqa: BLE001 - nothing escapes: fail closed
            box["r"] = ("error", type(exc).__name__, None)

    t = threading.Thread(target=run, name="svc-legal37-call", daemon=True)
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


class HttpLegal:
    wired = True

    def __init__(self, base_url: str, service_token: str, caller_token: str,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = TIMEOUT_S):
        if not (base_url and service_token and caller_token):
            raise ValueError("HttpLegal needs a URL, the service token and the caller token")
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {service_token}", "X-LEGAL-Caller-Token": caller_token}
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

    @staticmethod
    def body(req: HandoffRequest) -> dict:
        subjects = [f"svc:ticket:{req.ticket_id}"] + ([f"client:{req.account_id}"] if req.account_id else [])
        return {"request_id": req.handoff_id, "channel": "department", "requester_ref": f"svc:{req.ticket_id}",
                "kind": req.kind, "facts": {"dsar": req.kind == "privacy_request"}, "subject_refs": subjects}

    def handoff(self, req: HandoffRequest) -> Handoff:
        if req.kind not in LEGAL_KINDS:
            return Handoff("refused")
        res = self._call("POST", "/legal/v1/requests", self.body(req))
        if res is None or res[0] != "ok" or res[1] >= 500:
            return Handoff("unavailable")
        status, data = res[1], res[2]
        if status in (200, 201):
            try:
                ans = json.loads(data)
                if ans["request_id"] != req.handoff_id or not _MATTER.fullmatch(ans["matter_id"]):
                    raise ValueError
            except Exception:  # noqa: BLE001 - an answer we cannot read is not proof of delivery
                return Handoff("unavailable")
            return Handoff("delivered", ans["matter_id"])
        if 400 <= status < 500:
            return Handoff("refused")
        return Handoff("unavailable")
