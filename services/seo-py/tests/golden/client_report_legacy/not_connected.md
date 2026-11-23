# SEO technical audit: ` acme.test `

Prepared by Z Best Media.

## Cover

- **Website audited:** ` https://acme.test `
- **Audit ID:** ` seo-aud-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa `
- **Run:** 2026-10-24 09:00:00 UTC to 2026-10-24 09:02:30 UTC
- **Run outcome:** NOT\_CONNECTED (whether every check could run; not a score)
- **Audit record:** report SHA-256 ` bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb `, recorded on a tamper-evident ledger when the audit completed.
- **Pages audited (1):** ` / `

### What was checked

| Area | Status | Findings | Data freshness |
|---|---|---|---|
| Crawlability | **NOT CONNECTED** | — | not observed in this audit |
| Sitemaps and ` llms.txt ` | **NOT COVERED** | — | not observed in this audit |
| Content structure | **NOT COVERED** | — | not observed in this audit |
| Entity consistency | **NOT COVERED** | — | not observed in this audit |
| Search performance (Google Search Console) | **NOT CONNECTED** | — | not observed in this audit |
| AI visibility (answer engines) | **NOT COVERED** | — | not observed in this audit |

Findings are counted per rule (one rule may affect several pages). An area marked NOT CONNECTED, NOT COVERED or NOT CHECKED was not observed: it has no findings because it could not be looked at, not because there is nothing there.

### Not connected for this audit

- Perplexity answer engine.
- Web fetching: no page could be read.
- Google Search Console (your own search data).
- Prompt volume: how often real people ask the market questions is not known.
- JavaScript rendering: pages were read as raw HTML; content added by JavaScript was not seen.

### How to read this report

- Every finding states its **severity** (critical, high, medium, low, info), its **evidence class** (OBSERVED: measured by this audit's own requests; INFERRED: drawn by a stated rule; ESTIMATED: an estimate with its assumption), the **rule** that produced it, and a **recommended fix**.
- There is no overall score and no grade. Findings of different kinds are not added together.
- This report does not promise rankings, traffic or citations by any search or answer engine, and it makes no comparison claims about any other provider.
- Text from your pages, sitemaps and robots.txt is quoted as data only. Other URLs are shown as templates (identifiers replaced by placeholders such as {slug} or {number}); the exact URLs are in the audit record.

## Crawlability

**Status: NOT CONNECTED.** Whether search and AI crawlers can reach and index the audited pages: ` robots.txt `, status codes, redirects, noindex signals and canonical links.

Data freshness: not observed in this audit.

The data source for this area is not connected for this audit. Nothing is reported here because this area was not checked. This is not a clean result.

## Sitemaps and ` llms.txt `

**Status: NOT COVERED.** Whether the XML sitemaps can be read and list the right URLs, and whether an ` llms.txt ` file is published (an emerging, non-standard convention).

Data freshness: not observed in this audit.

This audit did not include this check. Nothing is reported here because this area was not checked. This is not a clean result.

## Content structure

**Status: NOT COVERED.** How each audited page is structured for machines: title, description, headings, language, structured data (JSON-LD) and how much text is in the raw HTML.

Data freshness: not observed in this audit.

This audit did not include this check. Nothing is reported here because this area was not checked. This is not a clean result.

## Entity consistency

**Status: NOT COVERED.** Whether the business details in the pages' Organization / LocalBusiness markup match your canonical business record (name, address, phone).

Data freshness: not observed in this audit.

Not enough could be observed to report on this area. Nothing is reported here because this area was not checked. This is not a clean result.

## Search performance (Google Search Console)

**Status: NOT CONNECTED.** Clicks, impressions, click-through rate and position from your own Google Search Console property, when it is connected.

Data freshness: not observed in this audit.

Google Search Console is not connected to this service. Nothing is reported here because this area was not checked. This is not a clean result. (reason ` GSC_NOT_CONNECTED `)

## AI visibility (answer engines)

**Status: NOT COVERED.** Whether answer engines (Perplexity when connected) cite or mention your site for a fixed set of market questions, sampled several times each. Cited means the engine's search returned a page of your site for the answer (cited = returned by search); it does not mean the answer's text linked to that page.

Data freshness: not observed in this audit.

No set of market questions was chosen for this audit, so no answer engine was asked anything. Nothing is reported here because this area was not checked. This is not a clean result. (reason ` NO_PROMPT_SET `)

Prompt volume: **NOT CONNECTED**. How often real people ask these questions is not known.

## Methodology and limits

### Applies to the whole audit

- Wave 1 reads public pages only; nothing here is paired with traffic, conversions or revenue (credit is not causation).
- No single visibility score is produced.

### Crawlability

**Method.** no fetcher is wired

### Entity consistency

**Method.** Organization / LocalBusiness JSON-LD compared with the canonical entity record.

### Search performance (Google Search Console)

**Method.** Search Analytics for the bound property.

### AI visibility (answer engines)

**Method.** No prompt set was given for this audit; no engine was asked anything.

### Versions

- bot families: ` 2026-10-10.1 `
- report: ` seo-audit-report/1 `
- schema rules: ` 2026-10-09.1 `
- report renderer: ` seo-client-report/2 `

---

Prepared by Z Best Media. This report recommends changes; it does not make any change to your site.
