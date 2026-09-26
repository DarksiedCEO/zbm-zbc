"""
Intelligence 5 — Kit Delivery (spec §C.5, CN-14, CN-22).

Delivers a reference to Creative's Andre-signed kit of the LIVE rulebook
version (never a copy edited by CN) with the SHA-256 of the kit exactly as
read, and records the clipper's acknowledgment of the rulebook, the
disclosure section and the rate-card version. A new rulebook version means a
new delivery; clips are judged by the version they were made under.
"""

from __future__ import annotations

from typing import Optional

from intelligences.common import item
from ports import KitAnswer

NUMBER, NAME, ACTOR = 5, "Kit Delivery", "intel_05_kit_delivery"


def kit_problem(kit: KitAnswer, live_version: Optional[int]) -> Optional[dict]:
    if not kit.available:
        return item("KIT_UNAVAILABLE", "CN-14", "Creative's kit could not be read: nothing delivered", "creative_production")
    if kit.status != "signed" or live_version is None or kit.rulebook_version != live_version or not kit.kit_sha256:
        return item("KIT_NOT_SIGNED", "CN-14", "only Creative's signed kit of the live rulebook version is delivered",
                    "creative_production", kit.kit_id)
    return None


def ack_problem(delivery: dict, body: dict) -> Optional[str]:
    if body["kit_delivery_id"] != delivery["kit_delivery_id"]:
        return "this acknowledgment names another kit delivery"
    if body["kit_sha256"] != delivery["kit_sha256"] or body["rulebook_version"] != delivery["rulebook_version"]:
        return "the acknowledged kit hash or rulebook version is not the one delivered"
    if body["rate_card_version"] != delivery["rate_card_version"]:
        return "the acknowledged rate-card version is not the one delivered"
    if delivery.get("acknowledged_at"):
        return "already acknowledged"
    return None
