# Stripe-session housekeeping (dormant scheduling)

Invoke the existing scheduler-authorized job only within an explicitly authorized operational procedure. This
change does not configure or enable a timer, new caller, provider, or credential. No automatic retries are added.

Each invocation reserves at most 25 eligible closes durably before contacting the provider. `close_sweep_order`
is an attempt-selection position, not expiry evidence. Failed pages remain open/eligible; later runs rotate
through a finite backlog. A crash after reservation can postpone the selected pages until a later rotation.
Journal/ledger failures prevent new reservations; endless new arrivals and outages prevent a time guarantee.
The independent Compliance activation cap remains 25 clients per invocation.

Inspect `checked`, `closed`, `failed`, and `activation_unknown` rather than HTTP 200 alone. HTTP 503 with
`outcome_unknown` / `took_effect: null` does not prove that nothing happened or that something did. Provider expiry,
other housekeeping, or local evidence may already have occurred. Do not automatically retry on the inherited
Retry-After header. Inspect existing local/provider evidence under authorized procedures before another invocation;
do not reset orders, rewrite the journal, mark failed pages closed, or reset request IDs as a recovery shortcut.
Orphaned ledger anchors and broader crash reconciliation remain governed by existing Finance recovery controls.

Malformed or exhausted order state refuses further provider closes. Preserve evidence and escalate for a separately
reviewed repair; there is no reset or migration endpoint in this increment. Older readers preserve generic session
fields but ignore fair selection, so rollback loses this guarantee. This note does not authorize any image rollback,
remove receipt compatibility gates, or establish hosted scheduling readiness. Real provider operations and cadence
require separate configuration, independent review, CI, and explicit activation authorization.
