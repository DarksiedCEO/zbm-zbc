"""
Crash recovery for the record-first services (red team Oct 30-31 2026, CH-1 / CH-2 / CH-6; fix wave A, ADR 0025).
Shared byte-for-byte by finance-py, legal-py and compliance-py (devtools/hygiene_check.py L4 checks the copies are
identical).

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

- every voidable item of the assessment is in the journal AND none of them is an anchor (AEGIS wave A F-2: without
  its anchor the line can never have been appended, so nothing was acknowledged) -> it is crash residue: the service
  voids it itself (roll back), recorded on the ledger and in the local log as a ``reconcile`` with ``"automatic": true``, then
  re-assesses against the ledger (it must now be clean) and logs ``crash_recovered``;
- an interrupted write that reached its anchor (a crash after the anchor, or a volume restored from a snapshot taken
  mid-write after which the write was acknowledged: indistinguishable here), a lease the volume does not hold (a
  restored or cloned volume), anything voidable the journal does NOT announce, or anything fatal -> real divergence (another instance, a
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


# AEGIS wave A F-2: event types recorded only once the write's local line was prepared AND anchored. If one of them is
# on the ledger, the line may have been appended and acknowledged before the volume was restored or cloned: never
# void it automatically.
ANCHORED_TYPES = frozenset({"local_log_appended", "rules_version_published", "register_version_published"})
LEASE_TYPE = "instance_lease"


def classify(assessment, journal: dict[str, str], ledger_types: dict[str, str]) -> Optional[tuple[str, str]]:
    """None when automatic rollback is PROVABLY safe; otherwise ``(code, reason)`` (the code goes on /health, the
    reason to the log). Safe means: nothing fatal, every voidable item was announced by THIS log's intent journal, and
    none of them is an anchor (or a version published after one). Without its anchor a line can never have been
    appended (the append happens only after the anchor is recorded), so nothing in the rolled-back set was ever
    acknowledged. ``ledger_types``: event id -> type for this department's ledger events.

    Codes: ``log_divergence`` (fatal, or residue this log did not announce); ``volume_generation_mismatch`` (the
    ledger holds an instance lease of this log that the volume does not: the volume was restored or cloned after
    another instance booted on it -- the lease written at every boot is the volume's generation marker);
    ``interrupted_write_anchored`` (the interrupted write had reached its anchor: crash or restore cannot be told
    apart, so Andre decides)."""
    if assessment.fatal:
        return "log_divergence", "; ".join(assessment.fatal)
    void = sorted(assessment.void_event_ids)
    foreign = [e for e in void if e not in journal]
    if any(ledger_types.get(e) == LEASE_TYPE for e in foreign):
        return ("volume_generation_mismatch", "the ledger holds an instance lease of this log the volume does not "
                "(a restored or cloned volume, or another instance): " + "; ".join(assessment.voidable))
    if foreign:
        return ("log_divergence", f"{len(foreign)} voidable ledger event(s) were not written by this log (first: "
                f"{foreign[0]}): " + "; ".join(assessment.voidable))
    anchored = [e for e in void if (ledger_types.get(e) or journal.get(e)) in ANCHORED_TYPES]
    if anchored or assessment.void_lines:
        return ("interrupted_write_anchored", f"an interrupted write had reached its anchor ({len(anchored)} anchored "
                "event(s)): it may have been committed and acknowledged before a restore, so it is never voided "
                "automatically: " + "; ".join(assessment.voidable))
    return None


def ledger_types(entries: Iterable[dict], department: str) -> dict[str, str]:
    return {e["event_id"]: str(e.get("event_type")) for e in entries
            if isinstance(e, dict) and e.get("department") == department and isinstance(e.get("event_id"), str)}


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


READ_BUDGET_S = 20.0           # a ledger read slower than this is not acted on (the next pass reads again)


class Settler:
    """CH-2 with AEGIS wave A F-3: one pass of the ledger watch over the intent journal. The ledger read runs OUTSIDE
    the service lock (a slow or hung ``GET /ledger/entries`` never stalls requests); the lock is taken only to check
    that the log and the journal did not move meanwhile, then to classify, roll back provably-safe residue and forget
    what is resolved. The service provides: ``lock``, ``log``, ``intent``, ``reconcile_mode``, ``closed``,
    ``divergence``/``divergence_code``, ``reconcile_required``, ``_absent_since``, ``_ledger_probe_ok`` and
    ``_auto_reconcile(assessment, when)``."""

    def __init__(self, svc, name: str, department: str, read_entries: Callable[[], list],
                 assess: Callable[[list], object], errors: tuple, keep: Optional[Callable[[], set]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.svc, self.name, self.department = svc, name, department
        self.read_entries, self.assess, self.errors = read_entries, assess, errors
        self.keep = keep or (lambda: set())
        self.clock = clock

    def _snapshot(self) -> tuple:
        return len(self.svc.log), tuple(sorted(self.svc.intent.ids()))

    def run(self) -> bool:
        svc = self.svc
        with svc.lock:
            if svc.closed or svc.reconcile_mode or svc.log.in_memory:
                return not len(svc.intent)
            if not len(svc.intent) and svc.divergence is None:
                return True
            snap = self._snapshot()
        t0 = self.clock()
        try:
            entries = self.read_entries()                     # no lock held here
        except self.errors as exc:
            svc._ledger_probe_ok = False
            alert("ledger_write_pending", self.name, pending=len(svc.intent), reason=type(exc).__name__)
            return False
        if self.clock() - t0 > READ_BUDGET_S:
            alert("ledger_read_slow", self.name, seconds=round(self.clock() - t0, 1))
            return False
        with svc.lock:
            if svc.closed or self._snapshot() != snap:
                return False                                 # the log or the journal moved: read again next pass
            try:
                a = self.assess(entries)
                journal = svc.intent.ids()
                verdict = classify(a, journal, ledger_types(entries, self.department)) \
                    if (a.voidable or a.fatal) else None
                svc.divergence = verdict[1] if verdict else None
                svc.divergence_code = verdict[0] if verdict else None
                if verdict is not None:
                    alert("log_divergence", self.name, code=verdict[0], pending=len(journal))
                    return False
                voided: set = set()
                if a.voidable:
                    svc._auto_reconcile(a, "running")
                    voided = set(a.void_event_ids)
                held = set(ledger_types(entries, self.department))
                rest = {k: v for k, v in svc.intent.ids().items() if k not in voided and k not in self.keep()}
                svc.intent.settle(resolved(rest, a, held, svc._absent_since, self.clock()))
                svc.reconcile_required = []
            except self.errors as exc:
                svc._ledger_probe_ok = False
                alert("ledger_write_pending", self.name, pending=len(svc.intent), reason=type(exc).__name__)
                return False
            if len(svc.intent):
                alert("ledger_write_pending", self.name, pending=len(svc.intent))
            return not len(svc.intent)


def now_monotonic() -> float:
    return time.monotonic()
