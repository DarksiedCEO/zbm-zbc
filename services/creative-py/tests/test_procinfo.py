"""Fix wave 16: the portable helpers in _procinfo.py.

The live tests run on Linux in CI and on macOS on the founder's machine.
These tests run BOTH code paths wherever the tools exist (ps and lsof are
also present on Linux), so the macOS path is exercised here too, and pin
the lsof output formats macOS produces.
"""

from __future__ import annotations

import os
import shutil
import socket

import pytest

import _procinfo
from _procinfo import listening_addrs, listening_addrs_lsof, parse_lsof_fn, rss_kib, rss_kib_ps


def test_rss_is_one_process_in_kib_and_both_paths_agree():
    pid = os.getpid()
    ballast = bytearray(64 * 1024 * 1024)  # touch 64 MiB so RSS is clearly above noise
    for i in range(0, len(ballast), 4096):
        ballast[i] = 1
    a = rss_kib(pid)
    b = rss_kib_ps(pid)
    assert a >= 64 * 1024 and b >= 64 * 1024, (a, b)  # KiB, not bytes or pages
    assert abs(a - b) <= max(a, b) * 0.10, (a, b)
    del ballast


def test_rss_of_an_exited_pid_raises_not_zero():
    import subprocess
    import sys

    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    with pytest.raises((AssertionError, FileNotFoundError)):
        rss_kib(p.pid)
    with pytest.raises(AssertionError):
        rss_kib_ps(p.pid)


def _listener(host: str) -> socket.socket:
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    s.bind((host, 0))
    s.listen()
    return s


# No live 0.0.0.0 listener here (it would pop the macOS firewall prompt);
# the wildcard mapping is pinned by test_parse_lsof_fn_macos_formats.
@pytest.mark.parametrize("host,expected", [("127.0.0.1", "127.0.0.1")])
def test_listening_addrs_reports_the_bound_address(host, expected):
    with _listener(host) as s:
        port = s.getsockname()[1]
        assert listening_addrs(port) == {expected}
        if shutil.which("lsof"):
            assert listening_addrs_lsof(port) == {expected}  # the macOS code path
    assert listening_addrs(port) == set()


def test_listening_addrs_ipv6_when_available():
    if not _procinfo.can_bind("::1"):
        pytest.skip("no IPv6 loopback in this environment")
    with _listener("::1") as s:
        port = s.getsockname()[1]
        assert listening_addrs(port) == {"::1"}
        if shutil.which("lsof"):
            assert listening_addrs_lsof(port) == {"::1"}


def test_parse_lsof_fn_macos_formats():
    out = (
        "p4711\nf5\ntIPv4\nn127.0.0.1:8091\n"
        "f6\ntIPv6\nn[::1]:8091\n"
        "f7\ntIPv4\nn*:8091\n"
        "f8\ntIPv6\nn*:8091\n"
        "f9\ntIPv4\nn127.0.0.1:8091->127.0.0.1:50000\n"
        "f10\ntIPv4\nn127.0.0.1:18091\n"
    )
    assert parse_lsof_fn(out, 8091) == {"127.0.0.1", "::1", "0.0.0.0", "::"}
    assert parse_lsof_fn("", 8091) == set()
