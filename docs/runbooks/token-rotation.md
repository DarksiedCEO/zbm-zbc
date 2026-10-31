# Runbook: rotating a token, or replacing one that leaked

Fix wave A (red team RTA-5, ADR 0025 section 4). For Finance (`FIN_`), Legal (`LEGAL_`) and Compliance (`COMPLIANCE_`);
the ledger's own callers are rotated through `LEDGER_CALLERS` (docs/deploy/railway.md, "LEDGER_CALLERS").

Every service accepts, during a rotation window, the current value AND a next value:

| What | Current variable | Next variable (rotation window only) |
|---|---|---|
| The service's bearer token | `<P>_SERVICE_TOKEN` | `<P>_SERVICE_TOKEN_NEXT` |
| A caller token | `<P>_CALLER_TOKENS` (JSON) | `<P>_CALLER_TOKENS_NEXT` (JSON, only the callers being rotated) |
| Andre's approval token | `<P>_ANDRE_APPROVAL_TOKEN_SHA256` | `<P>_ANDRE_APPROVAL_TOKEN_NEXT_SHA256` |

A next value must differ from every current value. Every new token is made the same way:

```sh
openssl rand -hex 32
```

Never paste a token into a chat, an issue or a commit. Set it only in Railway (Sealed variable).

## 0. Store Andre's token as a hash (once)

1. On your own machine: `printf %s "$ANDRE_TOKEN" | sha256sum | cut -d' ' -f1` (64 hex characters).
2. In Railway, on finance, legal and compliance: set `<P>_ANDRE_APPROVAL_TOKEN_SHA256` to that hash and deploy.
   Check one approval works (your token is compared with the stored hash).
3. Delete `<P>_ANDRE_APPROVAL_TOKEN` (the plaintext) on each of them and deploy again. From now on no service holds
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

A service bearer token is the same with `<P>_SERVICE_TOKEN_NEXT`: set it on the service, then move every caller of
that service to the new value (the Finance service token is used by stripe-gateway and seo; the Compliance service
token by finance, legal, seo and detection — see `.railway/railway.ts`), then make it `<P>_SERVICE_TOKEN` and delete
the `_NEXT` variable.

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

## Deploy order cheat sheet

Accepting side first with `_NEXT` (planned) or with the value replaced (leak); sending side second; then remove
`_NEXT`. A deploy of finance, legal or compliance never loses a write: an interrupted write is rolled back and
recorded at the next start (ADR 0025 section 1), and the start-up hold serves 503 until the ledger is reachable.
