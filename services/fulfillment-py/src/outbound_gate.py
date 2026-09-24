"""
OutboundContactGate — the single place automated outbound contact (call,
SMS, email) is authorized (fix wave 1, F3).

F3 (High, CONFIRMED): the recipient's zone was whatever the caller sent,
and redial protection was keyed by the caller-chosen task_id, so at 02:00
Los Angeles a caller claiming "UTC" got a call placed, and five fresh task
ids got five calls to the same number. The gate replaces both decisions
with ones the caller cannot steer:

  1. WHEN: the window (ContactWindow, default 08:00-21:00) must hold in
     every zone plausible for the NUMBER plus the claimed zone
     (recipient_zones.zones_to_check). The clock is the gate's own,
     read at authorization AND again at the moment the transport takes the
     number (redeem) — never a caller value, never a value captured at the
     start of a long batch.
  2. HOW OFTEN: attempt limits keyed by the normalized phone number (and,
     additionally, by customer_id when there is one), across all channels:
     at most `max_per_24h` attempts in any rolling 24 hours and at least
     `min_spacing` between attempts. Defaults 3 and 2h; configuration may
     only narrow them (fewer attempts, more spacing).
  3. WHO MAY SEND: a transport (SipDialerPort, MessageSenderPort) receives a
     ContactAuthorization, not a phone number. It can only be minted here,
     is bound to one channel, and yields the number once, from redeem(),
     which re-checks the window and limits and records the attempt
     atomically under the gate's lock. A transport that never redeems (the
     not-wired stand-ins) made no contact and consumes no attempt. Any
     future SMS/email/voice sender therefore cannot reach a number without
     passing this gate.

State is in process memory: it resets on restart and is not shared across
replicas (same limitation as the rest of this service — ADR 0002,
Decision 8; the durable fix is the deferred persistence layer).
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from contact_window import ContactWindow
from fulfillment_schema import TaskChannel
from recipient_zones import zones_to_check

AUTOMATED_CHANNELS = frozenset({TaskChannel.CALL, TaskChannel.SMS, TaskChannel.EMAIL})

HARD_MAX_ATTEMPTS_PER_24H = 3
HARD_MIN_SPACING = timedelta(hours=2)
_ROLLING = timedelta(hours=24)


class ContactRefused(Exception):
    """Raised by ContactAuthorization.redeem() when contact is no longer
    permitted at the moment the transport asks for the number."""


@dataclass(frozen=True)
class AttemptLimits:
    max_per_24h: int = HARD_MAX_ATTEMPTS_PER_24H
    min_spacing: timedelta = HARD_MIN_SPACING

    def __post_init__(self) -> None:
        if not (1 <= self.max_per_24h <= HARD_MAX_ATTEMPTS_PER_24H):
            raise ValueError(f"max attempts per 24h must be 1-{HARD_MAX_ATTEMPTS_PER_24H} (may only be narrowed)")
        if not (HARD_MIN_SPACING <= self.min_spacing <= _ROLLING):
            raise ValueError("minimum spacing must be between 120 and 1440 minutes (may only be widened)")


def parse_attempt_limits(max_raw: str | None, spacing_minutes_raw: str | None) -> AttemptLimits:
    try:
        max_n = int(max_raw) if max_raw is not None else HARD_MAX_ATTEMPTS_PER_24H
        spacing = timedelta(minutes=int(spacing_minutes_raw)) if spacing_minutes_raw is not None else HARD_MIN_SPACING
    except ValueError:
        raise ValueError("attempt limits must be integers") from None
    return AttemptLimits(max_n, spacing)


_MINT = object()


class ContactAuthorization:
    """Permission for ONE automated contact on ONE channel. Only
    OutboundContactGate.authorize() can create one. A transport gets the
    number from redeem(), exactly once."""

    __slots__ = ("_gate", "_channel", "_phone", "_customer_id", "_zones", "_redeemed")

    def __init__(self, _mint: object, gate: "OutboundContactGate", channel: TaskChannel,
                 phone: str, customer_id: str | None, zones: tuple[str, ...]):
        if _mint is not _MINT:
            raise TypeError("ContactAuthorization can only be issued by OutboundContactGate.authorize()")
        self._gate = gate
        self._channel = channel
        self._phone = phone
        self._customer_id = customer_id
        self._zones = zones
        self._redeemed = False

    @property
    def channel(self) -> TaskChannel:
        return self._channel

    @property
    def customer_id(self) -> str | None:
        return self._customer_id

    def redeem(self, channel: TaskChannel) -> str:
        """Called by the transport immediately before contacting. Re-checks
        window and limits on the gate's clock, records the attempt, and
        returns the number. Raises ContactRefused otherwise."""
        return self._gate._redeem(self, channel)

    def __repr__(self) -> str:  # never print the number
        return f"ContactAuthorization(channel={self._channel.value}, redeemed={self._redeemed})"


@dataclass(frozen=True)
class GateDecision:
    authorization: ContactAuthorization | None
    reason: str | None

    @property
    def allowed(self) -> bool:
        return self.authorization is not None


class OutboundContactGate:
    def __init__(
        self,
        *,
        window: ContactWindow,
        limits: AttemptLimits,
        clock: Callable[[], datetime],
        country_zones: dict[str, tuple[str, ...]] | None = None,
    ):
        self._window = window
        self._limits = limits
        self._clock = clock
        self._country_zones = dict(country_zones or {})
        self._lock = threading.Lock()
        self._attempts: dict[tuple[str, str], deque[datetime]] = {}
        self._ops = 0

    @property
    def window(self) -> ContactWindow:
        return self._window

    @property
    def limits(self) -> AttemptLimits:
        return self._limits

    # -- public ------------------------------------------------------------

    def authorize(
        self, *, channel: TaskChannel, phone: str, claimed_tz: str | None, customer_id: str | None
    ) -> GateDecision:
        if channel not in AUTOMATED_CHANNELS:
            return GateDecision(None, f"channel {channel.value!r} is not an automated outbound channel")
        verdict = zones_to_check(phone, claimed_tz, self._country_zones)
        if verdict.zones is None:
            return GateDecision(None, verdict.reason)
        with self._lock:
            reason = self._check(self._now(), verdict.zones, phone, customer_id)
        if reason is not None:
            return GateDecision(None, reason)
        return GateDecision(ContactAuthorization(_MINT, self, channel, phone, customer_id, verdict.zones), None)

    # -- internals ---------------------------------------------------------

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("gate clock must return a timezone-aware datetime")
        return now

    def _keys(self, phone: str, customer_id: str | None) -> list[tuple[str, str]]:
        keys = [("phone number", phone)]
        if customer_id:
            keys.append(("customer", customer_id))
        return keys

    def _check(self, now: datetime, zones: tuple[str, ...], phone: str, customer_id: str | None) -> str | None:
        for z in zones:
            if not self._window.allows(now, z):
                return (
                    f"outside permitted contact window ({self._window.describe()}) in {z}, one of the "
                    f"{len(zones)} time zone(s) checked for this number (every zone it could be in, plus the claimed zone) — not contacted"
                )
        for kind, key in self._keys(phone, customer_id):
            recent = [t for t in self._attempts.get((kind, key), ()) if t > now - _ROLLING]
            if len(recent) >= self._limits.max_per_24h:
                return (
                    f"attempt limit reached for this {kind}: {len(recent)} automated contact(s) in the "
                    f"last 24h (max {self._limits.max_per_24h}) — not contacted"
                )
            if recent:
                # an attempt recorded "after" now (clock moved backwards) gives a
                # negative interval, which is below any spacing: refused.
                since = now - max(recent)
                shown = timedelta(seconds=int(since.total_seconds()))
                if since < self._limits.min_spacing:
                    return (
                        f"minimum spacing not met for this {kind}: last automated contact {shown} ago "
                        f"(minimum {self._limits.min_spacing}) — not contacted"
                    )
        return None

    def _redeem(self, auth: ContactAuthorization, channel: TaskChannel) -> str:
        with self._lock:
            if auth._gate is not self:
                raise ContactRefused("authorization was issued by a different gate")
            if auth._redeemed:
                raise ContactRefused("authorization already used — one authorization, one contact")
            if channel != auth._channel:
                raise ContactRefused(
                    f"authorization is for {auth._channel.value!r}, not {channel.value!r}"
                )
            now = self._now()
            reason = self._check(now, auth._zones, auth._phone, auth._customer_id)
            if reason is not None:
                auth._redeemed = True  # a refused authorization is spent, never retried later
                raise ContactRefused(reason)
            for key in self._keys(auth._phone, auth._customer_id):
                ts = self._attempts.setdefault(key, deque())
                while ts and ts[0] <= now - _ROLLING:
                    ts.popleft()
                ts.append(now)
            auth._redeemed = True
            self._prune(now)
            return auth._phone

    def _prune(self, now: datetime) -> None:
        self._ops += 1
        if self._ops % 256:
            return
        cutoff = now - _ROLLING
        for key in [k for k, ts in self._attempts.items() if ts and max(ts) <= cutoff]:
            del self._attempts[key]
