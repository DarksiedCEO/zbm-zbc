"""Farrah's conformance fixtures (farrah-spec/1).

* CB-13: a PROCESS-LEVEL network audit hook (``sys.addaudithook``, installed once, cannot be removed) records every
  socket connect / name lookup; an autouse fixture fails any Farrah conformance test during which one went to
  anything but 127.0.0.1 (on top of the suite's socket guard in tests/conftest.py).
* ``fa``: an in-memory harness (fixed clock 2026-10-09, so 2026-09 is a closed month) with the client tenant
  ``acme`` on the Growth plan, analytics basis, and the fixture Osei store installed (GF-2).
* the lead lane: ``@pytest.mark.lead_gate`` items run only with CONFORMANCE_LEAD=1 (SUITE_PLAN 2.3: lead N, the
  full corpora, timing bars and all generated mutants run at the gate, never in the CI lane).
"""

from __future__ import annotations

import os

import pytest

from conformance.gen.farrah_util import setup

from conformance.gen.netaudit import ESCAPES, install

install()


def pytest_configure(config):
    config.addinivalue_line("markers", "lead_gate: the lead-gate lane (CONFORMANCE_LEAD=1)")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("CONFORMANCE_LEAD") == "1":
        return
    skip = pytest.mark.skip(reason="lead-gate lane: set CONFORMANCE_LEAD=1")
    for it in items:
        if "lead_gate" in it.keywords and "conformance/farrah" in str(it.fspath).replace(os.sep, "/"):
            it.add_marker(skip)


@pytest.fixture(autouse=True)
def _cb13_no_escape():
    before = len(ESCAPES)
    yield
    assert len(ESCAPES) == before, "CB-13: a network operation outside 127.0.0.1 during the test"


@pytest.fixture
def fa(tmp_path):
    from helpers import Harness
    from clock import FixedClock
    from datetime import datetime, timezone
    h = Harness(tmp_path, clock=FixedClock(datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc)))
    setup(h)
    return h


@pytest.fixture
def fad(tmp_path):
    """As ``fa`` with a durable data directory (restarts, data-directory scans)."""
    from helpers import Harness
    from clock import FixedClock
    from datetime import datetime, timezone
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"),
                clock=FixedClock(datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc)))
    setup(h)
    return h
