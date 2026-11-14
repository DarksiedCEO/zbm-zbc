"""
Search & Answer Intelligence (2) — SEO / AEO / GEO / LLMO — the core (ADR 0017).

One lock guards all state. Every state change is one ``_commit``: the typed evidence events the change carries
(tenant, kill switch, audit request / approval / report, entity field) are recorded on the ledger first; then the
exact log line is prepared, fsynced aside (pending line), anchored on the evidence ledger, appended to the local log,
and only then applied to memory — by the same ``_apply`` that rebuilds state from the log at start, so live state
and replayed state cannot diverge. The plumbing (``_commit``, ``_anchor``, ``_drop_pending``, ``verify_integrity``,
``_settle_pending``, ``_roll_forward``, ``_anchor_problem``, ``close()``, the data-directory claim with its single-use
adopt token, ``_integrity_and_ledger`` and the R6-M1 ``audit_evidence`` view) is bizdev-py's, unchanged in behaviour
(itself sales-py's / service-py's / security-py's as fixed in their AEGIS rounds).

Typed evidence payloads carry ids, codes and hashes only: never page text, a domain in the clear beyond the tenant's
own registered domains, or a provider answer. The ledger itself only ever receives the payload's SHA-256.

The department's work is in mixins: svc_tenants.py (tenants, tenant tokens, kill switches), svc_entity.py (the
canonical entity record — the propagation spine's read side), svc_audits.py (the audit product), svc_citations.py
(Callum's citation-source runs, ADR 0046). The agents are in
agents/ and the shared primitives in primitives/ (single task each). No network call is ever made with the lock held.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Optional

from clock import Clock, SystemClock, iso
from config import Settings
from errors import Conflict, NotFound, Unavailable
from ledger import DEPARTMENT, LedgerConflict, LedgerQueryFailed, LedgerRecordError, Recorder, derived_id, payload_sha256
from reasons import R
import sink_guard
from store import DISCARDED_NAME, PENDING_NAME, DataDirBusy, RecordLog, StoreCorrupt, StoreWriteError, verify_lines
from svc_attribution import AttributionMixin
from svc_audits import AuditsMixin
from svc_citations import CitationsMixin
from svc_engines import EnginesMixin
from svc_entity import EntityMixin, ZBM_ENTITY
from svc_gsc import GscMixin
from svc_invoices import InvoicesMixin
from svc_logs import LogsMixin
from svc_manager import ManagerMixin
from svc_optext import OperatorTextMixin
from svc_reports import ReportsMixin
from svc_schedules import SchedulesMixin
from svc_tenants import TenantsMixin, OWN_TENANT
from svc_vaulted import VaultedMixin

INTERNAL = "seo"
INTEGRITY_RETRY_S = 15
FORCED_MIN_S = 10
JOBS = ("integrity", "interrupted-audits", "schedule-tick")


def _parse_line(line: bytes) -> dict:
    """One log line parsed (a module function so the evidence view's parse count can be tested)."""
    return json.loads(line)


def _maybe(exc: Unavailable) -> Unavailable:
    """Mark an outcome as unknown: the line is pending and may still take effect (security-py round 2 N2)."""
    exc.maybe = True
    return exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_sha(body: dict) -> str:
    return payload_sha256(body)


class SeoService(TenantsMixin, EntityMixin, AuditsMixin, InvoicesMixin, LogsMixin, SchedulesMixin, ManagerMixin,
                 GscMixin, EnginesMixin, ReportsMixin, CitationsMixin, AttributionMixin, OperatorTextMixin, VaultedMixin):
    def __init__(self, settings: Settings, recorder: Recorder, log: RecordLog, ports=None,
                 clock: Optional[Clock] = None, lock_token: Optional[str] = None, *,
                 observe_existing_on_start: bool = False):
        if type(observe_existing_on_start) is not bool:
            raise TypeError("observe_existing_on_start must be a bool")
        import ports as ports_mod
        import log_sink
        log_sink.install()                               # sink job D: Python logging takes machine values only
        self.settings = settings
        self.rec = recorder
        self.log = log
        self.ports = ports or ports_mod.Ports.default(settings)
        self.clock = clock or SystemClock()
        self.lock = threading.RLock()
        self._tick_lock = threading.Lock()
        # state rebuilt from the log
        self.tenants: dict[str, dict] = {}
        self.switches: dict = {"global": False, "write": False, "capability": {}, "provider": {}}
        self.volatile_kills: list[dict] = []           # engaged while the ledger could not record them (fail closed)
        self.entities: dict[str, dict] = {}
        self.audits: dict[str, dict] = {}
        self.prompt_sets: dict[str, dict] = {}
        self.log_ingests: dict[str, dict] = {}
        self.schedules: dict[str, dict] = {}
        self.agent_states: dict[str, dict] = {}
        self.gsc_reads: dict[str, list] = {}             # tenant -> times of recorded Search Console reads (budget)
        self.engine_runs: dict[str, dict] = {}          # Wave 4: metered answer-engine runs (svc_engines.py)
        self.client_reports: dict[str, dict] = {}       # recorded client-report renders (svc_reports.py)
        self.citation_runs: dict[str, dict] = {}        # Callum's citation-source runs (svc_citations.py, ADR 0046)
        self.attribution_runs: dict[str, dict] = {}     # Farrah's attribution runs (svc_attribution.py, farrah-spec/1)
        self.requests: dict[tuple, tuple] = {}
        # memory only
        self.running_audits: set = set()               # audits this process is running now
        self.running_citation_runs: set = set()        # citation runs this process is running now (ADR 0046)
        self.running_attribution: set = set()          # attribution runs this process is running now (farrah-spec/1)
        self._log_verify_cache: dict = {}                # ingest -> {family|ip hash: verdict} (memory only)
        self._log_budget: dict = {}
        self._log_robots: dict = {}                      # open ingests' parsed robots.txt (memory only)
        self._log_inputs: dict = {}                      # ingest -> its vaulted robots.txt / sitemap sample (memory)
        self._log_full: dict = {}                        # ingest -> its full totals, rebuilt from the chunk blobs
        self._doc_handoff: dict = {}                     # blob ref -> document, during its own commit only
        self.integrity = {"ok": False, "checked_at": None, "problem": "not yet verified against the ledger"}
        self._last_integrity_try = 0
        self._own_pending: Optional[bytes] = None
        self._evidence_lock = threading.Lock()
        self._evidence_cache: dict = {"n": 0, "epoch": None, "lines": []}
        self._closed = False
        # log lines holding a non-machine value (written before sink job D): counted on the first status read, over
        # the lines the log held at start, and remembered (review 98b76cde SHOULD 1: not on the start path)
        self._legacy_prose: Optional[int] = None
        self._legacy_scan_len = 0
        self._dir_lock = getattr(settings, "data_dir_lock", None)
        self._lock_token: Optional[str] = None
        if self._dir_lock is not None:
            if lock_token is None:
                lock_token = self._dir_lock.claim()
            adopted = self._dir_lock.adopt(lock_token)
            if adopted is not None:
                self._lock_token = adopted
            else:
                self._dir_lock = None
                raise DataDirBusy("the data-directory claim handed to this service instance is not held (released, "
                                  "stale, another instance's, or already adopted); refusing to start")
        try:
            self.vault = self._open_vault(settings, repair=not observe_existing_on_start)
            # review c0c09bd1: operator free text (svc_optext.py) in its own vault file, same key and scheme
            self.optext_vault = self._open_vault(settings, repair=not observe_existing_on_start, operator=True)
            # review 2b26d980 MUST 2: the configured vault key ids are written to the log (the operator-text stub's
            # ``key_id``), so each must be one the sink guard takes: checked now (a closed configuration error, no
            # id in it), never found out at the first audit. They join the guard's closed ``key_id`` set.
            key = getattr(settings, "vault_key", None)
            try:
                sink_guard.register_key_ids([getattr(key, "key_id", None), getattr(key, "previous_key_id", None)])
            except ValueError as exc:
                raise RuntimeError(f"SEO_VAULT_KEY_ID / SEO_VAULT_KEY_PREVIOUS_ID: {exc}") from None
            import client_report
            if not isinstance(getattr(settings, "report_branding", None), client_report.Branding):
                # AEGIS L-8: refuse to start rather than answer the client-report route with a 500
                raise RuntimeError("settings.report_branding must be a client_report.Branding")
            self._start(observe_existing_on_start=observe_existing_on_start)
            self._record_vault_repair()
        except BaseException:
            self.close()
            raise

    def _record_vault_repair(self) -> None:
        """Review 601e328a SHOULD 3: a torn final vault line cut at open is recorded as a typed audit event with its
        counts only (never content); a ledger that cannot take it leaves the repair visible in health as unrecorded.
        Review 2b26d980 SHOULD 4: the same for BOTH vault files (the citation vault and the operator text vault):
        the event, the marker cleared once it is recorded, the repair in /health."""
        self._record_one_vault_repair(self.vault, "citation_vault_tail_repaired", "citation_vault")
        self._record_one_vault_repair(self.optext_vault, "operator_vault_tail_repaired", "operator_vault")

    def _record_one_vault_repair(self, vault, event_type: str, subject: str) -> None:
        rep = getattr(vault, "tail_repair", None)
        if not rep or not getattr(vault, "repairs", True):
            return
        import logging
        logging.getLogger("seo.vault").warning("vault: the final line was repaired at open (%d bytes removed, "
                                               "%d records removed, %d records kept)", rep["bytes_removed"],
                                               rep.get("records_removed", 0), rep["records_kept"])
        # review 08eea6e1 SHOULD 2: the payload is the repair marker's counts only (no log length), so the event a
        # start records after a repair an EARLIER, failed start made is the same event (the ledger is idempotent)
        import vault as _vault
        payload = {k: rep[k] for k in _vault.REPAIR_FIELDS if k in rep}
        eid = derived_id("evd", event_type, payload_sha256(payload))
        try:
            self._record_twice(eid, event_type, "seo", subject, payload, f"{event_type} {subject}")
            rep["recorded"] = True
        except LedgerRecordError:
            rep["recorded"] = False
            return
        clear = getattr(vault, "clear_repair_marker", None)
        if clear is not None:
            clear()

    @staticmethod
    def _open_vault(settings, repair: bool = True, operator: bool = False):
        """ADR 0051 (job 2): the client citation vault. With a data directory: ``citation_vault.jsonl`` there, under
        the configured key (no key: every citation run fails closed, VAULT_UNAVAILABLE). Without one (in-memory,
        non-production): the in-memory backend, same encryption, under the configured key or a random key held in
        memory only (nothing outlives the process)."""
        import vault
        key = getattr(settings, "vault_key", None)
        if settings.data_dir:
            name = vault.OPERATOR_VAULT_NAME if operator else vault.VAULT_NAME
            return vault.CitationVault(settings.data_dir, key, repair=repair, name=name)
        return vault.MemoryCitationVault(key if key is not None else vault.ephemeral_vault_key())

    def close(self) -> None:
        """Give the data directory back. A closed instance is inert: its log refuses every write, every commit is
        refused, ``verify_integrity`` does no ledger I/O, and the integrity and job routes answer 503 SERVICE_CLOSED."""
        with self.lock:
            self._closed = True
            with self.log.lock:
                self.log.closed = True
            if getattr(self, "_dir_lock", None) is not None:
                self._dir_lock.release_claim(self._lock_token)
                self._dir_lock = None
                self._lock_token = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def legacy_prose_lines(self) -> int:
        """Review 2b26d980 SHOULD 5: a line a release up to 8bd9e29e wrote may hold prose inline. Such lines are
        counted (``legacy_prose_lines`` in /health) and never served raw (``audit_export`` withholds the text).
        Review 98b76cde SHOULD 1: counted on the first read, not at start (the guard over every line made a start
        2 to 2.7 times slower), over the lines the log held at start (a line this process writes has passed the
        guard), and remembered. Read outside the service lock: the count walks a copy of the lines."""
        n = self._legacy_prose
        if n is None:
            n = 0
            for ln in self.log.raw_lines()[:self._legacy_scan_len]:
                r = json.loads(ln)
                if sink_guard.violations(r["kind"], r["data"], stored=True):
                    n += 1
            self._legacy_prose = n
        return n

    def _start(self, *, observe_existing_on_start: bool = False) -> None:
        for r in self.log.iter_records():
            self._apply(r["kind"], r["data"], r["at"])
        self._legacy_scan_len = len(self.log)
        self._sweep_ingests()                    # expired log ingests keep counts only (AEGIS 4c0a805 T4)
        if observe_existing_on_start:
            # Constructor-only: refuse provisioning/repair, but retain normal lock
            # acquisition and memory reconstruction. Later operations are unchanged.
            if self.log.in_memory or not self.log.path or not len(self.log):
                raise Unavailable(R("INTEGRITY_UNVERIFIED"))
            observed = self.observe_integrity()
            if observed["integrity"]["ok"] is not True or observed["ledger_valid"] is not True:
                raise Unavailable(R("INTEGRITY_UNVERIFIED"))
            with self.lock:
                self.integrity = dict(observed["integrity"])
        else:
            self.verify_integrity(force=True)

    # ================================================================================================ plumbing

    def now(self) -> datetime:
        return self.clock.now()

    def today(self) -> str:
        return self.clock.today().isoformat()

    def _anchor_ids(self, epoch: str, seq: int, line_sha: str) -> tuple[str, dict]:
        payload = {"epoch": epoch, "seq": seq, "line_sha256": line_sha}
        return derived_id("anc", epoch, seq, line_sha), payload

    def _commit(self, kind: str, data: dict, actor: str, evidence=None) -> dict:
        """Typed evidence first, then the ledger anchor, then the local log, then memory (fail closed at every step).
        ``evidence`` = (event_type, subject_id, payload, id_parts) or a list of them: the payload carries ids, codes
        and hashes only, and no time (a retry records the same event, which the ledger answers 200)."""
        with self.lock:
            if self._closed:
                raise Unavailable(R("SERVICE_CLOSED"))
            if not self.integrity["ok"]:
                raise Unavailable(R("INTEGRITY_UNVERIFIED"))
            at = iso(self.now())
            data = {**data, "actor": data.get("actor", actor)}   # the anchor's actor is read back from the line
            actor = data["actor"]
            # AEGIS round 6 M1: every typed evidence event carries the request key and the log seq it is meant for,
            # and the line names it (``evidence``). Record-first stays; an event whose line never made it into the
            # anchored log is visible as ``attempted`` (never ``committed``) in /audit/evidence.
            seq = len(self.log) + 1
            rk = data.get("request_id") or f"{INTERNAL}|{kind}"
            named, events = [], []
            for event_type, subject_id, payload, id_parts in (evidence if isinstance(evidence, list) else
                                                              [evidence] if evidence is not None else []):
                payload = {**payload, "rk": rk, "seq": seq}
                # AEGIS round 5: the id is the request key PLUS the payload hash - a retry with the same payload
                # dedupes on the ledger, a retry after the state changed gets a new id (never a lasting 409)
                eid = derived_id("evd", event_type, *id_parts, payload_sha256(payload))
                events.append((eid, event_type, subject_id, payload))
                named.append({"event_id": eid, "event_type": event_type, "subject_id": subject_id, "payload": payload})
            if named:
                data["evidence"] = named
            # sink job A: the line and every evidence payload hold machine values only, checked BEFORE anything is
            # written (no ledger event, no pending line). Any non-machine value refuses the whole commit (job D).
            try:
                sink_guard.enforce(kind, data)
                for _eid, event_type, _subject, payload in events:
                    sink_guard.enforce(sink_guard.LEDGER + str(event_type), payload)
            except sink_guard.SinkViolation as exc:
                # codes and a count only (the logging sink withholds anything else): the sink and the first path,
                # the path written as a code (``/content/prompts[]`` -> ``content.prompts.list``)
                first = (exc.paths[0] if exc.paths else "").strip("/").replace("/", ".").replace("[]", ".list") \
                    .replace("{key}", "KEY").replace("*", "ANY")
                logging.getLogger("seo.sink").error("commit refused by the sink guard: sink=%s paths=%d first=%s",
                                                    exc.kind.replace(":", "."), len(exc.paths), first or "none")
                raise Unavailable(R("SINK_REFUSED")) from None
            for eid, event_type, subject_id, payload in events:
                try:
                    self._record_twice(eid, event_type, actor, subject_id, payload, f"{event_type} {subject_id}")
                except LedgerRecordError:
                    raise Unavailable(R("LEDGER_UNAVAILABLE")) from None
            rec, line = self.log.prepare(kind, at, data)
            if rec["seq"] != seq:                           # the log moved under us: never anchor a mislabelled line
                raise Unavailable(R("STORE_UNAVAILABLE"))
            line_sha = sha256_hex(line)
            epoch = self.log.epoch or line_sha[:16]
            eid, payload = self._anchor_ids(epoch, rec["seq"], line_sha)
            self._own_pending = line                  # R4-1: only a line THIS process wrote is anchored by it
            try:
                self.log.write_pending(line)
            except StoreWriteError:
                if not self._drop_pending():
                    raise _maybe(Unavailable(R("STORE_UNAVAILABLE"))) from None
                raise Unavailable(R("STORE_UNAVAILABLE")) from None
            try:
                self._anchor(eid, actor, epoch, payload, kind, rec["seq"])
            except LedgerRecordError as exc:
                if exc.took_effect is False and self._drop_pending():
                    raise Unavailable(R("LEDGER_UNAVAILABLE")) from None
                # ADR 0012 H1 / N1: the ledger may hold this anchor, or record it late. The pending line is kept and
                # writes stop; the next integrity check rolls it forward (re-records the identical anchor, appends).
                self.integrity = {"ok": False, "checked_at": at, "problem": "a ledger answer was lost; the "
                                  "pending line is rolled forward at the next integrity check"}
                raise _maybe(Unavailable(R("LEDGER_UNAVAILABLE"))) from None
            try:
                self.log.append_prepared(rec, line)
            except StoreWriteError:
                self.integrity = {"ok": False, "checked_at": at, "problem": "a log line was anchored but not written; "
                                  "it is rolled forward at the next integrity check"}
                raise _maybe(Unavailable(R("STORE_UNAVAILABLE"))) from None
            self._own_pending = None   # R5-1: in the log now; a leftover file copy is stale and set aside next check
            self._drop_pending()
            self._apply(kind, data, at)
            return rec

    def _record_twice(self, eid, event_type, actor, subject, payload, summary) -> None:
        """One retry of the SAME event when the answer was lost (the ledger is idempotent on identical content)."""
        try:
            self.rec.record(eid, event_type, actor, subject, payload, summary)
        except LedgerRecordError as exc:
            if exc.took_effect is False or isinstance(exc, LedgerConflict):
                raise
            self.rec.record(eid, event_type, actor, subject, payload, summary)

    def _anchor(self, eid: str, actor: str, epoch: str, payload: dict, kind: str, seq: int) -> None:
        self._record_twice(eid, "log_anchor", actor, f"log:{epoch}", payload, f"{kind} #{seq}")

    def _drop_pending(self) -> bool:
        """True when the pending line is certainly gone. Otherwise writes stop until the next integrity check."""
        try:
            self.log.clear_pending()
            self._own_pending = None
            return True
        except StoreWriteError:
            self.integrity = {"ok": False, "checked_at": iso(self.now()),
                              "problem": "the pending line could not be removed"}
            return False

    def _idem(self, actor: str, rk: str, body: dict) -> Optional[tuple]:
        """Idempotency by (actor, ``op|target|request_id``) and the body's hash: the same body answers what the first
        call did, a different body is 409 REQUEST_ID_REUSED."""
        prev = self.requests.get((actor, rk))
        if prev is None:
            return None
        if prev[0] != request_sha(self._machine_body(body)):
            # review 2b26d980 SHOULD 3: a request recorded by a release before sink job D carries the LEGACY hash
            # (the plain SHA-256 of the body as it was sent). A retry of it after the deploy is the same request:
            # compared with the legacy computation, and only when the body holds a non-machine string (otherwise
            # the two computations are the same and the mismatch is a different body)
            if prev[0] != self._legacy_request_sha(body):
                raise Conflict(R("REQUEST_ID_REUSED"))
        return prev

    def _legacy_request_sha(self, body: dict) -> Optional[str]:
        """The idempotency hash a release up to 8bd9e29e recorded for ``body`` (the plain SHA-256 of the body), or
        None when it cannot be computed. Only ever COMPARED with a stored hash: never written, never returned."""
        try:
            return request_sha(body)
        except Exception:  # noqa: BLE001 - a body the legacy hash could not take was never recorded
            return None

    @staticmethod
    def rk(op: str, target: str, body: dict) -> str:
        """The request key: ``op|target|request_id`` (scoped: the same request_id on another op or target is a
        different request)."""
        return f"{op}|{target}|{body['request_id']}"

    def _req(self, data: dict, actor: str, rk: str, body: dict, obj) -> dict:
        """The request fields of a log line. ``request_sha`` is the SHA-256 of the body with every non-machine
        string replaced by its keyed HMAC (sink job D, ``_machine_body``): never a plain hash of client text."""
        return {**data, "actor": actor, "request_id": rk, "request_sha": request_sha(self._machine_body(body)),
                "_obj": obj}

    def _gate(self) -> None:
        if self._closed:
            raise Unavailable(R("SERVICE_CLOSED"))
        if not self.verify_integrity()["ok"]:
            raise Unavailable(R("INTEGRITY_UNVERIFIED"))

    def _get(self, table: dict, key: str, code: str) -> dict:
        x = table.get(key)
        if x is None:
            raise NotFound(R(code))
        return x


    # ================================================================================================ replay

    def _apply(self, kind: str, d: dict, at: str) -> None:
        handler = getattr(self, f"_a_{kind}", None)
        if handler is None:
            raise StoreCorrupt(f"log record of unknown kind {kind[:40]!r}")
        handler(d, at)
        if d.get("request_id") and d.get("actor"):
            self.requests[(d["actor"], d["request_id"])] = (d.get("request_sha"), d.get("_obj"))

    def _a_job_ran(self, d, at):
        pass

    # ================================================================================================ integrity

    def verify_integrity(self, force: bool = False, always: bool = False) -> dict:
        """Complete or set aside the pending line, then check every local line's anchor on the ledger, and that
        the ledger holds no anchor this log lacks (a truncated, rolled back, deleted or replaced log)."""
        with self.lock:
            if self._closed:                              # service-py a5dd261 M1: no ledger I/O from a closed instance
                return self._closed_integrity()
            mono = time.monotonic()
            if not force and (self.integrity["ok"] or mono - self._last_integrity_try < INTEGRITY_RETRY_S):
                return dict(self.integrity)
            if force and not always and mono - self._last_integrity_try < FORCED_MIN_S and self._last_integrity_try:
                return dict(self.integrity)     # ADR 0012 L7: a full ledger read at most every FORCED_MIN_S
            self._last_integrity_try = mono
            at = iso(self.now())
            try:
                entries = self.rec.client.entries()
            except LedgerQueryFailed:
                self.integrity = {"ok": False, "checked_at": at, "problem": "the ledger cannot be read"}
                return dict(self.integrity)
            mine = [e for e in entries if e.get("department") == DEPARTMENT and e.get("event_type") == "log_anchor"]
            problem, rolled = self._settle_pending({e.get("event_id"): e for e in mine})
            if problem is None and rolled:
                try:
                    entries = self.rec.client.entries()     # the roll-forward may have just recorded an anchor
                except LedgerQueryFailed:
                    problem = "the ledger cannot be read"
            if problem is None:
                mine = [e for e in entries if e.get("department") == DEPARTMENT and e.get("event_type") == "log_anchor"]
                problem = self._anchor_problem(mine, {e.get("event_id"): e for e in mine})
            if self.log.fault:                   # bug sweep E: a failed write could not be cut back
                problem = f"LOCAL_LOG_WRITE_FAULT: {self.log.fault}"
            self.integrity = {"ok": problem is None, "checked_at": at, "problem": problem}
            if problem is None:
                self._after_integrity()
            return dict(self.integrity)

    def _closed_integrity(self) -> dict:
        return {"ok": False, "checked_at": self.integrity.get("checked_at"), "problem": "this service instance is closed"}

    def observe_integrity(self) -> dict:
        """Opt-in observational hook; never repair, seed, or update integrity caches.

        Caller must separately prevent new work for a final readiness barrier. This
        result only covers a stable local snapshot over these fresh Ledger reads;
        it is neither an admission permission nor a durable/global readiness proof.
        Existing audit/job/startup verification retains its repairing behavior.
        """
        def snapshot():
            # Called with the service lock; log lock also excludes direct log writes.
            if self._closed:
                raise Unavailable(R("SERVICE_CLOSED"))
            with self.log.lock:
                lines = tuple(self.log._lines)
                if self.log.data_dir:
                    # Observation requires absence, not an empty-reader result.
                    # Preserve even empty/malformed sidecars for ordinary recovery.
                    for name in (PENDING_NAME, DISCARDED_NAME):
                        try:
                            os.lstat(os.path.join(self.log.data_dir, name))
                        except FileNotFoundError:
                            continue
                        return lines, "pending reconciliation required"
                if self._own_pending is not None or self.log.read_pending() is not None \
                        or self.log.read_discarded() is not None:
                    return lines, "pending reconciliation required"
                if OWN_TENANT not in self.tenants or ZBM_ENTITY not in self.entities:
                    return lines, "seed initialization required"
                if self.log.fault:
                    return lines, "local log write fault"
                if self.log.path:
                    with open(self.log.path, "rb") as fh:
                        for line in lines:
                            for offset in range(0, len(line), 65536):
                                chunk = line[offset:offset + 65536]
                                if fh.read(len(chunk)) != chunk:
                                    return lines, "local log differs from replayed state"
                            if fh.read(1) != b"\n":
                                return lines, "local log differs from replayed state"
                        if fh.read(1):
                            return lines, "local log differs from replayed state"
                verify_lines(list(lines))
                return lines, None

        def result(problem, ledger_ok, lines):
            return {"integrity": {"ok": problem is None and ledger_ok is True,
                                  "checked_at": iso(self.now()), "problem": problem},
                    "ledger_valid": ledger_ok is True, "log_length": len(lines),
                    "log_head_sha256": sha256_hex(lines[-1]) if lines else None}

        try:
            with self.lock:
                lines, problem = snapshot()
                if problem is not None:
                    return result(problem, False, lines)
            # Keep slow Ledger reads outside the service lock, as with existing audit.
            entries = self.rec.client.entries()
            ledger_ok = self.rec.client.verify() is True
            with self.lock:
                current, problem = snapshot()
                if current != lines:
                    problem = "local state changed during observation"
                if problem is None:
                    mine = [e for e in entries if e.get("department") == DEPARTMENT
                            and e.get("event_type") == "log_anchor"]
                    problem = self._anchor_problem(mine, {e.get("event_id"): e for e in mine})
                if problem is None and not ledger_ok:
                    problem = "the ledger chain could not be verified"
                return result(problem, ledger_ok, current)
        except (LedgerQueryFailed, OSError, StoreCorrupt):
            # No cached green result and no local reconciliation on read failure.
            return result("integrity evidence could not be read", False, ())

    def audit_integrity(self) -> dict:
        """``/seo/v1/audit/integrity``: a closed instance refuses 503 SERVICE_CLOSED."""
        return self._integrity_and_ledger(always=False)

    def _integrity_and_ledger(self, always: bool) -> dict:
        """The integrity job and the audit route (service-py's). The ledger's chain ``verify()`` (an HTTP call, up to
        the client timeout) runs OUTSIDE the service lock. It checks the ledger's OWN chain, independent of the local
        log, so its verdict is ALWAYS reported as returned (``ledger_valid`` is the real verdict, never a cached or
        assumed one). After it, the lock is re-taken, closed is re-checked (closed meanwhile: 503 SERVICE_CLOSED,
        nothing written), and the integrity result and log length are read fresh."""
        with self.lock:
            if self._closed:
                raise Unavailable(R("SERVICE_CLOSED"))
            self.verify_integrity(force=True, always=always)
        ledger_ok = self.rec.client.verify()
        with self.lock:
            if self._closed:
                raise Unavailable(R("SERVICE_CLOSED"))
            return {"integrity": dict(self.integrity), "ledger_valid": ledger_ok is True, "log_length": len(self.log)}

    def _settle_pending(self, by_id: dict) -> tuple[Optional[str], bool]:
        """security-py's (AEGIS R4-1 / R5-1): our own line (kept in memory) is rolled forward; a line found on disk
        is appended only if the ledger ALREADY holds its anchor, else set aside (``pending.discarded``), inert. If its
        anchor appears later (it was in flight when the process stopped), the next check appends it."""
        own = self._own_pending
        if own is not None:
            problem, appended = self._roll_forward(own, by_id, rerecord=True)
            if problem == "not vouched: stale":
                self._own_pending = None            # no longer the next line: fall through to the file path
                return self._settle_pending(by_id)
            if problem is not None:
                return problem, False
            self._own_pending = None
            try:
                self.log.clear_pending()
            except StoreWriteError:
                return "the pending line could not be removed", appended
            return None, appended
        appended_any = False
        raw = self.log.read_pending()
        if raw is not None:
            problem, appended = self._roll_forward(raw, by_id, rerecord=False)
            if problem is not None and not problem.startswith("not vouched"):
                return problem, False
            try:
                if not appended:
                    self.log.write_discarded(raw)
                self.log.clear_pending()
            except StoreWriteError:
                return "the pending line could not be set aside", appended
            appended_any |= appended
        aside = self.log.read_discarded()
        if aside is not None:
            problem, appended = self._roll_forward(aside, by_id, rerecord=False)
            if appended or problem == "not vouched: stale":
                try:
                    self.log.clear_discarded()
                except StoreWriteError:
                    pass
            appended_any |= appended
        return None, appended_any

    def _roll_forward(self, raw: bytes, by_id: dict, rerecord: bool) -> tuple[Optional[str], bool]:
        try:
            rec = json.loads(raw)
            seq, kind, data = rec["seq"], rec["kind"], rec["data"]
            verify_lines(list(self.log._lines) + [raw])        # the exact next line, chained to this log
            if not isinstance(data.get("actor"), str) or not re.fullmatch(r"[a-z0-9_]{1,64}", data["actor"]):
                raise ValueError("no ledger-valid actor")
            if getattr(self, f"_a_{kind}", None) is None:
                raise ValueError("unknown kind")
        except (ValueError, KeyError, TypeError, AttributeError, StoreCorrupt):
            return "not vouched: stale", False
        line_sha = sha256_hex(raw)
        epoch = self.log.epoch or line_sha[:16]
        eid, payload = self._anchor_ids(epoch, seq, line_sha)
        if rerecord:
            try:
                self._anchor(eid, data["actor"], epoch, payload, kind, seq)
            except LedgerRecordError:
                return "the pending line could not be anchored yet (ledger unavailable); kept for the next check", \
                    False
        else:
            e = by_id.get(eid)
            if e is None or e.get("payload_sha256") != payload_sha256(payload) \
                    or e.get("subject_id") != f"log:{epoch}":
                return "not vouched: no anchor on the ledger", False
        try:
            self.log.append_prepared(rec, raw)
        except StoreWriteError:
            return "the pending line is anchored on the ledger but cannot be written here", False
        try:
            self._apply(kind, data, rec["at"])
        except (KeyError, TypeError, ValueError, StoreCorrupt):
            return "an anchored pending line could not be applied; an operator must inspect the log", True
        return None, True

    def _anchor_problem(self, mine: list, by_id: dict) -> Optional[str]:
        shas = self.log.line_shas()
        epoch = self.log.epoch
        epochs = {e.get("subject_id") for e in mine}
        if epochs - ({f"log:{epoch}"} if epoch else set()):
            return "the ledger holds anchors of another seo log: this log was deleted or replaced"
        for seq, line_sha in enumerate(shas, start=1):
            eid, payload = self._anchor_ids(epoch, seq, line_sha)
            e = by_id.get(eid)
            if e is None or e.get("payload_sha256") != payload_sha256(payload) or e.get("subject_id") != f"log:{epoch}":
                return f"local log line {seq} has no matching anchor on the ledger"
        if len(mine) > len(shas):
            return "the ledger holds anchors beyond the local log: the log was truncated or rolled back"
        return None


    def _after_integrity(self) -> None:
        """Seed, once, what Wave 1 starts from: the ``zbm`` own-properties tenant and Z Best Media's canonical entity
        record (svc_entity.SEED). Recorded like any other change; a failure leaves integrity unverified."""
        try:
            self._seed()
        except Unavailable:
            self.integrity = {"ok": False, "checked_at": iso(self.now()),
                              "problem": "the seed records could not be committed yet"}

    # ================================================================================================ status

    def health(self) -> dict:
        legacy = self.legacy_prose_lines                 # outside the lock (the first read counts; see the property)
        with self.lock:
            closed = self._closed
            s = self.settings
            return {
                "status": "closed" if closed else ("ok" if self.integrity["ok"] and not self.log.fault else "degraded"),
                "closed": closed,
                "integrity": self._closed_integrity() if closed else dict(self.integrity),
                "in_memory": self.log.in_memory,
                "log_write_fault": bool(self.log.fault),
                "non_production": s.non_production,
                "andre_approvals_configured": None,          # filled by the API (the gate lives there)
                "kill_switches": self.switch_view(),
                "ports": self.ports.status(),
                "answer_engines": self.engine_status(),
                "invoice_verification": self.invoice_verification_status(),
                "search_console": self.gsc_status(),
                "tenants": len(self.tenants),
                "audits_running": len(self.running_audits),
                "audits_total": len(self.audits),
                "log_length": len(self.log),
                "citation_vault": self.vault.status(),        # counts only (review 601e328a SHOULD 2 / 3)
                "operator_vault": self.optext_vault.status(),  # review 2b26d980 SHOULD 4: the same counts
                # review 2b26d980 SHOULD 5: log lines holding a non-machine value (prose a release before sink job D
                # wrote inline), counted on the first read. Not 0: the export withholds their text; retire the
                # directory
                "legacy_prose_lines": legacy,
            }

    # ================================================================================================ jobs

    def run_job(self, name: str, body: dict) -> dict:
        if name not in JOBS:
            raise NotFound(R("JOB_UNKNOWN"))
        if name == "integrity":                          # refused when closed; verify() runs outside the lock
            out = self._integrity_and_ledger(always=True)
            return {"job": name, "integrity": out["integrity"], "ledger_valid": out["ledger_valid"]}
        if not self._tick_lock.acquire(blocking=False):
            raise Conflict(R("JOB_RUNNING"))
        try:
            with self.lock:
                self._gate()
                rk = self.rk("job", name, body)
                prev = self._idem("scheduler", rk, body)
                if prev:
                    return {"job": name, "already_ran": True, **(prev[1] or {})}
            if name == "schedule-tick":
                summary = self.schedule_tick()
            else:
                # ADR 0046: citation runs left running by a process that is gone are closed by the same job
                summary = {**self.mark_interrupted_audits(),
                           "citation_runs_interrupted": self.mark_interrupted_citation_runs(),
                           "attribution_runs_interrupted": self.mark_interrupted_attribution_runs()}
            with self.lock:
                self._gate()
                self._commit("job_ran", self._req({"job": name}, "scheduler", rk, body, summary), "scheduler")
            return {"job": name, **summary}
        finally:
            self._tick_lock.release()

    # ================================================================================================ audit

    def audit_evidence(self, limit: int, offset: int, event_type: Optional[str] = None) -> dict:
        """``/seo/v1/audit/evidence`` — the view Compliance (38) and auditors use (AEGIS round 6 M1). Every typed
        evidence event this department holds on the ledger, each marked:

        * ``committed`` — a local log line with seq ``s`` names the event (``data.evidence``), the ledger holds that
          line's anchor (``log_anchor`` for epoch, ``s`` and the line's SHA-256), the event's payload carries
          ``rk`` = the line's request key and ``seq`` = ``s``, and the ledger's ``payload_sha256`` is that payload's;
        * ``attempted`` — anything else: recorded first (record-first commit), but its state change never reached the
          anchored log (a refused or failed commit, or a retry that was later committed under another seq).

        Unanchored evidence = attempted, not done. Exactly one ``committed`` event exists per logical action.
        AEGIS round 7: under the service lock only the raw lines not yet seen are copied (no parsing, no hashing);
        they are parsed and hashed outside it and cached by log length, so a page costs only the new lines. The view
        is eventually consistent: a commit in flight while it is read may show as ``attempted``; re-read to settle."""
        with self._evidence_lock:
            with self.lock:
                if self._closed:
                    raise Unavailable(R("SERVICE_CLOSED"))
                cache = self._evidence_cache
                n = len(self.log)
                if n < cache["n"]:                       # never within one instance (append-only), but stay correct
                    cache = self._evidence_cache = {"n": 0, "epoch": None, "lines": []}
                new = self.log.raw_lines(cache["n"])
            for raw in new:                              # outside the service lock: parse and hash only new lines
                r = _parse_line(raw)
                line_sha = sha256_hex(raw)
                if cache["epoch"] is None:
                    cache["epoch"] = line_sha[:16]
                evs = r["data"].get("evidence")
                if evs:
                    rk = r["data"].get("request_id") or f"{INTERNAL}|{r['kind']}"
                    cache["lines"].append((r["seq"], line_sha, rk, r["kind"], evs))
                cache["n"] += 1
            epoch, lines, n_lines = cache["epoch"], list(cache["lines"]), cache["n"]
        try:
            entries = self.rec.client.entries()
        except LedgerQueryFailed:
            raise Unavailable(R("LEDGER_UNAVAILABLE")) from None
        mine = [e for e in entries if e.get("department") == DEPARTMENT]
        anchors = {e.get("event_id"): e for e in mine if e.get("event_type") == "log_anchor"}
        named: dict[str, tuple] = {}                     # event_id -> (seq, rk, payload, log kind) of an anchored line
        for seq, line_sha, line_rk, kind, evs in lines:
            aid, apayload = self._anchor_ids(epoch, seq, line_sha)
            a = anchors.get(aid)
            if a is None or a.get("payload_sha256") != payload_sha256(apayload) or a.get("subject_id") != f"log:{epoch}":
                continue
            for ev in evs:
                named[ev["event_id"]] = (seq, line_rk, ev.get("payload") or {}, kind)
        out, counts = [], {"committed": 0, "attempted": 0}
        for e in mine:
            if e.get("event_type") == "log_anchor" or (event_type is not None and e.get("event_type") != event_type):
                continue
            row = {"event_id": e.get("event_id"), "event_type": e.get("event_type"), "subject_id": e.get("subject_id"),
                   "ledger_seq": e.get("seq"), "payload_sha256": e.get("payload_sha256"), "status": "attempted",
                   "seq": None, "rk": None, "log_kind": None}
            hit = named.get(e.get("event_id"))
            if hit is not None:
                seq, line_rk, payload, log_kind = hit
                if payload.get("seq") == seq and payload.get("rk") == line_rk \
                        and payload_sha256(payload) == e.get("payload_sha256"):
                    row.update(status="committed", seq=seq, rk=line_rk, log_kind=log_kind)
            counts[row["status"]] += 1
            out.append(row)
        return {"rule": "unanchored evidence = attempted, not done", "consistency": "eventual; re-read to settle",
                "total": len(out), **counts, "limit": limit, "offset": offset,
                "evidence": out[offset:offset + limit], "log_length": n_lines}

    def audit_export(self, since_seq: int, limit: int) -> dict:
        """The local log (it holds ids, codes, hashes and the tenants' registered domains; no raw page body is ever
        stored). Sink job B: an audit's report and a drift report are in the tenant's vault, and their lines hold a
        ref, a keyed hash and machine facts. Review 2b26d980 SHOULD 5: EVERY served line passes the sink guard
        first (``sink_guard.withhold``): a string that is not a machine value (the prose a line written before sink
        job D holds inline: a report, entity values, prompts, robots.txt) is served as the code ``WITHHELD``, and
        the answer counts them (``withheld``)."""
        with self.lock:
            if self._closed:
                raise Unavailable(R("SERVICE_CLOSED"))
        out, withheld = [], 0
        for r in self.log.iter_records(max(1, since_seq)):
            d = {k: v for k, v in r["data"].items() if k not in ("_obj", "request_sha")}
            if r["kind"] == "audit_completed" and isinstance(d.get("report"), dict):
                # AEGIS gl-gsc L2: retention applies here too. Past SEO_GSC_RETENTION_DAYS the report's
                # search-performance breakdowns are served as totals only, as in the audit view (the section says
                # ``expired``; report_sha256 is still the hash of the report as recorded). Lines written before sink
                # job B only: later ones carry no report
                d["report"] = self.gsc_retained(d["report"], r["at"])
            d, n = sink_guard.withhold(d)
            withheld += n
            kind = r["kind"] if sink_guard.classify(r["kind"]) is not None else sink_guard.WITHHELD
            at = r["at"] if sink_guard.classify(r["at"]) is not None else sink_guard.WITHHELD
            out.append({"seq": r["seq"], "kind": kind, "at": at, "data": d})
            if len(out) >= limit:
                break
        return {"events": out, "log_length": len(self.log), "withheld": withheld}
