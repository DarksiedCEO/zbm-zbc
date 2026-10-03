"""
The supported way to run detection-py: `cd src && python3 serve.py [--host H] [--port P]`.

Fix wave 1 (Sep 24 2026, sweep of the "no body-size limit" finding): the
previous launch command, `python3 -m uvicorn api:app`, picks uvicorn's
httptools parser (installed by uvicorn[standard]), which puts NO limit on
the size of a request head — a request with a 20 MB header was read into
memory and answered 200. This launcher pins uvicorn's h11 parser with
h11_max_incomplete_event_size = api.MAX_HEADER_BYTES, so an oversized
request line or header block is refused (400) while it is still being read,
and adds a request-head deadline (REQUEST_HEAD_TIMEOUT_S) that uvicorn lacks.
Body limits are enforced by api._BodyLimitMiddleware under any launcher.
"""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

import launch_guard
from graceful_close import DRAIN_MAX_BYTES, DRAIN_TIMEOUT_S, GracefulCloseMixin, drains_max_from_env  # noqa: F401

from api import MAX_HEADER_BYTES

# uvicorn has no request-head timeout: a client that sends its request line
# and headers one byte at a time (slowloris) held a connection open for as
# long as it kept trickling (measured: still open after 20 s). The event loop
# was not blocked, but every such connection is held forever.
REQUEST_HEAD_TIMEOUT_S = 10.0


# The switch interval (fix wave 23; the class fixed in onboarding-py and the
# other serve.py launchers in fix wave 7, NEW-5, that detection-py missed):
# the interpreter lets a thread hold the GIL for sys.getswitchinterval()
# (5 ms by default) before another thread that wants it is served. A
# worst-case ~28 MiB batch is one CPU-bound parse of ~0.5 s in the threadpool,
# and GET /health needs the GIL on the event loop several times on its way
# through (accept, read, route, write) — each time it gave the GIL up for a
# syscall it waited up to a full slice to get it back, behind the parse and
# behind the loop's other work (503 refusals and their drains). Found on the
# w23 box: 5 ms slices -> max 0.44-0.51 s, the event loop's own lag never over
# 0.13 s (the time was GIL re-acquisition, not one long block).
# Measured (fix wave 24, Oct 1 2026; this 2-CPU box, Python 3.13.13; the live
# test test_health_latency_bound_under_16_concurrent_worst_case_batches: 16
# clients sending ~28 MiB worst-case batches, /health time to first byte from a
# prober in its own process; 5 runs per row):
#   1 ms, no other load ............ p50 11-17 ms, max 0.10-0.16 s
#   1 ms, three busy loops ......... p50 6-8 ms,   max 0.21-0.27 s
#   5 ms, three busy loops ......... p50 11-14 ms, max 0.23-0.45 s
# (The wave-23 notes said 0.09-0.10 s here and 0.11-0.15 s in api.py: single
# sessions under unstated load; AEGIS round 23, three busy loops: 1 ms max
# 0.11-0.17 s, 5 ms 0.12-0.24 s.) The test's bound, 0.5 s, is unchanged.
# DETECTION_SWITCH_INTERVAL_SECONDS overrides the interval.
#
# Fix wave 24, F3 (AEGIS N23-S-3): the override accepted anything in (0, 1) —
# 0.5 s (a thread could hold the GIL for half a second: the bound this setting
# exists for is gone) or 1e-7 s (a switch storm) started the service. Only
# SWITCH_INTERVAL_MIN_S <= value <= SWITCH_INTERVAL_MAX_S (100 us .. 50 ms)
# starts; and the launcher checks the interval actually in force
# (sys.getswitchinterval() after setting it, to the microsecond CPython keeps)
# before it serves — anything else refuses to start.
#
# Fix wave 25, H5 (AEGIS N24-S-6): the in-force check compared floats, and
# CPython keeps the interval as a whole number of microseconds (it truncates
# 1e6 x the value): 0.0001 is kept as 100 us and read back as
# 9.999999999999999e-05, below the 0.0001 float bound — the launcher refused
# the range's own lower end. The interval in force is now compared in integer
# microseconds, round(getswitchinterval() x 1e6), against [100, 50000], and
# with the value set to within the microsecond CPython truncates. The value
# in force is printed at start (stderr) so a launcher-level check can read it.
#
# Fix wave 26b (scout C5-3): this check was not shared — four launchers carried hand copies and four others accepted
# any positive interval. It now lives in src/launch_guard.py, byte-identical in every service; these names stay for
# this launcher's callers and tests.
SWITCH_INTERVAL_MIN_US = launch_guard.SWITCH_INTERVAL_MIN_US
SWITCH_INTERVAL_MAX_US = launch_guard.SWITCH_INTERVAL_MAX_US
SWITCH_INTERVAL_MIN_S = launch_guard.SWITCH_INTERVAL_MIN_S   # 0.0001: the env value's own range check
SWITCH_INTERVAL_MAX_S = launch_guard.SWITCH_INTERVAL_MAX_S   # 0.05


def _switch_interval_from_env(name: str = "DETECTION_SWITCH_INTERVAL_SECONDS", default: float = 0.001) -> float:
    return launch_guard.switch_interval_from_env(name, default)


def _apply_switch_interval(value: float) -> int:
    """Sets the interval and returns the one in force, in whole microseconds (as CPython keeps it); refuses
    (RuntimeError) unless it is within [SWITCH_INTERVAL_MIN_US, SWITCH_INTERVAL_MAX_US] and is `value` to within the
    microsecond CPython truncates (launch_guard)."""
    return launch_guard.apply_switch_interval(value, "DETECTION_SWITCH_INTERVAL_SECONDS")


SWITCH_INTERVAL_S: float = _switch_interval_from_env()


# Graceful close (fix wave 21, L1) with the wave-22 bounds (G5/G6: bounded reads
# through one shared buffer; the concurrency slot and the answered request's
# buffered body released before the drain; the drain discards in that buffer; at
# most DRAINS_MAX (DETECTION_DRAINS_MAX, default 512) drains at once) — the module shared
# byte-for-byte by the ten Python services (src/graceful_close.py).
DRAINS_MAX: int = drains_max_from_env("DETECTION_DRAINS_MAX")


class _HeadDeadlineH11Protocol(GracefulCloseMixin, H11Protocol):
    """uvicorn's h11 protocol plus a request-head deadline: a connection
    whose next request head has not fully arrived within
    REQUEST_HEAD_TIMEOUT_S (counted from connect, or from the end of the
    previous response) is closed. The body has its own deadline
    (api.BODY_READ_TIMEOUT_S); idle keep-alive stays uvicorn's 5 s."""

    drains_max = DRAINS_MAX

    _head_timer = None
    _head_cycle = None

    def _arm_head_deadline(self) -> None:
        self._disarm_head_deadline()
        self._head_cycle = self.cycle
        self._head_timer = self.loop.call_later(REQUEST_HEAD_TIMEOUT_S, self._head_deadline_passed)

    def _disarm_head_deadline(self) -> None:
        if self._head_timer is not None:
            self._head_timer.cancel()
            self._head_timer = None

    def _head_deadline_passed(self) -> None:
        self._head_timer = None
        if not self.transport.is_closing():
            self.transport.close()

    def connection_made(self, transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        self._arm_head_deadline()

    def handle_events(self) -> None:
        super().handle_events()
        # A new RequestResponseCycle means a complete request head was parsed.
        if self._head_timer is not None and self.cycle is not self._head_cycle:
            self._disarm_head_deadline()

    def on_response_complete(self) -> None:
        # Armed first: the parent may parse a pipelined request head right
        # away (via handle_events above), which disarms it again.
        self._arm_head_deadline()
        super().on_response_complete()

    def connection_lost(self, exc) -> None:
        self._disarm_head_deadline()
        super().connection_lost(exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the detection-py REST service.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    in_force_us = _apply_switch_interval(SWITCH_INTERVAL_S)
    print(f"detection-py: GIL switch interval in force: {in_force_us} us", file=sys.stderr, flush=True)
    # Fix wave 26b (scout C5-4): uvicorn re-raises the SIGTERM it captured after its graceful shutdown; with the
    # default disposition the process died of it and no atexit handler ran. Inside sigterm_exits() the stop is a
    # normal exit (status 143) and exit handlers run.
    with launch_guard.sigterm_exits():
        uvicorn.run(
            "api:app",
            host=args.host,
            port=args.port,
            http=_HeadDeadlineH11Protocol,
            h11_max_incomplete_event_size=MAX_HEADER_BYTES,
            timeout_keep_alive=5,
        )


if __name__ == "__main__":
    main()
