"""
The Obligation Register — data model (spec §B).

Rows are plain dicts with exactly the seed's fields, validated by
``ObligationRow`` (strict: unknown fields refused). A register VERSION is an
immutable tuple of rows plus its metadata; nothing edits a row in place.
Everything here is pure: the service decides when a version is published.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from jurisdictions import is_known_register_code
from ledger import canonical
from textguard import has_control_chars

ROW_ID_RE = re.compile(r"[A-Z0-9][A-Z0-9-]{1,39}")
JURISDICTION_RE = re.compile(r"ALL|EU|[A-Z]{2}|[A-Z]{2}-[A-Z0-9]{1,3}")
DOMAINS = ("advertising_disclosure", "reviews_metrics", "claims", "impersonation_ai", "email_sms", "children_age",
           "privacy", "accessibility", "consumer_contracts", "sanctions", "payments_msb", "tax", "rights_ip",
           "platform_policy", "political", "program", "house_rule", "counsel")
SOURCE_KINDS = ("statute", "reg", "guidance", "case-law", "platform-policy", "house-rule", "counsel-question")
QUALITIES = ("primary", "secondary", "vendor", "founder", "none")
STATUSES = ("verified", "unverified", "expired", "superseded")
OWNERS = ("i01_register", "i02_activation_gate", "i03_payout_gate", "i04_publish_gate", "i05_control_monitor",
          "i06_change_watcher", "i07_jurisdiction", "i08_sanctions", "i09_disclosure", "i10_accessibility",
          "i11_evidence_audit")
GATES = ("activation", "payout", "publish", "control")
CHECKS = (
    "engine_invariant", "jurisdiction_class", "eu_kit", "no_cold_outbound_ca", "age_18_plus",
    "age_method_highly_effective", "sanctions_clear_fresh", "sanctions_owners", "tax_form_on_file", "tin_match",
    "w8_current", "foreign_services_attestation", "rail_kyc_verified", "disclosure_training_attested",
    "creator_accounts_disclosed", "no_sentiment_pay", "no_review_suppression", "pooled_funds_false",
    "claims_substantiated", "hbnr_clause", "prohibited_category", "verified_views_attested", "disclosure_present",
    "platform_toggle", "in_video_label", "label_vocabulary", "local_label", "ai_disclosure_synthetic",
    "likeness_consent", "personal_use_attested", "music_source_declared", "rights_cleared_no_strike",
    "accessibility_pass", "consent_banner", "required_docs_current", "canspam_elements", "sms_consent_artifact",
    "subscription_terms", "chatbot_disclosure", "flag_blocks", "counsel_memo", "control_sla", "threshold_counter",
    "effective_date_reminder",
)
# Row parameters that hold jurisdiction lists (HR-05/06/07); their entries must be known codes (AEGIS N14-3).
JURISDICTION_PARAMS = ("operate", "operate_excludes", "region_required", "conditional", "refuse")
APPLIES_KEYS = ("lanes", "subject_kinds", "asset_types", "platforms", "jurisdictions", "flags_any", "flags_none")

# Shelf life by source_kind (spec B.1). statute/guidance/platform are the
# lead's defaults; reg, case-law and counsel memos (recorded as guidance) are
# spec choices flagged for Andre; house rules and counsel questions carry none.
SHELF_LIFE_DAYS: dict[str, Optional[int]] = {
    "statute": 180, "reg": 180, "guidance": 90, "case-law": 90, "platform-policy": 30,
    "house-rule": None, "counsel-question": None,
}

# The seed's own titles exceed B.1's 160 chars for three counsel rows (the
# report's questions verbatim, spec §E; CQ-02 is 223). The seed is binding
# and copied unchanged, so the limit enforced is 240 (ADR 0006, choice 3).
TITLE_MAX = 240
URL_MAX = 2048


def _date(v: Optional[str], name: str) -> Optional[date]:
    if v is None:
        return None
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ValueError(f"{name} must be YYYY-MM-DD or null")
    return date.fromisoformat(v)


def _url_ok(u: str) -> bool:
    return (isinstance(u, str) and 0 < len(u) <= URL_MAX and not has_control_chars(u) and " " not in u
            and (u.startswith("https://") or u.startswith("http://") or u.startswith("urn:")))


class AppliesWhen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lanes: Optional[list[Literal["client", "zbc_brand", "zbc_creator"]]] = Field(default=None, max_length=3)
    subject_kinds: Optional[list[Literal["zbc_clip", "zbm_work"]]] = Field(default=None, max_length=2)
    asset_types: Optional[list[str]] = Field(default=None, max_length=20)
    platforms: Optional[list[str]] = Field(default=None, max_length=20)
    jurisdictions: Optional[list[str]] = Field(default=None, max_length=60)
    flags_any: Optional[list[str]] = Field(default=None, max_length=20)
    flags_none: Optional[list[str]] = Field(default=None, max_length=20)

    @field_validator("asset_types", "platforms", "flags_any", "flags_none")
    @classmethod
    def _slugs(cls, v):
        if v is not None:
            for s in v:
                if not re.fullmatch(r"[a-z0-9_]{1,40}", s):
                    raise ValueError("entries must be lowercase slugs")
        return v

    @field_validator("jurisdictions")
    @classmethod
    def _codes(cls, v):
        if v is not None:
            for s in v:
                if not JURISDICTION_RE.fullmatch(s) or not is_known_register_code(s):
                    raise ValueError("jurisdiction entries must be known ISO 3166 codes, EU or ALL")
        return v


class ObligationRow(BaseModel):
    """Exactly the seed's fields (spec B.1). Unknown fields -> refused."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    jurisdiction: str
    domain: Literal[DOMAINS]  # type: ignore[valid-type]
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    obligation: str = Field(min_length=1, max_length=1200)
    source_url: Optional[str]
    additional_sources: list[str] = Field(max_length=20)
    source_kind: Literal[SOURCE_KINDS]  # type: ignore[valid-type]
    source_quality: Literal[QUALITIES]  # type: ignore[valid-type]
    effective_date: Optional[str]
    effective_note: Optional[str] = Field(max_length=300)
    verified_at: Optional[str]
    expires_at: Optional[str]
    status: Literal[STATUSES]  # type: ignore[valid-type]
    owner_intelligence: Literal[OWNERS]  # type: ignore[valid-type]
    gates: list[Literal[GATES]] = Field(min_length=1, max_length=4)  # type: ignore[valid-type]
    applies_when: AppliesWhen
    check: Literal[CHECKS]  # type: ignore[valid-type]
    penalty_note: str = Field(max_length=1000)
    counsel_flag: StrictBool
    report_ref: Optional[str] = Field(max_length=200)
    parameters: dict[str, Any]

    @model_validator(mode="after")
    def _rules(self):
        if not ROW_ID_RE.fullmatch(self.id):
            raise ValueError("id must match ^[A-Z0-9][A-Z0-9-]{1,39}$")
        if not JURISDICTION_RE.fullmatch(self.jurisdiction) or not is_known_register_code(self.jurisdiction):
            raise ValueError("jurisdiction must be a known ISO 3166-1/-2 code, EU or ALL")
        for key in JURISDICTION_PARAMS:
            vals = self.parameters.get(key)
            if vals is not None and (not isinstance(vals, list)
                                     or not all(isinstance(c, str) and is_known_register_code(c) for c in vals)):
                raise ValueError(f"parameters.{key} must list known ISO 3166 codes")
        for name in ("title", "obligation", "penalty_note", "effective_note", "report_ref"):
            v = getattr(self, name)
            if isinstance(v, str) and has_control_chars(v):
                raise ValueError(f"{name} contains control characters")
        if self.source_url is not None and not _url_ok(self.source_url):
            raise ValueError("source_url must be an http(s) or urn: URL without spaces or control characters")
        for u in self.additional_sources:
            if not _url_ok(u):
                raise ValueError("additional_sources entries must be URLs")
        for name in ("effective_date", "verified_at", "expires_at"):
            _date(getattr(self, name), name)
        if len(set(self.gates)) != len(self.gates):
            raise ValueError("gates must not repeat")
        if len(canonical(self.parameters)) > 8192:
            raise ValueError("parameters larger than 8 KB")
        return self


def validate_row(row: dict) -> dict:
    """Validate and return the row dict unchanged (the dict is what is hashed)."""
    ObligationRow.model_validate(row)
    return row


def expected_expiry(row: dict) -> Optional[str]:
    """``verified_at + shelf life``; None for house rules, counsel questions and unverified rows."""
    life = SHELF_LIFE_DAYS.get(row["source_kind"])
    if life is None or row.get("verified_at") is None:
        return None
    return (date.fromisoformat(row["verified_at"]) + timedelta(days=life)).isoformat()


def effective_status(row: dict, today: date) -> str:
    """Spec B.3: verified and today >= expires_at -> expired (derived at read time)."""
    st = row["status"]
    if st == "verified" and row.get("expires_at") and today >= date.fromisoformat(row["expires_at"]):
        return "expired"
    return st


def is_effective(row: dict, today: date) -> bool:
    """A row feeds gates only when its effective_date is null or <= today (UTC)."""
    ed = row.get("effective_date")
    return ed is None or date.fromisoformat(ed) <= today


def rows_sha256(rows: list[dict]) -> str:
    ordered = sorted(rows, key=lambda r: r["id"])
    return hashlib.sha256(canonical(ordered).encode("utf-8")).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class Version:
    version: int
    created_at: str
    approved_by: str
    proposal_ids: tuple[str, ...]
    rows_sha256: str
    prev_version_sha256: Optional[str]
    rows: tuple[dict, ...] = field(repr=False)

    @property
    def meta(self) -> dict:
        return {"version": self.version, "created_at": self.created_at, "approved_by": self.approved_by,
                "proposal_ids": list(self.proposal_ids), "rows_sha256": self.rows_sha256,
                "prev_version_sha256": self.prev_version_sha256}

    @property
    def version_sha256(self) -> str:
        return sha256_text(canonical(self.meta))

    def by_id(self) -> dict[str, dict]:
        return {r["id"]: r for r in self.rows}


def seed_checks(rows: list[dict]) -> list[str]:
    """Guardrail H.31 on any row set: verified rows have a source_url unless
    house rules; every expires_at equals verified_at + shelf life; ids unique."""
    problems = []
    seen: set[str] = set()
    for r in rows:
        try:
            validate_row(r)
        except Exception as exc:  # noqa: BLE001 - reported, never raised past here
            problems.append(f"{r.get('id', '?')}: invalid row ({type(exc).__name__})")
            continue
        if r["id"] in seen:
            problems.append(f"{r['id']}: duplicate id")
        seen.add(r["id"])
        if r["status"] == "verified" and r["source_url"] is None and r["source_kind"] != "house-rule":
            problems.append(f"{r['id']}: verified without source_url")
        if r["status"] == "verified" and r["expires_at"] != expected_expiry(r):
            problems.append(f"{r['id']}: expires_at {r['expires_at']} != verified_at + shelf life")
        if r["status"] == "unverified" and (r["verified_at"] is not None or r["expires_at"] is not None):
            problems.append(f"{r['id']}: unverified row carries verification dates")
    return problems
