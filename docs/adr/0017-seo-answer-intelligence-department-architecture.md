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
   domains; an audit of any other domain is refused `DOMAIN_NOT_AUTHORIZED`. Every object names one tenant; a
   tenant-scoped reader asking for another tenant's object gets the same 404 as for a missing one.
8. **Kill switches**, each with its own reason code and test: `global`, `write`, `tenant:<id>`,
   `capability:<fetch|render|ai_probe|audit|entity_write|prompt_sets>`, `provider:<web|openai|anthropic|google|
   perplexity>`. The dashboard or Compliance may engage; only Andre releases. An engage the ledger cannot record takes
   effect at once in memory and is shown as unrecorded. Environment switches (`SEO_KILL_GLOBAL`,
   `SEO_KILLED_CAPABILITIES`, `SEO_KILLED_PROVIDERS`) cannot be released at runtime. A run checks the live switches
   before every outbound step, so a switch engaged mid-run stops it (overall outcome KILLED).
9. **Crawled content is data, never an instruction.** Page and answer text is kept only as bounded extracts marked
   `untrusted`; nothing in it can change a decision, a state, a switch or the entity record (tested with injected
   instructions in titles, JSON-LD, llms.txt and provider answers).
10. **Propagation spine, read side.** The canonical entity record holds each field with source, provenance,
    authority, freshness (stale after 180 days unconfirmed) and history. The first record is Andre's own NAP exactly as
    stated: Z Best Media, 5318 East 2nd Street, Long Beach, CA; (562) 248-6617 — nothing he did not state is added.
    The on-site check compares a site's Organization / LocalBusiness JSON-LD and visible phone number with it; the
    record is the authority and is never written from a page.
11. **Primitives.** fetch: http/https only; DNS resolved once per hop and every address checked (loopback, private,
    link-local incl. 169.254.169.254, CGNAT, multicast, reserved, IPv4-mapped / 6to4 / NAT64); the connection made to
    the checked IP with Host and SNI carrying the name (no DNS rebinding); redirects by hand with a cap; decoded-size
    cap; per-phase timeouts plus an overall deadline; robots.txt (RFC 9309) honoured for the `ZBM-SEO-Audit` token;
    always identified. render: a separate port with explicit states (`RAW_OK_RENDER_NOT_CONNECTED`,
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
- The paid-audit flow takes an invoice id on trust from the caller; it does not verify payment with Finance (31)
  (no client built). Andre's approval is the control.
- Tests run against an in-process fixture server; nothing here has been run against a real public site yet.

## Unlock list

Answer-engine adapters with scoped, short-lived credentials; a renderer; Search Console and Bing Webmaster; a
prompt-volume source; the Finance (31) client to verify invoices; Department 28 clientfix hand-off; the remaining
agents per the spec's waves; the live run against ledger-rust; the console pages.
