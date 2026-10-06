# service-py — Customer Service (30) + Client Success (29)

One service for both brands, ZBM (full-service ad agency) and ZBC (Z Best Clips). It keeps one conversation per
client across email, chat, SMS and phone, answers routine questions **right away, but only with answers Andre
approved**, sends money, contracts and complaints to Andre (and Legal, Finance, Cybersecurity, Compliance where
they apply), keeps the consent registry, runs SLA timers, scores every account's health and starts a save plan for
an account at risk. No LLM calls: every decision is a deterministic, single-task intelligence.

Architecture, Andre's locked answers (Oct 5 2026) and the unlock list:
`docs/adr/0014-customer-service-success-department-architecture.md`.

**Status:** built and tested; not in force. No email, SMS, chat-push, voice or alert provider is chosen, so outbound
messages stay queued (visibly) and alerts are recorded, not sent. Only the Legal (37) handoff has a client; Finance
(31), Cybersecurity (22), Compliance (38), the results-trend source and contract end dates are ports with
fail-closed stand-ins.

## Run

```bash
cd services/service-py && python3 -m pytest -q          # no network; counts: docs/test-counts.md
export SVC_SERVICE_TOKEN=<secret>                       # required: refuses to start without it
export SVC_CALLER_TOKENS='{"hub": "<>=32 printable chars>", "email_gateway": "...", "sms_gateway": "...", "dashboard": "...", "scheduler": "..."}'
export SVC_ANDRE_APPROVAL_TOKEN=<Andre's secret>        # unset -> nothing can be approved, replied to or offered
export SVC_DATA_DIR=/var/lib/zbm/service               # required unless SVC_NON_PRODUCTION=1; owned by the service user, 0700
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret>
export SVC_SUPPORT_EMAIL_ZBM=support@zbestmedia.com SVC_SUPPORT_EMAIL_ZBC=support@zbestclips.com
cd src && python3 -m api                                # SVC_BIND_ADDR (127.0.0.1), SVC_PORT (8460)
```

Headers: `Authorization: Bearer <service token>` on every route but `/health`; `X-SVC-Caller-Token` everywhere
else; Andre's actions go through the `dashboard` caller AND carry `X-Andre-Approval-Token`. Callers (`src/config.py`
`KNOWN_CALLERS`): `hub` (sites, client portal, chat widget backend), `email_gateway`, `sms_gateway`, `voice_gateway`,
`onboarding`, `detection`, `finance_31`, `legal_37`, `compliance_38`, `scheduler`, `dashboard`.

| Setting | Default | Notes |
|---|---|---|
| `SVC_SERVICE_TOKEN` | — | required, 32..512 printable characters |
| `SVC_CALLER_TOKENS` | `{}` | JSON `{caller: token}`; known callers only; all distinct and different from the service token |
| `SVC_ANDRE_APPROVAL_TOKEN` | unset | Andre's approvals (FounderGate); equal to the service token or any caller token = not configured |
| `SVC_NON_PRODUCTION` | 0 | 1 allows the in-memory store (tests only). Never in production |
| `SVC_DATA_DIR` | — | required in production; a directory owned by the service user, mode 0700 |
| `SVC_HMAC_KEY_FILE` | — | required in production: a 0600 file holding 32+ random bytes, base64 (all zeros or fewer than 16 distinct bytes refused); keys the digests of stored bodies and consent texts (HMAC-SHA-256). Its fingerprint is written to the log at the first start; another key refuses start. `SVC_NON_PRODUCTION=1` without it uses a fixed test key |
| `LEDGER_SERVICE_URL`, `LEDGER_SERVICE_TOKEN` | unset | unset = every write refused (fail closed) |
| `SVC_SUPPORT_EMAIL_ZBM`, `SVC_SUPPORT_EMAIL_ZBC` | unset | the support identity per brand (inbound must be addressed to it; outbound is sent from it); must differ; unset = that brand's email refused |
| `SVC_SMS_NUMBER_ZBM`, `SVC_SMS_NUMBER_ZBC` | unset | the brand's SMS number, E.164; must differ; unset = that brand's SMS refused |
| `SVC_LEGAL_URL` / `SVC_LEGAL_TOKEN` / `SVC_LEGAL_CALLER_TOKEN` | unset | all three or none; Legal (37) `POST /legal/v1/requests`; none = handoff `not_wired` |
| `SVC_SLA_P1_FIRST_RESPONSE_MINUTES`, `SVC_SLA_P2_FIRST_RESPONSE_MINUTES`, `SVC_SLA_P3_FIRST_RESPONSE_MINUTES`, `SVC_SLA_P4_FIRST_RESPONSE_MINUTES` | 60, 240, 480, 1440 | 5..2880; never looser for a higher priority; never above that priority's resolution target |
| `SVC_SLA_P1_RESOLUTION_MINUTES`, `SVC_SLA_P2_RESOLUTION_MINUTES`, `SVC_SLA_P3_RESOLUTION_MINUTES`, `SVC_SLA_P4_RESOLUTION_MINUTES` | 480, 1440, 4320, 10080 | 60..20160; same ordering rule |
| `SVC_AT_RISK_THRESHOLD` | 60 | 1..99; a health score below it is at risk |
| `SVC_RENEWAL_WINDOW_DAYS` | 60 | 7..180 |
| `SVC_VOICE_PROVIDER`, `SVC_EMAIL_PROVIDER`, `SVC_SMS_PROVIDER`, `SVC_CHAT_PROVIDER`, `SVC_ALERT_PROVIDER` | unset | not built: setting one refuses start. Exception: `SVC_SMS_PROVIDER=nonprod_file` with `SVC_NON_PRODUCTION=1` and `SVC_NONPROD_OUTBOX_FILE` (absolute path) "sends" SMS by appending JSON lines to that file (the live run's sender; never in production) |
| `SVC_FINANCE_URL`, `SVC_CYBER_URL`, `SVC_COMPLIANCE_URL`, `SVC_RESULTS_URL`, `SVC_ONBOARDING_URL` | unset | clients not built: setting one refuses start |
| `SVC_BIND_ADDR`, `SVC_PORT` | 127.0.0.1, 8460 | |
| `SVC_REQUEST_HEAD_TIMEOUT_SECONDS`, `SVC_KEEP_ALIVE_TIMEOUT_SECONDS`, `SVC_LIMIT_CONCURRENCY`, `SVC_SWITCH_INTERVAL_SECONDS`, `SVC_DRAINS_MAX` | 10, 5, 128, 0.001, 512 | launcher tuning (`src/serve.py`, shared with the other Python services; the switch interval only 0.0001 .. 0.05, `src/launch_guard.py`) |

Addresses are taken lower-case (email) and E.164 (phone); the gateways normalise before posting. No body may carry
a date of birth, government id, card or bank account number, IP address or device field (422, anywhere in the body).

## Routes

| Route | Who | Purpose |
|---|---|---|
| `GET /health` | open | `status` only (`ok` / `degraded`) |
| `GET /svc/v1/status` | dashboard | integrity, wired ports, queues, SLA targets, Andre gate configured |
| `POST /svc/v1/contacts`; `GET /contacts/{id}` | hub, onboarding; dashboard | the minimum about a person: ref, email, phone, time zone, display name |
| `POST /svc/v1/consents`; `POST /consents/revoke`; `GET /contacts/{id}/consents` | hub, onboarding; hub, dashboard; dashboard, hub, compliance_38 | consent registry (express only; names the address it is for; text stored once by keyed hash) |
| `POST /svc/v1/contacts/{id}/sms-pause/clear` | Andre | lift the pause an unclear inbound SMS put on proactive SMS (never restores a revoked consent) |
| `POST /svc/v1/chat/messages`; `GET /chat/threads/{ticket_id}?contact_ref=&brand=` | hub | inbound chat (answered inline when an approved answer matches); the thread as the contact sees it |
| `POST /svc/v1/inbound/email` | email_gateway | inbound email to a brand's support identity |
| `POST /svc/v1/inbound/sms` | sms_gateway | inbound SMS; a bare STOP / UNSUBSCRIBE / CANCEL / END / QUIT revokes SMS consent at once |
| `POST /svc/v1/calls`; `POST /calls/route`; `POST /calls/{id}/handoff` | voice_gateway (handoff: also dashboard) | call records (refs only), where a call goes now, handoff to Andre |
| `PUT /svc/v1/phone/routing/{brand}` | Andre | business hours and in-hours action |
| `GET /svc/v1/tickets?status=&brand=&queue=`; `GET /tickets/{id}` | dashboard | queues and a ticket with its messages and handoffs |
| `POST /svc/v1/tickets/{id}/reply` | Andre | his reply (channel rules apply) |
| `POST /svc/v1/tickets/{id}/status`, `/priority` | dashboard (resolving or closing a money, legal, complaint, security or privacy ticket: Andre) | status machine; priority (SLA targets recomputed) |
| `POST /svc/v1/kb/articles`, `/templates`, `/offers`; `GET` the same | dashboard | a new version (always unapproved) |
| `POST .../{id}/approve`, `.../{id}/retire` | Andre | approve exactly one version by its content hash; retire |
| `POST /svc/v1/accounts`; `GET /accounts`; `GET /accounts/{id}/health` | onboarding; dashboard; dashboard | accounts, contract end, the explainable score |
| `POST /svc/v1/accounts/{id}/events` | hub | portal logins |
| `GET /svc/v1/renewals` | dashboard | contracts ending within the window |
| `GET /svc/v1/save-plans`; `POST /save-plans/{id}/offer`, `/close`; `POST /save-plans/{id}/steps/{step}/done` | dashboard; Andre; dashboard | save plans |
| `POST /svc/v1/nps/surveys`; `POST /nps/responses` | dashboard, scheduler; hub | NPS |
| `GET /svc/v1/alerts`, `/outbound?status=` | dashboard | alerts to Andre (delivery `not_wired` until a provider is chosen), outbound queue |
| `POST /svc/v1/outbound/{message_id}/resolve` | Andre | a message held as `sending` (send outcome unknown after a restart): `sent`, `requeue` or `cancel` |
| `POST /svc/v1/jobs/{sla-sweep,health-recompute,save-plan-tick,outbound-tick,handoff-retries,integrity}/run` | scheduler | the jobs (idempotent per request id) |
| `GET /svc/v1/audit/integrity`, `/audit/events` | dashboard, compliance_38 | integrity against the ledger; the log with personal data replaced by HMAC under a key made for that export (returned once with it) |

## Live run

```bash
LEDGER_BIN=/path/to/ledger-rust/target/release/server python3 devtools/live_run.py
```

The real ledger binary and the production entrypoint over real HTTP, a durable data directory, a restart and
`GET /ledger/verify`; exit 0 only if every check passes.
