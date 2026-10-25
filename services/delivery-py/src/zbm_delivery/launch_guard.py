"""
Launcher guards shared byte-for-byte by the Python services' launchers (fix wave 26b; scout C5-3, C5-4).
devtools/hygiene_check.py (rule L4) fails the build if the copies differ. Standard library only.

1. The GIL switch interval (C5-3). A thread holds the GIL for ``sys.getswitchinterval()`` (CPython's default
   5 ms) before another thread that wants it is served; every launcher sets 1 ms (fix wave 7, NEW-5). The
   override ``<PREFIX>_SWITCH_INTERVAL_SECONDS`` used to be range-checked only in detection-py (fix wave 24
   F3 / wave 25 H5) and copied by hand into four other launchers; four more accepted any positive number —
   3600 s (a thread may hold the GIL for an hour: what the setting exists for is gone), 1e-9 s (a switch
   storm), and nan, which passes a ``<= 0`` check. One check now: only SWITCH_INTERVAL_MIN_US ..
   SWITCH_INTERVAL_MAX_US (100 us .. 50 ms) starts (unset or blank: 1 ms), and the interval actually in
   force is compared in whole microseconds (CPython keeps it truncated to the microsecond: 0.0001 reads back
   as 9.999999999999999e-05, below a float bound of 0.0001).

2. SIGTERM (C5-4). uvicorn captures SIGTERM, shuts down gracefully and then RE-RAISES the signal with the
   disposition it found at start. With the default disposition the process dies of the signal and no
   ``atexit`` handler runs (delivery-py's temp dirs were left behind that way, wave 24 E6). The launchers
   serve inside ``sigterm_exits()``, so a stop is a normal interpreter exit with status 143 (128 + SIGTERM)
   and exit handlers run — also for a SIGTERM that arrives before uvicorn installs its own. The previous
   disposition is restored when serving returns, so a test that drives a launcher in-process with uvicorn
   stubbed leaves its own process's SIGTERM handling as it was.

3. The IPv6 wildcard bind (golive-combined, CI railway-images). A launcher given ``::`` (or ``[::]``) used to
   hand it to ``uvicorn.run``, which binds through asyncio's ``create_server``; asyncio sets IPV6_V6ONLY on
   every IPv6 socket it binds itself, so the service listened on IPv6 only and every IPv4 connection was
   refused (``ConnectionResetError`` through Docker's IPv4 port mapping, for all four Railway Python images;
   on Railway an IPv4 health check or an IPv4 private-network peer fails the same way, while the Go and Rust
   services, which bind ``[::]`` dual-stack, worked). ``serve()`` now binds the IPv6 wildcard itself with
   IPV6_V6ONLY cleared (one socket, IPv4 and IPv6 — what Go and Rust do) and hands uvicorn that socket; any
   other host is passed to ``uvicorn.run`` exactly as before. Binding is the only change: the protocol,
   limits and every guard are uvicorn's configuration as passed in.
"""

from __future__ import annotations

import contextlib
import math
import os
import signal
import socket
import sys

SWITCH_INTERVAL_DEFAULT_S = 0.001
SWITCH_INTERVAL_MIN_US = 100
SWITCH_INTERVAL_MAX_US = 50_000
SWITCH_INTERVAL_MIN_S = SWITCH_INTERVAL_MIN_US / 1_000_000   # 0.0001
SWITCH_INTERVAL_MAX_S = SWITCH_INTERVAL_MAX_US / 1_000_000   # 0.05


def switch_interval_from_env(name: str, default: float = SWITCH_INTERVAL_DEFAULT_S, error: type = RuntimeError) -> float:
    """The interval ``name`` asks for (unset or blank: ``default``). Raises ``error`` (the service refuses to
    start) unless SWITCH_INTERVAL_MIN_S <= value <= SWITCH_INTERVAL_MAX_S; nan, inf and text are refused."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (SWITCH_INTERVAL_MIN_S <= value <= SWITCH_INTERVAL_MAX_S):  # refuses nan and inf too
        raise error(f"{name}={raw!r} is invalid: expected seconds, {SWITCH_INTERVAL_MIN_S:g} <= value "
                    f"<= {SWITCH_INTERVAL_MAX_S:g}. This service refuses to start.")
    return value


def apply_switch_interval(value: float, name: str, error: type = RuntimeError) -> int:
    """Sets the interval and returns the one in force, in whole microseconds (as CPython keeps it). Raises
    ``error`` unless it is within [SWITCH_INTERVAL_MIN_US, SWITCH_INTERVAL_MAX_US] and is ``value`` to within
    the microsecond CPython truncates. ``name`` is the setting the value came from (for the message)."""
    sys.setswitchinterval(value)
    in_force_us = round(sys.getswitchinterval() * 1_000_000)
    if not (SWITCH_INTERVAL_MIN_US <= in_force_us <= SWITCH_INTERVAL_MAX_US) or abs(in_force_us - value * 1_000_000) >= 1:
        raise error(f"the GIL switch interval in force is {in_force_us} us, not the {value!r} s set "
                    f"({name}): this service refuses to start")
    return in_force_us


def exit_on_sigterm(signum, frame) -> None:
    """The SIGTERM handler: a normal interpreter exit (SystemExit 128 + signum), so exit handlers run."""
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def sigterm_exits():
    """Within the block, SIGTERM ends this process through a normal exit (enter it in the main thread, around
    the call that serves); the previous disposition is restored on the way out."""
    previous = signal.signal(signal.SIGTERM, exit_on_sigterm)
    try:
        yield
    finally:
        if previous is not None:          # None: a handler not installed from Python; nothing to put back
            signal.signal(signal.SIGTERM, previous)


IPV6_WILDCARDS = ("::", "[::]")
STARTUP_FAILURE = 3          # uvicorn's own exit status when the server did not start
BIND_FAILURE = 1             # uvicorn's exit status when its bind fails (it logs the error and exits 1)


def dual_stack_socket(host: str, port: int):
    """A bound (not yet listening) TCP socket on the IPv6 wildcard with IPV6_V6ONLY cleared, so it accepts IPv4
    (as v4-mapped addresses) and IPv6; ``None`` for any other host (uvicorn binds those itself, unchanged).
    SO_REUSEADDR as asyncio sets it. Raises OSError if the bind fails (the caller exits like uvicorn does)."""
    if host.strip() not in IPV6_WILDCARDS:
        return None
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(("::", port))
    except BaseException:
        sock.close()
        raise
    return sock


def serve(uvicorn_mod, app, host: str, port: int, name: str, **kwargs) -> None:
    """Serve ``app`` with ``uvicorn_mod`` (the launcher's uvicorn) and ``kwargs`` (its configuration). The IPv6
    wildcard is bound dual-stack here (section 3); every other host goes to ``uvicorn_mod.run`` as before.
    ``name`` is the service, for the one line saying where it listens."""
    try:
        sock = dual_stack_socket(host, port)
    except OSError as exc:
        print(f"{name}: cannot bind [::]:{port}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(BIND_FAILURE) from None
    if sock is None:
        uvicorn_mod.run(app, host=host, port=port, **kwargs)
        return
    try:
        config = uvicorn_mod.Config(app, host=host, port=port, **kwargs)
        server = uvicorn_mod.Server(config=config)
        print(f"{name}: listening on [::]:{port} (dual-stack: IPv4 and IPv6)", file=sys.stderr, flush=True)
        server.run(sockets=[sock])
    finally:
        sock.close()
    if not server.started:
        raise SystemExit(STARTUP_FAILURE)
