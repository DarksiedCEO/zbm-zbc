"""
ZBM workflow — sequencing, evidence and gates. It makes NO creative
judgement itself; every judgement is delegated to one intelligence module.

brief draft (1+3+4) -> Creative Lead approval (2) -> production job
(commission Enigma/Phantom Canvas, contract only) -> work submitted ->
Export Validator (4) -> Rights and Provenance (5) -> Creative Quality (8,
2-round cap, then Andre) -> Compliance (38) hard gate -> Andre final
approval (founder token).

Evidence rule: every approval, rejection, signature and cross-department
crossing is recorded through the ledger FIRST; state changes only after
the record succeeds, and no outside department is called before the
request to it is on the ledger (`RecordedPort`, or the intent record that
lists the requests, e.g. `production_opened`). If the ledger call fails,
`LedgerRecordError` propagates and nothing changed (the API returns 503
and says so). Server-assigned ids are consumed only when the record
succeeds, so a retry of a failed step reuses them (deterministic event ids).

Review cap (spec: "TWO rounds, then escalate"): rounds are counted per
REVIEW CHAIN = (brief_id, deliverable_id, variant_index) — one deliverable
variant of one approved brief — across EVERY job opened on that brief. A
new job never resets the count. While any work of a brief is escalated to
Andre and unresolved, no new job and no new work submission on that brief
is accepted; only Andre, with his own token, resolves it. A chain that was
escalated never gets a third round, whatever Andre decided.

Review cap per CLIENT DELIVERABLE (fix wave 2, N4; fix wave 4, F8): the
brief id is not the unit — a cloned brief is the same order. Every
deliverable has a DELIVERABLE KEY = (client, spec, variant_index):
- client: the client id, which is validated strictly at creation
  (lowercase [a-z0-9_] only, shared.types.ClientId) so "Client_Acme" or
  "client_acme." can't exist, and is additionally compared NORMALISED
  (`normalize_client_id`: NFKC, casefold, punctuation/whitespace/underscores
  removed);
- spec: {platform, placement, aspect_ratio reduced to lowest terms
  ("18:32" -> "9:16"), length_seconds}. Two specs are the SAME deliverable
  when platform, placement and reduced ratio are equal and their lengths are
  within 2 x the export tolerance (placement_spec.LENGTH_TOLERANCE_SECONDS,
  0.5 s) of each other — i.e. one render could pass export validation for
  both (a 30.5 s render fits a 30 s and a 31 s spec). The FORMAT is not part
  of it: re-wrapping the same cut (mp4 -> m4v) is the same deliverable.
  The deliverable_id, count, brief id and job id are not part of it either.
Rules:
- rounds are counted per deliverable key across every brief of that
  client; a pass closes the count (the next order starts at round 1);
- only one version per deliverable key is in flight (submitted / export
  passed / rights cleared) at a time, across all briefs;
- while work on a key is escalated to Andre and unresolved, any brief of
  that client with a deliverable matching that spec (any variant) is
  refused (409) at draft, approval, job opening and work submission;
- Andre's decision (his token) resolves it; after that a NEW brief for
  the same deliverable starts a fresh count; the escalated brief's own
  chain still never gets a third round.
`deliverable_fingerprint` (SHA-256 of the canonical spec) is only a label
for messages; matching never compares fingerprints.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from math import gcd

from pydantic import BaseModel, ConfigDict, Field

from shared.actors import ActorRegistry, Role
from shared.clock import Clock
from shared.departments import CommissionRequest, Departments
from shared.errors import CreativeError, NotFound, PreconditionFailed
from shared.founder import FOUNDER_ACTOR, FounderGate
from shared.ledger import (
    EvidenceRecorder,
    LedgerRecordError,
    OutcomeNotRecorded,
    PendingCreations,
    RecordedPort,
    content_sha256,
    serialized,
)
from shared.media import C2paStandIn
from shared.registry import PlatformRulesRegistry
from shared.rights import RightsRegistry
from shared.types import SafeId
from zbm import brief_writer, creative_lead, creative_quality, placement_spec, rights_provenance
from zbm.brief import BriefRecord, BriefStatus, Deliverable
from zbm.brief_writer import ClientRequirements
from zbm.creative_memory import MemoryDecision, ZbmCreativeMemory
from zbm.creative_quality import QualityDeclaration
from zbm.placement_spec import DeclaredExport
from zbm.results import PerformanceResult


class WorkStage(str, Enum):
    SUBMITTED = "submitted"
    EXPORT_FAILED = "export_failed"
    EXPORT_PASSED = "export_passed"
    RIGHTS_BLOCKED = "rights_blocked"
    RIGHTS_CLEARED = "rights_cleared"
    SENT_BACK = "sent_back"
    ESCALATED_TO_ANDRE = "escalated_to_andre"
    ESCALATION_ACCEPTED = "escalation_accepted_by_andre"
    KILLED_BY_ANDRE = "killed_by_andre"
    QUALITY_PASSED = "quality_passed"
    COMPLIANCE_BLOCKED = "compliance_blocked"
    COMPLIANCE_PASSED = "compliance_passed"
    APPROVED_BY_ANDRE = "approved_by_andre"


REWORK_STAGES = frozenset({WorkStage.EXPORT_FAILED, WorkStage.RIGHTS_BLOCKED, WorkStage.SENT_BACK})
IN_FLIGHT_STAGES = frozenset({WorkStage.SUBMITTED, WorkStage.EXPORT_PASSED, WorkStage.RIGHTS_CLEARED})


def _ratio(aspect_ratio: str) -> str:
    w, h = (int(x) for x in aspect_ratio.split(":"))
    g = gcd(w, h) or 1
    return f"{w // g}:{h // g}"


def normalize_client_id(client_id: str) -> str:
    """Client id as compared (F8): NFKC, casefold, only letters and digits."""
    import unicodedata

    return "".join(ch for ch in unicodedata.normalize("NFKC", client_id or "").casefold() if ch.isalnum())


def deliverable_spec(d: Deliverable) -> tuple:
    """(platform, placement, reduced aspect ratio, length_seconds) — format excluded."""
    return (d.platform.strip().lower(), d.placement.strip().lower(), _ratio(d.aspect_ratio), float(d.length_seconds))


def same_spec(a: tuple, b: tuple) -> bool:
    """One render could pass export validation for both specs (see the module docstring)."""
    return a[:3] == b[:3] and abs(a[3] - b[3]) <= 2 * placement_spec.LENGTH_TOLERANCE_SECONDS


def deliverable_fingerprint(d: Deliverable) -> str:
    """Label for messages only (N4) — matching uses `same_spec`."""
    spec = {"platform": d.platform.strip().lower(), "placement": d.placement.strip().lower(),
            "length_seconds": int(d.length_seconds), "aspect_ratio": _ratio(d.aspect_ratio),
            "format": d.format.strip().lower()}
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ProductionJob(BaseModel):
    job_id: str
    brief_id: str
    commissions: list[dict] = []
    ledger_event_ids: list[str] = []


class WorkSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deliverable_id: SafeId
    variant_index: int = 0
    declared: DeclaredExport
    asset_ids: list[SafeId] = Field(max_length=500)
    uses_ai_generative_fill: bool = False
    quality: QualityDeclaration


class WorkItem(BaseModel):
    work_id: str
    job_id: str
    brief_id: str
    submission: WorkSubmission
    round: int
    stage: WorkStage
    export_validation: dict | None = None
    rights: dict | None = None
    quality_decision: dict | None = None
    compliance: dict | None = None
    final_approval: dict | None = None
    ledger_event_ids: list[str] = []


@dataclass
class ZbmWorkflow:
    registry: PlatformRulesRegistry
    rights: RightsRegistry
    actors: ActorRegistry
    recorder: EvidenceRecorder
    clock: Clock
    departments: Departments
    founder: FounderGate
    memory: ZbmCreativeMemory = field(default_factory=ZbmCreativeMemory)
    briefs: dict[str, BriefRecord] = field(default_factory=dict)
    jobs: dict[str, ProductionJob] = field(default_factory=dict)
    work: dict[str, WorkItem] = field(default_factory=dict)
    _rounds_used: dict[tuple, int] = field(default_factory=dict)
    _escalated_chains: set = field(default_factory=set)
    _n: int = 1
    _pending: PendingCreations = field(default_factory=PendingCreations)  # LOST sweep

    def _peek(self, prefix: str) -> str:
        """The next id, NOT yet consumed: `_consume()` only after the record
        succeeded — or after it failed with an UNKNOWN outcome (the ledger
        may hold it), see shared.ledger.PendingCreations."""
        return f"{prefix}-{self._n:04d}"

    def _new_id(self, kind: str, sha: str, prefix: str) -> str:
        held = self._pending.get(kind, sha)
        return held[0] if held is not None else self._peek(prefix)

    def _consume(self) -> None:
        self._n += 1

    @staticmethod
    def _chain(brief_id: str, deliverable_id: str, variant_index: int) -> tuple:
        return (brief_id, deliverable_id, variant_index)

    def _key(self, brief: BriefRecord, deliverable_id: str, variant_index: int) -> tuple:
        """Deliverable key (N4/F8): (normalised client, spec, variant_index)."""
        return (normalize_client_id(brief.client_id), deliverable_spec(self._deliverable(brief, deliverable_id)),
                variant_index)

    @staticmethod
    def _same_key(a: tuple, b: tuple) -> bool:
        return a[0] == b[0] and a[2] == b[2] and same_spec(a[1], b[1])

    def _work_key(self, w: WorkItem) -> tuple:
        return self._key(self.get_brief(w.brief_id), w.submission.deliverable_id, w.submission.variant_index)

    def _rounds_for(self, key: tuple) -> int:
        return max((n for k, n in self._rounds_used.items() if self._same_key(k, key)), default=0)

    def _clear_rounds(self, key: tuple) -> None:
        for k in [k for k in self._rounds_used if self._same_key(k, key)]:
            del self._rounds_used[k]

    def _escalated_specs(self, client_id: str) -> list[tuple[tuple, str]]:
        """(spec, work id) of this client's work escalated to Andre and unresolved."""
        c = normalize_client_id(client_id)
        out = []
        for w in self.work.values():
            if w.stage is WorkStage.ESCALATED_TO_ANDRE:
                wc, spec, _ = self._work_key(w)
                if wc == c:
                    out.append((spec, w.work_id))
        return out

    def _refuse_if_deliverable_escalated(self, client_id: str, deliverables, subject: str, action: str) -> None:
        blocked = self._escalated_specs(client_id)
        if not blocked:
            return
        hits = [(d.deliverable_id, wid) for d in deliverables for spec, wid in blocked
                if same_spec(deliverable_spec(d), spec)]
        if hits:
            self._refuse(PreconditionFailed(
                f"client {client_id}: deliverable(s) {', '.join(sorted({d for d, _ in hits}))} match the spec of work "
                f"escalated to Andre after {creative_quality.MAX_ROUNDS} review rounds "
                f"({', '.join(sorted({w for _, w in hits}))}) — same platform, placement and aspect ratio, length "
                f"within {2 * placement_spec.LENGTH_TOLERANCE_SECONDS:g}s, any format; a new or cloned brief for "
                "that deliverable is refused until Andre resolves the escalation with his approval token"),
                "zbm_creative_lead", subject, action)

    def _open_escalations(self, brief_id: str) -> list[WorkItem]:
        return [w for w in self.work.values() if w.brief_id == brief_id and w.stage is WorkStage.ESCALATED_TO_ANDRE]

    def _refuse_if_escalated(self, brief_id: str, action: str) -> None:
        pending = self._open_escalations(brief_id)
        if pending:
            self._refuse(PreconditionFailed(
                f"brief {brief_id} has work escalated to Andre after {creative_quality.MAX_ROUNDS} review rounds "
                f"({', '.join(w.work_id for w in pending)}); no new job or review round on this brief until Andre "
                "resolves it with his approval token"), "zbm_creative_lead", brief_id, action)

    # --- guardrail refusals are recorded best-effort, and always stand ------
    def _refuse(self, exc: CreativeError, actor: str, subject_id: str, action: str) -> None:
        self.recorder.try_record("guardrail_refusal", actor if actor else "unknown", subject_id,
                                 {"action": action, "reason": exc.reason}, f"Refused {action}: {exc.reason}")
        raise exc

    # --- briefs -----------------------------------------------------------------
    def get_brief(self, brief_id: str) -> BriefRecord:
        b = self.briefs.get(brief_id)
        if b is None:
            raise NotFound(f"brief {brief_id!r} not found")
        return b

    @serialized
    def draft_brief(self, req: ClientRequirements, actor_id: str = brief_writer.WRITER_ACTOR) -> BriefRecord:
        self.actors.require_role(actor_id, Role.ZBM_BRIEF_WRITER)
        sha = content_sha256({"actor": actor_id, "requirements": req.model_dump(mode="json")})
        brief_id = self._new_id("brief", sha, "brief")
        rec = brief_writer.draft(req, brief_id, self.registry, self.clock.today(), drafted_by=actor_id)
        if rec.fields is not None:
            self._refuse_if_deliverable_escalated(rec.client_id, rec.fields.deliverables, brief_id, "brief draft")
        eid, rec = self._pending.record(self.recorder, "brief", sha, brief_id, rec, self._consume,
                                        "brief_drafted", actor_id, brief_id,
                                        rec.model_dump(mode="json", exclude={"ledger_event_ids"}),
                                        f"Brief {brief_id} drafted for {req.client_id}: {rec.status.value}")
        rec = rec.model_copy(update={"ledger_event_ids": [*rec.ledger_event_ids, eid]})
        self.briefs[brief_id] = rec
        return rec

    @serialized
    def review_brief(self, brief_id: str, approver_id: str) -> BriefRecord:
        b = self.get_brief(brief_id)
        try:
            result = creative_lead.review(b, approver_id, self.actors, self.registry, self.clock.today())
        except CreativeError as exc:
            self._refuse(exc, approver_id if _is_actor_like(approver_id) else "unknown", brief_id, "brief approval")
        approved = result.outcome == "approved"
        if approved:
            self._refuse_if_deliverable_escalated(b.client_id, b.fields.deliverables, brief_id, "brief approval")
        eid = self.recorder.record(
            "brief_approved" if approved else "brief_sent_back", approver_id, brief_id,
            {"outcome": result.outcome, "issues": result.issues, "spec_row_ids": result.spec_row_ids},
            f"Brief {brief_id} {result.outcome} by {approver_id}" + ("" if approved else f" ({len(result.issues)} issue(s))"),
        )
        updated = b.model_copy(update={
            "status": BriefStatus.APPROVED if approved else BriefStatus.SENT_BACK,
            "approved_by": approver_id if approved else None,
            "approved_at": self.clock.now() if approved else None,
            "review_issues": result.issues,
            "spec_row_ids": result.spec_row_ids or b.spec_row_ids,
            "ledger_event_ids": [*b.ledger_event_ids, eid],
        })
        self.briefs[brief_id] = updated
        return updated

    # --- production ------------------------------------------------------------------
    @serialized
    def open_job(self, brief_id: str) -> ProductionJob:
        """Record-first (F9): 1. `production_opened` lists every commission
        request; 2. the job is committed with those requests `requested`;
        3. only then are Enigma / Phantom Canvas called (request ids are
        deterministic, so the external contract can de-duplicate);
        4. their answers are recorded (`crossing_creative_agents`) and only
        then applied. A failure at 1 changes nothing and calls no one; a
        failure at 4 is reported as `took_effect: "partial"`."""
        b = self.get_brief(brief_id)
        if b.status is not BriefStatus.APPROVED or b.fields is None:
            self._refuse(PreconditionFailed(
                f"brief {brief_id} is {b.status.value}; nothing enters production without an approved brief"),
                "zbm_creative_lead", brief_id, "production start")
        self._refuse_if_escalated(brief_id, "production start")
        self._refuse_if_deliverable_escalated(b.client_id, b.fields.deliverables, brief_id, "production start")
        sha = content_sha256({"brief_id": brief_id})
        job_id = self._new_id("job", sha, "job")
        requests = [{"request_id": f"{job_id}.{d.deliverable_id}.{agent}", "agent": agent,
                     "deliverable_id": d.deliverable_id}
                    for d in b.fields.deliverables for agent in ("enigma", "phantom_canvas")]
        eid, requests = self._pending.record(
            self.recorder, "job", sha, job_id, requests, self._consume,
            "production_opened", "zbm_creative_lead", job_id,
            {"brief_id": brief_id, "job_id": job_id, "commission_requests": requests},
            f"Job {job_id} opened on approved brief {brief_id}; {len(requests)} commission "
            "request(s) to Enigma/Phantom Canvas follow")
        job = ProductionJob(job_id=job_id, brief_id=brief_id, ledger_event_ids=[eid], commissions=[
            {**r, "status": "requested", "commissioned": None, "reason": None, "external_ref": None} for r in requests])
        self.jobs[job_id] = job

        receipts = []
        for r in requests:
            d = self._deliverable(b, r["deliverable_id"])
            req = CommissionRequest(r["request_id"], "zbm", r["agent"],
                                    {"brief_id": brief_id, "deliverable": d.model_dump(), "maker_summary": b.maker_summary})
            try:
                receipt = self.departments.creative_agents.commission(req)
                receipts.append({"request_id": receipt.request_id, "agent": receipt.agent,
                                 "commissioned": receipt.commissioned, "reason": receipt.reason,
                                 "external_ref": receipt.external_ref})
            except Exception as exc:  # an unreachable agent is an answer ("not commissioned"), never a 500
                receipts.append({"request_id": req.request_id, "agent": req.agent, "commissioned": False,
                                 "reason": f"commission call failed: {type(exc).__name__}", "external_ref": None})
        try:
            eid2 = self.recorder.record("crossing_creative_agents", "zbm_creative_lead", job_id,
                                        {"job_id": job_id, "receipts": receipts},
                                        f"Job {job_id}: {sum(bool(x['commissioned']) for x in receipts)}/{len(receipts)} "
                                        "commissions accepted by Enigma/Phantom Canvas")
        except LedgerRecordError as exc:
            raise OutcomeNotRecorded(
                f"job {job_id} WAS opened and recorded and its {len(receipts)} commission request(s) were sent, but "
                f"recording the agents' answers failed ({exc}); the answers are not applied (commissions stay "
                "'requested')", {"job_id": job_id, "recorded": ["production_opened"],
                                 "outside_calls_made": len(receipts), "not_recorded": "crossing_creative_agents"}) from exc
        by_id = {x["request_id"]: x for x in receipts}
        job = job.model_copy(update={
            "commissions": [{**c, **by_id.get(c["request_id"], {}), "status": "answered"} for c in job.commissions],
            "ledger_event_ids": [eid, eid2]})
        self.jobs[job_id] = job
        return job

    def _deliverable(self, brief: BriefRecord, deliverable_id: str) -> Deliverable:
        for d in brief.fields.deliverables:
            if d.deliverable_id == deliverable_id:
                return d
        raise PreconditionFailed(f"deliverable {deliverable_id!r} is not in approved brief {brief.brief_id}")

    @serialized
    def submit_work(self, job_id: str, sub: WorkSubmission) -> WorkItem:
        job = self.jobs.get(job_id)
        if job is None:
            raise NotFound(f"job {job_id!r} not found")
        brief = self.get_brief(job.brief_id)
        d = self._deliverable(brief, sub.deliverable_id)
        if not 0 <= sub.variant_index < d.count:
            raise PreconditionFailed(f"variant_index {sub.variant_index} outside the brief's count ({d.count})")
        self._refuse_if_escalated(brief.brief_id, "work submission")
        chain = self._chain(brief.brief_id, sub.deliverable_id, sub.variant_index)
        if chain in self._escalated_chains:
            self._refuse(PreconditionFailed(
                f"deliverable {sub.deliverable_id} (variant {sub.variant_index}) of brief {brief.brief_id} was escalated "
                f"to Andre after {creative_quality.MAX_ROUNDS} review rounds; no more rounds, in this or any other job"),
                "zbm_creative_quality", brief.brief_id, "work submission")
        open_items = [w for w in self.work.values()
                      if self._chain(w.brief_id, w.submission.deliverable_id, w.submission.variant_index) == chain
                      and w.stage not in REWORK_STAGES]
        if open_items:
            raise PreconditionFailed(
                f"work {open_items[-1].work_id} (job {open_items[-1].job_id}) for this deliverable of brief "
                f"{brief.brief_id} is at {open_items[-1].stage.value}; a new version can only follow export failure, "
                "rights block or a send-back")
        # N4: the same client deliverable across ALL briefs (cloned briefs are the same order)
        self._refuse_if_deliverable_escalated(brief.client_id, [d], brief.brief_id, "work submission")
        key = self._key(brief, sub.deliverable_id, sub.variant_index)
        fp = deliverable_fingerprint(d)[:12]
        elsewhere = [w for w in self.work.values()
                     if w.stage in IN_FLIGHT_STAGES and self._same_key(self._work_key(w), key)]
        if elsewhere:
            raise PreconditionFailed(
                f"work {elsewhere[-1].work_id} (brief {elsewhere[-1].brief_id}) for the same client deliverable "
                f"(spec {fp}…, variant {key[2]}) is at {elsewhere[-1].stage.value}; one version "
                "at a time per client deliverable, across all briefs")
        used = self._rounds_for(key)
        if used >= creative_quality.MAX_ROUNDS:
            self._refuse(PreconditionFailed(
                f"client deliverable (spec {fp}…, variant {key[2]}) has used its "
                f"{creative_quality.MAX_ROUNDS} review rounds; only Andre can resolve it"),
                "zbm_creative_quality", brief.brief_id, "work submission")
        rnd = used + 1
        sha = content_sha256({"job_id": job_id, "submission": sub.model_dump(mode="json"), "round": rnd})
        work_id = self._new_id("work", sha, "work")
        eid, _ = self._pending.record(
            self.recorder, "work", sha, work_id, None, self._consume,
            "work_submitted", "zbm_creative_quality", work_id,
            {"job_id": job_id, "brief_id": brief.brief_id, "submission": sub.model_dump(mode="json"), "round": rnd},
            f"Work {work_id} submitted for {brief.brief_id}/{sub.deliverable_id} (round {rnd})")
        item = WorkItem(work_id=work_id, job_id=job_id, brief_id=brief.brief_id, submission=sub, round=rnd,
                        stage=WorkStage.SUBMITTED, ledger_event_ids=[eid])
        self.work[work_id] = item
        return item

    def get_work(self, work_id: str) -> WorkItem:
        w = self.work.get(work_id)
        if w is None:
            raise NotFound(f"work {work_id!r} not found")
        return w

    def _require_stage(self, w: WorkItem, *stages: WorkStage) -> None:
        if w.stage not in stages:
            raise PreconditionFailed(
                f"work {w.work_id} is at stage {w.stage.value}; this step needs {', '.join(s.value for s in stages)}")

    @serialized
    def validate_export(self, work_id: str) -> WorkItem:
        w = self.get_work(work_id)
        self._require_stage(w, WorkStage.SUBMITTED)
        brief = self.get_brief(w.brief_id)
        result = placement_spec.validate_export(w.submission.declared, self._deliverable(brief, w.submission.deliverable_id),
                                                self.registry, self.clock.today())
        eid = self.recorder.record("export_validated", "zbm_placement_spec", work_id, result.model_dump(mode="json"),
                                   f"Export {work_id}: {result.verdict}")
        stage = WorkStage.EXPORT_PASSED if result.verdict == "pass" else WorkStage.EXPORT_FAILED
        w = w.model_copy(update={"stage": stage, "export_validation": result.model_dump(mode="json"),
                                 "ledger_event_ids": [*w.ledger_event_ids, eid]})
        self.work[work_id] = w
        return w

    @serialized
    def check_rights(self, work_id: str) -> WorkItem:
        w = self.get_work(work_id)
        self._require_stage(w, WorkStage.EXPORT_PASSED)
        brief = self.get_brief(w.brief_id)
        A = "zbm_rights_provenance"
        res = rights_provenance.check(work_id, list(w.submission.asset_ids), list(brief.fields.rights_and_permissions),
                                      w.submission.uses_ai_generative_fill, self.rights,
                                      RecordedPort(self.departments.legal, "legal_37", self.recorder, A, work_id),
                                      self.clock.today(),
                                      stamper=RecordedPort(C2paStandIn(), "content_credentials", self.recorder, A, work_id))
        payload = {
            "cleared": res.cleared, "blockers": res.blockers, "provenance_stamp": res.provenance_stamp,
            "asset_checks": [c.__dict__ for c in res.asset_checks],
            "legal_crossing": res.legal_crossing.__dict__ if res.legal_crossing else None,
        }
        eid = self.recorder.record("rights_checked", "zbm_rights_provenance", work_id, payload,
                                   f"Rights for {work_id}: {'cleared' if res.cleared else 'NOT cleared'}")
        w = w.model_copy(update={"stage": WorkStage.RIGHTS_CLEARED if res.cleared else WorkStage.RIGHTS_BLOCKED,
                                 "rights": payload, "ledger_event_ids": [*w.ledger_event_ids, eid]})
        self.work[work_id] = w
        return w

    @serialized
    def quality_review(self, work_id: str, reviewer_id: str, notes: list[str]) -> WorkItem:
        w = self.get_work(work_id)
        try:
            self.actors.require_role(reviewer_id, Role.ZBM_CREATIVE_QUALITY)
        except CreativeError as exc:
            self._refuse(exc, reviewer_id if _is_actor_like(reviewer_id) else "unknown", work_id, "quality review")
        self._require_stage(w, WorkStage.RIGHTS_CLEARED)
        brief = self.get_brief(w.brief_id)
        dec = creative_quality.judge(brief.fields, w.submission.quality, notes, w.round)
        event = {"pass": "quality_passed", "send_back": "quality_sent_back", "escalate_to_andre": "quality_escalated"}[dec.outcome]
        eid = self.recorder.record(event, reviewer_id, work_id, dec.__dict__,
                                   f"Quality round {dec.round} on {work_id}: {dec.outcome} (reported to {dec.reported_to})")
        chain = self._chain(w.brief_id, w.submission.deliverable_id, w.submission.variant_index)
        key = self._work_key(w)
        if dec.outcome == "pass":
            self._clear_rounds(key)  # the deliverable is done; a later order starts at round 1
        else:
            self._rounds_used[key] = max(self._rounds_for(key), w.round)
        stage = {"pass": WorkStage.QUALITY_PASSED, "send_back": WorkStage.SENT_BACK,
                 "escalate_to_andre": WorkStage.ESCALATED_TO_ANDRE}[dec.outcome]
        if stage is WorkStage.ESCALATED_TO_ANDRE:
            self._escalated_chains.add(chain)
        for f in dec.findings:
            self.memory.add_feedback(brief.client_id, reviewer_id, f)
        w = w.model_copy(update={"stage": stage, "quality_decision": dec.__dict__,
                                 "ledger_event_ids": [*w.ledger_event_ids, eid]})
        self.work[work_id] = w
        return w

    @serialized
    def resolve_escalation(self, work_id: str, approval_token: str | None, decision: str) -> WorkItem:
        w = self.get_work(work_id)
        self._require_stage(w, WorkStage.ESCALATED_TO_ANDRE)
        try:
            self.founder.verify(approval_token)
        except CreativeError as exc:
            self._refuse(exc, FOUNDER_ACTOR, work_id, "escalation decision")
        if decision not in ("accept", "kill"):
            raise PreconditionFailed("decision must be 'accept' or 'kill'")
        eid = self.recorder.record("andre_escalation_decision", FOUNDER_ACTOR, work_id, {"decision": decision},
                                   f"Andre {decision}ed escalated work {work_id}")
        stage = WorkStage.ESCALATION_ACCEPTED if decision == "accept" else WorkStage.KILLED_BY_ANDRE
        w = w.model_copy(update={"stage": stage, "ledger_event_ids": [*w.ledger_event_ids, eid]})
        self.work[work_id] = w
        self._clear_rounds(self._work_key(w))  # resolved: a NEW brief for it starts fresh (this chain never does)
        return w

    @serialized
    def compliance_gate(self, work_id: str) -> WorkItem:
        w = self.get_work(work_id)
        self._require_stage(w, WorkStage.QUALITY_PASSED, WorkStage.ESCALATION_ACCEPTED, WorkStage.COMPLIANCE_BLOCKED)
        compliance = RecordedPort(self.departments.compliance, "compliance_38", self.recorder, "zbm_creative_quality", work_id)
        gate = compliance.review("zbm_work", work_id, {"brief_id": w.brief_id,
                                                                          "export": w.export_validation,
                                                                          "rights": w.rights})
        eid = self.recorder.record("crossing_compliance_38", "zbm_creative_quality", work_id, gate.__dict__,
                                   f"Compliance (38) on {work_id}: {'allowed' if gate.allowed else 'NOT allowed'} — {gate.reason}")
        w = w.model_copy(update={"stage": WorkStage.COMPLIANCE_PASSED if gate.allowed else WorkStage.COMPLIANCE_BLOCKED,
                                 "compliance": gate.__dict__, "ledger_event_ids": [*w.ledger_event_ids, eid]})
        self.work[work_id] = w
        return w

    @serialized
    def final_approval(self, work_id: str, approval_token: str | None) -> WorkItem:
        w = self.get_work(work_id)
        if w.stage is not WorkStage.COMPLIANCE_PASSED:
            self._refuse(PreconditionFailed(
                f"work {work_id} is at {w.stage.value}; Andre's final approval needs Compliance (38) to have passed first "
                "— not allowed yet"), FOUNDER_ACTOR, work_id, "final approval")
        try:
            self.founder.verify(approval_token)
        except CreativeError as exc:
            self._refuse(exc, FOUNDER_ACTOR, work_id, "final approval")
        eid = self.recorder.record("andre_final_approval", FOUNDER_ACTOR, work_id, {"work_id": work_id},
                                   f"Andre approved client-facing work {work_id}")
        w = w.model_copy(update={"stage": WorkStage.APPROVED_BY_ANDRE,
                                 "final_approval": {"approved_by": FOUNDER_ACTOR, "at": self.clock.now().isoformat()},
                                 "ledger_event_ids": [*w.ledger_event_ids, eid]})
        self.work[work_id] = w
        return w

    # --- memory ----------------------------------------------------------------------
    @serialized
    def learn_result(self, result: PerformanceResult, brief_id: str) -> MemoryDecision:
        brief = self.get_brief(brief_id)
        if brief.fields is None:
            raise PreconditionFailed("brief has no success targets")
        if result.client_id != brief.client_id:
            raise PreconditionFailed(f"result {result.result_id} is for client {result.client_id}, not {brief.client_id}")
        targets = [t for t in brief.fields.success_in_numbers]
        decisions = [self.memory.evaluate_winner(result, t) for t in targets]
        decision = next((d for d in decisions if d.learned), decisions[0])
        eid = self.recorder.record("memory_winner_admitted" if decision.learned else "memory_result_rejected",
                                   "zbm_creative_memory", result.result_id,
                                   {"result": result.model_dump(mode="json"), "reason": decision.reason},
                                   decision.reason)
        if decision.learned:
            self.memory.commit_winner(result)
        return MemoryDecision(decision.learned, f"{decision.reason} [ledger {eid}]")


def _is_actor_like(s: str) -> bool:
    import re

    return bool(re.fullmatch(r"[a-z0-9_]{1,64}", s or ""))
