# SEO technical audit: ` acme.com `

Prepared by Z Best Media.

## Cover

- **Website audited:** ` https://acme.com `
- **Audit ID:** ` seo-aud-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa `
- **Run:** 2026-10-24 09:00:00 UTC to 2026-10-24 09:02:30 UTC
- **Run outcome:** OK (whether every check could run; not a score)
- **Audit record:** report SHA-256 ` bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb `, recorded on a tamper-evident ledger when the audit completed.
- **Pages audited (1):** ` / `

### What was checked

| Area | Status | Findings | Data freshness |
|---|---|---|---|
| Crawlability | **NOT COVERED** | — | not observed in this audit |
| Sitemaps and ` llms.txt ` | **NOT COVERED** | — | not observed in this audit |
| Content structure | **NOT COVERED** | — | not observed in this audit |
| Entity consistency | **NOT COVERED** | — | not observed in this audit |
| Search performance (Google Search Console) | **NOT COVERED** | — | not observed in this audit |
| AI visibility (answer engines) | **CHECKED** | 1 | observed during the run; refresh recommended by 2026-10-31 |

Findings are counted per rule (one rule may affect several pages). An area marked NOT CONNECTED, NOT COVERED or NOT CHECKED was not observed: it has no findings because it could not be looked at, not because there is nothing there.

### How to read this report

- Every finding states its **severity** (critical, high, medium, low, info), its **evidence class** (OBSERVED: measured by this audit's own requests; INFERRED: drawn by a stated rule; ESTIMATED: an estimate with its assumption), the **rule** that produced it, and a **recommended fix**.
- There is no overall score and no grade. Findings of different kinds are not added together.
- This report does not promise rankings, traffic or citations by any search or answer engine, and it makes no comparison claims about any other provider.
- Text from your pages, sitemaps and robots.txt is quoted as data only. Other URLs are shown as templates (identifiers replaced by placeholders such as {slug} or {number}); the exact URLs are in the audit record.

## Crawlability

**Status: NOT COVERED.** Whether search and AI crawlers can reach and index the audited pages: ` robots.txt `, status codes, redirects, noindex signals and canonical links.

Data freshness: not observed in this audit.

This audit did not include this check. Nothing is reported here because this area was not checked. This is not a clean result.

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

This audit did not include this check. Nothing is reported here because this area was not checked. This is not a clean result.

## Search performance (Google Search Console)

**Status: NOT COVERED.** Clicks, impressions, click-through rate and position from your own Google Search Console property, when it is connected.

Data freshness: not observed in this audit.

This audit did not include this check. Nothing is reported here because this area was not checked. This is not a clean result.

## AI visibility (answer engines)

**Status: CHECKED.** Whether answer engines (Perplexity when connected) cite or mention your site for a fixed set of market questions, sampled several times each. Cited means the engine's search returned a page of your site for the answer (cited = returned by search); it does not mean the answer's text linked to that page.

Data freshness: observed during the run; refresh recommended by 2026-10-31.

| Engine | Status | Questions asked | Not asked |
|---|---|---|---|
| ` openai ` | CHECKED | 1 | 0 |

- ` openai `: Each of the 1 question(s) asked was asked 3 times.

Question set version 1, fingerprint ` bbbbbbbbbbbbbbbb `. Questions are referred to by number; their text and the answers are not reproduced.

Prompt volume: **NOT COVERED**. How often real people ask these questions is not known.

### Mentioned but not cited

- **Severity:** medium · **Recommended action:** Test a change
- **Evidence:** OBSERVED (measured by this audit's own requests) · **Rule:** ` CITATION_MENTIONED_NOT_CITED ` · **Area:** ` P4 `
- **Fix:** Publish a clear, crawlable page that answers this question, so engines can link to it.
- **Evidence:** engine: ` openai `; question: Question 1; usable answers: 3; mention rate: 100.0% (95% interval 43.9% to 100.0%); cited instead: ` competitor.test (3 answers) (a competitor you named) `

## Methodology and limits

### Applies to the whole audit

- Wave 1 reads public pages only; nothing here is paired with traffic, conversions or revenue (credit is not causation).
- No single visibility score is produced.

### AI visibility (answer engines)

**Method.** Each prompt of prompt set core v1 sent 3 times per engine. An answer is 'mentioned' when a brand term appears (case-insensitive, whole words) and 'cited' when the engine's search returned a web page (http or https) on ` acme.com ` or a subdomain for that answer: cited = returned by search (IDNA-aware; lookalike hosts are counted apart, never as ` acme.com `). Whether the answer's text points at that page is not measured. A URL written in an answer's text is not a citation, nor is one the model attached that its search did not return, except for an engine that returns no structured citations (Perplexity returns them). Rates exclude refusals and errors; intervals are 95% Wilson score intervals over usable answers. Classes: STRENGTH (cited in \>= half), MENTIONED\_NOT\_CITED, MENTION\_UNCONFIRMED (named only as a word that may not be the brand; mention rate unknown), OPPORTUNITY (others cited, not us), ABSENT, INCONCLUSIVE (not cited, but a citation was an unresolved redirect), INSUFFICIENT\_EVIDENCE (\< 3 usable answers).

**Limits.**

- One sample of N answers at one time from one account; answer engines vary by user, location, time and model version. These are observations, not search truth.
- No prompt-volume source is connected: how often real users ask these prompts is unknown.
- No single visibility score is produced; nothing here is calibrated against traffic or revenue.

### Versions

- bot families: ` 2026-10-10.1 `
- report: ` seo-audit-report/1 `
- schema rules: ` 2026-10-09.1 `
- report renderer: ` seo-client-report/2 `

---

Prepared by Z Best Media. This report recommends changes; it does not make any change to your site.
