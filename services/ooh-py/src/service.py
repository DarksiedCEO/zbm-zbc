"""
Out-of-Home & DOOH (15) — the core (ADR 0019, waves OOH-0 and OOH-1).

One lock guards all state. Every state change is one ``_commit``: the typed evidence events the change carries are
recorded on the ledger first; then the exact log line is prepared, fsynced aside (pending line), anchored on the
evidence ledger, appended to the local log, and only then applied to memory — by the same ``_apply`` that rebuilds
state from the log at start, so live state and replayed state cannot diverge. The plumbing (``_commit``, ``_anchor``,
``_drop_pending``, ``verify_integrity``, ``_settle_pending``, ``_roll_forward``, ``_anchor_problem``, ``close()``, the
data-directory claim with its single-use adopt token, ``_integrity_and_ledger`` and the R6-M1 ``audit_evidence`` view)
is seo-py's, unchanged in behaviour (itself bizdev-py's / sales-py's / service-py's / security-py's as fixed in their
AEGIS rounds).

Typed evidence payloads carry ids, codes, amounts and hashes only: never vendor text. The ledger itself only ever
receives the payload's SHA-256.

The department's work is in mixins: svc_tenants.py (tenants, kill switches) and svc_campaigns.py (the operating
spine: campaigns, inventory availability, price objects, the commercial model, client approval, funds, the purchase
gate, submission). As in seo-py, ledger writes (the typed evidence, the anchor, and the best-effort refusal records)
are made WITH the service lock held, so one change is recorded at a time and in log order; the ledger's chain
``verify()`` in the integrity route runs outside it. The ledger is the only network peer OOH-1 has.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime
from typing import Optional

import policy
from clock import Clock, SystemClock, iso
from config import Settings
from errors import Conflict, NotFound, Unavailable
from ledger import DEPARTMENT, LedgerConflict, LedgerQueryFailed, LedgerRecordError, Recorder, derived_id, payload_sha256
from reasons import R
from store import DataDirBusy, RecordLog, StoreCorrupt, StoreWriteError, verify_lines
from svc_campaigns import CampaignsMixin
from svc_inventory import InventoryMixin
from svc_tenants import TenantsMixin

INTERNAL = "ooh"
INTEGRITY_RETRY_S = 15
FORCED_MIN_S = 10
JOBS = ("integrity",)


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


class OohService(TenantsMixin, CampaignsMixin, InventoryMixin):
    def __init__(self, settings: Settings, recorder: Recorder, log: RecordLog, clock: Optional[Clock] = None,
                 lock_token: Optional[str] = None):
        self.settings = settings
        self.rec = recorder
        self.log = log
        self.clock = clock or SystemClock()
        self.lock = threading.RLock()
        # state rebuilt from the log
        self.tenants: dict[str, dict] = {}
        self.switches: dict = {"global": False, "write": False, "capability": {}}
        self.volatile_kills: list[dict] = []           # engaged while the ledger could not record them (fail closed)
        self.campaigns: dict[str, dict] = {}
        self.inventory: dict[str, dict] = {}
        self.prices: dict[str, dict] = {}
        self.authorizations: dict[str, dict] = {}
        self.orders: dict[str, str] = {}               # idempotency key -> authorization id (duplicate-order block)
        self.funds_evidence: dict[str, str] = {}       # funds evidence ref -> campaign id (one transfer, one campaign)
        self.vendors: dict[str, dict] = {}             # the vendor registry (Andre's; global: ZBM is the one buyer)
        # the CPA's / counsel's principal-or-agent confirmation, as Andre recorded it (None: UNDETERMINED, locked)
        self.pa_determination: Optional[dict] = None
        self._init_registry()                          # OOH-2: markets (all flags NOT_MET), sources, faces
        self.requests: dict[tuple, tuple] = {}
        # memory only
        self.integrity = {"ok": False, "checked_at": None, "problem": "not yet verified against the ledger"}
        self._last_integrity_try = 0
        self._own_pending: Optional[bytes] = None
        self._evidence_lock = threading.Lock()
        self._evidence_cache: dict = {"n": 0, "epoch": None, "lines": []}
        self._closed = False
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
            self._start()
        except BaseException:
            self.close()
            raise

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

    def _start(self) -> None:
        for r in self.log.iter_records():
            self._apply(r["kind"], r["data"], r["at"])
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
            named = []
            for event_type, subject_id, payload, id_parts in (evidence if isinstance(evidence, list) else
                                                              [evidence] if evidence is not None else []):
                payload = {**payload, "rk": rk, "seq": seq}
                # AEGIS round 5: the id is the request key PLUS the payload hash — a retry with the same payload
                # dedupes on the ledger, a retry after the state changed gets a new id (never a lasting 409)
                eid = derived_id("evd", event_type, *id_parts, payload_sha256(payload))
                try:
                    self._record_twice(eid, event_type, actor, subject_id, payload, f"{event_type} {subject_id}")
                except LedgerRecordError:
                    raise Unavailable(R("LEDGER_UNAVAILABLE")) from None
                named.append({"event_id": eid, "event_type": event_type, "subject_id": subject_id, "payload": payload})
            if named:
                data["evidence"] = named
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
        if prev[0] != request_sha(body):
            raise Conflict(R("REQUEST_ID_REUSED"))
        return prev

    @staticmethod
    def rk(op: str, target: str, body: dict) -> str:
        """The request key: ``op|target|request_id`` (scoped: the same request_id on another op or target is a
        different request)."""
        return f"{op}|{target}|{body['request_id']}"

    @staticmethod
    def _req(data: dict, actor: str, rk: str, body: dict, obj) -> dict:
        return {**data, "actor": actor, "request_id": rk, "request_sha": request_sha(body), "_obj": obj}

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

    def audit_integrity(self) -> dict:
        """``/ooh/v1/audit/integrity``: a closed instance refuses 503 SERVICE_CLOSED."""
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
            return "the ledger holds anchors of another ooh log: this log was deleted or replaced"
        for seq, line_sha in enumerate(shas, start=1):
            eid, payload = self._anchor_ids(epoch, seq, line_sha)
            e = by_id.get(eid)
            if e is None or e.get("payload_sha256") != payload_sha256(payload) or e.get("subject_id") != f"log:{epoch}":
                return f"local log line {seq} has no matching anchor on the ledger"
        if len(mine) > len(shas):
            return "the ledger holds anchors beyond the local log: the log was truncated or rolled back"
        return None


    def _after_integrity(self) -> None:
        """Nothing is seeded: OOH-1 starts empty (tenants are Andre's)."""

    # ================================================================================================ status

    def health(self) -> dict:
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
                "ports": {"finance_31": "NOT_CONNECTED", "vendor_orders": "NOT_CONNECTED", "esign": "NOT_CONNECTED",
                          "inventory_data": self.inventory_port.status, "measurement": "NOT_CONNECTED",
                          "programmatic_dooh": "NOT_CONNECTED"},
                "funds_verification": {"mode": "andre_gate", "sources_connected": list(policy.FUNDS_SOURCES_CONNECTED),
                                       "finance_31": "NOT_CONNECTED", "policy": policy.FUNDING_POLICY},
                "principal_agent": self.principal_agent_state(),
                "limits": {"max_purchase_amount": s.max_purchase_amount, "authority_max_days": s.authority_max_days,
                           "max_purchase_ceiling": policy.MAX_PURCHASE_CEILING,
                           "authority_days_ceiling": policy.AUTHORITY_DAYS_CEILING,
                           "inventory_max_per_campaign": s.inventory_max},
                "commercial": {"suggested_markup_pct": policy.SUGGESTED_MARKUP_PCT, "suggested_markup_applied": False},
                "agents_built": 0,
                "tenants": len(self.tenants),
                "campaigns": len(self.campaigns),
                "log_length": len(self.log),
            }

    # ================================================================================================ jobs

    def run_job(self, name: str, body: dict) -> dict:
        if name not in JOBS:
            raise NotFound(R("JOB_UNKNOWN"))
        out = self._integrity_and_ledger(always=True)          # refused when closed; verify() runs outside the lock
        return {"job": name, "integrity": out["integrity"], "ledger_valid": out["ledger_valid"]}

    # ================================================================================================ audit

    def audit_evidence(self, limit: int, offset: int, event_type: Optional[str] = None) -> dict:
        """``/ooh/v1/audit/evidence`` — the view Compliance (38) and auditors use (AEGIS round 6 M1). Every typed
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
        """The local log (ids, codes, amounts, hashes and bounded vendor-text extracts marked untrusted)."""
        with self.lock:
            if self._closed:
                raise Unavailable(R("SERVICE_CLOSED"))
        out = []
        for r in self.log.iter_records(max(1, since_seq)):
            d = {k: v for k, v in r["data"].items() if k not in ("_obj", "request_sha")}
            out.append({"seq": r["seq"], "kind": r["kind"], "at": r["at"], "data": d})
            if len(out) >= limit:
                break
        return {"events": out, "log_length": len(self.log)}
