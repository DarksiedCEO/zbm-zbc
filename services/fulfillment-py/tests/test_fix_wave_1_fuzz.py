"""
Fix wave 1 — no input yields a 5xx. Every POST route gets malformed JSON,
wrong types, huge strings/numbers, NaN/Infinity, unicode edge cases (lone
surrogates, NUL, BOM, invalid UTF-8), deep nesting, and extreme datetimes;
every GET route gets junk query strings. Each must answer 4xx (or 2xx when
the input is in fact valid), never 500.

Found by this sweep (fix wave 1): a call event whose `started_at` sat at
the edge of the datetime range ("9999-12-31T23:59:59+00:00") made
missed-call detection compute `started_at + 5 min`, which raised
OverflowError -> HTTP 500. Root cause: every datetime field accepted any
aware datetime Python can represent. Fixed at the schema: all datetimes are
bounded to [2000-01-01, 2100-01-01) UTC (fulfillment_schema.BoundedAwareDatetime),
so no agent's timedelta arithmetic can leave the representable range.

The same payloads are replayed against the real process over TCP in
test_live_fuzz_over_real_http (tests/test_live_server.py harness).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api import app
from conftest import TEST_SERVICE_TOKEN

client = TestClient(
    app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}, raise_server_exceptions=False
)

POST_ROUTES = [
    "/agents/missed-call-detection/detect",
    "/agents/appointment-tracking/detect",
    "/agents/followup-sequencing/escalate",
    "/agents/callback-orchestration/run",
    "/agents/customer-dossier/update",
    "/agents/resolution-writeback/resolve",
]
GET_ROUTES = ["/health", "/fixtures/call-events", "/fixtures/appointments", "/fixtures/dossiers"]

_CALL = {
    "call_id": "a", "customer_id": "c", "phone_number": "+15551234567", "direction": "inbound",
    "status": "missed", "started_at": "2026-01-01T00:00:00Z", "line_id": "l",
}
_APPT = {
    "appointment_id": "a", "customer_id": "c", "scheduled_at": "2026-01-01T00:00:00Z",
    "service_type": "x", "status": "scheduled",
}
_TASK = {
    "task_id": "t1", "purpose": "missed_call_callback", "channel": "call", "customer_id": "c",
    "source_call_id": "k", "due_at": "2026-01-01T00:00:00Z", "reason": "r",
}


def _j(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8", "surrogatepass")


def _call(**kw):
    return {"call_events": [{**_CALL, **kw}]}


def _appt(**kw):
    return {"appointments": [{**_APPT, **kw}]}


def _task(**kw):
    return {**_TASK, **kw}


EXTREME_DATES = [
    "9999-12-31T23:59:59+00:00",
    "9999-12-31T23:59:59-14:00",
    "0001-01-01T00:00:00+14:00",
    "0001-01-01T00:00:00+00:00",
    "1999-12-31T23:59:59Z",
    "2100-01-01T00:00:00Z",
]

GENERIC_BODIES: list[bytes] = [
    b"", b"{", b"}", b"null", b"[]", b"1", b'"x"', b"true", b"NaN", b"Infinity", b"-Infinity",
    b'{"a":NaN}', b"\xff\xfe\x00", b"\xef\xbb\xbf{}", b'{"\\ud800":1}', b'{"a":"\\udfff"}',
    b"[" * 5000 + b"]" * 5000,
    b'{"x":' * 3000 + b"1" + b"}" * 3000,
    b'{"call_events":' + b"[" * 3000 + b"]" * 3000 + b"}",
    b'{"a":' + b"9" * 20000 + b"}",
    b'{"a":1e999999}',
    _j({"a": "x" * 2_000_000}),
    _j({"\u0000": "\u0000"}),
    b'{"call_events":[],"call_events":[]}',
]

ROUTE_BODIES: dict[str, list[bytes]] = {
    "/agents/missed-call-detection/detect": [
        *[_j(_call(started_at=d)) for d in EXTREME_DATES],
        *[_j(_call(ended_at=d)) for d in EXTREME_DATES],
        _j(_call(call_id="a" * 1_000_000)),
        _j(_call(call_id="\u0000")),
        _j(_call(call_id="\ud800")),
        _j(_call(phone_number="+1" + "5" * 5000)),
        _j(_call(phone_number="+１５５５１２３４５６７")),
        _j(_call(duration_seconds="NaN")),
        _j(_call(duration_seconds=1e308)),
        b'{"call_events":[' + _j({**_CALL})[:-1] + b',"duration_seconds":' + b"9" * 5000 + b"}]}",
        b'{"call_events":[' + _j({**_CALL})[:-1] + b',"duration_seconds":NaN}]}',
        b'{"call_events":[' + _j({**_CALL})[:-1] + b',"duration_seconds":Infinity}]}',
        _j(_call(voicemail_transcript="\ud83d" * 100)),
        _j({"call_events": [_CALL] * 1001}),
        _j({"call_events": {"0": _CALL}}),
        _j({"call_events": [[_CALL]]}),
    ],
    "/agents/appointment-tracking/detect": [
        *[_j(_appt(scheduled_at=d)) for d in EXTREME_DATES],
        *[_j(_appt(completion_confirmed_at=d)) for d in EXTREME_DATES],
        _j(_appt(scheduled_at="2026-01-01T00:00:00")),
        _j(_appt(scheduled_at=1e308)),
        _j(_appt(scheduled_at=-1e18)),
        _j(_appt(scheduled_at=10**30)),
        _j(_appt(status="SCHEDULED")),
        _j(_appt(service_type="é" * 129)),
    ],
    "/agents/followup-sequencing/escalate": [
        *[_j({"task": _task(status="failed", due_at=d)}) for d in EXTREME_DATES],
        *[_j({"task": _task(status="failed", created_at=d)}) for d in EXTREME_DATES],
        _j({"task": _task(status="failed", attempt_number=100)}),
        _j({"task": _task(status="failed", attempt_number=2**70)}),
        _j({"task": _task(status="failed", attempt_number="NaN")}),
        _j({"task": _task(status="failed", channel="human_handoff", attempt_number=4)}),
        _j({"task": _task(status="failed", task_id="a" * 256)}),
        _j({"task": _task(status="pending")}),
        _j({"task": [_TASK]}),
    ],
    "/agents/callback-orchestration/run": [
        *[
            _j({"tasks": [_task(due_at=d)], "phone_by_call_id": {"k": "+15551234567"},
                "timezone_by_call_id": {"k": "America/New_York"}})
            for d in EXTREME_DATES
        ],
        *[
            _j({"tasks": [_task(created_at=d)], "phone_by_call_id": {"k": "+15551234567"},
                "timezone_by_call_id": {"k": "America/New_York"}})
            for d in EXTREME_DATES
        ],
        *[
            _j({"tasks": [_task()], "phone_by_call_id": {"k": "+15551234567"}, "timezone_by_call_id": {"k": tz}})
            for tz in ["Not/AZone", "../../etc/passwd", "/etc/localtime", ".", "America/", "\u0000",
                       "UTC\u0000x", "\ud800", "é", "", "a" * 65]
        ],
        _j({"tasks": [_task()], "phone_by_call_id": {"k": "not-a-number"}}),
        _j({"tasks": [_task()], "phone_by_call_id": {"k": ["+15551234567"]}}),
        _j({"tasks": [_task()], "phone_by_call_id": {"\ud800": "+15551234567"}}),
        _j({"tasks": [_task(source_call_id=None)], "phone_by_call_id": {}}),
        _j({"tasks": [_task(channel="sms")], "phone_by_call_id": {"k": "+15551234567"}}),
        _j({"tasks": [_task()], "phone_by_call_id": {"k": "+15551234567"}, "now": "2026-01-01T00:00:00Z"}),
    ],
    "/agents/customer-dossier/update": [
        *[_j({**_call(started_at=d), **_appt(scheduled_at=d)}) for d in EXTREME_DATES],
        _j({"call_events": [_CALL], "appointments": [_APPT] * 1001}),
        _j({"call_events": None}),
    ],
    "/agents/resolution-writeback/resolve": [
        _j({"events": [{"entity_type": "task", "entity_id": "x", "resolution_type": "booked"}]}),
        _j({"events": [{"entity_type": "order", "entity_id": "x", "resolution_type": "booked"}]}),
        _j({"events": [{"entity_type": "task", "entity_id": "x" * 257, "resolution_type": "booked"}]}),
        _j({"events": [{"entity_type": "task", "entity_id": "\ud800", "resolution_type": "booked"}]}),
        _j({"events": [{"entity_type": "task", "entity_id": "x", "resolution_type": "booked"}] * 1001}),
    ],
}

CASES = [(r, b) for r in POST_ROUTES for b in GENERIC_BODIES + ROUTE_BODIES[r]]
CASE_IDS = [f"{r.split('/')[2]}:{i}" for i, (r, _) in enumerate(CASES)]


@pytest.mark.parametrize("route,body", CASES, ids=CASE_IDS)
def test_post_route_never_answers_5xx(route, body):
    r = client.post(route, content=body, headers={"Content-Type": "application/json"})
    assert r.status_code < 500, (r.status_code, body[:120], r.text[:300])


@pytest.mark.parametrize("route", GET_ROUTES)
@pytest.mark.parametrize("qs", ["", "?a=%00", "?a=%ff%fe", "?" + "a" * 8000, "?a=%ud800"])
def test_get_route_never_answers_5xx(route, qs):
    r = client.get(route + qs)
    assert r.status_code < 500, (r.status_code, r.text[:200])


@pytest.mark.parametrize("d", EXTREME_DATES)
def test_out_of_range_datetimes_are_422_with_the_field_named(d):
    r = client.post("/agents/missed-call-detection/detect", json=_call(started_at=d))
    assert r.status_code == 422, r.text[:300]
    assert "started_at" in r.text


def test_in_range_edges_are_accepted():
    for d in ["2000-01-01T00:00:00Z", "2099-12-31T23:59:59Z", "2099-12-31T23:59:59+14:00"]:
        r = client.post("/agents/missed-call-detection/detect", json=_call(started_at=d))
        assert r.status_code == 200, (d, r.text[:300])
