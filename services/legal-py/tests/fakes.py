"""
TEST-ONLY fakes. They live here, never in src/, so no production code path can be wired to a dependency that
says "fine" (spec G2).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ledger import LedgerConflict, LedgerNotRecorded, LedgerQueryFailed, event_field_problems, payload_sha256  # noqa: E402
from ports import ComplianceRow, Delivery, EnvelopeAnswer, ProposalAnswer  # noqa: E402


@dataclass
class FakeLedgerClient:
    """Enforces the BUILD_CONTRACTS §2 field rules and idempotency (201/200/409); ``fail_all`` simulates an outage."""

    events: list = field(default_factory=list)
    fail_all: bool = False
    fail_on_type: Optional[str] = None
    readable: bool = True
    calls: int = 0

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        self.calls += 1
        if self.fail_all or (self.fail_on_type and event_type == self.fail_on_type):
            raise LedgerNotRecorded("simulated ledger outage (test double)")
        bad = event_field_problems(event_id, department, event_type, actor, subject_id, summary)
        if bad:
            raise LedgerNotRecorded(f"HTTP 400 (invalid {bad}, test double)")
        entry = {"event_id": event_id, "department": department, "event_type": event_type, "actor": actor,
                 "subject_id": subject_id, "payload_sha256": payload_sha256(payload), "summary": summary}
        for e in self.events:
            if e["event_id"] == event_id:
                if {k: e[k] for k in entry} == entry:
                    return
                raise LedgerConflict("409 (test double)")
        self.events.append({**entry, "payload": payload})

    def verify(self) -> bool:
        return not self.fail_all

    def entries(self) -> list:
        if self.fail_all or not self.readable:
            raise LedgerQueryFailed("simulated ledger outage (test double)")
        return [{k: v for k, v in e.items() if k != "payload"} for e in self.events]

    def of_type(self, t: str) -> list:
        return [e for e in self.events if e["event_type"] == t]


class FakeCompliance:
    """Records proposals; ``down`` answers unavailable; ``verified`` is the set of row ids reported verified."""

    def __init__(self):
        self.proposals: list[tuple[str, dict]] = []
        self.down = False
        self.refuse = False
        self.verified: set[str] = set()
        self.n = 0

    def create_proposal(self, request_id, body):
        if self.down:
            return ProposalAnswer("unavailable")
        if self.refuse:
            return ProposalAnswer("refused", None, 422)
        for rid_, b in self.proposals:
            if rid_ == request_id:
                return ProposalAnswer("created", f"prop-{rid_}", 200)
        self.n += 1
        self.proposals.append((request_id, body))
        return ProposalAnswer("created", f"prop-{request_id}", 201)

    def row(self, obligation_id):
        if self.down:
            return ComplianceRow(False, obligation_id)
        return ComplianceRow(True, obligation_id, "verified" if obligation_id in self.verified else "unverified", 2)


class FakeCounsel:
    def __init__(self, delivered: bool = True):
        self.packages: list[dict] = []
        self.delivered = delivered

    def deliver(self, package):
        self.packages.append(package)
        return Delivery(self.delivered, "fake counsel inbox")


class FakeCyber:
    def __init__(self):
        self.freezes: list = []

    def freeze(self, hold_id, systems, subject_refs):
        self.freezes.append((hold_id, tuple(systems)))
        return Delivery(True, "frozen (fake)")


class FakeDept:
    def __init__(self, name: str):
        self.name = name
        self.sent: list = []

    def notify(self, kind, subject_id, payload):
        self.sent.append((kind, subject_id, payload))
        return Delivery(True, f"{self.name} (fake)")


class FakeESign:
    def __init__(self):
        self.n = 0

    def create_envelope(self, doc_id, version, doc_sha256, signer_refs):
        self.n += 1
        return EnvelopeAnswer(True, f"env-{self.n}")

    def status(self, envelope_id):
        return "sent"

    def certificate(self, envelope_id):
        return b"certificate"
