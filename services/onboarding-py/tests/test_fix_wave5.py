"""Fix wave 5 (Sep 24 2026) — AEGIS round-4 findings on onboarding-py.

NEW-2  MED  legitimate large bodies were refused (422 scan_budget_exceeded)
            under light concurrency: the scan budget was WALL-CLOCK and every
            request shares the GIL. Now the budget is the request thread's
            own CPU time, heavy scans are bounded by a semaphore with a
            bounded queue (busy -> 503 + Retry-After, never 422), and the
            redundant second scan of every clean string is gone.
NEW-3  MED  unbounded unauthenticated headers and no idle / partial-head
            timeouts: ``python3 -m api`` now runs the hardened launcher
            (src/serve.py: h11, 16 KiB head cap, head deadline from connect
            and after every response, keep-alive timeout, limit_concurrency),
            and the body has a read deadline (408).
NEW-4  LOW  "did not proceed" was reported when the ledger had committed the
            record and only the reply was lost. A write whose outcome is
            unknown (timeout / reset after sending, 5xx other than 503, 409)
            is now reported as 503 ``proceeded: "unknown"`` with an
            instruction to retry the identical request.
LOW-E       a date of birth after the server's date was accepted.
"""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from _procinfo import rss_kib

import redaction
from config import OnboardingConfig, load_config
from conftest import TEST_SERVICE_TOKEN, client_for, free_test_port, make_service, start_body
from ledger import HttpLedgerClient, LedgerWriteError

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
AUTH = {"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}
LEDGER_TOKEN = "wave5-ledger-test-token"


# =============================================================================
# helpers: real processes on real sockets
# =============================================================================


def _wait_health(port: int, proc: subprocess.Popen, what: str) -> None:
    for _ in range(150):
        if proc.poll() is not None:
            raise AssertionError(f"{what} exited: {proc.stdout.read() if proc.stdout else ''}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    proc.kill()
    raise AssertionError(f"{what} did not start")


def _stop(proc: subprocess.Popen) -> str:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out or ""


class Stack:
    """A fake ledger (tools/fake_ledger_server.py, validates like ledger-rust)
    and onboarding-py started exactly as the README says: ``python3 -m api``."""

    def __init__(self, **env):
        self.ledger = self.api = None
        self.out = ""
        # Fix wave 22 (G3, N21-C-6): a failure or skip at any step after the ledger started stops what was started.
        try:
            self.lport = free_test_port()
            lenv = dict(os.environ, LEDGER_SERVICE_TOKEN=LEDGER_TOKEN)
            self.ledger = subprocess.Popen([sys.executable, str(ROOT / "tools" / "fake_ledger_server.py"), "--port", str(self.lport)],
                                           env=lenv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # the ledger listens before the api's port is chosen, so they differ
            _wait_health(self.lport, self.ledger, "fake ledger")
            self.port = free_test_port()
            aenv = {k: v for k, v in os.environ.items() if not k.startswith(("LEDGER_", "DETECTION_"))}
            aenv.update({"ONBOARDING_SERVICE_TOKEN": TEST_SERVICE_TOKEN, "ONBOARDING_PORT": str(self.port),
                         "LEDGER_SERVICE_URL": f"http://127.0.0.1:{self.lport}", "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN,
                         "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", **env})
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

    def pid(self) -> int:
        return self.api.pid


# Fix wave 16: portable (Linux /proc, macOS/BSD ps), server pid only; and it
# raises instead of returning 0 when the process is gone (0 made a vanished
# server look like "memory stayed flat").
_rss_kb = rss_kib


def _fact(i: int, value: str) -> dict:
    return {"field": f"note_{i}", "value": value, "provenance": "client_stated", "evidence": "call notes",
            "observed_at": "2026-09-01T12:00:00Z"}


# The AEGIS reproduction: 200 facts of 2,000 characters (the per-request
# maximum for facts), ~416 KB of JSON, entirely benign.
BENIGN_VALUE = ("Our team sells widgets and ships daily to customers in many states. " * 30)[:2000]
MAX_FACTS = {"facts": [_fact(i, BENIGN_VALUE) for i in range(200)]}
# Mixed-language / non-ASCII benign text (exercises the non-ASCII path).
BENIGN_NON_ASCII = ("Vendemos widgets en México y enviamos a diario — café, jalapeño, naïve résumé. " * 30)[:2000]
MAX_FACTS_NON_ASCII = {"facts": [_fact(i, BENIGN_NON_ASCII) for i in range(200)]}
MAX_HTML = {"html": "<html><body>" + ("<p>Widgets shipped daily to happy customers in every state.</p>" * 7_900)[:499_000]
            + "</body></html>"}


# =============================================================================
# NEW-2 — the scan budget measures the request's own work, not contention
# =============================================================================


def _burn(seconds: float) -> None:
    end = time.thread_time() + seconds
    x = 0
    while time.thread_time() < end:
        x += 1


def test_new2_scan_budget_counts_the_threads_own_cpu_time_not_wall_time():
    # Waiting (for the GIL, for I/O, for anything) is not the request's work.
    with redaction.scan_budget(0.05):
        time.sleep(0.2)
        assert redaction.find_credential("hello there") is None
    # Burning the request thread's own CPU past the budget is refused.
    with pytest.raises(redaction.ScanBudgetExceeded):
        with redaction.scan_budget(0.05):
            _burn(0.1)
            redaction.find_credential("hello there")


def test_new2_other_threads_cpu_does_not_count_against_a_request():
    # Contention: while another thread burns CPU (holding the GIL most of
    # the time), a request's budget is not spent.
    stop = threading.Event()

    def hog():
        while not stop.is_set():
            _burn(0.01)

    hogs = [threading.Thread(target=hog) for _ in range(4)]
    for h in hogs:
        h.start()
    try:
        with redaction.scan_budget(0.2):
            t0 = time.monotonic()
            while time.monotonic() - t0 < 0.6:  # wall time well past the budget
                redaction.find_credential("hello there")
                time.sleep(0.01)
    finally:
        stop.set()
        for h in hogs:
            h.join()


def test_new2_a_clean_string_is_scanned_once_per_request(monkeypatch):
    # The ingest check (refuse) and the second layer (scrub) used to scan
    # every clean string twice; within one request the verdict is reused.
    from onboarding_schema import requests as rq

    calls = Counter()
    real = redaction._find_credential_uncached

    def spy(text):
        calls[text] += 1
        return real(text)

    monkeypatch.setattr(redaction, "_find_credential_uncached", spy)
    with redaction.scan_budget(30):
        rq.FactsRequest.model_validate({"facts": [_fact(0, BENIGN_VALUE), _fact(1, BENIGN_VALUE)]})
    assert calls[BENIGN_VALUE] == 1, calls[BENIGN_VALUE]


def test_new2_max_size_benign_body_is_cheap_enough():
    # The measured cost of the AEGIS body was 1.56 s; the redundant pass and
    # the per-character format strip are gone. Measured after the fix:
    # 0.73-0.74 s alone, 0.81 s inside the full suite (AEGIS round 12, N12-3,
    # 2 vCPU Xeon 2.80 GHz). Bound: 1.2 s of CPU (1.5x the in-suite worst),
    # which still fails on the 1.56 s it exists to catch.
    from onboarding_schema import requests as rq

    for body in (MAX_FACTS, MAX_FACTS_NON_ASCII):
        t = time.thread_time()
        with redaction.scan_budget(30):
            rq.FactsRequest.model_validate(body)
        assert time.thread_time() - t < 1.2, time.thread_time() - t


def test_new2_scan_config_is_validated_and_loaded():
    # Fix wave 6 (N4): the step gate's knobs became the weighted budget's.
    cfg = load_config({"ONBOARDING_SCAN_BUDGET_SECONDS": "7.5", "ONBOARDING_SCAN_INFLIGHT_BYTES": "1000",
                       "ONBOARDING_SCAN_MIN_COST_BYTES": "3", "ONBOARDING_SCAN_MAX_WAITING": "4",
                       "ONBOARDING_SCAN_WAIT_SECONDS": "2.5", "ONBOARDING_BODY_READ_TIMEOUT_SECONDS": "9"})
    assert (cfg.scan_budget_seconds, cfg.scan_inflight_bytes, cfg.scan_min_cost_bytes, cfg.scan_max_waiting,
            cfg.scan_wait_seconds, cfg.body_read_timeout_seconds) == (7.5, 1000, 3, 4, 2.5, 9.0)
    for bad in ({"scan_min_cost_bytes": 0}, {"scan_max_waiting": -1}, {"scan_wait_seconds": 0},
                {"body_read_timeout_seconds": 0}, {"scan_inflight_bytes": 0}):
        with pytest.raises(Exception):
            OnboardingConfig(**bad)


def test_new2_busy_is_503_with_retry_after_and_never_422():
    # Nobody may wait: while the scan budget is (almost) all taken, a body
    # that does not fit is answered 503 + Retry-After ("busy"), with
    # proceeded: false. (Fix wave 6, N4: the gate is a weighted budget; with
    # a budget above the per-body minimum the test holds all but a sliver of
    # it — a light body still fits, a heavy one does not.)
    import api as api_mod

    svc = make_service(all_fakes=True)
    svc.config = replace(svc.config, scan_max_waiting=0, scan_inflight_bytes=512 * 1024, scan_min_cost_bytes=8 * 1024)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    gate = c.app.state.scan_admission
    assert gate.try_hold_for_test(gate.budget - 2 * gate.min_cost)  # room for a light body, not a heavy one
    try:
        r = c.post("/onboarding/clients/client_a/intake/facts", json=MAX_FACTS)
        assert r.status_code == 503, r.text
        assert int(r.headers["Retry-After"]) >= 1
        assert r.json()["proceeded"] is False and "busy" in r.json()["detail"]
        # a light body does not queue behind heavy scans: it fits in what is left
        assert c.post("/onboarding/clients/client_a/messages", json={"text": "hello"}).status_code == 200
    finally:
        gate.release_for_test()
    assert c.post("/onboarding/clients/client_a/intake/facts", json=MAX_FACTS).status_code == 200
    assert api_mod  # noqa


def test_new2_queue_wait_limit_is_503_not_422():
    svc = make_service(all_fakes=True)
    svc.config = replace(svc.config, scan_max_waiting=4, scan_wait_seconds=0.3)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    gate = c.app.state.scan_admission
    assert gate.try_hold_for_test()  # the whole budget
    try:
        t = time.monotonic()
        r = c.post("/onboarding/clients/client_a/intake/facts", json=MAX_FACTS)
        assert r.status_code == 503 and "Retry-After" in r.headers, r.text
        assert 0.25 <= time.monotonic() - t < 5
    finally:
        gate.release_for_test()


@pytest.fixture(scope="module")
def stack():
    s = Stack()
    yield s
    s.close()


def test_new2_real_uvicorn_12_concurrent_max_size_benign_bodies_all_succeed(stack):
    c = httpx.Client(base_url=stack.base, headers=AUTH, timeout=120)
    assert c.post("/onboarding/clients", json=start_body("conc_a")).status_code == 201
    bodies = [MAX_FACTS, MAX_FACTS_NON_ASCII, MAX_HTML] * 4

    def one(i):
        path = "/onboarding/clients/conc_a/" + ("access/website-scan" if "html" in bodies[i] else "intake/facts")
        with httpx.Client(base_url=stack.base, headers=AUTH, timeout=120) as cc:
            r = cc.post(path, json=bodies[i])
            return r.status_code, r.headers.get("Retry-After"), r.text[:200]

    with ThreadPoolExecutor(12) as ex:
        res = list(ex.map(one, range(12)))
    statuses = Counter(s for s, _, _ in res)
    assert 422 not in statuses, res
    assert statuses == Counter({200: 12}), res


def test_new2_real_uvicorn_hostile_bodies_are_still_refused_quickly(stack):
    c = httpx.Client(base_url=stack.base, headers=AUTH, timeout=60)
    assert c.post("/onboarding/clients", json=start_body("host_a")).status_code == 201
    # a max-size body whose LAST string carries a credential: refused 422
    facts = [_fact(i, BENIGN_VALUE) for i in range(199)] + [_fact(199, "the shopify password is Tangerine!42")]
    t = time.monotonic()
    r = c.post("/onboarding/clients/host_a/intake/facts", json={"facts": facts})
    assert r.status_code == 422 and time.monotonic() - t < 5, (r.status_code, r.text[:200])
    # adversarial max-size text (the round-3 pathological shapes): answered
    # quickly (credential refusal, a check, or the CPU budget), never hung
    for unit in ("a@", "login=", "user:", "eyJ-"):
        t = time.monotonic()
        r = c.post("/onboarding/clients/host_a/access/website-scan", json={"html": unit * (499_000 // len(unit))})
        assert r.status_code in (200, 422) and time.monotonic() - t < 10, (unit, r.status_code, r.text[:200])


# =============================================================================
# NEW-3 — hardened launcher: head cap, head deadline, idle timeout, body deadline
# =============================================================================


@pytest.fixture(scope="module")
def fast_stack():
    s = Stack(ONBOARDING_REQUEST_HEAD_TIMEOUT_SECONDS="2", ONBOARDING_BODY_READ_TIMEOUT_SECONDS="2")
    yield s
    s.close()


def _read_status(sock: socket.socket, timeout: float = 10) -> int:
    sock.settimeout(timeout)
    data = b""
    try:
        while b"\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    except (ConnectionResetError, socket.timeout):
        return 0
    return int(data.split(b" ", 2)[1]) if data.startswith(b"HTTP/") else 0


def _closed_within(sock: socket.socket, bound: float) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < bound:
        r, _, _ = select.select([sock], [], [], 0.2)
        if r:
            try:
                if sock.recv(65536) == b"":
                    return time.monotonic() - t0
            except OSError:
                return time.monotonic() - t0
    return None


def test_new3_huge_header_is_refused_and_not_buffered(fast_stack):
    pid = fast_stack.pid()
    before = _rss_kb(pid)
    s = socket.create_connection(("127.0.0.1", fast_stack.port), timeout=10)
    s.sendall(b"GET /health HTTP/1.1\r\nHost: a\r\nX-A: ")
    sent = 0
    try:
        for _ in range(100):  # up to 100 MiB, as in the AEGIS probe
            s.sendall(b"a" * (1 << 20))
            sent += 1
    except OSError:
        pass
    status = _read_status(s, 5)
    s.close()
    grew = _rss_kb(pid) - before
    assert status != 200
    assert sent < 100, "the server kept reading a 100 MiB header"
    assert grew < 20_000, f"RSS grew {grew} KB while a huge header was sent"


def test_new3_header_block_just_over_16_kib_is_refused_and_normal_one_served(fast_stack):
    with socket.create_connection(("127.0.0.1", fast_stack.port), timeout=10) as s:
        s.sendall(b"GET /health HTTP/1.1\r\nHost: a\r\nX-A: " + b"a" * (17 * 1024) + b"\r\n\r\n")
        assert _read_status(s) != 200
    with socket.create_connection(("127.0.0.1", fast_stack.port), timeout=10) as s:
        s.sendall(b"GET /health HTTP/1.1\r\nHost: a\r\nX-A: " + b"a" * (8 * 1024) + b"\r\n\r\n")
        assert _read_status(s) == 200


@pytest.mark.parametrize("opening", [b"", b"GET /health HTTP/1.1\r\nHost: x\r\n", b"G"])
def test_new3_idle_and_partial_head_connections_are_closed(fast_stack, opening):
    with socket.create_connection(("127.0.0.1", fast_stack.port), timeout=10) as s:
        if opening:
            s.sendall(opening)
        closed = _closed_within(s, 8)
    assert closed is not None and closed <= 4.5, f"connection still open (opening={opening!r})"


def test_new3_trickled_head_is_cut_off_at_the_deadline(fast_stack):
    with socket.create_connection(("127.0.0.1", fast_stack.port), timeout=10) as s:
        s.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nX-Slow: ")
        t0 = time.monotonic()
        closed = None
        while time.monotonic() - t0 < 10:
            try:
                s.sendall(b"a")
            except OSError:
                closed = time.monotonic() - t0
                break
            r, _, _ = select.select([s], [], [], 0.3)
            if r:
                try:
                    if s.recv(4096) == b"":
                        closed = time.monotonic() - t0
                        break
                except OSError:
                    closed = time.monotonic() - t0
                    break
        assert closed is not None and closed <= 4.5, closed


def test_new3_trickled_body_is_answered_408_at_the_body_deadline(fast_stack):
    with socket.create_connection(("127.0.0.1", fast_stack.port), timeout=10) as s:
        s.sendall((f"POST /onboarding/clients/x/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TEST_SERVICE_TOKEN}\r\n"
                   "Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n{").encode())
        t0 = time.monotonic()
        status = _read_status(s, 8)
        took = time.monotonic() - t0
    assert status == 408 and took < 5, (status, took)


def test_new3_keep_alive_and_pipelining_still_work_and_idle_keep_alive_closes(fast_stack):
    req = b"GET /health HTTP/1.1\r\nHost: t\r\n\r\n"
    with socket.create_connection(("127.0.0.1", fast_stack.port), timeout=5) as s:
        s.sendall(req)
        assert _read_status(s) == 200
        time.sleep(1)
        s.sendall(req + req)
        data = b""
        while data.count(b"HTTP/1.1 200") < 2:
            chunk = s.recv(65536)
            assert chunk, data
            data += chunk
        # then idle: closed by the keep-alive / head deadline
        assert _closed_within(s, 8) is not None


def test_new3_launcher_uses_h11_and_limits():
    import serve

    assert serve.MAX_HEADER_BYTES == 16 * 1024
    assert serve.REQUEST_HEAD_TIMEOUT_S > 0 and serve.KEEP_ALIVE_TIMEOUT_S > 0 and serve.LIMIT_CONCURRENCY > 12
    kw = serve.uvicorn_kwargs()
    assert kw["h11_max_incomplete_event_size"] == 16 * 1024
    assert kw["limit_concurrency"] == serve.LIMIT_CONCURRENCY
    assert kw["http"] is serve.HeadDeadlineH11Protocol


def test_new3_health_stays_responsive_while_idle_sockets_are_held(fast_stack):
    socks = [socket.create_connection(("127.0.0.1", fast_stack.port), timeout=5) for _ in range(20)]
    try:
        for _ in range(3):
            t = time.monotonic()
            assert httpx.get(fast_stack.base + "/health", timeout=3).status_code == 200
            assert time.monotonic() - t < 1
        time.sleep(3.5)
        still_open = 0
        for s in socks:
            s.setblocking(False)
            try:
                if s.recv(1) != b"":
                    still_open += 1
            except BlockingIOError:
                still_open += 1
            except OSError:
                pass
        assert still_open == 0, f"{still_open}/20 idle sockets still open after the head deadline"
    finally:
        for s in socks:
            s.close()


# =============================================================================
# NEW-4 — a write whose outcome is unknown is never reported as "did not proceed"
# =============================================================================


def _client(handler) -> HttpLedgerClient:
    return HttpLedgerClient("http://ledger.test", "tok", transport=httpx.MockTransport(handler))


def _raise(exc):
    def handler(req):
        raise exc
    return handler


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ConnectTimeout("no connect"),
                                 httpx.PoolTimeout("pool")])
def test_new4_not_sent_is_certainly_not_recorded(exc):
    with pytest.raises(LedgerWriteError) as ei:
        _client(_raise(exc)).record_event("onb-1", "onboarding", "t", "a", "s", {}, "x")
    assert ei.value.outcome == "not_recorded"


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("lost"), httpx.ReadError("reset"), httpx.WriteError("reset"),
                                 httpx.WriteTimeout("slow"), httpx.RemoteProtocolError("closed without response")])
def test_new4_lost_after_sending_is_unknown(exc):
    with pytest.raises(LedgerWriteError) as ei:
        _client(_raise(exc)).record_event("onb-1", "onboarding", "t", "a", "s", {}, "x")
    assert ei.value.outcome == "unknown"


# Fix wave 6 (N6): a bare 503 (body ``{}``) is no longer "not recorded" —
# only ledger-rust's exact shed body is (test_fix_wave6.py); any other 503
# may come from an intermediary that already forwarded the request.
@pytest.mark.parametrize("status,outcome", [(400, "not_recorded"), (401, "not_recorded"), (403, "not_recorded"),
                                            (408, "not_recorded"), (413, "not_recorded"), (503, "unknown"),
                                            (409, "unknown"), (500, "unknown"), (502, "unknown"), (504, "unknown")])
def test_new4_status_classification(status, outcome):
    with pytest.raises(LedgerWriteError) as ei:
        _client(lambda req: httpx.Response(status, json={})).record_event("onb-1", "onboarding", "t", "a", "s", {}, "x")
    assert ei.value.outcome == outcome


def test_new4_local_validation_failure_is_certainly_not_recorded():
    with pytest.raises(LedgerWriteError) as ei:
        _client(lambda req: httpx.Response(201)).record_event("bad id!", "onboarding", "t", "a", "s", {}, "x")
    assert ei.value.outcome == "not_recorded"


class _LossyLedger:
    """Commits the event (like ledger-rust), then loses the reply the first
    ``lose`` times — exactly the AEGIS lossy-proxy reproduction."""

    def __init__(self, lose: int = 1, loss=httpx.ReadTimeout("reply lost")):
        self.store: dict[str, dict] = {}
        self.codes: list[int] = []
        self.lose = lose
        self.loss = loss

    def handler(self, req):
        body = json.loads(req.content)
        prev = self.store.get(body["event_id"])
        code = 201 if prev is None else (200 if prev == body else 409)
        if prev is None:
            self.store[body["event_id"]] = body
        self.codes.append(code)
        if self.lose > 0:
            self.lose -= 1
            raise self.loss
        return httpx.Response(code, json={})


@pytest.mark.parametrize("loss", [httpx.ReadTimeout("reply lost"), httpx.RemoteProtocolError("closed"),
                                  httpx.ReadError("reset")])
def test_new4_reply_lost_after_commit_is_unknown_then_the_retry_is_201_with_no_duplicate(loss):
    lossy = _LossyLedger(lose=1, loss=loss)
    svc = make_service(all_fakes=True, ledger=_client(lossy.handler))
    c = client_for(svc)
    r = c.post("/onboarding/clients", json=start_body())
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["proceeded"] == "unknown", body
    assert "did not proceed" not in body["detail"]
    assert "retry" in body["retry"].lower() and "identical" in body["retry"].lower()
    assert "Retry-After" in r.headers
    assert svc.clients == {}  # staged state stays uncommitted until the retry resolves it
    r2 = c.post("/onboarding/clients", json=start_body())
    assert r2.status_code == 201, r2.text
    assert lossy.codes[:2] == [201, 200]  # the retried identical event was the ledger's 200
    started = [b for b in lossy.store.values() if b["event_type"] == "onboarding_started"]
    assert len(started) == 1
    assert len(lossy.store) == len({b["event_id"] for b in lossy.store.values()})


def test_new4_unknown_on_a_later_operation_then_retry_resolves_it_without_duplicates():
    lossy = _LossyLedger(lose=0)
    svc = make_service(all_fakes=True, ledger=_client(lossy.handler))
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    n = len(lossy.store)
    lossy.lose = 1
    msg = {"text": "can you guarantee results?"}  # a guarantee request: its ruling is recorded
    r = c.post("/onboarding/clients/client_a/messages", json=msg)
    assert r.status_code == 503 and r.json()["proceeded"] == "unknown", r.text
    assert svc.memory.get("client_a", "messages", []) == []  # staged, not committed
    r = c.post("/onboarding/clients/client_a/messages", json=msg)
    assert r.status_code == 200 and r.json()["intent"] == "guarantee_request", r.text
    assert lossy.codes[-1] == 200  # the retry's event was already recorded: the ledger's 200
    new = list(lossy.store.values())[n:]
    assert [b["event_type"] for b in new] == ["client_intent_ruling"], new
    assert len(svc.memory.get("client_a", "messages", [])) == 2


def test_new4_certain_failure_still_says_did_not_proceed():
    svc = make_service(all_fakes=True, ledger=_client(_raise(httpx.ConnectError("refused"))))
    r = client_for(svc).post("/onboarding/clients", json=start_body())
    assert r.status_code == 503 and r.json()["proceeded"] is False, r.text


def test_new4_unknown_after_outside_effects_is_reported_as_partial_and_unknown():
    # The contract was stored (an outside effect); then a result record's
    # reply is lost. proceeded stays True (something did happen); the
    # record's own fate is reported as unknown, with the retry instruction.
    lossy = _LossyLedger(lose=0)
    real = lossy.handler

    def handler(req):
        if json.loads(req.content)["event_type"] == "contract_storage_ruling" and lossy.lose == 0 and not getattr(lossy, "done", False):
            lossy.done = True
            lossy.lose = 1
        return real(req)

    svc = make_service(all_fakes=True, ledger=_client(handler))
    c = client_for(svc)
    r = c.post("/onboarding/clients", json=start_body())
    assert r.status_code == 503, r.text
    b = r.json()
    assert b["proceeded"] is True and b["completed"] is False and "contract_storage_put" in b["outside_effects_done"]
    assert b["ledger_write"] == "unknown" and "identical" in b["retry"]
    r = c.post("/onboarding/clients", json=start_body())
    assert r.status_code == 201, r.text
    rulings = [x for x in lossy.store.values() if x["event_type"] == "contract_storage_ruling"]
    assert len(rulings) == 1


# =============================================================================
# LOW-E — a date of birth after the server's date is refused
# =============================================================================


def _app(**over):
    body = {"creator_id": "clip_1", "legal_name": "Casey Clipper", "date_of_birth": "2000-01-01",
            "follower_count": 20000, "avg_engagement_rate": 0.05, "fake_follower_ratio": 0.02,
            "content_history_posts": 120, "network_fit_tags": ["beauty"], "w9_received": True,
            "creator_agreement_signed": True, "disclosure_training_completed": True}
    body.update(over)
    return body


@pytest.mark.parametrize("dob", ["9999-12-31", "2027-01-01"])
def test_lowe_future_dob_is_422(dob):
    svc = make_service(all_fakes=True)
    r = client_for(svc).post("/zbc/creators/applications", json=_app(date_of_birth=dob))
    assert r.status_code == 422, r.text
    assert "future" in json.dumps(r.json())
    assert svc.ledger.events == [] and svc.creators == {}


def test_lowe_dob_tomorrow_on_the_servers_calendar_is_422_and_today_is_not():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    latest_today = svc.now().astimezone(timezone(timedelta(hours=14))).date()  # the latest calendar date on Earth
    r = c.post("/zbc/creators/applications", json=_app(date_of_birth=(latest_today + timedelta(days=1)).isoformat()))
    assert r.status_code == 422 and "future" in r.text, r.text
    r = c.post("/zbc/creators/applications", json=_app(creator_id="clip_2", date_of_birth=latest_today.isoformat()))
    assert r.status_code == 201, r.text  # born today somewhere: a real (under-18) date, declined by vetting
    assert r.json()["vetting"]["outcome"] == "decline"
