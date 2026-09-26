"""
Intelligence 1 — Recruiting (spec §C.1, CN-08, CN-09, CN-26).

Decides who may be contacted, on which channel, with which template. Never
contacts anyone without an opt-in record or an inbound request. A recipient
is refused BEFORE any provider call when: it is a phone number (SMS is off,
CN-09 / CN-CQ-07); it has no opt-in record, or its opt-in was withdrawn
(CN-08); its opt-in's country is one CN-08 refuses (Canada: no cold
outbound, CASL unverified); the channel is not enabled (CN-09). The
recruiting template is always commercial and passes Compliance's publish
gate as ``email_campaign`` before any send (the service asks, record-first).
"""

from __future__ import annotations

from typing import Optional

from intelligences.common import item

NUMBER, NAME, ACTOR = 1, "Recruiting", "intel_01_recruiting"


def recipient_problem(kind: str, opt_in: Optional[dict], opted_out: bool, refuse_countries: list[str]) -> Optional[dict]:
    if kind == "phone":
        return item("SMS_OFF", "CN-09", "recipient is a phone number: SMS and calls are off (CN-09, counsel CN-CQ-07)")
    if kind != "email":
        return item("RECIPIENT_INVALID", "CN-08", "recipient is not an email address with an opt-in record")
    if opted_out:
        return item("OPTED_OUT", "CN-08", "recipient opted out; honored immediately")
    if opt_in is None:
        return item("NO_OPT_IN", "CN-08", "no opt-in record for this recipient: no cold outbound")
    if opt_in.get("recipient_country") in refuse_countries:
        return item("COUNTRY_REFUSED", "CN-08", "recipient country refused for recruiting outbound (no cold outbound to "
                    "Canada; CASL unverified)")
    return None


def channel_problem(channel: str, enabled: list[str]) -> Optional[dict]:
    if channel not in enabled:
        return item("CHANNEL_DISABLED", "CN-09", f"channel {channel} is not enabled")
    return None
