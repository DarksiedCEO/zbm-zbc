"""
Fix wave 26b (scout C5-6): creative-py's live tests pick ports with the shared helper (tests/_procinfo.py), not a
picker of their own, and every child they start is accepted only once it owns its port.

- ``conftest.free_port`` was creative-py's own pick-then-bind picker (CREATIVE_TEST_PORTS only); it is now
  ``_procinfo.pick_port`` over ``assigned_port_range("CREATIVE_TEST_PORTS")``, so the repo-wide
  ZBM_TEST_PORT_RANGE is honoured too.
- ``test_fix_wave_4._lossy_stack`` started the fake ledger and the lossy proxy on picked ports and accepted ANY
  answer there (``_wait``): when another process held the ledger's port, the stack yielded that process's URL as its
  ledger. It now starts each child with ``_procinfo.start_owned`` (owner-checked, retried on another port).
"""

from __future__ import annotations

import http.server
import threading

import pytest

import _procinfo
import conftest


def test_the_live_port_picker_is_the_shared_one_and_honours_the_repo_wide_range(monkeypatch):
    monkeypatch.setattr(_procinfo, "_HANDED_OUT", set())
    monkeypatch.delenv("CREATIVE_TEST_PORTS", raising=False)
    lo = _procinfo.pick_port()                 # some port that is free now
    monkeypatch.setenv("ZBM_TEST_PORT_RANGE", f"{lo}-{lo}")
    assert conftest.free_port() == lo
    assert conftest.free_port() == lo          # a range of one: handed out again once free (the shared rule)


class _Ok(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        body = b"[]"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def decoy():
    """Someone else's server on a port our picker hands out, answering every GET 200."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Ok)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    t.join(5)


def test_the_lossy_stack_never_takes_a_strangers_answer_for_its_ledger(monkeypatch, decoy):
    """The race, made deterministic: the first port picked is the one the decoy holds."""
    real_pick, real_free, picks = _procinfo.pick_port, conftest.free_port, []

    def first_is_decoy(real):
        def pick(*a, **k):
            picks.append(None)
            return decoy if len(picks) == 1 else real(*a, **k)
        return pick

    monkeypatch.setattr(_procinfo, "pick_port", first_is_decoy(real_pick))
    monkeypatch.setattr(conftest, "free_port", first_is_decoy(real_free))
    import test_fix_wave_4

    gen = test_fix_wave_4._lossy_stack()
    proxy, ledger_url, _tok = next(gen)
    try:
        assert f":{decoy}" not in ledger_url and f":{decoy}" not in proxy, (decoy, ledger_url, proxy)
    finally:
        gen.close()
