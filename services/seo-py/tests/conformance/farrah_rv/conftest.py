"""Reviewer suite (rv-farrah1), ported into the permanent suite in round 2: the CB-13 hook of the builder's suite is
reused when present."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from conformance.gen.netaudit import ESCAPES, install
    install()
except Exception:  # noqa: BLE001
    ESCAPES = []


@pytest.fixture(autouse=True)
def _no_escape():
    before = len(ESCAPES)
    yield
    assert len(ESCAPES) == before


def pytest_configure(config):
    config.addinivalue_line("markers", "rv_finding: a reviewer test that FAILED on 3b99c9d5 (fixed in round 2)")
    config.addinivalue_line("markers", "lead_gate: the lead-gate lane (CONFORMANCE_LEAD=1)")


def pytest_collection_modifyitems(config, items):
    """Ported (round 2): the lead lane's items (the reviewer's mutants) run only with CONFORMANCE_LEAD=1."""
    if os.environ.get("CONFORMANCE_LEAD") == "1":
        return
    skip = pytest.mark.skip(reason="lead-gate lane: set CONFORMANCE_LEAD=1")
    for it in items:
        if "lead_gate" in it.keywords and "farrah_rv" in str(it.fspath):
            it.add_marker(skip)
