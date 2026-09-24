"""Intelligence 9 (Promise Keeper) + the noon cutoff, DST and quiet hours."""

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from config import OnboardingConfig
from conftest import Clock, client_for, make_service, start_body
from intelligences import i09_promise_keeper as pk
from onboarding_schema import Commitment, CommitmentStatus

LA = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
NOON = time(12, 0)


def ec(local_dt):
    return pk.escalation_commitment(local_dt.astimezone(UTC), NOON, "America/Los_Angeles", time(17, 0), time(9, 0))


def test_before_noon_is_today_due_1700_local():
    c = ec(datetime(2026, 9, 24, 11, 59, 59, tzinfo=LA))
    assert c.text == "Andre will get back to you today." and c.form == "today"
    assert c.due_at == datetime(2026, 9, 24, 17, 0, tzinfo=LA).astimezone(UTC)


def test_exactly_noon_is_first_thing_tomorrow():
    c = ec(datetime(2026, 9, 24, 12, 0, 0, tzinfo=LA))
    assert c.text == "Andre will get back to you first thing tomorrow." and c.form == "first_thing_tomorrow"
    assert c.due_at == datetime(2026, 9, 25, 9, 0, tzinfo=LA).astimezone(UTC)


def test_after_noon_is_first_thing_tomorrow_and_never_shortly():
    c = ec(datetime(2026, 9, 24, 12, 0, 1, tzinfo=LA))
    assert c.form == "first_thing_tomorrow" and "shortly" not in c.text


def test_cutoff_is_computed_in_la_time_not_a_fixed_offset_across_spring_forward():
    # 2026-03-08 is the US spring-forward day. 19:30 UTC is 11:30 PST on Mar 7
    # (before noon) but 12:30 PDT on Mar 8 (after noon).
    assert pk.escalation_commitment(datetime(2026, 3, 7, 19, 30, tzinfo=UTC), NOON, "America/Los_Angeles", time(17), time(9)).form == "today"
    assert pk.escalation_commitment(datetime(2026, 3, 8, 19, 30, tzinfo=UTC), NOON, "America/Los_Angeles", time(17), time(9)).form == "first_thing_tomorrow"
    # Asked 13:00 PST on Mar 7 -> due 09:00 PDT on Mar 8 = 16:00 UTC (offset changed overnight).
    c = pk.escalation_commitment(datetime(2026, 3, 7, 21, 0, tzinfo=UTC), NOON, "America/Los_Angeles", time(17), time(9))
    assert c.due_at == datetime(2026, 3, 8, 16, 0, tzinfo=UTC)


def test_fall_back_day_due_time_uses_the_new_offset():
    # 2026-11-01 is the US fall-back day. Asked 13:00 PDT Oct 31 -> due 09:00 PST Nov 1 = 17:00 UTC.
    c = pk.escalation_commitment(datetime(2026, 10, 31, 20, 0, tzinfo=UTC), NOON, "America/Los_Angeles", time(17), time(9))
    assert c.due_at == datetime(2026, 11, 1, 17, 0, tzinfo=UTC)
    # Exactly noon PST on Nov 1 = 20:00 UTC -> tomorrow; 19:59:59 UTC -> today.
    assert pk.escalation_commitment(datetime(2026, 11, 1, 20, 0, tzinfo=UTC), NOON, "America/Los_Angeles", time(17), time(9)).form == "first_thing_tomorrow"
    assert pk.escalation_commitment(datetime(2026, 11, 1, 19, 59, 59, tzinfo=UTC), NOON, "America/Los_Angeles", time(17), time(9)).form == "today"


def test_naive_datetime_is_refused():
    with pytest.raises(ValueError):
        pk.escalation_commitment(datetime(2026, 9, 24, 10, 0), NOON, "America/Los_Angeles", time(17), time(9))


def test_quiet_hours_are_in_the_clients_own_time_zone():
    at = datetime(2026, 9, 24, 17, 0, tzinfo=UTC)  # 10:00 LA, 13:00 NY, 02:00 Tokyo
    assert pk.in_quiet_hours(at, "America/New_York", time(21), time(8)) is False
    assert pk.in_quiet_hours(at, "Asia/Tokyo", time(21), time(8)) is True
    assert pk.in_quiet_hours(at, "America/Los_Angeles", time(9), time(11)) is True  # non-wrapping window


def _commit(created, due, kind="deliverable", category="report", **kw):
    return Commitment(commitment_id="c1", client_id="x", kind=kind, category=category, text="Your audit report is due.",
                      owner="andre", created_at=created, due_at=due, **kw)


def _decide(c, now, tz="America/New_York"):
    return pk.decide(c, now, tz, time(21), time(8), NOON, "America/Los_Angeles", time(17), time(9), 3, 1)


def test_promise_keeper_warns_the_client_before_the_commitment_passes():
    created = datetime(2026, 9, 24, 13, 0, tzinfo=UTC)
    due = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)  # 16:00 NY
    c = _commit(created, due)
    early = _decide(c, datetime(2026, 9, 24, 14, 0, tzinfo=UTC))
    assert [a.action for a in early] == ["none"] and early[0].send_at == due - timedelta(hours=1)
    nudge = _decide(c, datetime(2026, 9, 24, 17, 30, tzinfo=UTC))
    assert [a.action for a in nudge] == ["nudge_andre", "none"]
    warn = _decide(c, datetime(2026, 9, 24, 19, 0, tzinfo=UTC))
    w = [a for a in warn if a.action == "warn_client"][0]
    assert w.send_at < due and "going to slip" in w.message and "The new time is" in w.message
    late = _decide(c, due + timedelta(minutes=1))
    assert [a.action for a in late] == ["nudge_andre", "breached"]


def test_on_track_commitment_triggers_no_messages():
    c = _commit(datetime(2026, 9, 24, 13, 0, tzinfo=UTC), datetime(2026, 9, 24, 20, 0, tzinfo=UTC), status=CommitmentStatus.ON_TRACK)
    assert [a.action for a in _decide(c, datetime(2026, 9, 24, 19, 30, tzinfo=UTC))] == ["none"]


def test_warning_moves_out_of_quiet_hours_but_stays_before_due():
    # Client in Tokyo, quiet 21:00-08:00 local; due 08:30 JST, so due-1h (07:30) is quiet.
    # The warning moves to the latest non-quiet minute before that: 20:59 JST the evening before.
    tokyo = ZoneInfo("Asia/Tokyo")
    due = datetime(2026, 9, 25, 8, 30, tzinfo=tokyo).astimezone(UTC)
    created = due - timedelta(hours=30)
    warn_at, override = pk.plan_warn_time(created, due, 1, "Asia/Tokyo", time(21), time(8))
    assert override is False
    assert warn_at == datetime(2026, 9, 24, 20, 59, tzinfo=tokyo).astimezone(UTC)
    # No non-quiet moment at all before due (commitment made inside quiet hours): sent anyway, flagged.
    due2 = datetime(2026, 9, 25, 7, 0, tzinfo=tokyo).astimezone(UTC)
    w2, override2 = pk.plan_warn_time(due2 - timedelta(hours=2), due2, 1, "Asia/Tokyo", time(21), time(8))
    assert override2 is True and w2 < due2


def test_service_rolls_unengaged_today_escalation_at_noon_and_tells_client_before_old_time():
    clock = Clock(datetime(2026, 9, 24, 10, 0, tzinfo=LA).astimezone(UTC))
    cfg = replace(OnboardingConfig(), deal_size_threshold_usd=Decimal("100000.00"))
    svc = make_service(all_fakes=True, config=cfg, clock=clock)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    r = c.post("/onboarding/clients/client_a/messages", json={"text": "I need a human"}).json()
    assert r["reply"].endswith("Andre will get back to you today.")
    old_due = datetime.fromisoformat(r["escalation"]["client_commitment_due_at"])
    clock.t = datetime(2026, 9, 24, 12, 0, tzinfo=LA).astimezone(UTC)
    t = c.post("/onboarding/clients/client_a/tick").json()
    acts = {a["action"]: a for a in t["commitment_actions"]}
    assert set(acts) == {"nudge_andre", "warn_client"}
    w = acts["warn_client"]
    assert datetime.fromisoformat(w["send_at"]) < old_due
    assert datetime.fromisoformat(w["new_due_at"]) == datetime(2026, 9, 25, 9, 0, tzinfo=LA).astimezone(UTC)
    assert "promise_warn_client" in svc.ledger.types() and "promise_nudge_andre" in svc.ledger.types()
    # Nothing further to do on the next tick: the promise already moved.
    assert c.post("/onboarding/clients/client_a/tick").json()["commitment_actions"] == []


def test_soft_trigger_message_held_during_the_clients_quiet_hours():
    clock = Clock(datetime(2026, 9, 24, 17, 0, tzinfo=UTC))  # 02:00 in Tokyo
    svc = make_service(all_fakes=True, clock=clock)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body(time_zone="Asia/Tokyo")).status_code == 201
    clock.advance(hours=49)  # 03:00 Tokyo, still quiet
    t = c.post("/onboarding/clients/client_a/tick").json()
    assert t["stuck"]["client_message_status"].startswith("held: client quiet hours")
