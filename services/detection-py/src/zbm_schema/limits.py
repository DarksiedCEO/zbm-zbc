"""
Field limits of the request models (fix wave 1, Sep 24 2026 — LOW-C).

Finding: the API advertised 1,000 items per batch, but the 2 MiB body limit
was sized from a *typical* order, so 1,000 orders x 30 line items (2.12 MiB)
was refused with 413. The body limit could not have been sized from the
true worst case: no string, list or integer in the models had a limit, so a
"legal" batch had no maximum size at all.

Every field of every request model now has one. src/request_limits.py
computes the worst-case JSON size of each route's largest legal batch from
these limits, and the per-route body limit is that plus headroom
(ADR 0001 "Request limits"). Anything outside a limit is a 422 naming the
field, never a 413 for a batch the API claims to accept.

Chosen from what the data actually is:
  - identifiers (order, customer, subscription, term, client, affiliate):
    64 characters. Shopify GIDs are ~40, UUIDs 36, Stripe ids ~30.
  - finding_id: "rrf1-" + 40 hex (a hash, Oct 6 2026, E-3); entity_id: an
    identifier (64). Until Oct 6 they were 128-character strings built by
    concatenation ("contract-{client_id}-{period_label}"), which collided.
  - short labels (status, platform, channel, period label, entity type):
    32. Agent ids: 64.
  - SKU and discount code: 64.
  - cause_description: 1024. The longest an agent writes is the
    discount-misuse text listing up to 10 codes of 64 characters (~850);
    tests/test_body_limits.py runs every agent on max-size input and
    checks each finding still validates.
  - line items per order: 50; discounts per order: 10.
  - integers: quantity <= 99,999,999,999,999,999 (the order-subtotal check
    already implied it: MAX_MONEY in cents at the minimum price of 0.01);
    attribution window <= 8,784 h (366 days); renewal interval <= 3,660
    days (10 years); touchpoint sequence <= 1,000,000.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BeforeValidator, StringConstraints

ID_MAX_CHARS = 64
FINDING_REF_MAX_CHARS = 128
LABEL_MAX_CHARS = 32
SKU_MAX_CHARS = 64
DISCOUNT_CODE_MAX_CHARS = 64
CAUSE_DESCRIPTION_MAX_CHARS = 1024

MAX_LINE_ITEMS_PER_ORDER = 50
MAX_DISCOUNTS_PER_ORDER = 10

MAX_QUANTITY = 99_999_999_999_999_999
MAX_ATTRIBUTION_WINDOW_HOURS = 8_784
MAX_RENEWAL_INTERVAL_DAYS = 3_660
MAX_TOUCHPOINT_SEQUENCE = 1_000_000

# E-12 (Revenue Recovery fix wave, Oct 6 2026): identifiers are never empty
# and use a safe charset. An empty order_id used to be accepted and every
# finding built from it collided on entity_id "" (probe P7). The charset is
# what real platform ids use (Shopify GIDs "gid://shopify/Order/1", Amazon
# "123-1234567-1234567", UUIDs, emails as customer ids) and excludes
# whitespace, control characters, quotes, "|", "=" and ";" — so an id can be
# a correlation-key component ("client|type|id") and a ledger summary token
# ("e=<id>") without escaping or ambiguity. First character alphanumeric, so
# no id can be "-" (the ledger summary's "none" marker) or start with ".".
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/@+#-]*$"

# A tenant (ZBM client) id is stricter: it is the ledger event subject_id of
# every scan event, and ledger-rust accepts only [A-Za-z0-9._:-] there.
CLIENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"

# Finding-only fields (ADR 0001 "Finding identity and the scan ledger record",
# Oct 6 2026). orchestrator-go writes every finding to the ledger as one event
# whose 280-character summary carries agent_id, leak_category, entity_type,
# entity_id, period_label, amount, evidence class, classification, confidence
# and methodology_id; these bounds are what make the worst case fit (275
# characters — tested on both sides: tests/test_finding_identity.py and
# orchestrator-go internal/orchestrator/ledger_record_test.go).
AGENT_ID_MAX_CHARS = 32
ENTITY_TYPE_MAX_CHARS = 16
PERIOD_LABEL_MAX_CHARS = 16
METHODOLOGY_ID_MAX_CHARS = 24
METHODOLOGY_MAX_CHARS = 600
SLUG_PATTERN = r"^[a-z0-9][a-z0-9_]*$"
AGENT_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
FINDING_ID_PATTERN = r"^rrf1-[0-9a-f]{40}$"

# Patterns that admit only printable ASCII no JSON encoder escapes (Python's
# json and Go's encoding/json alike): a string matching one is at most one
# byte per character on the wire (request_limits.py).
ASCII_SAFE_PATTERNS = frozenset({ID_PATTERN, CLIENT_ID_PATTERN, SLUG_PATTERN, AGENT_ID_PATTERN, FINDING_ID_PATTERN})

Id = Annotated[str, StringConstraints(min_length=1, max_length=ID_MAX_CHARS, pattern=ID_PATTERN)]
ClientId = Annotated[str, StringConstraints(min_length=1, max_length=ID_MAX_CHARS, pattern=CLIENT_ID_PATTERN)]
FindingRef = Annotated[str, StringConstraints(min_length=1, max_length=FINDING_REF_MAX_CHARS, pattern=FINDING_ID_PATTERN)]
PeriodLabel = Annotated[str, StringConstraints(min_length=1, max_length=PERIOD_LABEL_MAX_CHARS, pattern=ID_PATTERN)]
MethodologyId = Annotated[str, StringConstraints(min_length=1, max_length=METHODOLOGY_ID_MAX_CHARS, pattern=SLUG_PATTERN)]
Methodology = Annotated[str, StringConstraints(min_length=1, max_length=METHODOLOGY_MAX_CHARS)]
Label = Annotated[str, StringConstraints(min_length=1, max_length=LABEL_MAX_CHARS)]
AgentId = Annotated[str, StringConstraints(min_length=1, max_length=AGENT_ID_MAX_CHARS, pattern=AGENT_ID_PATTERN)]
Sku = Annotated[str, StringConstraints(max_length=SKU_MAX_CHARS)]
DiscountCode = Annotated[str, StringConstraints(max_length=DISCOUNT_CODE_MAX_CHARS)]
CauseDescription = Annotated[str, StringConstraints(max_length=CAUSE_DESCRIPTION_MAX_CHARS)]


_SEPARATORS = re.compile(r"[\s-]+")


def _normalize_slug(value: Any) -> Any:
    """E-11: a free-text status/platform label compared against a constant
    ("abandoned_cart", "tiktok_shop") is normalized first — trimmed,
    lowercased, runs of whitespace or "-" turned into "_" — so
    "Abandoned_Cart", " abandoned-cart " and "ABANDONED CART" all mean
    abandoned_cart instead of silently matching nothing (probe P6). Anything
    that is still not a slug afterwards is a 422, never a silent miss."""
    if isinstance(value, str):
        # The raw text is bounded too (before stripping), so a legal body
        # cannot pad a label with unbounded whitespace.
        if len(value) > LABEL_MAX_CHARS:
            raise ValueError(f"must be at most {LABEL_MAX_CHARS} characters")
        return _SEPARATORS.sub("_", value.strip()).lower()
    return value


# A normalized label: what Order.status and PlatformConnectionStatus.platform
# are compared on.
Slug = Annotated[
    str,
    BeforeValidator(_normalize_slug),
    StringConstraints(min_length=1, max_length=LABEL_MAX_CHARS, pattern=SLUG_PATTERN),
]
