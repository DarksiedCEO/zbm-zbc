"""Suppression: is this address or number on the do-not-contact list (ADR 0013 decision 11).

Decides: whether any of a contact's keyed hashes (email, phone) is suppressed. The list is global across both brands,
append-only and never deletable: there is no removal path in this service at all. Opt-outs, hard bounces and
complaints all land here. Never: stores a raw address."""

from __future__ import annotations

from typing import Iterable

NUMBER = 5
NAME = "suppression"
DECIDES = "whether a contact may be contacted at all on a channel"

REASONS = ("unsubscribe", "stop_reply", "hard_bounce", "complaint", "manual", "declined_reply", "consent_revoked")
EMAIL_CHANNELS = ("email",)
PHONE_CHANNELS = ("sms", "voice")


def suppressed(suppression: dict, hashes: Iterable[str]) -> bool:
    return any(h in suppression for h in hashes if h)


def hashes_for(channel: str, contact: dict) -> list[str]:
    if channel in EMAIL_CHANNELS:
        return [contact.get("email_hash")]
    return [contact.get("phone_hash")]
