"""
ZBC workflow — sequencing, evidence and gates for a CAMPAIGN. It makes no
creative judgement itself; every judgement is one intelligence module's.

Rulebook Writer drafts (1a) -> Campaign Rulebook approves (1, different
actor) -> Andre signs (founder token) -> Rights and Clearance (5) must be
cleared -> go live (rulebook FROZEN; Clipper Network announcement crossing)
-> Source Mining (2) -> Hook and Angle (3) -> Campaign Kit (6, commissions
Enigma / Phantom Canvas) -> Andre signs the kit -> clips submitted by
clippers are reviewed automatically by Clip Review (8) under the version
they were made under -> borderline clips go to the human queue -> payout
eligibility (Clip Review AND Verification and Integrity AND Compliance 38).
Creative Memory (7) learns only attested results.

Evidence rule: every approval, rejection, signature and crossing is
recorded through the ledger BEFORE state changes. If the record fails,
LedgerRecordError propagates, nothing changed, and the API says so (503).
Guardrail refusals are recorded best-effort and always stand.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from shared.actors import ActorRegistry, Role
from shared.clock import Clock
from shared.departments import Departments
from shared.errors import CreativeError, FrozenError, NotFound, PreconditionFailed, ValidationFailed
from shared.founder import FOUNDER_ACTOR, FounderGate
from shared.ledger import EvidenceRecorder
from shared.registry import PlatformRulesRegistry
from shared.rights import RightsRegistry
from zbc import campaign_kit, campaign_rulebook, clip_review, hook_angle, payout_eligibility, rights_clearance
from zbc import rulebook_writer, source_mining
from zbc.campaign_kit import CampaignKit, KitRequest
from zbc.clip_review import BrokenRule, ClipReviewDecision, ClipSubmission, RuleCitationError
from zbc.creative_memory import ClipResult, MemoryDecision, ZbcCreativeMemory
from zbc.hook_angle import HookSheet
from zbc.rights_clearance import ClearanceResult, DeclaredAsset
from zbc.rulebook import Rulebook, RuleKind, RulebookStatus, RulebookStore
from zbc.rulebook_writer import CampaignGoal
from zbc.source_mining import MomentMap, SourceMaterial

A_RULEBOOK = "zbc_campaign_rulebook"
A_RIGHTS = "zbc_rights_clearance"
A_MINING = "zbc_source_mining"
A_HOOKS = "zbc_hook_angle"
A_KIT = "zbc_campaign_kit"
A_REVIEW = "zbc_clip_review"
A_MEMORY = "zbc_creative_memory"
A_ELIGIBILITY = "zbc_payout_gate"


class HumanVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: str
    broken_rules: list[BrokenRule] = []
    note: str = ""


@dataclass
class ZbcWorkflow:
    registry: PlatformRulesRegistry
    rights: RightsRegistry
    actors: ActorRegistry
    recorder: EvidenceRecorder
    clock: Clock
    departments: Departments
    founder: FounderGate
    rulebooks: RulebookStore = field(default_factory=RulebookStore)
    memory: ZbcCreativeMemory = field(default_factory=ZbcCreativeMemory)
    goals: dict[tuple[str, int], CampaignGoal] = field(default_factory=dict)
    rights_checks: dict[str, tuple[list[DeclaredAsset], bool, ClearanceResult]] = field(default_factory=dict)
    moment_maps: dict[tuple[str, int], MomentMap] = field(default_factory=dict)
    hook_sheets: dict[tuple[str, int], list[HookSheet]] = field(default_factory=dict)
    kits: dict[str, CampaignKit] = field(default_factory=dict)
    submissions: dict[str, ClipSubmission] = field(default_factory=dict)
    decisions: dict[str, ClipReviewDecision] = field(default_factory=dict)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    # --- helpers ------------------------------------------------------------------
    def _refuse(self, exc: CreativeError, actor: str, subject_id: str, action: str):
        self.recorder.try_record("guardrail_refusal", actor if _actor_like(actor) else "unknown", subject_id,
                                 {"action": action, "reason": exc.reason}, f"Refused {action}: {exc.reason}")
        raise exc

    def _guard(self, fn, actor: str, subject_id: str, action: str):
        try:
            return fn()
        except CreativeError as exc:
            self._refuse(exc, actor, subject_id, action)

    def _live(self, campaign_id: str) -> Rulebook:
        rb = self.rulebooks.live(campaign_id)
        if rb is None:
            raise PreconditionFailed(f"campaign {campaign_id} has no live rulebook")
        return rb

    # --- Job 1: set the standard ----------------------------------------------------
    def draft_rulebook(self, goal: CampaignGoal, actor_id: str) -> Rulebook:
        cid = goal.campaign_id
        self._guard(lambda: self.actors.require_role(actor_id, Role.ZBC_RULEBOOK_WRITER), actor_id, cid, "rulebook draft")
        if self.rulebooks.versions(cid):
            raise PreconditionFailed(f"campaign {cid} already has a rulebook; edit the draft or revise the live version")
        rb = rulebook_writer.draft(goal, self.registry, self.clock.today(), version=1, drafted_by=actor_id)
        self.rulebooks.check_add(rb)
        self.recorder.record("rulebook_drafted", actor_id, f"{cid}:v1", {"rulebook": rb.model_dump(mode="json")},
                             f"Rulebook {cid} v1 drafted; {len(rb.rules)} rules, {len(rb.blocking_issues)} blocking issue(s)")
        self.rulebooks.commit(rb)
        self.goals[(cid, 1)] = goal
        return rb

    def edit_rulebook(self, campaign_id: str, version: int, goal: CampaignGoal, actor_id: str) -> Rulebook:
        subject = f"{campaign_id}:v{version}"
        existing = self.rulebooks.get(campaign_id, version)
        if existing.frozen:
            self._refuse(FrozenError(
                f"rulebook {campaign_id} v{version} is {existing.status.value} and FROZEN; it cannot be changed in "
                "place — draft a new version (revise) instead"), actor_id, subject, "in-place rulebook change")
        self._guard(lambda: self.actors.require_role(actor_id, Role.ZBC_RULEBOOK_WRITER), actor_id, subject, "rulebook edit")
        if goal.campaign_id != campaign_id:
            raise ValidationFailed("goal is for a different campaign", ["campaign_id"])
        if existing.supersedes_version is not None:
            prev = self.rulebooks.get(campaign_id, existing.supersedes_version)
            rb = rulebook_writer.revise(prev, goal, self.registry, self.clock.today(), version, drafted_by=actor_id)
        else:
            rb = rulebook_writer.draft(goal, self.registry, self.clock.today(), version=version, drafted_by=actor_id)
        self.rulebooks.check_replace(rb)
        self.recorder.record("rulebook_edited", actor_id, subject,
                             {"rulebook": rb.model_dump(mode="json"), "previous_status": existing.status.value},
                             f"Rulebook {campaign_id} v{version} edited; back to draft (approval/signature cleared)")
        self.rulebooks.commit(rb)
        self.goals[(campaign_id, version)] = goal
        return rb

    def revise_rulebook(self, campaign_id: str, goal: CampaignGoal, actor_id: str) -> Rulebook:
        self._guard(lambda: self.actors.require_role(actor_id, Role.ZBC_RULEBOOK_WRITER), actor_id, campaign_id, "revision")
        live = self._live(campaign_id)
        latest = self.rulebooks.versions(campaign_id)[-1]
        if latest.version != live.version:
            raise PreconditionFailed(f"revision v{latest.version} is already open ({latest.status.value}); edit it instead")
        v = self.rulebooks.next_version(campaign_id)
        rb = rulebook_writer.revise(live, goal, self.registry, self.clock.today(), v, drafted_by=actor_id)
        self.rulebooks.check_add(rb)
        self.recorder.record("rulebook_revision_drafted", actor_id, f"{campaign_id}:v{v}",
                             {"rulebook": rb.model_dump(mode="json"), "supersedes": live.version},
                             f"Rulebook {campaign_id} v{v} drafted as a revision of live v{live.version}")
        self.rulebooks.commit(rb)
        self.goals[(campaign_id, v)] = goal
        return rb

    def review_rulebook(self, campaign_id: str, version: int, approver_id: str) -> Rulebook:
        subject = f"{campaign_id}:v{version}"
        rb = self.rulebooks.get(campaign_id, version)
        result = self._guard(lambda: campaign_rulebook.review(rb, approver_id, self.actors, self.registry, self.clock.today()),
                             approver_id, subject, "rulebook approval")
        approved = result.outcome == "approved"
        new = rb.model_copy(update={
            "status": RulebookStatus.APPROVED if approved else RulebookStatus.SENT_BACK,
            "approved_by": approver_id if approved else None, "review_issues": tuple(result.issues),
        })
        self.rulebooks.check_replace(new)
        self.recorder.record("rulebook_approved" if approved else "rulebook_sent_back", approver_id, subject,
                             {"outcome": result.outcome, "issues": result.issues},
                             f"Rulebook {campaign_id} v{version} {result.outcome} by {approver_id}"
                             + ("" if approved else f" ({len(result.issues)} issue(s))"))
        self.rulebooks.commit(new)
        return new

    def sign_rulebook(self, campaign_id: str, version: int, founder_token: str | None) -> Rulebook:
        subject = f"{campaign_id}:v{version}"
        rb = self.rulebooks.get(campaign_id, version)
        if rb.status is not RulebookStatus.APPROVED:
            self._refuse(PreconditionFailed(
                f"rulebook {campaign_id} v{version} is {rb.status.value}; Andre signs only an approved rulebook"),
                FOUNDER_ACTOR, subject, "rulebook signature")
        self._guard(lambda: self.founder.verify(founder_token), FOUNDER_ACTOR, subject, "rulebook signature")
        now = self.clock.now()
        new = rb.model_copy(update={"status": RulebookStatus.SIGNED, "signed_by": FOUNDER_ACTOR, "signed_at": now})
        self.rulebooks.check_replace(new)
        self.recorder.record("rulebook_signed_by_andre", FOUNDER_ACTOR, subject, {"version": version},
                             f"Andre signed rulebook {campaign_id} v{version}")
        self.rulebooks.commit(new)
        return new

    def check_rights(self, campaign_id: str, assets: list[DeclaredAsset], uses_ai_generative_fill: bool = False) -> ClearanceResult:
        res = rights_clearance.check_campaign(campaign_id, assets, self.rights, self.departments.legal,
                                              self.clock.today(), uses_ai_generative_fill)
        self.recorder.record("rights_clearance_checked", A_RIGHTS, campaign_id, res.as_dict(),
                             f"Rights for {campaign_id}: {'cleared' if res.cleared else 'NOT cleared'} "
                             f"({len(res.blockers)} blocker(s), {len(res.flags)} flag(s))")
        self.rights_checks[campaign_id] = (list(assets), uses_ai_generative_fill, res)
        return res

    def go_live(self, campaign_id: str, version: int) -> Rulebook:
        subject = f"{campaign_id}:v{version}"
        rb = self.rulebooks.get(campaign_id, version)
        if rb.status is not RulebookStatus.SIGNED:
            self._refuse(PreconditionFailed(
                f"rulebook {campaign_id} v{version} is {rb.status.value}; only a rulebook Andre signed can go live"),
                A_RULEBOOK, subject, "go live")
        stored = self.rights_checks.get(campaign_id)
        if stored is None:
            self._refuse(PreconditionFailed(f"no rights clearance check on file for {campaign_id} (fails closed)"),
                         A_RIGHTS, subject, "go live")
        assets, gen_fill, _ = stored
        res = rights_clearance.check_campaign(campaign_id, assets, self.rights, self.departments.legal,
                                              self.clock.today(), gen_fill)  # re-checked, not trusted
        rc = rb.one(RuleKind.RIGHTS_CLEARED_ONLY)
        unchecked = sorted(set(rc.params.get("allowed_asset_ids", [])) - {a.asset_id for a in assets}) if rc else []
        if not res.cleared or unchecked:
            reasons = res.blockers + [f["reason"] + f" [{f['asset_id']}]" for f in res.flags]
            if unchecked:
                reasons.append(f"assets in {rc.rule_id} never rights-checked: {', '.join(unchecked)}")
            self._refuse(PreconditionFailed("rights not cleared: " + "; ".join(reasons)), A_RIGHTS, subject, "go live")
        previous = self.rulebooks.live(campaign_id)
        now = self.clock.now()
        announcement = self.departments.clipper_network.announce_rulebook_version(
            campaign_id, version, {"supersedes": previous.version if previous else None})
        self.recorder.record("crossing_clipper_network", A_RULEBOOK, subject, announcement.__dict__,
                             f"Clipper Network announcement of {campaign_id} v{version}: "
                             f"{'delivered' if announcement.allowed else 'NOT delivered'} — {announcement.reason}")
        self.recorder.record("rulebook_live", A_RULEBOOK, subject,
                             {"version": version, "superseded_version": previous.version if previous else None,
                              "rights": res.as_dict(), "announcement_delivered": announcement.allowed},
                             f"Rulebook {campaign_id} v{version} is LIVE and frozen"
                             + (f"; v{previous.version} superseded" if previous else ""))
        if previous is not None:
            sup = previous.model_copy(update={"status": RulebookStatus.SUPERSEDED, "superseded_at": now})
            self.rulebooks.check_replace(sup)
            self.rulebooks.commit(sup)
        live = rb.model_copy(update={"status": RulebookStatus.LIVE, "live_at": now})
        self.rulebooks.check_replace(live)
        self.rulebooks.commit(live)
        return live

    # --- Job 2: make the kit --------------------------------------------------------
    def build_moment_map(self, campaign_id: str, material: SourceMaterial) -> MomentMap:
        rb = self._live(campaign_id)
        mm = source_mining.build(material, rb, self.registry, self.clock.today())
        self.recorder.record("moment_map_built", A_MINING, f"{campaign_id}:v{rb.version}", mm.model_dump(mode="json"),
                             f"Moment Map for {campaign_id} v{rb.version}: {len(mm.moments)} moment(s), "
                             f"{len(mm.rejected)} segment(s) rejected")
        self.moment_maps[(campaign_id, rb.version)] = mm
        return mm

    def build_hook_sheets(self, campaign_id: str) -> list[HookSheet]:
        rb = self._live(campaign_id)
        mm = self.moment_maps.get((campaign_id, rb.version))
        if mm is None:
            raise PreconditionFailed(f"no Moment Map for {campaign_id} v{rb.version}")
        sh = hook_angle.sheets(mm, rb)
        self.recorder.record("hook_sheets_built", A_HOOKS, f"{campaign_id}:v{rb.version}",
                             {"sheets": [s.model_dump(mode="json") for s in sh]},
                             f"{len(sh)} hook sheet(s) for {campaign_id} v{rb.version}")
        self.hook_sheets[(campaign_id, rb.version)] = sh
        return sh

    def build_kit(self, campaign_id: str, req: KitRequest) -> CampaignKit:
        rb = self._live(campaign_id)
        mm = self.moment_maps.get((campaign_id, rb.version))
        sh = self.hook_sheets.get((campaign_id, rb.version))
        if mm is None or sh is None:
            raise PreconditionFailed(f"the kit needs a Moment Map and hook sheets for {campaign_id} v{rb.version}")
        kit_id = f"kit-{next(self._ids):04d}"
        kit = campaign_kit.build(kit_id, rb, mm, sh, req, self.departments.creative_agents)
        self.recorder.record("crossing_creative_agents", A_KIT, kit_id, {"commissions": kit.commissions},
                             f"Kit {kit_id}: {sum(c['commissioned'] for c in kit.commissions)}/{len(kit.commissions)} "
                             "seed-clip commissions accepted by Enigma/Phantom Canvas")
        self.recorder.record("campaign_kit_built", A_KIT, kit_id, kit.model_dump(mode="json"),
                             f"Kit {kit_id} for {campaign_id} v{rb.version}: {len(kit.seeds)} seed clip spec(s)")
        self.kits[campaign_id] = kit
        return kit

    def sign_kit(self, campaign_id: str, founder_token: str | None) -> CampaignKit:
        kit = self.kits.get(campaign_id)
        if kit is None:
            raise NotFound(f"no kit for campaign {campaign_id}")
        live = self.rulebooks.live(campaign_id)
        if kit.status != "draft" or live is None or live.version != kit.rulebook_version:
            self._refuse(PreconditionFailed(
                f"kit {kit.kit_id} is {kit.status} for v{kit.rulebook_version}; Andre signs a draft kit made for the live version"),
                FOUNDER_ACTOR, kit.kit_id, "kit signature")
        self._guard(lambda: self.founder.verify(founder_token), FOUNDER_ACTOR, kit.kit_id, "kit signature")
        self.recorder.record("campaign_kit_signed_by_andre", FOUNDER_ACTOR, kit.kit_id,
                             {"campaign_id": campaign_id, "rulebook_version": kit.rulebook_version,
                              "seed_clips_produced": kit.seed_clips_produced},
                             f"Andre signed kit {kit.kit_id} for {campaign_id} v{kit.rulebook_version}")
        signed = kit.model_copy(update={"status": "signed", "signed_by": FOUNDER_ACTOR})
        self.kits[campaign_id] = signed
        return signed

    # --- Job 3: judge the work ------------------------------------------------------
    def submit_clip(self, sub: ClipSubmission) -> ClipReviewDecision:
        cid = sub.campaign_id
        kit = self.kits.get(cid)
        if kit is None or kit.status != "signed":
            raise PreconditionFailed(f"campaign {cid} is not open for clips: Andre has not signed its kit")
        if sub.submission_id in self.submissions:
            raise PreconditionFailed(f"submission {sub.submission_id} already exists")
        rb = self.rulebooks.get(cid, sub.rulebook_version)
        if rb.live_at is None:
            raise PreconditionFailed(f"rulebook {cid} v{rb.version} never went live; clips can't be made under it")
        if sub.posted_at > self.clock.now():
            raise PreconditionFailed("posted_at is in the future")
        if sub.posted_at < rb.live_at or (rb.superseded_at is not None and sub.posted_at >= rb.superseded_at):
            raise PreconditionFailed(
                f"clip posted {sub.posted_at.isoformat()} is outside v{rb.version}'s live window; "
                "declare the version that was live when it was made")
        decision = clip_review.review(sub, rb, self.registry, self.clock.now(), decided_by=A_REVIEW)
        self.recorder.record("clip_reviewed", A_REVIEW, sub.submission_id,
                             {"decision": decision.model_dump(mode="json"), "submission": sub.model_dump(mode="json")},
                             f"Clip {sub.submission_id} under {cid} v{rb.version}: {decision.outcome}"
                             + (f" (broke {', '.join(b.rule_id for b in decision.broken_rules)})" if decision.broken_rules else ""))
        self.submissions[sub.submission_id] = sub
        self.decisions[sub.submission_id] = decision
        return decision

    def get_decision(self, submission_id: str) -> ClipReviewDecision:
        d = self.decisions.get(submission_id)
        if d is None:
            raise NotFound(f"submission {submission_id!r} not found")
        return d

    def human_review(self, submission_id: str, reviewer_id: str, verdict: HumanVerdict) -> ClipReviewDecision:
        current = self.get_decision(submission_id)
        sub = self.submissions[submission_id]
        self._guard(lambda: self.actors.require_role(reviewer_id, Role.ZBC_CLIP_HUMAN_REVIEWER),
                    reviewer_id, submission_id, "human clip review")
        if current.outcome != "human_review":
            raise PreconditionFailed(f"clip {submission_id} is {current.outcome}; only human_review clips are in the queue")
        if verdict.outcome not in ("pass", "reject"):
            raise ValidationFailed("human verdict must be 'pass' or 'reject'", ["outcome"])
        rb = self.rulebooks.get(sub.campaign_id, sub.rulebook_version)
        try:
            decision = clip_review.make_decision(
                rb, submission_id=submission_id, campaign_id=sub.campaign_id, rulebook_version=rb.version,
                outcome=verdict.outcome, broken_rules=tuple(verdict.broken_rules), human_review_reasons=(),
                checks=current.checks, decided_by=reviewer_id, decided_at=self.clock.now(),
            )
        except (RuleCitationError, ValueError) as exc:
            self._refuse(ValidationFailed("human verdict cites rules that are not in this rulebook version",
                                          [str(exc)]), reviewer_id, submission_id, "human clip review")
        self.recorder.record("clip_human_reviewed", reviewer_id, submission_id,
                             {"decision": decision.model_dump(mode="json"), "note": verdict.note,
                              "previous_reasons": list(current.human_review_reasons)},
                             f"Human review of {submission_id}: {decision.outcome}"
                             + (f" (broke {', '.join(b.rule_id for b in decision.broken_rules)})" if decision.broken_rules else ""))
        self.decisions[submission_id] = decision
        return decision

    def payout_eligibility(self, submission_id: str) -> dict:
        decision = self.get_decision(submission_id)
        sub = self.submissions[submission_id]
        res = payout_eligibility.evaluate(sub, decision, self.departments.verification, self.departments.compliance)
        self.recorder.record("crossing_verification_integrity", A_ELIGIBILITY, submission_id,
                             res.as_dict()["verification"],
                             f"Verification and Integrity on {submission_id}: "
                             f"{'verified' if res.verification.verified else 'NOT verified'} — {res.verification.reason}")
        self.recorder.record("crossing_compliance_38", A_ELIGIBILITY, submission_id, res.as_dict()["compliance"],
                             f"Compliance (38) on {submission_id}: "
                             f"{'allowed' if res.compliance.allowed else 'NOT allowed'} — {res.compliance.reason}")
        self.recorder.record("payout_eligibility_decided", A_ELIGIBILITY, submission_id, res.as_dict(),
                             f"Payout eligibility for {submission_id}: {'eligible' if res.eligible else 'NOT eligible'}"
                             + (f" ({len(res.blockers)} blocker(s))" if res.blockers else ""))
        return res.as_dict()

    # --- memory ---------------------------------------------------------------------
    def learn_result(self, result: ClipResult) -> MemoryDecision:
        sub = self.submissions.get(result.submission_id)
        if sub is None or sub.campaign_id != result.campaign_id:
            raise NotFound(f"no reviewed submission {result.submission_id} in campaign {result.campaign_id}")
        decision = self.memory.evaluate(result, self.departments.verification)
        if decision.attestation is not None:
            self.recorder.record("crossing_verification_integrity", A_MEMORY, result.result_id,
                                 {"verified": decision.attestation.verified, "reason": decision.attestation.reason,
                                  "attestation_id": decision.attestation.attestation_id},
                                 f"Verification and Integrity on result {result.result_id}: "
                                 f"{'verified' if decision.attestation.verified else 'NOT verified'}")
        self.recorder.record("memory_result_learned" if decision.learned else "memory_result_rejected", A_MEMORY,
                             result.result_id, {"result": result.model_dump(mode="json"), "reason": decision.reason},
                             decision.reason)
        if decision.learned and decision.winner is not None:
            self.memory.commit(decision.winner)
        return decision


def _actor_like(s: str | None) -> bool:
    import re

    return bool(re.fullmatch(r"[a-z0-9_]{1,64}", s or ""))
