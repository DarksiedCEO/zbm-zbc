# Operator delivery of an existing SEO report

Builder preparation only; independent review and provider verification are still required. This command does not generate a report, recover a paid event, replay a webhook, or fetch credentials automatically. Imports and the default command make no network calls.

Use an existing authorized SEO `render_client_report` HTML response exported as JSON. The command checks its content against `document_sha256` and `bytes`, and requires tenant/audit/report identifiers. The operator must independently confirm the export came from the authorized SEO service and that the client, invoice, recipient and sender are correct. An offline JSON export is not cryptographic proof of source authorization or payment.

Preview, without a key or outbox:

```sh
python3 -B deploy/gate5/report_cli.py \
  --report /absolute/private/approved-report.json \
  --client CLIENT_ID --invoice INVOICE_ID \
  --sender reports@zbestmedia.com --recipient client@example.com
```

Inspect **every** returned field and independently compare the report hash to the approved service export. Review the HTML attachment itself before approval. The digest binds the sender, recipient, all report identifiers, declared mapping, attachment bytes, subject and filename. It is not approval merely to run the preview.

When separately authorized to send, use the same inputs, add `--execute`, `--approve-sha256 EXACT_PREVIEW_DIGEST`, and `--outbox /absolute/canonical/private/report-outbox`. This role requires an interactive terminal and prompts without echo for a **dedicated report** Resend key. Do not use the backup-alert key. Never put keys in arguments or export files. The sender must be an approved mailbox at `zbestmedia.com`; provider verification of that domain and key scope is an operational prerequisite, not performed here.

The outbox directory must be an absolute canonical path owned by the current user and mode 0700. Its parent must already exist. Use the **same durable outbox directory for every attempt**. The database is mode 0600 and contains the frozen attachment and recipient in plaintext; protect and retain it as sensitive business data. Never delete, replace or move aside the database to retry a report.

Delivery is a single attempt. Before network dispatch, SQLite FULL-synchronous state commits `DISPATCHING`. A successful response stores `ACCEPTED` and the provider message ID. Acceptance is **not inbox delivery**. Repeating the exact accepted envelope with the same outbox returns its outcome without another send. Changing a frozen envelope at the same delivery identity refuses. The outbox has a fixed maximum of 32 identities and attachments are limited to 2 MiB.

Timeout, malformed response or ambiguous transport outcome becomes `UNKNOWN`. A process interruption can leave `DISPATCHING`. Both refuse another send, including after restart. This command intentionally supplies no retry/reset/reconciliation operation. Retain the outbox, inspect provider evidence through a separately authorized procedure and obtain a supported reconciliation decision. Do not assume no email was sent because the command returned an error. A definite provider error is also conservatively unresolved by this transport.

The adapter connects only to `https://api.resend.com/emails`, without a configurable URL, redirect path, proxy configuration or local mock mode. It uses a bounded child process for DNS/TLS/request/response and sends the credential through stdin to that owned child. Its default whole-child deadline is 10 seconds. There are no automatic sends on import and no runtime fixture injection switches. It does not guarantee memory zeroization, host secrecy, provider availability or inbox delivery.

## Builder verification

```sh
python3 -B -m unittest discover -s devtools/gate5_tests -p 'test_report_*.py' -v
```

Tests replace the transport exchange with in-process mocks; no emails, keychain reads, providers or actual network were used. Coverage: dry-run inactivity, explicit approval, content/hash/format refusal, recipient/mapping binding, duplicate/restart behavior, timeout UNKNOWN, interrupted DISPATCHING, capacity/conflict, attachment integrity, exact CLI wiring, corruption and private-path refusal. The test output is retained in the private release evidence, outside the public repository.

Adapted from the private Gate4 `report_delivery` and `report_transport` patterns, with production-facing subject, explicit recipient, no local mock destination, one-attempt policy and a new operator CLI. No changes to the SEO service, topology builder, historical paid flow, RC deployment or CI wiring are part of this subtask. This is implementer evidence, not AEGIS certification.

An initial builder test run had four setup errors because macOS temporary-directory aliases were noncanonical (`/var` versus `/private/var`). Fixtures now pass their canonical paths; the private-directory guard was not weakened. The corrected suite subsequently passed. No provider invocation occurred in any test run.
