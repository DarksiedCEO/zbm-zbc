"""
SipDialerPort — the integration seam for self-hosted SIP telephony via
`livekit/agents` (ADR 0002, Decision 3).

HONEST STATUS: no concrete LiveKit-backed implementation exists in this
repository. This sandbox has no real phone number, no SIP trunk
credentials, and no carrier account, so a live implementation cannot be
built or tested here — building one anyway would produce code that has
never actually placed a call, which is worse than not having it, because
it would look tested when it is not.

What DOES exist and IS real: the decision logic in
`agents/callback_orchestration.py` — whether a callback should happen,
which line it should go out on, and how the attempt should be recorded —
is fully implemented and fully tested against this Protocol. Wiring a
real `LiveKitSipDialer(SipDialerPort)` is a bounded, well-defined next
step: implement `place_call` against LiveKit's SIP trunk API, register
the class where `callback_orchestration.py` currently takes an injected
dialer, and nothing else in this department needs to change.

`InMemorySipDialer` below is the test double used by the test suite. It
is NOT a stand-in for real telephony — it exists only to let the
decision logic be tested without a live line.

Fix wave 1, F3 (Sep 24 2026): `place_call` no longer takes a phone number.
It takes a ContactAuthorization minted by OutboundContactGate, and an
implementation gets the number ONLY from `authorization.redeem(TaskChannel.CALL)`
immediately before dialing. redeem() re-checks the calling-hours window
(in every zone the number could be in) and the per-number attempt limits
on the gate's clock and records the attempt atomically; it raises
ContactRefused otherwise, and the implementation must let that propagate
without dialing. This is what makes the calling-hours rule impossible to
skip for any future dialer: there is no way to hand it a bare number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from fulfillment_schema import TaskChannel
from outbound_gate import ContactAuthorization


@dataclass(frozen=True)
class DialAttemptResult:
    call_id: str
    placed: bool
    line_id: str
    detail: str
    placed_at: datetime


class SipDialerPort(Protocol):
    """Abstract seam for placing an outbound callback. A real
    implementation talks to a LiveKit SIP trunk; this repository does not
    ship one — see module docstring."""

    def place_call(self, authorization: ContactAuthorization, line_id: str) -> DialAttemptResult:
        """Must call authorization.redeem(TaskChannel.CALL) to obtain the
        number, immediately before dialing, and must not dial if it raises."""
        ...


class NotWiredSipDialer:
    """Default dialer used when no concrete implementation is configured.
    Fails loudly and immediately rather than silently pretending a call
    was placed — the honest behavior when there is no real carrier
    connection behind this seam yet."""

    def place_call(self, authorization: ContactAuthorization, line_id: str) -> DialAttemptResult:
        # Never redeems: no contact is made, so no attempt is consumed.
        raise NotImplementedError(
            "No SipDialerPort implementation is configured. This sandbox has "
            "no live SIP trunk/LiveKit credentials, so outbound calling is not "
            "wired. Implement SipDialerPort against livekit/agents and inject "
            "it here to go live — see integrations/sip_dialer.py module "
            "docstring and docs/adr/0002-fulfillment-department-architecture.md."
        )


@dataclass
class InMemorySipDialer:
    """Test double only. Records what WOULD have been dialed without
    placing a real call. Used by the test suite to verify
    callback_orchestration's decision logic in isolation from telephony."""

    calls_placed: list[DialAttemptResult] = field(default_factory=list)
    numbers_dialed: list[str] = field(default_factory=list)
    fail_next: bool = False

    def place_call(self, authorization: ContactAuthorization, line_id: str) -> DialAttemptResult:
        phone_number = authorization.redeem(TaskChannel.CALL)  # raises ContactRefused -> no dial
        self.numbers_dialed.append(phone_number)
        result = DialAttemptResult(
            call_id=f"sim-{len(self.calls_placed) + 1}",
            placed=not self.fail_next,
            line_id=line_id,
            detail="simulated dial (test double, not a real call)" if not self.fail_next
            else "simulated dial failure (test double, not a real call)",
            placed_at=datetime.now(timezone.utc),
        )
        self.fail_next = False
        self.calls_placed.append(result)
        return result
