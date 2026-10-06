"""Intake admissibility: which caller may bring which kind of influencer record, and the 18+ rule (ADR 0015
decisions 6 and 8).

Decides: ``inbound_application`` comes only from ``hub`` (the creator application form) and must carry the creator's
own attestation that they are 18 or older (``adult_18_plus`` exactly ``true``) with the attestation text's version and
SHA-256; ``manual_research`` (a person at Andre's console found the profile) comes only from ``dashboard`` with an
evidence reference; ``public_profile`` and ``paid_database`` come only through their discovery ports (never over the
record API). A prospect that did not attest yet is stored but can never get a brief, a deal, content, a contract or a
payout. Never: stores a date of birth or an age, or accepts an attestation from anyone but the creator's own form."""

from __future__ import annotations

from typing import Optional

NUMBER = 1
NAME = "intake_admissibility"
DECIDES = "who may bring which influencer record, and the 18+ attestation rule"

API_SOURCES = ("inbound_application", "manual_research")
PORT_SOURCES = ("public_profile", "paid_database")
SOURCE_CALLERS = {"inbound_application": ("hub",), "manual_research": ("dashboard",)}


def source_problem(source: str, caller: str) -> Optional[str]:
    if source in PORT_SOURCES:
        return "SOURCE_NOT_ALLOWED"
    if caller not in SOURCE_CALLERS.get(source, ()):
        return "SOURCE_NOT_ALLOWED"
    return None


def attestation_problem(adult_18_plus) -> Optional[str]:
    """Only a literal ``true`` passes. ``false`` is a declared minor; anything else is no attestation."""
    if adult_18_plus is True:
        return None
    if adult_18_plus is False:
        return "MINOR_REFUSED"
    return "AGE_ATTESTATION_REQUIRED"


def contractable(influencer: dict) -> Optional[str]:
    """None when a brief, deal, contract, content or payout may involve this influencer; else the refusal code."""
    if influencer.get("blocked"):
        return "INFLUENCER_BLOCKED"
    if influencer.get("adult_attested") is not True:
        return "AGE_ATTESTATION_REQUIRED"
    return None
