"""The permanent conformance suite of core-spec/1 (PR #51; SUITE_PLAN.md section 1).

Two lanes, by marker:
  conformance        every test of this folder (added here); the CI lane, run on every push with the service's suite
                     and alone with:   python -m pytest -q -p no:cacheprovider -m conformance tests/conformance
                     (budget < 10 minutes single process)
  conformance_heavy  the lead gate's heavy lane: skipped unless CONFORMANCE_HEAVY=1, sharded by CONFORMANCE_SHARD=i/n:
                     CONFORMANCE_HEAVY=1 CONFORMANCE_SHARD=0/2 python -m pytest -q -p no:cacheprovider \\
                         -m conformance_heavy tests/conformance
                     The over-tightening corpora (O-4 W) need wordfreq==3.1.1 and pythainlp==5.4.0 in the gate's OWN
                     venv (never a repo dependency: core-spec/1 Q-5); without them those tests skip with the reason.
The suite only grows (``registry.json``, ``test_registry.py``). Failures print entry ids, SP / SEP ids, lanes and
counts, never a label text or a value."""
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def pytest_configure(config):
    config.addinivalue_line("markers", "conformance: core-spec/1 conformance suite, CI lane")
    config.addinivalue_line("markers", "conformance_heavy: core-spec/1 conformance suite, heavy lane (lead gate)")
    config.addinivalue_line("markers", "core2_ci: core-spec/2 G-2 CI lane (lanes_core2.CI; see LANES_CORE2.md)")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(session, config, items):
    heavy_on = os.environ.get("CONFORMANCE_HEAVY") == "1"
    skip = pytest.mark.skip(reason="heavy conformance lane: set CONFORMANCE_HEAVY=1 (lead gate)")
    from conformance import lanes_core2
    for it in items:
        if lanes_core2.is_ci(it.fspath):
            it.add_marker(pytest.mark.core2_ci)
        if not str(it.fspath).startswith(HERE):
            continue
        it.add_marker(pytest.mark.conformance)
        if it.get_closest_marker("conformance_heavy") and not heavy_on:
            it.add_marker(skip)


def shard() -> tuple:
    """(i, n) from CONFORMANCE_SHARD ("0/1" when unset)."""
    s = os.environ.get("CONFORMANCE_SHARD", "0/1")
    i, n = s.split("/")
    return int(i), int(n)
