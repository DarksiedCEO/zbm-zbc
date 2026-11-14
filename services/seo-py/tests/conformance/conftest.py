"""
The permanent conformance suite for the seo-py Wave-1 agents (docs/spec/wave1-spec-1.md, SUITE_PLAN). Every test in
this directory is marked ``conformance``; the heavy tier is ``lead_gate``.

  fast tier:  pytest tests/conformance -m "conformance and not lead_gate"
  lead gate:  pytest tests/conformance -m lead_gate

Seeds: the fast tier runs SEEDS_FAST, the lead gate SEEDS_LEAD plus one seed from the date. ``CONFORMANCE_SEED``
(environment) replays one seed. Every generated value is built at run time (gen/); a failure message carries the
seed and the case index.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

FAST_SESSION_LIMIT_S = 600.0
_START = {"t": None, "fast": False}


def pytest_configure(config):
    config.addinivalue_line("markers", "conformance: the permanent Wave-1 conformance suite (SUITE_PLAN)")
    config.addinivalue_line("markers", "lead_gate: the heavy tier of the conformance suite (lead gate, nightly)")


def pytest_collection_modifyitems(session, config, items):
    for item in items:
        if HERE in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.conformance)
    mark = getattr(config.option, "markexpr", "") or ""
    _START["fast"] = "not lead_gate" in mark
    if "lead_gate" not in mark:
        # the heavy tier runs only when asked for (``-m lead_gate``): a plain ``pytest`` (CI) runs the fast tier and
        # the rest of the service's suite; lead-gate tests are deselected, never skipped
        lead = [i for i in items if i.get_closest_marker("lead_gate") is not None]
        if lead:
            config.hook.pytest_deselected(items=lead)
            items[:] = [i for i in items if i.get_closest_marker("lead_gate") is None]


def pytest_sessionstart(session):
    _START["t"] = time.monotonic()


def pytest_runtest_setup(item):
    # the fast tier's hard limit (SUITE_PLAN 2): the session fails past FAST_SESSION_LIMIT_S
    if _START["fast"] and _START["t"] is not None and time.monotonic() - _START["t"] > FAST_SESSION_LIMIT_S:
        pytest.fail(f"conformance fast tier over {FAST_SESSION_LIMIT_S:.0f} s")
