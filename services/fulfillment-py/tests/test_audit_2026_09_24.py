"""
Regression tests for the Sep 24 2026 hostile audit (see README "Audit,
Sep 24 2026" and ADR 0002). Each test was written and run FAILING against
the pre-audit code before the fix it guards was made; tests that pin
already-correct behavior say so in their docstring.

In-process layer only (TestClient + agent functions). The real-socket
layer is in test_live_server.py.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api
from agents import callback_orchestration, customer_dossier, missed_call_detection
from conftest import TEST_SERVICE_TOKEN
from contact_window import ContactWindow, parse_contact_window
from fulfillment_schema import (
    CallDirection,
    CallEvent,
    CallStatus,
    FollowUpTask,
    LabeledValue,
    TaskChannel,
    TaskPurpose,
    TaskStatus,
)
from integrations.sip_dialer import DialAttemptResult, InMemorySipDialer
from outbound_gate import AttemptLimits, OutboundContactGate

SRC = Path(__file__).resolve().parents[1] / "src"
client = TestClient(api.app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})

# 2026-09-22 is during US daylight time: America/Los_Angeles = UTC-7.
NOON_UTC = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
UTC_0900 = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)   # 02:00 in Los Angeles
UTC_2000 = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)  # 13:00 in Los Angeles

LA = "America/Los_Angeles"
DEFAULT_WINDOW = ContactWindow.default()
# Fix wave 1, F3: the dialing tests below used "+15550101" (7 digits after
# +1, not a real NANP number) with a recipient "in UTC" — the unchecked
# number/zone pairing AEGIS exploited. They now use a real Los Angeles
# number in its real zone, at a time inside the strict +1 window.
LA_PHONE = "+12135550101"
LA_PHONE_2 = "+12135550202"
UTC_1800 = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)  # 11:00 in Los Angeles


def _gate(now: datetime) -> OutboundContactGate:
    return OutboundContactGate(window=DEFAULT_WINDOW, limits=AttemptLimits(), clock=lambda: now)


def _task(**overrides) -> FollowUpTask:
    defaults = dict(
        task_id="fu-a1",
        purpose=TaskPurpose.MISSED_CALL_CALLBACK,
        channel=TaskChannel.CALL,
        customer_id="cust_1",
        source_call_id="call_1",
        created_at=UTC_0900 - timedelta(minutes=1),
        due_at=UTC_0900 + timedelta(minutes=5),
        reason="test task",
    )
    defaults.update(overrides)
    return FollowUpTask(**defaults)


def _task_json(**overrides) -> dict:
    return _task(**overrides).model_dump(mode="json")


# --- A1: startup fails closed (already correct; pinned by a real process) ----

def test_startup_fails_closed_without_token_in_a_real_process():
    """Already-correct behavior, previously only asserted in README prose:
    importing api with FULFILLMENT_SERVICE_TOKEN unset must raise, in a
    fresh interpreter (the in-suite import can't test this — conftest has
    already set the token)."""
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(SRC)}
    r = subprocess.run(
        [sys.executable, "-c", "import api"], env=env, capture_output=True, text=True, timeout=30
    )
    assert r.returncode != 0
    assert "FULFILLMENT_SERVICE_TOKEN is not set" in r.stderr


# --- F1: quiet hours in the RECIPIENT's local time, fail closed -------------

def test_callback_at_2am_recipient_local_is_blocked_even_though_utc_is_daytime():
    """HIGH: the old check was `8 <= now.hour < 20` on a UTC clock. 09:00
    UTC is 02:00 in Los Angeles — the old code dialed it."""
    dialer = InMemorySipDialer()
    outcomes = callback_orchestration.orchestrate(
        [_task()], dialer,
        phone_by_call_id={"call_1": LA_PHONE}, line_by_call_id={},
        timezone_by_call_id={"call_1": LA}, gate=_gate(UTC_0900), now=UTC_0900,
    )
    assert outcomes[0].attempted is False
    assert "contact window" in outcomes[0].skip_reason
    assert dialer.calls_placed == []


def test_callback_at_1pm_recipient_local_is_allowed_even_though_utc_is_evening():
    dialer = InMemorySipDialer()
    task = _task(created_at=UTC_2000 - timedelta(minutes=1), due_at=UTC_2000 + timedelta(minutes=5))
    outcomes = callback_orchestration.orchestrate(
        [task], dialer,
        phone_by_call_id={"call_1": LA_PHONE}, line_by_call_id={},
        timezone_by_call_id={"call_1": LA}, gate=_gate(UTC_2000), now=UTC_2000,
    )
    assert outcomes[0].attempted is True
    assert len(dialer.calls_placed) == 1


@pytest.mark.parametrize("tz_map", [{}, {"call_1": ""}, {"call_1": "Not/AZone"}, {"call_1": "../../etc/passwd"}])
def test_unknown_or_invalid_recipient_timezone_fails_closed(tz_map):
    dialer = InMemorySipDialer()
    outcomes = callback_orchestration.orchestrate(
        [_task(created_at=NOON_UTC - timedelta(minutes=1))], dialer,
        phone_by_call_id={"call_1": LA_PHONE}, line_by_call_id={},
        timezone_by_call_id=tz_map, gate=_gate(NOON_UTC), now=NOON_UTC,
    )
    assert outcomes[0].attempted is False
    assert "time zone" in outcomes[0].skip_reason
    assert dialer.calls_placed == []


def test_contact_window_edges_are_start_inclusive_end_exclusive_in_local_time():
    w = DEFAULT_WINDOW
    la = LA
    # 08:00 PDT == 15:00 UTC; 21:00 PDT == 04:00 UTC next day.
    assert w.allows(datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc), la) is True
    assert w.allows(datetime(2026, 9, 22, 14, 59, 59, tzinfo=timezone.utc), la) is False
    assert w.allows(datetime(2026, 9, 23, 3, 59, 59, tzinfo=timezone.utc), la) is True
    assert w.allows(datetime(2026, 9, 23, 4, 0, tzinfo=timezone.utc), la) is False


def test_contact_window_follows_dst():
    """Same UTC instant-of-day, different answer across the DST change:
    15:30 UTC is 08:30 PDT in September but 07:30 PST in December."""
    assert DEFAULT_WINDOW.allows(datetime(2026, 9, 22, 15, 30, tzinfo=timezone.utc), LA) is True
    assert DEFAULT_WINDOW.allows(datetime(2026, 12, 22, 15, 30, tzinfo=timezone.utc), LA) is False


def test_contact_window_config_can_narrow_but_never_widen_past_8_to_21():
    assert parse_contact_window("09:00-17:30") == ContactWindow.from_hm(9, 0, 17, 30)
    for bad in ["07:59-21:00", "08:00-21:01", "00:00-23:59", "12:00-12:00", "18:00-09:00", "garbage", "8-21"]:
        with pytest.raises(ValueError):
            parse_contact_window(bad)


def test_contact_window_rejects_naive_now():
    with pytest.raises(ValueError):
        DEFAULT_WINDOW.allows(datetime(2026, 9, 22, 12, 0), LA)


def test_api_refuses_caller_supplied_now_which_could_bypass_quiet_hours():
    """HIGH: OrchestrateRequest accepted `now` from the caller "for
    testing", so any authenticated caller could claim it was noon and have
    the quiet-hours check evaluated against a lie. Extra fields are now
    rejected outright."""
    r = client.post(
        "/agents/callback-orchestration/run",
        json={
            "tasks": [_task_json()],
            "phone_by_call_id": {"call_1": LA_PHONE},
            "timezone_by_call_id": {"call_1": LA},
            "now": "2026-09-22T19:00:00Z",
        },
    )
    assert r.status_code == 422


def test_api_quiet_hours_use_the_server_clock(monkeypatch):
    dialer = InMemorySipDialer()
    monkeypatch.setattr(api, "_dialer", dialer)
    monkeypatch.setattr(api, "_now", lambda: UTC_0900)  # 02:00 in LA
    monkeypatch.setattr(api, "_attempted_task_ids", api._new_dedupe())
    r = client.post(
        "/agents/callback-orchestration/run",
        json={"tasks": [_task_json()], "phone_by_call_id": {"call_1": LA_PHONE}, "timezone_by_call_id": {"call_1": LA}},
    )
    assert r.status_code == 200
    assert r.json()["outcomes"][0]["attempted"] is False
    assert "contact window" in r.json()["outcomes"][0]["skip_reason"]
    assert dialer.calls_placed == []


# --- F2: duplicate call events / duplicate tasks / redial across requests ----

def test_duplicate_call_event_in_one_batch_produces_one_task():
    ev = CallEvent(
        call_id="call_dup", customer_id="c", phone_number="+15550101",
        direction=CallDirection.INBOUND, status=CallStatus.MISSED,
        started_at=NOON_UTC, line_id="line_main",
    )
    tasks = missed_call_detection.detect([ev, ev], now=NOON_UTC)
    assert [t.task_id for t in tasks] == ["fu-call_dup"]


def test_duplicate_task_id_in_one_batch_is_dialed_once():
    dialer = InMemorySipDialer()
    t = _task(created_at=UTC_1800 - timedelta(minutes=1))
    outcomes = callback_orchestration.orchestrate(
        [t, t], dialer,
        phone_by_call_id={"call_1": LA_PHONE}, line_by_call_id={},
        timezone_by_call_id={"call_1": LA}, gate=_gate(UTC_1800), now=UTC_1800,
    )
    assert len(dialer.calls_placed) == 1
    assert [o.attempted for o in outcomes] == [True, False]
    assert "duplicate" in outcomes[1].skip_reason


def test_api_does_not_redial_the_same_task_across_requests(monkeypatch):
    """Closes (for the life of the process) README gap 6: a caller that
    resubmits the same still-PENDING task no longer gets a second dial."""
    dialer = InMemorySipDialer()
    monkeypatch.setattr(api, "_dialer", dialer)
    monkeypatch.setattr(api, "_now", lambda: UTC_1800)
    monkeypatch.setattr(api, "_attempted_task_ids", api._new_dedupe())
    body = {
        "tasks": [_task_json(created_at=UTC_1800 - timedelta(minutes=1))],
        "phone_by_call_id": {"call_1": LA_PHONE},
        "timezone_by_call_id": {"call_1": LA},
    }
    r1 = client.post("/agents/callback-orchestration/run", json=body)
    r2 = client.post("/agents/callback-orchestration/run", json=body)
    assert r1.json()["outcomes"][0]["attempted"] is True
    assert r2.json()["outcomes"][0]["attempted"] is False
    assert "already attempted" in r2.json()["outcomes"][0]["skip_reason"]
    assert len(dialer.calls_placed) == 1


def test_api_concurrent_requests_for_the_same_task_dial_once(monkeypatch):
    class SlowDialer(InMemorySipDialer):
        def place_call(self, authorization, line_id):
            time.sleep(0.2)
            return super().place_call(authorization, line_id)

    dialer = SlowDialer()
    monkeypatch.setattr(api, "_dialer", dialer)
    monkeypatch.setattr(api, "_now", lambda: UTC_1800)
    monkeypatch.setattr(api, "_attempted_task_ids", api._new_dedupe())
    body = {
        "tasks": [_task_json(created_at=UTC_1800 - timedelta(minutes=1))],
        "phone_by_call_id": {"call_1": LA_PHONE},
        "timezone_by_call_id": {"call_1": LA},
    }
    threads = [threading.Thread(target=client.post, args=("/agents/callback-orchestration/run",), kwargs={"json": body}) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(dialer.calls_placed) == 1


def test_dialer_exception_mid_batch_keeps_earlier_outcomes_and_marks_unknown_as_failed():
    """A future real dialer raising (network error, carrier 5xx) on task 2
    used to propagate out of orchestrate() — the API returned 500 and the
    caller never learned task 1 WAS dialed, so a retry redials it. Now the
    exception becomes an outcome; the raising task is marked FAILED
    (outcome unknown), never left PENDING for an automatic redial."""

    class FlakyDialer(InMemorySipDialer):
        def place_call(self, authorization, line_id):
            if authorization.customer_id == "cust_2":
                authorization.redeem(TaskChannel.CALL)  # number handed to the carrier...
                raise ConnectionError("carrier unreachable")  # ...then the carrier fails
            return super().place_call(authorization, line_id)

    dialer = FlakyDialer()
    t1 = _task(task_id="fu-1", source_call_id="call_1", created_at=UTC_1800 - timedelta(minutes=1))
    t2 = _task(task_id="fu-2", source_call_id="call_2", customer_id="cust_2", created_at=UTC_1800 - timedelta(minutes=1))
    outcomes = callback_orchestration.orchestrate(
        [t1, t2], dialer,
        phone_by_call_id={"call_1": LA_PHONE, "call_2": LA_PHONE_2}, line_by_call_id={},
        timezone_by_call_id={"call_1": LA, "call_2": LA}, gate=_gate(UTC_1800), now=UTC_1800,
    )
    assert outcomes[0].task.status == TaskStatus.SENT
    assert outcomes[1].attempted is True
    assert outcomes[1].task.status == TaskStatus.FAILED
    assert "ConnectionError" in outcomes[1].skip_reason
    assert LA_PHONE_2 not in outcomes[1].skip_reason


def test_exhausted_escalation_retry_returns_the_same_resolution():
    exhausted = _task_json(
        task_id="fu-idem-esc4", purpose="escalation", channel="human_handoff",
        attempt_number=4, status="failed",
    )
    r1 = client.post("/agents/followup-sequencing/escalate", json={"task": exhausted}).json()
    r2 = client.post("/agents/followup-sequencing/escalate", json={"task": exhausted}).json()
    assert r1["resolution"]["resolution_id"] == r2["resolution"]["resolution_id"]


# --- F3: concurrency on in-memory dossier state ------------------------------

def test_concurrent_dossier_updates_do_not_lose_writes(monkeypatch):
    real = customer_dossier.build_or_update

    def slow_build(existing, calls, appts):
        out = real(existing, calls, appts)
        time.sleep(0.2)  # widen the read-modify-write window
        return out

    monkeypatch.setattr(api.customer_dossier, "build_or_update", slow_build)
    monkeypatch.setattr(api, "_dossiers", {})

    def post(cid):
        client.post("/agents/customer-dossier/update", json={"appointments": [{
            "appointment_id": f"a-{cid}", "customer_id": cid, "scheduled_at": "2026-09-22T12:00:00Z",
            "service_type": "x", "status": "scheduled"}]})

    threads = [threading.Thread(target=post, args=(f"cust_{i}",)) for i in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert set(api._dossiers) == {f"cust_{i}" for i in range(4)}


# --- F4: input validation — 422, never 500 -----------------------------------

def test_naive_datetime_is_422_not_500():
    r = client.post("/agents/appointment-tracking/detect", json={"appointments": [{
        "appointment_id": "a1", "customer_id": "c1", "scheduled_at": "2026-09-20T09:00:00",
        "service_type": "x", "status": "scheduled"}]})
    assert r.status_code == 422


def test_mixed_naive_and_aware_call_times_is_422_not_500():
    base = {"customer_id": "c", "phone_number": "+15550101", "direction": "inbound", "status": "missed", "line_id": "l"}
    r = client.post("/agents/missed-call-detection/detect", json={"call_events": [
        {**base, "call_id": "c1", "started_at": "2026-09-22T12:00:00Z"},
        {**base, "call_id": "c2", "started_at": "2026-09-22T13:00:00"},
    ]})
    assert r.status_code == 422


def test_unknown_resolution_type_is_422_not_500():
    r = client.post("/agents/resolution-writeback/resolve", json={"events": [
        {"entity_type": "call", "entity_id": "call_1", "resolution_type": "totally_made_up"}]})
    assert r.status_code == 422


def test_unknown_entity_type_is_422():
    r = client.post("/agents/resolution-writeback/resolve", json={"events": [
        {"entity_type": "spaceship", "entity_id": "x", "resolution_type": "booked"}]})
    assert r.status_code == 422


def test_escalating_a_non_failed_task_is_409_not_500():
    r = client.post("/agents/followup-sequencing/escalate", json={"task": _task_json(status="pending")})
    assert r.status_code == 409


@pytest.mark.parametrize("phone", ["5550101", "+0555", "+1 555 0101", "+1555010112345678", "call me"])
def test_phone_number_must_be_e164(phone):
    r = client.post("/agents/missed-call-detection/detect", json={"call_events": [{
        "call_id": "c1", "phone_number": phone, "direction": "inbound", "status": "missed",
        "started_at": "2026-09-22T12:00:00Z", "line_id": "l"}]})
    assert r.status_code == 422


def test_orchestrate_phone_map_values_must_be_e164():
    r = client.post("/agents/callback-orchestration/run", json={
        "tasks": [], "phone_by_call_id": {"call_1": "not a number"}})
    assert r.status_code == 422


def test_oversized_id_and_oversized_batch_are_rejected():
    ev = {"call_id": "x" * 129, "phone_number": "+15550101", "direction": "inbound",
          "status": "missed", "started_at": "2026-09-22T12:00:00Z", "line_id": "l"}
    assert client.post("/agents/missed-call-detection/detect", json={"call_events": [ev]}).status_code == 422
    ev["call_id"] = "ok"
    assert client.post("/agents/missed-call-detection/detect", json={"call_events": [ev] * 1001}).status_code == 422


def test_validation_errors_do_not_echo_caller_pii():
    """FastAPI's default 422 body includes each error's `input` — for a
    missing field that is the whole submitted object, i.e. the caller's
    phone number and voicemail transcript echoed straight back (and into
    any proxy/client log that records error bodies)."""
    r = client.post("/agents/missed-call-detection/detect", json={"call_events": [{
        "call_id": "c1", "phone_number": "+15550199", "status": "missed",
        "voicemail_transcript": "Hi this is Jordan Private",
        "started_at": "2026-09-22T12:00:00Z", "line_id": "l"}]})
    assert r.status_code == 422
    assert "+15550199" not in r.text
    assert "Jordan" not in r.text
    assert "direction" in r.text  # still says WHICH field is wrong


def test_task_reason_does_not_carry_the_full_phone_number():
    ev = CallEvent(
        call_id="c1", customer_id="c", phone_number="+15550199",
        direction=CallDirection.INBOUND, status=CallStatus.MISSED,
        started_at=NOON_UTC, line_id="line_main",
    )
    [task] = missed_call_detection.detect([ev], now=NOON_UTC)
    assert "+15550199" not in task.reason
    assert "0199" in task.reason  # last four kept so a human can still tell calls apart


# --- F5: money wire format per BUILD_CONTRACTS section 1 --------------------

# Fix wave 1, F15: this test used to feed STRINGS "0.125", "1.005", "12.3"
# and expect them rounded — i.e. it enshrined the defect (a wire string is
# never rounded; those are rejected now, see test_fix_wave_1_f15_money.py).
# Half-up rounding still applies to COMPUTED values, which is what is
# asserted here.
@pytest.mark.parametrize("given,expected", [
    (Decimal("0.125"), "0.13"), (Decimal("1.005"), "1.01"), (1.005, "1.01"), (49.99, "49.99"), (12, "12.00"),
    ("12.30", "12.30"),
])
def test_money_is_half_up_two_decimal_string(given, expected):
    v = LabeledValue(amount_usd=given, confidence="low")
    assert v.model_dump(mode="json")["amount_usd"] == expected
    assert v.amount_usd == Decimal(expected)


@pytest.mark.parametrize("given", ["0.004", 0.004, "0", "-1.00", "NaN", "Infinity", "0.125", "1.005", "12.3"])
def test_positive_money_rejects_zero_after_rounding_and_non_finite(given):
    with pytest.raises(ValidationError):
        LabeledValue(amount_usd=given, confidence="low")
