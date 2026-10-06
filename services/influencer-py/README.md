# influencer-py — Influencer & Partnership Marketing (11)

One service for both brands — ZBM (Z Best Media) and ZBC (Z Best Clips). It finds creators (their own applications,
profiles a person researched, and later a paid influencer database and public-profile data through ports that are not
built), reaches them inside the law (outreach email from a separate domain under CAN-SPAM; platform DMs only as Andre
approved each one), holds every further outreach when anyone replies, runs campaigns (influencer campaigns and
campaign-level co-marketing with brands and agencies), issues sponsored-content briefs that always carry an FTC
disclosure, makes deals (anything over $5,000 in total goes to Andre), sends contracts through Legal (37), checks
content for the disclosure before Andre approves it by hash, keeps material-connection records, and asks Finance (31)
to pay verified creators. Deterministic single-task components; no model calls; nothing generated is ever sent unless
it is an exact approved template or a DM Andre approved by hash. Referral and alliance partner commissions are
department 12's, not here.

Architecture, founder decisions and the unlock list: `docs/adr/0015-influencer-partnership-department-architecture.md`.

**Status:** built and tested; not in force. No send provider, DM provider, discovery source or department client is
wired: email and approved DMs stay queued, imports answer `SOURCE_NOT_WIRED`, contracts `LEGAL_UNAVAILABLE`, payee
verification `FINANCE_UNAVAILABLE`, and nothing is ever paid. Not wired into CI (`ci.yml`, `PY_SERVICES`) or
`docs/test-counts.md` yet (left to the integration lead).

## Run

```bash
cd services/influencer-py && python3 -m pytest -q -p no:cacheprovider     # no network
export INF_SERVICE_TOKEN=<secret>                           # required: refuses to start without it
export INF_CALLER_TOKENS='{"hub": "<>=32 printable chars>", "dashboard": "...", "influencer_agent": "...", "scheduler": "...", "provider_events": "...", "compliance_38": "..."}'
export INF_DATA_DIR=/var/lib/zbm/influencer                 # required unless INF_NON_PRODUCTION=1; owned by the service user, 0700
export INF_PII_HASH_KEY_FILE=/etc/zbm/influencer/pii.key    # openssl rand -hex 32 > pii.key; 0600; never change it
export INF_ANDRE_APPROVAL_TOKEN=<Andre's token>             # unset = nothing can be approved
export INF_OUTREACH_DOMAIN=zb-creators.example INF_ZBM_DOMAIN=zbestmedia.com INF_ZBC_DOMAIN=zbestclips.com
export INF_POSTAL_ADDRESS="<street address, city, state, zip>"
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret>
cd src && python3 -m api                                    # INF_BIND_ADDR (127.0.0.1), INF_PORT (8480)
```

Live run against the real ledger binary: `LEDGER_BIN=<ledger-rust>/target/release/server python3 devtools/live_run.py`
(no check depends on the hour or the day).

Headers: `Authorization: Bearer <service token>` on every route but `/health`; `X-INF-Caller-Token` everywhere
else; Andre's routes also `X-Andre-Approval-Token` (through the `dashboard` caller only; every refusal is recorded on the
ledger as `founder_approval_refused`).

| Setting | Default | Notes |
|---|---|---|
| `INF_SERVICE_TOKEN` | — | required, 32..512 printable characters |
| `INF_CALLER_TOKENS` | `{}` | JSON `{caller: token}`; callers in `src/config.py` `KNOWN_CALLERS`; all distinct, none equal to the service token |
| `INF_NON_PRODUCTION` | 0 | 1 allows the in-memory store and a fixed test PII key (never with `INF_DATA_DIR`). Never in production |
| `INF_DATA_DIR` | — | required in production; a directory owned by the service user, mode 0700; one process and one service instance per directory |
| `INF_PII_HASH_KEY_FILE` | — | required in production and whenever `INF_DATA_DIR` is set: a generated key of at least 32 bytes as hex or base64, file mode 0600, not a symlink. Its fingerprint is bound in the log; a different key refuses start |
| `INF_ANDRE_APPROVAL_TOKEN` | — | Andre's approvals; equal to the service or a caller token = not configured |
| `INF_OUTREACH_DOMAIN` | — | the only domain outreach email is sent from; unset = no email (`OUTREACH_NOT_CONFIGURED`) |
| `INF_ZBM_DOMAIN`, `INF_ZBC_DOMAIN` | — | the two brands' own domains (different registrable domains); both required with an outreach domain, which may share a registrable domain with neither |
| `INF_PRIMARY_DOMAINS` | — | optional comma list of further brand-owned domains the outreach domain must also be separate from |
| `INF_POSTAL_ADDRESS` | — | required with an outreach domain; printed in every email (CAN-SPAM) |
| `INF_OUTREACH_FROM_LOCAL` | `creators` | the From mailbox (must accept replies; `noreply` refused) |
| `INF_DAILY_SEND_CAP` | 50 | outreach emails per outreach domain per UTC day (1..200); over it they wait |
| `INF_QUEUE_MAX_PER_CALLER` | 500 | queued messages one caller may have waiting (1..5000); past it `429 QUEUE_FULL` |
| `INF_AUTO_APPROVE_MAX` | `5000.00` | largest deal total (and influencer and campaign aggregate) approved without Andre; at most 5000.00 |
| `INF_EMAIL_PROVIDER`, `INF_DM_PROVIDER`, `INF_PUBLIC_PROFILE_PROVIDER`, `INF_PAID_DATABASE_PROVIDER`, `INF_FINANCE_URL`, `INF_LEGAL_URL` | unset | not built: setting one (other than `none`/`0`) refuses start |
| `LEDGER_SERVICE_URL`, `LEDGER_SERVICE_TOKEN` | — | unset = every write refused (fail closed) |
| `INF_BIND_ADDR`, `INF_PORT` | 127.0.0.1, 8480 | |
| `INF_REQUEST_HEAD_TIMEOUT_SECONDS`, `INF_KEEP_ALIVE_TIMEOUT_SECONDS`, `INF_LIMIT_CONCURRENCY`, `INF_SWITCH_INTERVAL_SECONDS`, `INF_DRAINS_MAX` | 10, 5, 128, 0.001, 512 | launcher tuning (`src/serve.py`, shared with the other Python services) |

## Routes

All under `/inf/v1` except `/health`. "worker" = `dashboard` or `influencer_agent`; "Andre" = `dashboard` +
`X-Andre-Approval-Token`; "auditor" = `dashboard` or `compliance_38`.

| Route | Who | Purpose |
|---|---|---|
| `GET /health` | open | `ok`, `degraded` or (503) `closed`, nothing else |
| `GET /status` | dashboard | integrity, ports wired, send pace, queue, holds, pending approvals |
| `GET /intelligences` | worker | the eleven single-task components |
| `POST /applications` | hub | the creator application form: `adult_18_plus` must be exactly `true` (false: 422 `MINOR_REFUSED`, nothing kept; missing: 422 `AGE_ATTESTATION_REQUIRED`) |
| `POST /influencers` | dashboard | a prospect a person researched (never attested: no deal until the creator applies) |
| `POST /discovery/import` | influencer_agent, scheduler | public-profile / paid-database discovery through the ports (stand-ins: 503 `SOURCE_NOT_WIRED`) |
| `GET /influencers`, `/influencers/{id}` | worker | records, with suppressed / held / payee state |
| `POST /influencers/{id}/first-name` | dashboard | the only value `{{first_name}}` renders (verified by a person) |
| `POST /influencers/{id}/minor-review` | Andre | a record frozen by a declared minor: `confirm_minor` (blocked for good, suppressed) or `not_a_minor` (released without an attestation) |
| `POST /suppressions`; `GET /suppressions` | hub, dashboard, provider_events, influencer_agent; auditor | one list for both brands, append-only, no removal route |
| `POST /unsubscribe` | hub | the one-click link's token: suppresses every address and handle of the influencer |
| `POST /templates`, `/templates/{id}/versions`; `GET /templates`, `/templates/{id}` | worker | email drafts (never edited in place) |
| `POST /templates/{id}/versions/{v}/approve` | Andre | binds the content hash |
| `POST /outreach/email` | influencer_agent | queue (rules checked now and again at send) |
| `GET /outreach/messages`; `POST /outreach/messages/{id}/cancel` | worker | the queue (email and approved DMs) |
| `POST /dm-drafts`; `GET /dm-drafts` | influencer_agent; worker | the agent drafts a platform DM |
| `POST /dm-drafts/{id}/approve`, `/reject` | Andre | approve binds the hash and queues the DM (no DM provider: it stays queued) |
| `POST /events/email`; `POST /replies` | provider_events | bounces and complaints; replies on any channel (any reply holds) |
| `GET /holds`; `POST /holds/{id}/decision` | worker; Andre | `continue` lifts the hold; `opt_out` suppresses |
| `POST /campaigns`, `/campaigns/{id}/close`; `GET /campaigns`, `/campaigns/{id}` | worker | influencer or `co_marketing` (with its partner); live-content count |
| `POST /briefs`; `GET /briefs`, `/briefs/{id}` | worker (`GET /briefs/{id}` also hub) | a brief with its required disclosure and the fixed FTC section |
| `POST /briefs/{id}/approve` | Andre | binds the hash of the brief as issued |
| `POST /deals`; `GET /deals`, `/deals/{id}`; `POST /deals/{id}/cancel` | worker | approved here at or under the limit in all three aggregates, else `pending_andre` |
| `POST /deals/{id}/approve`, `/reject` | Andre | by the deal's hash; records the material connection |
| `POST /deals/{id}/contract`; `POST /deals/{id}/contract/confirm` | worker; worker, scheduler | through Legal (37) (stand-in: 503 `LEGAL_UNAVAILABLE`) |
| `POST /contents`; `GET /contents`, `/contents/{id}` | influencer_agent, hub; worker | content refused without the brief's disclosure up front |
| `POST /contents/{id}/approve`, `/reject` | Andre | the final content, by hash |
| `POST /contents/{id}/live` | worker | counts live only when approved (hash named again) and contracted |
| `GET /material-connections` | auditor | FTC material-connection records |
| `POST /tax-profiles` | hub, dashboard | a tax REFERENCE (`fin:`, `stripe:`, `vault:`), never a number |
| `POST /payees/{influencer_id}/verify` | dashboard, influencer_agent, scheduler | register at Finance and read the verification (stand-in: 503 `FINANCE_UNAVAILABLE`) |
| `POST /payouts`; `GET /payouts`, `/payouts/{id}` | worker | a payout request handed to Finance (31) for a verified payee |
| `POST /jobs/{send-queue,payout-retry,integrity}/run` | scheduler | jobs (one at a time) |
| `GET /audit/integrity`, `/audit/export` | auditor | integrity with the ledger's own verdict; export with emails and handles dropped (keyed hashes stay), names and texts as SHA-256 |

Every body is refused 422 before it is parsed when it carries a raw tax id (`TAX_ID_REFUSED`: a key naming one, or a
value shaped like one) or a date of birth, age, government id, payment, bank, IP, device or protected-trait key
(`FORBIDDEN_FIELD`). Money is a canonical two-decimal string; a JSON number is refused.
