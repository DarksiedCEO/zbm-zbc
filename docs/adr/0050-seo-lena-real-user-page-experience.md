# ADR 0050 — Lena, the real-user page-experience agent (Chrome UX Report field data)

Status: built on branch `claude/seo-crux-lena-20261108` from `codex/gate5-release-20261107` (a4af277). **Not in
force. Fixtures only:** nothing here has called the live Chrome UX Report API, no key exists, nothing is spent. The
owner approved a new single-task agent for CrUX on 2026-11-08; **"Lena" is a working name and the owner may rename
her** (the name is the agent id `lena` in the registry, the run guard and the report; a rename is a mechanical change
plus a lifecycle-state note, see "Open questions").

## Context

ADR 0017 builds the SEO department out of single-task agents (one human name, one job, a registry entry with inputs
and outputs). Real-user page experience — Google's Core Web Vitals as measured on real Chrome users — was not covered:
the crawl (Selene, Roman) sees raw HTML, never how fast a page was for people. Google publishes that field data, free,
through the Chrome UX Report (CrUX) API and the CrUX History API. Lena reads it and reports it. That is all she does.

## Decision

1. **One task.** Lena (`agents/lena.py`, task `page_experience`) reports the tenant's real-user Core Web Vitals from
   CrUX for the audited origin and, where CrUX has URL-level data, the audited URLs, per form factor (PHONE, DESKTOP,
   TABLET): the 75th percentile of each metric, its distribution (CrUX's histogram), its rating against web.dev's
   thresholds, the Core Web Vitals assessment, the collection period, freshness, and a trend over the History API's
   weekly periods (25 by default). **Lena does not run lab tests** (no Lighthouse, no synthetic page load, no request
   to the audited site at all), **does not fix anything**, does not forecast, and **produces no findings**: ratings
   are data in her section, never a recommendation. Turning a poor p75 into a finding with a decision state is a
   later, separate choice (see "Open questions").
2. **Registry entry** (`svc_manager.AGENTS`, the department manager's lifecycle and scorecards apply as for every agent;
   the run guard refuses a restricted or retired Lena with outcome `RESTRICTED`):

   | | |
   |---|---|
   | Agent | `lena` — Real-User Page Experience |
   | Task | `page_experience` (one per audit) |
   | Inputs | the audit's own target only: origin `<scheme>://<domain>` (a domain registered to the audit's tenant, already authorised by the audit) and the audit's own paths on it; the configured history depth |
   | Provider | the field-data port (`ports.Ports.field_data`): `crux_client.CruxClient`, or `NotConnectedFieldData` |
   | Outputs | an outcome envelope (no findings) in `report.agents`; the section in `report.page_experience` (`{outcome, reason, read_started, section}`); `GET /seo/v1/tenants/{tid}/audits/{aid}/page-experience`; ledger evidence `crux_read_started` and `crux_read_recorded` |
   | Never | lab tests, requests to the audited site, fixes, forecasts, findings, a single score |

3. **Vendor independence.** Lena reads field data through a two-method provider interface (`open_read(guard)` -> a
   read with `record(kind, target, form_factor)` and `history(kind, target, form_factor, periods)`, answering a neutral
   shape). CrUX is the only provider built; another source would be a second class with the same methods. Official
   API only (no scraping of PageSpeed Insights or any web UI).
4. **Honesty rules.**
   - Google's documented 404 ("chrome ux report data not found": too few samples, or not publicly discoverable) is
     `INSUFFICIENT_FIELD_DATA` — never fast, never slow, no rating. A 404 with any other body is
     `CRUX_PROTOCOL_ERROR`.
   - URL queries for a form factor whose ORIGIN has no field data are not sent and are listed `NOT_QUERIED`
     `ORIGIN_HAS_NO_FIELD_DATA` (an origin aggregates its URLs, so a URL of an origin with too few samples cannot have
     enough; stated as a skip, never as data). History for a key with no current data is `NOT_QUERIED`
     `NO_CURRENT_FIELD_DATA`.
   - A metric missing from an answer is `ABSENT`; a malformed one (histogram not contiguous, not starting at 0, an
     open bin before the last, densities outside 0..1 or not summing to 1 within 0.011, a number where CrUX documents
     a string or the reverse, non-finite values) is `MALFORMED` and dropped; its neighbours are kept. A record-level
     shape error (wrong key, wrong form factor, bad collection period, unexpected top-level field) makes that query
     `CRUX_PROTOCOL_ERROR`, never data. A URL normalised by Google is accepted only when `urlNormalizationDetails`
     names the URL asked for and the normalised URL is on the same origin.
   - The Core Web Vitals assessment is PASSED only when LCP, INP and CLS are all present and all good; FAILED when any
     present one is not good; otherwise INSUFFICIENT_FIELD_DATA.
   - **Provenance on every number:** every current and history entry, and every metric in it, carries `source`
     ("Google Chrome UX Report (CrUX) API"), `provider`, `api_version`, `method` (`records:queryRecord` /
     `records:queryHistoryRecord`), `collection_period` and `retrieved_at`; entries carry `freshness_days` (from the
     period's last day to the run's date) and `stale` (> 7 days for current data, > 14 for history).
   - **Trend** (descriptive): the median p75 of the earliest 6 weekly periods with data against the latest 6; fewer
     than 12 periods with data is `INSUFFICIENT_HISTORY`; a change under 10% or under the metric's minimum step (LCP
     100 ms, INP 20 ms, CLS 0.01, FCP 100 ms, TTFB 50 ms) is `NO_CLEAR_CHANGE`; otherwise `IMPROVING` / `WORSENING`.
     The periods are overlapping 28-day windows, so they are not independent samples; no significance test is made
     and nothing is forecast. The method text travels with every trend.
5. **Outcome.** Data and nothing missing: OK. Data with gaps (a failed query, a query not sent for a reason other
   than the two honest skips above, a malformed metric): PARTIAL with the stopping or failing code. Killed: KILLED.
   No data at all: the stopping code's outcome (`CRUX_AUTH_REFUSED` / `CRUX_BUDGET_EXHAUSTED` BLOCKED, the others
   FAILED), or INSUFFICIENT_EVIDENCE `CRUX_INSUFFICIENT_FIELD_DATA` when Google simply has none. Like Search Console
   (AEGIS gl-gsc M2), Lena's outcome stays inside her section and is left out of the audit's overall outcome
   (`svc_audits.OUTSIDE_OVERALL`): Google's availability or a call budget is not the audited site. A failed read never
   fails the audit (no 500; an unexpected exception is the section's `CRUX_PROTOCOL_ERROR` with only the exception's
   type recorded).
6. **Wiring and budgets** (`config._crux`, `ports.field_data_from_settings`, `svc_crux.py`):
   - `SEO_CRUX_ENABLED` (default `0`): off = `NotConnectedFieldData`, zero calls, the key file not read even if set.
     `1` requires `SEO_CRUX_API_KEY_FILE` (the *_FILE rules: absolute path, regular file owned by the service user,
     mode 0600/0400, no symlink, ≤ 1 KiB, one key of 20..128 `[A-Za-z0-9_-]`); missing or unusable **refuses
     start**; the key must differ from every token the service accepts or sends.
   - `SEO_CRUX_CALLS_PER_RUN` (0..120) and `SEO_CRUX_CALLS_PER_DAY` (0..20,000, any 24 hours, across all tenants:
     Google's quota is per project) both **default 0 = OFF** (BLOCKED `CRUX_BUDGET_EXHAUSTED`, no call). A run reserves
     min(per-run cap, what is left today) record-first; a completed audit is charged its actual calls, an
     interrupted one its whole reservation. Retries count.
   - `SEO_CRUX_TIMEOUT_SECONDS` (10) per attempt; `SEO_CRUX_DEADLINE_SECONDS` (60): one hard deadline over Lena's
     whole read — DNS on the gated resolver thread, connect, TLS, every socket read and write (finance_client's
     `_DeadlineBackend`), the body loop and every retry. A client without an injected transport that cannot install the
     deadline backend refuses to call (`CRUX_NOT_CONNECTED` `no_deadline_backend`).
   - `SEO_CRUX_HISTORY_PERIODS` (25; 1..40).
   - Egress allowlist: `chromeuxreport.googleapis.com` only, https on 443, the two documented method URLs only, no
     query string; checked on the URL and again at connect; no redirect followed; every resolved address globally
     routable. Bodies ≤ 1 MiB, identity encoding only.
   - Retries only on connect errors, timeouts, 429 and 500/502/503/504 (at most 3 attempts, `Retry-After` honoured
     up to 60 s, never past the deadline). 400 `API_KEY_INVALID` / 401 / 403 `PERMISSION_DENIED` (and the closed set of
     key-restriction reasons) are `CRUX_AUTH_REFUSED`; 403 `RESOURCE_EXHAUSTED` is `CRUX_RATE_LIMITED`; other 4xx and
     3xx `CRUX_PROTOCOL_ERROR`. Auth, rate, availability, budget and kill codes stop the run; the queries not sent are
     listed `NOT_QUERIED` with that code. Query order: origin current (phone, desktop, tablet), origin history, each
     URL's current data, each URL's history.
   - Kill switch `provider:crux` (new in `config.PROVIDER_SWITCHES`; env `SEO_KILLED_PROVIDERS=crux` too), plus
     global, write, the tenant's and `capability:audit`: checked before the read, before every request and on every
     socket operation (`CRUX_KILLED`).
   - Record-first: `crux_read_started` (tenant, audit, origin SHA-256, calls reserved) on the ledger and the log
     before any request leaves; no record, no request. `crux_read_recorded` (outcome, reason, section SHA-256, calls,
     counts) beside the report record.
7. **The key.** Held in `crux_client.ApiKey` (masked repr, never pickled or copied). The CrUX pages show the key as
   `?key=` in the URL; Google's system-parameters page documents the same API key as the `X-Goog-Api-Key` header.
   Lena sends it ONLY in that header, so it never appears in a URL, log, error, status, report or ledger entry (tested
   with an error answer that echoes the key). **That the CrUX endpoint honours the header is unverified against the
   live API**; if it does not, every read is `CRUX_AUTH_REFUSED` — there is deliberately no fallback to the query
   parameter.
8. **Tenant isolation** as everywhere: Lena asks only about the audit's own origin and paths (never a value from a
   crawled page or from Google's answer); the section lives in that tenant's audit; the route is tenant-scoped
   (another tenant's audit is the same 404; hub only with its own tenant token; finance_31 refused). CrUX data is
   public and aggregated (no personal data); asking about the client's origin does tell Google which origin this
   service looked up.
9. **Client report** (`client_report.py` hooks, the area itself in `report_page_experience.py`). A "Real-user page
   experience (Chrome UX Report)" area, shown for audits that ran Lena (audits
   made before her have no such area, so older reports render unchanged): per target and device, p75 with rating,
   Core Web Vitals assessment, collection period (flagged stale), the LCP / INP / CLS trend; NOT CONNECTED and the
   other not-checked states stated plainly; "no field data" explained as neither fast nor slow; evidence labelled
   "OBSERVED by Google", not by the audit's own requests.

## Review of 37c5bdb (CONDITIONAL), fixes

- **M1 — the other origin form.** "No data" used to read as "too few visits", which is wrong when the site lives on
  the other form of the address. (a) Wording: "Google has no field data for this exact origin." (reason text, the
  limits, the report rows "no field data for this exact address" and the note under the table). (b) When only one of
  the bare and `www.` forms of the audited domain is registered, Lena also asks the other form's **https** origin (its
  twin; a domain and its `www.` twin belong to one tenant, ADR 0017 decision 7): +3 record calls; history only where
  that origin has current data; the twin is its own target (`role: "twin"`), shown on its own labelled rows and
  **never merged** with the audited origin. The URL queries use the audited origin's host, or the twin's when only
  the twin has field data (`section.url_origin`). Both forms registered: no twin (each is audited on its own).
- **M2 — budgets and pacing.** `SEO_CRUX_TENANT_CALLS_PER_DAY` (0..20,000, default 0 = OFF, fail-closed bounds): a
  run reserves min(per run, global day left, tenant day left). Client-side pacing in `crux_client._Pacer`: a token
  bucket of 10 refilled at 90 a minute, process-wide, so any 60-second window holds at most 100 requests (retries
  included), under Google's 150; a wait that would pass the deadline stops the read `CRUX_RATE_LIMITED`
  `client_pacing` without sending. Tested structurally on the injected clock (sliding-window counts), no wall clock.
  **Recommended go-live values:** per run 6 + 6 × paths, at most 60 (the twin adds up to 3 record calls); per day
  about 2,000; per tenant per day about 200.
- **L2 — extra fields.** Every documented required field is still validated strictly; unknown extra fields are
  accepted and ignored (never read, never passed on), at most 16 per object (more: that object is malformed), and
  counted in `extra_fields_ignored` per answer and in the section's counts. **Go-live canary:** the first hand-run
  call must return HTTP 200 with `extra_fields_ignored` 0 (or the new fields read on Google's page) before any
  budget is raised above 0.
- **L3 — chained exceptions.** Every error this adapter raises is detached (`__cause__` and `__context__` cleared,
  context suppressed, raised outside the `except` block), so no httpx request — and so no key header — rides along
  on an exception (tested with a transport that raises an exception holding the request).
- **L1 kept:** header auth (`X-Goog-Api-Key`) is unverified against the live endpoint. **The first hand-run call
  must return HTTP 200 before any budget is set above 0.**
- Name stays "Lena" pending the owner; poor scores stay data only (no findings).

## Facts relied on (read Nov 8 2026)

| Fact | Source |
|---|---|
| `POST https://chromeuxreport.googleapis.com/v1/records:queryRecord`; `origin` or `url` (union); `formFactor` DESKTOP / PHONE / TABLET (omitted = all); `metrics` (omitted = all); response `record.key`, `record.metrics.<m>.histogram[{start,end,density}]` (last bin open), `percentiles.p75`, `collectionPeriod.{firstDate,lastDate}.{year,month,day}`, `urlNormalizationDetails`; CLS as strings; 150 queries/min per project, free, not raisable; daily data ~04:00 UTC, 28-day rolling, ~2 days behind | https://developer.chrome.com/docs/crux/api (page updated 2026-10-13) |
| `POST .../v1/records:queryHistoryRecord`; `collectionPeriodCount` 1..40, default 25; `histogramTimeseries[{start,end,densities}]`, `percentilesTimeseries.p75s`, `record.collectionPeriods` ascending; missing period = `"NaN"` densities / `null` p75; weekly, Mondays ~04:00 UTC; same quota | https://developer.chrome.com/docs/crux/history-api (page updated 2025-04-11) |
| 404 `{"error": {"code": 404, "message": "chrome ux report data not found", "status": "NOT_FOUND"}}` for an origin not publicly navigable or with insufficient samples | https://developer.chrome.com/docs/crux/guides/crux-api |
| FID retired: "the CrUX API and CrUX History API will stop serving data for the first_input_delay metric" | https://web.dev/blog/fid (2024-09-10) |
| API key as query parameter `key` or header `X-Goog-Api-Key` | https://docs.cloud.google.com/apis/docs/system-parameters (updated 2026-10-26) |
| LCP good ≤ 2500 ms, poor > 4000 ms; INP ≤ 200 / > 500 ms; CLS ≤ 0.1 / > 0.25; at the 75th percentile | https://web.dev/articles/defining-core-web-vitals-thresholds (updated 2025-05-07); https://web.dev/articles/inp (updated 2025-09-02) |
| FCP good ≤ 1.8 s, poor > 3.0 s | https://web.dev/articles/fcp (updated 2023-12-06) |
| TTFB good ≤ 0.8 s ("a rough guide"), poor > 1.8 s; not a Core Web Vital | https://web.dev/articles/ttfb (updated 2025-11-28) |

Metrics asked for: `largest_contentful_paint`, `interaction_to_next_paint`, `cumulative_layout_shift`,
`first_contentful_paint`, `experimental_time_to_first_byte`. The thresholds are versioned data
(`lena.THRESHOLDS_VERSION` `2026-11-08.1`) with their source on every entry. Not found on the pages read: an error
catalogue for 400 / 403 / 429 (the mapping above follows Google's general API error format); whether the header form
of the key is accepted by this endpoint.

## Not built (and why)

- **No live call, no key, no Railway wiring.** The key file is not mapped by the Railway entrypoint
  (docs/deploy/railway.md); `SEO_CRUX_ENABLED` stays 0 there. Going live needs: a Google Cloud project with the
  Chrome UX Report API enabled, an API key restricted to that API, the key as a secret file, the two caps raised,
  and one hand-run call to confirm the header form of the key and the live answer shape.
- No lab data (Lighthouse / PageSpeed Insights) — a different agent's job if ever wanted.
- No findings, decision states or fixes from field data; no drift rules over Lena's section (Osei's drift compares
  findings; Lena has none).
- No `effectiveConnectionType` or other CrUX dimensions; no LCP sub-parts, navigation types, RTT or ad metrics.
- No CrUX BigQuery dataset; no other field-data provider.
- No `http://` twin: the twin is asked on https only.
- No retention rule beyond the report's: the data is public aggregates, kept with the audit like the rest of it.
- No scheduled CrUX refresh of its own: Lena runs inside audits (scheduled re-audits included, under the same caps).

## Limitations (honest)

- CrUX covers opted-in Chrome users only (not other browsers), over 28 days, about two days behind; it cannot say
  why a page is slow.
- Exact keys: `https://example.com` and `https://www.example.com` are different origins; Lena asks for exactly the
  audited one.
- TTFB is experimental in CrUX and web.dev calls its threshold a rough guide; FCP and TTFB are not Core Web Vitals.
- The trend is descriptive (overlapping windows; no test; no forecast).
- The 429 / 400 / 403 mapping is from Google's general error format, not a CrUX-specific page; unknown reasons fall
  to `CRUX_PROTOCOL_ERROR` (fail closed).
- Tests use a fake CrUX behind `httpx.MockTransport` and a fake socket layer under the production deadline backend
  (`tests/crux_fakes.py`, `tests/test_lena.py`, `tests/test_lena_service.py`); nothing has been run against Google.

## Open questions for the owner

1. The name: keep "Lena" or rename (the agent id appears in recorded audit reports and lifecycle records; a rename
   after go-live needs a mapping note, before go-live it is free).
2. Should a POOR / NEEDS_IMPROVEMENT p75 become a finding (decision WATCH or ACT, data class `measured` with a
   third-party source label), or stay data only as built?
3. Budgets for go-live: the review recommends per run 6 + 6 × paths (≤ 60), per day ~2,000, per tenant per day ~200.
