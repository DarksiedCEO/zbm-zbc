"""
Fix wave 25 (found while proving H1): the live-test launchers trusted ANY
answer on the port they had picked as their own server's.

`_free_port()` picks the first free port of FULFILLMENT_TEST_PORT_RANGE; two
test processes sharing a range can both pick it before either binds (the
N20-M-3 race). The loser's server exits with EADDRINUSE about a second later,
but its launcher was already probing the port — and the WINNER answered. So
`_start` returned a process that was about to exit, and the test measured
someone else's server: `/proc/<pid>/status` of a zombie ("no VmRSS line"), 3
times in wave 25's first campaigns (two campaigns had been run concurrently in
one port range). `test_fix5_http_limits_live._start` retried EADDRINUSE only
when it saw the exit before the probe succeeded; `test_live_server._start`
never retried.

Now a launcher trusts the port only once its OWN child has announced the bind
(uvicorn logs "Uvicorn running on" right after it starts listening, before the
loop serves anything) while still running; a child that lost the port is
retried on a fresh one.
"""

from __future__ import annotations

import http.server
import threading

import pytest

import test_fix5_http_limits_live as fix5
import test_live_server as live


@pytest.fixture
def decoy():
    """Someone else's server holding a port of our range, answering every request 200."""

    class _Ok(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"      # the probe reads an HTTP/1.1 status line, as the real server sends

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", live._free_port()), _Ok)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    t.join(5)


def _first_pick_is(monkeypatch, module, port: int) -> None:
    """The race, made deterministic: the first port picked is the one the decoy already holds."""
    real, picks = module._free_port, []

    def pick():
        picks.append(None)
        return port if len(picks) == 1 else real()

    monkeypatch.setattr(module, "_free_port", pick)


def test_fix5_start_does_not_return_a_server_that_lost_its_port(monkeypatch, decoy):
    _first_pick_is(monkeypatch, fix5, decoy)
    proc, port = fix5._start()
    try:
        assert port != decoy, "the launcher took the decoy's answer for its own server"
        assert proc.poll() is None
        assert fix5._health(port)[0] == 200
    finally:
        fix5._stop(proc)


def test_live_server_start_does_not_return_a_server_that_lost_its_port(monkeypatch, decoy):
    _first_pick_is(monkeypatch, live, decoy)
    proc, host, port = live._start({})
    try:
        assert port != decoy, "the launcher took the decoy's answer for its own server"
        assert proc.poll() is None
        assert live._get(host, port, "/health") == 200
    finally:
        live._stop(proc)
