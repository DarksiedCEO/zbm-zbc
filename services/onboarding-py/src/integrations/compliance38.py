"""
HTTP client for the Compliance department (38) — ``services/compliance-py``,
``POST /compliance/v1/rule`` (Compliance spec §F.1, ADR 0006).

Implements the existing ``ComplianceDepartment`` protocol
(``rule(subject_id, lane, facts) -> Ruling``). Used only when
COMPLIANCE_SERVICE_URL, COMPLIANCE_SERVICE_TOKEN and COMPLIANCE_CALLER_TOKEN
are all set; otherwise the fail-closed ``NotBuiltComplianceDepartment``
stand-in stays in place. No fallback to "allowed" anywhere: any non-200,
timeout, transport error or unparseable/inconsistent answer is
``Ruling(False, ("compliance_department_38_ruling: Compliance (38)
unreachable or refused (<status>) — not allowed",), "not allowed yet")``.
Timeout 10 s is the TOTAL wall-clock budget of one call, both attempts
included, however slowly the server dribbles bytes (AEGIS N14-7); one retry
with the SAME request_id (Compliance answers an identical retry from its
idempotency store) on a transport error or 5xx while budget remains. The
answer is capped at 1 MiB before it is parsed, and any parse failure is
"not allowed", never an exception (N14-8). The answer must name the same
subject_id, lane and gate as the request, else "not allowed" (N14-10).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Optional

import httpx

from integrations.departments import NOT_ALLOWED_YET, NotBuiltComplianceDepartment, Ruling

TIMEOUT_S = 10.0                    # total per call (both attempts), wall clock
MAX_RESPONSE_BYTES = 1024 * 1024    # an answer larger than this is refused unparsed
_ROUTE = "/compliance/v1/rule"


def _refused(status: str) -> Ruling:
    return Ruling(False, (f"compliance_department_38_ruling: Compliance (38) unreachable or refused ({status}) — not allowed",),
                  NOT_ALLOWED_YET)


class HttpComplianceDepartment:
    def __init__(self, base_url: str, service_token: str, caller_token: str,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = TIMEOUT_S):
        if not (base_url and service_token and caller_token):
            raise ValueError("HttpComplianceDepartment needs a URL, the service token and the caller token")
        self._url = base_url.rstrip("/") + _ROUTE
        self._headers = {"Authorization": f"Bearer {service_token}", "X-Compliance-Caller-Token": caller_token}
        self._transport = transport
        self._timeout = timeout

    def rule(self, subject_id: str, lane: str, facts: dict) -> Ruling:
        body = {"request_id": "onb-" + uuid.uuid4().hex, "subject_id": subject_id, "lane": lane, "facts": facts}
        deadline = time.monotonic() + self._timeout
        status = "no answer"
        for _attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _refused("deadline exceeded")
            res = _run_bounded(lambda cancel, r=remaining: _exchange(self._url, body, self._headers, self._transport,
                                                                      r, cancel), remaining)
            if res is None:
                return _refused("deadline exceeded")
            kind, code, content = res
            if kind == "error":
                status = str(code)
                continue
            if kind == "too_large":
                return _refused("answer too large")
            if code >= 500:
                status = str(code)
                continue
            if code != 200:
                return _refused(str(code))
            return _parse(content, subject_id, lane)
        return _refused(status)


def _parse(content: bytes, subject_id: str, lane: str) -> Ruling:
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            return _refused("unparseable answer")
        allowed, lines, rid = data["allowed"], data["unmet_lines"], data["ruling_id"]
        gate, sid, got_lane = data.get("gate"), data.get("subject_id"), data.get("lane")
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - ValueError, KeyError, TypeError, RecursionError...: never raises, never allowed
        return _refused("unparseable answer")
    if (not isinstance(allowed, bool) or not isinstance(rid, str) or not isinstance(lines, list)
            or not all(isinstance(x, str) for x in lines) or gate != "activation"
            or sid != subject_id or got_lane != lane
            or (allowed and lines) or (not allowed and not lines)):
        return _refused("inconsistent answer")
    return Ruling(allowed, tuple(lines), detail=rid)


def _run_bounded(fn, seconds: float):
    """Run ``fn(cancel)`` in a daemon thread and wait at most ``seconds`` (wall clock). Returns its result,
    ("error", <name>, None) if it raised, or None when the deadline passed (the thread is told to stop and
    ends by itself within its own httpx timeout, which is never longer than the time that was left)."""
    box: dict = {}
    cancel = threading.Event()

    def run():
        try:
            box["r"] = fn(cancel)
        except BaseException as exc:  # noqa: BLE001 - nothing escapes to the caller: fail closed
            box["r"] = ("error", type(exc).__name__, None)

    t = threading.Thread(target=run, name="compliance38-call", daemon=True)
    t.start()
    t.join(max(0.0, seconds))
    if t.is_alive():
        cancel.set()
        return None
    return box.get("r", ("error", "no answer", None))


def _exchange(url: str, body: dict, headers: dict, transport, remaining: float, cancel: threading.Event):
    """One POST, streamed: ("ok", status, bytes) | ("too_large", status, None) | ("error", name, None)."""
    try:
        with httpx.Client(timeout=httpx.Timeout(max(0.01, remaining)), transport=transport,
                          follow_redirects=False) as c:
            with c.stream("POST", url, json=body, headers={**headers, "Accept-Encoding": "identity"}) as resp:
                if resp.status_code != 200:
                    return "ok", resp.status_code, b""
                enc = resp.headers.get("content-encoding", "identity").strip().lower()
                if enc not in ("", "identity"):
                    return "error", "encoded answer", None
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    return "too_large", resp.status_code, None
                buf = bytearray()
                for chunk in resp.iter_bytes():  # identity only (checked above): bytes as sent
                    if cancel.is_set():
                        return "error", "deadline", None
                    buf += chunk
                    if len(buf) > MAX_RESPONSE_BYTES:
                        return "too_large", resp.status_code, None
                return "ok", resp.status_code, bytes(buf)
    except httpx.HTTPError as exc:
        return "error", type(exc).__name__, None


def compliance_from_env(env: dict):
    url, tok, caller = env.get("COMPLIANCE_SERVICE_URL"), env.get("COMPLIANCE_SERVICE_TOKEN"), env.get("COMPLIANCE_CALLER_TOKEN")
    if url and tok and caller:
        return HttpComplianceDepartment(url, tok, caller)
    return NotBuiltComplianceDepartment()
