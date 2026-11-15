"""Marcus pipeline conformance fixtures (marcus-pipeline-spec/1 + CLARIFICATIONS v1.1 GP-1..GP-6).

* CB-13: a PROCESS-LEVEL network audit hook (``sys.addaudithook``, installed once, cannot be removed) records every
  socket connect / name lookup that targets anything but the loopback address; an autouse fixture fails any test of
  this folder during which one happened (on top of the suite's socket guard in tests/conftest.py).
* ``mp`` / ``mpd``: a harness (in memory / durable data directory) with a generated fixture directory
  (``SEO_MARCUS_FIXTURE_DIR``, GP-1..GP-3), the tenants ``acme`` (acme.test) and ``globex`` (globex.test).
* the lead lane: ``@pytest.mark.lead_gate`` items run only with CONFORMANCE_LEAD=1 (SUITE_PLAN 2.3).
"""

from __future__ import annotations

import os
import sys

import pytest

ESCAPES: list = []
_LOCAL = ("127.0.0.1", "localhost", "::1")
_EVENTS = ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex")


def _hook(event, args):
    if event in _EVENTS:
        target = args[1] if event == "socket.connect" else args[0]
        host = target[0] if isinstance(target, tuple) else target
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        if host not in _LOCAL:
            ESCAPES.append(event)


if not getattr(sys, "_marcus_pipeline_audit_hook", False):
    sys.addaudithook(_hook)
    sys._marcus_pipeline_audit_hook = True
    sys._marcus_pipeline_escapes = ESCAPES
else:                                                     # a second import of this conftest shares the first list
    ESCAPES = sys._marcus_pipeline_escapes


def pytest_configure(config):
    config.addinivalue_line("markers", "lead_gate: the lead-gate lane (CONFORMANCE_LEAD=1)")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("CONFORMANCE_LEAD") == "1":
        return
    skip = pytest.mark.skip(reason="lead-gate lane: set CONFORMANCE_LEAD=1")
    for it in items:
        if "lead_gate" in it.keywords and "conformance/marcus_pipeline" in str(it.fspath).replace(os.sep, "/"):
            it.add_marker(skip)


@pytest.fixture(autouse=True)
def _cb13_no_escape():
    before = len(ESCAPES)
    yield
    assert len(ESCAPES) == before, "CB-13: a network operation outside 127.0.0.1 during the test"


def _harness(tmp_path, data_dir=None, fixture=True, **env):
    from helpers import Harness
    from conformance.marcus_pipeline import gen_cc
    fx = tmp_path / "ccfx"
    if fixture:
        gen_cc.default_fixture(fx)
        env.setdefault("SEO_MARCUS_FIXTURE_DIR", str(fx))
    h = Harness(tmp_path, data_dir=data_dir, **env)
    h.fx = fx
    h.tenant("acme", domains=("acme.test",))
    h.tenant("globex", domains=("globex.test",))
    return h


@pytest.fixture
def mp(tmp_path):
    return _harness(tmp_path)


@pytest.fixture
def mpd(tmp_path):
    return _harness(tmp_path, data_dir=str(tmp_path / "data"))


@pytest.fixture
def make_mp(tmp_path):
    """A factory: ``make_mp(data=False, fixture=True, **env)`` (a second call in one test needs another subfolder)."""
    made = []

    def make(data: bool = False, fixture: bool = True, sub: str = "", **env):
        root = tmp_path / (sub or f"h{len(made)}")
        root.mkdir(parents=True, exist_ok=True)
        h = _harness(root, data_dir=str(root / "data") if data else None, fixture=fixture, **env)
        made.append(h)
        return h
    return make
