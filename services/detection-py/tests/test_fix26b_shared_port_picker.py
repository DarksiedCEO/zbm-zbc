"""
Fix wave 26b (scout C5-6): detection-py's live tests pick ports with the shared helper (tests/_procinfo.py), not a
picker of their own. ``test_request_limits_live._free_port`` was a pick-then-bind picker reading only
DETECTION_LIVE_TEST_PORTS (once, at import); it is now ``_procinfo.pick_port`` over
``assigned_port_range("DETECTION_LIVE_TEST_PORTS")``, so the repo-wide ZBM_TEST_PORT_RANGE is honoured too. Its
callers already accept a server only once the server has announced its own bind on the port.
"""

from __future__ import annotations

import _procinfo
import test_request_limits_live as live


def test_the_live_port_picker_is_the_shared_one_and_honours_the_repo_wide_range(monkeypatch):
    monkeypatch.setattr(_procinfo, "_HANDED_OUT", set())
    monkeypatch.delenv("DETECTION_LIVE_TEST_PORTS", raising=False)
    lo = _procinfo.pick_port()                 # some port that is free now
    monkeypatch.setenv("ZBM_TEST_PORT_RANGE", f"{lo}-{lo}")
    assert live._free_port() == lo
    assert live._free_port() == lo             # a range of one: handed out again once free (the shared rule)
