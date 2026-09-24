"""
Fix wave 5, AEGIS NEW-5 (LOW, design trade-off): the outbound gate's
new-key budget (4,166 new numbers/customers per rolling hour) could be spent
in one burst — 4,166 fresh numbers in 0.34 s — after which every NEW
legitimate number was refused for 60 minutes.

The service has one shared bearer token and no caller identity, so the
budget cannot be scoped per caller. Instead admission of new keys is also a
token bucket: burst NEW_KEY_BURST (100), refilled at max_new_keys_per_hour
per hour, i.e. 1/60 of the hourly budget per minute. The rolling-hour budget
still applies on top (fail closed is unchanged: nothing here admits a key the
old rule refused, except by time passing).

Plus a TestClient check of NEW-3's 431 head re-check in the middleware.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import api
import outbound_gate
from contact_window import ContactWindow
from fulfillment_schema import TaskChannel
from outbound_gate import AttemptLimits, ContactRefused, OutboundContactGate

from conftest import TEST_SERVICE_TOKEN

T0 = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)
NY = "America/New_York"
HOURLY = outbound_gate.DEFAULT_MAX_NEW_KEYS_PER_HOUR


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _gate(clock, **kw):
    return OutboundContactGate(window=ContactWindow.default(), limits=AttemptLimits(), clock=clock, **kw)


def _contact(g, phone, customer_id=None):
    d = g.authorize(channel=TaskChannel.CALL, phone=phone, claimed_tz=NY, customer_id=customer_id)
    if not d.allowed:
        return d.reason
    try:
        d.authorization.redeem(TaskChannel.CALL)
    except ContactRefused as exc:
        return str(exc)
    return None


def _num(i: int) -> str:
    return f"+1415{2_000_000 + i:07d}"


def test_defaults():
    assert outbound_gate.DEFAULT_NEW_KEY_BURST == 100
    assert HOURLY == 4166
    g = _gate(Clock(T0))
    s = g.status()
    assert s["new_key_burst"] == 100
    assert s["new_key_refill_per_minute"] == pytest.approx(HOURLY / 60, abs=0.01)


def test_a_burst_cannot_exhaust_the_hour_and_a_legit_number_gets_through_seconds_later():
    """The AEGIS reproduction: 4,166 fresh numbers offered within ~0.34 s."""
    clock = Clock(T0)
    g = _gate(clock)
    admitted = 0
    for i in range(HOURLY):
        clock.t = T0 + timedelta(seconds=0.34 * i / HOURLY)
        if _contact(g, _num(i)) is None:
            admitted += 1
    assert admitted <= outbound_gate.DEFAULT_NEW_KEY_BURST + 1, admitted
    # right after the burst, new numbers are refused (fail closed), with a reason
    reason = _contact(g, "+13125550100")
    assert reason is not None and "new numbers/customers" in reason
    # but not for the rest of the hour: the bucket refills 1/60 of the hour per minute
    clock.t = T0 + timedelta(seconds=2)
    assert _contact(g, "+13125550100") is None
    assert not g.status()["new_key_budget_exhausted"]


def test_sustained_attacker_gets_at_most_burst_plus_one_sixtieth_per_minute_and_the_hourly_cap_holds():
    clock = Clock(T0)
    g = _gate(clock, max_tracked_keys=1_000_000)
    per_minute: list[int] = []
    n = 0
    for minute in range(180):
        got = 0
        for sec in range(0, 60, 5):  # 12 bursts of 50 fresh numbers per minute
            clock.t = T0 + timedelta(minutes=minute, seconds=sec)
            for _ in range(50):
                n += 1
                if _contact(g, _num(n)) is None:
                    got += 1
        per_minute.append(got)
    # any minute: at most the burst plus one minute of refill (tokens that
    # accrue while the rolling-hour budget is the one refusing are the burst)
    assert max(per_minute) <= outbound_gate.DEFAULT_NEW_KEY_BURST + HOURLY / 60 + 1, max(per_minute)
    # in steady state, the refill rate: 1/60 of the hourly budget per minute
    assert max(per_minute[1:60]) <= HOURLY / 60 + 1, max(per_minute[1:60])
    for start in range(0, 180 - 60 + 1):
        assert sum(per_minute[start:start + 60]) <= HOURLY  # rolling-hour budget still holds
    assert sum(per_minute[60:120]) >= HOURLY * 0.95  # and is usable (not vacuous)


def test_small_configured_budget_keeps_hourly_semantics():
    # burst defaults to min(100, hourly): a 4/hour gate still admits 4 at once
    clock = Clock(T0)
    g = _gate(clock, max_tracked_keys=1000, max_new_keys_per_hour=4)
    assert g.status()["new_key_burst"] == 4
    for i in range(4):
        assert _contact(g, _num(i)) is None
    assert _contact(g, _num(99)) is not None
    clock.t = T0 + timedelta(hours=1)
    assert _contact(g, _num(99)) is None


def test_bucket_is_rechecked_at_redeem():
    g = _gate(Clock(T0), new_key_burst=1)
    a = g.authorize(channel=TaskChannel.CALL, phone=_num(1), claimed_tz=NY, customer_id=None)
    b = g.authorize(channel=TaskChannel.CALL, phone=_num(2), claimed_tz=NY, customer_id=None)
    assert a.allowed and b.allowed
    a.authorization.redeem(TaskChannel.CALL)
    with pytest.raises(ContactRefused, match="new numbers/customers"):
        b.authorization.redeem(TaskChannel.CALL)


def test_clock_moving_backwards_never_refills():
    clock = Clock(T0)
    g = _gate(clock, new_key_burst=2)
    assert _contact(g, _num(1)) is None and _contact(g, _num(2)) is None
    clock.t = T0 - timedelta(hours=5)
    assert _contact(g, _num(3)) is not None
    clock.t = T0  # back to where it was: still nothing refilled
    assert _contact(g, _num(3)) is not None


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, 4167])
def test_burst_must_be_sane(bad):
    with pytest.raises(ValueError):
        _gate(Clock(T0), new_key_burst=bad)


def test_status_route_reports_burst_state():
    c = TestClient(api.app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})
    body = c.get("/gate/status").json()
    for k in ("new_key_burst", "new_key_tokens_available", "new_key_refill_per_minute", "new_key_burst_exhausted"):
        assert k in body


def test_oversized_head_gets_431_under_any_launcher():
    """NEW-3: the middleware re-checks the head cap (python3 -m api refuses it
    in the parser first; TestClient has no parser limit at all)."""
    c = TestClient(api.app)
    r = c.get("/health", headers={"X-Pad": "a" * (api._MAX_HEADER_BYTES + 1)})
    assert r.status_code == 431
    assert c.get("/health", headers={"X-Pad": "a" * 1000}).status_code == 200
