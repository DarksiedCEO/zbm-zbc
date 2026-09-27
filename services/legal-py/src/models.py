"""
Request models (Legal spec §E): strict (unknown key -> 422, no type coercion), bounded strings, no control
characters. Uploaded blobs travel as base64 (``content_b64``) or, for document text that templates are filled
into, as a UTF-8 string (``text``); either is at most 5 MiB decoded. No model has an IP, user-agent, device, DOB,
government-id or payment field (G6; ``i03_acceptance.FORBIDDEN_KEYS`` refuses such keys anywhere in a body before
parsing). Money appears only as BUILD_CONTRACTS §1 strings: ContractTerms.monthly_spend_cap_usd and the S4
``disputed_amount_usd`` compared with LEGAL_DISPUTE_THRESHOLD (ADR 0010 choice 21). No float anywhere (G8).
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

ID_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"
ID_RE = re.compile(ID_PATTERN)
Id = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
DocId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,60}$")]
Version = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,4}\.[0-9]{1,4}$")]
Code = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
CqId = Annotated[str, StringConstraints(pattern=r"^(CQ|VI-CQ|CN-CQ|FIN-CQ)-[0-9]{2}$")]
ObligationId = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9-]{1,39}$")]
ClauseId = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2,5}-[A-Z0-9]{2,12}-[0-9]{2}$")]
Money = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]{0,11})\.[0-9]{2}$")]
Date = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
PartyRef = Annotated[str, StringConstraints(pattern=r"^(clipper|client|counterparty):[A-Za-z0-9._-]{1,100}$")]
Ref = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:/-]{1,200}$")]
B64 = Annotated[str, StringConstraints(min_length=1, max_length=7_200_000, pattern=r"^[A-Za-z0-9+/=\s]+$")]
Entity = Literal["zbc", "zbm", "silverback"]
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
_CONTROL_ML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")


def _printable(v: str) -> str:
    if _CONTROL.search(v):
        raise ValueError("control characters are not allowed")
    return v


def _multiline(v: str) -> str:
    if _CONTROL_ML.search(v):
        raise ValueError("control characters other than tab/newline are not allowed")
    return v


def _date(v: str) -> str:
    from datetime import date
    date.fromisoformat(v)
    return v


def _rfc3339(v: str) -> str:
    from clock import parse_iso
    try:
        parse_iso(v)
    except ValueError:
        raise ValueError("must be an RFC 3339 timestamp with a UTC offset") from None
    return v


Text = Annotated[str, StringConstraints(min_length=1, max_length=2000), AfterValidator(_multiline)]
Short = Annotated[str, StringConstraints(min_length=1, max_length=200), AfterValidator(_printable)]
DocText = Annotated[str, StringConstraints(min_length=1, max_length=5_300_000), AfterValidator(_multiline)]
ClauseText = Annotated[str, StringConstraints(min_length=1, max_length=20_000), AfterValidator(_multiline)]
IsoDate = Annotated[Date, AfterValidator(_date)]
Timestamp = Annotated[str, StringConstraints(min_length=1, max_length=40), AfterValidator(_rfc3339)]
Tri = Union[bool, Literal["unknown"]]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RunRequest(Strict):
    request_id: Id


# --- documents ---------------------------------------------------------------------------------------------------

class ClauseUse(Strict):
    clause_id: ClauseId
    position: Literal["standard", "fallback_1", "fallback_2"]


class DocVersionCreate(Strict):
    """No ``version`` field (AEGIS N17-5): Legal assigns the number, monotonic per document. Andre may ask for the
    next MAJOR number (``bump``); every other version is the next minor. A fill names the party it is for
    (``party_ref``, AEGIS N17-4): only that party can ever accept it."""
    request_id: Id
    entity: Entity
    text: Optional[DocText] = None                       # Andre's upload (counsel's draft)
    template_variables: Optional[dict] = None            # typed schema for {{placeholders}} in text
    clause_ids: list[ClauseUse] = Field(default_factory=list, max_length=200)
    variables: Optional[dict] = None                     # scheduler: fill the current version's placeholders
    party_ref: Optional[PartyRef] = None                 # required on a fill; optional on Andre's upload
    bump: Literal["major", "minor"] = "minor"            # Andre's upload only
    supersedes: Optional[Version] = None


class CounselReview(Strict):
    request_id: Id
    question_text: Text
    detected_change_sha256: Optional[Sha256] = None
    proposed_edit_text: Optional[ClauseText] = None
    facts: dict = Field(default_factory=dict)


class CounselSignoff(Strict):
    request_id: Id
    counsel_ref: Ref
    signed_on: IsoDate
    doc_sha256: Sha256
    memo_id: Optional[Id] = None
    memo_sha256: Optional[Sha256] = None
    countersignature_b64: Optional[B64] = None           # engagement letter only: counsel's countersigned copy


class DocDecision(Strict):
    request_id: Id
    decision: Literal["approve", "retire", "withdraw"]
    version_sha256: Sha256
    effective_at: Optional[Timestamp] = None


# --- acceptances and envelopes ------------------------------------------------------------------------------

class EsignConsent(Strict):
    disclosure_version: Version
    consented_at: Timestamp
    access_demonstrated: Literal[True]


class EvidenceRef(Strict):
    kind: Literal["session_ref"]
    sha256: Sha256
    content_b64: Optional[Annotated[str, StringConstraints(max_length=200_000, pattern=r"^[A-Za-z0-9+/=]+$")]] = None


class AcceptanceCreate(Strict):
    request_id: Id
    party_ref: PartyRef
    signer_identity_ref: Ref
    doc_id: DocId
    version: Version
    doc_sha256: Sha256
    presented_sha256: Sha256
    method: Literal["clickwrap_unticked_box"]
    presentation: Literal["scroll_to_accept", "link", "inline"]
    affirmative_act: bool
    esign_consent: Optional[EsignConsent] = None
    evidence_ref: Optional[EvidenceRef] = None


class EnvelopeCreate(Strict):
    request_id: Id
    doc_id: DocId
    version: Version
    party_ref: PartyRef
    signer_refs: list[Ref] = Field(min_length=1, max_length=10)


class ESignEvent(Strict):
    request_id: Id
    envelope_id: Ref
    status: Literal["completed", "declined", "voided"]
    signed_document_sha256: Optional[Sha256] = None
    certificate_b64: Optional[B64] = None


# --- playbooks ---------------------------------------------------------------------------------------------------

class Position(Strict):
    clause_id: ClauseId
    text: ClauseText


class PlaybookReview(Strict):
    request_id: Id
    our_template_version: Optional[Version] = None
    counterparty_positions: list[Position] = Field(default_factory=list, max_length=200)
    facts: dict[Code, Tri] = Field(default_factory=dict, max_length=100)
    counterparty_paper_text: Optional[DocText] = None


class PlaybookBody(Strict):
    playbook_id: Code
    doc_type: Code
    version: Version
    clauses: list[dict] = Field(min_length=1, max_length=200)


class PlaybookProposal(Strict):
    request_id: Id
    playbook: PlaybookBody
    counsel_memo_id: Optional[Id] = None


class PlaybookDecision(Strict):
    request_id: Id
    proposal_id: Id
    content_sha256: Sha256
    decision: Literal["approve", "reject"]
    acknowledge_weakening: bool = False


# --- obligations and contract storage ---------------------------------------------------------------------------

class DoneEvidence(Strict):
    ref: Ref
    sha256: Sha256


class ObligationDone(Strict):
    request_id: Id
    evidence: DoneEvidence


class ObligationWaive(Strict):
    request_id: Id
    memo_id: Optional[Id] = None


class ObligationEntry(Strict):
    request_id: Id
    memo_id: Id
    doc_id: DocId
    version: Version
    party: Literal["zbc", "zbm", "counterparty"]
    counterparty_ref: PartyRef
    obligation_code: Code
    due: Optional[IsoDate] = None
    alert_lead_days: int = Field(ge=0, le=365)
    owner_department: Literal["onboarding", "finance_31", "creative_production", "clipper_network", "compliance_38",
                              "legal_37", "andre"]


class ContractTermsModel(Strict):
    client_id: Id
    signed: bool
    signed_at: Optional[Timestamp] = None
    start_date: IsoDate
    end_date: Optional[IsoDate] = None
    services: list[Annotated[str, StringConstraints(max_length=64), AfterValidator(_printable)]] = Field(max_length=50)
    allowed_commitment_categories: list[Annotated[str, StringConstraints(max_length=64), AfterValidator(_printable)]] = \
        Field(max_length=50)
    monthly_spend_cap_usd: Optional[Money] = None
    ccpa_cpra_clause_present: bool


class Executed(Strict):
    doc_id: DocId
    version: Version
    doc_sha256: Sha256
    acceptance_id: Id


class ContractTermsPut(Strict):
    request_id: Id
    terms: ContractTermsModel
    executed: Executed


# --- register and memos -------------------------------------------------------------------------------------------

class Invalidate(Strict):
    request_id: Id
    source_ref: Ref
    detected_change_sha256: Sha256


class Cites(Strict):
    cq_ids: list[CqId] = Field(default_factory=list, max_length=100)
    obligation_ids: list[ObligationId] = Field(default_factory=list, max_length=100)
    doc_versions: list[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,60}@[0-9]{1,4}\.[0-9]{1,4}$")]] = \
        Field(default_factory=list, max_length=100)
    clause_ids: list[ClauseId] = Field(default_factory=list, max_length=500)
    retention_classes: list[Code] = Field(default_factory=list, max_length=20)
    signoff_topics: list[Code] = Field(default_factory=list, max_length=20)


class Answer(Strict):
    cq_id: CqId
    resolution: Literal["verified_rule", "blocks_stay", "needs_more_facts"]
    quoted_excerpt: Optional[Text] = None


class MemoProposal(Strict):
    """One Compliance register proposal backed by a FILED memo (AEGIS N17-8, step 2): ``supersede`` a counsel
    question the memo answered, or ``amend`` / ``reverify`` an obligation row it cites. The row's ``source_url``
    must be ``urn:legal37:memos:<memo_id>`` -- the memo id exists before the row is typed."""
    kind: Literal["supersede", "amend", "reverify"]
    target_id: Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9-]{1,39}$")]
    proposed_row: dict
    quoted_excerpt: Text


class MemoProposals(Strict):
    request_id: Id
    proposals: list[MemoProposal] = Field(min_length=1, max_length=100)


class MemoIntake(Strict):
    request_id: Id
    counsel_ref: Ref
    memo_date: IsoDate
    content_b64: B64
    cites: Cites
    answers: list[Answer] = Field(default_factory=list, max_length=100)
    retention_periods: dict[Code, Annotated[str, StringConstraints(pattern=r"^P[0-9]{1,3}[YMD]$")]] = \
        Field(default_factory=dict, max_length=20)
    signoff_scopes: dict[Code, dict] = Field(default_factory=dict, max_length=20)


# --- matters, holds, takedowns ------------------------------------------------------------------------------------

class MatterFacts(Strict):
    class_action_threat: Tri = False
    counterparty_represented: Tri = False
    deadline_stated: Tri = False
    prior_dispute: Tri = False
    dsar: bool = False
    client_flowed: bool = False


class Deadlines(Strict):
    return_date: Optional[IsoDate] = None
    client_deadline: Optional[IsoDate] = None
    response_due: Optional[IsoDate] = None


class MatterIntake(Strict):
    request_id: Id
    channel: Literal["email", "portal", "mail", "phone", "hub", "department"]
    requester_ref: Ref
    kind: Literal["agency_letter", "subpoena", "litigation_threat", "demand_letter", "ip_claim", "contract_dispute",
                  "privacy_request", "data_incident", "routine_contract", "question"]
    facts: MatterFacts = Field(default_factory=MatterFacts)
    disputed_amount_usd: Optional[Money] = None
    subject_refs: list[Ref] = Field(default_factory=list, max_length=200)
    custodians: list[Ref] = Field(default_factory=list, max_length=200)
    systems: list[Literal["email", "chat", "drive", "legal_store", "finance_log", "vi_evidence", "cn_records",
                          "creative_store"]] = Field(default_factory=list, max_length=8)
    deadlines: Deadlines = Field(default_factory=Deadlines)
    retention_class: Literal["matters"] = "matters"


class MatterClose(Strict):
    request_id: Id
    memo_id: Optional[Id] = None


class HoldAck(Strict):
    request_id: Id
    custodian: Ref


class HoldRelease(Strict):
    request_id: Id
    memo_id: Optional[Id] = None


class TakedownTarget(Strict):
    kind: Literal["zbc_hosted", "platform_post"]
    post_ref_sha256: Sha256
    platform: Literal["tiktok", "youtube", "instagram", "x", "zbc_portal", "other"]


class Elements(Strict):
    signature: bool
    work_identified: bool
    material_located: bool
    contact: bool
    good_faith_statement: bool
    perjury_statement: bool


class TakedownIn(Strict):
    request_id: Id
    target: TakedownTarget
    elements: Elements
    arguable_elements: list[Literal["signature", "work_identified", "material_located", "contact",
                                    "good_faith_statement", "perjury_statement"]] = Field(default_factory=list,
                                                                                          max_length=6)
    uploader_ref: Optional[Ref] = None


class CounterNotice(Strict):
    request_id: Id
    checklist: dict[Code, bool] = Field(default_factory=dict, max_length=20)


class ClaimantAction(Strict):
    request_id: Id
    filed: Literal[True]
    court_ref_sha256: Optional[Sha256] = None


class OutboundNotice(Strict):
    request_id: Id
    target: TakedownTarget
    variables: dict = Field(default_factory=dict)
    license_or_fair_use_possible: bool
    counsel_memo_id: Optional[Id] = None


# --- filings, sign-offs, music ------------------------------------------------------------------------------------

class FilingCreate(Strict):
    request_id: Id
    entity: Entity
    kind: Literal["dmca_agent_designation", "tm_application", "tm_statement_of_use", "tm_section_8", "tm_section_9",
                  "tm_section_15", "sos_statement_of_information", "fbn_statement", "insurance_policy_notice"]
    reference: Optional[Ref] = None
    filed_on: Optional[IsoDate] = None
    registration_date: Optional[IsoDate] = None
    formation_month: Optional[int] = Field(default=None, ge=1, le=12)
    due_year: Optional[int] = Field(default=None, ge=2020, le=2100)
    window_opens: Optional[IsoDate] = None
    window_closes: Optional[IsoDate] = None
    expires_on: Optional[IsoDate] = None
    owner: Literal["andre", "legal_37"] = "andre"


class FilingFiled(Strict):
    request_id: Id
    filed_on: IsoDate
    reference: Ref


class SignoffRequest(Strict):
    request_id: Id
    topic: Code                                          # an id, never free text (AEGIS N17-7 structural rule)
    subject_id: Id
    facts: dict = Field(default_factory=dict)


class Music(Strict):
    present: bool
    source: Literal["commercial_library", "licensed", "none"]
    track_or_license_id: Optional[Ref] = None


class MusicRuling(Strict):
    request_id: Id
    subject_kind: Literal["zbc_clip", "zbm_work"]
    subject_id: Id
    platform: Literal["tiktok", "youtube", "instagram", "x", "facebook", "snapchat", "other"]
    paid: bool
    music: Music
    reposted_or_reedited_by_zbc: bool
    music_changed_since_approval: bool


# --- rules and reconcile ------------------------------------------------------------------------------------------

class RuleProposalRequest(Strict):
    request_id: Id
    kind: Literal["add", "amend", "retire"]
    target_id: Optional[Annotated[str, StringConstraints(pattern=r"^LG-[0-9]{2}[a-z]?$")]] = None
    proposed_row: Optional[dict] = None


class RuleDecision(Strict):
    proposal_id: Id
    content_sha256: Sha256
    decision: Literal["approve", "reject"]
    note: Optional[Short] = None
    acknowledge_weakening: bool = False


class RuleDecisions(Strict):
    request_id: Id
    decisions: list[RuleDecision] = Field(min_length=1, max_length=50)


class ReconcileRequest(Strict):
    request_id: Id
    head_sha256: Sha256
    void_lines: list[Annotated[int, Field(ge=1, le=10**12)]] = Field(max_length=10_000)
    void_event_ids: list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]] = \
        Field(max_length=10_000)
