"""Fix wave 6 (Sep 24 2026) — AEGIS round-5 findings on onboarding-py.

N6  LOW   503 semantics differed from creative-py: ANY ledger 503 counted as
          "certainly not recorded". Only ledger-rust's own load-shed answer
          (503 with its exact body, written before the request is read)
          proves that; a 503 from anything in between may have been sent
          after the request was forwarded. Now the same rule as
          services/creative-py/src/shared/ledger.py: shed body -> not
          recorded; any other 5xx -> unknown.
N4  LOW   the heavy-scan gate was a step at 64 KiB: 40 concurrent 58 KB
          bodies bypassed it, filled the thread pool and starved light GETs
          (p50 3.1 s) and /health (2.4 s). Now a weighted budget
          (``ScanAdmission``): every body takes cost proportional to its size
          from a shared in-flight budget, so many medium bodies are throttled
          like one large one.
W5-L      facts per client grew without a cap and the profile was rebuilt over
          all of them: capped at ``max_facts_per_client`` (409 beyond it,
          nothing stored) and ``facts_history_per_field`` latest observations.
T         the per-pattern timing tests were flaky under machine load: bounds
          are now scaled by a measured slowdown and a machine-independent
          linearity ratio is asserted (test_fix_wave4.py).

The live tests (``real_stack``) run ``python3 -m api`` against the REAL
ledger-rust binary: the ``ledger_bin`` session fixture (conftest.py, fix
wave 7) uses ONBOARDING_LEDGER_RUST_BIN if set, else builds it with cargo
(into CARGO_TARGET_DIR or services/ledger-rust/target, at the path cargo
reports — fix wave 8, N7-6); only a missing cargo skips them, with a
reason that ``-rs`` prints (they used to skip silently).
"""

from __future__ import annotations

import json
import os
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from config import OnboardingConfig
from conftest import TEST_SERVICE_TOKEN, client_for, free_test_port, make_service, start_body
import ledger
from ledger import HttpLedgerClient, LedgerWriteError
from onboarding_schema import requests as rq
from test_fix_wave5 import BENIGN_VALUE, MAX_FACTS, _fact, _stop, _wait_health

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
REPO = ROOT.parents[1]
AUTH = {"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}
LEDGER_TOKEN = "wave6-ledger-test-token"
# Pinned here independently of src/ledger.py: what bin/server.rs ``shed`` writes.
LEDGER_SHED_BODY = {"error": "ledger-rust is at its connection limit; retry shortly"}


# =============================================================================
# N6 — only ledger-rust's exact shed answer is "certainly not recorded"
# =============================================================================


def _client(handler) -> HttpLedgerClient:
    return HttpLedgerClient("http://ledger.test", "tok", transport=httpx.MockTransport(handler))


def _record(c: HttpLedgerClient) -> LedgerWriteError:
    with pytest.raises(LedgerWriteError) as ei:
        c.record_event("onb-1", "onboarding", "t", "a", "s", {}, "x")
    return ei.value


def test_n6_shed_503_with_the_exact_body_is_certainly_not_recorded():
    exc = _record(_client(lambda req: httpx.Response(503, json=LEDGER_SHED_BODY, headers={"Retry-After": "1"})))
    assert exc.outcome == "not_recorded"
    assert "shed" in str(exc) and "nothing was recorded" in str(exc)


@pytest.mark.parametrize("body", [
    {},                                                        # bare 503
    {"error": "ledger-rust is at its connection limit"},       # nearly the shed body
    {"error": "upstream unavailable"},                         # a gateway
    {"detail": LEDGER_SHED_BODY["error"]},                     # same text, different key
    "Service Unavailable",                                     # HTML/text from a proxy
    "",                                                        # empty body
])
def test_n6_any_other_503_is_unknown(body):
    if isinstance(body, dict):
        handler = lambda req: httpx.Response(503, json=body)  # noqa: E731
    else:
        handler = lambda req: httpx.Response(503, text=body)  # noqa: E731
    exc = _record(_client(handler))
    assert exc.outcome == "unknown", str(exc)
    assert "may or may not be recorded" in str(exc)


@pytest.mark.parametrize("status", [500, 502, 504, 507])
def test_n6_other_5xx_stays_unknown_even_with_the_shed_body(status):
    # The body alone proves nothing: the shed answer is 503 + this body.
    exc = _record(_client(lambda req: httpx.Response(status, json=LEDGER_SHED_BODY)))
    assert exc.outcome == "unknown"


def test_n6_shed_rule_is_the_same_as_creative_py_and_ledger_rust():
    # creative-py's constant (the shared rule, ADR 0004 <-> ADR 0005) ...
    creative = (REPO / "services" / "creative-py" / "src" / "shared" / "ledger.py").read_text()
    m = re.search(r"^LEDGER_SHED_BODY\s*=\s*(\{.*\})\s*$", creative, re.M)
    assert m, "creative-py no longer defines LEDGER_SHED_BODY"
    assert eval(m.group(1)) == LEDGER_SHED_BODY  # noqa: S307 (a literal dict from our own repo)
    assert ledger.LEDGER_SHED_BODY == LEDGER_SHED_BODY
    # ... and the body ledger-rust's shed() actually writes.
    rust = (REPO / "services" / "ledger-rust" / "src" / "bin" / "server.rs").read_text()
    shed = rust[rust.index("async fn shed("):]
    m = re.search(r'json!\(\{"error":\s*"([^"]+)"\}\)', shed)
    assert m and {"error": m.group(1)} == LEDGER_SHED_BODY
    assert "503 Service Unavailable" in shed
    # is_ledger_shed is exact: status AND body
    is_ledger_shed = ledger.is_ledger_shed
    assert is_ledger_shed(httpx.Response(503, json=LEDGER_SHED_BODY))
    assert not is_ledger_shed(httpx.Response(502, json=LEDGER_SHED_BODY))
    assert not is_ledger_shed(httpx.Response(503, json={**LEDGER_SHED_BODY, "extra": 1}))
    assert not is_ledger_shed(httpx.Response(503, text=json.dumps(LEDGER_SHED_BODY) + "x"))


# =============================================================================
# N4 — a weighted admission budget instead of a size step
# =============================================================================


def test_n4_cost_is_proportional_with_a_floor_and_a_cap():
    from api import ScanAdmission

    g = ScanAdmission(budget_bytes=512 * 1024, min_cost_bytes=8192, max_waiting=0, wait_s=0.1)
    assert g.cost(0) == 8192 and g.cost(100) == 8192 and g.cost(8192) == 8192
    assert g.cost(58 * 1024) == 58 * 1024
    assert g.cost(512 * 1024) == 512 * 1024 and g.cost(1024 * 1024) == 512 * 1024  # over the budget: takes all of it
    # the floor can never exceed the budget
    assert ScanAdmission(1000, 5000, 0, 0.1).cost(1) == 1000


def test_n4_many_medium_bodies_fill_the_budget_like_one_large_one():
    from api import ScanAdmission, ServiceBusy

    g = ScanAdmission(budget_bytes=512 * 1024, min_cost_bytes=8192, max_waiting=0, wait_s=0.1)
    held = []
    body = 58 * 1024
    while True:
        cm = g.hold(body)
        try:
            cm.__enter__()
        except ServiceBusy:
            break
        held.append(cm)
    # 9 x 58 KiB = 522 KiB > 512 KiB: exactly 8 fit, the 9th is busy at once
    assert len(held) == 8
    assert g.available == 512 * 1024 - 8 * body
    # a tiny body still fits in the remainder (48 KiB left >= 8 KiB floor) ...
    with g.hold(20):
        assert g.available == 512 * 1024 - 8 * body - 8192
        # ... but a max-size one does not: it needs the whole budget
        with pytest.raises(ServiceBusy):
            with g.hold(1024 * 1024):
                pass
    for cm in held:
        cm.__exit__(None, None, None)
    assert g.available == 512 * 1024
    # released: a max-size body fits again
    with g.hold(1024 * 1024) as c:
        assert c == 512 * 1024


def test_n4_a_waiter_is_admitted_when_budget_frees_and_times_out_otherwise():
    from api import ScanAdmission, ServiceBusy

    g = ScanAdmission(budget_bytes=1000, min_cost_bytes=1, max_waiting=2, wait_s=0.5)
    first = g.hold(1000)
    first.__enter__()
    got = []

    def waiter():
        t = time.monotonic()
        with g.hold(600):
            got.append(time.monotonic() - t)

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.15)
    assert not got  # still waiting: nothing free
    first.__exit__(None, None, None)
    th.join(2)
    assert got and got[0] < 0.5
    # timeout: the budget stays taken past wait_s -> busy, and the waiter count is back to 0
    with g.hold(1000):
        t = time.monotonic()
        with pytest.raises(ServiceBusy):
            with g.hold(1):
                pass
        assert 0.4 <= time.monotonic() - t < 2
        assert g.waiting == 0


def _medium_body(kb: int = 58) -> bytes:
    per = 2000
    n = max(1, kb * 1024 // (per + 120))
    return json.dumps({"facts": [_fact(i, BENIGN_VALUE[:per]) for i in range(n)]}).encode()


@pytest.mark.parametrize("budget,min_cost,peak_lo,peak_hi", [
    (None, None, 1, 1),                # the defaults: one scan at a time, whatever the size
    (512 * 1024, 8 * 1024, 2, 8),      # a raised budget: 58 KB bodies share it, 9 x 58 KB > 512 KiB
])
def test_n4_in_process_forty_concurrent_58kb_bodies_never_scan_more_than_the_budget_at_once(monkeypatch, budget, min_cost,
                                                                                            peak_lo, peak_hi):
    # Instrument the scan itself: how many bodies are inside model_validate
    # at the same moment. With the step gate (64 KiB) all 40 were.
    svc = make_service(all_fakes=True)
    if budget is not None:
        svc.config = replace(svc.config, scan_inflight_bytes=budget, scan_min_cost_bytes=min_cost)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    body = _medium_body(58)
    assert 56 * 1024 <= len(body) <= 60 * 1024, len(body)
    budget = svc.config.scan_inflight_bytes
    lock, inside, peak = threading.Lock(), [0], [0]
    real = rq.FactsRequest.model_validate

    def slow_validate(payload, *a, **kw):
        with lock:
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
        try:
            time.sleep(0.05)
            return real(payload, *a, **kw)
        finally:
            with lock:
                inside[0] -= 1

    monkeypatch.setattr(rq.FactsRequest, "model_validate", slow_validate)

    def one(_):
        r = c.post("/onboarding/clients/client_a/intake/facts", content=body, headers={"Content-Type": "application/json"})
        return r.status_code

    with ThreadPoolExecutor(40) as ex:
        codes = Counter(ex.map(one, range(40)))
    assert peak_lo <= peak[0] <= peak_hi, (peak[0], codes)
    assert peak[0] <= max(1, budget // len(body)), (peak[0], codes)
    assert set(codes) <= {200, 503}, codes
    assert codes[200] >= max(1, budget // len(body)), codes


# =============================================================================
# wave-5 leftover — bounded facts per client and per field
# =============================================================================


def _facts(n: int, prefix: str, value: str = "v") -> dict:
    return {"facts": [{"field": f"{prefix}{i}", "value": value, "provenance": "client_stated", "evidence": "e",
                       "observed_at": "2026-09-01T12:00:00Z"} for i in range(n)]}


def test_w5l_facts_beyond_the_cap_are_409_and_nothing_is_stored():
    svc = make_service(all_fakes=True)
    svc.config = replace(svc.config, max_facts_per_client=450)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    for k in range(2):
        assert c.post("/onboarding/clients/client_a/intake/facts", json=_facts(200, f"n{k}_")).status_code == 200
    rec = svc.clients["client_a"]
    assert len(rec.facts) == 400
    events_before = len(svc.ledger.events)
    r = c.post("/onboarding/clients/client_a/intake/facts", json=_facts(51, "over_"))
    assert r.status_code == 409, r.text
    j = r.json()
    assert j["max_facts_per_client"] == 450 and j["facts_stored"] == 400 and j["facts_in_request"] == 51
    assert "cap is 450" in j["detail"] and "nothing was stored" in j["detail"]
    assert len(rec.facts) == 400 and len(svc.ledger.events) == events_before
    # exactly up to the cap is fine
    assert c.post("/onboarding/clients/client_a/intake/facts", json=_facts(50, "fit_")).status_code == 200
    assert len(rec.facts) == 450
    assert c.post("/onboarding/clients/client_a/intake/facts", json=_facts(1, "one_")).status_code == 409
    # the client is not stuck: restating a known field replaces history, it does not add
    assert len(rec.facts) == 450


def test_w5l_each_field_keeps_only_its_latest_observations_and_the_profile_is_right():
    svc = make_service(all_fakes=True)
    svc.config = replace(svc.config, facts_history_per_field=5)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    for i in range(12):
        body = {"facts": [{"field": "business_name", "value": f"Acme v{i}", "provenance": "client_stated",
                           "evidence": "call", "observed_at": f"2026-09-{i + 1:02d}T12:00:00Z"}]}
        assert c.post("/onboarding/clients/client_a/intake/facts", json=body).status_code == 200
    rec = svc.clients["client_a"]
    assert [f.value for f in rec.facts] == [f"Acme v{i}" for i in range(7, 12)]
    assert rec.profile.fields["business_name"].value == "Acme v11"
    assert rec.profile.fields["business_name"].conflict is True  # five distinct client-stated values
    # a confirmed value newer than the retained history resolves it
    body = {"facts": [{"field": "business_name", "value": "Acme Widgets", "provenance": "client_confirmed",
                       "evidence": "confirmed", "observed_at": "2026-09-20T12:00:00Z"}]}
    assert c.post("/onboarding/clients/client_a/intake/facts", json=body).status_code == 200
    assert rec.profile.fields["business_name"].value == "Acme Widgets"
    assert rec.profile.fields["business_name"].conflict is False
    assert len(rec.facts) == 5


def test_w5l_repeated_large_submissions_do_not_grow_memory_or_rebuild_cost_without_bound():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    cap = 2000  # the documented default; asserted against the config at the end
    rec = svc.clients["client_a"]
    sizes, cpu = [], []
    from service import OnboardingError

    # 30 x 200 facts with distinct field names (the AEGIS flood shape): the
    # first 10 fill the cap, the other 20 are refused. The service is called
    # directly so thread_time is the operation's own CPU (the TestClient runs
    # the app on another thread).
    for k in range(30):
        req = rq.FactsRequest.model_validate(_facts(200, f"note{k}_", "x" * 200))
        t = time.thread_time()
        try:
            svc.add_facts("client_a", req)
            status = 200
        except OnboardingError as exc:
            status = exc.status_code
        cpu.append(time.thread_time() - t)
        assert status == (200 if k < 10 else 409), (k, status)
        assert len(rec.facts) <= cap
        sizes.append(len(rec.facts))
    # and through the API the refusal is a 409 with the message
    r = c.post("/onboarding/clients/client_a/intake/facts", json=_facts(1, "late_"))
    assert r.status_code == 409 and "cap is 2000" in r.json()["detail"], r.text
    assert sizes[9:] == [cap] * 21
    assert len(rec.profile.dropped_fields) == cap
    assert svc.config.max_facts_per_client == cap
    # the accepted requests' cost is bounded: the 10th (profile over 2,000
    # facts) costs at most a small multiple of the 1st (over 200)
    assert cpu[9] < 4 * cpu[0] + 0.05, cpu[:10]
    # refused requests are cheap: no scan, no rebuild
    assert statistics.median(cpu[10:]) < cpu[0], (statistics.median(cpu[10:]), cpu[0])


def test_w5l_caps_are_configured_validated_and_documented():
    from config import load_config

    cfg = load_config({"ONBOARDING_MAX_FACTS_PER_CLIENT": "300", "ONBOARDING_FACTS_HISTORY_PER_FIELD": "7"})
    assert (cfg.max_facts_per_client, cfg.facts_history_per_field) == (300, 7)
    assert (OnboardingConfig().max_facts_per_client, OnboardingConfig().facts_history_per_field) == (2000, 20)
    for bad in ({"max_facts_per_client": 0}, {"facts_history_per_field": 0}):
        with pytest.raises(Exception):
            OnboardingConfig(**bad)
    readme = (ROOT / "README.md").read_text()
    assert "ONBOARDING_MAX_FACTS_PER_CLIENT" in readme and "ONBOARDING_FACTS_HISTORY_PER_FIELD" in readme
    assert "ONBOARDING_SCAN_INFLIGHT_BYTES" in readme and "ONBOARDING_HEAVY_BODY_BYTES" not in readme


# =============================================================================
# live: python3 -m api against the REAL ledger-rust
# =============================================================================


class RealStack:
    def __init__(self, ledger_bin: Path, ledger_env: dict | None = None, ledger_url: str | None = None):
        self.tmp = tempfile.TemporaryDirectory(prefix="onb-w6-")
        self.ledger = self.api = None
        self.out = ""
        # Fix wave 22 (G3, N21-C-6): a failure (or a skip: no free port) at ANY step after the ledger started stops
        # what was started; before, only the API's own health wait did, and the ledger could be left running.
        try:
            self.lport = free_test_port()
            lenv = dict(os.environ, LEDGER_SERVICE_TOKEN=LEDGER_TOKEN, LEDGER_PORT=str(self.lport),
                        LEDGER_LOG_PATH=str(Path(self.tmp.name) / "ledger.jsonl"), **(ledger_env or {}))
            self.ledger = subprocess.Popen([str(ledger_bin)], env=lenv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _wait_health(self.lport, self.ledger, "ledger-rust")
            self.port = free_test_port()
            aenv = {k: v for k, v in os.environ.items() if not k.startswith(("LEDGER_", "DETECTION_"))}
            aenv.update({"ONBOARDING_SERVICE_TOKEN": TEST_SERVICE_TOKEN, "ONBOARDING_PORT": str(self.port),
                         "LEDGER_SERVICE_URL": ledger_url or f"http://127.0.0.1:{self.lport}", "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN,
                         "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
            self.api = subprocess.Popen([sys.executable, "-m", "api"], cwd=str(SRC), env=aenv, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
            _wait_health(self.port, self.api, "onboarding-py")
        except BaseException:
            self.close()
            raise
        self.base = f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        if self.api is not None:
            self.out = _stop(self.api)
        if self.ledger is not None:
            _stop(self.ledger)
        self.tmp.cleanup()


@pytest.fixture(scope="module")
def real_stack(ledger_bin):
    s = RealStack(ledger_bin)
    yield s
    s.close()


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def test_n4_live_forty_concurrent_58kb_bodies_keep_health_and_light_gets_fast(real_stack):
    base = real_stack.base
    c = httpx.Client(base_url=base, headers=AUTH, timeout=60)
    assert c.post("/onboarding/clients", json=start_body("flood_a")).status_code == 201
    body = _medium_body(58)
    r = c.post("/onboarding/clients/flood_a/intake/facts", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    stop = threading.Event()
    codes = Counter()
    hl, ll = [], []

    def flood():
        with httpx.Client(base_url=base, headers=AUTH, timeout=120) as cc:
            while not stop.is_set():
                try:
                    r = cc.post("/onboarding/clients/flood_a/intake/facts", content=body,
                                headers={"Content-Type": "application/json"})
                    codes[r.status_code] += 1
                    if r.status_code == 503:
                        stop.wait(min(2.0, float(r.headers.get("Retry-After", "1"))))
                except httpx.HTTPError as exc:
                    codes[type(exc).__name__] += 1

    def probe():
        with httpx.Client(base_url=base, headers=AUTH, timeout=30) as cc:
            while not stop.is_set():
                t = time.monotonic()
                codes["health_" + str(cc.get("/health").status_code)] += 1
                hl.append(time.monotonic() - t)
                t = time.monotonic()
                codes["light_" + str(cc.get("/onboarding/clients/flood_a").status_code)] += 1
                ll.append(time.monotonic() - t)
                stop.wait(0.2)

    threads = [threading.Thread(target=flood) for _ in range(40)] + [threading.Thread(target=probe)]
    for t in threads:
        t.start()
    time.sleep(1.0)  # let the flood build up before measuring
    hl.clear(), ll.clear()
    time.sleep(8.0)
    stop.set()
    for t in threads:
        t.join(120)
    summary = (f"codes={dict(codes)} /health p50={_pct(hl, .5) * 1000:.0f}ms p95={_pct(hl, .95) * 1000:.0f}ms "
               f"max={max(hl) * 1000:.0f}ms; light GET p50={_pct(ll, .5) * 1000:.0f}ms p95={_pct(ll, .95) * 1000:.0f}ms "
               f"max={max(ll) * 1000:.0f}ms")
    print(summary)
    # the finding's numbers: /health max 2.4 s, light GET p50 3.1 s
    assert _pct(hl, .5) < 0.5 and _pct(hl, .95) < 0.5, summary
    assert _pct(ll, .5) < 1.0, summary
    assert len(hl) >= 10 and len(ll) >= 10, summary
    assert codes[200] > 0 and 422 not in codes and not any(isinstance(k, str) and k.endswith("Error") for k in codes), summary
    assert set(k for k in codes if isinstance(k, int)) <= {200, 503}, summary


def _hold_ledger_connections(port: int, n: int) -> list[socket.socket]:
    socks = []
    for _ in range(n):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        socks.append(s)
    return socks


@pytest.fixture(scope="module")
def shed_stack(ledger_bin):
    s = RealStack(ledger_bin, ledger_env={"LEDGER_MAX_CONNECTIONS": "1"})
    yield s
    s.close()


def test_n6_live_ledger_rust_shed_503_is_reported_as_not_recorded(shed_stack):
    c = httpx.Client(base_url=shed_stack.base, headers=AUTH, timeout=30)
    # the ledger's one connection slot is held by an idle socket -> every
    # further connection gets shed() : 503 + exact body, request never read
    held = _hold_ledger_connections(shed_stack.lport, 1)
    try:
        time.sleep(0.2)
        probe = httpx.get(f"http://127.0.0.1:{shed_stack.lport}/health", timeout=5)
        assert probe.status_code == 503 and probe.json() == LEDGER_SHED_BODY, probe.text
        r = c.post("/onboarding/clients", json=start_body("shed_a"))
        assert r.status_code == 503, r.text
        j = r.json()
        assert j["proceeded"] is False and j["ledger_write"] == "not_recorded", j
        assert "shed" in j["detail"] and "did not proceed" in j["detail"], j
        assert c.get("/onboarding/clients/shed_a").status_code == 404  # nothing committed
    finally:
        for s in held:
            s.close()
    time.sleep(0.5)
    # the slot is free again: the identical retry proceeds
    r = c.post("/onboarding/clients", json=start_body("shed_a"))
    assert r.status_code == 201, r.text


class _Proxy503(threading.Thread):
    """An intermediary in front of the real ledger that forwards the request
    and then answers 503 with ITS OWN body (a gateway timeout page)."""

    def __init__(self, upstream_port: int):
        super().__init__(daemon=True)
        self.port = free_test_port()
        self.upstream = upstream_port
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", self.port))
        self.srv.listen(16)
        self.forwarded = 0
        self.stop = threading.Event()

    def run(self):
        self.srv.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        with conn:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            m = re.search(rb"content-length:\s*(\d+)", head, re.I)
            need = int(m.group(1)) if m else 0
            while len(rest) < need:
                rest += conn.recv(65536)
            # forward to the real ledger, read its answer, throw it away
            with socket.create_connection(("127.0.0.1", self.upstream), timeout=5) as up:
                up.sendall(head + b"\r\n\r\n" + rest)
                up.settimeout(5)
                try:
                    while up.recv(65536):
                        pass
                except socket.timeout:
                    pass
            self.forwarded += 1
            body = b'{"error":"upstream timed out"}'
            conn.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nConnection: close\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)


@pytest.fixture(scope="module")
def proxied_stack(ledger_bin):
    # Fix wave 22 (G3, AEGIS N21-C-6): everything is started INSIDE one try/finally. Before, the ledger was started
    # first and the rest outside any try: the API port's free_test_port() skipped (a narrow range whose ports sat in
    # TIME_WAIT) and left ledger-rust running for good (the round-21 review found one still up hours later).
    tmp = tempfile.TemporaryDirectory(prefix="onb-w6p-")
    ledger = api = proxy = None
    try:
        lport = free_test_port()
        lenv = dict(os.environ, LEDGER_SERVICE_TOKEN=LEDGER_TOKEN, LEDGER_PORT=str(lport),
                    LEDGER_LOG_PATH=str(Path(tmp.name) / "ledger.jsonl"))
        ledger = subprocess.Popen([str(ledger_bin)], env=lenv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _wait_health(lport, ledger, "ledger-rust")
        proxy = _Proxy503(lport)
        proxy.start()
        port = free_test_port()
        aenv = {k: v for k, v in os.environ.items() if not k.startswith(("LEDGER_", "DETECTION_"))}
        aenv.update({"ONBOARDING_SERVICE_TOKEN": TEST_SERVICE_TOKEN, "ONBOARDING_PORT": str(port),
                     "LEDGER_SERVICE_URL": f"http://127.0.0.1:{proxy.port}", "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN,
                     "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        api = subprocess.Popen([sys.executable, "-m", "api"], cwd=str(SRC), env=aenv, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
        _wait_health(port, api, "onboarding-py")
        yield {"base": f"http://127.0.0.1:{port}", "lport": lport, "proxy": proxy, "log": Path(tmp.name) / "ledger.jsonl"}
    finally:
        if api is not None:
            _stop(api)
        if proxy is not None:
            proxy.stop.set()
        if ledger is not None:
            _stop(ledger)
        tmp.cleanup()


def test_n6_live_non_shed_503_from_an_intermediary_is_unknown_and_the_ledger_did_record(proxied_stack):
    base, proxy = proxied_stack["base"], proxied_stack["proxy"]
    c = httpx.Client(base_url=base, headers=AUTH, timeout=30)
    r = c.post("/onboarding/clients", json=start_body("gw_a"))
    assert r.status_code == 503, r.text
    j = r.json()
    assert j["proceeded"] == "unknown" and j["ledger_write"] == "unknown", j
    assert "may or may not have been recorded" in j["detail"] and "retry" in j, j
    assert proxy.forwarded >= 1
    # The intermediary's 503 hid a real append: the ledger holds the event.
    entries = httpx.get(f"http://127.0.0.1:{proxied_stack['lport']}/ledger/entries",
                        headers={"Authorization": f"Bearer {LEDGER_TOKEN}"}, timeout=10)
    assert entries.status_code == 200
    assert "gw_a" in entries.text, entries.text[:500]
    # so "did not proceed" would have been a lie; the state stayed uncommitted
    assert c.get("/onboarding/clients/gw_a").status_code == 404
