# ADR 0047 — Priya, the keyword agent: the client's keyword ledger from first-party data

Status: built and tested on branch `claude/seo-priya-keywords-20261108` (from `codex/gate5-release-20261107`,
a4af277); **not in force**, review pending. Search & Answer Intelligence (2), ADR 0017 ("Priya" is one of the sixteen
single-task agents of the Oct 6 2026 spec; Wave 1 did not build her). No live call, key or spend exists for anything
below. (Numbered 0047 to stay clear of a concurrent SEO branch; renumber at rebase if it collides.)

## Decision

1. **One agent, one task.** Priya (`services/seo-py/src/agents/priya.py`) produces the tenant's keyword ledger. She
   does not crawl, rank, write, publish, buy data, estimate market volume, compute keyword difficulty or call a model.
   The service side (`src/svc_keywords.py`) does the gates, the record-first bookkeeping and the reads. Her card
   (`priya.CARD`, returned with every run) names her inputs and outputs. She is registered with the department
   manager (`svc_manager.AGENTS`) like every other agent: lifecycle, a `keyword_runs` scorecard from her recorded
   runs, and a restricted or retired Priya does not run (`AGENT_RESTRICTED`).
2. **Route.** `POST /seo/v1/tenants/{tid}/keyword-ledger` `{"request_id", "sources": ["gsc"] | ["gsc", "bing"] |
   ["bing"], "bing_site_domain"}` (201) and `GET /seo/v1/tenants/{tid}/keyword-ledger-runs`. Callers: `dashboard` and
   `seo_agent` for an own tenant; a client tenant needs Andre (dashboard + approval token), a bound Finance (31)
   client and Compliance (38)'s current activation of it (ADR 0024, asked outside the lock, no override) — the same
   gate as the client report. The hub cannot run it (a run spends a Search Console read); `compliance_38` reads the
   run list. Tenant scoping is the existing `scoped()` dependency: another tenant's run is the same 404 as a missing
   one. No request can name a Search Console property; Bing's site must be one of the tenant's registered domains
   (`DOMAIN_NOT_AUTHORIZED` otherwise).
3. **Record-first, and the ledger is not stored.** `keyword_ledger_started` (run, tenant, sources, property SHA-256,
   window, Bing site SHA-256) is on the ledger and the log before any request leaves; no record, no request
   (tested: the fake Google sees the event on its first request; a down ledger is 503 with zero requests). The ledger
   carries query text, and ADR 0017 GSC-6 keeps query text, its hash and per-query rows out of the log, so the
   ledger is computed on request, answered once (`no-store`) and never persisted: `keyword_ledger_recorded` keeps its
   SHA-256, status, outcome and counts. A replay of the same request id answers that summary with `ledger: null`
   (`REQUEST_ID_REUSED` for a changed body). A kill switch engaged during the run, or a crash, records
   `keyword_ledger_interrupted` (`KILLED_*` / `RUN_FAILED` with the exception's type only) and discards the ledger.
4. **Kill switches:** global, write, the tenant's, the new `capability:keywords`, `provider:gsc` and the new
   `provider:bing` (both also as `SEO_KILLED_CAPABILITIES` / `SEO_KILLED_PROVIDERS` at start), checked before the run,
   before every request and on every socket operation (the GSC connector's guard).
5. **Budget.** A run that reads Search Console counts as one read against its **own** budget,
   `SEO_GSC_LEDGER_READS_PER_DAY` (default 2 per tenant in any 24 hours; a whole number 1..12, anything else refuses
   start), kept apart from `SEO_GSC_READS_PER_DAY` (audits and scheduled re-audits) so ledger runs can never starve an
   audit, and audits never block a ledger (tested both ways). A run that cannot read (not connected, unbound, killed,
   over budget) spends nothing. (Review round 1, LOW a; it answers founder question 1 with a conservative default.)

## The ledger (`priya-keyword-ledger/1`, rules version `2026-11-08.2`)

Every row: `key`, `query` (normalised: NFKC, invisible characters removed, case-folded, whitespace collapsed; carried
as `observed` text, bounded and marked `untrusted` — data, never an instruction), `tier`, `origin` (observed /
derived), `intent_class` (ADR 0017's classifier), `metrics` per source, `volume_status`, `market_volume` (always
null), `opportunity`, `difficulty` (always null), `confidence`, `uncertainty_flags`, `near_duplicate`, `evidence`.

- **ESTABLISHED**: at least 20 impressions in **Search Console's** window (`ESTABLISHED_MIN_IMPRESSIONS`;
  founder-pending default). The tier is decided on Search Console's count whenever Search Console measured the query;
  Bing's rows cover their own multi-week dates, so their summed total is never compared with the 28-day threshold:
  a query only Bing measured stays EMERGING, kept as measured, `volume_status` `MEASURED_NOT_TIERED`, flag
  `BING_ONLY_UNTIERED`, confidence NONE. Confidence LOW below 100, MEDIUM below 1,000, HIGH at 1,000+ (MEDIUM at most
  when the read was truncated). Each source's numbers are kept apart and never summed: Search Console's with the
  property hash, the window (Pacific Time, final data, web, aggregated by property) and the **exact Garwood** 95%
  Poisson interval on impressions (`impressions_poisson_95`: lower chi²(0.025; 2n)/2, upper chi²(0.975; 2n+2)/2,
  rounded outward to 0.01; exact incomplete-gamma bounds up to n = 1,000, the Wilson–Hilferty form of the same
  quantiles above, which agreed with exact values within 0.003 and erred outward at every count checked from 1,001 to
  10^12); Bing's with its own date range. What the band means: impressions are the engine's full count for the
  property in the window, not a sample, so the band is not sampling error; it models period-to-period variability
  (how far such a count could move from one window to the next at an unchanged rate). It is not a forecast. The
  metric is named `impressions_on_this_property`: it is not market search volume.
- **EMERGING**: (a) observed queries below the threshold — their thin counts kept as measured, `BELOW_THRESHOLD`,
  confidence NONE; (b) derived variants with **no volume** (`UNKNOWN_NOT_MEASURED`, metrics null, flag
  `NOT_MEASURED`). Derivation is deterministic, no model: a *frame* is an observed query with one of the top 25
  established non-brand head terms (1-4 words) replaced by `{head}` (`{head} near me`, `how to choose a {head}`); a
  frame seen around at least 2 distinct heads (up to 20 frames) is applied to the other heads (up to 200 variants);
  a variant already observed is not derived again; every variant is PII-checked; its evidence names the frame's hash,
  the head and the supporting observed rows. Kinds: question, conversational (6+ words), constraint (a closed list:
  near me, under, without, best, vs, price, …), else long_tail.
- **Uncertainty flags** (closed): BELOW_THRESHOLD, LOW_IMPRESSION_COUNT, SINGLE_SOURCE, GSC_ROWS_TRUNCATED (the read
  stopped at a row cap: the tail past it is unread), ANONYMISED_REMAINDER (Search Console's property totals exceed its
  query rows; the remainder is reported in `sources.search_console.unattributed`, never spread over rows),
  NOT_MEASURED, NEAR_DUPLICATE, BING_WINDOW_DIFFERS, BING_ONLY_UNTIERED.
- **Duplicates:** spellings that normalise to one query are merged (counted in `duplicate_spellings_merged`; one
  engine's rows only, so nothing is double counted). Near-duplicates (same words up to order, a plural `s` and stop
  words, on the lookalike fold) are grouped and flagged, **never summed** into one number.
- **Status** (closed): OK (every requested source read), PARTIAL (some read, some not), EMPTY_MEASURED (a source was
  read and gave no usable query rows — a measurement, with its anonymised remainder, not "no demand"), INCONCLUSIVE
  (nothing could be read: an empty ledger then means nothing was measured, never zero). The envelope's outcome follows
  (OK / PARTIAL / NOT_CONNECTED / BLOCKED / FAILED / KILLED) with the first failing source's reason. A failed source
  never fails the run and is never half-read: a page that fails mid-pagination after its retries fails the whole
  source (rows already received are discarded).
- **Row cap** 1,000 (ESTABLISHED first, then by impressions); `rows_dropped_past_cap` says how many were cut.

## Data-source honesty

| Source | State in this build | What it measures |
|---|---|---|
| Google Search Console Search Analytics (existing `gsc_client`, ADR 0017 "Search Console") | built; NOT_CONNECTED until Andre installs the service-account key and binds the tenant | property totals by date (1 request) and query rows (`dimensions=[query]`, `aggregationType=byProperty`, paged within the existing caps): impressions, clicks, CTR, average position **of this property** |
| Bing Webmaster Tools `GetQueryStats` (`src/bing_webmaster.py`) | adapter built, **fixtures only**; no wiring | the site's top queries on Bing: Clicks, Impressions, AvgClickPosition, AvgImpressionPosition, Date per row |
| Bing `GetKeywordStats` / `GetKeyword` (keyword research) | interface only (`keyword_stats` answers `BING_NOT_CONNECTED` `not_built`) | — |
| Google Ads `KeywordPlanIdeaService.GenerateKeywordIdeas` (`src/keyword_planner.py`) | interface only, NOT_BUILT; future | — |
| Any paid keyword / SEO data vendor | **not used, by owner rule (hard stop)** | — |

Bing facts, read on Microsoft's pages on Nov 8 2026: GetQueryStats(siteUrl) "Get detailed traffic statistics for top
queries", "The data will be updated every week", JSON
`GET https://ssl.bing.com/webmaster/api.svc/json/GetQueryStats?siteUrl=…&apikey=…` answering
`{"d": [{"__type": "QueryStats:#…", "AvgClickPosition", "AvgImpressionPosition", "Clicks", "Date": "/Date(ms-0700)/",
"Impressions", "Query"}]}`
(https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.getquerystats?view=bing-webmaster-dotnet);
the method list including GetKeyword, GetKeywordStats, GetRelatedKeywords, GetQueryStats, GetQueryTrafficStats,
GetPageQueryStats (https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi?view=bing-webmaster-dotnet);
JSON errors are HTTP 400 with `{"ErrorCode":3,"Message":"InvalidApiKey"}`
(https://learn.microsoft.com/en-us/bingwebmaster/getting-started); SOAP and POX retire Aug 31 2026, JSON/HTTP stays
(https://learn.microsoft.com/en-us/bingwebmaster/api-protocols); one API key per Bing Webmaster user, valid for all of
that user's verified sites, rotated by delete + generate (https://learn.microsoft.com/en-us/bingwebmaster/getting-access).
**Not found / unverified:** Bing quotas and rate limits; what period GetQueryStats covers and what one row's Date
stands for; whether AvgImpressionPosition is on Search Console's scale (so Bing positions are reported as Bing gave
them and never fed into the CTR curve); GetKeywordStats' answer shape and meaning (its member page could not be read;
a secondary source describes weekly keyword-research impressions per country/language — market data, which would
also send the client's query text to Microsoft). The adapter is strict on the documented shape (exactly `{"d": [...]}`,
the required documented row fields present and validated, optional documented fields validated when present, fields
Bing adds that are not documented ignored — never read or passed on — up to 64 per row; whole non-negative counts, clicks ≤ impressions, `/Date(ms±hhmm)/` dates between 2000 and
2100, no repeated query+date, NaN refused, identity encoding, 8 MiB cap), allowlists `ssl.bing.com` on 443 under
`/webmaster/api.svc/json/`, never follows a redirect, retries only 429 / 5xx / transport errors with backoff inside a
deadline and a request budget, keeps the key in a masked, unpicklable `ApiKey`, and never puts a URL (which carries the
key, Bing's design) or Bing's text in an error. Without an injected transport it refuses to call Bing at all
(`no_deadline_backend`): the production deadline-bound transport the Google connector has is **not built**.

Google Ads Keyword Planner (https://developers.google.com/google-ads/api/docs/keyword-planning/generate-keyword-ideas,
read Nov 8 2026): customer_id, language, geo targets, keyword or URL seeds; `avg_monthly_searches` (12-month average),
competition, competition_index, top-of-page bids. The claim that accounts without ad spend see bucketed ranges is a
Keyword Planner UI behaviour from secondary sources, **not stated** on the API page read; the interface carries every
volume as `VolumeRange(low, high, bucketed)` so a range stays a range whatever Google returns. It needs a Google Ads
account and developer token: a founder decision, not built.

## What is derived, and how (no fabricated metrics)

- **Opportunity** = the row's CTR gap against the **property's own** position curve: impression-weighted CTR per
  position bucket (1, 2, 3, 4-5, 6-10, 11-20, 21+) over the same window's established, non-brand Search Console rows;
  a bucket with fewer than 5 queries or 200 impressions is INSUFFICIENT_SUPPORT and every row in it INCONCLUSIVE.
  A row's expected CTR **leaves the row itself out** of its bucket (leave-one-out), and the support minimums apply
  to what is left. The row's CTR carries a **Wilson score 95% interval** (`ctr_95`); the gap's interval
  (`ctr_gap_95`) is the expected CTR minus that interval. The curve's own uncertainty is not added, so the stated
  interval understates the true uncertainty (said in the limitations). Status: INCONCLUSIVE for a row under the
  20-impression threshold (thin data: never COMPUTED, whatever its position) and for a gap interval that includes 0;
  COMPUTED otherwise, with `direction` `below_curve` / `above_curve`. `click_gap_modeled` = max(0, gap ×
  impressions) with `click_gap_modeled_95` from the gap interval, data class `modeled`, not traffic. Brand rows are
  NOT_APPLICABLE. No published industry CTR curve is used (none is this site, and their sources disagree).
- **Difficulty** is null with a stated reason: no owned data (no backlink or SERP data) supports an Ahrefs-style
  keyword difficulty, and none is invented.
- **Market volume** is null on every row with a stated reason.

## Personal data

A query that ADR 0017's classifier calls `pii_suspect` (e-mails in any spelling, one phone-like run of 7+ digits not
an ISO date, id- or secret-like words, and — added in review round 1, reworked in rounds 2 to 4,
`search_performance` rules `2026-11-08.7`, so Osei's query classes use it too — address forms and over-long text):
- an id- or secret-like word (round 4 narrowed this from the log templater's rule, which dropped model numbers, tire
  sizes, decimals and years — `24acc636a003`, `225/65r17`, `3.5`, `2012` — as ids): an e-mail in any spelling, a
  UUID, 16+ hex characters, a JWT-like dotted token, 20+ token characters, anything over 40 characters, a
  letters-and-digits word of 16+ characters or of 8+ characters switching between letters and digits at least 60% of
  the time (`a1b2c3d4e5f6g7`), a file name (`jane_cv.pdf`), and (round 5) a base64- or AWS-secret-like word holding
  `/`, `+` or `=` (20+ such characters with a digit, `+` or `=`, or 12+ with two of them), and (round 7, fail safe)
  any URL-ish word whose parts are not all plain: each `/` segment and `.` label must be a slug of hyphen-separated
  words, each all letters (≤ 15) or all digits (≤ 4), or a short version / page token (`v2`, `page2`, `http2`);
  otherwise every piece between `/ ? & = # ; :` is judged by the token rules, plus known secret prefixes (AWS
  `akia`/`asia`, Google `aiza`, GitHub, Slack, Stripe-style) and credential query parameters (`token=`, `key=`,
  `password=`, `sig=`, … with 6+ characters). Round 6's wider host exemption let 15 path secrets through and is
  gone. `docs.example.com/v2/api/reference`, `acme.com/blog/5-ways-to-save` and short tire sizes are kept; 7+ digits in one run stay the digit-run rule's;
- a street address: a house number (at the start, or after a space not preceded by `top` / `best`), an optional
  direction, 1-3 street-name tokens (none a stop or quantity word such as `near`, `in`, `best`, `hour`, `bedroom`,
  `lane`, `way`), then a suffix. `old`, `new`, `good` may start a street name (`17 old mill road`, `40 good hope
  rd`); as the whole name (`12 new street`) the suffix must end the query or be followed only by a unit, a state and
  a ZIP, so `3 new dr pepper flavors`, `2 old court cases`, `10 good place to eat` are kept (round 5) and `12 new
  street london` is missed. After a **strong** suffix (abbreviations that are not English words — `st`, `ave`,
  `blvd`, `rd`, `dr`, `ln`, `ct`, `pl`, `ter`, `cir`, `hwy`, `pkwy`, … — and `street`, `avenue`, `boulevard`) anything
  may follow (`123 main st 2nd floor`, `123 main st springfield il 62701 hours`); after an **ordinary-word** suffix
  (`road`, `lane`, `drive`, `court`, `way`, `terrace`, `place`, `loop`, `plaza`) only up to three place words, a state and a
  ZIP (and, after a ZIP, anything) to the end (`12 kings road chelsea`, `42 wallaby way sydney`); a unit (`apt` /
  `suite` / `unit` / `#` N) only after such a street;
- a state name or code with a 5-digit ZIP not followed by a unit word (`btu`, `miles`, `jobs`, `sq`, `zip`, …); the
  codes `in`, `or`, `me`, `ok` are left out because they are English words;
- PO boxes, PMB numbers, rural routes (`rr 2 box 15`, `hc 1 box 9`) and ZIP+4, anywhere;
- outside the US (rounds 4 and 5): UK postcodes — one of the ~120 real postcode areas, the district, a **space**,
  and an inward part (a digit and two letters, never c i k m o v, and not an ordinal or size unit: `x5 3rd`, `m4 1tb`,
  `s9 2nd` are kept) (`sw1a 1aa`, `ox1 4ag`); Canadian postal codes (`m5v 3l9`); French / Quebec leading forms
  (`350 rue sherbrooke`, `12 chemin du lac`, `quai`, `impasse`, `allée`; `place`, `route`, `avenue`, `boulevard` only
  with `de` / `du` / `des` / `d'`: `6 place de la concorde` drops, `6 place setting dinnerware` is kept). Other
  countries' formats are not covered: the rule is US-centric;
- any query over 512 characters (`search_performance.MAX_QUERY_CHARS`): not examined, counted as personal data
  (this also bounds the regex work per query).

Checked on the raw and the normalised text (fullwidth digits, lookalikes). A dropped query never enters the ledger;
only `pii_suspect_dropped` {queries, clicks, impressions}, where `queries` counts **distinct** normalised queries (a
query in both sources, or on many Bing dates, is one). The round-3 and round-4 reviewers' probe sets are regression
tables in `tests/test_gsc.py` (Osei) with subsets in `tests/test_priya.py`: round 3, 0 of 72 ordinary queries
dropped and 61 of 64 addresses dropped; round 4, 0 of 95 ordinary queries dropped and 49 of 50 addresses dropped;
round 5's regression table (19 ordinary queries kept, 10 addresses and secrets dropped) passes.
**Known gaps, disclosed** — an address of these kinds is NOT caught and goes into the returned ledger as the engine
reported it: bare named streets with no suffix (`1200 broadway`, `123 n main`); landmark or building-name
addresses; streets with more than three name words or an unlisted suffix (`trail`, `pike`, `row`, …); a state code
that is also an English word with its ZIP (`portland or 97201`); postal formats of countries other than the US, UK,
Canada and the French leading forms above (and UK / Canadian street addresses without a postcode are caught only
when they use a listed suffix); a UK postcode written without its space (`sw1a1aa`), a real UK postcode whose inward
part looks like an ordinal or unit (`… 2nd`, `… 1tb`), `12 new street` followed by a city. Secrets not caught:
base64 under 12 characters, and a random password written as a plain word (`reset password hunter`-style queries;
no rule can tell such a word from an ordinary one). **Over-drops by design (round 7, privacy over recall):** URLs
with a path part mixing letters and digits that is not a short version token (`shop.com/products/sku12345678`), a
slug word over 15 letters (`…/internationalization-guide`), or a host label with digits (`web2print-shop.com/…`);
pinned in `tests/test_gsc.py`. **Known over-drops:** a non-address shaped like `<number> <word> st|ave|dr|ct|pl…`, or `<number> <word>
court|place|way…` at the end of a query (`5 food court ideas`). A bare ZIP (`plumber 90210`) is kept. Derived variants are checked the same way, and a frame is built only from a query that has no capitalised
word as the engine reported it, and only when every word it adds around the head is in a closed vocabulary
(`priya.FRAME_VOCAB`: stop, question, constraint and commercial words and a short list of generic verbs); a head
with a capitalised word is not used. The ledger is handed out once and not stored; the recorded summary holds hashes
and counts. **Limitation:** personal names cannot be reliably detected (ADR 0017 GSC-12); the ledger shows the
dashboard / agent runtime the query text Google or Bing reported (both already anonymise rare queries), so a query
naming a person can appear in the answer as an observed row. The frame rules only stop such a word from being
transferred into new variants.

## EMERGING hand-off to Theo and Naomi (contract only)

`ledger.handoff`, schema `priya-emerging-handoff/1`: `tenant_id`, `run_id`, `generated_at`, `consumers` (theo:
conversation mining, NOT_BUILT; naomi: prompt volume, NOT_CONNECTED), `items` (exactly `key`, `query` (untrusted),
`origin`, `emerging_kinds`, `volume_status` (BELOW_THRESHOLD or UNKNOWN_NOT_MEASURED only), `observed_counts` (null
for derived items), `evidence`, `requested_measurements`), and the rule "an item is a candidate to measure, not a
measured demand". `priya.validate_handoff` is the check the consumers are to run. Nothing consumes it yet.

## Not built (honest)

- Bing wiring: `SEO_BING_WEBMASTER_KEY_FILE` stays in `config.NOT_BUILT` (setting it refuses start); no production
  transport with a hard deadline and DNS checks; no per-tenant Bing read budget; no owner-approved onboarding step
  (the client adds ZBM's Bing Webmaster user to its site). Connecting needs all four and an amendment here.
- Bing GetKeywordStats, Google Ads Keyword Planner: interfaces only.
- Theo, Naomi's prompt-volume source: not built; the hand-off is a contract.
- No ledger storage, history or drift between runs (it would need a query-text retention decision); no client
  report section; no dashboard UI; no scheduled runs.
- Thresholds, caps and the frame rule are this service's defaults, unmeasured against a real client (founder-pending).

## Tests

`services/seo-py/tests/test_priya.py` (the ledger rules), `tests/test_priya_service.py` (route, gates,
record-first, storage, budget, kill switches before and mid-read, client gates, isolation, Google errors and partial
pages, sampling and truncation, Bing via an injected fake, crash, manager, restart), `tests/test_bing_webmaster.py`
(the adapter against a fake Bing; key never leaks; NOT_BUILT wiring). Counts: `docs/test-counts.md`.

## Founder questions

1. Priya's Search Console reads now have their own budget (`SEO_GSC_LEDGER_READS_PER_DAY`, default 2): keep the
   default, or set another (1..12)?
2. Thresholds: 20 impressions for ESTABLISHED, 100 / 1,000 for MEDIUM / HIGH — keep or set?
3. Bing: approve wiring the key (a secret file like Search Console's), the onboarding step and a Bing read budget?
4. May client query text be sent to Microsoft's GetKeywordStats (market data), or stay inside first-party reads?
5. Google Ads Keyword Planner: open a Google Ads account / developer token for it, or leave it out?
6. Should the ledger be retained (it holds query text, which ADR 0017 keeps out of the log), enabling history and drift?
