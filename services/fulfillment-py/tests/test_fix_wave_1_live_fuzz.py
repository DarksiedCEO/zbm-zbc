"""
Fix wave 1 — the fuzz corpus of test_fix_wave_1_fuzz.py replayed against the
real service process (`python3 -m api`) over TCP, so the "never 5xx" claim
does not rest on TestClient alone. Also sends raw-socket junk that TestClient
cannot express (malformed request line, bad Content-Length, non-UTF-8
header bytes).
"""

from __future__ import annotations

import http.client
import socket

import pytest

from test_fix_wave_1_fuzz import CASES, GET_ROUTES
from test_live_server import TOKEN, _start, _stop


@pytest.fixture(scope="module")
def server():
    proc, host, port = _start({})
    yield host, port
    _stop(proc)


def _post(host, port, path, body: bytes, ctype="application/json") -> int:
    conn = http.client.HTTPConnection(host, port, timeout=30)
    conn.request("POST", path, body=body, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": ctype})
    r = conn.getresponse()
    r.read()
    conn.close()
    return r.status


def test_every_fuzz_case_is_below_500_over_real_http(server):
    host, port = server
    bad = []
    for route, body in CASES:
        code = _post(host, port, route, body)
        if code >= 500:
            bad.append((route, body[:80], code))
    assert bad == []
    assert len(CASES) > 200


@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""])
def test_wrong_content_type_is_below_500(server, ctype):
    host, port = server
    assert _post(host, port, "/agents/missed-call-detection/detect", b'{"call_events":[]}', ctype) < 500


@pytest.mark.parametrize("path", GET_ROUTES)
def test_get_routes_with_junk_query_are_below_500(server, path):
    host, port = server
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("GET", path + "?a=%00&b=%ff%fe&" + "c" * 4000, headers={"Authorization": f"Bearer {TOKEN}"})
    assert conn.getresponse().status < 500
    conn.close()


@pytest.mark.parametrize("raw", [
    b"POST /agents/missed-call-detection/detect HTTP/1.1\r\nHost: x\r\nContent-Length: 999\r\n"
    b"Content-Type: application/json\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\n\r\n{\"call_events\":[]}",
    b"POST /agents/missed-call-detection/detect HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n",
    b"POST /agents/missed-call-detection/detect HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer \xff\xfe\r\n"
    b"Content-Length: 2\r\n\r\n{}",
    b"GARBAGE\r\n\r\n",
])
def test_raw_malformed_http_never_gets_a_500(server, raw):
    host, port = server
    with socket.create_connection((host, port), timeout=3) as s:
        s.sendall(raw)
        s.shutdown(socket.SHUT_WR)
        data = b""
        try:
            while chunk := s.recv(65536):
                data += chunk
        except (TimeoutError, ConnectionResetError):
            pass
    status_line = data.split(b"\r\n", 1)[0]
    # Either the server closed without answering (truncated body) or it
    # answered 4xx. Never 5xx.
    assert not status_line.startswith(b"HTTP/1.1 5"), status_line
