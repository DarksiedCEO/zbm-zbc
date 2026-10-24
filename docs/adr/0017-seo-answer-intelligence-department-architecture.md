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
    orca_publish; clientfix. All but fetch are NOT_CONNECTED in Wave 1. (Search Console: built Oct 24 2026 and
    NOT_CONNECTED until a service-account key is configured; section "Search Console". Wave 4: the Perplexity
    engine has a real adapter, connected only with a key; see W4.)
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
| 1 | Perplexity in AI monitoring targets | **Included** (an engine port and a kill switch like the others) | decided: Andre chose Perplexity (go-live plan); adapter built in Wave 4 (W4) |
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
credentials for providers are a requirement for whichever adapter is built first; Wave 1 holds none. (Wave 4: Perplexity
offers no scoped or short-lived key; the compensating controls are in W4-5.)

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
   `/status`. The client calls finance-py's narrow `GET /fin/v1/invoices/{id}/status` (service bearer token + this
   service's own caller token, `X-FIN-Caller-Token`; finance-py gained the caller name `seo_02`, which since AEGIS SEO
   Wave 3 M2 reaches that one route and nothing else; W3-5). Per-attempt timeout
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

W3-3. **War room** (ADR 0018; `devtools/warroom/drivers/seo_py.py`, `scenarios/seo-py.json`, CI job `warroom`). The
   war room merged in from `wire-bugfix-wave-2026-10-07` (ebc8403) now gates this department too. The driver wires the
   service's REAL fetcher (production address policy, ports, redirect handling, decoding, deadline) to a fake
   internet inside the sealed worker: a fake DNS (public names; names answering private, mixed, IPv4-mapped, NAT64
   and CGNAT addresses; a rebinding name whose second answer is loopback; the numeric spellings `inet_aton` accepts)
   and an in-process web server that records every request; an internal address that is ever reached answers a
   secret marker. Fifteen scenarios: SSRF targets through redirects, canonical links and robots / sitemap / llms.txt
   (the suite's pinned targets plus names and spellings), odd ports / schemes / credentials, compression bombs and
   odd codings, trickled bodies, giant and malformed files, XML entity quarantine, crawled prompt injection,
   identifiers and hostile content in uploaded logs (leaks judged with the suite's own PII needles through the shared
   lookalike fold, independent of the templater), cross-tenant access by a hub token (a 404 must be byte-identical to
   a missing object's), personal-data keys, kill switches before / during / against crawling, and paid runs against
   a hostile Finance (31). Every invariant is MUST. Seeds 1-3 found three MUST failures, each an audit answering 500
   and left `running` because of what a hostile site served — WR-F008 (a redirect Location httpx cannot parse raised
   `httpx.InvalidURL` out of the fetcher), WR-F009 (an unreadable canonical became `None` and was dereferenced),
   WR-F010 (an invalid IPv6 URL in robots.txt or a sitemap index raised in Delia) — promoted to the replay library
   (`replay/seo-py.json`, R0001-R0015), filed in `findings.md` and fixed: the fetcher refuses such targets
   (REFUSED_URL), the parser counts an unreadable canonical (`CANONICAL_INVALID` finding), the agents read crawled
   URLs through `agents.split_url` / `host_of`, and a run that raises anyway is closed `interrupted` with reason
   `RUN_FAILED` at once (no 500, nothing left running). The war room also showed identifiers disguised with invisible
   characters or letter lookalikes surviving log templating (scored SHOULD then); fixed as well: invisible characters
   are removed before the one decode, and each path segment is judged on the repo's shared lookalike fold (seo-py now
   carries the byte-identical `src/lookalikes.py`, hygiene rule L4), while a segment that is kept is kept as written
   (a real Cyrillic path is not rewritten). Over-minimisation, stated: a non-Latin segment whose fold is 20+ letters
   is now `{token}`.

W3-4. **Not built in Wave 3, and why** (the rest of the unlock list):
   - Department 28 clientfix hand-off: the only Department 28 service in the repository (`services/delivery-py`,
     ADR 0011) is the AEGIS fix engine for this repository's own code; it has no intake for fixes on a client's
     site, so a port would invent its contract. Needs Department 28's design and a founder decision.
   - Console pages: `apps/dashboard-ts` is one page for Revenue Recovery findings; no department has console pages
     and there is no pattern to follow. The evidence views exist on the API (`/audit/evidence`, `/department`,
     `/status`).
   - Answer-engine adapters, Search Console, Bing Webmaster: need provider credentials (and the scoped, short-lived
     credential design the security model requires).
   - A renderer, a prompt-volume source, listing writes (GBP, Apple Business Connect, Bing Places): founder decisions
     pending (which renderer and where it runs; which data source; Legal wording for non-reverting listings, flag 6).
   - Marcus and Julian (flags 2 and 3) and the spec's remaining agents: founder decisions or data sources pending.
   - Andre's approvals through Cybersecurity (22) passkeys: no service has adopted security-py's approval API yet;
     a cross-department change, not this department's alone.

W3-5. **AEGIS SEO Wave 3 review (f9f750d), findings closed.**
   - M1: the Finance lookup's overall deadline now holds within one attempt: the production transport routes every
     socket operation (connect, TLS, read, write) through a deadline backend bounded by the time left, and the body
     loop stops at the deadline (FINANCE_UNAVAILABLE). AEGIS's drip probe (1 byte / 0.4 s, `timeout_s=1`,
     `attempts=1`) now ends at the 1 s deadline. Tested on an injected monotonic clock.
   - M2: finance-py gives `seo_02` exactly one route, the new narrow `GET /fin/v1/invoices/{id}/status` (no amounts,
     lines, descriptions, template variables or legal references; refund and chargeback as flags); a middleware
     refuses `seo_02` everywhere else before any route runs, and `caller()` with no names no longer admits it. The
     client reads only that route and refuses an answer carrying any other field. A test enumerates every finance-py
     route from the app and asserts 403 for `seo_02` on all but that one; other callers' reach is unchanged.
   - M3: an invalid canonical counts for CANONICAL_MULTIPLE (one valid plus one invalid is two), and
     CANONICAL_INVALID is reported whenever any canonical is invalid.
   - L1: after the Finance call the tenant's Finance binding must still be the one the verdict was judged against;
     otherwise the run is refused `TENANT_FINANCE_CLIENT_CHANGED` (at a tick the slot waits for the next tick).
   - L2: RUN_FAILED records the failed step (agent) and a safe location (`module:function:line` of the innermost frame
     in this service's source, plus the exception's type, never its message) in the log line and the ledger evidence.
     A scheduled slot whose run fails is consumed but counted `failed`, not `ran`.
   - L3: a redirect target httpx cannot encode or IDNA-encode (full-width host, `xn--`) is REFUSED_URL, not
     CONNECT_FAILED; nothing is connected to (requests go to checked addresses only).
   - L4: the war room's paid-run scenario is split: Finance cannot answer → MUST 503 with the override offered;
     Finance says no → MUST 409, not overridable; Finance confirms → the run completes and `invoice_verified` is on
     the ledger.

W3-6. **AEGIS SEO Wave 3, round 2 Lows (after the certified c60e1c7 and 73fe3cc).**
   - Host strictness (`primitives/fetch.py`): after IDNA a host is either an IP literal (normalised; an IPv6 zone id
     is refused) or plain LDH labels (`[a-z0-9-]`, 1-63 characters a label, no leading or trailing hyphen, at most
     253 in all); anything else is REFUSED_URL before any resolver call, so a control character
     (`127.0.0.1\x0bevil`; glibc's `inet_aton` reads `0177.0.0.1\x0b` as 127.0.0.1), a Unicode space IDNA maps to an
     ASCII space (U+3000, U+00A0, U+2003, U+205F), an underscore or an empty label never reaches getaddrinfo. A real
     IDN becomes xn-- labels and passes; characters IDNA maps to nothing (a zero-width space) give the same name,
     which then meets the address policy like any other; the legacy-numeric refusal is unchanged. Underscores are
     refused on purpose: they are not host-name characters (RFC 952, RFC 1123 section 2.1), DNS uses them for
     service labels (`_dmarc`, `_sip._tcp`) that are never a web origin, and the rare site published under one gets
     a visible REFUSED_URL rather than a fetch; revisit only if a real client needs it. While normalising literals,
     an IPv6 literal's Host header is now bracketed (`[2606:4700::1]`), as RFC 9110 requires.
   - Finance client DNS (`finance_client.py`): the Finance host is resolved INSIDE the lookup's deadline. getaddrinfo
     runs on its own daemon thread under a slot of one process-wide gate of two; the lookup waits only for the time
     left. With both slots held a lookup is refused at once (never queued), so at most two resolver threads exist,
     whatever the resolver does. A slot is taken before the thread starts and given back exactly once (a guarded
     release in the thread's `finally`, or by the caller when the thread cannot start); a forked child starts with
     every slot free. A late, failed, refused, empty or non-address answer is ConnectTimeout / ConnectError, i.e.
     FINANCE_UNAVAILABLE (fail closed). An IP literal is used without any lookup. The connection goes to the resolved
     addresses in order, each attempt bounded by the time left, and the socket layer is handed only IP literals (a
     test with a getaddrinfo that refuses every name proves no second lookup); httpcore takes Host, TLS SNI and the
     certificate hostname from the URL's origin, not from the connect address, so HTTPS is unchanged (tested: SNI
     and Host stay `finance.internal`). Chosen over requiring an IP literal in `SEO_FINANCE_URL`: it keeps service
     names and DNS-based failover usable and puts the same hard bound on every phase. Residual: a hung system
     resolver keeps its thread and slot until the C library gives up (glibc: `timeout` x `attempts` per name server,
     resolv.conf), and while both slots are held every Finance lookup is FINANCE_UNAVAILABLE; it never holds a lookup
     past its deadline. Daemon threads, so a hung lookup never delays process exit.
   - Finance `/health` for `seo_02`: allowed. `/health` needs no token at all, so refusing it to `seo_02` protected
     nothing; `seo_02` gets exactly the anonymous answer (status, service, rules version and pinning, storage mode,
     entities, reconcile mode and flag, log-fault flag, Stripe-wiring flags; no client, invoice or money data, and a
     test pins that key set). Only `GET /health` is admitted; any other method or spelling, and every other route
     but the invoice status, stays 403.

Founder-pending defaults added by Wave 3: (12) verification is ON by default, so until Finance is wired every paid run
needs Andre's override; (13) one invoice pays for one one-off audit or one schedule (with all its slots), and an
interrupted audit gives its invoice back; (14) any refund or chargeback on the invoice, partial ones included, refuses
the run; (15) Andre may override only an UNVERIFIABLE answer, never a definitive one, and never a reused invoice; (16)
no check of the invoice's amount, kind or line code against what was sold — Finance has no SEO line code and no
per-audit price is set (the locked pricing is managed tiers per month); that is a founder and Finance decision.

War room limitations: the fake web replaces the socket, so what only a socket shows — a HEADER trickled byte by byte,
TLS, a real resolver's behaviour — is proven by the suite's real-socket tests, not by the war room; the chaos is the
library's (no LLM personas, ADR 0018); each case is one short exchange on a fresh harness (no concurrency or load).

Wave 3 limitations: Invoice consumption is known only to this service
(Finance does not learn that an invoice paid for an audit), and it is not enforced in `trust` mode. A one-off audit's
verification is point-in-time: a refund after the run does not touch the recorded audit. The tenant → Finance client
binding is Andre's statement and is not cross-checked with Legal. The live run cannot reach a PAID invoice through
Finance's production entrypoint (its bank feed and Legal are fail-closed stand-ins there); the paid path is proven by
the contract test against Finance's app with its test fakes.

## Search Console (first-party truth) — go-live plan days 4-7, built Oct 24 2026 on `golive-gsc`

**Status: built and tested; NOT_CONNECTED.** No Google credential exists yet. Without
`SEO_GSC_SERVICE_ACCOUNT_FILE` the Search Console port is the old `NotConnectedSource`, every audit's
`search_performance` section answers `GSC_NOT_CONNECTED` with zero requests, and the live run proves at process level
(an audit hook on every name lookup and connect) that nothing leaves 127.0.0.1. Code: `src/gsc_client.py` (the
connector), `src/svc_gsc.py` (binding, budget, record-first read, the audit section), `src/agents/search_performance.py`
(aggregation; Osei's task), `src/agents/drift.py` (SP1-SP4).

GSC-1. **Model.** A client adds this service's Google service account (its `client_email`) as a user of the client's
own Search Console property; this service authenticates as that service account (two-legged JWT bearer flow, no
user consent screen, no refresh token) with the read-only scope and reads aggregate performance for that one property.
Nothing is ever written to Google.

GSC-2. **Google facts this build relies on**, each read on Google's pages on Oct 24 2026 (WebFetch; the Search Console
reference pages came back in a localized rendering, so the numbers and identifiers below were read from it, not
translated prose):

| Fact | Source |
|---|---|
| Token endpoint `POST https://oauth2.googleapis.com/token`, `application/x-www-form-urlencoded`, `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`, `assertion=<JWT>` | https://developers.google.com/identity/protocols/oauth2/service-account (fetched as https://developers.google.com/accounts/docs/OAuth2ServiceAccount, which serves it) |
| JWT header `alg` RS256 and `typ` JWT required, `kid` optional (the key id); claims `iss` (service-account e-mail), `scope` (space-delimited), `aud` = the token URL, `iat`, `exp` at most one hour after `iat`; `sub` only for domain-wide delegation (not used); signature RSASSA-PKCS1-v1_5 with SHA-256, base64url | same |
| Answer: `access_token`, `token_type` Bearer, `expires_in` (3600 in Google's example), `scope`; errors as `{"error", "error_description"}` with `invalid_grant` (clock skew / exp more than 65 minutes after iat; invalid signature: key deleted, disabled or expired), `unauthorized_client`, `access_denied`, `admin_policy_enforced`, `invalid_client`, `deleted_client`, `disabled_client`, `invalid_scope`, `org_internal` | same |
| Scope `https://www.googleapis.com/auth/webmasters.readonly` accepted by both methods used (the other being `.../auth/webmasters`) | https://developers.google.com/webmaster-tools/v1/searchanalytics/query and https://developers.google.com/webmaster-tools/v1/sites/list |
| `POST https://www.googleapis.com/webmasters/v3/sites/{siteUrl}/searchAnalytics/query`; siteUrl `http://www.example.com/` (URL-prefix) or `sc-domain:example.com` (Domain property) | https://developers.google.com/webmaster-tools/v1/searchanalytics/query; also https://developers.google.com/webmaster-tools/search-console-api-original/v3/searchanalytics/query |
| Request: `startDate` / `endDate` YYYY-MM-DD in Pacific Time, inclusive, required; `dimensions` from country, device, page, query, searchAppearance, date, hour (none = one merged row; a dimension once); `type` default web; `aggregationType` auto (default), byPage, byProperty (not with page grouping or filtering; not for discover / googleNews), byNewsShowcasePanel; `rowLimit` 1-25,000, default 1,000; `startRow` zero-based, default 0, past the end = success with zero rows; `dataState` final (default) / all / hourly_all, case-insensitive | same |
| Response: `rows[]` of `keys[]` (request order), `clicks`, `impressions`, `ctr` (0-1), `position`; `responseAggregationType`; `metadata.first_incomplete_date` / `first_incomplete_hour` only with dataState all / hourly_all, in America/Los_Angeles; sorted by clicks descending (date-grouped: ascending); dates without data omitted; the API "does not guarantee every row" (top rows only) | same |
| Pagination: repeat the query raising `startRow` by the row limit until a response has 0 rows; at most 50,000 rows per day per search type; data typically available after 2-3 days; with page or query dimensions "some data may be dropped" | https://developers.google.com/webmaster-tools/v1/how-tos/all-your-data |
| `GET https://www.googleapis.com/webmasters/v3/sites` -> `{"siteEntry": [{"siteUrl", "permissionLevel"}]}`, permissionLevel siteFullUser, siteOwner, siteRestrictedUser, siteUnverifiedUser | https://developers.google.com/webmaster-tools/v1/sites/list and https://developers.google.com/webmaster-tools/v1/sites |
| Errors `{"error": {"code", "message", "errors": [{"domain", "reason", "message", ...}]}}`; 401 unauthorized / authError / expired; 403 insufficientPermissions, quotaExceeded, rateLimitExceeded, userRateLimitExceeded, accessNotConfigured (API not enabled), forbidden, accountDisabled, ...; 404 notFound; 429 rateLimitExceeded; 500 internalError; 503 backendError | https://developers.google.com/webmaster-tools/v1/errors |
| Quotas: Search Analytics 1,200 QPM per site, 1,200 QPM per user, 40,000 QPM and 30,000,000 QPD per project; short-term (10-minute) and long-term (1-day) load limits, page and query grouping the most expensive, both together the worst; exceeding any quota answers "quota exceeded" (wait 15 minutes for the short-term one) | https://developers.google.com/webmaster-tools/limits |
| CTR = clicks / impressions; position = average position of the topmost result; chart data aggregated by property, table rows by page for Pages; chart and table totals can differ | https://support.google.com/webmasters/answer/7576553 |
| Users need a Google Account; groups cannot be added; Owner / Full / Restricted; the permissions table gives Restricted users the Performance report; at most 100 non-owners per property | https://support.google.com/webmasters/answer/7687615 |
| A service account's e-mail can be added to a verified property as a **delegated owner** ("Provide your service account email as the delegated owner"); the e-mail is the key's `client_email` | https://developers.google.com/search/apis/indexing-api/v3/prereqs |

Not found on the pages read: how far back Search Analytics data goes (the oft-quoted 16 months is from secondary
sources only — UNVERIFIED; the connector asks for at most 90 days, so nothing here depends on it); a Retry-After on
quota errors (honoured when present, capped at 60 s).

GSC-3. **U1 — can a service-account e-mail be added as a USER (Full / Restricted) of a property? UNVERIFIED.** Google's
pages confirm adding a service account as a delegated OWNER (Indexing API prerequisites) and that a user must be a
Google Account, but no page read says a service account may be added as a Full or Restricted user. The connector does
not depend on the level: it reads only when sites.list shows the property at siteOwner, siteFullUser or
siteRestrictedUser (siteUnverifiedUser or absent = `GSC_PROPERTY_NOT_GRANTED`). Andre's first client onboarding
settles U1: if Search Console refuses the e-mail as a user, the fallback Google documents is a delegated owner — more
power than a read needs, so that is a founder decision (pending default 18 below), never automatic.

GSC-4. **The connector** (`gsc_client.py`, standard library + httpx + `cryptography`):
   - Key file: `SEO_GSC_SERVICE_ACCOUNT_FILE`, the *_FILE rules of `config._secret_file_bytes` (absolute path, regular
     file, no symlink, owned by the service user, mode 0600 / 0400, at most 16 KiB). The JSON must be a Google
     service-account key: `type` service_account, a `…iam.gserviceaccount.com` `client_email`, a hex
     `private_key_id`, an RSA private key of at least 2048 bits, and `token_uri` exactly Google's token URL (the
     assertion never goes anywhere else). A bad file refuses start; no error ever quotes it (the JSON parser's and
     cryptography's exceptions are replaced, `from None`). The key lives in `ServiceAccountKey` (`__slots__`, no
     `__dict__`, redacted repr, pickling and copying refused); `Settings.gsc_key` is `repr=False`. The PEM text is
     dropped after parsing (Python cannot zero a string; the key itself is held by OpenSSL through cryptography).
   - Token: a JWT signed per request of a token (`iat` = now, `exp` = now + 3600), cached as `_Secret` (redacted repr,
     never pickled) until `expires_in` minus min(300 s, half its life). A 401 from the API with a cached token drops it
     and retries once with a fresh one; a second 401 is `GSC_AUTH_REFUSED`. Token-endpoint 400 / 401 / 403 is
     `GSC_AUTH_REFUSED` with Google's `error` from the documented closed set (never `error_description`); an `error`
     that is not a string (Google documents a string) is `GSC_PROTOCOL_ERROR` `token_error_shape`.
   - Hard deadline: ONE deadline per read (`SEO_GSC_DEADLINE_SECONDS`, default 60) over the token, the site check and
     every page. Production requests go through Finance's `_DeadlineBackend` (finance_client.py; AEGIS SEO Wave 3 M1
     and round 2): the Google host is resolved on a gated daemon thread inside the deadline (this module's own gate of
     two), the connection is made to the resolved addresses, and every connect, TLS step, read and write is bounded by
     the time left; the body loop checks the deadline too. If httpx stops exposing the pool's network backend, the
     client refuses to call Google (`GSC_NOT_CONNECTED`) rather than call it unbounded. Per-attempt timeout
     `SEO_GSC_TIMEOUT_SECONDS`.
   - Allowlist: `oauth2.googleapis.com` and `www.googleapis.com` (both from the pages above), https, port 443, no
     credentials or fragment in the URL, checked before every request and again in the network backend; redirects are
     never followed (a 3xx is `GSC_PROTOCOL_ERROR`); every resolved address must be globally routable (a private,
     loopback, link-local or mapped answer is refused, never connected to); an address literal is never a Google host.
   - Retries: at most 3 attempts per request and only for connect errors, timeouts, 429, 500 / 502 / 503 / 504 and
     403 rateLimitExceeded / userRateLimitExceeded, with backoff 1 s then 3 s (Retry-After honoured up to 60 s) and
     never past the deadline. 403 quotaExceeded / dailyLimitExceeded is `GSC_RATE_LIMITED` at once (Google: wait).
     A request budget per read (`SEO_GSC_MAX_REQUESTS`, default 30, retries included) -> `GSC_BUDGET_EXHAUSTED`.
     Start-up refuses a budget below what the page caps need when every request succeeds first time — 3 + 2 ·
     min(`SEO_GSC_MAX_PAGES`, ceil(`SEO_GSC_MAX_ROWS` / `SEO_GSC_PAGE_ROWS`)), 23 at the defaults — so a large
     property ends a read `truncated`, not over budget (AEGIS gl-gsc L5); retries draw on the rest.
   - Pagination: page dims `page` and `query` are read in pages of `SEO_GSC_PAGE_ROWS` (default 5,000) from startRow 0,
     stopping at a short or empty page, at `SEO_GSC_MAX_PAGES` (10) pages or `SEO_GSC_MAX_ROWS` (50,000) rows; a cap
     that stops it after a full page sets `truncated: true`. A key repeated within or across pages (the data moved
     between requests) is `GSC_PROTOCOL_ERROR`. The date query is one page (at most window-days rows).
   - Strict parsing: documented top-level fields only; `rows` a list no longer than the rowLimit asked; each row
     `keys` (exactly one string per dimension), `clicks` and `impressions` whole, finite, 0..1e15 (a larger integer
     is refused before any float conversion) and clicks <=
     impressions, `ctr` 0..1, `position` >= 0; dates inside the window; `responseAggregationType` equal to what was
     asked; `metadata` only its two documented keys; identity encoding only (gzip refused), bodies capped (16 KiB
     token, 512 KiB sites, 8 MiB per query page, by Content-Length and on the bytes read). JSON `NaN` is refused.
   - Reason codes and the section outcome: `GSC_NOT_CONNECTED` (NOT_CONNECTED), `GSC_PROPERTY_UNBOUND` (NOT_CONNECTED:
     no binding), `GSC_KILLED` (KILLED), `GSC_AUTH_REFUSED` / `GSC_PROPERTY_NOT_GRANTED` / `GSC_BUDGET_EXHAUSTED`
     (BLOCKED), `GSC_RATE_LIMITED` / `GSC_UNAVAILABLE` / `GSC_PROTOCOL_ERROR` (FAILED); plus `GSC_PROPERTY_INVALID`
     and `GSC_PROPERTY_TAKEN` for bindings. A failed read never fails the audit (no 500; the audit completes with the
     section's outcome and reason). Every membership test on a value from Google checks that it is a string first;
     and if a malformed answer still raises something other than `GscError` (a parser gap), the section is FAILED
     `GSC_PROTOCOL_ERROR` with `error_type` in its facts and only the exception's type logged (AEGIS gl-gsc M1: a
     list `permissionLevel`, an object `error` from the token endpoint and a 400-digit metric used to end the whole
     audit `RUN_FAILED`).

GSC-5. **Tenant binding.** `POST /seo/v1/tenants/{tid}/gsc-property` (Andre's gate: dashboard caller plus his approval
token) binds a tenant to `sc-domain:<domain>` or `http(s)://<domain>/…` where the domain is one of the tenant's own
registered domains or its `www.` twin (`GSC_PROPERTY_INVALID` otherwise); `null` unbinds. A property, or one
overlapping it, belongs to one tenant (`GSC_PROPERTY_TAKEN`; registering the domain itself to a second tenant was
already `DOMAIN_TAKEN`). Recorded as `tenant_gsc_property_set` (the property's SHA-256 on the ledger). A read uses
only the binding in state — no request can name a property — and a binding whose domain the tenant no longer holds is
not read (`GSC_PROPERTY_UNBOUND`). The connector compares sites.list against that one property and never stores or
returns the list (which would show other clients' properties). The hub reaches only its own tenant's audits (the
existing scoping; tested).

GSC-6. **Data, PII, retention, evidence.** Three queries per read, all `type=web`, `dataState=final`, window
`SEO_GSC_WINDOW_DAYS` (28) ending three Pacific-Time days ago: (1) `date` aggregated byProperty — the property totals
by date and, summed, the window totals (CTR recomputed from sums, never averaged; position impression-weighted over
the dates, labelled an approximation); (2) `page` aggregated byPage — each URL reduced to a template by the log
templater (`agents.logs.template_path`: e-mails, UUIDs, numbers, ids, tokens, file names replaced) and then the slug
rule (`search_performance.slug_segment`, AEGIS gl-gsc L1): every segment that is not empty, a placeholder, one plain
word of ASCII letters or such a word with a static extension becomes `{slug}` (`/users/jane-smith/profile` ->
`/users/{slug}/profile`; hyphenated, dotted, underscored, digit-bearing and non-ASCII segments are never kept);
CamelCase segments (`JaneSmith`) are `{slug}` too; literal words are lower-cased and the trailing slash dropped, so
one page is one template; `{offsite}` for a host not the property's; a template that still holds a literal word but
was produced by fewer than 3 distinct PAGES (`TEMPLATE_MIN_URLS`; a page is `page_key`: the path decoded once,
lower-cased, without its trailing slash, host / www / scheme / port / query ignored — AEGIS gl-gsc certification:
counting URL rows let one page reported under www, http and https keep its name) is counted in `{other}`, so no kept
template describes one or two pages
(placeholder-only templates such as `/` or `/{slug}` identify nothing and are kept); at most 200 templates, the rest
(fewest clicks first) in `{other}`; reported as by-page sums that are NOT comparable with property totals; (3) `query` aggregated byProperty — each query classified into `pii_suspect`, `brand`, `question`,
`local`, `commercial`, `other` (versioned `2026-10-24.3`) and dropped. `pii_suspect` is judged first, on the shared
lookalike fold after invisible characters are removed: an e-mail in any spelling, ONE phone-like run of 7+ digits
(digits joined only by phone separators, up to five in a row: space, dot, parentheses, slash, underscore, comma, plus,
the ASCII hyphen, the Unicode hyphens and dashes U+2010..U+2015, the minus sign and the middle dot — `555 010 0199`,
`(555) 010-0199`, `+44 20 7946 0958`, `555/010/0199`, `555–010–0199`, `555 -- 010 -- 0199`; ISO dates such as
`2024-10-24` are taken out first; in a run with two or more year-like groups, 19xx / 20xx, those groups do not
count; AEGIS gl-gsc certification L4 widened the separators and added the date rule), or a word the templater would
replace (a word of digits and separators only is left to the run rule). Digits in different words are never added
up (AEGIS gl-gsc L4: `iphone 15 pro 256gb 2024`, `24/7 plumber 90210` and `covid 19 2020 2021` used to be
`pii_suspect`); a phone number with one year-like group (`555 010 2024`) is still caught, one with two is not. No query text, hash of a query, URL or per-query / per-URL row is stored — a page is
stored only as the template above, which keeps plain one-word path segments (`/pricing`, `/users`) when 3 or more
URLs share the template: dropping beats hashing, since a hash of a low-entropy name or e-mail falls to a dictionary. The query remainder (`unattributed`:
anonymised queries and rows past the caps) is reported, never spread over the classes. Evidence class OBSERVED,
first-party, data class measured, effect class null; `headline_score` is null; the section carries the Goodhart
pairing (revenue NOT_CONNECTED -> decision INSUFFICIENT_EVIDENCE) and produces no findings. Retention: the aggregates
live in the audit report (append-only log; nothing identifying, so nothing to shred, decision 18); after
`SEO_GSC_RETENTION_DAYS` (90) a report's search-performance section is served as window totals only — by the audit
view, by drift (which compares totals only) and by `GET /audit/export` (AEGIS gl-gsc L2: the export used to return
the breakdowns of expired reports).

GSC-7. **Wiring.** Audit: Osei's `search_performance` step runs after Naomi (the run guard first: a restricted Osei is
RESTRICTED); the report gets `search_performance` = {outcome, reason, read_started, section}. **The section's outcome
is not part of the audit's overall outcome** (AEGIS gl-gsc M2): `report.outcome` is the worst outcome over every
other envelope (`svc_audits.overall_outcome`, stated in `report.outcome_rule`), so OK, `provider:gsc` engaged, a spent
read budget or a Google outage leave the overall outcome exactly what it would be without Search Console; the
section keeps its own outcome and reason in its envelope and in `report.search_performance`. Record-first:
`gsc_read_started` (tenant, audit, property SHA-256, window) is committed to the ledger and the log BEFORE any request
leaves (tested: the fake Google sees the event already recorded on its first request); a read that cannot be recorded
is not made (BLOCKED with the ledger's reason); the result's SHA-256 is recorded beside the report as
`gsc_read_recorded`. Osei's drift: `drift.compare` adds `search_performance` with rules `2026-10-24.2` (AEGIS
gl-gsc L3) — SP0 NOT_CONNECTED (either run had no key, no binding, or predates the section: nothing was measured),
SP1 TOOL_FAILURE (either run tried and did not read: KILLED, BLOCKED, FAILED, RESTRICTED), SP2 MEASUREMENT_ERROR
(property, window length, data state, type or rules version moved), SP3 overlapping windows: with `o` of `d` days
shared (`windows_overlap_days`), final data for a shared day is the same in both reads, so the difference of the
totals is exactly new-only days minus old-only days — two Poisson counts over `d - o` days; under "no change" its
variance is estimated as (before + after)·(d - o)/d and the band is 2·sqrt of that (o = 0 gives the plain
2·sqrt(before + after)); with fewer than 7 fresh days (`SP_MIN_FRESH_DAYS`; a daily schedule over a 28-day window has
one) the metric is UNKNOWN, not compared, since a day or two is dominated by day-of-week effects; inside the band
SAMPLING_NOISE, SP4 outside it REAL_CHANGE (observed; no cause claimed). Real counts are over-dispersed relative to
Poisson, so REAL_CHANGE means "outside Poisson noise", not "significant"; and if Google revised a shared day the
revision shows as a difference. Drift uses only what survives retention (property, window, totals, versions), and the
API and the scheduler both compare the retained view, so one pair gets one classification whichever path asks and
whether or not the breakdowns have expired. The decision stays INSUFFICIENT_EVIDENCE whatever the class. The department scorecard
keeps Osei's main card for data hygiene and adds a `search_performance` sub-card. `/status` shows `search_console`
(state, service-account e-mail, scope, hosts, switch, caps, budget, tenants bound, requests made). Kill switch
`provider:gsc` (also `SEO_KILLED_PROVIDERS=gsc`), with global / write / tenant / `capability:audit` as for every step,
checked before the read, before every request and on every socket operation. Budget: `SEO_GSC_READS_PER_DAY` (4) per
tenant in any 24 hours counted from recorded `gsc_read_started` on the injected clock — scheduled re-audits count
too, so a schedule can never exceed it (over budget: BLOCKED `GSC_BUDGET_EXHAUSTED`, no request, the audit still
runs). At the defaults one read costs at most 30 requests, far under Google's per-site 1,200 QPM.

GSC-8. **Dependency.** `cryptography==50.0.1` added to seo-py's requirements for RS256 — the exact pin security-py
already carries (and delivery-py's frozen lock), so no new package or version enters the repository; hand-rolled RSA
on the standard library was not an option. AEGIS gl-gsc (info) suggested 50.0.2: not bumped on this branch, because seo-py alone moving
would put a second cryptography version in the repository; the bump belongs in one change that moves security-py's
pin, regenerates delivery-py's hashed `uv.lock` and reruns all three suites.

GSC-9. **The key on Railway.** Railway variables are environment strings, and this service reads the key only from a
file (the *_FILE rules). Store the JSON key base64-encoded in a Railway variable (e.g. `SEO_GSC_SERVICE_ACCOUNT_B64`;
the service never reads it) and make the start command write it to a 0600 file before exec'ing the service:
`sh -c 'umask 077 && mkdir -p /tmp/seo-secrets && printf %s "$SEO_GSC_SERVICE_ACCOUNT_B64" | base64 -d > /tmp/seo-secrets/gsc.json && unset SEO_GSC_SERVICE_ACCOUNT_B64 && SEO_GSC_SERVICE_ACCOUNT_FILE=/tmp/seo-secrets/gsc.json exec python3 -m api'`
(from `services/seo-py/src`). The file is owned by the service user and 0600 (umask), so the start-up checks pass,
and the variable is gone from the service's environment. Railway's own secret-handling features were not checked in
this build; whatever it offers for sensitive variables should be used for this one.

GSC-10. **Andre's steps** (nothing here can be done for him; the connector stays NOT_CONNECTED until step 4):
   1. Google Cloud console: create a project for this (e.g. "zbm-seo-search-console").
   2. APIs & Services -> Library: enable the **Google Search Console API** for that project (without it Google answers
      403 accessNotConfigured, which this service reports as `GSC_AUTH_REFUSED`).
   3. IAM & Admin -> Service accounts: create a service account (no project roles are needed; skip that step). On it,
      Keys -> Add key -> Create new key -> **JSON**. The download is the only copy. Note its `client_email`.
   4. Railway: add the key as a secret variable and the start command of GSC-9; deploy. Delete the downloaded file
      from the laptop once Railway holds it (a lost key is replaced by creating a new one and deleting the old).
   5. Check `GET /seo/v1/status`: `search_console.state` is `connected` and `service_account` shows the e-mail.
   6. For each client: the client (an owner of the property) opens Search Console -> the property -> Settings ->
      Users and permissions -> Add user, pastes the service-account e-mail and picks **Restricted** (Google's table
      gives Restricted users the Performance report). If Search Console refuses it as a user (U1), stop and decide
      whether to ask for a delegated owner instead (pending default 18).
   7. Bind the tenant: `POST /seo/v1/tenants/{tid}/gsc-property` with `{"request_id": …, "property": …}` written
      exactly as Search Console shows the property (`sc-domain:example.com` or `https://www.example.com/`), with his
      approval token.
   8. Run an audit; the report's `search_performance` should say OK. `GSC_PROPERTY_NOT_GRANTED` = step 6 not done or
      not yet effective; `GSC_AUTH_REFUSED` = steps 2-4.
   Key rotation: create a new key, update the Railway variable, redeploy, delete the old key in the console.

GSC-11. **Tests.** `tests/test_gsc.py` (count: `docs/test-counts.md`; no network, injected clocks): the JWT verified with a key generated at
run time (never in the repository); the token request; cache and refresh; Google's 401 / 403 reasons / 404 / 429 /
5xx; retries stopping at the deadline; the request budget; pagination (exact multiples, caps, truncation, repeats)
and totals (CTR, position, zero impressions, remainder, rows exceeding totals, missing dates); 17 malformed answers;
gzip, oversized and endless bodies; a drip on the injected clock through MockTransport AND through the production
deadline backend over a fake socket layer (TLS SNI is Google's name, the connect goes to the resolved address, every
read bounded); DNS inside the deadline and non-global answers; the allowlist; a key that never leaks (errors, reprs,
pickling, the log, the ledger, /status, the export); Andre-only binding; cross-tenant refusal; PII queries dropped;
the kill switch before and mid-read; the budget including scheduled re-audits; no record, no request; retention;
the SP rules. `tests/test_aegis_gl_gsc.py`: the AEGIS gl-gsc findings (M1, M2, L1-L6), each failing on 0c9a96a. Live run (W3-1): see GSC-1 and the README. War room (W3-3): five `gsc-*` scenarios, 33 cases, against a
hostile fake Google; seeds 1-3 pass.

GSC-12. **Limitations (honest).**
   - Never run against Google: no credential exists. Everything above is proven against fakes built from the pages in
     GSC-2; the first real read will be the first contact. U1 is unverified.
   - Strictness has a cost: a new top-level field from Google, or a row Google sends with clicks > impressions, stops
     the read visibly (`GSC_PROTOCOL_ERROR`) instead of being half-read.
   - Names in queries are not detected as such; they are safe only because no query text is kept. Page templates
     collapse every multi-word slug to `{slug}` and send templates of fewer than 3 URLs to `{other}`, but a plain
     one-word segment shared by 3 or more URLs is kept as written: a site whose paths name people with single words
     (`/users/jsmith/{slug}` across three posts) can still show such a word. The threshold of 3 is this service's
     choice, unmeasured.
   - The page-template and query-class breakdowns cover the window, not each date; the query-class split is
     heuristic and uncalibrated; brand detection uses the tenant's domains and entity name only.
   - Position over the window is an impression-weighted mean of Google's daily positions, not Google's own
     aggregate.
   - The budget, caps and window are defaults nobody has measured against a real client (pending default 17).
   - Resolver threads: as for Finance, a hung system resolver keeps its gated thread until the C library gives up;
     with both slots held, reads are `GSC_UNAVAILABLE`.
   - The append-only log keeps the aggregates past the retention period (served as totals only); purging needs a new
     log epoch (decision 18).

Founder-pending defaults added by the Search Console build: (17) 4 reads per tenant per day, a 28-day window ending 3
days ago, 30 requests per read, 5,000-row pages, 10 pages, 50,000 rows, 90-day retention of breakdowns; (18) the
service account is added as a Restricted user, and a delegated owner only if Andre decides so after U1; (19) the
six query classes and the PII rule are this service's, versioned, and drop rather than hash.

## Wave 4 (go-live plan days 4-7; built Oct 24 2026 on `golive-perplexity`): the Perplexity answer-engine adapter

Flag 1 is now Andre's decision: Perplexity is the first answer engine. The adapter ships NOT_CONNECTED: with no key the
engine answers NOT_CONNECTED and the service starts exactly as before. With a key and no budget it is connected but
asks nothing. Andre installs the key and the budgets on Railway (W4-6).

W4-1. **The Perplexity API, verified on Oct 24 2026** (docs.perplexity.ai, read through the web; nothing was called):
   - Request: `POST https://api.perplexity.ai/v1/sonar` with `Authorization: Bearer <key>` and
     `Content-Type: application/json`; body `model` and `messages` (`role`, `content`)
     (<https://docs.perplexity.ai/docs/sonar/quickstart>). The other request fields include `stream`, `max_tokens`,
     `temperature`, `top_p`, `response_format`, the search filters and `web_search_options` (`search_context_size`,
     `user_location`). This adapter sends only `model`, `messages` (one fixed system line and the prompt) and
     `stream: false` (<https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/how-to.md>).
   - **Sonar Chat Completions support ended on September 27, 2026.** Synchronous and streaming requests still work:
     Perplexity reformulates them as Agent API requests (`POST https://api.perplexity.ai/v1/agent`). Asynchronous Sonar
     requests are no longer supported (<https://docs.perplexity.ai/docs/sonar/overview>). This adapter makes
     synchronous requests only. Moving to the Agent API is follow-up work (W4-8).
   - Models: `sonar`, `sonar-pro`, `sonar-reasoning-pro`, `sonar-deep-research` (the how-to above). The allowlist here
     is closed: `sonar` (the general model, the default) and `sonar-pro`.
   - Response: `id`, `model`, `created`, `object` (`"chat.completion"`), `choices[]` (`message.role`,
     `message.content`, `finish_reason`), `citations` (a list of URLs), `search_results[]` (`title`, `url`, `date`,
     `last_updated`, `snippet`, `source`, `place_metadata`) (the quickstart, and
     <https://docs.perplexity.ai/docs/sonar/openai-compatibility>). The documented examples elide some fields with
     `// ...`.
   - **`citations` is removed, and the reference still shows it (re-checked Oct 24 2026 after AEGIS H2).** The
     changelog's May 2025 entry "Enhanced API Responses with Search Results" adds `search_results` ("All
     applications should now use the `search_results` field") and was updated: "The `citations` field has been fully
     deprecated and removed." (<https://docs.perplexity.ai/docs/resources/changelog>). The Sonar API reference example
     (<https://docs.perplexity.ai/api-reference/sonar-post>) and the Sonar migration page
     (<https://docs.perplexity.ai/api-reference/chat-completions-post>) still show `citations` next to
     `search_results` (`title`, `url`, `date`, `last_updated`, `snippet`, `source`, `place_metadata`), and neither
     states which fields are required. The two sources contradict each other, so the parser takes citation URLs from
     `search_results[].url` first and from `citations` only when present; neither is required.
   - The same pages (re-checked Oct 24 2026): "Sonar Chat Completions support ended on September 27, 2026."
     "Synchronous and streaming requests keep working: they are being reformulated as Agent API requests, rolling out
     gradually by model." "Asynchronous Sonar requests are not reformulated and are no longer supported." The Sonar
     example still posts to `https://api.perplexity.ai/v1/sonar`; no page says that endpoint stops answering, and
     none documents the response shape of a reformulated request
     (<https://docs.perplexity.ai/api-reference/chat-completions-post>,
     <https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/how-to>). The current endpoint is therefore still
     usable, and this adapter stays on it.
   - Model names: the Sonar examples show `sonar`. `perplexity/sonar` appears on none of the pages read (the
     changelog shows the `perplexity/` prefix only for the Decisions API's `perplexity/pplx-decider-*`), so it is
     not accepted: an answer reporting any model outside the allowlist stays ENGINE_PROTOCOL_ERROR
     (`MODEL_NOT_ALLOWED`) until the docs or the first real call (W4-6 step 5a) show otherwise.
   - Fields seen in the docs that the adapter does not read, and so ignores: a choice's `delta`, `images`,
     `related_questions`, `effort` / `preset` (Agent API, Oct 2026), a search result's `id` (Agent API search results:
     `id`, `title`, `url`, `snippet`, `date`, <https://docs.perplexity.ai/docs/cookbook/articles/streaming-citations/README>)
     and `usage.tool_calls_details` (changelog, Oct 2026).
   - Usage and cost: `usage.prompt_tokens`, `completion_tokens`, `total_tokens`, `search_context_size` and
     `usage.cost.{input_tokens_cost, output_tokens_cost, request_cost, total_cost}` in US dollars. Perplexity announced
     these on Aug 1 2025 (<https://community.perplexity.ai/t/cost-information-now-included-in-api-response-json/833>).
     The current Sonar pages do not show `usage`. The parser also accepts, as optional fields that are never required,
     `citation_tokens`, `num_search_queries`, `reasoning_tokens` and the matching `*_cost` fields. Those come from
     knowledge of the reasoning and deep-research models, and **this session did not verify them**.
   - Rate limits: a limited request gets HTTP 429 with `Retry-After` and no `x-ratelimit-*` headers. `error.type` is
     `request_rate_limit_exceeded`, `token_rate_limit_exceeded` or `overloaded`. For request limits `Retry-After` is
     normally 1 second, and for token limits it includes up to 10 seconds of random delay. Perplexity advises backoff
     with jitter, and a rejected request still counts toward the limit
     (<https://docs.perplexity.ai/docs/admin/rate-limits-usage-tiers.md>). That page lists Agent API QPS by tier
     (tier 0: 1, up to 33 at tiers 4-5) and **gives no Sonar-specific figure**.
   - Errors: the docs index (<https://docs.perplexity.ai/llms.txt>) has no Sonar error-code reference. Mapping 401
     and 403 to ENGINE_AUTH_REFUSED is the plain HTTP meaning. Mapping **402 is this service's assumption** (an account
     that cannot pay).
   - Pricing: the current pricing page (<https://docs.perplexity.ai/docs/getting-started/pricing.md>) does not list
     Sonar model prices. So the budgets are set in requests and dollars, with a conservative reserve per request, and
     the reported `total_cost` is used wherever the response carries one.
   - Data retention (<https://docs.perplexity.ai/guides/privacy-security>): "Perplexity does not use Customer Content
     to train generative AI models". Zero Data Retention is available only to eligible customers as an account-level
     setting and is not the default. Perplexity logs usage metadata (token counts, model, timestamps, duration, the API
     key). Some third-party model providers require limited retention. Perplexity cites SOC 2 Type II, a 2025 HIPAA gap
     assessment and CAIQlite. The page states no retention period for prompts or outputs.
   - Keys (<https://docs.perplexity.ai/docs/admin/api-key-management>,
     <https://docs.perplexity.ai/api-reference/generate-auth-token-post>): a key is created in the console or with
     `POST /generate_auth_token` (an optional `token_name`) and shown once. It is revoked with `/revoke_auth_token`.
     **No expiry and no scope are documented**. Perplexity suggests rotating keys, for example every 90 days.

W4-2. **The adapter** (`src/perplexity.py`) and its runs (`src/svc_engines.py`):
   - Calls are fixed: one base URL, one path, one method. Redirects are never followed, and no proxy is taken from
     the environment (`trust_env=False`). An answer from a model outside the allowlist is ENGINE_PROTOCOL_ERROR.
   - Each question has one HARD overall deadline, `SEO_PERPLEXITY_TIMEOUT_SECONDS` (default 30), covering every
     attempt. The production transport routes DNS, connect, TLS and every read and write through
     `_GuardedDeadlineBackend`, the Finance client's deadline-backend pattern (W3-5 M1):
     - the name is resolved inside the deadline, on the resolver gate pattern of W3-6, with its own gate of two;
     - every resolved address must be public, so a resolver that answers a private or metadata address is refused
       before any connect;
     - the kill-switch guard is checked before every socket operation, and again once the response is in (a switch
       engaged during the last read drops the answer; the request stays charged);
     - each socket read waits at most `READ_SLICE_S` (2 s) before the guard is checked again, within the deadline, so
       a switch engaged while Perplexity is still composing stops the request within about 2 s (AEGIS L1);
     - the body loop stops at the deadline.
   - The response is bounded: at most 512 KiB, a declared length over that is refused, identity coding only,
     `application/json` only. Duplicate JSON keys, NaN and wrong types are refused (pydantic strict). Strictness covers the
     fields the adapter relies on: the answer text, the model, every citation URL and the usage and cost numbers. A
     field it does not model is ignored and dropped (never read or stored), because the API adds fields over time and
     a known-but-irrelevant field must not stop the run (AEGIS H2; an earlier build refused unknown fields at every
     level). `citations` is optional, and so are `search_results`, a choice's `index` and the token counts.
   - **A run stops spending on an unreadable shape.** Two ENGINE_PROTOCOL_ERRORs with no answer between them (across
     prompts; only an answer resets the count, a 503 or a transport error between them does not: AEGIS round 2) stop
     the engine for that run, detail `REPEATED_PROTOCOL_ERRORS`.
   - **Drift is visible** (AEGIS round 2). Because unmodelled fields are ignored, a moved citations field would
     otherwise look like answers that cite nothing. Each answer carries the NAMES (never the values) of the
     top-level keys the parser does not model (at most 20; a name that is not a plain identifier is `<other>`), and
     the engine's facts carry `drift`: those names, `zero_source_answers` and `answers`, plus `zero_source_answers`
     per prompt. When every answer had no source, Callum adds a finding `ENGINE_ANSWERS_WITHOUT_SOURCES` with the
     key names. The smoke helper prints `zero_sources` and `unmodelled_keys`. A run that ends
     with errors and no answer at all is `FAILED` (detail `NO_ANSWERS`, reason the most frequent error code), never
     `OK`.
   - Retries are bounded (`SEO_PERPLEXITY_MAX_ATTEMPTS`, 1..3) and happen only on 429, 5xx and transport errors.
     `Retry-After` in whole seconds is honoured when it fits in the time left. Otherwise the answer is given at once
     (ENGINE_RATE_LIMITED), so the adapter never sleeps past the deadline and never waits more than 30 s. Without
     `Retry-After` the backoff is 0.5 s then 1.5 s, plus up to 25% jitter.
   - The reason codes are a closed set: ENGINE_AUTH_REFUSED, ENGINE_RATE_LIMITED, ENGINE_UNAVAILABLE,
     ENGINE_PROTOCOL_ERROR, ENGINE_BUDGET_EXHAUSTED, ENGINE_KILLED, ENGINE_NOT_CONNECTED. An error body is never read.
     After an auth refusal, exhausted rate-limit retries, an exhausted budget or a switch, the run stops asking that
     engine; the remaining prompts are reported as `prompts_not_asked`.
   - **Budgets fail closed, and 0 means OFF.** There are four limits:
     - `SEO_PERPLEXITY_DAILY_CALLS` and `SEO_PERPLEXITY_DAILY_SPEND_USD` (global);
     - `SEO_PERPLEXITY_TENANT_DAILY_CALLS` and `SEO_PERPLEXITY_TENANT_DAILY_SPEND_USD` (per tenant).

     All four default to 0, and while any one of them is 0 nothing is asked (ENGINE_BUDGET_EXHAUSTED, detail
     `BUDGET_OFF`). Budgets count per UTC day of the service clock.
   - **Record-first reservations.** Before a prompt's requests, the service commits `engine_calls_reserved`
     (samples x attempts requests, each at `SEO_PERPLEXITY_RESERVE_USD_PER_CALL`, default $0.05). The commit order is
     typed evidence on the ledger, then the log line, then memory, and the reservation must fit the global and the
     tenant's remaining day. A request is never sent without one: the meter refuses a request beyond the reservation's
     count or spend. After the engine's prompts, `engine_run_settled` records:
     - the requests made;
     - the spend: the reported cost where Perplexity gave one, and the full reserve for any request whose cost is
       unknown or reported as 0, including failures (a request is never free here, so the spend budget always binds;
       AEGIS L2);
     - a single request whose reported cost exceeds 4x the reserve per call (`COST_GUARD_MULTIPLE`) stops the run
       (`ENGINE_BUDGET_EXHAUSTED`, detail `COST_ABOVE_RESERVE`) and adds a finding `ENGINE_COST_ABOVE_RESERVE`: the
       cost is only known after the request, so the guard limits the damage to that one request (AEGIS round 2);
     - the answers and the stop reason.

     A run's usage is its settlement, or the sum of its reservations while unsettled. A process that dies mid-run
     leaves its reservations charged, and they are replayed from the log at start.
   - Kill switches: `provider:perplexity` and `capability:ai_probe`, plus the run's tenant, write, audit and global
     switches. They are checked at the reservation, before every request and before every socket operation. A switch
     engaged mid-request stops the engine at its next socket operation (Callum KILLED).

W4-3. **What is stored, and what is sent.**
   - The answer text is scanned in memory (brand mention, injection-like phrases) and dropped. A report keeps, per
     prompt:
     - the answers' SHA-256;
     - the cited hosts (the top 10, with counts);
     - whether the tenant was cited, and at which 1-based position in Perplexity's own citation order;
     - lookalike counts, `errors_by_reason`, usage and cost.
   - Citations come only from `citations` and `search_results`. A URL that the model wrote into its text is not a
     citation, because a crawled page could have talked the model into writing it.
   - Domain matching (`agents/domain_match.py`) compares IDNA A-labels by exact host, the `www.` twin or a subdomain,
     never by substring: `evilsite.test` and `site.test.evil.test` are not `site.test`. A host whose Unicode form folds
     to the tenant's domain on the shared lookalike table (`lookalikes.py`, e.g. a Cyrillic `і`) is counted as a
     `lookalike_citation` and never as the tenant.
   - IDNA (AEGIS M1): Python's codec is IDNA2003, which maps `ß`→`ss`, `ς`→`σ` and drops ZWJ and soft hyphens.
     A host counts as a name only when every label survives the IDNA round trip unchanged (after NFKC and lower
     case) and it holds no format, default-ignorable, control or space character. Any other host is never the tenant
     (`faß.de` is not `fass.de`); when its loosened form folds to the tenant it is a lookalike. No new dependency.
     The cost: a tenant domain that needs `ß` or a final sigma matches nothing.
   - Public suffixes (AEGIS L3): a tenant may not register a public suffix (`github.io`, `co.uk`, `vercel.app`, ...:
     422 `DOMAIN_INVALID`), and a public-suffix tenant domain from an older log matches nothing, so one customer can
     never claim every `*.github.io` site. Refusal at registration was chosen. Since AEGIS round 2 the WHOLE
     Public Suffix List is vendored (`agents/public_suffix_list.dat`, unchanged, MPL-2.0 header kept; source
     <https://publicsuffix.org/list/public_suffix_list.dat> as packaged by nixpkgs, snapshot of Dec 28 2025, sha256
     `25d100199dd39263ee00b2b8a125a6a121905cefc814a7679ed3bbe135748ade`), implemented with its wildcard and exception
     rules and the implicit `*` rule, ICANN and PRIVATE sections both, no new dependency. Thirteen platform domains the
     PSL does not list (`amazonaws.com`, `wordpress.com`, `railway.app`, ...) stay refused as a documented local deny
     list. Refresh: replace the file whole from that URL, update the date and hash in `domain_match.py` and here, run
     `tests/test_perplexity.py`.
   - A citation URL holding a backslash, an ASCII control character or whitespace is refused before parsing
     (AEGIS M2: `https://evil.com\@acme.com/` parses as host `acme.com` but a browser goes to `evil.com`).
   - Prompts carry no personal data (`agents/prompt_guard.py`). A prompt set holding an e-mail address (any at-sign
     spelling, after the shared fold, including `[@]`, `&#64;` and "john at example dot com"), seven or more digits
     however separated (spaces, commas, underscores, every Unicode dash and connector), or an IPv4 address is refused
     422 `PROMPT_PERSONAL_DATA`. Allowed on purpose (AEGIS M3, tightened in round 2): a 4-digit year range (`2025-2026`,
     `2025 2026`), a ROUND count (1-3 digits then only `000` groups, e.g. `1 000 000`, with no other digit within
     three characters; `1 234 567` and `0612 345 678` are numbers) and a two- or three-part version (`v1.2.3`). A
     dotted quad that could be an address is refused whatever precedes it (`version 192.168.0.1` included). Prose
     such as "listed at yelp dot com" is not taken for a spelled-out address (a short list of words prose puts
     before "at").
     **Personal names and street addresses are not detected**: no pattern tells them from a business or a place, so
     prompt sets stay staff-written generic market questions. A stored prompt is checked again before sending and is
     skipped, unquoted in the report.
   - The request carries only the prompt and one fixed instruction: no tenant, brand, domain, user identifier or
     safety identifier.

W4-4. **Wiring.**
   - The probe framework (Callum) asks a METERED port only through the service's budget hook (`engine_budget`);
     without one the engine is BLOCKED.
   - Audits and scheduled re-audits take the same path (`_run_audit`), so both are budgeted and recorded alike.
   - Naomi stays NOT_CONNECTED, because Perplexity is not a prompt-volume source.
   - `/status` adds `answer_engines`: per engine its state, model, limits, the key's SOURCE (`file` or `env`, never the
     key), the budgets with today's usage, and whether a switch covers it.
   - Callum's scorecard adds `engine_runs`, built from the recorded reservations and settlements only: runs,
     settled, unsettled, requests, answers, spend and stop reasons.
   - The new ledger evidence types are `engine_calls_reserved` and `engine_run_settled`, carrying ids, counts,
     Decimal strings and hashes only.
   - `SEO_PERPLEXITY_API_KEY_FILE` left the NOT_BUILT list.

W4-5. **Key handling, and the controls that compensate for a long-lived key.**
   - Sources:
     - `SEO_PERPLEXITY_API_KEY_FILE`, the repository's `*_FILE` rule: a regular file owned by the service user, mode
       0600 or 0400, no symlink, no FIFO.
     - `SEO_PERPLEXITY_API_KEY`, a plain variable, because Railway service variables are environment variables and
       Railway mounts no secret files. The repository already takes the Finance service and caller tokens as plain
       variables, so this follows existing practice.

     Setting both refuses start. The key must be 20..512 printable characters and differ from every token the
     service accepts or sends.
   - The key is held in `ApiKey`: its repr is masked, it cannot be pickled, and `reveal()` is called in one place,
     the Authorization header. It never reaches a log line, an exception, `/status`, a report, the log or the ledger.
     A test scans every one of those, plus the data directory, after success, 429, 5xx, a malformed body echoing the
     key and a 401. The live run scans the ledger, the data directory and the process output.
   - Perplexity documents no scoped, short-lived or expiring keys, so the security model's requirement cannot be met
     as written. The compensating controls are:
     1. the key can only ever go to `api.perplexity.ai`, which must resolve to public addresses: no redirect, no
        proxy, a fixed path;
     2. spend is capped by four fail-closed budgets, every request is reserved on the ledger first, and requests of
        unknown cost are charged in full;
     3. a dedicated key per environment, named for its use (`token_name`), so revoking it touches nothing else;
     4. rotation every 90 days and revocation on any suspicion (W4-6);
     5. kill switches, including the start-time-sticky `SEO_KILLED_PROVIDERS=perplexity`;
     6. prompts free of personal data, so a leaked key exposes no client data through the request history;
     7. the file form preferred wherever the platform has secret files.

     An env-var key is readable by anyone who can read the process environment or the Railway project's variables.
     That is an accepted residual risk, stated here.

W4-6. **How Andre installs the key on Railway** (his steps; nothing here is automated):
   1. In the Perplexity console (API settings), create a key named for this use, e.g. `zbm-seo-railway-prod`. Copy
      it once; it is not shown again.
   2. In Railway, open the seo-py service, then Variables. Add `SEO_PERPLEXITY_API_KEY` with the key, and do NOT also
      set `SEO_PERPLEXITY_API_KEY_FILE`.
   3. In the same place, set the four budgets (all four above 0, or nothing is asked). A cautious start, Andre's call:
      - `SEO_PERPLEXITY_DAILY_CALLS=200`
      - `SEO_PERPLEXITY_DAILY_SPEND_USD=5`
      - `SEO_PERPLEXITY_TENANT_DAILY_CALLS=100`
      - `SEO_PERPLEXITY_TENANT_DAILY_SPEND_USD=3`

      Leave `SEO_PERPLEXITY_MODEL` unset (`sonar`) and `SEO_PERPLEXITY_RESERVE_USD_PER_CALL` unset ($0.05) unless he
      decides otherwise.
   4. Redeploy. On the dashboard, `GET /seo/v1/status` must show `answer_engines.perplexity.state = "connected"`,
      `key_source = "env"` and `budget.state = "on"`. Any configuration error refuses start, with a message that never
      contains the key.
   5a. **First real call, before any budget above the cautious start** (AEGIS H2). The parser has never seen a real
      response (W4-8), and Perplexity is reformulating Sonar requests as Agent API requests model by model. So the
      first contact is ONE request, by hand: on a machine (or a Railway shell) with the key, run
      `python3 devtools/smoke_one_call.py --one-real-call` from `services/seo-py`. It makes exactly one request (one
      attempt, a meter that allows one, the usual deadline), prints the outcome, the model reported, the answer's
      length and hash (never its text), the number of citation URLs and their first hosts, and the usage and cost, and
      never prints the key. `ANSWER` with citations > 0 and a plausible cost: go on to step 5. `ENGINE_PROTOCOL_ERROR`
      (or `MODEL_NOT_ALLOWED`): the response shape or model changed; keep the budgets where they are (or at 0),
      do not raise them, and have the parser updated from the docs first. Any other code: fix that first (key,
      account, rate). Alternatively, start with every budget at its smallest useful value (e.g. 9 calls and $1 per
      day, global and tenant) and run step 5 once with a one-prompt set.
   5. First run: create a prompt set for `zbm` with `"engines": ["perplexity"]` (generic market questions, no names
      or numbers), then request an own-properties audit with its `prompt_set_id`. Read Callum's facts in the report:
      class, citation rate and interval, position, usage and cost. Then check `answer_engines.perplexity.budget` in
      `/status`.
   6. To stop it: engage `provider:perplexity` on `POST /seo/v1/kill-switches` (the dashboard or Compliance; only
      Andre releases it), or set `SEO_KILLED_PROVIDERS=perplexity` and redeploy (sticky).
   7. To rotate (every 90 days): create a new key, replace the variable, redeploy, check `/status`, then revoke the
      old key in the Perplexity console. If a leak is suspected, engage the switch first, revoke at once, then install
      a new key.

W4-7. **Proof.**
   - `tests/test_perplexity.py` uses no network: a fake Perplexity behind `httpx.MockTransport`, and, for the socket
     layer, a fake network backend under httpcore's real connection pool. All deadlines run on an injected clock. It
     covers:
     - the documented request;
     - strict parsing, with 19 malformed shapes;
     - huge, endless, encoded, mistyped and redirected bodies;
     - 401, 402 and 403; 429 with `Retry-After`; 5xx then success; retries exhausted; transport errors;
     - slow drip, both through the transport and through the socket;
     - a slow connect, and a private resolver answer;
     - the production transport installing the guarded backend;
     - a switch engaged mid-request;
     - the meter;
     - key masking, and the key-file rules;
     - NOT_CONNECTED with no key, and budgets off by default;
     - global and per-tenant budgets and their isolation, the spend budget, unknown cost charged in full;
     - reservation before each request, settlement, the scorecard and the evidence view;
     - switches before and during a run, replay after a restart, scheduled runs, cross-tenant 404s;
     - domain-match and prompt-guard edge cases, prompt injection in the answer;
     - the key-leak scan;
     - the AEGIS gl-pplx fixes (Oct 24 2026): a run crossing UTC midnight (each reservation on its own day, the
       settlement split by day, replay), the documented response variants (no `citations`, a choice's `delta`, extra
       fields, no `index`), two protocol errors stopping a run, a no-answer run FAILED, the one-call smoke helper,
       IDNA2003 mappings and invisible characters, backslash and whitespace citation URLs, the widened prompt guard,
       read slices and the post-response guard, zero cost charged at the reserve, public-suffix tenant domains, and
       one test per AEGIS mutant (all 24 of AEGIS's `mutate.py` mutants are killed);
     - AEGIS round 2 (after certification at 7942b3a): only round counts allowed and no version-masked dotted quad,
       prose "at ... dot" not taken for an address, protocol errors between 503s stopping the run, the whole Public
       Suffix List (wildcards, exceptions, both sections) plus the local deny list, the smoke helper never echoing a
       setting and holding to one request, the 4x-reserve cost guard and its finding, and drift visibility (unmodelled
       key names, zero-source answers, the finding).
   - The live run (W3-1) now runs the service under a socket guard that refuses and records any non-loopback DNS
     lookup or connection, and it ends with that record empty. It proves:
     - a key file open to group or others, or a key set two ways, refuses start;
     - NOT_CONNECTED with no key, and an audit with a Perplexity prompt set asks nothing and reserves nothing;
     - a prompt set holding personal data is refused;
     - with a key file but budgets at 0, the engine is connected and BLOCKED;
     - the key is in no ledger entry, data file or process output.
   - The war room adds eight MUST scenarios: hostile answers, the protocol attacks (relied-on fields mistyped, a
     documented variant that must be answered, no-answer runs never OK), lookalike citations, a control, switches
     before and during a run, budgets off, and a ledger outage while the reservation is recorded.

W4-8. **Limitations (honest).**
   - Nothing here has been run against the real Perplexity API: the build machine has no internet and no key. The
     response model follows the documented examples, which elide fields. Fields not modelled are ignored; a field
     the adapter relies on with another shape is ENGINE_PROTOCOL_ERROR, and two in a row stop the run. Andre's
     single smoke call (W4-6 step 5a) is the first real contact.
   - Sonar Chat Completions are past Perplexity's end-of-support date and are served by reformulating them as Agent
     API requests. Their behaviour may change. **Next step (planned, not built): migrate to `POST /v1/agent`.** The
     how-to (<https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/how-to>) maps `sonar` and `sonar-pro` to
     `effort: "fast"`, the answer to `output_text`, and citations to the `output` item of `type: "search_results"`
     (each result with an `id` that the text's `[n]` markers refer to). The work: a second request kind on the same
     fixed host (`/v1/agent`, `input` messages, `effort`), a strict model of `output_text` and the `search_results`
     output item, the usage and cost fields once the docs show them, the same deadline, budget and switch rules, and
     a new first-real-call smoke. It is not built now because the docs keep the current endpoint usable (W4-1).
   - Spend here is Perplexity's reported cost, or the reserve where none is reported. Perplexity's invoice is
     authoritative, and nothing reconciles the two.
   - A prompt's reservation is the upper bound (samples x attempts), so a run near the day's limit can be refused even
     though fewer requests would have sufficed (conservative).
   - `Retry-After` given as an HTTP date is ignored, and the backoff applies.
   - The optional usage fields and the 402 mapping noted in W4-1 are unverified.
   - Observations come from one account at one time, so engines vary by user, location and model version. Perplexity
     reports no model version, so the version is recorded as `sonar@?`.
   - Naomi has no volume source.
   - An env-var key is visible to whoever can read the environment or the Railway variables (W4-5).

Founder-pending defaults added by Wave 4 (numbered after the Search Console build's (17)-(19)):
- (20) every budget defaults to 0 (off);
- (21) the model is `sonar`;
- (22) the reserve per request is $0.05;
- (23) after exhausted 429 retries, the run stops asking Perplexity for that audit;
- (24) a plain environment variable is accepted for the key on Railway;
- (25) `sonar-reasoning-pro` and `sonar-deep-research` are not on the allowlist.

## Unlock list

Answer-engine adapters with scoped, short-lived credentials (OpenAI, Anthropic, Google); a renderer; Bing Webmaster; a
prompt-volume source; Department 28 clientfix hand-off; the remaining agents per the spec's waves; the console pages.
Search Console is built and NOT_CONNECTED until Andre's steps (section "Search Console"). (Built in Wave 3: the live
run against ledger-rust, W3-1; the Finance (31) client to verify invoices, W3-2. Built in Wave 4: the Perplexity
answer-engine adapter, W4.)
