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
Timeout 10 s; one retry with the SAME request_id (Compliance answers an
identical retry from its idempotency store) on a transport error or 5xx.
"""

from __future__ import annotations

import uuid
from typing import Optional

import httpx

from integrations.departments import NOT_ALLOWED_YET, NotBuiltComplianceDepartment, Ruling

TIMEOUT_S = 10.0
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
            return _parse(resp)
        return _refused(status)


def _parse(resp: httpx.Response) -> Ruling:
    try:
        data = resp.json()
        allowed, lines, rid = data["allowed"], data["unmet_lines"], data["ruling_id"]
    except (ValueError, KeyError, TypeError):
        return _refused("unparseable answer")
    if (not isinstance(allowed, bool) or not isinstance(rid, str) or not isinstance(lines, list)
            or not all(isinstance(x, str) for x in lines) or data.get("gate") != "activation"
            or (allowed and lines) or (not allowed and not lines)):
        return _refused("inconsistent answer")
    return Ruling(allowed, tuple(lines), detail=rid)


def compliance_from_env(env: dict):
    url, tok, caller = env.get("COMPLIANCE_SERVICE_URL"), env.get("COMPLIANCE_SERVICE_TOKEN"), env.get("COMPLIANCE_CALLER_TOKEN")
    if url and tok and caller:
        return HttpComplianceDepartment(url, tok, caller)
    return NotBuiltComplianceDepartment()
