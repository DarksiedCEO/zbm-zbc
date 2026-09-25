"""
Fix wave 7, NEW-4 (MED, CONFIRMED): the single parse slot was FIFO, so
authenticated junk bodies starved legitimate requests linearly.

The finding (AEGIS `ful_slot6.py`): 8 senders looping 3.9 MiB bodies of
255 000 unknown keys took a legitimate small `detect` from 4 ms to a p50 of
1.08 s; 32 senders to 4.4 s; ~18 s at the concurrency limit. Every body,
whatever its size, queued for the one parse slot behind every junk body in
front of it, and each junk body cost ~140 ms of parse before pydantic could
refuse it.

What must hold (in-process here, and against the real `python3 -m api`):
  - structurally absurd JSON (more than _MAX_JSON_MEMBERS members, more than
    _MAX_JSON_CONTAINERS objects/arrays, deeper than _MAX_JSON_DEPTH) is
    refused by a cheap byte-level pre-scan BEFORE the full parse: a bounded
    422 in a few ms, never ~140 ms;
  - every route's worst-case legitimate batch, and bodies whose strings are
    full of quotes, backslashes, commas and brackets, still pass the pre-scan;
  - bodies of at most _SMALL_BODY_BYTES parse in their own lane and never
    wait behind a large parse; large bodies share one slot with a short
    wait and then 503 + Retry-After;
  - live, with 8 and with 32 junk senders: legit small `detect` p50 < 50 ms
    and p99 < 250 ms, `/health` unaffected, a legit maximum orchestrate batch
    still succeeds, RSS bounded during and after the flood.

Ports: FULFILLMENT_TEST_PORT_RANGE when set (this wave: 20520-20539).
"""

from __future__ import annotations

import http.client
import json
import statistics
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_SERVICE_TOKEN
from test_fix4_limits import MAX_BATCHES
from test_fix5_http_limits_live import _start as _start_quiet, _stop  # DEVNULL: a PIPE fills and blocks the server
from test_live_server import TOKEN

import api

MIB = 1024 * 1024
DETECT = "/agents/missed-call-detection/detect"
ORCHESTRATE = "/agents/callback-orchestration/run"
HEADERS = {"Authorization": f"Bearer {TEST_SERVICE_TOKEN}", "Content-Type": "application/json"}
PRESCAN_TIME_BUDGET_S = 0.060  # the full parse of these bodies cost 140-230 ms

client = TestClient(api.app)


def _post(body: bytes, route: str = DETECT, n: int = 3):
    client.post(route, headers=HEADERS, content=b'{"call_events":[]}')  # warm the path
    best = float("inf")
    for _ in range(n):
        t0 = time.perf_counter()
        r = client.post(route, headers=HEADERS, content=body)
        best = min(best, time.perf_counter() - t0)
    return r, best


def _junk_keys(n: int = 255_000) -> bytes:
    return json.dumps({f"k{i}": 1 for i in range(n)}).encode()  # the AEGIS "keys" body


ABSURD = {
    "255k unknown keys (AEGIS keys)": _junk_keys(),
    "1M array items (AEGIS array)": json.dumps({"call_events": [1] * 1_000_000}).encode(),
    "250k objects (AEGIS objs)": json.dumps({"call_events": [{"x": i} for i in range(250_000)]}).encode(),
    "100k-deep arrays (AEGIS deep)": ("[" * 100_000 + "]" * 100_000).encode(),
    "60k unknown keys": json.dumps({f"k{i}": "x" for i in range(60_000)}).encode(),
    "40k small nested arrays": json.dumps({"call_events": [[[[[[[[[[1]]]]]]]]]] * 40_000}).encode(),
    "900k empty strings": b'["",' * 900_000 + b'""]',
    "4 MiB of quotes": b'"' * (4 * MIB),
    "33 levels deep": b"[" * 33 + b"]" * 33,
    "1 MiB nested arrays": b"[" * MIB,
}


# --- the pre-scan --------------------------------------------------------------

@pytest.mark.parametrize("name", list(ABSURD))
def test_absurd_json_is_refused_by_the_prescan_fast(name):
    body = ABSURD[name]
    r, took = _post(body)
    assert r.status_code == 422, r.text[:200]
    assert took < PRESCAN_TIME_BUDGET_S, f"{name}: took {took * 1000:.0f} ms"
    content = r.json()
    assert len(r.content) < 8 * 1024
    assert content["error_count"] == 1
    [err] = content["detail"]
    assert err["loc"] == ["body"]
    assert err["type"] in {"json_too_many_members", "json_too_many_containers", "json_too_deep"}, err
    assert "k254999" not in r.text


def _legit_events(n: int, transcript: str) -> bytes:
    return json.dumps({"call_events": [{
        "call_id": f"c{i}", "phone_number": "+15550100", "direction": "inbound", "status": "voicemail",
        "started_at": "2026-09-22T12:00:00Z", "line_id": "l1", "voicemail_transcript": transcript,
    } for i in range(n)]}).encode()


STRING_HEAVY = {
    "escaped quotes": _legit_events(200, '\\"' * 4_000),
    "backslashes": _legit_events(200, "\\" * 8_000),
    "mixed escapes": _legit_events(200, '\\"\\\\\\n\\t\\/\\u00e9' * 600),
    "commas and colons": _legit_events(200, ",:" * 4_000),
    "brackets and braces": _legit_events(200, "{[]}" * 2_000),
    "trailing odd backslashes before the quote": _legit_events(200, "a\\\\\\\\" * 2_000),
    "newlines and tabs": _legit_events(200, "\n\t\r\b\f" * 1_600),
}


@pytest.mark.parametrize("name", list(STRING_HEAVY))
def test_bodies_whose_strings_look_structural_still_pass_the_prescan(name):
    body = STRING_HEAVY[name]
    assert len(body) <= api._MAX_BODY_BYTES
    r, _ = _post(body, n=1)
    assert r.status_code == 200, r.text[:300]


@pytest.mark.parametrize("route", list(MAX_BATCHES))
def test_every_worst_case_max_batch_passes_the_prescan_with_headroom(route):
    body = json.dumps(MAX_BATCHES[route]).encode()
    shape = api._json_shape(bytearray(body))
    assert shape.violation is None, shape
    # The caps are sized from the batch contract, not from today's bodies:
    # the largest legitimate body uses well under half of each.
    assert shape.members * 1.5 <= api._MAX_JSON_MEMBERS, shape
    assert shape.containers * 1.5 <= api._MAX_JSON_CONTAINERS, shape
    assert shape.depth * 2 <= api._MAX_JSON_DEPTH, shape
    r = client.post(route, headers=HEADERS, content=body)
    assert r.status_code == 200, (route, r.status_code, r.text[:300])


def test_prescan_counts_exactly_on_a_hand_built_body():
    body = bytearray(b'{"a":[1,2,{"b":"x,y:{[\\"]"}],"c":{"d":null},"e":"\\\\"}')
    shape = api._json_shape(body)
    assert shape.violation is None
    assert shape.containers == 4  # root, [..], {"b"..}, {"d"..}
    assert shape.depth == 3
    # members counted as (commas outside strings + non-empty containers): an
    # upper bound on keys + items that is exact when no container is empty.
    assert shape.members == 4 + 4


def test_prescan_cost_is_bounded_on_every_4mib_shape():
    """Pure C passes over the bytes plus O(cap) Python work: no 4 MiB body
    costs more than a few tens of ms, whatever it is made of."""
    cases = [
        b"x" * (4 * MIB), b'"' + b"a" * (4 * MIB - 2) + b'"', b'"' + b'\\"' * (2 * MIB - 2) + b'"',
        b'"' * (4 * MIB), b"\\" * (4 * MIB), b'\\"' * (2 * MIB), b'"' + b"," * (4 * MIB - 2) + b'"',
        b'["",' * MIB + b'""]', b"[" * (4 * MIB), _junk_keys(),
    ]
    for body in cases:
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            api._json_shape(bytearray(body))
            best = min(best, time.perf_counter() - t0)
        assert best < 0.100, f"pre-scan took {best * 1000:.0f} ms on {body[:12]!r}..."


# --- the lanes -----------------------------------------------------------------

def _padded_small_and_large() -> tuple[bytes, bytes]:
    core = b'{"call_events":[]}'
    small = core + b" " * (1024 - len(core))
    large = core + b" " * (api._SMALL_BODY_BYTES + 1 - len(core))
    return small, large


def test_small_bodies_never_wait_behind_a_held_large_lane(monkeypatch):
    """Three large bodies whose parse takes 400 ms each: a small body posted
    meanwhile must come back in well under one of those parses. Before: one
    FIFO slot, so the small body waited for all three (~1.2 s).

    One shared portal (`with TestClient(...)`): outside the context manager
    every request gets its own event loop, hence its own lanes, and nothing
    here would be observed."""
    small, large = _padded_small_and_large()
    real_parse = api._parse

    def slow_large_parse(model, body):
        if len(body) > api._SMALL_BODY_BYTES:
            time.sleep(0.4)
        return real_parse(model, body)

    monkeypatch.setattr(api, "_parse", slow_large_parse)
    results: list[int] = []
    with TestClient(api.app) as shared:
        threads = [threading.Thread(target=lambda: results.append(
            shared.post(DETECT, headers=HEADERS, content=large).status_code)) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.1)  # the first large parse is in progress, two are queued
        t0 = time.perf_counter()
        r = shared.post(DETECT, headers=HEADERS, content=small)
        took = time.perf_counter() - t0
        for t in threads:
            t.join()
    assert r.status_code == 200, r.text[:200]
    assert took < 0.2, f"small body waited {took * 1000:.0f} ms behind large parses"
    assert results == [200, 200, 200], results


def test_large_lane_overload_is_503_with_retry_after_not_an_unbounded_queue(monkeypatch):
    small, large = _padded_small_and_large()
    real_parse = api._parse

    def slow_large_parse(model, body):
        if len(body) > api._SMALL_BODY_BYTES:
            time.sleep(0.6)
        return real_parse(model, body)

    monkeypatch.setattr(api, "_parse", slow_large_parse)
    monkeypatch.setattr(api, "_LARGE_WAIT_S", 0.2)
    responses = []
    lock = threading.Lock()
    with TestClient(api.app) as shared:
        def post():
            r = shared.post(DETECT, headers=HEADERS, content=large)
            with lock:
                responses.append(r)

        threads = [threading.Thread(target=post) for _ in range(3)]
        for t in threads:
            t.start()
            time.sleep(0.05)
        for t in threads:
            t.join()
        codes = sorted(r.status_code for r in responses)
        assert codes == [200, 503, 503], codes
        for r in responses:
            if r.status_code == 503:
                assert r.headers.get("Retry-After") == "1"
                assert "large" in r.json()["detail"]
        # small bodies were never refused
        assert shared.post(DETECT, headers=HEADERS, content=small).status_code == 200


def test_large_bodies_beyond_the_byte_budget_are_refused_before_being_read(monkeypatch):
    """The budget: a large body takes its Content-Length from the bucket
    before it is read. Once the burst is spent and the refill would take
    longer than the wait, the refusal is immediate (Retry-After), and a small
    body is unaffected. After the bucket refills a large body is admitted."""
    small, large = _padded_small_and_large()
    monkeypatch.setattr(api, "_LARGE_BURST_BYTES", len(large))  # exactly one large body of burst
    monkeypatch.setattr(api, "_LARGE_BYTES_PER_S", len(large) * 2)  # refills one in 0.5 s
    monkeypatch.setattr(api, "_LARGE_WAIT_S", 0.2)
    with TestClient(api.app) as shared:  # one loop, one bucket
        assert shared.post(DETECT, headers=HEADERS, content=large).status_code == 200
        t0 = time.perf_counter()
        r = shared.post(DETECT, headers=HEADERS, content=large)
        took = time.perf_counter() - t0
        assert r.status_code == 503, r.text[:200]
        assert took < 0.35, f"the refusal took {took * 1000:.0f} ms"
        assert r.headers.get("Retry-After") == "1"
        assert "budget" in r.json()["detail"]
        assert shared.post(DETECT, headers=HEADERS, content=small).status_code == 200
        time.sleep(0.6)
        assert shared.post(DETECT, headers=HEADERS, content=large).status_code == 200


def test_large_bodies_are_admitted_in_arrival_order(monkeypatch):
    small, large = _padded_small_and_large()
    monkeypatch.setattr(api, "_LARGE_BURST_BYTES", len(large))
    monkeypatch.setattr(api, "_LARGE_BYTES_PER_S", len(large) * 8)  # one every 125 ms after the burst
    monkeypatch.setattr(api, "_LARGE_WAIT_S", 5.0)
    done: list[int] = []
    lock = threading.Lock()
    with TestClient(api.app) as shared:
        def post(i: int):
            r = shared.post(DETECT, headers=HEADERS, content=large)
            with lock:
                done.append((i, r.status_code))

        threads = [threading.Thread(target=post, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
            time.sleep(0.04)
        t0 = time.perf_counter()
        r = shared.post(DETECT, headers=HEADERS, content=small)  # meanwhile: not behind them
        small_took = time.perf_counter() - t0
        for t in threads:
            t.join()
    assert r.status_code == 200 and small_took < 0.1, small_took
    assert done == [(0, 200), (1, 200), (2, 200), (3, 200)], done


def test_chunked_body_pays_the_budget_as_it_streams(monkeypatch):
    small, large = _padded_small_and_large()
    monkeypatch.setattr(api, "_LARGE_BURST_BYTES", 1)
    monkeypatch.setattr(api, "_LARGE_BYTES_PER_S", 1)
    monkeypatch.setattr(api, "_LARGE_WAIT_S", 0.2)

    def chunks():
        for i in range(0, len(large), 16 * 1024):
            yield large[i:i + 16 * 1024]

    r = client.post(DETECT, headers=HEADERS, content=chunks())  # no Content-Length
    assert r.status_code == 503, r.text[:200]
    assert r.headers.get("Retry-After") == "1"
    r = client.post(DETECT, headers=HEADERS, content=iter([small]))  # chunked but small: no budget
    assert r.status_code == 200, r.text[:200]


@pytest.mark.parametrize("declared", [5, 18, 100, None])
def test_understated_overstated_or_missing_content_length_still_parses_the_bytes_sent(declared):
    """The body buffer is pre-sized from Content-Length. Drive _off_loop with
    a hand-built ASGI request whose header lies (h11 would not let a real
    client, but the buffer logic must not depend on that): the parse sees
    exactly the bytes sent — no zero padding, nothing dropped."""
    import asyncio
    from starlette.requests import Request

    body = b'{"call_events":[]}'
    headers = [(b"content-type", b"application/json")]
    if declared is not None:
        headers.append((b"content-length", str(declared).encode()))
    chunks = [body[:7], body[7:]]

    async def receive():
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}
        return {"type": "http.disconnect"}

    seen = []

    async def run():
        request = Request({"type": "http", "method": "POST", "headers": headers, "path": DETECT, "query_string": b""}, receive)
        return await api._off_loop(request, api.CallEventsRequest, lambda req: seen.append(req) or {"ok": True})

    response = asyncio.run(run())
    assert response.status_code == 200, response.body[:200]
    assert seen and seen[0].call_events == []


def test_lane_threshold_and_budgets_are_the_documented_ones():
    assert api._SMALL_BODY_BYTES == 64 * 1024
    assert api._LARGE_LANE_SLOTS == 1
    assert api._LARGE_BYTES_PER_S == 32 * MIB and api._LARGE_BURST_BYTES == 16 * MIB
    assert api._LARGE_WAIT_S == 2.0
    assert api._MAX_JSON_MEMBERS == 32 * api._MAX_BATCH
    assert api._MAX_JSON_CONTAINERS == 4 * api._MAX_BATCH
    assert api._MAX_JSON_DEPTH == 32


# --- the sweep: refusals decided from the head alone -------------------------------

@pytest.mark.parametrize("name,headers,body,code", [
    ("413 from Content-Length", {"Content-Length": str(api._MAX_BODY_BYTES + 1)}, b"{}", 413),
    ("400 bad Content-Length", {"Content-Length": "12abc"}, b"{}", 400),
    ("431 head too large", {"X-Pad": "x" * (api._MAX_HEADER_BYTES + 1)}, b"{}", 431),
])
def test_head_only_refusals_are_held_so_a_looping_client_cannot_flood(name, headers, body, code):
    """The AEGIS `objs` body is 4.39 MB: it never reached the parse slot, it
    was 413'd from Content-Length before auth — ~1 ms of loop time each, so 8
    looping senders got ~400 attempts/s through and legit p50 went 4 -> 45 ms.
    A refusal that costs nothing to decide is now answered after
    _HEAD_REFUSAL_DELAY_S, which caps a looping client at 4 attempts/s per
    connection. Live: legit p50 back to 4 ms under 8 and 32 such senders."""
    from starlette.testclient import TestClient as _TC

    anon = _TC(api.app)
    t0 = time.perf_counter()
    r = anon.post(DETECT, headers={"Content-Type": "application/json", **headers}, content=body)
    took = time.perf_counter() - t0
    assert r.status_code == code, (name, r.status_code, r.text[:200])
    assert took >= api._HEAD_REFUSAL_DELAY_S, f"{name}: answered in {took * 1000:.0f} ms, not held"
    assert r.headers.get("connection") == "close"


def test_the_hold_applies_only_to_refusals_not_to_accepted_requests():
    t0 = time.perf_counter()
    r = client.post(DETECT, headers=HEADERS, content=b'{"call_events":[]}')
    assert r.status_code == 200
    assert time.perf_counter() - t0 < api._HEAD_REFUSAL_DELAY_S
    t0 = time.perf_counter()
    assert client.get("/health").status_code == 200
    assert time.perf_counter() - t0 < api._HEAD_REFUSAL_DELAY_S


# --- the real process: the AEGIS scenario ------------------------------------------

@pytest.fixture(scope="module")
def server():
    proc, port = _start_quiet()
    yield proc, "127.0.0.1", port
    _stop(proc)


def _rss_mib(pid: int) -> int:
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    raise AssertionError("no VmRSS")


def _pct(values: list[float], p: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]


def _request(host, port, method, path, body=None, timeout=120):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    conn.request(method, path, body=body, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


def _flood(server, n_senders: int, seconds: float, junk: bytes):
    proc, host, port = server
    stop = time.monotonic() + seconds
    lock = threading.Lock()
    codes: dict = {}
    legit: list[float] = []
    health: list[float] = []
    errors: list[str] = []
    peak = [_rss_mib(proc.pid)]
    base = peak[0]
    _, fixture = _request(host, port, "GET", "/fixtures/call-events")
    legit_body = json.dumps({"call_events": json.loads(fixture)}).encode()
    assert len(legit_body) <= 64 * 1024  # the small lane

    def count(key):
        with lock:
            codes[key] = codes.get(key, 0) + 1

    def sender():
        conn = http.client.HTTPConnection(host, port, timeout=120)
        while time.monotonic() < stop:
            try:
                conn.request("POST", DETECT, body=junk, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
                r = conn.getresponse()
                r.read()
                count(("junk", r.status))
            except OSError as exc:
                count(("junk", type(exc).__name__))
                conn = http.client.HTTPConnection(host, port, timeout=120)
        conn.close()

    def legit_client():
        conn = http.client.HTTPConnection(host, port, timeout=60)
        while time.monotonic() < stop:
            t0 = time.monotonic()
            try:
                conn.request("POST", DETECT, body=legit_body, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
                r = conn.getresponse()
                r.read()
                count(("legit", r.status))
            except OSError as exc:
                with lock:
                    errors.append("legit:" + type(exc).__name__)
                conn = http.client.HTTPConnection(host, port, timeout=60)
            with lock:
                legit.append(time.monotonic() - t0)
            time.sleep(0.1)
        conn.close()

    def prober():
        while time.monotonic() < stop:
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
            time.sleep(0.2)

    threads = ([threading.Thread(target=sender) for _ in range(n_senders)]
               + [threading.Thread(target=legit_client) for _ in range(2)]
               + [threading.Thread(target=prober)])
    for t in threads:
        t.start()
    # A legitimate LARGE batch in the middle of the flood must still succeed.
    time.sleep(seconds / 2)
    big = json.dumps(MAX_BATCHES[ORCHESTRATE]).encode()
    t0 = time.monotonic()
    big_attempts = 0
    while True:  # a legitimate client honors Retry-After on 503
        big_attempts += 1
        big_status, big_data = _request(host, port, "POST", ORCHESTRATE, big)
        if big_status != 503 or big_attempts >= 8:
            break
        time.sleep(1.0)
    big_took = time.monotonic() - t0
    for t in threads:
        t.join()
    time.sleep(3)
    idle = _rss_mib(proc.pid)
    return dict(codes=codes, legit=legit, health=health, errors=errors, base=base, peak=peak[0], idle=idle,
                big=(big_status, big_took, big_attempts))


LIVE_JUNK = {
    "keys": _junk_keys,                                                                   # AEGIS: 255k unknown keys, 3.4 MiB
    "objs": lambda: json.dumps({"call_events": [{"x": i} for i in range(250_000)]}).encode(),  # 3.65 MB
    "array": lambda: json.dumps({"call_events": [1] * 1_000_000}).encode(),               # 3.0 MB
    "oversized": lambda: b'{"call_events":[' + b"1," * (2 * MIB) + b"1]}",                # 4 MiB + 3: 413 before auth
}


@pytest.mark.parametrize("n_senders,kind", [(8, "keys"), (32, "keys"), (32, "objs"), (32, "array"), (8, "oversized")])
def test_live_junk_flood_does_not_starve_small_legit_requests(server, n_senders, kind):
    junk = LIVE_JUNK[kind]()
    res = _flood(server, n_senders, 6.0, junk)
    codes, legit, health = res["codes"], res["legit"], res["health"]
    summary = (f"{n_senders} {kind} senders: codes={codes} legit n={len(legit)} p50={_pct(legit, .5) * 1000:.0f}ms "
               f"p99={_pct(legit, .99) * 1000:.0f}ms max={max(legit) * 1000:.0f}ms; /health p50={_pct(health, .5) * 1000:.0f}ms "
               f"p99={_pct(health, .99) * 1000:.0f}ms max={max(health) * 1000:.0f}ms; RSS base {res['base']} peak {res['peak']} "
               f"idle {res['idle']} MiB; large batch {res['big'][0]} in {res['big'][1]:.2f}s ({res['big'][2]} attempts)")
    print(summary)
    assert res["errors"] == [], res["errors"]
    # A refused sender may also see a reset: a 413 closes the connection
    # with its body unread (fix wave 4), and a client still writing gets RST.
    assert all(k[0] != "junk" or k[1] in (413, 422, 503, "ConnectionResetError", "BrokenPipeError") for k in codes), codes
    assert codes.get(("junk", 413 if kind == "oversized" else 422), 0) > 0, codes
    assert codes.get(("legit", 200), 0) == len(legit), codes
    assert len(legit) >= 40, summary  # 2 clients x ~10/s x 6 s when not starved
    assert _pct(legit, .5) < 0.050, summary
    assert _pct(legit, .99) < 0.250, summary
    assert _pct(health, .99) < 0.250, summary
    assert res["big"][0] == 200, res["big"]
    assert res["big"][1] < 10.0, summary
    # Memory: at most the in-flight bodies (n x 3.4 MiB, the fix-wave-5
    # trade-off) plus one parse's worth, never a parse per sender.
    assert res["peak"] - res["base"] < n_senders * 4 + 64, summary
    assert res["idle"] - res["base"] < 64, summary
