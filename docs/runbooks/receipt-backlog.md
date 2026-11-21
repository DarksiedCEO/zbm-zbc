# Founder receipt backlog

Use authorized GET `/fin/v1/client-receipts/backlog?limit=50` with the existing service bearer and Andre approval.
There are no new credentials, settings or activation steps. A successful view makes no journal/ledger/provider writes.
Existing rejected-founder auditing remains active. This inventory is not a send action or permission to retry.

All recorded receipt states appear. Read `paid_on`, `created_at`, `creation_age_seconds`, `legacy_sent_at`,
`acceptance_record`, `acceptance_recorded_at`, `manual_delivery_review_recorded` and fixed issues separately.
Age is operational elapsed time, not a business-day or contractual deadline. No two-day receipt-send SLA is inferred.
`acceptance_record:present` only describes a recorded slot; Finance recorded its time. `acceptance_record_validated`
and `delivery_verified` stay false. It does not prove accepted payload validity, inbox delivery or acknowledgement.
A legacy sent flag/timestamp and manual review are not provider evidence. Late acceptance can coexist with uncertainty,
review or withdrawal. Do not change or resend a receipt merely because this view looks old or has unknown fields.

Follow `next_cursor` with the same optional limit, or omit limit to adopt the cursor's limit. On409
`receipt_backlog_snapshot_changed`, discard the old cursor and restart the first page. Clock-relative ages/issues may
change without cursor invalidation. Cursor hashes cover safe normalized display fields, not private body/history.
Malformed key references are snapshot-local ordinal placeholders; null IDs/unknown fields are not actionable identities.
No record is omitted because of unknown status/timestamps. Capacity503 means the supported5,000-record/response bound
was exceeded; do not interpret it as empty backlog or delete journals to fit. Closed/malformed collection refusal is
also unavailable, not healthy. Larger inventories need a reviewed bounded index strategy.

This is local receipt inventory only: external request intake completeness and contractual deadline evaluation remain
false. The route does not inspect orphan envelopes, validate journal history, contact providers, deliver mail, retry,
close, repair or migrate anything. Keep the separate receipt activation and rollback gates in effect.
