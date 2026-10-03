"""
Fix wave 26b (scout C5-6): onboarding-py's live tests pick ports with the shared helper (tests/_procinfo.py), not a
picker of their own, and every child they start is accepted only once it owns its port.

- ``conftest.free_test_port`` was onboarding-py's own pick-then-bind picker (ONBOARDING_TEST_PORT_RANGE only); it
  is now ``_procinfo.pick_port`` over ``assigned_port_range("ONBOARDING_TEST_PORT_RANGE")``, so the repo-wide
  ZBM_TEST_PORT_RANGE is honoured too.
- Every launcher here (the entrypoint test's ``_start``, the fake-ledger ``Stack``, the real-ledger stacks, the
  real-uvicorn tests) picked a port, started the child and took ANY 200 on that port's /health as its child's
  answer: when another process held the port, the test talked to THAT process. They now start each child with
  ``conftest.start_live`` (``_procinfo.start_owned``: the shared picker, the owner check, another port when the
  child lost the race).
"""

from __future__ import annotations

import http.server
import threading

import pytest

import _procinfo
import conftest


def test_the_live_port_picker_is_the_shared_one_and_honours_the_repo_wide_range(monkeypatch):
    monkeypatch.setattr(_procinfo, "_HANDED_OUT", set())
    monkeypatch.delenv("ONBOARDING_TEST_PORT_RANGE", raising=False)
    lo = _procinfo.pick_port()                 # some port that is free now
    monkeypatch.setenv("ZBM_TEST_PORT_RANGE", f"{lo}-{lo}")
    assert conftest.free_test_port() == lo
    assert conftest.free_test_port() == lo     # a range of one: handed out again once free (the shared rule)


class _Ok(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def decoy():
    """Someone else's server on a port our picker hands out, answering every GET (/health included) 200."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Ok)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    t.join(5)


def _first_pick_is(monkeypatch, port: int, *modules) -> None:
    """The race, made deterministic: the first port picked (by any picker) is the one the decoy holds."""
    picks = []

    def first_is(real):
        def pick(*a, **k):
            picks.append(None)
            return port if len(picks) == 1 else real(*a, **k)
        return pick

    monkeypatch.setattr(_procinfo, "pick_port", first_is(_procinfo.pick_port))
    for mod in (conftest, *modules):
        if hasattr(mod, "free_test_port"):         # a module that imported the picker by name (before the fix)
            monkeypatch.setattr(mod, "free_test_port", first_is(mod.free_test_port))


def test_the_entrypoint_launcher_never_takes_a_strangers_answer_for_its_server(monkeypatch, decoy):
    import test_auth_and_entrypoint as entry

    _first_pick_is(monkeypatch, decoy)
    proc, _host, port = entry._start({})
    try:
        assert port != decoy, "the launcher took the decoy's answer for its own server"
        assert proc.poll() is None
        assert _procinfo.listener_owned_by(proc.pid, port)
    finally:
        proc.terminate()
        proc.wait(30)


def test_the_fake_ledger_stack_never_takes_a_strangers_answer_for_its_ledger(monkeypatch, decoy):
    import test_fix_wave5 as w5

    _first_pick_is(monkeypatch, decoy, w5)
    stack = w5.Stack()
    try:
        assert decoy not in (stack.lport, stack.port), (decoy, stack.lport, stack.port)
        assert _procinfo.listener_owned_by(stack.ledger.pid, stack.lport)
        assert _procinfo.listener_owned_by(stack.api.pid, stack.port)
    finally:
        stack.close()
