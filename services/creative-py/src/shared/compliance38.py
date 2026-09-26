"""
HTTP client for Compliance (38) — ``services/compliance-py``,
``POST /compliance/v1/review`` (Compliance spec §F.1, ADR 0006).

Implements the existing ``Compliance38Port`` protocol
(``review(subject_kind, subject_id, facts) -> GateResult``). Used only when
COMPLIANCE_SERVICE_URL, COMPLIANCE_SERVICE_TOKEN and COMPLIANCE_CALLER_TOKEN
are all set; otherwise the fail-closed ``NotBuiltCompliance38`` stays.
No fallback to "allowed": any non-200, timeout, transport error or
unparseable/inconsistent answer is ``GateResult("compliance_38", False,
"Compliance (38) unreachable or refused (<status>): not allowed")``.
Timeout 10 s; one retry with the SAME request_id on a transport error or 5xx.

Creative's ZBM gate passes its opaque ``export`` and ``rights`` objects in
``facts``; Compliance's schema is strict, so this client carries them in
``caller_context`` (stored there only as a SHA-256, never interpreted).
"""

from __future__ import annotations

import uuid
from typing import Optional

import httpx

from shared.departments import GateResult, NotBuiltCompliance38

TIMEOUT_S = 10.0
_ROUTE = "/compliance/v1/review"
_CONTEXT_KEYS = ("export", "rights")
_GATE = {"zbc_clip": "payout", "zbm_work": "publish"}


def _refused(status: str) -> GateResult:
    return GateResult("compliance_38", False, f"Compliance (38) unreachable or refused ({status}): not allowed")


class HttpCompliance38:
    def __init__(self, base_url: str, service_token: str, caller_token: str,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = TIMEOUT_S):
        if not (base_url and service_token and caller_token):
            raise ValueError("HttpCompliance38 needs a URL, the service token and the caller token")
        self._url = base_url.rstrip("/") + _ROUTE
        self._headers = {"Authorization": f"Bearer {service_token}", "X-Compliance-Caller-Token": caller_token}
        self._transport = transport
        self._timeout = timeout

    def review(self, subject_kind: str, subject_id: str, facts: dict) -> GateResult:
        facts = dict(facts)
        context = {k: facts.pop(k) for k in _CONTEXT_KEYS if k in facts}
        body = {"request_id": "cre-" + uuid.uuid4().hex, "subject_kind": subject_kind, "subject_id": subject_id,
                "facts": facts}
        if context:
            body["caller_context"] = context
        status = "no answer"
        for _attempt in range(2):
            try:
                with httpx.Client(timeout=self._timeout, transport=self._transport, follow_redirects=False) as c:
                    resp = c.post(self._url, json=body, headers=self._headers)
            except httpx.HTTPError as exc:
                status = type(exc).__name__
                continue
            if resp.status_code >= 500:
                status = str(resp.status_code)
                continue
            if resp.status_code != 200:
                return _refused(str(resp.status_code))
            return _parse(resp, subject_kind)
        return _refused(status)


def _parse(resp: httpx.Response, subject_kind: str) -> GateResult:
    try:
        data = resp.json()
        allowed, reason, ref = data["allowed"], data["reason"], data["reference"]
        gate = data.get("gate")
    except (ValueError, KeyError, TypeError):
        return _refused("unparseable answer")
    if (not isinstance(allowed, bool) or not isinstance(reason, str) or not isinstance(ref, str)
            or gate != _GATE.get(subject_kind) or (allowed and not reason.startswith("allowed under register v"))):
        return _refused("inconsistent answer")
    return GateResult("compliance_38", allowed, reason[:1000], ref)


def compliance_from_env(env: dict):
    url, tok, caller = env.get("COMPLIANCE_SERVICE_URL"), env.get("COMPLIANCE_SERVICE_TOKEN"), env.get("COMPLIANCE_CALLER_TOKEN")
    if url and tok and caller:
        return HttpCompliance38(url, tok, caller)
    return NotBuiltCompliance38()
