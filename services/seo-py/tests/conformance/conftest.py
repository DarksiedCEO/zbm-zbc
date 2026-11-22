"""The permanent conformance suite (SUITE_PLAN.md; priya-spec/1 for the ``test_priya_*`` modules). Two lanes:
``conformance`` (every push) and ``conformance_heavy`` (skipped unless ``CONFORMANCE_HEAVY=1``; the lead gate runs
it). Every value is synthetic and made at run time; failures print class / disguise / context ids and counts only."""

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

HEAVY = os.environ.get("CONFORMANCE_HEAVY") == "1"


def pytest_configure(config):
    config.addinivalue_line("markers", "conformance: conformance suite, CI lane (every push)")
    config.addinivalue_line("markers", "conformance_heavy: conformance suite, heavy lane (CONFORMANCE_HEAVY=1)")


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="heavy conformance lane: set CONFORMANCE_HEAVY=1")
    for item in items:
        if "conformance_heavy" in item.keywords and not HEAVY:
            item.add_marker(skip)
