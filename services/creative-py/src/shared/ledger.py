"""
Evidence ledger client — BUILD_CONTRACTS.md section 2, implemented as written.

`LedgerClient.record_event(event_id, department, event_type, actor,
subject_id, payload, summary) -> None` raises `LedgerRecordError` on ANY
failure (transport error, non-2xx, 409 conflict, local validation failure).
Callers treat that as "the decision did not take effect" — see
`EvidenceRecorder` and every workflow in `zbm/` and `zbc/`, which record
FIRST and only then commit state.

Implementations:
- `HttpLedgerClient`   — real HTTP against ledger-rust `POST /ledger/events`.
- `UnconfiguredLedgerClient` — the API default when LEDGER_SERVICE_URL /
  LEDGER_SERVICE_TOKEN are unset: every record fails, so every decision is
  refused (fail closed, never silently unrecorded).
- `FakeLedgerClient`   — in-memory test double that enforces the same
  field contract and idempotency semantics (201/200/409) as the real one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

DEPARTMENT = "creative_production"

_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class LedgerRecordError(Exception):
    """The event was NOT recorded. The decision that needed it must not
    take effect."""


def payload_sha256(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_event_fields(
    event_id: str, department: str, event_type: str, actor: str, subject_id: str, summary: str
) -> None:
    """Local check of the section-2 field rules so a malformed event fails
    here, loudly, instead of as an opaque 400 from the ledger."""
    problems = []
    if not _EVENT_ID_RE.match(event_id):
        problems.append("event_id")
    for name, value in (("department", department), ("event_type", event_type), ("actor", actor)):
        if not _NAME_RE.match(value):
            problems.append(name)
    if not _EVENT_ID_RE.match(subject_id):
        problems.append("subject_id")
    if not (1 <= len(summary) <= 280) or _CONTROL_RE.search(summary):
        problems.append("summary")
    if problems:
        raise LedgerRecordError(f"ledger event fails contract validation on: {', '.join(problems)}")


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


class HttpLedgerClient:
    """Real client for ledger-rust `POST /ledger/events`."""

    def __init__(self, base_url: str, token: str, timeout: float = 5.0, transport: httpx.BaseTransport | None = None):
        if not base_url or not token:
            raise ValueError("HttpLedgerClient needs both a base URL and a token")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport

    @classmethod
    def from_env(cls) -> "HttpLedgerClient":
        url = os.environ.get("LEDGER_SERVICE_URL")
        token = os.environ.get("LEDGER_SERVICE_TOKEN")
        if not url or not token:
            raise ValueError("LEDGER_SERVICE_URL and LEDGER_SERVICE_TOKEN must both be set")
        return cls(url, token)

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        validate_event_fields(event_id, department, event_type, actor, subject_id, summary)
        body = {
            "event_id": event_id,
            "department": department,
            "event_type": event_type,
            "actor": actor,
            "subject_id": subject_id,
            "payload_sha256": payload_sha256(payload),
            "summary": summary,
        }
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                resp = client.post(
                    f"{self._base_url}/ledger/events",
                    json=body,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.HTTPError as exc:
            raise LedgerRecordError(f"ledger unreachable: {type(exc).__name__}") from exc
        if resp.status_code in (200, 201):
            return
        if resp.status_code == 409:
            raise LedgerRecordError("ledger refused: event_id already recorded with different content (409)")
        raise LedgerRecordError(f"ledger refused the event: HTTP {resp.status_code}")


class UnconfiguredLedgerClient:
    """Default when the ledger isn't configured: every record fails."""

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        raise LedgerRecordError(
            "evidence ledger not configured (LEDGER_SERVICE_URL / LEDGER_SERVICE_TOKEN unset); "
            "no decision can take effect without a ledger record"
        )


@dataclass
class FakeLedgerClient:
    """Test double. Same validation and idempotency semantics as the
    contract; `fail_all` / `fail_next` simulate an unreachable ledger."""

    events: list[dict] = field(default_factory=list)
    fail_all: bool = False
    fail_next: bool = False

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        if self.fail_all or self.fail_next:
            self.fail_next = False
            raise LedgerRecordError("simulated ledger outage (test double)")
        validate_event_fields(event_id, department, event_type, actor, subject_id, summary)
        entry = {
            "event_id": event_id,
            "department": department,
            "event_type": event_type,
            "actor": actor,
            "subject_id": subject_id,
            "payload_sha256": payload_sha256(payload),
            "summary": summary,
        }
        for existing in self.events:
            if existing["event_id"] == event_id:
                if {k: existing[k] for k in entry} == entry:
                    return  # idempotent retry
                raise LedgerRecordError("event_id conflict (409, test double)")
        self.events.append({**entry, "payload": payload})

    def of_type(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e["event_type"] == event_type]


def _clean_summary(summary: str) -> str:
    cleaned = _CONTROL_RE.sub(" ", summary).strip() or "(no summary)"
    return cleaned[:280]


@dataclass
class EvidenceRecorder:
    """Thin wrapper every workflow uses. Always department
    "creative_production". Returns the event_id on success; raises
    LedgerRecordError otherwise (callers must not commit)."""

    client: LedgerClient

    def record(self, event_type: str, actor: str, subject_id: str, payload: dict[str, Any], summary: str) -> str:
        event_id = f"cp:{event_type}:{uuid.uuid4().hex}"
        self.client.record_event(
            event_id=event_id,
            department=DEPARTMENT,
            event_type=event_type,
            actor=actor,
            subject_id=subject_id,
            payload=payload,
            summary=_clean_summary(summary),
        )
        return event_id

    def try_record(self, event_type: str, actor: str, subject_id: str, payload: dict[str, Any], summary: str) -> str | None:
        """Best-effort record for REFUSALS (guardrail trips). A refusal
        stands whether or not it could be recorded — failing to record a
        refusal must never turn it into an approval."""
        try:
            return self.record(event_type, actor, subject_id, payload, summary)
        except LedgerRecordError:
            return None
