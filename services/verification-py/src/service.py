"""
Verification and Integrity service layer: state, record-first plumbing, every operation.

Order of every state change (spec §A, §E; ADR 0006 decisions 4-5 pattern), without exception:
  1. every port call is recorded on the ledger first (``crossing_<port>_requested``, ids and hashes only);
  2. the operation's own ledger events are recorded (deterministic ``vi-<abbrev>-<40 hex>`` ids);
  3. ONE local-log line holding all of the operation's records is anchored on the ledger, then appended
     (fsynced) — an operation is atomic locally (ADR 0007 choice 4);
  4. only then is it applied to memory (and the purgeable platform-data side store) and answered.
A failure at 1-3 raises ``Unavailable`` (HTTP 503): nothing was issued. State is event-sourced from the log.
One lock serializes every operation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

import platforms as P
import reasons as R
import rules as RU
from adapters.base import caption_sha256
from clock import Clock, SystemClock, iso, parse_iso
from config import Settings
from errors import Conflict, Invalid, NotFound, Unavailable
from intelligences import (i01_platform_metrics as i01, i02_view_certifier as i02, i03_clip_fingerprint as i03,
                           i04_liveness as i04, i05_age_assurance as i05, i06_engagement_anomaly as i06,
                           i07_duplicate_identity as i07, i08_stolen_content as i08, i09_strike_ledger as i09,
                           i10_evidence_audit as i10)
from ledger import LedgerConflict, LedgerQueryFailed, LedgerRecordError, Recorder, canonical, derived_id, payload_sha256
from models import ID_RE
from ports import (AccountAnswer, AdapterAnswer, AgeProviderAnswer, NotBuiltClipperNetwork, NotBuiltFinance31,
                   NotBuiltLegal37, NotBuiltPeople43, NotWiredAdapter, NotWiredAgeAssuranceProvider,
                   NotWiredCompliance, NotWiredMediaIntake, NotWiredOEmbed, NotWiredPerceptualHasher,
                   NotWiredTokenVault, PayoutIdentity, RegisterRow, TakedownAnswer, VaultStore, ViewCap)
from sidestore import PlatformDataStore, SideStoreError
from store import RecordLog, StoreWriteError
from textguard import injection_rules_in

IDEMPOTENCY_WINDOW = timedelta(minutes=15)
IDEMPOTENCY_MAX = 200_000
OAUTH_STATE_TTL = timedelta(minutes=10)
FEED_PAGE = 500
ADAPTER_VERSION = "verification-py/0.1"
DEFAULT_LAG_DAYS = 14
EVIDENCE = i10.ACTOR
JOBS = ("liveness", "metrics", "revisions", "anomaly", "certify", "retention")   # the daily order the scheduler uses
# rules a positive certification relies on (their Compliance basis rows must be in force: VI-21)
CERTIFY_RULES = ("VI-01", "VI-03", "VI-04", "VI-06", "VI-07", "VI-08", "VI-09", "VI-10", "VI-11", "VI-12", "VI-13",
                 "VI-14", "VI-17", "VI-18", "VI-19", "VI-20", "VI-21")
AGE_RULES = ("VI-11", "VI-21")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
HOLD_CODES_FROM_FINDING = {"bought_engagement": "BOUGHT_ENGAGEMENT", "platform_stripped": "PLATFORM_STRIPPED",
                           "duplicate_identity": "DUPLICATE_IDENTITY", "account_shared": "ACCOUNT_SHARED",
                           "stolen_content": "STOLEN_MATCH"}


def rid(prefix: str, *parts: Any) -> str:
    """ULID-shaped id: ``vi-<prefix>-`` + 26 Crockford base32 characters, derived deterministically from the
    record's identity (ADR 0007 choice 5: deterministic so retries are idempotent; not time-sortable)."""
    d = hashlib.sha256(canonical(list(parts)).encode("utf-8", "surrogatepass")).digest()
    n = int.from_bytes(d[:17], "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[n & 31])
        n >>= 5
    return f"vi-{prefix}-" + "".join(out)


def sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8", "surrogatepass")).hexdigest()


def sha_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()


def facts_sha256(facts: Any) -> str:
    """What the thin clients compute over what they sent: sorted keys, compact separators, ASCII escapes."""
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8", "surrogatepass")).hexdigest()


def _dt(v: str) -> datetime:
    return parse_iso(v)


@dataclass
class Ports:
    vault: Any = field(default_factory=NotWiredTokenVault)
    adapters: dict = field(default_factory=lambda: {p: NotWiredAdapter(p) for p in P.CERTIFIABLE})
    oembed: Any = field(default_factory=NotWiredOEmbed)
    hasher: Any = field(default_factory=NotWiredPerceptualHasher)
    media: Any = field(default_factory=NotWiredMediaIntake)
    age: Any = field(default_factory=NotWiredAgeAssuranceProvider)
    compliance: Any = field(default_factory=NotWiredCompliance)
    legal: Any = field(default_factory=NotBuiltLegal37)
    finance: Any = field(default_factory=NotBuiltFinance31)
    people: Any = field(default_factory=NotBuiltPeople43)
    clipper_network: Any = field(default_factory=NotBuiltClipperNetwork)


class Op:
    """One operation: its ledger events, its local-log records, side-store mutations applied after commit."""

    def __init__(self, svc: "VIService", op_id: str, actor: str, subject: str):
        self.svc, self.op_id, self.actor, self.subject = svc, op_id, actor, subject
        self.events: list[str] = []
        self.ops: list[tuple[str, dict]] = []
        self.side: list[Callable[[], None]] = []
        self._memo: dict = {}
        self.key: Optional[bytes] = None
        self._key_asked = False

    # --- ledger + records
    def record(self, event_id: str, event_type: str, actor: str, subject: str, payload: dict, summary: str) -> str:
        self.svc._record(event_id, event_type, actor, subject, payload, summary)
        self.events.append(event_id)
        return event_id

    def add(self, kind: str, rec: dict) -> dict:
        self.ops.append((kind, rec))
        return rec

    # --- ports (record-first crossings)
    def call(self, port: str, action: str, args: tuple, fn: Callable, fallback: Any) -> Any:
        k = (port, action, canonical(list(args)))
        if k in self._memo:
            return self._memo[k]
        eid = derived_id("x", self.op_id, port, action, list(args), len(self.events))
        self.record(eid, f"crossing_{port}_requested", self.actor, self.subject[:128],
                    {"port": port, "action": action, "args_sha256": sha(list(args)), "op": self.op_id},
                    f"Request to {port}: {action}")
        try:
            ans = fn()
        except Exception:  # noqa: BLE001 - a port that raises is unavailable, never a pass; its text is dropped
            ans = fallback
        self._memo[k] = ans
        return ans

    def identity_key(self) -> Optional[bytes]:
        if not self._key_asked:
            self._key_asked = True
            v = self.svc.ports.vault
            k = self.call("vault", "identity_hmac_key", (), lambda: v.identity_hmac_key(), None)
            self.key = k if isinstance(k, (bytes, bytearray)) and len(k) >= 16 else None
        return self.key


class VIService:
    def __init__(self, settings: Settings, recorder: Recorder, log: RecordLog, side: PlatformDataStore,
                 seed_bytes: bytes, expected_seed_sha256: str, ports: Optional[Ports] = None,
                 clock: Optional[Clock] = None, pinned_sha256: Optional[str] = None):
        self.cfg = settings
        self.recorder = recorder
        self.log = log
        self.side = side
        self.ports = ports or Ports()
        self.clock = clock or SystemClock()
        self.lock = threading.RLock()
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
        self.connections: dict[str, dict] = {}
        self.pending_oauth: dict[str, dict] = {}      # memory only: state sha -> {verifier, redirect_uri, ...}
        self.submissions: dict[str, dict] = {}
        self.fingerprints: dict[str, dict] = {}
        self.fetches: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}
        self.sub_snapshots: dict[str, list[str]] = {}
        self.sub_fetches: dict[str, list[str]] = {}
        self.liveness: dict[tuple[str, str], dict] = {}
        self.certs: dict[str, dict] = {}
        self.cert_by_sub: dict[str, str] = {}
        self.clawbacks: dict[str, dict] = {}
        self.holds: dict[str, dict] = {}
        self.findings: dict[str, dict] = {}
        self.strikes: dict[str, dict] = {}
        self.bans: dict[str, dict] = {}
        self.banned: set[tuple[str, str]] = set()
        self.ages: dict[str, dict] = {}
        self.latest_age: dict[str, str] = {}
        self.identities: dict[str, dict] = {}
        self.hmac_owner: dict[tuple[str, str], str] = {}
        self.clipper_hmacs: dict[str, set] = {}
        self.attestations: dict[str, dict] = {}
        self.job_runs: dict[tuple[str, str], dict] = {}
        self.screens: dict[str, dict] = {}
        self.quota = i01.QuotaBook(settings)
        self.idem: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
        self.sidestore_degraded = False
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
        if not self.log.in_memory:
            try:
                a = self.assess_log()
            except LedgerQueryFailed as exc:
                raise RuntimeError(f"refusing to start: the local log cannot be verified against the evidence "
                                   f"ledger ({exc})") from None
            if a.fatal or (a.voidable and not self.reconcile_mode):
                hint = ("" if a.fatal or not a.voidable else
                        " -- only Andre can void these: start with VI_RECONCILE_MODE=1 and POST /vi/v1/reconcile "
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

    def _record(self, event_id, event_type, actor, subject, payload, summary) -> str:
        self._ledger_conflict = False
        if self.reconcile_mode and not self._reconciling:
            raise Unavailable("reconcile mode (VI_RECONCILE_MODE=1): only Andre's POST /vi/v1/reconcile is answered; "
                              "restart without it once the log is reconciled", ledger_write="not_recorded")
        try:
            return self.recorder.record(event_id, event_type, actor, subject, payload, summary)
        except LedgerRecordError as exc:
            self._ledger_conflict = isinstance(exc, LedgerConflict)
            raise Unavailable(f"evidence ledger write failed ({type(exc).__name__}); nothing was issued",
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
            raise Unavailable(f"local store write failed ({exc}); nothing was issued") from None
        for kind, r in op.ops:
            self._apply(kind, r)
        if op.side:
            for fn in op.side:
                fn()
            try:
                self.side.save()
            except SideStoreError:
                self.sidestore_degraded = True

    def _apply(self, kind: str, r: dict) -> None:
        if kind == "proposal":
            self.proposals[r["proposal_id"]] = {**r, "status": "open", "decided_at": None}
        elif kind == "decision":
            for d in r["decisions"]:
                p = self.proposals[d["proposal_id"]]
                p.update(status="approved" if d["decision"] == "approve" else "rejected", decided_at=r["decided_at"])
            v = r.get("version")
            if v:
                self.versions.append(RU.Version(v["version"], v["created_at"], v["approved_by"], tuple(v["proposal_ids"]),
                                                v["rows_sha256"], v["prev_version_sha256"], tuple(v["rows"])))
        elif kind == "connection":
            self.connections[r["connection_id"]] = r
            if r.get("status") == "active" and r.get("platform_account_id_hmac"):
                key = (f"account:{r['platform']}", r["platform_account_id_hmac"])
                self.hmac_owner.setdefault(key, r["clipper_id"])
                self.clipper_hmacs.setdefault(r["clipper_id"], set()).add(key)
        elif kind == "submission":
            self.submissions[r["submission_id"]] = r
        elif kind == "submission_update":
            self.submissions[r["submission_id"]].update(r["fields"])
        elif kind == "fingerprint":
            self.fingerprints[r["submission_id"]] = r
        elif kind == "fetch":
            self.fetches[r["fetch_id"]] = r
            self.sub_fetches.setdefault(r["submission_id"], []).append(r["fetch_id"])
            if r.get("quota") and self._replaying:
                self._apply_quota(r["quota"])   # live calls were counted when they were made
        elif kind == "snapshot":
            self.snapshots[r["snapshot_id"]] = r
            self.sub_snapshots.setdefault(r["submission_id"], []).append(r["snapshot_id"])
        elif kind == "liveness":
            self.liveness[(r["submission_id"], r["day"])] = r
        elif kind == "certification":
            self.certs[r["certification_id"]] = r
            self.cert_by_sub[r["submission_id"]] = r["certification_id"]
        elif kind == "clawback":
            self.clawbacks[r["clawback_id"]] = r
        elif kind == "hold":
            self.holds[r["hold_id"]] = r
        elif kind == "finding":
            self.findings[r["finding_id"]] = r
        elif kind == "strike":
            self.strikes[r["strike_id"]] = r
        elif kind == "ban":
            self.bans[r["clipper_id"]] = r
            for k in r["blocked"]:
                self.banned.add(tuple(k))
        elif kind == "age":
            self.ages[r["attestation_id"]] = r
            self.latest_age[r["subject_id"]] = r["attestation_id"]
        elif kind == "identity":
            self.identities[r["clipper_id"]] = r
            for k, h in r["hmacs"].items():
                if h:
                    self.hmac_owner.setdefault((k, h), r["clipper_id"])
                    self.clipper_hmacs.setdefault(r["clipper_id"], set()).add((k, h))
        elif kind == "attestation":
            self.attestations[r["attestation_id"]] = r
            if r.get("request_sha256"):
                key = (r["principal"], r["request_id"])
                self.idem[key] = {"h": r["request_sha256"], "at": parse_iso(r["first_used_at"]),
                                  "response": r["response"], "outcome_sha256": r["outcome_sha256"],
                                  "attestation_id": r["attestation_id"]}
                self.idem.move_to_end(key)
                while len(self.idem) > IDEMPOTENCY_MAX:
                    self.idem.popitem(last=False)
        elif kind == "job_run":
            self.job_runs[(r["job"], r["day"])] = r
        elif kind == "screen":
            self.screens[r["submission_id"]] = r
        # lease, reconcile, founder_refused, injection, retention, audit: evidence only

    def _apply_quota(self, q: dict) -> None:
        at = parse_iso(q["at"])
        self.quota.spend(q["platform"], q["units"], at)

    # --- idempotency

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

    def _idem_store(self, key: tuple, h: str, response: dict) -> dict:
        self.idem[key] = {"h": h, "at": self._now(), "response": response}
        self.idem.move_to_end(key)
        while len(self.idem) > IDEMPOTENCY_MAX:
            self.idem.popitem(last=False)
        return response

    # --- log vs ledger (AEGIS N14-4 / N15-1 / N15-2 pattern)

    def assess_log(self) -> i10.Assessment:
        client = self.recorder.client
        if not hasattr(client, "entries"):
            raise LedgerQueryFailed("this ledger client cannot read entries")
        entries = client.entries()
        shas = self.log.line_shas()
        lines, referenced, leases, reconciles = [], set(), [], []
        for rec, s in zip(self.log.iter_records(), shas):
            d = rec["data"]
            lines.append((rec["seq"], s, bool(d.get("anchored"))))
            referenced.update(d.get("ledger_event_ids") or [])
            for kind, r in d.get("ops", []):
                if kind == "lease":
                    leases.append((rec["seq"], r.get("instance_id"), r.get("lease_event_id")))
                elif kind == "reconcile":
                    reconciles.append((rec["seq"], r.get("payload"), r.get("reconcile_event_id"), d.get("rules_version")))
        return i10.assess(entries, self.log.epoch, lines, referenced, self.rules_version or 0,
                          strict=not self.log.in_memory, local_rulings=set(), local_leases=leases, reconciles=reconciles)

    def snapshot_evidence_problems(self, entries: Optional[list] = None) -> list[str]:
        """A1: every snapshot's ledger event must carry the SHA-256 of the snapshot record the log holds."""
        if entries is None:
            entries = self.recorder.client.entries()
        held = {e.get("event_id"): e.get("payload_sha256") for e in entries
                if isinstance(e, dict) and e.get("department") == "verification_integrity"}
        bad = [s["snapshot_id"] for s in self.snapshots.values()
               if held.get(s["ledger_event_id"]) != payload_sha256(self._snapshot_payload(s))]
        return [f"{len(bad)} snapshot(s) whose ledger payload hash differs or is missing (first {bad[0]})"] if bad else []

    def integrity(self) -> dict:
        with self.lock:
            try:
                entries = self.recorder.client.entries()
                a = self.assess_log()
                problems = a.problems + self.snapshot_evidence_problems(entries)
                chain = self.log.verify()
            except LedgerQueryFailed as exc:
                return {"status": "red", "problems": [f"ledger unreadable: {exc}"]}
            if not chain:
                problems.append("local log hash chain does not verify")
            return {"status": "red" if problems else "green", "problems": problems,
                    "log_lines": len(self.log), "rules_version": self.rules_version}

    def _write_lease(self) -> None:
        with self.lock:
            n = len(self.log)
            head = self.log.line_shas()[-1]
            eid = i10.lease_id(self.log.epoch, self.instance_id, n, head)
            op = Op(self, f"lease|{self.instance_id}", EVIDENCE, i10.LOG_SUBJECT)
            op.record(eid, i10.LEASE_TYPE, EVIDENCE, i10.LOG_SUBJECT,
                      {"instance_id": self.instance_id, "epoch": self.log.epoch, "head_seq": n, "head_sha256": head},
                      f"V&I instance lease at log line {n}")
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
                raise Conflict("the local log head moved since you read the plan; read GET /vi/v1/reconcile again")
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
                    "ledger_event_ids": [eid]}
            return self._idem_store(key, h, resp)

    # ================================================================== rules (§B.6)

    @property
    def current(self) -> Optional[RU.Version]:
        return self.versions[-1] if self.versions else None

    @property
    def rules_version(self) -> Optional[int]:
        return self.current.version if self.current else None

    def rules(self) -> dict[str, dict]:
        """Rows used to cite reasons: the version in force, else the seed (for VI-00 before approval)."""
        return self.current.by_id() if self.current else self.seed_by_id

    def ensure_seed_proposal(self) -> None:
        with self.lock:
            if any(p["kind"] == "seed" for p in self.proposals.values()):
                return
            pid = rid("prop", "seed", self.seed_sha)
            p = RU.seed_proposal(self.seed_rows, self.seed_sha, iso(self._now()), pid)
            op = Op(self, pid, "andre", f"proposal:{pid}")
            op.record(derived_id("prop", pid, p["content_sha256"]), "rules_proposal_created", "intel_10_evidence_audit",
                      f"proposal:{pid}", {"proposal_id": pid, "kind": "seed", "content_sha256": p["content_sha256"],
                                          "seed_sha256": self.seed_sha, "rows": len(self.seed_rows)},
                      f"Rules seed loaded ({len(self.seed_rows)} rules); seed proposal created for Andre")
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
            self._commit(op)
            return self._idem_store(key, h, {"proposal": p, "ledger_event_ids": op.events})

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
                                               f"V&I rules version {n} published ({len(new_rows)} rules)")
                op.events.append(vid)
                version_meta = {**meta, "rows": new_rows}
            op.add("decision", {"request_id": request_id, "decided_at": iso(now), "version": version_meta,
                                "decisions": [{"proposal_id": d["proposal_id"], "decision": d["decision"],
                                               "note": d.get("note"),
                                               "acknowledged_weakening": d.get("acknowledge_weakening") is True}
                                              for d, _ in chosen]})
            self._commit(op, after_anchor=publish)
            return self._idem_store(key, h, {"decided": len(chosen), "approved": len(approvals),
                                             "rules_version": self.rules_version,
                                             "rules_sha256": self.current.rows_sha256 if self.current else None,
                                             "ledger_event_ids": op.events})

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
                      f"Instruction-like text in client/clipper data ignored ({len(found)} pattern(s)); ruling unaffected")
            op.add("injection", {"op_sha256": sha_text(op.op_id), "rules": found})

    # ================================================================== shared checks

    def _rule_status_reasons(self, rule_ids: tuple, op: Op) -> list[dict]:
        """VI-21: V&I rules the ruling relies on must be verified, and every Compliance row they cite must be in
        force (read through the thin client; unavailable → DEPENDENCY_UNAVAILABLE)."""
        rules = self.rules()
        out = []
        basis: set[str] = set()
        for r_id in rule_ids:
            row = rules.get(r_id)
            if row is None:
                out.append(R.item("RULE_NOT_IN_FORCE", f"V&I rule {r_id} is not in the version in force", (), rules))
                continue
            if row["status"] != "verified":
                out.append(R.item("RULE_NOT_IN_FORCE", f"V&I rule {r_id} is unverified", (), rules))
            basis.update(row["basis_obligation_ids"])
        for oid in sorted(basis):
            ans = self._compliance_row(oid, op)
            if not ans.available:
                out.append(R.item("DEPENDENCY_UNAVAILABLE", f"Compliance register row {oid} unavailable", (), rules))
            elif ans.effective_status != "verified":
                out.append(R.item("RULE_NOT_IN_FORCE", f"Compliance row {oid} is {ans.effective_status}", (), rules))
        return out

    def _compliance_row(self, oid: str, op: Op) -> RegisterRow:
        c = self.ports.compliance
        ans = op.call("compliance_38", "register_row", (oid,), lambda: c.row(oid), RegisterRow(False, oid))
        return ans if isinstance(ans, RegisterRow) else RegisterRow(False, oid)

    def _lag(self, op: Op) -> tuple[int, str, Optional[int], list[dict]]:
        """HR-13 settlement_lag_days (§C.2 step 1). Stand-in/unavailable → 14 days, ``settlement_source:
        default`` and DEPENDENCY_UNAVAILABLE (the default only places the window; it never certifies)."""
        rules = self.rules()
        ans = self._compliance_row("HR-13", op)
        if not ans.available:
            return DEFAULT_LAG_DAYS, "default", None, [R.item("DEPENDENCY_UNAVAILABLE", "Compliance HR-13 unavailable: "
                                                              "settlement lag defaulted to 14 days", (), rules)]
        lag = ans.parameters.get("settlement_lag_days")
        rng = ans.parameters.get("settlement_lag_days_allowed_range")
        ok_rng = (isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, int) and not isinstance(x, bool)
                                                                  for x in rng))
        lo, hi = (rng if ok_rng else [7, 14])
        lo, hi = max(lo, 7), min(hi, 14)
        if ans.effective_status != "verified":
            return DEFAULT_LAG_DAYS, "default", ans.register_version, [
                R.item("RULE_NOT_IN_FORCE", f"Compliance HR-13 is {ans.effective_status}", (), rules)]
        if isinstance(lag, bool) or not isinstance(lag, int) or not lo <= lag <= hi:
            return DEFAULT_LAG_DAYS, "default", ans.register_version, [
                R.item("RULE_NOT_IN_FORCE", f"Compliance HR-13 settlement_lag_days {lag!r} outside [{lo}, {hi}]", (),
                       rules)]
        return lag, "compliance_hr13", ans.register_version, []

    def _platform_reasons(self, platform: str) -> list[dict]:
        rules = self.rules()
        if platform in P.NOT_PAYABLE:
            return [R.item("PLATFORM_NOT_PAYABLE", f"{platform} is not payable (no certifiable API count)", (), rules)]
        if platform == "x":
            if not self.cfg.platform_x_enabled:
                return [R.item("PLATFORM_DISABLED", "X is disabled (VI_PLATFORM_X_ENABLED=0)", (), rules)]
            return []
        if platform not in self.cfg.platforms_enabled:
            return [R.item("PLATFORM_DISABLED", f"{platform} is not in VI_PLATFORMS_ENABLED", (), rules)]
        return []

    def _enabled(self, platform: str) -> bool:
        return not self._platform_reasons(platform)

    def _active_connection(self, clipper_id: str, platform: str) -> Optional[dict]:
        cands = [c for c in self.connections.values() if c["clipper_id"] == clipper_id and c["platform"] == platform
                 and c["status"] == "active"]
        return max(cands, key=lambda c: c["seq"]) if cands else None

    def _latest_connection(self, clipper_id: str, platform: str) -> Optional[dict]:
        cands = [c for c in self.connections.values() if c["clipper_id"] == clipper_id and c["platform"] == platform]
        return max(cands, key=lambda c: c["seq"]) if cands else None

    def _open_holds(self, subjects: list[tuple[str, str]]) -> list[dict]:
        want = set(subjects)
        return sorted((h for h in self.holds.values() if h["status"] == "open"
                       and (h["subject_kind"], h["subject_id"]) in want), key=lambda h: h["hold_id"])

    def _findings_on(self, sub_id: Optional[str], clipper_id: str, kinds: tuple, statuses=("open", "upheld")) -> list[dict]:
        return sorted((f for f in self.findings.values() if f["kind"] in kinds and f["status"] in statuses
                       and ((f["subject_kind"] == "clip" and f["subject_id"] == sub_id)
                            or f.get("clipper_id") == clipper_id)), key=lambda f: f["finding_id"])

    def _age_status(self, subject_id: str, op: Op, with_rules: bool = True) -> tuple[str, list[dict], Optional[str]]:
        """(adult | minor | unknown, reasons, attestation id). ``inconclusive``/none → unknown."""
        rules = self.rules()
        aid = self.latest_age.get(subject_id)
        if aid is None:
            return "unknown", [R.item("AGE_NOT_ASSURED", "no age attestation on file", (), rules)], None
        a = self.ages[aid]
        if a["result"] == "minor":
            return "minor", [R.item("AGE_MINOR", "age attestation: under 18", (aid,), rules)], aid
        if a["result"] != "adult":
            return "unknown", [R.item(a.get("code") or "AGE_NOT_ASSURED", f"age attestation {a['result']}", (aid,),
                                      rules)], aid
        dup = self._findings_on(None, subject_id, ("duplicate_identity", "account_shared"))
        if dup:
            return "unknown", [R.item("DUPLICATE_IDENTITY", "adult attestation invalidated by a duplicate-identity "
                                      "finding", [f["finding_id"] for f in dup], rules)], aid
        return "adult", [], aid

    def _identity_reasons(self, clipper_id: str) -> list[dict]:
        rules = self.rules()
        idc = self.identities.get(clipper_id)
        out = []
        if idc is None:
            out.append(R.item("DUPLICATE_IDENTITY", "no identity check on file for the clipper", (), rules))
        elif idc["status"] == "incomplete":
            out.append(R.item("DEPENDENCY_UNAVAILABLE", f"identity check incomplete ({idc['why']})",
                              (idc["check_id"],), rules))
        return out

    # ================================================================== connections (§B.1)

    def _connection_view(self, c: dict) -> dict:
        return {k: c.get(k) for k in ("connection_id", "clipper_id", "platform", "platform_account_id_hmac", "scopes",
                                      "status", "granted_at", "revoked_at", "last_ok_at", "account_eligibility",
                                      "reasons", "reason_lines", "state_expires_at")}

    def connections_start(self, principal: str, request_id: str, clipper_id: str, platform: str,
                          redirect_uri: str) -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, "connections/start",
                                     {"clipper_id": clipper_id, "platform": platform, "redirect_uri": redirect_uri})
            if ent:
                return ent["response"]
            now = self._now()
            rules = self.rules()
            cid = rid("con", principal, request_id)
            op = Op(self, cid, i01.ACTOR, clipper_id)
            reasons = []
            if self.current is None:
                reasons.append(R.item("RULES_NOT_IN_FORCE", "no V&I rule version is in force", (), rules))
            reasons += self._platform_reasons(platform)
            if clipper_id in self.bans:
                reasons.append(R.item("CONNECTION_REVOKED", "clipper banned (Andre-approved)", (), rules))
            url = None
            verifier = secrets.token_urlsafe(48)
            state = secrets.token_urlsafe(32)
            if not reasons:
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                v = self.ports.vault
                url = op.call("vault", "authorization_url", (cid, platform),
                              lambda: v.authorization_url(platform, state, challenge, redirect_uri, P.SCOPES[platform]),
                              None)
                if not isinstance(url, str) or not url.startswith("https://") or len(url) > 4096:
                    url = None
                    reasons.append(R.item("DEPENDENCY_UNAVAILABLE", "token vault not wired: no authorization URL", (),
                                          rules))
            state_sha = sha_text(state)
            expires = iso(now + OAUTH_STATE_TTL)
            rec = {"connection_id": cid, "clipper_id": clipper_id, "platform": platform,
                   "platform_account_id_hmac": None, "scopes": [], "vault_ref": None,
                   "status": "pending" if url else "refused", "granted_at": None, "revoked_at": None,
                   "last_ok_at": None, "account_eligibility": None, "state_sha256": state_sha if url else None,
                   "state_expires_at": expires if url else None, "reasons": reasons, "reason_lines": R.lines(reasons),
                   "created_at": iso(now), "seq": len(self.connections) + 1}
            etype = "connection_started" if url else "connection_refused"
            op.record(derived_id("con", cid, etype), etype, i01.ACTOR, clipper_id,
                      {"connection_id": cid, "platform": platform, "state_sha256": rec["state_sha256"],
                       "codes": R.codes(reasons), "rules_version": self.rules_version},
                      f"Connection {'started' if url else 'refused'} ({platform})")
            op.add("connection", rec)
            self._commit(op)
            if url:
                self.pending_oauth[state_sha] = {"connection_id": cid, "verifier": verifier,
                                                 "redirect_uri": redirect_uri, "expires": now + OAUTH_STATE_TTL}
                resp = {"started": True, "connection_id": cid, "authorization_url": url, "state_expires_at": expires,
                        "ledger_event_ids": op.events}
            else:
                resp = {"started": False, "connection_id": cid, "reasons": reasons, "reason_lines": R.lines(reasons),
                        "ledger_event_ids": op.events}
            return self._idem_store(key, h, resp)

    def connections_complete(self, principal: str, request_id: str, state: str, code: str) -> dict:
        with self.lock:
            state_sha = sha_text(state)
            key, h, ent = self._idem(principal, request_id, "connections/complete", {"state_sha256": state_sha,
                                                                                    "code_sha256": sha_text(code)})
            if ent:
                return ent["response"]
            pend = self.pending_oauth.pop(state_sha, None)
            if pend is None:
                raise NotFound("unknown or already used OAuth state (states live 10 minutes, in memory only)")
            base = self.connections[pend["connection_id"]]
            now = self._now()
            if now > pend["expires"]:
                raise Conflict("OAuth state expired; start again")
            rules = self.rules()
            cid, platform, clipper_id = base["connection_id"], base["platform"], base["clipper_id"]
            op = Op(self, f"{cid}|complete", i01.ACTOR, clipper_id)
            v = self.ports.vault
            # the code and the verifier never enter a crossing payload, record, response or log (VI-16)
            stored = op.call("vault", "exchange_and_store", (cid, platform),
                             lambda: v.exchange_and_store(platform, code, pend["verifier"], pend["redirect_uri"]),
                             VaultStore(False))
            vref = stored.vault_ref if isinstance(stored, VaultStore) and stored.available else None
            reasons: list[dict] = []
            eligibility = None
            acct_hmac = None
            raw_account = None
            held: list[dict] = []
            finding = None
            try:
                if vref is None:
                    reasons.append(R.item("DEPENDENCY_UNAVAILABLE", "token vault refused to store the grant", (), rules))
                else:
                    if tuple(sorted(stored.granted_scopes)) != tuple(sorted(P.SCOPES[platform])):
                        reasons.append(R.item("NOT_CONNECTED", "granted scopes differ from exactly the required scopes "
                                              "(least access)", (), rules))
                    adapter = self.ports.adapters.get(platform)
                    acct = op.call(f"platform_{platform}", "account", (cid,),
                                   lambda: adapter.account(v, vref), AccountAnswer(False))
                    if not isinstance(acct, AccountAnswer) or not acct.available or not acct.account_id:
                        reasons.append(R.item("ADAPTER_UNAVAILABLE", f"{platform} account lookup unavailable", (), rules))
                    else:
                        if acct.bio:
                            self._injection(op, {"bio": acct.bio})
                        key_b = op.identity_key()
                        if key_b is None:
                            reasons.append(R.item("DEPENDENCY_UNAVAILABLE", "identity HMAC key unavailable (vault)", (),
                                                  rules))
                        else:
                            raw_account = acct.account_id
                            acct_hmac = i07.hmac_hex(key_b, f"account:{platform}", acct.account_id)
                            if platform == "instagram":
                                ok = bool(acct.is_professional) and (acct.followers or 0) >= P.IG_MIN_FOLLOWERS
                                eligibility = {"professional": acct.is_professional, "followers_at_least_100":
                                               (acct.followers or 0) >= P.IG_MIN_FOLLOWERS, "eligible": ok}
                                if not ok:
                                    reasons.append(R.item("PLATFORM_NOT_PAYABLE", "Instagram account is not a professional "
                                                          "account with at least 100 followers", (), rules))
                            kind = f"account:{platform}"
                            other = self.hmac_owner.get((kind, acct_hmac))
                            if (kind, acct_hmac) in self.banned or (other is not None and other != clipper_id):
                                finding, held = self._identity_finding(op, "account_shared", "ACCOUNT_SHARED", clipper_id,
                                                                       other, kind, now)
                                reasons.append(R.item("ACCOUNT_SHARED", "this social account is already held by another "
                                                      "clipper identity (or is banned)",
                                                      [finding["finding_id"]] if finding else (), rules))
                status = "active" if not reasons else "refused"
                if status == "refused" and vref is not None:
                    op.call("vault", "destroy", (cid,), lambda: v.destroy(vref), False)
                rec = dict(base, status=status, scopes=sorted(stored.granted_scopes) if vref else [],
                           vault_ref=vref if status == "active" else None,
                           platform_account_id_hmac=acct_hmac if status == "active" else None,
                           granted_at=iso(now) if status == "active" else None,
                           last_ok_at=iso(now) if status == "active" else None, account_eligibility=eligibility,
                           reasons=reasons, reason_lines=R.lines(reasons), state_sha256=None, state_expires_at=None)
                etype = "connection_active" if status == "active" else "connection_refused"
                op.record(derived_id("con", cid, etype), etype, i01.ACTOR, clipper_id,
                          {"connection_id": cid, "platform": platform, "account_hmac": acct_hmac if status == "active" else None,
                           "codes": R.codes(reasons), "scopes_sha256": sha(sorted(stored.granted_scopes) if vref else [])},
                          f"Connection {status} ({platform})")
                op.add("connection", rec)
                if status == "active":
                    rule = P.RAW_RETENTION_RULE[platform]
                    op.side.append(lambda: self.side.put(f"{cid}:account_id", cid, platform, "account_id", raw_account,
                                                         iso(now), rule, "connection"))
                self._commit(op)
            except Unavailable:
                if vref is not None:
                    try:
                        v.destroy(vref)
                    except Exception:  # noqa: BLE001 - best effort; the grant must not outlive a refused op
                        pass
                raise
            resp = {"connection": self._connection_view(rec), "ledger_event_ids": op.events}
            return self._idem_store(key, h, resp)

    def _identity_finding(self, op: Op, kind: str, code: str, newer: str, older: Optional[str], hmac_kind: str,
                          now: datetime) -> tuple[dict, list[dict]]:
        """Open a duplicate_identity / account_shared finding and hold the NEWER identity (§C.7)."""
        rules = self.rules()
        fid = rid("fnd", kind, newer, older, hmac_kind)
        existing = self.findings.get(fid)
        if existing and existing["status"] == "open":
            return existing, []
        f = {"finding_id": fid, "kind": kind, "code": code, "subject_kind": "clipper", "subject_id": newer,
             "clipper_id": newer, "other_clipper_id": older, "status": "open", "evidence_ids": [],
             "rule_id": R.CATALOG[code], "opened_at": iso(now), "decided_by": None, "decided_at": None,
             "decision_note_sha256": None, "matched_kind": hmac_kind.split(":")[0]}
        op.record(derived_id("fnd", fid, "open"), "finding_opened", i07.ACTOR, newer,
                  {"finding_id": fid, "kind": kind, "rule_id": f["rule_id"], "matched_kind": f["matched_kind"]},
                  f"Finding opened: {kind}")
        op.add("finding", f)
        hold = self._open_hold(op, "clipper", newer, "identity", [R.item(code, f"{kind.replace('_', ' ')} finding open "
                                                                          "(human decision)", [fid], rules)], now, fid)
        return f, [hold] if hold else []

    def _open_hold(self, op: Op, subject_kind: str, subject_id: str, cause: str, reasons: list[dict], now: datetime,
                   finding_id: Optional[str] = None, key: str = "") -> Optional[dict]:
        hid = rid("hld", subject_kind, subject_id, cause, finding_id, key)
        cur = self.holds.get(hid) or next((r for k, r in reversed(op.ops) if k == "hold" and r["hold_id"] == hid), None)
        if cur is not None and cur["status"] in ("open", "released", "upheld"):
            return None
        hold = {"hold_id": hid, "subject_kind": subject_kind, "subject_id": subject_id, "cause": cause,
                "finding_id": finding_id, "reasons": reasons, "reason_lines": R.lines(reasons), "opened_at": iso(now),
                "status": "open", "released_by": None, "released_at": None, "decided_by": None,
                "decision_note_sha256": None}
        op.record(derived_id("hld", hid, "open"), "hold_opened", i06.ACTOR if cause == "anomaly" else EVIDENCE,
                  subject_id, {"hold_id": hid, "cause": cause, "codes": R.codes(reasons), "finding_id": finding_id},
                  f"Hold opened on {subject_kind}: {cause}")
        op.add("hold", hold)
        return hold

    def revoke_connection(self, principal: str, request_id: str, connection_id: str, reason: str = "revoked") -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, "connections/revoke", {"id": connection_id})
            if ent:
                return ent["response"]
            c = self.connections.get(connection_id)
            if c is None:
                raise NotFound("no such connection")
            op = Op(self, f"{connection_id}|revoke|{request_id}", i01.ACTOR, c["clipper_id"])
            self._revoke(op, c, reason, self._now())
            self._commit(op)
            return self._idem_store(key, h, {"connection": self._connection_view(self.connections[connection_id]),
                                             "ledger_event_ids": op.events})

    def _revoke(self, op: Op, c: dict, why: str, now: datetime) -> None:
        if c["status"] != "active":
            if c["status"] == "pending":
                op.add("connection", dict(c, status="revoked", revoked_at=iso(now)))
            return
        v = self.ports.vault
        vref = c.get("vault_ref")
        op.call("vault", "destroy", (c["connection_id"],), lambda: v.destroy(vref), False)
        op.record(derived_id("con", c["connection_id"], "revoked"), "connection_revoked", i01.ACTOR, c["clipper_id"],
                  {"connection_id": c["connection_id"], "why_sha256": sha_text(why)}, "Connection revoked")
        op.add("connection", dict(c, status="revoked", revoked_at=iso(now), vault_ref=None))
        # §B.5 VI-15b: every non-statistics datum fetched through this connection goes now (well within 24 h)
        subs = [s for s in self.submissions.values() if s["clipper_id"] == c["clipper_id"]
                and s["platform"] == c["platform"]]
        keys = self.side.keys_for([c["connection_id"]] + [s["submission_id"] for s in subs])
        if keys:
            self._purge(op, keys, "connection_revoked", now)

    def _purge(self, op: Op, keys: list[str], cause: str, now: datetime) -> None:
        with self.side.lock:
            doomed = [(k, self.side.entries[k]) for k in keys if k in self.side.entries]
        counts: dict[str, int] = {}
        for _, e in doomed:
            counts[e["rule"]] = counts.get(e["rule"], 0) + 1
        if not doomed:
            return
        pid = derived_id("ret", op.op_id, cause, sorted(k for k, _ in doomed))
        op.record(pid, "retention_purged", EVIDENCE, "retention", {"cause": cause, "counts": counts,
                                                                   "keys_sha256": sha(sorted(k for k, _ in doomed))},
                  f"Platform data purged ({cause}): {sum(counts.values())} value(s)")
        op.add("retention", {"cause": cause, "counts": counts, "at": iso(now),
                             "fields": sorted({f"{e['platform']}:{e['field']}" for _, e in doomed})})
        op.side.append(lambda: self.side.delete([k for k, _ in doomed]))

    def list_connections(self, clipper_id: str) -> list[dict]:
        with self.lock:
            return [self._connection_view(c) for c in sorted(self.connections.values(), key=lambda c: c["seq"])
                    if c["clipper_id"] == clipper_id]

    # ================================================================== submissions (§D.2, §C.8)

    def register_submission(self, principal: str, request_id: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, "submissions", body)
            if ent:
                return ent["response"]
            sid = body["submission_id"]
            now = self._now()
            rules = self.rules()
            facts = {k: body.get(k) for k in ("campaign_id", "rulebook_version", "clipper_id", "platform", "posted_at",
                                              "min_days_live", "collab_permitted", "target_regions")}
            facts["post_ref_sha256"] = sha_text(body["post_ref"])
            if sid in self.submissions:
                old = self.submissions[sid]
                if old["facts_sha256"] != sha(facts):
                    raise Conflict("submission already registered with different facts")
                resp = {"submission_id": sid, "certification_id": self.cert_by_sub.get(sid), "already_registered": True}
                return self._idem_store(key, h, resp)
            op = Op(self, f"sub|{sid}", i08.ACTOR, sid)
            self._injection(op, {"post_ref": body["post_ref"], "media_ref": body.get("media_ref"),
                                 "seed_media_refs": body.get("seed_media_refs")})
            media_sha = sig = None
            m, hs = self.ports.media, self.ports.hasher
            if body.get("media_ref"):
                ref = body["media_ref"]
                media_sha = op.call("media_intake", "sha256", (sha_text(ref),), lambda: m.sha256(ref), None)
                sig = op.call("hasher", "video_signature", (sha_text(ref),), lambda: hs.video_signature(ref), None)
                media_sha = media_sha if isinstance(media_sha, str) and len(media_sha) == 64 else None
                sig = sig if isinstance(sig, dict) else None
            seeds = []
            for ref in body.get("seed_media_refs") or []:
                s_sha = op.call("media_intake", "sha256", (sha_text(ref),), lambda r=ref: m.sha256(r), None)
                s_sig = op.call("hasher", "video_signature", (sha_text(ref),), lambda r=ref: hs.video_signature(r), None)
                seeds.append({"ref": sha_text(ref)[:32], "media_sha256": s_sha if isinstance(s_sha, str) else None,
                              "signature": s_sig if isinstance(s_sig, dict) else None})
            others = [{"submission_id": s["submission_id"], "clipper_id": s["clipper_id"],
                       "media_sha256": s.get("media_sha256"), "signature": s.get("media_signature"),
                       "received_seq": s["received_seq"]}
                      for s in self.submissions.values() if s["clipper_id"] != body["clipper_id"]]
            match_fn = lambda a, b: hs.match(a, b)  # noqa: E731
            stolen = i08.check(media_sha, sig, others, seeds, lambda a, b: op.call(
                "hasher", "match", (sha(a), sha(b)), lambda: match_fn(a, b), None))
            if not body.get("media_ref"):
                stolen = {"status": "incomplete", "matched": [], "presumed_original": None,
                          "why": "no media_ref (the submitted file is not available to V&I)"}
            sub = {"submission_id": sid, **{k: body.get(k) for k in ("campaign_id", "rulebook_version", "clipper_id",
                                                                    "platform", "posted_at", "min_days_live",
                                                                    "collab_permitted", "target_regions")},
                   "post_ref_sha256": facts["post_ref_sha256"], "facts_sha256": sha(facts),
                   "media_sha256": media_sha, "media_signature": sig, "stolen_check": stolen["status"],
                   "registered_at": iso(now), "received_seq": len(self.submissions) + 1, "create_time": None,
                   "video_id_sha256": None}
            op.record(derived_id("sub", sid, sub["facts_sha256"]), "submission_registered", i08.ACTOR, sid,
                      {"submission_id": sid, "facts_sha256": sub["facts_sha256"], "platform": body["platform"],
                       "stolen_check": stolen["status"], "rules_version": self.rules_version},
                      f"Submission registered ({body['platform']}); stolen-content check {stolen['status']}")
            op.add("submission", sub)
            holds = []
            if stolen["status"] == "match":
                fid = rid("fnd", "stolen", sid)
                f = {"finding_id": fid, "kind": "stolen_content", "code": "STOLEN_MATCH", "subject_kind": "clip",
                     "subject_id": sid, "clipper_id": body["clipper_id"], "status": "open", "evidence_ids": [],
                     "rule_id": "VI-14", "opened_at": iso(now), "decided_by": None, "decided_at": None,
                     "decision_note_sha256": None, "presumed_original": stolen["presumed_original"],
                     "matched": [{k: x[k] for k in ("kind", "id")} for x in stolen["matched"]][:20]}
                op.record(derived_id("fnd", fid, "open"), "finding_opened", i08.ACTOR, sid,
                          {"finding_id": fid, "kind": "stolen_content", "rule_id": "VI-14",
                           "matches": len(stolen["matched"])}, "Finding opened: stolen_content")
                op.add("finding", f)
                hh = self._open_hold(op, "clip", sid, "stolen", [R.item("STOLEN_MATCH", "matches "
                                     f"{len(stolen['matched'])} earlier clip(s); presumed original "
                                     f"{stolen['presumed_original']}", [fid], rules)], now, fid)
                holds += [hh] if hh else []
            elif stolen["status"] == "incomplete":
                hh = self._open_hold(op, "clip", sid, "stolen_incomplete",
                                     [R.item("STOLEN_CHECK_INCOMPLETE", stolen.get("why", "incomplete"), (), rules)], now)
                holds += [hh] if hh else []
            cert = self._new_cert(sub, now)
            first = (R.item("RULES_NOT_IN_FORCE", "no V&I rule version is in force", (), rules) if self.current is None
                     else R.item("NOT_YET_SETTLED", "registered; not yet evaluated", (), rules))
            cert.update(reasons=[first], reason_lines=R.lines([first]))
            op.record(derived_id("cer", cert["certification_id"], "pending", 0), "certification_issued", i02.ACTOR, sid,
                      self._cert_payload(cert), "Certification record opened: pending")
            op.add("certification", cert)
            if body["platform"] in P.CERTIFIABLE:
                rule = P.RAW_RETENTION_RULE[body["platform"]]
                ref = body["post_ref"]
                op.side.append(lambda: self.side.put(f"{sid}:post_ref", sid, body["platform"], "post_ref", ref, iso(now),
                                                     rule, "submission"))
            self._commit(op)
            return self._idem_store(key, h, {"submission_id": sid, "certification_id": cert["certification_id"],
                                             "stolen_check": stolen["status"],
                                             "holds": [x["hold_id"] for x in holds], "ledger_event_ids": op.events})

    def _new_cert(self, sub: dict, now: datetime) -> dict:
        cid = rid("cert", sub["submission_id"])
        return {"certification_id": cid, "submission_id": sub["submission_id"], "campaign_id": sub["campaign_id"],
                "rulebook_version": sub["rulebook_version"], "clipper_id": sub["clipper_id"],
                "platform": sub["platform"], "connection_id": None, "window": None, "status": "pending",
                "certified_views": None, "original_views": None,
                "metric_name": P.METRIC_NAME.get(sub["platform"], "views"), "basis": "platform_api_owner_oauth",
                "evidence_ids": [], "reasons": [], "reason_lines": [], "revisions": [], "clawback_ids": [],
                "rules_version": self.rules_version, "compliance_register_version": None, "settlement_source": None,
                "ledger_event_id": None, "evaluated_at": iso(now), "evaluation": 0, "certified_at": None}

    def _cert_payload(self, c: dict) -> dict:
        return {"certification_id": c["certification_id"], "status": c["status"], "certified_views": c["certified_views"],
                "codes": R.codes(c["reasons"]), "rule_ids": sorted({r["rule_id"] for r in c["reasons"]}),
                "rules_version": c["rules_version"], "compliance_register_version": c["compliance_register_version"],
                "settlement_source": c["settlement_source"], "evaluation": c["evaluation"]}

    # ================================================================== fetch (i01)

    def _fetch(self, op: Op, sub: dict, purpose: str, metrics: tuple, now: datetime) -> dict:
        """One adapter call for a submission (record-first). Returns the fetch record (also staged in ``op``)."""
        rules = self.rules()
        sid, platform = sub["submission_id"], sub["platform"]
        fid = rid("fch", sid, purpose, iso(now), len(op.ops))
        base = {"fetch_id": fid, "submission_id": sid, "purpose": purpose, "platform": platform, "fetched_at": iso(now),
                "available": False, "live_state": "unknown", "cause": None, "connection_id": None, "video": None,
                "snapshot_ids": [], "source_response_sha256": None, "quota": None, "reasons": []}

        def fail(cause: str, code: str, msg: str) -> dict:
            rec = dict(base, cause=cause, reasons=[R.item(code, msg, (), rules)])
            op.record(derived_id("fch", fid), "platform_fetch_failed", i01.ACTOR, sid,
                      {"fetch_id": fid, "purpose": purpose, "cause": cause}, f"{platform} fetch not made: {cause}")
            return op.add("fetch", rec)

        if not self._enabled(platform) or platform not in P.CERTIFIABLE:
            return fail("platform_disabled", "PLATFORM_DISABLED", f"{platform} not enabled")
        conn = self._active_connection(sub["clipper_id"], platform)
        if conn is None:
            return fail("not_connected", "NOT_CONNECTED", "no active connection for this platform")
        base["connection_id"] = conn["connection_id"]
        why = self.quota.allow(platform, purpose, now)
        if why:
            return fail("quota_exhausted", "QUOTA_EXHAUSTED", why)
        video_ref = self.side.get(f"{sid}:post_ref")
        account_id = self.side.get(f"{conn['connection_id']}:account_id")
        if video_ref is None or account_id is None:
            return fail("platform_data_purged", "ADAPTER_UNAVAILABLE", "raw post reference or account id purged")
        # the side store is mutable (purgeable) and not anchored: its raw values must match the log's hashes
        key_chk = op.identity_key()
        if sha_text(video_ref) != sub["post_ref_sha256"] or key_chk is None or \
                i07.hmac_hex(key_chk, f"account:{platform}", account_id) != conn["platform_account_id_hmac"]:
            return fail("platform_data_mismatch", "ADAPTER_UNAVAILABLE", "raw platform data does not match the "
                        "hashes in the evidence log")
        adapter = self.ports.adapters.get(platform)
        hint = sub.get("create_time") or int(parse_iso(sub["posted_at"]).timestamp())
        v = self.ports.vault
        ans = op.call(f"platform_{platform}", "fetch", (fid, purpose, list(metrics)),
                      lambda: adapter.fetch(v, conn["vault_ref"], account_id, video_ref, metrics, hint),
                      AdapterAnswer(False))
        if not isinstance(ans, AdapterAnswer):
            ans = AdapterAnswer(False)
        units = ans.cost_units if platform in ("youtube", "x") else 1
        quota = {"platform": platform, "units": units, "at": iso(now), "purpose": purpose}
        self.quota.spend(platform, units, now)
        op.record(derived_id("qta", fid), "quota_spent", i01.ACTOR, sid,
                  {"platform": platform, "units": units, "day": iso(now)[:10], "purpose": purpose},
                  f"{platform} quota spent: {units}")
        if ans.rate_limited:
            wait = self.quota.rate_limited(platform, now)
            op.record(derived_id("rl", fid), "platform_rate_limited", i01.ACTOR, sid,
                      {"platform": platform, "backoff_s": wait, "http_status": ans.http_status},
                      f"{platform} rate limited (429): backing off {wait} s")
        elif ans.available:
            self.quota.ok(platform)
        rec = dict(base, quota=quota, source_response_sha256=ans.source_response_sha256)
        if not ans.available:
            rec.update(cause="rate_limited" if ans.rate_limited else "adapter_unavailable",
                       reasons=[R.item("QUOTA_EXHAUSTED" if ans.rate_limited else "ADAPTER_UNAVAILABLE",
                                       f"{platform} {'rate limited' if ans.rate_limited else 'adapter unavailable'}",
                                       (), rules)])
            op.record(derived_id("fch", fid), "platform_fetch_failed", i01.ACTOR, sid,
                      {"fetch_id": fid, "purpose": purpose, "cause": rec["cause"]}, f"{platform} fetch unavailable")
            return op.add("fetch", rec)
        rec.update(available=True, live_state=ans.live_state if ans.live_state in ("live", "gone", "private") else "unknown")
        side_puts = []
        if ans.video is not None:
            vf = ans.video
            key_b = op.identity_key()
            if key_b is None:
                rec.update(available=False, cause="identity_key_unavailable",
                           reasons=[R.item("DEPENDENCY_UNAVAILABLE", "identity HMAC key unavailable (vault)", (), rules)])
                op.record(derived_id("fch", fid), "platform_fetch_failed", i01.ACTOR, sid,
                          {"fetch_id": fid, "purpose": purpose, "cause": rec["cause"]}, f"{platform} fetch unusable")
                return op.add("fetch", rec)
            if vf.caption:
                self._injection(op, {"caption": vf.caption})
            cover_pdq = None
            if platform in P.PERCEPTUAL_CAPABLE and self.cfg.tt_cover_pdq and vf.cover_image_bytes:
                hs = self.ports.hasher
                data = vf.cover_image_bytes
                cover_pdq = op.call("hasher", "image_pdq", (fid,), lambda: hs.image_pdq(data), None)
                cover_pdq = cover_pdq if isinstance(cover_pdq, str) and len(cover_pdq) == 64 else None
            rec["video"] = {"video_id_sha256": sha_text(vf.video_id),
                            "author_id_hmac": i07.hmac_hex(key_b, f"account:{platform}", vf.author_id),
                            "create_time": int(vf.create_time), "duration_ms": vf.duration_ms,
                            "caption_sha256": caption_sha256(vf.caption), "cover_pdq": cover_pdq,
                            "is_collab": vf.is_collab, "rights_restricted": vf.rights_restricted}
            rule = P.RAW_RETENTION_RULE[platform]
            side_puts.append((f"{sid}:video_id", "video_id", vf.video_id, rule))
            if vf.share_url:
                side_puts.append((f"{sid}:share_url", "share_url", vf.share_url, rule))
        vid_sha = rec["video"]["video_id_sha256"] if rec["video"] else sub.get("video_id_sha256")
        until = i01.retention_until(now, self.cfg.evidence_retention_days)
        stats_rule = P.STATS_RETENTION_RULE[platform]
        items = sorted((m, val, None) for m, val in ans.values.items() if m in P.METRICS and m != "country_views")
        items += sorted(("country_views", val, {"country": c}) for c, val in ans.country_values.items())
        for metric, val, dim in items:
            if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                continue
            snp = rid("snp", fid, metric, dim)
            s = i01.snapshot(snp, conn["connection_id"], sid, platform, vid_sha, metric, val, dim, iso(now),
                             ans.source_endpoint if ans.source_endpoint in P.SOURCE_ENDPOINTS else "fake",
                             ans.source_response_sha256, ADAPTER_VERSION, until, stats_rule)
            s["fetch_id"] = fid
            eid = derived_id("snp", snp)
            s["ledger_event_id"] = eid
            op.record(eid, "metric_snapshot_recorded", i01.ACTOR, sid, self._snapshot_payload(s),
                      f"{platform} {metric} snapshot: {val}")
            op.add("snapshot", s)
            rec["snapshot_ids"].append(snp)
        op.record(derived_id("fch", fid), "platform_fetched", i01.ACTOR, sid,
                  {"fetch_id": fid, "purpose": purpose, "live_state": rec["live_state"],
                   "source_response_sha256": ans.source_response_sha256, "snapshots": len(rec["snapshot_ids"]),
                   "video_sha256": sha(rec["video"])}, f"{platform} fetched for {purpose}: {rec['live_state']}")
        op.add("fetch", rec)
        at = iso(now)
        for k, fld, val, rule in side_puts:
            op.side.append(lambda k=k, fld=fld, val=val, rule=rule: self.side.put(k, sid, platform, fld, val, at, rule,
                                                                                 "submission"))
        op.side.append(lambda: self.side.refresh(f"{sid}:post_ref", at))
        op.side.append(lambda: self.side.refresh(f"{conn['connection_id']}:account_id", at))
        if rec["video"] and sub.get("create_time") is None:
            op.add("submission_update", {"submission_id": sid, "fields": {
                "create_time": rec["video"]["create_time"], "video_id_sha256": rec["video"]["video_id_sha256"]}})
        return rec

    @staticmethod
    def _snapshot_payload(s: dict) -> dict:
        return {k: s[k] for k in ("snapshot_id", "connection_id", "submission_id", "platform", "video_id_sha256",
                                  "metric", "dimension", "value", "fetched_at", "source_endpoint",
                                  "source_response_sha256", "adapter_version", "retention_until", "retention_rule")}

    def _staged(self, op: Op, kind: str) -> list[dict]:
        return [r for k, r in op.ops if k == kind]

    def _sub_now(self, op: Op, sid: str) -> dict:
        sub = dict(self.submissions[sid])
        for r in self._staged(op, "submission_update"):
            if r["submission_id"] == sid:
                sub.update(r["fields"])
        return sub

    # ================================================================== approval fingerprint (i03)

    def approve_submission(self, principal: str, request_id: str, sid: str, review_ref: Optional[str]) -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, "approval", {"sid": sid, "review_ref": review_ref})
            if ent:
                return ent["response"]
            sub = self.submissions.get(sid)
            if sub is None:
                raise NotFound("submission not registered")
            fp = self.fingerprints.get(sid)
            if fp is not None and fp["available"]:
                return self._idem_store(key, h, {"submission_id": sid, "fingerprint": fp, "already_taken": True})
            now = self._now()
            op = Op(self, f"fp|{sid}|{request_id}", i03.ACTOR, sid)
            f = self._fetch(op, sub, "approval", ("views", "likes"), now)
            ok = f["available"] and f["live_state"] == "live" and f["video"] is not None
            fpr = {"submission_id": sid, "taken_at": iso(now), "available": ok, "fetch_id": f["fetch_id"],
                   "review_ref": review_ref, "fingerprint": i03.fingerprint(**{k: f["video"][k] for k in (
                       "video_id_sha256", "author_id_hmac", "create_time", "duration_ms", "caption_sha256",
                       "cover_pdq")}) if ok else None,
                   "reasons": [] if ok else (f["reasons"] or [R.item("FINGERPRINT_UNAVAILABLE", "approval fetch did not "
                                                                    "return the live post", (), self.rules())])}
            fpr["reason_lines"] = R.lines(fpr["reasons"])
            op.record(derived_id("fp", sid, request_id), "approval_fingerprint_taken", i03.ACTOR, sid,
                      {"submission_id": sid, "available": ok, "fingerprint_sha256": sha(fpr["fingerprint"]),
                       "fetch_id": f["fetch_id"]}, f"Approval fingerprint {'taken' if ok else 'unavailable'}")
            op.add("fingerprint", fpr)
            self._commit(op)
            return self._idem_store(key, h, {"submission_id": sid, "fingerprint": fpr, "ledger_event_ids": op.events})

    # ================================================================== certification (i02)

    def _window(self, sub: dict, lag: int) -> Optional[dict]:
        ct = sub.get("create_time")
        if ct is None:
            return None
        create = datetime.fromtimestamp(ct, timezone.utc)
        min_live = sub.get("min_days_live") or 0
        settle = create + timedelta(days=max(lag, min_live))
        min_end = create + timedelta(days=min_live) if sub.get("min_days_live") else None
        watch_end = max(create + timedelta(days=self.cfg.revision_watch_days), settle)
        return {"create_time": iso(create), "lag_days": lag, "min_live_days": sub.get("min_days_live"),
                "settle_at": settle, "settle_at_iso": iso(settle), "min_live_end": min_end,
                "revision_watch_end": watch_end}

    @staticmethod
    def _window_view(w: Optional[dict]) -> Optional[dict]:
        if w is None:
            return None
        return {"create_time": w["create_time"], "lag_days": w["lag_days"], "min_live_days": w["min_live_days"],
                "settle_at": w["settle_at_iso"], "min_live_end": iso(w["min_live_end"]) if w["min_live_end"] else None,
                "revision_watch_end": iso(w["revision_watch_end"])}

    def _fetches_of(self, op: Optional[Op], sid: str) -> list[dict]:
        out = [self.fetches[f] for f in self.sub_fetches.get(sid, [])]
        if op is not None:
            out += [r for r in self._staged(op, "fetch") if r["submission_id"] == sid]
        return out

    def _snapshots_of(self, op: Optional[Op], sid: str) -> list[dict]:
        out = [self.snapshots[s] for s in self.sub_snapshots.get(sid, [])]
        if op is not None:
            out += [r for r in self._staged(op, "snapshot") if r["submission_id"] == sid]
        return out

    def _settlement(self, op: Optional[Op], sid: str, w: dict) -> tuple[Optional[dict], Optional[dict]]:
        """(the views snapshot fetched in the settlement window, its fetch record) — the first one."""
        lo, hi = w["settle_at"], w["settle_at"] + timedelta(hours=self.cfg.settle_fetch_grace_h)
        metric = P.PAYABLE_METRIC.get(self.submissions[sid]["platform"], "views")
        cands = [f for f in self._fetches_of(op, sid) if f["available"] and lo <= parse_iso(f["fetched_at"]) <= hi]
        cands.sort(key=lambda f: f["fetched_at"])
        snaps = {s["snapshot_id"]: s for s in self._snapshots_of(op, sid)}
        for f in cands:
            for s_id in f["snapshot_ids"]:
                s = snaps.get(s_id)
                if s and s["metric"] == metric and s["dimension"] is None:
                    return s, f
            return None, f
        return None, None

    def _liveness_eval(self, op: Optional[Op], sub: dict, w: dict, today: date) -> tuple[str, list[dict]]:
        rules = self.rules()
        if w.get("min_live_end") is None:
            return "unknown", [R.item("MIN_LIVE_UNKNOWN", "the rulebook's minimum live days were not registered", (),
                                      rules)]
        days = i04.required_days(int(parse_iso(w["create_time"]).timestamp()), w["min_live_end"])
        states = {}
        recs = dict(((k[1], v) for k, v in self.liveness.items() if k[0] == sub["submission_id"]))
        if op is not None:
            for r in self._staged(op, "liveness"):
                if r["submission_id"] == sub["submission_id"]:
                    recs[r["day"]] = r
        for d_iso, r in recs.items():
            states[date.fromisoformat(d_iso)] = r
        return i04.evaluate(days, states, today, max_gap_days=self.cfg.liveness_max_gap_days,
                            oembed_max_consecutive=self.cfg.oembed_max_consecutive_days, rules=rules,
                            evidence_for=lambda d: (states[d]["check_id"],) if d in states else ())

    def _evaluate(self, op: Op, sub: dict, now: datetime, allow_fetch: bool, entries_held: Optional[dict]) -> dict:
        """Gather every A.1 condition (no short-circuit) and decide. Returns the new certification record."""
        rules = self.rules()
        sid = sub["submission_id"]
        cert = dict(self.certs[self.cert_by_sub[sid]])
        if cert["status"] in ("certified", "revised", "voided"):
            return cert
        gathered: list[dict] = []
        lag, source, reg_version, lag_reasons = self._lag(op)
        w = self._window(sub, lag)
        fallback_deadline = parse_iso(sub["posted_at"]) + timedelta(days=max(lag, sub.get("min_days_live") or 0),
                                                                    hours=self.cfg.settle_fetch_grace_h)
        rules_in_force = self.current is not None
        if not rules_in_force:
            status, reasons, views = i02.decide(now, False, w, fallback_deadline, None, False, [], rules)
            return self._cert_update(cert, status, reasons, views, w, source, reg_version, now)
        gathered += lag_reasons
        gathered += self._rule_status_reasons(CERTIFY_RULES, op)
        gathered += self._platform_reasons(sub["platform"])
        conn = self._active_connection(sub["clipper_id"], sub["platform"])
        if conn is None:
            last = self._latest_connection(sub["clipper_id"], sub["platform"])
            if last is not None and last["status"] in ("revoked", "expired"):
                gathered.append(R.item("CONNECTION_REVOKED", f"connection {last['status']}", (last["connection_id"],),
                                       rules))
            else:
                gathered.append(R.item("NOT_CONNECTED", "the posting account is not connected through OAuth", (), rules))
                if last is not None:
                    gathered += [r for r in last.get("reasons") or [] if r["code"] == "PLATFORM_NOT_PAYABLE"]
        elif conn.get("account_eligibility") and not conn["account_eligibility"].get("eligible", True):
            gathered.append(R.item("PLATFORM_NOT_PAYABLE", "account not eligible", (conn["connection_id"],), rules))
        # platform create time vs clipper posted_at (§C.2 step 3)
        if sub.get("create_time") is not None:
            diff = abs(sub["create_time"] - parse_iso(sub["posted_at"]).timestamp())
            if diff > self.cfg.posted_at_tolerance_min * 60:
                gathered.append(R.item("POSTED_AT_MISMATCH", f"platform create time differs from posted_at by "
                                       f"{int(diff // 60)} min", (), rules))
        if w is not None:
            grace_end = w["settle_at"] + timedelta(hours=self.cfg.settle_fetch_grace_h)
            snap, sfetch = self._settlement(op, sid, w)
            if snap is None and sfetch is None and allow_fetch and w["settle_at"] <= now <= grace_end:
                sfetch = self._fetch(op, sub, "settlement", tuple(P.AVAILABLE_METRICS.get(sub["platform"], ("views",))),
                                     now)
                snap, sfetch = self._settlement(op, sid, w)
            if sfetch is not None and snap is None and sfetch["available"]:
                gathered.append(R.item("METRIC_UNAVAILABLE", "settlement fetch returned no views value",
                                       (sfetch["fetch_id"],), rules))
            if snap is not None and entries_held is not None and \
                    entries_held.get(snap["ledger_event_id"]) != payload_sha256(self._snapshot_payload(snap)) and \
                    snap["ledger_event_id"] not in op.events:
                gathered.append(R.item("SETTLEMENT_SNAPSHOT_MISSING", "settlement snapshot evidence does not match the "
                                       "ledger", (snap["snapshot_id"],), rules))
                snap = None
                sfetch = None
            now_fp = sfetch["video"] if sfetch and sfetch["available"] else None
            if sfetch is not None and sfetch["available"] and sfetch["live_state"] != "live":
                gathered.append(R.item("DELETED_BEFORE_MIN_LIVE" if w.get("min_live_end") and
                                       parse_iso(sfetch["fetched_at"]) <= w["min_live_end"] else "METRIC_UNAVAILABLE",
                                       f"post {sfetch['live_state']} at the settlement fetch", (sfetch["fetch_id"],),
                                       rules))
            if now_fp is not None and conn is not None and now_fp["author_id_hmac"] != conn["platform_account_id_hmac"]:
                gathered.append(R.item("AUTHOR_MISMATCH", "video author is not the connected account",
                                       (sfetch["fetch_id"],), rules))
            if sub["platform"] == "instagram" and not sub.get("collab_permitted"):
                if now_fp is None or now_fp.get("is_collab") is not False:
                    gathered.append(R.item("COLLAB_POST", "Collab co-post (or collab status unknown) and the campaign "
                                           "does not permit collabs", (), rules))
            if now >= w["settle_at"]:
                fp = self.fingerprints.get(sid)
                dist = None
                a_fp = fp["fingerprint"] if fp and fp["available"] else None
                if a_fp and now_fp and a_fp.get("cover_pdq") and now_fp.get("cover_pdq"):
                    hs = self.ports.hasher
                    dist = op.call("hasher", "distance", (a_fp["cover_pdq"], now_fp["cover_pdq"]),
                                   lambda: hs.distance(a_fp["cover_pdq"], now_fp["cover_pdq"]), None)
                    dist = dist if isinstance(dist, int) and not isinstance(dist, bool) else None
                if sfetch is not None or fp is not None:
                    _, fr = i03.compare(a_fp, now_fp, sub["platform"],
                                        require_perceptual=self.cfg.require_perceptual_match,
                                        cover_pdq_enabled=self.cfg.tt_cover_pdq, pdq_distance=dist,
                                        max_hamming=self.cfg.pdq_max_hamming, rules=rules,
                                        evidence=tuple(x for x in (sfetch and sfetch["fetch_id"],) if x))
                    gathered += fr
                else:
                    gathered.append(R.item("FINGERPRINT_UNAVAILABLE", "no approval fingerprint and no settlement fetch",
                                           (), rules))
            lv, lr = self._liveness_eval(op, sub, w, now.date())
            gathered += lr
        else:
            snap, grace_end = None, None
            lv = "unknown"
            if sub.get("min_days_live") is None:
                gathered.append(R.item("MIN_LIVE_UNKNOWN", "the rulebook's minimum live days were not registered", (),
                                       rules))
        # holds, findings, age, identity
        for hh in self._open_holds([("clip", sid), ("clipper", sub["clipper_id"])]) + \
                [x for x in self._staged(op, "hold") if x["status"] == "open"
                 and (x["subject_kind"], x["subject_id"]) in (("clip", sid), ("clipper", sub["clipper_id"]))]:
            gathered += hh["reasons"]
        for f in self._findings_on(sid, sub["clipper_id"], ("bought_engagement", "platform_stripped")):
            gathered.append(R.item("BOUGHT_ENGAGEMENT" if f["kind"] == "bought_engagement" else "PLATFORM_STRIPPED",
                                   f"{f['kind'].replace('_', ' ')} finding {f['status']}", (f["finding_id"],), rules))
        for f in self._findings_on(sid, sub["clipper_id"], ("stolen_content",), ("upheld",)):
            gathered.append(R.item("STOLEN_MATCH", "stolen-content finding upheld", (f["finding_id"],), rules))
        age, age_r, _ = self._age_status(sub["clipper_id"], op)
        gathered += age_r
        gathered += self._identity_reasons(sub["clipper_id"])
        grace_open = w is not None and now <= w["settle_at"] + timedelta(hours=self.cfg.settle_fetch_grace_h)
        status, reasons, views = i02.decide(now, True, w, fallback_deadline, snap, grace_open, gathered, rules)
        new = self._cert_update(cert, status, reasons, views, w, source, reg_version, now)
        if conn is not None:
            new["connection_id"] = conn["connection_id"]
        new["liveness"] = lv
        if snap is not None:
            new["evidence_ids"] = sorted({snap["snapshot_id"], snap["fetch_id"]} | set(new["evidence_ids"]))
        return new

    def _cert_update(self, cert: dict, status: str, reasons: list[dict], views: Optional[int], w: Optional[dict],
                     source: str, reg_version: Optional[int], now: datetime) -> dict:
        new = dict(cert, status=status, reasons=reasons, reason_lines=R.lines(reasons), certified_views=views,
                   window=self._window_view(w), rules_version=self.rules_version, settlement_source=source,
                   compliance_register_version=reg_version, evaluated_at=iso(now))
        if status == "certified":
            new.update(original_views=views, certified_at=iso(now), window_fixed=True)
        return new

    def _cert_changed(self, old: dict, new: dict) -> bool:
        keys = ("status", "certified_views", "window", "settlement_source", "rules_version", "connection_id")
        return any(old.get(k) != new.get(k) for k in keys) or \
            [(r["code"], r["message"]) for r in old["reasons"]] != [(r["code"], r["message"]) for r in new["reasons"]]

    def _issue_cert(self, op: Op, new: dict, now: datetime) -> dict:
        new = dict(new, evaluation=new["evaluation"] + 1)
        eid = derived_id("cer", new["certification_id"], new["evaluation"], new["status"], sha(new["reasons"]))
        new["ledger_event_id"] = eid
        op.record(eid, "certification_issued", i02.ACTOR, new["submission_id"], self._cert_payload(new),
                  f"Certification {new['status']}" + (f": {new['certified_views']} {new['metric_name']}"
                                                      if new["certified_views"] is not None else
                                                      f" ({len(new['reasons'])} reasons)"))
        op.add("certification", new)
        self._integrity_findings(op, new, now)
        return new

    def _integrity_findings(self, op: Op, cert: dict, now: datetime) -> None:
        """Objective integrity failures become auto-upheld findings and strikes at once (§C.9, choice 16)."""
        for code in ("DELETED_BEFORE_MIN_LIVE", "CAPTION_CHANGED", "HASH_MISMATCH"):
            hits = [r for r in cert["reasons"] if r["code"] == code]
            if not hits:
                continue
            fid = rid("fnd", "integrity", cert["submission_id"], code)
            if fid in self.findings or any(r["finding_id"] == fid for r in self._staged(op, "finding")):
                continue
            f = {"finding_id": fid, "kind": "integrity_fail", "code": code, "subject_kind": "clip",
                 "subject_id": cert["submission_id"], "clipper_id": cert["clipper_id"], "status": "upheld",
                 "evidence_ids": sorted({e for r in hits for e in r["evidence_ids"]})[:20], "rule_id": R.CATALOG[code],
                 "opened_at": iso(now), "decided_by": "auto (spec C.9: objective platform evidence)",
                 "decided_at": iso(now), "decision_note_sha256": None}
            op.record(derived_id("fnd", fid, "open"), "finding_opened", i09.ACTOR, cert["submission_id"],
                      {"finding_id": fid, "kind": "integrity_fail", "code": code, "rule_id": f["rule_id"],
                       "auto_upheld": True}, f"Finding opened and auto-upheld: {code}")
            op.add("finding", f)
            self._strike(op, f, now)

    def _strike(self, op: Op, finding: dict, now: datetime) -> Optional[dict]:
        cls = i09.strike_class(finding)
        if cls is None:
            return None
        sid = rid("stk", finding["finding_id"])
        if sid in self.strikes:
            return None
        exp = i09.expires_at(cls, now)
        s = {"strike_id": sid, "clipper_id": finding["clipper_id"], "class": cls, "finding_ids": [finding["finding_id"]],
             "rule_id": finding["rule_id"], "issued_at": iso(now), "expires_at": iso(exp) if exp else None,
             "status": "active", "ban_recommended": cls == "S3", "escalated_into": None, "escalates": []}
        op.record(derived_id("stk", sid), "strike_issued", i09.ACTOR, finding["clipper_id"],
                  {"strike_id": sid, "class": cls, "rule_id": s["rule_id"], "finding_ids": s["finding_ids"],
                   "ban_recommended": s["ban_recommended"]}, f"Strike {cls} issued" +
                  ("; ban recommended (Andre decides in Clipper Network)" if cls == "S3" else ""))
        op.add("strike", s)
        if cls == "S3":
            self._s3_holds(op, s, finding, now)
        self._escalate(op, finding["clipper_id"], now)
        return s

    def _s3_holds(self, op: Op, strike: dict, finding: dict, now: datetime) -> None:
        code = HOLD_CODES_FROM_FINDING.get(finding["kind"], "BOUGHT_ENGAGEMENT")
        self._open_hold(op, "clipper", strike["clipper_id"], "strike_s3",
                        [R.item(code, f"S3 strike {strike['strike_id']}: every open certification of the clipper held",
                                [strike["strike_id"], *strike["finding_ids"]], self.rules())], now,
                        finding_id=finding["finding_id"], key=strike["strike_id"])

    def _escalate(self, op: Op, clipper_id: str, now: datetime) -> None:
        mine = {s["strike_id"]: s for s in self.strikes.values() if s["clipper_id"] == clipper_id}
        for s in self._staged(op, "strike"):
            if s["clipper_id"] == clipper_id:
                mine[s["strike_id"]] = s
        esc = i09.escalation(list(mine.values()), now, parse_iso)
        if not esc:
            return
        cls, ids = esc
        sid = rid("stk", "esc", clipper_id, ids)
        if sid in mine:
            return
        fids = sorted({f for i in ids for f in mine[i]["finding_ids"]})
        exp = i09.expires_at(cls, now)
        s = {"strike_id": sid, "clipper_id": clipper_id, "class": cls, "finding_ids": fids,
             "rule_id": mine[ids[-1]]["rule_id"], "issued_at": iso(now), "expires_at": iso(exp) if exp else None,
             "status": "active", "ban_recommended": cls == "S3", "escalated_into": None, "escalates": ids}
        for i in ids:
            op.add("strike", dict(mine[i], escalated_into=sid))
        op.record(derived_id("stk", sid), "strike_issued", i09.ACTOR, clipper_id,
                  {"strike_id": sid, "class": cls, "escalates": ids, "rule_id": s["rule_id"]},
                  f"Strike escalated to {cls}")
        op.add("strike", s)
        if cls == "S3":
            f = next((self.findings.get(x) for x in fids if self.findings.get(x)), None) or \
                {"finding_id": fids[0], "kind": "integrity_fail"}
            self._s3_holds(op, s, f, now)
        self._escalate(op, clipper_id, now)

    # ================================================================== anomaly (i06)

    def _screen(self, op: Op, sub: dict, now: datetime, settlement: Optional[tuple]) -> Optional[dict]:
        sid = sub["submission_id"]
        ct = sub.get("create_time")
        if ct is None:
            return None
        snaps = self._snapshots_of(op, sid)
        series = sorted((int(parse_iso(s["fetched_at"]).timestamp()), s["value"]) for s in snaps
                        if s["metric"] == "views" and s["dimension"] is None)
        latest = {}
        src = settlement[1]["snapshot_ids"] if settlement and settlement[1] else None
        for s in sorted(snaps, key=lambda s: s["fetched_at"]):
            if src is None or s["snapshot_id"] in src:
                if s["dimension"] is None:
                    latest[s["metric"]] = s["value"]
        country = {s["dimension"]["country"]: s["value"] for s in snaps if s["metric"] == "country_views" and s["dimension"]}
        fetch = settlement[1] if settlement else None
        duration = ((fetch or {}).get("video") or {}).get("duration_ms")
        peaks = []
        for c in self.certs.values():
            if c["clipper_id"] != sub["clipper_id"] or c["submission_id"] == sid or c["platform"] != sub["platform"] \
                    or c["status"] not in ("certified", "revised"):
                continue
            other = self.submissions[c["submission_id"]]
            if other.get("create_time") is None or not i06.baseline_ok(sub["platform"], ct, other["create_time"]):
                continue
            ser = sorted((int(parse_iso(s["fetched_at"]).timestamp()), s["value"]) for s in self._snapshots_of(None, c["submission_id"])
                         if s["metric"] == "views" and s["dimension"] is None)
            pk = i06.peak_daily_gain(ser)
            if pk is not None:
                peaks.append(pk)
        cn = self.ports.clipper_network
        cap = op.call("clipper_network", "view_cap", (sub["campaign_id"],), lambda: cn.view_cap(sub["campaign_id"]),
                      ViewCap(False))
        cap = cap if isinstance(cap, ViewCap) else ViewCap(False)
        x = i06.ScreenInput(sub["platform"], ct, series, latest.get("views"), latest.get("likes"),
                            latest.get("avg_view_percentage"), latest.get("reels_avg_watch_time_ms"), duration, country,
                            sub.get("target_regions"), peaks[-20:], cap.available,
                            cap.cap if isinstance(cap.cap, int) and not isinstance(cap.cap, bool) else None)
        res = i06.screen(x, self.cfg, self.rules(), (sid,))
        scr = {"screen_id": rid("scr", sid, iso(now)), "submission_id": sid, "at": iso(now), "decision": res["decision"],
               "signals": res["signals"], "decision_rate": res["decision_rate"], "reasons": res["reasons"]}
        op.record(derived_id("scr", scr["screen_id"]), "anomaly_screened", i06.ACTOR, sid,
                  {"screen_id": scr["screen_id"], "decision": res["decision"],
                   "signals": {k: v["status"] for k, v in res["signals"].items()}, "decision_rate": res["decision_rate"]},
                  f"Anomaly screen: {res['decision']} ({res['decision_rate']['evaluated']}/"
                  f"{res['decision_rate']['applicable']} signals evaluated)")
        op.add("screen", scr)
        if res["decision"] == "hold":
            self._open_hold(op, "clip", sid, "anomaly", res["reasons"], now,
                            key=",".join(R.codes(res["reasons"])))
        return scr

    # ================================================================== jobs

    def run_job(self, principal: str, request_id: str, job: str) -> dict:
        if job not in JOBS:
            raise NotFound("no such job")
        with self.lock:
            key, h, ent = self._idem(principal, request_id, f"jobs/{job}", {})
            if ent:
                return ent["response"]
            now = self._now()
            day = now.date().isoformat()
            done = self.job_runs.get((job, day))
            if done is not None:
                return self._idem_store(key, h, {"job": job, "day": day, "already_ran": True, "summary": done["summary"]})
            summary = getattr(self, f"_job_{job}")(now)
            op = Op(self, f"job|{job}|{day}", i01.ACTOR, f"job:{job}")
            op.record(derived_id("job", job, day), "job_cycle_completed", EVIDENCE, f"job:{job}",
                      {"job": job, "day": day, "summary_sha256": sha(summary)}, f"Scheduler job {job} ran for {day}")
            op.add("job_run", {"job": job, "day": day, "at": iso(now), "summary": summary})
            self._commit(op)
            return self._idem_store(key, h, {"job": job, "day": day, "already_ran": False, "summary": summary,
                                             "ledger_event_ids": op.events})

    def _in_watch(self, sub: dict, now: datetime) -> bool:
        if sub["platform"] not in P.CERTIFIABLE:
            return False
        start = datetime.fromtimestamp(sub["create_time"], timezone.utc) if sub.get("create_time") else \
            parse_iso(sub["posted_at"]) - timedelta(hours=1)
        w = self._window(sub, DEFAULT_LAG_DAYS) if sub.get("create_time") else None
        c = self.certs.get(self.cert_by_sub.get(sub["submission_id"], ""), {})
        end = parse_iso(c["window"]["revision_watch_end"]) if c.get("window") else \
            (w["revision_watch_end"] if w else start + timedelta(
                days=max(self.cfg.revision_watch_days, (sub.get("min_days_live") or 0) + DEFAULT_LAG_DAYS) + 2))
        if w and w.get("min_live_end"):
            end = max(end, w["min_live_end"])
        return start <= now <= end

    def _job_liveness(self, now: datetime) -> dict:
        today = now.date().isoformat()
        out = {"checked": 0, "live": 0, "not_live": 0, "unknown": 0}
        subs = sorted(self.submissions.values(), key=lambda s: s["received_seq"])
        # priority: clips inside their min-live period first (§C.1)
        def in_min_live(s):
            if not s.get("create_time") or not s.get("min_days_live"):
                return 1
            return 0 if now <= datetime.fromtimestamp(s["create_time"], timezone.utc) + timedelta(days=s["min_days_live"]) else 1
        for sub in sorted(subs, key=in_min_live):
            sid = sub["submission_id"]
            if (sid, today) in self.liveness or not self._in_watch(sub, now):
                continue
            op = Op(self, f"liv|{sid}|{today}", i04.ACTOR, sid)
            purpose = "liveness" if in_min_live(sub) == 0 else "revision"
            f = self._fetch(op, self._sub_now(op, sid), purpose, ("views", "likes"), now)
            state = f["live_state"] if f["available"] else "unknown"
            cause = None if f["available"] else f["cause"]
            if state == "unknown" and self.cfg.oembed_enabled and sub["platform"] == "tiktok":
                url = self.side.get(f"{sid}:share_url")
                if url and self.quota.oembed_allow(sid, now):
                    self.quota.oembed_spend(sid, now)
                    oe = self.ports.oembed
                    ans = op.call("platform_tiktok_oembed", "check", (sid, today), lambda: oe.check(url), "unknown")
                    if ans == "live":
                        state, cause = "live_public_fallback", "oembed_only"
            if cause == "quota_exhausted" or cause == "rate_limited":
                cause = "QUOTA_EXHAUSTED"
            chk = rid("liv", sid, today)
            rec = {"check_id": chk, "submission_id": sid, "day": today, "state": state, "cause": cause,
                   "fetch_id": f["fetch_id"], "checked_at": iso(now)}
            op.record(derived_id("liv", chk, iso(now)), "liveness_checked", i04.ACTOR, sid,
                      {"check_id": chk, "day": today, "state": state, "cause": cause}, f"Liveness {today}: {state}")
            op.add("liveness", rec)
            self._commit(op)
            out["checked"] += 1
            out["live" if state in i04.OK_STATES else ("unknown" if state == "unknown" else "not_live")] += 1
        return out

    def _job_metrics(self, now: datetime) -> dict:
        today = now.date().isoformat()
        out = {"fetched": 0, "unavailable": 0}
        for sub in sorted(self.submissions.values(), key=lambda s: s["received_seq"]):
            sid = sub["submission_id"]
            if not self._in_watch(sub, now) or any(f["purpose"] == "metrics" and f["fetched_at"][:10] == today
                                                   for f in self._fetches_of(None, sid)):
                continue
            op = Op(self, f"met|{sid}|{today}", i01.ACTOR, sid)
            f = self._fetch(op, sub, "metrics", tuple(P.AVAILABLE_METRICS.get(sub["platform"], ())), now)
            self._commit(op)
            out["fetched" if f["available"] else "unavailable"] += 1
        return out

    def _ledger_held(self) -> dict:
        try:
            entries = self.recorder.client.entries()
        except LedgerQueryFailed as exc:
            raise Unavailable(f"the evidence ledger cannot be read ({exc}); no certification issued") from None
        return {e.get("event_id"): e.get("payload_sha256") for e in entries
                if isinstance(e, dict) and e.get("department") == "verification_integrity"}

    def _job_certify(self, now: datetime) -> dict:
        held = self._ledger_held()
        out = {"evaluated": 0, "changed": 0, "certified": 0}
        for sub in sorted(self.submissions.values(), key=lambda s: s["received_seq"]):
            sid = sub["submission_id"]
            cert = self.certs[self.cert_by_sub[sid]]
            if cert["status"] in ("certified", "revised", "voided"):
                continue
            op = Op(self, f"cer|{sid}|{iso(now)}", i02.ACTOR, sid)
            new = self._evaluate(op, self._sub_now(op, sid), now, True, held)
            if new["status"] in ("not_certified", "certified") and new.get("window"):
                w = self._window(self._sub_now(op, sid), new["window"]["lag_days"])
                snap, sf = self._settlement(op, sid, w)
                if snap is not None and self._should_screen(op, sub, new):
                    scr = self._screen(op, self._sub_now(op, sid), now, (snap, sf))
                    if scr and scr["decision"] == "hold":
                        new = self._evaluate(op, self._sub_now(op, sid), now, False, held)
            out["evaluated"] += 1
            if self._cert_changed(cert, new):
                new = self._issue_cert(op, new, now)
                out["changed"] += 1
                out["certified"] += new["status"] == "certified"
            self._commit(op)
        return out

    def _should_screen(self, op: Op, sub: dict, cert: dict) -> bool:
        """Screen once per settlement (an anomaly hold is released only by a human, then not re-opened)."""
        sid = sub["submission_id"]
        prev = self.screens.get(sid)
        staged = [s for s in self._staged(op, "screen") if s["submission_id"] == sid]
        if staged:
            return False
        if prev is None:
            return True
        settle = cert["window"]["settle_at"]
        return prev["at"] < settle

    def _job_anomaly(self, now: datetime) -> dict:
        today = now.date().isoformat()
        out = {"screened": 0, "held": 0}
        for sub in sorted(self.submissions.values(), key=lambda s: s["received_seq"]):
            sid = sub["submission_id"]
            cert = self.certs[self.cert_by_sub[sid]]
            if cert["status"] != "pending" or not sub.get("create_time") or sub["platform"] not in P.CERTIFIABLE:
                continue
            if (self.screens.get(sid) or {}).get("at", "")[:10] == today:
                continue
            series = sorted((int(parse_iso(s["fetched_at"]).timestamp()), s["value"]) for s in self._snapshots_of(None, sid)
                            if s["metric"] == "views" and s["dimension"] is None)
            if i06.peak_daily_gain(series) is None:
                continue          # not screenable yet: no two snapshots ~24 h apart (the settlement screen still runs)
            op = Op(self, f"ano|{sid}|{today}", i06.ACTOR, sid)
            scr = self._screen(op, sub, now, None)
            self._commit(op)
            out["screened"] += 1
            out["held"] += bool(scr and scr["decision"] == "hold")
        return out

    def _job_revisions(self, now: datetime) -> dict:
        today = now.date().isoformat()
        out = {"checked": 0, "revised": 0, "unavailable": 0}
        for sub in sorted(self.submissions.values(), key=lambda s: s["received_seq"]):
            sid = sub["submission_id"]
            cert = self.certs[self.cert_by_sub[sid]]
            if cert["status"] not in ("certified", "revised") or now > parse_iso(cert["window"]["revision_watch_end"]):
                continue
            if any(f["purpose"] == "revision_watch" and f["fetched_at"][:10] == today for f in self._fetches_of(None, sid)):
                continue
            op = Op(self, f"rev|{sid}|{today}", i02.ACTOR, sid)
            f = self._fetch(op, sub, "revision_watch", ("views",), now)
            out["checked"] += 1
            snap = next((s for s in self._staged(op, "snapshot") if s["fetch_id"] == f["fetch_id"]
                         and s["metric"] == P.PAYABLE_METRIC[sub["platform"]] and s["dimension"] is None), None)
            if snap is None:
                out["unavailable"] += 1
                self._commit(op)
                continue
            rv = i02.revision(cert["certified_views"], cert["original_views"], snap["value"], self.cfg.strip_share)
            if rv is not None:
                self._revise(op, cert, rv, snap, "platform_revision_down", now)
                out["revised"] += 1
            self._commit(op)
        return out

    def _revise(self, op: Op, cert: dict, rv: dict, snap: Optional[dict], cause: str, now: datetime) -> dict:
        rules = self.rules()
        n = len(cert["revisions"]) + 1
        clb = rid("clb", cert["certification_id"], n)
        revision = {"revision_no": n, "at": iso(now), "old_views": rv["old_views"], "new_views": rv["new_views"],
                    "snapshot_id": snap["snapshot_id"] if snap else None, "cause": cause}
        clawback = {"clawback_id": clb, "certification_id": cert["certification_id"],
                    "submission_id": cert["submission_id"], "clipper_id": cert["clipper_id"],
                    "views_delta": rv["views_delta"], "cause": cause, "rule_id": "VI-05" if cause != "void_upheld_fraud"
                    else "VI-10", "snapshot_id": revision["snapshot_id"], "issued_at": iso(now)}
        status = "voided" if cause == "void_upheld_fraud" else "revised"
        code = "VOIDED" if status == "voided" else "REVISED_DOWN"
        reasons = [R.item(code, f"{rv['old_views']} -> {rv['new_views']} ({cause})",
                          [x for x in (revision["snapshot_id"], clb) if x], rules)]
        new = dict(cert, status=status, certified_views=rv["new_views"], revisions=cert["revisions"] + [revision],
                   clawback_ids=cert["clawback_ids"] + [clb], reasons=reasons, reason_lines=R.lines(reasons),
                   evaluation=cert["evaluation"] + 1, evaluated_at=iso(now))
        eid = derived_id("crv", cert["certification_id"], n)
        new["ledger_event_id"] = eid
        op.record(eid, "certification_revised", i02.ACTOR, cert["submission_id"],
                  {**self._cert_payload(new), "revision_no": n, "old_views": rv["old_views"],
                   "new_views": rv["new_views"], "cause": cause}, f"Certification {status}: {rv['old_views']} -> "
                                                                  f"{rv['new_views']}")
        op.record(derived_id("clb", clb), "clawback_issued", i02.ACTOR, cert["submission_id"],
                  {k: clawback[k] for k in ("clawback_id", "certification_id", "views_delta", "cause", "rule_id",
                                            "snapshot_id")}, f"Clawback record: {rv['views_delta']} views (counts only)")
        op.add("certification", new)
        op.add("clawback", clawback)
        if rv.get("stripped") and cause == "platform_revision_down":
            fid = rid("fnd", "stripped", cert["submission_id"])
            if fid not in self.findings:
                f = {"finding_id": fid, "kind": "platform_stripped", "code": "PLATFORM_STRIPPED", "subject_kind": "clip",
                     "subject_id": cert["submission_id"], "clipper_id": cert["clipper_id"], "status": "upheld",
                     "evidence_ids": [x for x in (revision["snapshot_id"], clb) if x], "rule_id": "VI-10",
                     "opened_at": iso(now), "decided_by": "auto (spec C.9: platform stripped >= threshold)",
                     "decided_at": iso(now), "decision_note_sha256": None}
                op.record(derived_id("fnd", fid, "open"), "finding_opened", i09.ACTOR, cert["submission_id"],
                          {"finding_id": fid, "kind": "platform_stripped", "rule_id": "VI-10", "auto_upheld": True},
                          "Finding opened and auto-upheld: platform stripped")
                op.add("finding", f)
                self._strike(op, f, now)
        return new

    def _job_retention(self, now: datetime) -> dict:
        op = Op(self, f"ret|{iso(now)}", EVIDENCE, "retention")
        doomed: dict[str, str] = {}
        for k, e in self.side.all():
            age = now - parse_iso(e["stored_at"])
            if age > timedelta(days=P.RAW_REFRESH_DAYS):
                if e["owner_kind"] == "connection" and self._refresh_account(op, e, now):
                    continue       # refreshed by a new fetch (VI-15b "delete or refresh")
                doomed[k] = "refresh_expired_30d"
                continue
            if e["owner_kind"] == "submission":
                c = self.certs.get(self.cert_by_sub.get(e["owner_id"], ""), {})
                sub = self.submissions.get(e["owner_id"], {})
                end = parse_iso(c["window"]["revision_watch_end"]) if c.get("window") else None
                if end is None and sub.get("create_time"):
                    end = datetime.fromtimestamp(sub["create_time"], timezone.utc) + timedelta(
                        days=self.cfg.revision_watch_days)
                if end is not None and now > end:
                    doomed[k] = "revision_watch_ended"
            elif e["owner_kind"] == "connection":
                c = self.connections.get(e["owner_id"])
                if c is None or c["status"] != "active":
                    doomed[k] = "connection_not_active"
        # minors: everything V&I holds about a minor subject goes (the attestation and identity HMACs stay, VI-CQ-03)
        for subj, aid in self.latest_age.items():
            if self.ages[aid]["result"] == "minor":
                owners = [c["connection_id"] for c in self.connections.values() if c["clipper_id"] == subj]
                owners += [s["submission_id"] for s in self.submissions.values() if s["clipper_id"] == subj]
                for k in self.side.keys_for(owners):
                    doomed[k] = "minor"
        by_cause: dict[str, list[str]] = {}
        for k, cause in doomed.items():
            by_cause.setdefault(cause, []).append(k)
        for cause, keys in sorted(by_cause.items()):
            self._purge(op, sorted(keys), cause, now)
        if op.ops:
            self._commit(op)
        return {"purged": len(doomed), "by_cause": {c: len(k) for c, k in sorted(by_cause.items())}}

    def _refresh_account(self, op: Op, e: dict, now: datetime) -> bool:
        c = self.connections.get(e["owner_id"])
        if c is None or c["status"] != "active":
            return False
        adapter, v = self.ports.adapters.get(c["platform"]), self.ports.vault
        acct = op.call(f"platform_{c['platform']}", "account", (c["connection_id"], iso(now)[:10]),
                       lambda: adapter.account(v, c["vault_ref"]), AccountAnswer(False))
        key_b = op.identity_key()
        if not isinstance(acct, AccountAnswer) or not acct.available or not acct.account_id or key_b is None:
            return False
        if i07.hmac_hex(key_b, f"account:{c['platform']}", acct.account_id) != c["platform_account_id_hmac"]:
            return False
        at = iso(now)
        op.side.append(lambda: self.side.refresh(f"{c['connection_id']}:account_id", at))
        op.add("retention", {"cause": "refreshed", "counts": {}, "at": at, "fields": [f"{c['platform']}:account_id"]})
        return True

    # ================================================================== attestations (§D.1)

    def _attest_common(self, sid: str, op: Op, now: datetime) -> tuple[Optional[dict], Optional[dict], list[dict]]:
        """(submission, certification, current blocking reasons beyond the certification's own)."""
        sub = self.submissions.get(sid)
        if sub is None:
            return None, None, []
        cert = self.certs[self.cert_by_sub[sid]]
        extra = []
        for hh in self._open_holds([("clip", sid), ("clipper", sub["clipper_id"])]):
            extra += hh["reasons"]
        for f in self._findings_on(sid, sub["clipper_id"], ("bought_engagement", "platform_stripped"), ("open",)):
            extra.append(R.item("BOUGHT_ENGAGEMENT" if f["kind"] == "bought_engagement" else "PLATFORM_STRIPPED",
                                f"{f['kind'].replace('_', ' ')} finding open", (f["finding_id"],), self.rules()))
        return sub, cert, extra

    def _anomaly_passed(self, sid: str) -> bool:
        """Screened, no anomaly hold open, and either the latest screen found nothing or every anomaly hold on the
        clip was released by a human (a released hold is a human's 'passed'; an upheld one never is)."""
        scr = self.screens.get(sid)
        if scr is None:
            return False
        holds = [h for h in self.holds.values() if h["subject_kind"] == "clip" and h["subject_id"] == sid
                 and h["cause"] == "anomaly"]
        if any(h["status"] != "released" for h in holds):
            return False
        return scr["decision"] == "no_hold" or bool(holds)

    def _cert_verified(self, cert: dict) -> bool:
        if cert["status"] == "certified":
            return True
        if cert["status"] == "revised":
            return not self._findings_on(cert["submission_id"], cert["clipper_id"],
                                         ("bought_engagement", "platform_stripped", "stolen_content",
                                          "duplicate_identity", "account_shared"), ("open",))
        return False

    def _attest(self, principal: str, request_id: str, route: str, body: dict, subject: str, event_type: str,
                build: Callable[[Op, datetime], tuple[dict, list[dict]]]) -> dict:
        """Record-first attestation answer, idempotent with re-evaluation (the AEGIS N14-15b pattern): a replay
        returns the stored answer only when the outcome is unchanged; otherwise a new attestation is issued."""
        with self.lock:
            key, h, ent = self._idem(principal, request_id, route, body)
            now = self._now()
            base_id = rid("att", principal, request_id)
            op = Op(self, base_id, i10.ACTOR, subject)
            answer, reasons = build(op, now)
            outcome = {"answer": {k: v for k, v in answer.items() if k not in ("attestation_id", "reason")},
                       "codes": [(r["code"], r["message"]) for r in reasons], "rules_version": self.rules_version}
            outcome_sha = sha(outcome)
            if ent is not None and ent.get("outcome_sha256") == outcome_sha:
                return ent["response"]
            att_id = base_id if ent is None else rid("att", principal, request_id, outcome_sha)
            if att_id in self.attestations:
                return self.attestations[att_id]["response"]
            answer = dict(answer, attestation_id=att_id)
            eid = derived_id("att", att_id)
            op.record(eid, event_type, i10.ACTOR, subject[:128],
                      {"attestation_id": att_id, "request_id_sha256": sha_text(request_id),
                       "facts_sha256": answer.get("facts_sha256"), "outcome_sha256": outcome_sha,
                       "codes": R.codes(reasons), "rule_ids": sorted({r["rule_id"] for r in reasons}),
                       "rules_version": self.rules_version}, f"{event_type.replace('_', ' ')}: "
                                                            f"{'positive' if not reasons else f'{len(reasons)} reasons'}")
            rec = {"attestation_id": att_id, "kind": event_type, "subject_id": subject, "principal": principal,
                   "request_id": request_id, "request_sha256": h, "outcome_sha256": outcome_sha,
                   "first_used_at": iso(ent["at"]) if ent else iso(now), "issued_at": iso(now), "response": answer,
                   "ledger_event_id": eid}
            op.add("attestation", rec)
            self._commit(op)
            return answer

    def attest_hr13(self, principal: str, request_id: str, body: dict) -> dict:
        sid = body["submission_id"]
        sent = {k: body[k] for k in ("submission_id", "post_ref", "platform", "posted_at", "settlement_lag_days")}
        fsha = facts_sha256(sent)

        def build(op: Op, now: datetime):
            rules = self.rules()
            reasons: list[dict] = []
            sub, cert, extra = self._attest_common(sid, op, now)
            base = {"request_id": request_id, "facts_sha256": fsha, "rules_pinned": self.rules_pinned,
                    "submission_id": sid, "certification_id": cert["certification_id"] if cert else None,
                    "settlement_source": cert.get("settlement_source") if cert else None}
            if self.current is None:
                reasons = [R.item("RULES_NOT_IN_FORCE", "no V&I rule version is in force", (), rules)]
            elif sub is None:
                reasons = [R.item("SUBMISSION_UNKNOWN", "submission not registered with V&I", (), rules)]
            else:
                mism = []
                if sha_text(body["post_ref"]) != sub["post_ref_sha256"]:
                    mism.append("post_ref")
                if body["platform"] != sub["platform"]:
                    mism.append("platform")
                if parse_iso(body["posted_at"]) != parse_iso(sub["posted_at"]):
                    mism.append("posted_at")
                if mism:
                    reasons.append(R.item("FACTS_MISMATCH", "differs from the registration: " + ", ".join(mism), (),
                                          rules))
                lag_used = (cert.get("window") or {}).get("lag_days")
                if lag_used is None:
                    lag_used, _, _, _ = self._lag(op)
                if body["settlement_lag_days"] != lag_used:
                    reasons.append(R.item("LAG_MISMATCH", f"settlement_lag_days {body['settlement_lag_days']} but V&I "
                                          f"used {lag_used}", (), rules))
                reasons += cert["reasons"] + extra
            reasons = R.dedupe(reasons)
            # verified_views reflects the certification and the request's facts only (spec §D.1); the other port
            # fields below carry their own reasons, which are listed too
            verified = bool(sub and cert and not reasons and self._cert_verified(cert) and self.current)
            anomaly_ok = bool(sub and self._anomaly_passed(sid))
            purchased = True
            live_ok = False
            strike = True
            if sub is not None and self.current is not None:
                purchased = bool(self._findings_on(sid, sub["clipper_id"], ("bought_engagement", "platform_stripped"))) \
                    or sid not in self.screens
                w = self._window(sub, (cert.get("window") or {}).get("lag_days") or DEFAULT_LAG_DAYS)
                lv = "unknown"
                if w is not None:
                    lv, _ = self._liveness_eval(None, sub, w, now.date())
                live_ok = lv == "pass"
                lg = self.ports.legal
                ta = op.call("legal_37", "takedown_notices", (sub["post_ref_sha256"],),
                             lambda: lg.takedown_notices(sub["post_ref_sha256"]), TakedownAnswer(False))
                ta = ta if isinstance(ta, TakedownAnswer) else TakedownAnswer(False)
                last = max((f for f in self._fetches_of(None, sid) if f["available"] and f["video"]),
                           key=lambda f: f["fetched_at"], default=None)
                rr = (last or {}).get("video", {}).get("rights_restricted") if last else None
                vi22 = (self.rules().get("VI-22") or {}).get("status") == "verified" and self.current is not None
                strike, sr = i04.copyright_strike(lv, ta.available, ta.notices if isinstance(ta.notices, int) else 1,
                                                  rr, vi22, rules)
                reasons += sr
                if not live_ok and not any(r["code"] in ("DELETED_BEFORE_MIN_LIVE", "LIVENESS_GAP", "MIN_LIVE_UNKNOWN")
                                           for r in reasons):
                    reasons.append(R.item("MIN_LIVE_UNKNOWN", "minimum live period not yet evidenced", (), rules))
            reasons = R.dedupe(reasons)
            ans = dict(base, verified_views=verified, anomaly_screen_passed=anomaly_ok,
                       purchased_engagement=purchased, still_live_at_minimum_period=live_ok, copyright_strike=strike,
                       reasons=reasons, reason_lines=R.lines(reasons),
                       reason=("verified under V&I rules v%d" % self.rules_version) if not reasons else
                       f"{len(reasons)} reasons: " + "; ".join(R.lines(reasons)), rules_version=self.rules_version)
            ans["reason"] = ans["reason"]
            ans["reason"] = ans["reason"][:1000]
            return ans, reasons

        return self._attest(principal, request_id, "clips/hr13", sent, sid, "hr13_attested", build)

    def attest_creative_clip(self, principal: str, request_id: str, sid: str, facts: dict) -> dict:
        fsha = facts_sha256(facts)

        def build(op: Op, now: datetime):
            rules = self.rules()
            sub, cert, extra = self._attest_common(sid, op, now)
            reasons: list[dict] = []
            if self.current is None:
                reasons = [R.item("RULES_NOT_IN_FORCE", "no V&I rule version is in force", (), rules)]
            elif sub is None:
                reasons = [R.item("SUBMISSION_UNKNOWN", "submission not registered with V&I", (), rules)]
            else:
                mism = [k for k in ("campaign_id", "rulebook_version", "clipper_id") if facts[k] != sub[k]]
                if sha_text(facts["post_ref"]) != sub["post_ref_sha256"]:
                    mism.append("post_ref")
                if parse_iso(facts["posted_at"]) != parse_iso(sub["posted_at"]):
                    mism.append("posted_at")
                if mism:
                    reasons.append(R.item("FACTS_MISMATCH", "differs from the registration: " + ", ".join(mism), (),
                                          rules))
                reasons += cert["reasons"] + extra
            reasons = R.dedupe(reasons)
            verified = bool(sub and not reasons and self._cert_verified(cert))
            checks = {"certification_id": cert["certification_id"] if cert else None,
                      "status": cert["status"] if cert else None,
                      "certified_views": cert["certified_views"] if cert and verified else None,
                      "same_clip": "fail" if any(r["code"] in ("HASH_MISMATCH", "CAPTION_CHANGED") for r in reasons) else
                      ("pass" if verified else "unknown"),
                      "still_live": "pass" if verified else "unknown",
                      "min_live_met": "pass" if verified else ("fail" if any(
                          r["code"] in ("DELETED_BEFORE_MIN_LIVE", "LIVENESS_GAP") for r in reasons) else "unknown"),
                      "anomaly": "passed" if (sub and self._anomaly_passed(sid)) else
                      ("hold" if sub and self.screens.get(sid) else "not_screened"),
                      "stolen_check": sub["stolen_check"] if sub else None,
                      "age": self._age_status(sub["clipper_id"], op)[0] if sub else "unknown",
                      "identity": (self.identities.get(sub["clipper_id"]) or {}).get("status", "none") if sub else "none"}
            ans = {"subject_id": sid, "verified": verified, "checks": checks, "request_id": request_id,
                   "facts_sha256": fsha, "rules_pinned": self.rules_pinned, "reasons": reasons,
                   "reason_lines": R.lines(reasons),
                   "reason": (f"verified under V&I rules v{self.rules_version}" if verified else
                              f"{len(reasons)} reasons: " + "; ".join(R.lines(reasons)))[:1000]}
            return ans, reasons

        return self._attest(principal, request_id, "clips/attest", {"submission_id": sid, "facts": facts}, sid,
                            "clip_attested", build)

    def attest_result(self, principal: str, request_id: str, result_id: str, facts: dict) -> dict:
        fsha = facts_sha256(facts)

        def build(op: Op, now: datetime):
            rules = self.rules()
            sid = facts["submission_id"]
            sub, cert, extra = self._attest_common(sid, op, now)
            reasons: list[dict] = []
            if self.current is None:
                reasons = [R.item("RULES_NOT_IN_FORCE", "no V&I rule version is in force", (), rules)]
            elif facts["result_id"] != result_id:
                reasons = [R.item("FACTS_MISMATCH", "result_id differs from the facts' result_id", (), rules)]
            elif sub is None:
                reasons = [R.item("SUBMISSION_UNKNOWN", "the result's submission is not registered", (), rules)]
            else:
                if facts["source"] == "self_reported":
                    reasons.append(R.item("FACTS_MISMATCH", "self-reported results are never verified", (), rules))
                mism = []
                if facts["platform"].strip().lower() != sub["platform"]:
                    mism.append("platform")
                if facts["campaign_id"] != sub["campaign_id"]:
                    mism.append("campaign_id")
                if mism:
                    reasons.append(R.item("FACTS_MISMATCH", "differs from the registration: " + ", ".join(mism), (),
                                          rules))
                if sub["platform"] == "youtube" and not self.cfg.feed_youtube_enabled:
                    reasons.append(R.item("YT_AGGREGATION_UNRESOLVED", "YouTube results stay out of the cross-clipper "
                                          "library pending VI-CQ-01", (), rules))
                if cert["status"] not in ("certified", "revised") or not self._cert_verified(cert):
                    reasons.append(R.item(cert["reasons"][0]["code"] if cert["reasons"] else "NOT_YET_SETTLED",
                                          f"certification is {cert['status']}", (cert["certification_id"],), rules))
                reasons += extra
            reasons = R.dedupe(reasons)
            verified = not reasons
            checks = {"certification_id": cert["certification_id"] if cert else None,
                      "status": cert["status"] if cert else None,
                      "verified_views": cert["certified_views"] if (cert and verified) else None}
            ans = {"subject_id": result_id, "verified": verified, "checks": checks, "request_id": request_id,
                   "facts_sha256": fsha, "rules_pinned": self.rules_pinned, "reasons": reasons,
                   "reason_lines": R.lines(reasons),
                   "reason": (f"verified under V&I rules v{self.rules_version}" if verified else
                              f"{len(reasons)} reasons: " + "; ".join(R.lines(reasons)))[:1000]}
            return ans, reasons

        return self._attest(principal, request_id, "results/attest", {"result_id": result_id, "facts": facts},
                            result_id, "result_attested", build)

    # ================================================================== age (i05)

    def age_check(self, principal: str, request_id: str, body: dict) -> dict:
        with self.lock:
            safe_body = {k: v for k, v in body.items() if k != "dob"}
            safe_body["dob_sha256"] = sha_text(body["dob"])
            key, h, ent = self._idem(principal, request_id, "age/checks", safe_body)
            if ent:
                return ent["response"]
            now = self._now()
            rules = self.rules()
            subject = body["subject_id"]
            aid = rid("age", principal, request_id)
            op = Op(self, aid, i05.ACTOR, subject)
            try:
                dob = date.fromisoformat(body["dob"])
            except ValueError:
                raise Invalid("dob must be a real calendar date (YYYY-MM-DD)") from None
            if dob > now.date() or dob.year < 1900:
                raise Invalid("dob out of range")
            prior = self.latest_age.get(subject)
            provider = provider_ref = None
            buffer_applied = False
            needs_human = False
            if prior and self.ages[prior]["result"] == "minor":
                result, reasons = "minor", [R.item("AGE_MINOR", "an earlier attestation for this subject was under 18 "
                                                   "(no re-attempt under another DOB)", (prior,), rules)]
            else:
                pre = i05.pre_check(dob, body["dob_field_neutral"], body["method"], now.date(), rules)
                if pre is not None:
                    result, reasons = pre
                else:
                    prov = self.ports.age
                    method, sess, dob_s = body["method"], body["provider_session_ref"], body["dob"]
                    ans = op.call("age_provider", "check", (aid, method), lambda: prov.check(method, sess, dob_s),
                                  AgeProviderAnswer("unavailable"))
                    ans = ans if isinstance(ans, AgeProviderAnswer) else AgeProviderAnswer("unavailable")
                    result, reasons, buffer_applied, needs_human = i05.judge(method, ans, self.cfg.fae_buffer_age, rules)
                    provider = ans.provider if isinstance(ans.provider, str) else None
                    provider_ref = ans.provider_ref if isinstance(ans.provider_ref, str) else None
            rec = {"attestation_id": aid, "subject_id": subject, "result": result, "method": body["method"],
                   "provider": (provider or "")[:64] or None,
                   "provider_ref_sha256": sha_text(provider_ref) if provider_ref else None,
                   "checked_at": iso(now), "dob_field_neutral": body["dob_field_neutral"],
                   "buffer_applied": buffer_applied, "code": reasons[0]["code"] if reasons else None,
                   "reasons": reasons, "reason_lines": R.lines(reasons), "rules_version": self.rules_version}
            op.record(derived_id("age", aid), "age_attested", i05.ACTOR, subject,
                      {"attestation_id": aid, "result": result, "method": body["method"],
                       "provider_ref_sha256": rec["provider_ref_sha256"], "buffer_applied": buffer_applied,
                       "codes": R.codes(reasons)}, f"Age attestation: {result}")
            op.add("age", rec)
            if needs_human:
                self._open_hold(op, "clipper", subject, "age_review", reasons, now, key=aid)
            if result == "minor":
                # hard block: connections revoked and everything V&I holds about the subject purged now
                for c in [c for c in self.connections.values() if c["clipper_id"] == subject]:
                    self._revoke(op, c, "minor", now)
                owners = [c["connection_id"] for c in self.connections.values() if c["clipper_id"] == subject]
                owners += [s["submission_id"] for s in self.submissions.values() if s["clipper_id"] == subject]
                keys = self.side.keys_for(owners)
                op.record(derived_id("mnp", aid), "minor_data_purged", i05.ACTOR, subject,
                          {"attestation_id": aid, "values_purged": len(keys)}, "Minor: application data purged")
                if keys:
                    self._purge(op, keys, "minor", now)
            self._commit(op)
            shown = reasons + ([R.item("RULES_NOT_IN_FORCE", "no V&I rule version is in force: no answer built on "
                                       "this attestation is positive", (), rules)] if self.current is None else [])
            resp = {"attestation_id": aid, "subject_id": subject, "result": result, "method": body["method"],
                    "buffer_applied": buffer_applied, "rules_in_force": self.current is not None, "reasons": shown,
                    "reason_lines": R.lines(shown), "ledger_event_ids": op.events}
            return self._idem_store(key, h, resp)

    def _age_answer(self, op: Op, a: dict) -> tuple[str, list[dict]]:
        rules = self.rules()
        if self.current is None:
            return "unknown", [R.item("RULES_NOT_IN_FORCE", "no V&I rule version is in force", (), rules)]
        status, reasons, _ = self._age_status(a["subject_id"], op)
        if self.latest_age.get(a["subject_id"]) != a["attestation_id"] and status != "minor":
            status, reasons = "unknown", [R.item("AGE_NOT_ASSURED", "a later attestation supersedes this one", (), rules)]
        if status == "adult":
            reasons = self._rule_status_reasons(AGE_RULES, op)
            if reasons:
                status = "unknown"
        return status, reasons

    def age_attestation(self, attestation_id: str) -> dict:
        with self.lock:
            a = self.ages.get(attestation_id)
            if a is None:
                raise NotFound("no such attestation")
            op = Op(self, f"aga|{attestation_id}", i05.ACTOR, a["subject_id"])
            status, reasons = self._age_answer(op, a)
            ans = {"attestation_id": attestation_id, "status": status, "rules_pinned": self.rules_pinned,
                   "reasons": reasons, "reason_lines": R.lines(reasons),
                   "reason": (f"adult under V&I rules v{self.rules_version}" if status == "adult" else
                              "; ".join(R.lines(reasons)) or status)[:1000]}
            op.record(derived_id("aga", attestation_id, sha(ans)), "age_answer_issued", i05.ACTOR, a["subject_id"],
                      {"attestation_id": attestation_id, "status": status, "codes": R.codes(reasons),
                       "rules_version": self.rules_version}, f"Age attestation answered: {status}")
            op.add("age_answer", {"attestation_id": attestation_id, "status": status, "at": iso(self._now())})
            self._commit(op)
            return ans

    def age_subject(self, subject_id: str) -> dict:
        with self.lock:
            aid = self.latest_age.get(subject_id)
            op = Op(self, f"ags|{subject_id}", i05.ACTOR, subject_id)
            if aid is None:
                reasons = [R.item("AGE_NOT_ASSURED", "no age attestation on file", (), self.rules())]
                status = "unknown"
            else:
                status, reasons = self._age_answer(op, self.ages[aid])
            ans = {"allowed": status == "adult", "unmet": R.lines(reasons), "detail": aid or "no attestation",
                   "subject_id": subject_id, "rules_pinned": self.rules_pinned}
            op.record(derived_id("ags", subject_id, sha(ans)), "age_answer_issued", i05.ACTOR, subject_id,
                      {"attestation_id": aid, "status": status, "codes": R.codes(reasons),
                       "rules_version": self.rules_version}, f"Age status answered: {status}")
            op.add("age_answer", {"attestation_id": aid, "status": status, "at": iso(self._now())})
            self._commit(op)
            return ans

    # ================================================================== identity (i07)

    def identity_check(self, principal: str, request_id: str, clipper_id: str, email: str) -> dict:
        with self.lock:
            key, h, ent = self._idem(principal, request_id, "identity/checks",
                                     {"clipper_id": clipper_id, "email_sha256": sha_text(email)})
            if ent:
                return ent["response"]
            now = self._now()
            chk = rid("idc", principal, request_id)
            op = Op(self, chk, i07.ACTOR, clipper_id)
            key_b = op.identity_key()
            fin = self.ports.finance
            pay = op.call("finance_31", "payout_identity_hmac", (clipper_id,),
                          lambda: fin.payout_identity_hmac(clipper_id), PayoutIdentity(False))
            pay = pay if isinstance(pay, PayoutIdentity) else PayoutIdentity(False)
            why = []
            hm = {"email": None, "payout": None}
            if key_b is None:
                why.append("identity HMAC key unavailable (vault)")
            else:
                hm["email"] = i07.hmac_hex(key_b, "email", i07.normalize_email(email))
            if pay.available and isinstance(pay.identity_hmac, str) and len(pay.identity_hmac) == 64:
                hm["payout"] = pay.identity_hmac
            else:
                why.append("Finance 31 payout identity unavailable")
            found = []
            for kind, hv, other in i07.matches(hm, self.hmac_owner, clipper_id):
                f, _ = self._identity_finding(op, "duplicate_identity", "DUPLICATE_IDENTITY", clipper_id, other, kind, now)
                found.append(f["finding_id"])
            for kind, hv in hm.items():
                if hv and (kind, hv) in self.banned and not found:
                    f, _ = self._identity_finding(op, "duplicate_identity", "DUPLICATE_IDENTITY", clipper_id, None,
                                                  kind, now)
                    found.append(f["finding_id"])
            status = "finding" if found else ("incomplete" if why else "clear")
            rec = {"check_id": chk, "clipper_id": clipper_id, "status": status, "why": "; ".join(why),
                   "hmacs": {k: v for k, v in hm.items() if v}, "findings": found, "checked_at": iso(now)}
            op.record(derived_id("idc", chk), "identity_checked", i07.ACTOR, clipper_id,
                      {"check_id": chk, "status": status, "hmac_kinds": sorted(rec["hmacs"]), "findings": found},
                      f"Identity check: {status}")
            op.add("identity", rec)
            self._commit(op)
            return self._idem_store(key, h, {"check_id": chk, "clipper_id": clipper_id, "status": status,
                                             "clear": status == "clear", "why": rec["why"], "findings": found,
                                             "ledger_event_ids": op.events})

    # ================================================================== human decisions, bans

    def reviewer_allowed(self, name: str) -> bool:
        """A delegate token counts only when People 43 confirms the delegate (stand-in: nobody → Andre only)."""
        with self.lock:
            op = Op(self, f"ppl|{name}|{iso(self._now())}", EVIDENCE, "reviewer")
            p = self.ports.people
            try:
                ok = op.call("people_43", "delegate_active", (name,), lambda: p.delegate_active(name), None)
            except Unavailable:
                return False
            return ok is True

    def decide_hold(self, who: str, request_id: str, hold_id: str, decision: str, reason: str) -> dict:
        with self.lock:
            key, h, ent = self._idem(who, request_id, "holds/decision", {"id": hold_id, "d": decision, "r": reason})
            if ent:
                return ent["response"]
            hold = self.holds.get(hold_id)
            if hold is None:
                raise NotFound("no such hold")
            if hold["status"] != "open":
                raise Conflict(f"hold is already {hold['status']}")
            if decision not in ("release", "uphold"):
                raise Invalid("a hold is released or upheld (overturn applies to findings)")
            now = self._now()
            op = Op(self, f"hd|{hold_id}|{request_id}", who, hold["subject_id"])
            new = dict(hold, status="released" if decision == "release" else "upheld", decided_by=who,
                       released_by=who if decision == "release" else None,
                       released_at=iso(now) if decision == "release" else None, decision_note_sha256=sha_text(reason))
            op.record(derived_id("hld", hold_id, decision), "hold_decided", who, hold["subject_id"],
                      {"hold_id": hold_id, "decision": decision, "note_sha256": sha_text(reason)},
                      f"Hold {decision}d by {who}")
            op.add("hold", new)
            if decision == "uphold":
                self._uphold_effects(op, hold, who, reason, now)
            self._commit(op)
            return self._idem_store(key, h, {"hold": new, "ledger_event_ids": op.events})

    def _uphold_effects(self, op: Op, hold: dict, who: str, reason: str, now: datetime) -> None:
        if hold["cause"] == "anomaly" and hold["subject_kind"] == "clip":
            sub = self.submissions[hold["subject_id"]]
            fid = rid("fnd", "bought", hold["hold_id"])
            f = {"finding_id": fid, "kind": "bought_engagement", "code": "BOUGHT_ENGAGEMENT", "subject_kind": "clip",
                 "subject_id": sub["submission_id"], "clipper_id": sub["clipper_id"], "status": "upheld",
                 "evidence_ids": [hold["hold_id"]], "rule_id": "VI-10", "opened_at": iso(now), "decided_by": who,
                 "decided_at": iso(now), "decision_note_sha256": sha_text(reason)}
            op.record(derived_id("fnd", fid, "upheld"), "finding_decided", who, sub["submission_id"],
                      {"finding_id": fid, "kind": "bought_engagement", "decision": "uphold", "rule_id": "VI-10"},
                      "Anomaly hold upheld as inflated: bought-engagement finding upheld")
            op.add("finding", f)
            self._strike(op, f, now)
            self._void_if_certified(op, sub["submission_id"], now)
        elif hold.get("finding_id") and hold["finding_id"] in self.findings:
            f = self.findings[hold["finding_id"]]
            if f["status"] == "open":
                self._decide_finding(op, f, "uphold", who, reason, now)

    def _void_if_certified(self, op: Op, sid: str, now: datetime) -> None:
        cert = self.certs[self.cert_by_sub[sid]]
        staged = [c for c in self._staged(op, "certification") if c["certification_id"] == cert["certification_id"]]
        cert = staged[-1] if staged else cert
        if cert["status"] in ("certified", "revised") and cert["certified_views"]:
            self._revise(op, cert, {"old_views": cert["certified_views"], "new_views": 0,
                                    "views_delta": -cert["certified_views"], "stripped": False}, None,
                         "void_upheld_fraud", now)

    def _decide_finding(self, op: Op, f: dict, decision: str, who: str, reason: str, now: datetime) -> dict:
        new = dict(f, status="upheld" if decision == "uphold" else "overturned", decided_by=who, decided_at=iso(now),
                   decision_note_sha256=sha_text(reason))
        op.record(derived_id("fnd", f["finding_id"], decision), "finding_decided", who, f["subject_id"],
                  {"finding_id": f["finding_id"], "kind": f["kind"], "decision": decision, "rule_id": f["rule_id"]},
                  f"Finding {f['kind']} {new['status']} by {who}")
        op.add("finding", new)
        if decision == "uphold":
            self._strike(op, new, now)
            if f["kind"] == "bought_engagement" and f["subject_kind"] == "clip":
                self._void_if_certified(op, f["subject_id"], now)
        else:
            for s in self.strikes.values():
                if f["finding_id"] in s["finding_ids"] and s["status"] == "active":
                    op.record(derived_id("stk", s["strike_id"], "lapsed"), "strike_lapsed", who, s["clipper_id"],
                              {"strike_id": s["strike_id"], "cause": "finding_overturned"}, "Strike lapsed (overturned)")
                    op.add("strike", dict(s, status="overturned"))
            for hh in self.holds.values():
                if hh.get("finding_id") == f["finding_id"] and hh["status"] == "open":
                    op.record(derived_id("hld", hh["hold_id"], "lapsed"), "hold_decided", who, hh["subject_id"],
                              {"hold_id": hh["hold_id"], "decision": "lapsed"}, "Hold lapsed (finding overturned)")
                    op.add("hold", dict(hh, status="released", released_by=who, released_at=iso(now)))
        return new

    def decide_finding_route(self, who: str, request_id: str, finding_id: str, decision: str, reason: str) -> dict:
        with self.lock:
            key, h, ent = self._idem(who, request_id, "findings/decision", {"id": finding_id, "d": decision, "r": reason})
            if ent:
                return ent["response"]
            f = self.findings.get(finding_id)
            if f is None:
                raise NotFound("no such finding")
            if decision not in ("uphold", "overturn"):
                raise Invalid("a finding is upheld or overturned")
            if f["status"] == "overturned" or (f["status"] == "upheld" and decision == "uphold"):
                raise Conflict(f"finding is already {f['status']}")
            now = self._now()
            op = Op(self, f"fd|{finding_id}|{request_id}", who, f["subject_id"])
            new = self._decide_finding(op, f, decision, who, reason, now)
            self._commit(op)
            return self._idem_store(key, h, {"finding": new, "ledger_event_ids": op.events})

    def ban(self, request_id: str, clipper_id: str, cn_decision_id: str, approved_at: str) -> dict:
        """Clipper Network reports Andre's ban decision; the API verified BOTH the caller and Andre's token."""
        with self.lock:
            key, h, ent = self._idem("clipper_network", request_id, "bans",
                                     {"c": clipper_id, "d": cn_decision_id, "a": approved_at})
            if ent:
                return ent["response"]
            if clipper_id in self.bans:
                raise Conflict("clipper already banned")
            now = self._now()
            recommended = any(s["clipper_id"] == clipper_id and s["ban_recommended"] and s["status"] == "active"
                              for s in self.strikes.values())
            op = Op(self, f"ban|{clipper_id}", "andre", clipper_id)
            blocked = sorted([list(k) for k in self.clipper_hmacs.get(clipper_id, set())])
            for c in [c for c in self.connections.values() if c["clipper_id"] == clipper_id]:
                self._revoke(op, c, "banned", now)
            rec = {"clipper_id": clipper_id, "cn_decision_id": cn_decision_id, "approved_at": approved_at,
                   "approved_by": "andre", "recommended_by_vi": recommended, "blocked": blocked, "at": iso(now)}
            op.record(derived_id("ban", clipper_id), "ban_propagated", "andre", clipper_id,
                      {"clipper_id": clipper_id, "cn_decision_id": cn_decision_id, "blocked_hmacs": len(blocked),
                       "recommended_by_vi": recommended}, f"Ban propagated: {len(blocked)} identity/account HMAC(s) blocked")
            op.add("ban", rec)
            self._commit(op)
            return self._idem_store(key, h, {"ban": rec, "ledger_event_ids": op.events})

    # ================================================================== reads

    def cert_view(self, c: dict) -> dict:
        return {k: c.get(k) for k in ("certification_id", "submission_id", "campaign_id", "rulebook_version",
                                      "clipper_id", "platform", "connection_id", "window", "status", "certified_views",
                                      "metric_name", "basis", "evidence_ids", "reasons", "reason_lines", "revisions",
                                      "clawback_ids", "rules_version", "compliance_register_version",
                                      "settlement_source", "ledger_event_id", "evaluated_at", "certified_at")}

    def get_certification(self, cid: str) -> dict:
        with self.lock:
            c = self.certs.get(cid)
            if c is None:
                raise NotFound("no such certification")
            return self.cert_view(c)

    def certification_for(self, sid: str) -> dict:
        with self.lock:
            cid = self.cert_by_sub.get(sid)
            if cid is None:
                raise NotFound("submission not registered")
            return self.cert_view(self.certs[cid])

    def _feed(self, cursor: int, pick: Callable[[str, dict], Optional[dict]]) -> dict:
        out, last = [], cursor
        for rec in self.log.iter_records(cursor + 1):
            last = rec["seq"]
            for kind, r in rec["data"].get("ops", []):
                x = pick(kind, r)
                if x is not None:
                    out.append({"seq": rec["seq"], **x})
            if len(out) >= FEED_PAGE:
                break
        return {"items": out[:FEED_PAGE], "next_cursor": last if last < len(self.log) else None}

    def feed_verified(self, cursor: int) -> dict:
        def pick(kind, r):
            if kind != "certification" or r["status"] not in ("certified", "revised", "voided"):
                return None
            if r["platform"] == "youtube" and not self.cfg.feed_youtube_enabled:
                return None
            return {k: r.get(k) for k in ("certification_id", "submission_id", "campaign_id", "platform", "status",
                                          "certified_views", "metric_name", "rules_version", "evaluated_at")}
        with self.lock:
            return self._feed(cursor, pick)

    def feed_clawbacks(self, cursor: int) -> dict:
        with self.lock:
            return self._feed(cursor, lambda k, r: dict(r) if k == "clawback" else None)

    def feed_strikes(self, cursor: int) -> dict:
        with self.lock:
            return self._feed(cursor, lambda k, r: dict(r) if k == "strike" else None)

    def list_holds(self) -> list[dict]:
        with self.lock:
            return sorted((dict(h) for h in self.holds.values()), key=lambda h: (h["status"] != "open", h["opened_at"]))

    def list_findings(self) -> list[dict]:
        with self.lock:
            return sorted((dict(f) for f in self.findings.values()),
                          key=lambda f: (f["status"] != "open", f["opened_at"]))

    def clipper_integrity(self, clipper_id: str) -> dict:
        with self.lock:
            now = self._now()
            strikes = [s for s in self.strikes.values() if s["clipper_id"] == clipper_id]
            active = [s for s in strikes if i09.is_active(s, now, parse_iso)]
            fnd = [f for f in self.findings.values() if f.get("clipper_id") == clipper_id]
            aid = self.latest_age.get(clipper_id)
            return {"clipper_id": clipper_id,
                    "strikes_active": {c: len([s for s in active if s["class"] == c]) for c in ("S1", "S2", "S3")},
                    "strikes": strikes, "ban_recommended": any(s["ban_recommended"] for s in active),
                    "banned": clipper_id in self.bans,
                    "bought_engagement_findings": [{k: f[k] for k in ("finding_id", "kind", "status")} for f in fnd
                                                   if f["kind"] in ("bought_engagement", "platform_stripped")],
                    "duplicate_identity_findings": [{k: f[k] for k in ("finding_id", "kind", "status")} for f in fnd
                                                    if f["kind"] in ("duplicate_identity", "account_shared")],
                    "age": self.ages[aid]["result"] if aid else "none",
                    "identity": (self.identities.get(clipper_id) or {}).get("status", "none"),
                    "open_holds": [h["hold_id"] for h in self._open_holds([("clipper", clipper_id)])],
                    "note": "integrity facts only; never a payout decision"}

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
        return {"status": "ok", "service": "verification-py", "rules_version": self.rules_version,
                "in_memory": self.log.in_memory, "rules_pinned": self.rules_pinned, "production": self.rules_pinned,
                "reconcile_mode": self.reconcile_mode, "reconcile_required": bool(self.reconcile_required),
                "platform_data_store_degraded": self.sidestore_degraded}
