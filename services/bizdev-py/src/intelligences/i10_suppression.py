"""Suppression: is this address on the do-not-contact list (ADR 0016 decision 20). Copied from sales-py's
i05_suppression, email only.

Decides: whether a contact's keyed email hash is suppressed. The list is global across both brands, append-only and
never deletable: there is no removal path in this service at all. Opt-outs, hard bounces and complaints all land
here. Never: stores a raw address."""

from __future__ import annotations

NUMBER = 10
NAME = "suppression"
DECIDES = "whether a contact may be emailed at all"

REASONS = ("unsubscribe", "stop_reply", "hard_bounce", "complaint", "manual", "andre_opt_out")


def suppressed(suppression: dict, email_hash) -> bool:
    return bool(email_hash) and email_hash in suppression
