"""
Onboarding service layer (ADR 0004).

The intelligences never call each other or another department. This layer
passes results between them, talks to the other departments through their
ports (integrations/*), and writes EVERY crossing and gate ruling to the
evidence ledger through ``LedgerClient`` (BUILD_CONTRACTS.md section 2).

Record-first discipline (fix wave 1, F2/F9): for every operation the
decision is computed first (pure), then the ledger records of every
decision are written, THEN outside effects happen (payouts, handoff,
contract storage, pushes to Andre, sending data to Revenue Recovery) and
state changes. Each outside effect is preceded by the record that
authorizes it; its result is recorded after it. In-process effects (bus
publishes, client-memory writes) are deferred to the end of a successful
operation.

- A ledger write that fails BEFORE any outside effect: nothing happened,
  state is unchanged, the API answers 503 ``proceeded: false`` — or, when
  the write's outcome is UNKNOWN (the reply was lost after sending; fix wave
  5, NEW-4), 503 ``proceeded: "unknown"`` with an instruction to retry the
  identical request. Either way the staged state stays uncommitted and the
  subject's sequence does not advance, so the retry derives the same event
  ids, the ledger answers 200 for any it already holds, and the retry
  commits the operation.
- A ledger write that fails AFTER an outside effect (only result records
  can): nothing further happens, state reflects what did happen, and the
  API answers 503 ``proceeded: true, completed: false`` naming every
  outside effect that was done (``LedgerWriteAfterEffects``). It never
  claims "did not proceed" when something did.

Event ids are deterministic (``ledger.derive_event_id``): a retry of an
operation whose ledger write committed but whose response was lost
replays the same ids, gets the ledger's 200, and records nothing twice.

State is in-process memory (like fulfillment-py). Per-client state lives in
``ClientRecord`` objects keyed by client id and is only ever reached through
that id — one client's data never flows into another client's work.

Time comes only from the injected clock. No request can set "now" or any
date a decision is evaluated against (fix wave 1, F1): the clipper age is
computed on the server's date at UTC-12 (``age_evaluation_date``), the
contract term and the 1099 tax year on the server's America/Los_Angeles
date (``business_date``), and caller timestamps that feed a decision
(fact ``observed_at``, a grant's ``account_last_activity_at``, a contract's
``signed_at``) are refused when they are in the server's future.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time as _time_mod
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from config import OnboardingConfig
from guardrails import OutboundBlocked, ai_disclosure_first_message, check_outbound, scan_for_injection
from integrations.bus import EventBus, InProcessEventBus
from integrations.departments import Departments
from integrations.platforms import NotWiredPlatformProbe, NotWiredPlatformWriter, PlatformProbe, PlatformWriteRefused
from integrations.revenue_recovery import RevenueRecoveryClient, RevenueRecoveryError
from integrations.vault import RefusingVault, SecretsVault
from intelligences import (
    i01_client_understanding as i01,
    i02_conversation as i02,
    i03_priority_fusion as i03,
    i04_platform_access as i04,
    i05_setup as i05,
    i06_audit_baseline as i06,
    i07_momentum_moment as i07,
    i08_risk_anomaly as i08,
    i09_promise_keeper as i09,
    i10_escalation_briefing as i10,
    i11_creator_vetting as i11,
    i12_brand_campaign as i12,
    i13_learning_loop as i13,
    i14_contract_obligation as i14,
    i15_compliance as i15,
)
from journal import ANCHOR_TYPE, ATTEMPT_KIND, EvidenceJournal
from ledger import (DEPARTMENT, EvidenceLineOwed, LedgerClient, LedgerQueryFailed, LedgerWriteAfterEffects,
                    LedgerWriteError, UnconfiguredLedgerClient, build_body, derive_event_id, payload_sha256)
from name_key import name_key_text
from statelog import Codec, StateCodecError, StateReplayError, StateTracker
from store import DataDirBusy, RecordLog, StoreWriteError
from memory import (
    AndreApprovalError,
    ClientMemoryStore,
    InstitutionalMemory,
    Playbook,
    PlaybookApprovalError,
    andre_action_token,
    verify_andre_token,
)
from redaction import redact_text, scrub_obj
from onboarding_schema import (
    HARD_TRIGGERS,
    AccessGrantIn,
    AccessVerification,
    ActivationDecision,
    Baseline,
    ClipperApplication,
    Commitment,
    CommitmentStatus,
    DomainEvent,
    Escalation,
    MAX_PLAUSIBLE_AGE_YEARS,
    Lane,
    MergedPlan,
    Person,
    Provenance,
    TriggerKind,
    VettingDecision,
    VettingOutcome,
    client_stated_amount,
    money_str,
)
from onboarding_schema import requests as rq
from practices import ad_disclosure, clean_exit, creator_tax, permission_receipt, recommend_score

log = logging.getLogger("onboarding.service")

# The company's business calendar: contract term, 1099 tax year.
BUSINESS_TZ = ZoneInfo("America/Los_Angeles")
# 18+ (written in stone) is evaluated on the EARLIEST calendar date anywhere
# on Earth (UTC-12, the "anywhere on Earth" convention): an 18th birthday
# counts only once that day has begun everywhere. Stricter than both the
# America/Los_Angeles date and the UTC date, so no applicant is approved
# while still 17 in their own time zone.
AGE_EVALUATION_TZ = timezone(timedelta(hours=-12))
LATEST_TZ = timezone(timedelta(hours=14))
# Wave F (AEGIS F-3): how long a write of a stopped process may still be on its way to the ledger (twice the ledger
# client's 5 s timeout); start-up waits out the rest of it before treating "not on the ledger" as final
LEDGER_INFLIGHT_GRACE_S = 10.0
# Wave F (AEGIS F-2): the ledger scan writes its high-water mark once it has read this many events (bounds each start)
SCAN_CHECKPOINT_EVENTS = 1000


# --- errors the API maps to status codes ---------------------------------------


class OnboardingError(Exception):
    status_code = 400

    def __init__(self, detail: str, body: Optional[dict] = None):
        super().__init__(detail)
        self.detail = detail
        self.body = body or {}


class NotFound(OnboardingError):
    status_code = 404


class Conflict(OnboardingError):
    status_code = 409


class Refused(OnboardingError):
    status_code = 403


class Invalid(OnboardingError):
    status_code = 422


class ServiceClosed(OnboardingError):
    """Bug sweep D (E-5/F-3): this instance was closed (its data directory given back); nothing is done."""

    status_code = 503


class UpstreamUnavailable(OnboardingError):
    status_code = 502


# --- per-subject records ---------------------------------------------------------


@dataclass
class SoftIssue:
    issue_id: str
    trigger: TriggerKind
    snag: str
    attempt: str
    attempted_at: datetime
    status: str = "attempted"  # attempted | resolved | escalated


@dataclass
class ClientRecord:
    client_id: str
    lane: Lane
    business_name: str
    signer: Person
    login_holder: Optional[Person]
    time_zone: str
    quiet_start: time
    quiet_end: time
    preferred_channel: Optional[str]
    deal_size: Optional[Decimal]
    started_at: datetime
    last_progress_at: datetime
    p1_disclosure_sent: bool = False
    facts: list = field(default_factory=list)
    vertical: Optional[str] = None
    ask_counts: dict = field(default_factory=dict)
    profile: Any = None
    detected_tags: list = field(default_factory=list)
    verifications: dict = field(default_factory=dict)  # platform -> AccessVerification
    grants: dict = field(default_factory=dict)  # platform -> AccessGrantIn (metadata only)
    receipts_sent: set = field(default_factory=set)
    findings: list = field(default_factory=list)
    raw_findings: list = field(default_factory=list)
    baseline: Optional[Baseline] = None
    risk: Optional[i08.RiskRuling] = None
    hard_stop: bool = False
    client_priorities: list = field(default_factory=list)
    plan: Optional[MergedPlan] = None
    plan_choices: dict = field(default_factory=dict)
    first_win_finding: Optional[str] = None
    first_win_at: Optional[datetime] = None
    recommend_asked: bool = False
    recommend_score: Optional[int] = None
    commitments: dict = field(default_factory=dict)
    escalation_ids: list = field(default_factory=list)
    soft_issues: dict = field(default_factory=dict)
    stalls: int = 0
    last_health: Optional[int] = None
    activation: Optional[ActivationDecision] = None
    handoff_accepted: bool = False
    exited: bool = False
    injection_flags: int = 0
    # Fix wave 3 (N2): set only when start_client stopped after an outside
    # effect; a retry of the same start completes the missing records.
    start_digest: Optional[str] = None
    start_view: Optional[dict] = None
    # Fix wave 4 (A1): the detection result of an audit that stopped after
    # Revenue Recovery answered (its result records are owed). A retry of the
    # same audit uses it instead of sending the account data again. Holds
    # the request digest and what detection returned — never the account data.
    audit_known: Optional[dict] = None


@dataclass
class CreatorRecord:
    creator_id: str
    application: ClipperApplication
    vetting: VettingDecision
    p1_disclosure_sent: bool
    w9_on_file: bool
    disclosure_training: bool
    agreement_signed: bool
    activation: Optional[ActivationDecision] = None
    payout_active: bool = False
    payments: list = field(default_factory=list)  # (date, Decimal)
    # Fix wave 3 (N2): set only when apply_creator stopped after an outside
    # effect; a retry of the same application completes it.
    apply_digest: Optional[str] = None
    apply_out: Optional[dict] = None
    # Bug sweep D (M): the 1099 payee identity -- the same person under two creator ids aggregates (``person_key``)
    person_key: str = ""


def person_key(legal_name: str, date_of_birth) -> str:
    """Bug sweep D (M, 1099 split; AEGIS O-1): one key per PERSON, from the legal name and the date of birth.
    Onboarding holds no tax identifier (P8, minimum data), so this is the identity the 1099 total is kept against: two
    creator ids for the same person share one total. The name is canonicalised by ``name_key.name_key_text`` (format
    characters removed, NFKC, casefold, apostrophe / dash variants folded, lookalike letters folded with creative-py's
    confusables table), so a zero-width space, a soft hyphen, a curly apostrophe or a Cyrillic "а" never splits a
    person. Only the hash is kept. ``pk2-``: the first cut's ``pk-`` keys were never deployed (no data to migrate)."""
    dob = date_of_birth.isoformat() if date_of_birth is not None else ""
    material = json.dumps(["onboarding-1099-person-v2", name_key_text(legal_name), dob])
    return "pk2-" + hashlib.sha256(material.encode()).hexdigest()[:32]


def _hhmm(s: Optional[str], default: time) -> time:
    if not s:
        return default
    hh, mm = s.split(":")
    try:
        return time(int(hh), int(mm))
    except ValueError as exc:
        raise Invalid("quiet hours must be valid HH:MM") from exc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dump(model) -> Any:
    return model.model_dump(mode="json") if model is not None else None


def _redact_value(v: Any) -> Any:
    """Fix wave 3 (N5): client free text is kept only in redacted form."""
    if isinstance(v, str):
        return redact_text(v)
    if isinstance(v, list):
        return [redact_text(x) if isinstance(x, str) else x for x in v]
    return v


def _latest_per_field(facts: list, keep: int) -> list:
    """The facts with only the latest ``keep`` observations of each field
    (by arrival order; the input order is preserved). Fix wave 6."""
    seen: dict[str, int] = {}
    kept = []
    for f in reversed(facts):
        n = seen.get(f.field, 0)
        if n < keep:
            seen[f.field] = n + 1
            kept.append(f)
    kept.reverse()
    return kept


class OnboardingService:
    def __init__(
        self,
        config: OnboardingConfig,
        ledger: LedgerClient,
        revenue_recovery: RevenueRecoveryClient,
        departments: Optional[Departments] = None,
        probe: Optional[PlatformProbe] = None,
        vault: Optional[SecretsVault] = None,
        bus: Optional[EventBus] = None,
        clock: Callable[[], datetime] = _utc_now,
        andre_approval_key: Optional[str] = None,
        platform_knowledge: Optional[dict] = None,
        platform_writer=None,
        log: Optional[RecordLog] = None,
        dir_lock=None,
        lock_token: Optional[str] = None,
        sleep: Callable[[float], None] = _time_mod.sleep,
        ledger_inflight_grace_s: float = LEDGER_INFLIGHT_GRACE_S,
    ):
        self.config = config
        self.ledger = ledger
        self.rr = revenue_recovery
        self.depts = departments or Departments()
        self.probe = probe or NotWiredPlatformProbe()
        self.vault = vault or RefusingVault()
        self.bus = bus or InProcessEventBus()
        self.clock = clock
        self.knowledge = platform_knowledge or i04.PLATFORM_KNOWLEDGE
        self.writer = platform_writer or NotWiredPlatformWriter()
        self.playbook = Playbook(approval_key=andre_approval_key)
        self.memory = ClientMemoryStore()
        self.institutional = InstitutionalMemory()
        self.clients: dict[str, ClientRecord] = {}
        self.creators: dict[str, CreatorRecord] = {}
        self.escalations: dict[str, Escalation] = {}
        self.campaigns: dict[tuple[str, str], dict] = {}
        self._andre_key = andre_approval_key
        self._lock = threading.RLock()
        # Deterministic-id state (ADR 0004 "Event ids"): an epoch, a
        # per-subject operation sequence that advances only when an operation
        # completes, and per-operation occurrence counters.
        #
        # Fix wave 4 (restart): the operations that CREATE a subject
        # (start_client, apply_creator) use the configured, stable instance id
        # as their epoch, so a restarted process's retry of them derives the
        # same ids and dedupes at the ledger. Every other operation also
        # carries a per-process boot id: state is in-process, so after a
        # restart a subject's history starts again at sequence 0 and, with a
        # stable epoch alone, a NEW event could take the id of a pre-restart
        # event (it would be silently deduped, or refused as a 409). Only the
        # creating operations can be retried across a restart anyway — every
        # other one needs state the restart lost (404).
        self._instance = config.instance_id
        self._boot = uuid.uuid4().hex[:16]
        self._epoch = f"{self._instance}:{self._boot}"
        self._seq: dict[str, int] = {}
        self._depth = 0
        self._op_name = ""
        self._effects: list[str] = []
        self._deferred: list[Callable[[], None]] = []
        self._deferred_fx: list[Callable[[], None]] = []
        self._occ: dict[tuple, int] = {}
        self._idc: dict[tuple, int] = {}
        self._touched: set[str] = set()
        # Fix wave 3 (N2/N7): result records of an outside effect that could
        # not be written yet, per subject, keyed by their deterministic event
        # id. The next operation on that subject writes them first (same id:
        # idempotent at the ledger).
        self._pending: dict[str, dict[str, tuple]] = {}
        self._op_pending: list[tuple[str, str]] = []
        self._esc_plans: dict[str, "OnboardingService._EscalationPlan"] = {}
        # Bug sweep D (D-1): the local anchored evidence log. Every ledger event an operation writes is collected
        # (``_op_written``) and named by ONE anchored line when the operation's state is applied; an event no anchored
        # line names is ``attempted`` (GET /onboarding/audit/evidence).
        self._op_written: list[dict] = []
        self._owed_deferred = []
        self._closed = False
        self._sleep, self._grace = sleep, float(ledger_inflight_grace_s)
        # Wave F (AEGIS F-2): subjects held because the ledger holds evidence of theirs that no local line names (the
        # process stopped between a ledger record and its local line): every action on them is refused until Andre
        # resolves it (``resolve_quarantine``). Replayed like any other state.
        self._quarantine: dict[str, dict] = {}
        self._named_events: set = set()
        self._ledger_hw = 0
        self._dir_lock = dir_lock
        self._lock_token: Optional[str] = None
        if self._dir_lock is not None:
            # E-5/F-3 (finance-py's single writer): one service instance per data directory, also within a process
            adopted = self._dir_lock.adopt(lock_token if lock_token is not None else self._dir_lock.claim())
            if adopted is None:
                self._dir_lock = None
                raise DataDirBusy("the data-directory claim handed to this service instance is not held (released, "
                                  "stale, another instance's, or already adopted); refusing to start")
            self._lock_token = adopted
        try:
            self.log = log if log is not None else RecordLog(None)
            self.journal = EvidenceJournal(
                self.log, DEPARTMENT,
                lambda eid, et, actor, sid, payload, summary: self.ledger.record_event(eid, DEPARTMENT, et, actor, sid,
                                                                                        payload, summary),
                payload_hash=lambda p: payload_sha256(scrub_obj(p)), id_prefix="onb-anc-")
            # 1099 totals and payment idempotency survive a restart: they are replayed from the log (bug sweep D, M)
            self._paid: dict[tuple[str, int], Decimal] = {}
            self._payment_rids: dict[str, dict] = {}       # payment id (request_id) -> the payment (AEGIS O-3)
            # Wave F (M-1): every consequential entity is rebuilt from the log's state deltas, in log order
            self._state = self._make_state_tracker()
            self._replay_state()
            self._startup_ledger_checks()
            self._replay_payments()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Give the data directory back. A closed instance refuses every operation and its log every write."""
        with self._lock:
            self._closed = True
            log_ = getattr(self, "log", None)
            if log_ is not None:
                with log_.lock:
                    log_.closed = True
            if self._dir_lock is not None:
                self._dir_lock.release_claim(self._lock_token)
                self._dir_lock = None
                self._lock_token = None

    @property
    def closed(self) -> bool:
        return self._closed

    # Wave F (M-1): the line kinds and data keys this build writes; replay refuses anything else (fail closed)
    _LINE_KINDS = frozenset({"op", "partial", "payment_intent", "payment_intent_resolved", ATTEMPT_KIND, "quarantine"})
    _LINE_KEYS = frozenset({"op", "effects", "partial", "rk", "evidence", "state", "payment", "payment_intent",
                            "payment_intents_closed", "outcome", "quarantined", "ledger_seq_scanned"})

    def _make_state_tracker(self) -> StateTracker:
        """The consequential state, as the log carries it (ADR 0004 "Wave F fixes"): clients, creators, escalations,
        campaigns, escalation plans (briefing retries), owed result records, the playbook's history and the
        institutional patterns. Not carried: per-client memory walls (P11/P4 deletion; the log is append-only, see the
        ADR), the per-process event-id counters (``_seq``, scoped by the boot id) and the 1099 totals / payment ids
        (replayed from their own payment lines, ``_replay_payments``)."""
        import importlib
        import pkgutil
        import sys

        import intelligences
        import memory as memory_mod
        import onboarding_schema
        mods = [sys.modules[__name__], onboarding_schema, rq, memory_mod]
        mods += [importlib.import_module(f"intelligences.{m.name}") for m in pkgutil.iter_modules(intelligences.__path__)]
        st = StateTracker(Codec(mods))
        st.add_keyed("clients", self, "clients")
        st.add_keyed("creators", self, "creators")
        st.add_keyed("escalations", self, "escalations")
        st.add_keyed("campaigns", self, "campaigns")
        st.add_keyed("escalation_plans", self, "_esc_plans")
        st.add_keyed("owed_results", self, "_pending")
        st.add_keyed("quarantine", self, "_quarantine")
        st.add_append_only("playbook", lambda: self.playbook.history)
        st.add_append_only("institutional", lambda: self.institutional._patterns)
        return st

    def _replay_state(self) -> None:
        """Rebuild every consequential entity from the log, in order (Wave F, M-1). Deterministic and idempotent (the
        same lines always give the same state), and fail closed: a line of an unknown kind, with a data key this
        build does not write, or whose state cannot be decoded faithfully refuses start-up with its line number.
        Lines written before Wave F carry no state: they are evidence only (nothing to rebuild from them)."""
        for r in self.log.iter_records():
            where = f"local log line {r.get('seq')} ({r.get('kind')!r})"
            d = r.get("data")
            if r.get("kind") not in self._LINE_KINDS or not isinstance(d, dict):
                raise StateReplayError(f"refusing to start: {where}: not a line kind this build writes; the log "
                                       "cannot be interpreted (inspect it; nothing was skipped)")
            unknown = sorted(set(d) - self._LINE_KEYS)
            if unknown:
                raise StateReplayError(f"refusing to start: {where}: unknown field(s) {unknown}; the log cannot be "
                                       "interpreted (inspect it; nothing was skipped)")
            if "state" in d:
                self._state.apply(d["state"], where)
            self._named_events.update(n.get("event_id") for n in d.get("evidence") or [])
            if d.get("payment_intent"):
                self._named_events.add(d["payment_intent"].get("event_id"))
            if isinstance(d.get("ledger_seq_scanned"), int):
                self._ledger_hw = max(self._ledger_hw, d["ledger_seq_scanned"])
        try:
            self._state.baseline()
        except StateCodecError as exc:
            raise StateReplayError(f"refusing to start: the replayed state cannot be re-encoded ({exc})") from None

    def _startup_ledger_checks(self) -> None:
        """Wave F (AEGIS F-2, F-3), for a log on disk with a configured ledger:

        * the last line's anchor is re-sent (it may not have landed before the process stopped);
        * if the last line is younger than the in-flight grace (a write of the stopped process may still be on its way
          to the ledger), start-up waits out the rest of it once (``sleep``; bounded by the grace) before reading;
        * every event of this department on the ledger after the last scanned seq that NO local line names (the
          process stopped between a ledger record and its local line) puts its subject in quarantine: every action
          on it is refused until Andre resolves it. The scan reads only this department, from the last scanned seq,
          through the paged filtered read; a ledger that cannot be read refuses start-up (an unapplied revocation
          could otherwise go unnoticed)."""
        if self.log.in_memory or isinstance(self.ledger, UnconfiguredLedgerClient) \
                or not hasattr(self.ledger, "entries_filtered"):
            return
        self.journal.reanchor_tail()
        raw = self.log.raw_lines()
        if raw:
            try:
                age = (self.now() - datetime.fromisoformat(json.loads(raw[-1])["at"])).total_seconds()
            except (KeyError, ValueError, TypeError):
                age = 0.0
            if 0 <= age < self._grace:
                self._sleep(self._grace - age)
        try:
            events = self.ledger.entries_filtered(DEPARTMENT, None, after_seq=self._ledger_hw or None)
        except LedgerQueryFailed as exc:
            raise RuntimeError(f"refusing to start: the evidence ledger cannot be read to check for evidence this log "
                               f"never applied ({exc}); a revocation or onboarding lost in a crash would go "
                               "unnoticed") from None
        top = max([self._ledger_hw] + [e["seq"] for e in events if isinstance(e.get("seq"), int)])
        orphans = [e for e in events if e.get("event_type") != ANCHOR_TYPE and e.get("event_id") not in self._named_events]
        held = sorted({e.get("subject_id") for e in orphans if isinstance(e.get("subject_id"), str)})
        for sid in held:
            prev = self._quarantine.get(sid) or {"event_ids": [], "event_types": []}
            mine = [e for e in orphans if e.get("subject_id") == sid]
            self._quarantine[sid] = {
                "event_ids": sorted(set(prev["event_ids"]) | {e["event_id"] for e in mine}),
                "event_types": sorted(set(prev["event_types"]) | {e.get("event_type") for e in mine})}
        if held or len(events) >= SCAN_CHECKPOINT_EVENTS:
            delta, mark = self._state.delta()
            extra: dict = {"quarantined": held, "ledger_seq_scanned": top}
            if delta is not None:
                extra["state"] = delta
            try:
                self.journal.commit("quarantine", self.now().isoformat(), "ledger_scan", [], extra,
                                    owe_on_failure=True)
            except (LedgerWriteError, StoreWriteError) as exc:
                log.warning("ledger scan line owed (%s); written before the next action", type(exc).__name__)
            mark()
            if held:
                log.error("quarantined %d subject(s): the ledger holds evidence no local line names", len(held))
        self._ledger_hw = top

    def _refuse_if_quarantined(self, subject_id: str) -> None:
        q = self._quarantine.get(subject_id)
        if q is not None:
            raise Conflict(f"{subject_id} is held: the ledger holds evidence of it this service never applied "
                           f"({', '.join(q['event_types'])}); nothing is done until Andre resolves it "
                           "(POST /onboarding/quarantine/{subject_id}/resolve)",
                           {"quarantined": True, "event_types": list(q["event_types"])})

    def list_quarantine(self) -> dict:
        with self._lock:
            return {"quarantined": [{"subject_id": k, **v} for k, v in self._quarantine.items()]}

    def resolve_quarantine(self, subject_id: str, req: rq.QuarantineResolveRequest) -> dict:
        """Andre's release of a held subject (he has reconciled it with the ledger); recorded, then lifted."""
        with self._op("resolve_quarantine"):
            q = self._quarantine.get(subject_id)
            if q is None:
                raise NotFound("subject is not held")
            try:
                verify_andre_token(self._andre_key,
                                   lambda key: andre_action_token(key, "quarantine_resolve", subject_id),
                                   req.approval_token)
            except AndreApprovalError as exc:
                raise Refused(str(exc)) from None
            self._record("quarantine_resolved", "andre", subject_id, {"event_ids": list(q["event_ids"])},
                         "Andre resolved evidence this service had never applied")
            del self._quarantine[subject_id]
            return {"subject_id": subject_id, "released": True}

    def state_snapshot(self) -> bytes:
        """Every consequential entity, canonically encoded (a restarted service must match the live one exactly)."""
        with self._lock:
            return self._state.snapshot()

    def _apply_payment(self, pay: dict) -> None:
        if pay["request_id"] in self._payment_rids:      # one payment id counts once, whatever replays it
            return
        k = (pay["person_key"], int(pay["year"]))
        self._paid[k] = self._paid.get(k, Decimal("0.00")) + Decimal(pay["amount_usd"])
        self._payment_rids[pay["request_id"]] = pay

    def _replay_payments(self) -> None:
        """AEGIS O-2: the 1099 totals match the LEDGER. Every payment writes a ``payment_intent`` line (anchored, with
        the ``creator_payment_tracked`` event id it is about to record) BEFORE its ledger record, and the operation's
        line names the payment after. At start, committed payments are applied; an intent no committed line names
        (the operation's line was owed when the process stopped, or its reply was lost) is applied exactly when the
        ledger holds its event. Deduplicated by payment id. Unresolved intents with an unreadable ledger refuse
        start-up: a total that may be short is never served."""
        # AEGIS O-2b: EVERY intent per payment id (each attempt -- another boot, another day -- has its own event id);
        # the payment counts once if ANY of them is on the ledger. Intents are CLOSED once resolved (a line naming
        # their event ids), so the whole-ledger read below happens only while some intent is open.
        intents: dict[str, list[dict]] = {}
        closed: set[str] = set()
        for r in self.log.iter_records():
            d = r.get("data") or {}
            for pk in (d.get("payment") or {}, (d.get("payment_intent") or {}).get("payment") or {}):
                if pk.get("person_key", "").startswith("pk-"):
                    raise RuntimeError(f"refusing to start: local log line {r['seq']} holds a first-cut 1099 person "
                                       "key (pk-), which the current key (pk2-, AEGIS O-1) cannot be matched with; "
                                       "this build never wrote one -- inspect the log before starting")
            if d.get("payment"):
                self._apply_payment(d["payment"])
            if d.get("payment_intent"):
                intents.setdefault(d["payment_intent"]["request_id"], []).append(d["payment_intent"])
            if d.get("payment_intents_closed"):
                closed.update(d["payment_intents_closed"])
        open_ = {rid: [i for i in its if i["event_id"] not in closed] for rid, its in intents.items()
                 if rid not in self._payment_rids}
        open_ = {rid: its for rid, its in open_.items() if its}
        if not open_:
            return
        want = {i["event_id"] for its in open_.values() for i in its}
        try:
            # Wave F: only this department's payment events, through the ledger's paged filtered read, stopping once
            # every open intent's event has been seen (the cost is bounded by where they are, not by the ledger)
            if hasattr(self.ledger, "entries_filtered"):
                found = self.ledger.entries_filtered(DEPARTMENT, "creator_payment_tracked", want=want)
            else:
                found = self.ledger.entries()  # type: ignore[attr-defined]
            held = {e.get("event_id") for e in found
                    if isinstance(e, dict) and e.get("event_type") == "creator_payment_tracked"}
        except (LedgerQueryFailed, AttributeError) as exc:
            raise RuntimeError(f"refusing to start: {len(open_)} creator payment(s) may be on the ledger without a "
                               f"committed local line, and the ledger cannot be read ({exc}); the 1099 totals would "
                               "be short") from None
        for rid, its in sorted(open_.items()):
            hit = next((i for i in its if i["event_id"] in held), None)
            ids = [i["event_id"] for i in its]
            extra: dict = {"payment_intents_closed": ids}
            if hit is not None:
                self._apply_payment(hit["payment"])
                extra["payment"] = hit["payment"]           # applied: replayed as a committed payment from now on
            extra["outcome"] = "applied" if hit is not None else "not_on_ledger"
            try:
                # resolved at THIS start (the process that wrote the intents is gone: nothing of it is in flight)
                self.journal.commit("payment_intent_resolved", self.now().isoformat(), f"payment|{rid}", [], extra)
            except (LedgerWriteError, StoreWriteError) as exc:
                log.warning("payment intent %s left open (%s); resolved again at the next start", rid,
                            type(exc).__name__)

    # --- infrastructure --------------------------------------------------------

    def now(self) -> datetime:
        n = self.clock()
        if n.tzinfo is None:
            raise RuntimeError("service clock must be timezone-aware")
        return n

    def business_date(self):
        """Today on the company's calendar (America/Los_Angeles), from the server clock."""
        return self.now().astimezone(BUSINESS_TZ).date()

    def age_evaluation_date(self):
        """The date clipper age is computed on: the server's date at UTC-12."""
        return self.now().astimezone(AGE_EVALUATION_TZ).date()

    def latest_calendar_date(self):
        """The server's date in the latest time zone on Earth (UTC+14): no
        real event can be dated after it."""
        return self.now().astimezone(LATEST_TZ).date()

    def _not_in_future(self, when: Optional[datetime], what: str) -> None:
        if when is not None and when > self.now():
            raise Invalid(f"{what} is in the future by the server's clock; refused (no request can move the clock)")

    @contextmanager
    def _op(self, name: str, *, creates_subject: bool = False):
        """One operation: serialized, with its own effect list, deferred
        in-process effects and occurrence counters. On completion (a result,
        or a recorded refusal) deferred effects run and the sequence of
        every subject it recorded against advances; on a ledger failure
        neither happens, so a retry replays the same event ids."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                if self._closed:
                    raise ServiceClosed("this onboarding instance is closed; nothing was done")
                self._flush_owed_lines()
                self._op_written = []
                self._op_name, self._effects, self._deferred, self._deferred_fx = name, [], [], []
                self._epoch = self._instance if creates_subject else f"{self._instance}:{self._boot}"
                self._occ, self._idc, self._touched = {}, {}, set()
                self._op_pending = []
            self._depth += 1
            completed = refused = False
            try:
                yield
                completed = True
            except OnboardingError:
                completed = refused = True
                raise
            except LedgerWriteAfterEffects:
                if outer:
                    # outside effects happened: the events written so far (their authorizing records) are named
                    # now; the owed result records are named by the operation that writes them
                    self._commit_line(name, partial=True)
                raise  # pending result records stay: the next operation writes them
            except LedgerWriteError:
                if outer:
                    # Nothing outside happened and no state was committed:
                    # the result records this operation queued are not owed.
                    for sid, eid in self._op_pending:
                        self._pending.get(sid, {}).pop(eid, None)
                    self._attempt_line(name)
                raise
            finally:
                self._depth -= 1
                owed = None
                if outer and completed:
                    # Wave F (M-1): the operation's deferred STATE changes are applied first, so its line carries the
                    # state it really applied (replayed at start); bus publishes still wait for the line
                    for fn in self._deferred:
                        fn()
                    self._deferred = []
                    owed = self._commit_line(name, refused=refused)
                    if owed is not None:
                        # in-process effects (bus publishes) wait for the owed line: nothing goes out after a failed
                        # record; the flush that writes the line runs them
                        self._owed_deferred.extend(self._deferred_fx)
                        self._deferred_fx = []
                    for fn in self._deferred_fx:
                        fn()
                    for sid in self._touched:
                        self._seq[sid] = self._seq.get(sid, 0) + 1
                if outer:
                    self._deferred = []
                    self._deferred_fx = []
                    self._op_written = []
                    self._op_extra = None
                if owed is not None:
                    raise owed

    _op_extra: Optional[dict] = None
    _owed_deferred: list = []

    def _flush_owed_lines(self) -> None:
        """An owed evidence line is written before anything else happens (refused, 503, while it cannot be)."""
        if not self.journal.owed:
            return
        try:
            self.journal.flush()
        except LedgerWriteError as exc:
            raise LedgerWriteError(f"an owed evidence line could not be written ({exc}); nothing was done",
                                   exc.outcome) from None
        except StoreWriteError as exc:
            raise LedgerWriteError(f"an owed evidence line could not be written to the local log ({exc}); nothing "
                                   "was done") from None
        pending, self._owed_deferred = list(self._owed_deferred), []
        for fn in pending:
            fn()

    def _commit_line(self, name: str, partial: bool = False, refused: bool = False) -> Optional[EvidenceLineOwed]:
        """Name every event this operation wrote in ONE anchored local line (bug sweep D, D-1). Returns the error to
        raise when it could not be written (the line is then owed), None otherwise."""
        if not self._op_written and refused:
            # a refusal that recorded nothing changed nothing: what it read stays marked (compared at the next line),
            # so a flood of cheap refusals never pays for re-encoding the entities it looked at
            return None
        state, mark_held = self._state.delta()
        if not self._op_written and state is None:
            return None
        extra = {"op": name, "effects": list(self._effects)}
        if partial:
            extra["partial"] = True
        if self._op_extra:
            extra.update(self._op_extra)
        if state is not None:
            extra["state"] = state
        try:
            self.journal.commit("partial" if partial else "op", self.now().isoformat(), name, self._op_written,
                                extra, owe_on_failure=True)
        except (LedgerWriteError, StoreWriteError) as exc:
            mark_held()       # owed: the identical line (with this state) is written before anything else
            outcome = getattr(exc, "outcome", "not_recorded")
            log.error("evidence line owed after %s: %s", name, type(exc).__name__)
            return EvidenceLineOwed(f"the action took effect; its local evidence line could not be written yet "
                                    f"({type(exc).__name__})", list(self._effects), outcome,
                                    "do not repeat this action: it took effect; its evidence line is written before "
                                    "the next action")
        mark_held()
        return None

    def _attempt_line(self, name: str) -> None:
        """Wave F (AEGIS F-2): an operation that failed before taking effect names the events it left on the ledger in
        an ``attempt`` line (no state; never ``committed``), so a restart does not mistake them for evidence of an
        action this log lost. Best effort: the operation's own error is what the caller gets."""
        if not self._op_written:
            return
        try:
            self.journal.commit(ATTEMPT_KIND, self.now().isoformat(), name, self._op_written,
                                {"op": name, "effects": []}, owe_on_failure=True)
        except (LedgerWriteError, StoreWriteError) as exc:
            log.warning("attempt line for %s owed (%s)", name, type(exc).__name__)

    def _event_id(self, event_type: str, subject_id: str, payload: dict) -> str:
        ph = payload_sha256(scrub_obj(payload))
        key = (subject_id, event_type, ph)
        occ = self._occ.get(key, 0)
        self._occ[key] = occ + 1
        self._touched.add(subject_id)
        return derive_event_id(f"{self._epoch}:{self._op_name}", DEPARTMENT, event_type, subject_id,
                               self._seq.get(subject_id, 0), occ, ph)

    def _write(self, eid: str, event_type: str, actor: str, subject_id: str, payload: dict, summary: str) -> None:
        try:
            self.ledger.record_event(eid, DEPARTMENT, event_type, actor, subject_id, payload, summary)
            body = build_body(eid, DEPARTMENT, event_type, actor, subject_id, payload, summary)
            self._op_written.append({"event_id": eid, "event_type": event_type, "subject_id": subject_id,
                                     "payload_sha256": body["payload_sha256"]})
        except LedgerWriteAfterEffects:
            raise
        except LedgerWriteError as exc:
            if self._effects:
                raise LedgerWriteAfterEffects(str(exc), self._effects, exc.outcome, exc.retry_hint) from None
            raise

    def _record(self, event_type: str, actor: str, subject_id: str, payload: dict, summary: str) -> str:
        """Write one ledger event with a deterministic id. Raises
        LedgerWriteError on failure — nothing further happens. If outside
        effects of this operation already happened, the error says which
        (LedgerWriteAfterEffects) so the API can report them honestly."""
        eid = self._event_id(event_type, subject_id, payload)
        self._write(eid, event_type, actor, subject_id, payload, summary)
        return eid

    def _record_result(self, event_type: str, actor: str, subject_id: str, payload: dict, summary: str) -> str:
        """The record of an outside effect's RESULT (fix wave 3, N2/N7). It
        is queued with its deterministic id before the write; if the write
        fails after an effect happened, the next operation on the subject
        (a retry, or any other) writes it first with the SAME id — so a
        missing result record is never skipped by an "already done" path
        and never written twice."""
        return self._record_results([(event_type, actor, subject_id, payload, summary)])[0]

    def _record_results(self, records: list[tuple[str, str, str, dict, str]]) -> list[str]:
        """Several result records of one outside effect: ALL are queued as
        owed before the first is written (fix wave 4, A1), so a failure on
        the first still leaves the later ones owed, in order."""
        queued = []
        for event_type, actor, subject_id, payload, summary in records:
            eid = self._event_id(event_type, subject_id, payload)
            self._pending.setdefault(subject_id, {})[eid] = (event_type, actor, payload, summary)
            self._op_pending.append((subject_id, eid))
            queued.append((eid, event_type, actor, subject_id, payload, summary))
        for eid, event_type, actor, subject_id, payload, summary in queued:
            self._write(eid, event_type, actor, subject_id, payload, summary)
            self._pending[subject_id].pop(eid, None)
        return [q[0] for q in queued]

    def _flush_pending(self, subject_id: str) -> None:
        """Write the result records a previous operation owes (same ids)."""
        pend = self._pending.get(subject_id)
        for eid, (event_type, actor, payload, summary) in list((pend or {}).items()):
            self._write(eid, event_type, actor, subject_id, payload, summary)
            pend.pop(eid, None)

    def _effect(self, name: str, fn: Callable[[], Any], done: Optional[Callable[[Any], bool]] = None) -> Any:
        """Make one outside effect. Only call after the record that
        authorizes it was written. Counted as done when ``fn`` returns (and
        ``done(result)`` holds, e.g. a push that was actually delivered)."""
        result = fn()
        if done is None or done(result):
            self._effects.append(name)
        return result

    def _push_andre(self, key: str, payload: dict) -> tuple[bool, str]:
        """Push to Andre's phone. A channel that raises is a push that was
        NOT delivered (fix wave 2): it is recorded and handled like any other
        undelivered push, never a 500 that skips the rest of the operation
        (e.g. the client warning in the same tick)."""
        try:
            delivered, detail = self.depts.notifier.push(key, payload)
        except Exception as exc:  # noqa: BLE001  (any channel failure = not delivered)
            return False, f"push channel raised {type(exc).__name__}; not delivered"
        return bool(delivered), str(detail)

    def _defer(self, fn: Callable[[], None]) -> None:
        """In-process effects (bus publish, client memory) run only when the
        operation completes — never before a record that might still fail."""
        if self._depth == 0:
            fn()
        else:
            self._deferred.append(fn)

    def _mem_put(self, client_id: str, key: str, value: Any) -> None:
        self._defer(lambda: self.memory.put(client_id, key, value))

    def _derive_id(self, prefix: str, subject_id: str, width: int = 16) -> str:
        """Ids that appear in ledger payloads are deterministic too, so a
        retried operation's payloads (and therefore event ids) are identical."""
        k = (prefix, subject_id)
        n = self._idc.get(k, 0)
        self._idc[k] = n + 1
        material = json.dumps([self._epoch, self._op_name, prefix, subject_id, self._seq.get(subject_id, 0), n])
        return f"{prefix}-{hashlib.sha256(material.encode()).hexdigest()[:width]}"

    def _client(self, client_id: str) -> ClientRecord:
        self._refuse_if_quarantined(client_id)
        rec = self.clients.get(client_id)
        if rec is None:
            raise NotFound("unknown client")
        if rec.exited:
            raise Conflict("client has exited (P4); no further onboarding actions")
        if self._depth > 0:
            self._flush_pending(client_id)
        return rec

    def _creator(self, creator_id: str) -> CreatorRecord:
        self._refuse_if_quarantined(creator_id)
        rec = self.creators.get(creator_id)
        if rec is None:
            raise NotFound("unknown creator")
        if self._depth > 0:
            self._flush_pending(creator_id)
        return rec

    def _flag_injection(self, subject_id: str, text: str, source: str) -> list[dict]:
        """Client content is DATA. Flags are logged and recorded, and
        returned for the operator — no decision function reads them."""
        return self._record_injection(subject_id, source, scan_for_injection(text, source))

    def _record_injection(self, subject_id: str, source: str, flags: list) -> list[dict]:
        """The recording half of ``_flag_injection`` (fix wave 7): the scan
        itself is pure and ``add_facts`` runs it before taking the lock."""
        if flags:
            payload = {"source": source, "rules": sorted({f.rule for f in flags})}
            self._record("injection_flagged", "guardrail_content_is_data", subject_id, payload,
                         f"Client content from {source} flagged as possible prompt injection; ignored")
            self._publish("injection_flagged", subject_id, payload)
        return [{"rule": f.rule, "source": f.source} for f in flags]

    def _publish(self, event_type: str, subject_id: str, payload: dict) -> None:
        ev = DomainEvent(event_type=event_type, subject_id=subject_id, payload=payload, at=self.now())
        if self._depth == 0:
            self.bus.publish(ev)
        else:
            self._deferred_fx.append(lambda: self.bus.publish(ev))

    def _count_flags(self, rec: ClientRecord, flags: list) -> None:
        """The injection counter changes only when the operation completes
        (its records are all written) — fix wave 3, N2 sweep."""
        n = len(flags)
        if n:
            self._defer(lambda: setattr(rec, "injection_flags", rec.injection_flags + n))

    # --- client lane: start -------------------------------------------------------

    def start_client(self, req: rq.StartClientRequest) -> dict:
        """Stage then commit (fix wave 3, N2): the client record and its
        escalation become visible only after every record they depend on is
        written. If a result record fails AFTER an outside effect (contract
        stored, briefing delivered), the state that did happen is committed,
        the API says ``proceeded: true, completed: false``, and a retry of
        the same start writes the missing records (same event ids) and
        finishes the start instead of answering 409."""
        with self._op("start_client", creates_subject=True):
            self._refuse_if_quarantined(req.client_id)
            digest = hashlib.sha256(req.model_dump_json().encode()).hexdigest()
            existing = self.clients.get(req.client_id)
            if existing is not None:
                if existing.start_view is not None:
                    return self._complete_start(existing, digest)
                raise Conflict("client already onboarding")
            try:
                ZoneInfo(req.time_zone)
            except Exception as exc:  # noqa: BLE001
                raise Invalid("time_zone is not a valid IANA time zone") from exc
            if req.contract is not None and req.contract.client_id != req.client_id:
                raise Invalid("contract.client_id does not match client_id")
            if req.contract is not None:
                self._not_in_future(req.contract.signed_at, "contract.signed_at")
            now = self.now()
            rec = ClientRecord(
                client_id=req.client_id, lane=Lane(req.lane), business_name=req.business_name,
                signer=req.signer, login_holder=req.login_holder, time_zone=req.time_zone,
                quiet_start=_hhmm(req.quiet_hours_start, self.config.default_quiet_start),
                quiet_end=_hhmm(req.quiet_hours_end, self.config.default_quiet_end),
                preferred_channel=req.preferred_channel.value if req.preferred_channel else None,
                deal_size=req.deal_size_usd, started_at=now, last_progress_at=now,
            )
            first_name = req.signer.name.split()[0]
            first_message = ai_disclosure_first_message(first_name)
            holder = req.login_holder or req.signer
            deal_escalate, deal_reason = self._deal_size_ruling(req.deal_size_usd)
            link_id = self._derive_id("acl", rec.client_id)

            # 1. Every decision is recorded first.
            self._record("onboarding_started", "onboarding_service", rec.client_id,
                         {"lane": rec.lane.value, "deal_size_usd": money_str(rec.deal_size) if rec.deal_size else None},
                         f"Onboarding started ({rec.lane.value} lane)")
            self._record("first_message_sent", "intel_02_conversation", rec.client_id,
                         {"ai_disclosure": True, "human_offered": True, "wording_status": "pending counsel"},
                         "P1 first message: AI identity disclosed, human offered")
            self._record("access_link_routed", "intel_04_platform_access", rec.client_id,
                         {"routed_to_login_holder": req.login_holder is not None, "link_id": link_id},
                         "P22 secure access link routed to the login holder")
            self._record("deal_size_ruling", "intel_10_escalation_briefing", rec.client_id,
                         {"escalate": deal_escalate, "threshold_set": self.config.deal_size_threshold_usd is not None},
                         deal_reason)
            if req.contract is not None:
                self._record("contract_storage_request", "intel_14_contract", rec.client_id,
                             {"services": req.contract.services, "signed": req.contract.signed},
                             "Store signed contract terms in contract storage")
            plan = None
            if deal_escalate:
                plan = self._prepare_escalation(rec, TriggerKind.DEAL_SIZE, deal_reason, attempted=None, client_live=True,
                                                eid=self._derive_id("esc", rec.client_id))

            # 2. Outside effects, each authorized by a record above, each
            #    followed by the record of its result. State is staged on
            #    ``rec``/``esc`` and committed only at the end.
            contract_status = "none supplied"
            rec.p1_disclosure_sent = True
            esc = self._build_escalation(rec, plan) if plan is not None else None
            view = {
                "client_id": rec.client_id,
                "lane": rec.lane.value,
                "first_message": first_message,
                "first_message_wording_status": "pending counsel review (Cal. B&P Code 17941)",
                "access_link": {
                    "link_id": link_id,
                    "send_to": holder.email,
                    "send_to_is_signer": req.login_holder is None,
                    "delivered": False,
                    "delivery_detail": "no delivery channel is wired (intake channel is an open item); the link id is issued, not sent",
                },
                "contract_storage": contract_status,
                "deal_size_ruling": {"escalate": deal_escalate, "reason": deal_reason},
                "escalation_id": esc.escalation_id if esc is not None else None,
            }
            try:
                if req.contract is not None:
                    try:
                        self._effect("contract_storage_put", lambda: self.depts.contracts.put(req.contract))
                        contract_status = "stored"
                    except Exception as exc:  # noqa: BLE001  (stand-in refuses)
                        contract_status = f"not stored: {exc}"
                    view["contract_storage"] = contract_status
                    self._record_result("contract_storage_ruling", "intel_14_contract", rec.client_id,
                                        {"stored": contract_status == "stored"}, f"Contract storage: {contract_status}"[:280])
                if esc is not None:
                    self._deliver_escalation(rec, esc, plan)
            except LedgerWriteAfterEffects:
                rec.start_digest, rec.start_view = digest, view
                self._commit_start(rec, esc, plan)
                raise
            self._commit_start(rec, esc, plan)
            self._mem_put(rec.client_id, "business_name", rec.business_name)
            return self._start_response(view)

    def _commit_start(self, rec: ClientRecord, esc: Optional[Escalation], plan) -> None:
        self.clients[rec.client_id] = rec
        if esc is not None:
            self._attach_escalation(rec, esc, plan)

    def _start_response(self, view: dict) -> dict:
        out = {k: v for k, v in view.items() if k != "escalation_id"}
        eid = view.get("escalation_id")
        out["escalation"] = _dump(self.escalations.get(eid)) if eid else None
        return out

    def _complete_start(self, rec: ClientRecord, digest: str) -> dict:
        """Retry of a start that stopped after an outside effect: write the
        owed result records (same ids), deliver a briefing that was never
        attempted or make the commitment whose record failed — never a
        second push for a briefing already attempted."""
        self._flush_pending(rec.client_id)
        eid = rec.start_view.get("escalation_id")
        if eid:
            esc = self.escalations[eid]
            if esc.push_attempts == 0:
                self._deliver_escalation(rec, esc, self._esc_plans[eid])
            else:
                self._complete_escalation(rec, esc)
        view, same = rec.start_view, digest == rec.start_digest
        rec.start_digest = rec.start_view = None
        self._mem_put(rec.client_id, "business_name", rec.business_name)
        if not same:
            raise Conflict("client already onboarding (the earlier start's missing records are now written)")
        return self._start_response(view)

    def _deal_size_ruling(self, deal: Optional[Decimal]) -> tuple[bool, str]:
        threshold = self.config.deal_size_threshold_usd
        if threshold is None:
            return True, ("Deal-size threshold is not set by Andre; until it is, every deal escalates (fail closed)")
        if deal is None:
            return True, "Deal size unknown; escalating because it cannot be compared to the threshold"
        if deal > threshold:
            return True, f"Deal size {money_str(deal)} is above the {money_str(threshold)} threshold"
        return False, f"Deal size {money_str(deal)} is at or below the {money_str(threshold)} threshold"

    # --- escalation (hard/soft) + briefing + commitment ---------------------------

    @dataclass
    class _EscalationPlan:
        eid: str
        trigger: TriggerKind
        hard: bool
        snag: str
        attempted: Optional[str]
        briefing: Any
        commitment: Any
        commitment_id: str
        client_live: bool
        raised_at: datetime

    def _prepare_escalation(self, rec: ClientRecord, trigger: TriggerKind, snag: str, attempted: Optional[str],
                            client_live: bool, eid: Optional[str] = None) -> "OnboardingService._EscalationPlan":
        """Decide and RECORD an escalation (and the client commitment that
        will be made if, and only if, the briefing reaches Andre). No
        outside effect, no state change."""
        now = self.now()
        hard = trigger in HARD_TRIGGERS
        cfg = self.config
        briefing = i10.build_briefing(
            client_line=f"{rec.business_name} ({rec.lane.value} lane, client since {rec.started_at.date().isoformat()})",
            baseline=rec.baseline,
            client_priorities=rec.client_priorities,
            trigger=trigger,
            snag=snag,
            attempted=attempted,
        )
        c = i09.escalation_commitment(now, cfg.commitment_cutoff_local, cfg.commitment_tz, cfg.today_due_local, cfg.first_thing_local)
        eid = eid or self._derive_id("esc", rec.client_id)
        commitment_id = self._derive_id("cmt", rec.client_id)
        self._record("escalation_raised", "intel_10_escalation_briefing", rec.client_id,
                     {"escalation_id": eid, "trigger": trigger.value, "hard": hard, "snag": snag,
                      "attempted": attempted, "proposed_commitment": c.form, "proposed_due_at": c.due_at.isoformat(),
                      "commitment_id_if_delivered": commitment_id},
                     f"{'HARD' if hard else 'SOFT'} escalation: {trigger.value}")
        self._record("andre_push_request", "intel_10_escalation_briefing", rec.client_id,
                     {"escalation_id": eid, "briefing_fields": sorted(briefing.model_dump().keys())},
                     "Briefing pack pushed to Andre's phone before he engages")
        return self._EscalationPlan(eid, trigger, hard, snag, attempted, briefing, c, commitment_id, client_live, now)

    def _build_escalation(self, rec: ClientRecord, p: "OnboardingService._EscalationPlan") -> Escalation:
        """The escalation object, NOT yet in state (stage then commit, N2)."""
        return Escalation(
            escalation_id=p.eid, client_id=rec.client_id, trigger=p.trigger, hard=p.hard, reason=p.snag, snag=p.snag,
            attempted_resolution=p.attempted, raised_at=p.raised_at, briefing=p.briefing, push_delivered=False,
            push_detail="push not attempted yet", push_attempts=0, client_commitment_text=p.commitment.text,
            client_commitment_due_at=p.commitment.due_at,
            client_message_status="held: briefing not delivered to Andre; no commitment is made to the client until "
                                  "Andre acknowledges",
            commitment_id=None,
        )

    def _attach_escalation(self, rec: ClientRecord, esc: Escalation, p: "OnboardingService._EscalationPlan") -> None:
        if esc.escalation_id not in self.escalations:
            rec.escalation_ids.append(esc.escalation_id)
        self.escalations[esc.escalation_id] = esc
        self._esc_plans[esc.escalation_id] = p

    def _deliver_escalation(self, rec: ClientRecord, esc: Escalation, p: "OnboardingService._EscalationPlan",
                            attempt: int = 1) -> None:
        """The outside effect (push to Andre's phone), then the record of its
        result, then (only if delivered) the client commitment."""
        delivered, detail = self._effect("andre_push", lambda: self._push_andre(p.eid, p.briefing.model_dump(mode="json")),
                                         done=lambda r: bool(r[0]))
        esc.push_attempts, esc.push_delivered, esc.push_detail = attempt, delivered, detail
        if not delivered:
            esc.client_message_status = ("held: briefing not delivered to Andre (" + detail + "); no commitment is made to "
                                         "the client until Andre acknowledges")
            self._record_result("andre_push_not_delivered", "intel_10_escalation_briefing", rec.client_id,
                                {"escalation_id": p.eid, "delivered": False, "attempt": attempt,
                                 "max_attempts": self.config.escalation_push_max_attempts,
                                 "will_retry": attempt < self.config.escalation_push_max_attempts},
                                "Briefing NOT delivered to Andre; no client commitment made; retried on tick")
            return
        self._make_commitment(rec, esc, p)

    def _make_commitment(self, rec: ClientRecord, esc: Escalation, p: "OnboardingService._EscalationPlan",
                         defer: bool = False) -> str:
        """Returns the client message status ("released" / "held: ...")."""
        # Fix wave 2 (L3): the commitment exists in state only once its record
        # is written. If that record fails (after the push was delivered),
        # the operation stops with a 503, the client is never given the time,
        # and state says so: no commitment, message held (ADR 0004). A retry
        # of the operation (or the next tick) makes it then (fix wave 3).
        c = p.commitment
        esc.client_message_status = ("held: briefing delivered to Andre, but the commitment could not be recorded; "
                                     "the client has not been told a time")
        self._record("client_commitment_made", "intel_09_promise_keeper", rec.client_id,
                     {"commitment_id": p.commitment_id, "escalation_id": p.eid, "form": c.form, "due_at": c.due_at.isoformat(),
                      "push_delivered": True},
                     f"Client told: {c.text}")
        now = self.now()
        send_ok = p.client_live or not i09.in_quiet_hours(now, rec.time_zone, rec.quiet_start, rec.quiet_end)
        status = "released" if send_ok else "held: client quiet hours; send at the next allowed moment"

        def commit() -> None:
            esc.client_message_status = status
            esc.commitment_id = p.commitment_id
            esc.client_commitment_text, esc.client_commitment_due_at = c.text, c.due_at
            rec.commitments[p.commitment_id] = Commitment(
                commitment_id=p.commitment_id, client_id=rec.client_id, kind="escalation_callback", category=c.form,
                text=c.text, owner="andre", created_at=now, due_at=c.due_at,
            )

        # ``defer``: the time reaches the client only in the reply of an
        # operation that may still write records after this one (tick); the
        # commitment exists once that reply is certain (fix wave 3).
        if defer:
            self._defer(commit)
        else:
            commit()
        return status

    def _complete_escalation(self, rec: ClientRecord, esc: Escalation, client_live: Optional[bool] = None,
                             defer: bool = False) -> Optional[str]:
        """Finish an escalation left incomplete by a failed record: if the
        briefing reached Andre but the commitment was never recorded, make it
        now (a fresh time if the planned one has passed). Never pushes."""
        if esc.resolved_at is not None or not esc.push_delivered or esc.commitment_id is not None:
            return None
        p = self._esc_plans[esc.escalation_id]
        now = self.now()
        if client_live is not None:
            p.client_live = client_live
        if p.commitment.due_at <= now:
            cfg = self.config
            p.commitment = i09.escalation_commitment(now, cfg.commitment_cutoff_local, cfg.commitment_tz,
                                                     cfg.today_due_local, cfg.first_thing_local)
        return self._make_commitment(rec, esc, p, defer=defer)

    def _escalate(self, rec: ClientRecord, trigger: TriggerKind, snag: str, attempted: Optional[str], client_live: bool) -> Escalation:
        eid = self._derive_id("esc", rec.client_id)
        existing = self.escalations.get(eid)
        if existing is not None:
            # A retry of this same operation after it stopped part-way: the
            # escalation (and its push) already exist — complete, never push again.
            self._complete_escalation(rec, existing, client_live)
            return existing
        plan = self._prepare_escalation(rec, trigger, snag, attempted, client_live, eid=eid)
        esc = self._build_escalation(rec, plan)
        try:
            self._deliver_escalation(rec, esc, plan)
        except LedgerWriteAfterEffects:
            self._attach_escalation(rec, esc, plan)  # it happened: state says so
            raise
        self._attach_escalation(rec, esc, plan)
        return esc

    def _open_escalation(self, rec: ClientRecord, trigger: TriggerKind) -> Optional[Escalation]:
        for eid in rec.escalation_ids:
            e = self.escalations[eid]
            if e.trigger == trigger and e.resolved_at is None:
                return e
        return None

    def _soft_trigger(self, rec: ClientRecord, trigger: TriggerKind, snag: str, attempt_text: str) -> dict:
        """Soft trigger: ONE resolution attempt first. A second firing of the
        same trigger while the attempt is still open, once the
        soft-resolution window has passed, escalates."""
        now = self.now()
        open_issue = next((i for i in rec.soft_issues.values() if i.trigger == trigger and i.status == "attempted"), None)
        if open_issue is not None:
            if now - open_issue.attempted_at >= timedelta(hours=self.config.soft_resolution_window_hours):
                return self._escalate_issue(rec, open_issue, snag)
            return {"soft_issue": self._issue_view(open_issue), "escalation": None,
                    "detail": "one resolution attempt already made; waiting for it to work before escalating"}
        attempt = check_outbound(attempt_text)
        iid = self._derive_id("iss", rec.client_id)
        self._record("soft_trigger_attempt", "intel_02_conversation", rec.client_id,
                     {"issue_id": iid, "trigger": trigger.value, "snag": snag},
                     f"Soft trigger {trigger.value}: one resolution attempt before escalating")
        issue = SoftIssue(iid, trigger, snag, attempt, now)
        # The attempt message goes to the client in THIS response: the issue
        # ("one attempt made") exists only once the operation completes
        # (fix wave 3, N2 sweep) — a later failed record means it was not sent.
        self._defer(lambda: rec.soft_issues.__setitem__(iid, issue))
        if i09.in_quiet_hours(now, rec.time_zone, rec.quiet_start, rec.quiet_end):
            nxt = i09._next_allowed(now, now + timedelta(hours=24), rec.time_zone, rec.quiet_start, rec.quiet_end)
            status = f"held: client quiet hours; send at {nxt.isoformat() if nxt else 'next allowed moment'}"
        else:
            status = "released"
        return {"soft_issue": self._issue_view(issue), "escalation": None, "client_message": attempt,
                "client_message_status": status}

    def _escalate_issue(self, rec: ClientRecord, issue: SoftIssue, snag: Optional[str] = None) -> dict:
        esc = self._escalate(rec, issue.trigger, snag or issue.snag, attempted=issue.attempt, client_live=False)
        issue.status = "escalated"
        return {"soft_issue": self._issue_view(issue), "escalation": _dump(esc)}

    @staticmethod
    def _issue_view(i: SoftIssue) -> dict:
        return {"issue_id": i.issue_id, "trigger": i.trigger.value, "snag": i.snag, "attempt": i.attempt,
                "attempted_at": i.attempted_at.isoformat(), "status": i.status}

    def issue_outcome(self, client_id: str, issue_id: str, req: rq.IssueOutcomeRequest) -> dict:
        with self._op("issue_outcome"):
            rec = self._client(client_id)
            issue = rec.soft_issues.get(issue_id)
            if issue is None:
                raise NotFound("unknown issue")
            if issue.status != "attempted":
                raise Conflict(f"issue already {issue.status}")
            if req.resolved:
                self._record("soft_trigger_resolved", "intel_13_learning_loop", rec.client_id,
                             {"issue_id": issue.issue_id, "trigger": issue.trigger.value},
                             "Soft trigger resolved by the agent's one attempt")
                self._learn(rec, issue.trigger, issue.trigger.value, issue.attempt, "resolved by the agent's attempt")
                issue.status = "resolved"
                return {"soft_issue": self._issue_view(issue), "escalation": None}
            return self._escalate_issue(rec, issue)

    def _require_andre(self, rec: ClientRecord, action: str, escalation_id: str, token: Optional[str], *fields: str) -> None:
        """Actions attributed to Andre need HIS secret, not the shared
        service token: an HMAC-SHA256 approval token over the exact action,
        compared in constant time (same mechanism as the playbook)."""
        try:
            verify_andre_token(self._andre_key,
                               lambda key: andre_action_token(key, action, rec.client_id, escalation_id, *fields), token)
        except AndreApprovalError as exc:
            self._record("escalation_action_refused", "guardrail_andre_only", rec.client_id,
                         {"action": action, "escalation_id": escalation_id},
                         f"{action} refused: no valid Andre approval token")
            raise Refused(str(exc)) from None

    def acknowledge_escalation(self, client_id: str, escalation_id: str, req: Optional[rq.EscalationAckRequest] = None) -> dict:
        with self._op("acknowledge_escalation"):
            rec = self._client(client_id)
            esc = self.escalations.get(escalation_id)
            if esc is None or esc.client_id != rec.client_id:
                raise NotFound("unknown escalation")
            self._require_andre(rec, "escalation_acknowledge", escalation_id, req.approval_token if req else None)
            if esc.acknowledged_at is not None:
                raise Conflict("already acknowledged")
            now = self.now()
            self._record("escalation_acknowledged", "andre", rec.client_id, {"escalation_id": escalation_id},
                         "Andre acknowledged the briefing; the clock started")
            esc.acknowledged_at = now
            if esc.commitment_id and esc.commitment_id in rec.commitments:
                rec.commitments[esc.commitment_id].engaged = True
            return _dump(esc)

    def resolve_escalation(self, client_id: str, escalation_id: str, req: rq.EscalationResolveRequest) -> dict:
        with self._op("resolve_escalation"):
            rec = self._client(client_id)
            esc = self.escalations.get(escalation_id)
            if esc is None or esc.client_id != rec.client_id:
                raise NotFound("unknown escalation")
            self._require_andre(rec, "escalation_resolve", escalation_id, req.approval_token, req.resolution, req.snag_category)
            if esc.resolved_at is not None:
                raise Conflict("already resolved")
            now = self.now()
            resolution = redact_text(req.resolution)  # fix wave 3 (N5): free text kept only redacted
            entry = self._learn(rec, esc.trigger, req.snag_category, esc.attempted_resolution, resolution, dry=True)
            self._record("escalation_resolved", "andre", rec.client_id,
                         {"escalation_id": escalation_id, "log": entry, "approved_by": "andre"},
                         "Andre resolved the escalation (approval token verified); snag and resolution logged")
            names = self._known_names(rec)
            self._defer(lambda: self.institutional.record(entry, names))
            esc.resolution, esc.resolved_at = resolution, now
            if esc.acknowledged_at is None:
                esc.acknowledged_at = now
            if esc.commitment_id and esc.commitment_id in rec.commitments:
                cm = rec.commitments[esc.commitment_id]
                cm.engaged = True
                cm.breach_nudge_pending = False  # Andre resolved it; nothing left to nudge about
                if cm.status != CommitmentStatus.BREACHED:
                    cm.status = CommitmentStatus.KEPT if now <= cm.due_at else CommitmentStatus.BREACHED
            return _dump(esc)

    def _known_names(self, rec: ClientRecord) -> tuple[str, ...]:
        names = [rec.business_name, rec.client_id, rec.signer.name, rec.signer.email]
        if rec.login_holder:
            names += [rec.login_holder.name, rec.login_holder.email]
        names += [p for n in [rec.signer.name] + ([rec.login_holder.name] if rec.login_holder else []) for p in n.split() if len(p) > 2]
        return tuple(n for n in names if n)

    def _learn(self, rec: ClientRecord, trigger: TriggerKind, snag_category: str, attempted: Optional[str], resolution: str, dry: bool = False) -> dict:
        from memory import strip_identifiers

        entry = strip_identifiers(
            i13.escalation_log_entry(rec.lane.value, trigger.value, snag_category, attempted, resolution), self._known_names(rec)
        )
        if not dry:
            names = self._known_names(rec)
            self._defer(lambda: self.institutional.record(entry, names))
        return entry

    def list_escalations(self) -> list[dict]:
        """Andre's queue: briefing packs, newest first. Needed because the
        push channel to his phone is not wired."""
        with self._lock:
            return [_dump(e) for e in sorted(self.escalations.values(), key=lambda e: e.raised_at, reverse=True)]

    # --- intake -------------------------------------------------------------------

    def _facts_after(self, rec: ClientRecord, req: rq.FactsRequest) -> int:
        """How many facts the client would hold after ``req``, once each
        field is trimmed to its latest ``facts_history_per_field``."""
        keep = self.config.facts_history_per_field
        per_field: dict[str, int] = {}
        for f in rec.facts:
            per_field[f.field] = per_field.get(f.field, 0) + 1
        for f in req.facts:
            per_field[f.field] = per_field.get(f.field, 0) + 1
        return sum(min(n, keep) for n in per_field.values())

    def _facts_over_cap(self, rec: ClientRecord, req: rq.FactsRequest) -> None:
        """Fix wave 6 (wave-5 leftover): the stored facts are bounded. A
        request that would take the client past the cap is refused whole,
        before anything is scanned, flagged, recorded or stored. The size
        that counts is the one the client would hold AFTER the per-field
        trimming (fix wave 7, NEW-6: it was stored + requested, so at the
        cap a restatement of a field at its history limit — the correction
        the message asks for — was refused)."""
        cap, keep = self.config.max_facts_per_client, self.config.facts_history_per_field
        after = self._facts_after(rec, req)
        if after > cap:
            raise Conflict(
                f"this client holds {len(rec.facts)} intake facts and would hold {after} after this request's "
                f"{len(req.facts)}; the cap is {cap} per client — nothing was stored. Each field keeps its latest "
                f"{keep} observations: restating a field that already has {keep} replaces its oldest and adds "
                "nothing, restating one with fewer adds one, a new field adds one. Correct facts within that, "
                "or exit and re-onboard the client if the intake must start over",
                {"facts_stored": len(rec.facts), "facts_in_request": len(req.facts), "facts_after_request": after,
                 "max_facts_per_client": cap})

    def add_facts(self, client_id: str, req: rq.FactsRequest) -> dict:
        # Fix wave 7 (NEW-5 class): the request's own work — the injection
        # scan and the redaction of up to 200 x 2000 chars, ~1.5 s of CPU for
        # a body of profile fields — used to run under the service lock, so
        # every other operation waited behind it. It is pure, so it runs
        # BEFORE the lock (and, through the API, inside the admission lane
        # that serialized the body's scan); the lock then covers only the
        # cheap commit. The cheap refusals (unknown or exited client, cap)
        # come first, so a refused request never pays for the scan.
        for f in req.facts:
            self._not_in_future(f.observed_at, "fact observed_at")
        with self._lock:
            rec = self.clients.get(client_id)
            if rec is None or rec.exited or self._facts_after(rec, req) > self.config.max_facts_per_client:
                with self._op("add_facts"):  # the refusal, exactly as one operation made it before
                    self._facts_over_cap(self._client(client_id), req)
            allowed = i01.LANE_FIELDS[rec.lane]
        scanned = []
        for f in req.facts:
            vals = f.value if isinstance(f.value, list) else [f.value]
            for v in vals:
                if isinstance(v, str):
                    scanned.append((f"intake_fact:{f.field}", scan_for_injection(v, f"intake_fact:{f.field}")))
        # Fix wave 3 (N5): only structured facts are kept — a field outside
        # this lane's profile keeps its name (reported as dropped) but not
        # its value — and every kept value/evidence is the redacted copy.
        new = [i01.Fact(f.field, _redact_value(f.value) if f.field in allowed else None, f.provenance,
                        redact_text(f.evidence), f.observed_at) for f in req.facts]
        with self._op("add_facts"):
            rec = self._client(client_id)
            for f in req.facts:
                self._not_in_future(f.observed_at, "fact observed_at")
            self._facts_over_cap(rec, req)  # again, under the operation: the client may have changed meanwhile
            flags = []
            for source, found in scanned:
                flags += self._record_injection(rec.client_id, source, found)
            rec.facts.extend(new)
            # Each field keeps only its latest N observations (arrival order),
            # so a field restated many times does not grow without bound and
            # the profile rebuild below is O(cap).
            rec.facts = _latest_per_field(rec.facts, self.config.facts_history_per_field)
            if req.vertical:
                rec.vertical = req.vertical
            for f in new:
                if f.field in rec.ask_counts:
                    rec.ask_counts.pop(f.field)
            rec.profile = i01.build_profile(rec.client_id, rec.lane, rec.facts, self.config.gaps_short_list_size)
            rec.last_progress_at = self.now()
            self._count_flags(rec, flags)
            for f in new:
                if f.field in i01.LANE_FIELDS[rec.lane]:
                    self._mem_put(rec.client_id, f"fact:{f.field}", f.value)
            nq = i02.next_question(rec.profile, rec.vertical, rec.ask_counts)
            if nq is not None:
                rec.ask_counts[nq.field] = rec.ask_counts.get(nq.field, 0) + 1
            return {"profile": _dump(rec.profile), "next_question": nq.__dict__ if nq else None, "injection_flags": flags}

    def add_document(self, client_id: str, req: rq.DocumentRequest) -> dict:
        with self._op("add_document"):
            rec = self._client(client_id)
            flags = self._flag_injection(rec.client_id, req.text, "document")
            self._count_flags(rec, flags)
            docs = self.memory.get(rec.client_id, "documents", [])
            # Fix wave 3 (N5): the raw text is never kept; the redacted copy is.
            docs.append({"name": redact_text(req.name), "chars": len(req.text), "redacted_text": redact_text(req.text)})
            self._mem_put(rec.client_id, "documents", docs)
            return {"stored": True, "injection_flags": flags,
                    "extraction": "not built: turning a document into profile facts needs a model; facts must be entered via /intake/facts"}

    def message(self, client_id: str, req: rq.MessageRequest) -> dict:
        with self._op("message"):
            rec = self._client(client_id)
            flags = self._flag_injection(rec.client_id, req.text, "client_message")
            self._count_flags(rec, flags)
            d = i02.decide_reply(req.text, self.config.spanish_enabled)
            if d.intent != "answer":
                self._record("client_intent_ruling", "intel_02_conversation", rec.client_id, {"intent": d.intent},
                             f"Client message classified as {d.intent}")
            msgs = self.memory.get(rec.client_id, "messages", [])
            msgs.append({"from": "client", "text": redact_text(req.text)})  # fix wave 3 (N5): redacted copy only
            if d.reply:
                msgs.append({"from": "agent", "text": d.reply})
            self._mem_put(rec.client_id, "messages", msgs)
            escalation = None
            if d.intent == "human_request":
                # Fix wave 3: one open human request pages Andre once. A repeat
                # (or a retry of a request that stopped part-way) reuses it and
                # only completes what is missing; an undelivered briefing is
                # retried by tick, not by the client asking again.
                escalation = self._open_escalation(rec, TriggerKind.HUMAN_REQUESTED)
                if escalation is not None:
                    self._complete_escalation(rec, escalation, client_live=True)
                else:
                    escalation = self._escalate(rec, TriggerKind.HUMAN_REQUESTED, "Client asked to talk to a human",
                                                attempted=None, client_live=True)
            nq = None
            if d.intent == "answer" and rec.profile is not None:
                q = i02.next_question(rec.profile, rec.vertical, rec.ask_counts)
                if q is not None:
                    rec.ask_counts[q.field] = rec.ask_counts.get(q.field, 0) + 1
                    nq = q.__dict__
            reply = d.reply
            if escalation is not None:
                # Only say Andre has the summary, and only give a time, if the
                # briefing actually reached him (push delivered).
                if escalation.push_delivered:
                    reply = check_outbound(f"{i02.HUMAN_HANDOFF_REPLY} {escalation.client_commitment_text}")
                else:
                    reply = check_outbound(i02.HUMAN_HANDOFF_PENDING_REPLY)
            return {"intent": d.intent, "reply": reply, "next_question": nq, "injection_flags": flags,
                    "escalation": _dump(escalation), "account_changed": False}

    def recap(self, client_id: str) -> dict:
        with self._op("recap"):
            rec = self._client(client_id)
            covered = {}
            if rec.profile is not None:
                for name, pf in rec.profile.fields.items():
                    if pf.provenance in (Provenance.CLIENT_STATED, Provenance.CLIENT_CONFIRMED) and name != "monthly_revenue_usd":
                        covered[name] = ", ".join(pf.value) if isinstance(pf.value, list) else str(pf.value)
            steps = []
            if rec.profile and rec.profile.gaps:
                steps.append(f"We still need: {', '.join(g.field.replace('_', ' ') for g in rec.profile.gaps)}.")
            if not rec.verifications:
                steps.append("The person who holds your logins uses the secure access link.")
            open_c = [c.text for c in rec.commitments.values() if c.status in (CommitmentStatus.OPEN, CommitmentStatus.ON_TRACK, CommitmentStatus.RESCHEDULED)]
            try:
                text = i02.recap(rec.signer.name.split()[0], covered, steps, open_c)
            except OutboundBlocked:
                text = i02.recap(rec.signer.name.split()[0], {}, steps, open_c)
            recaps = self.memory.get(rec.client_id, "recaps", [])
            recaps.append(text)
            self._mem_put(rec.client_id, "recaps", recaps)
            return {"recap": text}

    # --- platform access ------------------------------------------------------------

    def website_scan(self, client_id: str, req: rq.WebsiteScanRequest) -> dict:
        with self._op("website_scan"):
            rec = self._client(client_id)
            flags = self._flag_injection(rec.client_id, req.html, "website")
            self._count_flags(rec, flags)
            tags = i04.scan_tags(req.html)
            detected = [t["platform"] for t in tags]
            today = self.business_date()
            plans = []
            for t in tags:
                p = i04.Platform(t["platform"])
                pl = i04.plan_access_request(p, "audit", rec.signer, rec.login_holder, today,
                                             self.config.platform_fact_shelf_life_days, self.knowledge)
                plans.append({
                    "platform": p.value, "job": pl.job, "required_role": pl.required_role, "send_to": pl.send_to,
                    "send_to_is_signer": pl.send_to_is_signer, "steps": pl.steps, "quotable": pl.quotable,
                    "client_message_status": "released" if pl.quotable else f"held: {pl.quotable_reason}",
                    "ask_or_escalate": pl.ask_or_escalate,
                })
            self._record("platform_request_plan", "intel_04_platform_access", rec.client_id,
                         {"platforms": detected, "quotable": {p["platform"]: p["quotable"] for p in plans}},
                         f"Tag scan found {len(tags)} platform tag(s)")
            rec.detected_tags = detected
            rec.last_progress_at = self.now()
            return {"detected": tags, "access_requests": plans, "injection_flags": flags,
                    "site_fetch": "not built: the caller supplies the page HTML"}

    def add_grant(self, client_id: str, grant: AccessGrantIn) -> dict:
        with self._op("add_grant"):
            rec = self._client(client_id)
            self._not_in_future(grant.account_last_activity_at, "account_last_activity_at")
            now = self.now()
            self._record("platform_probe_request", "intel_04_platform_access", rec.client_id,
                         {"platform": grant.platform.value, "account_type": grant.account_type.value, "role": grant.granted_role},
                         f"P21 live check requested for {grant.platform.value} grant")
            live_ok, live_detail = self.probe.check(grant.platform.value, grant.account_id)
            v: AccessVerification = i04.verify_grant(grant, now, self.config.stale_account_days, live_ok, live_detail, self.knowledge)
            receipt, quotable, why = (None, False, "grant not usable")
            if v.usable:
                receipt, quotable, why = permission_receipt.build_receipt(grant, self.business_date(), self.config.platform_fact_shelf_life_days, self.knowledge)
            self._record("access_verification_ruling", "intel_04_platform_access", rec.client_id,
                         {"platform": grant.platform.value, "usable": v.usable, "problems": [p.code for p in v.problems],
                          "receipt_sent": bool(receipt and quotable)},
                         f"P21 {grant.platform.value}: {'usable' if v.usable else 'not usable'}")
            rec.grants[grant.platform.value] = grant
            rec.verifications[grant.platform.value] = v
            if receipt is not None and quotable:
                rec.receipts_sent.add(grant.platform.value)
            else:
                rec.receipts_sent.discard(grant.platform.value)
            rec.last_progress_at = now
            return {"verification": _dump(v), "client_message": check_outbound(v.client_message),
                    "permission_receipt": _dump(receipt), "permission_receipt_status": "sent" if (receipt and quotable) else f"held: {why}"}

    def offer_credential(self, client_id: str) -> dict:
        """Tier 2/3 (vault). The body is never read by the API; the vault
        stand-in refuses. Nothing is stored."""
        with self._op("offer_credential"):
            rec = self._client(client_id)
            self._record("vault_store_refused", "intel_04_platform_access", rec.client_id, {"vault_certified": self.vault.certified},
                         "Credential offered; vault not certified, refused; nothing stored")
            try:
                self.vault.store(rec.client_id, "unspecified", "")
            except PermissionError as exc:
                raise Refused(str(exc), {
                    "stored": False,
                    "client_message": check_outbound(
                        "Please don't send passwords. We use delegated access you control instead; the person who "
                        "holds your logins can grant it from the secure access link, and revoke it any time."),
                }) from None
            raise Refused("vault refused")  # pragma: no cover  (no certified vault exists)

    # --- audit (6) + risk (8) --------------------------------------------------------

    def audit(self, client_id: str, req: rq.AuditRequest) -> dict:
        """Fix wave 4 (A1): the two rulings are the RESULT of the detection
        call (an outside effect that sends the account data), so they are
        owed records (``_record_results``): if either write fails, the next
        operation on the client writes both, same ids. The detection result
        is kept on the client (``audit_known``) from the moment it is known,
        so a retry of the same audit never sends the account data again."""
        with self._op("audit"):
            rec = self._client(client_id)  # writes any owed result records first
            digest = hashlib.sha256(req.model_dump_json().encode()).hexdigest()
            known = rec.audit_known if rec.audit_known is not None and rec.audit_known["digest"] == digest else None
            if known is None:
                kinds = sorted(k for k, v in req.account_data.items() if v)
                self._record("revenue_recovery_request", "intel_06_audit_baseline", rec.client_id,
                             {"data_kinds": kinds, "rows": sum(len(v) for v in req.account_data.values())},
                             "Account pull sent to Revenue Recovery detection")
                try:
                    raw = self._effect("revenue_recovery_detect", lambda: self.rr.detect(req.account_data))
                    overlaps = self.rr.overlaps(raw)
                except RevenueRecoveryError as exc:
                    # The call failed: no result exists, nothing was detected
                    # (as an undelivered push, not counted as an effect).
                    self._record("revenue_recovery_failed", "intel_06_audit_baseline", rec.client_id,
                                 {"data_kinds": kinds, "error": str(exc)[:200]},
                                 "Revenue Recovery unavailable; no audit recorded")
                    raise UpstreamUnavailable(f"Revenue Recovery unavailable: {exc}; no audit recorded") from None
                findings, rejected = i06.consume(raw, overlaps)
                base = i06.baseline(findings)
                stated = self._stated_revenue(rec)
                risk = i08.assess(req.risk_signals, stated, req.observed_monthly_revenue_usd, self.config.revenue_mismatch_tolerance)
                known = {"digest": digest, "raw": raw, "findings": findings, "rejected": rejected, "base": base, "risk": risk,
                         "risk_event_recorded": False, "risk_event_published": False}
                rec.audit_known = known  # it happened: a retry reuses it
                self._record_results([
                    ("revenue_recovery_ruling", "intel_06_audit_baseline", rec.client_id,
                     {"finding_ids": [f.finding_id for f in findings], "rejected": rejected,
                      "totals_by_classification": {k: money_str(v) for k, v in base.totals_by_classification.items()}},
                     f"Revenue Recovery returned {len(findings)} finding(s), {len(rejected)} rejected"),
                    ("risk_ruling", "intel_08_risk_anomaly", rec.client_id, {"kind": risk.kind, "reasons": list(risk.reasons)},
                     f"Risk and Anomaly: {risk.kind}"),
                ])
            else:
                # Reusing the known result writes no new ruling, but this is
                # still an audit of the subject: its completion must advance
                # the subject's sequence, or the NEXT audit (same payload)
                # would derive the ids of these rulings and be deduped away.
                self._touched.add(rec.client_id)
            raw, findings, rejected, base, risk = (known["raw"], known["findings"], known["rejected"], known["base"],
                                                   known["risk"])
            if risk.kind != "nothing" and not known["risk_event_published"]:
                if not known["risk_event_recorded"]:
                    self._record("risk_event_published", "intel_08_risk_anomaly", rec.client_id, {"kind": risk.kind},
                                 "Anomaly event published to the risk watcher / AEGIS when the audit completes (consumers not built)")
                    known["risk_event_recorded"] = True
                self._publish("risk_anomaly", rec.client_id, {"kind": risk.kind, "reasons": list(risk.reasons)})
                self._defer(lambda: known.__setitem__("risk_event_published", True))
            # The audit is finished once this operation completes; until then a
            # retry keeps using the known detection result.
            self._defer(lambda: setattr(rec, "audit_known", None) if rec.audit_known is known else None)
            rec.raw_findings, rec.findings, rec.baseline, rec.risk = raw, findings, base, risk
            rec.last_progress_at = self.now()
            out = {"baseline": _dump(base), "findings": [_dump(f) for f in findings], "rejected_findings": rejected,
                   "risk": {"kind": risk.kind, "reasons": list(risk.reasons)}, "soft_issue": None, "escalation": None,
                   "client_facing_numbers": "paused" if risk.kind != "nothing" else "allowed"}
            if risk.kind == "hard_stop":
                rec.hard_stop = True
                esc = self._escalate(rec, TriggerKind.AUDIT_ANOMALY, "Risk hard stop: " + "; ".join(risk.reasons),
                                     attempted="none: hard stop — work paused immediately", client_live=False)
                out["escalation"] = _dump(esc)
            elif risk.kind == "soft_trigger":
                r = self._soft_trigger(rec, TriggerKind.AUDIT_ANOMALY, "; ".join(risk.reasons),
                                       "Thanks for your patience. Some numbers in your account don't match what we expected from "
                                       "what you told us. Could you help us understand the difference? It helps us get your plan right.")
                out.update({"soft_issue": r.get("soft_issue"), "escalation": r.get("escalation"), "client_message": r.get("client_message")})
            return out

    @staticmethod
    def _stated_revenue(rec: ClientRecord) -> Optional[Decimal]:
        if rec.profile is None:
            return None
        pf = rec.profile.fields.get("monthly_revenue_usd")
        if pf is None:
            return None
        # A figure the client typed, not contract money (see money.py).
        try:
            return client_stated_amount(pf.value)
        except ValueError:
            return None

    # --- plan (3), setup (5), momentum (7), recommend (P20) ----------------------------

    def plan(self, client_id: str, req: rq.PlanRequest) -> dict:
        with self._op("plan"):
            rec = self._client(client_id)
            if rec.baseline is None:
                raise Conflict("no audit baseline yet; the merged plan needs what we found AND what the client said")
            plan = i03.fuse(rec.client_id, rec.findings, req.client_priorities)
            self._record("merged_plan", "intel_03_priority_fusion", rec.client_id,
                         {"order": [i.topic for i in plan.items], "disagreements": [d.topic for d in plan.disagreements]},
                         f"Merged plan: {len(plan.items)} item(s), {len(plan.disagreements)} disagreement(s) shown to client")
            rec.client_priorities, rec.plan, rec.plan_choices = list(req.client_priorities), plan, {}
            self._mem_put(rec.client_id, "client_priorities", list(req.client_priorities))
            rec.last_progress_at = self.now()
            return _dump(plan)

    def plan_choice(self, client_id: str, req: rq.PlanChoiceRequest) -> dict:
        with self._op("plan_choice"):
            rec = self._client(client_id)
            if rec.plan is None or not any(d.topic == req.topic for d in rec.plan.disagreements):
                raise NotFound("no open disagreement on that topic")
            self._record("plan_choice", "intel_03_priority_fusion", rec.client_id, {"topic": req.topic, "choice": req.choice},
                         f"Client chose '{req.choice}' on {req.topic}")
            rec.plan_choices[req.topic] = req.choice
            if req.choice == "accept_recommendation":
                order = {t: i for i, t in enumerate(rec.plan.recommended_order)}
                items = sorted(rec.plan.items, key=lambda i: order.get(i.topic, 999))
                rec.plan = rec.plan.model_copy(update={"items": items})
            return {"plan": _dump(rec.plan), "choices": dict(rec.plan_choices), "plan_agreed": self._plan_agreed(rec)}

    @staticmethod
    def _plan_agreed(rec: ClientRecord) -> bool:
        return rec.plan is not None and all(d.topic in rec.plan_choices for d in rec.plan.disagreements)

    def setup_plan(self, client_id: str) -> dict:
        with self._lock:
            rec = self._client(client_id)
            verified = [p for p, v in rec.verifications.items() if v.usable]
            out = i05.build_setup_plan(verified, rec.detected_tags, [f.leak_category for f in rec.findings], rec.preferred_channel)
            out["phase"] = "phase 2 — not certified for real clients"
            return out

    def account_change(self, client_id: str, req: rq.AccountChangeRequest) -> dict:
        with self._op("account_change"):
            rec = self._client(client_id)
            proposal = i05.ChangeProposal(req.change_id, req.platform, req.description, True,
                                          i05.change_digest(req.change_id, req.platform, req.description))
            approvals = [i05.ClientApproval(a.change_id, a.digest, a.approved_by_client, a.source) for a in req.client_approvals]
            ok, why = i05.authorize_change(proposal, approvals)
            self._record("account_change_ruling", "intel_05_setup", rec.client_id,
                         {"change_id": req.change_id, "platform": req.platform, "authorized": ok},
                         f"Account change {'authorized by client' if ok else 'refused: no explicit client yes'}")
            if not ok:
                raise Refused(why, {"executed": False, "digest_to_confirm": proposal.digest})
            try:
                self.writer.apply(rec.client_id, {"change_id": req.change_id})
            except PlatformWriteRefused as exc:
                raise Refused(f"client said yes, but {exc}", {"executed": False}) from None
            return {"executed": True}  # pragma: no cover  (no platform writer exists)

    def momentum(self, client_id: str) -> dict:
        with self._op("momentum"):
            rec = self._client(client_id)
            p = i07.pick(rec.findings)
            self._record("momentum_ruling", "intel_07_momentum_moment", rec.client_id,
                         {"finding_id": p.finding_id, "rejected": p.rejected}, f"Momentum Moment: {p.reason}"[:280])
            return {"finding_id": p.finding_id, "score": p.score, "reason": p.reason, "rejected": p.rejected,
                    "phase": "phase 2 scoring — not certified for real clients"}

    def first_win(self, client_id: str, req: rq.FirstWinRequest) -> dict:
        with self._op("first_win"):
            rec = self._client(client_id)
            f = next((x for x in rec.findings if x.finding_id == req.finding_id), None)
            if f is None:
                raise NotFound("unknown finding")
            ok, why = i07.provable(f)
            if not ok:
                raise Conflict(f"cannot record an unproven win: {why}")
            if rec.first_win_at is not None:
                raise Conflict("first win already recorded")
            now = self.now()
            self._record("first_win", "intel_07_momentum_moment", rec.client_id, {"finding_id": f.finding_id},
                         "First real (provable) win delivered")
            rec.first_win_finding, rec.first_win_at, rec.last_progress_at = f.finding_id, now, now
            may, _ = recommend_score.may_ask(True, rec.recommend_asked)
            msg = None
            if may:
                rec.recommend_asked = True
                msg = check_outbound(recommend_score.ASK_TEXT)
            return {"first_win": f.finding_id, "value": f.recoverable_value.render(), "recommend_question": msg}

    def recommend_score_submit(self, client_id: str, req: rq.RecommendScoreRequest) -> dict:
        with self._op("recommend_score_submit"):
            rec = self._client(client_id)
            if rec.first_win_at is None or not rec.recommend_asked:
                raise Conflict("the recommend score is asked only right after the first real win; not asked yet")
            if rec.recommend_score is not None:
                raise Conflict("score already recorded")
            path, text = recommend_score.route(req.score)
            self._record("recommend_score", "intel_13_learning_loop", rec.client_id, {"score": req.score, "path": path},
                         f"P20 recommend score {req.score}: {path}")
            out = {"path": path, "client_message": text, "soft_issue": None, "escalation": None}
            if path == "soft_escalation":
                r = self._soft_trigger(rec, TriggerKind.LOW_RECOMMEND_SCORE, f"Recommend score {req.score}", text)
                out.update({"soft_issue": r.get("soft_issue"), "escalation": r.get("escalation")})
            rec.recommend_score = req.score  # after every record it depends on (fix wave 3, N2 sweep)
            return out

    # --- promise keeper + stuck (tick) ---------------------------------------------------

    def tick(self, client_id: str) -> dict:
        with self._op("tick"):
            rec = self._client(client_id)
            now = self.now()
            cfg = self.config
            out: dict = {"stuck": None, "commitment_actions": []}
            s = i02.stuck_signal(rec.last_progress_at, now, cfg.stuck_window_hours, rec.ask_counts)
            if s.stuck:
                trig = TriggerKind.STUCK if "no progress" in s.reason else TriggerKind.FRICTION
                is_new = not any(i.trigger == trig and i.status == "attempted" for i in rec.soft_issues.values())
                r = self._soft_trigger(rec, trig, s.reason,
                                       "Quick check-in: we're waiting on one step to keep your setup moving. "
                                       "Is anything getting in the way? If it's easier to talk to a person, just say so.")
                if is_new:
                    self._defer(lambda: setattr(rec, "stalls", rec.stalls + 1))
                out["stuck"] = {"reason": s.reason, **r}
            for c in list(rec.commitments.values()):
                actions = i09.decide(c, now, rec.time_zone, rec.quiet_start, rec.quiet_end, cfg.commitment_cutoff_local,
                                     cfg.commitment_tz, cfg.today_due_local, cfg.first_thing_local,
                                     cfg.andre_nudge_lead_hours, cfg.client_warn_lead_hours, cfg.andre_nudge_max_attempts)
                for a in actions:
                    if a.action == "none":
                        continue
                    self._record(f"promise_{a.action}", "intel_09_promise_keeper", rec.client_id,
                                 {"commitment_id": c.commitment_id, "send_at": a.send_at.isoformat() if a.send_at else None,
                                  "new_due_at": a.new_due_at.isoformat() if a.new_due_at else None,
                                  "quiet_hours_override": a.quiet_hours_override},
                                 f"Promise Keeper: {a.action} ({a.reason})"[:280])
                    if a.action == "nudge_andre":
                        # The breach nudge is a new nudge with its own budget.
                        new_breach_nudge = now >= c.due_at and c.status != CommitmentStatus.BREACHED
                        failures = 0 if new_breach_nudge else c.andre_nudge_failures
                        delivered, detail = self._effect("andre_push", lambda: self._push_andre(
                            f"nudge-{c.commitment_id}", {"commitment": c.text, "due_at": c.due_at.isoformat(), "reason": a.reason}),
                            done=lambda r: bool(r[0]))
                        # Fix wave 2 (L2): nudged ONLY on confirmed delivery;
                        # otherwise counted, recorded and retried next tick.
                        attempt = failures + 1
                        will_retry = (not delivered) and attempt < cfg.andre_nudge_max_attempts
                        payload = {"commitment_id": c.commitment_id, "delivered": delivered, "attempt": attempt,
                                   "max_attempts": cfg.andre_nudge_max_attempts, "will_retry": will_retry}
                        summary = ("Nudge to Andre delivered" if delivered else
                                   f"Nudge to Andre NOT delivered ({detail}); "
                                   + ("retry next tick" if will_retry else "no retries left"))[:280]
                        if delivered:
                            # It happened: state says so; the result record is
                            # owed until written (fix wave 3, N7 class).
                            c.andre_nudged, c.andre_nudge_failures, c.breach_nudge_pending = True, 0, False
                            self._record_result("promise_nudge_result", "intel_09_promise_keeper", rec.client_id, payload, summary)
                        else:
                            # Nothing happened outside: the attempt is counted
                            # when the tick completes (fix wave 3, N2 sweep), so
                            # a tick that stops later is retried as the SAME
                            # attempt (same event ids and summaries: 200, never
                            # a 409 from a changed summary).
                            self._record("promise_nudge_result", "intel_09_promise_keeper", rec.client_id, payload, summary)

                            def _not_delivered(c=c, attempt=attempt, reset=new_breach_nudge, pending=now >= c.due_at):
                                if reset:
                                    c.andre_nudged = False
                                c.andre_nudge_failures = attempt
                                c.breach_nudge_pending = pending
                            self._defer(_not_delivered)
                    elif a.action == "warn_client":
                        # The client is told the new real time in THIS response:
                        # the commitment tracks the new time (fresh nudge/warn
                        # state) once the tick completes — if a later record
                        # fails, the warning was not sent and the retry re-sends
                        # it (fix wave 3, N2 sweep). Actions of one commitment
                        # come nudge-first, so the order of effects is unchanged.
                        def _rescheduled(c=c, a=a):
                            c.status = CommitmentStatus.RESCHEDULED
                            c.proposed_new_due_at = a.new_due_at
                            c.due_at = a.new_due_at
                            c.andre_nudged = False
                            c.andre_nudge_failures = 0
                            c.breach_nudge_pending = False
                            c.client_warned = False
                            c.category = "first_thing_tomorrow"
                        self._defer(_rescheduled)
                    elif a.action == "breached":
                        self._defer(lambda c=c: setattr(c, "status", CommitmentStatus.BREACHED))
                    out["commitment_actions"].append({"commitment_id": c.commitment_id, "action": a.action, "reason": a.reason,
                                                      "send_at": a.send_at.isoformat() if a.send_at else None,
                                                      "client_message": a.message, "new_due_at": a.new_due_at.isoformat() if a.new_due_at else None,
                                                      "quiet_hours_override": a.quiet_hours_override})
            out["escalation_deliveries"] = self._retry_escalation_pushes(rec)
            return out

    def _retry_escalation_pushes(self, rec: ClientRecord) -> list[dict]:
        """Fix wave 3 (wave-2 leftover): a briefing that did not reach Andre
        is retried on tick — bounded by ``escalation_push_max_attempts``
        (initial push included), every attempt recorded before and after,
        like the Promise Keeper nudge. Once delivered, the client commitment
        is made (a fresh time, since the client hears it now). A briefing
        delivered whose commitment record failed is completed here too."""
        cfg = self.config
        out = []
        for eid in list(rec.escalation_ids):
            esc = self.escalations[eid]
            if esc.resolved_at is not None or esc.acknowledged_at is not None:
                continue
            p = self._esc_plans[eid]
            if esc.push_delivered:
                status = self._complete_escalation(rec, esc, client_live=False, defer=True)
                if status is not None:
                    out.append(self._delivery_view(esc, p, True, esc.push_attempts, False, status))
                continue
            if esc.push_attempts >= cfg.escalation_push_max_attempts:
                continue
            attempt = esc.push_attempts + 1
            self._record("andre_push_retry_request", "intel_10_escalation_briefing", rec.client_id,
                         {"escalation_id": eid, "attempt": attempt, "max_attempts": cfg.escalation_push_max_attempts},
                         f"Briefing push to Andre retried (attempt {attempt})")
            delivered, detail = self._effect("andre_push", lambda: self._push_andre(eid, p.briefing.model_dump(mode="json")),
                                             done=lambda r: bool(r[0]))
            will_retry = (not delivered) and attempt < cfg.escalation_push_max_attempts
            payload = {"escalation_id": eid, "delivered": delivered, "attempt": attempt,
                       "max_attempts": cfg.escalation_push_max_attempts, "will_retry": will_retry}
            summary = ("Briefing delivered to Andre on retry" if delivered else
                       f"Briefing NOT delivered to Andre ({detail}); " + ("retry next tick" if will_retry else "no retries left"))[:280]
            status = esc.client_message_status
            if delivered:
                esc.push_attempts, esc.push_delivered, esc.push_detail = attempt, True, detail
                self._record_result("andre_push_result", "intel_10_escalation_briefing", rec.client_id, payload, summary)
                status = self._complete_escalation(rec, esc, client_live=False, defer=True) or status
            else:
                self._record("andre_push_result", "intel_10_escalation_briefing", rec.client_id, payload, summary)

                def _not_delivered(esc=esc, attempt=attempt, detail=detail):
                    esc.push_attempts, esc.push_detail = attempt, detail
                self._defer(_not_delivered)  # counted when the tick completes (as the nudge)
            out.append(self._delivery_view(esc, p, delivered, attempt, will_retry, status))
        return out

    @staticmethod
    def _delivery_view(esc: Escalation, p, delivered: bool, attempt: int, will_retry: bool, status: str) -> dict:
        return {"escalation_id": esc.escalation_id, "delivered": delivered, "attempt": attempt, "will_retry": will_retry,
                "client_message": check_outbound(p.commitment.text) if (delivered and status == "released") else None,
                "client_message_status": status}

    # --- health (13), view, memory, exit ----------------------------------------------

    def health(self, client_id: str) -> dict:
        with self._op("health"):
            rec = self._client(client_id)
            now = self.now()
            h = i13.health(
                stalls=rec.stalls, escalations=len(rec.escalation_ids),
                breached_commitments=sum(1 for c in rec.commitments.values() if c.status == CommitmentStatus.BREACHED),
                recommend_score=rec.recommend_score, hours_since_progress=(now - rec.last_progress_at).total_seconds() / 3600,
                stuck_window_hours=self.config.stuck_window_hours, first_win_at=rec.first_win_at, started_at=rec.started_at,
                previous_score=rec.last_health,
            )
            rec.last_health = h.score
            return {"score": h.score, "band": h.band, "early_warning": h.early_warning, "reasons": list(h.reasons),
                    "scorecard": h.scorecard, "phase": "phase 2 — not certified for real clients"}

    def view(self, client_id: str) -> dict:
        """What the client (and the operator) can see — this client only (P11)."""
        with self._lock:
            rec = self.clients.get(client_id)
            if rec is None:
                raise NotFound("unknown client")
            return {
                "client_id": rec.client_id, "lane": rec.lane.value, "business_name": rec.business_name, "exited": rec.exited,
                "profile": _dump(rec.profile),
                "access": {p: _dump(v) for p, v in rec.verifications.items()},
                "permission_receipts_sent": sorted(rec.receipts_sent),
                "baseline": _dump(rec.baseline),
                "plan": _dump(rec.plan),
                "commitments": [_dump(c) for c in rec.commitments.values()],
                "escalations": [{"escalation_id": e, "trigger": self.escalations[e].trigger.value,
                                 "resolved": self.escalations[e].resolved_at is not None} for e in rec.escalation_ids],
                "soft_issues": [self._issue_view(i) for i in rec.soft_issues.values()],
                "first_win": rec.first_win_finding,
                "activation": _dump(rec.activation),
                "memory": self.memory.view(rec.client_id),
            }

    def delete_memory(self, client_id: str) -> dict:
        with self._op("delete_memory"):
            self._refuse_if_quarantined(client_id)
            rec = self.clients.get(client_id)
            if rec is None:
                raise NotFound("unknown client")
            self._flush_pending(rec.client_id)
            self._record("client_memory_deleted", "memory_client", rec.client_id, {}, "Client requested deletion of their memory (P11)")
            deleted = self.memory.delete(rec.client_id)
            return {"deleted": deleted}

    def exit(self, client_id: str, req: rq.ExitRequest) -> dict:
        with self._op("exit"):
            rec = self._client(client_id)
            plan = clean_exit.exit_plan(rec.client_id, list(rec.grants.values()), self.vault.certified, req.memory_choice)
            self._record("clean_exit", "practice_p4_clean_exit", rec.client_id,
                         {"steps": [s["action"] for s in plan["steps"]], "memory_choice": req.memory_choice},
                         "P4 clean exit plan issued; access revoke, vault destroy, reports export, memory")
            export = self.memory.export(rec.client_id) if req.memory_choice == "export_then_destroy" else None
            self.memory.delete(rec.client_id)
            destroyed = self.vault.destroy_all(rec.client_id)
            rec.exited = True
            return {"exit_plan": plan, "memory_export": export, "vault_secrets_destroyed": destroyed,
                    "executed": "plan only: automated revoke and report export are not wired"}

    # --- activation gates (14 + 15) ----------------------------------------------------

    def activate_client(self, client_id: str) -> dict:
        with self._op("activate_client"):
            rec = self._client(client_id)
            today = self.business_date()
            self._record("contract_lookup_request", "intel_14_contract", rec.client_id, {}, "Contract terms requested from contract storage")
            terms = self.depts.contracts.get(rec.client_id)
            plan_services = sorted({i.service for i in rec.plan.items}) if rec.plan else []
            open_cats = [c.category for c in rec.commitments.values() if c.kind != "escalation_callback"
                         and c.status in (CommitmentStatus.OPEN, CommitmentStatus.ON_TRACK, CommitmentStatus.RESCHEDULED)]
            g14 = i14.client_gate(terms, today, plan_services, open_cats)
            self._record("compliance_dept_request", "intel_15_compliance", rec.client_id, {"lane": rec.lane.value},
                         "Activation ruling requested from Compliance department (38)")
            dept = self.depts.compliance.rule(rec.client_id, rec.lane.value, {})
            self._record("billing_request", "intel_15_compliance", rec.client_id, {}, "Billing setup (P16) status requested")
            billing = self.depts.billing.billing_ready(rec.client_id)
            usable = [p for p, v in rec.verifications.items() if v.usable]
            facts = {
                "p1_disclosure_sent": rec.p1_disclosure_sent,
                "p1_wording_counsel_approved": self.config.p1_wording_counsel_approved,
                "p23_clause_counsel_approved": self.config.p23_clause_counsel_approved,
                "baseline_done": rec.baseline is not None,
                "plan_agreed": self._plan_agreed(rec),
                "access_verified": bool(usable),
                "receipts_sent": bool(usable) and all(p in rec.receipts_sent for p in usable),
                "no_hard_stop": not rec.hard_stop,
                "no_open_hard_escalation": not any(self.escalations[e].hard and self.escalations[e].resolved_at is None for e in rec.escalation_ids),
                "billing_ready": billing.allowed,
                "compliance_dept_allowed": dept.allowed,
            }
            g15 = i15.gate(rec.lane, facts)

            def handoff() -> tuple[Optional[dict], list[str]]:
                if rec.handoff_accepted:  # a retry after a partial failure: never hand off twice
                    return {"accepted": True, "detail": "already accepted"}, []
                self._record("activation_handoff_request", "onboarding_service", rec.client_id, {"lane": rec.lane.value},
                             "Full client file handed to the receiving department")
                ruling = self._effect("activation_handoff", lambda: self.depts.handoff.accept(self._client_file(rec)),
                                      done=lambda r: r.allowed)
                rec.handoff_accepted = ruling.allowed  # reality; its result record is owed until written (N7 class)
                self._record_result("activation_handoff_ruling", "onboarding_service", rec.client_id, {"accepted": ruling.allowed},
                                    f"Handoff {'accepted' if ruling.allowed else 'not accepted'}")
                info = {"accepted": ruling.allowed, "detail": ruling.detail or "; ".join(ruling.unmet)}
                return info, ([] if ruling.allowed else [f"handoff/{u}" for u in ruling.unmet])

            return self._finish_activation(rec.client_id, rec.lane, g14, g15, handoff, rec, "client handoff")

    def _client_file(self, rec: ClientRecord) -> dict:
        return {
            "client_id": rec.client_id, "lane": rec.lane.value, "profile": _dump(rec.profile),
            "verified_access": [p for p, v in rec.verifications.items() if v.usable],
            "merged_plan": _dump(rec.plan),
            "open_commitments": [_dump(c) for c in rec.commitments.values() if c.status != CommitmentStatus.KEPT],
        }

    def _finish_activation(self, subject_id: str, lane: Lane, g14, g15, after_gates: Callable[[], tuple], rec,
                           crossing: str) -> dict:
        """Record-first (fix wave 1, F2): both gate rulings AND the
        activation ruling are recorded BEFORE the post-gate crossing
        (client handoff / creator payout activation). If any of those
        records fails, the crossing never happens. The crossing's result is
        then recorded as ``activation_outcome``; a failed crossing blocks
        activation and says why."""
        self._record("contract_gate_ruling", "intel_14_contract", subject_id, {"passed": g14.passed, "unmet": g14.unmet, "drift": g14.drift},
                     f"Contract gate (14): {'passed' if g14.passed else f'{len(g14.unmet)} unmet'}")
        self._record("compliance_gate_ruling", "intel_15_compliance", subject_id, {"passed": g15.passed, "unmet": g15.unmet},
                     f"Compliance gate (15): {'passed' if g15.passed else f'{len(g15.unmet)} unmet'}")
        unmet = [f"contract_14/{u}" for u in g14.unmet] + [f"compliance_15/{u}" for u in g15.unmet]
        gates_passed = g14.passed and g15.passed
        self._record("activation_ruling", "intel_15_compliance", subject_id,
                     {"activated": gates_passed, "gates_passed": gates_passed, "unmet": unmet,
                      "next_crossing": crossing if gates_passed else None},
                     f"Activation allowed by gates 14 and 15; {crossing} follows" if gates_passed
                     else f"Activation blocked: {len(unmet)} requirement(s) unmet")
        handoff = None
        if gates_passed:
            handoff, extra = after_gates()
            unmet += extra
        activated = not unmet
        decision = ActivationDecision(subject_id=subject_id, lane=lane, activated=activated, contract=g14, compliance=g15,
                                      unmet=unmet, handoff=handoff)
        if gates_passed:
            self._record("activation_outcome", "intel_15_compliance", subject_id, {"activated": activated, "unmet": unmet},
                         f"Activation {'complete' if activated else f'not complete: {crossing} failed'}")
        rec.activation = decision  # only after every record it depends on (fix wave 3, N2 sweep)
        if not activated:
            raise Conflict("activation blocked", _dump(decision))
        return _dump(decision)

    # --- ZBC creator lane (11, P8, P9) ----------------------------------------------------

    def apply_creator(self, app: ClipperApplication) -> dict:
        with self._op("apply_creator", creates_subject=True):
            self._refuse_if_quarantined(app.creator_id)
            digest = hashlib.sha256(app.model_dump_json().encode()).hexdigest()
            existing = self.creators.get(app.creator_id)
            if existing is not None:
                if existing.apply_out is not None:
                    return self._complete_apply(existing, digest)
                raise Conflict("creator already applied")
            # Age is computed on the SERVER's date (UTC-12), never a caller date (F1).
            evaluated_on = self.age_evaluation_date()
            if app.date_of_birth is not None and app.date_of_birth > self.latest_calendar_date():
                # Fix wave 5 (LOW-E): a date of birth after today's date (the
                # latest calendar date anywhere on Earth, UTC+14, so no real
                # birth date is refused) is not a date of birth.
                raise Invalid("date_of_birth is in the future by the server's date; a date of birth cannot be "
                              "later than today")
            if app.date_of_birth is not None and i11.age_on(app.date_of_birth, evaluated_on) > MAX_PLAUSIBLE_AGE_YEARS:
                # Fix wave 4 (D1): refused before anything is recorded.
                raise Invalid(f"date_of_birth implies an age over {MAX_PLAUSIBLE_AGE_YEARS} on the server's date; "
                              "not a plausible date of birth")
            flags = self._flag_injection(app.creator_id, app.bio, "creator_bio")
            decision = i11.vet(app, evaluated_on)
            decision = decision.model_copy(update={"injection_flags": [f["rule"] for f in flags]})
            first = ai_disclosure_first_message(app.legal_name.split()[0], company="ZBC")
            self._record("first_message_sent", "intel_02_conversation", app.creator_id,
                         {"ai_disclosure": True, "human_offered": True}, "P1 first message to clipper applicant")
            self._record("vetting_ruling", "intel_11_creator_vetting", app.creator_id,
                         {"outcome": decision.outcome.value, "reasons": decision.reasons,
                          "age_evaluated_on": evaluated_on.isoformat(), "age_evaluation_clock": "server, UTC-12 date"},
                         f"Creator Vetting: {decision.outcome.value}")
            referral = None
            if decision.outcome == VettingOutcome.SEND_TO_ANDRE:
                self._record("andre_push_request", "intel_11_creator_vetting", app.creator_id, {"reasons": decision.reasons},
                             "Clipper application sent to Andre with written reasons")
            # Fix wave 3 (N5): the bio is client free text — kept only redacted.
            stored_app = app.model_copy(update={"bio": redact_text(app.bio)})
            rec = CreatorRecord(app.creator_id, stored_app, decision, True, app.w9_received, app.disclosure_training_completed,
                                app.creator_agreement_signed, person_key=person_key(app.legal_name, app.date_of_birth))
            message = {
                VettingOutcome.APPROVE: "You're approved. We're setting up your materials, tracking links and payment details now.",
                VettingOutcome.DECLINE: ("Thank you for applying. We can't accept this application. "
                                         + ("Clippers must be 18 or older." if any("under 18" in r for r in decision.reasons) else "")).strip(),
                VettingOutcome.INCOMPLETE: "Thanks for applying. We need your date of birth to continue; clippers must be 18 or older.",
                VettingOutcome.SEND_TO_ANDRE: "Thanks for applying. A person on our team is reviewing your application.",
            }[decision.outcome]
            out = {"first_message": first, "vetting": _dump(decision), "applicant_message": check_outbound(message),
                   "andre_referral": referral, "activation": None}
            # Stage then commit (fix wave 3, N2): the creator becomes visible
            # only after every record it depends on — or, if an outside
            # effect already happened, as it really is, with a retry of the
            # same application completing it (never a permanent 409).
            try:
                if decision.outcome == VettingOutcome.SEND_TO_ANDRE:
                    delivered, detail = self._effect("andre_push", lambda: self._push_andre(
                        f"vet-{app.creator_id}", {"reasons": decision.reasons}), done=lambda r: bool(r[0]))
                    out["andre_referral"] = {"delivered": delivered, "detail": detail}
                    self._record_result("andre_push_result", "intel_11_creator_vetting", app.creator_id, {"delivered": delivered},
                                        f"Clipper referral {'delivered to' if delivered else 'NOT delivered to'} Andre")
                elif decision.outcome == VettingOutcome.APPROVE:
                    # Approval triggers INSTANT activation (spec) — through the same gates.
                    try:
                        out["activation"] = self._activate_creator(rec)
                    except Conflict as exc:
                        out["activation"] = exc.body
            except LedgerWriteAfterEffects:
                rec.apply_digest, rec.apply_out = digest, out
                self.creators[app.creator_id] = rec
                raise
            self.creators[app.creator_id] = rec
            return out

    def _complete_apply(self, rec: CreatorRecord, digest: str) -> dict:
        """Retry of an application that stopped after an outside effect:
        write the owed result records (same ids) and finish the activation
        (the payout is never activated twice)."""
        self._flush_pending(rec.creator_id)
        out, same = rec.apply_out, digest == rec.apply_digest
        if rec.vetting.outcome == VettingOutcome.APPROVE and (rec.activation is None or not rec.activation.activated):
            try:
                out["activation"] = self._activate_creator(rec)
            except Conflict as exc:
                out["activation"] = exc.body
        rec.apply_digest = rec.apply_out = None
        if not same:
            raise Conflict("creator already applied (the earlier application's missing records are now written)")
        return out

    def creator_flag(self, creator_id: str, which: str, req: rq.CreatorFlagRequest) -> dict:
        with self._op("creator_flag"):
            rec = self._creator(creator_id)
            self._record(f"creator_{which}", "practice_p8_p9", rec.creator_id, {"received": req.received}, f"Creator {which}: {req.received}")
            if which == "w9":
                rec.w9_on_file = req.received
            elif which == "disclosure_training":
                rec.disclosure_training = req.received
            return {"creator_id": rec.creator_id, "w9_on_file": rec.w9_on_file, "disclosure_training": rec.disclosure_training}

    def activate_creator(self, creator_id: str) -> dict:
        with self._op("activate_creator"):
            return self._activate_creator(self._creator(creator_id))

    def _activate_creator(self, rec: CreatorRecord) -> dict:
        self._flush_pending(rec.creator_id)
        self._record("age_verification_request", "intel_15_compliance", rec.creator_id, {},
                     "18+ verification requested from Verification and Integrity")
        age = self.depts.verification.age_verified_18_plus(rec.creator_id)
        self._record("compliance_dept_request", "intel_15_compliance", rec.creator_id, {"lane": "zbc_creator"},
                     "Activation ruling requested from Compliance department (38)")
        dept = self.depts.compliance.rule(rec.creator_id, Lane.ZBC_CREATOR.value, {})
        facts = {
            "p1_disclosure_sent": rec.p1_disclosure_sent,
            "p1_wording_counsel_approved": self.config.p1_wording_counsel_approved,
            "vetting_approved": rec.vetting.outcome == VettingOutcome.APPROVE,
            "age_verified": age.allowed and rec.vetting.outcome == VettingOutcome.APPROVE,
            "w9_on_file": rec.w9_on_file,
            "disclosure_training": rec.disclosure_training,
            "compliance_dept_allowed": dept.allowed,
        }
        g14 = i14.creator_gate(rec.agreement_signed)
        g15 = i15.gate(Lane.ZBC_CREATOR, facts)

        def payout() -> tuple[Optional[dict], list[str]]:
            ok, why = creator_tax.payout_activation_allowed(rec.w9_on_file)
            if not ok:  # unreachable while w9_on_file is a gate-15 requirement; kept as a second lock
                return {"payout_account": False, "detail": why}, [f"payout/{why}"]
            if rec.payout_active:  # a retry after a partial failure: never activate twice
                return {"payout_account": True, "detail": "already active"}, []
            self._record("payout_activation_request", "intel_11_creator_vetting", rec.creator_id, {"w9_on_file": True},
                         "Approved clipper with W-9 on file -> payout account activation requested")
            r = self._effect("payout_account_activation", lambda: self.depts.payouts.activate_payout_account(rec.creator_id),
                             done=lambda x: x.allowed)
            rec.payout_active = r.allowed  # reality
            # Fix wave 3 (N7): owed until written; a retry (which takes the
            # "already active" path above) writes it first, same event id.
            self._record_result("payout_activation_ruling", "intel_11_creator_vetting", rec.creator_id, {"allowed": r.allowed},
                                f"Payout account {'active' if r.allowed else 'not activated'}")
            return {"payout_account": r.allowed, "detail": r.detail or "; ".join(r.unmet)}, [f"payout/{u}" for u in r.unmet]

        result = self._finish_activation(rec.creator_id, Lane.ZBC_CREATOR, g14, g15, payout, rec, "payout account activation")
        result["materials"] = {"tracking_link_ids": [self._derive_id("trk", rec.creator_id, 12)], "materials_pack": "issued",
                               "detail": "tracking-link and materials services are not wired; ids are issued only"}
        return result

    def creator_payment(self, creator_id: str, req: rq.CreatorPaymentRequest) -> dict:
        """Bug sweep D (M): idempotent on ``request_id`` (a replay answers the first result and records nothing; the
        same id with another amount is 409), and the 1099 total is kept per PERSON (``person_key``), not per creator
        id, and replayed from the local log at start (it survives a restart)."""
        with self._op("creator_payment"):
            prev = self._payment_rids.get(req.request_id)
            if prev is not None:
                # AEGIS O-3: the payment id alone is the key -- the same id under another creator is another payment
                if prev["creator_id"] != creator_id or Decimal(prev["amount_usd"]) != req.amount_usd:
                    raise Conflict("request_id already used for a different payment (another creator or amount); "
                                   "nothing was recorded")
                return dict(prev["status"])
            rec = self._creator(creator_id)
            # Owner ruling (fix wave 4, P1: refuse): payments are tracked only
            # for a creator whose activation is COMPLETE (gates passed and the
            # payout account active). A W-9 alone is not enough; a declined,
            # under-18 or not-yet-activated creator gets a 409 with the
            # reason and nothing is recorded.
            ok, why = creator_tax.payout_activation_allowed(rec.w9_on_file)
            if not ok:
                raise Conflict(why)
            if rec.activation is None or not rec.activation.activated or not rec.payout_active:
                reason = ("creator is not activated: payments are tracked only for a creator whose activation is "
                          "complete (vetting approved, gates 14 and 15 passed, payout account active)")
                if rec.vetting.outcome != VettingOutcome.APPROVE:
                    reason += f"; vetting outcome is {rec.vetting.outcome.value}"
                raise Conflict(reason, {"activated": False, "vetting_outcome": rec.vetting.outcome.value})
            # The tax year is the server's business date, never a caller date (F1 sweep).
            paid_on = self.business_date()
            year = paid_on.year
            pk = rec.person_key or person_key(rec.application.legal_name, rec.application.date_of_birth)
            paid = self._paid.get((pk, year), Decimal("0.00")) + req.amount_usd
            status = creator_tax.form_1099_status(year, paid, self.config.form_1099_thresholds_usd)
            payload = {"year": year, "recorded_on": paid_on.isoformat(), "amount_usd": money_str(req.amount_usd),
                       "paid_to_date_usd": status["paid_to_date_usd"], "form_1099_required": status["form_1099_required"],
                       "person_key": pk, "request_id": req.request_id}
            pay = {"creator_id": rec.creator_id, "request_id": req.request_id, "person_key": pk, "year": year,
                   "amount_usd": money_str(req.amount_usd), "status": scrub_obj(dict(status))}
            eid = self._event_id("creator_payment_tracked", rec.creator_id, payload)
            # AEGIS O-2: the intent (with the event id) is anchored and in the local log BEFORE the ledger record, so a
            # restart can count this payment exactly when the ledger holds it, whatever happens to the op's own line
            try:
                self.journal.commit("payment_intent", self.now().isoformat(), "creator_payment", [],
                                    {"payment_intent": {"request_id": req.request_id, "event_id": eid, "payment": pay}})
            except StoreWriteError as exc:
                raise LedgerWriteError(f"the payment intent could not be written to the local log ({exc}); nothing "
                                       "was recorded") from None
            self._write(eid, "creator_payment_tracked", "practice_p8_tax", rec.creator_id, payload,
                        f"1099 tracking {year}: {status['detail']}"[:280])
            # the payment is part of the operation's evidence line (replayed at start); applied when the op completes
            self._op_extra = {"payment": pay}
            self._apply_payment(pay)
            rec.payments.append((paid_on, req.amount_usd))
            return status

    def creator_post_check(self, creator_id: str, req: rq.CaptionRequest) -> dict:
        with self._op("creator_post_check"):
            rec = self._creator(creator_id)
            flags = self._flag_injection(rec.creator_id, req.caption, "creator_caption")
            ok, why = ad_disclosure.check_caption(req.caption)
            gok = True
            try:
                check_outbound(req.caption)
            except OutboundBlocked as exc:
                gok, why = False, f"{why}; also {exc}" if ok else f"{why}; also {exc}"
            self._record("ad_disclosure_check", "practice_p9_disclosure", rec.creator_id, {"allowed": ok and gok},
                         f"P9 pre-post disclosure check: {'allowed' if ok and gok else 'blocked'}")
            return {"allowed": ok and gok, "detail": why, "injection_flags": flags}

    # --- ZBC brand lane (12) ---------------------------------------------------------

    def plan_campaign(self, brand_id: str, req: rq.CampaignRequest) -> dict:
        with self._op("plan_campaign"):
            rec = self._client(brand_id)
            if rec.lane != Lane.ZBC_BRAND:
                raise Conflict("campaigns are for the ZBC brand lane")
            if (brand_id, req.campaign_id) in self.campaigns:
                raise Conflict("campaign already planned")
            plan = i12.plan_campaign(brand_id, req.campaign_id, req.regulated, req.wants_owned_addon, req.requested_budget_usd,
                                     self.config.proving_campaign_budget_cap_usd)
            digest = i12.plan_digest(plan)
            self._record("campaign_plan", "intel_12_brand_campaign", brand_id,
                         {"campaign_id": req.campaign_id, "distribution": plan.distribution.value, "owned_addon": plan.owned_addon_selected,
                          "digest": digest}, f"Campaign {req.campaign_id}: rented-first plan with proving campaign")
            self.campaigns[(brand_id, req.campaign_id)] = {"plan": plan, "result": None}
            return {"plan": _dump(plan), "plan_digest": digest, "phase": "phase 3 — not certified"}

    def approve_campaign(self, brand_id: str, campaign_id: str, req: rq.CampaignApproveRequest) -> dict:
        with self._op("approve_campaign"):
            rec = self._client(brand_id)
            c = self.campaigns.get((brand_id, campaign_id))
            if c is None:
                raise NotFound("unknown campaign")
            ok, why = i12.approve(c["plan"], req.brand_yes_campaign_id, req.plan_digest)
            unmet = [] if ok else [f"brand_yes: {why}"]
            if rec.activation is None or not rec.activation.activated:
                unmet.append("brand_account_activated: brand account has not passed the Contract (14) and Compliance (15) gates")
            self._record("campaign_approval_ruling", "intel_12_brand_campaign", brand_id,
                         {"campaign_id": campaign_id, "approved": not unmet, "unmet": unmet},
                         f"Campaign {campaign_id}: {'approved' if not unmet else 'not approved'}")
            if unmet:
                raise Conflict("campaign not approved", {"unmet": unmet})
            c["plan"] = c["plan"].model_copy(update={"approved": True})
            return {"approved": True, "plan": _dump(c["plan"])}

    def proving_result(self, brand_id: str, campaign_id: str, req: rq.ProvingResultRequest) -> dict:
        with self._op("proving_result"):
            self._client(brand_id)
            c = self.campaigns.get((brand_id, campaign_id))
            if c is None:
                raise NotFound("unknown campaign")
            result = req.model_dump()
            ok, why = i12.may_scale(c["plan"], result)
            self._record("campaign_proving_result", "intel_12_brand_campaign", brand_id,
                         {"campaign_id": campaign_id, "evidence": req.evidence, "may_scale": ok}, f"Proving result: {why}"[:280])
            c["result"] = result
            c["plan"] = c["plan"].model_copy(update={"scale_allowed": ok})
            return {"may_scale": ok, "detail": why}

    # --- playbook + learning --------------------------------------------------------------

    def propose_rules(self) -> dict:
        with self._lock:
            return {"proposals": i13.propose_rules(self.institutional.patterns()), "institutional_patterns": self.institutional.patterns()}

    def change_playbook(self, req: rq.PlaybookRuleRequest) -> dict:
        with self._op("change_playbook"):
            try:
                self.playbook.check_approval(req.rule_id, req.version, req.text, req.approval_token)
            except PlaybookApprovalError as exc:
                self._record("playbook_change_refused", "memory_playbook", "playbook", {"rule_id": req.rule_id, "version": req.version},
                             "Playbook change refused: no valid Andre approval")
                raise Refused(str(exc)) from None
            self._record("playbook_change", "memory_playbook", "playbook", {"rule_id": req.rule_id, "version": req.version, "text": req.text},
                         f"Playbook rule {req.rule_id} v{req.version} approved by Andre")
            rule = self.playbook.append(req.rule_id, req.version, req.text, self.now())
            return {"rule_id": rule.rule_id, "version": rule.version, "approved_at": rule.approved_at.isoformat()}

    def audit_evidence(self, limit: int = 200, offset: int = 0, event_type: Optional[str] = None) -> dict:
        """``GET /onboarding/audit/evidence`` (bug sweep D, D-1; bizdev-py's R6 view): every Onboarding event on the
        ledger, ``committed`` (named by an anchored local line, same type and payload hash) or ``attempted``
        (recorded first, never committed). Only the raw lines are copied under the service lock; parsing, hashing and
        the ledger read run outside it (bug sweep D, slow I/O under the lock)."""
        with self._lock:
            if self._closed:
                raise ServiceClosed("this onboarding instance is closed")
            raw = self.log.raw_lines()
        try:
            entries = self.ledger.entries()  # type: ignore[attr-defined]
        except (LedgerQueryFailed, AttributeError):
            raise UpstreamUnavailable("the evidence ledger could not be read") from None
        return self.journal.audit(raw, entries, limit, offset, event_type)

    def store_health(self) -> dict:
        """Lock-free by design: ``/health`` must answer while an operation holds the service lock."""
        return {"in_memory": self.log.in_memory, "log_lines": len(self.log), "evidence_lines_owed": len(self.journal.owed),
                "log_write_fault": bool(self.log.fault), "closed": self._closed}

    def verify_log(self) -> bool:
        """Re-verify the local chain from disk (outside the service lock; ``RecordLog`` has its own)."""
        return self.log.verify()

    def playbook_view(self) -> dict:
        with self._lock:
            return {"history": [{"rule_id": r.rule_id, "version": r.version, "text": r.text, "approved_at": r.approved_at.isoformat()}
                                for r in self.playbook.history]}
