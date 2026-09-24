"""
ZBM intelligence 5 — Rights and Provenance.

Job: prove ZBM may use every asset in a piece of work.
Decides: cleared / not cleared, with a reason per asset.

Fails closed:
- no declared assets                      -> not cleared (undeclared assets can't be proven);
- an asset not listed in the approved brief's rights_and_permissions -> not cleared;
- an asset with no clearance record, or none granting the brief's use today -> not cleared;
- AI generative fill is OFF by default; turning it on needs every asset's
  record to grant it AND a Legal (37) sign-off — the Legal stand-in says
  "not allowed yet", so today any work using generative fill is not cleared.

Content Credentials (c2pa-rs) stamping is an interface only today: the
result reports `provenance_stamp` as not applied; it is not a blocker,
because the spec schedules the stamp for later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from shared.departments import GateResult, Legal37Port
from shared.media import C2paStandIn, NotIntegrated, ProvenanceStamper
from shared.rights import AssetCheck, RightsRegistry, Use
from zbm.brief import RightsNeed


@dataclass
class RightsProvenanceResult:
    cleared: bool
    asset_checks: list[AssetCheck] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    provenance_stamp: str = ""
    legal_crossing: GateResult | None = None


def check(
    work_id: str,
    asset_ids: list[str],
    brief_needs: list[RightsNeed],
    uses_ai_generative_fill: bool,
    rights: RightsRegistry,
    legal: Legal37Port,
    today: date,
    stamper: ProvenanceStamper | None = None,
) -> RightsProvenanceResult:
    res = RightsProvenanceResult(cleared=False)
    if not asset_ids:
        res.blockers.append("no assets declared; rights for undeclared assets cannot be proven")
    needs = {n.asset_id: n for n in brief_needs}
    for asset_id in asset_ids:
        need = needs.get(asset_id)
        if need is None:
            res.blockers.append(f"{asset_id}: not listed in the approved brief's rights_and_permissions")
            continue
        chk = rights.check_asset(asset_id, need.use, today)
        res.asset_checks.append(chk)
        if not chk.cleared:
            res.blockers.append(f"{asset_id} ({need.asset_kind.value}): {chk.reason}")
    if uses_ai_generative_fill:
        for asset_id in asset_ids:
            chk = rights.check_asset(asset_id, Use.AI_GENERATIVE_FILL, today)
            if not chk.cleared:
                res.blockers.append(f"{asset_id}: AI generative fill not granted ({chk.reason})")
        gate = legal.signoff("ai_generative_fill", work_id, {"asset_ids": asset_ids})
        res.legal_crossing = gate
        if not gate.allowed:
            res.blockers.append(f"AI generative fill needs a rights sign-off: {gate.reason}")

    stamp = (stamper or C2paStandIn()).stamp(work_id, {"assets": asset_ids})
    res.provenance_stamp = f"not_applied: {stamp.reason}" if isinstance(stamp, NotIntegrated) else f"applied: {stamp}"
    res.cleared = not res.blockers
    return res
