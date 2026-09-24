"""
Intelligence 12 — Brand Campaign (ZBC).  PHASE 3.

Decides: the distribution plan (rented-first) and the minimal proving
campaign for a ZBC brand.

Rules (Sep 24 2026 update to the locked spec):
- Distribution is RENTED (clips on ZBC creators' accounts) for every brand.
  Always. Owned posting (on the brand's own accounts) is an OPTIONAL
  ADD-ON, offered to regulated brands; it never replaces rented. A
  non-regulated brand asking for owned posting is told it is an add-on for
  regulated brands and Andre decides — not silently granted.
- Value is proven with a small live campaign before scaling. The proving
  campaign's size is configuration (draft defaults, not confirmed):
  budget cap, creator count, days. A requested budget above the cap is
  capped, and the plan says so.
- ZBC approval is PER CAMPAIGN: ``approve`` needs the brand's explicit yes
  for THIS campaign id and THIS plan digest. A yes for another campaign,
  or for a changed plan, does not carry over.
- ``may_scale`` is false until a proving result is recorded with evidence
  (observed metrics). Success measures are what we will MEASURE, never a
  promised number (no guarantees).
- Every post needs ad disclosure (P9) — enforced at post time by
  practices.ad_disclosure.

NOT certified until all four certification types pass, before the first
brand campaign, with P9 live.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Optional

from onboarding_schema import CampaignPlan, DistributionModel, money_str

from ._status import PHASE3_STATUS

NUMBER = 12
NAME = "Brand Campaign (ZBC)"
PHASE = 3
STATUS = PHASE3_STATUS

PROVING_CREATORS = 3
PROVING_DAYS = 14
SUCCESS_MEASURES = [
    "views delivered (observed on the creators' platforms)",
    "click-throughs on campaign tracking links (observed)",
    "cost per 1,000 views (observed)",
    "brand-safety and disclosure compliance on every post",
]


def plan_digest(plan: CampaignPlan) -> str:
    body = plan.model_dump(mode="json", exclude={"approved", "scale_allowed"})
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def plan_campaign(
    brand_id: str,
    campaign_id: str,
    regulated: bool,
    wants_owned_addon: bool,
    requested_budget_usd: Optional[Decimal],
    budget_cap_usd: Decimal,
) -> CampaignPlan:
    if regulated:
        note = "Owned posting is available as an optional add-on for regulated brands, alongside rented distribution."
    elif wants_owned_addon:
        note = "Owned posting is an add-on for regulated brands; your request was noted for Andre to decide. Rented distribution is the plan."
    else:
        note = "Rented distribution on ZBC creators' accounts."
    selected = regulated and wants_owned_addon
    if requested_budget_usd is None:
        budget, budget_note = budget_cap_usd, "no budget requested; proving budget set to the cap"
    elif requested_budget_usd > budget_cap_usd:
        budget, budget_note = budget_cap_usd, "requested budget is above the proving-campaign cap; capped until value is proven"
    else:
        budget, budget_note = requested_budget_usd, "requested budget is within the proving-campaign cap"
    return CampaignPlan(
        brand_id=brand_id,
        campaign_id=campaign_id,
        distribution=DistributionModel.RENTED,
        owned_addon_offered=regulated,
        owned_addon_selected=selected,
        owned_addon_note=note,
        proving_campaign={
            "budget_usd": money_str(budget),
            "budget_note": budget_note,
            "creators": PROVING_CREATORS,
            "days": PROVING_DAYS,
            "ad_disclosure_required_on_every_post": True,
        },
        success_measures=list(SUCCESS_MEASURES),
    )


def approve(plan: CampaignPlan, brand_yes_campaign_id: str, brand_yes_digest: str) -> tuple[bool, str]:
    if brand_yes_campaign_id != plan.campaign_id:
        return False, "approval is per campaign: this yes was for a different campaign"
    if brand_yes_digest != plan_digest(plan):
        return False, "approval does not match this exact plan (it changed, or the yes was for another version)"
    return True, "brand approved this exact campaign plan"


def may_scale(plan: CampaignPlan, proving_result: Optional[dict]) -> tuple[bool, str]:
    if not plan.approved:
        return False, "campaign not approved"
    if not proving_result:
        return False, "no proving result recorded yet; value must be proven before scaling"
    if proving_result.get("evidence") != "observed" or not proving_result.get("views_delivered"):
        return False, "proving result has no observed evidence; not proven"
    return True, "proving campaign delivered observed results; scaling may be proposed to the brand"
