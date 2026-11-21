# Dormant receipt transport boundary

No mail is enabled by this module. Existing Finance mail factory remains disabled; no environment activation,
secret provisioning, schedule, webhook, migration, sender-policy mapping or provider operation is added. Importing
or constructing the transport does not send. It exposes a lower-level explicit send_frozen contract, not the current
ClientMailPort. Legacy/incomplete receipt envelopes are refused before a worker/network attempt.

A future authorized caller must already have a persisted complete immutable plaintext request: exact sender,
one recipient, subject/text bytes, digest, idempotency key, schema/provider/account profile+revision, first attempt
and retry cutoff. The transport checks this input; it does not create or persist it. Original window must be positive,
<=23 hours, aware UTC, not future-first and not expired/rolled back. Never reset a key, attempt or deadline to clear
failure; never enrich a previously attempted legacy envelope with a guessed sender or account.

Profile IDs/revisions are not credential attestation. Verified sender ownership, entity sender approval, actual
credential/account binding and rotation, least privilege, live/staging isolation and private evidence custody remain
separate prerequisites. Only the fixed Resend endpoint exists; no endpoint/environment override is supported.
The private worker protocol is not an operator CLI or a route around business claims and compatibility gates.

An accepted outcome confirms only provider API acceptance. Failed is a definite local preflight or recognized
permanent provider refusal; uncertain may follow actual acceptance. All 409/429/5 xx and transport/deadline failures
remain uncertain and stop automation. No automatic retry, including generic provider retry suggestions. The current
ReceiptDeliveryOutcome retry state is never produced by this boundary. Review existing evidence under an authorized
procedure; this module stores no provider ID and supplies no recovery or resend permission.

Total work is bounded by the smaller of original remaining window and configured 1–30 seconds(default 10), with DNS/
read in an owned child and kill/reap cleanup. No private payload/token/error content is emitted. Absolute wall-clock
correctness and arbitrary OS suspension cannot be guaranteed; deadline expiry never proves no external effect.
Existing receipt claims, withdrawal handling, attempt ceilings, rollback guards and disabled defaults are untouched.

Before production wiring require a separate schema/reader migration review, full claim/acceptance/local-append
crash tests with fake ports, operator credential/account evidence, CI/image validation and explicit activation.
No current journal can be declared newly compatible merely because this lower-level module exists.
