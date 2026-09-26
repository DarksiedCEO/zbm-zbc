"""
Fix wave 6, N2 (MED, CONFIRMED): 422 amplification.

The finding: the validation-error handler stripped each error's `input` (A8)
but still reported EVERY error in full. A 60 000-unknown-key body (~600 KB)
against an `extra="forbid"` request model produced 60 000 `extra_forbidden`
errors, each with its own `loc` — a 5.4 MB 422 body, built and serialized on
the event loop. 20 concurrent (authenticated) senders: RSS 52 -> 547 MB,
`/health` 0.9 s. A single 1 MiB unknown key was echoed whole in its `loc`.

What must hold (in-process here, and against the real process below):
  - a 422 body is < 8 KiB whatever the request was: at most 20 errors are
    listed plus the total `error_count`; each `loc` is bounded in depth and
    per-item length; `msg`/`type` are bounded;
  - a body with more unknown top-level keys than can be named is refused
    with ONE error, not one per key (pydantic never enumerates them);
  - 1 MiB junk / 60k-unknown-key bodies are answered in < 100 ms;
  - the small, documented shape is unchanged: `loc`/`type`/`msg` per error,
    up to 20 unknown keys still named (a stale client sending `now` is told
    exactly that, README A2);
  - 20 concurrent 60k-key bodies keep the real process's RSS growth
    < 100 MB and `/health` < 500 ms throughout.

Ports: FULFILLMENT_TEST_PORT_RANGE when set (this wave: 20320-20339).
"""

from __future__ import annotations

import http.client
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from _procinfo import rss_mib
from conftest import TEST_SERVICE_TOKEN
from test_live_server import TOKEN, _start, _stop

import api

MIB = 1024 * 1024
BODY_BUDGET = 8 * 1024
TIME_BUDGET_S = 0.100
DETECT = "/agents/missed-call-detection/detect"
HEADERS = {"Authorization": f"Bearer {TEST_SERVICE_TOKEN}", "Content-Type": "application/json"}

client = TestClient(api.app)


def _sixty_k_keys() -> bytes:
    return json.dumps({f"k{i}": "x" for i in range(60_000)}).encode()


def _post(body: bytes, route: str = DETECT):
    """The response, and the best-of-3 wall time: the CPU cost of answering
    this body, not whatever else the box was doing during one attempt."""
    client.post(route, headers=HEADERS, content=b'{"call_events":[]}')  # warm the path
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        r = client.post(route, headers=HEADERS, content=body)
        best = min(best, time.perf_counter() - t0)
    return r, best


# --- bounded body, bounded time ------------------------------------------------

def test_60k_unknown_keys_is_one_small_422_not_60k_errors():
    r, took = _post(_sixty_k_keys())
    assert r.status_code == 422
    assert len(r.content) < BODY_BUDGET, f"422 body is {len(r.content)} bytes"
    assert took < TIME_BUDGET_S, f"took {took * 1000:.0f} ms"
    body = r.json()
    # not enumerated: one error names the problem, the count is honest.
    # Fix wave 7 (NEW-4): 60 000 keys is over the JSON shape cap (32 000
    # members), so the byte-level pre-scan refuses it before the full parse
    # — `json_too_many_members`, not pydantic's `too_many_fields` (which
    # still answers an object of 21..32 000 keys: see the 21-key case below).
    assert len(body["detail"]) == 1, body["detail"][:3]
    assert body["detail"][0]["type"] == "json_too_many_members"
    assert body["error_count"] == 1
    assert "k59999" not in r.text


JUNK = {
    "1MiB of x": b"x" * MIB,
    "1MiB nested arrays": b"[" * MIB,
    "1MiB single unknown key": json.dumps({"a" * MIB: 1}).encode(),
    "1MiB string in a bounded field": json.dumps({"call_events": [{"call_id": "x" * MIB}]}).encode(),
    "1MiB of unknown keys": json.dumps({f"k{i}": "x" for i in range(85_000)}, separators=(",", ":")).encode(),
    "truncated after 1MiB": b'{"call_events":[' + b'{"call_id":"a"},' * 65_536 + b"xx",
    "1000 empty events": json.dumps({"call_events": [{}] * 1000}).encode(),
    "1000 events with 50 unknown keys each": json.dumps({"call_events": [{f"u{j}": 1 for j in range(50)}] * 1000}).encode(),
    "deep loc": json.dumps({"call_events": [{"extras": [[[[[[[[[[[[1]]]]]]]]]]]]}]}).encode(),
}


@pytest.mark.parametrize("name", list(JUNK))
def test_junk_bodies_get_a_small_fast_422(name):
    body = JUNK[name]
    r, took = _post(body)
    assert r.status_code == 422, r.text[:200]
    assert len(r.content) < BODY_BUDGET, f"{name}: 422 body is {len(r.content)} bytes"
    assert took < TIME_BUDGET_S, f"{name}: took {took * 1000:.0f} ms"
    assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in r.text


@pytest.mark.parametrize("route,body", [
    ("/agents/appointment-tracking/detect", {"appointments": [{}] * 1000}),
    ("/agents/customer-dossier/update", {"call_events": [{}] * 1000, "appointments": [{}] * 1000}),
    ("/agents/callback-orchestration/run", {"tasks": [{}] * 1000, "phone_by_call_id": {f"c{i}": "nope" for i in range(1000)}}),
    ("/agents/resolution-writeback/resolve", {"events": [{}] * 1000}),
    ("/agents/followup-sequencing/escalate", {"task": {f"k{i}": 1 for i in range(1000)}}),
])
def test_every_post_route_bounds_its_422(route, body):
    r, took = _post(json.dumps(body).encode(), route)
    assert r.status_code == 422, r.text[:200]
    assert len(r.content) < BODY_BUDGET, f"{route}: 422 body is {len(r.content)} bytes"
    assert took < TIME_BUDGET_S, f"{route}: took {took * 1000:.0f} ms"
    assert len(r.json()["detail"]) <= api._MAX_REPORTED_ERRORS


# --- the shape --------------------------------------------------------------------

def test_many_errors_report_the_first_20_and_the_total():
    r, _ = _post(json.dumps({"call_events": [{}] * 1000}).encode())
    body = r.json()
    assert len(body["detail"]) == 20
    per_event = len([e for e in body["detail"] if e["loc"][:3] == ["body", "call_events", 0]])
    assert per_event >= 1
    assert body["error_count"] == 1000 * per_event
    assert body["truncated"] is True
    assert body["detail"][0]["loc"] == ["body", "call_events", 0, "call_id"]
    assert body["detail"][0]["type"] == "missing"
    assert body["detail"][0]["msg"] == "Field required"


def test_one_error_keeps_the_documented_shape():
    r, _ = _post(json.dumps({"call_events": [{
        "call_id": "c1", "phone_number": "+15550199", "status": "missed",
        "started_at": "2026-09-22T12:00:00Z", "line_id": "l"}]}).encode())
    assert r.status_code == 422
    assert r.json() == {
        "detail": [{"loc": ["body", "call_events", 0, "direction"], "type": "missing", "msg": "Field required"}],
        "error_count": 1,
    }


def test_up_to_20_unknown_keys_are_still_named_beyond_that_one_error():
    named = {"call_events": [], **{f"stale{i}": 1 for i in range(20)}}
    r, _ = _post(json.dumps(named).encode())
    body = r.json()
    assert body["error_count"] == 20
    assert {e["loc"][1] for e in body["detail"]} == {f"stale{i}" for i in range(20)}
    assert all(e["type"] == "extra_forbidden" for e in body["detail"])

    too_many = {"call_events": [], **{f"stale{i}": 1 for i in range(21)}}
    r, _ = _post(json.dumps(too_many).encode())
    body = r.json()
    assert body["error_count"] == 1
    assert body["detail"][0]["type"] == "too_many_fields"
    assert body["detail"][0]["loc"] == ["body"]
    assert "stale20" not in r.text


def test_stale_client_sending_now_is_still_told_which_key_is_unknown():
    r, _ = _post(json.dumps({"tasks": [], "phone_by_call_id": {}, "now": "2026-01-01T00:00:00Z"}).encode(),
                 "/agents/callback-orchestration/run")
    assert r.status_code == 422
    assert r.json()["detail"] == [{"loc": ["body", "now"], "type": "extra_forbidden", "msg": "Extra inputs are not permitted"}]


def test_loc_items_and_depth_are_clipped():
    r, _ = _post(json.dumps({"call_events": [], "z" * 500: 1}).encode())
    [err] = r.json()["detail"]
    assert err["loc"][0] == "body"
    assert len(err["loc"][1]) <= api._MAX_LOC_ITEM_CHARS
    assert "z" * (api._MAX_LOC_ITEM_CHARS + 1) not in r.text
    assert all(len(str(item)) <= api._MAX_LOC_ITEM_CHARS for e in r.json()["detail"] for item in e["loc"])
    assert all(len(e["loc"]) <= api._MAX_LOC_DEPTH + 1 for e in r.json()["detail"])


def test_bounded_body_holds_for_adversarial_loc_and_msg_sizes():
    """Direct check of the renderer with the worst error shapes pydantic could
    hand it: deep locs of long multi-byte keys, long messages, many errors."""
    errors = [{"loc": ("body", *["€" * 300] * 30, i), "type": "t" * 300, "msg": "m" * 5000} for i in range(500)]
    body = api._bounded_validation_body(errors, 500)
    assert len(body) < BODY_BUDGET, len(body)
    content = json.loads(body)
    assert content["error_count"] == 500
    assert 1 <= len(content["detail"]) <= api._MAX_REPORTED_ERRORS
    assert content["truncated"] is True


def test_fastapi_level_validation_errors_use_the_same_bound():
    """The RequestValidationError handler (anything FastAPI itself raises)
    renders through the same bounded builder."""
    from fastapi.exceptions import RequestValidationError

    exc = RequestValidationError([{"loc": ("query", f"q{i}"), "type": "missing", "msg": "Field required"} for i in range(5000)])
    resp = api._validation_error_without_input(None, exc)
    assert resp.status_code == 422
    assert len(resp.body) < BODY_BUDGET
    assert json.loads(resp.body)["error_count"] == 5000


# --- the real process: 20 concurrent senders ---------------------------------------

@pytest.fixture(scope="module")
def server():
    proc, host, port = _start({})
    yield proc, host, port
    _stop(proc)


# Fix wave 16: portable (Linux /proc, macOS/BSD ps); measures only the server pid.
_rss_mib = rss_mib


def test_20_concurrent_60k_key_bodies_keep_rss_and_health_bounded(server):
    proc, host, port = server
    body = _sixty_k_keys()
    codes: list[int] = []
    sizes: list[int] = []
    errors: list[str] = []
    lock = threading.Lock()
    stop = threading.Event()
    health: list[float] = []
    peak = [_rss_mib(proc.pid)]
    before = peak[0]

    def sender():
        for _ in range(5):
            try:
                conn = http.client.HTTPConnection(host, port, timeout=120)
                conn.request("POST", DETECT, body=body,
                             headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
                r = conn.getresponse()
                data = r.read()
                conn.close()
                with lock:
                    codes.append(r.status)
                    sizes.append(len(data))
            except OSError as exc:
                with lock:
                    errors.append(type(exc).__name__)

    def prober():
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                conn = http.client.HTTPConnection(host, port, timeout=10)
                conn.request("GET", "/health")
                conn.getresponse().read()
                conn.close()
            except OSError as exc:
                with lock:
                    errors.append("health:" + type(exc).__name__)
            health.append(time.monotonic() - t0)
            peak[0] = max(peak[0], _rss_mib(proc.pid))
            stop.wait(0.05)

    senders = [threading.Thread(target=sender) for _ in range(20)]
    probe = threading.Thread(target=prober)
    probe.start()
    for t in senders:
        t.start()
    for t in senders:
        t.join()
    stop.set()
    probe.join()

    assert errors == []
    assert codes and set(codes) == {422}, set(codes)
    assert max(sizes) < BODY_BUDGET, f"largest 422 body {max(sizes)} bytes"
    assert peak[0] - before < 100, f"RSS grew {before} -> {peak[0]} MiB"
    assert health and max(health) < 0.5, f"/health max {max(health) * 1000:.0f} ms over {len(health)} probes"
