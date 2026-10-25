# SEO technical audit: ` acme.test `

Prepared by Z Best Media.

## Cover

- **Website audited:** ` https://acme.test `
- **Audit ID:** ` seo-aud-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa `
- **Run:** 2026-10-24 09:00:00 UTC to 2026-10-24 09:02:30 UTC
- **Run outcome:** PARTIAL (whether every check could run; not a score)
- **Audit record:** report SHA-256 ` bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb `, recorded on Z Best Media's tamper-evident ledger when the audit completed.
- **Pages audited (1):** ` / `

### What was checked

| Area | Status | Findings | Data freshness |
|---|---|---|---|
| Crawlability | **CHECKED** | 7 | observed during the run; refresh recommended by 2026-10-25 |
| Sitemaps and ` llms.txt ` | **CHECKED** | 3 | observed during the run; refresh recommended by 2026-10-25 |
| Content structure | **NOT COVERED** | — | not observed in this audit |
| Entity consistency | **CHECKED** | 1 | observed during the run; refresh recommended by 2026-10-31 |
| Search performance (Google Search Console) | **CHECKED** | 0 | Google data for 2026-09-24 to 2026-10-21 (Pacific Time, final data; Google's figures lag about 3 days) |
| AI visibility (answer engines) | **CHECKED** | 1 | observed during the run; refresh recommended by 2026-10-31 |

Findings are counted per rule (one rule may affect several pages). An area marked NOT CONNECTED, NOT COVERED or NOT CHECKED was not observed: it has no findings because it could not be looked at, not because there is nothing there.

### Not connected for this audit

- JavaScript rendering: pages were read as raw HTML; content added by JavaScript was not seen.

### How to read this report

- Every finding states its **severity** (critical, high, medium, low, info), its **evidence class** (OBSERVED: measured by this audit's own requests; INFERRED: drawn by a stated rule; ESTIMATED: an estimate with its assumption), the **rule** that produced it, and a **recommended fix**.
- There is no overall score and no grade. Findings of different kinds are not added together.
- This report does not promise rankings, traffic or citations by any search or answer engine, and it makes no comparison claims about any other provider.
- Text from your pages, sitemaps and robots.txt is quoted as data only. Other URLs are shown as templates (identifiers replaced by placeholders such as {slug} or {number}); the exact URLs are in the audit record.

## Crawlability

**Status: CHECKED.** Whether search and AI crawlers can reach and index the audited pages: ` robots.txt `, status codes, redirects, noindex signals and canonical links.

Data freshness: observed during the run; refresh recommended by 2026-10-25.

### ` robots.txt ` blocks a search crawler

- **Severity:** critical · **Recommended action:** Fix
- **Evidence:** OBSERVED (measured by this audit's own requests) · **Rule:** ` ROBOTS_BLOCKS_SEARCH_CRAWLER ` · **Area:** ` P2 `
- **Fix:** Remove or narrow the Disallow rule for this crawler unless blocking it is intended.
- **Where:** ` /robots.txt `
- **Evidence at ` /robots.txt `:** token: ` Googlebot<script> `; blocked paths: ` / `, ` /{slug}/{slug}/{email}/{number} `; rules: ` <script>alert(1)</script>](javascript:alert(1)) **bold** <img src=x onerror=ale… `, ` [withheld: may contain personal data] `

### More than one canonical link

- **Severity:** high · **Recommended action:** Fix
- **Evidence:** OBSERVED (measured by this audit's own requests) · **Rule:** ` CANONICAL_MULTIPLE ` · **Area:** ` P1 `
- **Fix:** Keep exactly one rel=canonical on the page.
- **Where:** ` / `
- **Evidence at ` / `:** count: 3; invalid: 1; canonicals: ` /{slug}/{slug}/{email}/{number} `, ` {offsite} (xn--80ak6aa92e.com) `, ` {unreadable} `

### Canonical points to another host

- **Severity:** medium · **Recommended action:** Test a change
- **Evidence:** OBSERVED (measured by this audit's own requests) · **Rule:** ` CANONICAL_OTHER_HOST ` · **Area:** ` P1 `
- **Fix:** Confirm the other host is really the preferred copy; otherwise point the canonical at this page.
- **Where:** ` / `
- **Evidence at ` / `:** canonical: ` {offsite} (evil.test) `

### Estimated thing

- **Severity:** low · **Recommended action:** Test a change
- **Evidence:** ESTIMATED (an estimate; the assumption is not part of this finding's record, so treat it as unverified) · **Rule:** ` ESTIMATED_THING ` · **Area:** ` P1 `
- **Fix:** No standard fix is written for this rule yet; ask us about it (the details are in the audit record).
- **Where:** ` / `

### Page is not HTML

- **Severity:** low · **Recommended action:** Monitor (may be intentional)
- **Evidence:** OBSERVED (measured by this audit's own requests) · **Rule:** ` PAGE_NOT_HTML ` · **Area:** ` P1 `
- **Fix:** Check that this URL is meant to be a web page; serve it as text/html if so.
- **Where:** ` / `
- **Evidence at ` / `:** content type: ` <script>alert(1)</script>](javascript:alert(1)) **bold** <i… `

### Page redirects

- **Severity:** low · **Recommended action:** Monitor (may be intentional)
- **Evidence:** OBSERVED (measured by this audit's own requests) · **Rule:** ` REDIRECTED ` · **Area:** ` P1 `
- **Fix:** Link to the final URL directly; use a permanent redirect if the move is permanent.
- **Where:** ` /{slug}/{slug}/{email}/{number} `
- **Evidence at ` /{slug}/{slug}/{email}/{number} `:** statuses: 301; temporary: no; final url: ` {unreadable} `

### Some future rule

- **Severity:** low · **Recommended action:** Monitor (may be intentional)
- **Evidence:** OBSERVED (measured by this audit's own requests) · **Rule:** ` SOME_FUTURE_RULE ` · **Area:** ` P1 `
- **Fix:** No standard fix is written for this rule yet; ask us about it (the details are in the audit record).
- **Where:** ` / `

## Sitemaps and ` llms.txt `

**Status: CHECKED.** Whether the XML sitemaps can be read and list the right URLs, and whether an ` llms.txt ` file is published (an emerging, non-standard convention).

Data freshness: observed during the run; refresh recommended by 2026-10-25.

### Sitemap lists URLs ` robots.txt ` blocks

- **Severity:** medium · **Recommended action:** Fix
- **Evidence:** OBSERVED (measured by this audit's own requests) · **Rule:** ` SITEMAP_LISTS_ROBOTS_BLOCKED_URL ` · **Area:** ` P3 `
- **Fix:** Remove blocked URLs from the sitemap, or allow them in ` robots.txt `.
- **Evidence:** count: 1; examples: ` /{slug}/{slug}/{email}/{number} `

### ` llms.txt ` does not follow the proposed format

- **Severity:** low · **Recommended action:** Test a change
- **Evidence:** OBSERVED (measured by this audit's own requests) · **Rule:** ` LLMS_TXT_FORMAT ` · **Area:** ` P3 `
- **Fix:** Optional: follow the ` llmstxt.org ` proposal (a \# title, a \> summary, sections of absolute links).
- **Where:** ` /llms.txt `
- **Evidence at ` /llms.txt `:** issues: ` <script>alert(1)</script>](javascript:alert(1)) **bold** <i… `, ` <b>x</b> `

### Sitemap uses an unexpected XML namespace

- **Severity:** low · **Recommended action:** Fix
- **Evidence:** OBSERVED (measured by this audit's own requests) · **Rule:** ` SITEMAP_NAMESPACE ` · **Area:** ` P3 `
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
- **Evidence:** OBSERVED (measured by this audit's own requests) · **Rule:** ` ENTITY_FIELD_MISMATCH ` · **Area:** ` P6 `
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

No findings for the parts of this area that were checked.

## AI visibility (answer engines)

**Status: CHECKED.** Whether answer engines (Perplexity when connected) cite or mention your site for a fixed set of market questions, sampled several times each.

Data freshness: observed during the run; refresh recommended by 2026-10-31.

| Engine | Status | Questions asked | Not asked |
|---|---|---|---|
| ` perplexity ` | CHECKED | 1 | 0 |

Each question was asked 5 times per engine (question set version 2, fingerprint ` dddddddddddddddd `). Questions are referred to by number; their text and the answers are not reproduced.

Prompt volume: **NOT CONNECTED**. How often real people ask these questions is not known.

### Not mentioned or cited; other sources were

- **Severity:** medium · **Recommended action:** Test a change
- **Evidence:** OBSERVED (measured by this audit's own requests) · **Rule:** ` CITATION_OPPORTUNITY ` · **Area:** ` P4 `
- **Fix:** Test a page that answers this question directly; see which kinds of sources were cited instead.
- **Evidence:** engine: ` perplexity `; question: Question 1; usable answers: 5; cited instead: ` {host} (1 answers) `, ` {host} (1 answers) `

## Methodology and limits

### Applies to the whole audit

- \[withheld: may contain personal data\]

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

### Versions

- bot families: ` 2026-10-10.1 `
- report: ` seo-audit-report/1 `
- schema rules: ` 2026-10-09.1 `
- report renderer: ` seo-client-report/1 `

---

Prepared by Z Best Media. This report recommends changes; it does not make any change to your site.
