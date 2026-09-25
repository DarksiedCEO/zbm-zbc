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
recorded through the ledger BEFORE state changes, and every request to an
outside department is on the ledger BEFORE it is made (`RecordedPort`, or
the intent record that lists it: `rulebook_live` before the Clipper
Network announcement, `campaign_kit_built` before the seed commissions).
If the record fails, LedgerRecordError propagates, nothing changed, no one
was called, and the API says so (503). Guardrail refusals are recorded
best-effort and always stand.

Rulebook version of a clip (fix wave 1, F13): `posted_at` and
`rulebook_version` are ASSERTED by the clipper; the server records its own
receipt time (`received_at`). A post dated in the future is refused. A
clip may declare the version that was live when it says it posted, but a
version that has been superseded is judged automatically only if the clip
reaches us within `superseded_grace_hours` (default 72, config
CREATIVE_SUPERSEDED_GRACE_HOURS) of the supersession; later, the clip goes
to the human queue with the reason — a backdated post can't buy older,
more lenient rules automatically.

Retry receipt time (fix wave 2, N1): when an operation's ledger record
fails, its time is kept ONLY so an identical retry rebuilds the identical
event. That reservation is bound to the exact content (SHA-256 of the
canonical submission / verdict) and expires after `RETRY_WINDOW` (15
minutes). A retry with different content never inherits the old time (and, since
fix wave 6 / N5, is not held back by a CERTAIN failure either: only an
UNCERTAIN outcome refuses different content).
A clip `submission_id` is bound to its content from its first attempt,
permanently: different content under a used id is refused (409), never
recorded as a new decision — and the clip's ledger event id is derived
from (submission_id, content hash), so a second version of the same
content (e.g. a stale retry with a fresh receipt time) is refused by the
ledger's own 409.

Uncertain outcomes (fix wave 4, LOST): when the record call fails in a way
that leaves the LEDGER's outcome unknown (response lost, timeout, 5xx, 409
— `took_effect != False`), the attempt is remembered — its content hash,
its time, the EXACT record it sent and the decision it would commit — in a
bounded store (`MAX_PENDING_ATTEMPTS`, oldest evicted first) until it is
resolved. An identical-content retry at ANY later time replays that exact
record: the ledger answers 200 (it had it) or 201 (it didn't), and the
decision takes effect once, with its original, true receipt/decision time
(the content is bound, so this is not backdating). Different content under
the same operation stays refused (409) while it is unresolved. Only a
failure that CERTAINLY did not reach the ledger (connection refused,
4xx refusal) keeps the short RETRY_WINDOW reservation of N1.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from shared.actors import ActorRegistry, Role
from shared.clock import Clock
from shared.departments import Departments
from shared.errors import CreativeError, FrozenError, GuardrailViolation, NotFound, PreconditionFailed, ValidationFailed
from shared.founder import FOUNDER_ACTOR, FounderGate
from shared.ledger import (
    EvidenceRecorder,
    LedgerRecordError,
    OutcomeNotRecorded,
    PendingCreations,
    RecordedPort,
    check_subject,
    content_sha256,
    serialized,
)
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

DEFAULT_SUPERSEDED_GRACE_HOURS = 72
# How long a failed operation's first-attempt time may be reused by an
# identical retry (N1). Short on purpose: a retry, not a reservation.
RETRY_WINDOW = timedelta(minutes=15)
# Bound on remembered failed attempts (LOST). Past it the oldest is dropped;
# an identical retry of a dropped UNCERTAIN attempt then meets the ledger's
# 409 and is reported as conflicting (never as "did not take effect").
MAX_PENDING_ATTEMPTS = 10_000
# An uncertain human verdict can be withdrawn (LOW-D) only this long after it
# was sent: ledger-rust drops a connection after 15 s (REQUEST_DEADLINE), but
# an append already queued on its blocking pool can still complete after
# that, so the margin is generous.
WITHDRAW_MIN_AGE = timedelta(minutes=2)


@dataclass
class _Attempt:
    """A failed attempt of one operation (N1 / LOST)."""

    sha: str
    at: datetime
    uncertain: bool  # the ledger may hold it: replay `record` exactly
    record: tuple[tuple, dict] = ((), {})
    result: Any = None
    event_id: str = ""  # the event id the failed record was sent under
    sent_at: datetime | None = None  # when it was last sent (a replay re-sends it)


@dataclass(frozen=True)
class ClipPlan:
    """A clip review computed off the workflow lock, with the inputs it
    read (fix wave 9, M1): committed only while they are still current."""

    sha: str
    rb: Rulebook
    received_at: datetime
    to_human: tuple[str, ...]
    rows: dict
    attempt: Any
    decision: ClipReviewDecision


def _sha256(obj) -> str:
    import hashlib
    import json

    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8", "surrogatepass")).hexdigest()


def submission_sha256(sub: ClipSubmission) -> str:
    """Canonical content hash of a clip submission (every field)."""
    return _sha256(sub.model_dump(mode="json"))


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
    superseded_grace_hours: int = DEFAULT_SUPERSEDED_GRACE_HOURS
    _n: int = 1
    # Failed attempts by operation (N1 / LOST): a CERTAIN failure keeps its
    # time for an identical retry within RETRY_WINDOW; an UNCERTAIN one keeps
    # its exact record until resolved (bounded, see MAX_PENDING_ATTEMPTS).
    _attempts: "OrderedDict[str, _Attempt]" = field(default_factory=OrderedDict)
    _pending_new: PendingCreations = field(default_factory=PendingCreations)  # server-assigned kit ids
    # submission_id -> content hash, from the first attempt on (N1).
    _submission_content: dict[str, str] = field(default_factory=dict)

    # --- helpers ------------------------------------------------------------------
    @staticmethod
    def _subject(campaign_id: str, version: int) -> str:
        return check_subject(f"{campaign_id}:v{version}", "rulebook subject")

    def _op_now(self, op: str, content_sha: str) -> datetime:
        """The time of this operation: the clock, or — ONLY when an earlier
        attempt of the SAME operation with the SAME content failed at the
        ledger (certainly-not-recorded: less than RETRY_WINDOW ago) — that
        attempt's time, so the retry rebuilds the identical event.
        Different content, or an expired reservation, gets the clock (N1).
        A refusal for any other reason never reserves a time."""
        now = self.clock.now()
        held = self._attempts.get(op)
        if held is not None and not held.uncertain:
            if held.sha == content_sha and timedelta(0) <= now - held.at <= RETRY_WINDOW:
                return held.at
            # different content, or an expired reservation: the reservation is gone (N5)
            self._attempts.pop(op, None)
        return now

    def _pending_other_content(self, op: str, content_sha: str) -> bool:
        """An earlier attempt of `op` with DIFFERENT content failed at the
        ledger and its outcome is UNKNOWN (it may be on the ledger): a
        different decision now is refused until it is resolved (replayed
        or withdrawn). A CERTAIN failure (took_effect False: connection
        refused, a 4xx, the ledger's shed 503) holds nothing — the ledger
        has no record, so a different decision may follow at once (fix
        wave 6, N5; the RETRY_WINDOW reservation of N1 only lets an
        IDENTICAL retry reuse the first attempt's time)."""
        held = self._attempts.get(op)
        return held is not None and held.sha != content_sha and held.uncertain

    def _replay_uncertain(self, op: str, content_sha: str):
        """LOST: an identical retry of an attempt whose outcome is unknown
        replays its EXACT record (ledger 200 if it had it, 201 if not) and
        returns the decision that attempt would have committed; None when
        there is nothing to replay. A failed replay raises and stays pending."""
        held = self._attempts.get(op)
        if held is None or not held.uncertain or held.sha != content_sha:
            return None
        args, kwargs = held.record
        held.sent_at = self.clock.now()
        self.recorder.record(*args, **kwargs)
        self._attempts.pop(op, None)
        return held.result

    def _record_op(self, op: str, content_sha: str, at: datetime, result, *args, **kwargs) -> str:
        planned = self.recorder.planned_event_id(*args, **kwargs)
        try:
            eid = self.recorder.record(*args, **kwargs)
        except LedgerRecordError as exc:
            self._attempts[op] = _Attempt(content_sha, at, exc.took_effect is not False, (args, kwargs), result,
                                          planned, self.clock.now())
            self._attempts.move_to_end(op)
            while len(self._attempts) > MAX_PENDING_ATTEMPTS:
                self._attempts.popitem(last=False)
            raise
        self._attempts.pop(op, None)
        return eid

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
    @serialized
    def draft_rulebook(self, goal: CampaignGoal, actor_id: str) -> Rulebook:
        cid = goal.campaign_id
        subject = self._subject(cid, 1)
        self._guard(lambda: self.actors.require_role(actor_id, Role.ZBC_RULEBOOK_WRITER), actor_id, cid, "rulebook draft")
        if self.rulebooks.versions(cid):
            raise PreconditionFailed(f"campaign {cid} already has a rulebook; edit the draft or revise the live version")
        rb = rulebook_writer.draft(goal, self.registry, self.clock.today(), version=1, drafted_by=actor_id)
        self.rulebooks.check_add(rb)
        self.recorder.record("rulebook_drafted", actor_id, subject, {"rulebook": rb.model_dump(mode="json")},
                             f"Rulebook {cid} v1 drafted; {len(rb.rules)} rules, {len(rb.blocking_issues)} blocking issue(s)")
        self.rulebooks.commit(rb)
        self.goals[(cid, 1)] = goal
        return rb

    @serialized
    def edit_rulebook(self, campaign_id: str, version: int, goal: CampaignGoal, actor_id: str) -> Rulebook:
        subject = self._subject(campaign_id, version)
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
        self.rulebooks.check_no_reuse(rb)
        self.recorder.record("rulebook_edited", actor_id, subject,
                             {"rulebook": rb.model_dump(mode="json"), "previous_status": existing.status.value},
                             f"Rulebook {campaign_id} v{version} edited; back to draft (approval/signature cleared)")
        self.rulebooks.commit(rb)
        self.goals[(campaign_id, version)] = goal
        return rb

    @serialized
    def revise_rulebook(self, campaign_id: str, goal: CampaignGoal, actor_id: str) -> Rulebook:
        self._guard(lambda: self.actors.require_role(actor_id, Role.ZBC_RULEBOOK_WRITER), actor_id, campaign_id, "revision")
        live = self._live(campaign_id)
        latest = self.rulebooks.versions(campaign_id)[-1]
        if latest.version != live.version:
            raise PreconditionFailed(f"revision v{latest.version} is already open ({latest.status.value}); edit it instead")
        v = self.rulebooks.next_version(campaign_id)
        subject = self._subject(campaign_id, v)
        rb = rulebook_writer.revise(live, goal, self.registry, self.clock.today(), v, drafted_by=actor_id)
        self.rulebooks.check_add(rb)
        self.recorder.record("rulebook_revision_drafted", actor_id, subject,
                             {"rulebook": rb.model_dump(mode="json"), "supersedes": live.version},
                             f"Rulebook {campaign_id} v{v} drafted as a revision of live v{live.version}")
        self.rulebooks.commit(rb)
        self.goals[(campaign_id, v)] = goal
        return rb

    @serialized
    def review_rulebook(self, campaign_id: str, version: int, approver_id: str) -> Rulebook:
        subject = self._subject(campaign_id, version)
        rb = self.rulebooks.get(campaign_id, version)
        result = self._guard(lambda: campaign_rulebook.review(rb, approver_id, self.actors, self.registry, self.clock.today()),
                             approver_id, subject, "rulebook approval")
        approved = result.outcome == "approved"
        new = rb.model_copy(update={
            "status": RulebookStatus.APPROVED if approved else RulebookStatus.SENT_BACK,
            "approved_by": approver_id if approved else None, "review_issues": tuple(result.issues),
            "review_warnings": tuple(result.warnings),
        })
        self.rulebooks.check_replace(new)
        self.recorder.record("rulebook_approved" if approved else "rulebook_sent_back", approver_id, subject,
                             {"outcome": result.outcome, "issues": result.issues, "warnings": result.warnings},
                             f"Rulebook {campaign_id} v{version} {result.outcome} by {approver_id}"
                             + ("" if approved else f" ({len(result.issues)} issue(s))"))
        self.rulebooks.commit(new)
        return new

    @serialized
    def sign_rulebook(self, campaign_id: str, version: int, founder_token: str | None) -> Rulebook:
        subject = self._subject(campaign_id, version)
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

    @serialized
    def check_rights(self, campaign_id: str, assets: list[DeclaredAsset], uses_ai_generative_fill: bool = False) -> ClearanceResult:
        check_subject(campaign_id, "campaign id")
        legal = RecordedPort(self.departments.legal, "legal_37", self.recorder, A_RIGHTS, campaign_id)
        res = rights_clearance.check_campaign(campaign_id, assets, self.rights, legal,
                                              self.clock.today(), uses_ai_generative_fill)
        self.recorder.record("rights_clearance_checked", A_RIGHTS, campaign_id, res.as_dict(),
                             f"Rights for {campaign_id}: {'cleared' if res.cleared else 'NOT cleared'} "
                             f"({len(res.blockers)} blocker(s), {len(res.flags)} flag(s))")
        self.rights_checks[campaign_id] = (list(assets), uses_ai_generative_fill, res)
        return res

    @serialized
    def go_live(self, campaign_id: str, version: int) -> Rulebook:
        """Record first (F9): `rulebook_live` is recorded and the version
        committed LIVE before Clipper Network is told anything; the
        announcement's answer is recorded afterwards (a failure there is
        `took_effect: "partial"` — the version IS live)."""
        subject = self._subject(campaign_id, version)
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
        legal = RecordedPort(self.departments.legal, "legal_37", self.recorder, A_RIGHTS, subject)
        res = rights_clearance.check_campaign(campaign_id, assets, self.rights, legal,
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
        sup = previous.model_copy(update={"status": RulebookStatus.SUPERSEDED, "superseded_at": now}) if previous else None
        live = rb.model_copy(update={"status": RulebookStatus.LIVE, "live_at": now})
        if sup is not None:
            self.rulebooks.check_replace(sup)
        self.rulebooks.check_replace(live)
        facts = {"supersedes": previous.version if previous else None}
        self.recorder.record("rulebook_live", A_RULEBOOK, subject,
                             {"version": version, "superseded_version": previous.version if previous else None,
                              "rights": res.as_dict(),
                              "clipper_network_announcement": {"status": "requested", "facts": facts}},
                             f"Rulebook {campaign_id} v{version} is LIVE and frozen"
                             + (f"; v{previous.version} superseded" if previous else "")
                             + "; Clipper Network announcement follows")
        if sup is not None:
            self.rulebooks.commit(sup)
        self.rulebooks.commit(live)
        try:
            announcement = self.departments.clipper_network.announce_rulebook_version(campaign_id, version, facts)
        except Exception as exc:  # an unreachable department is an answer ("not delivered"), never a 500
            from shared.departments import GateResult
            announcement = GateResult("clipper_network", False, f"announcement call failed: {type(exc).__name__}")
        try:
            self.recorder.record("crossing_clipper_network", A_RULEBOOK, subject, announcement.__dict__,
                                 f"Clipper Network announcement of {campaign_id} v{version}: "
                                 f"{'delivered' if announcement.allowed else 'NOT delivered'} — {announcement.reason}")
        except LedgerRecordError as exc:
            raise OutcomeNotRecorded(
                f"rulebook {campaign_id} v{version} IS live (recorded) and Clipper Network was sent the announcement, "
                f"but recording its answer failed ({exc})",
                {"rulebook": subject, "recorded": ["rulebook_live"], "outside_calls_made": 1,
                 "not_recorded": "crossing_clipper_network"}) from exc
        return live

    # --- Job 2: make the kit --------------------------------------------------------
    @serialized
    def build_moment_map(self, campaign_id: str, material: SourceMaterial) -> MomentMap:
        rb = self._live(campaign_id)
        mm = source_mining.build(material, rb, self.registry, self.clock.today())
        self.recorder.record("moment_map_built", A_MINING, self._subject(campaign_id, rb.version), mm.model_dump(mode="json"),
                             f"Moment Map for {campaign_id} v{rb.version}: {len(mm.moments)} moment(s), "
                             f"{len(mm.rejected)} segment(s) rejected")
        self.moment_maps[(campaign_id, rb.version)] = mm
        return mm

    @serialized
    def build_hook_sheets(self, campaign_id: str) -> list[HookSheet]:
        rb = self._live(campaign_id)
        mm = self.moment_maps.get((campaign_id, rb.version))
        if mm is None:
            raise PreconditionFailed(f"no Moment Map for {campaign_id} v{rb.version}")
        sh = hook_angle.sheets(mm, rb)
        self.recorder.record("hook_sheets_built", A_HOOKS, self._subject(campaign_id, rb.version),
                             {"sheets": [s.model_dump(mode="json") for s in sh]},
                             f"{len(sh)} hook sheet(s) for {campaign_id} v{rb.version}")
        self.hook_sheets[(campaign_id, rb.version)] = sh
        return sh

    @serialized
    def build_kit(self, campaign_id: str, req: KitRequest) -> CampaignKit:
        rb = self._live(campaign_id)
        mm = self.moment_maps.get((campaign_id, rb.version))
        sh = self.hook_sheets.get((campaign_id, rb.version))
        if mm is None or sh is None:
            raise PreconditionFailed(f"the kit needs a Moment Map and hook sheets for {campaign_id} v{rb.version}")
        sha = content_sha256({"campaign_id": campaign_id, "version": rb.version, "request": req.model_dump(mode="json")})
        held = self._pending_new.get("kit", sha)
        # consumed only once the kit is recorded — or its outcome is unknown (LOST sweep)
        kit_id = held[0] if held is not None else f"kit-{self._n:04d}"
        kit = campaign_kit.build(kit_id, rb, mm, sh, req)

        def burn() -> None:
            self._n += 1

        _, kit = self._pending_new.record(
            self.recorder, "kit", sha, kit_id, kit, burn, "campaign_kit_built", A_KIT, kit_id, kit.model_dump(mode="json"),
            f"Kit {kit_id} for {campaign_id} v{rb.version}: {len(kit.seeds)} seed clip spec(s); "
            f"{len(kit.commissions)} commission request(s) to Enigma/Phantom Canvas follow")
        self.kits[campaign_id] = kit
        receipts = campaign_kit.commission_seeds(kit, self.departments.creative_agents)
        try:
            self.recorder.record("crossing_creative_agents", A_KIT, kit_id, {"kit_id": kit_id, "receipts": receipts},
                                 f"Kit {kit_id}: {sum(bool(r['commissioned']) for r in receipts)}/{len(receipts)} "
                                 "seed-clip commissions accepted by Enigma/Phantom Canvas")
        except LedgerRecordError as exc:
            raise OutcomeNotRecorded(
                f"kit {kit_id} WAS built and recorded and its {len(receipts)} commission request(s) were sent, but "
                f"recording the agents' answers failed ({exc}); the answers are not applied",
                {"kit_id": kit_id, "recorded": ["campaign_kit_built"], "outside_calls_made": len(receipts),
                 "not_recorded": "crossing_creative_agents"}) from exc
        kit = campaign_kit.apply_receipts(kit, receipts)
        self.kits[campaign_id] = kit
        return kit

    @serialized
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
    def review_clip_unlocked(self, sub: ClipSubmission) -> "ClipPlan | None":
        """Fix wave 9 (AEGIS round 8 M1): Clip Review is pure over its inputs
        (the submission, one frozen rulebook version, the registry rows, the
        receipt time), and a worst-case review costs a second of CPU; run
        under the service-wide workflow lock it stalled every brief and
        rulebook action behind it. The inputs are taken under the lock (a
        few dictionary reads and a copy of the registry rows), the review
        runs WITHOUT it, and `submit_clip(sub, plan)` commits under the lock
        only if every input is still what the review saw — otherwise it
        reviews again, under the lock, on the current state. None when there
        is nothing to precompute (a precondition fails, or an uncertain
        attempt is to be replayed): `submit_clip` then does everything,
        refusals included, exactly as before."""
        with self.recorder.lock:
            cid = sub.campaign_id
            kit = self.kits.get(cid)
            if kit is None or kit.status != "signed" or sub.submission_id in self.submissions:
                return None
            sha = submission_sha256(sub)
            bound = self._submission_content.get(sub.submission_id)
            if bound is not None and bound != sha:
                return None
            op = f"clip:{sub.submission_id}"
            held = self._attempts.get(op)
            if held is not None and held.uncertain:
                return None  # a replay (or a refusal of different content) decides; nothing to review
            try:
                rb = self.rulebooks.get(cid, sub.rulebook_version)
            except CreativeError:
                return None
            if rb.live_at is None:
                return None
            received_at = self._op_now(op, sha)
            now = self.clock.now()
            if sub.posted_at > now or sub.posted_at < rb.live_at or (
                    rb.superseded_at is not None and sub.posted_at >= rb.superseded_at):
                return None
            to_human = self._grace_route(rb, received_at)
            rows = dict(self.registry.rows)
            attempt = self._attempts.get(op)
        snapshot = PlatformRulesRegistry(rows=rows)
        decision = clip_review.review(sub, rb, snapshot, received_at, decided_by=A_REVIEW,
                                      received_at=received_at, route_to_human=to_human)
        return ClipPlan(sha, rb, received_at, to_human, rows, attempt, decision)

    def _grace_route(self, rb: Rulebook, received_at: datetime) -> tuple[str, ...]:
        grace = timedelta(hours=self.superseded_grace_hours)
        if rb.superseded_at is not None and received_at - rb.superseded_at > grace:
            return (f"declared v{rb.version} was superseded at {rb.superseded_at.isoformat()}; this clip reached "
                    f"us at {received_at.isoformat()}, beyond the {self.superseded_grace_hours}h grace window, and "
                    "its posted_at is self-asserted — a human confirms which version it was made under",)
        return ()

    def _plan_still_holds(self, plan: "ClipPlan", sha: str, op: str, rb: Rulebook) -> bool:
        """Every input the unlocked review read is still the current one."""
        rows = self.registry.rows
        return (plan.sha == sha and plan.rb is rb and self._attempts.get(op) is plan.attempt
                and len(rows) == len(plan.rows) and all(rows.get(k) is v for k, v in plan.rows.items()))

    @serialized
    def submit_clip(self, sub: ClipSubmission, plan: "ClipPlan | None" = None) -> ClipReviewDecision:
        cid = sub.campaign_id
        kit = self.kits.get(cid)
        if kit is None or kit.status != "signed":
            raise PreconditionFailed(f"campaign {cid} is not open for clips: Andre has not signed its kit")
        if sub.submission_id in self.submissions:
            raise PreconditionFailed(f"submission {sub.submission_id} already exists")
        sha = submission_sha256(sub)
        bound = self._submission_content.get(sub.submission_id)
        if bound is not None and bound != sha:
            self._refuse(PreconditionFailed(
                f"submission id {sub.submission_id} was already used (an earlier attempt whose record failed) with "
                "different content; a submission id is bound to its first content — submit under a new id"),
                A_REVIEW, sub.submission_id, "clip submission")
        op = f"clip:{sub.submission_id}"
        replayed = self._replay_uncertain(op, sha)  # LOST: the first attempt's exact record and decision
        if replayed is not None:
            self.submissions[sub.submission_id] = sub
            self.decisions[sub.submission_id] = replayed
            return replayed
        rb = self.rulebooks.get(cid, sub.rulebook_version)
        if rb.live_at is None:
            raise PreconditionFailed(f"rulebook {cid} v{rb.version} never went live; clips can't be made under it")
        # the unlocked review's inputs, if they all still hold (M1); else review here, under the lock
        fresh = plan is not None and self._plan_still_holds(plan, sha, op, rb)
        # server receipt time; the first attempt's only on an identical retry inside RETRY_WINDOW (N1)
        received_at = plan.received_at if fresh else self._op_now(op, sha)
        if sub.posted_at > self.clock.now():
            raise PreconditionFailed(
                f"posted_at {sub.posted_at.isoformat()} is in the future (server time {self.clock.now().isoformat()})")
        if sub.posted_at < rb.live_at or (rb.superseded_at is not None and sub.posted_at >= rb.superseded_at):
            raise PreconditionFailed(
                f"clip posted {sub.posted_at.isoformat()} is outside v{rb.version}'s live window; "
                "declare the version that was live when it was made")
        to_human = self._grace_route(rb, received_at)
        if fresh and to_human == plan.to_human:
            decision = plan.decision
        else:
            decision = clip_review.review(sub, rb, self.registry, received_at, decided_by=A_REVIEW,
                                          received_at=received_at, route_to_human=to_human)
        # The id is bound to this content from the first attempt that reaches the ledger call on.
        self._submission_content[sub.submission_id] = sha
        self._record_op(op, sha, received_at, decision, "clip_reviewed", A_REVIEW, sub.submission_id,
                        {"decision": decision.model_dump(mode="json"), "submission": sub.model_dump(mode="json"),
                         "content_sha256": sha, "received_at": received_at.isoformat(),
                         "superseded_grace_hours": self.superseded_grace_hours},
                        f"Clip {sub.submission_id} under {cid} v{rb.version}: {decision.outcome}"
                        + (f" (broke {', '.join(b.rule_id for b in decision.broken_rules)})" if decision.broken_rules else ""),
                        op_key={"submission_id": sub.submission_id, "content_sha256": sha})
        self.submissions[sub.submission_id] = sub
        self.decisions[sub.submission_id] = decision
        return decision

    def get_decision(self, submission_id: str) -> ClipReviewDecision:
        d = self.decisions.get(submission_id)
        if d is None:
            raise NotFound(f"submission {submission_id!r} not found")
        return d

    @serialized
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
        op = f"human:{submission_id}"
        sha = _sha256({"reviewer": reviewer_id, "verdict": verdict.model_dump(mode="json")})
        if self._pending_other_content(op, sha):
            raise PreconditionFailed(
                f"a different human verdict on {submission_id} was sent and the ledger's answer was lost, so its "
                "outcome is unknown (it may already be on the ledger); re-send that same verdict to resolve it, "
                "or withdraw it (POST .../human-review/withdraw) once the ledger confirms it holds no such event")
        replayed = self._replay_uncertain(op, sha)  # LOST
        if replayed is not None:
            self.decisions[submission_id] = replayed
            return replayed
        decided_at = self._op_now(op, sha)
        try:
            decision = clip_review.make_decision(
                rb, submission_id=submission_id, campaign_id=sub.campaign_id, rulebook_version=rb.version,
                outcome=verdict.outcome, broken_rules=tuple(verdict.broken_rules), human_review_reasons=(),
                checks=current.checks, decided_by=reviewer_id, decided_at=decided_at,
                received_at=current.received_at,
            )
        except (RuleCitationError, ValueError) as exc:
            self._refuse(ValidationFailed("human verdict cites rules that are not in this rulebook version",
                                          [str(exc)]), reviewer_id, submission_id, "human clip review")
        self._record_op(op, sha, decided_at, decision, "clip_human_reviewed", reviewer_id, submission_id,
                        {"decision": decision.model_dump(mode="json"), "note": verdict.note,
                         "previous_reasons": list(current.human_review_reasons)},
                        f"Human review of {submission_id}: {decision.outcome}"
                        + (f" (broke {', '.join(b.rule_id for b in decision.broken_rules)})" if decision.broken_rules else ""))
        self.decisions[submission_id] = decision
        return decision

    @serialized
    def withdraw_uncertain_verdict(self, submission_id: str, reviewer_id: str) -> dict:
        """Fix wave 5 (LOW-D): clear an UNCERTAIN human verdict without
        re-sending it. Only the reviewer who sent it (authenticated), only
        once WITHDRAW_MIN_AGE has passed since it was sent, and only if the
        ledger, read now, holds no entry under that verdict's event id.
        The withdrawal is itself recorded first (`clip_human_verdict_withdrawn`,
        naming the withdrawn event id; deterministic payload, so a retry
        after a lost response re-sends the identical record); then the clip
        is back in the queue and any verdict can be given. If the ledger
        DOES hold it, nothing changes: re-send the same verdict to commit
        it. Residual (documented, ADR 0005 decision 21): ledger-rust's
        blocking pool is not time-bounded, so an append queued there for
        longer than WITHDRAW_MIN_AGE could still land AFTER the withdrawal;
        the ledger then shows the withdrawal before the verdict it names,
        which an audit can see."""
        self.get_decision(submission_id)
        self._guard(lambda: self.actors.require_role(reviewer_id, Role.ZBC_CLIP_HUMAN_REVIEWER),
                    reviewer_id, submission_id, "withdraw human verdict")
        op = f"human:{submission_id}"
        held = self._attempts.get(op)
        if held is None or not held.uncertain:
            raise PreconditionFailed(f"no human verdict on {submission_id} is awaiting an uncertain ledger outcome")
        (event_type, sender, *_), _ = held.record
        if sender != reviewer_id:
            self._refuse(GuardrailViolation(f"only {sender!r}, who sent the uncertain verdict on {submission_id}, "
                                            "may withdraw it"), reviewer_id, submission_id, "withdraw human verdict")
        age = self.clock.now() - (held.sent_at or held.at)
        if age < WITHDRAW_MIN_AGE:
            raise PreconditionFailed(
                f"the uncertain verdict on {submission_id} was sent {int(age.total_seconds())} s ago; a request "
                f"still in flight could yet be recorded, so it can be withdrawn only after "
                f"{int(WITHDRAW_MIN_AGE.total_seconds())} s (or re-send the same verdict now)")
        if self.recorder.client.find_event(held.event_id) is not None:
            raise PreconditionFailed(
                f"the ledger holds the verdict on {submission_id} (event {held.event_id}); it can't be withdrawn — "
                "re-send the same verdict to commit it")
        eid = self.recorder.record(
            "clip_human_verdict_withdrawn", reviewer_id, submission_id,
            {"withdrawn_event_id": held.event_id, "withdrawn_event_type": event_type, "verdict_sha256": held.sha},
            f"Human verdict on {submission_id} withdrawn by {reviewer_id}: ledger holds no event {held.event_id}")
        self._attempts.pop(op, None)
        return {"submission_id": submission_id, "withdrawn_event_id": held.event_id, "event_id": eid,
                "outcome": self.decisions[submission_id].outcome}

    @serialized
    def payout_eligibility(self, submission_id: str) -> dict:
        decision = self.get_decision(submission_id)
        sub = self.submissions[submission_id]
        res = payout_eligibility.evaluate(
            sub, decision,
            RecordedPort(self.departments.verification, "verification_integrity", self.recorder, A_ELIGIBILITY, submission_id),
            RecordedPort(self.departments.compliance, "compliance_38", self.recorder, A_ELIGIBILITY, submission_id))
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
    @serialized
    def learn_result(self, result: ClipResult) -> MemoryDecision:
        sub = self.submissions.get(result.submission_id)
        if sub is None or sub.campaign_id != result.campaign_id:
            raise NotFound(f"no reviewed submission {result.submission_id} in campaign {result.campaign_id}")
        decision = self.memory.evaluate(
            result, RecordedPort(self.departments.verification, "verification_integrity", self.recorder, A_MEMORY,
                                 result.result_id))
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
