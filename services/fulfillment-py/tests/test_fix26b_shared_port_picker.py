"""
Fix wave 26b (scout C5-6): fulfillment-py's live tests pick ports with the shared helper (tests/_procinfo.py), not a
picker of their own. ``test_live_server._free_port`` (imported by the other live modules) was a pick-then-bind
picker reading only FULFILLMENT_TEST_PORT_RANGE; it is now ``_procinfo.pick_port`` over
``assigned_port_range("FULFILLMENT_TEST_PORT_RANGE")``, so the repo-wide ZBM_TEST_PORT_RANGE is honoured too. The
launchers already accept a server only once it has announced its own bind (test_fix25_start_owns_port.py).
"""

from __future__ import annotations

import _procinfo
import test_live_server as live


def test_the_live_port_picker_is_the_shared_one_and_honours_the_repo_wide_range(monkeypatch):
    monkeypatch.setattr(_procinfo, "_HANDED_OUT", set())
    monkeypatch.delenv("FULFILLMENT_TEST_PORT_RANGE", raising=False)
    lo = _procinfo.pick_port()                 # some port that is free now
    monkeypatch.setenv("ZBM_TEST_PORT_RANGE", f"{lo}-{lo}")
    assert live._free_port() == lo
    assert live._free_port() == lo             # a range of one: handed out again once free (the shared rule)
