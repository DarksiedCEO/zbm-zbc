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
that fails (ledger down, a 409 while a watcher cycle runs) is printed and retried at the next tick. Nothing here can
open a gate: it only produces the evidence the gates require.
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
                 out: Callable[[str], None] = lambda line: print(line, flush=True)):
        self.svc, self.interval = svc, timedelta(seconds=interval_s)
        self.watcher, self.clock, self.out = watcher, clock or SystemClock(), out
        self.last_controls: Optional[datetime] = None
        self.last_watcher: Optional[datetime] = None
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
            self.out(f"compliance scheduler: {name} run failed at {iso(now)} "
                     f"({type(exc).__name__}: {str(getattr(exc, 'reason', ''))[:200]}); retried at the next tick")
            return False
        self.out(f"compliance scheduler: {name} run at {iso(now)}")
        return True

    def tick(self) -> None:
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
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(TICK_S)

    def start(self) -> "Scheduler":
        self._thread = threading.Thread(target=self._loop, name="compliance-scheduler", daemon=True)
        self._thread.start()
        self.out(f"compliance scheduler: controls every {int(self.interval.total_seconds())} s; Change Watcher "
                 + ("every 24 h" if self.watcher else "off (COMPLIANCE_WATCHER_ENABLED is not 1)"))
        return self

    def stop(self) -> None:
        self._stop.set()


__all__ = ["Scheduler"]
