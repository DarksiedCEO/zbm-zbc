"""
Ports to everything outside Cybersecurity (22) (ADR 0012). Each has a fail-closed stand-in that says what is
missing; /health lists which are wired.

- ``ComplianceControls``: push C-14 results to Compliance (38). Stand-in: ``unavailable``.
- ``AlertChannel`` x3 (text message, email, phone push): Andre picked all three; no provider is chosen yet, so
  each is ``NotWiredChannel`` (status ``not_wired``) and the alert stays queued in the incident's record.
- ``PreservationAdapter`` per system Legal can ask us to preserve (email, chat, drive): none is connected yet, so a
  Legal hold on those systems answers ``delivered: False`` with the systems named, never a false "frozen".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class ControlPush:
    status: str                  # delivered | refused | unavailable
    http_status: Optional[int] = None


class ComplianceControls(Protocol):
    def push_control_result(self, control_id: str, request_id: str, result: str, tested_at: str,
                            evidence: list[dict]) -> ControlPush: ...


class NotWiredCompliance:
    def push_control_result(self, control_id, request_id, result, tested_at, evidence) -> ControlPush:
        return ControlPush("unavailable")


@dataclass(frozen=True)
class AlertMessage:
    """Codes and ids only: an alert never carries a secret, a token, a value or any caller-supplied text."""
    alert_id: str
    severity: str
    code: str
    incident_id: str
    subject: str


class AlertChannel(Protocol):
    name: str

    def send(self, msg: AlertMessage) -> str: ...      # delivered | failed | not_wired


class NotWiredChannel:
    def __init__(self, name: str):
        self.name = name

    def send(self, msg: AlertMessage) -> str:
        return "not_wired"


class PreservationAdapter(Protocol):
    system: str

    def preserve(self, hold_id: str, subject_refs: list[str]) -> bool: ...

    def release(self, hold_id: str) -> bool: ...


CHANNELS = ("sms", "email", "push")


@dataclass
class Ports:
    compliance: ComplianceControls
    channels: dict
    preservation: dict

    @classmethod
    def default(cls) -> "Ports":
        return cls(NotWiredCompliance(), {c: NotWiredChannel(c) for c in CHANNELS}, {})
