# ADR 0038: Explicit ZBM receipt provider workflow

Status: local implementation; independent review, CI and activation gates remain required.

## Scope and default

The existing scheduler-authenticated receipt send route can construct ResendReceiptMail only when
FIN_RECEIPT_MAIL_PROVIDER explicitly equals resend. Default disabled retains NotBuiltClientMail and reads no
receipt key file. ZBM is the sole supported entity; ZBC does not gain outbound mail. No new route, cron, caller,
provider lookup, delivery webhook, refund, money posting or automatic retry is introduced.

Profile ID/revision and sender are explicit operator configuration, never inferred from contact addresses.
They do not attest that a key belongs to the correct provider account or that its sender/domain is verified.
A private owner-only regular key file is opened without symlink following and bounded to 4096 bytes. The re_ prefix
and printable non-whitespace 32–4096 character format are local assumptions, not an account verification. These
are not Finance onboarding 64 hex tokens. No raw secret config setting is parsed by the app.

## One claim and complete immutable request

After existing eligibility checks, a new unattempted ZBM receipt freezes canonical from/to/subject/text bytes,
digest, provider/version, profile/revision, entity/receipt binding, key, contact revision and original UTC 23 hour
window. Request and sending claim commit together before Gather's recorded crossing and provider I/O.
The bridge uses actual current UTC, not first-attempt time, and the transport rechecks the original deadline.
Profile object, credential and request are snapshotted for the call. Changing sender/account is not key rotation;
operational account attestation remains required even for same-profile key rotation.

A second invocation cannot terminalize a fresh sending claim. After a crashed/stale real claim or any prior real
attempt, there is no automatic retry or new key: stop uncertain and require founder review. Existing mock-only
retry fixtures remain available only through injected test ports. A real typed retry response, accepted without
valid provider ID, or malformed outcome is uncertain after possible I/O. Legacy attempted receipts and all legacy
saved envelopes are refused for real delivery; no sender/profile enrichment or attempts reset is performed.

## Private API acceptance, not delivery

ReceiptDeliveryOutcome adds an optional private provider ID; generic mock accepted-without-ID stays supported,
but real-envelope acceptance requires the ID. Strict answer validation rejects IDs on nonacceptance outcomes.
Worker output is bounded 512 bytes (fixed result keys plus at most 128 ID characters), not arbitrary provider data.
The complete request stays immutable. Acceptance ID/time append once in its private envelope record; conflicting
replacement is refused. ID, address, body, key and credential do not enter receipt API results or ledger payloads.
Existing sent/sent_at/sent boolean mean adapter/API acceptance, never inbox delivery/read. Late acceptance may
append private evidence but must not erase founder review or resurrect a withdrawn receipt.

The receipt send route normalizes Unavailable conservatively to outcome_unknown with took_effect:null, even if
failure occurred before I/O. Provider acceptance followed by local append failure can leave sending without an ID.
Claim anchoring followed by append failure can leave orphan ledger evidence with zero provider calls. The existing
integrity gate can refuse restart; this change does not repair those journals or invent reconciliation success.
Founder delivery-review is terminal evidence, never permission to resend.

## Reader compatibility and ownership

The fixed 8e632340 and 774e46b2 target profiles remain unchanged. A distinct current runtime evaluator validates all
historical new-version records, strict nested fields/types, canonical bytes/digest, receipt/contact/entity/window
binding (including initial first-attempt matching same-record claimed_at and last_attempt_at), status/acceptance relationship, immutable request and nonreplaceable acceptance evidence. It accepts known
legacy history without rewriting it. Unknown/superseded semantics refuse startup under the same directory claim,
before replay/lease/scheduler action; failed construction releases the claim. No current-artifact CLI target is
invented: target profiles are not artifact attestations or deployment permission.

The prior guarded 63ed reader should reject new vocabulary, but old unguarded images cannot execute this guard.
External rollback-target enforcement remains OPEN and blocks activation. Neither a marker nor local preflight
prevents an old Railway GUI rollback. Clock faults/suspension and provider account attestation remain assumptions.

## Container scope expansion

Finance Dockerfile adds only optional --secret-file FIN_RECEIPT_RESEND_KEY_FILE. Root-approved source-only IaC
FIN_RECEIPT_RESEND_KEY: preserve() keeps the existing secret-preservation detector intact. It creates no value,
enables no provider and authorizes no config plan/apply. Generic entrypoint may materialize an explicitly supplied
secret even while app mail is disabled; root launch writes owner-only file then drops to uid 10001. Application
disabled mode never reads it. No other IaC setting/resource changes.

## Validation and remaining activation gates

Owned fake HTTP provider, two separate HTTP client processes against one writer, full receipt-history controls,
typed-answer negatives, auth/config/privacy tests, separate Finance restart processes with real local ledger and
injected append faults, and actual pinned image/entrypoint proof are required. Full Finance/live/hygiene/lint/count,
IaC type/deploy and secret scans supplement independent review; they do not certify real provider behavior.

Before activation: approved sender/domain/account, safe key custody/rotation and environment isolation, verified
recipient contacts, external rollback enforcement, monitored ambiguity reconciliation and scheduler invocation
ownership, plus explicit founder activation authorization. No real provider or production action is part of this ADR.

Fresh claims use the existing15minute policy. A sufficiently large forward wall-clock jump can make an active
claim appear stale and stop it uncertain; this is a disclosed availability limitation, never permission to resend.
