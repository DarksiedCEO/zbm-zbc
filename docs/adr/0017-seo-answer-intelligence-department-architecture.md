# ADR 0017 — Search & Answer Intelligence (2): SEO / AEO / GEO / LLMO, Wave 1

Status: accepted for build, Oct 9 2026, from the founder-approved merged spec of Oct 6 2026
(`claude/seo-department-merged-spec-2026-10-06.md` in the ZBM/ZBC Backend project). Built on branch
`seo-dept-02-wave1`. **Not in force.** Wave 1 only; eight open flags carry the defaults below, each **pending a
founder decision**.

## Context

The spec defines sixteen single-task agents (Marcus, Delia, Priya, Theo, Naomi, Zara, Callum, Idris, Selene, Roman,
Vesper, Adaeze, Julian, Sable, Osei, Farrah), a canonical loop (observe → verify → diagnose → prioritize → plan →
authorize → execute → recrawl/requery → measure → calibrate → learn), ten capability areas P1–P10, seven shared
primitives, a propagation spine anchored on one canonical entity record, reliability rules for AI answers, an outcome
envelope with evidence classes and decision states, a security model with kill switches, and the build waves. This ADR
records what Wave 1 builds: the read core, machine readability, AI-visibility probes and minimal proof, proven on ZBM's
own properties, then sold as an audit.

## Decisions

1. **One service, `services/seo-py`** (Python; FastAPI, pydantic strict; the locked stack). Ledger department `seo`,
   event ids `seo-<abbr>-<40 hex>`, port 8500, env prefix `SEO_`, `X-SEO-Caller-Token`. The record-first plumbing
   (`store.py` with `DataDirLock`, `close()`, short-write cut-back; `_commit`, integrity, roll-forward, R6-M1
   `GET /seo/v1/audit/evidence`), the API limits, the founder gate, `graceful_close.py` and `launch_guard.py` are
   bizdev-py's (ADR 0016), copied, not reinvented.
2. **Fail closed everywhere.** No ledger → no write. Integrity unverified → no write. A port that is not connected
   answers `NOT_CONNECTED`; it never returns invented data, and selecting one by environment refuses start.
3. **Callers** (`config.KNOWN_CALLERS`): `dashboard` (Andre's console; never Andre by itself), `seo_agent` (the
   agent runtime), `scheduler`, `hub` (a client's read-only view, always with a tenant token), `finance_31` (audit
   status for invoices it issued; no report body), `compliance_38` (evidence, export, integrity).
4. **Outcome envelope** (`envelope.py`), returned by every agent: `outcome` (OK, PARTIAL, NOT_CONNECTED, BLOCKED,
   FAILED, KILLED, QUARANTINED, INSUFFICIENT_EVIDENCE) separate from `findings`; `facts` (what was measured) separate
   from both; `methodology`, `limitations`, `not_connected`. An agent that observed nothing reports no findings.
5. **Evidence classes.** Data: measured, estimated, modeled, inferred, unknown. Effect: observed, attributed,
   incremental, causally supported, financially verified. Every Wave-1 finding's `effect_class` is null: no effect is
   claimed ("credit is not causation").
6. **Decision states:** ACT, TEST, WATCH, DEFER, STOP, DO_NOT_BUILD, DO_NOT_PUBLISH, DO_NOT_SPEND,
   INSUFFICIENT_EVIDENCE. A choice that may be a policy (blocking an AI crawler) is WATCH, never a defect.
7. **Tenants.** `own` (ZBM's properties; the `zbm` tenant is seeded) or `client`. Andre registers each tenant's
   domains; an audit of any other domain is refused `DOMAIN_NOT_AUTHORIZED`. A domain (with its `www.` twin)
   belongs to one tenant only: registering it, or any subdomain or parent of it, to a second tenant is refused
   `DOMAIN_TAKEN`, so a client's site can never be audited free through the own-properties tenant (AEGIS 00cc66b
   M2, 4434eeb N1). Every object names one tenant; a
   tenant-scoped reader asking for another tenant's object gets the same 404 as for a missing one.
8. **Kill switches**, each with its own reason code and test: `global`, `write`, `tenant:<id>`,
   `capability:<fetch|render|ai_probe|audit|entity_write|prompt_sets>`, `provider:<web|openai|anthropic|google|
   perplexity>`. The dashboard or Compliance may engage; only Andre releases. An engage the ledger cannot record takes
   effect at once in memory and is shown as unrecorded. Environment switches (`SEO_KILL_GLOBAL`,
   `SEO_KILLED_CAPABILITIES`, `SEO_KILLED_PROVIDERS`) cannot be released at runtime. A run checks the live switches
   — global, its tenant, `capability:audit`, `write`, and the step's own capability and provider — between steps and
   before every socket operation, so a switch engaged mid-run stops it. If `capability:audit`, `write` or the
   tenant's switch is engaged when the run ends, the completion record is refused: the audit is recorded
   `interrupted` with the switch's reason code (a terminal bookkeeping record, not new work) and the report is
   discarded (AEGIS 00cc66b M3).
9. **Crawled content is data, never an instruction.** Page and answer text is kept only as bounded extracts marked
   `untrusted`; nothing in it can change a decision, a state, a switch or the entity record (tested with injected
   instructions in titles, JSON-LD, llms.txt and provider answers).
10. **Propagation spine, read side.** The canonical entity record holds each field with source, provenance,
    authority, freshness (stale after 180 days unconfirmed) and history. The first record is Andre's own NAP exactly as
    stated: Z Best Media, 5318 East 2nd Street, Long Beach, CA; (562) 248-6617 — nothing he did not state is added.
    The on-site check compares a site's Organization / LocalBusiness JSON-LD and visible phone number with it; the
    record is the authority and is never written from a page.
11. **Primitives.** fetch: http/https only, ports 80 and 443 only (`REFUSED_PORT`); DNS resolved once per hop and
    every address checked (loopback, private, link-local incl. 169.254.169.254, CGNAT, multicast, reserved,
    site-local fec0::/10, the 6to4 relay 192.88.99.0/24, IPv4-mapped / 6to4 / NAT64); the connection made to the
    checked IP with Host and SNI carrying the name (no DNS rebinding); redirects by hand with a cap; the body read
    raw and decoded here incrementally (zlib `max_length` per step against the remaining budget), at most one content
    coding (gzip or deflate; every member of a multi-member gzip body decoded within the same budget, trailing
    non-gzip bytes a `PROTOCOL_ERROR`; stacked or other codings `UNSUPPORTED_ENCODING`; a MemoryError is `RESOURCE_LIMIT`,
    never an exception); one hard overall deadline over connect, TLS, headers and body for every hop, enforced on
    every socket operation by a network backend that also checks the kill switches; robots.txt (RFC 9309) honoured
    for the `ZBM-SEO-Audit` token; always identified. The browser-identity fetch used by access-diff is made only
    where robots.txt allows this service's own crawler, and honours robots.txt for that crawler (AEGIS 00cc66b H1,
    M1, L1-L3). render: a separate port with explicit states (`RAW_OK_RENDER_NOT_CONNECTED`,
    `RAW_OK_RENDER_FAILED`, `JS_DEPENDENT`, …). parse: tolerant stdlib HTML extract plus JSON-LD validation against a
    versioned rule table. diff, access-diff (`BOT_DIFFERENTIAL`), link-diff, change-detect (a failed fetch is
    `TOOL_FAILURE`, never a change).
12. **Ports** (`ports.py`): fetch (connected); render; answer engines openai / anthropic / google / perplexity;
    prompt volume; first-party search_console / bing_webmaster / server_logs / analytics / crm; zero_day;
    orca_publish; clientfix. All but fetch are NOT_CONNECTED in Wave 1.
13. **The audit product.** `POST /seo/v1/tenants/{tid}/audits` runs Selene, Delia, Roman, the entity check, Callum
    and Naomi and stores the report record-first (`audit_requested`; `audit_approved_by_andre` when given;
    `audit_report_recorded` with the report's SHA-256). Own properties need no payment and refuse an invoice id; a
    client audit needs a Finance (31) invoice id as an input (`fin-inv-…`, never looked up) and Andre's approval. A
    run lost to a ledger failure or a dead process is recorded `interrupted` by a job, never completed.
14. **Bot families** are versioned data (`agents/bots.py`) with a source note and an explicit "not complete".
15. **Osei-lite:** malformed inputs (XML with DOCTYPE / ENTITY, malformed XML, oversized bodies, invalid JSON-LD,
    malformed provider answers) are quarantined with reason, size and SHA-256, never parsed further; every observation
    carries an `observed_at` and a refresh-due time from the refresh tiers.
16. **AI-answer reliability** (probe framework): N samples per prompt (at least 3), refusals and errors counted and
    excluded from rates, 95% Wilson intervals and variance, prompt sets versioned and hashed, model versions recorded
    and mixed versions flagged; Callum classes Strength / Opportunity / Mentioned-Not-Cited (plus Absent and
    Insufficient evidence). No single visibility score; Roman's per-engine checklists are labelled heuristic and
    uncalibrated and never combined.
17. **Pricing** (locked by Andre) is a config table only: managed $2,000 / $4,500 / $7,500 per month, above that
    "Contact us"; self-serve $29–500 only after the dashboard launches. Nothing here charges, quotes or invoices.
18. **Log retention.** Raw page bodies and provider answers are never persisted; the log holds ids, codes, hashes,
    the tenants' registered domains and bounded report extracts. The log is append-only and hash-chained, so a
    time-based purge is not possible without a new log epoch (see limitations).

## Founder-pending defaults (the eight open flags)

| # | Flag | Default built | Status |
|---|---|---|---|
| 1 | Perplexity in AI monitoring targets | **Included** (an engine port and a kill switch like the others) | founder decision pending |
| 2 | Marcus | Authority intelligence on bought / open data, not an Ahrefs-scale crawler; **not in Wave 1** | founder decision pending |
| 3 | Julian | Bound to hand-off rules; publishing via Dept 28 / Creative; **not in Wave 1** | founder decision pending |
| 4 | Tenant isolation | The existing record-first log + ledger pattern with cross-tenant negative tests; **no Postgres** | founder decision pending |
| 5 | Zero-Day / ORCA Publish | **Ports, NOT_CONNECTED** | founder decision pending |
| 6 | Non-reverting listings | Needs Legal wording; **not in Wave 1** | founder decision pending |
| 7 | Market-superiority claims | **None anywhere** (code, reports, docs) | founder decision pending |
| 8 | GBP API application | **Wave 0, Andre's job** (with Apple Business Connect and Bing Places); no code | founder decision pending |

## What Wave 1 does

Selene (crawlability), Delia (sitemaps, llms.txt), Roman (content structure, heuristic per-engine checklists), the
probe framework for Naomi and Callum, Osei-lite, the canonical entity record with the on-site consistency check, the
audit product, tenants, kill switches, the evidence view.

## What Wave 1 does NOT do

No rendering; no answer-engine, prompt-volume or first-party adapters (so no AI answer is ever fetched in production,
and nothing is paired with traffic, conversions or revenue — the Goodhart pairing waits for first-party data); no
Marcus, Priya, Theo, Zara, Idris, Vesper, Adaeze, Julian, Sable or Farrah; no listing writes (GBP, Apple Business
Connect, Bing Places); no fix execution (Department 28 clientfix); no Stripe or Finance calls; no department-manager
queues, scorecards or agent lifecycle (active → watch → retrain → restricted → retired); no scheduled re-audits;
no live-run script against the real ledger binary (tests use the fake ledger with ledger-rust's contract).

## Security model

Bearer token on every route but `/health`; per-caller tokens compared by digest with no early exit; Andre only
through the dashboard with his own token; tenant tokens only with the hub caller; personal-data keys refused in any
body; request limits before any route; no-store on every answer; one process per data directory (flock) and one
service instance per claim; SSRF-safe fetch; kill switches; crawled content as data. Short-lived, scoped, revocable
credentials for providers are a requirement for whichever adapter is built first; Wave 1 holds none.

## Limitations (honest)

- Observations come from one network location at one time; a CDN, firewall or geo rule may answer others
  differently. The search-truth drift classes are partly built (change-detect separates tool failure from change);
  model drift and surface drift need the answer-engine and first-party ports.
- Bot access by verified IP range is not observable by changing the User-Agent; access-diff detects UA-based
  differences only.
- The bot family list and the structured-data rule table are this service's versioned data, not complete and not
  any engine's validator.
- llms.txt is an emerging, non-standard convention; it is reported as such and its absence is not a defect.
- Per-engine readiness is an uncalibrated heuristic checklist; it is not a visibility measurement.
- The append-only log cannot purge by age; retention is by minimisation (no bodies stored). A purge needs a new log
  epoch and an ADR.
- (Wave 1/2) The paid-audit flow took an invoice id on trust from the caller; Andre's approval was the control. Wave 3
  (W3-2) verifies it with Finance (31) by default; `SEO_INVOICE_VERIFICATION=trust` keeps the old behaviour.
- Tests run against an in-process fixture server; nothing here has been run against a real public site yet.
- The hard fetch deadline relies on installing a network backend into httpx's connection pool (no public httpx
  parameter exists in 0.28); if a future httpx moves it, the fetcher refuses to run rather than fetch without it.
- Brotli and zstd responses are refused (`UNSUPPORTED_ENCODING`) rather than decoded; the crawler never advertises
  them, but a misconfigured server sending them anyway cannot be read.

## Wave 2 (decision-free parts; built Oct 10 2026 on `seo-dept-02-wave1`)

W2-1. **Crawler access from first-party logs** (P2, Selene's `log_access` task; `agents/logs.py`, `svc_logs.py`).
   A client (hub with its tenant token) or the console uploads an access log for one of the tenant's registered
   domains in base64 chunks that end on a line boundary (Common, Combined or JSON lines; ≤ 90,000 bytes a chunk,
   `SEO_LOG_MAX_BYTES` an ingest). Each chunk is parsed in memory and only its aggregate delta is committed
   record-first, idempotent by request id and sequence. Lines over 8 KiB, invalid UTF-8, malformed lines, bad IPs and
   bad statuses are quarantined and counted. Requests are attributed to bot families by User-Agent token (versioned
   bot list, now `2026-10-10.1` with DNS suffixes). **A User-Agent is a claim:** a hit is `verified` or `spoofed`
   only through the bot-verification port (reverse DNS plus forward-confirm against the operator's documented host
   names: Googlebot, Bingbot, Applebot), which is NOT_CONNECTED unless `SEO_BOT_VERIFY_DNS=1`; everything else stays
   `claimed`. IP-range verification (how OpenAI, Anthropic, Perplexity and Common Crawl identify their crawlers) is a
   NOT_CONNECTED port. The report: per-family volume, status mix, distinct client IPs, verified / spoofed / claimed,
   robots-disallowed hits (today's robots.txt, fetched through the guarded fetcher), and important URLs (the latest
   audit's sitemap sample of that domain) that no search crawler requested — missing, never invented, when there is
   no audit.
W2-2. **Log retention rule** (amended for AEGIS 1472041 H1). Nothing identifying is retained, so the append-only log
   holds nothing that would need shredding: raw lines, raw URL paths, query strings, IPs, IP hashes and User-Agent
   strings never reach the log, the ledger, a report or an error. A path is reduced to a TEMPLATE before anything is
   counted (`agents/logs.template_path`: decoded once; e-mail addresses, UUIDs, numeric ids, long hex / base64 /
   token-like segments and letter-digit mixes replaced by `{email}`, `{uuid}`, `{id}`, `{token}`). Exact paths are
   compared only in memory: against robots.txt and the public sitemap sample, both fixed when the ingest is created
   (counts and sample indices are kept). Distinct client IPs are a HyperLogLog estimate (256 registers of keyed hashes
   under `SEO_LOG_HASH_KEY_FILE`; no hash is kept). An ingest older than `SEO_LOG_RETENTION_DAYS` (default 90) is
   served as totals only (`expired`); from then on only its counts stay in memory — report, templates, sketches,
   robots.txt and sitemap sample are evicted, and replay at start-up compacts expired ingests as it goes instead of
   rebuilding them (AEGIS 4c0a805 T4). Templating (AEGIS 4c0a805 T1) first NFKC-normalises and folds confusable
   at-signs, recognises e-mails in any spelling (`@`, fullwidth / small at-signs, `(at)`, `[at]`, ` at … dot`,
   `%40` after one decode), turns any segment with 7+ digits in total — phone numbers, SSNs, card numbers, whatever
   separates the digits — and short numeric segments that together hold 7+ digits into `{number}`, and replaces the
   stem of any file name with a non-code extension (`{file}.pdf`). Crypto-shredding was not built: with no identifying data retained there is
   nothing to shred, and a homemade cipher on the standard library was judged worse than not storing the data.
   **Bounds** (H2): `SEO_LOG_MAX_OPEN_INGESTS` open ingests per tenant, `SEO_LOG_MAX_INGESTS` ingests and
   `SEO_LOG_TENANT_BYTES` bytes per tenant within a retention period, `SEO_LOG_MAX_BYTES` per ingest; per-ingest state
   is fixed-size (at most 500 templates per family, 256 registers); a finished ingest's robots.txt leaves memory.
   **DNS verifier bounds** (M1): non-globally-routable addresses are never looked up; one shared pool of four workers
   for the process, a lookup submitted only when a worker is free (never queued behind a hung resolver), and a
   process-lifetime lookup cap.
W2-3. **Search-truth drift** (`agents/drift.py`, Osei). Two completed audits of the same tenant and target are
   compared finding by finding; every change gets exactly one class by the first rule whose evidence holds:
   R1 TOOL_FAILURE, R2 MEASUREMENT_ERROR (the ruler moved — bot list, rule table, entity record version, or the prompt
   set — and the thing measured did not), R3 UNKNOWN (the ruler AND the thing changed: a confound, never REAL_CHANGE),
   R4 MODEL_DRIFT, R5 SAMPLING_NOISE, R6 SURFACE_DRIFT, R7 REAL_CHANGE, R8 UNKNOWN (rules version `2026-10-10.2`,
   amended for AEGIS 1472041 M4; the evidence of each rule is in the module docstring). Reports
   now carry per-page content fingerprints, rule versions and the robots.txt hash so the rules have evidence.
W2-4. **Scheduled re-audits.** One audit per schedule per slot, the audit id derived from (schedule, slot): duplicate,
   replayed and post-restart ticks are no-ops and a slot is never run twice; missed slots are not back-filled; each
   schedule's due slot is computed when its turn in the tick comes, after the earlier audits finished (L2). A paying
   client's schedule needs Andre and a Finance (31) invoice id at creation (one invoice id per schedule — see the
   pending default below); resuming a paused client schedule is Andre's. Budget: `SEO_SCHEDULE_BUDGET_RUNS` per tenant
   per `SEO_SCHEDULE_PERIOD_DAYS` window (over-budget slots recorded `BUDGET_EXHAUSTED`). Kill switches leave a due
   slot for the next tick; a switch engaged mid-run interrupts the run. Consecutive completed runs get a recorded drift
   report.
W2-5. **Department manager** (`svc_manager.py`). Work queues per agent (audits running, schedule slots due, open
   log ingests), scorecards computed only from recorded runs (runs, outcomes, findings produced, findings confirmed
   and overturned from recorded drift reports, NOT_CONNECTED and failure rates; null over zero runs), and the
   lifecycle active → watch → retrain → restricted → retired. Moves into or out of `restricted`, and into `retired`
   (terminal), are Andre's alone; each move is recorded with its reason code and the scorecard's SHA-256. A
   restricted or retired agent is refused by the run guard everywhere (AEGIS 1472041 M2/M3): in an audit its outcome
   is `RESTRICTED` with no findings (Osei's data-hygiene step included: the report's `data_hygiene` is then null, while
   the parsers' own fail-closed quarantine still applies); a restricted Selene refuses log ingest creation, chunk
   uploads and parsing, and log reports (403 `AGENT_RESTRICTED`); a restricted Osei refuses drift views and records
   no drift. Scorecards count only recorded agent envelopes; drift reports are counted separately, never as runs.
   An agent move replays before any state check (L1). Restricting is allowed under the write switch.
W2-6. New capability switches `logs` and `schedules`; the run guard of a log ingest checks `logs`, of an audit
   `audit`.

Founder-pending defaults added by Wave 2: (9) a paying client's schedule carries ONE invoice id (the managed-tier
subscription invoice) for all its runs, bounded by the budget cap — per-run invoicing is a founder decision;
(10) the scheduler does not back-fill missed slots; (11) the drift rules (R1–R7) are this service's, versioned, and
not calibrated against any engine.

Wave 2 limitations: the log covers only what the client uploads; a User-Agent claim from a family without DNS
verification can never be confirmed until the IP-range port is built; budget and verification caches reset with the
process (a restarted ingest re-verifies up to its budget); a scheduled slot whose run was interrupted is consumed,
not retried; SURFACE_DRIFT says only that the intervals separated with the same prompts and models, never why; the
department manager's "confirmed" means "persisted in the next run", not "verified by a person".

## Wave 3 (decision-free parts; built Oct 10 2026 on `seo-dept-02-wave1`)

W3-1. **Live run against the real ledger** (`services/seo-py/devtools/live_run.py`, CI job `live-run (seo-py, 3.12 /
   3.13)`). ledger-rust's release binary and `cd src && python3 -m api` as separate processes over real HTTP, durable
   data directory, production mode: NOT_BUILT and missing-key settings refuse start; start-up integrity against the
   real ledger and the seed; a second process on the data directory refuses; tenant, domain, hub-scope, invoice and
   Andre rules over the wire; a kill switch engaged by Compliance (38) holds across a restart until Andre releases
   it; audit reports recorded first with their SHA-256 on the ledger; one scheduled run per slot; the department
   scorecard; the evidence view committed-exactly-once; a truncated log detected; nothing identifying in the clear on
   the ledger; `GET /ledger/verify` valid. **Limitation:** the live run never crawls. CI has no site to crawl and a
   live run must not depend on the internet, so the web provider switch is engaged before the first audit and every
   fetch stops at the guard (which runs before name resolution). The crawler, SSRF guard and parsers are proven only
   against the in-process fixture server and the war room (W3-3), never against a real public site.

W3-2. **Finance (31) invoice verification** (`src/finance_client.py`, `src/svc_invoices.py`). Behind
   `SEO_INVOICE_VERIFICATION`, default `finance` (the safe option); `trust` is the Wave 1/2 behaviour and is shown in
   `/status`. The client calls the one existing read route, finance-py's `GET /fin/v1/invoices/{id}` (service bearer
   token + this service's own caller token, `X-FIN-Caller-Token`; finance-py gained the caller name `seo_02`, which
   like every Finance caller reaches only the routes open to any caller and can write nothing). Per-attempt timeout
   (`SEO_FINANCE_TIMEOUT_SECONDS`), one overall deadline, at most three attempts and only for connect errors,
   timeouts, 429 and 5xx; no redirects; a 64 KiB body cap; identity encoding only; anything malformed, an answer
   about another invoice, or a 404 that is not Finance's "no such invoice" is UNVERIFIABLE, never "paid". Only the
   facts the verdict needs are kept (id, status, entity, client id, kind, currency, refunded / charged back, paid
   time); amounts, lines and template variables are dropped and never reach the log or the ledger. Andre binds each
   client tenant to its Finance client id (`POST /tenants/{tid}/finance-client`). A client's audit or schedule then
   proceeds only when Finance confirms the invoice is Z Best Media's, this client's, USD, `paid`, with nothing
   refunded or charged back, and the invoice has not paid for another run. The domain, invoice-required and Andre
   checks come first, so nobody but Andre can make this service look an invoice up. DEFINITIVE answers (not found,
   another entity or client, unsupported currency, not paid, refunded) are refused 409 and cannot be overridden;
   UNVERIFIABLE ones (`FINANCE_NOT_CONFIGURED`, `FINANCE_UNAVAILABLE`, `FINANCE_AUTH_REFUSED`,
   `FINANCE_RESPONSE_INVALID`) are refused 503 with `override_allowed: true`, and Andre may override them on the same
   request (which already carries his verified approval) by naming `invoice_override` from a closed set
   (`ANDRE_CONFIRMED_PAYMENT`, `FINANCE_OUTAGE`). A verified invoice is recorded as `invoice_verified`, an override as
   `invoice_verification_overridden_by_andre`, both in the same record-first commit as the request; a refused attempt
   records nothing. Finance is asked outside the service lock and every state rule (idempotency, kill switches,
   reuse) is checked again after the answer. A schedule's invoice is verified when it is created and again before
   every slot: a definitive refusal skips the slot with Finance's reason (recorded); an unverifiable answer leaves the
   slot for the next tick (never overridden at a tick: Andre is not there). Tests: `tests/test_invoice_verification.py`
   (Finance down, timeouts, 5xx, malformed, oversized, encoded, redirected, another invoice's body, wrong tokens,
   wrong tenant, wrong entity, unpaid, refunded, charged back, replay, a concurrent replay during the Finance call, a
   kill switch engaged during it, restart); finance-py's `tests/test_seo_invoice_contract.py` runs this client against
   Finance's real app through an invoice's whole life (draft → issued → paid); the live run (W3-1) runs it against the
   real Finance process.

Founder-pending defaults added by Wave 3: (12) verification is ON by default, so until Finance is wired every paid run
needs Andre's override; (13) one invoice pays for one one-off audit or one schedule (with all its slots), and an
interrupted audit gives its invoice back; (14) any refund or chargeback on the invoice, partial ones included, refuses
the run; (15) Andre may override only an UNVERIFIABLE answer, never a definitive one, and never a reused invoice; (16)
no check of the invoice's amount, kind or line code against what was sold — Finance has no SEO line code and no
per-audit price is set (the locked pricing is managed tiers per month); that is a founder and Finance decision.

Wave 3 limitations: finance-py's invoice route returns the whole invoice record (lines, template variables) to any
caller including `seo_02`; this service drops all but the verdict's fields, but Finance discloses more than is needed
— a narrower status route is a Finance (31) change not made here. Invoice consumption is known only to this service
(Finance does not learn that an invoice paid for an audit), and it is not enforced in `trust` mode. A one-off audit's
verification is point-in-time: a refund after the run does not touch the recorded audit. The tenant → Finance client
binding is Andre's statement and is not cross-checked with Legal. The live run cannot reach a PAID invoice through
Finance's production entrypoint (its bank feed and Legal are fail-closed stand-ins there); the paid path is proven by
the contract test against Finance's app with its test fakes.

## Unlock list

Answer-engine adapters with scoped, short-lived credentials; a renderer; Search Console and Bing Webmaster; a
prompt-volume source; Department 28 clientfix hand-off; the remaining agents per the spec's waves; the console pages.
(Built in Wave 3: the live run against ledger-rust, W3-1; the Finance (31) client to verify invoices, W3-2.)
