# ADR 0037: Dormant complete-payload receipt transport

Status: implemented locally; independent review and CI required. No provider request or activation authorized.

## Boundary, not production wiring

`resend_receipts.py` provides explicitly constructed `FrozenResendRequest`, `ResendTransportProfile` and
`ResendReceiptTransport.send_frozen(request, now)`. It is deliberately not a ClientMailPort: no enabled flag or
legacy deliver_receipt method. Current receipt envelopes lack sender/account binding and are refused before any
worker starts. Production factories, environment settings, disabled mail default, receipt journal schema, fixed
reader profiles, claims/attempts, financial paths and existing receipt safeguards remain unchanged.

The request supplies version/provider, nonsecret account-profile ID/revision, complete canonical plaintext body
bytes plus SHA256, exact idempotency key and original first-attempt/retry-until UTC timestamps. The immutable
transport profile supplies the expected profile/revision and exact approved sender. Body contains only from,
one-recipient to array, subject and text; no dynamic sender, template, HTML, attachments, tags or reply-to. No
sender policy is guessed. At each call the digest, canonical bytes, fields, profile and sender must match. A
profile label is a caller's local binding assertion, not proof of credential account ownership or sender approval.

Only aware zero-offset timestamps are accepted, with first_attempt_at <= supplied now < retry_until and an
original positive window at most 23 hours. Current wall time is checked too; future-first, expired, clock rollback,
profile drift, malformed credentials/headers, legacy or incomplete input fail before network. Worker rechecks
its actual UTC time and the unchanged original window before opening the client and immediately before POST.
No enrichment, attempt reset, new key, cutoff refresh, credential persistence or journal rewrite is provided.

## Provider contract and conservative outcome

Fixed HTTPS `https://api.resend.com/emails`, one POST of the exact saved bytes/key. Existing httpx dependency,
certificate verification, trust_env false, redirects disabled and no automatic retries. Response capped 64 KiB;
compressed/invalid/mixed/duplicate/nonfinite/unexpected shapes refuse acceptance. HTTP 200 needs exactly one valid
bounded id string; the ID is discarded. Valid documented status+name permanent refusals may return failed;
all 409/429/5 xx, redirects, malformed/oversized responses and transport/deadline failures return uncertain. No retry
outcome is produced, even where provider documentation suggests retrying: this codebase's retry promises known
nonacceptance. Accepted means API acceptance only, never delivery/read evidence.

Official sources checked 2026-11-01 (public read-only docs, not provider API requests):
[Send Email](https://resend.com/docs/api-reference/emails/send-email),
[official OpenAPI](https://github.com/resend/resend-openapi/blob/main/resend.yaml),
[idempotency](https://resend.com/docs/dashboard/emails/idempotency-keys),
[errors](https://resend.com/docs/api-reference/errors).
They specify 200 success, required sender/recipient/subject, plaintext support, keys up to 256 characters and 24 hour
retention. Concurrent/mismatched-key 409 s cannot establish nonacceptance of earlier work. Our narrower payload and
outcome policy is conservative local design, not a stronger provider guarantee.

## Total deadline and privacy

Constructor/import perform no I/O. The parent gives an owned subprocess only bounded stdin, with credentials/body
absent from argv and repr. Its environment is minimal, stderr suppressed. A selector handles bounded nonblocking
stdin and stdout; output is stopped at 128 bytes before JSON parsing. Worker emits only the fixed outcome. HTTP body,
provider free text, recipient, ID, key, token and exception traceback never enter domain output/logs.

Total parent monotonic budget is the lesser of configured 1–30 seconds(default 10) and original remaining window.
Actual UTC remaining time is rechecked after serialization; worker repeats window checks. DNS, stalled headers
and trickle reads occur only inside that child. On deadline/overflow/error, kill and reap that owned PID and close
its pipes; do not signal unrelated processes or groups. Worker starts no descendants/listeners. No retry occurs.
The budget bounds work, not hard realtime OS process launch/reaping latency. Arbitrary clock faults, suspension
between last wall check and write, and restored/cloned-account key isolation are not solved by this boundary.

Exactly one named hygiene exception permits the causal deadline test: entered 10 second DNS/header/trickle stage,
configured 1 second budget, return within 3 seconds and owned PID reaped. No broad exemption, skip, performance target
or production timeout increase. Test-only worker harness can replace endpoint/transport; no public setting or
constructor endpoint override exists. All actual HTTP tests target owned loopback, never Resend.

## Activation dependencies and compatibility

This is not a safe drop-in activation. Before wiring: persist the complete frozen payload/profile/window in a
separately reviewed schema; define new reader capabilities without changing historical claims; prove claim-before-
I/O, uncertain acceptance and crash/restart behavior; verify approved entity sender/domain/account and credential
custody/rotation; isolate live/staging keys and reconcile provider IDs privately; obtain explicit activation approval.
Current old-image rollback enforcement remains a deployment gate. Existing envelopes must not be retroactively
filled with guessed sender/account data or assigned new keys. See the dormant transport runbook.
