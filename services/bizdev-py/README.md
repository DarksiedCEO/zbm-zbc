# bizdev-py — New Business Development (12)

One service for both brands — ZBM (Z Best Media) and ZBC (Z Best Clips) — that owns the department's two jobs:

- **Big pursuits:** enterprise and government bids, RFP / RFQ responses and formal pitches, as multi-month pursuits
  with stages, deadlines, bid / no-bid qualification and win / loss.
- **Partnerships:** referral partners, agency alliances and white-label deals, with commissions paid through Finance.

Everyday leads and deals stay with Sales (27, `services/sales-py`); influencer and co-marketing work is department 11;
political work and public affairs are department 14 (public-sector procurement bids are here). Deterministic
single-task intelligences; no model calls. The AI drafts; Andre approves every response and pitch by its content
hash.

Architecture, founder decisions and the unlock list: `docs/adr/0016-new-business-development-department-architecture.md`.

**Status:** built and tested; not in force. No provider or department client is wired: outreach email, submissions
and partner payouts stay `queued`, won pursuits' hand-offs stay `pending_delivery`, bid-portal import answers
`SOURCE_NOT_WIRED`, and every Legal (37) agreement hand-off is refused `LEGAL_UNAVAILABLE` (so no partner deal can be
marked won yet). Nothing is ever paid.

## Run

```bash
cd services/bizdev-py && python3 -m pytest -q -p no:cacheprovider     # no network; counts: docs/test-counts.md
export NBD_SERVICE_TOKEN=<secret>                       # required: refuses to start without it
export NBD_CALLER_TOKENS='{"dashboard": "<>=32 printable chars>", "bizdev_agent": "...", "scheduler": "...", "provider_events": "...", "hub": "...", "finance_31": "...", "compliance_38": "..."}'
export NBD_DATA_DIR=/var/lib/zbm/bizdev                 # required unless NBD_NON_PRODUCTION=1; owned by the service user, 0700
export NBD_PII_HASH_KEY_FILE=/etc/zbm/bizdev/pii.key    # openssl rand -hex 32 > pii.key; 0600; never change it
export NBD_ANDRE_APPROVAL_TOKEN=<Andre's token>         # unset = nothing can be approved
export NBD_OUTREACH_DOMAIN=zbm-partners.example NBD_ZBM_DOMAIN=zbestmedia.com NBD_ZBC_DOMAIN=zbestclips.com
export NBD_POSTAL_ADDRESS="<street address, city, state, zip>"
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret>
cd src && python3 -m api                                # NBD_BIND_ADDR (127.0.0.1), NBD_PORT (8490)
```

Live run against the real ledger binary and this production entrypoint:
`LEDGER_BIN=<ledger-rust>/target/release/server python3 devtools/live_run.py` (no check depends on the time of day).

**Request-id contract.** Every write carries a `request_id`: a UUID (with or without hyphens) or 16..64 hex
characters, in any case — it is lower-cased before the shape check and before the request key is built, so
`ABCD…` and `abcd…` are the same request. Nothing else is accepted (422). The request key is
`op|target|request_id` per caller, with the SHA-256 of the body: the same body is answered as the first time (also
after a restart), a different body under the same key is `409 REQUEST_ID_REUSED`. Finance's ids
(`finance_event_id`, `finance_ref`) are finance-py's own generated ids (`fin-<prefix>-<40 hex>` or 26 Crockford).

Headers: `Authorization: Bearer <service token>` on every route but `/health`; `X-NBD-Caller-Token` everywhere
else; Andre's routes also `X-Andre-Approval-Token`, accepted only through the `dashboard` caller.

| Setting | Default | Notes |
|---|---|---|
| `NBD_SERVICE_TOKEN` | — | required, 32..512 printable characters |
| `NBD_CALLER_TOKENS` | `{}` | JSON `{caller: token}`; callers in `src/config.py` `KNOWN_CALLERS`; all distinct, none equal to the service token |
| `NBD_NON_PRODUCTION` | 0 | 1 allows the in-memory store and a fixed test PII key. Never in production |
| `NBD_DATA_DIR` | — | required in production; a directory owned by the service user, mode 0700; one process and one service instance per directory (flock + single-use claim token) |
| `NBD_PII_HASH_KEY_FILE` | — | required in production and whenever `NBD_DATA_DIR` is set: a generated key of at least 32 bytes as hex or base64, file mode 0600, not a symlink. Its fingerprint is bound in the log; a different key refuses start |
| `NBD_ANDRE_APPROVAL_TOKEN` | — | Andre's approvals; equal to the service or a caller token = not configured (every approval refused) |
| `NBD_DEAL_APPROVAL_THRESHOLD` | `10000.00` | a counterparty group's aggregate value above which Andre approves the deal; may only be lowered |
| `NBD_AGGREGATION_WINDOW_DAYS` | 365 | how far back sibling deals count toward the aggregate (90..3650) |
| `NBD_OUTREACH_DOMAIN` | — | the only domain outreach email is sent from; unset = no outreach (`OUTREACH_NOT_CONFIGURED`) |
| `NBD_ZBM_DOMAIN`, `NBD_ZBC_DOMAIN` | — | the brands' own domains; both required with an outreach domain, which must not share a registrable domain with either |
| `NBD_PRIMARY_DOMAINS` | — | optional comma list of further brand-owned domains the outreach domain must be separate from |
| `NBD_POSTAL_ADDRESS` | — | required with an outreach domain; printed in every email (CAN-SPAM) |
| `NBD_OUTREACH_FROM_LOCAL` | `partners` | the From mailbox (must accept replies; `noreply` refused) |
| `NBD_DAILY_SEND_CAP` | 50 | outreach emails per UTC date (1..200), counted by the service clock's date |
| `NBD_UNKNOWN_TICKS_BEFORE_TASK` | 6 | job runs a submission or payout may stay `sending` with an unknown outcome before one task opens for Andre (1..1000) |
| `NBD_PAYOUT_MAX_REFUSALS` | 3 | refusals by Finance after which a payout is `held` (never resent) with a task for Andre (1..20) |
| `NBD_QUEUE_MAX_PER_CALLER` | 500 | queued messages or submissions one caller may have waiting (1..5000); past it `429 QUEUE_FULL` |
| `NBD_EMAIL_PROVIDER`, `NBD_SUBMISSION_PROVIDER`, `NBD_BID_SOURCE_PROVIDER`, `NBD_ONBOARDING_URL`, `NBD_FINANCE_URL`, `NBD_LEGAL_URL`, `NBD_SALES_SUPPRESSION_URL` | unset | not built: setting one refuses start |
| `LEDGER_SERVICE_URL`, `LEDGER_SERVICE_TOKEN` | — | unset = every write refused (fail closed) |
| `NBD_BIND_ADDR`, `NBD_PORT` | 127.0.0.1, 8490 | |
| `NBD_REQUEST_HEAD_TIMEOUT_SECONDS`, `NBD_KEEP_ALIVE_TIMEOUT_SECONDS`, `NBD_LIMIT_CONCURRENCY`, `NBD_SWITCH_INTERVAL_SECONDS`, `NBD_DRAINS_MAX` | 10, 5, 128, 0.001, 512 | launcher tuning (`src/serve.py`, shared with the other Python services) |

## Routes

All under `/nbd/v1` except `/health`. "worker" = `dashboard` or `bizdev_agent`; "Andre" = `dashboard` +
`X-Andre-Approval-Token`.

| Route | Who | Purpose |
|---|---|---|
| `GET /health` | open | `ok`, `degraded` or (503) `closed`, nothing else |
| `GET /status` | dashboard | integrity, ports wired, queues, pending approvals, threshold |
| `GET /intelligences` | worker | the thirteen single-task components |
| `POST /pursuits`; `GET /pursuits`, `/pursuits/{id}` | worker | a pursuit: rfp, rfq, enterprise_bid, government_bid, formal_pitch; shows its deal gate |
| `POST /pursuits/import` | bizdev_agent, scheduler | bid-portal sourcing: always `503 SOURCE_NOT_WIRED` (no fetching code) |
| `POST /pursuits/{id}/qualification` | worker | the seven criteria; i01 recommends bid / no_bid / needs_andre |
| `POST /pursuits/{id}/bid-decision` | worker for `no_bid`; Andre for `bid` | names the exact qualification hash |
| `POST /pursuits/{id}/value` | worker | changes the value (un-approves the deal) |
| `POST /pursuits/{id}/deadline` | Andre | moves a deadline (a buyer's addendum) |
| `POST /pursuits/{id}/deal-approval` | Andre | binds value, aggregate and group (`deal_gate.binding_sha256`) |
| `POST /pursuits/{id}/checklist` | worker | a government bid: add addendum items (never remove or edit) |
| `POST /pursuits/{id}/checklist/{item_id}/attest` | Andre | one item, by its exact hash |
| `POST /pursuits/{id}/won` | Andre | after a delivered submission; hands off to Onboarding and Finance |
| `POST /pursuits/{id}/lost`; `/withdraw` | worker before anything was delivered, Andre after; Andre | close (a delivered bid stays in its counterparty's aggregate) |
| `POST /pursuits/{id}/agreements` | worker | an NDA through Legal (37): `503 LEGAL_UNAVAILABLE` while Legal is a stand-in |
| `GET /handoffs` | worker | won-pursuit hand-offs |
| `POST /blocks`, `/blocks/{id}/versions`; `GET /blocks`, `/blocks/{id}` | worker | boilerplate (immutable versions) |
| `POST /blocks/{id}/versions/{v}/approve`, `/retire` | Andre | by content hash; retire = never used again |
| `POST /responses`, `/responses/{id}/versions`; `GET /responses/{id}` | worker | a response or pitch from approved blocks + custom text |
| `POST /responses/{id}/approve` | Andre | exact version and hash, naming exactly the sensitivity flags raised |
| `POST /responses/{id}/submit` | worker | every gate; queued for the submission port |
| `POST /submissions/{id}/reconcile` | Andre | settle a stuck `sending` submission by its `state_sha256`: `delivered` or `not_delivered` (the port is asked first; if it says delivered: `409 PORT_SAYS_DELIVERED`) |
| `GET /submissions`; `POST /submissions/{id}/cancel` | worker | the submission queue (`queued`, `sending` — outcome unknown, reconciled, never resubmittable — `submitted`, `refused`, `cancelled`) |
| `POST /partners`; `GET /partners`, `/partners/{id}` | worker | referral, agency_alliance, white_label |
| `POST /partners/{id}/rate`; `/rate/approve` | worker; Andre | versioned commission rate (0.01..50.00 %) |
| `POST /partners/{id}/payee` | Andre | Finance payee ref + tax reference (`vault:tax:` / `tok:` only) |
| `POST /partners/{id}/agreements` | worker | partner agreement / NDA / white-label contract through Legal (37) |
| `POST /partner-deals`; `GET /partner-deals`, `/partner-deals/{id}` | worker | a deal brought by a partner |
| `POST /partner-deals/{id}/value`; `/lost` | worker | |
| `POST /partner-deals/{id}/deal-approval`; `/won` | Andre | won needs an approved rate and a Legal agreement in force |
| `POST /finance/events` | finance_31 | a client payment, refund or chargeback on a won partner deal |
| `POST /finance/payouts/{id}/paid` | finance_31 | Finance paid a payout it took (a payout whose answer was lost stays `sending` and is reconciled by `payout-request`) |
| `POST /payouts/{id}/reconcile` | Andre | settle a stuck `sending` or `held` payout by its `state_sha256`: `paid` or `not_paid` (the port is asked first; if Finance holds it: `409 PORT_SAYS_DELIVERED`) |
| `GET /payouts` | dashboard, finance_31 | payout requests (no tax reference shown) |
| `POST /contacts`; `GET /contacts/{id}` | worker | email-only contacts of partners and pursuits |
| `POST /contacts/{id}/merge-fields` | dashboard | the only values `{{first_name}}` / `{{company}}` render |
| `POST /templates`, `/templates/{id}/versions`; `GET /templates`, `/templates/{id}` | worker | email templates |
| `POST /templates/{id}/versions/{v}/approve` | Andre | by content hash |
| `POST /outreach/email` | bizdev_agent | queue one email (names the template's approved hash) |
| `GET /outreach/messages`; `POST /outreach/messages/{id}/cancel` | worker | the outreach queue |
| `POST /events/email`; `POST /replies` | provider_events | bounces, complaints; replies (always recorded, never refused for its sender; any reply holds the contact) |
| `POST /unsubscribe` | hub | the one-click link's token |
| `POST /suppressions`; `GET /suppressions` | dashboard, bizdev_agent, provider_events; dashboard, compliance_38 | append-only, both brands |
| `GET /holds`; `POST /holds/{id}/decision` | dashboard; Andre | `resume` or `opt_out` for the hold's whole group, naming its `state_sha256` (a reply that arrives after Andre looked makes it stale: 409) |
| `GET /tasks`; `POST /tasks/{id}/close` | dashboard; Andre | Andre's review queue |
| `POST /jobs/{send-queue,submission-queue,deadline-sweep,handoff-retry,payout-request,integrity}/run` | scheduler | jobs, one at a time (`409 JOB_RUNNING`) |
| `GET /audit/integrity`, `/audit/export` | dashboard, compliance_38 | ledger verdict as returned; export with emails as keyed hashes, text as SHA-256 |

A closed instance (after `close()`) answers the integrity and job routes `503 SERVICE_CLOSED` and writes nothing.
