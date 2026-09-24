"""Constrained scalar types shared by both layers.

`SafeId` uses the exact character class the ledger contract (BUILD_CONTRACTS
section 2) requires for `event_id` / `subject_id`, so any object id in this
service can be used as a ledger subject without re-encoding.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

SAFE_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"
ACTOR_PATTERN = r"^[a-z0-9_]{1,64}$"

# Ids the service DERIVES ledger subjects from (a campaign id becomes
# "{campaign_id}:v{version}") are bounded to 100 characters, and rulebook
# versions to MAX_RULEBOOK_VERSION, so every derived subject is at most
# 100 + 2 + 7 = 109 <= 128 characters (integration defect 2). A longer id
# is refused as input (422) — it can never surface as a "ledger failure".
BOUNDED_ID_MAX = 100
BOUNDED_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,100}$"
MAX_RULEBOOK_VERSION = 9_999_999

SafeId = Annotated[str, StringConstraints(pattern=SAFE_ID_PATTERN)]
CampaignId = Annotated[str, StringConstraints(pattern=BOUNDED_ID_PATTERN)]
ActorId = Annotated[str, StringConstraints(pattern=ACTOR_PATTERN)]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
ShortStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=280)]
