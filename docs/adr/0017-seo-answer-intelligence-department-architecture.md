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
    NOT_CONNECTED until a service-account key is configured; section "Search Console".)
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
`{offsite}` for a host not the property's; a template that still holds a literal word but was produced by fewer than
3 distinct page URLs (`TEMPLATE_MIN_URLS`) is counted in `{other}`, so no kept template describes one or two pages
(placeholder-only templates such as `/` or `/{slug}` identify nothing and are kept); at most 200 templates, the rest
(fewest clicks first) in `{other}`; reported as by-page sums that are NOT comparable with property totals; (3) `query` aggregated byProperty — each query classified into `pii_suspect`, `brand`, `question`,
`local`, `commercial`, `other` (versioned `2026-10-24.2`) and dropped. `pii_suspect` is judged first, on the shared
lookalike fold after invisible characters are removed: an e-mail in any spelling, ONE phone-like run of 7+ digits
(digits joined only by spaces, hyphens, dots or parentheses: `555 010 0199`, `(555) 010-0199`, `+44 20 7946 0958`;
in a run with two or more year-like groups, 19xx / 20xx, those groups do not count), or a word the templater would
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
on the standard library was not an option.

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

## Unlock list

Answer-engine adapters with scoped, short-lived credentials; a renderer; Bing Webmaster; a prompt-volume source;
Department 28 clientfix hand-off; the remaining agents per the spec's waves; the console pages. Search Console is
built and NOT_CONNECTED until Andre's steps (section "Search Console"). (Built in Wave 3: the live run against
ledger-rust, W3-1; the Finance (31) client to verify invoices, W3-2.)
