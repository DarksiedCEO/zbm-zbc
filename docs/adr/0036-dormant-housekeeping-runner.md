# ADR 0036: Dormant hourly Stripe housekeeping runner

Status: local implementation; independent review and CI required. No schedule or provider activated.

## Authority and scope

Finance optionally recognizes `ops_cron`, a named-only caller for exactly
`POST /fin/v1/jobs/stripe-sessions/run`. The shared public health route remains public. The existing exact-path
middleware rejects other methods, encoded/ambiguous paths and other routes before route-body parsing; the job
handler separately rejects other job names. The existing service bearer is still required. Existing scheduler
permissions remain unchanged. Existing configurations need not contain this new identity to start.

A standalone standard-library script, `services/finance-py/src/housekeeping_runner.py`, remains disabled unless
`FIN_HOUSEKEEPING_ENABLED=1`. It has no Finance-service imports, data-directory access, file claim, startup replay,
lease or scheduler ownership. Never mount the Finance volume into a runner. This increment changes no image,
entrypoint, IaC, credentials, caller mapping in deployment, mail, daily reconciliation, alerts or provider settings.

## One shot and uncertainty

One enabled invocation sends one request. Its ID is `stripe-sessions-YYYYMMDDTHHZ`, normalized to UTC from an
aware clock. Repeated invocations in one hour reuse the ID but can execute repeatedly: the server's repeatable job
records are not durable once-per-hour deduplication. No catch-up, exact-once, overlap exclusion or cadence SLA is
claimed. No automatic retries occur for any HTTP status, redirect, read/DNS/connect failure or timeout. Operator
procedures must separately control overlap and authorize invocation.

HTTP200 requires exactly the existing job response keys, matching request/job, canonical date, boolean replay
indicator and the fixed eight integer counters with per-sweep bounds. `failed>0` or `activation_unknown>0` means
`degraded` and nonzero exit, not clean maintenance. HTTP401/403 is `refused`; all other non200 responses (including
ALL503), malformed/oversized/unexpected output and transport/deadline failures are `outcome_unknown`, nonzero exit.
No response's `took_effect` claim is used. An unknown result can follow actual provider expiry or earlier local
work; it must never be read as no work. Disabled exits0 with explicit `disabled`, never `completed`.

## Transport and process boundary

Configuration accepts only structural origins without credentials, paths, queries, fragments, control characters
or invalid host/port syntax. HTTPS uses normal certificate verification. HTTP is limited to Railway-private service
names or literal loopback for owned local use. The configured origin is trusted operator configuration: valid HTTPS
syntax/certificates do not prove it is Finance. Never accept an origin from a request or untrusted user input.
Bearer and caller tokens must be distinct bounded printable non-template strings. Their dataclass repr is private.

The parent passes a bounded private JSON payload through the worker's stdin, never argv; the worker receives a
minimal environment rather than inherited service credentials. Proxy use is explicitly disabled; redirects are
refused. The HTTP response is read at most64KiB+1 and rejected if oversized. Only fixed result keys and validated
bounded integer counters leave the worker. Its stderr is suppressed; raw response/config/exception data is never
printed. Parent checks the fixed worker output limit before parsing and independently validates its schema.

One owned Python subprocess contains DNS, connection and full response reading. A monotonic total budget(default60,
configurable integer1–120seconds) bounds its communicate operation, including stdin transfer, DNS and trickle reads.
The parent kills only that worker on expiry and reaps/closes its pipes before returning unknown. No subprocess groups,
unrelated processes or global settings are touched; the worker creates no descendants/listeners. Launch and teardown
have OS scheduling overhead, so the configured budget is a work deadline, not a hard realtime return guarantee.
The deadline check also refuses a result observed after the budget. There is no retry or detached background work.

Two exact-name hygiene lint exceptions permit deadline assertions, not performance assertions: an owned DNS stall
and ten-second trickle must be interrupted by a one-second configured budget and return within three seconds with
worker reaped. Other lint rules, skips, timing thresholds and worker ownership remain unchanged.

## Dependencies and limits

Based on the separate durable-fairness candidate(ADR0035), whose independent review is still a release gate.
Its ordinal reservation improves finite-backlog close fairness but cannot promise successful closure during outages,
unbounded new arrivals or unavailable local persistence. The prior seven-case characterization remains valid evidence
of pre-repair starvation and ambiguous external-effect/local-commit outcomes; it is not a runner success certificate.
Local owned HTTP/fake-port tests and full Finance/live gates do not establish actual Stripe behavior, hosted timing,
secret routing, production activation or readiness. No external schedule or real provider was invoked.
