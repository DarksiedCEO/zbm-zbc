"""
Unit tests for the fix-wave-1 F3 gate: recipient_zones.py (which zones a
number can be in) and outbound_gate.py (the single place automated
outbound contact is authorized). The HTTP-level AEGIS reproductions are in
test_fix_wave_1_f3_api.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contact_window import ContactWindow
from fulfillment_schema import TaskChannel
from integrations.message_sender import NotWiredMessageSender
from integrations.sip_dialer import InMemorySipDialer, NotWiredSipDialer
from outbound_gate import (
    AttemptLimits,
    ContactAuthorization,
    ContactRefused,
    OutboundContactGate,
    parse_attempt_limits,
)
from recipient_zones import (
    CONTINENTAL_US_CA_ZONES,
    NANP_CLAIMABLE_ZONES,
    _NPA_OUTSIDE_CONTINENT,
    parse_country_zones,
    zones_to_check,
)

SRC = Path(__file__).resolve().parents[1] / "src"
LA, NY, HNL = "America/Los_Angeles", "America/New_York", "Pacific/Honolulu"
LA_PHONE, NY_PHONE, HI_PHONE, AK_PHONE = "+12135550101", "+12125550101", "+18085550101", "+19075550101"


def Z(y, mo, d, h, mi=0, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def gate_at(t: datetime, **kw) -> tuple[OutboundContactGate, Clock]:
    clock = Clock(t)
    kw.setdefault("limits", AttemptLimits())
    return OutboundContactGate(window=ContactWindow.default(), clock=clock, **kw), clock


def allowed(t: datetime, phone: str, tz: str, channel: TaskChannel = TaskChannel.CALL, **kw) -> bool:
    g, _ = gate_at(t, **kw)
    return g.authorize(channel=channel, phone=phone, claimed_tz=tz, customer_id=None).allowed


def contact(g: OutboundContactGate, phone: str, tz: str, channel=TaskChannel.CALL, customer_id=None) -> str | None:
    """authorize + redeem, like a transport would. Returns the refusal reason or None."""
    d = g.authorize(channel=channel, phone=phone, claimed_tz=tz, customer_id=customer_id)
    if not d.allowed:
        return d.reason
    try:
        d.authorization.redeem(channel)
    except ContactRefused as exc:
        return str(exc)
    return None


# --- window edges in the strictest zones -----------------------------------------

@pytest.mark.parametrize("t,expected", [
    # Daylight time (Sep): latest start = 08:00 PDT = 15:00Z; earliest end = 21:00 NDT (St. John's, UTC-2:30) = 23:30Z
    (Z(2026, 9, 22, 14, 59, 59), False),
    (Z(2026, 9, 22, 15, 0, 0), True),
    (Z(2026, 9, 22, 23, 29, 59), True),
    (Z(2026, 9, 22, 23, 30, 0), False),
    # Standard time (Dec): 08:00 PST = 16:00Z; 21:00 NST (UTC-3:30) = 00:30Z next day
    (Z(2026, 12, 22, 15, 59, 59), False),
    (Z(2026, 12, 22, 16, 0, 0), True),
    (Z(2026, 12, 23, 0, 29, 59), True),
    (Z(2026, 12, 23, 0, 30, 0), False),
])
def test_continental_plus1_window_edges_are_set_by_the_strictest_zones(t, expected):
    for phone, tz in [(LA_PHONE, LA), (NY_PHONE, NY), (NY_PHONE, "America/St_Johns")]:
        assert allowed(t, phone, tz) is expected, (t, phone, tz)


@pytest.mark.parametrize("t,expected", [
    (Z(2026, 9, 22, 17, 59, 59), False),   # 07:59:59 HST
    (Z(2026, 9, 22, 18, 0, 0), True),      # 08:00 HST
    (Z(2026, 9, 23, 6, 59, 59), True),     # 20:59:59 HST
    (Z(2026, 9, 23, 7, 0, 0), False),      # 21:00 HST
])
def test_hawaii_number_window_edges(t, expected):
    assert allowed(t, HI_PHONE, HNL) is expected


@pytest.mark.parametrize("t,expected", [
    (Z(2026, 9, 22, 16, 59, 59), False),   # 07:59:59 HDT in Adak (UTC-9), the westernmost Alaska zone
    (Z(2026, 9, 22, 17, 0, 0), True),
    (Z(2026, 9, 23, 4, 59, 59), True),     # 20:59:59 AKDT in Anchorage (UTC-8)
    (Z(2026, 9, 23, 5, 0, 0), False),
])
def test_alaska_number_window_spans_every_alaska_zone(t, expected):
    assert allowed(t, AK_PHONE, "America/Anchorage") is expected


def test_claimed_zone_narrows_but_never_widens():
    # 17:00Z = 10:00 PDT (continental OK) but 07:00 HST: a 212 number claimed to be in Honolulu is refused.
    assert allowed(Z(2026, 9, 22, 17, 0), NY_PHONE, HNL) is False
    assert allowed(Z(2026, 9, 22, 18, 0), NY_PHONE, HNL) is True
    # 06:00Z: 20:00 in Honolulu, 02:00 in New York — the AEGIS lie.
    assert allowed(Z(2026, 9, 22, 6, 0), NY_PHONE, HNL) is False
    # 16:00Z: 12:00 in New York, 06:00 in Honolulu — the lie the other way round.
    assert allowed(Z(2026, 9, 22, 16, 0), HI_PHONE, NY) is False


# --- zone consistency with the number ----------------------------------------------

@pytest.mark.parametrize("claimed", [
    "UTC", "Etc/UTC", "GMT", "Asia/Tokyo", "Europe/London", "Etc/GMT+5", "US/Eastern", "EST5EDT",
    "America/Mexico_City", "", None, "Not/AZone", "../../etc/passwd",
])
def test_plus1_number_refuses_any_zone_not_valid_for_nanp(claimed):
    g, _ = gate_at(Z(2026, 9, 22, 18, 0))
    d = g.authorize(channel=TaskChannel.CALL, phone=LA_PHONE, claimed_tz=claimed, customer_id=None)
    assert not d.allowed
    assert "time zone" in d.reason


@pytest.mark.parametrize("phone", [
    "+15550101", "+1213555010", "+121355501011", "+11235550101", "+10135550101",
    "+12131550101",  # exchange code cannot start with 1
    "+18005550101", "+18885550101", "+18445550101", "+19005550101", "+15005550101",
    "+15335550101", "+12115550101", "+19115550101", "+12905550101", "+13705550101", "+19605550101",
    "+14565550101", "+17005550101", "+16225550101",
])
def test_invalid_or_non_geographic_plus1_numbers_fail_closed(phone):
    v = zones_to_check(phone, NY, {})
    assert v.zones is None
    assert phone not in v.reason


def test_outside_continent_npa_table_is_pinned():
    """Removing a row here would let a caller claim a continental zone for
    a Hawaii/Alaska/Pacific-territory number and get it called at night."""
    assert _NPA_OUTSIDE_CONTINENT["808"] == (HNL,)
    assert set(_NPA_OUTSIDE_CONTINENT["907"]) >= {"America/Anchorage", "America/Adak", "America/Nome"}
    assert _NPA_OUTSIDE_CONTINENT["671"] == ("Pacific/Guam",)
    assert _NPA_OUTSIDE_CONTINENT["670"] == ("Pacific/Saipan",)
    assert _NPA_OUTSIDE_CONTINENT["684"] == ("Pacific/Pago_Pago",)
    assert _NPA_OUTSIDE_CONTINENT["787"] == _NPA_OUTSIDE_CONTINENT["939"] == ("America/Puerto_Rico",)
    assert _NPA_OUTSIDE_CONTINENT["340"] == ("America/St_Thomas",)


def test_every_listed_zone_resolves_and_continental_span_is_st_johns_to_pacific():
    from zoneinfo import ZoneInfo

    for z in NANP_CLAIMABLE_ZONES:
        ZoneInfo(z)
    offsets = {ZoneInfo(z).utcoffset(datetime(2026, 1, 15)) for z in CONTINENTAL_US_CA_ZONES}
    assert min(offsets) == timedelta(hours=-8) and max(offsets) == timedelta(hours=-3, minutes=-30)
    assert {"America/St_Johns", LA, "America/Vancouver"} <= set(CONTINENTAL_US_CA_ZONES)


def test_non_plus1_numbers_fail_closed_unless_a_country_rule_is_configured():
    t = Z(2026, 9, 22, 12, 0)  # 13:00 in London
    assert allowed(t, "+442071234567", "Europe/London") is False
    uk = {"country_zones": parse_country_zones("44=Europe/London")}
    assert allowed(t, "+442071234567", "Europe/London", **uk) is True
    assert allowed(t, "+442071234567", "Asia/Tokyo", **uk) is False          # claim must match the rule
    assert allowed(Z(2026, 9, 22, 6, 0), "+442071234567", "Europe/London", **uk) is False  # 07:00 BST
    assert allowed(t, "+33142685300", "Europe/Paris", **uk) is False         # +33 still unconfigured


def test_configured_country_with_several_zones_needs_all_of_them():
    au = {"country_zones": parse_country_zones("61=Australia/Perth,Australia/Sydney")}
    # 23:00Z = 07:00 Perth (AWST), 09:00 Sydney (AEST): Perth is still night.
    assert allowed(Z(2026, 9, 21, 23, 0), "+61212345678", "Australia/Sydney", **au) is False
    # 02:00Z = 10:00 Perth, 12:00 Sydney.
    assert allowed(Z(2026, 9, 22, 2, 0), "+61212345678", "Australia/Sydney", **au) is True


@pytest.mark.parametrize("bad", [
    "1=America/New_York", "01=Europe/London", "44", "44=", "44=Not/AZone", "44=Europe/London;44=Europe/London",
    "4=Europe/London;44=Europe/London", "4444=Europe/London", "x=Europe/London",
])
def test_country_zone_config_rejects_anything_unverifiable(bad):
    with pytest.raises(ValueError):
        parse_country_zones(bad)


# --- attempt limits -----------------------------------------------------------------

def test_attempt_limits_are_narrow_only():
    assert parse_attempt_limits(None, None) == AttemptLimits(3, timedelta(hours=2))
    assert parse_attempt_limits("1", "240") == AttemptLimits(1, timedelta(hours=4))
    for mx, sp in [("4", None), ("0", None), (None, "119"), (None, "1441"), ("x", None), (None, "2h")]:
        with pytest.raises(ValueError):
            parse_attempt_limits(mx, sp)


def test_sms_and_email_share_the_number_limit_with_calls():
    g, clock = gate_at(Z(2026, 9, 22, 15, 0))
    assert contact(g, LA_PHONE, LA, TaskChannel.CALL) is None
    clock.t += timedelta(minutes=10)  # the escalation sequence's SMS step
    assert "spacing" in contact(g, LA_PHONE, LA, TaskChannel.SMS)
    clock.t += timedelta(hours=2)
    assert contact(g, LA_PHONE, LA, TaskChannel.SMS) is None
    clock.t += timedelta(hours=2)
    assert contact(g, LA_PHONE, LA, TaskChannel.EMAIL) is None
    clock.t += timedelta(hours=2)
    assert "attempt limit" in contact(g, LA_PHONE, LA, TaskChannel.CALL)


@pytest.mark.parametrize("channel", [TaskChannel.SMS, TaskChannel.EMAIL])
def test_sms_and_email_are_held_to_the_same_window(channel):
    assert allowed(Z(2026, 9, 22, 9, 0), LA_PHONE, LA, channel) is False   # 02:00 PDT
    assert allowed(Z(2026, 9, 22, 9, 0), LA_PHONE, "UTC", channel) is False
    assert allowed(Z(2026, 9, 22, 18, 0), LA_PHONE, LA, channel) is True


def test_human_handoff_is_not_an_automated_channel():
    g, _ = gate_at(Z(2026, 9, 22, 18, 0))
    d = g.authorize(channel=TaskChannel.HUMAN_HANDOFF, phone=LA_PHONE, claimed_tz=LA, customer_id=None)
    assert not d.allowed


def test_customer_limit_applies_across_numbers_and_number_limit_across_customers():
    g, clock = gate_at(Z(2026, 9, 22, 18, 0))
    assert contact(g, LA_PHONE, LA, customer_id="c1") is None
    assert "customer" in contact(g, "+12135550199", LA, customer_id="c1")
    assert "phone number" in contact(g, LA_PHONE, LA, customer_id="c2")  # fresh customer id doesn't help


def test_clock_moving_backwards_does_not_reopen_the_limit():
    g, clock = gate_at(Z(2026, 9, 22, 20, 0))
    assert contact(g, LA_PHONE, LA) is None
    clock.t = Z(2026, 9, 22, 16, 0)
    assert "spacing" in contact(g, LA_PHONE, LA)


def test_gate_rejects_a_naive_clock():
    g = OutboundContactGate(window=ContactWindow.default(), limits=AttemptLimits(), clock=lambda: datetime(2026, 9, 22, 18))
    with pytest.raises(ValueError):
        g.authorize(channel=TaskChannel.CALL, phone=LA_PHONE, claimed_tz=LA, customer_id=None)


def test_concurrent_contacts_to_one_number_allow_exactly_one():
    g, _ = gate_at(Z(2026, 9, 22, 18, 0))
    results: list[str | None] = []
    barrier = threading.Barrier(16)

    def worker(i: int) -> None:
        barrier.wait()
        results.append(contact(g, LA_PHONE, LA, customer_id=f"c{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results.count(None) == 1


# --- the authorization is the only way a transport gets a number --------------------

def test_authorization_cannot_be_forged():
    g, _ = gate_at(Z(2026, 9, 22, 18, 0))
    with pytest.raises(TypeError):
        ContactAuthorization(object(), g, TaskChannel.CALL, LA_PHONE, None, (LA,))


def test_authorization_is_single_use_and_channel_bound():
    g, _ = gate_at(Z(2026, 9, 22, 18, 0))
    a = g.authorize(channel=TaskChannel.CALL, phone=LA_PHONE, claimed_tz=LA, customer_id=None).authorization
    with pytest.raises(ContactRefused):
        a.redeem(TaskChannel.SMS)
    b = g.authorize(channel=TaskChannel.CALL, phone=LA_PHONE, claimed_tz=LA, customer_id=None).authorization
    assert b.redeem(TaskChannel.CALL) == LA_PHONE
    with pytest.raises(ContactRefused):
        b.redeem(TaskChannel.CALL)
    assert LA_PHONE not in repr(b)


def test_redeem_rechecks_the_window_at_contact_time():
    g, clock = gate_at(Z(2026, 9, 23, 6, 59, 59))  # 20:59:59 HST
    a = g.authorize(channel=TaskChannel.CALL, phone=HI_PHONE, claimed_tz=HNL, customer_id=None).authorization
    assert a is not None
    clock.t = Z(2026, 9, 23, 7, 0)  # 21:00 HST by the time the dialer asks for the number
    with pytest.raises(ContactRefused, match="contact window"):
        a.redeem(TaskChannel.CALL)


def test_two_authorizations_minted_before_either_is_used_yield_one_contact():
    g, _ = gate_at(Z(2026, 9, 22, 18, 0))
    a = g.authorize(channel=TaskChannel.CALL, phone=LA_PHONE, claimed_tz=LA, customer_id=None).authorization
    b = g.authorize(channel=TaskChannel.SMS, phone=LA_PHONE, claimed_tz=LA, customer_id=None).authorization
    a.redeem(TaskChannel.CALL)
    with pytest.raises(ContactRefused, match="spacing"):
        b.redeem(TaskChannel.SMS)


def test_not_wired_transports_make_no_contact_and_consume_no_attempt():
    g, _ = gate_at(Z(2026, 9, 22, 18, 0))
    for _ in range(5):
        a = g.authorize(channel=TaskChannel.CALL, phone=LA_PHONE, claimed_tz=LA, customer_id=None).authorization
        with pytest.raises(NotImplementedError):
            NotWiredSipDialer().place_call(a, "line")
        s = g.authorize(channel=TaskChannel.SMS, phone=LA_PHONE, claimed_tz=LA, customer_id=None).authorization
        with pytest.raises(NotImplementedError):
            NotWiredMessageSender().send(s, "hi")
    dialer = InMemorySipDialer()
    dialer.place_call(g.authorize(channel=TaskChannel.CALL, phone=LA_PHONE, claimed_tz=LA, customer_id=None).authorization, "l")
    assert dialer.numbers_dialed == [LA_PHONE]


# --- startup refuses unverifiable configuration --------------------------------------

@pytest.mark.parametrize("var,value", [
    ("FULFILLMENT_CONTACT_MAX_ATTEMPTS_PER_24H", "10"),
    ("FULFILLMENT_CONTACT_MIN_SPACING_MINUTES", "10"),
    ("FULFILLMENT_COUNTRY_ZONES", "1=Asia/Tokyo"),
    ("FULFILLMENT_COUNTRY_ZONES", "44=Not/AZone"),
])
def test_bad_gate_configuration_refuses_to_start(var, value):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(SRC),
           "FULFILLMENT_SERVICE_TOKEN": "t", var: value}
    r = subprocess.run([sys.executable, "-c", "import api"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert var in r.stderr
