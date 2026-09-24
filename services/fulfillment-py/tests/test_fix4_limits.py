"""
Fix wave 4 (Sep 24 2026) — defect classes AEGIS round 3 found in sibling
services, ruled out or fixed here with evidence:

  1. request body size: 413 from Content-Length before the body is read,
     and while streaming (chunked, no Content-Length); 422 for item caps.
  2. event-loop blocking: JSON parsing, validation, agent work and response
     rendering run off the event loop (see test_fix4_live.py for the
     real-uvicorn /health latency proof).
  3. regexes: every regex in src/ is inventoried here and timed on 100 KB
     adversarial inputs (< 50 ms each).
  4. outbound gate: new-key admission is rate limited so the tracked-number
     cap cannot be filled in a burst; capacity is visible (status/alert).
  5. customer-dossier/update copies and returns only the affected dossiers.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

import api
from conftest import TEST_SERVICE_TOKEN
from contact_window import ContactWindow, parse_contact_window
from fulfillment_schema import CustomerDossier, EntityId, PhoneE164, TaskChannel, TaskId
from fulfillment_schema import money
from integrations.sip_dialer import InMemorySipDialer
import outbound_gate
from outbound_gate import AttemptLimits, ContactRefused, OutboundContactGate
import recipient_zones
from recipient_zones import parse_country_zones, zones_to_check

AUTH = {"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}
client = TestClient(api.app, headers=AUTH, raise_server_exceptions=False)
anon = TestClient(api.app, raise_server_exceptions=False)

LIMIT = 4 * 1024 * 1024  # the contract: 4 MiB, derived below from the max batches
SRC = Path(__file__).resolve().parents[1] / "src"

# --- worst-case max batches (every bounded field at its maximum) ------------

I128 = "a" * 128
I256 = "t" * 256
D = "2026-09-22T12:00:00.000000+14:00"
PHONE15 = "+" + "1" * 15


def _ev(i: int, transcript: str | None = None) -> dict:
    return {"call_id": f"{i:0>128}", "customer_id": I128, "phone_number": PHONE15, "direction": "outbound",
            "status": "no_answer", "started_at": D, "ended_at": D, "duration_seconds": 86400,
            "voicemail_transcript": transcript, "line_id": I128}


def _appt(i: int) -> dict:
    return {"appointment_id": f"{i:0>128}", "customer_id": I128, "scheduled_at": D, "service_type": "s" * 128,
            "status": "completed", "completion_confirmed_at": D, "technician_id": I128}


def _task(i: int) -> dict:
    return {"task_id": f"{i:0>256}", "purpose": "appointment_confirmation", "channel": "human_handoff",
            "customer_id": I128, "source_call_id": f"{i:0>128}", "source_appointment_id": I128,
            "created_at": D, "due_at": D, "attempt_number": 100, "status": "escalated", "reason": "r" * 2000}


KEYS = [f"{i:0>128}" for i in range(1000)]
MAX_BATCHES = {
    "/agents/missed-call-detection/detect": {"call_events": [_ev(i) for i in range(1000)]},
    "/agents/appointment-tracking/detect": {"appointments": [_appt(i) for i in range(1000)]},
    "/agents/customer-dossier/update": {"call_events": [_ev(i) for i in range(1000)],
                                        "appointments": [_appt(i) for i in range(1000)]},
    # human_handoff tasks: the orchestrator skips them, so nothing is dialed.
    "/agents/callback-orchestration/run": {
        "tasks": [_task(i) for i in range(1000)],
        "phone_by_call_id": {k: PHONE15 for k in KEYS},
        "line_by_call_id": {k: I128 for k in KEYS},
        "timezone_by_call_id": {k: "x" * 64 for k in KEYS},
    },
    "/agents/resolution-writeback/resolve": {"events": [
        {"entity_type": "appointment", "entity_id": f"{i:0>256}", "customer_id": I128,
         "resolution_type": "escalated_to_human"} for i in range(1000)]},
}


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    monkeypatch.setattr(api, "_dossiers", {})
    monkeypatch.setattr(api, "_attempted_task_ids", api._new_dedupe())


# === 1. body size ============================================================

def test_limit_constant_is_the_contract():
    assert api._MAX_BODY_BYTES == LIMIT


@pytest.mark.parametrize("route", list(MAX_BATCHES))
def test_worst_case_max_batch_fits_under_the_limit_and_is_accepted(route):
    body = json.dumps(MAX_BATCHES[route]).encode()
    assert len(body) < LIMIT, (route, len(body))
    r = client.post(route, content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 200, (route, r.status_code, r.text[:300])


def _padded(route: str, size: int) -> bytes:
    """A valid body for `route` padded with JSON whitespace to exactly `size` bytes."""
    core = json.dumps({"call_events": []} if "missed-call" in route else {"events": []}).encode()
    return core + b" " * (size - len(core))


def test_body_exactly_at_limit_is_accepted():
    body = _padded("/agents/missed-call-detection/detect", LIMIT)
    assert len(body) == LIMIT
    r = client.post("/agents/missed-call-detection/detect", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text[:200]


@pytest.mark.parametrize("route", list(MAX_BATCHES))
def test_body_one_byte_over_limit_is_413(route):
    body = _padded(route, LIMIT + 1)
    r = client.post(route, content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 413, (r.status_code, r.text[:200])
    assert str(LIMIT) in r.json()["detail"]


def test_streamed_body_without_content_length_is_413_while_streaming():
    sent = []

    def chunks():
        for _ in range(64):  # 64 x 128 KiB = 8 MiB offered
            sent.append(1)
            yield b" " * (128 * 1024)

    r = client.post("/agents/missed-call-detection/detect", content=chunks(),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413, (r.status_code, r.text[:200])


def test_oversized_content_length_is_413_even_unauthenticated():
    r = anon.post("/agents/missed-call-detection/detect", content=_padded("/agents/missed-call-detection/detect", LIMIT + 1))
    assert r.status_code == 413


def test_auth_is_checked_before_the_body_is_parsed():
    # Before: FastAPI parsed the body first, so an anonymous caller got a
    # 422 JSON error (and the parse cost) instead of a 401.
    r = anon.post("/agents/missed-call-detection/detect", content=b"{not json",
                  headers={"Content-Type": "application/json"})
    assert r.status_code == 401


LIST_FIELDS = [
    ("/agents/missed-call-detection/detect", "call_events", lambda i: _ev(i)),
    ("/agents/appointment-tracking/detect", "appointments", lambda i: _appt(i)),
    ("/agents/customer-dossier/update", "call_events", lambda i: _ev(i)),
    ("/agents/customer-dossier/update", "appointments", lambda i: _appt(i)),
    ("/agents/resolution-writeback/resolve", "events",
     lambda i: {"entity_type": "task", "entity_id": f"e{i}", "resolution_type": "booked"}),
    ("/agents/callback-orchestration/run", "tasks", lambda i: dict(_task(i), reason="r")),
]
DICT_FIELDS = ["phone_by_call_id", "line_by_call_id", "timezone_by_call_id"]


@pytest.mark.parametrize("route,field,make", LIST_FIELDS)
def test_1001_items_is_422_naming_the_field(route, field, make):
    body = {field: [make(i) for i in range(1001)]}
    if route.endswith("/run"):
        body.setdefault("phone_by_call_id", {})
    r = client.post(route, json=body)
    assert r.status_code == 422, r.text[:200]
    assert any(e["loc"][:2] == ["body", field] and e["type"] == "too_long" for e in r.json()["detail"])


@pytest.mark.parametrize("field", DICT_FIELDS)
def test_orchestrate_map_with_1001_entries_is_422(field):
    body = {"tasks": [], "phone_by_call_id": {}}
    body[field] = {f"k{i}": ("+12125550101" if field == "phone_by_call_id" else "America/New_York") for i in range(1001)}
    r = client.post("/agents/callback-orchestration/run", json=body)
    assert r.status_code == 422
    assert any(e["loc"][:2] == ["body", field] for e in r.json()["detail"])


def test_validation_errors_still_never_echo_input():
    r = client.post("/agents/missed-call-detection/detect",
                    json={"call_events": [{"phone_number": "+12125550101", "voicemail_transcript": "SECRET-PII"}]})
    assert r.status_code == 422
    assert "SECRET-PII" not in r.text and "+12125550101" not in r.text


def test_non_json_content_type_is_refused():
    r = client.post("/agents/missed-call-detection/detect", content=b'{"call_events":[]}',
                    headers={"Content-Type": "text/plain"})
    assert r.status_code in (415, 422)


# === 2. event loop: the handlers are not coroutines doing CPU work ===========

def test_health_is_a_coroutine_and_agent_work_is_offloaded():
    """Structural half of the proof (the timing half is live, in
    test_fix4_live.py): /health must not queue behind the thread pool, and
    no route may declare a pydantic body parameter (FastAPI parses and
    validates those ON the event loop, before the handler runs)."""
    import inspect
    from fastapi.routing import APIRoute

    for route in api.app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path == "/health":
            assert inspect.iscoroutinefunction(route.endpoint)
        assert route.dependant.body_params == [], route.path


# === 3. regexes ==============================================================

REGEX_INVENTORY = {
    # file -> number of regex sites; a new regex fails this until it is timed below.
    "recipient_zones.py": 3,          # E164, _NANP, country-code fullmatch
    "contact_window.py": 1,           # _WINDOW_RE
    "fulfillment_schema/money.py": 2,  # _FLOAT_TEXT, WIRE_PATTERN
    "fulfillment_schema/__init__.py": 2,  # _ID_PATTERN, PhoneE164 pattern (pydantic-core)
}
_REGEX_SITE = re.compile(r"re\.(?:compile|match|fullmatch|search|sub|subn|findall|finditer|split)\(|pattern\s*=")


def test_regex_inventory_is_complete():
    found = {}
    for p in SRC.rglob("*.py"):
        n = len(_REGEX_SITE.findall(p.read_text()))
        if n:
            found[str(p.relative_to(SRC))] = n
    # _ID_PATTERN is defined once and referenced by two `pattern=` sites.
    assert found == {**REGEX_INVENTORY, "fulfillment_schema/__init__.py": 3}, found


N = 100_000
ADVERSARIAL = [
    "1" * N, "+" + "1" * N, "+1" + "2" * N + "x", "+" * N, "1" * N + "\n", "+1" + "2" * (N - 3) + "\n",
    "0" * N, "00:00-" * (N // 6), "0" * N + ":", "1." * (N // 2), "-" + "1" * N + ".", "1" * N + "x",
    "a" * N + "!", "a:" * (N // 2), "a." * (N // 2) + " ", "é" * N, " " * N,
]


def _worst(fn) -> float:
    worst = 0.0
    for s in ADVERSARIAL:
        t = time.perf_counter()
        try:
            fn(s)
        except Exception:  # noqa: BLE001 — rejection is fine; only time matters
            pass
        worst = max(worst, time.perf_counter() - t)
    return worst


REGEX_CALLS = {
    "E164.match": lambda s: recipient_zones.E164.match(s),
    "E164.fullmatch": lambda s: recipient_zones.E164.fullmatch(s),
    "_NANP.fullmatch": lambda s: recipient_zones._NANP.fullmatch(s),
    "zones_to_check": lambda s: zones_to_check(s, "America/New_York", {"44": ("Europe/London",)}),
    "parse_country_zones": parse_country_zones,
    "parse_country_zones cc": lambda s: parse_country_zones(s + "=Europe/London"),
    "_WINDOW_RE": parse_contact_window,
    "money._FLOAT_TEXT": lambda s: money._FLOAT_TEXT.fullmatch(s),
    "money.WIRE_PATTERN (unguarded)": lambda s: money.WIRE_PATTERN.fullmatch(s),
    "money.to_money": money.to_money,
    "EntityId": TypeAdapter(EntityId).validate_python,
    "TaskId": TypeAdapter(TaskId).validate_python,
    "PhoneE164": TypeAdapter(PhoneE164).validate_python,
    "PhoneE164 json": lambda s: TypeAdapter(PhoneE164).validate_json(json.dumps(s)),
}


@pytest.mark.parametrize("name", list(REGEX_CALLS))
def test_regex_is_linear_on_100kb_adversarial_input(name):
    fn = REGEX_CALLS[name]
    _worst(fn)  # warm up
    assert _worst(fn) < 0.050, name


def test_phone_with_trailing_newline_is_not_e164():
    """re.match with `$` also matches before a trailing "\\n": the gate keyed
    '+12125550101\\n' as a different number from '+12125550101' (a second
    attempt budget for the same phone). pydantic-core's regex already
    rejected it at the API; the gate's own check must too."""
    assert zones_to_check("+12125550101\n", "America/New_York", {}).zones is None
    assert zones_to_check("+442071234567\n", "Europe/London", {"44": ("Europe/London",)}).zones is None
    with pytest.raises(Exception):
        TypeAdapter(PhoneE164).validate_python("+12125550101\n")


# === 4. outbound gate capacity ===============================================

T0 = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)  # daytime everywhere in continental US/CA
NY = "America/New_York"


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


def test_default_new_key_budget_cannot_fill_the_cap_within_24h():
    assert outbound_gate.DEFAULT_MAX_NEW_KEYS_PER_HOUR * 24 < outbound_gate.DEFAULT_MAX_TRACKED_KEYS


def test_new_key_admission_is_rate_limited_per_rolling_hour():
    clock = Clock(T0)
    g = _gate(clock, max_tracked_keys=1000, max_new_keys_per_hour=4)
    for i in range(4):
        assert _contact(g, f"+1212555{i:04d}") is None
    reason = _contact(g, "+12125559999")
    assert reason is not None and "new numbers/customers" in reason
    # an already-tracked number is still decided only by its own limits
    clock.t += timedelta(hours=2)
    assert _contact(g, "+12125550000") is None
    clock.t = T0 + timedelta(hours=1)
    assert _contact(g, "+12125559999") is None


def test_rate_limit_rechecked_at_redeem():
    g = _gate(Clock(T0), max_tracked_keys=1000, max_new_keys_per_hour=1)
    a = g.authorize(channel=TaskChannel.CALL, phone="+12125550001", claimed_tz=NY, customer_id=None)
    b = g.authorize(channel=TaskChannel.CALL, phone="+12125550002", claimed_tz=NY, customer_id=None)
    assert a.allowed and b.allowed
    a.authorization.redeem(TaskChannel.CALL)
    with pytest.raises(ContactRefused, match="new numbers/customers"):
        b.authorization.redeem(TaskChannel.CALL)


def test_burst_attacker_cannot_fill_the_cap_in_24h_and_known_numbers_keep_working(monkeypatch):
    """Scaled simulation: cap 240, budget 9/h (9*24 = 216 < 240). An attacker
    offering 100 fresh numbers (each with a fresh customer id) every 10
    minutes for 24h never fills the cap, and a number the service already
    contacts is never refused for capacity. Calling hours are held open here
    (they are not what this test is about; they only add refusals)."""
    monkeypatch.setattr(ContactWindow, "allows", lambda self, now, tz: True)
    clock = Clock(T0)
    g = _gate(clock, max_tracked_keys=240, max_new_keys_per_hour=9)
    assert _contact(g, "+13125550000", customer_id="real") is None  # a real customer, 2 keys
    n = 0
    peak = 0
    for step in range(6 * 24):
        clock.t = T0 + timedelta(minutes=10 * step)
        for _ in range(100):
            n += 1
            _contact(g, f"+1415{2_000_000 + n:07d}", customer_id=f"atk{n}")
        assert g.tracked_keys < 240
        peak = max(peak, g.tracked_keys)
        if step % 18 == 17:  # every 3h: the real customer is never refused for capacity
            reason = _contact(g, "+13125550000", customer_id="real")
            assert reason is None or "attempt limit" in reason or "spacing" in reason, reason
    assert not g.status()["at_capacity"]
    # 4 contacts x 2 keys fit in 9 per hour: the attacker used all of it (not vacuous)
    assert peak >= 8 * 24


def test_gate_status_reports_capacity_and_near_capacity_alert():
    g = _gate(Clock(T0), max_tracked_keys=10, max_new_keys_per_hour=100)
    for i in range(7):
        assert _contact(g, f"+1212555{i:04d}") is None
    s = g.status()
    assert s["tracked_keys"] == 7 and s["max_tracked_keys"] == 10
    assert s["near_capacity"] is False and s["at_capacity"] is False
    assert s["new_keys_last_hour"] == 7 and s["max_new_keys_per_hour"] == 100
    assert _contact(g, "+12125550100") is None
    s = g.status()
    assert s["utilization"] == 0.8 and s["near_capacity"] is True


@pytest.mark.parametrize("bad", [0, -1, True, 10**9])
def test_new_key_budget_must_be_sane(bad):
    with pytest.raises(ValueError):
        _gate(Clock(T0), max_new_keys_per_hour=bad)


def test_gate_status_route_requires_auth_and_reports_capacity():
    assert anon.get("/gate/status").status_code == 401
    r = client.get("/gate/status")
    assert r.status_code == 200
    body = r.json()
    for k in ("tracked_keys", "max_tracked_keys", "utilization", "near_capacity", "at_capacity",
              "new_keys_last_hour", "max_new_keys_per_hour"):
        assert k in body


def test_orchestrate_response_carries_gate_capacity(monkeypatch):
    monkeypatch.setattr(api, "_dialer", InMemorySipDialer())
    monkeypatch.setattr(api, "_now", lambda: T0)
    r = client.post("/agents/callback-orchestration/run", json={"tasks": [], "phone_by_call_id": {}})
    assert r.status_code == 200
    assert r.json()["gate"]["max_tracked_keys"] == api._GATE.max_tracked_keys


# === 5. customer dossier update ==============================================

def _calls(customers):
    return {"call_events": [
        {"call_id": f"{c}-call", "customer_id": c, "phone_number": "+12125550101", "direction": "inbound",
         "status": "missed", "started_at": "2026-09-22T12:00:00Z", "line_id": "l"} for c in customers]}


def test_dossier_update_returns_only_the_affected_dossiers():
    assert client.post("/agents/customer-dossier/update", json=_calls(["a", "b", "c"])).status_code == 200
    r = client.post("/agents/customer-dossier/update", json=_calls(["b"]))
    assert r.status_code == 200
    assert [d["customer_id"] for d in r.json()["dossiers"]] == ["b"]
    assert set(api._dossiers) == {"a", "b", "c"}


def test_dossier_update_does_not_copy_unaffected_dossiers():
    client.post("/agents/customer-dossier/update", json=_calls(["a", "b"]))
    before_a = api._dossiers["a"]
    client.post("/agents/customer-dossier/update", json=_calls(["b"]))
    assert api._dossiers["a"] is before_a


def test_dossier_update_cost_does_not_grow_with_the_store(monkeypatch):
    big = {f"x{i}": CustomerDossier(customer_id=f"x{i}", call_history=[f"c{j}" for j in range(50)])
           for i in range(20_000)}
    monkeypatch.setattr(api, "_dossiers", big)
    t = time.perf_counter()
    r = client.post("/agents/customer-dossier/update", json=_calls(["new"]))
    elapsed = time.perf_counter() - t
    assert elapsed < 0.25, elapsed
    assert r.status_code == 200 and len(r.json()["dossiers"]) == 1
    assert len(api._dossiers) == 20_001


# === new defect found while profiling: quadratic missed-call detection =======

def _brute_prior_misses(events, ev):
    from agents.missed_call_detection import _REPEAT_LOOKBACK
    from fulfillment_schema import CallDirection
    return sum(1 for s in events if s.phone_number == ev.phone_number and s.direction == CallDirection.INBOUND
               and s.is_unresolved and s.call_id != ev.call_id and s.started_at <= ev.started_at
               and (ev.started_at - s.started_at) <= _REPEAT_LOOKBACK)


def test_missed_call_detection_is_not_quadratic_in_calls_from_one_number():
    """1000 missed calls from ONE number (a max batch) cost 3.47 s of CPU
    before: every event rescanned every sibling."""
    from agents import missed_call_detection
    from fulfillment_schema import CallEvent

    base = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    events = [CallEvent(call_id=f"c{i}", phone_number="+12125550101", direction="inbound", status="missed",
                        started_at=base + timedelta(minutes=i), line_id="l") for i in range(1000)]
    missed_call_detection.detect(events[:10], now=base)
    t = time.perf_counter()
    tasks = missed_call_detection.detect(events, now=base)
    assert time.perf_counter() - t < 0.25
    assert len(tasks) == 1000


def test_missed_call_prior_miss_counts_match_the_definition():
    import random
    from agents import missed_call_detection
    from fulfillment_schema import CallEvent

    rng = random.Random(4)
    base = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    for _ in range(30):
        events = [CallEvent(
            call_id=f"c{i}", phone_number=rng.choice(["+12125550101", "+12125550102", "+13125550100"]),
            direction=rng.choice(["inbound", "inbound", "outbound"]),
            status=rng.choice(["missed", "voicemail", "answered", "busy", "failed"]),
            started_at=base + timedelta(hours=rng.choice([0, 0, 1, 23, 24, 25, 48]), minutes=rng.choice([0, 0, 30])),
            line_id="l") for i in range(40)]
        by_id = {e.call_id: e for e in events}
        for task in missed_call_detection.detect(events, now=base):
            ev = by_id[task.source_call_id]
            n = _brute_prior_misses(events, ev)
            window = timedelta(minutes=2) if n else timedelta(minutes=5)
            assert task.due_at == ev.started_at + window
            assert (f", {n} prior missed call(s)" in task.reason) == (n > 0)


# === new defect found while profiling: tz database re-read per zone check ====

def test_gate_decisions_for_a_max_batch_do_not_reread_the_tz_database():
    """zoneinfo keeps only 8 zones strongly cached; the gate checks 44 for a
    continental +1 number, so every check re-read tz files from disk: one
    1000-task orchestrate batch held the dial lock for ~4.2 s."""
    g = _gate(Clock(T0), max_tracked_keys=10_000, max_new_keys_per_hour=10_000)
    _contact(g, "+12125550000")  # warm
    t = time.perf_counter()
    for i in range(1, 1000):
        assert _contact(g, f"+1212{2_000_000 + i}", customer_id=f"c{i}") is None
    assert time.perf_counter() - t < 1.0


def test_resolve_timezone_cache_is_bounded_and_still_fails_closed():
    from contact_window import resolve_timezone

    for bad in ["", None, "Not/AZone", "../etc/passwd", "UTC\x00", "a" * 64]:
        assert resolve_timezone(bad) is None
    assert resolve_timezone("America/New_York") is resolve_timezone("America/New_York")
    info = getattr(resolve_timezone, "cache_info", None)
    assert info is not None and info().maxsize is not None and info().maxsize <= 4096
