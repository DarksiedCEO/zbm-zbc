"""Suppression: is this influencer on the do-not-contact list (ADR 0015 decision 11).

Decides: whether ANY of an influencer's keyed hashes (email, every handle) is suppressed. The list is one list across
both brands (ZBM and ZBC), append-only and never deletable: there is no removal path in this service at all. An
opt-out on any channel (the one-click link, an opt-out reply, a complaint, a declared minor) suppresses every hash of
the influencer, so it stops email AND platform DMs for both brands. Never: stores a raw address or handle."""

from __future__ import annotations

from typing import Iterable

NUMBER = 4
NAME = "suppression"
DECIDES = "whether an influencer may be contacted at all"

REASONS = ("unsubscribe", "opt_out_reply", "hard_bounce", "complaint", "manual", "minor_declared", "hold_opt_out")


def suppressed(suppression: dict, hashes: Iterable[str]) -> bool:
    return any(h in suppression for h in hashes if h)


def hashes_of(influencer: dict) -> list[str]:
    out = [influencer.get("email_hash")] + [h["handle_hash"] for h in influencer.get("handles", ())]
    return [h for h in out if h]
