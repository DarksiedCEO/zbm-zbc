"""
Finance (31) service layer: state, record-first plumbing, rules, evidence. The operations live in the mixins
(``svc_books``, ``svc_payables``, ``svc_payees``, ``svc_payouts``, ``svc_recon``); this module assembles them.

Order of every state change (spec §A, §E; ADR 0006 decisions 4-5 / ADR 0007 decision 3 pattern), without exception:
  1. every port call is recorded on the ledger first (``crossing_<port>_requested``, ids and an argument hash only),
     and — for every route that calls another department or a rail — made BEFORE the service lock is taken
     (``Gather``; the AEGIS N16-1 lesson), so an unrelated request never waits on a remote answer;
  2. the operation's own ledger events are recorded (deterministic ``fin-<abbrev>-<40 hex>`` ids);
  3. ONE local-log line holding all of the operation's records (journal entries included) is anchored on the
     ledger (``local_log_appended``), then appended (fsynced);
  4. only then is it applied to memory and answered.
A failure at 1-3 raises ``Unavailable`` (HTTP 503): nothing was posted, released or approved. State is
event-sourced from the log. One lock serializes every state change; the payout release holds a per-batch mutex
around its rail calls (made outside the service lock) so a concurrent second release cannot submit anything.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import chart as C
import money as M
import reasons as R
import rules as RU
from clock import Clock, SystemClock, iso, parse_iso
from config import Settings
from errors import Conflict, FinError, Invalid, NotFound, Unavailable
from intelligences import i01_journal as J
from intelligences import i08_treasury as T
from intelligences import i10_evidence_audit as i10
from ledger import LedgerConflict, LedgerQueryFailed, LedgerRecordError, Recorder, canonical, derived_id, payload_sha256
from models import ID_RE
from ports import Ports
from store import RecordLog, StoreWriteError
from textguard import injection_rules_in

IDEMPOTENCY_WINDOW = timedelta(minutes=15)
IDEMPOTENCY_MAX = 200_000
EVIDENCE = i10.ACTOR
LA = ZoneInfo("America/Los_Angeles")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SUBJECT_BAD = __import__("re").compile(r"[^A-Za-z0-9._:-]")
COLLECTIONS = ("rc_proposals", "rate_cards", "profiles", "client_profiles", "payees", "callbacks", "tax", "handoffs",
               "payables", "batches", "items", "exceptions", "recon_runs", "breaks", "invoices", "receipts", "disputes",
               "refunds", "treasury_ops", "controls", "close", "locks", "tax_readiness", "job_runs", "rail_events",
               "integrity_checks", "clawbacks", "offboarding")


def rid(prefix: str, *parts: Any) -> str:
    """``fin-<prefix>-`` + 26 Crockford base32 characters, derived deterministically from the record's identity
    (retries are idempotent; not time-sortable — the V&I ADR 0007 choice 5)."""
    d = hashlib.sha256(canonical(list(parts)).encode("utf-8", "surrogatepass")).digest()
    n = int.from_bytes(d[:17], "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[n & 31])
        n >>= 5
    return f"fin-{prefix}-" + "".join(out)


def sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8", "surrogatepass")).hexdigest()


def sha_text(s: str) -> str:
    return hashlib.sha256(str(s).encode("utf-8", "surrogatepass")).hexdigest()


def facts_sha256(facts: Any) -> str:
    """What the thin clients compute over what they sent: sorted keys, compact separators, ASCII escapes."""
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8", "surrogatepass")).hexdigest()


class Refused(FinError):
    """409 — the action is not allowed now; the body carries reason items and reason lines."""

    status_code = 409

    def __init__(self, message: str, reasons: list[dict], **body):
        super().__init__(message, reasons=reasons, reason_lines=R.lines(reasons), **body)


class InvalidReasons(FinError):
    """422 — a domain rule refuses this input (journal invariant, banned word, surcharge, contact mismatch)."""

    status_code = 422

    def __init__(self, message: str, reasons: list[dict], **body):
        super().__init__(message, reasons=reasons, reason_lines=R.lines(reasons), **body)


class PostingRefused(Exception):
    def __init__(self, status: int, reasons: list[dict]):
        super().__init__(R.lines(reasons)[0] if reasons else "refused")
        self.status = status
        self.reasons = reasons


class Gather:
    """Port calls of one operation, made OUTSIDE the service lock. Each crossing is recorded on the ledger first
    (ids and an argument hash only); an exception from a port is its fallback answer (text dropped)."""

    def __init__(self, svc: "FinanceService", op_id: str, actor: str, subject: str):
        self.svc, self.op_id, self.actor, self.subject = svc, op_id, actor, subject[:128]
        self.events: list[str] = []
        self.memo: dict = {}

    def call(self, port: str, action: str, args: tuple, fn: Callable, fallback: Any) -> Any:
        k = (port, action, canonical(list(args)))
        if k in self.memo:
            return self.memo[k]
        eid = derived_id("x", self.op_id, port, action, list(args))
        with self.svc._xlock:
            self.svc._record(eid, f"crossing_{port}_requested", self.actor, self.subject,
                             {"port": port, "action": action, "args_sha256": sha(list(args)),
                              "op_sha256": sha_text(self.op_id)}, f"Request to {port}: {action}")
        if eid not in self.events:
            self.events.append(eid)
        try:
            ans = fn()
        except Exception:  # noqa: BLE001 - a port that raises is unavailable, never a pass; its text is dropped
            ans = fallback
        self.memo[k] = ans
        return ans


class Op:
    """One operation: its ledger events and its local-log records (journal entries included)."""

    def __init__(self, svc: "FinanceService", op_id: str, actor: str, subject: str, gather: Optional[Gather] = None):
        self.svc, self.op_id, self.actor, self.subject = svc, op_id, actor, subject[:128]
        self.events: list[str] = list(gather.events) if gather else []
        self.ops: list[tuple[str, dict]] = []
        self.staged_entries: list[dict] = []
        self.staged: dict[str, dict[str, dict]] = {}

    def record(self, event_id: str, event_type: str, actor: str, subject: str, payload: dict, summary: str) -> str:
        self.svc._record(event_id, event_type, actor, subject[:128], payload, summary)
        if event_id not in self.events:
            self.events.append(event_id)
        return event_id

    def put(self, coll: str, key: str, rec: dict) -> dict:
        assert coll in COLLECTIONS, coll
        self.ops.append(("put", {"coll": coll, "id": key, "rec": rec}))
        self.staged.setdefault(coll, {})[key] = rec
        return rec

    def get(self, coll: str, key: str) -> Optional[dict]:
        s = self.staged.get(coll, {})
        return s[key] if key in s else self.svc.db[coll].get(key)

    def add(self, kind: str, rec: dict) -> dict:
        self.ops.append((kind, rec))
        return rec


class FinanceService:
    def __init__(self, settings: Settings, recorder: Recorder, log: RecordLog, seed_bytes: bytes,
                 expected_seed_sha256: str, ports: Optional[Ports] = None, clock: Optional[Clock] = None,
                 pinned_sha256: Optional[str] = None):
        self.cfg = settings
        self.recorder = recorder
        self.log = log
        self.ports = ports or Ports()
        self.clock = clock or SystemClock()
        self.lock = threading.RLock()
        self._xlock = threading.Lock()
        self.release_locks: dict[str, threading.Lock] = {}
        seed_sha = hashlib.sha256(seed_bytes).hexdigest()
        if seed_sha != expected_seed_sha256:
            raise RuntimeError(f"rules seed SHA-256 {seed_sha} does not match the expected {expected_seed_sha256}; "
                               "refusing to start (the seed must be the generated file, unchanged)")
        self.seed_sha = seed_sha
        self.rules_pinned = seed_sha == (pinned_sha256 or expected_seed_sha256)
        self.seed_rows = RU.load_seed(seed_bytes)
        self.seed_by_id = {r["rule_id"]: r for r in self.seed_rows}
        # state (event-sourced)
        self.versions: list[RU.Version] = []
        self.proposals: dict[str, dict] = {}
        self.db: dict[str, dict[str, dict]] = {c: {} for c in COLLECTIONS}
        self.entries: list[dict] = []
        self.entries_by_id: dict[str, dict] = {}
        self.entry_keys: dict[tuple[str, str], str] = {}
        self.heads: dict[str, Optional[str]] = {}
        self.balances: dict = {}
        self.pay_by_sub: dict[str, str] = {}
        self.pay_by_cert: dict[str, str] = {}
        self.idem: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
        self._ledger_conflict = False
        self.instance_id = secrets.token_hex(8)
        self.reconcile_mode = settings.reconcile_mode
        self.reconcile_required: list[str] = []
        self._reconciling = False
        self._replaying = True
        for rec in self.log.iter_records():
            for kind, r in rec["data"].get("ops", []):
                self._apply(kind, r)
        self._replaying = False
        problem = J.verify_chain(self.entries)
        if problem:
            raise RuntimeError(f"refusing to start: {problem}")
        if not self.log.in_memory:
            try:
                a = self.assess_log()
            except LedgerQueryFailed as exc:
                raise RuntimeError(f"refusing to start: the local log cannot be verified against the evidence "
                                   f"ledger ({exc})") from None
            if a.fatal or (a.voidable and not self.reconcile_mode):
                hint = ("" if a.fatal or not a.voidable else
                        " -- only Andre can void these: start with FIN_RECONCILE_MODE=1 and POST /fin/v1/reconcile "
                        "(README, 'Reconciling the local log with the ledger')")
                raise RuntimeError("refusing to start: " + "; ".join(a.problems) + hint)
            self.reconcile_required = list(a.voidable)
        if settings.unmatched_tin_policy == "withhold_24" and not self.counsel_verified("FIN-CQ-06"):
            raise RuntimeError("FIN_UNMATCHED_TIN_POLICY=withhold_24 needs counsel row FIN-CQ-06 verified (spec C.6); "
                               "refusing to start")
        if self.reconcile_mode:
            return
        try:
            self.ensure_seed_proposal()
        except Unavailable:
            pass
        if not self.log.in_memory and len(self.log):
            try:
                self._write_lease()
            except Unavailable as exc:
                raise RuntimeError(f"refusing to start: the instance lease could not be recorded ({exc.reason})") from None

    # ================================================================== plumbing

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _today_la(self) -> date:
        return self._now().astimezone(LA).date()

    def _record(self, event_id, event_type, actor, subject, payload, summary) -> str:
        self._ledger_conflict = False
        if self.reconcile_mode and not self._reconciling:
            raise Unavailable("reconcile mode (FIN_RECONCILE_MODE=1): only Andre's POST /fin/v1/reconcile is answered; "
                              "restart without it once the log is reconciled", ledger_write="not_recorded")
        subject = _SUBJECT_BAD.sub("-", str(subject))[:128] or "finance"
        try:
            return self.recorder.record(event_id, event_type, actor, subject, payload, summary)
        except LedgerRecordError as exc:
            self._ledger_conflict = isinstance(exc, LedgerConflict)
            raise Unavailable(f"evidence ledger write failed ({type(exc).__name__}); nothing took effect",
                              ledger_write="unknown" if exc.took_effect != False else "not_recorded") from None  # noqa: E712

    def _commit(self, op: Op, after_anchor: Optional[Callable] = None) -> None:
        if not op.ops:
            return
        data = {"ops": [[k, r] for k, r in op.ops], "ledger_event_ids": list(op.events),
                "rules_version": self.rules_version, "anchored": True}
        rec, line = self.log.prepare(op.ops[0][0], iso(self._now()), data)
        line_sha = hashlib.sha256(line).hexdigest()
        epoch = self.log.epoch or line_sha[:16]
        self._record(i10.anchor_id(epoch, rec["seq"], line_sha), i10.ANCHOR_TYPE, EVIDENCE, i10.LOG_SUBJECT,
                     {"epoch": epoch, "seq": rec["seq"], "line_sha256": line_sha, "kind": rec["kind"]},
                     f"Local log line {rec['seq']} ({rec['kind']}) anchored")
        if after_anchor is not None:
            after_anchor()
        try:
            self.log.append_prepared(rec, line)
        except StoreWriteError as exc:
            if rec["kind"] == "decision" and self.log.path:
                try:
                    with open(f"{self.log.path}.unwritten-{rec['seq']}", "wb") as fh:
                        fh.write(line + b"\n")
                except OSError:
                    pass
            raise Unavailable(f"local store write failed ({exc}); nothing took effect") from None
        for kind, r in op.ops:
            self._apply(kind, r)

    def _apply(self, kind: str, r: dict) -> None:
        if kind == "put":
            self.db[r["coll"]][r["id"]] = r["rec"]
            if r["coll"] == "payables":
                self.pay_by_sub[r["rec"]["submission_id"]] = r["id"]
                self.pay_by_cert[r["rec"]["certification_id"]] = r["id"]
        elif kind == "journal":
            self.entries.append(r)
            self.entries_by_id[r["entry_id"]] = r
            self.entry_keys[(r["entity"], r["idempotency_key"])] = r["entry_id"]
            self.heads[r["entity"]] = r["entry_sha256"]
            J.apply_balances(self.balances, r)
        elif kind == "idem":
            key = (r["principal"], r["request_id"])
            self.idem[key] = {"h": r["h"], "at": parse_iso(r["at"]), "response": r["response"]}
            self.idem.move_to_end(key)
            while len(self.idem) > IDEMPOTENCY_MAX:
                self.idem.popitem(last=False)
        elif kind == "proposal":
            self.proposals[r["proposal_id"]] = {**r, "status": "open", "decided_at": None}
        elif kind == "decision":
            for d in r["decisions"]:
                p = self.proposals[d["proposal_id"]]
                p.update(status="approved" if d["decision"] == "approve" else "rejected", decided_at=r["decided_at"])
            v = r.get("version")
            if v:
                self.versions.append(RU.Version(v["version"], v["created_at"], v["approved_by"], tuple(v["proposal_ids"]),
                                                v["rows_sha256"], v["prev_version_sha256"], tuple(v["rows"])))
        # lease, reconcile, founder_refused, injection, audit: evidence only

    # --- idempotency (15 min window; different body -> 409; stored answer persisted with the operation) --------------

    def _idem(self, principal: str, request_id: str, route: str, body: Any) -> tuple[tuple, str, Optional[dict]]:
        if not isinstance(request_id, str) or not ID_RE.fullmatch(request_id):
            raise Invalid("request_id must be 1-128 characters of [A-Za-z0-9._:-]")
        key = (principal, request_id)
        h = sha({"route": route, "body": body})
        ent = self.idem.get(key)
        if ent is not None:
            if ent["h"] != h:
                raise Conflict("request_id already used with a different body")
            if self._now() - ent["at"] > IDEMPOTENCY_WINDOW:
                raise Conflict("request_id reused (first used more than 15 minutes ago)")
        return key, h, ent

    def _idem_add(self, op: Op, key: tuple, h: str, response: dict) -> dict:
        """Stage the stored answer with the operation (so a retry after a restart gets it too)."""
        op.add("idem", {"principal": key[0], "request_id": key[1], "h": h, "at": iso(self._now()), "response": response})
        return response

    def _idem_mem(self, key: tuple, h: str, response: dict) -> dict:
        self.idem[key] = {"h": h, "at": self._now(), "response": response}
        self.idem.move_to_end(key)
        while len(self.idem) > IDEMPOTENCY_MAX:
            self.idem.popitem(last=False)
        return response

    # --- journal posting (intelligence 1 + the treasury check of intelligence 8) --------------------------------------

    def _staged_balances(self, op: Op) -> dict:
        b = dict(self.balances)
        for e in op.staged_entries:
            J.apply_balances(b, e)
        return b

    def _find_entry(self, op: Optional[Op], entity: str, key: str) -> Optional[dict]:
        if op is not None:
            for e in op.staged_entries:
                if e["entity"] == entity and e["idempotency_key"] == key:
                    return e
        eid = self.entry_keys.get((entity, key))
        return self.entries_by_id.get(eid) if eid else None

    def _post(self, op: Op, entity: str, lines: list[dict], memo: str, source: dict, key: str,
              approval_ref: Optional[str] = None, reverses: Optional[str] = None,
              effective_date: Optional[date] = None, actor: str = J.ACTOR) -> dict:
        """Validate and stage one journal entry (idempotent per (entity, key)). Raises PostingRefused."""
        existing = self._find_entry(op, entity, key)
        if existing is not None:
            return existing
        eff = (effective_date or self._today_la()).isoformat()
        entry_id = rid("je", entity, key)
        heads = [e["entry_sha256"] for e in op.staged_entries if e["entity"] == entity]
        head = heads[-1] if heads else self.heads.get(entity)
        eid = derived_id("je", entity, key)
        entry = J.build(entry_id, entity, eff, iso(self._now()), lines, memo, source, key, head, eid, reverses,
                        approval_ref)
        by_id = dict(self.entries_by_id)
        by_id.update({e["entry_id"]: e for e in op.staged_entries})
        problems = J.validate(entry, self._locked_periods(op), by_id)
        if problems:
            raise PostingRefused(422, problems)
        ok, before, after = T.posting_allowed(self._staged_balances(op), entry)
        if not ok:
            raise PostingRefused(409, [R.item("TREASURY_BREACH", f"posting {memo} would leave the restricted pool "
                                                                 f"{M.sfmt(after['gap'])} short of creator/client "
                                                                 "liabilities")])
        d_tot, _ = J.totals(entry)
        op.record(eid, "journal_entry_posted", actor, entry_id,
                  {"entry_id": entry_id, "entity": entity, "flow": memo, "total": M.fmt(d_tot),
                   "entry_sha256": entry["entry_sha256"], "lines": len(entry["lines"]),
                   "source_kind": source.get("kind"), "source_id": source.get("id")},
                  f"Journal {entity} {memo}: {M.fmt(d_tot)} ({len(entry['lines'])} lines)")
        if entity == "zbc" and after["gap"] < 0 and memo in T.FACT_FLOWS:
            op.record(derived_id("tb", entry_id), "treasury_breach", T.ACTOR, entry_id,
                      {"entry_id": entry_id, "gap": M.sfmt(after["gap"]), "flow": memo},
                      f"Treasury invariant broken by {memo}: gap {M.sfmt(after['gap'])} (FC-03 red)")
        op.staged_entries.append(entry)
        op.add("journal", entry)
        return entry

    def _locked_periods(self, op: Optional[Op] = None) -> set:
        out = {tuple(k.split("|")) for k, v in self.db["locks"].items() if v.get("locked")}
        if op is not None:
            out |= {tuple(k.split("|")) for k, v in op.staged.get("locks", {}).items() if v.get("locked")}
        return out

    def bal(self, account: str, sub: Optional[str] = None, entity: str = "zbc", op: Optional[Op] = None) -> Decimal:
        return J.account_balance(self._staged_balances(op) if op else self.balances, entity, account, sub)

    # --- log vs ledger (AEGIS N14-4 / N15-1 / N15-2 / N16-7) ----------------------------------------------------------

    def assess_log(self) -> i10.Assessment:
        client = self.recorder.client
        if not hasattr(client, "entries"):
            raise LedgerQueryFailed("this ledger client cannot read entries")
        entries = client.entries()
        shas = self.log.line_shas()
        lines, referenced, leases, reconciles, metas = [], set(), [], [], []
        for rec, s in zip(self.log.iter_records(), shas):
            d = rec["data"]
            lines.append((rec["seq"], s, bool(d.get("anchored"))))
            referenced.update(d.get("ledger_event_ids") or [])
            for kind, r in d.get("ops", []):
                if kind == "lease":
                    leases.append((rec["seq"], r.get("instance_id"), r.get("lease_event_id")))
                elif kind == "reconcile":
                    reconciles.append((rec["seq"], r.get("payload"), r.get("reconcile_event_id"), d.get("rules_version")))
                elif kind == "decision" and r.get("version"):
                    metas.append(r["version"])
        return i10.assess(entries, self.log.epoch, lines, referenced, self.rules_version or 0,
                          strict=not self.log.in_memory, local_rulings=set(), local_leases=leases, reconciles=reconciles,
                          local_versions=i10.local_version_events(self.log.epoch, metas))

    def integrity(self, record: bool = False) -> dict:
        """FC-04: the local chain verifies, the journal chain verifies, every local line is anchored, and the ledger's
        own /ledger/verify passes."""
        with self.lock:
            problems: list[str] = []
            try:
                a = self.assess_log()
                problems += a.problems
            except LedgerQueryFailed as exc:
                problems.append(f"ledger unreadable: {exc}")
            if not self.log.verify():
                problems.append("local log hash chain does not verify")
            jp = J.verify_chain(self.entries)
            if jp:
                problems.append(jp)
            try:
                if not self.recorder.client.verify():
                    problems.append("GET /ledger/verify is not valid")
            except Exception:  # noqa: BLE001
                problems.append("GET /ledger/verify could not be read")
            out = {"status": "red" if problems else "green", "problems": problems, "log_lines": len(self.log),
                   "journal_entries": len(self.entries), "rules_version": self.rules_version,
                   "checked_at": iso(self._now())}
            if record and not self.reconcile_mode:
                op = Op(self, f"integrity|{iso(self._now())}|{len(self.log)}", EVIDENCE, "integrity")
                cid = rid("int", op.op_id)
                op.record(derived_id("ctl", cid), "control_result_recorded", EVIDENCE, "FC-04",
                          {"control": "FC-04", "status": out["status"], "problems": len(problems)},
                          f"FC-04 journal integrity: {out['status']}")
                op.put("integrity_checks", cid, {"check_id": cid, "seq": len(self.db["integrity_checks"]) + 1,
                                                 **{k: out[k] for k in ("status", "checked_at")},
                                                 "problems": [p[:200] for p in problems]})
                self._commit(op)
            return out

    def _write_lease(self) -> None:
        with self.lock:
            n = len(self.log)
            head = self.log.line_shas()[-1]
            eid = i10.lease_id(self.log.epoch, self.instance_id, n, head)
            op = Op(self, f"lease|{self.instance_id}", EVIDENCE, i10.LOG_SUBJECT)
            op.record(eid, i10.LEASE_TYPE, EVIDENCE, i10.LOG_SUBJECT,
                      {"instance_id": self.instance_id, "epoch": self.log.epoch, "head_seq": n, "head_sha256": head},
                      f"Finance instance lease at log line {n}")
            op.add("lease", {"instance_id": self.instance_id, "lease_event_id": eid, "head_seq": n, "head_sha256": head})
            self._commit(op)

    def reconcile_plan(self) -> dict:
        with self.lock:
            a = self.assess_log()
            shas = self.log.line_shas()
            return {"epoch": self.log.epoch, "head_seq": len(shas), "head_sha256": shas[-1] if shas else None,
                    "rules_version": self.rules_version, "fatal": a.fatal, "problems": a.voidable,
                    "voidable": {"lines": sorted(a.void_lines), "event_ids": sorted(a.void_event_ids)},
                    "reconcile_mode": self.reconcile_mode}

    def reconcile(self, request_id: str, head_sha256: str, void_lines: list[int], void_event_ids: list[str]) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, "reconcile",
                                     {"head": head_sha256, "lines": void_lines, "ids": void_event_ids})
            if ent:
                return ent["response"]
            plan = self.reconcile_plan()
            if plan["fatal"]:
                raise Conflict("cannot reconcile: " + "; ".join(plan["fatal"]))
            if head_sha256 != plan["head_sha256"]:
                raise Conflict("the local log head moved since you read the plan; read GET /fin/v1/reconcile again")
            if not plan["voidable"]["event_ids"]:
                raise Conflict("nothing to reconcile: the local log matches the ledger")
            if sorted(set(void_lines)) != plan["voidable"]["lines"] or \
                    sorted(set(void_event_ids)) != plan["voidable"]["event_ids"]:
                raise Conflict("the void list is not exactly what the ledger shows now; nothing was voided",
                               expected=plan["voidable"])
            payload = i10.reconcile_payload(plan["epoch"], self.rules_version, plan["head_seq"], plan["head_sha256"],
                                            void_lines, void_event_ids)
            eid = i10.reconcile_id(plan["epoch"], plan["head_seq"], payload)
            self._reconciling = True
            try:
                op = Op(self, f"reconcile|{eid}", "andre", i10.LOG_SUBJECT)
                op.record(eid, i10.RECONCILE_TYPE, "andre", i10.LOG_SUBJECT, payload,
                          f"Andre reconciled the local log at line {plan['head_seq']}: "
                          f"{len(payload['void_event_ids'])} ledger event(s) declared void")
                op.add("reconcile", {"payload": payload, "reconcile_event_id": eid, "request_id": request_id})
                self._commit(op)
            finally:
                self._reconciling = False
            left = self.assess_log()
            self.reconcile_required = list(left.voidable)
            resp = {"reconcile_event_id": eid, "voided": payload["void_event_ids"], "void_lines": payload["void_lines"],
                    "remaining_problems": left.problems, "restart_required": self.reconcile_mode,
                    "ledger_event_ids": [eid], "request_id": request_id}
            return self._idem_mem(key, h, resp)

    # ================================================================== rules (§B.12)

    @property
    def current(self) -> Optional[RU.Version]:
        return self.versions[-1] if self.versions else None

    @property
    def rules_version(self) -> Optional[int]:
        return self.current.version if self.current else None

    def rules(self) -> dict[str, dict]:
        return self.current.by_id() if self.current else self.seed_by_id

    def counsel_verified(self, cq: str) -> bool:
        row = (self.current.by_id() if self.current else {}).get(cq)
        return bool(row and row["status"] == "verified")

    def _rules_reasons(self) -> list[dict]:
        if self.current is None:
            return [R.item("RULES_NOT_IN_FORCE", "no Andre-approved Finance rule version is in force")]
        return []

    def require_rules(self) -> None:
        rs = self._rules_reasons()
        if rs:
            raise Refused("no Andre-approved Finance rule version is in force (FIN-00)", rs)

    def ensure_seed_proposal(self) -> None:
        with self.lock:
            if any(p["kind"] == "seed" for p in self.proposals.values()):
                return
            pid = rid("prop", "seed", self.seed_sha)
            p = RU.seed_proposal(self.seed_rows, self.seed_sha, iso(self._now()), pid)
            op = Op(self, pid, "andre", f"proposal:{pid}")
            op.record(derived_id("prop", pid, p["content_sha256"]), "rules_proposal_created", EVIDENCE,
                      f"proposal:{pid}", {"proposal_id": pid, "kind": "seed", "content_sha256": p["content_sha256"],
                                          "seed_sha256": self.seed_sha, "rows": len(self.seed_rows)},
                      f"Finance rules seed loaded ({len(self.seed_rows)} rules); seed proposal created for Andre")
            op.add("proposal", p)
            self._commit(op)

    def rules_view(self) -> dict:
        self.ensure_seed_proposal()
        with self.lock:
            v = self.current
            open_ = sorted((p for p in self.proposals.values() if p["status"] == "open"), key=lambda p: p["created_at"])
            return {"rules_version": v.version if v else None, "rules_sha256": v.rows_sha256 if v else None,
                    "rules_pinned": self.rules_pinned, "seed_sha256": self.seed_sha,
                    "rules": [dict(r, in_force=True) for r in v.rows] if v else [],
                    "versions": [{**x.meta, "version_sha256": x.version_sha256} for x in self.versions],
                    "open_proposals": open_}

    def create_rule_proposal(self, request_id: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, "rules/proposals", body)
            if ent:
                return ent["response"]
            pid = rid("prop", "andre", request_id)
            p = RU.build_proposal(body["kind"], body.get("target_id"), body.get("proposed_row"),
                                  self.current.by_id() if self.current else None, "andre", iso(self._now()), pid)
            op = Op(self, pid, "andre", f"proposal:{pid}")
            op.record(derived_id("prop", pid, p["content_sha256"]), "rules_proposal_created", "andre", f"proposal:{pid}",
                      {"proposal_id": pid, "kind": p["kind"], "target_id": p["target_id"],
                       "content_sha256": p["content_sha256"], "weakening": p["weakening"]},
                      f"Rules proposal created: {p['kind']} {p['target_id']}")
            self._injection(op, {"row": body.get("proposed_row")})
            op.add("proposal", p)
            resp = self._idem_add(op, key, h, {"proposal": p, "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def decide_rules(self, request_id: str, decisions: list[dict]) -> dict:
        self.ensure_seed_proposal()
        with self.lock:
            key, h, ent = self._idem("andre", request_id, "rules/decisions", decisions)
            if ent:
                return ent["response"]
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
            new_rows = RU.apply(list(self.current.rows) if self.current else None, approvals) if approvals else None
            unacked = [p["proposal_id"] for d, p in chosen if d["decision"] == "approve" and p["weakening"]
                       and d.get("acknowledge_weakening") is not True]
            if unacked:
                why = "; ".join(f"{pid}: {', '.join(self.proposals[pid]['weakening_reasons'])}" for pid in unacked)
                raise Invalid(f"approval weakens the rules ({why}); approving needs acknowledge_weakening: true after "
                              "reviewing the diff. Nothing was applied")
            now = self._now()
            op = Op(self, f"decide|{request_id}", "andre", "rules")
            for d, p in chosen:
                op.record(derived_id("pd", p["proposal_id"], p["content_sha256"], d["decision"]), "rules_proposal_decided",
                          "andre", f"proposal:{p['proposal_id']}",
                          {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": d["decision"],
                           "note_sha256": sha(d.get("note")), "weakening": p["weakening"],
                           "acknowledged_weakening": d.get("acknowledge_weakening") is True},
                          f"Andre {d['decision']}d rules proposal {p['proposal_id']} ({p['kind']})")
            version_meta = None
            publish = None
            if new_rows is not None:
                n = (self.rules_version or 0) + 1
                prev = self.current.version_sha256 if self.current else None
                meta = {"version": n, "created_at": iso(now), "approved_by": "andre",
                        "proposal_ids": [p["proposal_id"] for p in approvals], "rows_sha256": RU.rows_sha256(new_rows),
                        "prev_version_sha256": prev}
                epoch = self.log.epoch or "0" * 16
                vid = i10.version_event_id(epoch, n, meta["rows_sha256"], prev)
                vpayload = {k: meta[k] for k in ("version", "rows_sha256", "prev_version_sha256", "proposal_ids")}
                publish = lambda: self._record(vid, i10.VERSION_TYPE, "andre", f"rules:v{n}", vpayload,  # noqa: E731
                                               f"Finance rules version {n} published ({len(new_rows)} rules)")
                op.events.append(vid)
                version_meta = {**meta, "rows": new_rows}
            op.add("decision", {"request_id": request_id, "decided_at": iso(now), "version": version_meta,
                                "decisions": [{"proposal_id": d["proposal_id"], "decision": d["decision"],
                                               "note": d.get("note"),
                                               "acknowledged_weakening": d.get("acknowledge_weakening") is True}
                                              for d, _ in chosen]})
            resp_version = (self.rules_version or 0) + 1 if new_rows is not None else self.rules_version
            resp = {"decided": len(chosen), "approved": len(approvals), "rules_version": resp_version,
                    "rules_sha256": version_meta["rows_sha256"] if version_meta else
                    (self.current.rows_sha256 if self.current else None), "ledger_event_ids": op.events,
                    "request_id": request_id}
            self._idem_add(op, key, h, resp)
            self._commit(op, after_anchor=publish)
            return resp

    def founder_refused(self, route: str, reason: str) -> None:
        with self.lock:
            if self.reconcile_mode:
                return
            op = Op(self, f"far|{route}|{iso(self._now())}|{len(self.log)}", EVIDENCE, "founder_gate")
            eid = self.recorder.try_record(derived_id("far", op.op_id), "founder_approval_refused", EVIDENCE,
                                           "founder_gate", {"route": route[:64], "reason_sha256": sha_text(reason)},
                                           "Andre approval refused")
            if eid:
                op.events.append(eid)
            op.add("founder_refused", {"route": route[:64], "reason": reason[:200]})
            try:
                self._commit(op)
            except Unavailable:
                pass

    def _injection(self, op: Op, obj: Any) -> None:
        found = injection_rules_in(obj)
        if found:
            op.record(derived_id("inj", op.op_id, len(op.events)), "injection_text_ignored", EVIDENCE, op.subject[:128],
                      {"rules": found, "count": len(found), "op_sha256": sha_text(op.op_id)},
                      f"Instruction-like text in caller data ignored ({len(found)} pattern(s)); outcome unaffected")
            op.add("injection", {"op_sha256": sha_text(op.op_id), "rules": found})

    # ================================================================== Compliance rows the rules rely on (§D.3)

    def _rows_reasons(self, g: Gather, oids: tuple, rule: str) -> list[dict]:
        out = []
        cmp_ = self.ports.compliance
        from ports import RegisterRow
        for oid in sorted(set(oids)):
            ans = g.call("compliance_38", "row", (oid,), lambda o=oid: cmp_.row(o), RegisterRow(False, oid))
            if not isinstance(ans, RegisterRow) or not ans.available or ans.obligation_id != oid:
                out.append(R.item("DEPENDENCY_UNAVAILABLE:compliance_38", f"Compliance row {oid} unavailable",
                                  rule=rule, obligation_id=oid))
                continue
            status = ans.effective_status
            if status == "verified" and ans.expires_at:
                try:
                    if date.fromisoformat(ans.expires_at[:10]) < self._now().date():
                        status = "expired"
                except ValueError:
                    status = "unknown"
            if status != "verified":
                out.append(R.item("RULE_NOT_IN_FORCE", f"Compliance row {oid} is {status}", rule=rule, obligation_id=oid))
        return out

    def _counsel_reasons(self, *cqs: str) -> list[dict]:
        return [R.item("COUNSEL_UNVERIFIED", f"counsel/CPA row {cq} is not verified (no approved memo)",
                       obligation_id=cq) for cq in cqs if not self.counsel_verified(cq)]

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
                out.append(i10.export_entry(rec))
                if len(out) >= i10.PAGE:
                    break
            more = last < len(self.log)
            eid = self._record(derived_id("aud", principal, since, until, cursor, len(self.log)), "audit_export_issued",
                               EVIDENCE, "audit_export", {"cursor": cursor, "count": len(out),
                                                          "next_cursor": last if more else None},
                               f"Audit export page served ({len(out)} records)")
            return {"records": out, "next_cursor": last if more else None, "ledger_event_id": eid,
                    "rules_version": self.rules_version}

    def health(self) -> dict:
        return {"status": "ok", "service": "finance-py", "rules_version": self.rules_version,
                "in_memory": self.log.in_memory, "rules_pinned": self.rules_pinned, "production": self.rules_pinned,
                "entities": list(C.ENTITIES), "reconcile_mode": self.reconcile_mode,
                "reconcile_required": bool(self.reconcile_required)}


from svc_books import BooksMixin  # noqa: E402
from svc_payables import PayablesMixin  # noqa: E402
from svc_payees import PayeesMixin  # noqa: E402
from svc_payouts import PayoutsMixin  # noqa: E402
from svc_recon import ReconMixin  # noqa: E402


class Service(BooksMixin, PayablesMixin, PayeesMixin, PayoutsMixin, ReconMixin, FinanceService):
    """The assembled Finance service."""
