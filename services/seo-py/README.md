# seo-py — Search & Answer Intelligence (2): SEO / AEO / GEO / LLMO, Wave 1

Department 2 for ZBM: how search engines and AI answer engines can reach, read and cite a site. Wave 1 is the read
core, machine readability, the AI-visibility probe framework and minimal proof, run first on ZBM's own properties and
then sold as a paid audit. Architecture, the eight founder-pending defaults, what Wave 1 does and does not do, the
port list and the limitations: `docs/adr/0017-seo-answer-intelligence-department-architecture.md`.

**Status:** built and tested (count: `docs/test-counts.md`); not in force. The only connected port is the web
fetcher. Rendering, every answer engine (OpenAI, Anthropic, Google, Perplexity), prompt volume, all first-party
sources (Search Console, Bing Webmaster Tools, logs, analytics, CRM), Zero-Day, ORCA Publish and Department 28
clientfix are `NOT_CONNECTED` ports: they answer `NOT_CONNECTED`, never invented data, and setting any of their
switches refuses start. This service never charges anyone and never calls Stripe.

**Wave 2 (decision-free parts):** first-party log ingests (crawler access, verified / spoofed / claimed bots,
keyed-hash IPs only), search-truth drift between audits, scheduled re-audits with drift reports, and the department
manager (queues, scorecards, lifecycle). The bot-verification port is NOT_CONNECTED unless `SEO_BOT_VERIFY_DNS=1`;
IP-range verification is NOT_CONNECTED. See ADR 0017, section "Wave 2".

## Run

```bash
cd services/seo-py && python3 -m pytest -q -p no:cacheprovider     # no internet; counts: docs/test-counts.md
export SEO_SERVICE_TOKEN=<secret>                       # required: refuses to start without it
export SEO_CALLER_TOKENS='{"dashboard": "<>=32 printable chars>", "seo_agent": "...", "scheduler": "...", "hub": "...", "finance_31": "...", "compliance_38": "..."}'
export SEO_TENANT_TOKENS='{"zbm": "<>=32 printable chars>"}'   # optional: a client's read-only view through the hub
export SEO_DATA_DIR=/var/lib/zbm/seo                    # required unless SEO_NON_PRODUCTION=1; owned by the service user, 0700
export SEO_ANDRE_APPROVAL_TOKEN=<Andre's token>         # unset = nothing that needs Andre can happen
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret>
cd src && python3 -m api                                # SEO_BIND_ADDR (127.0.0.1), SEO_PORT (8500)
```

## Live run

```bash
cd services/ledger-rust && cargo build --locked --release --bin server && cd -
LEDGER_BIN=services/ledger-rust/target/release/server python3 services/seo-py/devtools/live_run.py
```

The real ledger-rust binary, this service's production entrypoint and Finance (31)'s (finance-py, for the invoice
verification leg) as separate processes over real HTTP with real tokens and durable data directories (CI job
`live-run (seo-py, …)`; ADR 0017 Wave 3, W3-1 and W3-2). It makes no outbound
connection: the web provider switch is engaged over the API before the first audit, so every fetch stops at the
kill-switch guard before name resolution. Exit 0 only when every check held; the script prints its own N/N.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `SEO_SERVICE_TOKEN` | — (required) | bearer token for every route except `/health` |
| `SEO_CALLER_TOKENS` | `{}` | `{caller: token}` for `dashboard`, `seo_agent`, `scheduler`, `hub`, `finance_31`, `compliance_38` (header `X-SEO-Caller-Token`) |
| `SEO_TENANT_TOKENS` | `{}` | `{tenant_id: token}` (header `X-SEO-Tenant-Token`, only with the `hub` caller): read-only, own tenant only |
| `SEO_ANDRE_APPROVAL_TOKEN` | unset | Andre's approval (`X-Andre-Approval-Token`, only through `dashboard`); equal to any other token = not configured |
| `SEO_NON_PRODUCTION` | `0` | `1` allows an in-memory run without `SEO_DATA_DIR` (tests only) |
| `SEO_DATA_DIR` | — | the record log and its lock (flock: one process per directory) |
| `SEO_KILL_GLOBAL` | `0` | `1` starts with the global kill switch engaged; it cannot be released at runtime |
| `SEO_KILLED_CAPABILITIES` | empty | comma list of `fetch`, `render`, `ai_probe`, `audit`, `entity_write`, `prompt_sets` killed at start (sticky) |
| `SEO_KILLED_PROVIDERS` | empty | comma list of `web`, `openai`, `anthropic`, `google`, `perplexity` killed at start (sticky) |
| `SEO_FETCH_TIMEOUT_SECONDS` | `10` | per-phase timeout (1..60); the whole response must arrive within twice this |
| `SEO_FETCH_MAX_BYTES` | 2 MiB | decoded body cap (64 KiB..10 MiB) |
| `SEO_FETCH_MAX_REDIRECTS` | `5` | redirect cap (0..10), every hop re-checked |
| `SEO_AUDIT_MAX_PAGES` | `10` | paths per audit (1..25) |
| `SEO_PROBE_SAMPLES` | `5` | answers sampled per prompt per engine (3..50) |
| `SEO_BOT_INFO_URL` | unset | an https page about the crawler, added to its User-Agent |
| `SEO_LOG_HASH_KEY_FILE` | — (required with `SEO_DATA_DIR`) | key for the keyed hashes behind the distinct-client-IP sketch of uploaded logs (`openssl rand -hex 32`, mode 0600); no IP and no IP hash is ever stored |
| `SEO_LOG_MAX_BYTES` | 256 MiB | bytes per log ingest (1 MiB..2 GiB) |
| `SEO_LOG_RETENTION_DAYS` | `90` | after this an ingest is served as totals only (`expired`) |
| `SEO_LOG_MAX_OPEN_INGESTS` | `2` | open log ingests per tenant (1..20) |
| `SEO_LOG_MAX_INGESTS` | `30` | log ingests per tenant within a retention period (1..1000) |
| `SEO_LOG_TENANT_BYTES` | 1 GiB | log bytes per tenant within a retention period |
| `SEO_BOT_VERIFY_DNS` | `0` | `1` connects the bot-verification port (reverse DNS + forward-confirm with the system resolver); `0` = every bot hit stays "claimed" |
| `SEO_BOT_VERIFY_MAX` | `200` | DNS verifications per ingest (1..5000); past it hits stay "claimed" |
| `SEO_SCHEDULE_BUDGET_RUNS` | `8` | scheduled audits per tenant per period (1..100) |
| `SEO_SCHEDULE_PERIOD_DAYS` | `30` | the budget period (1..365) |
| `SEO_INVOICE_VERIFICATION` | `finance` | `finance`: a paying client's audit or schedule runs only when Finance (31) confirms its invoice is paid, Z Best Media's, this tenant's Finance client's, not refunded and not used before (ADR 0017 W3-2); `trust`: the Wave 1/2 behaviour (the id is not checked), shown in `/status` |
| `SEO_FINANCE_URL`, `SEO_FINANCE_TOKEN`, `SEO_FINANCE_CALLER_TOKEN` | unset | Finance (31)'s base URL, its service token and this service's `seo_02` caller token there (all three or none). Unset with `finance`: every paid run is refused `FINANCE_NOT_CONFIGURED` unless Andre overrides |
| `SEO_FINANCE_TIMEOUT_SECONDS` | `5` | per-attempt timeout of the invoice lookup (1..30); at most three attempts within one overall deadline |
| `SEO_BIND_ADDR` / `SEO_PORT` | `127.0.0.1` / `8500` | where the API listens |
| `SEO_REQUEST_HEAD_TIMEOUT_SECONDS`, `SEO_KEEP_ALIVE_TIMEOUT_SECONDS`, `SEO_LIMIT_CONCURRENCY`, `SEO_SWITCH_INTERVAL_SECONDS`, `SEO_DRAINS_MAX` | 10 / 5 / 128 / 0.001 / 512 | the hardened launcher (`src/serve.py`, shared with every Python service) |
| `LEDGER_SERVICE_URL`, `LEDGER_SERVICE_TOKEN` | unset | the evidence ledger; unset = every write refused (fail closed) |

The fetcher only ever connects to ports 80 and 443, accepts at most one content coding (gzip or deflate), decoded
within the byte cap, and enforces one hard deadline (twice `SEO_FETCH_TIMEOUT_SECONDS`) over the whole fetch.

**Not built — setting any of these refuses start:** `SEO_RENDERER`, `SEO_OPENAI_API_KEY_FILE`,
`SEO_ANTHROPIC_API_KEY_FILE`, `SEO_GOOGLE_API_KEY_FILE`, `SEO_PERPLEXITY_API_KEY_FILE`,
`SEO_SEARCH_CONSOLE_CREDENTIALS_FILE`, `SEO_BING_WEBMASTER_KEY_FILE`, `SEO_PROMPT_VOLUME_PROVIDER`, `SEO_ZERO_DAY_URL`,
`SEO_ORCA_PUBLISH_URL`, `SEO_CLIENTFIX_URL`.

## Routes (`/seo/v1`)

| Route | Who | What |
|---|---|---|
| `GET /health` | anyone | `ok`, `degraded` or `closed` |
| `GET /status` | dashboard | integrity, kill switches, port states, counts |
| `GET /pricing` | dashboard, seo_agent, hub, finance_31 | the locked tier table (config only) |
| `GET /tenants`, `POST /tenants` | dashboard / Andre | list; create (`own` or `client`) |
| `GET /tenants/{tid}` | scoped | one tenant (hub: its own only; anything else 404) |
| `POST /tenants/{tid}/domains` | Andre | the domains this tenant may be audited on |
| `POST /tenants/{tid}/finance-client` | Andre | bind a client tenant to its Finance (31) client id, against which paid runs' invoices are checked (W3-2) |
| `GET`, `POST /kill-switches` | dashboard, compliance_38 | engage any switch; releasing needs Andre |
| `GET /tenants/{tid}/entity` | scoped | the canonical entity record with source, provenance, authority, freshness, history |
| `POST /entities/{eid}/fields` | Andre | set one field (version-checked; the old value goes to history) |
| `POST /tenants/{tid}/prompt-sets`, `GET .../{psid}` | dashboard, seo_agent | versioned prompt sets |
| `POST /tenants/{tid}/audits` | dashboard, seo_agent (+ Andre for clients) | run an audit; `invoice_id` (Finance 31) required for a client tenant and verified with Finance; `invoice_override` (Andre, only when Finance cannot answer) |
| `GET /tenants/{tid}/audits[/{aid}]` | scoped (finance_31 gets status only) | audits and reports |
| `POST /tenants/{tid}/log-ingests`, `.../{iid}/chunks`, `.../{iid}/finish`; `GET` both | dashboard, seo_agent, hub (own tenant) | first-party log upload in line-aligned base64 chunks; Selene's `log_access` report (Wave 2) |
| `GET /tenants/{tid}/audits/{aid}/drift?against={aid}` | scoped | search-truth drift between two completed audits (Wave 2) |
| `POST /tenants/{tid}/schedules`, `.../{sid}/status`; `GET` both | dashboard, seo_agent (+ Andre for clients) | scheduled re-audits; drift report per consecutive pair (Wave 2) |
| `GET /department`, `POST /agents/{agent}/lifecycle` | dashboard (+ Andre for restricted / retired), compliance_38 reads | work queues, scorecards, lifecycle (Wave 2) |
| `POST /jobs/{integrity,interrupted-audits,schedule-tick}/run` | scheduler | integrity check; record interrupted runs; run due scheduled slots |
| `GET /audit/{integrity,export,evidence}` | dashboard, compliance_38 | the R6-M1 evidence view: unanchored evidence = attempted, not done |

## Layout

`src/primitives/` the seven shared primitives (fetch, render, parse, diff / access-diff / link-diff / change-detect)
and robots.txt; `src/agents/` Selene, Delia, Roman, the probe framework (Naomi, Callum), the entity check, Osei-lite,
and the versioned bot family list; `src/svc_*.py` tenants and kill switches, the entity record, audits; the record-first
plumbing in `src/service.py` and `src/store.py` is bizdev-py's.

Tests talk only to an in-process HTTP fixture server on 127.0.0.1 (`tests/fixture_server.py`); every other connect
attempt raises.
