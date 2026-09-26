"""
Compliance (38) service layer: state, record-first plumbing, operations.

Order of every state change (spec §G, C.9, B.7), without exception:
  1. every port call is recorded on the ledger first (``crossing_*``);
  2. the change's own ledger events are recorded (deterministic ids);
  3. the change is appended to the local log (fsynced when on disk);
  4. only then is it applied to in-memory state and answered.
A failure at 1-3 raises ``Unavailable`` (HTTP 503): nothing was issued.
State is event-sourced from the local log, so a restart with
COMPLIANCE_DATA_DIR replays exactly what was answered.

One lock serializes every operation (as creative-py's recorder lock does):
check-then-record-then-commit never interleaves.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from clock import Clock, SystemClock, iso, parse_iso
from controls import INTERNAL_CONTROLS, SEED_CONTROLS, control_status, expand_obligations, public_view
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from facts import ID_RE
from fetcher import FeedFetcher, FetchFailed, FetchRefused, NotWiredFetcher, Source, host_of, is_excluded_host, seeded_sources
from intelligences import (i01_register, i02_activation_gate, i03_payout_gate, i04_publish_gate, i05_control_monitor,
                           i06_change_watcher, i07_jurisdiction, i08_sanctions, i10_accessibility, i11_evidence_audit)
from intelligences.engine import Ctx, evaluate, finalize, unmet_line
from ledger import LedgerConflict, LedgerQueryFailed, LedgerRecordError, Recorder, canonical, derived_id
from ports import (A11yAnswer, AccessibilityChecker, AgeAttestation, ClipAttestation, DocVersion, Finance31Port,
                   Legal37Port, NotBuiltFinance31, NotBuiltLegal37, NotBuiltVerificationIntegrity,
                   NotWiredAccessibilityChecker, NotWiredSanctionsProvider, RailStatus, SanctionsScreeningProvider,
                   ScreenAnswer, TaxStatus, VerificationIntegrityPort)
from register import Version, effective_status, rows_sha256, sha256_text
from store import RecordLog, StoreWriteError
from textguard import injection_rules_in

SPEC_SEED_SHA256 = "4e3821d018a42be76ede1bf501004f0a8c5590327fa98ddd9f8c3f16845f584d"
IDEMPOTENCY_WINDOW = timedelta(minutes=15)
IDEMPOTENCY_MAX = 200_000
REGISTER_PAGE = 100
MAX_DECISIONS = 200
PAGE_FETCHES_PER_DAY = 2

GATE_EVENT = {"activation": "activation_ruling", "payout": "payout_ruling", "publish": "publish_ruling"}
GATE_ACTOR = {"activation": i02_activation_gate.ACTOR, "payout": i03_payout_gate.ACTOR,
              "publish": i04_publish_gate.ACTOR}
EVIDENCE_ACTOR = i11_evidence_audit.ACTOR


@dataclass
class Config:
    sanctions_freshness_days: int = 1
    disclosure_max_offset_s: float = 3.0
    a11y_max_age_days: int = 30
    watcher_enabled: bool = False
    site_owner_caller: str = "creative_production"
    watcher_max_proposals_per_cycle: int = 50     # AEGIS N14-11
    watcher_max_proposals_per_source: int = 20


@dataclass
class Ports:
    verification: VerificationIntegrityPort = field(default_factory=NotBuiltVerificationIntegrity)
    finance: Finance31Port = field(default_factory=NotBuiltFinance31)
    legal: Legal37Port = field(default_factory=NotBuiltLegal37)
    sanctions: SanctionsScreeningProvider = field(default_factory=NotWiredSanctionsProvider)
    accessibility: AccessibilityChecker = field(default_factory=NotWiredAccessibilityChecker)
    fetcher: FeedFetcher = field(default_factory=NotWiredFetcher)


def _b32(s: str, n: int = 26) -> str:
    return base64.b32encode(hashlib.sha256(s.encode("utf-8", "surrogatepass")).digest()).decode("ascii").lower()[:n]


def _sha(obj: Any) -> str:
    return sha256_text(canonical(obj))


class PortCalls:
    """Record-first proxy to the ports for one operation. Each distinct call is
    recorded as ``crossing_<port>_requested`` BEFORE it is made (payload: ids
    and hashes only); answers are memoized for the operation. A port that
    raises is treated as unavailable (fail closed)."""

    def __init__(self, svc: "ComplianceService", op_id: str, actor: str, subject: str, events: list[str]):
        self.svc, self.op_id, self.actor, self.subject, self.events = svc, op_id, actor, subject, events
        self._memo: dict = {}

    def _call(self, port: str, action: str, args: tuple, fn, fallback):
        key = (port, action, canonical(list(args)))
        if key in self._memo:
            return self._memo[key]
        eid = derived_id("x", self.op_id, port, action, list(args))
        self.svc._record(eid, f"crossing_{port}_requested", self.actor, self.subject,
                         {"port": port, "action": action, "args_sha256": _sha(list(args)), "op": self.op_id},
                         f"Request to {port}: {action}")
        self.events.append(eid)
        try:
            ans = fn()
        except Exception:  # noqa: BLE001 - any port failure is "unavailable", never a pass
            ans = fallback
        self._memo[key] = ans
        return ans

    def vi_age(self, attestation_id: str) -> AgeAttestation:
        p = self.svc.ports.verification
        return self._call("verification_integrity", "age_attestation", (attestation_id,),
                          lambda: p.age_attestation(attestation_id), AgeAttestation(False))

    def vi_clip(self, submission_id, post_ref, platform, posted_at, lag) -> ClipAttestation:
        p = self.svc.ports.verification
        return self._call("verification_integrity", "attest_clip", (submission_id, post_ref, platform, posted_at, lag),
                          lambda: p.attest_clip(submission_id, post_ref, platform, posted_at, lag), ClipAttestation(False))

    def fin_tax(self, payee_id: str) -> TaxStatus:
        p = self.svc.ports.finance
        return self._call("finance_31", "tax_status", (payee_id,), lambda: p.tax_status(payee_id), TaxStatus(False))

    def fin_rail(self, payee_id: str) -> RailStatus:
        p = self.svc.ports.finance
        return self._call("finance_31", "rail_status", (payee_id,), lambda: p.rail_status(payee_id), RailStatus(False))

    def legal_doc(self, doc_id: str) -> DocVersion:
        p = self.svc.ports.legal
        return self._call("legal_37", "current_version", (doc_id,), lambda: p.current_version(doc_id), DocVersion(False))


class GateEnv:
    """What the checks may read: ports (recorded), config, stored records."""

    def __init__(self, svc: "ComplianceService", ports: PortCalls):
        self.svc, self.ports, self.config = svc, ports, svc.config

    def screen(self, sid: str) -> Optional[dict]:
        return self.svc.screens.get(sid)

    def latest_screen(self, subject_id: str, role: str, owner_of: Optional[str]) -> Optional[dict]:
        cands = [s for s in self.svc.screens.values() if s["subject_id"] == subject_id and s["role"] == role
                 and (owner_of is None or s.get("owner_of") == owner_of)]
        return max(cands, key=lambda s: s["seq"]) if cands else None

    def sanctions_list_version(self) -> Optional[str]:
        return (self.svc.sanctions_list or {}).get("list_version")

    def a11y_results(self, content_sha: Optional[str]) -> list[dict]:
        return list(self.svc.a11y.get(content_sha or "", []))

    def activation(self, lane: str, subject_id: str) -> tuple[str, Optional[dict], Optional[str]]:
        """The subject's LATEST activation ruling in this lane, refused or not (AEGIS N14-1):
        ("none" | "blocked" | "allowed", profile when allowed, ruling id). A refused or blocked
        re-activation makes an earlier allowed one stale: it no longer counts."""
        rid = self.svc.latest_activation.get((lane, subject_id))
        if rid is None:
            return "none", None, None
        r = self.svc.rulings[rid]
        return ("allowed", r.get("profile"), rid) if r["allowed"] else ("blocked", None, rid)

    def latest_allowed_activation(self, lane: str, subject_id: str) -> Optional[dict]:
        return self.activation(lane, subject_id)[1]


class ComplianceService:
    def __init__(self, config: Config, recorder: Recorder, log: RecordLog, seed_bytes: bytes,
                 expected_seed_sha256: str = SPEC_SEED_SHA256, ports: Optional[Ports] = None,
                 clock: Optional[Clock] = None, sources: Optional[list[Source]] = None, reconcile_mode: bool = False):
        """``expected_seed_sha256`` other than the spec's pinned hash marks the service NON-PRODUCTION
        (``seed_pinned: false`` in /health and in every ruling; AEGIS N14-13). config.py allows that only
        with COMPLIANCE_ALLOW_UNPINNED_SEED=1.

        ``reconcile_mode`` (COMPLIANCE_RECONCILE_MODE=1, AEGIS N15-1): a disk log whose only problems against
        the ledger are VOIDABLE (see i11_evidence_audit.assess) starts, but answers nothing except reads and
        Andre's ``POST /compliance/v1/reconcile``; FATAL problems (a register rollback among them) still refuse."""
        self.config = config
        self.recorder = recorder
        self.log = log
        self.ports = ports or Ports()
        self.clock = clock or SystemClock()
        self.sources = sources if sources is not None else seeded_sources()
        self.lock = threading.RLock()
        seed_sha = hashlib.sha256(seed_bytes).hexdigest()
        if seed_sha != expected_seed_sha256:
            raise RuntimeError(f"seed file SHA-256 {seed_sha} does not match the expected {expected_seed_sha256}; "
                               "refusing to start (the seed must be copied unchanged)")
        self.seed_sha = seed_sha
        self.seed_pinned = seed_sha == SPEC_SEED_SHA256
        seed = json.loads(seed_bytes)
        self.seed_rows: list[dict] = seed["rows"]
        self.seed_by_id = {r["id"]: r for r in self.seed_rows}
        # state (event-sourced from the log)
        self.versions: list[Version] = []
        self.proposals: dict[str, dict] = {}
        self.rulings: dict[str, dict] = {}
        self.latest_activation: dict[tuple[str, str], str] = {}   # (lane, subject) -> latest activation ruling id
        self.screens: dict[str, dict] = {}
        self.a11y: dict[str, list[dict]] = {}
        self.control_defs: dict[str, dict] = {c["control_id"]: dict(c) for c in SEED_CONTROLS}
        self.control_state: dict[str, dict] = {}
        self.holds: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}
        self.seen_items: dict[str, set] = {}
        self.page_fetches: dict[tuple[str, str], int] = {}
        self.last_cycle: Optional[dict] = None
        self.sanctions_list: Optional[dict] = None
        self.expired_announced: set[tuple[str, str]] = set()
        self.idem: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
        self._wbudget: Optional[dict] = None   # Change Watcher drafting budget, set per cycle (N14-11)
        self._ledger_conflict = False          # the last _record failure was a 409 (a different record, same id)
        self.instance_id = secrets.token_hex(8)  # AEGIS N15-2: this process; its lease names it on the ledger
        self.reconcile_mode = bool(reconcile_mode)
        self.reconcile_required: list[str] = []  # voidable problems pending Andre (at start; after a reconcile)
        self._reconciling = False
        for rec in self.log.iter_records():
            self._apply(rec["kind"], rec["data"]["record"])
        if not self.log.in_memory:
            # AEGIS N14-4 / N15-1 / N15-2: the local log must match what the ledger anchors (no truncated tail,
            # no rewritten line, no register version behind the ledger's, no stray ruling, no newer lease from
            # another instance). Unverifiable = refuse (fail closed).
            try:
                a = self.assess_log()
            except LedgerQueryFailed as exc:
                raise RuntimeError(f"refusing to start: the local log cannot be verified against the evidence "
                                   f"ledger ({exc})") from None
            if a.fatal or (a.voidable and not self.reconcile_mode):
                hint = ("" if a.fatal or not a.voidable else
                        " -- only Andre can void these: start with COMPLIANCE_RECONCILE_MODE=1 and POST "
                        "/compliance/v1/reconcile (README, 'Reconciling the local log with the ledger')")
                raise RuntimeError("refusing to start: " + "; ".join(a.problems) + hint)
            self.reconcile_required = list(a.voidable)
        if self.reconcile_mode:
            return  # nothing is written (no seed proposal, no lease) until Andre reconciles and the service restarts
        try:
            self.ensure_seed_proposal()
        except Unavailable:
            pass  # ledger down at start: retried on the next inbox / decision call
        if not self.log.in_memory and len(self.log):
            try:
                self._write_lease()
            except Unavailable as exc:
                raise RuntimeError(f"refusing to start: the instance lease could not be recorded ({exc.reason})") from None

    # ------------------------------------------------------------------ plumbing

    def _now(self) -> datetime:
        """The clock's instant in UTC (AEGIS N14-14): every date the service computes
        (expiry, effective dates, SLA, freshness) is a UTC date whatever tz the clock uses."""
        return self.clock.now().astimezone(timezone.utc)

    def _record(self, event_id: str, event_type: str, actor: str, subject: str, payload: dict, summary: str) -> str:
        self._ledger_conflict = False
        if self.reconcile_mode and not self._reconciling:
            raise Unavailable("reconcile mode (COMPLIANCE_RECONCILE_MODE=1): only Andre's POST /compliance/v1/reconcile "
                              "is answered; restart without it once the log is reconciled", ledger_write="not_recorded")
        try:
            return self.recorder.record(event_id, event_type, actor, subject, payload, summary)
        except LedgerRecordError as exc:
            self._ledger_conflict = isinstance(exc, LedgerConflict)
            raise Unavailable(f"evidence ledger write failed ({type(exc).__name__}); nothing was issued",
                              ledger_write="unknown" if exc.took_effect != False else "not_recorded") from None  # noqa: E712

    def _commit(self, kind: str, record: dict, event_ids: list[str], after_anchor=None) -> dict:
        """Anchor the exact next local-log line on the ledger, append it (fsynced), then apply it.

        ``after_anchor`` (decisions): records the register version event AFTER the anchor and before the
        append, so a ledger failure there leaves only a voidable stray anchor, never a published version
        without its line (AEGIS N15-1: a version on the ledger above the local one is a rollback, fatal).
        A failure after the anchor leaves the anchor on the ledger: nothing on the ledger alone withdraws it;
        the next start (and C-11) reports it until Andre reconciles (N15-1). A decision line that could not be
        written is kept beside the log (``<log>.unwritten-<seq>``, best effort) so the operator can restore it."""
        data = {"record": record, "ledger_event_ids": list(event_ids), "register_version": self.version_number,
                "anchored": True}
        rec, line = self.log.prepare(kind, iso(self._now()), data)
        line_sha = hashlib.sha256(line).hexdigest()
        epoch = self.log.epoch or line_sha[:16]
        self._record(i11_evidence_audit.anchor_id(epoch, rec["seq"], line_sha), i11_evidence_audit.ANCHOR_TYPE,
                     EVIDENCE_ACTOR, i11_evidence_audit.LOG_SUBJECT,
                     {"epoch": epoch, "seq": rec["seq"], "line_sha256": line_sha, "kind": kind},
                     f"Local log line {rec['seq']} ({kind}) anchored")
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
            raise Unavailable(f"local store write failed ({exc}); nothing was issued") from None
        self._apply(kind, record)
        return record

    def assess_log(self) -> "i11_evidence_audit.Assessment":
        """Compare the local log with the ledger (raises LedgerQueryFailed when unreadable)."""
        client = self.recorder.client
        if not hasattr(client, "entries"):
            raise LedgerQueryFailed("this ledger client cannot read entries")
        entries = client.entries()
        shas = self.log.line_shas()
        lines, referenced, rulings, leases, reconciles, metas = [], set(), set(), [], [], []
        for rec, sha in zip(self.log.iter_records(), shas):
            d = rec["data"]
            lines.append((rec["seq"], sha, bool(d.get("anchored"))))
            referenced.update(d.get("ledger_event_ids") or [])
            r = d.get("record") or {}
            if rec["kind"] == "decision" and r.get("version"):
                metas.append(r["version"])
            if rec["kind"] == "ruling":
                rulings.add(r.get("ruling_id"))
            elif rec["kind"] == "lease":
                leases.append((rec["seq"], r.get("instance_id"), r.get("lease_event_id")))
            elif rec["kind"] == "reconcile":
                reconciles.append((rec["seq"], r.get("payload"), r.get("reconcile_event_id"), d.get("register_version")))
        return i11_evidence_audit.assess(entries, self.log.epoch, lines, referenced, self.version_number or 0,
                                         strict=not self.log.in_memory, local_rulings=rulings, local_leases=leases,
                                         reconciles=reconciles,
                                         local_versions=i11_evidence_audit.local_version_events(self.log.epoch, metas))

    def anchor_problems(self) -> list[str]:
        return self.assess_log().problems

    def _write_lease(self) -> None:
        """AEGIS N15-2: every start of a disk log records an instance lease (instance id + log head) on the
        ledger and in the log. A later lease from another instance (a copy of this data directory started
        elsewhere) makes this instance's C-11 red and refuses its next start."""
        with self.lock:
            n = len(self.log)
            head = self.log.line_shas()[-1]
            eid = i11_evidence_audit.lease_id(self.log.epoch, self.instance_id, n, head)
            self._record(eid, i11_evidence_audit.LEASE_TYPE, EVIDENCE_ACTOR, i11_evidence_audit.LOG_SUBJECT,
                         {"instance_id": self.instance_id, "epoch": self.log.epoch, "head_seq": n, "head_sha256": head},
                         f"Compliance instance lease at log line {n}")
            self._commit("lease", {"instance_id": self.instance_id, "lease_event_id": eid, "head_seq": n,
                                   "head_sha256": head}, [eid])

    def reconcile_plan(self) -> dict:
        """What Andre would void now (GET /compliance/v1/reconcile): the voidable problems and their exact items."""
        with self.lock:
            a = self.assess_log()
            shas = self.log.line_shas()
            return {"epoch": self.log.epoch, "head_seq": len(shas), "head_sha256": shas[-1] if shas else None,
                    "register_version": self.version_number, "fatal": a.fatal, "problems": a.voidable,
                    "voidable": {"lines": sorted(a.void_lines), "event_ids": sorted(a.void_event_ids)},
                    "reconcile_mode": self.reconcile_mode}

    def reconcile(self, request_id: str, head_sha256: str, void_lines: list[int], void_event_ids: list[str]) -> dict:
        """AEGIS N15-1: Andre's explicit operator action (the API verified his token). Voids exactly the stray
        anchors, rulings and leases the ledger shows now, bound to this log's head and register version; a
        rollback (fatal) is never reconciled."""
        with self.lock:
            key, h, cached = self._idem_check("andre", request_id, "reconcile",
                                              {"head": head_sha256, "lines": void_lines, "ids": void_event_ids})
            if cached:
                return cached
            plan = self.reconcile_plan()
            if plan["fatal"]:
                raise Conflict("cannot reconcile: " + "; ".join(plan["fatal"]))
            if head_sha256 != plan["head_sha256"]:
                raise Conflict("the local log head moved since you read the plan; read GET /compliance/v1/reconcile again")
            if not plan["voidable"]["event_ids"]:
                raise Conflict("nothing to reconcile: the local log matches the ledger")
            if sorted(set(void_lines)) != plan["voidable"]["lines"] or \
                    sorted(set(void_event_ids)) != plan["voidable"]["event_ids"]:
                raise Conflict("the void list is not exactly what the ledger shows now; nothing was voided",
                               expected=plan["voidable"])
            payload = i11_evidence_audit.reconcile_payload(plan["epoch"], self.version_number, plan["head_seq"],
                                                           plan["head_sha256"], void_lines, void_event_ids)
            eid = i11_evidence_audit.reconcile_id(plan["epoch"], plan["head_seq"], payload)
            self._reconciling = True
            try:
                self._record(eid, i11_evidence_audit.RECONCILE_TYPE, "andre", i11_evidence_audit.LOG_SUBJECT, payload,
                             f"Andre reconciled the local log at line {plan['head_seq']}: "
                             f"{len(payload['void_event_ids'])} ledger event(s) declared void")
                self._commit("reconcile", {"payload": payload, "reconcile_event_id": eid, "request_id": request_id}, [eid])
            finally:
                self._reconciling = False
            left = self.assess_log()
            self.reconcile_required = list(left.voidable)
            resp = {"reconcile_event_id": eid, "voided": payload["void_event_ids"], "void_lines": payload["void_lines"],
                    "remaining_problems": left.problems, "restart_required": self.reconcile_mode,
                    "ledger_event_ids": [eid]}
            return self._idem_store(key, h, resp)

    def _apply(self, kind: str, r: dict) -> None:
        if kind in ("proposal", "proposal_redraft"):
            self.proposals[r["proposal_id"]] = {**r, "status": "open", "decided_at": None, "note": None}
        elif kind == "decision":
            for d in r["decisions"]:
                p = self.proposals[d["proposal_id"]]
                p.update(status="approved" if d["decision"] == "approve" else "rejected", decided_at=r["decided_at"],
                         note=d.get("note"))
            v = r.get("version")
            if v:
                self.versions.append(Version(v["version"], v["created_at"], v["approved_by"], tuple(v["proposal_ids"]),
                                             v["rows_sha256"], v["prev_version_sha256"], tuple(v["rows"])))
            if r.get("controls") is not None:
                self.control_defs = {k: dict(c) for k, c in r["controls"].items()}
        elif kind == "ruling":
            self.rulings[r["ruling_id"]] = r
            if r["gate"] == "activation":
                self.latest_activation[(r["lane"], r["subject_id"])] = r["ruling_id"]
            if r.get("request_sha256") and r.get("first_used_at"):
                # idempotency survives a restart (AEGIS N14-15b): the store is rebuilt from the log
                key = (r["principal"], r["request_id"])
                self.idem[key] = {"h": r["request_sha256"], "at": parse_iso(r["first_used_at"]),
                                  "response": self.ruling_view(r), "outcome_sha256": r.get("outcome_sha256"),
                                  "ruling_id": r["ruling_id"]}
                self.idem.move_to_end(key)
                while len(self.idem) > IDEMPOTENCY_MAX:
                    self.idem.popitem(last=False)
        elif kind == "screen":
            self.screens[r["screen_id"]] = r
        elif kind == "a11y":
            self.a11y.setdefault(r["content_sha256"], []).append(r)
        elif kind == "control_result":
            prev = self.control_state.get(r["control_id"], {})
            st = {"last_result": r["result"], "last_tested_at": r["tested_at"],
                  "last_passed_at": r["tested_at"] if r["result"] == "pass" else prev.get("last_passed_at"),
                  "evidence": r["evidence"], "by": r["by"], "detail": r.get("detail")}
            self.control_state[r["control_id"]] = st
        elif kind == "hold_open":
            self.holds[r["hold_id"]] = dict(r)
        elif kind == "hold_release":
            self.holds[r["hold_id"]].update(status="released", released_at=r["released_at"])
        elif kind == "snapshot":
            self.snapshots[r["url"]] = r
            if r.get("item_keys"):
                self.seen_items.setdefault(r["url"], set()).update(r["item_keys"])
            day = r["fetched_at"][:10]
            self.page_fetches[(r["url"], day)] = self.page_fetches.get((r["url"], day), 0) + 1
        elif kind == "watcher_cycle":
            self.last_cycle = r
        elif kind == "sanctions_list":
            self.sanctions_list = r
        elif kind == "obligation_expired":
            self.expired_announced.add((r["obligation_id"], r["expires_at"]))
        # founder_refused, injection, jurisdiction_resolved, audit_export, fetch_attempt: evidence only

    def _idem_check(self, principal: str, request_id: str, route: str, body: Any) -> tuple[tuple, str, Optional[dict]]:
        if not isinstance(request_id, str) or not ID_RE.fullmatch(request_id):
            raise Invalid("request_id must be 1-128 characters of [A-Za-z0-9._:-]")
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

    def _idem_store(self, key: tuple, h: str, response: dict) -> dict:
        self.idem[key] = {"h": h, "at": self._now(), "response": response}
        while len(self.idem) > IDEMPOTENCY_MAX:
            self.idem.popitem(last=False)
        return response

    # ------------------------------------------------------------------ register

    @property
    def current(self) -> Optional[Version]:
        return self.versions[-1] if self.versions else None

    @property
    def version_number(self) -> Optional[int]:
        return self.current.version if self.current else None

    def rows_by_id(self) -> Optional[dict[str, dict]]:
        return self.current.by_id() if self.current else None

    def ever_ids(self) -> set[str]:
        ids: set[str] = set()
        for v in self.versions:
            ids.update(r["id"] for r in v.rows)
        return ids

    def ensure_seed_proposal(self) -> None:
        with self.lock:
            if any(p["kind"] == "seed" for p in self.proposals.values()):
                return
            now = self._now()
            p = i01_register.seed_proposal(self.seed_rows, self.seed_sha, now)
            e1 = self._record(derived_id("seed", self.seed_sha), "seed_loaded", i01_register.ACTOR, f"proposal:{p['proposal_id']}",
                              {"seed_sha256": self.seed_sha, "rows": len(self.seed_rows), "proposal_id": p["proposal_id"]},
                              f"Seed file hash verified; seed proposal created ({len(self.seed_rows)} rows)")
            e2 = self._record(derived_id("prop", p["proposal_id"], p["content_sha256"]), "register_proposal_created",
                              i01_register.ACTOR, f"proposal:{p['proposal_id']}",
                              {"proposal_id": p["proposal_id"], "kind": "seed", "content_sha256": p["content_sha256"]},
                              "Register proposal created: seed")
            self._commit("proposal", p, [e1, e2])

    def register_rows(self, gate=None, jurisdiction=None, status=None, domain=None, page: int = 1) -> dict:
        with self.lock:
            v = self.current
            if v is None:
                return {"register_version": None, "rows": [], "page": page, "total": 0}
            today = self._now().date()
            rows = []
            for r in v.rows:
                st = effective_status(r, today)
                if gate and gate not in r["gates"]:
                    continue
                if jurisdiction and not (r["jurisdiction"] == jurisdiction or r["jurisdiction"].startswith(jurisdiction + "-")):
                    continue
                if status and st != status:
                    continue
                if domain and r["domain"] != domain:
                    continue
                rows.append({**r, "effective_status": st})
            start = (page - 1) * REGISTER_PAGE
            return {"register_version": v.version, "rows_sha256": v.rows_sha256, "total": len(rows), "page": page,
                    "page_size": REGISTER_PAGE, "rows": rows[start:start + REGISTER_PAGE]}

    def register_row(self, oid: str) -> dict:
        with self.lock:
            history = []
            for v in self.versions:
                r = v.by_id().get(oid)
                if r is not None:
                    history.append({"version": v.version, "row_sha256": _sha(r), "status": r["status"],
                                    "verified_at": r["verified_at"], "expires_at": r["expires_at"]})
            v = self.current
            row = v.by_id().get(oid) if v else None
            if row is None:
                raise NotFound("no such obligation in the version in force")
            return {"register_version": v.version, "row": {**row, "effective_status": effective_status(row, self._now().date())},
                    "history": history}

    def version_list(self) -> list[dict]:
        with self.lock:
            return [{**v.meta, "version_sha256": v.version_sha256, "rows": len(v.rows)} for v in self.versions]

    def inbox(self) -> list[dict]:
        self.ensure_seed_proposal()
        with self.lock:
            open_ = [p for p in self.proposals.values() if p["status"] == "open"]

            def eff(p):
                row = p.get("proposed_row") or {}
                return (row.get("effective_date") or "9999-12-31", p["created_at"], p["proposal_id"])
            return [{k: v for k, v in p.items()} for p in sorted(open_, key=eff)]

    def create_proposal(self, principal: str, request_id: str, body: dict) -> dict:
        with self.lock:
            key, h, cached = self._idem_check(principal, request_id, "proposals", body)
            if cached:
                return cached
            now = self._now()
            pid = "prop-" + _b32(f"{principal}|{request_id}", 20)
            cur = self.rows_by_id()
            p = i01_register.build_proposal(body["kind"], body.get("target_id"), body.get("proposed_row"),
                                            body.get("evidence"), principal, now, cur, self.ever_ids(),
                                            self.control_defs, pid)
            events = []
            actor = "andre" if principal == "andre" else principal
            events.append(self._record(derived_id("prop", pid, p["content_sha256"]), "register_proposal_created", actor,
                                       f"proposal:{pid}", {"proposal_id": pid, "kind": p["kind"],
                                                           "target_id": p["target_id"], "content_sha256": p["content_sha256"]},
                                       f"Register proposal created: {p['kind']} {p['target_id'] or ''}"))
            deferred: list = []
            events += self._injection_check(f"proposal:{pid}", {"row": body.get("proposed_row"), "ev": body.get("evidence")},
                                            f"proposal:{pid}", actor, deferred)
            self._commit("proposal", p, events)
            for kind, rec, eids in deferred:
                self._commit(kind, rec, eids)
            return self._idem_store(key, h, {"proposal": p, "ledger_event_ids": events})

    def decide(self, request_id: str, decisions: list[dict]) -> dict:
        """Andre only (the API verified his token). Atomic: all approvals create one version, or nothing."""
        self.ensure_seed_proposal()
        with self.lock:
            key, h, cached = self._idem_check("andre", request_id, "decisions", decisions)
            if cached:
                return cached
            if not decisions or len(decisions) > MAX_DECISIONS:
                raise Invalid(f"1..{MAX_DECISIONS} decisions per call")
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
            if any(p.get("evidence") is None and p["kind"] == "reverify" for p in approvals):
                raise Conflict("a reverify proposal without evidence cannot be approved")
            now = self._now()
            base = list(self.current.rows) if self.current else None
            new_rows, new_controls = (base, self.control_defs)
            version_meta = None
            recheck: dict[str, dict] = {}
            if approvals:
                new_rows, new_controls = i01_register.apply(base, self.control_defs, approvals, now.date(), recheck)
            # AEGIS N14-9 / N15-3: weakening is recomputed at APPROVAL time against the row or control as it is
            # NOW (not the proposal-time base); a flagged, now-weakening or changed-since-drafted proposal is
            # approved only with an explicit acknowledgment, and the refusal shows the current diff.
            unacked = [recheck.get(p["proposal_id"]) or {"proposal_id": p["proposal_id"],
                                                          "weakening_reasons": p.get("weakening_reasons") or []}
                       for d, p in chosen if d["decision"] == "approve"
                       and i01_register.needs_acknowledgment(p, recheck.get(p["proposal_id"]))
                       and d.get("acknowledge_weakening") is not True]
            if unacked:
                why = "; ".join(f"{u['proposal_id']}: {', '.join(u['weakening_reasons']) or 'weakening changed since drafted'}"
                                for u in unacked)
                raise Invalid(f"approval weakens the register as it stands now ({why}); approving needs "
                              "acknowledge_weakening: true after reviewing the diff. Nothing was applied",
                              recheck=unacked[:20])
            row_change = approvals and any(p["kind"] not in ("control", *i01_register.INFO_KINDS) for p in approvals)
            events: list[str] = []
            for d, p in chosen:
                t = "register_proposal_approved" if d["decision"] == "approve" else "register_proposal_rejected"
                events.append(self._record(derived_id("pd", p["proposal_id"], p["content_sha256"], d["decision"]), t, "andre",
                                           f"proposal:{p['proposal_id']}",
                                           {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"],
                                            "decision": d["decision"], "note_sha256": _sha(d.get("note")),
                                            "weakening": bool(p.get("weakening")) or bool(
                                                (recheck.get(p["proposal_id"]) or {}).get("weakening_reasons")),
                                            "acknowledged_weakening": d.get("acknowledge_weakening") is True},
                                           f"Andre {d['decision']}d proposal {p['proposal_id']} ({p['kind']})"))
            if row_change:
                n = (self.version_number or 0) + 1
                prev = self.current.version_sha256 if self.current else None
                meta = {"version": n, "created_at": iso(now), "approved_by": "andre",
                        "proposal_ids": [p["proposal_id"] for p in approvals
                                         if p["kind"] not in ("control", *i01_register.INFO_KINDS)],
                        "rows_sha256": rows_sha256(new_rows), "prev_version_sha256": prev}
                epoch = self.log.epoch or "0" * 16  # the seed proposal line always exists before a decision
                vid = i11_evidence_audit.version_event_id(epoch, n, meta["rows_sha256"], prev)
                vargs = (vid, "register_version_published", i01_register.ACTOR, f"register:v{n}",
                         {k: meta[k] for k in ("version", "rows_sha256", "prev_version_sha256", "proposal_ids")},
                         f"Register version {n} published ({len(new_rows)} rows)")
                publish_version = lambda: self._record(*vargs)  # noqa: E731 - after the line's anchor (N15-1)
                events.append(vid)
                version_meta = {**meta, "rows": new_rows}
            if not row_change:
                publish_version = None
            record = {"request_id": request_id, "decided_at": iso(now),
                      "decisions": [{"proposal_id": d["proposal_id"], "decision": d["decision"], "note": d.get("note"),
                                     "acknowledged_weakening": d.get("acknowledge_weakening") is True}
                                    for d, _ in chosen],
                      "version": version_meta,
                      "controls": new_controls if any(p["kind"] == "control" for p in approvals) else None}
            self._commit("decision", record, events, after_anchor=publish_version)
            resp = {"decided": len(chosen), "approved": len(approvals), "register_version": self.version_number,
                    "rows_sha256": self.current.rows_sha256 if self.current else None, "ledger_event_ids": events}
            return self._idem_store(key, h, resp)

    # ------------------------------------------------------------------ gates

    def _injection_check(self, op_id: str, obj: Any, subject: str, actor: str,
                         deferred: Optional[list] = None) -> list[str]:
        """Record ``injection_text_ignored`` (ledger first). The local-log record is
        committed now, or appended to ``deferred`` for the caller to commit after
        its own main record (so a later 503 leaves no stray local record)."""
        rules = injection_rules_in(obj)
        if not rules:
            return []
        eid = self._record(derived_id("inj", op_id), "injection_text_ignored", actor, subject,
                           {"rules": rules, "count": len(rules), "op": op_id},
                           f"Instruction-like text in client data ignored ({len(rules)} pattern(s)); ruling unaffected")
        if deferred is None:
            self._commit("injection", {"op": op_id, "rules": rules}, [eid])
        else:
            deferred.append(("injection", {"op": op_id, "rules": rules}, [eid]))
        return [eid]

    def gate(self, principal: str, gate: str, request_id: str, subject_id: str, facts: Any, *,
             lane: Optional[str] = None, subject_kind: Optional[str] = None, caller_context: Any = None,
             route: str = "") -> dict:
        """Evaluate, record on the ledger, commit, answer (spec C.2).

        Idempotent replay (AEGIS N14-15b): a retry with the same request_id and body is RE-EVALUATED;
        the stored ruling is returned only when the new evaluation has the same outcome (allowed, unmet,
        register version, facts). If an input changed since (a hold opened, the register moved, a
        control went red, a row expired...), a new ruling is issued under a new id derived from the new
        outcome, so the ledger never sees two different rulings under one event id."""
        with self.lock:
            body = {"gate": gate, "subject_id": subject_id, "lane": lane, "subject_kind": subject_kind, "facts": facts,
                    "caller_context": caller_context}
            key, h, ent = self._idem_entry(principal, request_id, route or gate, body)
            if not isinstance(subject_id, str) or not ID_RE.fullmatch(subject_id):
                raise Invalid("subject_id must be 1-128 characters of [A-Za-z0-9._:-]")
            now = self._now()
            base_id = "cmp-rul-" + _b32(f"{principal}|{request_id}")
            actor = GATE_ACTOR[gate]
            events: list[str] = []
            ports = PortCalls(self, base_id, actor, subject_id, events)   # crossings: same ids on every retry
            env = GateEnv(self, ports)
            v = self.current
            rows = v.by_id() if v else {}
            if gate == "activation":
                ctx, hints = i02_activation_gate.build(lane, subject_id, facts, rows, self.seed_by_id, env, now)
            elif gate == "payout":
                ctx, hints = i03_payout_gate.build(subject_id, facts, rows, self.seed_by_id, env, now), {}
            else:
                ctx, hints = i04_publish_gate.build(subject_id, facts, rows, self.seed_by_id, env, now), {}

            deferred: list = []
            events += self._injection_check(base_id, {"facts": facts, "caller_context": caller_context}, subject_id,
                                            EVIDENCE_ACTOR, deferred)
            new_holds: list[dict] = []
            if v is None:
                items = [ctx.u("HR-04", "register_not_in_force",
                               "no register version is in force: Andre has not approved the seed")]
            else:
                items = evaluate(ctx)
                mismatch = hints.get("signal_mismatch")
                if mismatch:
                    hid = "hold-" + _b32(f"sig|{subject_id}|{mismatch['declared']}|{mismatch['signal']}", 20)
                    if hid not in self.holds:
                        new_holds.append({"hold_id": hid, "subject_id": subject_id, "kind": "jurisdiction_signal_mismatch",
                                          "obligation_id": "HR-05", "releasable": True, "status": "open",
                                          "opened_at": iso(now), "released_at": None,
                                          "detail": f"declared {mismatch['declared']}, network signal {mismatch['signal']}"})
                items += self._control_items(ctx, lane)
                for hold in new_holds:
                    items.append(ctx.u(hold["obligation_id"], f"hold_open:{hold['hold_id']}",
                                       f"hold open: {hold['kind']} (released only by Andre)"))
                items += self._hold_items(ctx, self._hold_subjects(gate, subject_id, ctx.facts))
            unmet = finalize(items)
            allowed = not unmet
            facts_sha = _sha(facts)
            outcome = {"gate": gate, "allowed": allowed, "lane": lane, "subject_kind": subject_kind,
                       "unmet": [[u["obligation_id"], u["code"]] for u in unmet], "register_version": self.version_number,
                       "facts_sha256": facts_sha}
            outcome_sha = _sha(outcome)
            if ent is not None and ent.get("outcome_sha256") == outcome_sha:
                return ent["response"]                  # inputs still current: the stored answer
            ruling_id = base_id if ent is None else "cmp-rul-" + _b32(f"{principal}|{request_id}|{outcome_sha}")
            if ruling_id in self.rulings:
                if self.rulings[ruling_id].get("outcome_sha256") == outcome_sha:
                    return self._idem_store_ruling(key, h, self.rulings[ruling_id], ent)
                ruling_id = "cmp-rul-" + _b32(f"{principal}|{request_id}|{outcome_sha}")
                if ruling_id in self.rulings:
                    return self._idem_store_ruling(key, h, self.rulings[ruling_id], ent)
            hold_events = []
            for hold in new_holds:
                hold_events.append(self._record(derived_id("hold", hold["hold_id"]), "hold_opened", i07_jurisdiction.ACTOR,
                                                subject_id, {"hold_id": hold["hold_id"], "kind": hold["kind"],
                                                             "obligation_id": hold["obligation_id"]},
                                                f"Hold opened: {hold['kind']}"))
            events += hold_events
            expired_new = []
            for oid in sorted(set(ctx.expired_rows)):
                exp = rows[oid]["expires_at"]
                if (oid, exp) not in self.expired_announced:
                    events.append(self._record(derived_id("exp", oid, exp), "obligation_expired", i01_register.ACTOR,
                                               f"obligation:{oid}", {"obligation_id": oid, "expires_at": exp},
                                               f"Obligation {oid} expired on {exp}; it blocks every gate it feeds"))
                    expired_new.append({"obligation_id": oid, "expires_at": exp})
            summary = (f"{gate.capitalize()} {'allowed' if allowed else 'blocked'}: "
                       f"{len(unmet)} unmet under register v{self.version_number or 0}")
            try:
                events.append(self._record(ruling_id, GATE_EVENT[gate], actor, subject_id,
                                           {"ruling_id": ruling_id, **outcome}, summary))
            except Unavailable:
                if ruling_id != base_id or not self._ledger_conflict:
                    raise
                # the ledger already holds a DIFFERENT ruling under the request's first id (an earlier attempt
                # was recorded but never committed here, and the state has moved since): issue under the
                # outcome-derived id instead of refusing forever (AEGIS N14-15b)
                ruling_id = "cmp-rul-" + _b32(f"{principal}|{request_id}|{outcome_sha}")
                events.append(self._record(ruling_id, GATE_EVENT[gate], actor, subject_id,
                                           {"ruling_id": ruling_id, **outcome}, summary))
            for kind, rec, eids in deferred:
                self._commit(kind, rec, eids)
            for hold, hev in zip(new_holds, hold_events):
                self._commit("hold_open", hold, [hev])
            for e in expired_new:
                self._commit("obligation_expired", e, [])
            lines = [unmet_line(u) for u in unmet]
            record = {"ruling_id": ruling_id, "gate": gate, "subject_id": subject_id, "lane": lane,
                      "subject_kind": subject_kind, "allowed": allowed, "unmet": unmet, "unmet_lines": lines,
                      "register_version": self.version_number, "evaluated_at": iso(now), "facts_sha256": facts_sha,
                      "caller_context_sha256": _sha(caller_context) if caller_context is not None else None,
                      "row_ids_evaluated": sorted(set(ctx.evaluated)), "ledger_event_id": ruling_id,
                      "ledger_event_ids": list(events), "principal": principal, "request_id": request_id,
                      "request_sha256": h, "outcome_sha256": outcome_sha,
                      "first_used_at": iso(ent["at"]) if ent is not None else iso(now),
                      "replaces_ruling_id": ent.get("ruling_id") if ent is not None else None}
            if gate == "activation" and allowed:
                owners = [self.screens[s]["subject_id"] for s in ctx.facts.get("owner_screen_ids") or [] if s in self.screens]
                record["profile"] = i02_activation_gate.profile(lane, ctx.facts, owners)
            if gate == "payout":
                record["disclosure_evidence_ref"] = (ctx.facts.get("disclosure") or {}).get("platform_toggle_evidence_ref")
                record["clipper_id"] = ctx.facts.get("clipper_id")
                record["campaign_id"] = ctx.facts.get("campaign_id")
            self._commit("ruling", record, events)   # _apply also refreshes the idempotency entry
            return self.idem[key]["response"]

    def _idem_entry(self, principal: str, request_id: str, route: str, body: Any) -> tuple[tuple, str, Optional[dict]]:
        """Like ``_idem_check`` but returns the stored ENTRY (gates re-evaluate before answering from it)."""
        if not isinstance(request_id, str) or not ID_RE.fullmatch(request_id):
            raise Invalid("request_id must be 1-128 characters of [A-Za-z0-9._:-]")
        key = (principal, request_id)
        h = _sha({"route": route, "body": body})
        ent = self.idem.get(key)
        if ent is not None:
            if ent["h"] != h:
                raise Conflict("request_id already used with a different body")
            if self._now() - ent["at"] > IDEMPOTENCY_WINDOW:
                raise Conflict("request_id reused (first used more than 15 minutes ago)")
        return key, h, ent

    def _idem_store_ruling(self, key: tuple, h: str, r: dict, ent: Optional[dict]) -> dict:
        view = self.ruling_view(r)
        self.idem[key] = {"h": h, "at": ent["at"] if ent is not None else self._now(), "response": view,
                          "outcome_sha256": r.get("outcome_sha256"), "ruling_id": r["ruling_id"]}
        return view

    def _hold_subjects(self, gate: str, subject_id: str, f: dict) -> list[str]:
        subs = [subject_id]
        if gate == "payout":
            subs += [f.get("clipper_id"), f.get("campaign_id")]
        elif gate == "publish":
            subs += [f.get("client_id")]
        return [s for s in subs if s]

    def _hold_items(self, ctx: Ctx, subjects: list[str]) -> list[dict]:
        out = []
        for hold in sorted(self.holds.values(), key=lambda x: x["hold_id"]):
            if hold["status"] == "open" and hold["subject_id"] in subjects:
                msg = f"hold open: {hold['kind']}" + (" (permanent; counsel)" if not hold["releasable"] else " (released only by Andre)")
                out.append(ctx.u(hold["obligation_id"], f"hold_open:{hold['hold_id']}", msg))
        return out

    def _control_items(self, ctx: Ctx, lane: Optional[str]) -> list[dict]:
        out = []
        rows = self.rows_by_id()
        now = self._now()
        for cid, d in sorted(self.control_defs.items()):
            if ctx.gate not in d["blocks_gates"]:
                continue
            if cid == "C-04" and ctx.gate == "activation" and lane != "zbc_creator":
                continue  # ADR 0006 choice 8: C-04 (sanctions list) blocks creator activation and payout (H.15)
            st = control_status(d, self.control_state.get(cid, {}), rows, now)
            if st["status"] != "green":
                first = (expand_obligations(d["obligation_ids"], rows or {}) or d["obligation_ids"])[0]
                out.append(ctx.u(first, f"control_red:{cid}", f"control {cid} ({d['title']}) is red: {st['reason']}"))
        return out

    def ruling_view(self, r: dict) -> dict:
        base = {"ruling_id": r["ruling_id"], "gate": r["gate"], "subject_id": r["subject_id"], "allowed": r["allowed"],
                "unmet": r["unmet"], "unmet_lines": r["unmet_lines"], "register_version": r["register_version"],
                "evaluated_at": r["evaluated_at"], "ledger_event_id": r["ledger_event_id"],
                "seed_pinned": self.seed_pinned,
                # AEGIS N15-8: what the ruling answers and was computed on; the thin clients check both
                "request_id": r.get("request_id"), "facts_sha256": r.get("facts_sha256")}
        if r["gate"] == "activation":
            base.update(lane=r["lane"], detail=r["ruling_id"])
        else:
            base.update(subject_kind=r["subject_kind"], reference=r["ruling_id"], reason=review_reason(r))
        return base

    def get_ruling(self, rid: str) -> dict:
        with self.lock:
            r = self.rulings.get(rid)
            if r is None:
                raise NotFound("no such ruling")
            return self.ruling_view(r)

    # ------------------------------------------------------------------ sanctions / accessibility

    def screen(self, principal: str, request_id: str, body: dict) -> dict:
        with self.lock:
            key, h, cached = self._idem_check(principal, request_id, "screen", body)
            if cached:
                return cached
            now = self._now()
            sid = "scr-" + _b32(f"{principal}|{request_id}", 20)
            subject = body["subject_id"]
            if body["role"] == "owner" and not body.get("owner_of"):
                raise Invalid("an owner screen names owner_of (the payee it owns)")
            if body["role"] == "payee" and body.get("owner_of"):
                raise Invalid("a payee screen has no owner_of")
            events: list[str] = []
            ports = PortCalls(self, sid, i08_sanctions.ACTOR, f"screen:{sid}", events)
            prov = self.ports.sanctions
            args = (body["legal_name"], list(body.get("aliases") or []), body.get("dob"), body["country"], body.get("region"))
            # the crossing payload carries only a hash of the inputs (DOB and names never leave in clear)
            ans: ScreenAnswer = ports._call("sanctions_provider", "screen", (_sha(list(args)),),
                                            lambda: prov.screen(*args), ScreenAnswer("unavailable"))
            if ans.result not in ("clear", "potential_match", "match", "unavailable"):
                ans = ScreenAnswer("unavailable")
            input_sha = _sha({"legal_name": body["legal_name"], "aliases": body.get("aliases") or [],
                              "country": body["country"], "region": body.get("region")})
            rec = {"screen_id": sid, "subject_id": subject, "role": body["role"], "owner_of": body.get("owner_of"),
                   "legal_name": body["legal_name"], "aliases": list(body.get("aliases") or []),
                   "country": body["country"], "region": body.get("region"), "result": ans.result,
                   "list_version": ans.list_version, "screened_at": ans.screened_at or iso(now),
                   "provider_ref": ans.provider_ref, "input_sha256": input_sha, "recorded_at": iso(now),
                   "seq": len(self.screens) + 1}
            events.append(self._record(derived_id("scr", sid), "sanctions_screen_result", i08_sanctions.ACTOR, f"screen:{sid}",
                                       {"screen_id": sid, "result": ans.result, "list_version": ans.list_version,
                                        "input_sha256": input_sha, "role": body["role"]},
                                       f"Sanctions screen result: {ans.result}"))
            hold = None
            if ans.result in ("potential_match", "match"):
                hold_subject = body.get("owner_of") or subject
                hold = {"hold_id": "hold-" + _b32(f"scr|{sid}", 20), "subject_id": hold_subject,
                        "kind": "sanctions_potential_match" if ans.result == "potential_match" else "sanctions_match",
                        "obligation_id": "US-OFAC-01", "releasable": ans.result == "potential_match", "status": "open",
                        "opened_at": iso(now), "released_at": None, "detail": f"screen {sid}"}
                events.append(self._record(derived_id("hold", hold["hold_id"]), "hold_opened", i08_sanctions.ACTOR,
                                           hold_subject, {"hold_id": hold["hold_id"], "kind": hold["kind"],
                                                          "obligation_id": "US-OFAC-01"},
                                           f"Hold opened: {hold['kind']}"))
            self._commit("screen", rec, events)
            if hold:
                self._commit("hold_open", hold, events[-1:])
            resp = {"screen_id": sid, "result": ans.result, "list_version": ans.list_version,
                    "screened_at": rec["screened_at"], "hold_id": hold["hold_id"] if hold else None,
                    "ledger_event_ids": events}
            return self._idem_store(key, h, resp)

    def accessibility_check(self, principal: str, request_id: str, body: dict) -> dict:
        with self.lock:
            key, h, cached = self._idem_check(principal, request_id, "a11y", body)
            if cached:
                return cached
            now = self._now()
            cid = "a11y-" + _b32(f"{principal}|{request_id}", 20)
            events: list[str] = []
            ports = PortCalls(self, cid, i10_accessibility.ACTOR, f"a11y:{cid}", events)
            chk = self.ports.accessibility
            a: A11yAnswer = ports._call("accessibility_provider", "check",
                                        (body["asset_ref"], body["asset_type"], body["content_sha256"]),
                                        lambda: chk.check(body["asset_ref"], body["asset_type"], body["content_sha256"]),
                                        A11yAnswer(False))
            rec = {"check_id": cid, "asset_ref": body["asset_ref"], "asset_type": body["asset_type"],
                   "content_sha256": body["content_sha256"], "owner_id": body["owner_id"], "available": bool(a.available),
                   "passed": bool(a.available and a.passed), "standard": a.standard, "violations_count": a.violations_count,
                   "report_sha256": a.report_sha256, "tool": a.tool, "tool_version": a.tool_version,
                   "checked_at": a.checked_at or iso(now), "covers_captions": bool(a.covers_captions),
                   "overlay_scripts_disabled": bool(a.overlay_scripts_disabled)}
            events.append(self._record(derived_id("a11y", cid), "accessibility_check_result", i10_accessibility.ACTOR,
                                       f"a11y:{cid}", {"check_id": cid, "content_sha256": rec["content_sha256"],
                                                       "available": rec["available"], "passed": rec["passed"],
                                                       "report_sha256": rec["report_sha256"]},
                                       f"Accessibility check: {'passed' if rec['passed'] else 'not passed'}"))
            self._commit("a11y", rec, events)
            return self._idem_store(key, h, {**rec, "status": "checked" if rec["available"] else "unavailable",
                                             "ledger_event_ids": events})

    # ------------------------------------------------------------------ holds / jurisdiction

    def list_holds(self) -> list[dict]:
        with self.lock:
            return [dict(hh) for hh in sorted(self.holds.values(), key=lambda x: x["hold_id"])]

    def release_hold(self, request_id: str, hold_id: str, reason: str) -> dict:
        with self.lock:
            key, h, cached = self._idem_check("andre", request_id, f"release:{hold_id}", reason)
            if cached:
                return cached
            hold = self.holds.get(hold_id)
            if hold is None:
                raise NotFound("no such hold")
            if hold["status"] != "open":
                raise Conflict("hold is not open")
            if not hold["releasable"]:
                raise Conflict("a sanctions match is a permanent block: no release through the API (counsel)")
            now = self._now()
            eid = self._record(derived_id("rel", hold_id), "hold_released_by_andre", "andre", hold["subject_id"],
                               {"hold_id": hold_id, "reason_sha256": sha256_text(reason)}, f"Hold released by Andre: {hold['kind']}")
            self._commit("hold_release", {"hold_id": hold_id, "released_at": iso(now), "reason": reason}, [eid])
            return self._idem_store(key, h, {"hold_id": hold_id, "status": "released", "ledger_event_ids": [eid]})

    def resolve(self, principal: str, request_id: str, body: dict) -> dict:
        with self.lock:
            key, h, cached = self._idem_check(principal, request_id, "resolve", body)
            if cached:
                return cached
            rows = self.rows_by_id() or self.seed_by_id
            params = i07_jurisdiction.Params.from_rows(rows)
            out = []
            if body.get("person") is not None:
                from facts import JURISDICTION, validate
                j, wrong = validate(JURISDICTION, body["person"])
                r = i07_jurisdiction.resolve_person(j, params, "person")
                out.append(r)
            for t in body.get("targets") or []:
                out.append(i07_jurisdiction.resolve_target(t, params))
            answers = [{"who": r.who, "code": r.code, "class": r.cls, "reason": r.reason, "cites": list(r.cites),
                        "missing": list(r.missing)} for r in out]
            rid = "jur-" + _b32(f"{principal}|{request_id}", 20)
            eid = self._record(derived_id("jur", rid), "jurisdiction_resolved", i07_jurisdiction.ACTOR, rid,
                               {"answers": [[a["code"], a["class"]] for a in answers]},
                               f"Jurisdictions resolved: {len(answers)}")
            self._commit("jurisdiction_resolved", {"resolution_id": rid, "answers": answers}, [eid])
            signal_hold = None
            if body.get("person") and body.get("network_country_signal") and \
                    body["network_country_signal"] != (body["person"] or {}).get("declared_country"):
                signal_hold = "network signal contradicts the declared country: a gate would open a hold (not allow)"
            return self._idem_store(key, h, {"resolution_id": rid, "register_version": self.version_number,
                                             "answers": answers, "note": signal_hold, "ledger_event_ids": [eid]})

    # ------------------------------------------------------------------ controls

    def controls_view(self) -> list[dict]:
        with self.lock:
            rows = self.rows_by_id()
            now = self._now()
            out = []
            for cid, d in sorted(self.control_defs.items()):
                st = self.control_state.get(cid, {})
                s = control_status(d, st, rows, now)
                out.append({**d, "status": s["status"], "reason": s["reason"], "not_in_force": s["not_in_force"],
                            "last_result": st.get("last_result"), "last_passed_at": st.get("last_passed_at"),
                            "last_tested_at": st.get("last_tested_at")})
            return out

    def control_view(self, cid: str) -> dict:
        for c in self.controls_view():
            if c["control_id"] == cid:
                return c
        raise NotFound("no such control")

    def trust_center(self) -> list[dict]:
        with self.lock:
            rows = self.rows_by_id()
            now = self._now()
            return [public_view(d, control_status(d, self.control_state.get(cid, {}), rows, now),
                                self.control_state.get(cid, {})) for cid, d in sorted(self.control_defs.items())]

    def owner_caller(self, cid: str) -> str:
        d = self.control_defs.get(cid)
        if d is None:
            raise NotFound("no such control")
        o = d["owner_department"]
        return self.config.site_owner_caller if o == "site_owner" else o

    def _control_result(self, cid: str, result: str, tested_at: str, evidence: list, by: str, op: str,
                        detail: Optional[str] = None) -> list[str]:
        d = self.control_defs[cid]
        rows = self.rows_by_id()
        now = self._now()
        before = control_status(d, self.control_state.get(cid, {}), rows, now)["status"]
        prev = self.control_state.get(cid, {})
        sim = {"last_result": result, "last_passed_at": tested_at if result == "pass" else prev.get("last_passed_at")}
        after = control_status(d, sim, rows, now)["status"]
        events = [self._record(derived_id("ctl", op, cid), "control_result_recorded", by, f"control:{cid}",
                               {"control_id": cid, "result": result, "tested_at": tested_at,
                                "evidence_sha256": [e.get("sha256") for e in evidence]},
                               f"Control {cid} result: {result}")]
        if before != after:
            events.append(self._record(derived_id("cst", op, cid), "control_status_changed", i05_control_monitor.ACTOR,
                                       f"control:{cid}", {"control_id": cid, "from": before, "to": after},
                                       f"Control {cid} {before} -> {after}"))
        self._commit("control_result", {"control_id": cid, "result": result, "tested_at": tested_at,
                                        "evidence": evidence, "by": by, "detail": detail}, events)
        return events

    def push_control_result(self, principal: str, cid: str, request_id: str, body: dict) -> dict:
        with self.lock:
            owner = self.owner_caller(cid)
            if owner == "compliance":
                raise Forbidden("this control is computed by Compliance itself (POST /compliance/v1/controls/internal/run)")
            if principal != owner:
                raise Forbidden(f"only the control's owner ({owner}) may submit its results")
            key, h, cached = self._idem_check(principal, request_id, f"control:{cid}", body)
            if cached:
                return cached
            tested = parse_iso(body["tested_at"])
            if tested > self._now() + timedelta(minutes=5):
                raise Invalid("tested_at is in the future")
            events = self._control_result(cid, body["result"], iso(tested), body["evidence"], principal,
                                          f"{principal}|{request_id}")
            return self._idem_store(key, h, {"control": self.control_view(cid), "ledger_event_ids": events})

    def run_internal_controls(self, request_id: str) -> dict:
        with self.lock:
            key, h, cached = self._idem_check("scheduler", request_id, "controls/internal/run", {})
            if cached:
                return cached
            now = self._now()
            today = now.date()
            op = f"scheduler|{request_id}"
            events: list[str] = []
            drafted = self._draft_reverify(op, events)
            rows = list(self.current.rows) if self.current else []
            results: dict[str, tuple[bool, str]] = {}
            open_reverify = {p["target_id"] for p in self.proposals.values() if p["status"] == "open" and p["kind"] == "reverify"}
            results["C-01"] = i05_control_monitor.test_c01(rows, open_reverify, today) if self.current else (False, "no register version")
            results["C-02"] = i05_control_monitor.test_c02(self.config.watcher_enabled, self.last_cycle, now)
            ports = PortCalls(self, derived_id("int", op), i08_sanctions.ACTOR, "control:C-04", events)
            prov = self.ports.sanctions
            lv = ports._call("sanctions_provider", "current_list_version", (), lambda: prov.current_list_version(), None)
            if isinstance(lv, str) and 0 < len(lv) <= 128:
                results["C-04"] = (True, "provider list version refreshed")
                events.append(self._record(derived_id("slv", op), "control_result_recorded", i08_sanctions.ACTOR,
                                           "control:C-04", {"list_version_sha256": sha256_text(lv)},
                                           "Sanctions list version refreshed"))
                self._commit("sanctions_list", {"list_version": lv, "refreshed_at": iso(now)}, events[-1:])
            else:
                results["C-04"] = (False, "sanctions provider unavailable: list version unknown")
            payouts = [r for r in self.rulings.values() if r["gate"] == "payout"]
            results["C-05"] = i05_control_monitor.test_c05(payouts, now)
            ledger_ok = False
            try:
                ledger_ok = bool(self.recorder.client.verify())
            except Exception:  # noqa: BLE001
                ledger_ok = False
            log_ok = self.log.verify()
            try:
                anchor = self.anchor_problems()   # AEGIS N14-4: the local log head against the ledger
            except LedgerQueryFailed as exc:
                anchor = [f"ledger entries unreadable ({exc})"]
            results["C-11"] = (ledger_ok and log_ok and not anchor,
                               (f"ledger verify {'passed' if ledger_ok else 'FAILED'}; local log chain "
                                f"{'verified' if log_ok else 'BROKEN'}; ledger anchors "
                                + ("match the local log" if not anchor else "MISMATCH: " + "; ".join(anchor)))[:600])
            results["C-12"] = i05_control_monitor.test_c12()
            results["C-15"] = i05_control_monitor.test_c15(rows, today)
            results["C-17"] = i05_control_monitor.test_c17()
            for cid in INTERNAL_CONTROLS:
                ok, detail = results[cid]
                d = self.control_defs.get(cid)
                intel = (d or {}).get("owner_intelligence") or "i05_control_monitor"
                actor = "intel_" + intel[1:]
                events += self._control_result(cid, "pass" if ok else "fail", iso(now), [], actor, op, detail)
            for oid, exp in self._newly_expired(rows, today):
                events.append(self._record(derived_id("exp", oid, exp), "obligation_expired", i01_register.ACTOR,
                                           f"obligation:{oid}", {"obligation_id": oid, "expires_at": exp},
                                           f"Obligation {oid} expired on {exp}; it blocks every gate it feeds"))
                self._commit("obligation_expired", {"obligation_id": oid, "expires_at": exp}, events[-1:])
            resp = {"results": {k: {"result": "pass" if v[0] else "fail", "detail": v[1]} for k, v in sorted(results.items())},
                    "reverify_proposals_drafted": drafted, "controls": self.controls_view(), "ledger_event_ids": events}
            return self._idem_store(key, h, resp)

    def _newly_expired(self, rows: list[dict], today) -> list[tuple[str, str]]:
        out = []
        for r in rows:
            if r["status"] != "superseded" and effective_status(r, today) == "expired" and \
                    (r["id"], r["expires_at"]) not in self.expired_announced:
                out.append((r["id"], r["expires_at"]))
        return out

    def _draft_reverify(self, op: str, events: list[str]) -> list[str]:
        if not self.current:
            return []
        now = self._now()
        drafted = []
        open_targets = {p["target_id"] for p in self.proposals.values() if p["status"] == "open" and p["kind"] == "reverify"}
        for row in i01_register.reverify_candidates(list(self.current.rows), now.date()):
            if row["id"] in open_targets:
                continue
            snap = self.snapshots.get(row.get("source_url") or "")
            if not snap or now - parse_iso(snap["fetched_at"]) > i01_register.REVERIFY_SNAPSHOT_MAX_AGE:
                continue
            built = i01_register.reverify_row_from_snapshot(row, snap)
            if built is None:
                continue
            new, ev = built
            pid = "prop-" + _b32(f"i01|{row['id']}|{snap['normalized_sha256']}|{snap['fetched_at']}", 20)
            if pid in self.proposals:
                continue
            p = i01_register.build_proposal("reverify", row["id"], new, ev, "i01_register", now, self.rows_by_id(),
                                            self.ever_ids(), self.control_defs, pid)
            events.append(self._record(derived_id("prop", pid, p["content_sha256"]), "register_proposal_created",
                                       i01_register.ACTOR, f"proposal:{pid}",
                                       {"proposal_id": pid, "kind": "reverify", "target_id": row["id"],
                                        "content_sha256": p["content_sha256"]},
                                       f"Register proposal created: reverify {row['id']}"))
            self._commit("proposal", p, events[-1:])
            drafted.append(pid)
        return drafted

    # ------------------------------------------------------------------ Change Watcher

    def watcher_sources(self) -> list[Source]:
        out = list(self.sources)
        known = {s.url for s in out}
        for r in (self.current.rows if self.current else []):
            u = r.get("source_url")
            if r["source_kind"] == "platform-policy" and r["status"] != "superseded" and u and u not in known \
                    and u.startswith("https://") and not is_excluded_host(host_of(u)):
                out.append(Source(f"platform-{r['id'].lower()}", u, "page"))
                known.add(u)
        return out

    def watcher_run(self, request_id: str) -> dict:
        with self.lock:
            key, h, cached = self._idem_check("scheduler", request_id, "watcher/run", {})
            if cached:
                return cached
            if not self.config.watcher_enabled:
                return self._idem_store(key, h, {"ran": False, "reason": "COMPLIANCE_WATCHER_ENABLED is not 1; "
                                                 "control C-02 stays red", "proposals": []})
            now = self._now()
            op = f"scheduler|{request_id}"
            events: list[str] = []
            ok, failed, skipped, proposals, dropped, cosmetic = [], [], [], [], 0, 0
            notices: list[str] = []
            rows = list(self.current.rows) if self.current else []
            # AEGIS N14-11: at most N drafted proposals per cycle and per source; the excess is summarised
            # in ONE "source flooded" inbox item per source (kind watch_notice) for Andre
            self._wbudget = {"cycle_left": self.config.watcher_max_proposals_per_cycle}
            for src in self.watcher_sources():
                day = iso(now)[:10]
                if src.method == "page" and self.page_fetches.get((src.url, day), 0) >= PAGE_FETCHES_PER_DAY:
                    skipped.append(src.source_id)
                    continue
                eid = self._record(derived_id("x", op, "feed_fetch", src.url), "crossing_feed_fetch_requested",
                                   i06_change_watcher.ACTOR, f"source:{src.source_id}"[:128],
                                   {"url_sha256": sha256_text(src.url), "method": src.method}, f"Fetch {src.method} source")
                events.append(eid)
                try:
                    res = self.ports.fetcher.fetch(src.url)
                    raw = res.body
                except (FetchRefused, FetchFailed) as exc:
                    failed.append(src.source_id)
                    events.append(self._record(derived_id("wsf", op, src.url), "watcher_source_failed",
                                               i06_change_watcher.ACTOR, f"source:{src.source_id}"[:128],
                                               {"url_sha256": sha256_text(src.url), "error": type(exc).__name__},
                                               "Watcher source failed"))
                    continue
                except Exception:  # noqa: BLE001 - a fetcher bug is a failed source, never a crash
                    failed.append(src.source_id)
                    continue
                self._wbudget.update(source_left=self.config.watcher_max_proposals_per_source, undrafted=0)
                try:
                    made, d, c = self._process_source(src, raw, res.fetched_at, rows, op, events)
                except (i06_change_watcher.FeedParseError, Invalid, Conflict):
                    failed.append(src.source_id)
                    events.append(self._record(derived_id("wsf", op, src.url), "watcher_source_failed",
                                               i06_change_watcher.ACTOR, f"source:{src.source_id}"[:128],
                                               {"url_sha256": sha256_text(src.url), "error": "unprocessable"},
                                               "Watcher source failed: unparseable feed or undraftable change"))
                    continue
                ok.append(src.source_id)
                proposals += made
                dropped += d
                cosmetic += c
                if self._wbudget["undrafted"]:
                    notices += self._flood_notice(src, self._wbudget["undrafted"], len(made), op, events)
            self._wbudget = None
            cyc = {"at": iso(now), "ok": ok, "failed": failed, "skipped_rate_limit": skipped,
                   "proposals": proposals, "dropped_unmatched": dropped, "cosmetic_discarded": cosmetic,
                   "flood_notices": notices}
            events.append(self._record(derived_id("wcc", op), "watcher_cycle_completed", i06_change_watcher.ACTOR,
                                       "watcher", {"ok": len(ok), "failed": len(failed), "proposals": len(proposals),
                                                   "dropped": dropped, "cosmetic": cosmetic, "flood_notices": len(notices)},
                                       f"Watcher cycle: {len(ok)} ok, {len(failed)} failed, {len(proposals)} proposals"))
            self._commit("watcher_cycle", cyc, events[-1:])
            return self._idem_store(key, h, {"ran": True, **cyc, "ledger_event_ids": events})

    def _process_source(self, src: Source, raw: bytes, fetched_at: str, rows: list[dict], op: str,
                        events: list[str]) -> tuple[list[str], int, int]:
        now = self._now()
        snap = i06_change_watcher.snapshot(raw)
        prev = self.snapshots.get(src.url)
        made: list[str] = []
        dropped = cosmetic = 0
        inj = injection_rules_in(snap["text"])
        if inj:
            events.append(self._record(derived_id("inj", op, src.url), "injection_text_ignored", i06_change_watcher.ACTOR,
                                       f"source:{src.source_id}"[:128], {"rules": inj, "count": len(inj)},
                                       "Instruction-like text on a watched page ignored"))
        item_keys: list[str] = []
        if src.method == "feed":
            items = i06_change_watcher.parse_feed(raw)
            seen = self.seen_items.get(src.url, set())
            item_keys = [it.key for it in items]
            if prev is not None and prev["normalized_sha256"] != snap["normalized_sha256"]:
                for it in items:
                    if it.key in seen:
                        continue
                    targets = i06_change_watcher.classify_item(it, src, rows)
                    if not targets:
                        dropped += 1
                        continue
                    for row in targets:
                        made += self._draft_change(src, row, it.link or src.url, it.doc_number, it.effective_date,
                                                   it.published, raw, snap, f"{it.title} — {it.abstract}"[:2000],
                                                   fetched_at, it.key, events)
        else:
            if prev is not None and prev["normalized_sha256"] == snap["normalized_sha256"]:
                if prev["raw_sha256"] != snap["raw_sha256"]:
                    cosmetic += 1
            elif prev is not None:
                excerpt = i06_change_watcher.first_difference_excerpt(prev.get("text", ""), snap["text"])
                for row in i06_change_watcher.page_rows(src, rows):
                    made += self._draft_change(src, row, src.url, None, None, None, raw, snap, excerpt, fetched_at,
                                               f"page|{snap['normalized_sha256']}", events)
        rec = {"url": src.url, "source_id": src.source_id, "fetched_at": fetched_at, "raw_sha256": snap["raw_sha256"],
               "normalized_sha256": snap["normalized_sha256"], "text": snap["text"], "excerpt": snap["text"][:2000],
               "item_keys": item_keys}
        self._commit("snapshot", rec, events[-1:] if events else [])
        return made, dropped, cosmetic

    def _flood_notice(self, src: Source, undrafted: int, drafted: int, op: str, events: list[str]) -> list[str]:
        now = self._now()
        pid = "prop-" + _b32(f"i06|flood|{op}|{src.source_id}", 20)
        if pid in self.proposals:
            return []
        watch = {"source_id": src.source_id, "url_sha256": sha256_text(src.url), "drafted": drafted,
                 "undrafted": undrafted, "cap_per_source": self.config.watcher_max_proposals_per_source,
                 "cap_per_cycle": self.config.watcher_max_proposals_per_cycle,
                 "note": "source flooded: changes beyond the cap were NOT drafted; review the source by hand"}
        p = i01_register.watch_notice_proposal(pid, now, watch)
        events.append(self._record(derived_id("prop", pid, p["content_sha256"]), "register_proposal_created",
                                   i06_change_watcher.ACTOR, f"proposal:{pid}",
                                   {"proposal_id": pid, "kind": "watch_notice", "content_sha256": p["content_sha256"],
                                    "undrafted": undrafted}, f"Watcher: source flooded, {undrafted} change(s) not drafted"))
        self._commit("proposal", p, events[-1:])
        return [pid]

    def _draft_change(self, src: Source, row: dict, link: str, doc_number, feed_effective, published, raw: bytes,
                      snap: dict, excerpt: str, fetched_at: str, item_key: str, events: list[str]) -> list[str]:
        now = self._now()
        note = (f"Change detected by i06 on {fetched_at[:10]} ({doc_number or 'no document number'}); "
                f"effective {feed_effective or 'not given by the source (Andre to confirm)'}; re-verification required")
        new_row = i06_change_watcher.changed_row(row, link, note)
        doc = doc_number if doc_number and len(doc_number) <= 64 else None
        ev = {"source_url": link, "fetched_at": fetched_at, "snapshot_sha256": snap["raw_sha256"],
              "normalized_text_sha256": sha256_text(excerpt) if src.method == "feed" else snap["normalized_sha256"],
              "quoted_excerpt": excerpt[:2000], "doc_number": doc}
        latency = None
        if published:
            latency = round((parse_iso(fetched_at) - parse_iso(published + "T00:00:00Z")).total_seconds() / 3600, 1)
        watch = {"source_id": src.source_id, "item_key_sha256": sha256_text(item_key), "item_published": published,
                 "detected_at": fetched_at, "detection_latency_hours": latency, "feed_effective_date": feed_effective,
                 "effective_date_flag": None if feed_effective else "effective date not given by the source; Andre to confirm"}
        # Page sources redraft an open proposal for the same (source, row) in place
        # (new content, new content_sha256): a decision against the old hash is a 409.
        base_id = f"i06|{src.source_id}|{row['id']}" if src.method == "page" else f"i06|{src.source_id}|{row['id']}|{item_key}"
        pid = "prop-" + _b32(base_id, 20)
        existing = self.proposals.get(pid)
        kind = "proposal"
        if existing is not None:
            if existing["status"] != "open":
                pid = "prop-" + _b32(f"{base_id}|{snap['normalized_sha256']}", 20)
                if pid in self.proposals:
                    return []
            else:
                if existing.get("evidence", {}) and existing["evidence"].get("normalized_text_sha256") == ev["normalized_text_sha256"]:
                    return []
                kind = "proposal_redraft"
        budget = getattr(self, "_wbudget", None)
        if budget is not None:
            if budget["cycle_left"] <= 0 or budget["source_left"] <= 0:
                budget["undrafted"] += 1
                return []
            budget["cycle_left"] -= 1
            budget["source_left"] -= 1
        p = i01_register.build_proposal("amend", row["id"], new_row, ev, "i06_change_watcher", now, self.rows_by_id(),
                                        self.ever_ids(), self.control_defs, pid, watch=watch)
        events.append(self._record(derived_id("prop", pid, p["content_sha256"]), "register_proposal_created",
                                   i06_change_watcher.ACTOR, f"proposal:{pid}",
                                   {"proposal_id": pid, "kind": "amend", "target_id": row["id"],
                                    "content_sha256": p["content_sha256"], "redraft": kind == "proposal_redraft"},
                                   f"Register proposal {'redrafted' if kind == 'proposal_redraft' else 'created'}: amend {row['id']}"))
        events.append(self._record(derived_id("wcd", pid, p["content_sha256"]), "watcher_change_detected",
                                   i06_change_watcher.ACTOR, f"obligation:{row['id']}",
                                   {"proposal_id": pid, "source_id": src.source_id, "doc_number": doc,
                                    "detection_latency_hours": latency}, f"Change detected for {row['id']}"))
        self._commit(kind, p, events[-2:])
        return [pid]

    # ------------------------------------------------------------------ audit / refusals

    def founder_refused(self, route: str, reason: str) -> None:
        """Best effort: a refusal stands whether or not it could be recorded."""
        with self.lock:
            now = self._now()
            op = f"{route}|{iso(now)}|{len(self.log)}"
            eid = self.recorder.try_record(derived_id("far", op), "founder_approval_refused", EVIDENCE_ACTOR, "founder_gate",
                                           {"route": route[:64], "reason_sha256": sha256_text(reason)},
                                           "Andre approval refused")
            try:
                self._commit("founder_refused", {"route": route[:64], "reason": reason[:200]}, [eid] if eid else [])
            except Unavailable:
                pass

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
                out.append(i11_evidence_audit.export_entry(rec))
                if len(out) >= i11_evidence_audit.PAGE:
                    break
            more = last < len(self.log)
            op = f"{principal}|{since}|{until}|{cursor}|{len(self.log)}"
            eid = self._record(derived_id("aud", op), "audit_export_issued", EVIDENCE_ACTOR, "audit_export",
                               {"cursor": cursor, "count": len(out), "next_cursor": last if more else None},
                               f"Audit export page served ({len(out)} records)")
            return {"records": out, "next_cursor": last if more else None, "ledger_event_id": eid,
                    "register_version": self.version_number}

    def health(self) -> dict:
        return {"status": "ok", "service": "compliance-py", "register_version_in_force": self.version_number,
                "in_memory": self.log.in_memory, "seed_pinned": self.seed_pinned, "production": self.seed_pinned,
                "reconcile_mode": self.reconcile_mode, "reconcile_required": bool(self.reconcile_required)}


def review_reason(r: dict) -> str:
    if r["allowed"]:
        return f"allowed under register v{r['register_version']}"
    reason = f"{len(r['unmet'])} unmet: " + "; ".join(r["unmet_lines"])
    return reason[:1000]
