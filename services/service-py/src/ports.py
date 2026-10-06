"""
Ports to everything outside Customer Service (30) + Client Success (29) (ADR 0014). Each has a fail-closed
stand-in that says what is missing; /svc/v1/status lists which are wired. A port that raises is treated as
unavailable by the service and its exception text is dropped. Nothing is ever sent to a port with the lock held.

Sending (email, SMS, chat push): no provider is chosen, so each is ``NotWiredSender`` (``wired = False``): outbound
messages stay ``queued``, visibly, and the outbound tick does not touch them (nothing is recorded as sent).
Voice: no provider; ``SVC_VOICE_PROVIDER`` refuses start. Alerts to Andre: ``NotWiredAlerts`` (recorded, status
``not_wired``). Handoffs: Legal (37) has a thin client (legal_client.py); Finance (31), Cybersecurity (22) and
Compliance (38) are NotWired (the route each needs is in the ADR unlock list). Signals for the health score:
results trend, payment status, contract end dates: NotWired answers ``available=False`` (the score shows the signal
as unknown, never as fine).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

NOT_WIRED = "not_wired"


# --- sending ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Outbound:
    """What a sending provider receives: the address, the brand identity it is sent from, and the exact text."""
    message_id: str
    brand: str
    channel: str
    to: str
    sender: str
    subject: Optional[str]
    text: str


class Sender(Protocol):
    wired: bool

    def send(self, msg: Outbound) -> str: ...            # sent | failed


class NotWiredSender:
    wired = False

    def __init__(self, channel: str):
        self.channel = channel

    def send(self, msg: Outbound) -> str:
        return NOT_WIRED


# --- alerts to Andre --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Alert:
    """Codes and ids only: an alert never carries a message body, an address or any caller-supplied text."""
    alert_id: str
    code: str
    subject: str


class AlertPort(Protocol):
    wired: bool

    def send(self, alert: Alert) -> str: ...             # delivered | failed | not_wired


class NotWiredAlerts:
    wired = False

    def send(self, alert: Alert) -> str:
        return NOT_WIRED


# --- handoffs -----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Handoff:
    status: str                       # delivered | refused | unavailable | not_wired
    reference: Optional[str] = None


@dataclass(frozen=True)
class HandoffRequest:
    """Refs and codes only: the department reads the ticket here (it is never sent the message body)."""
    handoff_id: str
    ticket_id: str
    brand: str
    category: str
    kind: str                         # legal-py intake kind, or the department's own code
    account_id: Optional[str]


class HandoffPort(Protocol):
    wired: bool

    def handoff(self, req: HandoffRequest) -> Handoff: ...


class NotWiredHandoff:
    wired = False

    def __init__(self, department: str):
        self.department = department

    def handoff(self, req: HandoffRequest) -> Handoff:
        return Handoff(NOT_WIRED)


# --- health-score signals ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Trend:
    available: bool
    direction: Optional[str] = None   # up | flat | down


@dataclass(frozen=True)
class PaymentStatus:
    available: bool
    status: Optional[str] = None      # current | late | failed


@dataclass(frozen=True)
class ContractEnd:
    available: bool
    end_date: Optional[str] = None    # YYYY-MM-DD


class ResultsPort(Protocol):
    wired: bool

    def trend(self, account_id: str) -> Trend: ...


class FinanceSignals(Protocol):
    wired: bool

    def payment_status(self, account_id: str) -> PaymentStatus: ...


class ContractsPort(Protocol):
    wired: bool

    def contract_end(self, account_id: str) -> ContractEnd: ...


class NotWiredResults:
    wired = False

    def trend(self, account_id: str) -> Trend:
        return Trend(False)


class NotWiredFinanceSignals:
    wired = False

    def payment_status(self, account_id: str) -> PaymentStatus:
        return PaymentStatus(False)


class NotWiredContracts:
    wired = False

    def contract_end(self, account_id: str) -> ContractEnd:
        return ContractEnd(False)


HANDOFF_DEPARTMENTS = ("legal_37", "finance_31", "cybersecurity_22", "compliance_38")


@dataclass
class Ports:
    senders: dict = field(default_factory=dict)          # channel -> Sender
    alerts: AlertPort = None                             # type: ignore[assignment]
    handoffs: dict = field(default_factory=dict)         # department -> HandoffPort
    results: ResultsPort = None                          # type: ignore[assignment]
    finance: FinanceSignals = None                       # type: ignore[assignment]
    contracts: dict = field(default_factory=dict)        # "legal_37" | "onboarding" -> ContractsPort

    @classmethod
    def default(cls) -> "Ports":
        return cls(senders={c: NotWiredSender(c) for c in ("email", "sms", "chat")}, alerts=NotWiredAlerts(),
                   handoffs={d: NotWiredHandoff(d) for d in HANDOFF_DEPARTMENTS}, results=NotWiredResults(),
                   finance=NotWiredFinanceSignals(),
                   contracts={"legal_37": NotWiredContracts(), "onboarding": NotWiredContracts()})

    def wired(self) -> dict:
        return {"senders": {c: bool(getattr(s, "wired", False)) for c, s in sorted(self.senders.items())},
                "alerts": bool(getattr(self.alerts, "wired", False)),
                "handoffs": {d: bool(getattr(p, "wired", False)) for d, p in sorted(self.handoffs.items())},
                "results": bool(getattr(self.results, "wired", False)),
                "finance": bool(getattr(self.finance, "wired", False)),
                "contracts": {k: bool(getattr(p, "wired", False)) for k, p in sorted(self.contracts.items())},
                "voice": False}
