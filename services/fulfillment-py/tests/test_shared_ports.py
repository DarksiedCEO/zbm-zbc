"""Fix wave 25 (scout C5-6): the shared port helper in _procinfo.py. This file is byte-identical in every service
that carries _procinfo.py (devtools/hygiene_check.py, rule L4).

A live test may only trust a port its OWN child is listening on: ``wait_owned`` checks the LISTEN socket's owner,
and ``start_owned`` retries when the child lost the port to another process (the pick-then-bind race every
per-service picker had)."""
from __future__ import annotations

import socket
import subprocess
import sys

import pytest

import _procinfo
from _procinfo import ChildExited, assigned_port_range, listener_owned_by, pick_port, start_owned, wait_owned

CHILD = r'''
import socket, sys, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen(8)
sys.stdin.read()          # until the test closes our stdin
'''


def _child(port: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", CHILD, str(port)], stdin=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)


def _stop(p: subprocess.Popen) -> None:
    p.stdin.close()
    p.wait(timeout=30)


def test_one_knob_and_its_legacy_fallbacks(monkeypatch):
    monkeypatch.delenv("ZBM_TEST_PORT_RANGE", raising=False)
    monkeypatch.delenv("X_LEGACY_RANGE", raising=False)
    assert assigned_port_range("X_LEGACY_RANGE") is None
    monkeypatch.setenv("X_LEGACY_RANGE", "30010-30012")
    r = assigned_port_range("X_LEGACY_RANGE")              # parser inputs only: nothing binds these ports
    assert (r.start, r.stop) == (30010, 30013)
    monkeypatch.setenv("ZBM_TEST_PORT_RANGE", "30020-30020")
    r = assigned_port_range("X_LEGACY_RANGE")
    assert (r.start, r.stop) == (30020, 30021)
    for bad in ("x", "5-4", "80-90", "30000"):
        monkeypatch.setenv("ZBM_TEST_PORT_RANGE", bad)
        with pytest.raises(ValueError):
            assigned_port_range()


def test_the_owner_check_accepts_only_the_child_that_listens():
    port = pick_port()
    p = _child(port)
    try:
        wait_owned(p, port)
        assert listener_owned_by(p.pid, port)
        import os
        assert not listener_owned_by(os.getpid(), port)
    finally:
        _stop(p)


def test_a_child_that_lost_its_port_to_a_stranger_is_never_trusted():
    stranger = socket.socket()
    stranger.bind(("127.0.0.1", 0))
    stranger.listen(1)
    port = stranger.getsockname()[1]
    p = _child(port)                       # its bind fails: the stranger holds the port
    try:
        with pytest.raises(ChildExited):
            wait_owned(p, port)
    finally:
        p.kill()
        p.wait()
        stranger.close()


def test_start_owned_retries_on_another_port_after_losing_the_race(monkeypatch):
    strangers, started = [], []

    def start(port):
        if not strangers:                  # first attempt: someone takes the port between the pick and the bind
            s = socket.socket()
            s.bind(("127.0.0.1", port))
            s.listen(1)
            strangers.append(s)
        p = _child(port)
        started.append((p, port))
        return p

    try:
        proc, port = start_owned(start)
        assert len(started) == 2 and port != started[0][1] and listener_owned_by(proc.pid, port)
        assert started[0][0].returncode is not None      # the loser was reaped
        _stop(proc)
    finally:
        for s in strangers:
            s.close()
        for p, _ in started:
            if p.poll() is None:
                p.kill()
                p.wait()


def test_a_range_hands_each_port_out_once_then_only_a_free_one_again(monkeypatch):
    """Fix wave 26b (C5-6): a range is handed out port by port first; once every port of it has been, a port that is
    free again is handed out again (the callers accept a port only once their own child holds it), and a range with
    no free port left raises."""
    monkeypatch.setattr(_procinfo, "_HANDED_OUT", set())
    lo = pick_port()                       # some currently free port; the range around it may be partly taken
    ports = range(lo, lo + 1)
    assert pick_port(ports) == lo
    assert pick_port(ports) == lo          # handed out before, free again
    holder = socket.socket()
    try:
        holder.bind(("127.0.0.1", lo))
        holder.listen(1)
        with pytest.raises(RuntimeError):
            pick_port(ports)
    finally:
        holder.close()
