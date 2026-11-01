# ADR 0032: durable receipt envelope, mock-only delivery contract

Status: implementation candidate; independent review and production integration remain required.

## Scope

The receipt scheduler route previously supplied a receipt view to a bool-returning stand-in. This increment adds founder-only contact management and a durable delivery contract. No real mail adapter, provider credentials, cron, acknowledgement tracking, deadlines for client action, restoration mechanism or infrastructure is introduced. Production still constructs `NotBuiltClientMail`, which is disabled and performs no I/O.

## Contact and privacy

Andre alone may PUT `/fin/v1/clients/{client_id}/billing-contact` with a bounded single ASCII mailbox, or `email: null` to disable it. The response and audit event contain an opaque revision and enabled flag, never the mailbox or a bare mailbox hash. The private anchored journal stores the contact and envelope; receipt read/send responses omit them. Existing journal access controls and backups therefore protect recipient PII. There is deliberately no general contact export endpoint.

The first eligible send freezes `to`, a fixed subject, plaintext receipt body and idempotency key. The body uses only the existing client-facing receipt fields and is capped at 32 KiB. Retries read persisted content instead of rendering it again, including after restart or code deployment. A copy is passed to the port so adapter mutation cannot rewrite durable state. A changed or disabled contact after an attempt stops automation; editing it back does not reset the delivery state.

## Claim, retry and uncertainty

A committed claim and frozen envelope precede every possible port call. Attempts are charged at claim time, including a process crash. The first claim fixes a 23-hour conservative retry deadline, never extended, and at most seven claims are allowed. The existing 15-minute claim/backoff control remains. Clock rollback, deadline expiry or attempt exhaustion require founder review. A historical attempted receipt without a frozen envelope is uncertain; this increment cannot establish its previous recipient, body, key or provider acceptance and must not invent a fresh send.

`ReceiptDeliveryOutcome` is strictly validated through the common adapter-answer validator. Only `accepted`, `retry`, `failed`, and `uncertain` are valid. `accepted` denotes provider acceptance, not inbox delivery. `retry` promises the provider did not accept the message. An exception, untyped/truthy answer or unknown code becomes uncertainty, with no free-form adapter result recorded. Future adapters must independently prove this promise and their idempotency retention and timeout behavior; 23 hours is a local policy, not a claim about any configured provider. There is no provider configured here.

Money matching, withdrawals, claim ownership and late-outcome guards remain. A late positive outcome after withdrawal is recorded as `withdrawn_after_send`; a late failure does not undo another claim's outcome. Disabled mail or a missing contact consumes no attempt. Failed/uncertain/reviewed states do not automatically resume.

Andre may POST `delivery-review` with an evidence reference/hash for failed or uncertain receipts. This records `delivery_reviewed`, not `sent`, and does not mint a new key or authorize a resend. Any necessary external resolution is manual and outside this increment. Reviewing evidence does not prove delivery.

## Legacy tests and deployment limits

The inherited bool port is restricted to an explicitly marked in-process test fixture (`FakeClientMail`). Production factories contain no selector for it, and neither request data nor environment configuration can enable that marker. Existing receipts with contact or envelope state cannot use that compatibility path even with the fixture injected. Inherited accounting tests retain their established behavior; new tests exercise the typed path.

New private journal collections require a compatible reader. Rolling an already-written journal back to code that does not recognize them is not claimed safe. Production activation requires separate provider/credential/domain/privacy/timeout review, deployment and recovery validation, and remote CI. This increment makes no inbox-delivery, acknowledgement, production-readiness or restoration claim.
