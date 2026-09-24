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

Bounded (fix wave 1, audit: unbounded growth). Before, the attempt history
kept a key per number and per customer ever contacted and only dropped
expired keys on every 256th contact. Now: (a) attempts older than the 24h
window are evicted on the gate's clock — a full sweep whenever that clock
has moved a minute since the last one, or whenever the history is at its
cap; (b) at most `max_tracked_keys` keys (numbers + customers) are held.
At the cap with nothing expired, a contact that would add a key is REFUSED
(fail closed, at authorize and again at redeem): dropping live history
would silently re-open the per-number limit. Per key, at most
`max_per_24h` attempts are ever inside the window, so the cap bounds the
whole structure.

Capacity exhaustion (fix wave 4, AEGIS round 3, PLAUSIBLE/low): a holder of
the service token could fill the tracked-key cap with contacts to fresh
numbers/customers, after which every NEW number is refused for up to 24h
(fail closed — correct, but a denial of service). Mitigation, never a relaxation:
  (c) admission of NEW keys is rate limited: at most `max_new_keys_per_hour`
      in any rolling hour (checked at authorize and again at redeem). The
      default is floor(cap / 24), so 24 hours of admissions cannot reach the
      cap: filling it takes more than a day of sustained real contacts that
      are also kept alive by re-contacting, not a burst. Already-tracked
      numbers are unaffected (their own limits still decide).
  (d) capacity is visible: status() (GET /gate/status and every orchestrate
      response) reports utilization, near_capacity (>= 80%) and whether this
      hour's new-key budget is exhausted, and the API logs a warning.
Every refusal here is fail closed: there is no path that contacts a number
the gate could not record. ADR 0002, Decision 10.

Burst shaping (fix wave 5, AEGIS NEW-5, LOW): (c) alone let one burst spend
the whole hour — 4,166 fresh numbers in 0.34 s, then every NEW legitimate
number refused for 60 minutes. There is no caller identity to scope the
budget by (one shared service token), so admission of new keys must ALSO
pass a token bucket:
  (e) capacity `new_key_burst` (default min(100, max_new_keys_per_hour)),
      refilled continuously at max_new_keys_per_hour per hour — 1/60 of the
      hourly budget per minute (69.4/min at the default) — on the gate's
      clock; a clock that moves backwards refills nothing. Checked at
      authorize and again at redeem, spent at redeem, like (c).
Worst case now: a burst takes at most `new_key_burst` new keys at once; a
legitimate new number is refused for at most ~1 s after a burst ends. An
attacker who KEEPS offering fresh numbers still competes with legitimate new
numbers for every token (up to the full hourly budget) for as long as the
attack lasts — that is visible (status() and the capacity warning) and needs
per-caller identity to fix, which this service does not have. Nothing here
admits a key that (c) refuses; it only refuses more.
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
DEFAULT_MAX_TRACKED_KEYS = 100_000
_MAX_TRACKED_KEYS_CEILING = 1_000_000
DEFAULT_MAX_NEW_KEYS_PER_HOUR = DEFAULT_MAX_TRACKED_KEYS // 24  # 4,166: 24h of admissions < cap
DEFAULT_NEW_KEY_BURST = 100  # token bucket capacity; refill = hourly budget / 3600 per second
_NEW_KEY_WINDOW = timedelta(hours=1)
NEAR_CAPACITY_FRACTION = 0.8
_SWEEP_EVERY = timedelta(minutes=1)
_SWEEP_AT_CAP_EVERY = timedelta(seconds=1)


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
        max_tracked_keys: int = DEFAULT_MAX_TRACKED_KEYS,
        max_new_keys_per_hour: int = DEFAULT_MAX_NEW_KEYS_PER_HOUR,
        new_key_burst: int | None = None,
    ):
        for name, value in (("max_tracked_keys", max_tracked_keys), ("max_new_keys_per_hour", max_new_keys_per_hour)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_TRACKED_KEYS_CEILING:
                raise ValueError(f"{name} must be an integer 1-{_MAX_TRACKED_KEYS_CEILING}")
        if new_key_burst is None:
            new_key_burst = min(DEFAULT_NEW_KEY_BURST, max_new_keys_per_hour)
        if isinstance(new_key_burst, bool) or not isinstance(new_key_burst, int) or not 1 <= new_key_burst <= max_new_keys_per_hour:
            raise ValueError(f"new_key_burst must be an integer 1-{max_new_keys_per_hour} (max_new_keys_per_hour)")
        self._max_tracked_keys = max_tracked_keys
        self._max_new_keys_per_hour = max_new_keys_per_hour
        # (e) token bucket over new-key admissions: starts full, refills at
        # max_new_keys_per_hour per hour of gate-clock time.
        self._new_key_burst = new_key_burst
        self._new_key_refill_per_s = max_new_keys_per_hour / 3600.0
        self._new_key_tokens = float(new_key_burst)
        self._new_key_refilled_at: datetime | None = None
        # gate-clock stamps of new-key admissions in the last hour; never
        # longer than max_new_keys_per_hour (the check refuses before that).
        self._new_key_stamps: deque[datetime] = deque()
        self._window = window
        self._limits = limits
        self._clock = clock
        self._country_zones = dict(country_zones or {})
        self._lock = threading.Lock()
        self._attempts: dict[tuple[str, str], deque[datetime]] = {}
        self._last_sweep: datetime | None = None

    @property
    def window(self) -> ContactWindow:
        return self._window

    @property
    def limits(self) -> AttemptLimits:
        return self._limits

    @property
    def max_tracked_keys(self) -> int:
        return self._max_tracked_keys

    @property
    def tracked_keys(self) -> int:
        """Numbers + customers currently held in the attempt history."""
        with self._lock:
            return len(self._attempts)

    def status(self) -> dict:
        """Capacity metrics for monitoring/alerting (fix wave 4)."""
        with self._lock:
            now = self._now()
            self._maybe_sweep(now)
            self._prune_new_key_stamps(now)
            self._refill_new_key_tokens(now)
            tracked = len(self._attempts)
            new_last_hour = len(self._new_key_stamps)
            tokens = self._new_key_tokens
        utilization = round(tracked / self._max_tracked_keys, 4)
        return {
            "tracked_keys": tracked,
            "max_tracked_keys": self._max_tracked_keys,
            "utilization": utilization,
            "near_capacity": utilization >= NEAR_CAPACITY_FRACTION,
            "at_capacity": tracked >= self._max_tracked_keys,
            "new_keys_last_hour": new_last_hour,
            "max_new_keys_per_hour": self._max_new_keys_per_hour,
            "new_key_budget_exhausted": new_last_hour >= self._max_new_keys_per_hour,
            "new_key_burst": self._new_key_burst,
            "new_key_tokens_available": int(tokens),
            "new_key_refill_per_minute": round(self._new_key_refill_per_s * 60, 2),
            "new_key_burst_exhausted": tokens < 1,
        }

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
        self._maybe_sweep(now)
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
        new_keys = sum(1 for k in self._keys(phone, customer_id) if k not in self._attempts)
        if new_keys and len(self._attempts) + new_keys > self._max_tracked_keys:
            if self._last_sweep is None or abs(now - self._last_sweep) >= _SWEEP_AT_CAP_EVERY:
                self._sweep(now)
            if len(self._attempts) + new_keys > self._max_tracked_keys:
                return (
                    f"attempt history is full ({len(self._attempts)} numbers/customers contacted in the "
                    f"last 24h, max {self._max_tracked_keys}) — not contacted (fail closed: the attempt "
                    "limit could not be enforced for a number it cannot remember)"
                )
        if new_keys:
            self._prune_new_key_stamps(now)
            if len(self._new_key_stamps) + new_keys > self._max_new_keys_per_hour:
                return (
                    f"new numbers/customers admitted in the last hour: {len(self._new_key_stamps)} "
                    f"(max {self._max_new_keys_per_hour}) — not contacted (fail closed; numbers already "
                    "being contacted are unaffected); retry later"
                )
            self._refill_new_key_tokens(now)
            if self._new_key_tokens < new_keys:
                return (
                    f"new numbers/customers are admitted at most {self._new_key_burst} at once, then "
                    f"{self._new_key_refill_per_s * 60:.1f} per minute; none available right now — not "
                    "contacted (fail closed; numbers already being contacted are unaffected); retry in a few seconds"
                )
        return None

    def _refill_new_key_tokens(self, now: datetime) -> None:
        # Only forward clock movement refills; a clock that moved backwards
        # refills nothing until it passes the last refill point again.
        if self._new_key_refilled_at is None:
            self._new_key_refilled_at = now
            return
        if now <= self._new_key_refilled_at:
            return
        elapsed = (now - self._new_key_refilled_at).total_seconds()
        self._new_key_tokens = min(float(self._new_key_burst),
                                   self._new_key_tokens + elapsed * self._new_key_refill_per_s)
        self._new_key_refilled_at = now

    def _prune_new_key_stamps(self, now: datetime) -> None:
        # Stamps from a clock that later moved backwards stay until they are
        # an hour old: counted longer, never shorter (conservative).
        cutoff = now - _NEW_KEY_WINDOW
        while self._new_key_stamps and self._new_key_stamps[0] <= cutoff:
            self._new_key_stamps.popleft()

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
                if key not in self._attempts:
                    self._new_key_stamps.append(now)  # budget checked by _check above
                    self._new_key_tokens -= 1  # bucket checked (and refilled) by _check above
                ts = self._attempts.setdefault(key, deque())
                while ts and ts[0] <= now - _ROLLING:
                    ts.popleft()
                ts.append(now)
            auth._redeemed = True
            return auth._phone

    def _maybe_sweep(self, now: datetime) -> None:
        # Time-based, not op-count-based: expired history goes within a
        # minute of gate-clock time regardless of traffic. A clock that moved
        # backwards also triggers a sweep (which then evicts nothing early).
        if self._last_sweep is None or not (self._last_sweep <= now < self._last_sweep + _SWEEP_EVERY):
            self._sweep(now)

    def _sweep(self, now: datetime) -> None:
        """Drop every attempt at or before now - 24h, and every key left empty.
        Attempts stamped after `now` (clock moved backwards) are kept."""
        self._last_sweep = now
        cutoff = now - _ROLLING
        for key in list(self._attempts):
            ts = self._attempts[key]
            if all(t > cutoff for t in ts):  # at most max_per_24h entries: cheap
                continue
            kept = deque(t for t in ts if t > cutoff)
            if kept:
                self._attempts[key] = kept
            else:
                del self._attempts[key]
