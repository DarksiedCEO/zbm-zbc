"""Express consent for SMS and voice (TCPA; ADR 0013 decision 12).

Decides: whether a phone number has an active, recorded express consent for one channel and one brand. The registry
keeps every event (grant: source, time, the exact consent text version and its SHA-256; revocation); the latest event
for (phone, channel, brand) decides. A revocation from the person (STOP, a reply, a call) revokes every channel and
both brands. Consent to ZBM is not consent to ZBC (consent is given to a seller). Never: infers consent from an
inquiry, a scan, a purchase or a referral."""

from __future__ import annotations

NUMBER = 6
NAME = "consent"
DECIDES = "active express consent per phone, channel and brand"

CHANNELS = ("sms", "voice")
SOURCES = ("web_form", "paper_form", "recorded_call", "keyword_optin")


def key(phone_hash: str, channel: str, brand: str) -> str:
    return f"{phone_hash}|{channel}|{brand}"


def active(consents: dict, phone_hash: str, channel: str, brand: str) -> bool:
    events = consents.get(key(phone_hash, channel, brand)) or []
    return bool(events) and events[-1]["event"] == "granted"
