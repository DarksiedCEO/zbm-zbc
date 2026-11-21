# ADR 0030: Unknown Checkout payment review

Status: accepted for implementation; independent review and remote CI required.

## Decision

A verified webhook is a signal to read Stripe truth. If a found Checkout session has no local Finance session
record and its linked, found payment is processing or succeeded, Finance records a high-priority
`stripe_unknown_session` break in the same durable operation as event handling. It acknowledges only after that
operation commits. API unavailability or inconsistent identity refuses; unknown unpaid or not-found objects do
not invent a payment anomaly. Current Compliance checks and the known-session payment path remain in place.

This is detection, not recovery. It does not book cash, link an invoice, assign a client, make a refund, or restore
records. `difference` is `0.00` with `amount_unassessed: true`: zero is a schema placeholder, not proof that the
books balance. Provider amount/currency data is not used for this break.

One break is keyed by a domain-separated SHA-256 of the session identity. The new break and its audit payload
hold only domain-separated session/payment hashes, fixed text and status codes. They add no raw provider IDs,
client metadata or free-form provider text. Existing webhook and dependency evidence formats are unchanged;
this feature does not claim to redact those pre-existing formats. Non-founder break views omit references,
state details and external-review evidence.

`processing` can advance once to `succeeded`. A later processing observation never downgrades it. Duplicate
observations cannot multiply the break or reopen a resolved succeeded review. A changed payment identity for
the same session refuses. Newly observed success reopens a previously resolved processing review, preserves its
prior resolution, and requires a fresh founder decision. No event automatically closes a break.

The existing founder-only resolution route accepts this leg only with explanation `unknown`, nonempty bounded
external evidence references/digests, and no journal-entry or timing shortcut. This records that Andre reviewed
it externally; Finance cannot verify the external evidence contents or claim recovery. It leaves accounting,
Compliance holds and payment attribution unchanged. The journal records every resolution; it is not an undo or
restore facility. See [operator runbook](../runbooks/unknown-checkout-payment-review.md).

## Validation boundary

Tests use runtime-generated credentials and the real Stripe adapter against an owned in-memory HTTP simulator.
They cover privacy, durable restart, duplicate/reordered events, missing/unavailable/inconsistent provider truth,
ledger/store refusal and retry, no new monetary entries, normal known-session dispatch, and founder evidence
resolution. The full Finance suite and live harness remain required. No provider delivery, founder notification,
production recovery, or real-money claim follows from these local checks. Mail/cron/restore remain separate work.
