# sales-py — Lead Generation & Opportunity Intelligence (26) + Sales (27)

One service for both brands — ZBM (full-service agency: Revenue Recovery, social, billboards/OOH, TV, radio,
digital media buys, creative) and ZBC (Z Best Clips, clipping campaigns). It takes in leads from four sources,
dedupes, scores and routes them, runs the pipeline (accounts, contacts, leads, opportunities, activities, tasks),
does outreach inside the law (cold email under CAN-SPAM from a separate domain; texts and calls only with recorded
consent, TCPA quiet hours), keeps the two price books, builds proposals and hands won deals to Onboarding and
Finance. Deterministic single-task components; no model calls. Recruiting clippers is not here (Clipper Network).

Architecture, founder decisions and the unlock list: `docs/adr/0013-sales-leadgen-department-architecture.md`.

**Status:** built and tested; not in force. No send provider, lead source or department client is wired: email, SMS
and voice stay queued, imports answer `SOURCE_NOT_WIRED`, proposals cannot be sent (`LEGAL_UNAVAILABLE`) and won
hand-offs stay `pending_delivery`.

## Run

```bash
cd services/sales-py && python3 -m pytest -q -p no:cacheprovider     # no network; counts: docs/test-counts.md
export SALES_SERVICE_TOKEN=<secret>                       # required: refuses to start without it
export SALES_CALLER_TOKENS='{"hub": "<>=32 printable chars>", "dashboard": "...", "sales_agent": "...", "scheduler": "..."}'
export SALES_DATA_DIR=/var/lib/zbm/sales                  # required unless SALES_NON_PRODUCTION=1; owned by the service user, 0700
export SALES_PII_HASH_KEY_FILE=/etc/zbm/sales/pii.key     # openssl rand -hex 32 > pii.key; 0600; never change it
export SALES_ANDRE_APPROVAL_TOKEN=<Andre's token>         # unset = nothing can be approved
export SALES_OUTREACH_DOMAIN=zbm-outreach.example SALES_ZBM_DOMAIN=zbestmedia.com SALES_ZBC_DOMAIN=zbestclips.com
export SALES_POSTAL_ADDRESS="<street address, city, state, zip>"
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret>
cd src && python3 -m api                                  # SALES_BIND_ADDR (127.0.0.1), SALES_PORT (8450)
```

Live run against the real ledger binary: `LEDGER_BIN=<ledger-rust>/target/release/server python3 devtools/live_run.py`.

Headers: `Authorization: Bearer <service token>` on every route but `/health`; `X-SALES-Caller-Token` everywhere
else; Andre's routes also `X-Andre-Approval-Token` (through the `dashboard` caller only).

| Setting | Default | Notes |
|---|---|---|
| `SALES_SERVICE_TOKEN` | — | required, 32..512 printable characters |
| `SALES_CALLER_TOKENS` | `{}` | JSON `{caller: token}`; callers in `src/config.py` `KNOWN_CALLERS`; all distinct, none equal to the service token |
| `SALES_NON_PRODUCTION` | 0 | 1 allows the in-memory store and a fixed test PII key. Never in production |
| `SALES_DATA_DIR` | — | required in production; a directory owned by the service user, mode 0700 |
| `SALES_PII_HASH_KEY_FILE` | — | required in production and whenever `SALES_DATA_DIR` is set: a generated key of at least 32 bytes as hex or base64 (`openssl rand -hex 32`), file mode 0600, not a symlink. Its fingerprint is bound in the log; a different key refuses start |
| `SALES_ANDRE_APPROVAL_TOKEN` | — | Andre's approvals; equal to the service or a caller token = not configured |
| `SALES_OUTREACH_DOMAIN` | — | the only domain cold email is sent from; unset = no cold email (`OUTREACH_NOT_CONFIGURED`) |
| `SALES_ZBM_DOMAIN`, `SALES_ZBC_DOMAIN` | — | the two brands' own domains (different registrable domains); both required with an outreach domain, which must not share a registrable domain with either |
| `SALES_PRIMARY_DOMAINS` | — | optional comma list of further brand-owned domains the outreach domain must also be separate from |
| `SALES_QUEUE_MAX_PER_CALLER` | 2000 | queued messages one caller may have waiting (1..20000); past it `429 QUEUE_FULL` |
| `SALES_POSTAL_ADDRESS` | — | required with an outreach domain; printed in every email |
| `SALES_OUTREACH_FROM_LOCAL` | `hello` | the From mailbox (must accept replies; `noreply` refused) |
| `SALES_WARMUP_SCHEDULE` | `20,30,40,60,80,100,150,200` | sends per day by warm-up step; day 1 ≤ 50, never decreasing, at most doubling, ≤ 500 |
| `SALES_DAILY_SEND_CAP` | 200 | ceiling on any day (1..500) |
| `SALES_AUTO_APPROVE_MAX` | `10000.00` | largest proposal agents send without Andre; at most 10000.00 |
| `SALES_STALE_LEAD_DAYS` | 30 | an open lead untouched this long is marked `stale` (7..365) |
| `SALES_EMAIL_PROVIDER`, `SALES_SMS_PROVIDER`, `SALES_VOICE_PROVIDER`, `SALES_PUBLIC_DATA_PROVIDER`, `SALES_PAID_LEAD_PROVIDER`, `SALES_ONBOARDING_URL`, `SALES_FINANCE_URL`, `SALES_LEGAL_URL` | unset | not built: setting one refuses start |
| `LEDGER_SERVICE_URL`, `LEDGER_SERVICE_TOKEN` | — | unset = every write refused (fail closed) |
| `SALES_BIND_ADDR`, `SALES_PORT` | 127.0.0.1, 8450 | |
| `SALES_REQUEST_HEAD_TIMEOUT_SECONDS`, `SALES_KEEP_ALIVE_TIMEOUT_SECONDS`, `SALES_LIMIT_CONCURRENCY`, `SALES_SWITCH_INTERVAL_SECONDS`, `SALES_DRAINS_MAX` | 10, 5, 128, 0.001, 512 | launcher tuning (`src/serve.py`, shared with the other Python services) |

## Routes

All under `/sales/v1` except `/health`. "worker" = `dashboard` or `sales_agent`; "Andre" = `dashboard` +
`X-Andre-Approval-Token`.

| Route | Who | Purpose |
|---|---|---|
| `GET /health` | open | `ok` or `degraded`, nothing else |
| `GET /status` | dashboard | integrity, ports wired, send pace, queue, pending approvals |
| `GET /intelligences` | worker | the twelve single-task components |
| `POST /leads` | hub, onboarding, detection (inbound); dashboard (referral, partner) | a lead with its evidence |
| `POST /leads/import` | sales_agent, scheduler | public-data / paid-provider leads through their ports |
| `GET /leads`, `/leads/{id}`; `POST /leads/{id}/owner`, `/disqualify`, `/convert` | worker | pipeline |
| `GET /contacts/{id}` | worker | contact, consent, suppression and hold state |
| `POST /accounts/{id}/display-name`, `/contacts/{id}/first-name` | dashboard | the only values `{{company}}` / `{{first_name}}` render (verified by a person) |
| `POST /tasks/{id}/decision` | Andre | any reply (every channel, but exact auto-reply texts) holds texts and calls to the contact's numbers; `not_an_opt_out` lifts it, `opt_out` makes it permanent |
| `POST /contacts/{id}/time-zone` | hub, onboarding, dashboard | never the agent; a +1 number needs an American zone |
| `GET /opportunities`, `/opportunities/{id}`; `POST /opportunities/{id}/stage` | worker | stages (not `closed_won`) |
| `POST /activities`; `GET /tasks`; `POST /tasks/{id}/close` | worker | activities and tasks |
| `POST /consents` | hub, onboarding, dashboard | express consent for sms / voice, per brand |
| `POST /consents/revoke` | hub, dashboard, provider_events, sales_agent | revokes every channel and brand, suppresses |
| `POST /suppressions`; `GET /suppressions` | hub, dashboard, provider_events, sales_agent; dashboard, compliance_38 | append-only |
| `POST /unsubscribe` | hub | the one-click link's token |
| `GET /templates`, `/templates/{id}`; `POST /templates`, `/templates/{id}/versions`, `/templates/{id}/versions/{v}/edit` | worker | drafts |
| `POST /templates/{id}/versions/{v}/approve` | Andre | binds the content hash |
| `POST /outreach/email`, `/outreach/sms`, `/outreach/voice` | sales_agent | queue (rules checked now and again at send) |
| `GET /outreach/messages`; `POST /outreach/messages/{id}/cancel` | worker | the queue |
| `POST /events/email`; `POST /replies` | provider_events | bounces, complaints, replies |
| `GET /pricebook/{brand}` | worker | the price book |
| `POST /pricebook/{brand}/lines/{line_id}/approve`, `/withdraw` | Andre | versioned prices |
| `POST /proposals`; `GET /proposals`, `/proposals/{id}` | worker | build (auto-approved or `pending_andre`) |
| `POST /proposals/{id}/approve` | Andre | binds the content hash |
| `POST /proposals/{id}/send`, `/lost`; `GET /handoffs` | worker | send needs Legal |
| `POST /proposals/{id}/won` | Andre; or worker with the client's acceptance confirmed by Legal | hands off to Onboarding and Finance |
| `POST /jobs/{send-queue,warmup-reset,handoff-retry,stale-leads,integrity}/run` | scheduler | jobs |
| `GET /audit/integrity`, `/audit/export` | dashboard, compliance_38 | export with emails and phones as keyed hashes |
