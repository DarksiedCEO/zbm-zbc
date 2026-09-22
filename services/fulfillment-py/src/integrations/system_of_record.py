"""
SystemOfRecordPort — the generic write-back seam behind the
Numa-style "resolve to completion" model (ADR 0002, Decision 4).

HONEST STATUS: no concrete CRM/job-management integration exists in
this repository (no ServiceTitan, Jobber, HubSpot, etc.). Which system
to write to is a per-client fact this build does not have yet — wiring
one in before a real client names it would be guessing at an API this
repo has never called and has no way to test against.

What DOES exist and IS real: `agents/resolution_writeback.py` always
produces a `ResolutionRecord` for every resolved call/appointment/task
and always ATTEMPTS a write-back through this port — the completeness
guarantee (nothing resolves silently) does not depend on which concrete
system is behind the seam. `write_back_status=NOT_CONFIGURED` is the
honest, structurally-visible answer when (as here) no adapter is
wired — never silently treated as success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from fulfillment_schema import ResolutionRecord, WriteBackStatus


@dataclass(frozen=True)
class WriteBackResult:
    status: WriteBackStatus
    detail: str


class SystemOfRecordPort(Protocol):
    def write_back(self, record: ResolutionRecord) -> WriteBackResult:
        ...


class NotConfiguredSystemOfRecord:
    """Default adapter when no client-specific CRM/job system is wired.
    Returns NOT_CONFIGURED rather than raising — unlike the SIP seam,
    "no system of record configured yet" is an expected, common steady
    state (a brand-new client may not have one wired on day one), not an
    error condition. The distinction matters: this must never be
    confused with WriteBackStatus.SUCCESS by any caller."""

    def write_back(self, record: ResolutionRecord) -> WriteBackResult:
        return WriteBackResult(
            status=WriteBackStatus.NOT_CONFIGURED,
            detail=(
                "No SystemOfRecordPort implementation configured for this "
                "client. Resolution was recorded locally but not written to "
                "any external CRM/job-management system."
            ),
        )


@dataclass
class InMemorySystemOfRecord:
    """Test double only. Simulates a working system-of-record adapter so
    the write-back completeness logic can be tested end-to-end without a
    real CRM connection."""

    written: list[ResolutionRecord] = field(default_factory=list)
    fail_next: bool = False

    def write_back(self, record: ResolutionRecord) -> WriteBackResult:
        if self.fail_next:
            self.fail_next = False
            return WriteBackResult(
                status=WriteBackStatus.FAILED,
                detail="simulated write-back failure (test double)",
            )
        self.written.append(record)
        return WriteBackResult(
            status=WriteBackStatus.SUCCESS,
            detail="simulated write-back succeeded (test double, not a real CRM)",
        )
