# SEO technical audit: ` acme.test `

Prepared by Z Best Media.

## Executive summary

**Partial audit: 5 of 6 areas checked.** 1 page(s), as requested.

**What was checked:** Crawlability, Sitemaps and ` llms.txt `, Entity consistency, Search performance (Google Search Console), AI visibility (answer engines).

**Not covered by this report:**

- Content structure: not checked. This audit did not include this check.

**Top issues, most important first:**

1. **` robots.txt ` blocks a search crawler** (critical; Crawlability; 1 location(s)). A search engine's crawler is told not to read some or all of your pages, which can keep them out of its results. *Recommended:* Remove or narrow the Disallow rule for this crawler unless blocking it is intended.
2. **More than one canonical link** (high; Crawlability; 1 location(s)). Several canonical links leave search engines to guess which page to list. *Recommended:* Keep exactly one rel=canonical on the page.
3. **Business detail differs from your record** (high; Entity consistency; 1 location(s)). Search engines and AI assistants may show business details that differ from your records. *Recommended:* Change the markup to match your business record exactly (the record is the authority). The values are in the audit record.
4. **Canonical points to another host** (medium; Crawlability; 1 location(s)). Search engines are told another website holds the preferred copy of this page. *Recommended:* Confirm the other host is really the preferred copy; otherwise point the canonical at this page.
5. **Sitemap lists URLs ` robots.txt ` blocks** (medium; Sitemaps and ` llms.txt `; site-wide). Your sitemap and ` robots.txt ` disagree, which wastes crawl effort. *Recommended:* Remove blocked URLs from the sitemap, or allow them in ` robots.txt `.

7 more issue(s) are listed in the sections below.

**What to do first:** ` robots.txt ` blocks a search crawler (Crawlability). Remove or narrow the Disallow rule for this crawler unless blocking it is intended.

## Audit details

- **Website audited:** ` https://acme.test `
- **Audit run:** 2026-10-24 09:00:00 UTC to 2026-10-24 09:02:30 UTC
- **Coverage:** Partial audit: 5 of 6 areas checked
- **Pages requested (1):** ` / `

### What was checked

| Area | Status | Findings | Data freshness |
|---|---|---|---|
| Crawlability | **CHECKED** | 7 | observed during the run; refresh recommended by 2026-10-25 |
| Sitemaps and ` llms.txt ` | **CHECKED** | 3 | observed during the run; refresh recommended by 2026-10-25 |
| Content structure | **NOT COVERED** | — | not observed in this audit |
| Entity consistency | **CHECKED** | 1 | observed during the run; refresh recommended by 2026-10-31 |
| Search performance (Google Search Console) | **CHECKED** | 0 | Google data for 2026-09-24 to 2026-10-21 (Pacific Time, final data; Google's figures lag about 3 days) |
| AI visibility (answer engines) | **CHECKED** | 1 | observed during the run; refresh recommended by 2026-10-31 |

Findings are counted per issue (one issue may affect several pages). An area marked NOT CONNECTED, NOT COVERED or NOT CHECKED was not observed: it has no findings because it could not be looked at, not because there is nothing there.

### Not connected for this audit

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

- **Severity:** critical · **Recommended action:** Fix
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Remove or narrow the Disallow rule for this crawler unless blocking it is intended.
- **Where:** ` /robots.txt `
- **Evidence at ` /robots.txt `:** token: ` Googlebot<script> `; blocked paths: ` / `, ` /{slug}/{slug}/{email}/{number} `; rules: ` <script>alert(1)</script>](javascript:alert(1)) **bold** <img src=x onerror=ale… `, ` [withheld: may contain personal data] `

### More than one canonical link

- **Severity:** high · **Recommended action:** Fix
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Keep exactly one rel=canonical on the page.
- **Where:** ` / `
- **Evidence at ` / `:** count: 3; invalid: 1; canonicals: ` /{slug}/{slug}/{email}/{number} `, ` {offsite} (xn--80ak6aa92e.com) `, ` {unreadable} `

### Canonical points to another host

- **Severity:** medium · **Recommended action:** Test a change
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Confirm the other host is really the preferred copy; otherwise point the canonical at this page.
- **Where:** ` / `
- **Evidence at ` / `:** canonical: ` {offsite} (evil.test) `

### Estimated thing

- **Severity:** low · **Recommended action:** Test a change
- **Evidence:** ESTIMATED (an estimate; the assumption is not part of this finding's record, so treat it as unverified)
- **Fix:** No standard fix is written for this rule yet; ask us about it (the details are in the audit record).
- **Where:** ` / `

### Page is not HTML

- **Severity:** low · **Recommended action:** Monitor (may be intentional)
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Check that this URL is meant to be a web page; serve it as text/html if so.
- **Where:** ` / `
- **Evidence at ` / `:** content type: ` <script>alert(1)</script>](javascript:alert(1)) **bold** <i… `

### Page redirects

- **Severity:** low · **Recommended action:** Monitor (may be intentional)
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Link to the final URL directly; use a permanent redirect if the move is permanent.
- **Where:** ` /{slug}/{slug}/{email}/{number} `
- **Evidence at ` /{slug}/{slug}/{email}/{number} `:** statuses: 301; temporary: no; final url: ` {unreadable} `

### Some future rule

- **Severity:** low · **Recommended action:** Monitor (may be intentional)
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** No standard fix is written for this rule yet; ask us about it (the details are in the audit record).
- **Where:** ` / `

## Sitemaps and ` llms.txt `

**Status: CHECKED.** Whether the XML sitemaps can be read and list the right URLs, and whether an ` llms.txt ` file is published (an emerging, non-standard convention).

Data freshness: observed during the run; refresh recommended by 2026-10-25.

### Sitemap lists URLs ` robots.txt ` blocks

- **Severity:** medium · **Recommended action:** Fix
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Remove blocked URLs from the sitemap, or allow them in ` robots.txt `.
- **Evidence:** count: 1; examples: ` /{slug}/{slug}/{email}/{number} `

### ` llms.txt ` does not follow the proposed format

- **Severity:** low · **Recommended action:** Test a change
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Optional: follow the ` llmstxt.org ` proposal (a \# title, a \> summary, sections of absolute links).
- **Where:** ` /llms.txt `
- **Evidence at ` /llms.txt `:** issues: ` <script>alert(1)</script>](javascript:alert(1)) **bold** <i… `; ` <b>x</b> `

### Sitemap uses an unexpected XML namespace

- **Severity:** low · **Recommended action:** Fix
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Use the ` sitemaps.org ` 0.9 namespace.
- **Where:** ` /{slug}/{slug}/{email}/{number} `

## Content structure

**Status: NOT COVERED.** How each audited page is structured for machines: title, description, headings, language, structured data (JSON-LD) and how much text is in the raw HTML.

Data freshness: not observed in this audit.

This audit did not include this check. Nothing is reported here because this area was not checked. This is not a clean result.

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
| ` /u/{email}/{slug} ` | 1 | 2 | 50.0% |
| ` /blog/{slug} ` | 40 | 500 | 8.0% |
| ` {other} ` | 3 | 30 | 10.0% |

No findings in this area.

## AI visibility (answer engines)

**Status: CHECKED.** Whether answer engines (Perplexity when connected) cite or mention your site for a fixed set of market questions, sampled several times each. Cited means the engine's search returned a page of your site for the answer (cited = returned by search); it does not mean the answer's text linked to that page.

Data freshness: observed during the run; refresh recommended by 2026-10-31.

| Engine | Status | Questions asked | Not asked |
|---|---|---|---|
| ` perplexity ` | CHECKED | 1 | 0 |

- ` perplexity `: Each of the 1 question(s) asked was asked 5 times.

Question set version 2, fingerprint ` dddddddddddddddd `. Questions are referred to by number; their text and the answers are not reproduced.

Prompt volume: **NOT CONNECTED**. How often real people ask these questions is not known.

### Not mentioned or cited; other sources were

- **Severity:** medium · **Recommended action:** Test a change
- **Evidence:** OBSERVED (measured by this audit's own requests)
- **Fix:** Test a page that answers this question directly; see which kinds of sources were cited instead.
- **Evidence:** engine: ` perplexity `; question: Question 1; usable answers: 5; cited instead: ` {host} (1 answers) `, ` {host} (1 answers) `

## Methodology and limits

### Crawlability

**Method.** \[withheld: may contain personal data\]

**Limits.**

- \[withheld: may contain personal data\]

### Sitemaps and ` llms.txt `

**Method.** Sitemaps.

### Entity consistency

**Method.** entity

### Search performance (Google Search Console)

**Method.** Search Analytics for the bound property.

**Limits.**

- Google returns top rows only.

### AI visibility (answer engines)

**Method.** Each prompt of prompt set core\<script\> v2 sent 5 times per engine.

## Appendix: audit record

- **Audit ID:** ` seo-aud-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa `
- **Audit record:** report SHA-256 ` bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb `, recorded on a tamper-evident ledger when the audit completed.

### Reference codes

For support requests: the reference code of each finding in this report.

| Finding | Reference |
|---|---|
| Business detail differs from your record | ` ENTITY_FIELD_MISMATCH ` |
| Canonical points to another host | ` CANONICAL_OTHER_HOST ` |
| Estimated thing | ` ESTIMATED_THING ` |
| More than one canonical link | ` CANONICAL_MULTIPLE ` |
| Not mentioned or cited; other sources were | ` CITATION_OPPORTUNITY ` |
| Page is not HTML | ` PAGE_NOT_HTML ` |
| Page redirects | ` REDIRECTED ` |
| Sitemap lists URLs ` robots.txt ` blocks | ` SITEMAP_LISTS_ROBOTS_BLOCKED_URL ` |
| Sitemap uses an unexpected XML namespace | ` SITEMAP_NAMESPACE ` |
| Some future rule | ` SOME_FUTURE_RULE ` |
| ` llms.txt ` does not follow the proposed format | ` LLMS_TXT_FORMAT ` |
| ` robots.txt ` blocks a search crawler | ` ROBOTS_BLOCKS_SEARCH_CRAWLER ` |

### Versions

- bot families: ` 2026-10-10.1 `
- report: ` seo-audit-report/1 `
- schema rules: ` 2026-10-09.1 `
- report renderer: ` seo-client-report/6 `

---

Prepared by Z Best Media. This report recommends changes; it does not make any change to your site.
