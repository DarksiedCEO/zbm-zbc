# ADR 0035: Durable Stripe page-close scheduling

Status: implemented locally; independent review and remote CI required. No recurring operation activated.

## Problem and decision

Selecting the oldest 25 eligible Checkout pages repeatedly lets 25 persistently unavailable pages prevent all
later pages from ever being attempted. The Compliance activation cursor does not rotate this separate close queue.
Select at most 25 eligible pages by `close_sweep_order`, then creation time and session ID. Missing scheduling
order means zero. Before any expiry request, durably reserve increasing orders on the selected existing
`stripe_sessions` records through the normal anchored journal operation. Keep the existing Stripe-turn then
service-lock order, re-evaluate eligibility inside that turn, and leave Compliance gathering/limits unchanged.

The order is scheduling history, not proof of a provider call, expiry, or successful job. Interrupted reservations
remain meaningful: later invocations can reach other pages before revisiting them. Failed pages stay eligible;
no failure is discarded or reclassified as success. For a finite eligible backlog with continued successful local
reservation commits, repeated invocations reach every page despite persistent failures among earlier pages.
This is not a time SLA, success guarantee, fairness guarantee under unbounded new arrivals, or automatic retry.
When the journal/ledger cannot reserve, no provider close call starts. Preserve the existing response counters.

Orders are exact nonboolean integers from zero through 2^63-1. Reject malformed orders or exhausted space before
provider calls; never wrap, reset, infer an order from wall time, or silently repair persisted data. The maximum
includes terminal sessions, so a restart or backward clock does not reuse old scheduling positions. This adds
bounded per-record metadata, not a new collection, monetary entry, invoice change, or alteration of Checkout
content bindings. Existing generic put replay preserves the complete session record. An older reader ignores the
field for selection and can starve again; this change does not make an old image fair or authorize rollback.

## Uncertain outcome at the caller boundary

Provider expiry can precede a failed local commit or the final completed-job record. Only the `stripe-sessions`
job route normalizes `Unavailable` to HTTP 503 with fixed detail, `outcome: outcome_unknown`, and nullable
`took_effect: null`. Null means unknown, not false and not proof that an effect occurred. It deliberately discards
underlying exception text/fields. Conservative uncertainty also applies to pre-provider failures because this
job can already have performed earlier housekeeping. Other jobs, general errors, and financial handling retain
their existing behavior. The existing generic Retry-After header is not authorization for automatic retry.

No provider retry is added. A later explicit invocation may re-observe provider state; a successful response
with failed/activation_unknown counters is incomplete work, not proof of clean maintenance. Stable request IDs
still do not provide durable once-per-hour execution for this repeatable job. Runner, cron, provider activation,
mail, reconciliation redesign, restore, and deployment enforcement remain outside this increment.

## Evidence boundary

Owned fake-page tests cover the reproduced persistent-failure starvation, stable equal-time order, durable
restart, clock rollback, concurrent calls, reservation interruption/write failure, failed-page recovery,
malformed/exhausted orders, local failures after external expiry and after close completion, and error privacy.
No real Stripe behavior, payment race, deployment, or real-money assertion follows from these tests. Full Finance
and the unchanged live harness are separate required gates. See the operator runbook for recovery limits.
