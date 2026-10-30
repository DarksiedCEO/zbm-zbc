"""
In-process scheduler (ADR 0024): the daily job the audit's minimal path asked for, without a second Railway service
or a ``scheduler`` token held anywhere.

- Internal controls (``ComplianceService.run_internal_controls``: C-01, C-02, C-04, C-05, C-11, C-12, C-15, C-17,
  and the re-verification proposals drafted from fresh snapshots) at start-up and then every
  COMPLIANCE_SCHEDULER_INTERVAL_SECONDS (default 6 h, inside C-11's 24 h SLA with room for a failed run or two).
  C-11 (evidence integrity) blocks every gate until it has passed once, and again whenever it is older than 24 h.
- The Change Watcher (``watcher_run``) once every 24 h, only when COMPLIANCE_WATCHER_ENABLED=1; it drafts proposals
  for Andre, it never changes the register.
- After each run the readiness summary is printed (expired rules and the gates they affect).

Each run is the same service call the ``scheduler`` caller would make over HTTP, recorded on the ledger first; a run
that fails (ledger down, a 409 while a watcher cycle runs) is printed and retried at the next 60 s tick (not at the
next interval). Any other error in a tick (the readiness summary, the clock...) is caught and printed too: the
thread never dies of an exception (AEGIS F-4).

Liveness (``liveness()``, reported in ``GET /health``): alive (the thread runs), the last successful controls and
watcher runs, the last error. Unhealthy when the thread is dead, or when the last successful controls run (or the
start, before the first one) is older than two intervals: ``/health`` then answers 503.

A thread that ends without ``stop()`` (only something ``except Exception`` does not catch can end it) prints one line
and calls ``on_thread_death(1)``; ``api.main`` passes a hook that exits the process non-zero, so Railway's ON_FAILURE
policy restarts the service (and its first tick re-runs the controls). Failing or stale controls (ledger down) never
exit: that would restart in a loop; they stay fail closed through C-11 and show as the 503. Nothing here can open a
gate: it only produces the evidence the gates require.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import readiness as readiness_mod
from clock import Clock, SystemClock, iso

TICK_S = 60.0
WATCHER_EVERY = timedelta(hours=24)


class Scheduler:
    def __init__(self, svc, interval_s: int = 21600, watcher: bool = False, clock: Optional[Clock] = None,
                 out: Callable[[str], None] = lambda line: print(line, flush=True),
                 on_thread_death: Optional[Callable[[int], None]] = None):
        self.svc, self.interval = svc, timedelta(seconds=interval_s)
        self.watcher, self.clock, self.out = watcher, clock or SystemClock(), out
        self.last_controls: Optional[datetime] = None
        self.last_watcher: Optional[datetime] = None
        self.started_at: Optional[datetime] = None
        self.last_error: Optional[dict] = None
        self.on_thread_death = on_thread_death
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def due_controls(self) -> bool:
        return self.last_controls is None or self._now() - self.last_controls >= self.interval

    def due_watcher(self) -> bool:
        return self.watcher and (self.last_watcher is None or self._now() - self.last_watcher >= WATCHER_EVERY)

    def _run(self, name: str, fn) -> bool:
        now = self._now()
        try:
            fn(f"sched-{name}-{now.strftime('%Y%m%dT%H%M%SZ')}")
        except Exception as exc:  # noqa: BLE001 - printed (type and reason only) and retried at the next tick
            self._failed(name, now, exc)
            return False
        self.out(f"compliance scheduler: {name} run at {iso(now)}")
        return True

    def _failed(self, where: str, now: datetime, exc: BaseException) -> None:
        err = f"{type(exc).__name__}: {str(getattr(exc, 'reason', '') or '')[:200]}".rstrip(": ")
        self.last_error = {"at": iso(now), "where": where, "error": err}
        self.out(f"compliance scheduler: {where} run failed at {iso(now)} ({err}); retried at the next tick")

    def tick(self) -> None:
        """One pass; never raises (AEGIS F-4): an error anywhere is recorded and printed, the thread goes on."""
        try:
            self._tick()
        except Exception as exc:  # noqa: BLE001
            now = self._now()
            err = f"{type(exc).__name__}: {str(getattr(exc, 'reason', '') or exc)[:200]}"
            self.last_error = {"at": iso(now), "where": "tick", "error": err}
            self.out(f"compliance scheduler: tick failed at {iso(now)} ({err}); retried at the next tick")

    def _tick(self) -> None:
        ran = False
        if self.due_controls() and self._run("controls", self.svc.run_internal_controls):
            self.last_controls = self._now()
            ran = True
        if self.due_watcher() and self._run("watcher", self.svc.watcher_run):
            self.last_watcher = self._now()
            ran = True
        if ran:
            for line in readiness_mod.summary_lines(self.svc.readiness()):
                self.out(line)

    def _loop(self) -> None:
        cause = "the loop returned"
        try:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(TICK_S)
        except BaseException as exc:  # noqa: BLE001 - reported below; the process is then ended by the hook
            cause = type(exc).__name__
        finally:
            if not self._stop.is_set():
                self.out(f"compliance scheduler: scheduler thread stopped unexpectedly ({cause}) at "
                         f"{iso(self._now())}; exiting non-zero so the platform restarts the service")
                if self.on_thread_death is not None:
                    self.on_thread_death(1)

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def liveness(self) -> dict:
        now = self._now()
        ref = self.last_controls or self.started_at
        reason = None
        if not self.alive():
            reason = "scheduler thread is not running"
        elif ref is not None and now - ref > 2 * self.interval:
            reason = (f"no successful controls run for more than two intervals "
                      f"({int(2 * self.interval.total_seconds())} s)")
        return {"enabled": True, "alive": self.alive(), "healthy": reason is None, "reason": reason,
                "interval_s": int(self.interval.total_seconds()),
                "started_at": iso(self.started_at) if self.started_at else None,
                "last_controls_ok_at": iso(self.last_controls) if self.last_controls else None,
                "last_watcher_ok_at": iso(self.last_watcher) if self.last_watcher else None,
                "last_error": self.last_error}

    def start(self) -> "Scheduler":
        self.started_at = self._now()
        self._thread = threading.Thread(target=self._loop, name="compliance-scheduler", daemon=True)
        self._thread.start()
        self.out(f"compliance scheduler: controls every {int(self.interval.total_seconds())} s; Change Watcher "
                 + ("every 24 h" if self.watcher else "off (COMPLIANCE_WATCHER_ENABLED is not 1)"))
        return self

    def stop(self) -> None:
        self._stop.set()


__all__ = ["Scheduler"]
