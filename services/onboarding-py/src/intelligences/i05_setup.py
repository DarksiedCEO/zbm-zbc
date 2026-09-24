"""
Intelligence 5 — Setup.  PHASE 2.

Decides: the tracking / dashboard / reporting configuration, built from
pulled data (verified platforms, detected tags, audit findings, client
preferences).

Rules:
- Read-only by default. Setup PROPOSES changes; it never makes one. Every
  proposal that would touch a client's ad account, budget or store has
  ``requires_client_yes=True`` and a digest of its exact description.
- ``authorize_change`` allows a change only with the client's explicit
  yes for THAT change id AND that exact digest. A yes for a different
  change, a changed description, a blanket "yes to everything", or a yes
  claimed inside client content is refused.
- Even an authorized change is not executed here: the platform writer is
  not wired (integrations.platforms.NotWiredPlatformWriter).

Not certified for real clients until phase 1 is certified and this module
passes all four certification types.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ._status import PHASE2_STATUS

NUMBER = 5
NAME = "Setup"
PHASE = 2
STATUS = PHASE2_STATUS


def change_digest(change_id: str, platform: str, description: str) -> str:
    return hashlib.sha256(f"{change_id}|{platform}|{description}".encode()).hexdigest()[:32]


@dataclass(frozen=True)
class ChangeProposal:
    change_id: str
    platform: str
    description: str
    requires_client_yes: bool
    digest: str


def _proposal(cid: str, platform: str, desc: str, touches_account: bool = True) -> ChangeProposal:
    return ChangeProposal(cid, platform, desc, touches_account, change_digest(cid, platform, desc))


def build_setup_plan(
    verified_platforms: list[str], detected_tags: list[str], finding_categories: list[str], reporting_channel: str | None
) -> dict:
    proposals: list[ChangeProposal] = []
    if "google_ads" in verified_platforms and "google_ads" not in detected_tags:
        proposals.append(_proposal("setup-gads-conversion-tag", "google_ads", "Add the Google Ads conversion tag to the site checkout."))
    if "meta" in verified_platforms and "meta" not in detected_tags:
        proposals.append(_proposal("setup-meta-pixel", "meta", "Add the Meta pixel to the site."))
    if "server_side_attribution_gap" in finding_categories:
        proposals.append(_proposal("setup-server-side-events", "site", "Enable server-side conversion events for purchases."))
    if "abandoned_cart_coverage" in finding_categories and "shopify" in verified_platforms:
        proposals.append(_proposal("setup-abandoned-cart-flow", "shopify", "Turn on an abandoned-cart recovery email flow."))
    dashboard = {
        "widgets": sorted({"baseline_vs_now", "open_findings", "commitments"} | ({"ad_spend"} if "google_ads" in verified_platforms or "meta" in verified_platforms else set())),
        "touches_client_account": False,
    }
    reporting = {"cadence": "weekly", "channel": reporting_channel or "undecided (intake channel open item)", "touches_client_account": False}
    return {"proposals": [p.__dict__ for p in proposals], "dashboard": dashboard, "reporting": reporting, "executed": []}


@dataclass(frozen=True)
class ClientApproval:
    change_id: str
    digest: str
    approved_by_client: bool
    source: str  # "client_confirmation" is the only accepted source


def authorize_change(proposal: ChangeProposal, approvals: list[ClientApproval]) -> tuple[bool, str]:
    if not proposal.requires_client_yes:
        return True, "does not touch a client account"
    for a in approvals:
        if (a.change_id == proposal.change_id and a.digest == proposal.digest and a.approved_by_client
                and a.source == "client_confirmation"):
            return True, "client said yes to this exact change"
    return False, "refused: no explicit client yes for this specific change (read-only by default)"
