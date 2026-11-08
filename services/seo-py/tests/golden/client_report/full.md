# SEO technical audit: ` acme.test `

Prepared by Z Best Media.

## Executive summary

**Complete audit: all 6 areas checked.** 3 page(s), as requested.

**What was checked:** Crawlability, Sitemaps and ` llms.txt `, Content structure, Entity consistency, Search performance (Google Search Console), AI visibility (answer engines).

**Top issues, most important first:**

1. **` robots.txt ` blocks a search crawler** (high; Crawlability; 1 location(s)). A search engine's crawler is told not to read some or all of your pages, which can keep them out of its results. *Recommended:* Remove or narrow the Disallow rule for this crawler unless blocking it is intended.
2. **Business detail differs from your record** (high; Entity consistency; 1 location(s)). Search engines and AI assistants may show business details that differ from your records. *Recommended:* Change the markup to match your business record exactly (the record is the authority). The values are in the audit record.
3. **Page asks not to be indexed (noindex)** (medium; Crawlability; 1 location(s)). This page asks search engines not to list it, so it can be missing from search results. *Recommended:* Remove the noindex (meta robots or X-Robots-Tag header) if this page should appear in search; keep it if hiding it is intended.
4. **Structured data has errors** (medium; Content structure; 1 location(s)). Search engines and AI assistants read this markup to understand your business; with required details missing they may ignore it or show incomplete details. *Recommended:* Add the required properties listed below to the JSON-LD.
5. **Not exactly one H1 heading** (medium; Content structure; 1 location(s)). A missing or repeated main heading makes the page's topic less clear. *Recommended:* Give the page one H1 that states its topic.

6 more issue(s) are listed in the sections below.

**What to do first:** ` robots.txt ` blocks a search crawler (Crawlability). Remove or narrow the Disallow rule for this crawler unless blocking it is intended.

## Audit details

- **Website audited:** ` https://acme.test `
- **Audit run:** 2026-10-24 09:00:00 UTC to 2026-10-24 09:02:30 UTC
- **Coverage:** Complete audit: all 6 areas checked
- **Pages requested (3):** ` / `, ` /about `, ` /pricing `

### What was checked

| Area | Status | Findings | Data freshness |
|---|---|---|---|
| Crawlability | **CHECKED** | 5 | observed during the run; refresh recommended by 2026-10-25 |
| Sitemaps and ` llms.txt ` | **CHECKED** | 3 | observed during the run; refresh recommended by 2026-10-25 |
| Content structure | **CHECKED** | 3 | observed during the run; refresh recommended by 2026-10-31 |
| Entity consistency | **CHECKED** | 1 | observed during the run; refresh recommended by 2026-10-31 |
| Search performance (Google Search Console) | **CHECKED** | 0 | Google data for 2026-09-24 to 2026-10-21 (Pacific Time, final data; Google's figures lag about 3 days) |
| AI visibility (answer engines) | **CHECKED** | 2 | observed during the run; refresh recommended by 2026-10-31 |

Findings are counted per issue (one issue may affect several pages). An area marked NOT CONNECTED, NOT COVERED or NOT CHECKED was not observed: it has no findings because it could not be looked at, not because there is nothing there.

### Not connected for this audit

- OpenAI answer engine.
- Web analytics (traffic, conversions).
- CRM (leads, revenue).
- Prompt volume: how often real people ask the market questions is not known.
- JavaScript rendering: pages were read as raw HTML; content added by JavaScript was not seen.

### How to read this report

- Every finding states its **severity** (critical, high, medium, low, info), its **evidence class** (OBSERVED: measured by this audit's own requests; INFERRED: drawn by a stated rule; ESTIMATED: an estimate with its assumption) and a **recommended fix**.
- There is no overall score and no grade. Findings of different kinds are not added together.
- This report does not promise rankings, traffic or citations by any search or answer engine, and it makes no comparison claims about any other provider.
- Text from your pages, sitemaps and robots.txt is quoted as data only. Addresses the audit found by following links are shown as templates (identifiers replaced by placeholders such as {slug} or {number}); the exact addresses are in the audit record.

## Crawlability

**Status: CHECKED.** Whether search and AI crawlers can reach and index the audited pages: ` robots.txt `, status codes, redirects, noindex signals and canonical links.

Data freshness: observed during the run; refresh recommended by 2026-10-25.

### ` robots.txt ` blocks a search crawler

- **Severity:** high · **Recommended action:** Fix
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Remove or narrow the Disallow rule for this crawler unless blocking it is intended.
- **Where:** ` /robots.txt `
- **Evidence at ` /robots.txt `:** token: ` Bingbot `; blocked paths: ` /pricing `; rules: ` Disallow: /pricing `

### Page asks not to be indexed (noindex)

- **Severity:** medium · **Recommended action:** Test a change
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Remove the noindex (meta robots or X-Robots-Tag header) if this page should appear in search; keep it if hiding it is intended.
- **Where:** ` /pricing `

### Redirect chain

- **Severity:** medium · **Recommended action:** Fix
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Point links and redirects straight at the final URL (one hop), using a permanent (301/308) redirect.
- **Where:** ` /about `
- **Evidence at ` /about `:** statuses: 301, 302; hops: 2; temporary: yes; final url: ` /{slug} `

### No canonical link

- **Severity:** low · **Recommended action:** Monitor (may be intentional)
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Add a self-referencing rel=canonical to the page.
- **Where:** ` / `, ` /pricing `

### ` robots.txt ` blocks an AI or dataset crawler

- **Severity:** info · **Recommended action:** Monitor (may be intentional)
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** No fix needed if this is your policy. If you want this operator to read your site, allow its user-agent token.
- **Where:** ` /robots.txt `
- **Evidence at ` /robots.txt `:** token: ` GPTBot `; purpose: ` ai_training `; blocked paths: ` / `; rules: ` Disallow: / `

## Sitemaps and ` llms.txt `

**Status: CHECKED.** Whether the XML sitemaps can be read and list the right URLs, and whether an ` llms.txt ` file is published (an emerging, non-standard convention).

Data freshness: observed during the run; refresh recommended by 2026-10-25.

### Sitemap lists URLs on another host

- **Severity:** medium · **Recommended action:** Fix
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** List only URLs of this site in its sitemap.
- **Evidence:** count: 2; examples: ` {offsite} (cdn.example) `

### Every sitemap URL has the same lastmod

- **Severity:** low · **Recommended action:** Test a change
- **Evidence:** INFERRED (drawn by a stated rule from what was observed, not measured directly: every URL carries the same lastmod, so it likely does not reflect real changes)
- **Fix:** Set \<lastmod\> per URL from the page's real last change, or leave it out.
- **Evidence:** count: 40

### No ` llms.txt `

- **Severity:** info · **Recommended action:** Monitor (may be intentional)
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Nothing required: ` llms.txt ` is an emerging, non-standard convention and its absence is not a defect.
- **Where:** ` /llms.txt `
- **Evidence at ` /llms.txt `:** status: 404

## Content structure

**Status: CHECKED.** How each audited page is structured for machines: title, description, headings, language, structured data (JSON-LD) and how much text is in the raw HTML.

Data freshness: observed during the run; refresh recommended by 2026-10-31.

### Not exactly one H1 heading

- **Severity:** medium · **Recommended action:** Fix
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Give the page one H1 that states its topic.
- **Where:** ` /pricing `
- **Evidence at ` /pricing `:** h1 count: 0

### Structured data has errors

- **Severity:** medium · **Recommended action:** Fix
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Add the required properties listed below to the JSON-LD.
- **Where:** ` /about `
- **Evidence at ` /about `:** issues: required property ` address ` is missing from ` LocalBusiness `

### Title length outside the usual range

- **Severity:** low · **Recommended action:** Test a change
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Consider a title of roughly 15-65 characters that describes the page (a heuristic, not a rule).
- **Where:** ` / `
- **Evidence at ` / `:** length: 80; heuristic range: 15, 65

## Entity consistency

**Status: CHECKED.** Whether the business details in the pages' Organization / LocalBusiness markup match your canonical business record (name, address, phone).

Data freshness: observed during the run; refresh recommended by 2026-10-31.

### Business detail differs from your record

- **Severity:** high · **Recommended action:** Fix
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Change the markup to match your business record exactly (the record is the authority). The values are in the audit record.
- **Where:** ` / `
- **Evidence at ` / `:** field: ` telephone `

## Search performance (Google Search Console)

**Status: CHECKED.** Clicks, impressions, click-through rate and position from your own Google Search Console property, when it is connected.

Data freshness: Google data for 2026-09-24 to 2026-10-21 (Pacific Time, final data; Google's figures lag about 3 days).

Property ` sc-domain:acme.test `, 2026-09-24 to 2026-10-21 (28 days).

| Measure | Value | Evidence |
|---|---|---|
| Clicks | 140 | OBSERVED (first-party, as reported by Google Search Console) |
| Impressions | 1,400 | OBSERVED (first-party, as reported by Google Search Console) |
| Click-through rate | 10.0% | OBSERVED (clicks divided by impressions over the window) |
| Average position | 7.5 | ESTIMATED (assumption: the impression-weighted mean of Google's daily positions approximates the window's position; it is not Google's own figure) |

These figures are not paired with revenue (analytics and CRM are not connected), so no decision is drawn from them.

**By query class** (no query text is kept):

| Class | Clicks | Impressions | CTR |
|---|---|---|---|
| set aside (looked like personal data; dropped) | 2 | 5 | 40.0% |
| brand | 20 | 100 | 20.0% |
| questions | 10 | 150 | 6.7% |
| local intent | 5 | 60 | 8.3% |
| commercial intent | 0 | 0 | n/a |
| other | 3 | 40 | 7.5% |
| unattributed (anonymised by Google, or past the caps) | 100 | 1,045 | n/a |

**By page template** (aggregated by page; not comparable with the totals above):

| Page template | Clicks | Impressions | CTR |
|---|---|---|---|
| ` /blog/{slug} ` | 40 | 500 | 8.0% |
| ` {other} ` | 3 | 30 | 10.0% |

No findings in this area.

## AI visibility (answer engines)

**Status: CHECKED.** Whether answer engines (Perplexity when connected) cite or mention your site for a fixed set of market questions, sampled several times each. Cited means the engine's search returned a page of your site for the answer (cited = returned by search); it does not mean the answer's text linked to that page.

Data freshness: observed during the run; refresh recommended by 2026-10-31.

| Engine | Status | Questions asked | Not asked |
|---|---|---|---|
| ` perplexity ` | CHECKED | 2 | 0 |

- ` perplexity `: Each of the 2 question(s) asked was asked 5 times.

Question set version 2, fingerprint ` dddddddddddddddd `. Questions are referred to by number; their text and the answers are not reproduced.

Prompt volume: **NOT CONNECTED**. How often real people ask these questions is not known.

### Not mentioned or cited; other sources were

- **Severity:** medium · **Recommended action:** Test a change
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Test a page that answers this question directly; see which kinds of sources were cited instead.
- **Evidence:** engine: ` perplexity `; question: Question 2; usable answers: 5; cited instead: ` yelp.com (3 answers) `, ` rival.test (2 answers) (a competitor you named) `

### Cited in at least half of the answers

- **Severity:** info · **Recommended action:** Monitor (may be intentional)
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Keep the cited pages current and accurate.
- **Evidence:** engine: ` perplexity `; question: Question 1; usable answers: 5; citation rate: 80.0% (95% interval 37.5% to 96.4%); mention rate: 100.0% (95% interval 56.5% to 100.0%)

## Methodology and limits

### Applies to the whole audit

- This audit reads public pages only; nothing here is paired with traffic, conversions or revenue (credit is not causation).
- No single visibility score is produced.

### Crawlability

**Method.** Fetched ` robots.txt ` and each audited page (bot family list 2026-10-10.1).

**Limits.**

- Observed from one network location at one time.

### Sitemaps and ` llms.txt `

**Method.** Sitemaps from ` robots.txt ` Sitemap lines.

**Limits.**

- ` llms.txt ` is non-standard.

### Content structure

**Method.** Raw HTML parsed; structured data validated (rules 2026-10-09.1).

**Limits.**

- Heuristic checklist, uncalibrated.

### Entity consistency

**Method.** Organization / LocalBusiness JSON-LD compared with the canonical entity record.

**Limits.**

- Only the audited pages were read.

### Search performance (Google Search Console)

**Method.** Search Analytics for the bound property.

**Limits.**

- Google returns top rows only.

### AI visibility (answer engines)

**Method.** Each prompt of prompt set core v2 sent 5 times per engine.

**Limits.**

- One sample of N answers at one time from one account.

## Appendix: audit record

- **Audit ID:** ` seo-aud-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa `
- **Audit record:** report SHA-256 ` bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb `, recorded on a tamper-evident ledger when the audit completed.

### Reference codes

For support requests: the reference code of each finding in this report.

| Finding | Reference |
|---|---|
| Business detail differs from your record | ` ENTITY_FIELD_MISMATCH ` |
| Cited in at least half of the answers | ` CITATION_STRENGTH ` |
| Every sitemap URL has the same lastmod | ` SITEMAP_LASTMOD_ALL_IDENTICAL ` |
| No canonical link | ` CANONICAL_MISSING ` |
| No ` llms.txt ` | ` LLMS_TXT_ABSENT ` |
| Not exactly one H1 heading | ` H1_COUNT ` |
| Not mentioned or cited; other sources were | ` CITATION_OPPORTUNITY ` |
| Page asks not to be indexed (noindex) | ` NOINDEX ` |
| Redirect chain | ` REDIRECT_CHAIN ` |
| Sitemap lists URLs on another host | ` SITEMAP_LOC_OTHER_HOST ` |
| Structured data has errors | ` STRUCTURED_DATA_ERRORS ` |
| Title length outside the usual range | ` TITLE_LENGTH ` |
| ` robots.txt ` blocks a search crawler | ` ROBOTS_BLOCKS_SEARCH_CRAWLER ` |
| ` robots.txt ` blocks an AI or dataset crawler | ` ROBOTS_BLOCKS_AI_OR_DATASET_CRAWLER ` |

### Inputs set aside

Malformed or unsafe inputs were set aside unread (never parsed further):

- xml doctype refused: 1

### Versions

- bot families: ` 2026-10-10.1 `
- report: ` seo-audit-report/1 `
- schema rules: ` 2026-10-09.1 `
- report renderer: ` seo-client-report/6 `

---

Prepared by Z Best Media. This report recommends changes; it does not make any change to your site.
