"""
Lead Generation & Opportunity Intelligence (26) + Sales (27) — the core (ADR 0013).

One lock guards all state. Every state change is one ``_commit``: when the change is a send, a consent change, a
suppression, a price approval or a proposal approval, its typed evidence event is recorded on the ledger first; then
the exact log line is prepared, fsynced aside (pending line), anchored on the evidence ledger, appended to the local
log, and only then applied to memory — the same ``_apply`` that rebuilds state from the log at start, so live state
and replayed state cannot diverge. The plumbing (``_commit``, ``_anchor``, ``_settle_pending``, ``_roll_forward``,
``verify_integrity``) is security-py's as fixed in its AEGIS rounds 1-5 (ADR 0012 amendments).

The department's work is in three mixins: svc_leads.py (intake, pipeline, tasks), svc_outreach.py (consent,
suppression, templates, outreach, replies, sending pace) and svc_deals.py (price books, proposals, hand-offs).
The deciding rules are in intelligences/ (deterministic, one job each).
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from typing import Optional

from clock import Clock, SystemClock, iso
from config import Settings
from errors import Conflict, NotFound, Unavailable
from intelligences import i02_identity, i12_audit_export
from ledger import DEPARTMENT, LedgerConflict, LedgerQueryFailed, LedgerRecordError, Recorder, derived_id, payload_sha256
from ports import Ports
from reasons import R
from store import RecordLog, StoreCorrupt, StoreWriteError, verify_lines
from svc_deals import DealsMixin
from svc_leads import LeadsMixin
from svc_outreach import OutreachMixin

INTEGRITY_RETRY_S = 15
FORCED_MIN_S = 10
JOBS = ("send-queue", "warmup-reset", "handoff-retry", "stale-leads", "integrity")


def _maybe(exc: Unavailable) -> Unavailable:
    """Mark an outcome as unknown: the line is pending and may still take effect (ADR 0012 round 2 N2)."""
    exc.maybe = True
    return exc


def sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def request_sha(body: dict) -> str:
    return payload_sha256(body)


class SalesService(LeadsMixin, OutreachMixin, DealsMixin):
    def __init__(self, settings: Settings, recorder: Recorder, log: RecordLog, ports: Optional[Ports] = None,
                 clock: Optional[Clock] = None):
        self.settings = settings
        self.rec = recorder
        self.log = log
        self.ports = ports or Ports.default()
        self.clock = clock or SystemClock()
        self.lock = threading.RLock()
        self.pii_key: bytes = settings.pii_key.reveal()
        self.pii_fp = i02_identity.key_fingerprint(self.pii_key)
        # state rebuilt from the log
        self.bound_pii_fp: Optional[str] = None
        self.accounts: dict[str, dict] = {}
        self.domain_index: dict[str, str] = {}
        self.contacts: dict[str, dict] = {}
        self.email_index: dict[str, str] = {}
        self.phone_index: dict[str, str] = {}
        self.leads: dict[str, dict] = {}
        self.opps: dict[str, dict] = {}
        self.activities: dict[str, list] = {}
        self.tasks: dict[str, dict] = {}
        self.suppression: dict[str, dict] = {}
        self.consents: dict[str, list] = {}
        self.templates: dict[str, dict] = {}
        self.messages: dict[str, dict] = {}
        self.warmup: dict = {"step": 0, "started_on": None, "advanced_on": None}
        self.day_stats: dict[str, dict] = {}
        self.pricebook: dict[str, dict] = {}
        self.proposals: dict[str, dict] = {}
        self.handoffs: dict[str, dict] = {}
        self.requests: dict[tuple, tuple] = {}
        # memory only
        self.integrity = {"ok": False, "checked_at": None, "problem": "not yet verified against the ledger"}
        self._last_integrity_try = 0
        self._own_pending: Optional[bytes] = None
        self.handoff_attempts: dict[str, dict] = {}
        self._init_pricebook()
        for r in self.log.iter_records():
            self._apply(r["kind"], r["data"], r["at"])
        if self.bound_pii_fp is not None and self.bound_pii_fp != self.pii_fp:
            raise StoreCorrupt("SALES_PII_HASH_KEY_FILE holds a different key from the one this log was written "
                               "with: every suppression, consent and dedupe hash would stop matching. Refusing to "
                               "start; restore the original key")
        self.verify_integrity(force=True)

    # ================================================================================================ plumbing

    def now(self) -> datetime:
        return self.clock.now()

    def today(self) -> str:
        return self.now().date().isoformat()

    def _anchor_ids(self, epoch: str, seq: int, line_sha: str) -> tuple[str, dict]:
        payload = {"epoch": epoch, "seq": seq, "line_sha256": line_sha}
        return derived_id("anc", epoch, seq, line_sha), payload

    def _commit(self, kind: str, data: dict, actor: str, evidence: Optional[tuple] = None) -> dict:
        """Typed evidence first (when the change needs one), then the ledger anchor, then the local log, then
        memory (fail closed at every step). ``evidence`` = (event_type, subject_id, payload, id_parts): the payload
        carries ids and hashes only, never a raw email, phone or name, and no time (a retry records the same
        event, which the ledger answers 200)."""
        with self.lock:
            if not self.integrity["ok"]:
                raise Unavailable(R("INTEGRITY_UNVERIFIED"))
            at = iso(self.now())
            data = {**data, "actor": data.get("actor", actor)}   # the anchor's actor is read back from the line
            actor = data["actor"]
            if evidence is not None:
                event_type, subject_id, payload, id_parts = evidence
                try:
                    self.rec.record(derived_id("evd", event_type, *id_parts), event_type, actor, subject_id, payload,
                                    f"{event_type} {subject_id}")
                except LedgerRecordError:
                    raise Unavailable(R("LEDGER_UNAVAILABLE")) from None
            rec, line = self.log.prepare(kind, at, data)
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

    def _anchor(self, eid: str, actor: str, epoch: str, payload: dict, kind: str, seq: int) -> None:
        """Record the anchor; one retry of the SAME event when the answer was lost (the ledger is idempotent on
        identical content, so the retry either records it or confirms it is there)."""
        try:
            self.rec.record(eid, "log_anchor", actor, f"log:{epoch}", payload, f"{kind} #{seq}")
        except LedgerRecordError as exc:
            if exc.took_effect is False or isinstance(exc, LedgerConflict):
                raise
            self.rec.record(eid, "log_anchor", actor, f"log:{epoch}", payload, f"{kind} #{seq}")

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
        """Idempotency by (actor, operation, target, request_id) — ``rk`` is ``operation|target|request_id`` — and
        the body's hash: the same body answers what the first call did, a different body is 409."""
        prev = self.requests.get((actor, rk))
        if prev is None:
            return None
        if prev[0] != request_sha(body):
            raise Conflict(R("REQUEST_ID_REUSED"))
        return prev

    @staticmethod
    def _req(data: dict, actor: str, rk: str, body: dict, obj) -> dict:
        return {**data, "actor": actor, "request_id": rk, "request_sha": request_sha(body), "_obj": obj}

    def _gate(self) -> None:
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
            raise StoreCorrupt(f"log record of unknown kind {kind!r}")
        handler(d, at)
        if d.get("request_id") and d.get("actor"):
            self.requests[(d["actor"], d["request_id"])] = (d.get("request_sha"), d.get("_obj"))

    def _a_pii_key_bound(self, d, at):
        self.bound_pii_fp = d["fingerprint"]

    def _a_job_ran(self, d, at):
        pass

    # ================================================================================================ integrity

    def verify_integrity(self, force: bool = False, always: bool = False) -> dict:
        """Complete or set aside the pending line, then check every local line's anchor on the ledger, and that
        the ledger holds no anchor this log lacks (a truncated, rolled back, deleted or replaced log)."""
        with self.lock:
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
            self.integrity = {"ok": problem is None, "checked_at": at, "problem": problem}
            if problem is None:
                self._after_integrity()
            return dict(self.integrity)

    def _settle_pending(self, by_id: dict) -> tuple[Optional[str], bool]:
        """Settle a line that was prepared but may not have reached the log. Returns (problem, appended).

        - A line THIS process wrote (``_own_pending``, kept in memory) is rolled forward: its identical anchor is
          re-recorded (the ledger answers 200 whether or not it already held it), then it is appended. The file
          copy is never what is trusted (ADR 0012 R4-1).
        - A line found on disk at start (this process did not write it) is appended only if the ledger ALREADY holds
          its anchor. Otherwise it is kept aside (``pending.discarded``), never anchored by us: a forged line stays
          inert. If its anchor appears later (it was in flight when the process stopped), the next check appends it.
        """
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
        self._apply(kind, data, rec["at"])
        return None, True

    def _anchor_problem(self, mine: list, by_id: dict) -> Optional[str]:
        shas = self.log.line_shas()
        epoch = self.log.epoch
        epochs = {e.get("subject_id") for e in mine}
        if epochs - ({f"log:{epoch}"} if epoch else set()):
            return "the ledger holds anchors of another sales log: this log was deleted or replaced"
        for seq, line_sha in enumerate(shas, start=1):
            eid, payload = self._anchor_ids(epoch, seq, line_sha)
            e = by_id.get(eid)
            if e is None or e.get("payload_sha256") != payload_sha256(payload) or e.get("subject_id") != f"log:{epoch}":
                return f"local log line {seq} has no matching anchor on the ledger"
        if len(mine) > len(shas):
            return "the ledger holds anchors beyond the local log: the log was truncated or rolled back"
        return None

    def _after_integrity(self) -> None:
        """Bind the PII hash key's fingerprint into the log once (a later start with another key refuses)."""
        if self.bound_pii_fp is None:
            try:
                self._commit("pii_key_bound", {"fingerprint": self.pii_fp}, "sales")
            except Unavailable:
                pass

    # ================================================================================================ status

    def health(self) -> dict:
        with self.lock:
            s = self.settings
            return {
                "status": "ok" if self.integrity["ok"] else "degraded",
                "integrity": dict(self.integrity),
                "in_memory": self.log.in_memory,
                "non_production": s.non_production,
                "outreach_domain": s.outreach_domain,
                "email_outreach_configured": bool(s.outreach_domain and s.postal_address),
                "ports_wired": self.ports.wired(),
                "warmup_step": self.warmup["step"] + 1,
                "send_cap_today": self._cap_today(),
                "sent_today": self._stats(self.today())["sent"],
                "queued": sum(1 for m in self.messages.values() if m["status"] == "queued"),
                "pending_andre": sum(1 for p in self.proposals.values() if p["status"] == "pending_andre"),
                "handoffs_pending": sum(1 for h in self.handoffs.values() if h["status"] == "pending_delivery"),
                "prices_approved": sum(1 for x in self.pricebook.values() if x["approved"]),
                "suppressed": len(self.suppression),
                "open_tasks": sum(1 for t in self.tasks.values() if t["status"] == "open"),
            }

    # ================================================================================================ jobs

    def run_job(self, name: str, body: dict) -> dict:
        if name not in JOBS:
            raise NotFound(R("JOB_UNKNOWN"))
        if name == "integrity":
            res = self.verify_integrity(force=True, always=True)   # the job always reads the ledger
            return {"job": name, "integrity": res, "ledger_valid": self.rec.client.verify()}
        if name == "send-queue":
            return self.send_tick(body)
        if name == "handoff-retry":
            return self.retry_handoffs(body)
        with self.lock:
            self._gate()
            rk = f"job|{name}|{body['request_id']}"
            prev = self._idem("scheduler", rk, body)
            if prev:
                return {"job": name, "already_ran": True, **(prev[1] or {})}
            if name == "warmup-reset":
                result = self._warmup_reset(body, rk)
            else:
                result = self._age_leads(body, rk)
            return {"job": name, **result}

    # ================================================================================================ audit

    def audit_export(self, since_seq: int, limit: int) -> dict:
        """The local log, personal data minimised (i12): emails and phones as keyed hashes, names and notes as
        SHA-256."""
        out = []
        for r in self.log.iter_records(max(1, since_seq)):
            d = {k: v for k, v in r["data"].items() if k not in ("_obj", "request_sha")}
            out.append({"seq": r["seq"], "kind": r["kind"], "at": r["at"], "data": i12_audit_export.minimise(d)})
            if len(out) >= limit:
                break
        return {"events": out, "log_length": len(self.log)}
