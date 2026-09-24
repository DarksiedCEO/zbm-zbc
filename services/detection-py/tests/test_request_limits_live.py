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
  - a body larger than MAX_BODY_BYTES is refused with 413 before any parsing:
    immediately from Content-Length (no body byte has to be sent), and while
    streaming for a chunked body with no Content-Length;
  - /health answers within 1 s while a large valid batch and an oversized
    body are in flight;
  - a request head larger than the header cap is refused, not buffered.

Ports: 19960-19969 (this wave's assigned range for detection-py tests).
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
from pathlib import Path

import pytest

from conftest import TEST_SERVICE_TOKEN

SRC = Path(__file__).resolve().parents[1] / "src"
PORTS = range(19960, 19970)
MIB = 1024 * 1024
AUTH = f"Bearer {TEST_SERVICE_TOKEN}"


def _free_port() -> int:
    for port in PORTS:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port in 19960-19969")


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    env = {**os.environ, "ZBM_SERVICE_TOKEN": TEST_SERVICE_TOKEN}
    proc = subprocess.Popen(
        [sys.executable, "serve.py", "--host", "127.0.0.1", "--port", str(port)],
        cwd=SRC, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                status, _, _ = _request(port, "GET", "/health", timeout=1)
                if status == 200:
                    break
            except OSError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("detection-py did not start")
            time.sleep(0.1)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


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
    from api import MAX_BATCH_ITEMS
    large_valid = _orders_body(MAX_BATCH_ITEMS)   # the biggest batch the API accepts
    oversized = _orders_body(52_000)              # ~33 MB, the reproduction's size
    assert len(oversized) > 30 * MIB

    results: dict[str, list] = {"valid": [], "oversized": []}
    stop = threading.Event()

    def hammer(kind: str, body: bytes):
        while not stop.is_set():
            results[kind].append(_request(server, "POST", DETECT, body, headers={"Authorization": AUTH}))

    threads = [threading.Thread(target=hammer, args=("valid", large_valid)),
               threading.Thread(target=hammer, args=("oversized", oversized))]
    for t in threads:
        t.start()
    health = []
    try:
        end = time.monotonic() + 4
        while time.monotonic() < end:
            status, _, elapsed = _request(server, "GET", "/health", timeout=10)
            health.append((status, elapsed))
            time.sleep(0.05)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=60)

    worst = max(e for _, e in health)
    assert all(s == 200 for s, _ in health)
    assert worst < 1.0, f"/health took {worst:.2f}s while large bodies were in flight"
    assert results["valid"] and results["oversized"]
    assert all(r[0] == 200 for r in results["valid"]), {r[0] for r in results["valid"]}
    assert all(r[0] == 413 for r in results["oversized"]), {r[0] for r in results["oversized"]}


def test_oversized_content_length_is_refused_before_the_body_is_sent(server):
    with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
        s.sendall((f"POST {DETECT} HTTP/1.1\r\nHost: t\r\nAuthorization: {AUTH}\r\n"
                   "Content-Type: application/json\r\nContent-Length: 34603008\r\n\r\n").encode())
        t0 = time.monotonic()
        status, body = _read_response(s)  # no body byte sent at all
    assert status == 413
    assert time.monotonic() - t0 < 1.0
    assert b"2097152" in body  # the limit is named


def test_oversized_chunked_body_is_refused_while_streaming(server):
    with socket.create_connection(("127.0.0.1", server), timeout=10) as s:
        s.sendall((f"POST {DETECT} HTTP/1.1\r\nHost: t\r\nAuthorization: {AUTH}\r\n"
                   "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n").encode())
        chunk = b" " * (256 * 1024)
        sent = 0
        answered = False
        while sent < 64 * MIB:
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
    assert answered or sent < 64 * MIB
    assert sent <= 16 * MIB, f"client streamed {sent} bytes of a chunked body before it was refused"


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
