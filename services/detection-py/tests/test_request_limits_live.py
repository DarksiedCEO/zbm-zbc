"""
Request-size and event-loop limits, tested against a REAL uvicorn process on
a real socket (fix wave 1, Sep 24 2026 — finding "detection-py has no body
size limit and parses on the event loop").

The finding: every agent route parsed its body with
`model.model_validate_json(raw)` inside an `async def` dependency, i.e. ON
the event loop, after reading the whole body into memory with no limit. A
33 MB body held /health for 3.2 s. TestClient cannot show this (it has no
real concurrency), so these tests start the service exactly as the README
launches it (src/serve.py) and talk to it over TCP from several threads.

What must hold:
  - a body larger than the route's limit is refused with 413 before any parsing:
    immediately from Content-Length (no body byte has to be sent), and while
    streaming for a chunked body with no Content-Length;
  - /health answers within 1 s while a large valid batch and an oversized
    body are in flight;
  - LOW-C (fix wave 1): with 16 clients sending worst-case legal batches
    (~28 MiB each) at once, /health stays under HEALTH_BOUND_S, at most
    MAX_CONCURRENT_HEAVY batches run and the rest get 503 + Retry-After;
  - a request head larger than the header cap is refused, not buffered;
  - a stalled large request without a valid token is answered 401 at once
    and does not hold the heavy slot.

Ports: OS-assigned by default; DETECTION_LIVE_TEST_PORTS=20160-20169 (for
example) moves them to an assigned range. Either way a server counts as this
module's own only once its own stderr says it is running on that port (fix
wave 25, scout A D4 / R-HYGIENE L2: the default used to be the literal range
19960-19969, picked free and then trusted on any /health answer).
"""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from conftest import TEST_SERVICE_TOKEN

SRC = Path(__file__).resolve().parents[1] / "src"
_RANGE = os.environ.get("DETECTION_LIVE_TEST_PORTS", "").strip()
if _RANGE:
    _lo, _, _hi = _RANGE.partition("-")
    PORTS: range | None = range(int(_lo), int(_hi) + 1)
else:
    PORTS = None          # OS-assigned (fix wave 25)
MIB = 1024 * 1024
AUTH = f"Bearer {TEST_SERVICE_TOKEN}"


def _free_port() -> int:
    """A CANDIDATE port (another process can take it before the server binds it): callers accept the server only
    once it has announced its own bind on that port (`_announced`)."""
    if PORTS is None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
    for port in PORTS:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # fix wave 22 (G3): TIME_WAIT is free
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in {PORTS.start}-{PORTS.stop - 1}")


_SERVER_PID: dict[int, int] = {}  # port -> the pid of the server this module started on it (fix wave 25, D2)


def _announced(err, port: int) -> bool:
    """True once the server writing to the (unlinked temp) file `err` has logged its own bind on `port` — uvicorn
    logs it right after binding; an answer on a port picked free a moment earlier can be another process's."""
    return f"Uvicorn running on http://127.0.0.1:{port}".encode() in os.pread(err.fileno(), os.fstat(err.fileno()).st_size, 0)


@pytest.fixture(scope="module")
def server():
    for _attempt in range(5):           # a port lost to another process between the pick and the bind: another
        port = _free_port()
        env = {**os.environ, "ZBM_SERVICE_TOKEN": TEST_SERVICE_TOKEN}
        err = tempfile.TemporaryFile()  # unlinked at once: nothing is left behind
        proc = subprocess.Popen(
            [sys.executable, "serve.py", "--host", "127.0.0.1", "--port", str(port)],
            cwd=SRC, env=env, stdout=subprocess.DEVNULL, stderr=err,
        )
        try:
            deadline = time.monotonic() + 30
            while proc.poll() is None and not _announced(err, port):
                if time.monotonic() > deadline:
                    raise RuntimeError("detection-py did not start")
                time.sleep(0.05)
            if proc.poll() is None:
                status, _, _ = _request(port, "GET", "/health", timeout=10)
                assert status == 200, status
                break
        except BaseException:
            _stop_server(proc, err)
            raise
        _stop_server(proc, err)         # exited before it announced the bind (e.g. the port was taken)
    else:
        raise RuntimeError("detection-py could not bind a port in 5 attempts")
    _SERVER_PID[port] = proc.pid
    try:
        yield port
    finally:
        _stop_server(proc, err)


def _stop_server(proc: subprocess.Popen, err) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    err.close()


def _read_response(sock: socket.socket) -> tuple[int, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1]) if head else 0
    length = None
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b":")
        if k.strip().lower() == b"content-length":
            length = int(v.strip())
    while length is not None and len(body) < length:
        chunk = sock.recv(65536)
        if not chunk:
            break
        body += chunk
    return status, body


def _request(port: int, method: str, path: str, body: bytes | None = None,
             timeout: float = 30, headers: dict[str, str] | None = None) -> tuple[int, bytes, float]:
    t0 = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        hdrs = {"Host": "t", "Connection": "close", **(headers or {})}
        if body is not None:
            hdrs.setdefault("Content-Type", "application/json")
            hdrs["Content-Length"] = str(len(body))
        head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n"
        s.sendall(head.encode())
        if body is not None:
            # Send the body in pieces and stop as soon as the server answers
            # (a 413 is sent before the body is read, then the socket closes).
            view = memoryview(body)
            for i in range(0, len(body), 256 * 1024):
                if select.select([s], [], [], 0)[0]:
                    break
                try:
                    s.sendall(view[i:i + 256 * 1024])
                except (BrokenPipeError, ConnectionResetError):
                    break
        status, resp = _read_response(s)
    return status, resp, time.monotonic() - t0


# Fix wave 25 (scout A D2; FIX_WAVE_23b item 2: "find what the test measures"). The /health bounds below were
# measured by a thread of the pytest process — the process that also runs the 16 (or 2) sender threads — as plain
# wall time. On a 2-CPU box with 2 busy loops (R-LOAD) and other suites running beside it, the max went to 0.61 s
# (E-A dev run, load 5.4) while the documented bound is about the SERVER: its event loop must not wait behind the
# parse (the 1 ms switch interval, ADR 0001). That wall time also held (a) the prober waiting for the GIL behind its own
# sender threads and for a CPU, and (b) the server's event loop waiting for a CPU the busy loops held — neither is the
# service's latency. Now the prober is its own process (no sender threads beside it) and every probe reports, next to
# its wall time, the time the kernel kept the prober and the server's event-loop thread RUNNABLE but not running
# (/proc/<pid>/task/<tid>/schedstat, field 2) during it; the bound applies to wall minus the larger of those waits — the time the
# server had the CPU and still had not answered (its GIL waits behind the parse included). Where schedstat is not
# available (macOS) the waits are reported as 0: plain wall time, as before. Every raw number is printed.
_PROBER = r"""
import json, os, socket, sys, time
port, server_pid, seconds, every = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
def wait_s(pid, tid):
    try:
        with open(f"/proc/{pid}/task/{tid}/schedstat") as fh:
            return int(fh.read().split()[1]) / 1e9
    except (OSError, IndexError, ValueError):
        return None
me = os.getpid()
end = time.monotonic() + seconds
while time.monotonic() < end:
    w0, s0, t0 = wait_s(me, me), wait_s(server_pid, server_pid), time.monotonic()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        status = int(data.split(b" ", 2)[1]) if data else 0
    except OSError as exc:
        status = type(exc).__name__
    wall = time.monotonic() - t0
    w1, s1 = wait_s(me, me), wait_s(server_pid, server_pid)
    waits = [b - a for a, b in ((w0, w1), (s0, s1)) if a is not None and b is not None]
    print(json.dumps({"status": status, "wall": wall, "prober_wait": waits[0] if len(waits) > 0 else 0.0,
                      "server_wait": waits[1] if len(waits) > 1 else 0.0}), flush=True)
    time.sleep(every)
"""


def _probe_health(port: int, seconds: float, every: float = 0.05) -> list[dict]:
    """GET /health from a separate process for `seconds`; per probe: status, wall, and the run-queue waits of the
    prober and of the server's event-loop thread during it (see _PROBER)."""
    import json as _json
    out = subprocess.run([sys.executable, "-c", _PROBER, str(port), str(_SERVER_PID[port]), str(seconds), str(every)],
                         capture_output=True, text=True, timeout=seconds + 120,
                         env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert out.returncode == 0, out.stderr[-2000:]
    return [_json.loads(ln) for ln in out.stdout.splitlines() if ln.strip()]


def _service_latency(p: dict) -> float:
    """A probe's wall time minus the time the prober or the server's loop was kept off the CPU — the LARGER of the
    two waits only: they can overlap (both runnable, neither running), and subtracting their sum would credit an
    overlap twice (E-A review). Never subtracts more than the wall time one of them spent waiting for a CPU."""
    return max(0.0, p["wall"] - max(p["prober_wait"], p["server_wait"]))


def _order(i: int) -> dict:
    return {
        "order_id": f"ord_{i:06d}", "customer_id": f"cust_{i}", "placed_at": "2026-06-12T10:00:00Z",
        "status": "completed",
        "line_items": [{"sku": f"SKU-{i}-{k}", "unit_price_usd": "120.00", "quantity": 1} for k in range(3)],
        "discounts": [{"code": "AFF-EXTEND10", "percent_off": 10.0, "amount_off_usd": None}],
        "affiliate": {"affiliate_id": "aff_stale_3", "click_timestamp": "2026-06-01T09:00:00Z",
                      "order_timestamp": "2026-06-12T10:00:00Z", "attribution_window_hours": 24},
        "source_platform": "shopify", "recovery_attempted": False,
    }


def _orders_body(n: int) -> bytes:
    return json.dumps({"orders": [_order(i) for i in range(n)]}).encode()


DETECT = "/agents/affiliate-coupon-extension/detect"


def test_health_stays_responsive_while_large_and_oversized_bodies_are_in_flight(server):
    from api import MAX_BATCH_ITEMS, ROUTE_BODY_LIMITS
    large_valid = _orders_body(MAX_BATCH_ITEMS)   # a 1000-order batch
    oversized = _orders_body(70_000)              # ~44 MB: over the orders routes' limit
    assert len(oversized) > ROUTE_BODY_LIMITS[DETECT]

    results: dict[str, list] = {"valid": [], "oversized": []}
    stop = threading.Event()

    def hammer(kind: str, body: bytes):
        while not stop.is_set():
            results[kind].append(_request(server, "POST", DETECT, body, headers={"Authorization": AUTH}))

    threads = [threading.Thread(target=hammer, args=("valid", large_valid)),
               threading.Thread(target=hammer, args=("oversized", oversized))]
    for t in threads:
        t.start()
    try:
        probes = _probe_health(server, 4.0)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=60)

    worst = max(_service_latency(p) for p in probes)
    print(f"\n/health beside large and oversized bodies: n={len(probes)} max service latency {worst * 1000:.0f} ms "
          f"(max wall {max(p['wall'] for p in probes) * 1000:.0f} ms)")
    assert probes and all(p["status"] == 200 for p in probes), probes[:5]
    assert worst < 1.0, f"/health took {worst:.2f}s of the server's time while large bodies were in flight: {probes}"
    assert results["valid"] and results["oversized"]
    assert all(r[0] == 200 for r in results["valid"]), {r[0] for r in results["valid"]}
    assert all(r[0] == 413 for r in results["oversized"]), {r[0] for r in results["oversized"]}


# LOW-C (fix wave 1): the documented bound (ADR 0001 "Request limits") on
# /health latency while 16 clients send worst-case legal batches at once.
HEALTH_BOUND_S = 0.5


def test_health_latency_bound_under_16_concurrent_worst_case_batches(server):
    from api import MAX_CONCURRENT_HEAVY, ROUTE_BODY_LIMITS
    from test_body_limits import worst_body

    body = worst_body("orders")  # the largest legal batch of any route
    assert len(body) > 25 * MIB and len(body) <= ROUTE_BODY_LIMITS[DETECT]

    results: list[tuple[int, bytes, float]] = []
    lock = threading.Lock()
    stop = threading.Event()

    def client():
        while not stop.is_set():
            r = _request(server, "POST", DETECT, body, timeout=120, headers={"Authorization": AUTH})
            with lock:
                results.append(r)
            if r[0] == 503:
                time.sleep(0.2)

    threads = [threading.Thread(target=client) for _ in range(16)]
    for t in threads:
        t.start()
    try:
        # Fix wave 25 (D3): the measurement starts once a batch is really in flight (the first answer, 200 or 503,
        # means the server has read a whole worst-case batch), not after a fixed 0.5 s.
        deadline = time.monotonic() + 60
        while not results and time.monotonic() < deadline:
            time.sleep(0.05)
        assert results, "no worst-case batch was answered within 60 s"
        probes = _probe_health(server, 8.0)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=180)

    latencies = sorted(_service_latency(p) for p in probes)
    worst = latencies[-1]
    p50 = latencies[len(latencies) // 2]
    walls = sorted(p["wall"] for p in probes)
    print(f"\n/health under 16 concurrent worst-case batches: n={len(latencies)} service latency "
          f"p50={p50 * 1000:.0f}ms max={worst * 1000:.0f}ms (wall p50={walls[len(walls) // 2] * 1000:.0f}ms "
          f"max={walls[-1] * 1000:.0f}ms; max run-queue waits: prober {max(p['prober_wait'] for p in probes) * 1000:.0f}ms, "
          f"server loop {max(p['server_wait'] for p in probes) * 1000:.0f}ms); batches: "
          f"{sum(r[0] == 200 for r in results)} x 200, {sum(r[0] == 503 for r in results)} x 503")
    assert probes and all(p["status"] == 200 for p in probes), probes[:5]
    assert worst < HEALTH_BOUND_S, f"/health took {worst:.3f}s of the server's time (bound {HEALTH_BOUND_S}s): {probes}"
    codes = {r[0] for r in results}
    assert codes <= {200, 503}, codes
    assert any(r[0] == 200 for r in results), "no worst-case batch was ever served"
    assert any(r[0] == 503 for r in results), f"16 concurrent batches never hit the cap of {MAX_CONCURRENT_HEAVY}"
    for status, resp, _ in results:
        if status == 503:
            assert b"busy" in resp


def test_heavy_slot_is_not_held_by_a_stalled_request_without_a_valid_token(server):
    """The heavy slot is taken at the request head, before auth. A client
    without a valid token that declares a large body and never sends it must
    be answered 401 at once (freeing the slot), so it cannot starve real
    batches. With a valid token the same stall does hold the slot (bounded
    by the body deadline) — the documented trade-off (ADR 0001)."""
    from api import HEAVY_BODY_BYTES
    batch = _orders_body(1000)
    assert len(batch) > HEAVY_BODY_BYTES  # a real heavy request

    def stalled_then_batch(auth_line: str) -> tuple[bytes, int]:
        with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
            s.sendall((f"POST {DETECT} HTTP/1.1\r\nHost: t\r\n{auth_line}"
                       f"Content-Type: application/json\r\nContent-Length: {HEAVY_BODY_BYTES + 1}\r\n\r\n").encode())
            time.sleep(0.3)  # head parsed; the body is never sent
            status, _, _ = _request(server, "POST", DETECT, batch, timeout=60, headers={"Authorization": AUTH})
            s.settimeout(0.5)
            try:
                first_line = s.recv(200).split(b"\r\n")[0]
            except (TimeoutError, socket.timeout):
                first_line = b""
        return first_line, status

    for auth_line in ("", "Authorization: Bearer not-the-token\r\n"):
        first_line, status = stalled_then_batch(auth_line)
        assert first_line.startswith(b"HTTP/1.1 401"), first_line
        assert status == 200, status
    first_line, status = stalled_then_batch(f"Authorization: {AUTH}\r\n")
    assert first_line == b""  # still waiting for its body: it holds the slot
    assert status == 503


def test_oversized_content_length_is_refused_before_the_body_is_sent(server):
    from api import ROUTE_BODY_LIMITS
    limit = ROUTE_BODY_LIMITS[DETECT]
    with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
        s.sendall((f"POST {DETECT} HTTP/1.1\r\nHost: t\r\nAuthorization: {AUTH}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {limit + 1}\r\n\r\n").encode())
        # Fix wave 25 (R-HYGIENE L1): no wall-clock bound. No body byte is ever sent, so a 413 at all is the
        # refusal before the body (a server that waited for the body would answer 408 at its body deadline, or
        # nothing within this socket's 5 s timeout — the hang guard).
        status, body = _read_response(s)  # no body byte sent at all
    assert status == 413
    assert str(limit).encode() in body  # the limit is named


def test_oversized_chunked_body_is_refused_while_streaming(server):
    from api import ROUTE_BODY_LIMITS
    limit = ROUTE_BODY_LIMITS[DETECT]
    with socket.create_connection(("127.0.0.1", server), timeout=10) as s:
        s.sendall((f"POST {DETECT} HTTP/1.1\r\nHost: t\r\nAuthorization: {AUTH}\r\n"
                   "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n").encode())
        chunk = b" " * (256 * 1024)
        sent = 0
        answered = False
        while sent < limit + 64 * MIB:
            if select.select([s], [], [], 0)[0]:
                answered = True
                break
            try:
                s.sendall(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                break
            sent += len(chunk)
        status, _ = _read_response(s)
    assert status == 413
    # The 413 arrived while the client was still streaming. `sent` counts
    # bytes handed to the kernel, which includes loopback socket buffers
    # (several MiB), so the bound is loose; the point is it is not 64 MiB.
    assert answered or sent < limit + 64 * MIB
    assert sent <= limit + 16 * MIB, f"client streamed {sent} bytes of a chunked body before it was refused"


def test_request_head_larger_than_the_header_cap_is_refused(server):
    with socket.create_connection(("127.0.0.1", server), timeout=10) as s:
        try:
            s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\nX-Big: " + b"a" * (4 * MIB) + b"\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        try:
            status, _ = _read_response(s)
        except ConnectionResetError:
            status = 0  # closed without a response: refused, not served
    assert status != 200


def test_long_query_string_is_refused(server):
    status, _, _ = _request(server, "GET", "/health?q=" + "a" * (1 * MIB), timeout=10)
    assert status != 200


def test_slow_request_head_is_cut_off_and_others_are_served(server):
    from serve import REQUEST_HEAD_TIMEOUT_S
    bound = REQUEST_HEAD_TIMEOUT_S + 2
    with socket.create_connection(("127.0.0.1", server), timeout=1) as s:
        s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\nX-Slow: ")
        t0 = time.monotonic()
        closed_after = None
        while time.monotonic() - t0 < bound + 10:
            r, _, _ = select.select([s], [], [], 0.3)
            if r and not s.recv(4096):
                closed_after = time.monotonic() - t0
                break
            try:
                s.sendall(b"a")
            except OSError:
                closed_after = time.monotonic() - t0
                break
            status, _, elapsed = _request(server, "GET", "/health", timeout=5)
            assert status == 200 and elapsed < 1.0
    assert closed_after is not None and closed_after <= bound, f"slow-head connection open {closed_after}"


def test_keep_alive_and_pipelined_requests_still_work_with_the_head_deadline(server):
    req = b"GET /health HTTP/1.1\r\nHost: t\r\n\r\n"
    with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
        s.sendall(req)
        assert _read_response(s)[0] == 200
        time.sleep(1)  # reused connection, under the 5 s keep-alive
        s.sendall(req + req)  # pipelined
        data = b""
        while data.count(b"HTTP/1.1 200") < 2:
            chunk = s.recv(65536)
            assert chunk, f"connection closed after {data!r}"
            data += chunk
