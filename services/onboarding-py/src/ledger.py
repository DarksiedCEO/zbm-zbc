"""
LedgerClient — BUILD_CONTRACTS.md section 2.

Every cross-department crossing and every gate ruling in Onboarding is
written through ``LedgerClient.record_event`` with department
"onboarding". If the write fails, the action that required it does NOT
proceed: callers write first, then change state, and a
``LedgerWriteError`` propagates to the API, which answers 503 with
``proceeded: false``.

Payloads never contain credentials: the service only ever passes
secret-free structures, and ``record_event`` additionally runs the
payload and summary through ``redaction.scrub`` as defense in depth.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional, Protocol

import httpx

from redaction import scrub, scrub_obj

DEPARTMENT = "onboarding"

_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_NAME = re.compile(r"^[a-z0-9_]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class LedgerWriteError(RuntimeError):
    """The ledger write failed; the action must not proceed."""


def payload_sha256(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def new_event_id() -> str:
    return f"onb-{uuid.uuid4().hex}"


def clean_summary(summary: str) -> str:
    s = _CONTROL.sub(" ", scrub(summary)).strip() or "(no summary)"
    return s[:280]


def validate_event(event_id: str, department: str, event_type: str, actor: str, subject_id: str) -> None:
    if not _ID.match(event_id):
        raise LedgerWriteError("invalid event_id for ledger contract")
    if not _NAME.match(department) or not _NAME.match(event_type) or not _NAME.match(actor):
        raise LedgerWriteError("invalid department/event_type/actor for ledger contract")
    if not _ID.match(subject_id):
        raise LedgerWriteError("invalid subject_id for ledger contract")


class LedgerClient(Protocol):
    def record_event(
        self,
        event_id: str,
        department: str,
        event_type: str,
        actor: str,
        subject_id: str,
        payload: dict,
        summary: str,
    ) -> None: ...


def build_body(event_id, department, event_type, actor, subject_id, payload, summary) -> dict:
    validate_event(event_id, department, event_type, actor, subject_id)
    safe_payload = scrub_obj(payload)
    return {
        "event_id": event_id,
        "department": department,
        "event_type": event_type,
        "actor": actor,
        "subject_id": subject_id,
        "payload_sha256": payload_sha256(safe_payload),
        "summary": clean_summary(summary),
    }


class HttpLedgerClient:
    """POST /ledger/events on ledger-rust. 201 (new) and 200 (idempotent
    retry) are success; anything else — 409, 400, 401, 5xx, timeout,
    connection refused — is a LedgerWriteError. Error messages never
    include the token or the payload."""

    def __init__(self, base_url: str, token: str, timeout_s: float = 5.0, transport: Optional[httpx.BaseTransport] = None):
        if not base_url or not token:
            raise ValueError("HttpLedgerClient needs LEDGER_SERVICE_URL and LEDGER_SERVICE_TOKEN")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_s,
            transport=transport,
        )

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        body = build_body(event_id, department, event_type, actor, subject_id, payload, summary)
        try:
            r = self._client.post("/ledger/events", json=body)
        except httpx.HTTPError as exc:
            raise LedgerWriteError(f"ledger unreachable ({type(exc).__name__})") from None
        if r.status_code in (200, 201):
            return
        if r.status_code == 409:
            raise LedgerWriteError("ledger rejected event: same event_id with different content (409)")
        raise LedgerWriteError(f"ledger rejected event (HTTP {r.status_code})")


class UnconfiguredLedgerClient:
    """Default when LEDGER_SERVICE_URL / LEDGER_SERVICE_TOKEN are unset:
    every write fails, so every action that needs a record is refused."""

    def record_event(self, *args, **kwargs) -> None:
        raise LedgerWriteError(
            "ledger not configured (LEDGER_SERVICE_URL / LEDGER_SERVICE_TOKEN unset); "
            "refusing to proceed without an evidence record"
        )


@dataclass
class FakeLedgerClient:
    """Test double. Stores the full request body AND the scrubbed payload
    so tests can assert no secret reaches the ledger."""

    fail: bool = False
    events: list[dict] = field(default_factory=list)
    payloads: list[dict] = field(default_factory=list)

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        body = build_body(event_id, department, event_type, actor, subject_id, payload, summary)
        if self.fail:
            raise LedgerWriteError("fake ledger configured to fail")
        self.events.append(body)
        self.payloads.append(scrub_obj(payload))

    def types(self) -> list[str]:
        return [e["event_type"] for e in self.events]
