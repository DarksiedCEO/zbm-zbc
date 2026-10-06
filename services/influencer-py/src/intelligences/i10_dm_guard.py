"""Platform DM guard (ADR 0015 decision 10b; Andre, Oct 6 2026: platform DMs always need him).

Decides: whether a DM draft is admissible for Andre's review (a platform the influencer has a handle on, at most
``DM_MAX`` characters, no template placeholders, no hidden characters — i08's rule), and the content SHA-256 his
approval binds: the influencer, platform, handle hash, brand and the exact text. An approved DM is sent byte for byte
or not at all; any change is a new draft. Never: writes or edits a DM, or sends one."""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from intelligences import i08_disclosure

NUMBER = 10
NAME = "dm_guard"
DECIDES = "DM draft admissibility and the hash Andre approves"

DM_MAX = 1000


def problem(text: str) -> Optional[str]:
    if not text.strip() or len(text) > DM_MAX:
        return "DM_LENGTH"
    if "{{" in text or "}}" in text:
        return "PLACEHOLDER_UNKNOWN"
    if i08_disclosure.hidden_characters(text):
        return "CONTENT_HIDDEN_CHARACTERS"
    return None


def content_sha256(influencer_id: str, platform: str, handle_hash: str, brand: str, text: str) -> str:
    doc = {"influencer_id": influencer_id, "platform": platform, "handle_hash": handle_hash, "brand": brand,
           "text": text}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
