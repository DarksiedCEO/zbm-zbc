# Runbook: rotating a token, or replacing one that leaked

Fix wave A (red team RTA-5, ADR 0025 section 4). For Finance (`FIN_`), Legal (`LEGAL_`) and Compliance (`COMPLIANCE_`);
the ledger's own callers are rotated through `LEDGER_CALLERS` (docs/deploy/railway.md, "LEDGER_CALLERS").

Every service accepts, during a rotation window, the current value AND a next value:

| What | Current variable | Next variable (rotation window only) |
|---|---|---|
| The service's bearer token | `<P>_SERVICE_TOKEN` | `<P>_SERVICE_TOKEN_NEXT` |
| A caller token | `<P>_CALLER_TOKENS` (JSON) | `<P>_CALLER_TOKENS_NEXT` (JSON, only the callers being rotated) |
| Andre's approval token | `<P>_ANDRE_APPROVAL_TOKEN_SHA256` | `<P>_ANDRE_APPROVAL_TOKEN_NEXT_SHA256` |

Every `_NEXT` value needs `<P>_ROTATION_ENDS_AT`, when the window ends (ISO 8601 with a time zone, at most 14 days
ahead, e.g. `2026-11-07T00:00:00Z`). When the window ends, **only the `_NEXT` values are accepted and the old ones
are refused** (fail closed, AEGIS round 3 N3-9: a rotation is often a compromise of the old value). Make sure every
caller has the new value before that time. A window that has ended never stops a service from starting (round 2 N-4:
that would turn any restart into a crash loop), but every start logs a warning and `/health` is **503
`rotation_expired_unpromoted`** (`"rotation_expired": true`; the heartbeat alerts) until you promote: put each `_NEXT`
value into the current variable, delete the `_NEXT` variables and `<P>_ROTATION_ENDS_AT`, and deploy. `/health` shows `"rotation_in_progress": true` only while a window is open (AEGIS
wave A F-11, round 2 N-8). A `<P>_ROTATION_ENDS_AT` that is not ISO 8601 with a time zone refuses the deploy. A next value must differ from every current value. Every new token is made
the same way:

```sh
openssl rand -hex 32
```

Never paste a token into a chat, an issue or a commit. Set it only in Railway (Sealed variable).

## 0. Store Andre's token as a hash (once)

1. On your own machine: `printf %s "$ANDRE_TOKEN" | sha256sum | cut -d' ' -f1` (64 hex characters).
2. In Railway, on finance, legal and compliance: set `<P>_ANDRE_APPROVAL_TOKEN_SHA256` to that hash and deploy.
   Check one approval works (your token is compared with the stored hash).
3. Delete `<P>_ANDRE_APPROVAL_TOKEN` (the plaintext) on each of them and deploy again. (A plaintext token beside a
   hash of a DIFFERENT token refuses to start: the old plaintext can never keep working once a hash is set.) From now on no service holds
   your token, only its hash.

## 1. Planned rotation (no outage)

Example: the caller token `finance_31` that Finance presents to Legal (Railway: `finance.FIN_LEGAL_CALLER_TOKEN`,
accepted by Legal in `LEGAL_CALLER_TOKENS`).

1. `NEW=$(openssl rand -hex 32)` — keep it in your password manager only.
2. On the service that ACCEPTS the token (legal): set `LEGAL_CALLER_TOKENS_NEXT={"finance_31":"<NEW>"}` and deploy.
   Legal now accepts the old and the new value.
3. On the service that SENDS it (finance): set `FIN_LEGAL_CALLER_TOKEN=<NEW>` and deploy. Check finance's
   `/health` is 200 and one call that crosses (a billing-readiness read) answers.
4. On legal: put `<NEW>` into `LEGAL_CALLER_TOKENS` for `finance_31`, delete `LEGAL_CALLER_TOKENS_NEXT`, deploy.
   The old value is now refused everywhere.

### 1a. A service bearer token (callers hold an IaC reference to it)

Every caller of a service bearer is a **reference** in `.railway/railway.ts` to the accepting service's variable, not
a value of its own (AEGIS round 4 N4-6):

| Accepting service | Its variable | Callers' references |
|---|---|---|
| finance | `FIN_SERVICE_TOKEN` | stripe-gateway `STRIPE_GATEWAY_FINANCE_TOKEN: finance.env.FIN_SERVICE_TOKEN`; seo `SEO_FINANCE_TOKEN` |
| compliance | `COMPLIANCE_SERVICE_TOKEN` | finance `FIN_COMPLIANCE_TOKEN`, detection `DETECTION_COMPLIANCE_TOKEN`, legal `LEGAL_COMPLIANCE_TOKEN` (`${{compliance.COMPLIANCE_SERVICE_TOKEN}}`), seo |
| legal | `LEGAL_SERVICE_TOKEN` | finance `FIN_LEGAL_TOKEN`, compliance `COMPLIANCE_LEGAL_TOKEN` |

So the reference itself moves during the window. Five steps, each a deploy (example: finance; tested in
`services/finance-py/tests/test_fixwave_a_isolation.py::test_n4_6_stripe_gateway_to_finance_across_a_service_token_rotation`):

1. finance: set `FIN_SERVICE_TOKEN_NEXT=<NEW>` and `FIN_ROTATION_ENDS_AT` (a few days ahead); deploy. Old and new
   both work.
2. `.railway/railway.ts`: change every caller's reference from `finance.env.FIN_SERVICE_TOKEN` to
   `finance.env.FIN_SERVICE_TOKEN_NEXT` (for a `${{…}}` string, `${{finance.FIN_SERVICE_TOKEN_NEXT}}`); apply. Each
   caller redeploys and sends the new value. Check each caller once (stripe-gateway: a test webhook from Stripe's
   dashboard answers 200).
3. finance: set `FIN_SERVICE_TOKEN=<NEW>` and keep `FIN_SERVICE_TOKEN_NEXT=<NEW>` (equal values are accepted as
   "promoted"); deploy. The old value is refused from now on.
4. `.railway/railway.ts`: change the references back to `finance.env.FIN_SERVICE_TOKEN`; apply.
5. finance: delete `FIN_SERVICE_TOKEN_NEXT` and `FIN_ROTATION_ENDS_AT`; deploy.

Never delete a `_NEXT` variable while a reference still points at it (the caller would get an empty value). If the
window ends before step 3, only the new value works and finance's `/health` is 503 `rotation_expired_unpromoted`, with
one ALERT `rotation_expired_unpromoted` per start and one `rotation_old_token_refused` per refused request: a caller
still on the old value is loud, never a silent 401. **Stripe webhooks:** Stripe retries a refused delivery with
backoff for up to 3 days. Finish steps 2–5 (or move the reference) within that time, then in Stripe's dashboard
(Developers → Webhooks → the endpoint) resend any event still marked failed; finance's webhook handling is idempotent,
so a resend of an event that did arrive is harmless.

Andre's token: make a new one, set `<P>_ANDRE_APPROVAL_TOKEN_NEXT_SHA256` to its hash on the three services, deploy,
check an approval with the new token, then move that hash to `<P>_ANDRE_APPROVAL_TOKEN_SHA256` and delete the
`_NEXT` variable.

## 2. A token leaked (compromise)

Speed beats the overlap: the old value must stop working now.

1. Make the new value (`openssl rand -hex 32`).
2. On the ACCEPTING service, replace the current value directly (no `_NEXT`) and deploy. The leaked value is refused
   from this deploy on. The sender's calls fail closed (401/403) until step 3.
3. On the SENDING service(s), set the new value and deploy. For the Finance service token, deploy stripe-gateway
   first: Stripe retries a refused webhook for 3 days, so nothing is lost while the gateway catches up.
4. Read the ledger evidence for the window: `GET /fin/v1/audit/evidence`, `/legal/v1/audit/evidence`,
   `/compliance/v1/audit/evidence` (your token) for writes you do not recognise, and the services' logs for
   `founder_approval_refused`.
5. A leaked Andre token: replace its hash on all three services at once (step 0 with the new token), then check every
   approval since the leak in the audit evidence.

## Rolling the ledger back

Not after fix wave A has written an event id two departments share: the older binary refuses that log
(services/ledger-rust/README.md, "Rollback"). Roll forward.

## Deploy order cheat sheet

Accepting side first with `_NEXT` (planned) or with the value replaced (leak); sending side second; then remove
`_NEXT`. A deploy of finance, legal or compliance never loses a write: an interrupted write that never reached its
anchor is rolled back and recorded at the next start; one that did is held for you (ADR 0025 sections 1 and 7). Railway
health-checks `/livez`, so a deploy activates even when `/health` is 503 (held, degraded, reconcile mode).
