"""
ZBC intelligence 5 — Rights and Clearance.

Job: prove the client licensed ZBC WITH the right to sublicense to clippers,
and flag every uncleared music, likeness or footage asset. Fails closed.
Decides: cleared / not cleared for a campaign's declared assets, with a
reason per flag.

Rules:
C1  there must be a client licence for this campaign valid today — none
    means not cleared ("no licence, no campaign");
C2  that licence must grant sublicense_to_clippers — clippers post from
    THEIR OWN accounts, so without it ZBC can't hand the footage on;
C3  footage / image / brand assets must be covered by a sublicensing
    licence (footage comes from the rights holder; nothing scraped);
C4  music, likeness and voice need a clearance record granting
    `sublicense_to_clippers` today — the client licence alone doesn't
    prove the client owns the song or the face;
C5  AI generative fill on creator footage is OFF unless the licence permits
    it AND Legal (37) signs off; the Legal stand-in says "not allowed yet".
C6  no declared assets -> not cleared (nothing can be proven).

`contract_ref` on each record is a pointer into contract storage, which
does not exist yet: this intelligence checks the RECORDS, it cannot read
the contract behind them (gap, ADR 0005).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel, ConfigDict

from shared.departments import GateResult, Legal37Port
from shared.rights import AssetKind, CampaignLicense, RightsRegistry, Use
from shared.types import SafeId

LICENCE_COVERED_KINDS = frozenset({AssetKind.FOOTAGE, AssetKind.IMAGE, AssetKind.BRAND_ASSET, AssetKind.FONT})
RECORD_REQUIRED_KINDS = frozenset({AssetKind.MUSIC, AssetKind.LIKENESS, AssetKind.VOICE})


class DeclaredAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: SafeId
    kind: AssetKind


@dataclass
class ClearanceResult:
    campaign_id: str
    cleared: bool
    license_id: str | None
    blockers: list[str] = field(default_factory=list)
    flags: list[dict] = field(default_factory=list)
    cleared_asset_ids: list[str] = field(default_factory=list)
    legal_crossing: GateResult | None = None

    def as_dict(self) -> dict:
        return {
            "campaign_id": self.campaign_id, "cleared": self.cleared, "license_id": self.license_id,
            "blockers": self.blockers, "flags": self.flags, "cleared_asset_ids": self.cleared_asset_ids,
            "legal_crossing": self.legal_crossing.__dict__ if self.legal_crossing else None,
        }


def _valid(lic: CampaignLicense, today: date) -> bool:
    return lic.valid_from <= today <= lic.valid_until


def check_campaign(campaign_id: str, assets: list[DeclaredAsset], rights: RightsRegistry, legal: Legal37Port,
                   today: date, uses_ai_generative_fill: bool = False) -> ClearanceResult:
    res = ClearanceResult(campaign_id, False, None)
    if not assets:
        res.blockers.append("C6 no assets declared; rights for undeclared assets cannot be proven")
    valid = [lic for lic in rights.licenses_for(campaign_id) if _valid(lic, today)]
    sublicensing = [lic for lic in valid if lic.sublicense_to_clippers]
    if not valid:
        res.blockers.append(f"C1 no client licence for campaign {campaign_id} valid on {today}")
    elif not sublicensing:
        res.blockers.append(
            "C2 client licence " + ", ".join(sorted(lic.license_id for lic in valid))
            + " does not grant the right to sublicense to clippers"
        )
    if sublicensing:
        res.license_id = sorted(lic.license_id for lic in sublicensing)[0]
    covered = set().union(*(lic.covered_asset_ids for lic in sublicensing)) if sublicensing else set()

    for a in assets:
        if a.kind in LICENCE_COVERED_KINDS:
            if a.asset_id in covered:
                res.cleared_asset_ids.append(a.asset_id)
            else:
                res.flags.append({"asset_id": a.asset_id, "kind": a.kind.value,
                                  "reason": "C3 not covered by a client licence that sublicenses to clippers "
                                            "(footage must come from the rights holder; nothing is scraped)"})
        else:  # RECORD_REQUIRED_KINDS
            chk = rights.check_asset(a.asset_id, Use.SUBLICENSE_TO_CLIPPERS, today)
            if chk.cleared:
                res.cleared_asset_ids.append(a.asset_id)
            else:
                res.flags.append({"asset_id": a.asset_id, "kind": a.kind.value,
                                  "reason": f"C4 uncleared {a.kind.value}: {chk.reason}"})

    if uses_ai_generative_fill:
        if not any(lic.ai_generative_fill_permitted for lic in sublicensing):
            res.blockers.append("C5 AI generative fill is off by default; the licence does not permit it")
        gate = legal.signoff("ai_generative_fill", campaign_id, {"asset_ids": [a.asset_id for a in assets]})
        res.legal_crossing = gate
        if not gate.allowed:
            res.blockers.append(f"C5 AI generative fill needs a rights sign-off: {gate.reason}")

    res.cleared = not res.blockers and not res.flags
    return res
