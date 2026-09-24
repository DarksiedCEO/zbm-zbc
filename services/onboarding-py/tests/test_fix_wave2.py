"""Fix wave 2 (Sep 24 2026) — two onboarding leftovers from fix wave 1.

L2  Promise Keeper nudge flag: ``andre_nudged`` must be set only when the
    push to Andre was actually delivered. An undelivered nudge is recorded
    and retried on the next tick, a bounded number of times
    (``andre_nudge_max_attempts``); the client warning before the deadline
    still fires on time whatever happens to the nudges (including a push
    channel that raises).
L3  Commitment state after a partial failure: if the push to Andre is
    delivered but the ``client_commitment_made`` record then fails, the
    client was never told a time, so no commitment may exist in state
    (record-first: the commitment is stored only after its record — ADR 0004).
"""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from config import OnboardingConfig
from conftest import Clock, client_for, make_service, start_body
from ledger import FakeLedgerClient, LedgerWriteError

LA = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc


def la(day, hh, mm=0):
    return datetime(2026, 9, day, hh, mm, tzinfo=LA).astimezone(UTC)


class ScriptedNotifier:
    """Push channel to Andre's phone whose outcome the test controls:
    ``ok`` delivers, ``fail`` reports not delivered, ``raise`` raises."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.calls: list[str] = []

    def push(self, key, payload):
        self.calls.append(key)
        if self.mode == "ok":
            return True, "delivered (test)"
        if self.mode == "fail":
            return False, "test: push not delivered"
        raise ConnectionError("test: push gateway unreachable")


def _client_with_tomorrow_commitment():
    """Client started at 13:00 LA (after the noon cutoff): the deal-size
    escalation is delivered and the client is told "first thing tomorrow",
    due 09:00 LA on Sep 25. Nudge window opens 06:00 LA; client warning at
    08:00 LA (client in New York: 11:00, outside quiet hours)."""
    clock = Clock(la(24, 13))
    svc = make_service(all_fakes=True, clock=clock)
    svc.depts.notifier = ScriptedNotifier("ok")
    c = client_for(svc)
    r = c.post("/onboarding/clients", json=start_body())
    assert r.status_code == 201, r.text
    esc = r.json()["escalation"]
    assert esc["push_delivered"] is True and esc["commitment_id"]
    cm = svc.clients["client_a"].commitments[esc["commitment_id"]]
    assert cm.due_at == la(25, 9) and cm.category == "first_thing_tomorrow"
    return svc, c, clock, cm


def _nudge_pushes(svc):
    return [k for k in svc.depts.notifier.calls if k.startswith("nudge-")]


def _tick(c):
    r = c.post("/onboarding/clients/client_a/tick")
    assert r.status_code == 200, r.text
    return [a["action"] for a in r.json()["commitment_actions"]]


def _payloads(svc, event_type):
    return [p for e, p in zip(svc.ledger.events, svc.ledger.payloads) if e["event_type"] == event_type]


# --- L2: nudge flag only on confirmed delivery -------------------------------------


def test_l2_nudge_flag_set_only_on_confirmed_delivery_and_retried_next_tick():
    svc, c, clock, cm = _client_with_tomorrow_commitment()
    svc.depts.notifier.mode = "fail"
    clock.t = la(25, 6, 30)
    assert _tick(c) == ["nudge_andre"]
    assert cm.andre_nudged is False, "an undelivered nudge must not count as nudging Andre"
    assert len(_nudge_pushes(svc)) == 1
    [res] = _payloads(svc, "promise_nudge_result")
    assert res["delivered"] is False and res["attempt"] == 1 and res["will_retry"] is True

    # Next tick: retried; this time it is delivered.
    svc.depts.notifier.mode = "ok"
    clock.advance(minutes=5)
    assert _tick(c) == ["nudge_andre"]
    assert cm.andre_nudged is True
    assert len(_nudge_pushes(svc)) == 2
    assert _payloads(svc, "promise_nudge_result")[-1]["delivered"] is True

    # Delivered once: no further nudges.
    clock.advance(minutes=5)
    assert _tick(c) == []
    assert len(_nudge_pushes(svc)) == 2


def test_l2_nudge_retries_are_bounded_and_every_attempt_is_recorded():
    svc, c, clock, cm = _client_with_tomorrow_commitment()
    svc.depts.notifier.mode = "fail"
    max_attempts = 3
    assert getattr(svc.config, "andre_nudge_max_attempts", 3) == max_attempts
    clock.t = la(25, 6, 0)
    for _ in range(max_attempts + 4):
        _tick(c)
        clock.advance(minutes=5)
    assert len(_nudge_pushes(svc)) == max_attempts
    results = _payloads(svc, "promise_nudge_result")
    assert [r["attempt"] for r in results] == list(range(1, max_attempts + 1))
    assert all(r["delivered"] is False for r in results)
    assert [r["will_retry"] for r in results] == [True] * (max_attempts - 1) + [False]
    assert cm.andre_nudged is False and cm.andre_nudge_failures == max_attempts


@pytest.mark.parametrize("mode", ["fail", "raise"])
def test_l2_client_warning_fires_on_time_even_when_every_nudge_push_fails(mode):
    svc, c, clock, cm = _client_with_tomorrow_commitment()
    svc.depts.notifier.mode = mode
    clock.t = la(25, 6, 0)
    _tick(c)  # nudge attempt 1 (fails)
    clock.t = la(25, 8, 0)  # warn time: 1h before 09:00 LA
    actions = _tick(c)
    assert "warn_client" in actions, actions
    assert "nudge_andre" in actions, "attempt 2 is still made in the same tick"
    assert cm.status.value == "rescheduled" and cm.due_at > la(25, 9)
    assert "promise_warn_client" in svc.ledger.types()


def test_l2_breach_nudge_not_delivered_is_retried_while_breached():
    svc, c, clock, cm = _client_with_tomorrow_commitment()
    # Isolate the breach path: an earlier (pre-due) nudge WAS delivered and the
    # client warning was handled, then the time passes unkept.
    cm.andre_nudged = True
    cm.client_warned = True
    svc.depts.notifier.mode = "fail"
    clock.t = la(25, 9, 1)
    actions = _tick(c)
    assert actions == ["nudge_andre", "breached"]
    assert cm.status.value == "breached" and cm.andre_nudged is False
    svc.depts.notifier.mode = "ok"
    clock.advance(minutes=5)
    assert _tick(c) == ["nudge_andre"], "the undelivered breach nudge is retried"
    assert cm.andre_nudged is True
    clock.advance(minutes=5)
    assert _tick(c) == []


def test_l2_escalation_push_that_raises_is_recorded_as_not_delivered():
    clock = Clock(la(24, 13))
    svc = make_service(all_fakes=True, clock=clock)
    svc.depts.notifier = ScriptedNotifier("raise")
    r = client_for(svc).post("/onboarding/clients", json=start_body())
    assert r.status_code == 201, r.text
    esc = r.json()["escalation"]
    assert esc["push_delivered"] is False and esc["commitment_id"] is None
    assert "andre_push_not_delivered" in svc.ledger.types()
    assert svc.clients["client_a"].commitments == {}


# --- L3: no commitment in state unless its record was written ----------------------


class FailOn(FakeLedgerClient):
    def __init__(self, *types):
        super().__init__()
        self.fail_types = set(types)

    def record_event(self, event_id, department, event_type, *a):
        if event_type in self.fail_types:
            raise LedgerWriteError("fake ledger: write refused")
        return super().record_event(event_id, department, event_type, *a)


def test_l3_start_client_commitment_not_stored_when_its_record_fails():
    svc = make_service(all_fakes=True, ledger=FailOn("client_commitment_made"))
    r = client_for(svc).post("/onboarding/clients", json=start_body())
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["proceeded"] is True and "andre_push" in body["outside_effects_done"]
    rec = svc.clients["client_a"]
    assert rec.commitments == {}, "the client was never told a time; no commitment may exist"
    [eid] = rec.escalation_ids
    esc = svc.escalations[eid]
    assert esc.push_delivered is True  # that did happen
    assert esc.commitment_id is None
    assert esc.client_message_status.startswith("held:") and "has not been told" in esc.client_message_status


def test_l3_message_human_commitment_not_stored_when_its_record_fails_and_tick_makes_no_promise_actions():
    clock = Clock()
    led = FailOn()
    cfg = replace(OnboardingConfig(), deal_size_threshold_usd=Decimal("100000.00"))  # 1500.00 does not escalate
    svc = make_service(all_fakes=True, config=cfg, ledger=led, clock=clock)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    assert svc.clients["client_a"].commitments == {}
    led.fail_types = {"client_commitment_made"}
    r = c.post("/onboarding/clients/client_a/messages", json={"text": "I want to talk to a human"})
    assert r.status_code == 503 and r.json()["proceeded"] is True, r.text
    assert svc.clients["client_a"].commitments == {}
    led.fail_types = set()
    clock.advance(hours=7)  # past the would-be "today" 17:00 due time
    t = c.post("/onboarding/clients/client_a/tick").json()
    assert t["commitment_actions"] == [], "no promise actions for a commitment the client never received"
    assert not [e for e in svc.ledger.types() if e.startswith("promise_")]


def test_l3_successful_escalation_still_stores_commitment_after_its_record():
    svc = make_service(all_fakes=True)
    r = client_for(svc).post("/onboarding/clients", json=start_body())
    assert r.status_code == 201
    esc = r.json()["escalation"]
    assert esc["commitment_id"] in svc.clients["client_a"].commitments
    assert esc["client_message_status"] in ("released", "held: client quiet hours; send at the next allowed moment")
    assert "client_commitment_made" in svc.ledger.types()


def test_l2_pending_breach_nudge_is_dropped_once_andre_resolves_the_escalation():
    from conftest import andre_resolve_body

    svc, c, clock, cm = _client_with_tomorrow_commitment()
    cm.client_warned = True
    svc.depts.notifier.mode = "fail"
    clock.t = la(25, 9, 1)
    assert _tick(c) == ["nudge_andre", "breached"] and cm.breach_nudge_pending is True
    [eid] = svc.clients["client_a"].escalation_ids
    r = c.post(f"/onboarding/clients/client_a/escalations/{eid}/resolve",
               json=andre_resolve_body("client_a", eid, "called the client", "deal_size"))
    assert r.status_code == 200, r.text
    assert cm.breach_nudge_pending is False
    clock.advance(minutes=5)
    assert _tick(c) == []
