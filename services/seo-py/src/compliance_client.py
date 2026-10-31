"""
Compliance (38) activation check for client runs (ADR 0024, founder decision Oct 30, 2026: Compliance is the hard
gate before a client is served).

Before a client tenant's audit or schedule is recorded, before every scheduled slot of it runs, and before a client
report is rendered, this service asks Compliance ``GET /compliance/v1/clients/{client_id}/activation`` (the service
bearer token plus this service's ``seo_02`` caller token in ``X-Compliance-Caller-Token``) for the tenant's client
(``finance_client_id``, the one client id Finance, Legal and Compliance share). Only a current ALLOW, from a
Compliance running its pinned (production) seed, lets the run proceed. Everything else refuses, and nothing can
override it (unlike an unverifiable invoice: there is no Andre override for Compliance):

  NOT ACTIVATED   COMPLIANCE_NOT_ACTIVATED (409): Compliance answered, and the answer is not a current ALLOW
  UNVERIFIABLE    COMPLIANCE_NOT_CONFIGURED, COMPLIANCE_UNAVAILABLE, COMPLIANCE_AUTH_REFUSED,
                  COMPLIANCE_RESPONSE_INVALID (503): no usable answer; fail closed

The transport is finance_client.py's (the same bounded deadline over every attempt and every socket operation, the
host lookup inside it, no redirects, a body cap, ``Accept-Encoding: identity``). Reads only; nothing is written to
Compliance.
"""

from __future__ import annotations

import re
from typing import Optional

from finance_client import RETRY_STATUSES, FinanceClient, _json, _Retry

CALLER_NAME_AT_COMPLIANCE = "seo_02"
CMP_CALLER_HEADER = "X-Compliance-Caller-Token"
CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,100}")
JURISDICTION = re.compile(r"US-[A-Z]{2}|PR|GU|VI|AS|MP")
RULING_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
UNVERIFIABLE = frozenset({"COMPLIANCE_NOT_CONFIGURED", "COMPLIANCE_UNAVAILABLE", "COMPLIANCE_AUTH_REFUSED",
                          "COMPLIANCE_RESPONSE_INVALID"})
NOT_ACTIVATED = "COMPLIANCE_NOT_ACTIVATED"
ALLOW = "ALLOW"


class ComplianceCheckError(Exception):
    """No current ALLOW. ``code`` is NOT_ACTIVATED or one of UNVERIFIABLE."""

    def __init__(self, code: str):
        if code not in UNVERIFIABLE and code != NOT_ACTIVATED:
            raise AssertionError(f"not a compliance check code: {code}")
        super().__init__(code)
        self.code = code


def parse_activation(obj, client_id: str) -> dict:
    """compliance-py ``activation_status`` -> {ruling_id, jurisdiction} for a current ALLOW; raises otherwise."""
    try:
        if not isinstance(obj, dict) or obj.get("client_id") != client_id or obj.get("lane") != "client":
            raise ValueError("not this client's activation")
        decision, current, pinned = obj.get("decision"), obj.get("current"), obj.get("seed_pinned")
        if decision not in ("ALLOW", "REFUSE") or not isinstance(current, bool) or not isinstance(pinned, bool):
            raise ValueError("decision")
        rid, juris = obj.get("ruling_id"), obj.get("jurisdiction")
        if rid is not None and (not isinstance(rid, str) or not RULING_ID.fullmatch(rid)):
            raise ValueError("ruling id")
        if juris is not None and (not isinstance(juris, str) or not JURISDICTION.fullmatch(juris)):
            raise ValueError("jurisdiction")
    except ValueError:
        raise ComplianceCheckError("COMPLIANCE_RESPONSE_INVALID") from None
    if decision != ALLOW or current is not True or pinned is not True or rid is None or juris is None:
        raise ComplianceCheckError(NOT_ACTIVATED)
    return {"verdict": ALLOW, "ruling_id": rid, "jurisdiction": juris}


class NotConnectedCompliance:
    """No Compliance (38) client configured: every check is COMPLIANCE_NOT_CONFIGURED (every client run refused)."""

    connected = False

    def activation(self, client_id: str) -> dict:
        raise ComplianceCheckError("COMPLIANCE_NOT_CONFIGURED")


class ComplianceClient(FinanceClient):
    """finance_client.FinanceClient's bounded transport, pointed at Compliance with this service's Compliance caller
    token. Only ``activation`` is offered."""

    connected = True

    def __init__(self, base_url: str, service_token: str, caller_token: str, **kw):
        super().__init__(base_url, service_token, caller_token, **kw)
        self._headers = {"Authorization": f"Bearer {service_token}", CMP_CALLER_HEADER: caller_token,
                         "Accept": "application/json", "Accept-Encoding": "identity"}

    def lookup(self, invoice_id: str) -> dict:          # never a Finance lookup through Compliance's credentials
        raise AssertionError("ComplianceClient answers activation checks only")

    def activation(self, client_id: str) -> dict:
        if not isinstance(client_id, str) or not CLIENT_ID.fullmatch(client_id):
            raise ComplianceCheckError("COMPLIANCE_RESPONSE_INVALID")
        end = self._mono() + self.deadline_s
        last = "COMPLIANCE_UNAVAILABLE"
        for attempt in range(self.attempts):
            left = end - self._mono()
            if left <= 0:
                break
            try:
                status, body = self._get(f"{self._base}/compliance/v1/clients/{client_id}/activation",
                                         min(self.timeout_s, left), end)
            except _Retry:
                last = "COMPLIANCE_UNAVAILABLE"
            else:
                if status == 200:
                    return parse_activation(_json(body), client_id)
                if status in (401, 403):
                    raise ComplianceCheckError("COMPLIANCE_AUTH_REFUSED")
                if status not in RETRY_STATUSES:
                    raise ComplianceCheckError("COMPLIANCE_RESPONSE_INVALID")
                last = "COMPLIANCE_UNAVAILABLE"
            if attempt + 1 < self.attempts:
                pause = self.backoff_s[min(attempt, len(self.backoff_s) - 1)] if self.backoff_s else 0.0
                if self._mono() + pause >= end:
                    break
                self._sleep(pause)
        raise ComplianceCheckError(last)


def check(port, client_id: Optional[str]) -> dict:
    """The verdict record for a client run: {verdict: ALLOW, ruling_id, jurisdiction}; raises ComplianceCheckError."""
    if not client_id:
        raise ComplianceCheckError("COMPLIANCE_NOT_ACTIVATED")
    try:
        return port.activation(client_id)
    except ComplianceCheckError:
        raise
    except Exception:  # noqa: BLE001 - any other failure of the port is "no usable answer"
        raise ComplianceCheckError("COMPLIANCE_UNAVAILABLE") from None


__all__ = ["ALLOW", "CALLER_NAME_AT_COMPLIANCE", "ComplianceCheckError", "ComplianceClient", "NotConnectedCompliance",
           "UNVERIFIABLE", "check", "parse_activation"]
