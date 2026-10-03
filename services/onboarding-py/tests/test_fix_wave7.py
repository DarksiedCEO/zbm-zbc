"""Fix wave 7 (Sep 24 2026) — AEGIS round-6 findings on onboarding-py.

NEW-5 LOW  the admission doc claimed "a small body never queues behind a
           large one", but with scan_min_cost_bytes == scan_inflight_bytes
           every body took the whole budget, so one client's 416 KB bodies
           back-to-back put other clients' 40-byte messages at p50 294 ms
           (4 uploaders 1.6 s, 12 uploaders 6 s), and 16 queued large bodies
           answered a tiny message 503. Now two lanes (``ScanLanes``): bodies
           of at most ``scan_small_body_bytes`` (16 KiB, ~1 ms of scan) take
           a small lane with its own budget and its own queue, so they never
           wait behind a large scan and a flood of large bodies cannot fill
           their queue; large bodies stay serialized as before.
NEW-6 LOW  the facts cap was checked on stored + requested counts, before the
           per-field trimming, so at the cap a correction to an existing field
           was refused (409) by a message that told the client to do exactly
           that. Now the refusal is on the size the client would hold AFTER
           trimming: a restatement never adds, so it is accepted at the cap.
SKIPS      the three live ledger-rust tests skipped silently when the binary
           was missing. The binary is now built by a session fixture
           (``ledger_bin`` in conftest.py, ``cargo build --release`` into the
           ignored target dir); without cargo they skip with a reason that
           ``-rs`` (on by default, pytest.ini) prints.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from collections import Counter, defaultdict
from dataclasses import replace

import httpx
import pytest

from config import OnboardingConfig, load_config
from conftest import TEST_SERVICE_TOKEN, client_for, make_service, start_body
from onboarding_schema import requests as rq
from test_fix_wave5 import MAX_FACTS
from test_fix_wave6 import ROOT, RealStack, _facts, _pct

AUTH = {"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}
SMALL_MESSAGE = {"text": "We are on Shopify and want faster follow-up."}
BIG_BODY = json.dumps(MAX_FACTS).encode()  # the AEGIS probe's 416 KB benign body


# =============================================================================
# NEW-5 — a small body never waits behind a large scan, and is never 503 for it
# =============================================================================


def test_new5_small_lane_is_configured_validated_and_documented():
    cfg = OnboardingConfig()
    assert cfg.scan_small_body_bytes == 16 * 1024 and cfg.scan_small_inflight == 1 and cfg.scan_small_max_waiting == 8
    cfg = load_config({"ONBOARDING_SCAN_SMALL_BODY_BYTES": "4096", "ONBOARDING_SCAN_SMALL_INFLIGHT": "2",
                       "ONBOARDING_SCAN_SMALL_MAX_WAITING": "3"})
    assert (cfg.scan_small_body_bytes, cfg.scan_small_inflight, cfg.scan_small_max_waiting) == (4096, 2, 3)
    for bad in ({"scan_small_body_bytes": -1}, {"scan_small_inflight": 0}, {"scan_small_max_waiting": -1}):
        with pytest.raises(Exception):
            OnboardingConfig(**bad)
    readme = (ROOT / "README.md").read_text()
    assert "ONBOARDING_SCAN_SMALL_BODY_BYTES" in readme
    # the claim the finding was about is no longer made of the single budget
    assert "a small body never queues behind a large one" not in (ROOT / "src" / "config.py").read_text()


def test_new5_lanes_route_by_size_and_a_small_body_never_waits_for_the_large_lane():
    from api import ScanLanes, ServiceBusy

    lanes = ScanLanes(replace(OnboardingConfig(), scan_max_waiting=0, scan_small_max_waiting=0, scan_wait_seconds=0.2))
    assert lanes.lane(0) is lanes.small and lanes.lane(16 * 1024) is lanes.small
    assert lanes.lane(16 * 1024 + 1) is lanes.large and lanes.lane(1024 * 1024) is lanes.large
    big = lanes.hold(len(BIG_BODY))
    big.__enter__()
    try:
        # the large lane is taken whole (serialized, as before) ...
        with pytest.raises(ServiceBusy):
            with lanes.hold(17 * 1024):
                pass
        # ... and a small body is admitted at once, without waiting for it. Fix wave 25 (scout A O2; R-HYGIENE L1):
        # no wall-clock bound — with max_waiting 0 nothing can wait: the large lane (held) would have raised
        # ServiceBusy, so being admitted IS the proof it is not behind the large lane.
        with lanes.hold(40):
            pass
        # small bodies are serialized among themselves in their own lane
        s = lanes.hold(40)
        s.__enter__()
        try:
            with pytest.raises(ServiceBusy):  # small queue full (max_waiting 0) -> busy, not a wait on the large lane
                with lanes.hold(40):
                    pass
        finally:
            s.__exit__(None, None, None)
    finally:
        big.__exit__(None, None, None)
    # a lane threshold of 0 disables the small lane: everything is large
    lanes = ScanLanes(replace(OnboardingConfig(), scan_small_body_bytes=0))
    assert lanes.lane(0) is lanes.large and lanes.small is None


def test_new5_a_tiny_message_is_200_while_a_large_scan_holds_the_budget_and_its_queue_is_full():
    # The finding's shape in-process: the large lane's budget is held (a 416
    # KB scan in progress) and its queue is full (max_waiting 0), so any
    # large body is busy. A 40-byte message must still be answered at once.
    svc = make_service(all_fakes=True)
    svc.config = replace(svc.config, scan_max_waiting=0, scan_wait_seconds=0.3)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    gate = c.app.state.scan_admission  # the large lane
    assert gate.try_hold_for_test()  # the whole large budget: a large scan is in progress
    try:
        r = c.post("/onboarding/clients/client_a/intake/facts", content=BIG_BODY, headers={"Content-Type": "application/json"})
        assert r.status_code == 503, r.text  # large: busy, as before
        lat = []
        for _ in range(5):
            t = time.monotonic()
            r = c.post("/onboarding/clients/client_a/messages", json=SMALL_MESSAGE)
            lat.append(time.monotonic() - t)
            assert r.status_code == 200, r.text
        assert max(lat) < 0.25, lat  # never waited the 0.3 s for the large lane, never 503
    finally:
        gate.release_for_test()


def test_new5_a_flood_of_queued_large_bodies_cannot_503_small_bodies():
    # 16 large waiters fill the large queue (the default), yet small bodies
    # keep their own reserved queue: none is refused.
    svc = make_service(all_fakes=True)
    svc.config = replace(svc.config, scan_max_waiting=3, scan_wait_seconds=1.5)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    gate = c.app.state.scan_admission
    assert gate.try_hold_for_test()
    codes = Counter()
    lock = threading.Lock()

    def big():
        r = c.post("/onboarding/clients/client_a/intake/facts", content=BIG_BODY, headers={"Content-Type": "application/json"})
        with lock:
            codes["big_" + str(r.status_code)] += 1

    ths = [threading.Thread(target=big) for _ in range(6)]
    try:
        for t in ths:
            t.start()
        deadline = time.monotonic() + 1.0
        while gate.waiting < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gate.waiting == 3, gate.waiting  # the large queue is full; 3 more were 503 at once
        for _ in range(8):
            r = c.post("/onboarding/clients/client_a/messages", json=SMALL_MESSAGE)
            with lock:
                codes["small_" + str(r.status_code)] += 1
    finally:
        gate.release_for_test()
        for t in ths:
            t.join(10)
    assert codes["small_200"] == 8 and codes["small_503"] == 0, codes
    # the 3 beyond the queue were busy at once; of the 3 queued, those the
    # 1.5 s wait did not reach (each scan is ~1 s of CPU here) were busy too
    assert codes["big_503"] >= 3 and codes["big_200"] >= 1 and codes["big_503"] + codes["big_200"] == 6, codes


def _kept_fields_body() -> bytes:
    # the AEGIS body shape, but with fields the client lane KEEPS (redacted)
    from intelligences import i01_client_understanding as i01
    from onboarding_schema import Lane

    allowed = i01.LANE_FIELDS[Lane.CLIENT]
    facts = [dict(MAX_FACTS["facts"][i], field=allowed[i % len(allowed)]) for i in range(200)]
    return json.dumps({"facts": facts}).encode()


def test_new5_the_large_lane_is_held_through_the_handler_and_the_service_lock_is_not(monkeypatch):
    # The class behind NEW-5: a large body's work is not only its check. The
    # service redacts the same text (~1.5 s of CPU for 416 KB of profile
    # fields) — under the service lock, outside the admission gate, before
    # this fix. Now: (1) the large lane is held while the handler runs, so
    # a second large body waits, while a message goes through the small
    # lane; (2) the redaction runs before the service lock is taken, so a
    # concurrent operation (a message) is not blocked by it.
    import service as service_mod

    svc = make_service(all_fakes=True)
    svc.config = replace(svc.config, scan_max_waiting=4, scan_wait_seconds=5.0)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    body = _kept_fields_body()
    inside = threading.Event()
    release = threading.Event()
    real_redact = service_mod.redact_text
    calls = {"redact_in_lock": 0, "redact": 0}

    def slow_redact(text):
        calls["redact"] += 1
        if svc._depth > 0 and len(text) > 1000:  # a fact value redacted inside an operation, i.e. under the lock
            calls["redact_in_lock"] += 1
        if calls["redact"] == 1:
            inside.set()
            release.wait(10)
        return real_redact(text)

    monkeypatch.setattr(service_mod, "redact_text", slow_redact)
    results = {}

    def big(name):
        r = c.post("/onboarding/clients/client_a/intake/facts", content=body, headers={"Content-Type": "application/json"})
        results[name] = (r.status_code, time.monotonic())

    t1 = threading.Thread(target=big, args=("first",))
    t1.start()
    assert inside.wait(10)  # the first large body is in its redaction (handler running, lane held)
    # Fix wave 25 (scout A O2; R-HYGIENE L1): the first large body is HELD in its redaction until `release`, so a
    # message behind the lock or the lane could not be answered at all; it must be answered while the hold lasts
    # (it was a 1 s wall-clock bound).
    got: list = []
    m = threading.Thread(target=lambda: got.append(c.post("/onboarding/clients/client_a/messages", json=SMALL_MESSAGE)))
    m.start()
    m.join(30)
    assert got and got[0].status_code == 200, got  # not behind the lock, not behind the lane
    t2 = threading.Thread(target=big, args=("second",))
    t2.start()
    # Fix wave 25 (scout A O3): wait until the second large body is QUEUED for the large lane — it used to sleep
    # 0.3 s and assert its absence, which also held when, on a loaded box, the second request had not even reached
    # the lane yet (nothing was measured).
    # (the lane's `waiting` count, read under its lock — E-A review: the first draft held on to the private list,
    # which `_grant` replaces with a new one)
    large = c.app.state.scan_lanes.large
    deadline = time.monotonic() + 10
    while large.waiting == 0 and "second" not in results and time.monotonic() < deadline:
        time.sleep(0.01)
    assert large.waiting == 1 and "second" not in results, (large.waiting, results)  # it waits for the lane
    released_at = time.monotonic()
    release.set()
    t1.join(30)
    t2.join(30)
    assert results["first"][0] == 200 and results["second"][0] == 200, results
    assert results["second"][1] > results["first"][1] > released_at
    assert calls["redact_in_lock"] == 0 and calls["redact"] >= 400, calls


def test_new5_one_request_scans_each_string_once_across_check_service_and_response_scrub(monkeypatch):
    import redaction

    svc = make_service(all_fakes=True)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    body = _kept_fields_body()
    real = redaction._find_credential_uncached
    seen = Counter()

    def counting(text):
        seen[text] += 1
        return real(text)

    monkeypatch.setattr(redaction, "_find_credential_uncached", counting)
    r = c.post("/onboarding/clients/client_a/intake/facts", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    value = MAX_FACTS["facts"][0]["value"]
    assert seen[value] == 1, seen[value]  # checked once, then the service's redaction and the response scrub reused the verdict


def test_new5_launcher_sets_a_1ms_switch_interval(monkeypatch):
    import sys as _sys

    import serve
    import uvicorn

    assert serve.SWITCH_INTERVAL_S == 0.001
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw, interval=_sys.getswitchinterval()))
    before = _sys.getswitchinterval()
    try:
        serve.run(object(), "127.0.0.1", 1)
    finally:
        _sys.setswitchinterval(before)
    assert seen["interval"] == 0.001 and seen["http"] is serve.HeadDeadlineH11Protocol
    monkeypatch.setenv("ONBOARDING_SWITCH_INTERVAL_SECONDS", "0")
    with pytest.raises(RuntimeError):
        serve._positive("ONBOARDING_SWITCH_INTERVAL_SECONDS", 0.001)


@pytest.fixture(scope="module")
def real_stack7(ledger_bin):
    s = RealStack(ledger_bin)
    yield s
    s.close()


def _serial_scenario(base: str, nbig: int, nsmall: int, dur: float) -> dict:
    """AEGIS round 6 ``onb_serial6.py``: NBIG clients each push the 416 KB
    body back-to-back, NSMALL other clients send a tiny message every 0.3 s;
    small-client latency and status codes are what matter."""
    stamp = int(time.time() * 1000) % 10_000_000
    stop = threading.Event()
    lock = threading.Lock()
    codes: Counter = Counter()
    lat: dict[str, list[float]] = defaultdict(list)
    # Fix wave 26 (W26-2a): every request's (sent, answered) window, and the instant each large body was answered
    # 200 (= one large scan done) — the ordering the NEW-5 property is about (see `_waited_for_a_scan`).
    windows: dict[str, list[tuple[float, float]]] = defaultdict(list)
    scans_done: list[float] = []

    def rec(k, code, dt):
        end = time.monotonic()
        with lock:
            codes[f"{k}_{code}"] += 1
            lat[k].append(dt)
            windows[k].append((end - dt, end))
            if k == "big" and code == 200:
                scans_done.append(end)

    def mk(cid):
        c = httpx.Client(base_url=base, headers=AUTH, timeout=180)
        r = c.post("/onboarding/clients", json=start_body(cid))
        assert r.status_code == 201, r.text
        return c

    def bigw(i):
        cid = f"big{stamp}_{i}"
        c, n = mk(cid), 0
        while not stop.is_set():
            n += 1
            if n % 3 == 0:  # facts cap 2000/client: a fresh client every 3 posts of ~200 facts
                cid = f"big{stamp}_{i}_{n}"
                c = mk(cid)
            t = time.monotonic()
            try:
                r = c.post(f"/onboarding/clients/{cid}/intake/facts", content=BIG_BODY, headers={"Content-Type": "application/json"})
                rec("big", r.status_code, time.monotonic() - t)
                if r.status_code == 503:
                    stop.wait(min(2.0, float(r.headers.get("retry-after", "1"))))
            except httpx.HTTPError as e:
                rec("big", type(e).__name__, time.monotonic() - t)

    def smallw(i):
        c = mk(f"sm{stamp}_{i}")
        while not stop.is_set():
            t = time.monotonic()
            try:
                r = c.post(f"/onboarding/clients/sm{stamp}_{i}/messages", json=SMALL_MESSAGE)
                rec("small", r.status_code, time.monotonic() - t)
            except httpx.HTTPError as e:
                rec("small", type(e).__name__, time.monotonic() - t)
            stop.wait(0.3)

    def health():
        c = httpx.Client(base_url=base, headers=AUTH, timeout=30)
        while not stop.is_set():
            t = time.monotonic()
            try:
                rec("health", c.get("/health").status_code, time.monotonic() - t)
            except httpx.HTTPError as e:
                rec("health", type(e).__name__, time.monotonic() - t)
            stop.wait(0.25)

    ths = ([threading.Thread(target=bigw, args=(i,)) for i in range(nbig)]
           + [threading.Thread(target=smallw, args=(i,)) for i in range(nsmall)] + [threading.Thread(target=health)])
    for t in ths:
        t.start()
    time.sleep(dur)
    stop.set()
    for t in ths:
        t.join(200)
    done = sorted(scans_done)
    gaps = [b - a for a, b in zip(done, done[1:])]
    out = {"codes": dict(codes), "scans_done": len(done), "scan_gap": statistics.median(gaps) if gaps else float("nan")}
    for k in ("big", "small", "health"):
        xs = lat[k]
        out[k] = {"n": len(xs), "p50": _pct(xs, .5), "p90": _pct(xs, .9), "max": max(xs) if xs else float("nan"),
                  "waited": _waited_for_a_scan(windows[k], scans_done)}
    return out


def _waited_for_a_scan(windows: list[tuple[float, float]], scans_done: list[float]) -> float:
    """Fix wave 26 (W26-2a), printed as a diagnostic, not asserted: the fraction of requests during which a large
    body was answered 200 (one large scan done). With the small lane disabled (the single budget NEW-5 was about)
    the small messages measured 0.93-1.0; without a wait it is chance, about latency / time between two scans' ends:
    0.00-0.12 on Linux, up to 0.29 with a 10 ms GIL switch interval, and an estimated 0.3-0.5 on the macos-26 runner
    from CI #2's numbers (158 ms beside scans ~0.35-0.5 s apart) -- too close to any bound to assert. It also misses
    a wait that ends before the large body's answer (an `async def` body dependency blocking the loop: /health
    p50 429 ms, this fraction 0.00), which `scan_gap` below catches."""
    import bisect

    done = sorted(scans_done)
    if not windows:
        return float("nan")
    hit = sum(1 for s, e in windows if bisect.bisect_right(done, s) < bisect.bisect_left(done, e))
    return hit / len(windows)


# Fix wave 26 (W26-2a; CI #2: 3 failed on macos-26 at `small p50 < 0.05`, 158 ms, every other assertion passing).
# The wall-clock bounds (small and /health p50 < 50 ms) measured the machine: on the macOS runner every request is
# slower beside a CPU-bound scan thread (GIL hand-offs; /health p50 42 ms there vs 4-7 ms on Linux), whether or not
# anything waited for the large lane. Reproduced on Linux with a 10 ms switch interval
# (ONBOARDING_SWITCH_INTERVAL_SECONDS=0.01): the fc19ce7 test fails 3/3 (small p50 106-266 ms) with no wait.
# Both bounds are now ratios of two things measured in the same run on the same server:
#  - a small message against /health: both pay the same machine and the same contention; only a message that
#    waits for the large lane pays a scan on top. Measured small p50 / health p50: fixed 3.0-6.0 (Linux, idle and
#    2 busy loops), 3.8 (macos-26, CI #2), 3.8-8.0 (10 ms switch interval); small lane disabled (= the defect)
#    82-1724 idle, 192-2174 under 2 busy loops, 17-311 with the 10 ms switch interval.
_SMALL_OVER_HEALTH_MAX = 12.0
# Fix wave 26b (AEGIS r26 R26-2: margins thin under macOS-like scheduling — fixed 9.0, defect 17.3 at a 10 ms switch
# interval). Measured on the build box (M4 Pro, macOS 26.6, 3.13; small lane off = ONBOARDING_SCAN_SMALL_BODY_BYTES=0
# is the defect), small p50 / health p50 for 1 / 4 / 12 uploaders: fixed 3.7 / 3.4 / 3.4 (1 ms interval) and
# 1.5 / 3.9 / 4.8 (10 ms); defect 5.4 / 128 / 398 (1 ms) and 5.5 / 30 / 97 (10 ms). With ONE uploader the defect
# reads under the bound (a small body waits at most for the one scan in flight, and the lane is idle while that
# uploader sends its next 416 KB), so that case could only ever fail a correct server: the ratio is asserted where the
# defect is visible — _RATIO_FROM_NBIG uploaders and more — and printed for fewer. Every other assertion still runs.
_RATIO_FROM_NBIG = 4
#  - /health against the time between two large scans' ends (the large lane is serial: one scan each): a /health
#    that waits for a scan on the event loop waits half a scan on average. Measured health p50 / scan gap: fixed
#    0.004-0.007 (Linux), 0.013-0.044 (10 ms switch interval), ~0.08-0.12 estimated on macos-26 (42 ms against
#    ~0.35-0.5 s); the body dependency made `async def` (validation on the loop, the fix wave 4 R1 defect) 0.66.
_HEALTH_OVER_SCAN_GAP_MAX = 0.25
# a run with fewer large scans than this (or almost no /health answers) measured nothing
_MIN_SCANS_DONE = 3
_MIN_HEALTH = 10


@pytest.mark.parametrize("nbig,nsmall", [(1, 4), (4, 4), (12, 4)])
def test_new5_live_small_messages_stay_fast_beside_large_uploaders(real_stack7, nbig, nsmall):
    r = _serial_scenario(real_stack7.base, nbig, nsmall, 8.0)
    small_over_health = r["small"]["p50"] / r["health"]["p50"]
    health_over_gap = r["health"]["p50"] / r["scan_gap"]
    summary = (f"big={nbig}x{len(BIG_BODY) // 1024}KB small={nsmall}: codes={r['codes']} "
               + " ".join(f"{k}: n={r[k]['n']} p50={r[k]['p50'] * 1000:.0f}ms p90={r[k]['p90'] * 1000:.0f}ms "
                          f"max={r[k]['max'] * 1000:.0f}ms waited-for-a-scan={r[k]['waited']:.2f}"
                          for k in ("big", "small", "health"))
               + f" scan_gap={r['scan_gap'] * 1000:.0f}ms small/health={small_over_health:.1f} "
               f"(bound {_SMALL_OVER_HEALTH_MAX}, {'asserted' if nbig >= _RATIO_FROM_NBIG else 'printed only'}) "
               f"health/scan_gap={health_over_gap:.3f} "
               f"(bound {_HEALTH_OVER_SCAN_GAP_MAX}) small/scan_gap={r['small']['p50'] / r['scan_gap']:.3f}")
    print(summary)
    codes = r["codes"]
    if r["scans_done"] < _MIN_SCANS_DONE or r["health"]["n"] < _MIN_HEALTH:
        pytest.fail(f"INVALID run: {r['scans_done']} large scans finished (need {_MIN_SCANS_DONE}), "
                    f"{r['health']['n']} /health answers (need {_MIN_HEALTH}), so nothing was measured -- {summary}")
    # the finding's numbers: small p50 294 ms (1 uploader), 1.6 s (4), 6 s (12), each message waiting for the
    # large scans ahead of it; /health 4-7 ms beside them on the same Linux box
    if nbig >= _RATIO_FROM_NBIG:
        assert small_over_health < _SMALL_OVER_HEALTH_MAX, summary
    assert r["small"]["n"] >= 40, summary
    assert codes.get("small_200", 0) == r["small"]["n"], summary  # never 503, never an error
    assert health_over_gap < _HEALTH_OVER_SCAN_GAP_MAX, summary
    # large bodies are still admitted (serialized), busy ones are 503 with nothing else
    assert codes.get("big_200", 0) >= 1 and set(k for k in codes if k.startswith("big_")) <= {"big_200", "big_503"}, summary


def test_new5_live_large_bodies_are_still_serialized(real_stack7):
    # Instrument nothing: the 416 KB body costs ~0.5-1.3 s of CPU, so 4
    # concurrent posts of it finish one after another if serialized, and
    # in about the time of one if they overlapped (they would if the large
    # lane admitted more than one). Wall time of the batch vs a single post.
    c = httpx.Client(base_url=real_stack7.base, headers=AUTH, timeout=120)
    assert c.post("/onboarding/clients", json=start_body("ser_a")).status_code == 201
    hdr = {"Content-Type": "application/json"}
    t = time.monotonic()
    assert c.post("/onboarding/clients/ser_a/intake/facts", content=BIG_BODY, headers=hdr).status_code == 200
    one = time.monotonic() - t
    ends = []
    lock = threading.Lock()

    def post():
        with httpx.Client(base_url=real_stack7.base, headers=AUTH, timeout=120) as cc:
            r = cc.post("/onboarding/clients/ser_a/intake/facts", content=BIG_BODY, headers=hdr)
            with lock:
                ends.append((time.monotonic(), r.status_code))

    ths = [threading.Thread(target=post) for _ in range(4)]
    t = time.monotonic()
    for th in ths:
        th.start()
    for th in ths:
        th.join(120)
    assert sorted(code for _, code in ends) == [200] * 4, ends
    finish = sorted(e - t for e, _ in ends)
    gaps = [b - a for a, b in zip(finish, finish[1:])]
    print(f"one={one:.2f}s finishes={[f'{x:.2f}' for x in finish]} gaps={[f'{g:.2f}' for g in gaps]}")
    # serialized: completions are spread out by about one scan each, not bunched
    assert statistics.median(gaps) > 0.4 * one, (one, finish)


# =============================================================================
# NEW-6 — the cap is checked on the size after per-field trimming
# =============================================================================


def _restate(field: str, value: str, provenance: str = "client_stated") -> dict:
    # evidence carries the value too: a field outside the lane's profile keeps
    # its name and evidence but not its value (fix wave 3), and these tests
    # look at what was kept
    return {"facts": [{"field": field, "value": value, "provenance": provenance, "evidence": f"ev:{value}",
                       "observed_at": "2026-09-02T12:00:00Z"}]}


def _kept(rec, field: str) -> list[str]:
    return [f.evidence.removeprefix("ev:") for f in rec.facts if f.field == field]


def test_new6_at_the_cap_a_correction_to_a_field_at_its_history_limit_is_accepted():
    # cap 400, history 20: 380 distinct fields plus one field ("brand") with
    # its full 20 observations = exactly 400 stored.
    svc = make_service(all_fakes=True)
    svc.config = replace(svc.config, max_facts_per_client=400, facts_history_per_field=20)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    for k in range(2):  # a request holds at most 200 facts
        assert c.post("/onboarding/clients/client_a/intake/facts", json=_facts(190, f"n{k}_")).status_code == 200
    for i in range(20):
        assert c.post("/onboarding/clients/client_a/intake/facts", json=_restate("brand", f"b{i}")).status_code == 200
    rec = svc.clients["client_a"]
    assert len(rec.facts) == 400 and _kept(rec, "brand") == [f"b{i}" for i in range(20)]
    # the finding: this restatement trims back to 400, yet it was 409 by a
    # message that told the client to restate
    r = c.post("/onboarding/clients/client_a/intake/facts", json=_restate("brand", "b20"))
    assert r.status_code == 200, r.text
    assert len(rec.facts) == 400 and _kept(rec, "brand") == [f"b{i}" for i in range(1, 21)]
    # a NEW field at the cap is still refused, and the message says what adds and what does not
    r = c.post("/onboarding/clients/client_a/intake/facts", json=_restate("business_name", "Acme Widgets"))
    assert r.status_code == 409, r.text
    j = r.json()
    assert (j["facts_stored"], j["facts_in_request"], j["facts_after_request"], j["max_facts_per_client"]) == (400, 1, 401, 400)
    assert "cap is 400" in j["detail"] and "nothing was stored" in j["detail"] and "would hold 401" in j["detail"], j
    assert "already has 20 replaces its oldest and adds nothing" in j["detail"] and "with fewer adds one" in j["detail"], j
    assert len(rec.facts) == 400
    # a field with ONE observation restated adds one (its history is not full): refused at the cap, accurately
    r = c.post("/onboarding/clients/client_a/intake/facts", json=_restate("n0_7", "corrected"))
    assert r.status_code == 409 and r.json()["facts_after_request"] == 401, r.text
    # 3 restatements of the full field plus 1 new field: refused for that 1 (post-trim 401)
    body = {"facts": sum((_restate("brand", f"c{i}")["facts"] for i in range(3)), []) + _restate("brand_new", "d")["facts"]}
    r = c.post("/onboarding/clients/client_a/intake/facts", json=body)
    assert r.status_code == 409 and r.json()["facts_after_request"] == 401 and r.json()["facts_in_request"] == 4, r.text
    assert len(rec.facts) == 400 and _kept(rec, "brand") == [f"b{i}" for i in range(1, 21)]


def test_new6_boundary_the_trimmed_size_is_what_counts():
    # history 3 per field, cap 6: field A restated 10 times in ONE request
    # trims to 3 (post-trim 3 <= 6: accepted); stored A(3) + B(3) with a
    # request of A x5 + B x5 + C x1 trims to 7 > 6: refused; A x5 + B x5 fits.
    svc = make_service(all_fakes=True)
    svc.config = replace(svc.config, max_facts_per_client=6, facts_history_per_field=3)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    rec = svc.clients["client_a"]
    many = {"facts": [_restate("A", f"a{i}")["facts"][0] for i in range(10)]}
    r = c.post("/onboarding/clients/client_a/intake/facts", json=many)
    assert r.status_code == 200, r.text
    assert _kept(rec, "A") == ["a7", "a8", "a9"] and len(rec.facts) == 3
    r = c.post("/onboarding/clients/client_a/intake/facts", json={"facts": [_restate("B", f"b{i}")["facts"][0] for i in range(3)]})
    assert r.status_code == 200 and len(rec.facts) == 6
    over = {"facts": [_restate(f, f"{f}{i}")["facts"][0] for f in ("A", "B") for i in range(5)] + _restate("C", "c")["facts"]}
    r = c.post("/onboarding/clients/client_a/intake/facts", json=over)
    assert r.status_code == 409 and r.json()["facts_after_request"] == 7 and r.json()["facts_in_request"] == 11, r.text
    assert len(rec.facts) == 6 and _kept(rec, "B") == ["b0", "b1", "b2"]
    fits = {"facts": [_restate(f, f"{f}{i}")["facts"][0] for f in ("A", "B") for i in range(5)]}
    r = c.post("/onboarding/clients/client_a/intake/facts", json=fits)
    assert r.status_code == 200, r.text
    assert len(rec.facts) == 6 and _kept(rec, "B") == ["B2", "B3", "B4"] and _kept(rec, "A") == ["A2", "A3", "A4"]
    # refusal happens before any flag/record/store: the ledger saw nothing for the refused request
    events = len(svc.ledger.events)
    assert c.post("/onboarding/clients/client_a/intake/facts", json=_restate("D", "d")).status_code == 409
    assert len(svc.ledger.events) == events and len(rec.facts) == 6


def test_new6_refused_requests_stay_cheap():
    # The post-trim count is O(stored + requested) and runs before any scan
    # or rebuild (wave 6's cost assertion still holds with it).
    svc = make_service(all_fakes=True)
    from service import OnboardingError

    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    accept = []
    for k in range(10):
        req = rq.FactsRequest.model_validate(_facts(200, f"note{k}_", "x" * 200))
        t = time.thread_time()
        svc.add_facts("client_a", req)
        accept.append(time.thread_time() - t)
    refused = []
    for k in range(10, 20):
        req = rq.FactsRequest.model_validate(_facts(200, f"note{k}_", "x" * 200))
        t = time.thread_time()
        with pytest.raises(OnboardingError) as ei:
            svc.add_facts("client_a", req)
        refused.append(time.thread_time() - t)
        assert ei.value.status_code == 409
    assert statistics.median(refused) < accept[0], (statistics.median(refused), accept[0])


# =============================================================================
# skips — the live ledger tests run by default, or skip with a printed reason
# =============================================================================


def test_skips_ledger_binary_is_built_by_the_fixture_and_the_reason_is_visible():
    import subprocess
    from conftest import ledger_rust_binary

    p = ledger_rust_binary()
    assert p is not None and p.is_file(), p
    assert (ROOT / "pytest.ini").read_text().find("-rs") >= 0
    readme = (ROOT / "README.md").read_text()
    assert "cargo build" in readme and "-rs" in readme
    # honest skip: no cargo on PATH and no binary named -> the reason names both
    env = {"PATH": "/nonexistent", "ONBOARDING_LEDGER_RUST_BIN": "/nonexistent/server"}
    import sys
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs", str(ROOT / "tests" / "test_fix_wave6.py"),
                          "-k", "live"], cwd=str(ROOT), env={**{k: v for k, v in __import__("os").environ.items()
                                                                if k not in ("PATH", "ONBOARDING_LEDGER_RUST_BIN")}, **env},
                         capture_output=True, text=True, timeout=120).stdout
    assert "3 skipped" in out and "cargo" in out and "ONBOARDING_LEDGER_RUST_BIN" in out, out[-1500:]
