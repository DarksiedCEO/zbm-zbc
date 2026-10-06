"""Lead intake: which caller may bring a lead from which source, with which evidence (ADR 0013 decision 6).

Decides: whether (caller, source, evidence kind) is an allowed combination, and that referral and partner leads name
their referrer. Never: scores, routes or stores anything; never accepts a public-data or paid-provider lead over the
API (those come only through their ports)."""

from __future__ import annotations

NUMBER = 1
NAME = "lead_intake"
DECIDES = "source and evidence admissibility per caller"

SOURCES = ("inbound", "referral", "partner", "public_data", "paid_provider")
API_SOURCES = ("inbound", "referral", "partner")

# caller -> source -> evidence kinds that caller may present
ALLOWED = {
    "hub": {"inbound": ("site_form", "zbc_campaign_inquiry")},
    "onboarding": {"inbound": ("site_form",)},
    "detection": {"inbound": ("rr_scan",)},
    "dashboard": {"referral": ("referral_note",), "partner": ("partner_note",)},
}
PORT_EVIDENCE = {"public_data": ("public_record",), "paid_provider": ("provider_record",)}


def admissible(caller: str, source: str, evidence_kind: str) -> bool:
    return evidence_kind in ALLOWED.get(caller, {}).get(source, ())


def port_admissible(source: str, evidence_kind: str) -> bool:
    return evidence_kind in PORT_EVIDENCE.get(source, ())


def needs_referrer(source: str) -> bool:
    return source in ("referral", "partner")
