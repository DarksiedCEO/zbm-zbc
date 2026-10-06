"""Government bid certifications and representations (ADR 0016 decision 12): fail closed.

Decides: the checklist a public-sector bid carries. Every government bid gets the BASELINE items whatever the request
says; the agent may add items from OPTIONAL (or ``custom`` items with a label). Each item is something only Andre may
attest, one item at a time, naming the item's exact hash; nothing is ever attested automatically, by default, in bulk
or by an agent. SENSITIVE items (conflict of interest, gifts and gratuities, lobbying, contingent fees, and any custom item
whose label raises an i06 sensitivity flag) are flagged to Andre as review tasks when they are added. Never: fetches a bid portal, decides an answer, or attests."""

from __future__ import annotations

import hashlib
import json

from intelligences import i06_sensitivity

NUMBER = 5
NAME = "gov_bid_checklist"
DECIDES = "the certification / representation checklist of a government bid and which items are sensitive"

BASELINE = ("sam_registration_active", "debarment_suspension_certification", "independent_price_determination",
            "authority_to_bind", "conflict_of_interest_disclosure", "gifts_gratuities_certification",
            "lobbying_certification_disclosure", "contingent_fee_representation")
OPTIONAL = ("small_business_representation", "minority_women_owned_representation", "insurance_certificate",
            "nondiscrimination_certification", "drug_free_workplace", "buy_american", "data_security_attestation",
            "prevailing_wage", "accessibility_conformance", "references_provided", "custom")
SENSITIVE = frozenset({"conflict_of_interest_disclosure", "gifts_gratuities_certification",
                       "lobbying_certification_disclosure", "contingent_fee_representation"})
MAX_ITEMS = 60


def item_sha256(pursuit_id: str, code: str, label: str) -> str:
    return hashlib.sha256(json.dumps({"pursuit_id": pursuit_id, "code": code, "label": label}, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def build(pursuit_id: str, requested: list) -> list:
    """The checklist: BASELINE first (always), then each requested item once. ``requested`` items are
    ``{"code", "label"?}``; a ``custom`` item needs a label, a catalogue item takes none (its code is its label)."""
    seen, items = set(), []
    for code, label in [(c, c) for c in BASELINE] + [(r["code"], r.get("label") or r["code"]) for r in requested]:
        if code not in BASELINE and code not in OPTIONAL:
            raise ValueError("CHECKLIST_CODE_UNKNOWN")
        if code == "custom" and label == "custom":
            raise ValueError("CHECKLIST_LABEL_REQUIRED")
        if code != "custom" and label != code:
            raise ValueError("CHECKLIST_LABEL_REFUSED")
        key = (code, label.casefold())
        if key in seen:
            continue
        seen.add(key)
        sensitive = code in SENSITIVE or (code == "custom" and bool(i06_sensitivity.flags(label)))
        items.append({"code": code, "label": label, "sensitive": sensitive,
                      "item_sha256": item_sha256(pursuit_id, code, label)})
    if len(items) > MAX_ITEMS:
        raise ValueError("CHECKLIST_TOO_LARGE")
    return items
