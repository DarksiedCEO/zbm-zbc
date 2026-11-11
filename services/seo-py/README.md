# seo-py — Search & Answer Intelligence (2): SEO / AEO / GEO / LLMO, Wave 1

Department 2 for ZBM: how search engines and AI answer engines can reach, read and cite a site. Wave 1 is the read
core, machine readability, the AI-visibility probe framework and minimal proof, run first on ZBM's own properties and
then sold as a paid audit. Architecture, the eight founder-pending defaults, what Wave 1 does and does not do, the
port list and the limitations: `docs/adr/0017-seo-answer-intelligence-department-architecture.md`.

**Status:** built and tested (count: `docs/test-counts.md`); not in force. The connected ports are the web
fetcher and, once Andre installs a key and budgets (ADR 0017 Wave 4), the Perplexity answer engine, which asks
Perplexity's Agent API (`POST /v1/agent`, model `perplexity/sonar`, effort `fast`; the legacy Sonar request is kept
only as a rollback, `SEO_PERPLEXITY_PROTOCOL=sonar`; ADR 0017 W4-8). Before any budget is raised, one hand-run
request checks the real response shape: `python3 devtools/smoke_one_call.py --one-real-call` (W4-6 step 5a).
Methodology: cited = returned by search (a URL counts as a citation when Perplexity's search returned it for the
answer, in search order, and only `http`/`https` URLs count). The OpenAI, Anthropic and Google adapters are built
(ADR 0046) and OFF by default (`SEO_<ENGINE>_ENABLED=1`, a key and every budget above 0). Rendering, prompt volume, all first-party
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
kill-switch guard before name resolution. Search Console: NOT_CONNECTED without a key (zero requests, nothing on the
ledger as a read), bad key files refuse start without quoting the key, and with a run-generated key plus
`SEO_KILLED_PROVIDERS=gsc` the section is `GSC_KILLED` before any request; an audit hook in every service process
records any non-loopback lookup or connect and the run fails unless that record is empty. Exit 0 only when every check
held; the script prints its own N/N.

## War room

`python3 devtools/warroom/run.py --department seo-py --seed 1` (from the repo root; CI job `warroom`, ADR 0018 and
ADR 0017 W3-3): the service's real fetcher against a fake internet of hostile sites, logs and callers, inside a sealed
worker. The `gsc-*` scenarios run the real Search Console connector against a hostile fake Google. Exit 1 on any new
MUST failure or ERROR.

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
| `SEO_KILLED_CAPABILITIES` | empty | comma list of `fetch`, `render`, `ai_probe`, `audit`, `entity_write`, `prompt_sets`, `logs`, `schedules`, `reports` killed at start (sticky) |
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
| `SEO_COMPLIANCE_URL`, `SEO_COMPLIANCE_TOKEN`, `SEO_COMPLIANCE_CALLER_TOKEN` | unset | ADR 0024: Compliance (38)'s base URL, its service token and this service's `seo_02` caller token there (all three or none). A client tenant's paid audit, every scheduled slot of it and every client report need Compliance's CURRENT activation of the tenant's client (`finance_client_id`, which must be bound first); no override. Unset: every client run is refused `COMPLIANCE_NOT_CONFIGURED`; own-tenant runs never ask |
| `SEO_FINANCE_TIMEOUT_SECONDS` | `5` | per-attempt timeout of the invoice lookup (1..30); at most three attempts within one overall deadline |
| `SEO_GSC_SERVICE_ACCOUNT_FILE` | unset | Google Search Console: absolute path to the Google service-account JSON key (regular file owned by the service user, mode 0600/0400, no symlink, ≤ 16 KiB). Unset = `NOT_CONNECTED` (no outbound call ever); a key that cannot be parsed refuses start. ADR 0017, "Search Console" |
| `SEO_GSC_WINDOW_DAYS` | `28` | days of final Search Analytics data per read (1..90), ending three Pacific-Time days ago |
| `SEO_GSC_PAGE_ROWS`, `SEO_GSC_MAX_PAGES`, `SEO_GSC_MAX_ROWS` | `5000` / `10` / `50000` | pagination caps per breakdown (rows per page 100..25,000; pages 1..40; rows 1,000..250,000); a capped breakdown says `truncated` |
| `SEO_GSC_MAX_REQUESTS` | `30` | HTTP requests per read, retries included (6..200); past it `GSC_BUDGET_EXHAUSTED` |
| `SEO_GSC_TIMEOUT_SECONDS`, `SEO_GSC_DEADLINE_SECONDS` | `10` / `60` | per-attempt timeout (1..60) and the hard deadline over a whole read (5..300): DNS, connect, TLS and body |
| `SEO_GSC_READS_PER_DAY` | `4` | Search Console reads per tenant in any 24 hours, scheduled re-audits included (1..48) |
| `SEO_GSC_RETENTION_DAYS` | `90` | after this a report's search-performance breakdowns are served as window totals only (1..480) |
| `SEO_PERPLEXITY_API_KEY_FILE` | unset | Wave 4: the Perplexity API key in a file (regular file owned by the service user, mode 0600/0400, no symlink). Unset (and no `SEO_PERPLEXITY_API_KEY`): the Perplexity engine is `NOT_CONNECTED` and the service starts as before |
| `SEO_PERPLEXITY_API_KEY` | unset | the same key as a plain variable, for platforms without secret files (Railway service variables). Setting both refuses start. The key is never logged, shown, recorded or reported (`/status` shows only `key_source`: `file` or `env`) |
| `SEO_PERPLEXITY_PROTOCOL` | `agent` | the request kind: `agent` (Perplexity's Agent API, `POST /v1/agent`, effort `fast`; the default since the Sonar Chat Completions end of support, ADR 0017 W4-8) or `sonar` (the legacy `POST /v1/sonar` request, a ROLLBACK only: Perplexity serves it by reformulating it as an Agent API request); anything else leaves the engine `NOT_CONNECTED` (reason `PROTOCOL_INVALID`), the service still starts |
| `SEO_PERPLEXITY_MODEL` | `perplexity/sonar` (agent), `sonar` (sonar) | closed allowlist per protocol: `perplexity/sonar` on the Agent API (`sonar` is accepted there as the same model); `sonar` or `sonar-pro` on the rollback protocol (`perplexity/sonar` is accepted there as `sonar`). Anything else leaves the service running with the Perplexity engine `NOT_CONNECTED` (`/status` reason `MODEL_NOT_SUPPORTED`). An answer reporting another model is `ENGINE_PROTOCOL_ERROR` (`MODEL_NOT_ALLOWED`) |
| `SEO_PERPLEXITY_TIMEOUT_SECONDS` | `30` | the hard overall deadline of one question (5..120): DNS, connect, TLS, every read and write, the body, and every retry |
| `SEO_PERPLEXITY_MAX_ATTEMPTS` | `3` | requests per question (1..3); retries only on 429, 5xx and transport errors, `Retry-After` honoured within the deadline |
| `SEO_PERPLEXITY_DAILY_CALLS`, `SEO_PERPLEXITY_DAILY_SPEND_USD` | `0`, `0` | the global daily budget (UTC day) in requests and US dollars. **0 = OFF: no request is made** (fail closed) |
| `SEO_PERPLEXITY_TENANT_DAILY_CALLS`, `SEO_PERPLEXITY_TENANT_DAILY_SPEND_USD` | `0`, `0` | the same per tenant. All four must be above 0 before Perplexity is ever asked anything |
| `SEO_PERPLEXITY_RESERVE_USD_PER_CALL` | `0.05` | what each request reserves against the spend budgets before it is sent (0.001..5), and what it costs when Perplexity reports no cost |
| `SEO_PERPLEXITY_RUN_MAX_CALLS`, `SEO_PERPLEXITY_RUN_MAX_SPEND_USD` | unset | ADR 0046 review round 1 L4: the per-run caps for Perplexity, **opt-in**. Neither set = no per-run cap (the behaviour before); both set = they bind as for the other engines (`RUN_CALL_CAP`, `RUN_SPEND_CAP`; 0 = OFF); one without the other refuses start |
| `SEO_OPENAI_ENABLED`, `SEO_ANTHROPIC_ENABLED`, `SEO_GOOGLE_ENABLED` | `0` | ADR 0046: `1` turns that answer engine on. **Every engine is OFF by default.** `1` without a key refuses start; a key without `1` refuses start (which one is meant?) |
| `SEO_OPENAI_API_KEY_FILE` / `SEO_OPENAI_API_KEY`, `SEO_ANTHROPIC_API_KEY_FILE` / `SEO_ANTHROPIC_API_KEY`, `SEO_GOOGLE_API_KEY_FILE` / `SEO_GOOGLE_API_KEY` | unset | the engine's key: the file form (the *_FILE rules: regular file owned by the service user, mode 0600/0400, no symlink) or the plain variable (Railway); both set refuses start; every key must differ from every token and every other key. Never logged, shown, recorded or reported (`/status` shows `key_source` only) |
| `SEO_OPENAI_MODEL`, `SEO_ANTHROPIC_MODEL`, `SEO_GOOGLE_MODEL` | `gpt-6-astra` / `claude-sonnet-5-5` / `gemini-3.8-flash` | closed allowlist per engine (`openai_engine.MODELS`, `anthropic_engine.MODELS`, `google_engine.MODELS`); anything else leaves only that engine `NOT_CONNECTED` (`MODEL_NOT_SUPPORTED`), never echoed |
| `SEO_OPENAI_TIMEOUT_SECONDS`, `SEO_ANTHROPIC_TIMEOUT_SECONDS`, `SEO_GOOGLE_TIMEOUT_SECONDS` | `60` | the hard overall deadline of one question (5..120), as for Perplexity |
| `SEO_OPENAI_MAX_ATTEMPTS`, `SEO_ANTHROPIC_MAX_ATTEMPTS`, `SEO_GOOGLE_MAX_ATTEMPTS` | `2` | requests per question (1..3); retries only on 429, 5xx and transport errors |
| `SEO_OPENAI_DAILY_CALLS`, `SEO_OPENAI_DAILY_SPEND_USD`, `SEO_OPENAI_TENANT_DAILY_CALLS`, `SEO_OPENAI_TENANT_DAILY_SPEND_USD`, `SEO_ANTHROPIC_DAILY_CALLS`, `SEO_ANTHROPIC_DAILY_SPEND_USD`, `SEO_ANTHROPIC_TENANT_DAILY_CALLS`, `SEO_ANTHROPIC_TENANT_DAILY_SPEND_USD`, `SEO_GOOGLE_DAILY_CALLS`, `SEO_GOOGLE_DAILY_SPEND_USD`, `SEO_GOOGLE_TENANT_DAILY_CALLS`, `SEO_GOOGLE_TENANT_DAILY_SPEND_USD` | `0` | the global and per-tenant daily budgets (UTC day) in requests and dollars. **0 = OFF: nothing is asked** |
| `SEO_OPENAI_RUN_MAX_CALLS`, `SEO_OPENAI_RUN_MAX_SPEND_USD`, `SEO_ANTHROPIC_RUN_MAX_CALLS`, `SEO_ANTHROPIC_RUN_MAX_SPEND_USD`, `SEO_GOOGLE_RUN_MAX_CALLS`, `SEO_GOOGLE_RUN_MAX_SPEND_USD` | `0` | the per-run caps: requests and dollars ONE audit or ONE citation run may reserve on that engine (`RUN_CALL_CAP`, `RUN_SPEND_CAP`). **0 = OFF.** All six budgets must be above 0 before the engine is asked anything |
| `SEO_OPENAI_RESERVE_USD_PER_CALL`, `SEO_ANTHROPIC_RESERVE_USD_PER_CALL`, `SEO_GOOGLE_RESERVE_USD_PER_CALL` | `0.10` / `0.10` / `0.05` | what each request reserves before it is sent and what it is charged. **A reserve ESTIMATE, not a measured cost:** none of the three APIs returns a dollar cost, so every request is booked at its reserve and the dollar budgets count estimates. The 4x cost guard can only act on a reported cost, so it cannot detect an overrun on these engines. Set a hard spend limit at each provider (its console's budget / usage limit) as the real ceiling |
| `SEO_ANTHROPIC_MAX_SEARCHES` | `3` | the web search tool's `max_uses` per question (1..10) |
| `SEO_CALLUM_RESOLVE_URLS` | `0` | ADR 0046: `1` lets Callum follow each cited URL's redirects and read its canonical through the SSRF-safe fetcher (robots.txt honoured, kill switches `capability:fetch` / `provider:web`); `0` = a redirector URL (Gemini grounding) is `INCONCLUSIVE` and every other URL is classified by its own host. A URL with a credential-shaped part is never fetched (a personal-data-shaped part alone does not block the fetch, ADR 0051); the log keeps a cited URL's host only (the full URL is in the client citation vault); a client URL whose redirect chain stops, is refused or lands on an error page is never a STRENGTH_URL (ADR 0046 B9) |
| `SEO_CALLUM_RESOLVE_MAX` | `40` | distinct cited URLs fetched per citation run (1..200) |
| `SEO_VAULT_KEY_FILE` (preferred) or `SEO_VAULT_KEY`, and `SEO_VAULT_KEY_ID` | unset | ADR 0051 (number to be confirmed at merge): the client citation vault key, base64url of exactly 32 random bytes (the file under the *_FILE rules: absolute path, regular file, owned by this user, mode 0600 / 0400, no symlink; the file and the variable both set refuses start), and its short id (a-z, 0-9, `-`, 1..32). Set together or not at all; anything malformed refuses. A citation run's WHOLE result (methodology, metrics, the MNC list, every row and each row's full evidence: cited URL, excerpt, title, hint) is encrypted as one blob into `SEO_DATA_DIR/citation_vault.jsonl` under the run's tenant; `seo_log.jsonl`, the ledger and events keep only the blob ref, one keyed hash of the result (`result_hmac`, HMAC-SHA256 under a sub-key of this key), the outcome code and counts (ADR 0051 decision 36, sink job C); the owner's view masks only high-precision findings (ADR 0051 job 3) and shows `vault_unavailable` when the blob cannot be read. Unset with `SEO_DATA_DIR`: every citation run fails closed (RUN_FAILED, VAULT_UNAVAILABLE) before any engine is asked, nothing written (never a plaintext fallback). Sink job D (ADR 0051 decisions 41-51): entity records, prompt sets and log ingests (robots.txt, sitemap sample, chunk deltas, the report) are vault blobs as well, a request body's free text is hashed with this key for idempotency, and the service log, the ledger, Python logging and error bodies carry machine values only; without the key those requests are refused `VAULT_UNAVAILABLE` (the service still starts: its own entity record is a named constant). In-memory mode (no data directory, non-production) without a key: a random key held in memory only. Never logged, shown or recorded. A real key is set on the live service only at merge time with Andre's approval |
| `SEO_VAULT_KEY_PREVIOUS_FILE` (preferred) or `SEO_VAULT_KEY_PREVIOUS`, and `SEO_VAULT_KEY_PREVIOUS_ID` | unset | the key being retired during a rotation (ADR 0051): decrypt only, every new write uses the current key; must differ from the current key in bytes and id; the file and the variable both set refuses start |
| `SEO_REPORT_BRAND_NAME`, `SEO_REPORT_BRAND_ACCENT` | `Z Best Media` / `#1f4e79` | white label of the client report: the preparer's name (1-60 letters, digits, spaces, `. , & ' ( ) -`) and accent colour (`#RRGGBB`); anything else refuses start |
| `SEO_BIND_ADDR` / `SEO_PORT` | `127.0.0.1` / `8500` | where the API listens |
| `SEO_REQUEST_HEAD_TIMEOUT_SECONDS`, `SEO_KEEP_ALIVE_TIMEOUT_SECONDS`, `SEO_LIMIT_CONCURRENCY`, `SEO_SWITCH_INTERVAL_SECONDS`, `SEO_DRAINS_MAX` | 10 / 5 / 128 / 0.001 / 512 | the hardened launcher (`src/serve.py`, shared with every Python service) |
| `LEDGER_SERVICE_URL`, `LEDGER_SERVICE_TOKEN` | unset | the evidence ledger; unset = every write refused (fail closed) |

**Search Console** (`src/gsc_client.py`, `src/svc_gsc.py`, `src/agents/search_performance.py`): read-only scope,
two allowlisted Google hosts, a hard deadline, bounded retries and pagination, aggregates only (no query text, no
URL), a `search_performance` section in every audit report (`NOT_CONNECTED` until a key is configured and Andre binds
the tenant's property), kill switch `provider:gsc`. Setup steps for Andre: ADR 0017, "Search Console".

**Client report** (`src/client_report.py`, `src/svc_reports.py`, `devtools/render_report.py`): a completed audit as
Markdown or one self-contained HTML page (inline CSS, no script, printable to PDF from a browser), rendered
deterministically with no model call; no overall score; NOT CONNECTED / NOT COVERED areas stated plainly; crawled text
escaped, URLs as templates, no prompt, answer or query text. `python3 devtools/render_report.py fetch ...` asks the
service (recorded on the ledger); `... preview AUDIT.json ...` renders offline with a PREVIEW banner. ADR 0017,
"Client report".

The fetcher only ever connects to ports 80 and 443, accepts at most one content coding (gzip or deflate), decoded
within the byte cap, and enforces one hard deadline (twice `SEO_FETCH_TIMEOUT_SECONDS`) over the whole fetch.

**Answer engines** (ADR 0046): one provider-agnostic interface, `src/answer_adapter.py` (shared plumbing, not an
agent): Perplexity (`src/perplexity.py`), OpenAI (`src/openai_engine.py`, Responses API + `web_search`), Anthropic
(`src/anthropic_engine.py`, Messages API + `web_search_20250305`) and Google (`src/google_engine.py`, Gemini
`generateContent` + Google Search grounding). Each has one allow-listed host, a hard deadline, bounded retries, a
bounded strict parse, the budget meter and the kill switches `provider:<engine>` and `capability:ai_probe`.
Switching provider is configuration only. None of the three new adapters has been called against its real API.
**Spend is estimated, not tracked:** OpenAI, Anthropic and Google return no dollar cost, so each request is booked at
its reserve ($0.10 / $0.10 / $0.05 by default) and the dollar budgets and per-run caps bound reserve estimates; the
cost guard cannot see an overrun on these engines. Set a hard spend limit at each provider as the real ceiling.
Google answers are attributed only to the exact allow-listed model: a `modelVersion` that differs, or is missing,
is refused (`MODEL_NOT_ALLOWED` / `MODEL_UNVERIFIED`, fail closed). Gemini's grounding-URI format is unverified
until a first live call; Callum treats every URL on `vertexaisearch.cloud.google.com` as an unresolved redirect.

**Brand matching** (`src/agents/brand_mention.py`, prompt set `brand_term_rules`): whole words, case-insensitive
unless `case_sensitive`; possessives match (`Acme's`), plurals do not (`Acmes`); optional `context_terms` (one must
appear in the answer) and `negative_terms` (a match within 40 characters of a whole-word occurrence of one is
discarded); whitespace (NBSP, runs of spaces) is normalised first. Mentions are confirmed by default, case-insensitive, as at a4af277 (`try acme`, `the iphone 15`;
a URL in the answer text such as `github.com/acme` is a mention, never a citation). Only a term on the bundled
common-word list (`src/agents/common_words.txt`, self-authored) or declared `common_word: true`, without a rule,
needs confirmation: a capitalised match that does not start a sentence (a line, list item, task box, heading, blockquote,
table cell, or text after `:` or an opening quote counts as a start; linear scanners, no regex backtracking), the client's domain named, the client cited,
or another brand term that is not itself a common word. Unconfirmed hits are `LOW_CONFIDENCE` in citation runs
and `MENTION_UNCONFIRMED` in the audit ("Possible mention, not confirmed", mention rate UNKNOWN, no page
recommendation). A prompt set with such a term and no rule returns `warnings` on create and read, and the methodology
names it.

**Not built — setting any of these refuses start:** `SEO_RENDERER`,
`SEO_SEARCH_CONSOLE_CREDENTIALS_FILE` (renamed: use `SEO_GSC_SERVICE_ACCOUNT_FILE`), `SEO_BING_WEBMASTER_KEY_FILE`, `SEO_PROMPT_VOLUME_PROVIDER`, `SEO_ZERO_DAY_URL`,
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
| `POST /tenants/{tid}/gsc-property` | Andre | bind the tenant to its own Search Console property (`sc-domain:<domain>` or `https://<domain>/`, one of its registered domains; `null` unbinds) |
| `POST /tenants/{tid}/finance-client` | Andre | bind a client tenant to its Finance (31) client id, against which paid runs' invoices are checked (W3-2) |
| `GET`, `POST /kill-switches` | dashboard, compliance_38 | engage any switch; releasing needs Andre |
| `GET /tenants/{tid}/entity` | scoped | the canonical entity record with source, provenance, authority, freshness, history |
| `POST /tenants/{tid}/entity` | Andre | create a client tenant canonical record from1–5 supplied field records; exact replay returns its current view |
| `POST /entities/{eid}/fields` | Andre | set one field (version-checked; the old value goes to history) |
| `POST /tenants/{tid}/prompt-sets`, `GET .../{psid}` | dashboard, seo_agent | versioned prompt sets; optional `brand_term_rules` (`[{term, case_sensitive, context_terms, negative_terms, common_word}]`, one per brand term) |
| `POST /tenants/{tid}/audits` | dashboard, seo_agent (+ Andre for clients) | run an audit; `invoice_id` (Finance 31) required for a client tenant and verified with Finance; `invoice_override` (Andre, only when Finance cannot answer) |
| `GET /tenants/{tid}/audits[/{aid}]` | scoped (finance_31 gets status only) | audits and reports |
| `POST /tenants/{tid}/audits/{aid}/client-report` | Andre | the client-facing report of a completed audit, `{"request_id", "format": "markdown" \| "html"}`; recorded first (`client_report_rendered` with the document's SHA-256), answered as JSON with the document in `content` (ADR 0017, "Client report") |
| `GET /tenants/{tid}/audits/{aid}/client-reports` | dashboard, compliance_38 | the recorded renders of one audit (ids and hashes, never the document) |
| `POST /tenants/{tid}/citation-runs` | dashboard, seo_agent (+ Andre, a bound Finance client and Compliance's ALLOW for clients) | ADR 0046: Callum's citation-source run (`{"request_id", "domain", "prompt_set_id", "engines"?}`): every cited URL classified STRENGTH_URL / CANONICAL_TO_CLIENT / OPPORTUNITY_URL / LOOKALIKE_URL / INCONCLUSIVE, Mentioned-Not-Cited per answer (LOW_CONFIDENCE apart); recorded first |
| `GET /tenants/{tid}/citation-runs[/{cid}]`, `GET .../{cid}/mnc-priority` | dashboard, seo_agent, hub (own tenant), compliance_38 | the runs; one run's rows (dashboard drill-down) and metrics; the `callum.mnc_priority/2` list for Roman and Adaeze |
| `POST /tenants/{tid}/log-ingests`, `.../{iid}/chunks`, `.../{iid}/finish`; `GET` both | dashboard, seo_agent, hub (own tenant) | first-party log upload in line-aligned base64 chunks; Selene's `log_access` report (Wave 2) |
| `GET /tenants/{tid}/audits/{aid}/drift?against={aid}` | scoped | search-truth drift between two completed audits (Wave 2) |
| `POST /tenants/{tid}/schedules`, `.../{sid}/status`; `GET` both | dashboard, seo_agent (+ Andre for clients) | scheduled re-audits; drift report per consecutive pair (Wave 2) |
| `GET /department`, `POST /agents/{agent}/lifecycle` | dashboard (+ Andre for restricted / retired), compliance_38 reads | work queues, scorecards, lifecycle (Wave 2) |
| `POST /jobs/{integrity,interrupted-audits,schedule-tick}/run` | scheduler | integrity check; record interrupted runs; run due scheduled slots |
| `GET /audit/{integrity,export,evidence}` | dashboard, compliance_38 | the R6-M1 evidence view: unanchored evidence = attempted, not done |

## Layout

`src/primitives/` the seven shared primitives (fetch, render, parse, diff / access-diff / link-diff / change-detect)
and robots.txt; `src/agents/` Selene, Delia, Roman, the probe framework (Naomi, Callum), Callum's citation
classifier (`agents/callum.py`, ADR 0046), the entity check, Osei-lite,
and the versioned bot family list; `src/svc_*.py` tenants and kill switches, the entity record, audits; the record-first
plumbing in `src/service.py` and `src/store.py` is bizdev-py's.

Tests talk only to an in-process HTTP fixture server on 127.0.0.1 (`tests/fixture_server.py`); every other connect
attempt raises.

## Homepage phone observation

New entity checks expose `homepage_phone_observation` (`homepage-phone-sample/1`):
`found`, `not_found_in_sample`, or `unknown`. It covers only explicitly selected `/`
with exact configured/requested/final URL agreement and valid raw-HTML parser evidence.
Other pages and redirects to another URL cannot satisfy it. Its limit is the first
20,000 extracted text characters; nonmatch is not whole-page absence or a claim about
visual visibility. The historical all-page `phone_in_visible_text` fact is not homepage proof.

Client reports use renderer/3 whenever the new field is present, including explicit
unknown presentation for malformed/future schemas. Field absence preserves renderer/2
bytes for historical reports; no stored audit or document hash is rewritten. A changed
retry still returns `REPORT_CHANGED`. Review unknown reasons before making a homepage
recommendation; do not treat it as an instruction to expand crawl scope. See
[ADR 0042](../../docs/adr/0042-homepage-phone-sample-evidence.md).


### Client canonical facts (ADR0045)

Create an existing client tenant's entity through `POST /seo/v1/tenants/{tid}/entity` with the existing dashboard caller and Andre approval. Supply a request UUID and a `fields` list, for example `{"field":"telephone","value":"202-555-0100","source":"client_statement","provenance":"Explicit synthetic example","authority":"first_party"}`. The example is synthetic, not client data. Each list entry must name one of name, street_address, locality, region or telephone;1–5distinct entries are permitted and omitted fields stay absent. The existing personal-data key scanner is unchanged; use the strict field-record shape, not arbitrary extra keys.

Creation stores supplied provenance without independently verifying it. It requires no payment or Compliance fact and grants no audit entitlement. Paid audits retain their existing Finance/Compliance gates. Use the returned entity ID and existing expected-version field update route for later corrections. Reuse the exact creation body/request ID only for replay; it returns the current view after later updates or restart. A changed/reordered body under that ID conflicts, and a new creation request cannot replace an existing entity. No facts, provider configuration, report delivery or human review are inferred.
