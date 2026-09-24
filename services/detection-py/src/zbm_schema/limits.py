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
  - finding_id / entity_id: 128. They are built from identifiers and
    labels, e.g. "contract-{client_id}-{period_label}" <= 106 and
    "{client_id}:{platform}" <= 97.
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

from typing import Annotated

from pydantic import StringConstraints

ID_MAX_CHARS = 64
FINDING_REF_MAX_CHARS = 128
LABEL_MAX_CHARS = 32
AGENT_ID_MAX_CHARS = 64
SKU_MAX_CHARS = 64
DISCOUNT_CODE_MAX_CHARS = 64
CAUSE_DESCRIPTION_MAX_CHARS = 1024

MAX_LINE_ITEMS_PER_ORDER = 50
MAX_DISCOUNTS_PER_ORDER = 10

MAX_QUANTITY = 99_999_999_999_999_999
MAX_ATTRIBUTION_WINDOW_HOURS = 8_784
MAX_RENEWAL_INTERVAL_DAYS = 3_660
MAX_TOUCHPOINT_SEQUENCE = 1_000_000

Id = Annotated[str, StringConstraints(max_length=ID_MAX_CHARS)]
FindingRef = Annotated[str, StringConstraints(max_length=FINDING_REF_MAX_CHARS)]
Label = Annotated[str, StringConstraints(max_length=LABEL_MAX_CHARS)]
AgentId = Annotated[str, StringConstraints(max_length=AGENT_ID_MAX_CHARS)]
Sku = Annotated[str, StringConstraints(max_length=SKU_MAX_CHARS)]
DiscountCode = Annotated[str, StringConstraints(max_length=DISCOUNT_CODE_MAX_CHARS)]
CauseDescription = Annotated[str, StringConstraints(max_length=CAUSE_DESCRIPTION_MAX_CHARS)]
