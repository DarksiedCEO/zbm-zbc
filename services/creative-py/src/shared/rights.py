"""
Rights records — shared reference data both layers consult.

Two record types:
- `ClearanceRecord`: one asset (footage, music, likeness, ...) with the uses
  its rights holder granted and the validity window.
- `CampaignLicense`: the ZBC client's licence to ZBC for a campaign's
  source material — including whether it grants the right to SUBLICENSE
  to clippers (the ZBC business model depends on it).

Records are append-only (an id can't be overwritten). `contract_ref` is a
pointer into contract storage, which does not exist yet — this service
cannot verify the contract behind a record (gap, ADR 0005).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from pydantic import BaseModel, ConfigDict, model_validator

from shared.errors import PreconditionFailed
from shared.types import NonEmptyStr, SafeId


class AssetKind(str, Enum):
    FOOTAGE = "footage"
    MUSIC = "music"
    LIKENESS = "likeness"
    VOICE = "voice"
    BRAND_ASSET = "brand_asset"
    IMAGE = "image"
    FONT = "font"


class Use(str, Enum):
    PAID_ADVERTISING = "paid_advertising"
    ORGANIC_SOCIAL = "organic_social"
    SUBLICENSE_TO_CLIPPERS = "sublicense_to_clippers"
    AI_GENERATIVE_FILL = "ai_generative_fill"


class ClearanceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: SafeId
    asset_id: SafeId
    asset_kind: AssetKind
    rights_holder: NonEmptyStr
    permitted_uses: frozenset[Use]
    valid_from: date
    valid_until: date
    contract_ref: NonEmptyStr

    @model_validator(mode="after")
    def _window(self) -> "ClearanceRecord":
        if self.valid_until < self.valid_from:
            raise ValueError("valid_until before valid_from")
        return self


class CampaignLicense(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    license_id: SafeId
    campaign_id: SafeId
    licensor: NonEmptyStr
    licensee: NonEmptyStr
    covered_asset_ids: frozenset[SafeId]
    sublicense_to_clippers: bool
    ai_generative_fill_permitted: bool = False
    valid_from: date
    valid_until: date
    contract_ref: NonEmptyStr


@dataclass(frozen=True)
class AssetCheck:
    asset_id: str
    cleared: bool
    reason: str
    record_id: str | None = None


@dataclass
class RightsRegistry:
    records: dict[str, ClearanceRecord] = field(default_factory=dict)
    licenses: dict[str, CampaignLicense] = field(default_factory=dict)

    def check_new_record(self, record: ClearanceRecord) -> None:
        if record.record_id in self.records:
            raise PreconditionFailed(f"clearance record {record.record_id!r} already exists; records are append-only")

    def commit_record(self, record: ClearanceRecord) -> None:
        self.records[record.record_id] = record

    def check_new_license(self, lic: CampaignLicense) -> None:
        if lic.license_id in self.licenses:
            raise PreconditionFailed(f"licence {lic.license_id!r} already exists; licences are append-only")

    def commit_license(self, lic: CampaignLicense) -> None:
        self.licenses[lic.license_id] = lic

    def licenses_for(self, campaign_id: str) -> list[CampaignLicense]:
        return [l for l in self.licenses.values() if l.campaign_id == campaign_id]

    def check_asset(self, asset_id: str, use: Use, today: date) -> AssetCheck:
        """Cleared only if SOME record for this asset grants `use` today."""
        candidates = [r for r in self.records.values() if r.asset_id == asset_id]
        if not candidates:
            return AssetCheck(asset_id, False, "no clearance record")
        for r in candidates:
            if use in r.permitted_uses and r.valid_from <= today <= r.valid_until:
                return AssetCheck(asset_id, True, f"cleared by {r.record_id} ({r.contract_ref})", r.record_id)
        return AssetCheck(asset_id, False, f"clearance record(s) exist but none grants {use.value!r} on {today}")


# --- recording (reference-data writes go through the ledger too) --------------

def record_clearance(rights: RightsRegistry, recorder, actors, actor_id: str, record: ClearanceRecord) -> str:
    """Append a clearance record. Ledger first; on ledger failure nothing is stored."""
    from shared.actors import Role

    actors.require_role(actor_id, Role.RIGHTS_RECORDER)
    rights.check_new_record(record)
    event_id = recorder.record(
        "rights_record_added", actor_id, record.record_id,
        record.model_dump(mode="json"),
        f"Clearance record {record.record_id} for {record.asset_kind.value} {record.asset_id}",
    )
    rights.commit_record(record)
    return event_id


def record_license(rights: RightsRegistry, recorder, actors, actor_id: str, lic: CampaignLicense) -> str:
    from shared.actors import Role

    actors.require_role(actor_id, Role.RIGHTS_RECORDER)
    rights.check_new_license(lic)
    event_id = recorder.record(
        "campaign_license_added", actor_id, lic.license_id,
        lic.model_dump(mode="json"),
        f"Campaign licence {lic.license_id} for {lic.campaign_id}; sublicense_to_clippers={lic.sublicense_to_clippers}",
    )
    rights.commit_license(lic)
    return event_id
