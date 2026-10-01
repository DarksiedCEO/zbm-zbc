import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
for p in (SRC, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import _tmproot  # noqa: E402  (fix wave 21, L4: before anything imports tempfile users; since wave 25 gitport makes its dir on first use)

# The suite builds every service from an explicit env dict; the process environment must not leak DLV_* or the
# ledger settings into anything that reads os.environ (the harness module sets only the DEER_FLOW_* it owns).
# Wave 22 (G3, N21-D-3): the suite's OWN settings survive — DLV_TEST_PORT_RANGE is the operator's port assignment for
# the live tests, not a service setting (it used to be popped here, so every run fell back to 18800-18849).
# Wave 25 (scout B H1): DLV_LIVE_SANDBOX_IMAGE too — the one input of tests/test_live_docker.py, set by the CI job
# delivery-docker-live; popping it made all three Docker live tests skip "is not set" on every machine, so that job
# (which fails on any skip) could never pass. tests/test_round25.py checks every DLV_* name a test reads is listed here.
# DLV_TEST_GOCACHE (wave 25): a Go build cache to keep across sessions; unset, the session root holds it.
SUITE_SETTINGS = ("DLV_TEST_PORT_RANGE", "DLV_LIVE_LOG_DIR", "DLV_LIVE_SANDBOX_IMAGE", "DLV_TEST_GOCACHE")
for k in list(os.environ):
    if k.startswith(("DLV_", "DEER_FLOW_", "LEDGER_SERVICE_", "LANGSMITH_", "LANGFUSE_", "GATEWAY_")) and k not in SUITE_SETTINGS:
        os.environ.pop(k, None)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, request):
    """G13 / BUILD_CONTRACTS §0: no socket connect in tests other than through the egress client's mock transport
    (which never opens a socket). Any real connect raises. The live modules (test_live_*) talk to a server they
    started on 127.0.0.1 within the assigned port range and are exempt."""
    import socket

    if request.module.__name__.startswith("test_live_"):
        return

    def refuse(*a, **k):
        raise RuntimeError("network access attempted in a test (not allowed)")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("DNS lookup attempted in a test")))


@pytest.fixture(autouse=True)
def _no_tiktoken_download(monkeypatch):
    """G11: tiktoken may be imported by langchain_openai's module; its loader must never run (it downloads)."""
    try:
        import tiktoken.load as tl
    except ImportError:
        return

    def boom(*a, **k):
        raise RuntimeError("tiktoken.load.read_file called in a test (a download path)")
    monkeypatch.setattr(tl, "read_file", boom)
    monkeypatch.setattr(tl, "read_file_cached", boom, raising=False)


def pytest_unconfigure(config):
    """L4: the session's private temp root goes with the session."""
    _tmproot.remove_session_root()


@pytest.fixture(autouse=True)
def _harness_cleanup():
    """L4: every Harness a test made is closed and its OWN temp dir removed when the test ends (pass or fail), so
    a full run never holds more than one test's worth of harness trees. Harnesses a module-scoped fixture made
    exist before this fixture starts and are left to that fixture (and to the session root)."""
    import helpers
    start = len(helpers.HARNESSES)
    yield
    made = helpers.HARNESSES[start:]
    del helpers.HARNESSES[start:]
    for h in made:
        try:
            h.close()
        except Exception:  # noqa: BLE001 - a harness the test already closed or that never started
            pass
    import shutil
    for h in made:
        if h.owns_tmp:
            shutil.rmtree(h.tmp, ignore_errors=True)
