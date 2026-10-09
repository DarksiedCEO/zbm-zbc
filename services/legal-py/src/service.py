"""
Legal (37) service layer: state, record-first plumbing and every operation (Legal spec §A-§F).

Order of every state change (spec §A "recorded on the ledger before it takes effect"; ADR 0006 decisions 4-5 and
the verification-py pattern), without exception:
  1. every text the operation stores is written to the content-addressed blob store first (write-once; a blob is
     inert until a log line cites it);
  2. every port call is recorded on the ledger first (``crossing_<port>_requested``, ids and hashes only);
  3. the operation's own ledger events are recorded (deterministic ``lg-<abbrev>-<40 hex>`` ids);
  4. ONE local-log line holding all of the operation's records is anchored on the ledger, then appended (fsynced);
  5. only then is it applied to memory and answered.
A failure at 1-4 raises ``Unavailable`` (HTTP 503): nothing took effect. State is event-sourced from the log.
One lock serializes every operation; the only remote port (Compliance 38) is never called while it is held
(memo-intake proposals are delivered after the memo's commit, the verification-py N16-1 rule).

Nothing here produces legal advice: outputs are records, dates, statuses and codes; every rendered outbound
text and every template variable passes the advice-text guard (``advice.py``). Uploaded text is data: it is
hashed, stored as a blob and scanned for injection patterns (logged, never followed).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import reasons as R
import rules as RU
from advice import AdviceGuard
from bizdays import BusinessCalendar, HolidaysUnknown
from clock import Clock, SystemClock, iso, parse_iso
from config import Settings
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from intelligences import (i01_documents as i01, i02_playbooks as i02, i03_acceptance as i03,
                           i04_obligations as i04, i05_memo_intake as i05, i06_matters as i06, i07_takedowns as i07,
                           i08_filings as i08, i09_music_policy as i09, i10_evidence_audit as i10)
from ledger import DEPARTMENT, LedgerQueryFailed, LedgerRecordError, Recorder, canonical, derived_id, payload_sha256
from models import ID_RE
import answers as A
from ports import ComplianceRow, Delivery, EnvelopeAnswer, ProposalAnswer, Ports
from store import BlobStore, DataDirBusy, RecordLog, StoreWriteError
from textguard import injection_rules_in

IDEMPOTENCY_WINDOW = timedelta(minutes=15)
IDEMPOTENCY_MAX = 200_000
EVIDENCE = i10.ACTOR
JOBS = ("obligations", "filings", "holds-renotice", "retention", "proposal-delivery")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
DEFAULT_HOLD_SYSTEMS = ("email", "chat", "drive", "legal_store")
OWNER_CALLER = {"onboarding": "onboarding", "finance_31": "finance_31", "creative_production": "creative_production",
                "clipper_network": "clipper_network", "compliance_38": "compliance_38", "legal_37": "andre",
                "andre": "andre"}
ORIGIN_PORT = {"vi": "verification_integrity", "cn": "clipper_network", "finance": "finance_31"}
BLOB_CLASS = {"document": "contracts_and_esign_evidence", "variables": "contracts_and_esign_evidence",
              "certificate": "contracts_and_esign_evidence", "paper": "contracts_and_esign_evidence",
              "clause": "contracts_and_esign_evidence", "memo": "legal_memos", "excerpt": "legal_memos",
              "question": "legal_memos", "evidence": "acceptance_records", "notice": "holds",
              "outbound": "takedowns"}
PERIOD_RE = re.compile(r"^P([0-9]{1,3})(Y|M|D)$")
# collections that log records ``put`` into (kind -> (attribute, key field))
PUT_KINDS = {
    "document": ("documents", "doc_id"), "doc_version": ("doc_versions", "key"), "blob": ("blobs", "sha256"),
    "acceptance": ("acceptances", "acceptance_id"), "envelope": ("envelopes", "envelope_id"),
    "pb_proposal": ("pb_proposals", "proposal_id"), "playbook": ("playbooks", "key"),
    "obligation": ("obligations", "obligation_id"), "contract": ("contracts", "client_id"),
    "memo": ("memos", "memo_id"), "cproposal": ("cproposals", "cproposal_id"), "matter": ("matters", "matter_id"),
    "hold": ("holds", "hold_id"), "takedown": ("takedowns", "notice_id"), "filing": ("filings", "filing_id"),
    "ruling": ("rulings", "ruling_id"), "signoff": ("signoffs", "signoff_id"), "engagement": ("engagements", "counsel_ref"),
    "review": ("reviews", "review_id"), "package": ("packages", "package_id"), "outbound": ("outbound", "outbound_id"),
}


def rid(prefix: str, *parts: Any) -> str:
    """``lg-<prefix>-`` + 26 Crockford base32 characters, derived deterministically from the record's identity
    (retries are idempotent; not time-sortable)."""
    d = hashlib.sha256(canonical(list(parts)).encode("utf-8", "surrogatepass")).digest()
    n = int.from_bytes(d[:17], "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[n & 31])
        n >>= 5
    return f"lg-{prefix}-" + "".join(out)


def sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8", "surrogatepass")).hexdigest()


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def facts_sha256(facts: Any) -> str:
    """What a thin client computes over what it sent (sorted keys, compact separators)."""
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8", "surrogatepass")).hexdigest()


def b64decode(value: str, what: str) -> bytes:
    try:
        data = base64.b64decode("".join(value.split()), validate=True)
    except (binascii.Error, ValueError):
        raise Invalid(f"{what}: not valid base64") from None
    if not data or len(data) > BlobStore.MAX_BYTES:
        raise Invalid(f"{what}: 1 byte .. 5 MiB decoded")
    return data


class Refused(Conflict):
    """409 carrying reason items (every refusal cites a rule id, §0.1.6)."""


class RefusedInvalid(Invalid):
    """422 carrying reason items (e.g. ADVICE_TEXT_BLOCKED on a template variable)."""


class Op:
    """One operation: its blobs, ledger events and local-log records."""

    def __init__(self, svc: "LegalService", op_id: str, actor: str, subject: str):
        self.svc, self.op_id, self.actor, self.subject = svc, op_id, actor, subject
        self.events: list[str] = []
        self.ops: list[tuple[str, dict]] = []
        self.after: list[Callable[[], None]] = []
        self.idem: Optional[dict] = None
        self.evidence: list[dict] = []   # bug sweep E (R6-M1): typed evidence this op's line names, payload included

    def record(self, event_id: str, event_type: str, actor: str, subject: str, payload: dict, summary: str) -> str:
        self.svc._record(event_id, event_type, actor, subject[:128], payload, summary)
        self.events.append(event_id)
        return event_id

    def ev(self, abbrev: str, event_type: str, subject: str, payload: dict, summary: str, actor: Optional[str] = None) -> str:
        """Typed evidence, recorded FIRST (before the line is anchored). Bug sweep E (bizdev-py R6-M1): the payload
        carries ``rk`` (this op's request key) and ``seq`` (the log line it is meant for), the id is derived from the
        op, the type, the subject, the position AND the payload's SHA-256 (no time): a retry with the same payload
        dedupes on the ledger, a retry after the state changed gets a new id (never a lasting 409), and the line
        names the event with its payload (``data.evidence``) so ``GET /legal/v1/audit/evidence`` can tell
        ``committed`` from ``attempted``."""
        payload = self.svc._bind(payload, self.op_id)
        eid = derived_id(abbrev, self.op_id, event_type, subject, len(self.events), payload_sha256(payload))
        self.record(eid, event_type, actor or self.actor, subject, payload, summary)
        self.evidence.append({"event_id": eid, "event_type": event_type, "subject_id": subject[:128],
                              "payload": payload})
        return eid

    def add(self, kind: str, rec: dict) -> dict:
        self.ops.append((kind, rec))
        return rec

    def update(self, coll: str, key: str, fields: dict) -> None:
        self.ops.append(("update", {"coll": coll, "key": key, "fields": fields}))

    def blob(self, data: bytes, klass: str, subject_refs: list[str]) -> str:
        try:
            h = self.svc.blobs.put(data)
        except StoreWriteError as exc:
            raise Unavailable(f"blob store write failed ({exc}); nothing took effect") from None
        # AEGIS N17-14: a blob's subject refs (and retention classes) are a SET that grows with every writer --
        # the same bytes stored for a second document, acceptance or memo carry that subject too, so a hold on any
        # of them blocks retention. Nothing is ever dropped (no cap that could cut a held subject off).
        refs, klass_name = set(subject_refs), BLOB_CLASS[klass]
        for k, r in self.ops:                   # the same blob earlier in this operation
            if k == "blob" and r["sha256"] == h:
                r["subject_refs"] = sorted(set(r["subject_refs"]) | refs)
                r["classes"] = sorted(set(r.get("classes") or [r["class"]]) | {klass_name})
                return h
            if k == "update" and r["coll"] == "blobs" and r["key"] == h:
                r["fields"]["subject_refs"] = sorted(set(r["fields"]["subject_refs"]) | refs)
                r["fields"]["classes"] = sorted(set(r["fields"]["classes"]) | {klass_name})
                return h
        meta = self.svc.blob_meta.get(h)
        if meta is None or meta.get("deleted"):
            self.add("blob", {"sha256": h, "class": klass_name, "classes": [klass_name],
                              "subject_refs": sorted(refs), "size": len(data), "stored_at": iso(self.svc._now()),
                              "deleted": False})
        else:
            have_refs, have_cls = set(meta.get("subject_refs") or []), set(meta.get("classes") or [meta["class"]])
            if not refs <= have_refs or klass_name not in have_cls:
                self.update("blobs", h, {"subject_refs": sorted(have_refs | refs),
                                         "classes": sorted(have_cls | {klass_name}),
                                         "last_ref_at": iso(self.svc._now())})
        return h

    def call(self, port: str, action: str, args: tuple, fn: Callable, fallback: Any) -> Any:
        """A port call, recorded on the ledger first (ids and hashes only); an exception is the fallback; the
        answer is strictly type-checked before anything uses it (AEGIS N17-3)."""
        self.ev("x", f"crossing_{port}_requested", self.subject,
                {"port": port, "action": action, "args_sha256": sha(list(args)), "op": self.op_id},
                f"Request to {port}: {action}")
        try:
            ans = fn()
        except Exception:  # noqa: BLE001 - a port that raises is unavailable, never a pass; its text is dropped
            return fallback
        typ = EnvelopeAnswer if action == "create_envelope" else type(fallback)
        try:
            return A.check(ans, typ, fallback)
        except A.Malformed as exc:
            self.ev("xbad", "adapter_answer_refused", self.subject,
                    {"port": port, "action": action, "args_sha256": sha(list(args)), "problem": exc.kind},
                    f"Malformed answer from {port} ({action}) refused; the fail-closed answer is used")
            return fallback


@dataclass
class Seeds:
    rules: bytes
    documents: bytes
    questions: bytes
    retention: bytes
    topics: bytes
    advice: bytes
    holidays: bytes


class LegalService:
    def __init__(self, settings: Settings, recorder: Recorder, log: RecordLog, blobs: BlobStore, seeds: Seeds,
                 ports: Optional[Ports] = None, clock: Optional[Clock] = None, rules_pinned: bool = True,
                 lock_token: Optional[str] = None):
        # bug sweep E (E-5/F-3; service-py V5-L1 / round 5c / a5dd261 L4 pattern): one service instance per data
        # directory, also within one process. api.build_service claims BEFORE the log is built and passes the
        # claim's token; this instance adopts that claim only if the token IS the current claim (single use), and
        # gives it back on close() or a failed start.
        self.lock = threading.RLock()
        self.log = log
        self._closed = False
        self._evidence_lock = threading.Lock()     # /audit/evidence cache; never taken under self.lock
        self._evidence_cache: dict = {"n": 0, "epoch": None, "lines": [], "log": log}
        self._dir_lock = getattr(settings, "data_dir_lock", None)
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
            self._start(settings, recorder, log, blobs, seeds, ports, clock, rules_pinned)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Give the data directory back (a restart in the same process closes the old instance first). A closed
        instance is inert: its log refuses every write and it records nothing on the ledger. Taken under the
        service lock, so it never interleaves with a commit."""
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

    def _start(self, settings: Settings, recorder: Recorder, log: RecordLog, blobs: BlobStore, seeds: Seeds,
               ports: Optional[Ports], clock: Optional[Clock], rules_pinned: bool) -> None:
        self.cfg = settings
        self.recorder = recorder
        self.log = log
        self.blobs = blobs
        self.ports = ports or Ports()
        self.clock = clock or SystemClock()
        self.tz = ZoneInfo(settings.business_tz)
        self.seed_sha = sha_bytes(seeds.rules)
        self.rules_pinned = rules_pinned
        self.seed_rows = RU.load_seed(seeds.rules)
        self.seed_by_id = {r["rule_id"]: r for r in self.seed_rows}
        self.guard = AdviceGuard.load(seeds.advice)
        self.cal = BusinessCalendar(seeds.holidays)
        docs = json.loads(seeds.documents)
        self.data_sheet_prefixes = tuple(docs["data_sheet_prefixes"])
        # state (event-sourced)
        self.versions: list[RU.Version] = []
        self.proposals: dict[str, dict] = {}
        self.documents: dict[str, dict] = {d["doc_id"]: dict(d) for d in docs["documents"]}
        self.doc_versions: dict[str, dict] = {}
        self.blob_meta: dict[str, dict] = {}
        self.acceptances: dict[str, dict] = {}
        self.envelopes: dict[str, dict] = {}
        self.pb_proposals: dict[str, dict] = {}
        self.playbooks: dict[str, dict] = {}
        self.obligations: dict[str, dict] = {}
        self.contracts: dict[str, dict] = {}
        self.register: dict[str, dict] = {}
        for q in json.loads(seeds.questions)["rows"]:
            self.register[q["cq_id"]] = {**q, "status": "unverified", "memo_ids": [], "review_by": None,
                                         "compliance": None}
        self.memos: dict[str, dict] = {}
        self.cproposals: dict[str, dict] = {}
        self.matters: dict[str, dict] = {}
        self.holds: dict[str, dict] = {}
        self.takedowns: dict[str, dict] = {}
        self.filings: dict[str, dict] = {}
        self.rulings: dict[str, dict] = {}
        self.signoffs: dict[str, dict] = {}
        self.engagements: dict[str, dict] = {}
        self.reviews: dict[str, dict] = {}
        self.packages: dict[str, dict] = {}
        self.outbound: dict[str, dict] = {}
        self.retention: dict[str, dict] = {c["class"]: {**c, "status": "unverified", "memo_id": None,
                                                         "review_by": None}
                                           for c in json.loads(seeds.retention)["classes"]}
        self.topics: dict[str, dict] = {t["topic"]: {**t, "scope": None, "memo_id": None, "status": "unverified",
                                                     "review_by": None}
                                        for t in json.loads(seeds.topics)["topics"]}
        self.job_runs: dict[str, dict] = {}
        self.idem: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
        self.instance_id = secrets.token_hex(8)
        self.reconcile_mode = settings.reconcile_mode
        self.reconcile_required: list[str] = []
        self._reconciling = False
        self._replaying = True
        for rec in self.log.iter_records():
            for kind, r in rec["data"].get("ops", []):
                self._apply(kind, r)
            ide = rec["data"].get("idem")
            if ide:
                self._idem_load(ide)
        self._replaying = False
        missing = [h for h, m in self.blob_meta.items() if not m.get("deleted") and not self.blobs.exists(h)]
        if missing:
            raise RuntimeError(f"refusing to start: {len(missing)} blob(s) the log cites are missing from the blob "
                               "store (deleted or moved outside the retention job)")
        if not self.log.in_memory:
            try:
                a = self.assess_log()
            except LedgerQueryFailed as exc:
                raise RuntimeError(f"refusing to start: the local log cannot be verified against the evidence "
                                   f"ledger ({exc})") from None
            if a.fatal or (a.voidable and not self.reconcile_mode):
                hint = ("" if a.fatal or not a.voidable else
                        " -- only Andre can void these: start with LEGAL_RECONCILE_MODE=1 and POST /legal/v1/reconcile "
                        "(README, 'Reconciling the local log with the ledger')")
                raise RuntimeError("refusing to start: " + "; ".join(a.problems) + hint)
            self.reconcile_required = list(a.voidable)
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

    def _today(self) -> date:
        """Legal's calendar date (LEGAL_BUSINESS_TZ): receipt dates, due dates, filing windows."""
        return self._now().astimezone(self.tz).date()

    def _bind(self, payload: dict, rk: str) -> dict:
        """Bug sweep E (bizdev-py R6-M1): the evidence payload names its request key and the log line it is meant
        for (the next seq; the commit refuses if the prepared line's seq differs)."""
        return {**payload, "rk": rk, "seq": len(self.log) + 1}

    def _record(self, event_id, event_type, actor, subject, payload, summary) -> str:
        if self._closed:                   # bug sweep E: a closed instance records nothing
            raise Unavailable("this service instance is closed", ledger_write="not_recorded")
        if self.reconcile_mode and not self._reconciling:
            raise Unavailable("reconcile mode (LEGAL_RECONCILE_MODE=1): only Andre's POST /legal/v1/reconcile is "
                              "answered; restart without it once the log is reconciled", ledger_write="not_recorded")
        try:
            return self.recorder.record(event_id, event_type, actor, subject, payload, summary)
        except LedgerRecordError as exc:
            raise Unavailable(f"evidence ledger write failed ({type(exc).__name__}); nothing took effect",
                              ledger_write="unknown" if exc.took_effect != False else "not_recorded") from None  # noqa: E712

    def _commit(self, op: Op, after_anchor: Optional[Callable] = None) -> None:
        if not op.ops:
            return
        data = {"ops": [[k, r] for k, r in op.ops], "ledger_event_ids": list(op.events),
                "rules_version": self.rules_version, "anchored": True}
        if op.idem:
            data["idem"] = op.idem
        if op.evidence:                   # bug sweep E (R6-M1): the line names its typed evidence
            data["rk"] = op.op_id
            data["evidence"] = op.evidence
        rec, line = self.log.prepare(op.ops[0][0], iso(self._now()), data)
        if any(e["payload"].get("seq") != rec["seq"] for e in op.evidence):
            # the log moved after the evidence was recorded: never anchor a line whose evidence names another seq
            # (that evidence stays visible as ``attempted`` in /audit/evidence)
            raise Unavailable("the local log moved while this operation was recorded; nothing took effect, retry")
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
        if op.idem:
            self._idem_load(op.idem)
        for fn in op.after:
            fn()

    def _apply(self, kind: str, r: dict) -> None:
        if kind in PUT_KINDS:
            attr, key = PUT_KINDS[kind]
            coll = self.blob_meta if attr == "blobs" else getattr(self, attr)
            coll[r[key]] = json.loads(json.dumps(r))
        elif kind == "update":
            coll = self.blob_meta if r["coll"] == "blobs" else getattr(self, r["coll"])
            coll[r["key"]].update(json.loads(json.dumps(r["fields"])))
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
        elif kind == "job_run":
            self.job_runs[f"{r['job']}|{r['day']}"] = r
        # lease, reconcile, founder_refused, refusal, injection, advice_block: evidence only

    # --- idempotency (request_id + route + body; 15 minutes; survives a restart through the log)

    def _idem_load(self, ide: dict) -> None:
        key = (ide["principal"], ide["request_id"])
        self.idem[key] = {"h": ide["h"], "at": parse_iso(ide["at"]), "response": ide["response"]}
        self.idem.move_to_end(key)
        while len(self.idem) > IDEMPOTENCY_MAX:
            self.idem.popitem(last=False)

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

    def _answer(self, op: Op, key: tuple, h: str, response: dict, after_anchor=None) -> dict:
        """Commit ``op`` with the answer stored for idempotent replay, then return it."""
        response = {**response, "ledger_event_ids": list(op.events)}
        op.idem = {"principal": key[0], "request_id": key[1], "h": h, "response": response, "at": iso(self._now())}
        if not op.ops:
            op.add("idem_only", {"request_id": key[1]})
        self._commit(op, after_anchor)
        return response

    # --- refusals (recorded best-effort; a refusal stands whether or not it was recorded)

    def _refuse(self, event_type: str, subject: str, reasons: list[dict], status: int = 409, op_id: str = "",
                **extra) -> None:
        rec = None
        if not self.reconcile_mode:
            # bug sweep E (R6-M1): no time in the id -- the payload's hash (with rk and seq) is; a retry of a refusal
            # whose line was not written records the SAME event (the ledger answers 200)
            rk = f"refuse|{event_type}|{sha(op_id)}"
            payload = self._bind({"codes": R.codes(reasons), "rule_ids": sorted({r["rule_id"] for r in reasons}),
                                  "op_sha256": sha(op_id)}, rk)
            eid = self.recorder.try_record(derived_id("ref", event_type, subject, op_id, payload_sha256(payload)),
                                           event_type, EVIDENCE, subject[:128], payload,
                                           f"Refused: {', '.join(R.codes(reasons))[:200]}")
            if eid:
                op = Op(self, rk, EVIDENCE, subject)
                op.events.append(eid)
                op.evidence.append({"event_id": eid, "event_type": event_type, "subject_id": subject[:128],
                                    "payload": payload})
                op.add("refusal", {"event_type": event_type, "subject": subject[:128], "codes": R.codes(reasons),
                                   "at": iso(self._now())})
                try:
                    self._commit(op)
                    rec = eid
                except Unavailable:
                    pass
        body = {"reasons": reasons, "reason_lines": R.lines(reasons), "recorded": rec is not None,
                "refusal_event_id": rec, **extra}
        code = reasons[0]["code"] if reasons else "REFUSED"
        cls = RefusedInvalid if status == 422 else Refused
        raise cls(code, **body)

    def _require_rules(self) -> None:
        if self.current is None:
            r = R.rules_not_in_force()
            raise Refused("RULES_NOT_IN_FORCE", reasons=[r], reason_lines=R.lines([r]), recorded=False)

    def _injection(self, op: Op, obj: Any) -> list[str]:
        found = injection_rules_in(obj)
        if found:
            op.ev("inj", "injection_text_ignored", op.subject,
                  {"rules": found, "count": len(found), "op_sha256": sha(op.op_id)},
                  f"Instruction-like text in uploaded data ignored ({len(found)} pattern(s)); nothing changed",
                  actor=EVIDENCE)
            op.add("injection", {"op_sha256": sha(op.op_id), "rules": found})
        return found

    def _guard(self, texts: list[str], subject: str, what: str, op_id: str) -> None:
        """LG-01: refuse (422 ADVICE_TEXT_BLOCKED, recorded) when any outbound text reads as advice."""
        hits = self.guard.scan_all(t for t in texts if isinstance(t, str))
        if hits:
            self._refuse("advice_text_blocked", subject,
                         [R.item("ADVICE_TEXT_BLOCKED", f"{what} reads as advice to a third party "
                                 f"({', '.join(hits)}); nothing was produced or sent")], 422, op_id,
                         pattern_ids=hits)

    # --- log vs ledger (AEGIS N14-4 / N15-1 / N15-2 / N16-7)

    def _ledger_entries(self) -> list:
        client = self.recorder.client
        if not hasattr(client, "entries"):
            raise LedgerQueryFailed("this ledger client cannot read entries")
        return client.entries()

    def assess_log(self, entries: Optional[list] = None) -> i10.Assessment:
        if entries is None:
            entries = self._ledger_entries()
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

    INTEGRITY_SNAPSHOT_TRIES = 3

    def integrity(self) -> dict:
        """Bug sweep E (slow I/O under the service lock): the ledger read (an HTTP call, up to the client timeout)
        and the local chain re-verify (a full file read and hash) run OUTSIDE the service lock; only the in-memory
        assessment runs under it. The log length is snapshotted first and re-checked after: a commit in between would
        pair a newer log with an older ledger read (a false red), so the read is repeated (at most
        INTEGRITY_SNAPSHOT_TRIES times, then once under the lock, as before)."""
        for attempt in range(self.INTEGRITY_SNAPSHOT_TRIES + 1):
            locked = attempt == self.INTEGRITY_SNAPSHOT_TRIES
            with self.lock:
                if self._closed:
                    return {"status": "red", "problems": ["this service instance is closed"]}
                n0 = len(self.log)
                live = [h for h, m in self.blob_meta.items() if not m.get("deleted")]
                if locked:                       # the log kept moving: fall back to one read under the lock
                    return self._integrity_from(*self._integrity_reads(live))
            reads = self._integrity_reads(live)
            with self.lock:
                if len(self.log) == n0 and not self._closed:
                    return self._integrity_from(*reads)
        raise AssertionError("unreachable")

    def _integrity_reads(self, live: list[str]) -> tuple:
        """The slow reads: the ledger, the local chain, every live blob's bytes against its SHA-256."""
        try:
            entries = self._ledger_entries()
        except LedgerQueryFailed as exc:
            return None, f"ledger unreadable: {exc}", False, 0
        bad = sum(1 for h in live if self.blobs.get(h) is None)
        return entries, None, self.log.verify(), bad

    def _integrity_from(self, entries, unreadable: Optional[str], chain: bool, bad: int) -> dict:
        with self.lock:
            if unreadable is not None:
                return {"status": "red", "problems": [unreadable]}
            try:
                a = self.assess_log(entries)
            except LedgerQueryFailed as exc:
                return {"status": "red", "problems": [f"ledger unreadable: {exc}"]}
            problems = list(a.problems)
            if not chain:
                problems.append("local log hash chain does not verify")
            if self.log.fault:                   # bug sweep E: a failed write could not be cut back
                problems.append(f"LOCAL_LOG_WRITE_FAULT: {self.log.fault}")
            if bad:
                problems.append(f"{bad} blob(s) missing or not matching their SHA-256")
            return {"status": "red" if problems else "green", "problems": problems, "log_lines": len(self.log),
                    "rules_version": self.rules_version}

    def _write_lease(self) -> None:
        with self.lock:
            n = len(self.log)
            head = self.log.line_shas()[-1]
            eid = i10.lease_id(self.log.epoch, self.instance_id, n, head)
            op = Op(self, f"lease|{self.instance_id}", EVIDENCE, i10.LOG_SUBJECT)
            op.record(eid, i10.LEASE_TYPE, EVIDENCE, i10.LOG_SUBJECT,
                      {"instance_id": self.instance_id, "epoch": self.log.epoch, "head_seq": n, "head_sha256": head},
                      f"Legal instance lease at log line {n}")
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
                raise Conflict("the local log head moved since you read the plan; read GET /legal/v1/reconcile again")
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
            return {"reconcile_event_id": eid, "voided": payload["void_event_ids"], "void_lines": payload["void_lines"],
                    "remaining_problems": left.problems, "restart_required": self.reconcile_mode,
                    "ledger_event_ids": [eid]}

    # ================================================================== rules (§B.12)

    @property
    def current(self) -> Optional[RU.Version]:
        return self.versions[-1] if self.versions else None

    @property
    def rules_version(self) -> Optional[int]:
        return self.current.version if self.current else None

    def rules(self) -> dict[str, dict]:
        return self.current.by_id() if self.current else self.seed_by_id

    def _param(self, rule_id: str, key: str, default: Any) -> Any:
        return (self.rules().get(rule_id) or {}).get("parameters", {}).get(key, default)

    @staticmethod
    def param_problems(row: Optional[dict]) -> Optional[str]:
        """Parameters the code reads must stay inside their statutory / structural bounds (a proposal outside them
        is refused, whatever the acknowledgment)."""
        if not row:
            return None
        p, rid_ = row.get("parameters") or {}, row.get("rule_id")
        if rid_ == "LG-10":
            lo, hi = p.get("restore_min_business_days"), p.get("restore_max_business_days")
            if not (isinstance(lo, int) and isinstance(hi, int) and 10 <= lo <= hi <= 14):
                return "LG-10 restore window must stay within 10..14 business days (17 U.S.C. 512(g)(2))"
        if rid_ == "LG-11" and not (isinstance(p.get("dmca_designation_years"), int) and
                                    1 <= p["dmca_designation_years"] <= 3):
            return "LG-11 dmca_designation_years must be 1..3 (the designation expires after three years)"
        if rid_ == "LG-12":
            libs = p.get("verified_platform_libraries")
            if not (isinstance(libs, list) and all(x in ("tiktok", "youtube", "instagram", "x", "facebook") for x in libs)):
                return "LG-12 verified_platform_libraries must list known platforms"
        return None

    def ensure_seed_proposal(self) -> None:
        with self.lock:
            if any(p["kind"] == "seed" for p in self.proposals.values()):
                return
            pid = rid("prop", "seed", self.seed_sha)
            p = RU.seed_proposal(self.seed_rows, self.seed_sha, iso(self._now()), pid)
            op = Op(self, pid, EVIDENCE, f"proposal:{pid}")
            op.record(derived_id("prop", pid, p["content_sha256"]), "rules_proposal_created", EVIDENCE,
                      f"proposal:{pid}", {"proposal_id": pid, "kind": "seed", "content_sha256": p["content_sha256"],
                                          "seed_sha256": self.seed_sha, "rows": len(self.seed_rows)},
                      f"Legal rules seed loaded ({len(self.seed_rows)} rules); seed proposal created for Andre")
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
            problem = self.param_problems(p.get("proposed_row"))
            if problem:
                raise Invalid(problem)
            op = Op(self, pid, "andre", f"proposal:{pid}")
            op.record(derived_id("prop", pid, p["content_sha256"]), "rules_proposal_created", "andre", f"proposal:{pid}",
                      {"proposal_id": pid, "kind": p["kind"], "target_id": p["target_id"],
                       "content_sha256": p["content_sha256"], "weakening": p["weakening"]},
                      f"Rules proposal created: {p['kind']} {p['target_id']}")
            self._injection(op, {"row": body.get("proposed_row")})
            op.add("proposal", p)
            return self._answer(op, key, h, {"proposal": p})

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
                                               f"Legal rules version {n} published ({len(new_rows)} rules)")
                op.events.append(vid)
                version_meta = {**meta, "rows": new_rows}
            op.add("decision", {"request_id": request_id, "decided_at": iso(now), "version": version_meta,
                                "decisions": [{"proposal_id": d["proposal_id"], "decision": d["decision"],
                                               "note_sha256": sha(d.get("note")),
                                               "acknowledged_weakening": d.get("acknowledge_weakening") is True}
                                              for d, _ in chosen]})
            self._commit(op, after_anchor=publish)
            return {"decided": len(chosen), "approved": len(approvals), "rules_version": self.rules_version,
                    "rules_sha256": self.current.rows_sha256 if self.current else None, "ledger_event_ids": op.events}

    def founder_refused(self, route: str, reason: str) -> None:
        with self.lock:
            if self.reconcile_mode:
                return
            # bug sweep E (R6-M1): no time in the op or event id; the payload (with rk and seq) is hashed into it
            op = Op(self, f"far|{route[:64]}", EVIDENCE, "founder_gate")
            payload = self._bind({"route": route[:64], "reason_sha256": sha(reason)}, op.op_id)
            eid = self.recorder.try_record(derived_id("far", op.op_id, payload_sha256(payload)),
                                           "founder_approval_refused", EVIDENCE, "founder_gate", payload,
                                           "Andre approval refused")
            if eid:
                op.events.append(eid)
                op.evidence.append({"event_id": eid, "event_type": "founder_approval_refused",
                                    "subject_id": "founder_gate", "payload": payload})
                op.add("founder_refused", {"route": route[:64], "reason_sha256": sha(reason)})
                try:
                    self._commit(op)
                except Unavailable:
                    pass

    # ================================================================== counsel questions (mirror, §B.5)

    def canonical_cq(self, cq_id: str) -> Optional[str]:
        row = self.register.get(cq_id)
        if row is None:
            return None
        return row["alias_of"] or cq_id

    def cq_status(self, cq_id: str) -> str:
        c = self.canonical_cq(cq_id)
        if c is None:
            return "unverified"
        row = self.register[c]
        if row["status"] != "verified":
            return "unverified"
        if not row.get("review_by") or self._today() >= date.fromisoformat(row["review_by"]):
            return "expired"
        return "verified"

    def cq_verified(self, cq_id: str) -> bool:
        return self.cq_status(cq_id) == "verified"

    def cq_memo(self, cq_id: str) -> Optional[str]:
        c = self.canonical_cq(cq_id)
        ids = self.register[c]["memo_ids"] if c else []
        return ids[-1] if ids and self.cq_verified(cq_id) else None

    def register_row_view(self, cq_id: str) -> dict:
        row = self.register.get(cq_id)
        if row is None:
            raise NotFound("no such counsel-question row")
        c = self.canonical_cq(cq_id)
        return {**{k: row[k] for k in ("cq_id", "origin", "question", "why_counsel_only", "source_url", "blocks",
                                        "alias_of", "related")},
                "status": self.cq_status(cq_id), "memo_ids": list(self.register[c]["memo_ids"]),
                "review_by": self.register[c]["review_by"], "check": "counsel_memo",
                "compliance": self.register[c].get("compliance"), "rules_pinned": self.rules_pinned}

    def register_view(self) -> dict:
        with self.lock:
            return {"rows": [self.register_row_view(k) for k in sorted(self.register)], "rules_pinned": self.rules_pinned}

    def invalidate(self, request_id: str, target: str, source_ref: str, change_sha: str) -> dict:
        """Compliance may only tighten (§C.5): a mirror row, a retention class or a sign-off topic -> unverified."""
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("compliance_38", request_id, f"invalidate/{target}",
                                     {"s": source_ref, "c": change_sha})
            if ent:
                return ent["response"]
            if target.startswith("retention:"):
                coll, k = "retention", target.split(":", 1)[1]
                if k not in self.retention:
                    raise NotFound("no such retention class")
            elif target.startswith("signoff:"):
                coll, k = "topics", target.split(":", 1)[1]
                if k not in self.topics:
                    raise NotFound("no such sign-off topic")
            else:
                coll, k = "register", self.canonical_cq(target)
                if k is None:
                    raise NotFound("no such counsel-question row")
            op = Op(self, f"inv|{request_id}", "compliance_38", target)
            was = getattr(self, coll)[k]["status"]
            op.ev("inv", "register_row_invalidated", target,
                  {"target": target, "canonical": k, "was": was, "source_ref_sha256": sha(source_ref),
                   "detected_change_sha256": change_sha}, f"Compliance invalidated {target} (tighten only)",
                  actor="compliance_38")
            op.update(coll, k, {"status": "unverified"})
            return self._answer(op, key, h, {"target": target, "canonical": k, "status": "unverified", "was": was})

    # ================================================================== documents (§B.1, §C.1)

    def _doc(self, doc_id: str, create_entity: Optional[str] = None) -> dict:
        d = self.documents.get(doc_id)
        if d is None:
            raise NotFound("no such document")
        return d

    def _versions_of(self, doc_id: str) -> list[dict]:
        return [v for v in self.doc_versions.values() if v["doc_id"] == doc_id]

    def _current(self, doc_id: str) -> Optional[dict]:
        return i01.current(self._versions_of(doc_id), self._now())

    def _in_force(self, v: dict) -> bool:
        """Approved, effective and not past review_by at this instant (spec §B.2 "approved at accepted_at")."""
        return i01.current([v], self._now()) is not None

    def _template(self, doc_id: str) -> Optional[dict]:
        """The version a scheduler fill uses: the highest in-force approved version that is itself a template
        (declares template variables and is not a fill). A filled instance is never a template."""
        vs = [v for v in self._versions_of(doc_id) if v.get("template_variables") and not v.get("template_ref")
              and self._in_force(v)]
        return max(vs, key=lambda v: i01.vkey(v["version"]), default=None)

    def _version(self, doc_id: str, version: str) -> dict:
        v = self.doc_versions.get(f"{doc_id}@{version}")
        if v is None:
            raise NotFound("no such document version")
        return v

    def _review_label(self, v: dict) -> str:
        so = v.get("counsel_signoff")
        if so and so.get("memo_id"):
            return f"counsel_memo:{so['memo_id']}"
        if so and so.get("counsel_ref"):
            return f"counsel_countersigned:{so['counsel_ref']}"
        if v.get("template_ref"):
            return f"counsel_template:{v['doc_id']}@{v['template_ref']['version']}"
        return "unreviewed"

    def version_view(self, v: dict) -> dict:
        out = {k: v.get(k) for k in ("doc_id", "version", "entity", "sha256", "status", "clause_ids",
                                     "template_variables", "template_ref", "counsel_signoff", "approved_by",
                                     "approved_at", "effective_at", "review_by", "supersedes", "created_at", "created_by")}
        out["review_label"] = self._review_label(v)
        return out

    def document_view(self, doc_id: str) -> dict:
        with self.lock:
            d = self._doc(doc_id)
            cur = self._current(doc_id)
            vs = sorted(self._versions_of(doc_id), key=lambda v: i01.vkey(v["version"]))
            return {**{k: d[k] for k in ("doc_id", "title", "doc_type", "entities", "counsel_required")},
                    "current_version": cur["version"] if cur else None,
                    "versions": [self.version_view(v) for v in vs], "rules_pinned": self.rules_pinned}

    def version_get(self, doc_id: str, version: str) -> dict:
        with self.lock:
            self._doc(doc_id)
            return self.version_view(self._version(doc_id, version))

    def version_text(self, doc_id: str, version: str) -> dict:
        with self.lock:
            v = self._version(doc_id, version)
            data = self.blobs.get(v["sha256"])
            if data is None:
                raise NotFound("the text blob is not held (deleted by retention)")
            return {"doc_id": doc_id, "version": version, "sha256": v["sha256"], "text": data.decode("utf-8")}

    def current_answer(self, doc_id: str) -> dict:
        """§E.1 protocol answer for compliance-py / clipper-network-py ``current_version``."""
        with self.lock:
            base = {"doc_id": doc_id, "available": False, "current_version": None, "doc_sha256": None,
                    "effective_at": None, "review_by": None, "rules_pinned": self.rules_pinned}
            if self.current is None:
                return {**base, "reason": R.line(R.rules_not_in_force())}
            if doc_id not in self.documents:
                return {**base, "reason": R.line(R.item("DOCUMENT_UNKNOWN", f"no document {doc_id[:60]} in the register"))}
            cur = self._current(doc_id)
            if cur is None:
                return {**base, "available": True,
                        "reason": R.line(R.item("NO_CURRENT_VERSION", f"{doc_id} has no approved, effective, "
                                                "unexpired version"))}
            return {**base, "available": True, "current_version": cur["version"], "doc_sha256": cur["sha256"],
                    "effective_at": cur["effective_at"], "review_by": cur["review_by"], "reason": "current"}

    def create_version(self, principal: str, request_id: str, doc_id: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem(principal, request_id, f"documents/{doc_id}/versions", body)
            if ent:
                return ent["response"]
            now = self._now()
            d = self.documents.get(doc_id)
            new_doc = None
            if d is None:
                if principal != "andre" or not doc_id.startswith(self.data_sheet_prefixes):
                    raise NotFound("no such document (data sheets soi_<entity> / fbn_<dba> are created by Andre's "
                                   "first upload)")
                new_doc = {"doc_id": doc_id, "title": f"Data sheet {doc_id}", "doc_type": "data_sheet",
                           "entities": [body["entity"]], "counsel_required": False, "approval_blocked_by": [],
                           "required_clause_ids": [], "esign_allowed": False}
                d = new_doc
            if principal != "andre" and (body.get("text") is not None or body.get("template_variables")
                                         or body.get("bump") != "minor"):
                # AEGIS N17-5: only Andre (counsel's draft) uploads a document; every other caller may only FILL
                raise Forbidden("only Andre's token uploads a document version; a caller may only fill the current "
                                "counsel-approved template (variables + party_ref)")
            if body["entity"] not in d["entities"]:
                raise Invalid(f"entity {body['entity']} is not an entity of {doc_id} (ZBC and ZBM are separate)")
            existing = self._versions_of(doc_id)
            # AEGIS N17-5: the number is Legal's, monotonic per document; no caller can choose or burn it
            version = i01.next_version([v["version"] for v in existing], body.get("bump") or "minor")
            if version is None:
                raise Conflict(f"{doc_id} has no version number left (9999.9999 reached)")
            body = {**body, "version": version}
            op = Op(self, f"ver|{principal}|{request_id}", i01.ACTOR if principal != "andre" else "andre", f"doc:{doc_id}")
            date_vars: dict[str, str] = {}
            template_ref = None
            variables_sha = None
            if principal == "andre":
                if body.get("text") is None or body.get("variables") is not None:
                    raise Invalid("Andre's upload carries the document text (counsel's draft) and no variables")
                text = body["text"]
                try:
                    schema = i01.validate_schema(body.get("template_variables") or {})
                except ValueError as exc:
                    raise Invalid(str(exc)) from None
                if i01.placeholders(text) != set(schema) or (i01.has_unresolved(text) and not schema):
                    raise Invalid("every {{placeholder}} in the text needs a template_variables entry and vice versa")
                clause_ids = [dict(c) for c in body.get("clause_ids") or []]
                self._injection(op, {"text": text})
            else:
                if body.get("text") is not None or body.get("variables") is None or body.get("template_variables"):
                    raise Invalid("a template fill carries variables only (the template is the current version)")
                tpl = self._template(doc_id)
                if tpl is None:
                    raise Refused("NO_APPROVED_TEMPLATE", reasons=[R.item("NO_APPROVED_TEMPLATE", f"{doc_id} has no "
                                  "in-force counsel-approved template version to fill")], recorded=False)
                if body["entity"] != tpl["entity"]:
                    raise Invalid("a fill keeps the template's entity")
                if not body.get("party_ref"):
                    raise Invalid("a fill names the party it is for (party_ref): only that party can accept it")
                schema = tpl["template_variables"] or {}
                try:
                    values = i01.check_variables(schema, body["variables"])
                except ValueError as exc:
                    raise Invalid(str(exc)) from None
                self._guard([v for n, v in values.items() if schema[n]["type"] == "string"], f"doc:{doc_id}",
                            "a template variable", op.op_id)
                self._injection(op, {"variables": body["variables"]})
                tdata = self.blobs.get(tpl["sha256"])
                if tdata is None:
                    raise Unavailable("the template's text blob is not readable")
                text = i01.render(tdata.decode("utf-8"), values)
                date_vars = {n: v for n, v in values.items() if schema[n]["type"] == "date"}
                variables_sha = op.blob(canonical(body["variables"]).encode("utf-8"), "variables",
                                        [f"doc:{doc_id}", f"doc:{doc_id}@{version}", body["party_ref"]])
                template_ref = {"version": tpl["version"], "sha256": tpl["sha256"]}
                clause_ids = [dict(c) for c in tpl["clause_ids"]]
                schema = {}
            raw = text.encode("utf-8", "surrogatepass")
            if len(raw) > BlobStore.MAX_BYTES:
                raise Invalid("document text larger than 5 MiB")
            unresolved = i01.has_unresolved(text)
            if template_ref is not None and unresolved:
                raise Invalid("the filled text still carries an unresolved {{field}}")
            party_ref = body.get("party_ref")
            h_text = op.blob(raw, "document", [f"doc:{doc_id}", f"doc:{doc_id}@{version}"]
                             + ([party_ref] if party_ref else []))
            if new_doc is not None:
                op.add("document", new_doc)
            v = {"key": f"{doc_id}@{body['version']}", "doc_id": doc_id, "version": body["version"],
                 "entity": body["entity"], "sha256": h_text, "template_variables": schema, "clause_ids": clause_ids,
                 "status": "draft", "counsel_signoff": None, "approved_by": None, "approved_at": None,
                 "effective_at": None, "review_by": None, "supersedes": body.get("supersedes"),
                 "template_ref": template_ref, "variables_sha256": variables_sha, "date_variables": date_vars,
                 "party_ref": party_ref, "unresolved_fields": unresolved,
                 "created_at": iso(now), "created_by": principal}
            op.ev("doc", "document_version_drafted", f"doc:{doc_id}",
                  {"doc_id": doc_id, "version": body["version"], "sha256": h_text, "entity": body["entity"],
                   "template_ref": template_ref, "clause_ids": sorted(c["clause_id"] for c in clause_ids),
                   "party_ref_sha256": sha(party_ref) if party_ref else None, "unresolved_fields": unresolved},
                  f"{doc_id} version {body['version']} drafted ({'fill' if template_ref else 'upload'})")
            op.add("doc_version", v)
            return self._answer(op, key, h, {"doc_id": doc_id, "version": body["version"], "sha256": h_text,
                                             "status": "draft", "review_label": self._review_label(v)})

    def counsel_review(self, request_id: str, doc_id: str, version: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, f"counsel-review/{doc_id}/{version}", body)
            if ent:
                return ent["response"]
            v = self._version(doc_id, version)
            if v["status"] not in ("draft", "counsel_review"):
                raise Conflict(f"version is {v['status']}")
            op = Op(self, f"cr|{request_id}", "andre", f"doc:{doc_id}")
            qsha = op.blob(body["question_text"].encode("utf-8"), "question", [f"doc:{doc_id}"])
            esha = op.blob(body["proposed_edit_text"].encode("utf-8"), "question", [f"doc:{doc_id}"]) \
                if body.get("proposed_edit_text") else None
            self._injection(op, {"q": body["question_text"], "e": body.get("proposed_edit_text"), "f": body.get("facts")})
            cur = self._current(doc_id)
            package = {"doc_id": doc_id, "version": version, "doc_sha256": v["sha256"],
                       "current_sha256": cur["sha256"] if cur else None,
                       "detected_change_sha256": body.get("detected_change_sha256"), "proposed_edit_sha256": esha,
                       "question_sha256": qsha, "facts_sha256": facts_sha256(body.get("facts") or {})}
            pkg_id = rid("pkg", "doc", request_id)
            ans = op.call("counsel_channel", "deliver", (pkg_id,), lambda: self.ports.counsel.deliver(package),
                          Delivery(False, "counsel channel raised"))
            delivered = isinstance(ans, Delivery) and ans.delivered is True
            op.ev("crp", "counsel_review_packaged", f"doc:{doc_id}", {**package, "package_id": pkg_id,
                                                                       "delivered": delivered},
                  f"{doc_id} {version} packaged for counsel ({'delivered' if delivered else 'not delivered'})")
            op.add("package", {"package_id": pkg_id, "kind": "document", **package, "delivered": delivered,
                               "at": iso(self._now())})
            op.update("doc_versions", v["key"], {"status": "counsel_review"})
            reasons = [] if delivered else [R.item("COUNSEL_NOT_DELIVERED", "no counsel channel is wired: the "
                                                   "package is recorded, not delivered")]
            return self._answer(op, key, h, {"package_id": pkg_id, "delivered": delivered, "status": "counsel_review",
                                             "reasons": reasons, "reason_lines": R.lines(reasons)})

    def counsel_signoff(self, request_id: str, doc_id: str, version: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, f"counsel-signoff/{doc_id}/{version}", body)
            if ent:
                return ent["response"]
            d = self._doc(doc_id)
            v = self._version(doc_id, version)
            subject = f"doc:{doc_id}"
            if v["status"] not in ("draft", "counsel_review"):
                raise Conflict(f"version is {v['status']}")
            if body["doc_sha256"] != v["sha256"]:
                self._refuse("counsel_signoff_refused", subject, [R.item("HASH_MISMATCH", "the sign-off names a "
                             "different document hash than this version's")], op_id=request_id)
            op = Op(self, f"so|{request_id}", "andre", subject)
            if doc_id == "engagement_letter":
                if not body.get("countersignature_b64") or body.get("memo_id"):
                    raise Invalid("the engagement letter's counsel record is counsel's countersigned copy "
                                  "(countersignature_b64), not a memo")
                cs = op.blob(b64decode(body["countersignature_b64"], "countersignature_b64"), "certificate", [subject])
                signoff = {"memo_id": None, "memo_sha256": None, "counsel_ref": body["counsel_ref"],
                           "countersignature_sha256": cs, "signed_on": body["signed_on"], "doc_sha256": v["sha256"]}
            else:
                memo = self.memos.get(body.get("memo_id") or "")
                reasons = []
                if memo is None:
                    reasons.append(R.item("MEMO_UNKNOWN", "the sign-off names no filed counsel memo"))
                else:
                    if body.get("memo_sha256") != memo["memo_sha256"]:
                        reasons.append(R.item("HASH_MISMATCH", "memo_sha256 differs from the filed memo"))
                    if memo["counsel_ref"] != body["counsel_ref"]:
                        reasons.append(R.item("MEMO_DOES_NOT_CITE", "the memo came from a different counsel_ref"))
                    if f"{doc_id}@{version}" not in memo["cites"]["doc_versions"]:
                        reasons.append(R.item("MEMO_DOES_NOT_CITE", f"the memo does not cite {doc_id}@{version}"))
                if reasons:
                    self._refuse("counsel_signoff_refused", subject, reasons, op_id=request_id)
                signoff = {"memo_id": memo["memo_id"], "memo_sha256": memo["memo_sha256"],
                           "counsel_ref": body["counsel_ref"], "countersignature_sha256": None,
                           "signed_on": body["signed_on"], "doc_sha256": v["sha256"]}
            op.ev("cso", "counsel_signoff_recorded", subject, {"doc_id": doc_id, "version": version, **signoff},
                  f"Counsel sign-off recorded for {doc_id} {version}")
            op.update("doc_versions", v["key"], {"counsel_signoff": signoff})
            _ = d
            return self._answer(op, key, h, {"doc_id": doc_id, "version": version, "counsel_signoff": signoff})

    def decide_version(self, request_id: str, doc_id: str, version: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, f"decision/{doc_id}/{version}", body)
            if ent:
                return ent["response"]
            d = self._doc(doc_id)
            v = self._version(doc_id, version)
            subject = f"doc:{doc_id}"
            now = self._now()
            if body["version_sha256"] != v["sha256"]:
                self._refuse("document_approval_refused", subject, [R.item("HASH_MISMATCH", "Andre's decision names a "
                             "different content hash than this version's")], op_id=request_id)
            op = Op(self, f"dd|{request_id}", "andre", subject)
            if body["decision"] == "approve":
                reasons = i01.approval_reasons(d, v, self.cq_verified, now)
                if reasons:
                    missing = any(r["code"] == "COUNSEL_RECORD_MISSING" for r in reasons)
                    self._refuse("approval_refused_no_counsel" if missing else "document_approval_refused", subject,
                                 reasons, op_id=request_id)
                eff = now
                if body.get("effective_at"):
                    eff = max(now, parse_iso(body["effective_at"]))
                fields = {"status": "approved", "approved_by": "andre", "approved_at": iso(now),
                          "effective_at": iso(eff), "review_by": iso(now + timedelta(days=self.cfg.memo_review_days))}
                op.ev("apv", "document_version_approved", subject,
                      {"doc_id": doc_id, "version": version, "sha256": v["sha256"], "effective_at": fields["effective_at"],
                       "review_by": fields["review_by"], "counsel_signoff_memo_id": (v.get("counsel_signoff") or {}).get("memo_id"),
                       "counsel_ref": (v.get("counsel_signoff") or {}).get("counsel_ref")},
                      f"Andre approved {doc_id} {version}")
                if doc_id == "engagement_letter":
                    so = v["counsel_signoff"]
                    op.add("engagement", {"counsel_ref": so["counsel_ref"], "doc_id": doc_id, "version": version,
                                          "doc_sha256": v["sha256"], "countersignature_sha256": so["countersignature_sha256"],
                                          "approved_at": iso(now)})
            elif body["decision"] == "retire":
                if v["status"] != "approved":
                    raise Conflict("only an approved version can be retired")
                fields = {"status": "retired"}
                op.ev("ret", "document_version_retired", subject, {"doc_id": doc_id, "version": version,
                                                                    "sha256": v["sha256"]},
                      f"Andre retired {doc_id} {version}")
            else:
                if v["status"] not in ("draft", "counsel_review"):
                    raise Conflict("only a draft can be withdrawn")
                fields = {"status": "withdrawn"}
                op.ev("wdr", "document_version_withdrawn", subject, {"doc_id": doc_id, "version": version},
                      f"Andre withdrew {doc_id} {version}")
            op.update("doc_versions", v["key"], fields)
            return self._answer(op, key, h, {"doc_id": doc_id, "version": version, **fields})

    # ================================================================== acceptances (§B.2, §C.3)

    def acceptance_view(self, a: dict) -> dict:
        return {k: a.get(k) for k in ("acceptance_id", "doc_id", "version", "doc_sha256", "accepted_at", "method",
                                      "evidence_sufficient", "party_ref")} | {"rules_pinned": self.rules_pinned}

    def get_acceptance(self, acceptance_id: str) -> dict:
        with self.lock:
            a = self.acceptances.get(acceptance_id)
            if a is None:
                raise NotFound("no such acceptance")
            return self.acceptance_view(a)

    def record_acceptance(self, principal: str, request_id: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem(principal, request_id, "acceptances", body)
            if ent:
                return ent["response"]
            now = self._now()
            subject = body["party_ref"]
            v = self.doc_versions.get(f"{body['doc_id']}@{body['version']}")
            reasons = []
            if v is None:
                reasons.append(R.item("DOCUMENT_UNKNOWN", f"no {body['doc_id']} version {body['version']}"))
            else:
                if body["doc_sha256"] != v["sha256"]:
                    reasons.append(R.item("ACCEPTANCE_HASH_MISMATCH", "doc_sha256 differs from the register's hash"))
                if not self._in_force(v):
                    reasons.append(R.item("VERSION_NOT_IN_FORCE", f"{body['doc_id']} {body['version']} is not "
                                          "approved and in force at accepted_at"))
                reasons += self._instance_reasons(v, body["party_ref"])
            if body["presented_sha256"] != body["doc_sha256"]:
                reasons.append(R.item("PRESENTED_TEXT_MISMATCH", "the text shown is not the document exactly"))
            if body["affirmative_act"] is not True:
                reasons.append(R.item("AFFIRMATIVE_ACT_MISSING", "acceptance needs the unticked box ticked"))
            if self.cfg.esign_consent_required and body["doc_id"] == "clipper_agreement" and not body.get("esign_consent"):
                reasons.append(R.item("ESIGN_CONSENT_MISSING", "clipper agreements need the ESIGN 7001(c) consent block "
                                      "(pending CQ-19)", cq_id="CQ-19"))
            ev = body.get("evidence_ref")
            ev_bytes = None
            if ev and ev.get("content_b64"):
                ev_bytes = b64decode(ev["content_b64"], "evidence_ref.content_b64")
                if sha_bytes(ev_bytes) != ev["sha256"]:
                    reasons.append(R.item("ACCEPTANCE_HASH_MISMATCH", "evidence_ref content does not match its sha256"))
            if reasons:
                self._refuse("acceptance_refused", subject, reasons, op_id=request_id)
            aid = rid("acc", principal, request_id)
            op = Op(self, f"acc|{aid}", i03.ACTOR, subject)
            blob = op.blob(ev_bytes, "evidence", [subject, f"acceptance:{aid}"]) if ev_bytes else None
            rec = {"acceptance_id": aid, "party_ref": body["party_ref"], "signer_identity_ref": body["signer_identity_ref"],
                   "doc_id": body["doc_id"], "version": body["version"], "doc_sha256": body["doc_sha256"],
                   "presented_sha256": body["presented_sha256"], "accepted_at": iso(now), "method": body["method"],
                   "presentation": body["presentation"], "affirmative_act": True,
                   "esign_consent": body.get("esign_consent"),
                   "evidence_ref": {"kind": ev["kind"], "sha256": ev["sha256"], "blob": blob} if ev else None,
                   "entry_by": principal}
            rec["evidence_sufficient"] = i03.clickwrap_sufficient(rec, self.cq_verified("CQ-19"),
                                                                  self.cfg.esign_consent_required,
                                                                  not self._instance_reasons(v, rec["party_ref"]))
            op.ev("acc", "acceptance_recorded", subject,
                  {"acceptance_id": aid, "doc_id": rec["doc_id"], "version": rec["version"], "doc_sha256": rec["doc_sha256"],
                   "method": rec["method"], "evidence_sufficient": rec["evidence_sufficient"],
                   "signer_identity_ref_sha256": sha(rec["signer_identity_ref"])},
                  f"Acceptance of {rec['doc_id']} {rec['version']} recorded")
            op.add("acceptance", rec)
            obligations = self._bind_obligations(op, rec, v) if rec["evidence_sufficient"] else []
            reasons = [] if rec["evidence_sufficient"] else [
                R.item("EVIDENCE_INSUFFICIENT", "clickwrap evidence is insufficient until counsel answers CQ-19",
                       cq_id="CQ-19")]
            return self._answer(op, key, h, {**self.acceptance_view(rec), "obligation_ids": obligations,
                                             "reasons": reasons, "reason_lines": R.lines(reasons)})

    def _instance_reasons(self, v: dict, party_ref: str) -> list[dict]:
        """AEGIS N17-4: a party accepts only a document INSTANCE bound to it. A template (declared variables, or any
        ``{{...}}`` left in the text) is never acceptable; a per-party fill (or a version Andre uploaded for one
        party) is acceptable only by that party. A version bound to no party and carrying no placeholder is a
        standard form (the same text for everyone, e.g. the clipper agreement) and any party may accept it."""
        out = []
        if v.get("template_variables") or v.get("unresolved_fields"):
            out.append(R.item("TEMPLATE_NOT_ACCEPTABLE", f"{v['doc_id']} {v['version']} is a template with unfilled "
                              "fields: only a filled instance bound to the party can be accepted"))
        elif v.get("unresolved_fields") is None and v.get("sha256"):
            data = self.blobs.get(v["sha256"])          # versions recorded before fix 18 carry no flag: read the text
            if data is None or i01.has_unresolved(data.decode("utf-8", "replace")):
                out.append(R.item("TEMPLATE_NOT_ACCEPTABLE", f"{v['doc_id']} {v['version']} has unfilled fields (or "
                                  "its text is not held): not acceptable"))
        bound = v.get("party_ref")
        if v.get("template_ref") and not bound:
            out.append(R.item("INSTANCE_NOT_BOUND", f"{v['doc_id']} {v['version']} is a fill bound to no party "
                              "(recorded before fix 18): not acceptable"))
        elif bound and bound != party_ref:
            out.append(R.item("INSTANCE_NOT_BOUND", f"{v['doc_id']} {v['version']} is bound to another party"))
        return out

    def _playbook_for(self, doc_type: str) -> Optional[dict]:
        pbs = [p for p in self.playbooks.values() if p["doc_type"] == doc_type and p["status"] == "approved"]
        return max(pbs, key=lambda p: i01.vkey(p["version"]), default=None)

    def _bind_obligations(self, op: Op, acc: dict, v: dict) -> list[str]:
        """LG-06: descriptors of the executed version's clauses, bound to its date variables and accepted_at."""
        doc = self.documents[v["doc_id"]]
        pb = self._playbook_for(doc["doc_type"])
        if pb is None:
            return []
        clauses = {c["clause_id"]: c for c in pb["clauses"]}
        fields = dict(v.get("date_variables") or {})
        fields["accepted_at"] = parse_iso(acc["accepted_at"]).astimezone(self.tz).date().isoformat()
        if v.get("effective_at"):
            fields["effective_at"] = parse_iso(v["effective_at"]).astimezone(self.tz).date().isoformat()
        out = []
        for use in v["clause_ids"]:
            c = clauses.get(use["clause_id"])
            if c is None:
                continue
            for i, dsc in enumerate(c["obligations"]):
                due, unbound = i04.bind_due(dsc["due_rule"], fields, self.cal)
                oid = rid("obl", acc["acceptance_id"], use["clause_id"], i)
                rec = {"obligation_id": oid, "source": {"doc_id": v["doc_id"], "version": v["version"],
                                                        "acceptance_id": acc["acceptance_id"], "clause_id": use["clause_id"]},
                       "party": dsc["party"], "counterparty_ref": acc["party_ref"], "obligation_code": dsc["code"],
                       "due": due, "due_rule": dsc["due_rule"], "unbound": unbound,
                       "alert_lead_days": dsc["lead_days"], "owner_department": dsc["owner"], "status": "open",
                       "done_evidence": None, "waived_by": None, "memo_id": None, "created_at": iso(self._now())}
                op.ev("obl", "obligation_created", f"obligation:{oid}",
                      {"obligation_id": oid, "code": dsc["code"], "due": due, "owner": dsc["owner"],
                       "acceptance_id": acc["acceptance_id"], "clause_id": use["clause_id"], "unbound": unbound},
                      f"Obligation {dsc['code']} created from {v['doc_id']} {v['version']}", actor=i04.ACTOR)
                op.add("obligation", rec)
                out.append(oid)
        return out

    def create_envelope(self, request_id: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, "envelopes", body)
            if ent:
                return ent["response"]
            d = self._doc(body["doc_id"])
            v = self._version(body["doc_id"], body["version"])
            if not d.get("esign_allowed"):
                raise Refused("ESIGN_DOC_TYPE_EXCLUDED", reasons=[R.item("ESIGN_DOC_TYPE_EXCLUDED", f"{d['doc_type']} "
                              "is outside ordinary commercial contracts (UETA 1633.3 exclusions UNVERIFIED)")],
                              recorded=False)
            if not self._in_force(v):
                raise Refused("VERSION_NOT_IN_FORCE", reasons=[R.item("VERSION_NOT_IN_FORCE", "only an approved, "
                              "in-force version goes out for signature")], recorded=False)
            inst = self._instance_reasons(v, body["party_ref"])
            if inst:
                raise Refused(inst[0]["code"], reasons=inst, recorded=False)
            op = Op(self, f"env|{request_id}", i03.ACTOR, body["party_ref"])
            esign = self.ports.esign
            ans = op.call("esign_provider", "create_envelope", (body["doc_id"], body["version"], v["sha256"]),
                          lambda: esign.create_envelope(body["doc_id"], body["version"], v["sha256"],
                                                        list(body["signer_refs"])), None)
            if ans is None or not getattr(ans, "available", False) or not getattr(ans, "envelope_id", None):
                reasons = [R.item("ESIGN_UNAVAILABLE", "no e-sign provider is wired: no envelope was created")]
                op.add("envelope_unavailable", {"request_id": request_id})
                return self._answer(op, key, h, {"created": False, "envelope_id": None, "reasons": reasons,
                                                 "reason_lines": R.lines(reasons)})
            rec = {"envelope_id": ans.envelope_id, "doc_id": body["doc_id"], "version": body["version"],
                   "doc_sha256": v["sha256"], "party_ref": body["party_ref"],
                   "signer_refs_sha256": sha(list(body["signer_refs"])), "status": "sent", "created_at": iso(self._now())}
            op.ev("env", "envelope_created", body["party_ref"], {k: rec[k] for k in ("envelope_id", "doc_id", "version",
                                                                                      "doc_sha256")},
                  f"Envelope created for {body['doc_id']} {body['version']}")
            op.add("envelope", rec)
            return self._answer(op, key, h, {"created": True, "envelope_id": rec["envelope_id"]})

    def esign_event(self, request_id: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("esign_gateway", request_id, "esign/events", body)
            if ent:
                return ent["response"]
            env = self.envelopes.get(body["envelope_id"])
            if env is None:
                raise NotFound("no such envelope")
            if env["status"] != "sent":
                raise Conflict(f"envelope is {env['status']}")
            op = Op(self, f"ese|{request_id}", i03.ACTOR, env["party_ref"])
            if body["status"] != "completed":
                op.update("envelopes", env["envelope_id"], {"status": body["status"]})
                op.ev("enx", "envelope_closed", env["party_ref"], {"envelope_id": env["envelope_id"],
                                                                    "status": body["status"]}, "Envelope closed unsigned")
                return self._answer(op, key, h, {"envelope_id": env["envelope_id"], "status": body["status"]})
            if not body.get("certificate_b64") or not body.get("signed_document_sha256"):
                raise Invalid("a completed envelope carries the certificate and the signed document hash")
            cert = op.blob(b64decode(body["certificate_b64"], "certificate_b64"), "certificate",
                           [env["party_ref"], f"envelope:{env['envelope_id']}"])
            sufficient = body["signed_document_sha256"] == env["doc_sha256"]
            aid = rid("acc", "env", env["envelope_id"])
            rec = {"acceptance_id": aid, "party_ref": env["party_ref"], "signer_identity_ref": f"envelope:{env['envelope_id']}",
                   "doc_id": env["doc_id"], "version": env["version"], "doc_sha256": env["doc_sha256"],
                   "presented_sha256": body["signed_document_sha256"], "accepted_at": iso(self._now()),
                   "method": "esign_envelope", "presentation": "inline", "affirmative_act": True, "esign_consent": None,
                   "evidence_ref": {"kind": "esign_certificate", "sha256": cert, "blob": cert},
                   "evidence_sufficient": sufficient, "entry_by": "esign_gateway"}
            op.ev("enc", "envelope_completed", env["party_ref"], {"envelope_id": env["envelope_id"], "certificate_sha256": cert,
                                                                   "signed_document_sha256": body["signed_document_sha256"],
                                                                   "evidence_sufficient": sufficient},
                  "Envelope completed; certificate stored in Legal's record")
            op.ev("acc", "acceptance_recorded", env["party_ref"], {"acceptance_id": aid, "method": "esign_envelope",
                                                                    "evidence_sufficient": sufficient},
                  f"Acceptance of {env['doc_id']} {env['version']} recorded (envelope)")
            op.update("envelopes", env["envelope_id"], {"status": "completed"})
            op.add("acceptance", rec)
            obls = self._bind_obligations(op, rec, self._version(env["doc_id"], env["version"])) if sufficient else []
            reasons = [] if sufficient else [R.item("SIGNED_HASH_MISMATCH", "the signed document hash differs from the "
                                                    "register's")]
            return self._answer(op, key, h, {**self.acceptance_view(rec), "obligation_ids": obls, "reasons": reasons,
                                             "reason_lines": R.lines(reasons)})

    # ================================================================== playbooks (§B.3, §C.2)

    def _memo_cites_clauses(self, memo_id: Optional[str], clause_ids: list[str]) -> list[dict]:
        memo = self.memos.get(memo_id or "")
        if memo is None:
            return [R.item("PLAYBOOK_MEMO_MISSING", "a playbook change needs a filed counsel memo id (LG-05)")]
        missing = sorted(set(clause_ids) - set(memo["cites"]["clause_ids"]))
        if missing:
            return [R.item("MEMO_DOES_NOT_CITE", f"the memo does not cite clause(s) {', '.join(missing[:10])}")]
        return []

    def create_playbook_proposal(self, request_id: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, "playbooks/proposals", body)
            if ent:
                return ent["response"]
            pb = body["playbook"]
            try:
                clauses = [i02.validate_clause(c) for c in pb["clauses"]]
            except ValueError as exc:
                raise Invalid(str(exc)) from None
            ids = [c["clause_id"] for c in clauses]
            if len(set(ids)) != len(ids):
                raise Invalid("clause ids repeat")
            if not any(d["doc_type"] == pb["doc_type"] for d in self.documents.values()):
                raise Invalid("doc_type names no document type in the register")
            problems = self._memo_cites_clauses(body.get("counsel_memo_id"), ids)
            if problems:
                self._refuse("playbook_change_refused", f"playbook:{pb['doc_type']}", problems, op_id=request_id)
            cur = self._playbook_for(pb["doc_type"])
            if cur is not None and i01.vkey(pb["version"]) <= i01.vkey(cur["version"]):
                raise Conflict("a playbook version must be higher than the one in force")
            pid = rid("pbp", request_id)
            op = Op(self, f"pbp|{request_id}", "andre", f"playbook:{pb['doc_type']}")
            self._injection(op, {"clauses": pb["clauses"]})
            stored = []
            for c in clauses:
                refs = [f"playbook:{pb['doc_type']}"]
                pos = {}
                for name in ("standard", "fallback_1", "fallback_2"):
                    t = c[f"{name}_text"]
                    pos[name] = None if t is None else {
                        "sha256": op.blob(t.encode("utf-8"), "clause", refs), "normalized_sha256": i02.norm_sha(t)}
                stored.append({"clause_id": c["clause_id"], "title_sha256": sha(c["title"]), **pos,
                               "walk_away": c["walk_away"], "escalation": c["escalation"],
                               "rationale_code": c["rationale_code"], "counsel_memo_id": body["counsel_memo_id"],
                               "obligations": c["obligations"]})
            new = {"playbook_id": pb["playbook_id"], "doc_type": pb["doc_type"], "version": pb["version"],
                   "clauses": stored}
            weak = i02.weakening(cur, new)
            p = {"proposal_id": pid, "playbook": new, "counsel_memo_id": body["counsel_memo_id"],
                 "created_at": iso(self._now()), "status": "open", "weakening": bool(weak), "weakening_reasons": weak,
                 "base_version": cur["version"] if cur else None}
            p["content_sha256"] = sha({k: p[k] for k in ("proposal_id", "playbook", "counsel_memo_id", "weakening",
                                                         "weakening_reasons", "base_version")})
            op.ev("pbp", "playbook_proposal_created", f"playbook:{pb['doc_type']}",
                  {"proposal_id": pid, "doc_type": pb["doc_type"], "version": pb["version"],
                   "content_sha256": p["content_sha256"], "weakening": p["weakening"],
                   "counsel_memo_id": body["counsel_memo_id"]},
                  f"Playbook proposal {pb['doc_type']} {pb['version']} created")
            op.add("pb_proposal", p)
            return self._answer(op, key, h, {"proposal": p})

    def decide_playbook(self, request_id: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, "playbooks/decisions", body)
            if ent:
                return ent["response"]
            p = self.pb_proposals.get(body["proposal_id"])
            if p is None:
                raise NotFound("no such playbook proposal")
            if p["status"] != "open":
                raise Conflict(f"proposal is {p['status']}")
            if body["content_sha256"] != p["content_sha256"]:
                raise Conflict("proposal changed since you read it (content_sha256 mismatch); nothing was applied")
            doc_type = p["playbook"]["doc_type"]
            op = Op(self, f"pbd|{request_id}", "andre", f"playbook:{doc_type}")
            if body["decision"] == "reject":
                op.update("pb_proposals", p["proposal_id"], {"status": "rejected"})
                op.ev("pbd", "playbook_proposal_rejected", f"playbook:{doc_type}", {"proposal_id": p["proposal_id"]},
                      "Andre rejected a playbook proposal")
                return self._answer(op, key, h, {"proposal_id": p["proposal_id"], "status": "rejected"})
            problems = self._memo_cites_clauses(p["counsel_memo_id"], [c["clause_id"] for c in p["playbook"]["clauses"]])
            cur = self._playbook_for(doc_type)
            if (cur["version"] if cur else None) != p["base_version"]:
                raise Conflict("the playbook in force changed since this proposal was drafted; propose again")
            if p["weakening"] and body.get("acknowledge_weakening") is not True:
                problems.append(R.item("WEAKENING_NOT_ACKNOWLEDGED", "this change weakens the playbook ("
                                       f"{', '.join(p['weakening_reasons'])}); approve with acknowledge_weakening"))
            if problems:
                self._refuse("playbook_change_refused", f"playbook:{doc_type}", problems, op_id=request_id)
            key_pb = f"{doc_type}@{p['playbook']['version']}"
            rec = {**p["playbook"], "key": key_pb, "status": "approved", "approved_by": "andre",
                   "approved_at": iso(self._now()), "counsel_memo_id": p["counsel_memo_id"],
                   "proposal_id": p["proposal_id"]}
            if cur is not None:
                op.update("playbooks", cur["key"], {"status": "retired"})
            op.update("pb_proposals", p["proposal_id"], {"status": "approved"})
            op.add("playbook", rec)
            op.ev("pbv", "playbook_version_published", f"playbook:{doc_type}",
                  {"doc_type": doc_type, "version": rec["version"], "proposal_id": p["proposal_id"],
                   "content_sha256": p["content_sha256"], "counsel_memo_id": p["counsel_memo_id"],
                   "weakening": p["weakening"]}, f"Playbook {doc_type} {rec['version']} published by Andre")
            return self._answer(op, key, h, {"doc_type": doc_type, "version": rec["version"], "status": "approved"})

    def playbook_view(self, doc_type: str) -> dict:
        with self.lock:
            pb = self._playbook_for(doc_type)
            return {"doc_type": doc_type, "playbook": pb, "rules_pinned": self.rules_pinned}

    def review(self, principal: str, request_id: str, doc_type: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem(principal, request_id, f"playbooks/{doc_type}/reviews", body)
            if ent:
                return ent["response"]
            if not any(d["doc_type"] == doc_type for d in self.documents.values()):
                raise NotFound("no such document type")
            review_id = rid("rev", principal, request_id)
            op = Op(self, f"rev|{review_id}", i02.ACTOR, f"review:{review_id}")
            paper_sha = None
            if body.get("counterparty_paper_text"):
                paper_sha = op.blob(body["counterparty_paper_text"].encode("utf-8"), "paper", [f"review:{review_id}"])
            inj = self._injection(op, {"paper": body.get("counterparty_paper_text"),
                                       "positions": [p["text"] for p in body.get("counterparty_positions") or []]})
            tv = body.get("our_template_version")
            template_ok = tv is not None and any(
                v["version"] == tv and v["status"] == "approved" and self.documents[v["doc_id"]]["doc_type"] == doc_type
                for v in self.doc_versions.values())
            counterparty_paper = paper_sha is not None or not template_ok
            res = i02.review(self._playbook_for(doc_type), list(body.get("counterparty_positions") or []),
                             dict(body.get("facts") or {}), self.cfg.agent_max_fallback, counterparty_paper)
            pos_shas = sorted((p["clause_id"], i02.norm_sha(p["text"])) for p in body.get("counterparty_positions") or [])
            rec = {"review_id": review_id, "doc_type": doc_type, "requested_by": principal,
                   "positions": [{"clause_id": c, "normalized_sha256": s} for c, s in pos_shas],
                   "paper_sha256": paper_sha, "result": {k: res[k] for k in ("per_clause", "accepted_by_agent",
                                                                            "escalate_to_counsel", "walk_away")},
                   "reason_codes": R.codes(res["reasons"]), "injection_rules": inj, "at": iso(self._now())}
            op.ev("rvw", "playbook_review", f"review:{review_id}",
                  {"review_id": review_id, "doc_type": doc_type, **rec["result"], "paper_sha256": paper_sha},
                  f"Playbook review of {doc_type}: {len(res['escalate_to_counsel'])} escalation(s)")
            if res["escalate_to_counsel"]:
                pkg = {"review_id": review_id, "doc_type": doc_type, "escalate": res["escalate_to_counsel"],
                       "positions_sha256": sha(pos_shas), "paper_sha256": paper_sha}
                ans = op.call("counsel_channel", "deliver", (review_id,), lambda: self.ports.counsel.deliver(pkg),
                              Delivery(False))
                op.ev("esc", "playbook_escalated", f"review:{review_id}",
                      {**pkg, "delivered": isinstance(ans, Delivery) and ans.delivered},
                      f"{len(res['escalate_to_counsel'])} clause(s) escalated to counsel")
            op.add("review", rec)
            return self._answer(op, key, h, {"review_id": review_id, **rec["result"], "unreviewed": True,
                                             "review_label": "unreviewed", "reasons": res["reasons"],
                                             "reason_lines": R.lines(res["reasons"]),
                                             "injection_text_ignored": bool(inj)})

    # ================================================================== obligations (§B.4, §C.4)

    def list_obligations(self, party_ref: Optional[str], owner: Optional[str], status: Optional[str]) -> dict:
        with self.lock:
            items = [o for o in self.obligations.values()
                     if (party_ref is None or o["counterparty_ref"] == party_ref)
                     and (owner is None or o["owner_department"] == owner) and (status is None or o["status"] == status)]
            return {"items": sorted(items, key=lambda o: (o["due"] or "9999", o["obligation_id"]))[:1000],
                    "rules_pinned": self.rules_pinned}

    def obligation_done(self, principal: str, request_id: str, oid: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem(principal, request_id, f"obligations/{oid}/done", body)
            if ent:
                return ent["response"]
            o = self.obligations.get(oid)
            if o is None:
                raise NotFound("no such obligation")
            if OWNER_CALLER[o["owner_department"]] != principal:
                raise Refused("NOT_OWNER", reasons=[R.item("NOT_OWNER", f"only {o['owner_department']} marks this "
                              "obligation done")], recorded=False)
            if o["status"] in ("done", "waived"):
                raise Conflict(f"obligation is {o['status']}")
            op = Op(self, f"obd|{request_id}", i04.ACTOR, f"obligation:{oid}")
            op.ev("obd", "obligation_done", f"obligation:{oid}", {"obligation_id": oid, "evidence": body["evidence"],
                                                                   "by": principal}, f"Obligation {oid} done")
            op.update("obligations", oid, {"status": "done", "done_evidence": dict(body["evidence"])})
            return self._answer(op, key, h, {"obligation_id": oid, "status": "done"})

    def obligation_waive(self, request_id: str, oid: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, f"obligations/{oid}/waive", body)
            if ent:
                return ent["response"]
            o = self.obligations.get(oid)
            if o is None:
                raise NotFound("no such obligation")
            if o["status"] in ("done", "waived"):
                raise Conflict(f"obligation is {o['status']}")
            op = Op(self, f"obw|{request_id}", "andre", f"obligation:{oid}")
            op.ev("obw", "obligation_waived", f"obligation:{oid}", {"obligation_id": oid, "memo_id": body.get("memo_id")},
                  f"Andre waived obligation {oid}")
            op.update("obligations", oid, {"status": "waived", "waived_by": "andre"})
            return self._answer(op, key, h, {"obligation_id": oid, "status": "waived"})

    def obligation_entry(self, request_id: str, body: dict) -> dict:
        """Counterparty paper: an obligation entered by Andre from a counsel memo only (§B.4)."""
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, "obligations", body)
            if ent:
                return ent["response"]
            if body["obligation_code"] not in i02.OBLIGATION_CODES:
                raise Invalid("unknown obligation_code")
            memo = self.memos.get(body["memo_id"])
            dv = f"{body['doc_id']}@{body['version']}"
            if memo is None or dv not in memo["cites"]["doc_versions"]:
                self._refuse("obligation_refused", body["counterparty_ref"],
                             [R.item("MEMO_DOES_NOT_CITE", f"counterparty-paper obligations need a filed memo that "
                                     f"cites {dv}")], op_id=request_id)
            oid = rid("obl", "andre", request_id)
            op = Op(self, f"obe|{request_id}", "andre", f"obligation:{oid}")
            rec = {"obligation_id": oid, "source": {"doc_id": body["doc_id"], "version": body["version"],
                                                    "acceptance_id": None, "clause_id": None},
                   "party": body["party"], "counterparty_ref": body["counterparty_ref"],
                   "obligation_code": body["obligation_code"], "due": body.get("due"), "due_rule": "fixed(entered)",
                   "unbound": body.get("due") is None, "alert_lead_days": body["alert_lead_days"],
                   "owner_department": body["owner_department"], "status": "open", "done_evidence": None,
                   "waived_by": None, "memo_id": body["memo_id"], "created_at": iso(self._now())}
            op.ev("obl", "obligation_created", f"obligation:{oid}", {"obligation_id": oid, "code": rec["obligation_code"],
                                                                      "due": rec["due"], "memo_id": body["memo_id"]},
                  "Obligation entered by Andre from a counsel memo")
            op.add("obligation", rec)
            return self._answer(op, key, h, {"obligation_id": oid, "status": "open"})

    # ================================================================== contract storage (§E.1, onboarding)

    def contract_get(self, client_id: str) -> Optional[dict]:
        with self.lock:
            c = self.contracts.get(client_id)
            if c is None or self.current is None:
                return None
            a = self.acceptances.get(c["acceptance_id"])
            terms = dict(c["terms"])
            terms["signed"] = bool(a and a["evidence_sufficient"])
            terms["signed_at"] = a["accepted_at"] if terms["signed"] else None
            terms["ccpa_cpra_clause_present"] = self._ccpa_present(c["doc_id"], c["version"])
            return terms

    def _ccpa_present(self, doc_id: str, version: str) -> bool:
        v = self.doc_versions.get(f"{doc_id}@{version}")
        if v is None:
            return False
        ok_pos = ("standard", "fallback_1")
        return any(c["clause_id"] == "MSA-CCPA-01" and c["position"] in ok_pos for c in v["clause_ids"]) and \
            self.cq_verified("CQ-20")

    def contract_put(self, request_id: str, client_id: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("onboarding", request_id, f"contracts/{client_id}", body)
            if ent:
                return ent["response"]
            t, ex = body["terms"], body["executed"]
            if t["client_id"] != client_id:
                raise Invalid("terms.client_id must equal the path client_id")
            a = self.acceptances.get(ex["acceptance_id"])
            reasons = []
            if a is None:
                reasons.append(R.item("NO_SUFFICIENT_ACCEPTANCE", "the executed block names no recorded acceptance"))
            else:
                if (a["doc_id"], a["version"], a["doc_sha256"]) != (ex["doc_id"], ex["version"], ex["doc_sha256"]):
                    reasons.append(R.item("ACCEPTANCE_HASH_MISMATCH", "the executed block does not match the acceptance"))
                if a["party_ref"] != f"client:{client_id}":
                    reasons.append(R.item("NO_SUFFICIENT_ACCEPTANCE", "the acceptance belongs to another party"))
                if ex["doc_id"] != "client_msa":
                    reasons.append(R.item("NO_SUFFICIENT_ACCEPTANCE", "contract terms come from an executed client_msa"))
            signed = bool(a and not reasons and a["evidence_sufficient"])
            if t["signed"] and not signed:
                reasons.append(R.item("NO_SUFFICIENT_ACCEPTANCE", "signed: true needs an evidence-sufficient acceptance "
                                      "of the executed MSA version", cq_id="CQ-19"))
            if t["ccpa_cpra_clause_present"] != self._ccpa_present(ex["doc_id"], ex["version"]):
                reasons.append(R.item("CQ_UNVERIFIED", "ccpa_cpra_clause_present differs from the executed version "
                                      "(MSA-CCPA-01 at standard or fallback_1, CQ-20 verified)", cq_id="CQ-20"))
            if reasons:
                self._refuse("contract_terms_refused", f"client:{client_id}", reasons, op_id=request_id)
            op = Op(self, f"ctp|{request_id}", i03.ACTOR, f"client:{client_id}")
            stored = dict(t)
            op.ev("ctp", "contract_terms_stored", f"client:{client_id}",
                  {"client_id_sha256": sha(client_id), "acceptance_id": ex["acceptance_id"], "doc_id": ex["doc_id"],
                   "version": ex["version"], "terms_sha256": sha(stored)}, "Contract terms stored for onboarding")
            op.add("contract", {"client_id": client_id, "terms": stored, "acceptance_id": ex["acceptance_id"],
                                "doc_id": ex["doc_id"], "version": ex["version"], "stored_at": iso(self._now())})
            return self._answer(op, key, h, {"client_id": client_id, "stored": True})

    # ================================================================== memos (§B.6, §C.5)

    def memo_view(self, memo_id: str) -> dict:
        with self.lock:
            m = self.memos.get(memo_id)
            if m is None:
                raise NotFound("no such memo")
            props = [{k: p[k] for k in ("cproposal_id", "kind", "target_id", "status", "compliance_proposal_id")}
                     for p in self.cproposals.values() if p["memo_id"] == memo_id]
            return {**{k: m[k] for k in ("memo_id", "counsel_ref", "memo_sha256", "memo_date", "received_at", "cites",
                                         "entered_by", "injection_flags")},
                    "answers": [{"cq_id": a["cq_id"], "resolution": a["resolution"]} for a in m["answers"]],
                    "proposals": sorted(props, key=lambda p: p["cproposal_id"]), "rules_pinned": self.rules_pinned}

    def _engagement_ok(self, counsel_ref: str) -> bool:
        e = self.engagements.get(counsel_ref)
        if e is None:
            return False
        v = self.doc_versions.get(f"{e['doc_id']}@{e['version']}")
        cur = self._current("engagement_letter")
        return v is not None and cur is not None and cur["key"] == v["key"]

    def file_memo(self, request_id: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, "memos", body)
            if ent:
                return ent["response"]
            now = self._now()
            data = b64decode(body["content_b64"], "content_b64")
            msha = sha_bytes(data)
            c = body["cites"]
            if any(m["memo_sha256"] == msha for m in self.memos.values()):
                raise Refused("MEMO_DUPLICATE", reasons=[R.item("MEMO_DUPLICATE", "this memo (by SHA-256) is already "
                              "filed")], recorded=False)
            if date.fromisoformat(body["memo_date"]) > self._today() + timedelta(days=1):
                raise Invalid("memo_date is in the future")
            for cq in c["cq_ids"]:
                if cq not in self.register:
                    raise Invalid(f"{cq} is not a counsel-question row")
            for k in c["retention_classes"]:
                if k not in self.retention:
                    raise Invalid(f"{k} is not a retention class")
            for k in c["signoff_topics"]:
                if k not in self.topics:
                    raise Invalid(f"{k} is not a sign-off topic")
            for k, scope in body["signoff_scopes"].items():
                sk = self.topics.get(k, {}).get("scope_key")
                if not isinstance(scope, dict) or not (scope == {"any": True} or (
                        set(scope) == {sk} and isinstance(scope[sk], list) and len(scope[sk]) <= 1000
                        and all(isinstance(x, str) and ID_RE.fullmatch(x) for x in scope[sk]))):
                    raise Invalid(f"signoff_scopes.{k}: {{\"{sk}\": [ids]}} or {{\"any\": true}}")
            reasons = []
            if not self._engagement_ok(body["counsel_ref"]):
                reasons.append(R.item("ENGAGEMENT_NOT_APPROVED", "counsel_ref names no approved, current engagement "
                                      "letter (LG-17)", cq_id="CQ-24"))
            aliases = [cq for cq in c["cq_ids"] if self.register[cq]["alias_of"]]
            for cq in aliases:
                reasons.append(R.item("MEMO_CITES_ALIAS", f"{cq} is an alias of {self.register[cq]['alias_of']}: the "
                                      "memo cites the canonical row", cq_id=cq))
            reasons += i05.uncited(body)
            if reasons:
                uncited = any(r["code"] == "MEMO_DOES_NOT_CITE" for r in reasons)
                self._refuse("memo_refused_uncited" if uncited else "memo_refused", "memos", reasons, op_id=request_id)
            memo_id = rid("mem", msha)
            op = Op(self, f"mem|{memo_id}", i05.ACTOR, f"memo:{memo_id}")
            op.blob(data, "memo", [f"memo:{memo_id}"])
            try:
                inj = self._injection(op, {"memo": data.decode("utf-8")})
            except UnicodeDecodeError:
                inj = []
            received = iso(now)
            review_by = (date.fromisoformat(body["memo_date"]) + timedelta(days=self.cfg.memo_review_days)).isoformat()
            answers = []
            for a in body["answers"]:
                ex = op.blob(a["quoted_excerpt"].encode("utf-8"), "excerpt", [f"memo:{memo_id}"]) \
                    if a.get("quoted_excerpt") else None
                answers.append({"cq_id": a["cq_id"], "resolution": a["resolution"], "excerpt_sha256": ex})
            memo = {"memo_id": memo_id, "counsel_ref": body["counsel_ref"], "memo_sha256": msha,
                    "memo_date": body["memo_date"], "received_at": received, "entered_by": "andre",
                    "answers": answers, "cites": dict(c), "injection_flags": {"rules": inj, "count": len(inj)}}
            op.ev("mem", "memo_filed", f"memo:{memo_id}",
                  {"memo_id": memo_id, "memo_sha256": msha, "counsel_ref": body["counsel_ref"], "memo_date": body["memo_date"],
                   "cites": dict(c), "answers": [{"cq_id": a["cq_id"], "resolution": a["resolution"]} for a in answers]},
                  f"Counsel memo filed ({len(answers)} answer(s))")
            op.add("memo", memo)
            effects: list[dict] = []
            new_props: list[str] = []
            for a in answers:
                row = self.register[a["cq_id"]]
                if a["resolution"] != "verified_rule":
                    effects.append({"cq_id": a["cq_id"], "effect": "no_change", "resolution": a["resolution"]})
                    op.update("register", a["cq_id"], {"memo_ids": row["memo_ids"] + [memo_id]})
                    continue
                if row["origin"] == "compliance":
                    # the row is Compliance's: it turns verified only when Andre approves, at Compliance, the
                    # proposal he files in step 2 (POST /legal/v1/memos/{memo_id}/compliance-proposals, N17-8)
                    op.update("register", a["cq_id"], {"memo_ids": row["memo_ids"] + [memo_id],
                                                        "compliance": {"cproposal_id": None,
                                                                       "status": "awaiting_proposal"}})
                    effects.append({"cq_id": a["cq_id"], "effect": "awaiting_proposal"})
                    continue
                op.update("register", a["cq_id"], {"status": "verified", "memo_ids": row["memo_ids"] + [memo_id],
                                                    "review_by": review_by, "compliance": row.get("compliance")})
                op.ev("rrv", "register_row_verified", a["cq_id"], {"cq_id": a["cq_id"], "memo_id": memo_id,
                                                                    "review_by": review_by},
                      f"{a['cq_id']} verified via counsel memo", actor=i05.ACTOR)
                effects.append({"cq_id": a["cq_id"], "effect": "verified", "review_by": review_by})
                port = ORIGIN_PORT.get(row["origin"])
                if port:
                    target = getattr(self.ports, port)
                    op.call(port, "notify", ("register_row_verified", a["cq_id"]),
                            lambda t=target, q=a["cq_id"]: t.notify("register_row_verified", q,
                                                                     {"memo_id": memo_id, "review_by": review_by}),
                            Delivery(False))
            for k, period in body["retention_periods"].items():
                op.update("retention", k, {"period": period})
            for k in c["retention_classes"]:
                op.update("retention", k, {"status": "verified", "memo_id": memo_id, "review_by": review_by})
                op.ev("rtv", "register_row_verified", f"retention:{k}", {"retention_class": k, "memo_id": memo_id},
                      f"Retention class {k} verified via counsel memo", actor=i05.ACTOR)
            for k in c["signoff_topics"]:
                op.update("topics", k, {"status": "verified", "memo_id": memo_id, "review_by": review_by,
                                        "scope": body["signoff_scopes"].get(k)})
                op.ev("tpv", "register_row_verified", f"signoff:{k}", {"topic": k, "memo_id": memo_id},
                      f"Sign-off topic {k} verified via counsel memo", actor=i05.ACTOR)
            resp_core = {"memo_id": memo_id, "memo_sha256": msha, "effects": effects, "review_by": review_by}
            self._commit_with_idem(op, key, h, resp_core)
        delivered = self.deliver_proposals(new_props)
        with self.lock:
            props = [{k: self.cproposals[p][k] for k in ("cproposal_id", "kind", "target_id", "status",
                                                          "compliance_proposal_id")} for p in new_props]
        resp = {**resp_core, "proposals": props, "delivery_attempts": delivered, "ledger_event_ids": op.events}
        return resp

    def memo_proposals(self, request_id: str, memo_id: str, body: dict) -> dict:
        """Step 2 of a memo-backed Compliance proposal (AEGIS N17-8). The memo is already filed, so its id exists
        and Andre types each row with ``source_url: urn:legal37:memos:<memo_id>`` (compliance-py accepts urn:
        URLs; the old ``legal37://`` form was refused there). Every target must be one the memo cites: a counsel
        question it answered ``verified_rule`` (supersede) or an obligation row in ``cites.obligation_ids``
        (amend / reverify). Delivery happens after the commit, outside the lock (N16-1)."""
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, f"memos/{memo_id}/proposals", body)
            if ent:
                return ent["response"]
            memo = self.memos.get(memo_id)
            if memo is None:
                raise NotFound("no such memo")
            urn = i05.memo_urn(memo_id)
            verified_cqs = {a["cq_id"] for a in memo["answers"] if a["resolution"] == "verified_rule"}
            reasons = []
            for p in body["proposals"]:
                row = p["proposed_row"]
                if not isinstance(row.get("id"), str) or len(canonical(row)) > 16_384:
                    raise Invalid("proposed_row: a Compliance register row object (with its id), at most 16 KiB")
                if row.get("source_url") != urn:
                    raise Invalid(f"proposed_row.source_url must be exactly {urn} (the filed memo)")
                if p["kind"] == "supersede":
                    if p["target_id"] not in verified_cqs:
                        reasons.append(R.item("MEMO_DOES_NOT_CITE", f"the memo does not answer {p['target_id']} "
                                              "verified_rule", cq_id=p["target_id"] if p["target_id"] in self.register
                                              else None))
                elif p["target_id"] not in memo["cites"]["obligation_ids"]:
                    reasons.append(R.item("MEMO_DOES_NOT_CITE", f"Compliance row {p['target_id']} is not cited by the "
                                          "memo"))
                if rid("cpr", memo_id, p["kind"], p["target_id"]) in self.cproposals:
                    raise Conflict(f"a {p['kind']} proposal for {p['target_id']} from this memo already exists")
            if reasons:
                self._refuse("memo_refused_uncited", f"memo:{memo_id}", reasons, op_id=request_id)
            op = Op(self, f"mpr|{memo_id}|{request_id}", i05.ACTOR, f"memo:{memo_id}")
            new_props = []
            for p in body["proposals"]:
                ex = op.blob(p["quoted_excerpt"].encode("utf-8"), "excerpt", [f"memo:{memo_id}"])
                cp_id = self._new_cproposal(op, memo, p["kind"], p["target_id"], p["proposed_row"], ex)
                new_props.append(cp_id)
                reg = self.register.get(p["target_id"])
                if p["kind"] == "supersede" and reg is not None:
                    op.update("register", p["target_id"], {"compliance": {"cproposal_id": cp_id, "status": "pending"}})
            self._injection(op, {"rows": [p["proposed_row"] for p in body["proposals"]]})
            resp_core = {"memo_id": memo_id, "cproposal_ids": new_props}
            self._commit_with_idem(op, key, h, resp_core)
        delivered = self.deliver_proposals(new_props)
        with self.lock:
            props = [{k: self.cproposals[p][k] for k in ("cproposal_id", "kind", "target_id", "status",
                                                          "compliance_proposal_id")} for p in new_props]
        return {**resp_core, "proposals": props, "delivery_attempts": delivered, "ledger_event_ids": op.events}

    def _commit_with_idem(self, op: Op, key: tuple, h: str, response: dict) -> None:
        op.idem = {"principal": key[0], "request_id": key[1], "h": h,
                   "response": {**response, "proposals": "see GET /legal/v1/memos/{id}", "ledger_event_ids": op.events},
                   "at": iso(self._now())}
        self._commit(op)

    def _new_cproposal(self, op: Op, memo: dict, kind: str, target_id: str, row: dict, excerpt_sha: Optional[str]) -> str:
        cp_id = rid("cpr", memo["memo_id"], kind, target_id)
        rec = {"cproposal_id": cp_id, "memo_id": memo["memo_id"], "kind": kind, "target_id": target_id,
               "replacement_id": row.get("id") if kind == "supersede" else target_id, "proposed_row": row,
               "excerpt_sha256": excerpt_sha, "status": "pending_delivery", "compliance_proposal_id": None,
               "attempts": 0, "http_status": None, "created_at": iso(self._now()),
               "request_id": f"lg37-{memo['memo_id'][7:]}-{hashlib.sha256(target_id.encode()).hexdigest()[:12]}"}
        op.ev("cpc", "compliance_proposal_created", f"memo:{memo['memo_id']}",
              {"cproposal_id": cp_id, "kind": kind, "target_id": target_id, "row_sha256": sha(row),
               "memo_id": memo["memo_id"]}, f"Compliance proposal ({kind} {target_id}) prepared from a counsel memo")
        op.add("cproposal", rec)
        return cp_id

    def _cproposal_body(self, p: dict) -> dict:
        memo = self.memos[p["memo_id"]]
        excerpt = ""
        if p.get("excerpt_sha256"):
            b = self.blobs.get(p["excerpt_sha256"])
            excerpt = b.decode("utf-8") if b else ""
        return {"kind": p["kind"], "target_id": p["target_id"], "proposed_row": p["proposed_row"],
                "evidence": i05.evidence(memo["memo_id"], memo["memo_sha256"], memo["received_at"], excerpt)}

    def deliver_proposals(self, ids: list[str]) -> int:
        """Deliver pending Compliance proposals OUTSIDE the service lock (N16-1): crossing recorded first under the
        lock, the call made without it, the outcome committed under it. Returns the number attempted."""
        n = 0
        for cp_id in ids:
            with self.lock:
                p = self.cproposals.get(cp_id)
                if p is None or p["status"] != "pending_delivery" or self.reconcile_mode:
                    continue
                body = self._cproposal_body(p)
                # bug sweep E (R6-M1): no time in the id; the attempt number and the payload's hash are
                xpay = {"port": "compliance_38", "action": "create_proposal", "cproposal_id": cp_id,
                        "body_sha256": sha(body), "attempt": p["attempts"]}
                xid = derived_id("x", "cpr", cp_id, p["attempts"], payload_sha256(xpay))
                try:
                    self._record(xid, "crossing_compliance_38_requested", i05.ACTOR, f"memo:{p['memo_id']}",
                                 xpay, "Request to compliance_38: create_proposal")
                except Unavailable:
                    continue
            n += 1
            bad = None
            try:
                ans = self.ports.compliance.create_proposal(p["request_id"], body)
            except Exception:  # noqa: BLE001
                ans = ProposalAnswer("unavailable")
            else:
                try:
                    ans = A.check(ans, ProposalAnswer, ProposalAnswer("unavailable"))
                except A.Malformed as exc:              # AEGIS N17-3: never "created" on a malformed answer
                    ans, bad = ProposalAnswer("unavailable"), exc.kind
            with self.lock:
                op = Op(self, f"cpd|{cp_id}|{p['attempts']}", i05.ACTOR, f"memo:{p['memo_id']}")
                op.events.append(xid)
                if bad:
                    op.ev("xbad", "adapter_answer_refused", f"memo:{p['memo_id']}",
                          {"port": "compliance_38", "action": "create_proposal", "cproposal_id": cp_id, "problem": bad},
                          "Malformed answer from compliance_38 (create_proposal) refused")
                status = {"created": "delivered", "refused": "refused_by_compliance"}.get(ans.status, "pending_delivery")
                etype = {"delivered": "compliance_proposal_delivered", "refused_by_compliance":
                         "compliance_proposal_refused", "pending_delivery": "compliance_proposal_pending_delivery"}[status]
                op.ev("cpd", etype, f"memo:{p['memo_id']}", {"cproposal_id": cp_id, "status": status,
                                                              "compliance_proposal_id": ans.compliance_proposal_id,
                                                              "http_status": ans.http_status},
                      f"Compliance proposal {status.replace('_', ' ')}")
                op.update("cproposals", cp_id, {"status": status, "compliance_proposal_id": ans.compliance_proposal_id,
                                                "attempts": p["attempts"] + 1, "http_status": ans.http_status})
                try:
                    self._commit(op)
                except Unavailable:
                    pass
        return n

    def _confirm_compliance_rows(self) -> int:
        """Delivered supersede proposals for Compliance-owned rows: the mirror row turns verified only when
        Compliance reports the replacement row verified (Andre approved it there). Reads outside the lock."""
        with self.lock:
            todo = [(p["cproposal_id"], p["target_id"], p["replacement_id"], p["memo_id"]) for p in self.cproposals.values()
                    if p["status"] == "delivered" and p["kind"] == "supersede"
                    and self.register.get(p["target_id"], {}).get("origin") == "compliance"
                    and self.register[p["target_id"]]["status"] != "verified"]
        n = 0
        for cp_id, cq, repl, memo_id in todo:
            try:
                row = A.check(self.ports.compliance.row(repl), ComplianceRow, ComplianceRow(False, repl))
            except Exception:  # noqa: BLE001 - raised or malformed (AEGIS N17-3): not proof of approval
                continue
            if not row.available or row.obligation_id != repl or row.effective_status != "verified":
                continue
            with self.lock:
                memo = self.memos[memo_id]
                review_by = (date.fromisoformat(memo["memo_date"]) + timedelta(days=self.cfg.memo_review_days)).isoformat()
                op = Op(self, f"cfm|{cp_id}", i05.ACTOR, cq)
                op.ev("rrv", "register_row_verified", cq, {"cq_id": cq, "memo_id": memo_id, "compliance_row": repl,
                                                           "review_by": review_by},
                      f"{cq} verified: Compliance approved the counsel-memo row")
                op.update("register", cq, {"status": "verified", "review_by": review_by,
                                           "compliance": {"cproposal_id": cp_id, "status": "approved_at_compliance"}})
                op.update("cproposals", cp_id, {"status": "approved_at_compliance"})
                try:
                    self._commit(op)
                    n += 1
                except Unavailable:
                    pass
        return n

    # ================================================================== matters and holds (§B.7, §C.6)

    def _template_ref(self, doc_id: str) -> Optional[dict]:
        cur = self._current(doc_id)
        return {"doc_id": doc_id, "version": cur["version"], "sha256": cur["sha256"]} if cur else None

    def _open_matter(self, op: Op, kind: str, facts: dict, amount: Optional[Decimal], channel: str, requester: str,
                     subject_refs: list[str], custodians: list[str], systems: list[str], deadlines: dict) -> dict:
        now = self._now()
        tri = i06.triage(kind, facts, amount, self.cfg.dispute_threshold)
        mid = rid("mat", op.op_id)
        dl = {k: v for k, v in deadlines.items() if v}
        reasons = list(tri["reasons"])
        if kind == "subpoena":
            reasons.append(R.item("SUBPOENA_NOTHING_PRODUCED", "return date calendared; nothing is produced without a "
                                  "counsel memo"))
        if kind == "privacy_request" and facts.get("dsar") and not facts.get("client_flowed"):
            try:
                dl.update(i06.dsar_deadlines(self._today(), self.cal,
                                             (self.rules().get("LG-16") or {}).get("parameters", {})))
            except HolidaysUnknown:
                pass
            reasons.append(R.item("DSAR_CLOCK_UNVERIFIED", "DSAR clocks rest on a secondary source (UNVERIFIED)"))
        routing = None
        if tri["route"] == "template_lane":
            routing = self._template_ref("not_legal_advice_v1")
            if routing is None:
                reasons.append(R.item("NO_APPROVED_TEMPLATE", "the routing notice not_legal_advice_v1 has no approved "
                                      "version yet (CQ-15)", cq_id="CQ-15"))
        m = {"matter_id": mid, "intake": {"channel": channel, "requester_ref_sha256": sha(requester), "kind": kind,
                                          "facts": dict(facts), "received_at": iso(now),
                                          "disputed_amount_usd": str(amount) if amount is not None else None},
             "severity": tri["severity"], "likelihood": tri["likelihood"], "route": tri["route"],
             "hold_required": tri["hold_required"], "hold_ids": [], "deadlines": dl, "status": "open",
             "closed_by": None, "retention_class": "matters", "subject_refs": sorted(set(subject_refs)),
             "routing_notice": routing, "reason_codes": R.codes(reasons)}
        op.ev("mat", "matter_opened", f"matter:{mid}", {"matter_id": mid, "kind": kind, "severity": tri["severity"],
                                                        "likelihood": tri["likelihood"], "deadlines": dl},
              f"Matter opened: {kind} ({tri['severity']}/{tri['likelihood']})", actor=i06.ACTOR)
        op.ev("mrt", "matter_routed", f"matter:{mid}", {"matter_id": mid, "route": tri["route"],
                                                        "hold_required": tri["hold_required"]},
              f"Matter routed: {tri['route']}", actor=i06.ACTOR)
        if tri["route"] in ("counsel_same_day", "counsel_standard"):
            pkg = {"matter_id": mid, "kind": kind, "severity": tri["severity"], "route": tri["route"], "deadlines": dl}
            ans = op.call("counsel_channel", "deliver", (mid,), lambda: self.ports.counsel.deliver(pkg), Delivery(False))
            if not (isinstance(ans, Delivery) and ans.delivered):
                reasons.append(R.item("COUNSEL_NOT_DELIVERED", "no counsel channel is wired: Andre is alerted instead"))
            if tri["route"] == "counsel_same_day":
                op.call("push", "notify", ("counsel_same_day", mid),
                        lambda: self.ports.push.notify("counsel_same_day", mid, {"severity": tri["severity"]}),
                        Delivery(False))
        op.add("matter", m)
        if tri["hold_required"]:
            hid = self._issue_hold(op, m, subject_refs, custodians, systems or list(DEFAULT_HOLD_SYSTEMS))
            m["hold_ids"] = [hid]
        m["_reasons"] = reasons
        return m

    def _issue_hold(self, op: Op, matter: dict, subject_refs: list[str], custodians: list[str], systems: list[str]) -> str:
        now = self._now()
        mid = matter["matter_id"]
        hid = rid("hld", mid)
        freeze = {}
        cyber = [s for s in systems if i06.SYSTEM_OWNER[s] == "cybersecurity_22"]
        if cyber:
            ans = op.call("cybersecurity_22", "freeze", (hid, cyber),
                          lambda: self.ports.cybersecurity_22.freeze(hid, cyber, subject_refs), Delivery(False))
            ok = isinstance(ans, Delivery) and ans.delivered
            for s in cyber:
                freeze[s] = "frozen" if ok else "not_frozen"
        for s in systems:
            owner = i06.SYSTEM_OWNER[s]
            if owner == "legal_37":
                freeze[s] = "frozen"
            elif owner != "cybersecurity_22":
                port = getattr(self.ports, owner)
                ans = op.call(owner, "notify", ("hold_freeze", hid, s),
                              lambda p=port, sys_=s: p.notify("hold_freeze", hid, {"system": sys_}), Delivery(False))
                freeze[s] = "frozen" if isinstance(ans, Delivery) and ans.delivered else "not_frozen"
        tpl = self._template_ref("lit_hold_notice")
        sent = None
        if tpl is not None and custodians:
            ans = op.call("people_43", "notify", ("hold_notice", hid),
                          lambda: self.ports.people_43.notify("hold_notice", hid, {"template": tpl,
                                                                                  "custodians": len(custodians)}),
                          Delivery(False))
            sent = iso(now) if isinstance(ans, Delivery) and ans.delivered else None
        if any(v == "not_frozen" for v in freeze.values()) or tpl is None or sent is None:
            op.call("push", "notify", ("hold_attention", hid),
                    lambda: self.ports.push.notify("hold_attention", hid, {"not_frozen": sorted(
                        k for k, v in freeze.items() if v == "not_frozen"), "notice_sent": sent is not None}),
                    Delivery(False))
        rec = {"hold_id": hid, "matter_id": mid, "custodians": sorted(set(custodians)), "systems": sorted(set(systems)),
               "subject_refs": sorted(set(subject_refs)), "notice_template": tpl, "sent_at": sent, "freeze": freeze,
               "acknowledgments": [], "renotice_every_days": self.cfg.hold_renotice_days,
               "renotice_due": (self._today() + timedelta(days=self.cfg.hold_renotice_days)).isoformat(),
               "status": "active", "released_by": None, "memo_id": None, "issued_at": iso(now)}
        op.ev("hld", "hold_issued", f"hold:{hid}", {"hold_id": hid, "matter_id": mid, "systems": rec["systems"],
                                                    "subject_refs_sha256": sha(rec["subject_refs"]),
                                                    "subject_ref_count": len(rec["subject_refs"]), "freeze": freeze,
                                                    "notice_template": tpl, "notice_sent": sent is not None},
              f"Litigation hold issued for matter {mid}", actor=i06.ACTOR)
        op.add("hold", rec)
        return hid

    def matter_intake(self, principal: str, request_id: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem(principal, request_id, "requests", body)
            if ent:
                return ent["response"]
            if body["kind"] == "subpoena" and not body["deadlines"].get("return_date"):
                raise Invalid("a subpoena intake carries its return_date (LG-15: calendared at once)")
            amount = Decimal(body["disputed_amount_usd"]) if body.get("disputed_amount_usd") else None
            op = Op(self, f"req|{principal}|{request_id}", i06.ACTOR, "matters")
            m = self._open_matter(op, body["kind"], dict(body["facts"]), amount, body["channel"], body["requester_ref"],
                                  list(body["subject_refs"]), list(body["custodians"]), list(body["systems"]),
                                  dict(body["deadlines"]))
            reasons = m.pop("_reasons")
            return self._answer(op, key, h, self._matter_answer(m, reasons))

    def _matter_answer(self, m: dict, reasons: list[dict]) -> dict:
        return {"matter_id": m["matter_id"], "severity": m["severity"], "likelihood": m["likelihood"],
                "route": m["route"], "hold_ids": m["hold_ids"], "deadlines": m["deadlines"],
                "routing_notice": m["routing_notice"], "status": m["status"], "unreviewed": True,
                "review_label": "unreviewed", "reasons": reasons, "reason_lines": R.lines(reasons)}

    def matter_view(self, mid: str) -> dict:
        with self.lock:
            m = self.matters.get(mid)
            if m is None:
                raise NotFound("no such matter")
            return {k: v for k, v in m.items() if k != "intake"} | {"kind": m["intake"]["kind"],
                                                                     "received_at": m["intake"]["received_at"]}

    def close_matter(self, request_id: str, mid: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, f"matters/{mid}/close", body)
            if ent:
                return ent["response"]
            m = self.matters.get(mid)
            if m is None:
                raise NotFound("no such matter")
            if m["status"] == "closed":
                raise Conflict("matter is closed")
            active = [x for x in m["hold_ids"] if self.holds[x]["status"] == "active"]
            if active:
                raise Refused("HELD", reasons=[R.item("HELD", f"{len(active)} active hold(s): release them (Andre, with a "
                              "memo) before closing")], recorded=False)
            op = Op(self, f"mcl|{request_id}", "andre", f"matter:{mid}")
            op.ev("mcl", "matter_closed", f"matter:{mid}", {"matter_id": mid, "memo_id": body.get("memo_id")},
                  "Andre closed the matter")
            op.update("matters", mid, {"status": "closed", "closed_by": "andre"})
            return self._answer(op, key, h, {"matter_id": mid, "status": "closed"})

    def hold_check(self, subject_ref: str) -> dict:
        with self.lock:
            ids = sorted(h["hold_id"] for h in self.holds.values()
                         if h["status"] == "active" and subject_ref in h["subject_refs"])
            return {"subject_ref": subject_ref, "held": bool(ids), "hold_ids": ids, "rules_pinned": self.rules_pinned}

    def _held(self, refs: list[str]) -> list[str]:
        refs_s = set(refs)
        return sorted(h["hold_id"] for h in self.holds.values() if h["status"] == "active"
                      and refs_s & set(h["subject_refs"]))

    def hold_ack(self, principal: str, request_id: str, hid: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem(principal, request_id, f"holds/{hid}/ack", body)
            if ent:
                return ent["response"]
            hold = self.holds.get(hid)
            if hold is None:
                raise NotFound("no such hold")
            if body["custodian"] not in hold["custodians"]:
                raise Invalid("not a custodian of this hold")
            op = Op(self, f"ack|{request_id}", i06.ACTOR, f"hold:{hid}")
            acks = hold["acknowledgments"] + [{"custodian": body["custodian"], "at": iso(self._now())}]
            op.ev("hak", "hold_acknowledged", f"hold:{hid}", {"hold_id": hid, "custodian_sha256": sha(body["custodian"])},
                  "Hold acknowledged by a custodian")
            op.update("holds", hid, {"acknowledgments": acks})
            return self._answer(op, key, h, {"hold_id": hid, "acknowledged": len(acks),
                                             "custodians": len(hold["custodians"])})

    def hold_release(self, request_id: str, hid: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, f"holds/{hid}/release", body)
            if ent:
                return ent["response"]
            hold = self.holds.get(hid)
            if hold is None:
                raise NotFound("no such hold")
            if hold["status"] != "active":
                raise Conflict("hold is released")
            if not body.get("memo_id") or body["memo_id"] not in self.memos:
                self._refuse("hold_release_refused", f"hold:{hid}", [R.item("HOLD_RELEASE_NEEDS_MEMO", "a hold is "
                             "released only by Andre with a filed counsel memo")], op_id=request_id)
            op = Op(self, f"rel|{request_id}", "andre", f"hold:{hid}")
            op.ev("hrl", "hold_released_by_andre", f"hold:{hid}", {"hold_id": hid, "memo_id": body["memo_id"]},
                  "Andre released the hold with a counsel memo")
            op.update("holds", hid, {"status": "released", "released_by": "andre", "memo_id": body["memo_id"]})
            return self._answer(op, key, h, {"hold_id": hid, "status": "released"})

    # ================================================================== takedowns (§B.8, §C.7)

    def _restore_bounds(self) -> tuple[int, int]:
        return (int(self._param("LG-10", "restore_min_business_days", 10)),
                int(self._param("LG-10", "restore_max_business_days", 14)))

    def takedown_in(self, principal: str, request_id: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem(principal, request_id, "takedowns", body)
            if ent:
                return ent["response"]
            nid = rid("tdn", principal, request_id)
            op = Op(self, f"tdn|{nid}", i07.ACTOR, f"takedown:{nid}")
            t = body["target"]
            valid = i07.valid(body["elements"])
            rec = {"notice_id": nid, "direction": "in", "target": dict(t), "elements": dict(body["elements"]),
                   "received_at": iso(self._now()), "valid": valid, "status": "actioned" if valid else "invalid",
                   "counter_notice": None, "forwarded_to": [], "matter_id": None,
                   "uploader_ref": body.get("uploader_ref"), "entered_by": principal}
            reasons = []
            op.ev("tdr", "takedown_received", f"takedown:{nid}", {"notice_id": nid, "valid": valid,
                                                                  "post_ref_sha256": t["post_ref_sha256"],
                                                                  "target_kind": t["kind"], "platform": t["platform"]},
                  f"Takedown notice received ({'valid' if valid else 'invalid'})")
            if not valid:
                missing = [e for e in i07.ELEMENTS if body["elements"][e] is not True]
                reasons.append(R.item("NOTICE_INVALID", f"{len(missing)} of the six 512(c)(3)(A) elements missing: "
                                      f"{', '.join(missing)}"))
                op.ev("tdi", "takedown_invalid", f"takedown:{nid}", {"notice_id": nid, "missing": missing,
                                                                     "arguable": list(body["arguable_elements"])},
                      "Takedown notice invalid (checklist)")
                if body["arguable_elements"]:
                    op.call("counsel_channel", "deliver", (nid,), lambda: self.ports.counsel.deliver(
                        {"notice_id": nid, "arguable": list(body["arguable_elements"])}), Delivery(False))
                op.add("takedown", rec)
                return self._answer(op, key, h, {"notice_id": nid, "valid": False, "status": "invalid",
                                                 "reasons": reasons, "reason_lines": R.lines(reasons)})
            fwd = []
            for port in ("clipper_network", "creative_production"):
                target = getattr(self.ports, port)
                ans = op.call(port, "notify", ("takedown_notice", nid),
                              lambda p=target: p.notify("takedown_notice", nid, {"post_ref_sha256": t["post_ref_sha256"],
                                                                                 "target_kind": t["kind"]}),
                              Delivery(False))
                fwd.append({"department": port, "delivered": isinstance(ans, Delivery) and ans.delivered})
            rec["forwarded_to"] = fwd
            op.ev("tdf", "takedown_forwarded", f"takedown:{nid}", {"notice_id": nid, "forwarded": fwd},
                  "Takedown forwarded to Clipper Network and Creative")
            refs = [f"post:{t['post_ref_sha256']}"] + ([body["uploader_ref"]] if body.get("uploader_ref") else [])
            m = self._open_matter(op, "ip_claim", {"deadline_stated": True}, None, "hub", principal, refs, [],
                                  ["legal_store", "creative_store", "cn_records"], {})
            reasons += m.pop("_reasons")
            rec["matter_id"] = m["matter_id"]
            repeat = None
            if t["kind"] == "zbc_hosted" and body.get("uploader_ref"):
                repeat = 1 + sum(1 for n in self.takedowns.values() if n.get("uploader_ref") == body["uploader_ref"]
                                 and i07.counts(n) and n["target"]["kind"] == "zbc_hosted")
            rec["repeat_count_for_uploader"] = repeat
            op.add("takedown", rec)
            return self._answer(op, key, h, {"notice_id": nid, "valid": True, "status": "actioned",
                                             "matter_id": m["matter_id"], "hold_ids": m["hold_ids"],
                                             "forwarded_to": fwd, "repeat_count_for_uploader": repeat,
                                             "reasons": reasons, "reason_lines": R.lines(reasons)})

    def counter_notice(self, principal: str, request_id: str, nid: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem(principal, request_id, f"takedowns/{nid}/counter-notice", body)
            if ent:
                return ent["response"]
            n = self.takedowns.get(nid)
            if n is None:
                raise NotFound("no such notice")
            if n["status"] != "actioned":
                raise Refused("TAKEDOWN_STATE", reasons=[R.item("TAKEDOWN_STATE", f"a counter-notice applies to an "
                              f"actioned valid notice (this one is {n['status']})")], recorded=False)
            received = self._today()
            lo, hi = self._restore_bounds()
            try:
                nb, na = self.cal.restore_window(received, lo, hi)
            except HolidaysUnknown as exc:
                raise Refused("HOLIDAYS_UNKNOWN", reasons=[R.item("HOLIDAYS_UNKNOWN", str(exc)[:180])], recorded=False)
            cn = {"received_at": iso(self._now()), "received_date": received.isoformat(),
                  "restore_not_before": nb.isoformat(), "restore_not_after": na.isoformat(),
                  "claimant_filed_action": False, "checklist_sha256": facts_sha256(body.get("checklist") or {}),
                  "checklist_status": "UNVERIFIED (512(g)(3) elements not in research; counsel confirms)"}
            op = Op(self, f"cnt|{request_id}", i07.ACTOR, f"takedown:{nid}")
            op.ev("cnr", "counter_notice_received", f"takedown:{nid}", {"notice_id": nid, "received_date": cn["received_date"]},
                  "Counter-notice received")
            op.ev("rws", "restore_window_set", f"takedown:{nid}", {"notice_id": nid, "restore_not_before": cn["restore_not_before"],
                                                                   "restore_not_after": cn["restore_not_after"]},
                  f"Restore window {cn['restore_not_before']} .. {cn['restore_not_after']}")
            op.update("takedowns", nid, {"status": "counter_noticed", "counter_notice": cn})
            return self._answer(op, key, h, {"notice_id": nid, "status": "counter_noticed", **cn})

    def claimant_action(self, principal: str, request_id: str, nid: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem(principal, request_id, f"takedowns/{nid}/claimant-action", body)
            if ent:
                return ent["response"]
            n = self.takedowns.get(nid)
            if n is None:
                raise NotFound("no such notice")
            if n["status"] != "counter_noticed":
                raise Refused("TAKEDOWN_STATE", reasons=[R.item("TAKEDOWN_STATE", "a claimant action applies after a "
                              "counter-notice")], recorded=False)
            op = Op(self, f"cla|{request_id}", i07.ACTOR, f"takedown:{nid}")
            m = self._open_matter(op, "litigation_threat", {"deadline_stated": True}, None, "hub", principal,
                                  [f"post:{n['target']['post_ref_sha256']}"], [], [], {})
            reasons = m.pop("_reasons")
            op.ev("tcl", "claimant_action_recorded", f"takedown:{nid}", {"notice_id": nid, "matter_id": m["matter_id"],
                                                                         "court_ref_sha256": body.get("court_ref_sha256")},
                  "Claimant filed an action: restore blocked")
            op.update("takedowns", nid, {"status": "litigated",
                                         "counter_notice": {**n["counter_notice"], "claimant_filed_action": True}})
            return self._answer(op, key, h, {"notice_id": nid, "status": "litigated", "matter_id": m["matter_id"],
                                             "hold_ids": m["hold_ids"], "reasons": reasons,
                                             "reason_lines": R.lines(reasons)})

    def restore(self, principal: str, request_id: str, nid: str) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem(principal, request_id, f"takedowns/{nid}/restore", {})
            if ent:
                return ent["response"]
            n = self.takedowns.get(nid)
            if n is None:
                raise NotFound("no such notice")
            if n["status"] == "litigated":
                raise Refused("CLAIMANT_ACTION_FILED", reasons=[R.item("CLAIMANT_ACTION_FILED", "the claimant filed an "
                              "action: no restore")], recorded=False)
            if n["status"] != "counter_noticed":
                raise Refused("TAKEDOWN_STATE", reasons=[R.item("TAKEDOWN_STATE", "restore follows a counter-notice")],
                              recorded=False)
            today = self._today()
            cn = n["counter_notice"]
            if today < date.fromisoformat(cn["restore_not_before"]):
                self._refuse("restore_refused", f"takedown:{nid}", [R.item("RESTORE_WINDOW_NOT_OPEN", "restore is not "
                             f"allowed before {cn['restore_not_before']} (10th business day)")], op_id=request_id)
            reasons = []
            if today > date.fromisoformat(cn["restore_not_after"]):
                reasons.append(R.item("RESTORE_LATE", f"restored after {cn['restore_not_after']} (14th business day)"))
            op = Op(self, f"rst|{request_id}", i07.ACTOR, f"takedown:{nid}")
            op.ev("trs", "takedown_restored", f"takedown:{nid}", {"notice_id": nid, "restored_on": today.isoformat(),
                                                                  "late": bool(reasons)}, "Material restored after the window")
            op.update("takedowns", nid, {"status": "restored"})
            return self._answer(op, key, h, {"notice_id": nid, "status": "restored", "reasons": reasons,
                                             "reason_lines": R.lines(reasons)})

    def withdraw_notice(self, principal: str, request_id: str, nid: str) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem(principal, request_id, f"takedowns/{nid}/withdraw", {})
            if ent:
                return ent["response"]
            n = self.takedowns.get(nid)
            if n is None:
                raise NotFound("no such notice")
            if n["status"] not in ("actioned", "counter_noticed"):
                raise Conflict(f"notice is {n['status']}")
            op = Op(self, f"twd|{request_id}", i07.ACTOR, f"takedown:{nid}")
            op.ev("twd", "takedown_withdrawn", f"takedown:{nid}", {"notice_id": nid}, "Claimant withdrew the notice")
            op.update("takedowns", nid, {"status": "withdrawn"})
            return self._answer(op, key, h, {"notice_id": nid, "status": "withdrawn"})

    def takedown_count(self, post_ref_sha256: str) -> dict:
        with self.lock:
            if self.current is None:
                return {"post_ref_sha256": post_ref_sha256, "available": False, "notices": 0,
                        "rules_pinned": self.rules_pinned, "reason": R.line(R.rules_not_in_force())}
            n = sum(1 for x in self.takedowns.values() if i07.counts(x) and x["target"]["post_ref_sha256"] == post_ref_sha256)
            return {"post_ref_sha256": post_ref_sha256, "available": True, "notices": n, "rules_pinned": self.rules_pinned}

    def takedown_view(self, nid: str) -> dict:
        with self.lock:
            n = self.takedowns.get(nid)
            if n is None:
                raise NotFound("no such notice")
            return dict(n)

    def outbound_notice(self, request_id: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, "takedowns/outbound", body)
            if ent:
                return ent["response"]
            tpl = self._template("dmca_procedure") or self._current("dmca_procedure")
            if tpl is None:
                raise Refused("NO_APPROVED_TEMPLATE", reasons=[R.item("NO_APPROVED_TEMPLATE", "outbound notices are "
                              "drafted only from the counsel-approved dmca_procedure template")], recorded=False)
            try:
                values = i01.check_variables(tpl["template_variables"] or {}, body["variables"])
            except ValueError as exc:
                raise Invalid(str(exc)) from None
            oid = rid("out", request_id)
            self._guard([values[n] for n, s in (tpl["template_variables"] or {}).items() if s["type"] == "string"],
                        f"outbound:{oid}", "an outbound notice variable", request_id)
            op = Op(self, f"out|{oid}", "andre", f"outbound:{oid}")
            if body["license_or_fair_use_possible"] and not (body.get("counsel_memo_id") in self.memos):
                op.call("counsel_channel", "deliver", (oid,), lambda: self.ports.counsel.deliver(
                    {"outbound_id": oid, "post_ref_sha256": body["target"]["post_ref_sha256"]}), Delivery(False))
                op.add("outbound", {"outbound_id": oid, "status": "needs_counsel", "target": dict(body["target"]),
                                    "at": iso(self._now())})
                self._commit(op)
                raise Refused("OUTBOUND_NEEDS_COUNSEL", reasons=[R.item("OUTBOUND_NEEDS_COUNSEL", "a licence or fair "
                              "use is possible: counsel reviews before any notice goes out (512(f))")],
                              recorded=True, outbound_id=oid)
            tdata = self.blobs.get(tpl["sha256"])
            if tdata is None:
                raise Unavailable("template text blob not readable")
            rendered = i01.render(tdata.decode("utf-8"), values).encode("utf-8")
            rsha = op.blob(rendered, "outbound", [f"outbound:{oid}", f"post:{body['target']['post_ref_sha256']}"])
            rec = {"outbound_id": oid, "status": "ready_not_delivered", "target": dict(body["target"]),
                   "template": {"doc_id": "dmca_procedure", "version": tpl["version"], "sha256": tpl["sha256"]},
                   "rendered_sha256": rsha, "signed_by": "andre", "counsel_memo_id": body.get("counsel_memo_id"),
                   "at": iso(self._now())}
            op.ev("ons", "outbound_notice_sent", f"outbound:{oid}", {k: rec[k] for k in ("outbound_id", "template",
                                                                                         "rendered_sha256", "signed_by",
                                                                                         "counsel_memo_id")} |
                  {"delivery": "not_wired"}, "Outbound DMCA notice signed by Andre (no delivery channel wired)")
            op.add("outbound", rec)
            return self._answer(op, key, h, {"outbound_id": oid, "status": rec["status"], "rendered_sha256": rsha,
                                             "template": rec["template"]})

    # ================================================================== filings (§B.9, §C.8)

    def create_filing(self, request_id: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, "filings", body)
            if ent:
                return ent["response"]
            try:
                dates = i08.compute(body["kind"], body, int(self._param("LG-11", "dmca_designation_years", 3)))
            except ValueError as exc:
                raise Invalid(str(exc)) from None
            fid = rid("fil", request_id)
            rec = {"filing_id": fid, "entity": body["entity"], "kind": body["kind"], "reference": body.get("reference"),
                   "filed_on": body.get("filed_on"), **dates, "alert_lead_days": self.cfg.filing_alert_days,
                   "status": "filed" if body.get("filed_on") and body["kind"] == "dmca_agent_designation" else "not_started",
                   "owner": body["owner"], "registration_date": body.get("registration_date"),
                   "announced_open": False, "announced_due": False, "created_at": iso(self._now())}
            op = Op(self, f"fil|{fid}", i08.ACTOR, f"filing:{fid}")
            op.ev("fil", "filing_recorded", f"filing:{fid}", {k: rec[k] for k in ("filing_id", "entity", "kind", "status",
                                                                                   "window_opens", "window_closes",
                                                                                   "expires_on")},
                  f"Filing calendared: {body['kind']}")
            op.add("filing", rec)
            return self._answer(op, key, h, {k: v for k, v in rec.items() if not k.startswith("announced")})

    def filing_ready(self, request_id: str, fid: str) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, f"filings/{fid}/ready", {})
            if ent:
                return ent["response"]
            f = self.filings.get(fid)
            if f is None:
                raise NotFound("no such filing")
            if f["status"] != "not_started":
                raise Conflict(f"filing is {f['status']}")
            if f["kind"] in i08.TRADEMARK_KINDS and not self.cq_verified("CQ-27"):
                raise Refused("CQ_UNVERIFIED", reasons=[R.item("CQ_UNVERIFIED", "trademark filings are ready only after "
                              "counsel answers CQ-27 (classes and clearance)", cq_id="CQ-27")], recorded=False)
            op = Op(self, f"frd|{request_id}", "andre", f"filing:{fid}")
            op.ev("frd", "filing_ready", f"filing:{fid}", {"filing_id": fid}, "Filing marked ready by Andre")
            op.update("filings", fid, {"status": "ready"})
            return self._answer(op, key, h, {"filing_id": fid, "status": "ready"})

    def filing_filed(self, request_id: str, fid: str, body: dict) -> dict:
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem("andre", request_id, f"filings/{fid}/filed", body)
            if ent:
                return ent["response"]
            f = self.filings.get(fid)
            if f is None:
                raise NotFound("no such filing")
            if f["status"] == "filed":
                raise Conflict("filing is already filed")
            fields = {"status": "filed", "filed_on": body["filed_on"], "reference": body["reference"],
                      "announced_due": False}
            if f["kind"] == "dmca_agent_designation":
                fields["expires_on"] = i08.add_years(date.fromisoformat(body["filed_on"]),
                                                     int(self._param("LG-11", "dmca_designation_years", 3))).isoformat()
            op = Op(self, f"ffd|{request_id}", "andre", f"filing:{fid}")
            op.ev("fil", "filing_recorded", f"filing:{fid}", {"filing_id": fid, "status": "filed",
                                                              "filed_on": body["filed_on"],
                                                              "expires_on": fields.get("expires_on")},
                  f"Filing recorded as filed: {f['kind']}")
            op.update("filings", fid, fields)
            return self._answer(op, key, h, {"filing_id": fid, **fields})

    def list_filings(self) -> dict:
        with self.lock:
            return {"items": sorted(({k: v for k, v in f.items() if not k.startswith("announced")}
                                     for f in self.filings.values()), key=lambda f: f["filing_id"]),
                    "rules_pinned": self.rules_pinned}

    # ================================================================== sign-offs and music (§B.11, §C.9)

    def signoff(self, principal: str, request_id: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, "signoffs", body)
            if ent:
                return ent["response"]
            fsha = facts_sha256(body["facts"])
            topic = self.topics.get(body["topic"])
            reasons = []
            if self.current is None:
                reasons.append(R.rules_not_in_force())
            if topic is None:
                reasons.append(R.item("TOPIC_UNKNOWN", "unknown sign-off topic: not allowed"))
            else:
                st = topic["status"] == "verified" and topic.get("review_by") and \
                    self._today() < date.fromisoformat(topic["review_by"])
                if not st:
                    reasons.append(R.item("SIGNOFF_NOT_VERIFIED", f"no standing counsel sign-off for {body['topic']}"))
                else:
                    sk = topic["scope_key"]
                    facts = body["facts"]
                    scope = topic.get("scope") or {}
                    ids = facts.get(sk) if isinstance(facts, dict) else None
                    if not isinstance(ids, list) or set(facts) != {sk} or not all(isinstance(x, str) for x in ids):
                        reasons.append(R.item("SIGNOFF_OUT_OF_SCOPE", f"facts must be exactly {{{sk}: [ids]}}"))
                    elif scope != {"any": True} and not set(ids) <= set(scope.get(sk) or []):
                        reasons.append(R.item("SIGNOFF_OUT_OF_SCOPE", "facts fall outside the standing sign-off's scope"))
            allowed = not reasons
            sid = rid("sgn", principal, request_id)
            reference = topic["memo_id"] if allowed else None
            op = Op(self, f"sgn|{sid}", EVIDENCE, f"signoff:{body['subject_id']}"[:128])
            op.ev("sgn", "signoff_answered", f"signoff:{body['subject_id']}"[:128],
                  {"signoff_id": sid, "topic_sha256": sha(body["topic"]), "allowed": allowed, "facts_sha256": fsha,
                   "reference": reference, "codes": R.codes(reasons)}, f"Sign-off answered: {'allowed' if allowed else 'not allowed'}")
            op.add("signoff", {"signoff_id": sid, "topic": body["topic"] if topic is not None else "unknown",
                               "topic_sha256": sha(body["topic"]), "subject_id": body["subject_id"],
                               "allowed": allowed, "facts_sha256": fsha, "reference": reference,
                               "codes": R.codes(reasons), "at": iso(self._now())})
            reason = "allowed: standing counsel sign-off" if allowed else "; ".join(R.lines(reasons))[:400]
            resp = {"department": "legal_37", "allowed": allowed, "reason": reason, "reference": reference,
                    "request_id": request_id, "facts_sha256": fsha, "rules_pinned": self.rules_pinned,
                    "review_label": f"counsel_memo:{reference}" if reference else "unreviewed"}
            return self._answer(op, key, h, resp)

    def music_ruling(self, principal: str, request_id: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, "music/rulings", body)
            if ent:
                return ent["response"]
            fsha = facts_sha256({k: v for k, v in body.items() if k != "request_id"})
            reasons = []
            if self.current is None:
                reasons.append(R.rules_not_in_force())
                allowed = False
            else:
                allowed, reasons = i09.ruling(body, list(self._param("LG-12", "verified_platform_libraries", [])),
                                              self.cq_verified("CQ-21"))
            rid_ = rid("mus", principal, request_id)
            op = Op(self, f"mus|{rid_}", i09.ACTOR, f"{body['subject_kind']}:{body['subject_id']}"[:128])
            op.ev("mus", "music_ruling", op.subject, {"ruling_id": rid_, "allowed": allowed, "facts_sha256": fsha,
                                                      "codes": R.codes(reasons), "platform": body["platform"],
                                                      "cq21_memo_id": self.cq_memo("CQ-21")},
                  f"Music ruling: {'allowed' if allowed else 'blocked'}")
            op.call("creative_production", "notify", ("music_ruling", rid_),
                    lambda: self.ports.creative_production.notify("music_ruling", rid_, {"allowed": allowed}),
                    Delivery(False))
            op.add("ruling", {"ruling_id": rid_, "subject_kind": body["subject_kind"], "subject_id": body["subject_id"],
                              "allowed": allowed, "codes": R.codes(reasons), "facts_sha256": fsha,
                              "requested_by": principal, "at": iso(self._now())})
            label = f"counsel_memo:{self.cq_memo('CQ-21')}" if self.cq_memo("CQ-21") else "unreviewed"
            return self._answer(op, key, h, {"allowed": allowed, "ruling_id": rid_, "reasons": reasons,
                                             "reason_lines": R.lines(reasons), "request_id": request_id,
                                             "facts_sha256": fsha, "rules_pinned": self.rules_pinned,
                                             "review_label": label})

    # ================================================================== retention (§B.10)

    def retention_view(self) -> dict:
        with self.lock:
            return {"classes": [{**v, "hold_override": True} for _, v in sorted(self.retention.items())],
                    "rules_pinned": self.rules_pinned}

    @staticmethod
    def _period_end(start: datetime, period: str) -> Optional[datetime]:
        m = PERIOD_RE.fullmatch(period or "")
        if not m:
            return None
        n, unit = int(m.group(1)), m.group(2)
        days = n * 365 if unit == "Y" else n * 30 if unit == "M" else n
        return start + timedelta(days=days)

    # ================================================================== jobs

    def run_job(self, principal: str, request_id: str, job: str) -> dict:
        if job not in JOBS:
            raise NotFound("no such job")
        with self.lock:
            self._require_rules()
            key, h, ent = self._idem(principal, request_id, f"jobs/{job}", {})
            if ent:
                return ent["response"]
            day = self._now().date().isoformat()
            done = self.job_runs.get(f"{job}|{day}")
            if done is not None:
                return {"job": job, "day": day, "already_ran": True, "summary": done["summary"]}
        pre = {}
        if job == "proposal-delivery":       # remote calls outside the lock (N16-1)
            with self.lock:
                pending = [p["cproposal_id"] for p in self.cproposals.values() if p["status"] == "pending_delivery"]
            pre = {"attempted": self.deliver_proposals(pending), "confirmed": self._confirm_compliance_rows()}
        with self.lock:
            done = self.job_runs.get(f"{job}|{day}")
            if done is not None:
                return {"job": job, "day": day, "already_ran": True, "summary": done["summary"]}
            summary = pre if job == "proposal-delivery" else getattr(self, "_job_" + job.replace("-", "_"))()
            op = Op(self, f"job|{job}|{day}", EVIDENCE, f"job:{job}")
            op.ev("job", "job_cycle_completed", f"job:{job}", {"job": job, "day": day, "summary_sha256": sha(summary)},
                  f"Scheduler job {job} ran for {day}")
            op.add("job_run", {"job": job, "day": day, "at": iso(self._now()), "summary": summary})
            return self._answer(op, key, h, {"job": job, "day": day, "already_ran": False, "summary": summary})

    def _job_obligations(self) -> dict:
        today = self._today()
        out = {"due_soon": 0, "missed": 0}
        for o in sorted(self.obligations.values(), key=lambda x: x["obligation_id"]):
            new = i04.next_status(o, today)
            if new is None:
                continue
            op = Op(self, f"obj|{o['obligation_id']}|{new}", i04.ACTOR, f"obligation:{o['obligation_id']}")
            op.ev("obs", f"obligation_{new}", f"obligation:{o['obligation_id']}",
                  {"obligation_id": o["obligation_id"], "due": o["due"], "owner": o["owner_department"]},
                  f"Obligation {o['obligation_code']} {new.replace('_', ' ')}")
            owner = o["owner_department"]
            if owner not in ("andre", "legal_37"):
                port = getattr(self.ports, owner)
                op.call(owner, "notify", (f"obligation_{new}", o["obligation_id"]),
                        lambda p=port: p.notify(f"obligation_{new}", o["obligation_id"], {"due": o["due"]}),
                        Delivery(False))
            op.call("push", "notify", (f"obligation_{new}", o["obligation_id"]),
                    lambda: self.ports.push.notify(f"obligation_{new}", o["obligation_id"], {"due": o["due"]}),
                    Delivery(False))
            op.update("obligations", o["obligation_id"], {"status": new})
            self._commit(op)
            out[new] += 1
        return out

    def _job_filings(self) -> dict:
        today = self._today()
        out = {"window_open": 0, "due_soon": 0, "lapsed": 0}
        for f in sorted(self.filings.values(), key=lambda x: x["filing_id"]):
            for evt in i08.evaluate(f, today):
                op = Op(self, f"fjb|{f['filing_id']}|{evt}|{today}", i08.ACTOR, f"filing:{f['filing_id']}")
                op.ev("fjb", f"filing_{evt}", f"filing:{f['filing_id']}",
                      {"filing_id": f["filing_id"], "kind": f["kind"], "deadline": str(i08.deadline(f))},
                      f"Filing {f['kind']}: {evt.replace('_', ' ')}")
                fields: dict = {"window_open": {"announced_open": True}, "due_soon": {"announced_due": True},
                                "lapsed": {"status": "lapsed"}}[evt]
                if evt in ("due_soon", "lapsed"):
                    op.call("push", "notify", (f"filing_{evt}", f["filing_id"]),
                            lambda e=evt: self.ports.push.notify(f"filing_{e}", f["filing_id"], {"kind": f["kind"]}),
                            Delivery(False))
                if evt == "lapsed":
                    m = self._open_matter(op, "filing_lapsed", {}, None, "department", "legal_37",
                                          [f"filing:{f['filing_id']}"], [], [], {})
                    m.pop("_reasons")
                    fields["matter_id"] = m["matter_id"]
                op.update("filings", f["filing_id"], fields)
                self._commit(op)
                out[evt] += 1
        return out

    def _job_holds_renotice(self) -> dict:
        today = self._today()
        n = 0
        for hold in sorted(self.holds.values(), key=lambda x: x["hold_id"]):
            if hold["status"] != "active" or today < date.fromisoformat(hold["renotice_due"]):
                continue
            op = Op(self, f"hrn|{hold['hold_id']}|{today}", i06.ACTOR, f"hold:{hold['hold_id']}")
            op.call("people_43", "notify", ("hold_renotice", hold["hold_id"]),
                    lambda: self.ports.people_43.notify("hold_renotice", hold["hold_id"], {}), Delivery(False))
            nxt = (today + timedelta(days=hold["renotice_every_days"])).isoformat()
            op.ev("hrn", "hold_renoticed", f"hold:{hold['hold_id']}", {"hold_id": hold["hold_id"], "next": nxt},
                  "Hold re-notice issued")
            op.update("holds", hold["hold_id"], {"renotice_due": nxt})
            self._commit(op)
            n += 1
        return {"renoticed": n}

    def _job_retention(self) -> dict:
        now = self._now()
        out = {"deleted": 0, "blocked_unverified": 0, "blocked_by_hold": 0, "not_due": 0}
        for h_, meta in sorted(self.blob_meta.items()):
            if meta.get("deleted"):
                continue
            # AEGIS N17-14: every class the blob was ever stored under must be verified and past its period,
            # counted from the LAST writer (a later subject's copy restarts the clock)
            since = max(parse_iso(meta["stored_at"]), parse_iso(meta.get("last_ref_at") or meta["stored_at"]))
            ends, verified = [], True
            for cname in meta.get("classes") or [meta["class"]]:
                cls = self.retention.get(cname)
                ok = cls is not None and cls["status"] == "verified" and cls.get("review_by") and \
                    self._today() < date.fromisoformat(cls["review_by"])
                e_ = self._period_end(since, cls["period"]) if cls else None
                verified = verified and bool(ok) and e_ is not None
                ends.append(e_)
            end = max(ends) if verified and ends else None
            cls = self.retention.get(meta["class"])
            if not verified or end is None:
                out["blocked_unverified"] += 1
                continue
            if now < end:
                out["not_due"] += 1
                continue
            held = self._held(meta["subject_refs"])
            if held:
                op = Op(self, f"dbh|{h_}|{now.date()}", EVIDENCE, "retention")
                op.ev("dbh", "deletion_blocked_by_hold", "retention",
                      {"blob_sha256": h_, "class": meta["class"], "hold_ids": held}, "Deletion blocked by an active hold")
                op.add("retention_block", {"blob_sha256": h_, "hold_ids": held, "at": iso(now)})
                self._commit(op)
                out["blocked_by_hold"] += 1
                continue
            op = Op(self, f"del|{h_}", EVIDENCE, "retention")
            op.ev("del", "blob_deleted", "retention", {"blob_sha256": h_, "class": meta["class"],
                                                        "period": cls["period"], "memo_id": cls["memo_id"]},
                  f"Retention: a {meta['class']} blob deleted")
            op.update("blobs", h_, {"deleted": True, "deleted_at": iso(now)})
            op.after.append(lambda x=h_: self.blobs.delete(x))
            self._commit(op)
            out["deleted"] += 1
        return out

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

    def audit_evidence(self, limit: int, offset: int, event_type: Optional[str] = None) -> dict:
        """``GET /legal/v1/audit/evidence`` (bug sweep E; bizdev-py R6-M1 / R7): every evidence event this department
        holds on the ledger, each marked

        * ``committed`` -- an ANCHORED local line names it: the ledger holds that line's ``local_log_appended``
          anchor (epoch, seq, line SHA-256, kind) and, for typed evidence the line lists in ``data.evidence``, the
          payload carries ``rk`` = the line's request key and ``seq`` = the line's seq, and the ledger's
          ``payload_sha256`` is that payload's. Events with a content-derived id (lease, reconcile, rules proposals,
          decisions and versions) and lines written before this change are named by ``ledger_event_ids`` only:
          committed when that anchored line names them and the ledger holds the event;
        * ``attempted`` -- anything else: recorded first, but its state change never reached the anchored log.

        Unanchored evidence = attempted, not done. Under the service lock only the raw lines not yet seen are
        copied; they are parsed and hashed outside it and cached by log length; the ledger is read outside it.
        Eventually consistent: a commit in flight may show as ``attempted``; re-read to settle."""
        with self._evidence_lock:
            with self.lock:
                if self._closed:
                    raise Unavailable("this service instance is closed")
                cache = self._evidence_cache
                if len(self.log) < cache["n"] or cache.get("log") is not self.log:
                    cache = self._evidence_cache = {"n": 0, "epoch": None, "lines": [], "log": self.log}
                new = self.log.raw_lines(cache["n"])
            for raw in new:
                r = json.loads(raw)
                line_sha = sha_bytes(raw)
                if cache["epoch"] is None:
                    cache["epoch"] = line_sha[:16]
                d = r.get("data") or {}
                cache["lines"].append((r["seq"], line_sha, r.get("kind"), d.get("rk"), d.get("evidence") or [],
                                       list(d.get("ledger_event_ids") or [])))
                cache["n"] += 1
            epoch, lines, n_lines = cache["epoch"], list(cache["lines"]), cache["n"]
        try:
            entries = self._ledger_entries()
        except LedgerQueryFailed:
            raise Unavailable("the evidence ledger cannot be read") from None
        mine = [e for e in entries if e.get("department") == DEPARTMENT]
        anchors = {e.get("event_id"): e for e in mine if e.get("event_type") == i10.ANCHOR_TYPE}
        typed: dict[str, tuple] = {}      # event id -> (seq, rk, payload, kind) from an anchored line's evidence
        named: dict[str, tuple] = {}      # event id -> (seq, kind) from an anchored line's ledger_event_ids
        for seq, line_sha, kind, line_rk, evs, ids in lines:
            a = anchors.get(i10.anchor_id(epoch, seq, line_sha))
            want = {"epoch": epoch, "seq": seq, "line_sha256": line_sha, "kind": kind}
            if a is None or a.get("payload_sha256") != payload_sha256(want) or a.get("subject_id") != i10.LOG_SUBJECT:
                continue
            for ev in evs:
                typed[ev.get("event_id")] = (seq, line_rk, ev.get("payload") or {}, kind)
            for eid in ids:
                named.setdefault(eid, (seq, kind))
        out, counts = [], {"committed": 0, "attempted": 0}
        for e in mine:
            et = e.get("event_type")
            if et == i10.ANCHOR_TYPE or (event_type is not None and et != event_type):
                continue
            row = {"event_id": e.get("event_id"), "event_type": et, "subject_id": e.get("subject_id"),
                   "ledger_seq": e.get("seq"), "payload_sha256": e.get("payload_sha256"), "status": "attempted",
                   "seq": None, "rk": None, "log_kind": None}
            eid = e.get("event_id")
            if eid in typed:
                seq, line_rk, payload, kind = typed[eid]
                if payload.get("seq") == seq and payload.get("rk") == line_rk \
                        and payload_sha256(payload) == e.get("payload_sha256"):
                    row.update(status="committed", seq=seq, rk=line_rk, log_kind=kind)
            elif eid in named:
                seq, kind = named[eid]
                row.update(status="committed", seq=seq, log_kind=kind)
            counts[row["status"]] += 1
            out.append(row)
        return {"rule": "unanchored evidence = attempted, not done", "consistency": "eventual; re-read to settle",
                "total": len(out), **counts, "limit": limit, "offset": offset,
                "evidence": out[offset:offset + limit], "log_length": n_lines}

    def health(self) -> dict:
        status = "closed" if self._closed else ("degraded" if self.log.fault else "ok")   # bug sweep E
        return {"status": status, "service": "legal-py", "rules_version": self.rules_version,
                "in_memory": self.log.in_memory, "rules_pinned": self.rules_pinned,
                "log_write_fault": bool(self.log.fault),
                "counsel_channel_wired": not type(self.ports.counsel).__name__.startswith("NotWired"),
                "reconcile_mode": self.reconcile_mode, "reconcile_required": bool(self.reconcile_required)}
