"""
Fix wave 4 — body limits and event-loop responsiveness against the real
service process (`python3 -m api`, real uvicorn) over TCP.

Before the fix, FastAPI read, json-decoded and pydantic-validated every
request body ON the event loop (only the handler body ran in the thread
pool), and there was no body limit: a 64 MiB JSON body stalled /health for
over a second for every client of the process.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

from test_live_server import TOKEN, _start, _stop

LIMIT = 4 * 1024 * 1024
DETECT = "/agents/missed-call-detection/detect"


@pytest.fixture(scope="module")
def server():
    proc, host, port = _start({})
    yield host, port
    _stop(proc)


def _max_size_body() -> bytes:
    """A valid 1000-event batch padded with transcripts to just under the limit."""
    ev = {"call_id": "", "customer_id": "c", "phone_number": "+12125550101", "direction": "inbound",
          "status": "voicemail", "started_at": "2026-09-22T12:00:00Z", "line_id": "l", "voicemail_transcript": ""}
    skeleton = len(json.dumps({"call_events": [dict(ev, call_id=f"call-{i:04d}") for i in range(1000)]}))
    per = (LIMIT - 1024 - skeleton) // 1000
    body = json.dumps({"call_events": [dict(ev, call_id=f"call-{i:04d}", voicemail_transcript="v" * per)
                                       for i in range(1000)]}).encode()
    assert LIMIT - 2048 < len(body) <= LIMIT
    return body


# Cheap to send, expensive to parse: 32M JSON array elements.
OVERSIZED = b'{"call_events":[],"pad":[' + b"0," * (32 * 1024 * 1024) + b"0]}"


def _read_status(s: socket.socket) -> int | None:
    data = b""
    try:
        while b"\r\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except (ConnectionResetError, TimeoutError):
        pass
    if not data.startswith(b"HTTP/1.1 "):
        return None
    return int(data[9:12])


def _raw_post(host, port, head_extra: bytes, payload_iter, results, key):
    s = socket.create_connection((host, port), timeout=60)
    try:
        s.sendall(b"POST " + DETECT.encode() + b" HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode()
                  + b"\r\nContent-Type: application/json\r\n" + head_extra + b"\r\n")
        try:
            for part in payload_iter:
                s.sendall(part)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the server answered and closed before taking the whole body
        results[key] = _read_status(s)
    finally:
        s.close()


def _chunked(body: bytes, size: int = 1 << 20):
    for i in range(0, len(body), size):
        part = body[i:i + size]
        yield f"{len(part):x}\r\n".encode() + part + b"\r\n"
    yield b"0\r\n\r\n"


def _pieces(body: bytes, size: int = 1 << 20):
    for i in range(0, len(body), size):
        yield body[i:i + size]


def _held(parts, hold_after: int, arrived: threading.Event, gate: threading.Event):
    """Yield ``parts``; once ``hold_after`` bytes are out, say so (``arrived``) and wait for ``gate`` before the rest:
    the request stays in flight, unfinished, for as long as the test needs."""
    sent, holding = 0, hold_after <= 0
    for part in parts:
        if not holding and sent + len(part) > hold_after:
            holding = True
            arrived.set()
            gate.wait(60)
        sent += len(part)
        yield part
    if not holding:
        arrived.set()


HELD_SAMPLES = 5


def test_health_answers_within_1s_while_max_size_and_oversized_requests_are_in_flight(server):
    """The guarantee: /health answers within 1 s while big requests are in flight. Annotated CI (Oct 10 2026, macOS
    3.13): only 2 samples were taken, because how long the requests stayed in flight was up to the machine. Now the
    max-size body and the chunked oversized body are HELD open part-way (their senders wait on a gate before the
    rest), so exactly HELD_SAMPLES health samples are taken while both are verified in flight (sender alive, no
    answer yet); then the gate opens and sampling goes on, as before, while the server receives and parses the rest
    (the parse is what stalled the loop before fix wave 4). Every sample keeps the 1 s bound."""
    host, port = server
    max_body = _max_size_body()
    results: dict[str, int | None] = {}
    gate = threading.Event()
    arrived = {"max": threading.Event(), "oversized_chunked": threading.Event()}
    workers = {
        "max": threading.Thread(target=_raw_post, args=(
            host, port, f"Content-Length: {len(max_body)}\r\n".encode(),
            _held(_pieces(max_body), len(max_body) - (1 << 20), arrived["max"], gate), results, "max")),
        "oversized_cl": threading.Thread(target=_raw_post, args=(
            host, port, f"Content-Length: {len(OVERSIZED)}\r\n".encode(), _pieces(OVERSIZED), results,
            "oversized_cl")),
        "oversized_chunked": threading.Thread(target=_raw_post, args=(
            host, port, b"Transfer-Encoding: chunked\r\n",
            _held(_chunked(OVERSIZED), LIMIT // 2, arrived["oversized_chunked"], gate), results,
            "oversized_chunked")),
    }

    def sample() -> float:
        t = time.perf_counter()
        conn = http.client.HTTPConnection(host, port, timeout=30)
        conn.request("GET", "/health")
        assert conn.getresponse().status == 200
        conn.close()
        return time.perf_counter() - t

    for w in workers.values():
        w.start()
    held, latencies = [], []
    try:
        for name, ev in arrived.items():
            assert ev.wait(30), f"{name} never reached its hold point"
        for _ in range(HELD_SAMPLES):
            # the condition the guarantee is about, verified at every sample: both held requests are mid-body
            assert all(workers[n].is_alive() and n not in results for n in arrived), results
            held.append(sample())
            time.sleep(0.02)   # a sampling rate, not a synchronisation
    finally:
        gate.set()
    while any(w.is_alive() for w in workers.values()):
        latencies.append(sample())
        time.sleep(0.02)       # a sampling rate, not a synchronisation
    for w in workers.values():
        w.join()
    assert len(held) == HELD_SAMPLES
    assert max(held + latencies) < 1.0, (held, latencies, results)
    assert results["max"] == 200, results
    assert results["oversized_cl"] in (413, None), results   # None: closed before we could read
    assert results["oversized_chunked"] in (413, None), results


def test_oversized_content_length_is_refused_before_the_body_is_sent(server):
    host, port = server
    with socket.create_connection((host, port), timeout=5) as s:
        s.sendall(b"POST " + DETECT.encode() + b" HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode()
                  + f"\r\nContent-Type: application/json\r\nContent-Length: {LIMIT + 1}\r\n\r\n".encode())
        # not one body byte sent: the answer must come from the header alone
        assert _read_status(s) == 413


def test_chunked_body_over_limit_is_413_over_real_http(server):
    host, port = server
    results: dict[str, int | None] = {}
    _raw_post(host, port, b"Transfer-Encoding: chunked\r\n", _chunked(b" " * (LIMIT + 1), 64 * 1024), results, "r")
    assert results["r"] == 413


def test_max_size_body_is_accepted_over_real_http(server):
    host, port = server
    conn = http.client.HTTPConnection(host, port, timeout=30)
    conn.request("POST", DETECT, body=_max_size_body(),
                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    r = conn.getresponse()
    body = json.loads(r.read())
    conn.close()
    assert r.status == 200 and len(body["tasks"]) == 1000
