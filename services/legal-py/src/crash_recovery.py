"""
Crash recovery for the record-first services (red team Oct 30-31 2026, CH-1 / CH-2 / CH-6; fix wave A, ADR 0025).
Shared byte-for-byte by finance-py, legal-py and compliance-py (``tests/test_crash_recovery_shared.py`` in each
checks the copies are identical).

The problem. A write records its rulings, then the ``local_log_appended`` anchor, on the evidence ledger BEFORE the
local line is fsynced (record-first). A SIGKILL / OOM / host loss inside that window, or a ledger answer lost in
transit, leaves ledger evidence the local log lacks. The start-up assessment (``i10.assess``) then calls it VOIDABLE
and the service refused to start until Andre ran the manual reconcile (FIN/LEGAL/COMPLIANCE_RECONCILE_MODE=1). A
second instance on the same ledger, a restored older volume, a truncated log or a forged event look exactly the
same to that assessment, which is why it never voided anything on its own.

The fix: an intent journal. Before the service asks the ledger to record a ruling, an anchor, a rules/register
version or a lease, it appends that event id to ``<log>.intent`` and fsyncs it. When the local line that names the
write is committed, its ids are taken out of the journal again. So the journal holds exactly the writes THIS log
started and never finished; the caller of every one of them got no 2xx (the answer is only sent after the local
append). At start (and while running, see ``LedgerWatch``):

- every voidable item of the assessment is in the journal -> it is crash residue: the service voids it itself
  (roll back), recorded on the ledger and in the local log as a ``reconcile`` with ``"automatic": true``, then
  re-assesses against the ledger (it must now be clean) and logs ``crash_recovered``;
- anything voidable that the journal does NOT announce, or anything fatal -> real divergence (another instance, a
  restored older volume, a truncated or edited log, a forged event): the service keeps refusing, as before, with a
  ``log_divergence`` ALERT line and the reason on /health (``startup_hold``).

Rolling back is safe because nothing in the window was acknowledged: an approval or a ruling is retried by its
caller (a new log position gives it new evidence ids), and a Stripe event is redelivered by Stripe.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Callable, Iterable, Optional

INTENT_SUFFIX = ".intent"
ABSENT_GRACE_S = 60.0           # an announced write the ledger never got is forgotten after this long

alert_log = logging.getLogger("zbm.alert")


def alert(event: str, service: str, **fields) -> None:
    """One structured, alert-able log line (JSON, one line, no secrets, ids only)."""
    alert_log.warning(json.dumps({"alert": event, "service": service, **fields}, sort_keys=True, default=str))


class IntentWriteError(Exception):
    """The intent journal could not be written: nothing may be recorded (fail closed)."""


class IntentJournal:
    """``<log>.intent``: one JSON line ``{"id": ..., "type": ...}`` per ledger write started and not yet named by a
    committed local line. ``note`` appends and fsyncs BEFORE the ledger call; ``settle`` removes ids (atomic
    rewrite). Without a log path (in-memory service) it is kept in memory only."""

    def __init__(self, log_path: Optional[str]):
        self.path = f"{log_path}{INTENT_SUFFIX}" if log_path else None
        self.lock = threading.Lock()
        self._ids: dict[str, str] = {}
        if self.path and os.path.exists(self.path):
            with open(self.path, "rb") as fh:
                raw = fh.read()
            for ln in raw.split(b"\n"):
                if not ln.strip():
                    continue
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue            # a torn last line: its fsync never returned, so its ledger call never ran
                if isinstance(d, dict) and isinstance(d.get("id"), str) and isinstance(d.get("type"), str):
                    self._ids[d["id"]] = d["type"]

    def __len__(self) -> int:
        with self.lock:
            return len(self._ids)

    def ids(self) -> dict[str, str]:
        with self.lock:
            return dict(self._ids)

    def note(self, event_id: str, event_type: str) -> None:
        with self.lock:
            if event_id in self._ids:
                return
            self._ids[event_id] = event_type
            if self.path is None:
                return
            line = json.dumps({"id": event_id, "type": event_type}, separators=(",", ":")).encode() + b"\n"
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    os.write(fd, line)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                del self._ids[event_id]
                raise IntentWriteError(f"intent journal not writable ({type(exc).__name__})") from None

    def settle(self, done: Iterable[str]) -> None:
        """Forget ``done``. A rewrite that fails leaves the old file: a superset is harmless (an id a committed line
        names is never voidable, so an extra entry can never make anything look like crash residue)."""
        with self.lock:
            changed = False
            for e in done:
                if self._ids.pop(e, None) is not None:
                    changed = True
            if changed and self.path is not None:
                try:
                    if not self._ids:
                        try:
                            os.unlink(self.path)
                        except FileNotFoundError:
                            pass
                        return
                    tmp = f"{self.path}.tmp"
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                    try:
                        os.write(fd, b"".join(json.dumps({"id": i, "type": t}, separators=(",", ":")).encode() + b"\n"
                                              for i, t in self._ids.items()))
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    os.replace(tmp, self.path)
                except OSError:
                    pass


# LedgerRecordError messages that say the ledger REFUSED this one event (a 409, a 4xx, a contract check): the ledger
# itself can write, so these never mark the service as unable to write
_REFUSAL_PREFIXES = ("ledger refused the event", "ledger event would be refused", "ledger event fails contract",
                     "ledger already holds a DIFFERENT record")


def cannot_write(exc: BaseException) -> bool:
    """True when a ledger record failure means the ledger cannot write now (unreachable, shed, 5xx, answer lost)."""
    return not str(exc).startswith(_REFUSAL_PREFIXES)


def unexplained(assessment, journal: dict[str, str]) -> Optional[str]:
    """None when every voidable item of ``assessment`` was announced by this log's intent journal (crash residue
    that may be rolled back automatically); otherwise why it is real divergence."""
    if assessment.fatal:
        return "; ".join(assessment.fatal)
    foreign = sorted(e for e in assessment.void_event_ids if e not in journal)
    if foreign:
        return (f"{len(foreign)} voidable ledger event(s) were not written by this log (first: {foreign[0]}): "
                + "; ".join(assessment.voidable))
    return None


def resolved(journal: dict[str, str], assessment, held: set[str], absent_since: dict[str, float], now: float,
             grace: float = ABSENT_GRACE_S) -> set[str]:
    """Journal ids that need nothing more: on the ledger and not voidable (committed, or an earlier attempt the
    log's own evidence explains), or absent from the ledger for ``grace`` seconds (the write never landed).
    ``absent_since`` is updated in place."""
    out: set[str] = set()
    for eid in journal:
        if eid in held:
            absent_since.pop(eid, None)
            if eid not in assessment.void_event_ids:
                out.add(eid)
        else:
            first = absent_since.setdefault(eid, now)
            if now - first >= grace:
                out.add(eid)
                absent_since.pop(eid, None)
    return out


class LedgerWatch:
    """CH-2 / CH-6: a daemon thread that keeps the service's view of "can I write?" current and retries pending
    ledger writes. Every ``idle_s`` it probes the ledger; while the intent journal holds an unfinished write or the
    ledger is unreachable it calls ``settle`` with exponential backoff (``base_s`` doubling to ``cap_s``). Both
    callables are the service's own (they take the service lock). Started by the API's ``main()`` only."""

    def __init__(self, name: str, settle: Callable[[], bool], probe: Callable[[], bool], pending: Callable[[], int],
                 idle_s: float = 15.0, base_s: float = 2.0, cap_s: float = 60.0):
        self.name, self.settle, self.probe, self.pending = name, settle, probe, pending
        self.idle_s, self.base_s, self.cap_s = idle_s, base_s, cap_s
        self._stop = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def start(self) -> "LedgerWatch":
        self.thread = threading.Thread(target=self._run, name=f"{self.name}-ledger-watch", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def step(self) -> bool:
        """One pass: True when the service can write and nothing is pending."""
        try:
            ok = self.probe()
            if ok and self.pending():
                ok = self.settle()
            return bool(ok) and not self.pending()
        except Exception:  # noqa: BLE001 - a failed pass is retried; only the type is logged
            alert("ledger_watch_error", self.name)
            return False

    def _run(self) -> None:
        delay = self.base_s
        while not self._stop.is_set():
            if self.step():
                delay = self.base_s
                wait = self.idle_s
            else:
                wait = delay
                delay = min(self.cap_s, delay * 2)
            self._stop.wait(wait)


def now_monotonic() -> float:
    return time.monotonic()
