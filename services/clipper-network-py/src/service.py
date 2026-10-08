"""
Clipper Network service layer: state, record-first plumbing, every operation.

Order of every state change (spec §A "recorded on the ledger before it takes
effect"; the compliance-py order, ADR 0006 decision 4), without exception:
  1. every port call is recorded on the ledger first (``crossing_<port>_requested``,
     payload: ids and an argument hash only — never an email, DOB or OAuth code);
  2. contact data, if any, is written to the contact store (never the log);
  3. the change's own ledger events are recorded (deterministic ids);
  4. ONE local-log line holding every object the operation changes is anchored
     on the ledger, appended (fsynced) and only then applied and answered.
A failure at 1-4 raises ``Unavailable`` (HTTP 503): nothing took effect here.
Message delivery happens AFTER the operation's commit, as its own recorded
step; a delivery that cannot be recorded leaves the message queued for
``/cn/v1/messages/flush`` (it never undoes the operation).

State is event-sourced from the local log (``batch`` lines of whole-object
puts), so a restart with CN_DATA_DIR replays exactly what was answered. One
lock serializes every operation.
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import hmac
import json
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from clock import Clock, SystemClock, iso, parse_iso
from contacts import ContactStore, ContactStoreError
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from intelligences import (i01_recruiting, i02_admission, i03_tiering, i04_enrolment, i05_kit_delivery, i06_comms,
                           i07_disputes, i08_discipline, i09_offboarding, i10_evidence_audit)
from intelligences.common import Citer, finalize, item, rules_not_in_force, unavailable, unmet_line
from ledger import (LedgerConflict, LedgerQueryFailed, LedgerRecordError, Recorder, canonical, clean_summary,
                    derived_id, payload_sha256)
from ports import (Ack, AgeAnswer, CertificationsAnswer, CompleteAnswer, ComplianceRuling, ConnectionsAnswer,
                   DelegateAnswer, DocVersionAnswer, FindingAnswer, IdentityAnswer, IntegrityAnswer, JurisdictionAnswer,
                   KitAnswer, OpenItemsAnswer, Ports, RateCardAnswer, RulebookAnswer, SendAnswer, StartAnswer, StrikeFeed,
                   TaxAnswer)
import rules as R
import templates as T
from store import DataDirBusy, RecordLog, StoreWriteError
from textguard import injection_rules_in, looks_like_phone, is_email, normalize_email

IDEMPOTENCY_WINDOW = timedelta(minutes=15)
IDEMPOTENCY_MAX = 200_000
EXPORT_PAGE = 500
MAX_DELIVERY_ATTEMPTS = 5
STRIKE_PAGES_PER_SYNC = 20
EVIDENCE_ACTOR = i10_evidence_audit.ACTOR
MEMBER_EXIT_TEMPLATES = ("offboarding_confirmation", "data_export_ready", "ban_notice", "appeal_outcome",
                         "appeal_received")
COLLECTIONS = ("clippers", "applications", "acceptances", "trainings", "admissions", "enrolment_rulings", "configs",
               "enrolments", "kits", "messages", "opt_ins", "suppression", "recruits", "disputes", "strikes",
               "strike_refusals", "ban_proposals", "offboardings", "tier_history", "announcements", "meta")


def _b32(s: str, n: int = 26) -> str:
    return base64.b32encode(hashlib.sha256(s.encode("utf-8", "surrogatepass")).digest()).decode("ascii").lower()[:n]


def _sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8", "surrogatepass")).hexdigest()


def _plain(obj: Any) -> Any:
    """Port answers (dataclasses) as plain data, for facts hashes."""
    if dataclasses.is_dataclass(obj):
        return {k: _plain(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(x) for x in obj]
    return obj


@dataclass
class Config:
    channels: tuple[str, ...] = ("discord_server_post", "email_opt_in", "inbound_form", "referral")
    postal_address: Optional[str] = None
    opt_out_url: Optional[str] = None


class PortCalls:
    """Record-first proxy to the ports for one operation: each distinct call is recorded as
    ``crossing_<port>_requested`` BEFORE it is made (payload: ids and a hash of the NON-sensitive
    arguments only); answers are memoized for the operation; a port that raises is unavailable."""

    def __init__(self, svc: "CNService", op_id: str, actor: str, subject: str, events: list[str]):
        self.svc, self.op_id, self.actor, self.subject, self.events = svc, op_id, actor, subject, events
        self._memo: dict = {}

    def call(self, port: str, action: str, args: tuple, fn: Callable, fallback):
        key = (port, action, canonical(list(args)))
        if key in self._memo:
            return self._memo[key]
        eid = derived_id("x", self.op_id, port, action, list(args))
        self.svc._record(eid, f"crossing_{port}_requested", self.actor, self.subject,
                         {"port": port, "action": action, "args_sha256": _sha(list(args)), "op": self.op_id},
                         f"Request to {port}: {action}", raw=True)
        self.events.append(eid)
        try:
            ans = fn()
        except Exception:  # noqa: BLE001 - any port failure is "unavailable", never a pass
            ans = fallback
        if not isinstance(ans, type(fallback)):
            ans = fallback      # a port that answers the wrong type is unavailable too
        self._memo[key] = ans
        return ans


class CNService:
    def __init__(self, config: Config, recorder: Recorder, log: RecordLog, contacts: ContactStore, seed_bytes: bytes,
                 expected_seed_sha256: str, pinned_seed_sha256: str, identity_key: str, ports: Optional[Ports] = None,
                 clock: Optional[Clock] = None, reconcile_mode: bool = False, dir_lock=None,
                 lock_token: Optional[str] = None):
        self.config = config
        self.recorder = recorder
        self.log = log
        self.contacts = contacts
        self.ports = ports or Ports()
        self.clock = clock or SystemClock()
        self.lock = threading.RLock()
        self.reconcile_mode = bool(reconcile_mode)
        self._named: dict[str, dict] = {}     # bug sweep C R6: evidence recorded, until its line names it
        # rulings recorded on the ledger and not yet named by a committed line -> the request epoch that recorded them;
        # one left behind by an earlier request is an ATTEMPT, named by the next committed line (never a ghost)
        self._pending_rulings: dict[str, int] = {}
        self._op_epoch = 0
        # bug sweep C (E-5/F-3, finance-py's single writer): one service instance per data directory, also within one
        # process. api.build_service claims the flock BEFORE the log is built and passes the claim's token; this
        # instance adopts it only if the token IS the current claim, and gives it back on close() or a failed start.
        self._closed = False
        self._dir_lock = dir_lock
        self._lock_token: Optional[str] = None
        if self._dir_lock is not None:
            if lock_token is None:
                lock_token = self._dir_lock.claim()
            adopted = self._dir_lock.adopt(lock_token)
            if adopted is None:
                self._dir_lock = None
                raise DataDirBusy("the data-directory claim handed to this service instance is not held (released, "
                                  "stale, another instance's, or already adopted); refusing to start")
            self._lock_token = adopted
        try:
            self._start(seed_bytes, expected_seed_sha256, pinned_seed_sha256, identity_key)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Give the data directory back (a restart in the same process closes the old instance first). A closed
        instance is inert: its log refuses every write and every ledger record is refused (503 SERVICE_CLOSED).
        Taken under the service lock, so it never interleaves with a commit."""
        with self.lock:
            self._closed = True
            with self.log.lock:
                self.log.closed = True
            if self._dir_lock is not None:
                self._dir_lock.release_claim(self._lock_token)
                self._dir_lock = None
                self._lock_token = None

    @property
    def closed(self) -> bool:
        return self._closed

    def _start(self, seed_bytes: bytes, expected_seed_sha256: str, pinned_seed_sha256: str, identity_key: str) -> None:
        self._idkey = identity_key.encode("ascii")
        self._volatile = secrets.token_bytes(32)      # idempotency hashes of DOB / OAuth code: never persisted
        seed_sha = hashlib.sha256(seed_bytes).hexdigest()
        if seed_sha != expected_seed_sha256:
            raise RuntimeError(f"rules seed SHA-256 {seed_sha} does not match the expected {expected_seed_sha256}; "
                               "refusing to start (the seed must be the pinned file, unchanged)")
        self.seed_sha = seed_sha
        self.rules_pinned = seed_sha == pinned_seed_sha256
        self.seed = json.loads(seed_bytes)
        self.seed_rules = {r["rule_id"]: r for r in self.seed["rules"]}
        self.versions: list[R.Version] = []
        self.proposals: dict[str, dict] = {}
        self.st: dict[str, dict] = {c: {} for c in COLLECTIONS}
        self.idem: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
        self._ledger_conflict = False
        self.instance_id = secrets.token_hex(8)
        self.reconcile_required: list[str] = []
        self._reconciling = False
        self.contacts_missing: list[str] = []
        for rec in self.log.iter_records():
            self._apply(rec["kind"], rec["data"]["record"])
        tampered, missing = self.contacts.reconcile(self._expected_contacts())
        if tampered:
            raise RuntimeError(f"refusing to start: {len(tampered)} contact record(s) differ from the hash the log "
                               "recorded (the contact store was edited)")
        self.contacts_missing = missing
        if not self.log.in_memory:
            try:
                a = self.assess_log()
            except LedgerQueryFailed as exc:
                raise RuntimeError(f"refusing to start: the local log cannot be verified against the evidence ledger "
                                   f"({exc})") from None
            if a.fatal or (a.voidable and not self.reconcile_mode):
                hint = ("" if a.fatal or not a.voidable else
                        " -- only Andre can void these: start with CN_RECONCILE_MODE=1 and POST /cn/v1/reconcile "
                        "(README, 'Reconciling the local log with the ledger')")
                raise RuntimeError("refusing to start: " + "; ".join(a.problems) + hint)
            self.reconcile_required = list(a.voidable)
        if self.reconcile_mode:
            return
        try:
            self.ensure_seed_proposal()
        except Unavailable:
            pass  # ledger down at start: retried on the next inbox / decision call
        if not self.log.in_memory and len(self.log):
            try:
                self._write_lease()
            except Unavailable as exc:
                raise RuntimeError(f"refusing to start: the instance lease could not be recorded ({exc.reason})") from None

    # ================================================================== plumbing

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def hmac_email(self, email: str) -> str:
        return hmac.new(self._idkey, normalize_email(email).encode("utf-8"), hashlib.sha256).hexdigest()

    def _volatile_hash(self, value: str) -> str:
        return hmac.new(self._volatile, value.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()

    def _record(self, event_id: str, event_type: str, actor: str, subject: str, payload: dict, summary: str,
                raw: bool = False) -> str:
        """Record one ledger event FIRST and return the id actually recorded.

        Bug sweep C (R6, the bizdev-py / finance-py pattern): ``event_id`` is the logical action's key ``rk``; the id
        recorded is ``i10_evidence_audit.evidence_id(rk, type, payload_sha256, actor/subject/summary hash)`` over a payload
        that also carries ``rk`` and ``seq`` (the next log line when it was recorded). The same action with the same
        content at the same log position is the same id (the ledger answers 200 to a retry); after any state change it
        is a NEW id, never a lasting 409. The commit that applies it names it (``data.evidence``); one that never
        commits leaves it ``attempted`` (GET /audit/evidence). ``raw``: the id and payload are recorded exactly as
        given — ids other code parses or hands out (port crossings, anchors, leases, reconciles, version events,
        ruling ids)."""
        self._ledger_conflict = False
        if self._closed:
            raise Unavailable("SERVICE_CLOSED: this Clipper Network instance is closed; nothing was recorded",
                              ledger_write="not_recorded")
        if not raw:
            rk = event_id
            payload = {**payload, "rk": rk, "seq": len(self.log) + 1}
            meta = i10_evidence_audit.meta_sha256(actor, subject[:128], clean_summary(summary))
            event_id = i10_evidence_audit.evidence_id(rk, event_type, payload_sha256(payload), meta)
        if self.reconcile_mode and not self._reconciling:
            raise Unavailable("reconcile mode (CN_RECONCILE_MODE=1): only Andre's POST /cn/v1/reconcile is answered; "
                              "restart without it once the log is reconciled", ledger_write="not_recorded")
        try:
            eid = self.recorder.record(event_id, event_type, actor, subject[:128], payload, summary)
        except LedgerRecordError as exc:
            self._ledger_conflict = isinstance(exc, LedgerConflict)
            raise Unavailable(f"evidence ledger write failed ({type(exc).__name__}); nothing took effect",
                              ledger_write="unknown" if exc.took_effect != False else "not_recorded") from None  # noqa: E712
        if event_type in i10_evidence_audit.RULING_TYPES:
            self._pending_rulings[eid] = self._op_epoch
        if not raw:
            if len(self._named) > 100_000:
                self._named.clear()
            self._named[eid] = {"event_id": eid, "event_type": event_type, "rk": rk,
                                "payload_sha256": payload_sha256(payload)}
        return eid

    def _commit(self, kind: str, record: dict, event_ids: list[str], after_anchor=None) -> dict:
        """Anchor the exact next local-log line on the ledger, append it (fsynced), then apply it (N14-4/N15-1)."""
        if self._closed:
            raise Unavailable("SERVICE_CLOSED: this Clipper Network instance is closed; nothing took effect")
        data = {"record": record, "ledger_event_ids": list(event_ids), "register_version": self.version_number,
                "anchored": True}
        named = [self._named[e] for e in event_ids if e in self._named]
        if named:
            data["evidence"] = named
        attempted = [e for e, ep in self._pending_rulings.items() if ep < self._op_epoch and e not in event_ids]
        if attempted:
            # a ruling recorded by an earlier try of this request that never committed (its id then moved to the
            # outcome-derived one): named here, so a restart does not take it for another instance's ruling
            data["attempted_event_ids"] = attempted
        rec, line = self.log.prepare(kind, iso(self._now()), data)
        line_sha = hashlib.sha256(line).hexdigest()
        epoch = self.log.epoch or line_sha[:16]
        self._record(i10_evidence_audit.anchor_id(epoch, rec["seq"], line_sha), i10_evidence_audit.ANCHOR_TYPE,
                     EVIDENCE_ACTOR, i10_evidence_audit.LOG_SUBJECT,
                     {"epoch": epoch, "seq": rec["seq"], "line_sha256": line_sha, "kind": kind},
                     f"Local log line {rec['seq']} ({kind}) anchored", raw=True)
        if after_anchor is not None:
            after_anchor()
        try:
            self.log.append_prepared(rec, line)
        except StoreWriteError as exc:
            if kind == "decision" and self.log.path:
                try:
                    with open(f"{self.log.path}.unwritten-{rec['seq']}", "wb") as fh:
                        fh.write(line + b"\n")
                except OSError:
                    pass
            raise Unavailable(f"local store write failed ({exc}); nothing took effect") from None
        for e in list(event_ids) + list(data.get("attempted_event_ids") or []):
            self._named.pop(e, None)
            self._pending_rulings.pop(e, None)
        self._apply(kind, record)
        return record

    def _batch(self, puts: list[tuple[str, str, dict]], events: list[str], note: str) -> None:
        self._commit("batch", {"note": note, "puts": [{"coll": c, "id": i, "value": v} for c, i, v in puts]}, events)

    def _apply(self, kind: str, r: dict) -> None:
        if kind == "batch":
            for p in r["puts"]:
                self.st[p["coll"]][p["id"]] = p["value"]
                if p["coll"] in ("admissions", "enrolment_rulings"):
                    self._rebuild_idem(p["value"])
        elif kind == "proposal":
            self.proposals[r["proposal_id"]] = {**r, "status": "open", "decided_at": None, "note": None}
        elif kind == "decision":
            for d in r["decisions"]:
                p = self.proposals[d["proposal_id"]]
                p.update(status="approved" if d["decision"] == "approve" else "rejected", decided_at=r["decided_at"],
                         note=d.get("note"))
            v = r.get("version")
            if v:
                self.versions.append(R.Version(v["version"], v["created_at"], v["approved_by"], tuple(v["proposal_ids"]),
                                               v["content_sha256"], v["prev_version_sha256"], tuple(v["rules"]),
                                               tuple(v["templates"])))
        # lease, reconcile, injection, founder_refused, evidence: nothing to apply

    def _rebuild_idem(self, r: dict) -> None:
        if not r.get("request_sha256"):
            return
        key = (r["principal"], r["request_id"])
        self.idem[key] = {"h": r["request_sha256"], "at": parse_iso(r["first_used_at"]), "response": r["view"],
                          "outcome_sha256": r["outcome_sha256"], "ruling_id": r["ruling_id"]}
        self.idem.move_to_end(key)
        while len(self.idem) > IDEMPOTENCY_MAX:
            self.idem.popitem(last=False)

    def _expected_contacts(self) -> dict[str, str]:
        out = {}
        for cid, c in self.st["clippers"].items():
            if c.get("contact_sha256"):
                out[f"clipper:{cid}"] = c["contact_sha256"]
            for a in c.get("connected_accounts") or []:
                if a.get("handle_contact_sha256"):
                    out[f"handle:{cid}:{a['vi_connection_id']}"] = a["handle_contact_sha256"]
        for oid, o in self.st["opt_ins"].items():
            if o.get("contact_sha256"):
                out[f"optin:{oid}"] = o["contact_sha256"]
        return out

    def _contact_put(self, key: str, value: dict) -> str:
        try:
            return self.contacts.put(key, value)
        except ContactStoreError as exc:
            raise Unavailable(f"contact store write failed ({exc}); nothing took effect") from None

    def _contact_rollback(self, key: str) -> None:
        """Best effort: a contact written for an operation that did not commit is an orphan (also purged at start)."""
        if key not in self._expected_contacts():
            try:
                self.contacts.delete(key)
            except ContactStoreError:
                pass

    def _idem_check(self, principal: str, request_id: str, route: str, body: Any) -> tuple[tuple, str, Optional[dict]]:
        self._op_epoch += 1               # bug sweep C R6: a new request (see _pending_rulings)
        key = (principal, request_id)
        h = _sha({"route": route, "body": body})
        ent = self.idem.get(key)
        if ent is not None:
            if ent["h"] != h:
                raise Conflict("request_id already used with a different body")
            if self._now() - ent["at"] > IDEMPOTENCY_WINDOW:
                raise Conflict("request_id reused (first used more than 15 minutes ago)")
            return key, h, ent["response"]
        return key, h, None

    def _idem_entry(self, principal: str, request_id: str, route: str, body: Any) -> tuple[tuple, str, Optional[dict]]:
        self._op_epoch += 1               # bug sweep C R6: a new request (see _pending_rulings)
        key = (principal, request_id)
        h = _sha({"route": route, "body": body})
        ent = self.idem.get(key)
        if ent is not None:
            if ent["h"] != h:
                raise Conflict("request_id already used with a different body")
            if self._now() - ent["at"] > IDEMPOTENCY_WINDOW:
                raise Conflict("request_id reused (first used more than 15 minutes ago)")
        return key, h, ent

    def _idem_store(self, key: tuple, h: str, response: dict) -> dict:
        self.idem[key] = {"h": h, "at": self._now(), "response": response}
        while len(self.idem) > IDEMPOTENCY_MAX:
            self.idem.popitem(last=False)
        return response

    def _injection(self, op_id: str, obj: Any, subject: str, actor: str) -> list[str]:
        """Client text is data: instruction-like text is recorded (rule names and counts only) and changes nothing."""
        found = injection_rules_in(obj)
        if not found:
            return []
        return [self._record(derived_id("inj", op_id), "injection_text_ignored", actor, subject,
                             {"rules": found, "count": len(found), "op": op_id},
                             f"Instruction-like text in client data ignored ({len(found)} pattern(s)); decision unaffected")]

    # ================================================================== evidence / reconcile (i10)

    def assess_log(self) -> "i10_evidence_audit.Assessment":
        client = self.recorder.client
        if not hasattr(client, "entries"):
            raise LedgerQueryFailed("this ledger client cannot read entries")
        entries = client.entries()
        return self._assess(entries)

    def _assess(self, entries: list) -> "i10_evidence_audit.Assessment":
        shas = self.log.line_shas()
        lines, referenced, rulings, leases, reconciles, metas, named = [], set(), set(), [], [], [], set()
        for rec, sha in zip(self.log.iter_records(), shas):
            d = rec["data"]
            lines.append((rec["seq"], sha, bool(d.get("anchored"))))
            referenced.update(d.get("ledger_event_ids") or [])
            # bug sweep C R6: committed actions (with their line's seq) and the earlier attempts a line names
            named.update((n.get("rk"), n.get("event_type"), rec["seq"]) for n in d.get("evidence") or [])
            rulings.update(x for x in d.get("attempted_event_ids") or [] if isinstance(x, str))
            r = d.get("record") or {}
            if rec["kind"] == "decision" and r.get("version"):
                metas.append(r["version"])
            if rec["kind"] == "batch":
                for p in r.get("puts") or []:
                    if p["coll"] in ("admissions", "enrolment_rulings"):
                        rulings.add(p["value"].get("ruling_id"))
            elif rec["kind"] == "lease":
                leases.append((rec["seq"], r.get("instance_id"), r.get("lease_event_id")))
            elif rec["kind"] == "reconcile":
                reconciles.append((rec["seq"], r.get("payload"), r.get("reconcile_event_id"), d.get("register_version")))
        return i10_evidence_audit.assess(entries, self.log.epoch, lines, referenced, self.version_number or 0,
                                         strict=not self.log.in_memory, local_rulings=rulings, local_leases=leases,
                                         reconciles=reconciles,
                                         local_versions=i10_evidence_audit.local_version_events(self.log.epoch, metas),
                                         committed_actions=named)

    def _write_lease(self) -> None:
        with self.lock:
            n = len(self.log)
            head = self.log.line_shas()[-1]
            eid = i10_evidence_audit.lease_id(self.log.epoch, self.instance_id, n, head)
            self._record(eid, i10_evidence_audit.LEASE_TYPE, EVIDENCE_ACTOR, i10_evidence_audit.LOG_SUBJECT,
                         {"instance_id": self.instance_id, "epoch": self.log.epoch, "head_seq": n, "head_sha256": head},
                         f"Clipper Network instance lease at log line {n}", raw=True)
            self._commit("lease", {"instance_id": self.instance_id, "lease_event_id": eid, "head_seq": n,
                                   "head_sha256": head}, [eid])

    def reconcile_plan(self) -> dict:
        with self.lock:
            a = self.assess_log()
            shas = self.log.line_shas()
            return {"epoch": self.log.epoch, "head_seq": len(shas), "head_sha256": shas[-1] if shas else None,
                    "rules_version": self.version_number, "fatal": a.fatal, "problems": a.voidable,
                    "voidable": {"lines": sorted(a.void_lines), "event_ids": sorted(a.void_event_ids)},
                    "reconcile_mode": self.reconcile_mode}

    def reconcile(self, request_id: str, head_sha256: str, void_lines: list[int], void_event_ids: list[str]) -> dict:
        with self.lock:
            key, h, cached = self._idem_check("andre", request_id, "reconcile",
                                              {"head": head_sha256, "lines": void_lines, "ids": void_event_ids})
            if cached:
                return cached
            plan = self.reconcile_plan()
            if plan["fatal"]:
                raise Conflict("cannot reconcile: " + "; ".join(plan["fatal"]))
            if head_sha256 != plan["head_sha256"]:
                raise Conflict("the local log head moved since you read the plan; read GET /cn/v1/reconcile again")
            if not plan["voidable"]["event_ids"]:
                raise Conflict("nothing to reconcile: the local log matches the ledger")
            if sorted(set(void_lines)) != plan["voidable"]["lines"] or \
                    sorted(set(void_event_ids)) != plan["voidable"]["event_ids"]:
                raise Conflict("the void list is not exactly what the ledger shows now; nothing was voided",
                               expected=plan["voidable"])
            payload = i10_evidence_audit.reconcile_payload(plan["epoch"], self.version_number, plan["head_seq"],
                                                           plan["head_sha256"], void_lines, void_event_ids)
            eid = i10_evidence_audit.reconcile_id(plan["epoch"], plan["head_seq"], payload)
            self._reconciling = True
            try:
                self._record(eid, i10_evidence_audit.RECONCILE_TYPE, "andre", i10_evidence_audit.LOG_SUBJECT, payload,
                             f"Andre reconciled the local log at line {plan['head_seq']}: "
                             f"{len(payload['void_event_ids'])} ledger event(s) declared void", raw=True)
                self._commit("reconcile", {"payload": payload, "reconcile_event_id": eid, "request_id": request_id}, [eid])
            finally:
                self._reconciling = False
            left = self.assess_log()
            self.reconcile_required = list(left.voidable)
            resp = {"reconcile_event_id": eid, "voided": payload["void_event_ids"], "void_lines": payload["void_lines"],
                    "remaining_problems": left.problems, "restart_required": self.reconcile_mode,
                    "ledger_event_ids": [eid]}
            return self._idem_store(key, h, resp)

    def integrity(self) -> dict:
        """Bug sweep C (slow I/O under the lock): the ledger's verify and entries (HTTP) and the chain re-read (disk)
        run OUTSIDE the service lock; only the comparison runs under it."""
        try:
            ledger_ok = bool(self.recorder.client.verify())
        except Exception:  # noqa: BLE001
            ledger_ok = False
        log_ok = self.log.verify()
        n0 = len(self.log)
        try:
            entries, unreadable = self.recorder.client.entries(), None
        except LedgerQueryFailed as exc:
            entries, unreadable = None, f"ledger entries unreadable ({exc})"
        with self.lock:
            if entries is not None and len(self.log) != n0:
                # a commit landed while the ledger was read: its anchor may be missing from that read (rare; re-read)
                try:
                    entries = self.recorder.client.entries()
                except LedgerQueryFailed as exc:
                    entries, unreadable = None, f"ledger entries unreadable ({exc})"
            problems = self._assess(entries).problems if entries is not None else [unreadable]
            if self.log.fault:
                problems.append(f"LOCAL_LOG_WRITE_FAULT: {self.log.fault}")
            return {"ledger_verify": ledger_ok, "local_log_chain": log_ok, "anchor_problems": problems,
                    "contacts_missing": len(self.contacts_missing),
                    "ok": ledger_ok and log_ok and not problems}

    def founder_refused(self, route: str, reason: str) -> None:
        """Best effort: a refusal stands whether or not it could be recorded."""
        with self.lock:
            if self.reconcile_mode:
                return
            op = f"{route}|{iso(self._now())}|{len(self.log)}"
            eid = self.recorder.try_record(derived_id("far", op), "founder_approval_refused", EVIDENCE_ACTOR,
                                           "founder_gate", {"route": route[:64], "reason_sha256": R.sha_text(reason)},
                                           "Andre approval refused")
            try:
                self._commit("founder_refused", {"route": route[:64], "reason": reason[:200]}, [eid] if eid else [])
            except Unavailable:
                pass

    # ================================================================== rules register (§B.9)

    @property
    def current(self) -> Optional[R.Version]:
        return self.versions[-1] if self.versions else None

    @property
    def version_number(self) -> Optional[int]:
        return self.current.version if self.current else None

    def p(self, rule_id: str, path: str) -> Any:
        return R.param(self.current, rule_id, path)

    def counsel_open(self, block: str) -> list[str]:
        v = self.current
        if v is None:
            return []
        return sorted(r["rule_id"] for r in v.rules if r["kind"] == "counsel" and r["status"] == "open"
                      and block in (r["parameters"].get("blocks") or []))

    def ensure_seed_proposal(self) -> None:
        with self.lock:
            if any(p["kind"] == "seed" for p in self.proposals.values()):
                return
            p = R.seed_proposal(self.seed, self.seed_sha, self._now())
            e1 = self._record(derived_id("seed", self.seed_sha), "rules_seed_loaded", EVIDENCE_ACTOR,
                              f"proposal:{p['proposal_id']}",
                              {"seed_sha256": self.seed_sha, "rules": len(self.seed["rules"]),
                               "templates": len(self.seed["templates"]), "proposal_id": p["proposal_id"]},
                              f"Rules seed hash verified; seed proposal created ({len(self.seed['rules'])} rules, "
                              f"{len(self.seed['templates'])} templates)")
            e2 = self._record(derived_id("prop", p["proposal_id"], p["content_sha256"]), "rules_proposal_created",
                              EVIDENCE_ACTOR, f"proposal:{p['proposal_id']}",
                              {"proposal_id": p["proposal_id"], "kind": "seed", "content_sha256": p["content_sha256"]},
                              "Rules proposal created: seed")
            self._commit("proposal", p, [e1, e2])

    def rules_view(self) -> dict:
        with self.lock:
            v = self.current
            if v is None:
                return {"rules_version": None, "rules": [], "templates": [], "rules_pinned": self.rules_pinned}
            return {"rules_version": v.version, "content_sha256": v.content_sha256, "version_sha256": v.version_sha256,
                    "rules": list(v.rules), "templates": list(v.templates), "rules_pinned": self.rules_pinned}

    def inbox(self) -> list[dict]:
        self.ensure_seed_proposal()
        with self.lock:
            return [dict(p) for p in sorted(self.proposals.values(), key=lambda x: (x["created_at"], x["proposal_id"]))
                    if p["status"] == "open"]

    def create_proposal(self, request_id: str, family: str, body: dict) -> dict:
        """Andre only (the API verified his token). A proposal never changes the register."""
        with self.lock:
            key, h, cached = self._idem_check("andre", request_id, f"{family}/proposals", body)
            if cached:
                return cached
            now = self._now()
            pid = "cn-prop-" + _b32(f"andre|{family}|{request_id}", 24)
            if family == "rules":
                kind = {"new": "rule_new", "amend": "rule_amend", "retire": "rule_retire",
                        "counsel_memo": "counsel_memo"}[body["kind"]]
                p = R.build_rule_proposal(pid, kind, body.get("target_id"), body.get("rule"), body.get("memo"),
                                          self.current, self.seed_rules, now)
            else:
                kind = {"new": "template_new", "amend": "template_amend", "retire": "template_retire"}[body["kind"]]
                p = R.build_template_proposal(pid, kind, body.get("target_id"), body.get("template"), self.current, now)
            events = [self._record(derived_id("prop", pid, p["content_sha256"]), "rules_proposal_created", "andre",
                                   f"proposal:{pid}", {"proposal_id": pid, "kind": kind, "target_id": p["target_id"],
                                                       "content_sha256": p["content_sha256"],
                                                       "weakening": p["weakening"]},
                                   f"Rules proposal created: {kind} {p['target_id'] or ''}")]
            events += self._injection(f"proposal:{pid}", {"rule": body.get("rule"), "memo": body.get("memo")}, f"proposal:{pid}",
                                      "andre")
            self._commit("proposal", p, events)
            return self._idem_store(key, h, {"proposal": p, "ledger_event_ids": events})

    def decide(self, request_id: str, decisions: list[dict]) -> dict:
        """Andre only. Atomic: all approvals build one new version, or nothing applies."""
        self.ensure_seed_proposal()
        with self.lock:
            key, h, cached = self._idem_check("andre", request_id, "decisions", decisions)
            if cached:
                return cached
            ids = [d["proposal_id"] for d in decisions]
            if len(set(ids)) != len(ids):
                raise Invalid("a proposal may appear only once per decision call")
            chosen = []
            for d in decisions:
                p = self.proposals.get(d["proposal_id"])
                if p is None:
                    raise NotFound(f"no proposal {d['proposal_id']}")
                if p["status"] != "open":
                    raise Conflict(f"proposal {d['proposal_id']} is already {p['status']}")
                if d["content_sha256"] != p["content_sha256"]:
                    raise Conflict(f"proposal {d['proposal_id']} changed since you read it (content_sha256 mismatch); "
                                   "nothing was applied")
                chosen.append((d, p))
            approvals = [p for d, p in chosen if d["decision"] == "approve"]
            now = self._now()
            recheck: dict[str, dict] = {}
            new_rules = new_templates = None
            if approvals:
                new_rules, new_templates = R.apply(self.current, approvals, recheck)
            unacked = [recheck.get(p["proposal_id"]) or {"proposal_id": p["proposal_id"],
                                                          "weakening_reasons": p.get("weakening_reasons") or []}
                       for d, p in chosen if d["decision"] == "approve"
                       and R.needs_acknowledgment(p, recheck.get(p["proposal_id"]))
                       and d.get("acknowledge_weakening") is not True]
            if unacked:
                why = "; ".join(f"{u['proposal_id']}: {', '.join(u['weakening_reasons']) or 'weakening changed since drafted'}"
                                for u in unacked)
                raise Invalid(f"approval weakens the rules as they stand now ({why}); approving needs "
                              "acknowledge_weakening: true after reviewing the diff. Nothing was applied",
                              recheck=unacked[:20])
            events: list[str] = []
            for d, p in chosen:
                t = "rules_proposal_approved" if d["decision"] == "approve" else "rules_proposal_rejected"
                events.append(self._record(derived_id("pd", p["proposal_id"], p["content_sha256"], d["decision"]), t,
                                           "andre", f"proposal:{p['proposal_id']}",
                                           {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"],
                                            "decision": d["decision"], "note_sha256": _sha(d.get("note")),
                                            "weakening": bool(p.get("weakening")) or bool(
                                                (recheck.get(p["proposal_id"]) or {}).get("weakening_reasons")),
                                            "acknowledged_weakening": d.get("acknowledge_weakening") is True},
                                           f"Andre {d['decision']}d proposal {p['proposal_id']} ({p['kind']})"))
            version_meta = None
            publish = None
            if approvals:
                n = (self.version_number or 0) + 1
                prev = self.current.version_sha256 if self.current else None
                csha = R.content_sha256(new_rules, new_templates)
                meta = {"version": n, "created_at": iso(now), "approved_by": "andre",
                        "proposal_ids": [p["proposal_id"] for p in approvals], "content_sha256": csha,
                        "prev_version_sha256": prev}
                epoch = self.log.epoch or "0" * 16
                vid = i10_evidence_audit.version_event_id(epoch, n, csha, prev)
                vargs = (vid, "rules_version_published", "andre", f"rules:v{n}",
                         {k: meta[k] for k in ("version", "content_sha256", "prev_version_sha256", "proposal_ids")},
                         f"Rule version {n} published ({len(new_rules)} rules, {len(new_templates)} templates)")
                publish = lambda: self._record(*vargs, raw=True)  # noqa: E731 - after the line's anchor (N15-1)
                events.append(vid)
                version_meta = {**meta, "rules": new_rules, "templates": new_templates}
                if any(p["kind"].startswith("template_") for p in approvals):
                    events.append(derived_id("tvp", vid))
                    tv_args = (events[-1], "template_version_published", "andre", f"rules:v{n}",
                               {"version": n, "templates": sorted(p["target_id"] for p in approvals
                                                                  if p["kind"].startswith("template_"))},
                               f"Template version(s) published in rule version {n}")
                    inner = publish
                    publish = lambda: (inner(), self._record(*tv_args, raw=True))  # noqa: E731
            record = {"request_id": request_id, "decided_at": iso(now),
                      "decisions": [{"proposal_id": d["proposal_id"], "decision": d["decision"], "note": d.get("note"),
                                     "acknowledged_weakening": d.get("acknowledge_weakening") is True}
                                    for d, _ in chosen],
                      "version": version_meta}
            self._commit("decision", record, events, after_anchor=publish)
            resp = {"decided": len(chosen), "approved": len(approvals), "rules_version": self.version_number,
                    "content_sha256": self.current.content_sha256 if self.current else None, "ledger_event_ids": events}
            return self._idem_store(key, h, resp)

    # ================================================================== comms (i06)

    def _window(self) -> str:
        return self.p("CN-15", "quiet_window")

    def _queue(self, op: str, template_id: str, variables: dict, *, clipper: Optional[dict] = None,
               opt_in: Optional[dict] = None, subject_refs: tuple = (), n: int = 0) -> tuple[Optional[dict], list[str], str]:
        """Build ONE message record (not committed): (record | None, ledger event ids, why). Every message comes from
        the approved template version in force (CN-15); the send time honours the recipient-local quiet window."""
        v = self.current
        tpl = v.template(template_id) if v else None
        if tpl is None:
            return None, [], f"template {template_id} is not in force"
        vars_ok = T.check_variables(tpl, variables)
        now = self._now()
        if clipper is not None:
            if tpl["purpose"] == "commercial":
                return None, [], "commercial templates are never sent to members from here"
            if clipper["status"] in ("offboarded",) or (clipper["status"] in ("offboarding", "banned", "refused")
                                                        and template_id not in MEMBER_EXIT_TEMPLATES + ("admission_decision",)):
                return None, [], f"clipper is {clipper['status']}: only exit notices are sent"
            channel, why = i06_comms.channel_for(tpl, clipper, bool(self.counsel_open("email_ca_clippers")))
            tz = clipper.get("time_zone")
            recipient = {"clipper_id": clipper["clipper_id"], "recipient_hmac": clipper["email_hmac"],
                         "opt_in_record_id": None}
            consent = "member_relationship"
        else:
            if opt_in is None or tpl["purpose"] != "commercial":
                return None, [], "recruiting messages need an opt-in record and a commercial template"
            channel, why, tz = "email", "email (opt-in)", opt_in["time_zone"]
            recipient = {"clipper_id": None, "recipient_hmac": opt_in["email_hmac"],
                         "opt_in_record_id": opt_in["opt_in_record_id"]}
            consent = opt_in["opt_in_record_id"]
        if channel is None:
            return None, [], why
        send_after = now if (channel == "in_app" and not tz) else i06_comms.send_after(now, tz, self._window())
        status = "queued" if send_after <= now else "deferred"
        who = recipient["clipper_id"] or recipient["opt_in_record_id"]
        mid = "cn-msg-" + _b32(f"{op}|{who}|{template_id}|{n}")
        cites = sorted(set((variables.get("rule_ids") or []) + ["CN-15", "CN-26"]
                           + (["CN-16"] if "automation_disclosure" in tpl["variables"].values() else [])
                           + (["CN-08"] if consent != "member_relationship" else [])))
        rec = {"message_id": mid, **recipient, "template_id": template_id, "template_version": tpl["version"],
               "rules_version": v.version, "channel": channel, "channel_reason": why, "purpose": tpl["purpose"],
               "consent_basis": consent, "variables": vars_ok, "subject_refs": list(subject_refs),
               "rule_ids_cited": cites, "queued_at": iso(now), "send_after": iso(send_after), "sent_at": None,
               "delivery_status": status, "attempts": 0, "rendered_sha256": None, "provider_ref": None,
               "last_reason": None}
        events = [self._record(derived_id("mq", mid), "message_queued", i06_comms.ACTOR, mid,
                               {"message_id": mid, "template_id": template_id, "template_version": tpl["version"],
                                "channel": channel, "purpose": tpl["purpose"], "send_after": rec["send_after"],
                                "rule_ids": cites}, f"Message queued: {template_id} v{tpl['version']} via {channel}")]
        if status == "deferred":
            events.append(self._record(derived_id("mdq", mid), "message_deferred_quiet_hours", i06_comms.ACTOR, mid,
                                       {"message_id": mid, "send_after": rec["send_after"]},
                                       "Message deferred to the recipient's quiet-window opening (CN-15)"))
        return rec, events, "queued" if status == "queued" else "deferred for quiet hours"

    def _filled(self, m: dict) -> tuple[dict, Optional[str]]:
        """Service-filled template values and the recipient address (None = unavailable)."""
        filled = {"automation_disclosure": self.p("CN-16", "disclosure_text")}
        if m["clipper_id"]:
            contact = self.contacts.get(f"clipper:{m['clipper_id']}")
            if contact:
                filled["display_name"] = contact["display_name"]
            if m["channel"] == "in_app":
                return filled, m["clipper_id"]
            return filled, (contact or {}).get("email")
        contact = self.contacts.get(f"optin:{m['opt_in_record_id']}")
        if self.config.postal_address:
            filled["postal_address"] = self.config.postal_address
        if self.config.opt_out_url:
            filled["opt_out_link"] = f"{self.config.opt_out_url}?ref={m['opt_in_record_id']}"
        return filled, (contact or {}).get("email")

    def _deliver(self, message_ids: list[str]) -> dict[str, str]:
        """Attempt delivery of due messages, each its own recorded step. A failure to RECORD stops here and leaves
        the message queued (flush retries); it never raises into the operation that queued it."""
        out: dict[str, str] = {}
        for mid in message_ids:
            m = self.st["messages"].get(mid)
            if m is None or m["delivery_status"] not in ("queued", "deferred", "not_delivered"):
                continue
            try:
                out[mid] = self._deliver_one(m)
            except Unavailable:
                out[mid] = "not_recorded"
                break
        return out

    def _deliver_one(self, m: dict) -> str:
        now = self._now()
        mid = m["message_id"]
        if parse_iso(m["send_after"]) > now:
            return m["delivery_status"]
        tz = None
        if m["clipper_id"]:
            tz = (self.st["clippers"].get(m["clipper_id"]) or {}).get("time_zone")
        else:
            tz = (self.st["opt_ins"].get(m["opt_in_record_id"]) or {}).get("time_zone")
        if tz and not (m["channel"] == "in_app" and not tz) and not i06_comms.inside_window(now, tz, self._window()):
            nxt = i06_comms.send_after(now, tz, self._window())
            ev = self._record(derived_id("mdq", mid, iso(nxt)), "message_deferred_quiet_hours", i06_comms.ACTOR, mid,
                              {"message_id": mid, "send_after": iso(nxt)}, "Message deferred: outside the quiet window")
            self._batch([("messages", mid, {**m, "delivery_status": "deferred", "send_after": iso(nxt)})], [ev],
                        "message deferred")
            return "deferred"
        if m["opt_in_record_id"]:
            o = self.st["opt_ins"].get(m["opt_in_record_id"])
            if o is None or o["status"] != "active" or self._suppressed(o["email_hmac"]):
                ev = self._record(derived_id("rsr", mid), "recruiting_send_refused", i01_recruiting.ACTOR, mid,
                                  {"message_id": mid, "code": "OPTED_OUT", "rule_id": "CN-08"},
                                  "Queued recruiting message cancelled: opt-in withdrawn (CN-08)")
                self._batch([("messages", mid, {**m, "delivery_status": "cancelled", "last_reason": "opted out"})], [ev],
                            "message cancelled")
                return "cancelled"
        tpl = None
        for v in reversed(self.versions):
            t = v.template(m["template_id"])
            if t is not None and t["version"] == m["template_version"]:
                tpl = t
                break
        filled, recipient = self._filled(m)
        body, reason = None, None
        if tpl is None:
            reason = "template version no longer available"
        elif recipient is None:
            reason = "recipient contact unavailable"
        else:
            try:
                body = T.render(tpl, m["variables"], filled, m["channel"])
            except Invalid as exc:
                reason = f"render refused: {exc.reason}"[:200]
        events: list[str] = []
        attempt = m["attempts"] + 1
        ans = SendAnswer(False, reason=reason or "")
        if body is not None:
            ports = PortCalls(self, f"{mid}|{attempt}", i06_comms.ACTOR, mid, events)
            prov = self.ports.messaging
            ans = ports.call("messaging_provider", "send", (mid, m["channel"], attempt),
                             lambda: prov.send(mid, m["channel"], recipient, body), SendAnswer(False, reason="error"))
        rendered = hashlib.sha256(body.encode("utf-8")).hexdigest() if body is not None else None
        if ans.delivered:
            events.append(self._record(derived_id("ms", mid, attempt), "message_sent", i06_comms.ACTOR, mid,
                                       {"message_id": mid, "rendered_sha256": rendered, "attempt": attempt},
                                       f"Message sent: {m['template_id']}"))
            new = {**m, "delivery_status": "sent", "sent_at": iso(now), "attempts": attempt, "rendered_sha256": rendered,
                   "provider_ref": (ans.provider_ref or "")[:128] or None, "variables": None}
        else:
            events.append(self._record(derived_id("mnd", mid, attempt), "message_not_delivered", i06_comms.ACTOR, mid,
                                       {"message_id": mid, "attempt": attempt, "rendered_sha256": rendered},
                                       f"Message not delivered: {m['template_id']}"))
            final = attempt >= MAX_DELIVERY_ATTEMPTS
            new = {**m, "delivery_status": "failed" if final else "not_delivered", "attempts": attempt,
                   "rendered_sha256": rendered, "last_reason": (ans.reason or reason or "not delivered")[:200],
                   "variables": None if final else m["variables"]}
        self._batch([("messages", mid, new)], events, "message delivery attempt")
        return new["delivery_status"]

    def flush_messages(self, request_id: str) -> dict:
        with self.lock:
            key, h, cached = self._idem_check("scheduler", request_id, "messages/flush", {})
            if cached:
                return cached
            if self.current is None:
                return self._idem_store(key, h, {"ran": False, "unmet": [rules_not_in_force()]})
            now = self._now()
            due = sorted((m for m in self.st["messages"].values()
                          if m["delivery_status"] in ("queued", "deferred", "not_delivered")
                          and parse_iso(m["send_after"]) <= now), key=lambda m: (m["send_after"], m["message_id"]))
            res = self._deliver([m["message_id"] for m in due])
            counts: dict[str, int] = {}
            for st in res.values():
                counts[st] = counts.get(st, 0) + 1
            return self._idem_store(key, h, {"ran": True, "attempted": len(res), "outcomes": counts})

    def messages_for(self, clipper_id: str) -> list[dict]:
        with self.lock:
            self._clipper(clipper_id)
            return [{k: v for k, v in m.items() if k != "variables"}
                    for m in sorted(self.st["messages"].values(), key=lambda x: (x["queued_at"], x["message_id"]))
                    if m["clipper_id"] == clipper_id]

    # ================================================================== recruiting (i01)

    def _suppressed(self, ehmac: str) -> bool:
        rec = self.st["suppression"].get(ehmac)
        return rec is not None and not rec.get("lifted_by")

    def opt_in(self, request_id: str, body: dict) -> dict:
        with self.lock:
            safe = {**body, "email": self.hmac_email(body["email"])}
            key, h, cached = self._idem_check("hub", request_id, "opt-ins", safe)
            if cached:
                return cached
            if not i06_comms.valid_time_zone(body["time_zone"]):
                raise Invalid("time_zone is not a known IANA time zone")
            oid = "cn-opt-" + _b32(f"hub|{request_id}")
            ehmac = self.hmac_email(body["email"])
            ckey = f"optin:{oid}"
            csha = self._contact_put(ckey, {"email": normalize_email(body["email"])})
            try:
                rec = {"opt_in_record_id": oid, "channel": "email_opt_in", "email_hmac": ehmac,
                       "recipient_country": body["recipient_country"], "time_zone": body["time_zone"],
                       "consent_text_sha256": body["consent_text_sha256"], "source_form_id": body["source_form_id"],
                       "captured_at": body["captured_at"], "recorded_at": iso(self._now()), "status": "active",
                       "contact_sha256": csha}
                ev = self._record(derived_id("oin", oid), "opt_in_recorded", i01_recruiting.ACTOR, oid,
                                  {"opt_in_record_id": oid, "email_hmac": ehmac, "country": body["recipient_country"],
                                   "consent_text_sha256": body["consent_text_sha256"]}, "Recruiting opt-in recorded")
                puts = [("opt_ins", oid, rec)]
                if self._suppressed(ehmac):
                    # a new explicit opt-in after an opt-out is new consent
                    puts.append(("suppression", ehmac, {**self.st["suppression"][ehmac], "lifted_by": oid}))
                self._batch(puts, [ev], "opt-in")
            except BaseException:
                self._contact_rollback(ckey)
                raise
            return self._idem_store(key, h, {"opt_in_record_id": oid, "status": "active", "ledger_event_ids": [ev]})

    def opt_out(self, request_id: str, body: dict) -> dict:
        """Honoured immediately (CAN-SPAM allows 10 business days; CN honours at once): the email HMAC is suppressed,
        every opt-in of it is withdrawn and its contact data deleted."""
        with self.lock:
            ehmac = self.hmac_email(body["email"])
            key, h, cached = self._idem_check("hub", request_id, "opt-outs", {"email": ehmac})
            if cached:
                return cached
            now = self._now()
            withdrawn = [o for o in self.st["opt_ins"].values() if o["email_hmac"] == ehmac and o["status"] == "active"]
            ev = self._record(derived_id("oout", ehmac, request_id), "opt_out_recorded", i01_recruiting.ACTOR,
                              "opt-out", {"email_hmac": ehmac, "opt_ins_withdrawn": len(withdrawn)},
                              "Recruiting opt-out honoured immediately")
            puts = [("suppression", ehmac, {"email_hmac": ehmac, "at": iso(now), "honored_by": iso(now)})]
            puts += [("opt_ins", o["opt_in_record_id"], {**o, "status": "withdrawn", "contact_sha256": None,
                                                        "withdrawn_at": iso(now)}) for o in withdrawn]
            self._batch(puts, [ev], "opt-out")
            for o in withdrawn:
                try:
                    self.contacts.delete(f"optin:{o['opt_in_record_id']}")
                except ContactStoreError:
                    pass  # no longer expected by the log: purged as an orphan at the next start
            return self._idem_store(key, h, {"email_hmac": ehmac, "opt_ins_withdrawn": len(withdrawn),
                                             "honored_at": iso(now), "ledger_event_ids": [ev]})

    def create_recruiting(self, request_id: str, body: dict) -> dict:
        with self.lock:
            recips = []
            for r in body["recipients"]:
                if looks_like_phone(r):
                    recips.append({"kind": "phone", "hmac": self.hmac_email(r)})
                elif is_email(r):
                    recips.append({"kind": "email", "hmac": self.hmac_email(r)})
                else:
                    recips.append({"kind": "invalid", "hmac": self.hmac_email(r)})
            safe = {**body, "recipients": recips}
            key, h, cached = self._idem_check("andre", request_id, "recruiting/campaigns", safe)
            if cached:
                return cached
            if self.current is None:
                raise Conflict("no rule version is in force (CN-00)", unmet=[rules_not_in_force()])
            if body["channel"] == "email_opt_in" and not recips:
                raise Invalid("an email recruiting campaign names its recipients (each must have an opt-in record)")
            if body["channel"] == "discord_server_post" and not body.get("discord_server_ref"):
                raise Invalid("a Discord server post names discord_server_ref")
            rid = "cn-rcr-" + _b32(f"andre|{request_id}")
            rec = {"recruit_id": rid, "channel": body["channel"], "template_id": body["template_id"],
                   "recipients": recips, "discord_server_ref": body.get("discord_server_ref"),
                   "created_at": iso(self._now()), "sends": []}
            ev = self._record(derived_id("rcr", rid), "recruiting_campaign_defined", "andre", rid,
                              {"recruit_id": rid, "channel": body["channel"], "recipients": len(recips),
                               "recipients_sha256": _sha(recips)}, "Recruiting campaign defined by Andre")
            self._batch([("recruits", rid, rec)], [ev], "recruiting campaign")
            return self._idem_store(key, h, {"recruit_id": rid, "recipients": len(recips), "ledger_event_ids": [ev]})

    def send_recruiting(self, request_id: str, recruit_id: str) -> dict:
        with self.lock:
            key, h, cached = self._idem_check("scheduler", request_id, f"recruiting/{recruit_id}/send", {})
            if cached:
                return cached
            rc = self.st["recruits"].get(recruit_id)
            if rc is None:
                raise NotFound("no such recruiting campaign")
            op = f"scheduler|{request_id}|{recruit_id}"
            events: list[str] = []
            refused: list[dict] = []
            puts: list[tuple] = []
            queued: list[str] = []

            def refuse_all(u: dict) -> dict:
                ev = self._record(derived_id("rsr", op, "all"), "recruiting_send_refused", i01_recruiting.ACTOR,
                                  recruit_id, {"code": u["code"], "rule_id": u["rule_id"], "recipients": len(rc["recipients"])},
                                  f"Recruiting send refused: {u['code']} ({u['rule_id']})")
                self._batch([("recruits", recruit_id, {**rc, "sends": rc["sends"] + [{"op": _sha(op), "refused_all": u}]})],
                            [ev], "recruiting refused")
                return self._idem_store(key, h, {"recruit_id": recruit_id, "sent": 0, "queued": 0, "refused": [u],
                                                 "ledger_event_ids": [ev]})
            if self.current is None:
                return refuse_all(rules_not_in_force())
            chp = i01_recruiting.channel_problem(rc["channel"], [c for c in self.config.channels
                                                                  if c in self.p("CN-09", "channels")])
            if chp:
                return refuse_all(chp)
            if rc["channel"] == "discord_server_post":
                return refuse_all(item("COMPLIANCE_ASSET_TYPE_MISSING", "CN-08", "Compliance's publish gate has no asset "
                                       "type for a Discord server post; recruiting posts wait until it has one"))
            if not (self.config.postal_address and self.config.opt_out_url):
                return refuse_all(item("CANSPAM_ELEMENTS_MISSING", "CN-08", "CN_POSTAL_ADDRESS and CN_OPT_OUT_URL must be "
                                       "configured: every recruiting email carries a postal address and a working opt-out"))
            tpl = self.current.template(rc["template_id"])
            if tpl is None:
                return refuse_all(item("TEMPLATE_NOT_IN_FORCE", "CN-15", "the recruiting template is not in force"))
            # classify every recipient BEFORE any provider call (A3)
            refuse_c = self.p("CN-08", "refuse_recipient_countries")
            eligible = []
            for i, r in enumerate(rc["recipients"]):
                oi = None
                if r["kind"] == "email":
                    cands = [o for o in self.st["opt_ins"].values() if o["email_hmac"] == r["hmac"] and o["status"] == "active"]
                    oi = max(cands, key=lambda o: o["recorded_at"]) if cands else None
                u = i01_recruiting.recipient_problem(r["kind"], oi, self._suppressed(r["hmac"]), refuse_c)
                if u:
                    events.append(self._record(derived_id("rsr", op, i), "recruiting_send_refused", i01_recruiting.ACTOR,
                                               recruit_id, {"recipient_hmac": r["hmac"], "code": u["code"],
                                                            "rule_id": u["rule_id"]},
                                               f"Recruiting recipient refused: {u['code']} ({u['rule_id']})"))
                    refused.append({**u, "recipient_hmac": r["hmac"]})
                else:
                    eligible.append(oi)
            countries = sorted({o["recipient_country"] for o in eligible})
            ruling = None
            if eligible:
                facts = self._email_campaign_facts(tpl, countries)
                ports = PortCalls(self, op, i01_recruiting.ACTOR, recruit_id, events)
                cmp_ = self.ports.compliance
                sid = f"{recruit_id}-{_b32(op, 10)}"
                ruling = ports.call("compliance_38", "review_email_campaign", (sid, _sha(facts)),
                                    lambda: cmp_.review_email_campaign(derived_id("rq", op), sid, facts),
                                    ComplianceRuling(False))
                if not ruling.available or not ruling.allowed:
                    u = (item("COMPLIANCE_BLOCKED", "CN-08", "Compliance's publish gate did not allow this email "
                              "campaign: " + "; ".join(ruling.unmet_lines)[:200], "compliance_38", ruling.ruling_id)
                         if ruling.available else unavailable("compliance_38", "CN-08", "email_campaign publish ruling"))
                    for i, o in enumerate(eligible):
                        events.append(self._record(derived_id("rsr", op, "c", i), "recruiting_send_refused",
                                                   i01_recruiting.ACTOR, recruit_id,
                                                   {"recipient_hmac": o["email_hmac"], "code": u["code"],
                                                    "rule_id": u["rule_id"]},
                                                   f"Recruiting recipient refused: {u['code']}"))
                        refused.append({**u, "recipient_hmac": o["email_hmac"]})
                    eligible = []
            for i, o in enumerate(eligible):
                m, evs, _why = self._queue(op, "recruiting_invite", {}, opt_in=o, n=i)
                if m is not None:
                    puts.append(("messages", m["message_id"], m))
                    events += evs
                    queued.append(m["message_id"])
            send = {"op": _sha(op), "at": iso(self._now()), "queued": len(queued), "refused": len(refused),
                    "compliance_ruling_id": ruling.ruling_id if ruling else None}
            if queued:
                events.append(self._record(derived_id("rsn", op), "recruiting_sent", i01_recruiting.ACTOR, recruit_id,
                                           {"queued": len(queued), "refused": len(refused),
                                            "compliance_ruling_id": send["compliance_ruling_id"]},
                                           f"Recruiting send: {len(queued)} queued, {len(refused)} refused"))
            puts.append(("recruits", recruit_id, {**rc, "sends": rc["sends"] + [send]}))
            self._batch(puts, events, "recruiting send")
            res = self._deliver(queued)
            return self._idem_store(key, h, {"recruit_id": recruit_id, "queued": len(queued),
                                             "sent": sum(1 for s in res.values() if s == "sent"),
                                             "refused": refused, "delivery": res, "ledger_event_ids": events})

    def _email_campaign_facts(self, tpl: dict, countries: list[str]) -> dict:
        flags = {k: False for k in ("paid_or_endorsement", "synthetic_performer", "ai_manipulated_media",
                                    "real_person_likeness", "implied_affiliation", "personal_use_claim", "claims_present",
                                    "health_or_earnings_claim", "child_directed", "child_access_likely",
                                    "political_content", "audience_data_sale", "uses_tracking", "collects_pii",
                                    "consumer_ecommerce")}
        return {"brief_id": f"cn-{tpl['template_id']}-v{tpl['version']}", "asset_type": "email_campaign",
                "asset_content_sha256": hashlib.sha256(tpl["body"].encode("utf-8")).hexdigest(), "client_id": "zbc",
                "target_jurisdictions": countries, "platforms": ["email"], "flags": flags, "claim_file_id": None,
                "claim_file_approved": False,
                "canspam": {"ad_identified": True, "postal_address_present": True, "opt_out_mechanism_present": True,
                            "opt_out_honor_business_days": 0, "sender_vendor_monitored": False},
                "recipient_countries": countries, "recipients_cold": False}

    # ================================================================== applications and relays

    def _clipper(self, clipper_id: str) -> dict:
        c = self.st["clippers"].get(clipper_id)
        if c is None:
            raise NotFound("no such clipper")
        return c

    def _open_application(self, clipper_id: str) -> Optional[dict]:
        apps = [a for a in self.st["applications"].values() if a["clipper_id"] == clipper_id and a["status"] == "open"]
        return max(apps, key=lambda a: a["submitted_at"]) if apps else None

    def apply(self, principal: str, request_id: str, body: dict) -> dict:
        """Application intake (hub, onboarding). Creates the ONE clipper identity for this person (spec §0.3: CN is the
        system of record) — or, for an email CN already holds, a separate applicant record that admission refuses as a
        duplicate (CN-03) while the first is unaffected. Client text is data: the statement is stored only as a hash;
        instruction-like text in it is recorded and ignored."""
        with self.lock:
            ehmac = self.hmac_email(body["email"])
            safe = {**body, "email": ehmac, "display_name": R.sha_text(body["display_name"]),
                    "statement": R.sha_text(body["statement"]) if body.get("statement") else None}
            key, h, cached = self._idem_check(principal, request_id, "applications", safe)
            if cached:
                return cached
            if body["declared_18_plus"] is not True:
                raise Invalid("Z Best Clips is 18+ only (CN-01): the application is refused and nothing is stored",
                              rule_id="CN-01")
            if body.get("time_zone") and not i06_comms.valid_time_zone(body["time_zone"]):
                raise Invalid("time_zone is not a known IANA time zone")
            for c in self.st["clippers"].values():
                if c["email_hmac"] == ehmac and c.get("minor"):
                    raise Conflict("this identity was refused under CN-01 (V&I attested a minor); CN has no "
                                   "re-application path", rule_id="CN-01")
            if body["channel"] == "email_opt_in":
                o = self.st["opt_ins"].get(body["opt_in_record_id"])
                if o is None or o["email_hmac"] != ehmac or o["status"] != "active":
                    raise Invalid("opt_in_record_id does not name an active opt-in record of this email (CN-08)")
            if body["channel"] == "referral":
                ref = self.st["clippers"].get(body["referrer_clipper_id"])
                if ref is None or ref["status"] != "active":
                    raise Invalid("referrer_clipper_id is not an active clipper (referral links come from active clippers)")
            existing = [c for c in self.st["clippers"].values() if c["email_hmac"] == ehmac
                        and (c["status"] not in ("offboarded",) or c.get("ban_decision_id"))]
            banned = [c["clipper_id"] for c in existing if c.get("ban_decision_id")]
            # ADR 0008 choice 6: a re-application while the identity is still an applicant updates THAT identity;
            # an email CN already holds under an admitted / suspended / banned / refused identity makes a separate
            # applicant record that admission refuses as a duplicate (CN-03), leaving the first untouched.
            same_open = [c for c in existing if c["status"] == "applicant"]
            existing = [c for c in existing if c["status"] != "applicant"]
            now = self._now()
            op = f"{principal}|{request_id}"
            app_id = "cn-app-" + _b32(op)
            clipper = None
            if same_open:
                clipper = {**same_open[0], "declared_country": body["declared_country"],
                           "declared_region": body.get("declared_region"),
                           "jurisdiction_attested": body["jurisdiction_attested"], "time_zone": body.get("time_zone")}
            puts: list[tuple] = []
            ckey = None
            if clipper is None:
                cid = "cn-clp-" + _b32(f"clp|{op}")
                ckey = f"clipper:{cid}"
                csha = self._contact_put(ckey, {"email": normalize_email(body["email"]),
                                                "display_name": body["display_name"], "handles": {}})
                clipper = {"clipper_id": cid, "status": "applicant", "display_name_sha256": R.sha_text(body["display_name"]),
                           "email_hmac": ehmac, "contact_sha256": csha, "declared_country": body["declared_country"],
                           "declared_region": body.get("declared_region"),
                           "jurisdiction_attested": body["jurisdiction_attested"], "time_zone": body.get("time_zone"),
                           "jurisdiction_ruling": None, "connected_accounts": [], "tier": None,
                           "age_status": None, "admission_id": None, "admitted_at": None, "created_at": iso(now),
                           "suspensions": [], "nominated": False, "minor": False, "intake_principal": principal,
                           "duplicate_of": existing[0]["clipper_id"] if existing else None,
                           "banned_identity_of": banned[0] if banned else None}
            try:
                events: list[str] = []
                app = {"application_id": app_id, "clipper_id": clipper["clipper_id"], "channel": body["channel"],
                       "referrer_clipper_id": body.get("referrer_clipper_id"),
                       "opt_in_record_id": body.get("opt_in_record_id"), "declared_18_plus": True,
                       "sag_aftra_member": body["sag_aftra_member"], "submitted_at": iso(now), "status": "open",
                       "decision_items": [], "statement_sha256": R.sha_text(body["statement"]) if body.get("statement") else None,
                       "statement_chars": len(body.get("statement") or ""), "principal": principal}
                events.append(self._record(derived_id("app", app_id), "application_received", i02_admission.ACTOR,
                                           clipper["clipper_id"], {"application_id": app_id, "clipper_id": clipper["clipper_id"],
                                                                   "channel": body["channel"], "email_hmac": ehmac,
                                                                   "country": body["declared_country"],
                                                                   "duplicate_email": bool(existing)},
                                           f"Application received via {body['channel']}"))
                events += self._injection(app_id, {"display_name": body["display_name"],
                                                   "statement": body.get("statement")}, clipper["clipper_id"],
                                          i02_admission.ACTOR)
                for a in self.st["applications"].values():
                    if a["clipper_id"] == clipper["clipper_id"] and a["status"] == "open":
                        puts.append(("applications", a["application_id"], {**a, "status": "withdrawn"}))
                        events.append(self._record(derived_id("apw", a["application_id"]), "application_withdrawn",
                                                   i02_admission.ACTOR, clipper["clipper_id"],
                                                   {"application_id": a["application_id"], "replaced_by": app_id},
                                                   "Earlier open application replaced by a new one"))
                puts += [("clippers", clipper["clipper_id"], clipper), ("applications", app_id, app)]
                msg = None
                if self.current is not None:
                    msg, mev, _ = self._queue(op, "application_received", {"application_id": app_id}, clipper=clipper)
                    if msg is not None:
                        puts.append(("messages", msg["message_id"], msg))
                        events += mev
                self._batch(puts, events, "application")
            except BaseException:
                if ckey:
                    self._contact_rollback(ckey)
                raise
            if msg is not None:
                self._deliver([msg["message_id"]])
            return self._idem_store(key, h, {"application_id": app_id, "clipper_id": clipper["clipper_id"],
                                             "status": "open", "injection_text_ignored": any(
                                                 e.startswith("cn-inj-") for e in events), "ledger_event_ids": events})

    def get_application(self, application_id: str) -> dict:
        with self.lock:
            a = self.st["applications"].get(application_id)
            if a is None:
                raise NotFound("no such application")
            return {k: v for k, v in a.items() if k != "principal"}

    def _live_clipper(self, clipper_id: str, allow=("applicant", "active", "suspended")) -> dict:
        c = self._clipper(clipper_id)
        if c["status"] not in allow:
            raise Conflict(f"clipper is {c['status']}: this action is not available", rule_id="CN-21")
        return c

    def connection_start(self, request_id: str, clipper_id: str, body: dict) -> dict:
        """Relay to V&I (the OAuth flow is V&I's). The optional display handle goes to the contact store only."""
        with self.lock:
            safe = {**body, "handle": R.sha_text(body["handle"]) if body.get("handle") else None}
            key, h, cached = self._idem_check("hub", request_id, f"connections/start/{clipper_id}", safe)
            if cached:
                return cached
            c = self._live_clipper(clipper_id)
            op = f"hub|{request_id}|start"
            events: list[str] = []
            ports = PortCalls(self, op, i02_admission.ACTOR, clipper_id, events)
            vi = self.ports.vi
            ans = ports.call("verification_integrity", "connection_start", (clipper_id, body["platform"]),
                             lambda: vi.connection_start(derived_id("rq", op), clipper_id, body["platform"],
                                                         body["redirect_uri"]), StartAnswer(False))
            puts = []
            hkey = None
            if ans.available and ans.started and ans.connection_id:
                acct = {"platform": body["platform"], "vi_connection_id": ans.connection_id,
                        "handle_sha256": R.sha_text(body["handle"]) if body.get("handle") else None,
                        "handle_contact_sha256": None, "status": "pending"}
                if body.get("handle"):
                    # the display handle is contact data: its own key in the contact store (deleted at exit)
                    hkey = f"handle:{clipper_id}:{ans.connection_id}"
                    acct["handle_contact_sha256"] = self._contact_put(hkey, {"handle": body["handle"]})
                accts = [a for a in c["connected_accounts"] if a["vi_connection_id"] != ans.connection_id] + [acct]
                puts.append(("clippers", clipper_id, {**c, "connected_accounts": accts}))
            try:
                events.append(self._record(derived_id("crs", op), "connection_relayed", i02_admission.ACTOR, clipper_id,
                                           {"action": "start", "platform": body["platform"], "available": ans.available,
                                            "started": ans.started, "connection_id": ans.connection_id},
                                           "Connection start relayed to V&I"))
                self._batch(puts, events, "connection start")
            except BaseException:
                if hkey:
                    self._contact_rollback(hkey)
                raise
            return self._idem_store(key, h, {"available": ans.available, "started": ans.started,
                                             "connection_id": ans.connection_id,
                                             "authorization_url": ans.authorization_url,
                                             "state_expires_at": ans.state_expires_at, "reasons": list(ans.reasons),
                                             "ledger_event_ids": events})

    def connection_complete(self, request_id: str, clipper_id: str, body: dict) -> dict:
        """Relay to V&I. The OAuth ``code`` is passed through and NEVER stored, logged, hashed into the ledger or
        exported (A9): the crossing payload hashes only (clipper_id); the idempotency hash uses a per-process key."""
        with self.lock:
            safe = {"state": self._volatile_hash(body["state"]), "code": self._volatile_hash(body["code"])}
            key, h, cached = self._idem_check("hub", request_id, f"connections/complete/{clipper_id}", safe)
            if cached:
                return cached
            c = self._live_clipper(clipper_id)
            op = f"hub|{request_id}|complete"
            events: list[str] = []
            ports = PortCalls(self, op, i02_admission.ACTOR, clipper_id, events)
            vi = self.ports.vi
            state, code = body["state"], body["code"]
            ans = ports.call("verification_integrity", "connection_complete", (clipper_id,),
                             lambda state=state, code=code: vi.connection_complete(derived_id("rq", op), state, code),
                             CompleteAnswer(False))
            del code, state     # the frame's names; the lambda held them only for the call (wave 25: bound, never free)
            puts = []
            if ans.available and ans.connection_id:
                accts = [({**a, "status": ans.status} if a["vi_connection_id"] == ans.connection_id else a)
                         for a in c["connected_accounts"]]
                puts.append(("clippers", clipper_id, {**c, "connected_accounts": accts}))
            events.append(self._record(derived_id("crc", op), "connection_relayed", i02_admission.ACTOR, clipper_id,
                                       {"action": "complete", "available": ans.available, "status": ans.status,
                                        "connection_id": ans.connection_id}, "Connection completion relayed to V&I"))
            self._batch(puts, events, "connection complete")
            return self._idem_store(key, h, {"available": ans.available, "connection_id": ans.connection_id,
                                             "status": ans.status, "reasons": list(ans.reasons),
                                             "ledger_event_ids": events})

    def age_check(self, request_id: str, clipper_id: str, body: dict) -> dict:
        """Relay to V&I ``/age/checks``: the DOB is passed through and never stored, logged or exported (A9, CN-24).
        CN keeps only the attestation id and its result; a ``minor`` result refuses the clipper at once (CN-01)."""
        with self.lock:
            safe = {**body, "dob": self._volatile_hash(body["dob"])}
            key, h, cached = self._idem_check("hub", request_id, f"age-check/{clipper_id}", safe)
            if cached:
                return cached
            c = self._live_clipper(clipper_id, allow=("applicant",))
            op = f"hub|{request_id}|age"
            events: list[str] = []
            ports = PortCalls(self, op, i02_admission.ACTOR, clipper_id, events)
            vi = self.ports.vi
            dob = body["dob"]
            ans = ports.call("verification_integrity", "age_check", (clipper_id, body["method"]),
                             lambda dob=dob: vi.age_check(derived_id("rq", op), clipper_id, dob, body["dob_field_neutral"],
                                                          body["method"], body["provider_session_ref"]), AgeAnswer(False))
            del dob     # the frame's name; the lambda held it only for the call (wave 25: bound, never free)
            age = {"vi_attestation_id": ans.attestation_id, "result": ans.status if ans.available else "unavailable",
                   "at": iso(self._now())}
            events.append(self._record(derived_id("age", op), "age_status_mirrored", i02_admission.ACTOR, clipper_id,
                                       {"available": ans.available, "result": age["result"],
                                        "vi_attestation_id": ans.attestation_id}, "Age check relayed to V&I"))
            new_c = {**c, "age_status": age}
            puts = [("clippers", clipper_id, new_c)]
            if ans.available and ans.status == "minor":
                puts, more = self._minor_refusal(new_c, op)
                events += more
            self._batch(puts, events, "age check")
            if ans.available and ans.status == "minor":
                self._try_start_offboarding(clipper_id, "minor", op)
            return self._idem_store(key, h, {"available": ans.available, "result": age["result"],
                                             "vi_attestation_id": ans.attestation_id,
                                             "status": self.st["clippers"][clipper_id]["status"], "ledger_event_ids": events})

    def _minor_refusal(self, c: dict, op: str, record_ruling: bool = True) -> tuple[list[tuple], list[str]]:
        """CN-01: V&I attests a minor -> status refused (no re-application path), open application refused. With
        ``record_ruling`` (the age-check relay) the refusal is its own admission ruling; admission itself passes
        False (its own ruling already says so)."""
        puts = [("clippers", c["clipper_id"], {**c, "status": "refused", "minor": True})]
        events: list[str] = []
        u = item("AGE_NOT_ADULT", "CN-01", "V&I attests a minor: refused, no re-application path",
                 "verification_integrity", (c.get("age_status") or {}).get("vi_attestation_id"))
        for a in self.st["applications"].values():
            if a["clipper_id"] == c["clipper_id"] and a["status"] == "open":
                puts.append(("applications", a["application_id"], {**a, "status": "refused", "decision_items": [u]}))
        if record_ruling:
            rid = derived_id("mnr", op, c["clipper_id"])
            puts.append(("admissions", rid, {"ruling_id": rid, "admission_id": rid, "clipper_id": c["clipper_id"],
                                             "admitted": False, "unmet": [u], "unmet_lines": [unmet_line(u)],
                                             "rules_version": self.version_number, "evaluated_at": iso(self._now()),
                                             "minor": True, "view": {"admission_id": rid, "unmet": [u]}}))
            events.append(self._record(rid, "admission_ruling", i02_admission.ACTOR, c["clipper_id"],
                                       {"ruling_id": rid, "admitted": False, "codes": [["CN-01", "AGE_NOT_ADULT"]],
                                        "minor": True}, "Refused: V&I attests a minor (CN-01); offboarding trigger minor",
                                       raw=True))
        return puts, events

    def accept_agreement(self, request_id: str, clipper_id: str, body: dict) -> dict:
        """B.3: an IP-free, versioned acceptance record (hashes, never the text) of the version Legal names current."""
        with self.lock:
            safe = {**body, "session_ref": R.sha_text(body["session_ref"])}
            key, h, cached = self._idem_check("hub", request_id, f"agreement/{clipper_id}", safe)
            if cached:
                return cached
            self._live_clipper(clipper_id)
            op = f"hub|{request_id}|agr"
            events: list[str] = []
            ports = PortCalls(self, op, i02_admission.ACTOR, clipper_id, events)
            legal = self.ports.legal
            cur = ports.call("legal_37", "current_version", ("clipper_agreement",),
                             lambda: legal.current_version("clipper_agreement"), DocVersionAnswer(False))
            unmet = []
            if self.current is None:
                unmet.append(rules_not_in_force())
            if body["box_ticked"] is not True:
                unmet.append(item("CLICKWRAP_NOT_TICKED", "CN-04", "acceptance needs the unticked box to be ticked by the clipper"))
            if not cur.available:
                unmet.append(unavailable("legal_37", "CN-04", "current Clipper Agreement version"))
            elif (body["version"], body["doc_sha256"]) != (cur.version, cur.doc_sha256):
                unmet.append(item("AGREEMENT_NOT_CURRENT", "CN-04", f"version {body['version']} is not Legal's current "
                                  f"version ({cur.version}) or its hash differs", "legal_37"))
            if body["presented_sha256"] != body["doc_sha256"]:
                unmet.append(item("PRESENTED_TEXT_MISMATCH", "CN-04", "the text shown is not the current document "
                                  "exactly (presented_sha256 differs from doc_sha256)"))
            if self.current is not None:
                unmet = Citer(self.current).check(unmet)
            if unmet:
                return self._idem_store(key, h, {"accepted": False, "unmet": unmet,
                                                 "unmet_lines": [unmet_line(u) for u in unmet], "ledger_event_ids": events})
            aid = "cn-agr-" + _b32(op)
            rec = {"acceptance_id": aid, "clipper_id": clipper_id, "doc_id": "clipper_agreement", "version": body["version"],
                   "doc_sha256": body["doc_sha256"], "presented_sha256": body["presented_sha256"],
                   "accepted_at": iso(self._now()), "method": "clickwrap_unticked_box",
                   "session_ref_sha256": R.sha_text(body["session_ref"])}
            events.append(self._record(derived_id("agr", aid), "agreement_accepted", i02_admission.ACTOR, clipper_id,
                                       {k: rec[k] for k in ("acceptance_id", "doc_id", "version", "doc_sha256",
                                                            "presented_sha256", "session_ref_sha256")},
                                       f"Clipper Agreement {body['version']} accepted (clickwrap)"))
            self._batch([("acceptances", aid, rec)], events, "agreement acceptance")
            return self._idem_store(key, h, {"accepted": True, "acceptance_id": aid, "unmet": [], "unmet_lines": [],
                                             "ledger_event_ids": events})

    def _latest_acceptance(self, clipper_id: str) -> Optional[dict]:
        accs = [a for a in self.st["acceptances"].values() if a["clipper_id"] == clipper_id]
        return max(accs, key=lambda a: (a["accepted_at"], a["acceptance_id"])) if accs else None

    def attest_training(self, request_id: str, clipper_id: str, body: dict) -> dict:
        with self.lock:
            key, h, cached = self._idem_check("hub", request_id, f"training/{clipper_id}", body)
            if cached:
                return cached
            self._live_clipper(clipper_id)
            if body["attested"] is not True:
                raise Invalid("disclosure training is attested with attested: true (CN-07)")
            tid = "cn-trn-" + _b32(f"hub|{request_id}")
            rec = {"training_id": tid, "clipper_id": clipper_id, "training_version": body["training_version"],
                   "attested_at": iso(self._now())}
            ev = self._record(derived_id("trn", tid), "training_attested", i02_admission.ACTOR, clipper_id,
                              {"training_id": tid, "training_version": body["training_version"]},
                              "Disclosure training attested")
            self._batch([("trainings", clipper_id, rec)], [ev], "training")
            return self._idem_store(key, h, {**rec, "ledger_event_ids": [ev]})

    # ================================================================== admission (i02)

    def admission(self, principal: str, request_id: str, clipper_id: str) -> dict:
        """Spec §C.2. Every check runs every time, each port call recorded first. Replay (N14-15b pattern): the same
        request_id + body is RE-EVALUATED; the stored ruling is returned only when the outcome is unchanged."""
        with self.lock:
            key, h, ent = self._idem_entry(principal, request_id, "admission", {"clipper_id": clipper_id})
            if ent is not None and ent["response"].get("admitted"):
                return ent["response"]     # the admission took effect: a retry gets that answer, never a re-run
            c = self._clipper(clipper_id)
            if c["status"] != "applicant":
                raise Conflict(f"clipper is {c['status']}: admission rules only on applicants"
                               + (" (refused under CN-01: no re-application path)" if c.get("minor") else ""))
            now = self._now()
            op = f"{principal}|{request_id}"
            base_id = "cn-adm-" + _b32(op, 40)
            events: list[str] = []
            v = self.current
            app = self._open_application(clipper_id)
            facts: dict = {"clipper_id": clipper_id, "rules_version": self.version_number}
            jur = None
            if v is None:
                items = [rules_not_in_force()]
            else:
                ports = PortCalls(self, base_id, i02_admission.ACTOR, clipper_id, events)
                vi, cmp_, fin, leg = self.ports.vi, self.ports.compliance, self.ports.finance, self.ports.legal
                if c.get("declared_country"):
                    jur = ports.call("compliance_38", "resolve_person",
                                     (c["declared_country"], c.get("declared_region"), bool(c.get("jurisdiction_attested"))),
                                     lambda: cmp_.resolve_person(derived_id("rq", base_id, "jur"), c["declared_country"],
                                                                 c.get("declared_region"), bool(c.get("jurisdiction_attested")),
                                                                 (app or {}).get("application_id") or clipper_id),
                                     JurisdictionAnswer(False))
                age = ports.call("verification_integrity", "age_subject", (clipper_id,), lambda: vi.age_subject(clipper_id),
                                 AgeAnswer(False))
                contact = self.contacts.get(f"clipper:{clipper_id}")
                email = (contact or {}).get("email")
                ident = (ports.call("verification_integrity", "identity_check", (clipper_id,),
                                    lambda: vi.identity_check(derived_id("rq", base_id, "idc"), clipper_id, email),
                                    IdentityAnswer(False)) if email else IdentityAnswer(False, reason="contact unavailable"))
                conns = ports.call("verification_integrity", "connections", (clipper_id,), lambda: vi.connections(clipper_id),
                                   ConnectionsAnswer(False))
                legal = ports.call("legal_37", "current_version", ("clipper_agreement",),
                                   lambda: leg.current_version("clipper_agreement"), DocVersionAnswer(False))
                tax = ports.call("finance_31", "tax_status", (clipper_id,), lambda: fin.tax_status(clipper_id), TaxAnswer(False))
                acceptance = self._latest_acceptance(clipper_id)
                training = self.st["trainings"].get(clipper_id)
                accounts = sorted(({"platform": a["platform"], "handle_sha256": a["handle_sha256"]}
                                   for a in c["connected_accounts"] if a.get("handle_sha256")
                                   and a["platform"] in ("youtube", "tiktok", "instagram", "x")),
                                  key=lambda a: (a["platform"], a["handle_sha256"]))
                cfacts = i02_admission.compliance_facts(c, app, acceptance, training, accounts) if app else {}
                act = (ports.call("compliance_38", "creator_activation", (clipper_id, _sha(cfacts)),
                                  lambda: cmp_.creator_activation(derived_id("rq", base_id, "act"), clipper_id, cfacts),
                                  ComplianceRuling(False)) if app else ComplianceRuling(False, reason="no application"))
                latest = (ports.call("compliance_38", "latest_activation", ("zbc_creator", clipper_id),
                                     lambda: cmp_.latest_activation("zbc_creator", clipper_id), ComplianceRuling(False))
                          if act.available and act.allowed else ComplianceRuling(False, reason="not read"))
                integ = ports.call("verification_integrity", "integrity", (clipper_id,), lambda: vi.integrity(clipper_id),
                                   IntegrityAnswer(False))
                dup = c.get("duplicate_of") if c.get("duplicate_of") in self.st["clippers"] else None
                x = i02_admission.Inputs(c, app, jur or JurisdictionAnswer(False, reason="no declared country"), age, dup,
                                         ident, conns, bool(self.p("CN-06", "required")),
                                         tuple(self.p("CN-06", "enabled_platforms")), legal, acceptance, training, tax, act,
                                         latest, integ)
                items = i02_admission.evaluate(x)
                if c.get("banned_identity_of"):
                    items.append(item("BANNED_IDENTITY", "CN-20", "this email belongs to an identity Andre banned; a ban "
                                      "is appealed once (CN-18), never re-applied around", "clipper_network",
                                      c["banned_identity_of"]))
                items = Citer(v).check(items)
                facts.update({"application_id": (app or {}).get("application_id"), "jurisdiction": _plain(jur),
                              "age": _plain(age), "duplicate_of": dup, "identity": _plain(ident),
                              "connections": _plain(conns), "legal": _plain(legal),
                              "acceptance_id": (acceptance or {}).get("acceptance_id"),
                              "training_id": (training or {}).get("training_id"), "tax_form_on_file": tax.form_on_file,
                              "tax_available": tax.available, "activation": _plain(act), "latest_activation": _plain(latest),
                              "integrity": _plain(integ), "compliance_facts_sha256": _sha(cfacts)})
            items = finalize(items)
            admitted = not items
            minor = any(u["code"] == "AGE_NOT_ADULT" and "minor" in u["message"] for u in items)
            facts_sha = _sha(facts)
            outcome = {"admitted": admitted, "unmet": [[u["rule_id"], u["code"]] for u in items],
                       "rules_version": self.version_number, "facts_sha256": facts_sha}
            outcome_sha = _sha(outcome)
            if ent is not None and ent.get("outcome_sha256") == outcome_sha:
                return ent["response"]
            ruling_id = base_id if ent is None else "cn-adm-" + _b32(f"{op}|{outcome_sha}", 40)
            if ruling_id in self.st["admissions"]:
                return self.st["admissions"][ruling_id]["view"]
            summary = f"Admission {'admitted' if admitted else 'not admitted'}: {len(items)} unmet under rules v{self.version_number or 0}"
            # the ruling id IS the ledger event id (callers read the ruling by it): recorded raw. Bug sweep C (R6): a
            # retry whose first try recorded a DIFFERENT outcome under the request's id (it never committed here) is
            # issued under the outcome-derived id instead of a lasting 409 (compliance-py N14-15b)
            try:
                events.append(self._record(ruling_id, "admission_ruling", i02_admission.ACTOR, clipper_id,
                                           {"ruling_id": ruling_id, **outcome}, summary, raw=True))
            except Unavailable:
                if ruling_id != base_id or not self._ledger_conflict:
                    raise
                ruling_id = "cn-adm-" + _b32(f"{op}|{outcome_sha}", 40)
                events.append(self._record(ruling_id, "admission_ruling", i02_admission.ACTOR, clipper_id,
                                           {"ruling_id": ruling_id, **outcome}, summary, raw=True))
            if jur is not None and jur.available:
                events.append(self._record(derived_id("jc", ruling_id), "jurisdiction_checked", i02_admission.ACTOR,
                                           clipper_id, {"class": jur.jurisdiction_class, "resolution_id": jur.resolution_id},
                                           f"Jurisdiction checked: {jur.jurisdiction_class}"))
            lines = [unmet_line(u) for u in items]
            view = {"admission_id": ruling_id, "clipper_id": clipper_id, "admitted": admitted, "unmet": items,
                    "unmet_lines": lines, "request_id": request_id, "facts_sha256": facts_sha,
                    "rules_pinned": self.rules_pinned, "rules_version": self.version_number, "ledger_event_id": ruling_id,
                    "status": "active" if admitted else ("refused" if minor else "applicant")}
            record = {"ruling_id": ruling_id, "admission_id": ruling_id, "clipper_id": clipper_id, "admitted": admitted,
                      "unmet": items, "unmet_lines": lines, "rules_version": self.version_number,
                      "evaluated_at": iso(now), "facts_sha256": facts_sha, "principal": principal,
                      "request_id": request_id, "request_sha256": h, "outcome_sha256": outcome_sha,
                      "first_used_at": iso(ent["at"]) if ent is not None else iso(now), "view": view,
                      "replaces_ruling_id": ent.get("ruling_id") if ent is not None else None}
            puts: list[tuple] = [("admissions", ruling_id, record)]
            new_c = dict(c)
            if jur is not None and jur.available:
                new_c["jurisdiction_ruling"] = {"class": jur.jurisdiction_class, "resolved_at": iso(now),
                                                "compliance_resolution_id": jur.resolution_id}
            if v is not None:
                new_c["age_status"] = {"vi_attestation_id": facts["age"]["attestation_id"],
                                       "result": facts["age"]["status"] if facts["age"]["available"] else "unavailable",
                                       "at": iso(now)}
                conns_now = {k["connection_id"]: k["status"] for k in (facts["connections"]["connections"] or [])} \
                    if facts["connections"]["available"] else {}
                accts = [{**a, "status": conns_now.get(a["vi_connection_id"], a["status"])} for a in c["connected_accounts"]]
                known = {a["vi_connection_id"] for a in accts}
                accts += [{"platform": k["platform"], "vi_connection_id": k["connection_id"], "handle_sha256": None,
                           "handle_contact_sha256": None, "status": k["status"]}
                          for k in (facts["connections"]["connections"] or []) if k["connection_id"] not in known]
                new_c["connected_accounts"] = accts
            prev = [a for a in self.st["admissions"].values() if a["clipper_id"] == clipper_id]
            prev_codes = max(prev, key=lambda a: a["evaluated_at"])["view"]["unmet"] if prev else None
            if admitted:
                new_c.update(status="active", tier="T0", admission_id=ruling_id, admitted_at=iso(now))
                th = f"cn-tier-{_b32(ruling_id, 20)}"
                puts.append(("tier_history", th, {"clipper_id": clipper_id, "from": None, "to": "T0", "at": iso(now),
                                                  "rule_id": "CN-10", "inputs_sha256": facts_sha,
                                                  "certification_ids_counted": []}))
                events.append(self._record(derived_id("tc", th), "tier_changed", i03_tiering.ACTOR, clipper_id,
                                           {"from": None, "to": "T0", "rule_id": "CN-10", "inputs_sha256": facts_sha},
                                           "Tier set to T0 (probation) at admission"))
                if app:
                    puts.append(("applications", app["application_id"], {**app, "status": "admitted", "decision_items": []}))
            elif app and not minor:
                puts.append(("applications", app["application_id"], {**app, "decision_items": items}))
            if minor:
                mputs, _ = self._minor_refusal({**new_c}, op, record_ruling=False)
                new_c = mputs[0][2]
                puts += mputs[1:]
            puts.append(("clippers", clipper_id, new_c))
            msg = None
            if v is not None and (admitted or prev_codes is None or
                                  [[u["rule_id"], u["code"]] for u in prev_codes] != outcome["unmet"]):
                msg, mev, _ = self._queue(ruling_id, "admission_decision",
                                          {"admission_id": ruling_id, "decision": "admitted" if admitted else "not_admitted",
                                           "rule_ids": sorted({u["rule_id"] for u in items}) if items else
                                           ["CN-01", "CN-02", "CN-03", "CN-04", "CN-05", "CN-06", "CN-07"],
                                           "open_count": len(items)}, clipper=new_c)
                if msg is not None:
                    puts.append(("messages", msg["message_id"], msg))
                    events += mev
            self._batch(puts, events, "admission")
            if msg is not None:
                self._deliver([msg["message_id"]])
            if minor:
                self._try_start_offboarding(clipper_id, "minor", op)
            self.idem[key] = {"h": h, "at": ent["at"] if ent is not None else now, "response": view,
                              "outcome_sha256": outcome_sha, "ruling_id": ruling_id}
            return view

    def clipper_view(self, clipper_id: str, with_contact: bool) -> dict:
        with self.lock:
            c = self._clipper(clipper_id)
            out = {k: v for k, v in c.items() if k not in ("intake_principal",)}
            out["active_suspension"] = self._active_suspension(c)
            out["connected_accounts"] = [{k: v for k, v in a.items() if k != "handle_contact_sha256"}
                                         for a in c["connected_accounts"]]
            if with_contact:
                contact = self.contacts.get(f"clipper:{clipper_id}")
                handles = {}
                for a in c["connected_accounts"]:
                    hv = self.contacts.get(f"handle:{clipper_id}:{a['vi_connection_id']}")
                    if hv:
                        handles[a["vi_connection_id"]] = hv["handle"]
                out["contact"] = ({"email": contact["email"], "display_name": contact["display_name"],
                                   "handles": handles} if contact else None)
            return out

    def _active_suspension(self, c: dict) -> Optional[dict]:
        now = self._now()
        for s in c.get("suspensions") or []:
            if s.get("lifted_at"):
                continue
            if s["until"] is None or parse_iso(s["until"]) > now:
                return s
        return None

    # ================================================================== campaigns: config, announcements, enrolment

    def _config(self, campaign_id: str) -> Optional[dict]:
        cfg = self.st["configs"].get(campaign_id)
        return cfg["versions"][-1] if cfg else None

    def _enrolments_of(self, campaign_id: Optional[str] = None, clipper_id: Optional[str] = None,
                       statuses=("active",)) -> list[dict]:
        return sorted((e for e in self.st["enrolments"].values()
                       if (campaign_id is None or e["campaign_id"] == campaign_id)
                       and (clipper_id is None or e["clipper_id"] == clipper_id) and e["status"] in statuses),
                      key=lambda e: e["enrolment_id"])

    def put_network_config(self, request_id: str, campaign_id: str, body: dict) -> dict:
        """Andre's B.4 config; one version per change. A changed rate card must take effect at least
        ``rate_notice_days`` ahead (CN-17) and is announced to every active enrolment."""
        with self.lock:
            key, h, cached = self._idem_check("andre", request_id, f"network-config/{campaign_id}", body)
            if cached:
                return cached
            if self.current is None:
                raise Conflict("no rule version is in force (CN-00)", unmet=[rules_not_in_force()])
            now = self._now()
            enabled = set(self.p("CN-06", "enabled_platforms"))
            bad = [p for p in body["platforms"] if p not in enabled]
            if bad:
                u = item("PLATFORM_NOT_ENABLED", "CN-06", f"platform(s) {', '.join(bad)} are not V&I-enabled payable "
                         "platforms")
                raise Conflict("network config refused", unmet=[u], unmet_lines=[unmet_line(u)])
            prev = self._config(campaign_id)
            eff = parse_iso(body["rate_card_effective_at"])
            ref = body["rate_card_ref"]
            changed = prev is not None and prev["rate_card_ref"] != ref
            notice = self.p("CN-17", "rate_notice_days")
            if changed and eff < now + timedelta(days=notice):
                u = item("RATE_NOTICE_TOO_SHORT", "CN-17", f"a rate-card change must take effect at least {notice} days "
                         f"after it is announced (effective {iso(eff)})")
                ev = self._record(derived_id("cfr", "andre", request_id), "network_config_refused", "andre", campaign_id,
                                  {"campaign_id": campaign_id, "code": u["code"], "rule_id": "CN-17"},
                                  "Network config refused: rate-card notice too short (CN-17)")
                self._commit("evidence", {"note": "network config refused", "campaign_id": campaign_id}, [ev])
                raise Conflict("network config refused", unmet=[u], unmet_lines=[unmet_line(u)])
            if prev is not None and not changed and prev["rate_card_effective_at"] != body["rate_card_effective_at"]:
                raise Invalid("rate_card_effective_at changes only together with a new rate_card_ref")
            n = (len(self.st["configs"][campaign_id]["versions"]) if prev else 0) + 1
            cv = {"config_version": n, "config_id": f"cn-cfg-{_b32(f'{campaign_id}|{n}', 20)}", "campaign_id": campaign_id,
                  **{k: copy.deepcopy(body[k]) for k in ("min_tier", "platforms", "clipper_jurisdictions", "max_clippers",
                                                         "max_submissions_per_clipper", "view_terms", "rate_card_ref",
                                                         "rate_card_effective_at", "opens_at", "closes_at")},
                  "created_at": iso(now), "rules_version": self.version_number}
            events = [self._record(derived_id("ncv", campaign_id, n, _sha(cv)), "network_config_versioned", "andre",
                                   campaign_id, {"campaign_id": campaign_id, "config_version": n, "config_sha256": _sha(cv),
                                                 "rate_card_changed": changed}, f"Network config v{n} set by Andre")]
            puts: list[tuple] = [("configs", campaign_id, {"campaign_id": campaign_id,
                                                           "versions": (self.st["configs"].get(campaign_id) or
                                                                        {"versions": []})["versions"] + [cv]})]
            msgs = []
            if changed:
                for i, e in enumerate(self._enrolments_of(campaign_id, statuses=("active", "paused"))):
                    c = self.st["clippers"].get(e["clipper_id"])
                    m, mev, _ = self._queue(f"andre|{request_id}", "rate_card_changed",
                                            {"campaign_id": campaign_id, "rate_card_doc_id": ref["finance_doc_id"],
                                             "rate_card_version": ref["version"], "rate_card_sha256": ref["sha256"],
                                             "effective_date": iso(eff)[:10], "rule_ids": ["CN-17"]}, clipper=c, n=i)
                    if m is not None:
                        puts.append(("messages", m["message_id"], m))
                        events += mev
                        msgs.append(m["message_id"])
            self._batch(puts, events, "network config")
            self._deliver(msgs)
            return self._idem_store(key, h, {"config": cv, "rate_card_changed": changed, "notified": len(msgs),
                                             "ledger_event_ids": events})

    def announce(self, request_id: str, campaign_id: str, version: int, facts: dict) -> dict:
        """Creative's ``announce_rulebook_version`` (CN-22): queued to every active enrolment; a new signed kit of the
        announced version is delivered to each. Answers ``GateResult`` fields: allowed only when every message was
        queued AND recorded and none was refused by the provider (the stand-in never delivers -> allowed false)."""
        with self.lock:
            key, h, cached = self._idem_check("creative_production", request_id, f"announce/{campaign_id}",
                                              {"version": version, "facts": facts})
            if cached:
                return cached
            ann = "cn-ann-" + _b32(f"creative_production|{request_id}")
            base = {"department": "clipper_network", "request_id": request_id, "reference": ann}
            if self.current is None:
                u = rules_not_in_force()
                return self._idem_store(key, h, {**base, "allowed": False, "reason": unmet_line(u)})
            op = f"creative|{request_id}"
            events: list[str] = []
            events += self._injection(ann, facts, campaign_id, i05_kit_delivery.ACTOR)
            ports = PortCalls(self, op, i05_kit_delivery.ACTOR, campaign_id, events)
            cre = self.ports.creative
            kit = ports.call("creative_production", "kit", (campaign_id,), lambda: cre.kit(campaign_id), KitAnswer(False))
            kit_ok = kit.available and kit.status == "signed" and kit.rulebook_version == version and kit.kit_sha256
            puts: list[tuple] = []
            msgs = []
            enrols = self._enrolments_of(campaign_id)
            for i, e in enumerate(enrols):
                c = self.st["clippers"].get(e["clipper_id"])
                m, mev, _ = self._queue(op, "rulebook_announced", {"campaign_id": campaign_id, "rulebook_version": version,
                                                                   "rule_ids": ["CN-22"]}, clipper=c, n=i)
                if m is not None:
                    puts.append(("messages", m["message_id"], m))
                    events += mev
                    msgs.append(m["message_id"])
                if kit_ok:
                    kd = self._kit_delivery(e, kit, version, op, i)
                    puts.append(("kits", kd["kit_delivery_id"], kd))
                    puts.append(("enrolments", e["enrolment_id"], {**e, "kit_delivery_id": kd["kit_delivery_id"]}))
                    events.append(self._record(derived_id("kd", kd["kit_delivery_id"]), "kit_delivered",
                                               i05_kit_delivery.ACTOR, e["enrolment_id"],
                                               {"kit_delivery_id": kd["kit_delivery_id"], "kit_id": kit.kit_id,
                                                "kit_sha256": kit.kit_sha256, "rulebook_version": version},
                                               f"Kit for rulebook v{version} delivered"))
            rec = {"announcement_id": ann, "campaign_id": campaign_id, "version": version, "facts_sha256": _sha(facts),
                   "enrolments": len(enrols), "messages": msgs, "kit_delivered": bool(kit_ok and enrols),
                   "at": iso(self._now())}
            events.append(self._record(derived_id("ann", ann), "rulebook_announcement_queued", i06_comms.ACTOR, campaign_id,
                                       {"announcement_id": ann, "version": version, "enrolments": len(enrols),
                                        "messages": len(msgs), "facts_sha256": rec["facts_sha256"]},
                                       f"Rulebook v{version} announcement queued to {len(msgs)} enrolment(s)"))
            puts.append(("announcements", ann, rec))
            self._batch(puts, events, "rulebook announcement")
            res = self._deliver(msgs)
            failed = [m for m, st in res.items() if st not in ("sent", "deferred")]
            missing = len(enrols) - len(msgs)
            allowed = not failed and missing == 0
            reason = (f"recorded; {len(msgs)} message(s) queued ({sum(1 for s in res.values() if s == 'sent')} sent, "
                      f"{sum(1 for s in res.values() if s == 'deferred')} waiting for quiet hours)"
                      if allowed else f"{len(failed)} of {len(msgs)} message(s) not delivered"
                      + (f"; {missing} enrolment(s) had no message" if missing else ""))
            if not enrols:
                reason = "recorded; 0 active enrolments to announce to"
            return self._idem_store(key, h, {**base, "allowed": allowed, "reason": reason,
                                             "kit_delivered": rec["kit_delivered"], "ledger_event_ids": events})

    def _kit_delivery(self, e: dict, kit: KitAnswer, version: int, op: str, n: int) -> dict:
        cfg = self._config(e["campaign_id"]) or {}
        kid = "cn-kit-" + _b32(f"{op}|{e['enrolment_id']}|{n}")
        return {"kit_delivery_id": kid, "enrolment_id": e["enrolment_id"], "clipper_id": e["clipper_id"],
                "campaign_id": e["campaign_id"], "kit_id": kit.kit_id, "kit_sha256": kit.kit_sha256,
                "rulebook_version": version, "rate_card_version": (cfg.get("rate_card_ref") or {}).get("version"),
                "delivered_at": iso(self._now()), "channel": "hub_reference", "acknowledged_at": None}

    def enrol(self, request_id: str, campaign_id: str, clipper_id: str) -> dict:
        """Spec §C.4 then §C.5: recorded, then the kit is delivered (one atomic commit)."""
        with self.lock:
            key, h, ent = self._idem_entry("hub", request_id, f"enrol/{campaign_id}", {"clipper_id": clipper_id})
            if ent is not None and ent["response"].get("eligible"):
                return ent["response"]     # the enrolment took effect: a retry gets that answer, never a re-run
            c = self._clipper(clipper_id)
            now = self._now()
            op = f"hub|{request_id}|{campaign_id}"
            base_id = "cn-enrr-" + _b32(op, 40)
            events: list[str] = []
            v = self.current
            cfg = self._config(campaign_id)
            facts: dict = {"campaign_id": campaign_id, "clipper_id": clipper_id, "rules_version": self.version_number,
                           "config_version": (cfg or {}).get("config_version")}
            kit = KitAnswer(False)
            rb = RulebookAnswer(False)
            if v is None:
                items = [rules_not_in_force()]
            else:
                ports = PortCalls(self, base_id, i04_enrolment.ACTOR, clipper_id, events)
                vi, cmp_, fin, leg, cre = (self.ports.vi, self.ports.compliance, self.ports.finance, self.ports.legal,
                                           self.ports.creative)
                jr = c.get("jurisdiction_ruling")
                fresh_h = self.p("CN-12", "jurisdiction_freshness_hours")
                jur = None
                if jr and parse_iso(jr["resolved_at"]) >= now - timedelta(hours=fresh_h):
                    jur = JurisdictionAnswer(True, jr["class"], jr["compliance_resolution_id"])
                elif c.get("declared_country"):
                    jur = ports.call("compliance_38", "resolve_person",
                                     (c["declared_country"], c.get("declared_region"), bool(c.get("jurisdiction_attested"))),
                                     lambda: cmp_.resolve_person(derived_id("rq", base_id, "jur"), c["declared_country"],
                                                                 c.get("declared_region"),
                                                                 bool(c.get("jurisdiction_attested")), clipper_id),
                                     JurisdictionAnswer(False))
                conns = ports.call("verification_integrity", "connections", (clipper_id,), lambda: vi.connections(clipper_id),
                                   ConnectionsAnswer(False))
                legal = ports.call("legal_37", "current_version", ("clipper_agreement",),
                                   lambda: leg.current_version("clipper_agreement"), DocVersionAnswer(False))
                rb = ports.call("creative_production", "live_rulebook", (campaign_id,),
                                lambda: cre.live_rulebook(campaign_id), RulebookAnswer(False))
                kit = ports.call("creative_production", "kit", (campaign_id,), lambda: cre.kit(campaign_id), KitAnswer(False))
                brand = ports.call("compliance_38", "latest_activation", ("zbc_brand", campaign_id),
                                   lambda: cmp_.latest_activation("zbc_brand", campaign_id), ComplianceRuling(False))
                ref = (cfg or {}).get("rate_card_ref") or {}
                rc = (ports.call("finance_31", "rate_card", (ref.get("finance_doc_id"), ref.get("version")),
                                 lambda: fin.rate_card(ref["finance_doc_id"], ref["version"]), RateCardAnswer(False))
                      if ref else RateCardAnswer(False))
                app = [a for a in self.st["applications"].values() if a["clipper_id"] == clipper_id
                       and a["status"] == "admitted"]
                sag = any(a["sag_aftra_member"] for a in app)
                blocks = self.counsel_open("enrolment") + (self.counsel_open("enrolment_sag_aftra") if sag else [])
                tier_cap = i03_tiering.cap(c.get("tier"), v.rule("CN-10")["parameters"])
                active = len(self._enrolments_of(clipper_id=clipper_id, statuses=("active", "paused")))
                in_campaign = len(self._enrolments_of(campaign_id, statuses=("active", "paused")))
                already = any(e["clipper_id"] == clipper_id for e in self._enrolments_of(campaign_id, statuses=("active", "paused")))
                acceptance = self._latest_acceptance(clipper_id)
                x = i04_enrolment.Inputs(c, cfg, now, self._active_suspension(c), jur, conns, legal, acceptance, rb, kit,
                                         brand, rc, active, tier_cap, in_campaign, blocks, already)
                items = Citer(v).check(i04_enrolment.evaluate(x))
                facts.update({"jurisdiction": _plain(jur), "connections": _plain(conns), "legal": _plain(legal),
                              "rulebook": _plain(rb), "kit": _plain(kit), "brand_activation": _plain(brand),
                              "rate_card": _plain(rc), "active_enrolments": active, "tier": c.get("tier"),
                              "campaign_clippers": in_campaign, "counsel_blocks": blocks,
                              "acceptance_id": (acceptance or {}).get("acceptance_id"),
                              "suspension": self._active_suspension(c)})
            items = finalize(items)
            eligible = not items
            facts_sha = _sha(facts)
            outcome = {"eligible": eligible, "unmet": [[u["rule_id"], u["code"]] for u in items],
                       "rules_version": self.version_number, "facts_sha256": facts_sha}
            outcome_sha = _sha(outcome)
            if ent is not None and ent.get("outcome_sha256") == outcome_sha:
                return ent["response"]
            ruling_id = base_id if ent is None else "cn-enrr-" + _b32(f"{op}|{outcome_sha}", 40)
            if ruling_id in self.st["enrolment_rulings"]:
                return self.st["enrolment_rulings"][ruling_id]["view"]
            try:
                events.append(self._record(ruling_id, "enrolment_ruling", i04_enrolment.ACTOR, clipper_id,
                                           {"ruling_id": ruling_id, "campaign_id": campaign_id, **outcome},
                                           f"Enrolment {'eligible' if eligible else 'refused'}: {len(items)} unmet",
                                           raw=True))
            except Unavailable:
                if ruling_id != base_id or not self._ledger_conflict:     # bug sweep C (R6), as admission
                    raise
                ruling_id = "cn-enrr-" + _b32(f"{op}|{outcome_sha}", 40)
                events.append(self._record(ruling_id, "enrolment_ruling", i04_enrolment.ACTOR, clipper_id,
                                           {"ruling_id": ruling_id, "campaign_id": campaign_id, **outcome},
                                           f"Enrolment {'eligible' if eligible else 'refused'}: {len(items)} unmet",
                                           raw=True))
            puts: list[tuple] = []
            enrolment = None
            msg = None
            if eligible:
                eid = "cn-enr-" + _b32(op)
                enrolment = {"enrolment_id": eid, "campaign_id": campaign_id, "clipper_id": clipper_id,
                             "rulebook_version": v.version, "config_version": cfg["config_version"],
                             "creative_rulebook_version": rb.live_version, "tier_at_enrolment": c.get("tier"),
                             "status": "active", "decision_items": [], "kit_delivery_id": None, "enrolled_at": iso(now),
                             "ruling_id": ruling_id}
                kd = self._kit_delivery(enrolment, kit, rb.live_version, op, 0)
                enrolment["kit_delivery_id"] = kd["kit_delivery_id"]
                puts += [("enrolments", eid, enrolment), ("kits", kd["kit_delivery_id"], kd)]
                events.append(self._record(derived_id("kd", kd["kit_delivery_id"]), "kit_delivered", i05_kit_delivery.ACTOR,
                                           eid, {"kit_delivery_id": kd["kit_delivery_id"], "kit_id": kit.kit_id,
                                                 "kit_sha256": kit.kit_sha256, "rulebook_version": rb.live_version},
                                           f"Kit delivered for rulebook v{rb.live_version}"))
                msg, mev, _ = self._queue(ruling_id, "kit_delivered", {"kit_id": kit.kit_id, "campaign_id": campaign_id,
                                                                "rulebook_version": rb.live_version,
                                                                "kit_sha256": kit.kit_sha256,
                                                                "rate_card_version": kd["rate_card_version"],
                                                                "rule_ids": ["CN-14", "CN-17"]}, clipper=c)
                if msg is not None:
                    puts.append(("messages", msg["message_id"], msg))
                    events += mev
            lines = [unmet_line(u) for u in items]
            view = {"ruling_id": ruling_id, "campaign_id": campaign_id, "clipper_id": clipper_id, "eligible": eligible,
                    "enrolment_id": enrolment["enrolment_id"] if enrolment else None,
                    "kit_delivery_id": enrolment["kit_delivery_id"] if enrolment else None, "unmet": items,
                    "unmet_lines": lines, "request_id": request_id, "facts_sha256": facts_sha,
                    "rules_pinned": self.rules_pinned, "rules_version": self.version_number, "ledger_event_id": ruling_id}
            puts.append(("enrolment_rulings", ruling_id, {
                "ruling_id": ruling_id, "campaign_id": campaign_id, "clipper_id": clipper_id, "eligible": eligible,
                "unmet": items, "evaluated_at": iso(now), "facts_sha256": facts_sha, "principal": "hub",
                "request_id": request_id, "request_sha256": h, "outcome_sha256": outcome_sha,
                "first_used_at": iso(ent["at"]) if ent is not None else iso(now), "view": view}))
            self._batch(puts, events, "enrolment")
            if msg is not None:
                self._deliver([msg["message_id"]])
            self.idem[key] = {"h": h, "at": ent["at"] if ent is not None else now, "response": view,
                              "outcome_sha256": outcome_sha, "ruling_id": ruling_id}
            return view

    def list_enrolments(self, campaign_id: str) -> list[dict]:
        with self.lock:
            return self._enrolments_of(campaign_id, statuses=("active", "paused", "withdrawn", "closed"))

    def kit_ack(self, request_id: str, enrolment_id: str, body: dict) -> dict:
        with self.lock:
            key, h, cached = self._idem_check("hub", request_id, f"kit-ack/{enrolment_id}", body)
            if cached:
                return cached
            e = self.st["enrolments"].get(enrolment_id)
            if e is None:
                raise NotFound("no such enrolment")
            self._live_clipper(e["clipper_id"], allow=("active", "suspended"))
            d = self.st["kits"].get(body["kit_delivery_id"])
            if d is None or d["enrolment_id"] != enrolment_id:
                raise NotFound("no such kit delivery for this enrolment")
            if not (body["rulebook_received"] and body["disclosure_section_received"]):
                raise Invalid("the acknowledgment confirms receipt of the rulebook AND the disclosure section (CN-14)")
            problem = i05_kit_delivery.ack_problem(d, body)
            if problem:
                raise Conflict(problem)
            now = self._now()
            ev = self._record(derived_id("ka", d["kit_delivery_id"]), "kit_acknowledged", i05_kit_delivery.ACTOR,
                              enrolment_id, {"kit_delivery_id": d["kit_delivery_id"], "kit_sha256": d["kit_sha256"],
                                             "rulebook_version": d["rulebook_version"],
                                             "rate_card_version": d["rate_card_version"]}, "Kit receipt acknowledged")
            new = {**d, "acknowledged_at": iso(now)}
            self._batch([("kits", d["kit_delivery_id"], new)], [ev], "kit acknowledgment")
            return self._idem_store(key, h, {**new, "ledger_event_ids": [ev]})

    # ================================================================== disputes (i07)

    def file_dispute(self, request_id: str, body: dict) -> dict:
        """Admissibility, routing, SLA; never the outcome. The statement is scanned (instruction-like text is recorded
        and ignored) and then kept only as a SHA-256; evidence refs only as hashes."""
        with self.lock:
            safe = {**body, "statement": R.sha_text(body["statement"])}
            key, h, cached = self._idem_check("hub", request_id, "disputes", safe)
            if cached:
                return cached
            c = self._clipper(body["clipper_id"])
            now = self._now()
            op = f"hub|{request_id}"
            did = "cn-dsp-" + _b32(op)
            events: list[str] = []
            events += self._injection(did, {"statement": body["statement"]}, c["clipper_id"], i07_disputes.ACTOR)
            unmet: list[dict] = []
            if self.current is None:
                unmet.append(rules_not_in_force())
            m = self.st["messages"].get(body["notice_message_id"])
            kind, ref = body["subject_kind"], body["subject_ref"]
            if m is None or m["clipper_id"] != c["clipper_id"] \
                    or m["template_id"] not in i07_disputes.NOTICE_TEMPLATES[kind]:
                unmet.append(item("DISPUTE_NOTICE_UNKNOWN", "CN-18", "the notice named is not a notice of this kind sent to "
                                  "this clipper"))
            elif ref not in (m.get("subject_refs") or []):
                unmet.append(item("DISPUTE_SUBJECT_MISMATCH", "CN-18", "the subject is not one the notice was about"))
            if self.current is not None and m is not None and not unmet:
                window = self.p("CN-18", "appeal_window_days")
                if now > parse_iso(m["queued_at"]) + timedelta(days=window):
                    unmet.append(item("APPEAL_WINDOW_CLOSED", "CN-18", f"the {window}-day appeal window from the notice "
                                      "has closed"))
            keys = self._appeal_keys(c["clipper_id"], kind, ref, m)
            dup = [d for d in self.st["disputes"].values() if d["clipper_id"] == c["clipper_id"]
                   and d["status"] != "refused"
                   and set(d.get("appeal_keys") or self._appeal_keys(d["clipper_id"], d["subject_kind"],
                                                                     d["subject_ref"], None)) & set(keys)]
            if dup:
                unmet.append(item("DISPUTE_ALREADY_FILED", "CN-18", f"one appeal per {kind.replace('_', ' ')}: "
                                  f"{dup[0]['dispute_id']} was already filed", "clipper_network", dup[0]["dispute_id"]))
            if self.current is not None:
                unmet = Citer(self.current).check(unmet)
            rec = {"dispute_id": did, "clipper_id": c["clipper_id"], "subject_kind": kind, "subject_ref": ref,
                   "notice_message_id": body["notice_message_id"], "filed_at": iso(now),
                   "statement_sha256": R.sha_text(body["statement"]), "statement_chars": len(body["statement"]),
                   "evidence_ref_sha256": [R.sha_text(e) for e in body["evidence_refs"]],
                   "route": i07_disputes.ROUTE[kind], "status": "refused" if unmet else "open", "decision_items": unmet,
                   "sla_due": None, "pushed": None, "outcome": None, "decided_by": None, "decided_at": None,
                   "vi_status_at_filing": None, "appeal_keys": keys}
            puts: list[tuple] = []
            msg = None
            if unmet:
                events.append(self._record(derived_id("dr", did), "dispute_refused", i07_disputes.ACTOR, c["clipper_id"],
                                           {"dispute_id": did, "codes": [[u["rule_id"], u["code"]] for u in unmet]},
                                           f"Dispute refused: {unmet[0]['code']}"))
            else:
                sla = i07_disputes.add_business_days(now.date(), self.p("CN-18", "sla_business_days"))
                rec["sla_due"] = sla.isoformat()
                if rec["route"] == "verification_integrity":
                    rec["vi_status_at_filing"] = self._vi_subject_status(kind, ref, did, events)
                events.append(self._record(derived_id("df", did), "dispute_filed", i07_disputes.ACTOR, c["clipper_id"],
                                           {"dispute_id": did, "subject_kind": kind, "route": rec["route"],
                                            "sla_due": rec["sla_due"], "statement_sha256": rec["statement_sha256"]},
                                           f"Dispute filed ({kind}); routed to {rec['route']}"))
                msg, mev, _ = self._queue(op, "appeal_received", {"dispute_id": did, "sla_date": rec["sla_due"],
                                                                  "rule_ids": ["CN-18"]}, clipper=c)
                if msg is not None:
                    puts.append(("messages", msg["message_id"], msg))
                    events += mev
            puts.append(("disputes", did, rec))
            self._batch(puts, events, "dispute")
            if msg is not None:
                self._deliver([msg["message_id"]])
            return self._idem_store(key, h, {**self._dispute_view(rec), "unmet_lines": [unmet_line(u) for u in unmet],
                                             "ledger_event_ids": events})

    def _appeal_keys(self, clipper_id: str, kind: str, ref: str, notice: Optional[dict]) -> list[str]:
        """What one appeal consumes (CN-18 "one appeal per flagged clip", AEGIS N16-5). A V&I-routed appeal (clip
        flag, V&I finding, strike) is about ONE underlying flag whichever kind the clipper picks: its keys are the
        V&I evidence ids and clip ids linked to the ref — every mirrored strike of this clipper whose strike id,
        finding ids or clip ids include it, and the notice's own subject refs. A CN-routed appeal keeps its
        (kind, ref) key."""
        if i07_disputes.ROUTE.get(kind) != "verification_integrity":
            return [f"{kind}:{ref}"]
        keys = {ref}
        for s in self.st["strikes"].values():
            if s["clipper_id"] != clipper_id:
                continue
            linked = {s["strike_id"], *(s.get("finding_ids") or []), *(s.get("subject_refs") or [])}
            if ref in linked:
                keys |= linked
        if notice is not None and ref in (notice.get("subject_refs") or []):
            keys |= set(notice.get("subject_refs") or [])
        return sorted(keys)

    def _vi_subject_status(self, kind: str, ref: str, op: str, events: list[str]) -> Optional[str]:
        """For V&I-routed disputes about a finding: the finding's status when filed (the outcome is read later)."""
        if kind not in ("vi_finding",):
            return None
        ports = PortCalls(self, op, i07_disputes.ACTOR, ref, events)
        vi = self.ports.vi
        a = ports.call("verification_integrity", "finding", (ref,), lambda: vi.finding(ref), FindingAnswer(False))
        return a.finding.status if a.available and a.finding else None

    def _dispute_view(self, d: dict) -> dict:
        return {k: v for k, v in d.items()}

    def get_dispute(self, dispute_id: str) -> dict:
        with self.lock:
            d = self.st["disputes"].get(dispute_id)
            if d is None:
                raise NotFound("no such dispute")
            packet = [{"strike_id": s["strike_id"], "class": s["class"], "vi_rule_id": s["rule_id"],
                       "status": s["status"], "evidence_ids": s["evidence_ids"], "finding_ids": s["finding_ids"]}
                      for s in self.st["strikes"].values() if s["clipper_id"] == d["clipper_id"]]
            return {**self._dispute_view(d), "evidence_packet": {"clipper_id": d["clipper_id"], "vi_strikes": packet,
                                                                 "cn_rule_ids": ["CN-18"] + sorted(
                                                                     {u["rule_id"] for u in d["decision_items"]})}}

    def dispute_outcome(self, request_id: str, dispute_id: str, decided_by: str, outcome: str, note: str) -> dict:
        with self.lock:
            key, h, cached = self._idem_check(decided_by, request_id, f"disputes/{dispute_id}/outcome",
                                              {"outcome": outcome, "note": note})
            if cached:
                return cached
            d = self.st["disputes"].get(dispute_id)
            if d is None:
                raise NotFound("no such dispute")
            if d["status"] != "open":
                raise Conflict(f"dispute is {d['status']}")
            if d["route"] != "clipper_network":
                raise Conflict("this dispute is about a V&I finding or strike: it is decided at V&I "
                               "(/vi/v1/findings/{id}/decision with the appeal id); CN reads the outcome")
            now = self._now()
            c = self.st["clippers"][d["clipper_id"]]
            events = [self._record(derived_id("do", dispute_id), "dispute_outcome", decided_by, d["clipper_id"],
                                   {"dispute_id": dispute_id, "outcome": outcome, "decided_by": decided_by,
                                    "note_sha256": R.sha_text(note)}, f"Dispute {outcome} by {decided_by}")]
            puts: list[tuple] = []
            new_c = c
            if outcome == "appeal_granted" and d["subject_kind"] == "suspension":
                sus = [({**s, "lifted_at": iso(now), "lifted_by": f"dispute:{dispute_id}"} if s.get("strike_id") == d["subject_ref"]
                        and not s.get("lifted_at") else s) for s in c.get("suspensions") or []]
                new_c = {**c, "suspensions": sus}
                if c["status"] == "suspended" and not any(not s.get("lifted_at") for s in sus if s["kind"] == "s3_full"):
                    new_c["status"] = "active"
                events.append(self._record(derived_id("dal", dispute_id), "discipline_applied", decided_by, d["clipper_id"],
                                           {"action": "suspension_lifted_on_appeal", "dispute_id": dispute_id,
                                            "rule_id": "CN-18"}, "Suspension lifted on appeal"))
                puts.append(("clippers", c["clipper_id"], new_c))
            nd = {**d, "status": "decided", "outcome": outcome, "decided_by": decided_by, "decided_at": iso(now)}
            puts.append(("disputes", dispute_id, nd))
            msg, mev, _ = self._queue(f"{decided_by}|{request_id}", "appeal_outcome",
                                      {"dispute_id": dispute_id, "outcome": outcome, "rule_ids": ["CN-18"]}, clipper=new_c)
            if msg is not None:
                puts.append(("messages", msg["message_id"], msg))
                events += mev
            self._batch(puts, events, "dispute outcome")
            if msg is not None:
                self._deliver([msg["message_id"]])
            return self._idem_store(key, h, {**nd, "ledger_event_ids": events})

    def disputes_sla_run(self, request_id: str) -> dict:
        """Scheduler: at SLA business day ``sla_push_business_day`` Andre is pushed (push stand-in: recorded as not
        delivered); V&I-routed finding disputes read the finding's status and close when it changed."""
        with self.lock:
            key, h, cached = self._idem_check("scheduler", request_id, "disputes/sla-run", {})
            if cached:
                return cached
            if self.current is None:
                return self._idem_store(key, h, {"ran": False, "unmet": [rules_not_in_force()]})
            now = self._now()
            push_day = self.p("CN-18", "sla_push_business_day")
            out = {"pushed": [], "closed_from_vi": []}
            for d in sorted(self.st["disputes"].values(), key=lambda x: x["dispute_id"]):
                if d["status"] != "open":
                    continue
                events: list[str] = []
                op = f"sla|{d['dispute_id']}|{request_id}"
                nd = dict(d)
                if d["route"] == "verification_integrity" and d["subject_kind"] == "vi_finding":
                    st = self._vi_subject_status("vi_finding", d["subject_ref"], op, events)
                    if st is not None and st != d["vi_status_at_filing"]:
                        outcome = "appeal_granted" if st == "overturned" else "appeal_denied"
                        nd.update(status="decided", outcome=outcome, decided_by="verification_integrity",
                                  decided_at=iso(now))
                        events.append(self._record(derived_id("do", d["dispute_id"]), "dispute_outcome",
                                                   "verification_integrity", d["clipper_id"],
                                                   {"dispute_id": d["dispute_id"], "outcome": outcome,
                                                    "decided_by": "verification_integrity"},
                                                   f"Dispute outcome read from V&I: {outcome}"))
                        out["closed_from_vi"].append(d["dispute_id"])
                if nd["status"] == "open" and not d["pushed"] and \
                        i07_disputes.business_days_between(parse_iso(d["filed_at"]).date(), now.date()) >= push_day:
                    ports = PortCalls(self, op, i07_disputes.ACTOR, d["dispute_id"], events)
                    push = self.ports.push
                    briefing = {"dispute_id": d["dispute_id"], "clipper_id": d["clipper_id"], "subject_kind": d["subject_kind"],
                                "sla_due": d["sla_due"]}
                    a = ports.call("push", "push", ("dispute_sla", d["dispute_id"]),
                                   lambda: push.push("dispute_sla", briefing), Ack(False))
                    nd["pushed"] = {"at": iso(now), "delivered": bool(a.available and a.ok)}
                    events.append(self._record(derived_id("dsw", d["dispute_id"]), "dispute_sla_warning", i07_disputes.ACTOR,
                                               d["clipper_id"], {"dispute_id": d["dispute_id"], "push_delivered":
                                                                 nd["pushed"]["delivered"], "sla_due": d["sla_due"]},
                                               f"Dispute SLA business day {push_day}: Andre pushed"))
                    out["pushed"].append({"dispute_id": d["dispute_id"], "delivered": nd["pushed"]["delivered"]})
                if nd != d:
                    self._batch([("disputes", d["dispute_id"], nd)], events, "dispute sla")
            return self._idem_store(key, h, {"ran": True, **out})

    # ================================================================== discipline (i08) and bans

    def discipline_sync(self, request_id: str) -> dict:
        """Scheduler: pull V&I's strike feed, mirror valid strikes, apply the CN-19 table. No route lets a caller
        create a strike; a strike whose evidence does not resolve at V&I is refused (STRIKE_EVIDENCE_MISSING)."""
        with self.lock:
            key, h, cached = self._idem_check("scheduler", request_id, "discipline/sync", {})
            if cached:
                return cached
            if self.current is None:
                return self._idem_store(key, h, {"ran": False, "unmet": [rules_not_in_force()]})
            op = f"scheduler|{request_id}"
            events: list[str] = []
            ports = PortCalls(self, op, i08_discipline.ACTOR, "strike-feed", events)
            vi = self.ports.vi
            cursor = (self.st["meta"].get("strike_cursor") or {}).get("cursor")
            strikes = []
            for page in range(STRIKE_PAGES_PER_SYNC):
                cur = cursor
                feed = ports.call("verification_integrity", "strikes", (cur, page), lambda c=cur: vi.strikes(c),
                                  StrikeFeed(False))
                if not feed.available:
                    if page == 0:
                        return self._idem_store(key, h, {"ran": False, "unmet": [unavailable(
                            "verification_integrity", "CN-19", "strike feed")], "ledger_event_ids": events})
                    break
                strikes += list(feed.strikes)
                if not feed.next_cursor or feed.next_cursor == cursor:
                    cursor = feed.next_cursor or cursor
                    break
                cursor = feed.next_cursor
            result = {"mirrored": [], "refused": [], "applied": [], "lifted": [], "ban_proposals": []}
            if strikes and events:
                # the feed crossings are committed with the cursor even if no strike is new
                self._batch([("meta", "strike_cursor", {"cursor": cursor})], list(events), "strike feed read")
                events = []
            elif cursor != (self.st["meta"].get("strike_cursor") or {}).get("cursor"):
                self._batch([("meta", "strike_cursor", {"cursor": cursor})], list(events), "strike feed read")
                events = []
            for s in strikes:
                self._apply_strike(s, op, result)
            return self._idem_store(key, h, {"ran": True, **result})

    def _apply_strike(self, s, op: str, result: dict) -> None:
        prev = self.st["strikes"].get(s.strike_id)
        if prev is not None and prev["status"] == s.status:
            return
        c = self.st["clippers"].get(s.clipper_id)
        events: list[str] = []
        sop = f"{op}|{s.strike_id}|{s.status}"
        if c is None:
            events.append(self._record(derived_id("srf", sop), "strike_refused", i08_discipline.ACTOR, s.strike_id,
                                       {"strike_id": s.strike_id, "code": "UNKNOWN_CLIPPER", "rule_id": "CN-19"},
                                       "V&I strike for a clipper CN does not hold: not mirrored"))
            self._batch([("strike_refusals", s.strike_id, {"strike_id": s.strike_id, "code": "UNKNOWN_CLIPPER",
                                                           "at": iso(self._now())})], events, "strike refused")
            result["refused"].append({"strike_id": s.strike_id, "code": "UNKNOWN_CLIPPER"})
            return
        ports = PortCalls(self, sop, i08_discipline.ACTOR, s.clipper_id, events)
        vi = self.ports.vi
        findings = {fid: ports.call("verification_integrity", "finding", (fid,), lambda f=fid: vi.finding(f),
                                    FindingAnswer(False)) for fid in s.finding_ids}
        problem = i08_discipline.evidence_problem(s, findings)
        now = self._now()
        if problem:
            u = item("STRIKE_EVIDENCE_MISSING", "CN-19", problem, "verification_integrity", s.strike_id)
            events.append(self._record(derived_id("srf", sop), "strike_refused", i08_discipline.ACTOR, s.clipper_id,
                                       {"strike_id": s.strike_id, "code": u["code"], "rule_id": "CN-19"},
                                       "V&I strike refused: evidence does not resolve (CN-19)"))
            self._batch([("strike_refusals", s.strike_id, {"strike_id": s.strike_id, "code": u["code"],
                                                           "message": u["message"], "at": iso(now)})], events,
                        "strike refused")
            result["refused"].append({"strike_id": s.strike_id, "code": u["code"], "message": u["message"]})
            return
        mirror = {"strike_id": s.strike_id, "clipper_id": s.clipper_id, "class": s.strike_class, "status": s.status,
                  "rule_id": s.rule_id, "finding_ids": list(s.finding_ids), "evidence_ids": list(s.evidence_ids),
                  "issued_at": s.issued_at, "subject_refs": list(s.subject_refs), "mirrored_at": iso(now)}
        events.append(self._record(derived_id("smr", sop), "strike_mirrored", i08_discipline.ACTOR, s.clipper_id,
                                   {"strike_id": s.strike_id, "class": s.strike_class, "status": s.status,
                                    "evidence_ids_sha256": _sha(list(s.evidence_ids))},
                                   f"V&I strike mirrored: {s.strike_class} {s.status}"))
        puts: list[tuple] = [("strikes", s.strike_id, mirror)]
        new_c = copy.deepcopy(c)
        msgs = []
        table = self.p("CN-19", "table")
        appeal = (now + timedelta(days=self.p("CN-18", "appeal_window_days"))).date().isoformat()
        terminal = c["status"] in ("banned", "offboarding", "offboarded", "refused")
        if s.status == "active" and (prev is None or prev["status"] != "active") and not terminal:
            cons = i08_discipline.consequence(s.strike_class, table)
            action = cons["action"]
            if action == "warning":
                pass
            elif action == "enrolment_suspension":
                until = (now + timedelta(days=cons["days"])).isoformat()
                new_c["suspensions"] = (new_c.get("suspensions") or []) + [
                    {"strike_id": s.strike_id, "kind": "s2_enrolment", "until": iso(parse_iso(until)), "at": iso(now)}]
                if new_c.get("tier") and new_c["tier"] != cons["tier_cap"]:
                    puts.append(self._tier_put(new_c, cons["tier_cap"], {"strike_id": s.strike_id}, [], events, sop))
                    new_c["tier"] = cons["tier_cap"]
            elif action == "suspension":
                new_c["suspensions"] = (new_c.get("suspensions") or []) + [
                    {"strike_id": s.strike_id, "kind": "s3_full", "until": None, "at": iso(now)}]
                if new_c["status"] == "active":
                    new_c["status"] = "suspended"
                if new_c.get("tier") and new_c["tier"] != "T0":
                    puts.append(self._tier_put(new_c, "T0", {"strike_id": s.strike_id}, [], events, sop))
                    new_c["tier"] = "T0"
                for e in self._enrolments_of(clipper_id=s.clipper_id):
                    puts.append(("enrolments", e["enrolment_id"], {**e, "status": "paused", "paused_by": s.strike_id}))
                if cons.get("ban_proposal"):
                    bp = "cn-ban-" + _b32(f"{s.strike_id}|proposal")
                    if bp not in self.st["ban_proposals"]:
                        puts.append(("ban_proposals", bp, {"proposal_id": bp, "clipper_id": s.clipper_id,
                                                           "strike_id": s.strike_id, "status": "pending_andre",
                                                           "proposed_at": iso(now), "rule_ids": ["CN-19", "CN-20"]}))
                        events.append(self._record(derived_id("bp", bp), "ban_proposed", i08_discipline.ACTOR,
                                                   s.clipper_id, {"proposal_id": bp, "strike_id": s.strike_id,
                                                                  "rule_ids": ["CN-19", "CN-20"]},
                                                   "Ban proposed to Andre (never automatic, CN-20)"))
                        result["ban_proposals"].append(bp)
            events.append(self._record(derived_id("dap", sop), "discipline_applied", i08_discipline.ACTOR, s.clipper_id,
                                       {"strike_id": s.strike_id, "class": s.strike_class, "action": action,
                                        "rule_id": "CN-19"}, f"Discipline applied: {action} ({s.strike_class})"))
            result["applied"].append({"strike_id": s.strike_id, "action": action})
            m, mev, _ = self._queue(sop, "strike_notice", {"strike_class": s.strike_class.lower(), "strike_id": s.strike_id,
                                                           "rule_ids": ["CN-19"], "evidence_ids": list(s.evidence_ids),
                                                           "consequence": action, "appeal_deadline": appeal},
                                    clipper=new_c, subject_refs=(s.strike_id, *s.subject_refs, *s.finding_ids), n=0)
            if m is not None:
                puts.append(("messages", m["message_id"], m))
                events += mev
                msgs.append(m["message_id"])
            if action in ("enrolment_suspension", "suspension"):
                sus = new_c["suspensions"][-1]
                m, mev, _ = self._queue(sop, "suspension_notice", {"suspension_kind": sus["kind"],
                                                                   "until": (sus["until"] or "further_review")[:10]
                                                                   if sus["until"] else "further_review",
                                                                   "rule_ids": ["CN-19"], "strike_id": s.strike_id,
                                                                   "appeal_deadline": appeal},
                                        clipper=new_c, subject_refs=(s.strike_id,), n=1)
                if m is not None:
                    puts.append(("messages", m["message_id"], m))
                    events += mev
                    msgs.append(m["message_id"])
        elif s.status == "overturned" and prev is not None and prev["status"] == "active":
            sus = [({**x, "lifted_at": iso(now), "lifted_by": f"overturned:{s.strike_id}"}
                    if x.get("strike_id") == s.strike_id and not x.get("lifted_at") else x)
                   for x in new_c.get("suspensions") or []]
            new_c["suspensions"] = sus
            if new_c["status"] == "suspended" and not any(x["kind"] == "s3_full" and not x.get("lifted_at") for x in sus):
                new_c["status"] = "active"
                for e in self._enrolments_of(clipper_id=s.clipper_id, statuses=("paused",)):
                    if e.get("paused_by") == s.strike_id:
                        puts.append(("enrolments", e["enrolment_id"], {**e, "status": "active", "paused_by": None}))
            for bp in self.st["ban_proposals"].values():
                if bp["strike_id"] == s.strike_id and bp["status"] == "pending_andre":
                    puts.append(("ban_proposals", bp["proposal_id"], {**bp, "status": "withdrawn_strike_overturned"}))
            events.append(self._record(derived_id("dlf", sop), "discipline_applied", i08_discipline.ACTOR, s.clipper_id,
                                       {"strike_id": s.strike_id, "action": "lifted", "rule_id": "CN-19"},
                                       "V&I overturned the strike: its consequence is lifted"))
            result["lifted"].append(s.strike_id)
        puts.append(("clippers", s.clipper_id, new_c))
        self._batch(puts, events, "strike")
        result["mirrored"].append(s.strike_id)
        self._deliver(msgs)

    def _tier_put(self, c: dict, to: str, inputs: dict, cert_ids: list, events: list, op: str) -> tuple:
        th = "cn-tier-" + _b32(f"{op}|{c['clipper_id']}|{to}", 20)
        events.append(self._record(derived_id("tc", th), "tier_changed", i03_tiering.ACTOR, c["clipper_id"],
                                   {"from": c.get("tier"), "to": to, "rule_id": "CN-10", "inputs_sha256": _sha(inputs),
                                    "certifications_counted": len(cert_ids)}, f"Tier {c.get('tier')} -> {to}"))
        return ("tier_history", th, {"clipper_id": c["clipper_id"], "from": c.get("tier"), "to": to, "at": iso(self._now()),
                                     "rule_id": "CN-10", "inputs_sha256": _sha(inputs), "inputs": inputs,
                                     "certification_ids_counted": list(cert_ids)})

    def ban_decision(self, request_id: str, clipper_id: str, proposal_id: str, decision: str, note: str,
                     andre_token: Optional[str] = None) -> dict:
        """Andre only (CN-20): the ban takes effect on his approval; V&I is then asked to block every account and
        identity HMAC (POST /vi/v1/bans) and offboarding starts (trigger ban).

        AEGIS N16-2: V&I's ban route needs Andre's own approval token, so CN passes through the EXACT token Andre
        sent on this request (``andre_token``, already verified by the FounderGate). It lives only in this call:
        never in a record, the idempotency store, a ledger payload, a log line or an export. A request without it
        is refused before anything happens. When the propagation could not be made (V&I down), Andre re-sends
        ``approve`` on the approved proposal and CN propagates again with that new request's token (the scheduler
        has no token and never propagates)."""
        if not isinstance(andre_token, str) or not andre_token:
            raise Forbidden("a ban is propagated to V&I with Andre's approval token, which this request lacks")
        with self.lock:
            key, h, cached = self._idem_check("andre", request_id, f"ban/{clipper_id}",
                                              {"proposal_id": proposal_id, "decision": decision, "note": note})
            if cached:
                return cached
            c = self._clipper(clipper_id)
            bp = self.st["ban_proposals"].get(proposal_id)
            if bp is None or bp["clipper_id"] != clipper_id:
                raise NotFound("no such ban proposal for this clipper")
            if bp["status"] == "approved" and decision == "approve" and bp.get("propagation") != "done":
                prop = self._propagate_ban(clipper_id, proposal_id, bp["decision_id"], bp["decided_at"], andre_token)
                return self._idem_store(key, h, {"proposal_id": proposal_id, "status": "approved",
                                                 "decision_id": bp["decision_id"],
                                                 "clipper_status": self.st["clippers"][clipper_id]["status"],
                                                 "vi_ban_propagation": prop, "offboarding_id": None,
                                                 "ledger_event_ids": []})
            if bp["status"] != "pending_andre":
                raise Conflict(f"ban proposal is {bp['status']}")
            if self.current is None:
                raise Conflict("no rule version is in force (CN-00)", unmet=[rules_not_in_force()])
            now = self._now()
            op = f"andre|{request_id}"
            events: list[str] = []
            if decision == "reject":
                events.append(self._record(derived_id("brj", proposal_id), "ban_rejected", "andre", clipper_id,
                                           {"proposal_id": proposal_id, "note_sha256": R.sha_text(note)},
                                           "Andre rejected the ban proposal"))
                self._batch([("ban_proposals", proposal_id, {**bp, "status": "rejected", "decided_at": iso(now)})], events,
                            "ban rejected")
                return self._idem_store(key, h, {"proposal_id": proposal_id, "status": "rejected",
                                                 "clipper_status": c["status"], "ledger_event_ids": events})
            decision_id = "cn-band-" + _b32(op)
            events.append(self._record(derived_id("bap", proposal_id), "ban_approved_by_andre", "andre", clipper_id,
                                       {"proposal_id": proposal_id, "decision_id": decision_id,
                                        "note_sha256": R.sha_text(note), "rule_ids": ["CN-20"]},
                                       "Andre approved the ban (CN-20)"))
            new_c = {**c, "status": "banned", "banned_at": iso(now), "ban_decision_id": decision_id}
            appeal = (now + timedelta(days=self.p("CN-18", "appeal_window_days"))).date().isoformat()
            puts: list[tuple] = [("ban_proposals", proposal_id, {**bp, "status": "approved", "decided_at": iso(now),
                                                                 "decision_id": decision_id, "propagation": "pending"}),
                                 ("clippers", clipper_id, new_c)]
            for e in self._enrolments_of(clipper_id=clipper_id, statuses=("active", "paused")):
                puts.append(("enrolments", e["enrolment_id"], {**e, "status": "withdrawn", "withdrawn_reason": "ban"}))
                events.append(self._record(derived_id("ew", e["enrolment_id"], "ban"), "enrolment_withdrawn",
                                           i09_offboarding.ACTOR, e["enrolment_id"],
                                           {"enrolment_id": e["enrolment_id"], "reason": "ban"}, "Enrolment withdrawn: ban"))
            m, mev, _ = self._queue(op, "ban_notice", {"decision_id": decision_id, "rule_ids": ["CN-19", "CN-20"],
                                                       "appeal_deadline": appeal}, clipper=new_c,
                                    subject_refs=(decision_id, proposal_id))
            if m is not None:
                puts.append(("messages", m["message_id"], m))
                events += mev
            self._batch(puts, events, "ban approved")
            prop = self._propagate_ban(clipper_id, proposal_id, decision_id, iso(now), andre_token)
            if m is not None:
                self._deliver([m["message_id"]])
            off = self._try_start_offboarding(clipper_id, "ban", op)
            return self._idem_store(key, h, {"proposal_id": proposal_id, "status": "approved", "decision_id": decision_id,
                                             "clipper_status": self.st["clippers"][clipper_id]["status"],
                                             "vi_ban_propagation": prop, "offboarding_id": off,
                                             "ledger_event_ids": events})

    def _propagate_ban(self, clipper_id: str, proposal_id: str, decision_id: str, approved_at: str,
                       andre_token: Optional[str]) -> str:
        bp = self.st["ban_proposals"][proposal_id]
        if bp.get("propagation") == "done":
            return "done"
        if not andre_token:
            return "needs_andre"          # only Andre's own token opens V&I's ban route (N16-2); nothing is sent
        events: list[str] = []
        op = f"ban|{decision_id}|{len(self.log)}"
        try:
            ports = PortCalls(self, op, i08_discipline.ACTOR, clipper_id, events)
            vi = self.ports.vi
            a = ports.call("verification_integrity", "ban", (clipper_id, decision_id),
                           lambda: vi.ban(derived_id("rq", decision_id), clipper_id, decision_id, approved_at,
                                          andre_token), Ack(False))
            state = "done" if a.available and a.ok else "pending"
            events.append(self._record(derived_id("bpr", op), "ban_propagation_requested", i08_discipline.ACTOR, clipper_id,
                                       {"decision_id": decision_id, "result": state},
                                       f"Ban propagation to V&I: {state}"))
            self._batch([("ban_proposals", proposal_id, {**bp, "propagation": state})], events, "ban propagation")
            return state
        except Unavailable:
            return "pending"

    # ================================================================== tiers (i03)

    def tiers_run(self, request_id: str) -> dict:
        with self.lock:
            key, h, cached = self._idem_check("scheduler", request_id, "tiers/run", {})
            if cached:
                return cached
            if self.current is None:
                return self._idem_store(key, h, {"ran": False, "unmet": [rules_not_in_force()]})
            now = self._now()
            params = self.current.rule("CN-10")["parameters"]
            changed, unavailable_for = [], []
            for c in sorted(self.st["clippers"].values(), key=lambda x: x["clipper_id"]):
                if c["status"] not in ("active", "suspended") or not c.get("admitted_at"):
                    continue
                op = f"scheduler|{request_id}|{c['clipper_id']}"
                events: list[str] = []
                ports = PortCalls(self, op, i03_tiering.ACTOR, c["clipper_id"], events)
                vi = self.ports.vi
                cid = c["clipper_id"]
                certs = ports.call("verification_integrity", "certifications", (cid,), lambda: vi.certifications(cid),
                                   CertificationsAnswer(False))
                if not certs.available:
                    unavailable_for.append(cid)
                    continue
                strikes = [s for s in self.st["strikes"].values() if s["clipper_id"] == cid]
                tier, inputs, ids = i03_tiering.compute(certs.certifications, strikes, c["admitted_at"], now, params,
                                                        bool(c.get("nominated")))
                if tier != c.get("tier"):
                    put = self._tier_put(c, tier, inputs, ids, events, op)
                    self._batch([put, ("clippers", cid, {**c, "tier": tier})], events, "tier change")
                    changed.append({"clipper_id": cid, "from": c.get("tier"), "to": tier, "inputs": inputs})
            return self._idem_store(key, h, {"ran": True, "changed": changed, "vi_unavailable_for": unavailable_for})

    def nominate(self, request_id: str, clipper_id: str, nominate: bool) -> dict:
        with self.lock:
            key, h, cached = self._idem_check("andre", request_id, f"nominate/{clipper_id}", {"nominate": nominate})
            if cached:
                return cached
            c = self._live_clipper(clipper_id, allow=("active", "suspended"))
            ev = self._record(derived_id("nom", clipper_id, request_id), "tier_nominated", "andre", clipper_id,
                              {"nominate": nominate, "rule_id": "CN-10"}, f"Andre {'nominated' if nominate else 'withdrew the nomination of'} this clipper for T3")
            self._batch([("clippers", clipper_id, {**c, "nominated": nominate})], [ev], "nomination")
            return self._idem_store(key, h, {"clipper_id": clipper_id, "nominated": nominate,
                                             "note": "the tier changes on the next POST /cn/v1/tiers/run (T3 = T2 + nomination)",
                                             "ledger_event_ids": [ev]})

    # ================================================================== offboarding (i09)

    def offboard(self, request_id: str, principal: str, clipper_id: str, trigger: str,
                 keep: Optional[bool]) -> dict:
        with self.lock:
            key, h, cached = self._idem_check(principal, request_id, f"offboarding/{clipper_id}",
                                              {"trigger": trigger, "keep": keep})
            if cached:
                return cached
            c = self._clipper(clipper_id)
            if c["status"] in ("offboarding", "offboarded"):
                raise Conflict(f"clipper is already {c['status']}")
            self._start_offboarding(clipper_id, trigger, f"{principal}|{request_id}", keep)
            return self._idem_store(key, h, self.offboarding_view(clipper_id))

    def _try_start_offboarding(self, clipper_id: str, trigger: str, op: str) -> Optional[str]:
        try:
            return self._start_offboarding(clipper_id, trigger, op, None)
        except (Unavailable, Conflict):
            return None     # picked up by POST /cn/v1/offboarding/run

    def _unsettled(self, clipper_id: str, op: str, events: list[str]) -> tuple[bool, Optional[str], bool]:
        """(has unsettled clips, last revision_watch_end, V&I answered)."""
        ports = PortCalls(self, op, i09_offboarding.ACTOR, clipper_id, events)
        vi = self.ports.vi
        a = ports.call("verification_integrity", "certifications", (clipper_id,), lambda: vi.certifications(clipper_id),
                       CertificationsAnswer(False))
        if not a.available:
            return True, None, False
        now = self._now()
        ends = [x.revision_watch_end for x in a.certifications if x.revision_watch_end
                and x.status in ("pending", "certified", "revised") and parse_iso(x.revision_watch_end) > now]
        return bool(ends), max(ends) if ends else None, True

    def _exit_deadline(self, trigger: str, start: datetime) -> Optional[datetime]:
        """CN-21: contact data and handles go ``post_exit_retention_days`` after the exit starts (0 for a minor)."""
        if self.current is None:
            return None
        return start + timedelta(days=0 if trigger == "minor" else self.p("CN-21", "post_exit_retention_days"))

    @staticmethod
    def _revoke_by(last_end: Optional[str], deadline: Optional[datetime]) -> Optional[str]:
        """Connections kept until the last settlement end at the retention deadline at the latest (N16-4)."""
        if deadline is None:
            return last_end
        if last_end is None:
            return iso(deadline)
        return iso(min(parse_iso(last_end), deadline))

    def _start_offboarding(self, clipper_id: str, trigger: str, op: str, keep: Optional[bool]) -> str:
        c = self._clipper(clipper_id)
        if c["status"] in ("offboarding", "offboarded"):
            existing = [o for o in self.st["offboardings"].values() if o["clipper_id"] == clipper_id]
            if existing:
                return existing[0]["offboarding_id"]
            raise Conflict("already offboarding")
        now = self._now()
        oid = "cn-off-" + _b32(f"{op}|{clipper_id}")
        events: list[str] = []
        steps: list[dict] = []
        puts: list[tuple] = []

        def step(name: str, status: str, evidence: Any = None) -> None:
            steps.append({"step": name, "status": status, "at": iso(now), "evidence": evidence})

        # (1) stop: status offboarding; enrolments withdrawn
        withdrawn = []
        for e in self._enrolments_of(clipper_id=clipper_id, statuses=("active", "paused")):
            puts.append(("enrolments", e["enrolment_id"], {**e, "status": "withdrawn", "withdrawn_reason": "offboarding"}))
            events.append(self._record(derived_id("ew", e["enrolment_id"], oid), "enrolment_withdrawn",
                                       i09_offboarding.ACTOR, e["enrolment_id"],
                                       {"enrolment_id": e["enrolment_id"], "reason": "offboarding"},
                                       "Enrolment withdrawn: offboarding"))
            withdrawn.append(e["enrolment_id"])
        step("status_offboarding", "done", {"enrolments_withdrawn": withdrawn})
        # (2) access
        step("cn_access_revoked", "done")
        ports = PortCalls(self, oid, i09_offboarding.ACTOR, clipper_id, events)
        hub = self.ports.hub
        a = ports.call("hub", "revoke_session", (clipper_id,), lambda: hub.revoke_session(clipper_id), Ack(False))
        step("hub_session_revoked", "done" if a.available and a.ok else "pending_dependency",
             None if a.ok else "hub port unavailable")
        immediate = trigger in i09_offboarding.IMMEDIATE_REVOKE
        unsettled, last_end, vi_ok = (False, None, True) if immediate else self._unsettled(clipper_id, oid, events)
        keep_conn = (not immediate) and unsettled and keep is not False
        # (5) the retention deadline (CN-21) is fixed now; everything kept is bounded by it (AEGIS N16-4)
        deadline = self._exit_deadline(trigger, now)
        revoke_after = self._revoke_by(last_end, deadline) if keep_conn else None
        conn_results = []
        if not keep_conn:
            vi = self.ports.vi
            for acct in c["connected_accounts"]:
                if acct["status"] in ("revoked", "refused", "expired"):
                    continue
                cid_ = acct["vi_connection_id"]
                r = ports.call("verification_integrity", "connection_revoke", (cid_,),
                               lambda x=cid_: vi.connection_revoke(derived_id("rq", oid, x), x), Ack(False))
                conn_results.append({"connection_id": cid_, "revoked": bool(r.available and r.ok)})
        pending_conn = [r for r in conn_results if not r["revoked"]]
        if keep_conn:
            step("vi_connections_revoked", "kept_until_last_settlement", {"revoke_after": revoke_after,
                                                                         "vi_answered": vi_ok})
        else:
            step("vi_connections_revoked", "pending_dependency" if pending_conn else "done", conn_results)
        # (3) Finance open items
        fin = self.ports.finance
        oi = ports.call("finance_31", "open_items", (clipper_id,), lambda: fin.open_items(clipper_id), OpenItemsAnswer(False))
        fstate = oi.state if oi.available and oi.state in ("none", "open") else "unknown"
        notified = None
        if fstate != "none":
            n = ports.call("finance_31", "notify_offboarding", (clipper_id, oid),
                           lambda: fin.notify_offboarding(clipper_id, oid), Ack(False))
            notified = bool(n.available and n.ok)
        step("finance_open_items", "done" if fstate == "none" else "pending_finance",
             {"state": fstate, "finance_notified": notified})
        finance_question = {"status": "resolved" if fstate == "none" else "unresolved", "state": fstate,
                            "flagged_to_andre": False, "flagged_at": None, "push_delivered": None}
        # (4) export
        export = self._export(clipper_id)
        esha = _sha(export)
        eref = "cn-exp-" + _b32(f"{oid}|{esha}", 20)
        events.append(self._record(derived_id("dex", oid), "data_exported", i09_offboarding.ACTOR, clipper_id,
                                   {"offboarding_id": oid, "export_ref": eref, "export_sha256": esha},
                                   "Clipper data export prepared"))
        step("export", "done", {"export_ref": eref, "export_sha256": esha})
        # (5) deletion scheduled: at the deadline whatever Finance answers (an unresolved Finance question is
        # flagged to Andre then, N16-4); only an open dispute delays it (spec C.9)
        if deadline is not None:
            delete_after = iso(deadline)
            step("deletion", "scheduled", {"delete_after": delete_after,
                                           "retention_days": (deadline - now).days})
        else:
            delete_after = None
            step("deletion", "waiting_for_rules", "no rule version in force: retention (CN-21) unknown")
        status = "pending_finance" if fstate != "none" else "in_progress"
        rec = {"offboarding_id": oid, "clipper_id": clipper_id, "trigger": trigger, "started_at": iso(now),
               "steps": steps, "finance_open_items": fstate, "finance_notified": notified, "export_ref": eref,
               "export_sha256": esha, "status": status, "delete_after": delete_after, "revoke_after": revoke_after,
               "keep_connections": keep_conn, "closed_at": None, "contact_deleted_at": None,
               "finance_question": finance_question, "settlement_known": vi_ok}
        for s_ in steps:
            events.append(self._record(derived_id("ofs", oid, s_["step"]), "offboarding_step", i09_offboarding.ACTOR,
                                       clipper_id, {"offboarding_id": oid, "step": s_["step"], "status": s_["status"],
                                                    "trigger": trigger}, f"Offboarding step {s_['step']}: {s_['status']}"))
        accts = [({**a, "status": "revoked"} if any(r["connection_id"] == a["vi_connection_id"] and r["revoked"]
                                                     for r in conn_results) else a) for a in c["connected_accounts"]]
        new_c = {**c, "status": "offboarding", "connected_accounts": accts, "offboarding_id": oid,
                 "status_before_offboarding": c["status"]}
        puts += [("offboardings", oid, rec), ("clippers", clipper_id, new_c)]
        msgs = []
        if self.current is not None:
            conn_word = "kept_until_last_settlement" if keep_conn else ("revoked" if not pending_conn else "revocation_pending")
            for i, (tid, vars_) in enumerate((
                    ("offboarding_confirmation", {"offboarding_id": oid, "connections": conn_word,
                                                  "offboarding_status": status,
                                                  "retention_days": 0 if trigger == "minor" else
                                                  self.p("CN-21", "post_exit_retention_days"),
                                                  "rule_ids": ["CN-21"]}),
                    ("data_export_ready", {"export_ref": eref, "export_sha256": esha}))):
                m, mev, _ = self._queue(oid, tid, vars_, clipper=new_c, n=i)
                if m is not None:
                    puts.append(("messages", m["message_id"], m))
                    events += mev
                    msgs.append(m["message_id"])
        self._batch(puts, events, "offboarding started")
        self._deliver(msgs)
        return oid

    def offboarding_view(self, clipper_id: str) -> dict:
        with self.lock:
            offs = [o for o in self.st["offboardings"].values() if o["clipper_id"] == clipper_id]
            if not offs:
                raise NotFound("no offboarding for this clipper")
            o = max(offs, key=lambda x: x["started_at"])
            return {**o, "clipper_status": self.st["clippers"][clipper_id]["status"]}

    def offboarding_run(self, request_id: str) -> dict:
        """Scheduler: advance every open exit — re-ask Finance, revoke kept connections after the last settlement,
        delete contact data when due (never while Finance has open items or a dispute is open), close. Also starts
        the exit of a clipper refused as a minor or banned whose exit could not start at the time, and retries a
        ban's propagation to V&I that is still pending."""
        with self.lock:
            key, h, cached = self._idem_check("scheduler", request_id, "offboarding/run", {})
            if cached:
                return cached
            now = self._now()
            out = {"started": [], "advanced": [], "closed": [], "deleted": [], "blocked": [], "ban_propagation": []}
            for bp in sorted(self.st["ban_proposals"].values(), key=lambda x: x["proposal_id"]):
                if bp["status"] == "approved" and bp.get("propagation") != "done":
                    # the scheduler holds no Andre token: it reports; Andre re-sends approve to propagate (N16-2)
                    st = self._propagate_ban(bp["clipper_id"], bp["proposal_id"], bp["decision_id"], bp["decided_at"],
                                             None)
                    out["ban_propagation"].append({"proposal_id": bp["proposal_id"], "result": st})
            for c in sorted(self.st["clippers"].values(), key=lambda x: x["clipper_id"]):
                if (c.get("minor") and c["status"] == "refused") or c["status"] == "banned":
                    oid = self._try_start_offboarding(c["clipper_id"], "minor" if c.get("minor") else "ban",
                                                      f"scheduler|{request_id}")
                    if oid:
                        out["started"].append(oid)
            for o in sorted(self.st["offboardings"].values(), key=lambda x: x["offboarding_id"]):
                if o["status"] == "closed":
                    continue
                cid = o["clipper_id"]
                op = f"scheduler|{request_id}|{o['offboarding_id']}"
                events: list[str] = []
                ports = PortCalls(self, op, i09_offboarding.ACTOR, cid, events)
                no = copy.deepcopy(o)
                c = self.st["clippers"][cid]
                new_c = copy.deepcopy(c)
                steps_new = []
                if no["finance_open_items"] != "none":
                    fin = self.ports.finance
                    oi = ports.call("finance_31", "open_items", (cid,), lambda: fin.open_items(cid), OpenItemsAnswer(False))
                    st = oi.state if oi.available and oi.state in ("none", "open") else "unknown"
                    if st != no["finance_open_items"]:
                        no["finance_open_items"] = st
                        steps_new.append({"step": "finance_open_items", "status": "done" if st == "none" else "pending_finance",
                                          "at": iso(now), "evidence": {"state": st}})
                    if st == "none" and no["status"] == "pending_finance":
                        no["status"] = "in_progress"
                    if st == "none":
                        no["finance_question"] = {**(no.get("finance_question") or {}), "status": "resolved",
                                                  "state": "none"}
                if no.get("delete_after") is None and self.current is not None:
                    no["delete_after"] = iso(self._exit_deadline(no["trigger"], parse_iso(no["started_at"])))
                deadline = parse_iso(no["delete_after"]) if no.get("delete_after") else None
                if no.get("keep_connections") and (not no.get("revoke_after") or no.get("settlement_known") is False):
                    # V&I could not say when the last settlement ends: ask again; kept no later than the deadline
                    unsettled, last_end, vi_ok = self._unsettled(cid, op, events)
                    if vi_ok:
                        no["revoke_after"] = self._revoke_by(last_end or iso(now), deadline)
                        no["settlement_known"] = True
                    elif deadline is not None:
                        no["revoke_after"] = iso(deadline)
                elif no.get("keep_connections") and deadline is not None and parse_iso(no["revoke_after"]) > deadline:
                    no["revoke_after"] = iso(deadline)          # an exit recorded before N16-4: bounded now
                if no.get("keep_connections") and no.get("revoke_after") and parse_iso(no["revoke_after"]) <= now:
                    vi = self.ports.vi
                    res = []
                    for acct in c["connected_accounts"]:
                        if acct["status"] in ("revoked", "refused", "expired"):
                            continue
                        x = acct["vi_connection_id"]
                        r = ports.call("verification_integrity", "connection_revoke", (x,),
                                       lambda x=x: vi.connection_revoke(derived_id("rq", op, x), x), Ack(False))
                        res.append({"connection_id": x, "revoked": bool(r.available and r.ok)})
                    if all(r["revoked"] for r in res):
                        no["keep_connections"] = False
                        new_c["connected_accounts"] = [{**a, "status": "revoked"} for a in c["connected_accounts"]]
                    steps_new.append({"step": "vi_connections_revoked", "status": "done" if all(r["revoked"] for r in res)
                                      else "pending_dependency", "at": iso(now), "evidence": res})
                open_disputes = [d["dispute_id"] for d in self.st["disputes"].values()
                                 if d["clipper_id"] == cid and d["status"] == "open"]
                due = deadline is not None and deadline <= now
                if due and not no.get("contact_deleted_at"):
                    if open_disputes:
                        out["blocked"].append({"offboarding_id": no["offboarding_id"], "why": "open dispute"})
                    else:
                        if no["finance_open_items"] != "none" and not (no.get("finance_question") or {}).get("flagged_to_andre"):
                            # CN-21 holds whatever Finance answers (N16-4): the unanswered question goes to Andre
                            push = self.ports.push
                            briefing = {"offboarding_id": no["offboarding_id"], "clipper_id": cid,
                                        "finance_open_items": no["finance_open_items"], "rule_id": "CN-21"}
                            pa = ports.call("push", "push", ("offboarding_finance_question", no["offboarding_id"]),
                                            lambda b=briefing: push.push("offboarding_finance_question", b), Ack(False))
                            no["finance_question"] = {"status": "unresolved", "state": no["finance_open_items"],
                                                      "flagged_to_andre": True, "flagged_at": iso(now),
                                                      "push_delivered": bool(pa.available and pa.ok)}
                            steps_new.append({"step": "finance_question", "status": "unresolved_flagged_to_andre",
                                              "at": iso(now), "evidence": {"state": no["finance_open_items"],
                                                                           "push_delivered": bool(pa.available and pa.ok)}})
                        keys = [f"clipper:{cid}"] + [f"handle:{cid}:{a['vi_connection_id']}" for a in c["connected_accounts"]
                                                     if a.get("handle_contact_sha256")]
                        events.append(self._record(derived_id("dd", no["offboarding_id"]), "data_deleted",
                                                   i09_offboarding.ACTOR, cid,
                                                   {"offboarding_id": no["offboarding_id"], "keys": len(keys),
                                                    "kept": ["acceptances (ids and hashes)", "ledger (ids and hashes)"]},
                                                   "Contact data and handles deleted (CN-21)"))
                        new_c["contact_sha256"] = None
                        new_c["connected_accounts"] = [{**a, "handle_contact_sha256": None}
                                                       for a in new_c["connected_accounts"]]
                        no["contact_deleted_at"] = iso(now)
                        steps_new.append({"step": "deletion", "status": "done", "at": iso(now), "evidence": {"keys": len(keys)}})
                        no["_delete_keys"] = keys
                        out["deleted"].append(no["offboarding_id"])
                if no.get("contact_deleted_at") and no["finance_open_items"] == "none" and not open_disputes \
                        and not no.get("keep_connections"):
                    no["status"] = "closed"
                    no["closed_at"] = iso(now)
                    new_c["status"] = "offboarded"
                    steps_new.append({"step": "closed", "status": "done", "at": iso(now), "evidence": None})
                    events.append(self._record(derived_id("oc", no["offboarding_id"]), "offboarding_closed",
                                               i09_offboarding.ACTOR, cid, {"offboarding_id": no["offboarding_id"]},
                                               "Offboarding closed"))
                    out["closed"].append(no["offboarding_id"])
                for s_ in steps_new:
                    events.append(self._record(derived_id("ofs", op, s_["step"]), "offboarding_step", i09_offboarding.ACTOR,
                                               cid, {"offboarding_id": no["offboarding_id"], "step": s_["step"],
                                                     "status": s_["status"]}, f"Offboarding step {s_['step']}: {s_['status']}"))
                no["steps"] = no["steps"] + steps_new
                keys = no.pop("_delete_keys", [])
                if no != o or new_c != c:
                    self._batch([("offboardings", no["offboarding_id"], no), ("clippers", cid, new_c)], events,
                                "offboarding advanced")
                    for k in keys:
                        try:
                            self.contacts.delete(k)
                        except ContactStoreError:
                            pass   # no longer expected by the log: purged as an orphan at the next start
                    if no["offboarding_id"] not in out["closed"]:
                        out["advanced"].append(no["offboarding_id"])
            return self._idem_store(key, h, {"ran": True, **out})

    def _export(self, clipper_id: str) -> dict:
        c = self.st["clippers"][clipper_id]
        contact = self.contacts.get(f"clipper:{clipper_id}")
        handles = {}
        for a in c["connected_accounts"]:
            hv = self.contacts.get(f"handle:{clipper_id}:{a['vi_connection_id']}")
            if hv:
                handles[a["vi_connection_id"]] = hv["handle"]
        mine = lambda coll: sorted((x for x in self.st[coll].values() if x.get("clipper_id") == clipper_id),  # noqa: E731
                                   key=lambda x: canonical(x))
        return {"clipper": {k: v for k, v in c.items() if k != "intake_principal"},
                "contact": ({"email": contact["email"], "display_name": contact["display_name"], "handles": handles}
                            if contact else None),
                "applications": [{k: v for k, v in a.items() if k != "principal"} for a in mine("applications")],
                "acceptances": mine("acceptances"), "training": self.st["trainings"].get(clipper_id),
                "enrolments": mine("enrolments"), "kit_deliveries": mine("kits"),
                "messages": [{k: v for k, v in m.items() if k != "variables"} for m in mine("messages")],
                "tier_history": mine("tier_history"), "strike_mirror": mine("strikes"),
                "disputes": mine("disputes"), "admissions": [{k: v for k, v in a.items() if k in
                                                              ("admission_id", "admitted", "unmet", "evaluated_at")}
                                                             for a in mine("admissions")]}

    def export_for(self, clipper_id: str) -> dict:
        """Hub only: the clipper's own data (contact included while it still exists)."""
        with self.lock:
            self._clipper(clipper_id)
            ex = self._export(clipper_id)
            # bug sweep C (R6): no clock in the id (it made every retry a new, unnamed event); the content and the
            # log position (seq, in the payload) identify the export
            ev = self._record(derived_id("dex", clipper_id, _sha(ex)), "data_exported",
                              i09_offboarding.ACTOR, clipper_id, {"export_sha256": _sha(ex)}, "Clipper data export served")
            self._commit("evidence", {"note": "export served", "clipper_id": clipper_id, "export_sha256": _sha(ex)}, [ev])
            return {"export": ex, "export_sha256": _sha(ex), "ledger_event_id": ev}

    # ================================================================== audit, health

    def audit_export(self, principal: str, since: Optional[str], until: Optional[str], cursor: int) -> dict:
        with self.lock:
            s = parse_iso(since) if since else None
            u = parse_iso(until) if until else None
            out, last = [], cursor
            for rec in self.log.iter_records(cursor + 1):
                last = rec["seq"]
                at = parse_iso(rec["at"])
                if (s and at < s) or (u and at > u):
                    continue
                out.append(i10_evidence_audit.export_entry(rec))
                if len(out) >= EXPORT_PAGE:
                    break
            more = last < len(self.log)
            op = f"{principal}|{since}|{until}|{cursor}|{len(self.log)}"
            eid = self._record(derived_id("aud", op), "audit_export_issued", EVIDENCE_ACTOR, "audit_export",
                               {"cursor": cursor, "count": len(out), "next_cursor": last if more else None},
                               f"Audit export page served ({len(out)} records)")
            return {"records": out, "next_cursor": last if more else None, "ledger_event_id": eid,
                    "rules_version": self.version_number}

    def audit_evidence(self, limit: int, offset: int, event_type: Optional[str] = None) -> dict:
        """``GET /cn/v1/audit/evidence`` (bug sweep C R6; bizdev-py / finance-py ``audit_evidence``). Every Clipper Network event on the
        ledger except the log anchors, each marked ``committed`` (a local line with seq ``s`` names it in
        ``data.evidence``, the ledger holds that line's anchor with the anchor payload's hash, and the ledger's
        ``payload_sha256`` is the named one, which carries ``rk`` and ``seq``), ``cited`` (listed by an anchored line
        without being typed evidence: crossings, rulings, versions, leases, reconciles, events recorded before this
        view existed, a failed try a later line names) or ``attempted`` (anything else: recorded first, never
        committed). Unanchored evidence = attempted, not done. Only the raw lines are copied under the service lock;
        parsing, hashing and the ledger read happen outside it. Eventually consistent: re-read to settle."""
        with self.lock:
            if self._closed:
                raise Unavailable("SERVICE_CLOSED: this Clipper Network instance is closed")
            raw = self.log.raw_lines()
        try:
            entries = self.recorder.client.entries()
        except LedgerQueryFailed:
            raise Unavailable("the evidence ledger could not be read") from None
        epoch = hashlib.sha256(raw[0]).hexdigest()[:16] if raw else None
        mine = [e for e in entries if isinstance(e, dict) and e.get("department") == "clipper_network"]
        anchors = {e.get("event_id"): e for e in mine if e.get("event_type") == i10_evidence_audit.ANCHOR_TYPE}
        named: dict[str, tuple] = {}
        cited: dict[str, int] = {}
        for ln in raw:
            r = json.loads(ln)
            line_sha = hashlib.sha256(ln).hexdigest()
            a = anchors.get(i10_evidence_audit.anchor_id(epoch, r["seq"], line_sha))
            want = payload_sha256({"epoch": epoch, "seq": r["seq"], "line_sha256": line_sha, "kind": r["kind"]})
            if a is None or a.get("payload_sha256") != want:
                continue
            for n in r["data"].get("evidence") or []:
                named[n["event_id"]] = (r["seq"], n.get("rk"), n.get("payload_sha256"))
            for eid in list(r["data"].get("ledger_event_ids") or []) + list(r["data"].get("attempted_event_ids") or []):
                cited.setdefault(eid, r["seq"])
        rows, counts = [], {"committed": 0, "cited": 0, "attempted": 0}
        for e in mine:
            et = e.get("event_type")
            if et == i10_evidence_audit.ANCHOR_TYPE or (event_type is not None and et != event_type):
                continue
            eid = e.get("event_id")
            n = named.get(eid)
            meta = i10_evidence_audit.meta_sha256(str(e.get("actor")), str(e.get("subject_id")), str(e.get("summary")))
            if n is not None and n[2] == e.get("payload_sha256") \
                    and eid == i10_evidence_audit.evidence_id(n[1] or "", et, e.get("payload_sha256") or "", meta):
                status, seq, rk = "committed", n[0], n[1]
            elif n is None and eid in cited:
                status, seq, rk = "cited", cited[eid], None
            else:
                status, seq, rk = "attempted", None, None
            counts[status] += 1
            rows.append({"event_id": eid, "event_type": et, "subject_id": e.get("subject_id"), "status": status,
                         "seq": seq, "rk": rk, "payload_sha256": e.get("payload_sha256")})
        return {"rule": "unanchored evidence = attempted, not done", "consistency": "eventual; re-read to settle",
                "events": rows[offset:offset + limit], "total": len(rows), "counts": counts, "limit": limit,
                "offset": offset, "log_lines": len(raw), "epoch": epoch}

    def health(self) -> dict:
        return {"status": "ok", "service": "clipper-network-py", "rules_version": self.version_number,
                "in_memory": self.log.in_memory, "rules_pinned": self.rules_pinned,
                "reconcile_mode": self.reconcile_mode, "reconcile_required": bool(self.reconcile_required),
                "log_write_fault": bool(self.log.fault)}

    def delegate_confirmed(self, name: str) -> None:
        """A delegate counts only when People (43) confirms it active (recorded crossing); the stand-in never does,
        so dispute outcomes are Andre's alone until People is built (spec §C.7)."""
        from errors import FounderRefused
        with self.lock:
            events: list[str] = []
            try:
                ports = PortCalls(self, f"delegate|{name}|{iso(self._now())}", i07_disputes.ACTOR, "people_43", events)
                people = self.ports.people
                a = ports.call("people_43", "delegate_active", (name,), lambda: people.delegate_active(name),
                               DelegateAnswer(False))
            except Unavailable:
                a = DelegateAnswer(False)
            if not (a.available and a.active):
                self.founder_refused("disputes/outcome", f"delegate {name} not confirmed by People (43)")
                raise FounderRefused("People (43) does not confirm this delegate (stand-in: Andre only)")
