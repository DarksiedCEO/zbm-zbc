"""
Fix wave 1, finding F3 (High, CONFIRMED by AEGIS, Sep 24 2026): quiet-hours
bypass and unlimited redials, reproduced over the API.

Pre-fix, `POST /agents/callback-orchestration/run` trusted the caller's
`timezone_by_call_id` without checking it against the number being dialed,
and deduplicated only by the caller-chosen `task_id`. AEGIS probe
(scratchpad/review/ful_tz.py), at 02:00 America/Los_Angeles dialing
+12135550101: zone "UTC" -> attempted True; "Asia/Tokyo" -> attempted
True; three fresh task ids -> 5 calls placed at 02:00 LA local.

Every test in this file drives the HTTP route only (no new imports), so it
ran — and failed — against the pre-fix code; the failing output is quoted
in the fix report. The window/limit rules themselves are unit-tested in
test_outbound_gate.py.

Times: 2026-09-22 is US/Canada daylight time. The strict window for a
continental +1 number is 08:00-21:00 in EVERY US/Canada continental zone,
i.e. 15:00Z (08:00 PDT) to 23:30Z (21:00 NDT, St. John's).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import api
from conftest import TEST_SERVICE_TOKEN
from fulfillment_schema import FollowUpTask, TaskChannel, TaskPurpose
from integrations.sip_dialer import InMemorySipDialer

client = TestClient(api.app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})
ROUTE = "/agents/callback-orchestration/run"

UTC_0900 = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)    # 02:00 Los Angeles
UTC_1800 = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)   # 11:00 LA, 14:00 NY, 15:30 St. John's
LA_NUMBER = "+12135550101"   # 213 = Los Angeles
NY_NUMBER = "+12125550101"   # 212 = New York City
HI_NUMBER = "+18085550101"   # 808 = Hawaii


class _Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def env(monkeypatch):
    dialer = InMemorySipDialer()
    clock = _Clock(UTC_1800)
    monkeypatch.setattr(api, "_dialer", dialer)
    monkeypatch.setattr(api, "_now", clock)
    monkeypatch.setattr(api, "_attempted_task_ids", api._new_dedupe())
    return dialer, clock


def _task(task_id: str, call_id: str = "call_1", customer_id: str | None = "cust_1", at: datetime = UTC_1800) -> dict:
    return FollowUpTask(
        task_id=task_id, purpose=TaskPurpose.MISSED_CALL_CALLBACK, channel=TaskChannel.CALL,
        customer_id=customer_id, source_call_id=call_id,
        created_at=at - timedelta(days=2), due_at=at + timedelta(minutes=5), reason="r",
    ).model_dump(mode="json")


def _run(tasks: list[dict], phones: dict[str, str], zones: dict[str, str]) -> list[dict]:
    r = client.post(ROUTE, json={"tasks": tasks, "phone_by_call_id": phones, "timezone_by_call_id": zones})
    assert r.status_code == 200, r.text
    return r.json()["outcomes"]


# --- the AEGIS reproductions ---------------------------------------------------

@pytest.mark.parametrize("claimed", ["UTC", "Asia/Tokyo", "Etc/GMT-9", "Europe/London"])
def test_aegis_la_number_at_2am_local_is_not_dialed_whatever_zone_the_caller_claims(env, claimed):
    dialer, clock = env
    clock.t = UTC_0900
    [o] = _run([_task("fu-1", at=UTC_0900)], {"call_1": LA_NUMBER}, {"call_1": claimed})
    assert o["attempted"] is False
    assert dialer.calls_placed == []


def test_aegis_honolulu_claimed_for_a_new_york_number_at_2am_new_york_is_not_dialed(env):
    """06:00Z = 02:00 EDT in New York but 20:00 HST in Honolulu. Pre-fix:
    the claimed zone alone decided, so this was dialed."""
    dialer, clock = env
    clock.t = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
    [o] = _run([_task("fu-1", at=clock.t)], {"call_1": NY_NUMBER}, {"call_1": "Pacific/Honolulu"})
    assert o["attempted"] is False
    assert "contact window" in o["skip_reason"]
    assert dialer.calls_placed == []


def test_new_york_claimed_for_a_hawaii_number_at_6am_hawaii_is_not_dialed(env):
    """The lie in the other direction: 16:00Z = 12:00 EDT, 06:00 HST."""
    dialer, clock = env
    clock.t = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)
    [o] = _run([_task("fu-1", at=clock.t)], {"call_1": HI_NUMBER}, {"call_1": "America/New_York"})
    assert o["attempted"] is False
    assert "contact window" in o["skip_reason"]
    assert dialer.calls_placed == []


def test_aegis_fresh_task_ids_do_not_redial_the_same_number(env):
    """Pre-fix: every fresh task_id was a fresh dial. Five requests, five
    different task ids, one number, all inside the window -> one call."""
    dialer, _ = env
    outcomes = [
        _run([_task(f"x{i}")], {"call_1": LA_NUMBER}, {"call_1": "America/Los_Angeles"})[0]
        for i in range(5)
    ]
    assert [o["attempted"] for o in outcomes] == [True, False, False, False, False]
    assert all("spacing" in o["skip_reason"] for o in outcomes[1:])
    assert len(dialer.calls_placed) == 1


def test_same_number_under_two_call_ids_in_one_batch_is_dialed_once(env):
    dialer, _ = env
    outcomes = _run(
        [_task("a", call_id="c1"), _task("b", call_id="c2")],
        {"c1": LA_NUMBER, "c2": LA_NUMBER},
        {"c1": "America/Los_Angeles", "c2": "America/Los_Angeles"},
    )
    assert [o["attempted"] for o in outcomes] == [True, False]
    assert len(dialer.calls_placed) == 1


def test_attempts_per_number_are_capped_per_rolling_24h_and_spaced(env):
    dialer, clock = env
    base = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)  # 08:00 PDT, first minute of the strict window

    def attempt(tid: str, t: datetime) -> dict:
        clock.t = t
        return _run([_task(tid, at=t)], {"call_1": LA_NUMBER}, {"call_1": "America/Los_Angeles"})[0]

    assert attempt("t1", base)["attempted"] is True
    early = attempt("t2", base + timedelta(hours=1, minutes=59))
    assert early["attempted"] is False and "spacing" in early["skip_reason"]
    assert attempt("t3", base + timedelta(hours=2))["attempted"] is True
    assert attempt("t4", base + timedelta(hours=4))["attempted"] is True
    capped = attempt("t5", base + timedelta(hours=6))
    assert capped["attempted"] is False and "attempt limit" in capped["skip_reason"]
    # Exactly 24h after the first attempt it leaves the rolling window.
    assert attempt("t6", base + timedelta(hours=24))["attempted"] is True
    assert len(dialer.calls_placed) == 4


def test_limits_also_apply_per_customer_across_numbers(env):
    dialer, _ = env
    outcomes = _run(
        [_task("a", call_id="c1"), _task("b", call_id="c2")],
        {"c1": LA_NUMBER, "c2": "+12135550199"},
        {"c1": "America/Los_Angeles", "c2": "America/Los_Angeles"},
    )
    assert [o["attempted"] for o in outcomes] == [True, False]
    assert "customer" in outcomes[1]["skip_reason"]
    assert len(dialer.calls_placed) == 1


# --- sweep: the clock the window is judged on is read at dial time -------------

def test_window_is_rechecked_at_dial_time_not_once_per_request(monkeypatch, env):
    """Pre-fix, `now` was read once per request and reused for every task in
    the batch. With a real dialer (seconds to minutes per call) a large
    batch started at 20:59 kept dialing after 21:00. Here each dial takes
    an hour of simulated time: 20:30 HST -> dial 1 -> 21:30 HST -> dial 2
    must be refused."""
    dialer, clock = env
    clock.t = datetime(2026, 9, 22, 6, 30, tzinfo=timezone.utc)  # 20:30 HST

    class SlowDialer(InMemorySipDialer):
        def place_call(self, *args, **kwargs):
            result = super().place_call(*args, **kwargs)
            clock.t = clock.t + timedelta(hours=1)
            return result

    slow = SlowDialer()
    monkeypatch.setattr(api, "_dialer", slow)
    outcomes = _run(
        [_task("a", call_id="c1", customer_id="ca", at=clock.t), _task("b", call_id="c2", customer_id="cb", at=clock.t)],
        {"c1": HI_NUMBER, "c2": "+18085550199"},
        {"c1": "Pacific/Honolulu", "c2": "Pacific/Honolulu"},
    )
    assert [o["attempted"] for o in outcomes] == [True, False]
    assert "contact window" in outcomes[1]["skip_reason"]
    assert len(slow.calls_placed) == 1


# --- numbers whose zone cannot be verified fail closed -------------------------

@pytest.mark.parametrize("phone,zone", [
    ("+442071234567", "Europe/London"),     # non-+1, no country rule configured
    ("+15550101", "America/Los_Angeles"),   # not a 10-digit NANP number
    ("+18005550101", "America/New_York"),   # toll-free: no location at all
    ("+11235550101", "America/New_York"),   # NPA cannot start with 1
])
def test_numbers_whose_zone_cannot_be_verified_are_not_dialed(env, phone, zone):
    dialer, _ = env
    [o] = _run([_task("fu-1")], {"call_1": phone}, {"call_1": zone})
    assert o["attempted"] is False
    assert dialer.calls_placed == []
    assert phone not in o["skip_reason"]  # reasons never carry the full number


def test_control_a_real_la_number_in_la_daytime_is_still_dialed(env):
    dialer, _ = env
    [o] = _run([_task("fu-1")], {"call_1": LA_NUMBER}, {"call_1": "America/Los_Angeles"})
    assert o["attempted"] is True
    assert o["dial_placed"] is True
    assert len(dialer.calls_placed) == 1
