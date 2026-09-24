"""
Onboarding service layer (ADR 0004).

The intelligences never call each other or another department. This layer
passes results between them, talks to the other departments through their
ports (integrations/*), and writes EVERY crossing and gate ruling to the
evidence ledger through ``LedgerClient`` (BUILD_CONTRACTS.md section 2).

Write-first discipline: for every operation, the decision is computed
first (pure), then the ledger records are written, THEN the crossing is
made and state changes. If a ledger write fails (``LedgerWriteError``), the
operation stops there, state is not changed, and the API answers 503 with
``proceeded: false``.

State is in-process memory (like fulfillment-py). Per-client state lives in
``ClientRecord`` objects keyed by client id and is only ever reached through
that id — one client's data never flows into another client's work.

Time comes only from the injected clock. No request can set "now".
"""

from __future__ import annotations

import logging
import threading
import uuid
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
from ledger import DEPARTMENT, LedgerClient, new_event_id
from memory import ClientMemoryStore, InstitutionalMemory, Playbook, PlaybookApprovalError
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
    Lane,
    MergedPlan,
    Person,
    Provenance,
    TriggerKind,
    VettingDecision,
    VettingOutcome,
    money_str,
    to_money,
)
from onboarding_schema import requests as rq
from practices import ad_disclosure, clean_exit, creator_tax, permission_receipt, recommend_score

log = logging.getLogger("onboarding.service")


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
    exited: bool = False
    injection_flags: int = 0


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
    payments: list = field(default_factory=list)  # (date, Decimal)


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
        self._lock = threading.RLock()

    # --- infrastructure --------------------------------------------------------

    def now(self) -> datetime:
        n = self.clock()
        if n.tzinfo is None:
            raise RuntimeError("service clock must be timezone-aware")
        return n

    def _record(self, event_type: str, actor: str, subject_id: str, payload: dict, summary: str) -> str:
        """Write one ledger event. Raises LedgerWriteError on failure — the
        caller must not proceed (it propagates to the API as 503)."""
        eid = new_event_id()
        self.ledger.record_event(eid, DEPARTMENT, event_type, actor, subject_id, payload, summary)
        return eid

    def _client(self, client_id: str) -> ClientRecord:
        rec = self.clients.get(client_id)
        if rec is None:
            raise NotFound("unknown client")
        if rec.exited:
            raise Conflict("client has exited (P4); no further onboarding actions")
        return rec

    def _creator(self, creator_id: str) -> CreatorRecord:
        rec = self.creators.get(creator_id)
        if rec is None:
            raise NotFound("unknown creator")
        return rec

    def _flag_injection(self, subject_id: str, text: str, source: str) -> list[dict]:
        """Client content is DATA. Flags are logged and recorded, and
        returned for the operator — no decision function reads them."""
        flags = scan_for_injection(text, source)
        if flags:
            payload = {"source": source, "rules": sorted({f.rule for f in flags})}
            self._record("injection_flagged", "guardrail_content_is_data", subject_id, payload,
                         f"Client content from {source} flagged as possible prompt injection; ignored")
            self._publish("injection_flagged", subject_id, payload)
        return [{"rule": f.rule, "source": f.source} for f in flags]

    def _publish(self, event_type: str, subject_id: str, payload: dict) -> None:
        self.bus.publish(DomainEvent(event_type=event_type, subject_id=subject_id, payload=payload, at=self.now()))

    # --- client lane: start -------------------------------------------------------

    def start_client(self, req: rq.StartClientRequest) -> dict:
        with self._lock:
            if req.client_id in self.clients:
                raise Conflict("client already onboarding")
            try:
                ZoneInfo(req.time_zone)
            except Exception as exc:  # noqa: BLE001
                raise Invalid("time_zone is not a valid IANA time zone") from exc
            if req.contract is not None and req.contract.client_id != req.client_id:
                raise Invalid("contract.client_id does not match client_id")
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

            self._record("onboarding_started", "onboarding_service", rec.client_id,
                         {"lane": rec.lane.value, "deal_size_usd": money_str(rec.deal_size) if rec.deal_size else None},
                         f"Onboarding started ({rec.lane.value} lane)")
            self._record("first_message_sent", "intel_02_conversation", rec.client_id,
                         {"ai_disclosure": True, "human_offered": True, "wording_status": "pending counsel"},
                         "P1 first message: AI identity disclosed, human offered")
            self._record("access_link_routed", "intel_04_platform_access", rec.client_id,
                         {"routed_to_login_holder": req.login_holder is not None},
                         "P22 secure access link routed to the login holder")
            self._record("deal_size_ruling", "intel_10_escalation_briefing", rec.client_id,
                         {"escalate": deal_escalate, "threshold_set": self.config.deal_size_threshold_usd is not None},
                         deal_reason)
            contract_status = "none supplied"
            if req.contract is not None:
                self._record("contract_storage_request", "intel_14_contract", rec.client_id,
                             {"services": req.contract.services, "signed": req.contract.signed},
                             "Store signed contract terms in contract storage")
                try:
                    self.depts.contracts.put(req.contract)
                    contract_status = "stored"
                except Exception as exc:  # noqa: BLE001  (stand-in refuses)
                    contract_status = f"not stored: {exc}"
                self._record("contract_storage_ruling", "intel_14_contract", rec.client_id,
                             {"stored": contract_status == "stored"}, f"Contract storage: {contract_status}"[:280])
            rec.p1_disclosure_sent = True
            escalation = None
            if deal_escalate:
                escalation = self._escalate(rec, TriggerKind.DEAL_SIZE, deal_reason, attempted=None, client_live=True)
            self.clients[rec.client_id] = rec
            self.memory.put(rec.client_id, "business_name", rec.business_name)
            return {
                "client_id": rec.client_id,
                "lane": rec.lane.value,
                "first_message": first_message,
                "first_message_wording_status": "pending counsel review (Cal. B&P Code 17941)",
                "access_link": {
                    "link_id": f"acl-{uuid.uuid4().hex[:16]}",
                    "send_to": holder.email,
                    "send_to_is_signer": req.login_holder is None,
                    "delivered": False,
                    "delivery_detail": "no delivery channel is wired (intake channel is an open item); the link id is issued, not sent",
                },
                "contract_storage": contract_status,
                "deal_size_ruling": {"escalate": deal_escalate, "reason": deal_reason},
                "escalation": _dump(escalation),
            }

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

    def _escalate(self, rec: ClientRecord, trigger: TriggerKind, snag: str, attempted: Optional[str], client_live: bool) -> Escalation:
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
        eid = f"esc-{uuid.uuid4().hex[:16]}"
        self._record("escalation_raised", "intel_10_escalation_briefing", rec.client_id,
                     {"escalation_id": eid, "trigger": trigger.value, "hard": hard, "snag": snag,
                      "attempted": attempted, "proposed_commitment": c.form, "proposed_due_at": c.due_at.isoformat()},
                     f"{'HARD' if hard else 'SOFT'} escalation: {trigger.value}")
        self._record("andre_push_request", "intel_10_escalation_briefing", rec.client_id,
                     {"escalation_id": eid, "briefing_fields": sorted(briefing.model_dump().keys())},
                     "Briefing pack pushed to Andre's phone before he engages")
        delivered, detail = self.depts.notifier.push(eid, briefing.model_dump(mode="json"))
        commitment_id = None
        if not delivered:
            status = ("held: briefing not delivered to Andre (" + detail + "); no commitment is made to the client "
                      "until Andre acknowledges")
        else:
            commitment_id = f"cmt-{uuid.uuid4().hex[:16]}"
            send_ok = client_live or not i09.in_quiet_hours(now, rec.time_zone, rec.quiet_start, rec.quiet_end)
            status = "released" if send_ok else "held: client quiet hours; send at the next allowed moment"
            self._record("client_commitment_made", "intel_09_promise_keeper", rec.client_id,
                         {"commitment_id": commitment_id, "escalation_id": eid, "form": c.form, "due_at": c.due_at.isoformat()},
                         f"Client told: {c.text}")
            rec.commitments[commitment_id] = Commitment(
                commitment_id=commitment_id, client_id=rec.client_id, kind="escalation_callback", category=c.form, text=c.text, owner="andre", created_at=now, due_at=c.due_at,
            )
        esc = Escalation(
            escalation_id=eid, client_id=rec.client_id, trigger=trigger, hard=hard, reason=snag, snag=snag,
            attempted_resolution=attempted, raised_at=now, briefing=briefing, push_delivered=delivered, push_detail=detail,
            client_commitment_text=c.text, client_commitment_due_at=c.due_at, client_message_status=status,
            commitment_id=commitment_id,
        )
        self.escalations[eid] = esc
        rec.escalation_ids.append(eid)
        return esc

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
        iid = f"iss-{uuid.uuid4().hex[:16]}"
        self._record("soft_trigger_attempt", "intel_02_conversation", rec.client_id,
                     {"issue_id": iid, "trigger": trigger.value, "snag": snag},
                     f"Soft trigger {trigger.value}: one resolution attempt before escalating")
        issue = SoftIssue(iid, trigger, snag, attempt, now)
        rec.soft_issues[iid] = issue
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
        with self._lock:
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

    def acknowledge_escalation(self, client_id: str, escalation_id: str) -> dict:
        with self._lock:
            rec = self._client(client_id)
            esc = self.escalations.get(escalation_id)
            if esc is None or esc.client_id != rec.client_id:
                raise NotFound("unknown escalation")
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
        with self._lock:
            rec = self._client(client_id)
            esc = self.escalations.get(escalation_id)
            if esc is None or esc.client_id != rec.client_id:
                raise NotFound("unknown escalation")
            if esc.resolved_at is not None:
                raise Conflict("already resolved")
            now = self.now()
            entry = self._learn(rec, esc.trigger, req.snag_category, esc.attempted_resolution, req.resolution, dry=True)
            self._record("escalation_resolved", "intel_13_learning_loop", rec.client_id,
                         {"escalation_id": escalation_id, "log": entry}, "Escalation resolved; snag and resolution logged")
            self.institutional.record(entry, self._known_names(rec))
            esc.resolution, esc.resolved_at = req.resolution, now
            if esc.acknowledged_at is None:
                esc.acknowledged_at = now
            if esc.commitment_id and esc.commitment_id in rec.commitments:
                cm = rec.commitments[esc.commitment_id]
                cm.engaged = True
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
            self.institutional.record(entry, self._known_names(rec))
        return entry

    def list_escalations(self) -> list[dict]:
        """Andre's queue: briefing packs, newest first. Needed because the
        push channel to his phone is not wired."""
        with self._lock:
            return [_dump(e) for e in sorted(self.escalations.values(), key=lambda e: e.raised_at, reverse=True)]

    # --- intake -------------------------------------------------------------------

    def add_facts(self, client_id: str, req: rq.FactsRequest) -> dict:
        with self._lock:
            rec = self._client(client_id)
            flags = []
            for f in req.facts:
                vals = f.value if isinstance(f.value, list) else [f.value]
                for v in vals:
                    if isinstance(v, str):
                        flags += self._flag_injection(rec.client_id, v, f"intake_fact:{f.field}")
            new = [i01.Fact(f.field, f.value, f.provenance, f.evidence, f.observed_at) for f in req.facts]
            rec.facts.extend(new)
            if req.vertical:
                rec.vertical = req.vertical
            for f in new:
                if f.field in rec.ask_counts:
                    rec.ask_counts.pop(f.field)
            rec.profile = i01.build_profile(rec.client_id, rec.lane, rec.facts, self.config.gaps_short_list_size)
            rec.last_progress_at = self.now()
            rec.injection_flags += len(flags)
            for f in new:
                if f.field in i01.LANE_FIELDS[rec.lane]:
                    self.memory.put(rec.client_id, f"fact:{f.field}", f.value)
            nq = i02.next_question(rec.profile, rec.vertical, rec.ask_counts)
            if nq is not None:
                rec.ask_counts[nq.field] = rec.ask_counts.get(nq.field, 0) + 1
            return {"profile": _dump(rec.profile), "next_question": nq.__dict__ if nq else None, "injection_flags": flags}

    def add_document(self, client_id: str, req: rq.DocumentRequest) -> dict:
        with self._lock:
            rec = self._client(client_id)
            flags = self._flag_injection(rec.client_id, req.text, "document")
            rec.injection_flags += len(flags)
            docs = self.memory.get(rec.client_id, "documents", [])
            docs.append({"name": req.name, "chars": len(req.text)})
            self.memory.put(rec.client_id, "documents", docs)
            return {"stored": True, "injection_flags": flags,
                    "extraction": "not built: turning a document into profile facts needs a model; facts must be entered via /intake/facts"}

    def message(self, client_id: str, req: rq.MessageRequest) -> dict:
        with self._lock:
            rec = self._client(client_id)
            flags = self._flag_injection(rec.client_id, req.text, "client_message")
            rec.injection_flags += len(flags)
            d = i02.decide_reply(req.text, self.config.spanish_enabled)
            if d.intent != "answer":
                self._record("client_intent_ruling", "intel_02_conversation", rec.client_id, {"intent": d.intent},
                             f"Client message classified as {d.intent}")
            msgs = self.memory.get(rec.client_id, "messages", [])
            msgs.append({"from": "client", "text": req.text})
            if d.reply:
                msgs.append({"from": "agent", "text": d.reply})
            self.memory.put(rec.client_id, "messages", msgs)
            escalation = None
            if d.intent == "human_request":
                escalation = self._escalate(rec, TriggerKind.HUMAN_REQUESTED, "Client asked to talk to a human", attempted=None, client_live=True)
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
        with self._lock:
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
            self.memory.put(rec.client_id, "recaps", recaps)
            return {"recap": text}

    # --- platform access ------------------------------------------------------------

    def website_scan(self, client_id: str, req: rq.WebsiteScanRequest) -> dict:
        with self._lock:
            rec = self._client(client_id)
            flags = self._flag_injection(rec.client_id, req.html, "website")
            rec.injection_flags += len(flags)
            tags = i04.scan_tags(req.html)
            rec.detected_tags = [t["platform"] for t in tags]
            today = self.now().date()
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
                         {"platforms": rec.detected_tags, "quotable": {p["platform"]: p["quotable"] for p in plans}},
                         f"Tag scan found {len(tags)} platform tag(s)")
            rec.last_progress_at = self.now()
            return {"detected": tags, "access_requests": plans, "injection_flags": flags,
                    "site_fetch": "not built: the caller supplies the page HTML"}

    def add_grant(self, client_id: str, grant: AccessGrantIn) -> dict:
        with self._lock:
            rec = self._client(client_id)
            now = self.now()
            self._record("platform_probe_request", "intel_04_platform_access", rec.client_id,
                         {"platform": grant.platform.value, "account_type": grant.account_type.value, "role": grant.granted_role},
                         f"P21 live check requested for {grant.platform.value} grant")
            live_ok, live_detail = self.probe.check(grant.platform.value, grant.account_id)
            v: AccessVerification = i04.verify_grant(grant, now, self.config.stale_account_days, live_ok, live_detail, self.knowledge)
            receipt, quotable, why = (None, False, "grant not usable")
            if v.usable:
                receipt, quotable, why = permission_receipt.build_receipt(grant, now.date(), self.config.platform_fact_shelf_life_days, self.knowledge)
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
        with self._lock:
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
        with self._lock:
            rec = self._client(client_id)
            kinds = sorted(k for k, v in req.account_data.items() if v)
            self._record("revenue_recovery_request", "intel_06_audit_baseline", rec.client_id,
                         {"data_kinds": kinds, "rows": sum(len(v) for v in req.account_data.values())},
                         "Account pull sent to Revenue Recovery detection")
            try:
                raw = self.rr.detect(req.account_data)
                overlaps = self.rr.overlaps(raw)
            except RevenueRecoveryError as exc:
                raise UpstreamUnavailable(f"Revenue Recovery unavailable: {exc}; no audit recorded") from None
            findings, rejected = i06.consume(raw, overlaps)
            base = i06.baseline(findings)
            stated = self._stated_revenue(rec)
            risk = i08.assess(req.risk_signals, stated, req.observed_monthly_revenue_usd, self.config.revenue_mismatch_tolerance)
            self._record("revenue_recovery_ruling", "intel_06_audit_baseline", rec.client_id,
                         {"finding_ids": [f.finding_id for f in findings], "rejected": rejected,
                          "totals_by_classification": {k: money_str(v) for k, v in base.totals_by_classification.items()}},
                         f"Revenue Recovery returned {len(findings)} finding(s), {len(rejected)} rejected")
            self._record("risk_ruling", "intel_08_risk_anomaly", rec.client_id, {"kind": risk.kind, "reasons": list(risk.reasons)},
                         f"Risk and Anomaly: {risk.kind}")
            if risk.kind != "nothing":
                self._record("risk_event_published", "intel_08_risk_anomaly", rec.client_id, {"kind": risk.kind},
                             "Anomaly event published to the risk watcher / AEGIS (consumers not built)")
                self._publish("risk_anomaly", rec.client_id, {"kind": risk.kind, "reasons": list(risk.reasons)})
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
        try:
            return to_money(pf.value)
        except ValueError:
            return None

    # --- plan (3), setup (5), momentum (7), recommend (P20) ----------------------------

    def plan(self, client_id: str, req: rq.PlanRequest) -> dict:
        with self._lock:
            rec = self._client(client_id)
            if rec.baseline is None:
                raise Conflict("no audit baseline yet; the merged plan needs what we found AND what the client said")
            plan = i03.fuse(rec.client_id, rec.findings, req.client_priorities)
            self._record("merged_plan", "intel_03_priority_fusion", rec.client_id,
                         {"order": [i.topic for i in plan.items], "disagreements": [d.topic for d in plan.disagreements]},
                         f"Merged plan: {len(plan.items)} item(s), {len(plan.disagreements)} disagreement(s) shown to client")
            rec.client_priorities, rec.plan, rec.plan_choices = list(req.client_priorities), plan, {}
            self.memory.put(rec.client_id, "client_priorities", list(req.client_priorities))
            rec.last_progress_at = self.now()
            return _dump(plan)

    def plan_choice(self, client_id: str, req: rq.PlanChoiceRequest) -> dict:
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            rec = self._client(client_id)
            p = i07.pick(rec.findings)
            self._record("momentum_ruling", "intel_07_momentum_moment", rec.client_id,
                         {"finding_id": p.finding_id, "rejected": p.rejected}, f"Momentum Moment: {p.reason}"[:280])
            return {"finding_id": p.finding_id, "score": p.score, "reason": p.reason, "rejected": p.rejected,
                    "phase": "phase 2 scoring — not certified for real clients"}

    def first_win(self, client_id: str, req: rq.FirstWinRequest) -> dict:
        with self._lock:
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
        with self._lock:
            rec = self._client(client_id)
            if rec.first_win_at is None or not rec.recommend_asked:
                raise Conflict("the recommend score is asked only right after the first real win; not asked yet")
            if rec.recommend_score is not None:
                raise Conflict("score already recorded")
            path, text = recommend_score.route(req.score)
            self._record("recommend_score", "intel_13_learning_loop", rec.client_id, {"score": req.score, "path": path},
                         f"P20 recommend score {req.score}: {path}")
            rec.recommend_score = req.score
            out = {"path": path, "client_message": text, "soft_issue": None, "escalation": None}
            if path == "soft_escalation":
                r = self._soft_trigger(rec, TriggerKind.LOW_RECOMMEND_SCORE, f"Recommend score {req.score}", text)
                out.update({"soft_issue": r.get("soft_issue"), "escalation": r.get("escalation")})
            return out

    # --- promise keeper + stuck (tick) ---------------------------------------------------

    def tick(self, client_id: str) -> dict:
        with self._lock:
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
                    rec.stalls += 1
                out["stuck"] = {"reason": s.reason, **r}
            for c in list(rec.commitments.values()):
                actions = i09.decide(c, now, rec.time_zone, rec.quiet_start, rec.quiet_end, cfg.commitment_cutoff_local,
                                     cfg.commitment_tz, cfg.today_due_local, cfg.first_thing_local,
                                     cfg.andre_nudge_lead_hours, cfg.client_warn_lead_hours)
                for a in actions:
                    if a.action == "none":
                        continue
                    self._record(f"promise_{a.action}", "intel_09_promise_keeper", rec.client_id,
                                 {"commitment_id": c.commitment_id, "send_at": a.send_at.isoformat() if a.send_at else None,
                                  "new_due_at": a.new_due_at.isoformat() if a.new_due_at else None,
                                  "quiet_hours_override": a.quiet_hours_override},
                                 f"Promise Keeper: {a.action} ({a.reason})"[:280])
                    if a.action == "nudge_andre":
                        self.depts.notifier.push(f"nudge-{c.commitment_id}", {"commitment": c.text, "due_at": c.due_at.isoformat(), "reason": a.reason})
                        c.andre_nudged = True
                    elif a.action == "warn_client":
                        # The client has been told the new real time BEFORE the
                        # old one passed: the commitment now tracks the new time,
                        # with fresh nudge/warn state for it.
                        c.status = CommitmentStatus.RESCHEDULED
                        c.proposed_new_due_at = a.new_due_at
                        c.due_at = a.new_due_at
                        c.andre_nudged = False
                        c.client_warned = False
                        c.category = "first_thing_tomorrow"
                    elif a.action == "breached":
                        c.status = CommitmentStatus.BREACHED
                    out["commitment_actions"].append({"commitment_id": c.commitment_id, "action": a.action, "reason": a.reason,
                                                      "send_at": a.send_at.isoformat() if a.send_at else None,
                                                      "client_message": a.message, "new_due_at": a.new_due_at.isoformat() if a.new_due_at else None,
                                                      "quiet_hours_override": a.quiet_hours_override})
            return out

    # --- health (13), view, memory, exit ----------------------------------------------

    def health(self, client_id: str) -> dict:
        with self._lock:
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
        with self._lock:
            rec = self.clients.get(client_id)
            if rec is None:
                raise NotFound("unknown client")
            self._record("client_memory_deleted", "memory_client", rec.client_id, {}, "Client requested deletion of their memory (P11)")
            deleted = self.memory.delete(rec.client_id)
            return {"deleted": deleted}

    def exit(self, client_id: str, req: rq.ExitRequest) -> dict:
        with self._lock:
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
        with self._lock:
            rec = self._client(client_id)
            today = self.now().date()
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
                self._record("activation_handoff_request", "onboarding_service", rec.client_id, {"lane": rec.lane.value},
                             "Full client file handed to the receiving department")
                ruling = self.depts.handoff.accept(self._client_file(rec))
                self._record("activation_handoff_ruling", "onboarding_service", rec.client_id, {"accepted": ruling.allowed},
                             f"Handoff {'accepted' if ruling.allowed else 'not accepted'}")
                info = {"accepted": ruling.allowed, "detail": ruling.detail or "; ".join(ruling.unmet)}
                return info, ([] if ruling.allowed else [f"handoff/{u}" for u in ruling.unmet])

            return self._finish_activation(rec.client_id, rec.lane, g14, g15, handoff, rec)

    def _client_file(self, rec: ClientRecord) -> dict:
        return {
            "client_id": rec.client_id, "lane": rec.lane.value, "profile": _dump(rec.profile),
            "verified_access": [p for p, v in rec.verifications.items() if v.usable],
            "merged_plan": _dump(rec.plan),
            "open_commitments": [_dump(c) for c in rec.commitments.values() if c.status != CommitmentStatus.KEPT],
        }

    def _finish_activation(self, subject_id: str, lane: Lane, g14, g15, after_gates: Callable[[], tuple], rec) -> dict:
        """Both gates are recorded. Only if BOTH pass does the post-gate
        crossing (client handoff / creator payout activation) happen."""
        self._record("contract_gate_ruling", "intel_14_contract", subject_id, {"passed": g14.passed, "unmet": g14.unmet, "drift": g14.drift},
                     f"Contract gate (14): {'passed' if g14.passed else f'{len(g14.unmet)} unmet'}")
        self._record("compliance_gate_ruling", "intel_15_compliance", subject_id, {"passed": g15.passed, "unmet": g15.unmet},
                     f"Compliance gate (15): {'passed' if g15.passed else f'{len(g15.unmet)} unmet'}")
        unmet = [f"contract_14/{u}" for u in g14.unmet] + [f"compliance_15/{u}" for u in g15.unmet]
        handoff = None
        if g14.passed and g15.passed:
            handoff, extra = after_gates()
            unmet += extra
        activated = not unmet
        decision = ActivationDecision(subject_id=subject_id, lane=lane, activated=activated, contract=g14, compliance=g15,
                                      unmet=unmet, handoff=handoff)
        self._record("activation_ruling", "intel_15_compliance", subject_id, {"activated": activated, "unmet": unmet},
                     f"Activation {'allowed' if activated else f'blocked: {len(unmet)} requirement(s) unmet'}")
        rec.activation = decision
        if not activated:
            raise Conflict("activation blocked", _dump(decision))
        return _dump(decision)

    # --- ZBC creator lane (11, P8, P9) ----------------------------------------------------

    def apply_creator(self, app: ClipperApplication) -> dict:
        with self._lock:
            if app.creator_id in self.creators:
                raise Conflict("creator already applied")
            flags = self._flag_injection(app.creator_id, app.bio, "creator_bio")
            decision = i11.vet(app)
            decision = decision.model_copy(update={"injection_flags": [f["rule"] for f in flags]})
            first = ai_disclosure_first_message(app.legal_name.split()[0], company="ZBC")
            self._record("first_message_sent", "intel_02_conversation", app.creator_id,
                         {"ai_disclosure": True, "human_offered": True}, "P1 first message to clipper applicant")
            self._record("vetting_ruling", "intel_11_creator_vetting", app.creator_id,
                         {"outcome": decision.outcome.value, "reasons": decision.reasons},
                         f"Creator Vetting: {decision.outcome.value}")
            referral = None
            if decision.outcome == VettingOutcome.SEND_TO_ANDRE:
                self._record("andre_push_request", "intel_11_creator_vetting", app.creator_id, {"reasons": decision.reasons},
                             "Clipper application sent to Andre with written reasons")
                delivered, detail = self.depts.notifier.push(f"vet-{app.creator_id}", {"reasons": decision.reasons})
                referral = {"delivered": delivered, "detail": detail}
            rec = CreatorRecord(app.creator_id, app, decision, True, app.w9_received, app.disclosure_training_completed,
                                app.creator_agreement_signed)
            self.creators[app.creator_id] = rec
            message = {
                VettingOutcome.APPROVE: "You're approved. We're setting up your materials, tracking links and payment details now.",
                VettingOutcome.DECLINE: ("Thank you for applying. We can't accept this application. "
                                         + ("Clippers must be 18 or older." if any("under 18" in r for r in decision.reasons) else "")).strip(),
                VettingOutcome.INCOMPLETE: "Thanks for applying. We need your date of birth to continue; clippers must be 18 or older.",
                VettingOutcome.SEND_TO_ANDRE: "Thanks for applying. A person on our team is reviewing your application.",
            }[decision.outcome]
            out = {"first_message": first, "vetting": _dump(decision), "applicant_message": check_outbound(message),
                   "andre_referral": referral, "activation": None}
            if decision.outcome == VettingOutcome.APPROVE:
                # Approval triggers INSTANT activation (spec) — through the same gates.
                try:
                    out["activation"] = self._activate_creator(rec)
                except Conflict as exc:
                    out["activation"] = exc.body
            return out

    def creator_flag(self, creator_id: str, which: str, req: rq.CreatorFlagRequest) -> dict:
        with self._lock:
            rec = self._creator(creator_id)
            self._record(f"creator_{which}", "practice_p8_p9", rec.creator_id, {"received": req.received}, f"Creator {which}: {req.received}")
            if which == "w9":
                rec.w9_on_file = req.received
            elif which == "disclosure_training":
                rec.disclosure_training = req.received
            return {"creator_id": rec.creator_id, "w9_on_file": rec.w9_on_file, "disclosure_training": rec.disclosure_training}

    def activate_creator(self, creator_id: str) -> dict:
        with self._lock:
            return self._activate_creator(self._creator(creator_id))

    def _activate_creator(self, rec: CreatorRecord) -> dict:
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
            self._record("payout_activation_request", "intel_11_creator_vetting", rec.creator_id, {"w9_on_file": True},
                         "Approved clipper with W-9 on file -> payout account activation requested")
            r = self.depts.payouts.activate_payout_account(rec.creator_id)
            self._record("payout_activation_ruling", "intel_11_creator_vetting", rec.creator_id, {"allowed": r.allowed},
                         f"Payout account {'active' if r.allowed else 'not activated'}")
            return {"payout_account": r.allowed, "detail": r.detail or "; ".join(r.unmet)}, [f"payout/{u}" for u in r.unmet]

        result = self._finish_activation(rec.creator_id, Lane.ZBC_CREATOR, g14, g15, payout, rec)
        result["materials"] = {"tracking_link_ids": [f"trk-{uuid.uuid4().hex[:12]}"], "materials_pack": "issued",
                               "detail": "tracking-link and materials services are not wired; ids are issued only"}
        return result

    def creator_payment(self, creator_id: str, req: rq.CreatorPaymentRequest) -> dict:
        with self._lock:
            rec = self._creator(creator_id)
            ok, why = creator_tax.payout_activation_allowed(rec.w9_on_file)
            if not ok:
                raise Conflict(why)
            year = req.paid_on.year
            paid = sum((a for d, a in rec.payments if d.year == year), Decimal("0.00")) + req.amount_usd
            status = creator_tax.form_1099_status(year, paid, self.config.form_1099_thresholds_usd)
            self._record("creator_payment_tracked", "practice_p8_tax", rec.creator_id,
                         {"year": year, "paid_to_date_usd": status["paid_to_date_usd"], "form_1099_required": status["form_1099_required"]},
                         f"1099 tracking {year}: {status['detail']}"[:280])
            rec.payments.append((req.paid_on, req.amount_usd))
            return status

    def creator_post_check(self, creator_id: str, req: rq.CaptionRequest) -> dict:
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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

    def playbook_view(self) -> dict:
        with self._lock:
            return {"history": [{"rule_id": r.rule_id, "version": r.version, "text": r.text, "approved_at": r.approved_at.isoformat()}
                                for r in self.playbook.history]}
