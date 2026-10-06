"""
Ports to everything outside the Client Fix lane (ADR 0017 decision 14). Each has a fail-closed stand-in that says what
is missing; /cfx/v1/status lists which are wired. Selecting a real one refuses start (config.NOT_BUILT). A port that
raises is treated as unavailable or unknown, never as success, and its exception text is dropped. No port is ever
called with the service lock held.

- ``Transport``: carries one connector request to a client platform. The REAL transport (not built) resolves the
  connection's vault reference through the Cybersecurity (22) vault at call time, attaches the token, enforces an
  egress allowlist of exactly the documented API hosts, and returns the HTTP status and parsed JSON. A connector never
  sees a token. Stand-in: ``wired = False`` — the executor refuses ``CONNECTOR_NOT_WIRED`` before any read or write.
- ``Detection``: Revenue Recovery re-detection (detection-py through orchestrator-go). ``rescan`` answers, per finding,
  ``cleared`` or ``present``; anything else is unknown. Stand-in: unknown for everything — nothing is ever counted
  fixed on the engine's own claim.
- ``Finance``: Finance (31). ``request_invoice`` (the quote's up-front Stripe invoice) and ``request_refund`` (an
  Andre-approved refund of unfixed items), plus ``refund_status`` to reconcile an unknown outcome. Stand-in:
  ``not_wired`` — invoices are not created and approved refunds stay ``queued``.
- ``Engineers``: the two fire teams (founder decision 2) running on Claude inside delivery-py's runtime: deer-flow's
  harness behind delivery-py's ``ZbmDockerSandboxProvider`` / ``ZbmGuardrailProvider`` / ``EgressChatModel``
  (Anthropic Messages backend). They receive a brief (findings, check codes, the snapshot-free resource list — never a
  token or a vault reference) and return change sets, which this service validates as untrusted data. Stand-in:
  raises ``ModelNotWired`` (503 ``MODEL_NOT_WIRED``): the Anthropic key is not added (founder decision 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from connectors.base import HttpAnswer, HttpRequest

NOT_WIRED = "not_wired"


class NotWired(Exception):
    pass


class ModelNotWired(Exception):
    pass


@dataclass(frozen=True)
class ConnView:
    """What the transport is told about a connection: ids and the vault reference, nothing else."""
    connection_id: str
    client_id: str
    connector: str
    account_ref: str
    token_ref: Optional[str]


class Transport(Protocol):
    wired: bool

    def call(self, conn: ConnView, request: HttpRequest) -> HttpAnswer: ...


class NotWiredTransport:
    wired = False

    def call(self, conn, request):
        raise NotWired("transport")


@dataclass(frozen=True)
class Delivery:
    status: str                       # delivered | refused | unknown | not_wired
    reference: Optional[str] = None


class Detection(Protocol):
    wired: bool

    def rescan(self, client_id: str, checks: list[dict]) -> dict:
        """``checks``: [{finding_id, check_code, resource}]. Answer: {finding_id: "cleared" | "present" | other}."""
        ...


class NotWiredDetection:
    wired = False

    def rescan(self, client_id, checks) -> dict:
        return {}


class Finance(Protocol):
    wired: bool

    def request_invoice(self, job_id: str, payload: dict) -> Delivery: ...

    def request_refund(self, refund_id: str, payload: dict) -> Delivery:
        """``delivered`` (Finance took it, with its reference) | ``refused`` (certainly not taken) | anything else
        (or an exception) = unknown: the refund stays ``sending`` and is reconciled through ``refund_status``."""
        ...

    def refund_status(self, refund_id: str) -> Delivery: ...


class NotWiredFinance:
    wired = False

    def request_invoice(self, job_id, payload) -> Delivery:
        return Delivery(NOT_WIRED)

    def request_refund(self, refund_id, payload) -> Delivery:
        return Delivery(NOT_WIRED)

    def refund_status(self, refund_id) -> Delivery:
        return Delivery("unknown")


class Engineers(Protocol):
    wired: bool

    def propose(self, team: str, brief: dict) -> list[dict]: ...


class NotWiredEngineers:
    wired = False

    def propose(self, team, brief) -> list[dict]:
        raise ModelNotWired("anthropic key not added")


@dataclass
class Ports:
    transport: Transport
    detection: Detection
    finance: Finance
    engineers: Engineers

    @classmethod
    def default(cls) -> "Ports":
        return cls(NotWiredTransport(), NotWiredDetection(), NotWiredFinance(), NotWiredEngineers())

    def wired(self) -> dict:
        return {k: bool(getattr(getattr(self, k), "wired", False))
                for k in ("transport", "detection", "finance", "engineers")}
