# Unknown Checkout payment review

A high `stripe_unknown_session` break means Finance read a processing or successful payment linked to a Checkout
session it does not have locally. It does not mean Finance knows the payer, invoice, currency-adjusted amount or
correct accounting treatment. `difference: 0.00` and `amount_unassessed: true` must never be treated as reconciled.

Andre reads `/fin/v1/breaks` using his existing approval credentials. Use the recorded observation time and the
existing authorized Stripe event evidence to investigate the session/payment in the correct Stripe environment.
The break's `session_ref` is SHA-256 of UTF-8 `checkout-session:` followed by the exact session ID; `payment_ref`
is SHA-256 of `checkout-payment:` followed by the exact payment ID. These hashes support matching authorized
records without adding raw IDs to this break. Do not put credentials or personal data in review references.

Determine whether cash exists, whether another environment owns the session, and whether any invoice or journal
entry already represents it. Use existing independently authorized accounting procedures if correction is needed.
This feature supplies no restore, re-import, refund, relinking or automatic accounting command. An unresolved
payment attribution or Compliance question must not be bypassed by this review closure.

After external review, Andre may POST `/fin/v1/breaks/{break_id}/resolution` with a fresh request ID,
`explanation_code: unknown`, and one or more `evidence` objects containing an opaque `ref` and the SHA-256 of the
retained review document. Do not send `entry_id` or `clears_by`. Closure attests only that the operator review
was performed: the service does not fetch that document or verify that money was recovered. Retain evidence
under existing access controls. No other caller may close this review.

If processing subsequently becomes succeeded, the break reopens for fresh review and retains the previous
resolution. Late processing after succeeded does not downgrade or reopen it. Duplicate callbacks do not create
extra reviews. A changed session/payment association refuses rather than guessing; investigate the upstream
record and retry after truth is corrected. Ledger/store unavailability refuses the callback, allowing retry;
do not mark the event handled manually. A restart replays the recorded break and its decisions.

The break is durable operator evidence, not proof of email delivery. Existing founder monitoring and provider
configuration must be validated separately. Never close the anomaly solely because no money was booked locally.
