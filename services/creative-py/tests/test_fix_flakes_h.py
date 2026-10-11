"""fix-flakes-h (the creative-py macOS CI flake, run 38110485481, Oct 11 2026): the server-CPU reader live tests use
(tests/_procinfo.py ``cpu_seconds``) — Linux /proc, macOS/BSD ``ps -o time=`` — never a wall-clock fallback.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from _procinfo import cpu_seconds, cpu_seconds_ps, parse_ps_time


@pytest.mark.parametrize("text,seconds", [
    ("0:00.07", 0.07),            # macOS, under a minute
    ("75:03.21", 4503.21),        # macOS minutes past 59
    ("1:02:03.45", 3723.45),      # hours
    ("00:00:01", 1.0),            # procps HH:MM:SS
    ("2-03:04:05", 2 * 86400 + 3 * 3600 + 4 * 60 + 5),  # procps days
    ("12", 12.0),
    ("  0:01.50\n", 1.5),
])
def test_ps_time_formats_parse_to_seconds(text, seconds):
    assert parse_ps_time(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "abc", "1-05", "1:2:3:4", "0:00,07", "-1:00.00"])
def test_a_ps_time_that_is_not_one_raises(text):
    with pytest.raises(ValueError):
        parse_ps_time(text)


BURNER = (
    "import sys, time\n"
    "while time.process_time() < 1.2:\n"
    "    pass\n"
    "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
    "sys.stdin.read()\n"
)


def test_cpu_seconds_reads_the_childs_cpu_and_the_ps_path_agrees():
    """A child that burned >= 1.2 s of its OWN CPU (then blocks): both readers see it — /proc on Linux at 10 ms, ps at
    whole seconds on procps / 10 ms on macOS — and they agree within ps's resolution."""
    p = subprocess.Popen([sys.executable, "-c", BURNER], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert p.stdout.readline() == b"ready\n"
        a = cpu_seconds(p.pid)
        b = cpu_seconds_ps(p.pid)
        assert a >= 1.1, a              # 10 ms ticks, truncated
        assert b >= 1.0 and abs(a - b) <= 1.0, (a, b)
    finally:
        p.stdin.close()
        p.wait()
        p.stdout.close()


def test_cpu_seconds_of_an_exited_pid_raises_never_zero_or_wall():
    """Both readers refuse a gone process (cpu_seconds_ps is the whole macOS path): never 0, never a wall time."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    with pytest.raises(AssertionError):
        cpu_seconds(p.pid)
    with pytest.raises(AssertionError):
        cpu_seconds_ps(p.pid)
