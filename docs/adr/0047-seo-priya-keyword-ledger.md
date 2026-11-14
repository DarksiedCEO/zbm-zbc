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

### The closed threat list (owner decision 2026-11-08; certified from round 18)

Certification of the personal-data rule is against THIS list and nothing else; anything outside it is a documented
residual (below). Classes: **card numbers** (13-19 digits, Luhn-valid, a card network's issuer prefix, the UATP
prefix 1 with 15 digits), **phone numbers** (NANP: 10 digits, or 11 after a 1, area code and exchange starting 2-9;
E.164: a '+' and 8-15 digits; vanity toll-free: 800 / 833 / 844 / 855 / 866 / 877 / 888 and seven keypad characters
with a letter, with or without the leading 1), **SSNs** (9 digits with a valid area, group and serial), **e-mail
addresses** and **street addresses** (the address rule below). Disguises, each alone and inside surrounding text:

1. any gap of up to 3 non-digit characters between two digits - ASCII letters, letters NFKC turns into ASCII
   letters, other scripts' letters, and any separator (punctuation, symbol, space);
2. digits in any script (every Unicode decimal-digit block) or fullwidth;
3. zero-width and bidi characters anywhere;
4. spelled digits (English and Spanish number words, mixed with digits - one word is enough - run together,
   glued to gap letters or separated; a number word inside a longer ordinary word, `phone`, is not one: priya-spec/1.1
   C-4);
5. e-mails spelled (the CLOSED lists of priya-spec/1.1 C-1, any case: at-words `at`, `arroba`, `arobase`,
   `chiocciola`, `klammeraffe`, `at-zeichen`, `malpa`, `kukac`, `snabel-a`, `apenstaartje`, `sobaka`; dot-words
   `dot`, `punto`, `point`, `ponto`, `punkt`, `kropka`, `pont`, `prikk`, `piste`, `nokta`, `titik`; any top-level
   domain of 2-24 letters; also written with `_` / `-` around the words; glued to fullwidth / ideographic
   punctuation and CJK context, C-2), bracketed (`[at]`, `(at)`, `{at}`, `<at>`, `[@]`, `[.]`,
   `(dot)` ...), base64 (standard or URL-safe, padded or not), base32 (either case, padded or not), hex,
   URL-encoded (whole or partly), rot13 or reversed (the whole text or each word), each whole or chunked (split
   into pieces with spaces);
6. composed pairs: two of these transforms applied to one value (measured residual at most 1%).

A query that ADR 0017's classifier calls `pii_suspect` (e-mails in any spelling, one phone-like run of 7+ digits not
an ISO date, id- or secret-like words, and — added in review round 1, reworked in rounds 2 to 4,
`search_performance` rules `2026-11-08.20`, so Osei's query classes use it too - address forms and over-long text):
- an id- or secret-like word (round 4 narrowed this from the log templater's rule, which dropped model numbers, tire
  sizes, decimals and years — `24acc636a003`, `225/65r17`, `3.5`, `2012` — as ids): an e-mail in any spelling, a
  UUID, 16+ hex characters, a JWT-like dotted token, 20+ token characters, anything over 40 characters, a
  letters-and-digits word of 16+ characters or of 8+ characters switching between letters and digits at least 60% of
  the time (`a1b2c3d4e5f6g7`), a file name (`jane_cv.pdf`), and (round 5) a base64- or AWS-secret-like word holding
  `/`, `+` or `=` (20+ such characters with a digit, `+` or `=`, or 12+ with two of them); pieces between
  `/ ? & = # ; :` judged by the token rules; known secret prefixes (AWS `akia`/`asia`, Google `aiza`, GitHub, Slack,
  Stripe-style); credential query parameters (`token=`, `key=`, `password=`, `sig=`, … with 6+ characters); and
  **split secrets** (round 8): each word with `/ - _ .` removed is judged by these rules
  (`abcd-1234-efgh-5678-ijkl-9012`), and the whole query with spaces removed by the prefix, letter-digit
  alternation and (round 9) hex rules (16+ hex characters with 4+ digits and 4+ letters: `9f86d081 884c7d65 …`);
  base64 of readable text, split or not (round 9: case kept, separators removed, decodes to 9+ nearly all printable
  bytes: `amFuZS5kb2 VAZXhhbXBs ZS5jb20=`);
- **one shared character normalisation** (round 10, `search_performance.clean_text`): the classifier's detection
  form and Priya's stored key (`priya.normalize`) use the same rules — NFKC; format characters, default-ignorables
  and variation selectors deleted; control (Cc), private-use (Co), unassigned and every space / line / paragraph
  separator (U+00A0, U+2007, U+202F, U+3000 …) made one space; ideographic and fullwidth full stops read as `.`.
  The detection form also removes every combining mark after NFKD and folds other-script digits (`٤`, `४`) to
  ASCII; the key form removes only marks sitting on ASCII. A test asserts that `normalize(q)` of every DROP case is
  itself dropped, so a value split by a control character can never reach the ledger joined;
- the separator-stripped text of a query **chunked like a secret** (round 10: at least two words mixing letters
  and digits, and more than half of all words) is judged by the FULL word rule set — 20+ token characters, 16+
  mixed, alternation, hex, and an entropy check (16+ characters at 3.5+ bits per character);
- base64 (round 10): the URL-safe alphabet (`-`, `_`) is mapped to the standard one before decoding, and `-` `_` `.`
  are also tried as separators; a decode counts only with a base64 tell (`=` padding, `+` / `/`, or a 16+
  character mixed-case-and-digit body of length divisible by 4 with entropy 3.5+) or when the decoded text is
  itself personal data — 33 mixed-case English and Spanish queries (`PlayStation Plus Games`, `Mejor Restaurante
  En Madrid`) are KEEP tests;
- spelled-out digits (round 10): 7+ number words in a row (`four one one one …`) are read as digits for the digit
  run and card rules;
- **combining marks and format characters** (round 9): every check runs on a detection form with all Mn / Me / Cf
  characters and variation selectors removed after NFKD (`5́ 5 5`, keycap `4⃣`, underline `1̲`,
  `Á Ḱ …`), and ideographic / fullwidth full stops (`。`, `｡`, `．`) read as `.`. The stored ledger key
  (`priya.normalize`) drops stray marks sitting on an ASCII character and every format character; marks that belong
  to a script (Devanagari, Thai, Hebrew points) and NFKC-composed letters (`café`) are kept;
- **URL-like queries, dropped whole** (round 8, fail safe; rounds 6-7's path exemptions were an arms race and are
  removed): after up to three percent-decodes, NFKC and the lookalike fold, a query with a scheme or `://`, `www.`,
  a percent escape still left, an IPv4 address, a host name (label + a listed gTLD or any two-letter country code
  except file-type words such as `js`, `py`, `md`), `host:port`, or a word holding `/` that is not a fraction
  (`1/2`, `24/7`), a tire size (`225/65r17`) or a listed pair (`a/b`, `and/or`, `w/o`, `n/a`, …). **Every URL is
  dropped, ordinary ones included** (`docs.example.com/v2/api/reference`, `shop.com/search?q=rugs`): URL-shaped
  search queries are rare and of little value in a keyword ledger, and privacy beats recall. Also dropped as URLs:
  tech names that are domains (`asp.net`); `50% off` and `node.js` are kept;
- a **card number** (round 8): a Luhn-valid run of 13-19 consecutive digit groups, whatever separates the groups —
  separators and (rounds 9-10) at most one filler token of up to three characters (` x `, `-ab-`, ` xy `), every
  window of consecutive groups tried — whatever its groups look like (`4000 2019 2020 2021`,
  `4539 x 1488 x 0343 x 6467`); a list of years that happens to pass Luhn is dropped too;
- URL-like additions (round 9): IPv6 literals (`2001:db8::1`, `fe80::…`, `::1`), hex IPv4 (`0xc0a80101`; decimal
  `3232235777` is the digit-run rule's), `javascript:` / `vbscript:` / `data:` / `file:` / `blob:` / `mailto:` /
  `ftp:` schemes, and hosts written with spaces or "dot" (`example . com`, `example dot com`, `example (dot) com`,
  `example [.] com`) for common TLDs, the label not a stop word (`the dot com bubble` is kept).
  **Still not caught (round 9, disclosed):** dotted octal or hex IPv4 (`0300.0250.1.1`, `0xc0.0xa8.1.1`); a
  "dot" host with an uncommon TLD; base64 of binary data split into short words; a time-like `hh:mm:ss` is kept
  and an IPv6 literal of three short groups could be missed. An address (IP, IPv6, URL) spaced out character by
  character is not caught.
- **Round 11.** The separator-stripped text of a secret-chunked query gets the alternation, hex, prefix and entropy
  (4.1+ bits) rules and an equal-length-chunks rule, but **no length rule**: round 10's length rule dropped spec
  queries (`12v 100ah lifepo4 battery`, `r410a r22 refrigerant`); 39 spec / model-number queries are KEEP tests.
  9+ digits across filler tokens set apart by separators (`123 x 45 x 6789`, `1234 -ab- 5678 -ab- 9`) are dropped
  (ordinary words and units — `in`, `to`, `vs`, `km`, `mhz` glued to a number — never join two numbers, so `cars
  under 30000 in 50000 miles` and `ddr5 6000mhz cl30 32gb` are kept); letters used as digits inside digit-like
  words (`4lll`, `SSS-OlO-Ol99`: l/i/| → 1, o → 0, s → 5, b → 8, z → 2) for the 9+ digit and Luhn rules;
  dingbat, negative circled and other enclosed digits (`❶`, `➀`, `⓵`); Spanish and mixed spelled digits, number
  words run together once separators are removed (forwards and reversed); 12+ one-character words judged as one
  token (forwards and reversed); base64 padding after case-folding; spaced or "dot" IPv4 (`192 . 0 . 2 . 1`,
  `192 dot 0 dot 2 dot 1`).
- **The attack corpus** (`tests/attack_corpus.py`, rounds 10-11): public test card numbers, AWS's documentation
  example key, a SHA-256 hex digest, an example JWT, base64 and percent-encoded e-mail / key, RFC 5737 / RFC 3849
  addresses and example.com, under every obfuscation — combining marks (acute, keycap, underline), zero-width,
  bidi, fullwidth, mathematical, Cyrillic homoglyphs, circled / superscript / dingbat / negative circled / double
  circled / parenthesized / full-stop / Arabic-Indic / Devanagari digits, letter-for-digit homoglyphs, eleven
  separators (each character / each group), English, Spanish and mixed spelled digits, reversal, filler letters /
  tokens, interleaving and case changes. **Single obfuscation: 478 generated cases, all dropped** (round 10's text
  said 388 cases — the count was 390 with its two pinned gaps; both gaps are closed now). **Composed mode** (two
  obfuscations, the second applied to the first's output). Round 11 tested a one-in-11 sample (1,748 cases, 5
  kept); the round-12 reviewer ran **every ordered pair (18,826 cases)** and found 57 kept (0.30%) in classes the
  sample had missed — filler digit runs written with homoglyph letters, a single spelled word in a 9+ digit run,
  homoglyph digits next to spelled words, and spelled words with a filler or an interleaved character inside them.
  Round 12 closed all of them (filler runs on the homoglyph view; one spelled word enough in a 9+ digit run;
  spelled digits looked for again with the most repeated short filler token, or an interleaved character at
  alternating positions, removed). **All 18,826 pairs are now dropped, and the committed test runs every pair**
  (`tests/test_query_privacy_r12.py::test_all_composed_pairs_drop`, in the default counted suite, about 10-20 s),
  with round 11's sample kept as a fast check. What is still untested: three or more obfuscations stacked, and
  obfuscations not in the corpus. **Correction (round 16):** "no composed residual" was true of THIS corpus only.
  The round-16 reviewer's own independent corpus (not read here) still kept **0.21%** at 60525ea7, in the classes
  named in the review — phone / SSN separators outside space . - (bullets, tildes, asterisks, katakana and other
  middle dots, Unicode dashes, fraction and division slashes), 7-digit local numbers in version shapes and vanity
  toll-free numbers — all fixed in round 16 and added to this corpus (eleven Unicode separators: 40,672 pairs).
  The committed test pins this corpus's residual at **0 of 40,672**; the examples behind the reviewer's 0.21% are
  theirs and are not reproduced here.
- **Round 13 (over-drop fixed).** Round 12 let ONE spelled number word count in a run of 9+ *tokens*, and found
  that word inside ordinary words (`ph-one`, reversed `ho` in hotel / honda): 32 of the reviewer's 115 year / date
  queries were dropped (`iphone 2023 2024`, `2024-12-25 hotel deals`, `2010 2020 ohio state`). Now one word counts
  only as a whole word in the query (never inside another word, never in run-together or reversed text) with 9+
  real digits, after the year-list (two or more years 1900-2099) and date exemptions; and calendar dates in every
  common order and separator with months and days in range (`2025/11/08`, `11/08/2025`, `08.11.2025`,
  `12-25-2024`) are never digit runs, cards or filler runs — closing an older over-drop of non-ISO dates
  (`11/08/2025 iphone` had been dropped since round 3). A 1,080-query year / date probe (10 year forms and 8 date
  forms × 30 common words, both orders) drops 0, and 45 curated year / date KEEP tests pass
  (`tests/test_query_privacy_r13.py`); all 18,826 composed pairs still drop.
- **Round 14 (a date must never hide a value).** Round 13 exempted every calendar date from the digit rules, so a
  card written partly as a date passed — the documented Mastercard 2-series test number written as `22230
  03/12/2003 222` (and `.` `-` `/` variants) — as did phones and SSNs next to a one-digit-month date (`2025/5/5
  0199`, `2 1909/9/9 99`); older code also stripped every ISO / slash date unconditionally inside the digit-run
  and filler rules. Now a date is exempt **only when it stands alone** (`isolated_dates_removed`: every digit
  within 12 characters on either side belongs to a date too), and the card (Luhn 13-19), digit-run (phone / SSN)
  and filler rules all run on that text, i.e. on the original digit stream wherever a date touches other digits.
  A probe that writes every date-shaped window of public test card numbers, 555 phones and an example SSN as a
  date (three orders, four separators, five glues: 200 cases) drops all of them. Over-drops fixed: date ranges
  (`2025-11-08/2025-11-09 hotel`, `25.12.2024-01.01.2025 urlaub`), year lists in a card window (a window made
  only of 19xx / 20xx groups is not a card — no card network issues numbers of that shape — and one non-year
  group makes it one again), decades (`1960s 1970s music`), spaced and compact dates (`2025 01 01 holiday`,
  `20251108 log`) and version numbers after their keyword (`firmware 2.3.11.4`, `version 10.0.19045.3803`, `rfc
  9110 2022`). **Kept as a disclosed over-drop:** a ZIP-sized number next to one year (`90210 2024
  restaurants`, `10001 2025 apartments`) — exempting it would let a 9-digit SSN regrouped 5 + 4 (`12345 2019`)
  through, and privacy takes precedence. Tests: `tests/test_query_privacy_r14.py`.
- **Round 15 — THE RULE (structural).** Three rounds in a row an exemption reopened a leak (round 14's version
  exemption let `v 4111111111111111`, `build 123456789`, `ios 202.555.0199` through). So: **the card (Luhn 13-19
  and a card network's issuer prefix), phone (NANP and E.164 shapes) and SSN (valid area / group / serial)
  detectors ALWAYS run on the raw normalised text and on its de-separated digit runs, with NO exemption of any
  kind — no date, time, year, version, keyword or ZIP rule ever changes what they see** (`pii_detectors`, called
  before any exemption is computed). Exemptions — calendar dates and times, year lists, dotted version numbers
  after their keyword (2-4 parts, each ≤ 5 digits), standards' numbers (`rfc 9110`), ZIP + year — may ONLY
  suppress the generic digit-run and token rules (and the IP check). The de-separated search reads every window of
  whole consecutive digit groups in a run as one number (`2025/5/5 0100 2024` → 202-555-0100) whenever the run holds
  a group that no date, time or year accounts for; a run of nothing but dates, times and years cannot spell a number
  someone chose, and a digit inside a mostly-letter word (`y2k`) is not a group. Cards need an issuer prefix (a
  property of card numbers, replacing round 14's year-window rule): `2001 2002 2003 2004` is not a card. Date + time
  (`11/8/2025 7pm`, `2025-11-08T09:30`, `2025-11-08 09:30 utc`) is one date token for the generic rules.
  **Disclosed structural over-drops:** a query whose digits ARE a valid card, phone or SSN however they are
  grouped — `10001 2025 apartments` (100-01-2025), `version 10.0.19045.3803` (190-45-3803), a date range whose 16
  digits are a Luhn-valid Mastercard-range number (`25.12.2024-01.01.2025`); `90210 2024 restaurants` is kept (902
  is not an SSN area). A committed property test (`tests/test_query_privacy_r15.py`) writes 10 fake cards / phones /
  SSNs in 5 formats next to, around and inside 18 exemption shapes (3,650 queries) and behind 12 keywords (1,200
  queries): every one is dropped.
- **Round 16.** (1) Detector separators by Unicode CATEGORY, never a list: in the detectors' view every run of up to
  16 punctuation (P*, every dash), symbol (S*), space (Z*) characters — continuing through marks (M*), after NFKC —
  between two letters or digits is a separator (`934•333•2089`, `878–70–3759`, `202・555・0100`, U+2057, U+1D15E);
  the generic digit rules and the token squashes read separators by category too; the detectors also run on the
  text before percent-decoding (`202%555%0100`). A scan of all 7,741 such characters (every P / S / Z character
  NFKC does not turn into a letter or digit) between the groups of a test card, two phones and an SSN, bare and
  with `rfc` / a year / a date (123,856 queries): **0 kept**; the committed property test draws every P* and Z*
  character and every eighth S* character from `unicodedata`. (2) New phone shapes: 7-digit local numbers with a
  punctuation or symbol separator (`version 814.0202`, `555-0199`, `814•0202`; a plain space is the generic
  rule's) and vanity toll-free numbers (`1-800-flo-wers`, `800 go fedex`). **Measured over-drop of the 7-digit
  rule:** 1 of 16 number-shaped ordinary queries (`300-0100 code`, itself a local-number shape) and 0 across every
  KEEP table and probe in the repository. (3) The ` at ` e-mail rule is now this classifier's own, e-mail SHAPED —
  `<name> at|(at)|[at] <domain> dot|.|[dot] <tld>`, or `<name> at <mail provider>`; `logs._AT_WORD` (the log
  templater's) is unchanged. Round 15's rule dropped about 98.5% of ` at ` queries; on 400 ordinary ` at ` queries
  (`deals at walmart`, `meeting at 2pm`) it now drops **0**, while `john at gmail dot com`, `john (at) gmail (dot)
  com`, `john at gmail.com`, `jane at gmail` still drop. (4) **Context never hides a value** (war room WR-F020 /
  WR-F021, PR #57): besides the whole query, the detectors and the base64 check run on every maximal run of
  non-ordinary tokens (`_suspicious_windows`), so words, tabs, emoji and CJK around a value
  (`rug cleaner amFuZS5kb2 VAZXhhbXBs ZS5jb20=`) never stop a match; the generic rules never run on a window (their
  exemptions need the query's context). Wrapped in context: 9,464 single-corpus queries (14 contexts) and 4,698
  from a composed sample (6 contexts) in the committed tests, and 52,962 from the one-in-11 composed sample (14
  contexts) offline — **0 kept**. All 12 known war-room failures (R0001, R0003-R0005, R0007, R0010-R0012, R0016-R0019)
  pass on replay with this classifier.
  7+ digits in one run stay the digit-run rule's;
- **Round 18 - the closed list, covered** (SUPERSEDED: the independent review of round 18 contradicted the numbers
  of this entry - its corpus lacked shapes of the list; round 19 below gives the numbers measured by the frozen
  spec's conformance suite) (rules `2026-11-08.18`; every number below was measured on 65f8b92a and on
  this round's code with the generator `tests/privacy_r18_corpus.py`, synthetic values made at run time).
  **What changed:** (1) THE DIGIT STREAM (`digit_stream_hit`): digit groups whose gaps are at most 3 non-digit
  characters of any kind form one stream, and every window of whole consecutive groups is read as one number -
  a card (`stream_card`: Luhn, issuer prefix, UATP), an SSN, a NANP phone (`nanp_phone`, area code and exchange
  2-9) or, after a '+', an E.164 phone; it runs on the detection form (fullwidth and other-script digits already
  ASCII), on the text before percent-decoding, and on the spelled view across gaps (`spelled_stream`: number words
  made digits, separators removed, so the gap is the letters between them), with no exemption of any kind, so a
  date or a time can never hide a value; (2) vanity toll-free numbers with any separator of up to 3 characters
  (`_vanity_any`); (3) e-mail views (`email_views_hit`): a marker string where spaces, brackets and punctuation are
  one marker, spelled keywords read only between markers (so `what`, `dotted` are not keywords) and tolerating chunk
  spaces inside keywords and labels, read as written, rot13'd, reversed whole and word by word, and after reading
  the keywords also rot13'd and reversed, must show a mailbox shape (a TLD of a closed list or any two letters);
  (4) encoded e-mails (`encoded_email`): every run of up to 12 words made of encoding characters is joined and
  decoded (percent, base64, base32, hex; as written, rot13'd, reversed; and once more for two encodings), and
  lower-case base32 written as plain words is decoded from each word to the run's end (`_plain_base32`);
  (5) an address followed or preceded by other text (`_address_core` sets aside emoji, CJK and punctuation; an
  ordinary-word suffix may be followed by up to three tokens of any kind); (6) the issuer table missed Diners 36
  (`3[4789]` read 34, 37, 38, 39): fixed; (7) software names (`chrome`, `python`, `node`, ...) are version keywords
  for the GENERIC rules only. **M1, the list, single disguise** (2,000 values per class, every disguise, alone and
  in context: 244,000 queries): 65f8b92a kept **18,345**; this round keeps **0** - per class kept at 65f8b92a /
  now: card 42 / 0, NANP phone 4,305 / 0, E.164 phone 1,452 / 0, SSN 6,976 / 0, vanity 39 / 0, e-mail 5,283 / 0,
  address 248 / 0. Through the service path (fake Search Console rows, Priya's ledger, the POST response, the run
  views, the audit export, every file under the data dir, every log record; the same 244,000 queries as rows): 65f8b92a let 18,345 into the returned ledger and the POST
  response (the ledger is handed out once and never stored, so 0 reached the data dir or the logs); this round:
  **0 in the ledger, 0 in the API responses, 0 on disk, 0 in the logs** (1,491 ordinary rows kept, none past the cap).
  **M2, composed pairs** (40,000 cases, each class and ordered transform pair in turn, a quarter in context):
  65f8b92a kept 4,777; this round keeps **2 (0.005%)**, pinned in `tests/test_query_privacy_r18.py` - a phone
  spelled with the number words run together and then a mixed gap put between digits, and an SSN with a mixed gap
  and then spelled run together (number words glued to letters and symbols with no separator are not read as
  digits). **Lost protection: 0** - every case 65f8b92a drops in a personal-data corpus is still dropped, cell by
  cell, over M1, M2 and the round-10 attack corpus (676 single, 40,672 composed). (In M3, ordinary queries, 84 cases
  65f8b92a dropped are kept now: those are the over-drops fixed below.)
  **M3, ordinary queries** (24,000; 80% text classes - plain, multilingual, questions, local intent - and 20%
  digit-bearing classes; that mix is an assumption, so each class's own rate is given): dropped at 65f8b92a **204
  (0.850%)**, now **162 (0.675%)** - ABOVE the round's 0.5% bar; see "Owner decision" below. New drops, **42**, both
  made personal data by list item 1: a date followed by a time (22 of 600 date queries: `<date> 3am` spells an SSN or
  a NANP phone across gaps of at most 3 characters) and a tire size with its load index (20 of 720 size queries:
  `<width>/<ratio>r<rim> <load><speed>` spells a NANP phone). Over-drops fixed, **84**: version numbers after a
  software name, 23 (generic rule), and `ddr<n> <speed> <size>gb` model numbers, 61 (generic 7-digit run: a number of
  one or two digits glued to a word at either end of a run, `ddr5`, `64gb`, is part of that word; a lone group, and
  any group of three or more digits, still counts; `longest_digit_run`). Still dropped: ZIP + year 65 of 480 (64:
  the 9 digits are an SSN; 1: a ZIP starting `1866` followed by a word is a vanity toll-free number), version numbers
  53 of 480 (46: the digits across the dots are a NANP phone or SSN of the list; 5: the round-15 de-separated rule's
  phone shape, whose exchange may start 0 or 1; 2: the round-12 homoglyph view, `bios` read as digits), model numbers
  2 of 960 (a 5-digit number starting `877` / `866` followed by a word is a vanity toll-free number). 0 dropped in
  plain text, multilingual text, questions, local intent, prices, years and quantities. With a smaller share of
  digit-bearing queries the total falls in proportion (the text classes drop 0). Nine KEEP cases of rounds 13 and
  15 are dropped now by list item 1 and are asserted dropped (`LIST_MANDATED_DROP`): eight dates followed by a time
  and `1999 2000 y2k`.
  **Owner decision (open):** of the 162 M3 drops, **155 are made personal data by the closed list itself** (a
  value's digits across gaps of at most 3 characters: date + time 22, tire size + load index 20, version numbers
  46, ZIP + year 64, vanity toll-free 3), so 0.646% of this corpus cannot be kept without letting a list case
  through (for example an SSN written 5 + 4, which is exactly ZIP + year). The other 7 (5 + 2 above) are older
  rules outside the list. The 0.5% bar and zero leaks on the list cannot both hold on this corpus; which one gives
  way, or whether the list is narrowed (for example ZIP + year, 64 cases), is the owner's call.
  **Cost:** median per-query classification on M3, 161.1 / 162.6 us at 65f8b92a and 183.3 / 176.8 us now in two
  interleaved runs (+13.8% / +8.7%; one shared machine with other jobs running, best of 3 per query); on the list
  sample the median fell from 138.2 / 137.3 to 83.5 / 82.2 us;
  identical detector calls are no longer repeated. Queries over 512 characters are not examined (as before), and the
  work per word is capped (each candidate stops at 12 words or 400 characters, each digit window at 19 digits), so
  no input backtracks catastrophically; the worst inputs found, at 512 characters, best of 5: 29.0 ms (base64-like
  chunks; 1.3 ms at 64), 15.3 ms (one-character words), 6.9 ms (`a at b dot` runs), 3.5 ms (spelled words), 1.3 ms
  (digits with letter gaps; 65f8b92a took 82-105 ms there); 64k-character text is not examined (about 10-45 ms,
  dominated by normalising it).
  **Residuals (outside the list, or measured):** the 2 composed cases above; 7-digit local numbers with letter
  gaps (the list's phones are NANP / E.164 / vanity; a plain 7-digit run is still the generic rule's); gaps of 4 or
  more characters (the older filler rules catch some); number words of languages other than English and Spanish;
  three or more stacked transforms; encodings other than the list's (base85, uuencode, ...); lower-case base32 of
  an e-mail shorter than 16 characters with no digit 2-7 in any chunk; a chunked, rot13'd or reversed spelled e-mail whose TLD is
  not two letters and not on the list (`_EM_TLD_WORDS`; written plainly, the older rule reads any TLD); the address rule's own gaps (below); personal names.
- **Round 19 - certified against the frozen spec `priya-spec/1`** (owner-approved 2026-11-13, every owner question at
  its default; rules `2026-11-08.19`). From this round the numbers are the permanent conformance suite's,
  `services/seo-py/tests/conformance/` (`test_priya_*.py`, registry `registry.json`, item ids in the test names;
  lanes `conformance` on every push and `conformance_heavy` with `CONFORMANCE_HEAVY=1`), measured on the final code
  and on 644b4901 / 65f8b92a; round 18's numbers were contradicted by the independent review and are replaced by
  these. The generators (`tests/conformance/priya_gen.py`) and the independent oracle
  (`tests/conformance/oracle/priya_list.py`, written from the list's text, importing nothing of the classifier)
  were built by the round-19 builder from the spec's text; the spec's reference harnesses were not used, so the
  boundary set is this suite's own enumeration (14,300 cases, not the reference's 70,346) and the frozen baseline
  bits of SUITE_PLAN C-1 are not committed (the lost-protection test reads both baselines from git history and
  skips without it).
  **What changed:** (1) vanity toll-free numbers (`_VANITY_ANY`): any gap of the list between the digits of 1 + 8xx
  (`8-0-0`, `8;88`, letters inside the code), also on the spelled views (the code as number words); (2) spelled
  e-mails: every at-word (`at`, `arroba`, `arobase`, `chiocciola`, `klammeraffe`) and dot-word (`dot`, `punto`,
  `ponto`, `punkt`), in the e-mail views and the older `_AT_EMAIL` rule; any top-level domain of 2-24 letters in
  every reading, reversed ones included (so `polka dot dress at target`, which read backwards is a mailbox with
  the TLD `polka`, is dropped: the oracle labels it list-mandated); `_` / `-` read as spaces (owner item OI-2);
  letters and marks of other scripts are markers in the e-mail views, so context in another script inside a
  reversed or chunked spelled e-mail never breaks it; a URL-encoded e-mail chunked inside a percent escape is
  joined again; (3) the ordinary-suffix address rule takes any amount of text after the street, and non-ASCII
  punctuation glued to a word (a CJK corner bracket between a word and the house number) separates words for the address rule; (4) spelled digits
  (`spelled_views`, `_spelled_hit`): number words read also inside a word when every leftover letter run is a gap
  of the list (`fivexzero`, `2one5`, `dosp`), and ONE number word is enough, with no date or year exemption (THE
  RULE; spec D14): `iphone 2023 2024`, `2024-2025 one piece`, `the one 2001 2002` spell an SSN of the list and are
  dropped (moved from the KEEP lists of rounds 13, 14 and 18 to list-mandated drops, each labelled mandated by the
  oracle, as G-7 allows); (5) the round-18 glued trim in `longest_digit_run` only for the closed unit / model token
  list (`_GLUED_AFTER`, `_GLUED_BEFORE`; spec LP-3); (6) the digit stream reads a `+` up to three characters before
  the first digit (OI-3); (7) GENERIC exemptions only (`generic_exempt`; the detectors never see them): tire sizes
  spaced, dashed or slashed; UPC-A / EAN-13 / ISBN-13 with a valid check digit after a code word, or an ISBN-13
  alone; codes after a part word; times `h:mm` and day/month pairs; ASINs; dotted version numbers of 3-4 parts and
  at most 8 digits with no keyword (never the 3.3.4 / 4.3.3 phone or 3.2.4 / 4.2.3 SSN shapes); `p/n` is not a URL;
  the 7-digit local rule no longer reads a number glued to a letter; a state code glued to five digits is not a
  state + ZIP; (8) linear work: the encoded-e-mail search decodes each window once as a stream from its start
  (both directions) and splits words at non-ASCII punctuation; the de-separated rule splices its barriers in one
  pass and scans a word only when a group is glued to letters on both sides; (9) Priya classifies a text once
  (the normalised key only when it differs; a derived variant once).
  **M-1, the matrix** (1,365 cells: C1-C7 x the applicable D01-D24 x X0-X12 (alone and in the query text of 12
  languages) x Y (number words en / es; at / dot words en, es, pt, fr, it, de); raw text and stored key, kept =
  Priya keeps the row): CI draw N = 5, 6,825 queries - kept at 65f8b92a 1,426, at 644b4901 1,031, now **0**; heavy
  draw N = 200, 273,000 queries - 55,198 / 39,667 / **0**. **M-2, the boundary set** (14,300 cases, enumerated in
  full on every run): 1,671 / 1,229 / **0**. **Composed pairs** (CP-1..CP-4; 512 ordered pairs, every pair a != b
  outside one family, structural construction): CI 4,000 cases - **0** kept; heavy 42,000 - 6,429 / 4,906 / **0**
  kept, so the worst pair is 0% (bars: <= 1.0% overall, <= 5% per ordered pair). **Lost protection: 0** - no query
  dropped by 644b4901 or by 65f8b92a is kept now, on the matrix (both draws), the boundary set, 42,000 composed
  pairs and the round-10 attack corpus (676 single, 40,672 composed), and every DROP assertion of
  `services/seo-py/tests` passes. **Service path** (fake Search Console AND Bing rows -> Priya -> the POST
  response, the run views, the audit export, every file under the data dir, every log record, the ledger events):
  CI 1,000 list rows + 1,000 ordinary rows, heavy 12,000 list rows + 2,000 ordinary rows: **0 at every output**.
  **OQ, ordinary queries** (the frozen 24,400-query corpus of OQ-1: 20 classes at the spec's sizes, 16,419 (67.3%)
  digit-bearing, 19 languages in the multilingual class): dropped at 65f8b92a 4,980, at 644b4901 5,638, now
  **2,764**; **2,685 list-mandated** by the oracle (the digit stream reads a valid SSN / NANP phone / card across
  gaps of at most three characters: ZIP + year 716 - owner item OI-1, kept dropping -, date + time 731, tire size
  394, version 193, flight 116, size 33, product code 2; the address rule: business addresses 500 of 500) and 0
  mandated kept; **79 not mandated, disclosed** (residual P-r9): version numbers 55 (26 by the generic 7-digit run:
  dotted versions of more than 8 digits with no keyword; 20 by the round-15 de-separated phone shape, whose
  exchange may start 0 or 1; 7 by the round-12 homoglyph view, `bios` read as digits; 2 by both), flights 11 (the
  de-separated phone shape: `<carrier><number> <m/d> <h:mm>`), part numbers 13 (the homoglyph view: a `b` inside
  the code read as 8). Against 644b4901 the not-mandated drops fell from 2,955 to 79 (part numbers 1,500 -> 13,
  product codes 712 -> 0, flights 398 -> 11, tire sizes 281 -> 0, versions 64 -> 55). Narrowing the homoglyph view
  or the de-separated phone shape would lose protection that rounds 11-15 pin (the fictional 555-01xx numbers have
  an exchange starting 0), so they stay, disclosed. The table (columns: class, n, dropped, mandated, not mandated,
  then 644b4901 dropped, 644b4901 not mandated, 65f8b92a dropped):

| class | n | dropped | mandated | not mandated | 644b4901 dropped | 644b4901 not mandated | 65f8b92a dropped |
|---|---|---|---|---|---|---|---|
| plain_text | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| multilingual | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| question | 2000 | 0 | 0 | 0 | 0 | 0 | 0 |
| local | 1500 | 0 | 0 | 0 | 0 | 0 | 0 |
| price | 1200 | 0 | 0 | 0 | 0 | 0 | 0 |
| size | 1200 | 33 | 33 | 0 | 33 | 0 | 0 |
| tire_size | 900 | 394 | 394 | 0 | 675 | 281 | 495 |
| model_number | 1500 | 0 | 0 | 0 | 0 | 0 | 261 |
| date | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| time | 600 | 0 | 0 | 0 | 0 | 0 | 0 |
| year | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip_year | 800 | 716 | 716 | 0 | 714 | 0 | 714 |
| version | 1000 | 248 | 193 | 55 | 257 | 64 | 278 |
| flight | 900 | 127 | 116 | 11 | 514 | 398 | 518 |
| product_code | 900 | 2 | 2 | 0 | 714 | 712 | 714 |
| part_number | 1500 | 13 | 0 | 13 | 1500 | 1500 | 1500 |
| business_address | 500 | 500 | 500 | 0 | 500 | 0 | 500 |
| quantity | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| date_time | 900 | 731 | 731 | 0 | 731 | 0 | 0 |

  **Cost (PF):** measured in process CPU time (the build machine is shared with other builders' jobs: load about 4
  on 2 cores, so wall-clock times there count other processes; the suite's perf module does the same). PF-1, the
  median per-query cost of the whole Priya path on OQ (every 4th query, best of 3, two runs): now 231.4 / 227.2 us,
  at 644b4901 394.7 / 387.1 us, at 65f8b92a 370.0 / 381.0 us (-41.4% / -41.3% and -37.5% / -40.4%: a text whose
  stored key equals it is classified once, PF-3); the classifier alone on both forms, as the baselines ran it,
  444.6 / 450.1 us (+12.6% / +16.3% against 644b4901, +20.2% / +18.1% against 65f8b92a). PF-2, the 27 adversarial
  families at 64-512 characters (interleaved, best of 5, raw and key): worst at 512 characters 21.60 ms (`to 12`
  filler runs), then 11.81 ms (`a at b dot` runs) and 9.72 ms (base64 three deep in 4-character chunks; the review
  measured double base64 at 148.4 ms on 644b4901); the largest ratio of any doubling 2.40x. Over 512 characters a
  query is dropped whole (asserted: a card inside a 600-character query never reaches an output).
  **Owner items, at the owner's decisions (2026-11-13, defaults):** OI-1 ZIP + year whose 9 digits are an SSN: kept
  dropping (716 of 800 in OQ); OI-2 `_at_` / `-at-` / `_dot_` / `-dot-` e-mails: read as spelled (dropped; no OQ
  query is affected); OI-3 E.164: `+` up to three characters before the first digit is read. Each is pinned in
  `tests/conformance/test_priya_owner_items.py`.
  **Residuals (spec section 8, owner-accepted classes):** P-r1 7-digit local numbers with letter gaps; P-r2 gaps of
  4+ characters; P-r3 number words of other languages (and at-words / dot-words other than the nine above); P-r4
  three or more stacked transforms; P-r5 encodings off the list; P-r6 lower-case base32 of a short e-mail with no
  digit 2-7; P-r7 the address rule's gaps (below); P-r8 personal names; P-r9 the 79 not-mandated drops above; P-r10
  URL-shaped queries dropped whole; P-r11 composed residual (0 measured). Also: a number word inside a longer word
  when a leftover letter run is longer than three (`nineteen`).
  **Also pinned (builder corpora, final code):** round 18's ordinary corpus 162 of 24,000 dropped (unchanged since
  round 18), round 19's `ordinary_r19` 1,760 of 24,000, its composed corpus (`composed_r19`, 42,000) 0 kept - in
  `tests/test_query_privacy_r18.py` / `_r19.py`.
- **Round 20 - review rv-priya19 closed against `priya-spec/1` and its v1.1 clarifications** (rules `2026-11-08.20`;
  the round-19 entry above is SUPERSEDED where the review contradicted it: its suite instantiated the at-word /
  dot-word cells narrowly - no French `point` - and its matrix kept 4,133 of the review's 291,200 cases). v1.1
  (lead, 2026-11-13) changes no bar: C-1 closes the at-word and dot-word lists (at: `at arroba arobase chiocciola
  klammeraffe at-zeichen malpa kukac snabel-a apenstaartje sobaka`; dot: `dot punto point ponto punkt kropka pont
  prikk piste nokta titik`; any case, every at-word x every dot-word x any TLD of 2-24 letters); C-2 adds the NFKC
  forms of fullwidth punctuation, the ideographic full stop and comma, and CJK context glued without spaces; C-3 makes
  the composed bar <= 5% for EVERY ordered pair (<= 1% overall unchanged); C-4 says a number word inside a longer
  ordinary word is not a list disguise; C-5 makes the reviewers' generators part of the permanent suite. Every number
  below was measured on the final code by the suite (`tests/conformance/`) or by the same generators.
  **What changed** (`search_performance.py`): (1) `AT_WORDS` / `DOT_WORDS` are the C-1 lists; a hyphenated at-word
  is also read spaced or run together (`_kw_alternation`); a keyword right after a spelled dot-word is the top-level
  domain's letters (`_despelled`: Austria's TLD is the at-word `at`); (2) C-2: the top-level domain may end with a
  full stop before the next marker (`_MAILBOX`: `com` + U+3002 is NFKC `com.`); the address core splits the ASCII forms of
  fullwidth punctuation and a full stop glued to the next word (`_ADDR_GLUE`); every character outside the
  encodings' alphabet separates words for the encoded-e-mail search (`encoded_email`); (3) the per-character view
  `script_view` (the query read one character for one, other-script letters one `#`, symbols NFKC widens kept
  whole) runs the digit stream, the vanity rule, the spelled reading, the e-mail views and the address rule: the
  lookalike fold makes Cyrillic `U+044E` two letters and NFKC makes `U+2036` two characters, so a gap of three there read as
  four or more (review: composed nanp `gap_other -> spelled_en` 8.6%, ssn `spelled_mixed -> gap_other` 7.5%; vanity
  with Cyrillic gap letters 128 - outside the spec but closed by the same view); (4) C-4: a number word inside a word
  is read only as a gap BETWEEN two units (`_number_words_read`, `_segmented`, `_unit_offset`): leftover letters at a
  word's edge count only toward a unit across a stretch of separators with no typed white space (`_WORD_END`: ASCII
  white space and U+3000; other space characters are separators of the list, D06), the whole gap at most three
  characters, so `iphone 2023 2024`, `best phone 2025 2026`, `2024 2025 phone deals` are kept again (restored to the
  KEEP lists of rounds 13 and 14) while `dosP` + U+21D2 U+2994 + `ocho` (a gap of a letter and two symbols between two number
  words) is read; the word is segmented by a linear dynamic programme, not the leftmost match (`ninefdosix`);
  (5) URL-encoded e-mails reversed whole or word by word (`_percent_chunked_email`); (6) found by this round's own
  tests, not by the review: an e-mail of six or seven characters (`ab@c.de`) base64 / base32 / hex encoded and
  FOLLOWED by a word was kept (its window decodes to the e-mail and then the word's bytes, a printable prefix under
  eight bytes): `_printable_prefix` also reads a 6-7 byte prefix holding an `@` with a byte before it and three
  after it (0 of 8,000 such probes kept now; the three OQ tables below are unchanged by it). `polka dot dress at target`
  stays DROPPED, and for the composed-pair bar, not one list rule: it is the ordered pair reversed -> spelled of a
  mailbox (CP-2 / CP-4; that pair would keep every value it makes); `test_query_privacy_r18` pins it with the reason.
  The suite (C-5, item 9): the reviewers' generators ported as `tests/conformance/rvgen/` (rv-priya18 `gen`,
  `build_bnd`, `build_p1p2`, `build_ord`, `oracle`; rv-priya19 `g1`-`g7`, `oracle19`; non-ASCII literals as escapes,
  drivers as functions, the random call order unchanged): each reproduces the review's own data file case for case
  (sha256 pinned in `test_priya_manifest.py`: p1 452,000, bnd 70,346, p2v2 42,476, ord 22,600, m1 291,200, b19
  11,532, cp19 43,680, oq19 24,400); the corpus registry `corpora.py`; `data/manifest.json` (drift guard of every
  corpus); the FROZEN lost-protection bits `data/baseline/<corpus>.<release>.bits.gz` written once by
  `devtools/conformance/calibrate.py` from `git archive` exports of 644b4901 and 65f8b92a (LP runs without git
  history now); `devtools/conformance/mutants.py` (57 mutants) and `gate.sh`; the builder's own generator now
  draws every C-1 pair (Y of D16-D18 = the at-word; the boundary set gains 1,452 at-word x dot-word cases, alone and
  CJK-glued: 15,752); `test_query_privacy_r20.py` pins every finding.
  **Before (5bf0d2a5) / after, every corpus** (kept = Priya keeps the row: raw text and stored key both kept).
  rv-priya19's corpora: M-1 m1 (291,200) **4,133 -> 0** (e-mail spelled / bracketed with `point` 1,758 + 973, `_` /
  `-` spellings 1,076 of which 169 glued to U+3002 + CJK, reversed / chunked 25; addresses glued to fullwidth or
  ideographic punctuation 142; vanity 159); boundary b19 (11,532) 4 -> 0; composed cp19 (43,680) **307 (0.703%) ->
  1** (0.002%), worst ordered pair 21.4% (e-mail spelled -> rot13) -> 1.4% (that one case: vanity spelled_hyphen -> zw_bidi, 1 of 70); cpx (4,000, the
  review's file) 125 -> 0, worst 7.5% -> 0%; the suite's draw of the same ten pairs (`rv19_cpx19`, 4,000) 0 kept;
  lost protection on the review's `lp_in` (4,449): 5 -> 0 (the upper-case `AROBASE ... Point` e-mails 644b4901
  dropped only by an accidental base32 reading). rv-priya18's corpora: P1 p1 (452,000) 0 -> 0; boundary bnd (70,346)
  0 -> 0; composed p2v2 (42,476) 5 -> 0; the attack corpus 676 + 40,672: 0 -> 0. The builder's suite (final code):
  matrix CI draw (N = 5, 7,800 cases) 0, heavy draw (N = 200, 312,000) 0, boundary set 15,752: 0, composed 4,000 and
  42,000: 0. **CP-4 (v1.1 C-3):** every ordered pair of every composed corpus <= 5% (worst 1.4%, the one cp19 case above; every other pair 0%);
  overall composed 4,000 and 42,000: 0; p2v2 0 of 42,476; cp19 1 of 43,680 (0.002%); cpx 0 of 4,000; cpx19 0 of 4,000. **LP:** 0 of BASE kept on every personal-data corpus of the registry (frozen bits; BASE =
  dropped by 644b4901 or 65f8b92a: 1,182,610 of 1,338,134 cases). **Service path** (fake Search Console and Bing
  rows -> Priya -> POST response, run views, audit export, every file under the data dir, logs, ledger events): CI
  1,000 list rows + 1,000 ordinary rows, heavy 12,000 builder rows and 12,000 reviewer rows: **0 at every output**.
  **OQ, ordinary queries** - each corpus labelled by BOTH independent oracles (the suite's `oracle/priya_list.py`,
  updated for C-1 / C-4 from the spec's text, and the review's `rvgen/rv19_oracle.py`); they agree on every drop of
  all three corpora (0 disagreements) and 0 mandated queries are kept. The frozen corpus `oq` (24,400): dropped
  2,762, list-mandated 2,683, **not mandated 79, disclosed** (P-r9): version numbers 55, flights 11, part
  numbers 13 (the rules and causes of round 19, unchanged); ZIP + year 714 of 800 (round 19's table said 716: two
  `mattress <zip> <year>` queries whose only extra digit was the `tres` inside `mattress` are kept now, C-4; the
  review's measurement of 2 not-mandated ZIP + year drops is gone with them). rv-priya18's `ord` (22,600): dropped
  1,955, mandated 1,751, **not mandated 204**: UPC / ISBN codes 200 - 199 with an INVALID
  check digit (the generic 7-digit run: the GTIN exemption needs a valid check digit, which `test_query_privacy_r20`
  pins - the review's mutant removing it changed exactly these 199 verdicts) and 1 whose twelve digits read as hex
  hold an `@` between two characters (the encoded-e-mail rule) - and tire sizes 4 (3 by the round-12 homoglyph
  view, 1 by the generic digit run).
  rv-priya19's `oq19` (24,400): dropped 2,643, mandated 2,383, **not mandated
  260**: tire sizes written `lt` / `p` + width / ratio with a slash and a load index 216 (the generic
  digit-run rules), flights 25 (24 by the round-15 de-separated phone shape, 1 by the homoglyph view), version
  numbers 18 (the homoglyph view), product codes 1 (a UPC whose digits read as hex hold an `@` shape: the
  encoded-e-mail rule). Against 5bf0d2a5 only drops
  went away (oq 2 ZIP + year, oq19 22 part numbers `... phone case`, ord 4 prices: each a number word inside an
  ordinary word). The tables (columns: class, n, dropped, mandated, not mandated - round 20's measurement, kept for the
  record; the asserted tables are round 21's - then dropped at 5bf0d2a5, 644b4901, 65f8b92a):

<!-- round20-priya-oq-table:begin -->
| class | n | dropped | mandated | not mandated | 5bf0d2a5 dropped | 644b4901 dropped | 65f8b92a dropped |
|---|---|---|---|---|---|---|---|
| plain_text | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| multilingual | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| question | 2000 | 0 | 0 | 0 | 0 | 0 | 0 |
| local | 1500 | 0 | 0 | 0 | 0 | 0 | 0 |
| price | 1200 | 0 | 0 | 0 | 0 | 0 | 0 |
| size | 1200 | 33 | 33 | 0 | 33 | 33 | 0 |
| tire_size | 900 | 394 | 394 | 0 | 394 | 675 | 495 |
| model_number | 1500 | 0 | 0 | 0 | 0 | 0 | 261 |
| date | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| time | 600 | 0 | 0 | 0 | 0 | 0 | 0 |
| year | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip_year | 800 | 714 | 714 | 0 | 716 | 714 | 714 |
| version | 1000 | 248 | 193 | 55 | 248 | 257 | 278 |
| flight | 900 | 127 | 116 | 11 | 127 | 514 | 518 |
| product_code | 900 | 2 | 2 | 0 | 2 | 714 | 714 |
| part_number | 1500 | 13 | 0 | 13 | 13 | 1500 | 1500 |
| business_address | 500 | 500 | 500 | 0 | 500 | 500 | 500 |
| quantity | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| date_time | 900 | 731 | 731 | 0 | 731 | 731 | 0 |
<!-- round20-priya-oq-table:end -->

<!-- round20-priya-oq-table-rv18_ord:begin -->
| class | n | dropped | mandated | not mandated | 5bf0d2a5 dropped | 644b4901 dropped | 65f8b92a dropped |
|---|---|---|---|---|---|---|---|
| plain_text | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| multilingual | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| question | 2000 | 0 | 0 | 0 | 0 | 0 | 0 |
| local | 1500 | 0 | 0 | 0 | 0 | 0 | 0 |
| price | 1200 | 1 | 1 | 0 | 5 | 1 | 1 |
| size | 1200 | 0 | 0 | 0 | 0 | 0 | 0 |
| tire_size | 900 | 309 | 305 | 4 | 309 | 527 | 337 |
| model_number | 1500 | 0 | 0 | 0 | 0 | 82 | 140 |
| date | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| time | 600 | 0 | 0 | 0 | 0 | 0 | 0 |
| date_time | 900 | 524 | 524 | 0 | 524 | 571 | 47 |
| year | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip_year | 800 | 536 | 536 | 0 | 536 | 536 | 536 |
| version | 1000 | 112 | 112 | 0 | 112 | 111 | 182 |
| flight | 600 | 0 | 0 | 0 | 0 | 0 | 0 |
| product_code | 900 | 289 | 89 | 200 | 289 | 311 | 311 |
| business_address | 500 | 173 | 173 | 0 | 173 | 173 | 173 |
| quantity | 1000 | 11 | 11 | 0 | 11 | 11 | 11 |
<!-- round20-priya-oq-table-rv18_ord:end -->

<!-- round20-priya-oq-table-rv19_oq19:begin -->
| class | n | dropped | mandated | not mandated | 5bf0d2a5 dropped | 644b4901 dropped | 65f8b92a dropped |
|---|---|---|---|---|---|---|---|
| plain_text | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| multilingual | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| question | 2000 | 0 | 0 | 0 | 0 | 0 | 0 |
| local | 1500 | 0 | 0 | 0 | 0 | 0 | 0 |
| price | 1200 | 1 | 1 | 0 | 1 | 1 | 1 |
| size | 1200 | 0 | 0 | 0 | 0 | 0 | 0 |
| tire_size | 900 | 397 | 181 | 216 | 397 | 648 | 467 |
| model_number | 1500 | 0 | 0 | 0 | 0 | 0 | 390 |
| date | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| time | 600 | 0 | 0 | 0 | 0 | 0 | 0 |
| year | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip_year | 800 | 718 | 718 | 0 | 718 | 718 | 718 |
| version | 1000 | 18 | 0 | 18 | 18 | 21 | 151 |
| flight | 900 | 270 | 245 | 25 | 270 | 317 | 319 |
| product_code | 900 | 2 | 1 | 1 | 2 | 628 | 628 |
| part_number | 1500 | 0 | 0 | 0 | 22 | 1500 | 1500 |
| business_address | 500 | 500 | 500 | 0 | 500 | 500 | 500 |
| quantity | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| date_time | 900 | 737 | 737 | 0 | 737 | 735 | 0 |
<!-- round20-priya-oq-table-rv19_oq19:end -->

  **Cost (PF, process CPU time; the build machine is shared):** PF-1, the median per-query cost of the whole Priya
  path on OQ (every 4th query, best of 3, two runs): now 321.1 / 320.6 us, at 644b4901 544.7 / 556.4 us (-41.1% / -42.4%), at 65f8b92a 515.9 / 516.0 us (-37.8% / -37.9%). PF-2, the 30 adversarial families at 64-512
  characters (three new: other-script letters the fold widens, number words inside words, C-1 words with CJK glue):
  worst at 512 characters 30.33 ms (`to 12` filler runs), then 17.25 ms (`a at b dot` runs) and 16.77 ms (C-1 words with CJK glue); the largest ratio of any doubling 2.35x (`mixed_all`, 64 -> 128 characters).
  **Mutants (G-6):** 57 mutants of `devtools/conformance/mutants.py` - the eight spec pins, the review's two
  behavioural survivors (the reversed-window search of encoded e-mails, the GTIN check digit), the review's other
  mutants where the code still holds their text, and 30 over round 20's code: **54 killed, 3 shown equivalent** (all eight pins and both review survivors killed; three of round 20's own were killed only after this round added the cases they missed: an NFKC multi-letter gap with a separator, a rot13 / word-reversed spelled e-mail ended by U+2026 before CJK, a short URL-encoded e-mail reversed). Equivalent: `p_key_never` (Priya skipping the stored key: no case of the 1,338,134 personal-data cases nor of 4,000 whitespace probes has its raw text kept and its key dropped - the key only collapses white space and casefolds, which the detection form already tolerates); `w_no_raw` (a window's undecoded form: the whole query's undecoded form is already read by the detectors, and a window is whole tokens of it separated from the rest by ordinary words of 2+ letters, so every window match is a whole-text match; 0 verdicts change on the review's 63,274-case e-mail sample); `n28` (U+3000 in `_WORD_END`: no U+3000 reaches `_number_words_read` - `clean_text` makes every space character a space and `script_view` makes U+3000 a space).
  **Residuals (spec section 8 and the review's outside-spec list):** P-r1 .. P-r11 as round 19; vanity numbers
  whose seven keypad characters hold a number word (the review's 31 at M-1 are dropped now; cp19 keeps
  1: `1one...`); `lt` / `p` tire sizes with a slash (216 in oq19, disclosed; 4 tire sizes in ord); UPC / ISBN with an
  invalid check digit (199 in ord, disclosed) and a 12-digit code whose hex reading holds an `@` shape (1 in ord, 1
  in oq19, disclosed); a gap holding typed white space next to a number word's
  leftover letters (`cuatro m siete` is two words, C-4); the review's `m1s` smoke file (an earlier generator, not
  reproduced) holds 2 UK postcode shapes outside the address rule (`cf15a`, an inward `0rd`: both oracles label
  them not mandated).
- **Round 21 - review rv-priya20 closed: the full C-2 glue set, glued spelled vanity** (rules `2026-11-08.21`). The
  review of a394a985 passed CP (4 of 43,680, 0.009%; worst ordered pair 1.25%), LP 0, the OQ tables, OI, PF and every
  frozen corpus at 0 kept, and rejected Z on two shapes plus four suite items. Every number below was measured on the
  final code by the suite (`tests/conformance/`) or by the same generators. **What changed**
  (`search_performance.py`): (1) C-2, the FULL glue set: round 20's suite drew only three of the 24 glue characters
  (U+3002, U+3001, U+FF1A), and the code split only those whose NFKC form is a bracket or `: ; ! ? " |`; NFKC makes most of the others ASCII characters that a value or a word can absorb (U+FF04
  `$` and U+FF06 `&` glued to a street word, U+FF0C / U+FF0E / U+3002 before the house number, U+FF0B `+` /
  U+FF05 `%` / U+FF1D `=` after a spelled e-mail's top-level domain, U+FF0D `-` / U+FF05 `%` / U+FF1D `=` inside an
  encoding's alphabet). Now every one of the 24 (`_C2_GLUE`: U+FF01-U+FF0F, U+FF1A-U+FF20, U+3001, U+3002) is ALSO
  read as a word separator: the text with each made a space is judged by every detector once more (`_classify_core`
  with `_detect_only`: the stream, vanity, spelled, e-mail and encoded views, and the plain e-mail and address rules
  that stand among the generic ones; the generic rules and their exemptions are not run on it), so a glue character
  is read both ways (a fullwidth `@` or `+` of a value stays one); the suite's oracle reads them both ways too
  (`priya_list.C2_GLUE`). (2) C-4 with D11 / D12 / D14: a spelled code digit glued to the seven keypad characters
  (`one eight zero zero` + `flowers` written `... zeroflowers`, `1 8 0 zero...`, `180zero...`): the vanity view
  reads a word that is units (digits and number words, a number word among them) followed by EXACTLY seven keypad
  characters as those digits and the seven (`_vanity_word`, `_units_only`); the toll-free code before it is still
  required, so `zeroflowers`, `800 onefootball`, `the 8 0 zerozone` are kept; with a space the case already dropped.
  (3) Found by the review's matrix after (1): a sep-spelled e-mail whose local part itself ends in an at-word
  (`...-at` + `_AT_` + domain) read every at-word as `@` and so showed no mailbox; with two at-words or more each is
  tried in turn as the one `@` (`_despelled(..., only)`, at most four; the oracle `_despell(..., only)` the same).
  (4) Outside the spec (review item 6): a window that decodes to under eight characters holds an e-mail only with its
  domain's `.` and two TLD letters (`_SHORT_MAILBOX`; `a@b.cc` is the shortest mailbox) - the bare `x@y` shape stays
  for longer decodes and for a window the span limits cut (the value runs on past its end). It was the cause of 8 of the review's 24,400 ordinary queries
  dropped: six `tent <size>` queries (`tent <n>mm`, `tent <a> x <b> x <c> cm`, `tent <a>x<b>`: a rot13 base64 reading
  of `tent` + the size decodes to five characters holding `@`) and two UPCs (the reversed text read as hex). All 8 are
  kept now and nothing personal is lost (LP 0 below; the six-character mailbox base64 / base32 / hex encoded, alone
  and before a word, stays dropped: `test_shortest_encoded_mailbox_is_still_dropped`); the same rule also removed
  rv-priya19's `oq19` one valid UPC whose digits read as hex held an `@` (kept now); rv-priya18's `ord` one (an invalid check digit) stays dropped by the generic digit run, as its other 199. (5) Found by this round's own tests, not by the review: D21 with chunks of one or two characters (an
  e-mail base32 / base64 / hex encoded and written `n f x g k 4 ...`) is longer than the twelve words a window
  joins, so it was read only where a twelve-word window happened to decode to a bare `x@y` (rv-priya18's family
  `chunk1_b32_nopad_lower` dropped so; a394a985 kept 12 of 2,400 (all lower-case base32 written one character a word; 300 e-mails x base32 lower / upper, base64, hex x one / two characters a word, in Devanagari, CJK, Arabic or English context) of this round's probes); every run of four or
  more such words set apart only by white space is now also read as one word (`_short_chunks_merged`, one more
  window pass: linear), and the oracle joins chunks over any run (it joined at most twelve): 0 of the 2,400 now, and 0 of `test_query_privacy_r21`'s 180. **The suite** (C-5): rv-priya20's generators ported as `rvgen/rv20_*` (`p20`, `p20b`, `g20m`,
  `g20bnd`, `g20cp`, `g20oq`, `g20vg`; non-ASCII literals as escapes, the command-line drivers removed, the
  environment no longer sets the matrix size; the random call order unchanged): each reproduces the review's corpus
  (m20 260,000, b20 37,663, cp20 43,680, oq20 24,400, the glued-vanity probe 1,680 - its counts and every case it
  listed as kept equal at the same index; sha256 pinned in `test_priya_manifest.py`) and joins the registry, the LP
  bits (calibrated from 644b4901 / 65f8b92a) and the CI / heavy lanes. The builder's own generator gains the full
  glue set (`priya_gen_c2.cases`: 13 class forms - card, NANP, E.164, SSN, vanity, address, and the e-mail plain,
  spelled, sep-spelled, base64, base32, hex and percent - x all 24 glue characters x a Latin, CJK, Arabic and
  Devanagari context word glued before / after / on both sides, plus the glue as the value's own separator: 8,640
  cases; a394a985 kept 59 of them) and glued vanity (`glued_vanity`: 672 cases, a394a985 kept 449); the boundary
  set is unchanged (frozen). `test_query_privacy_r21.py` pins every finding.
  **Before (a394a985) / after** (kept = Priya keeps the row): rv-priya20's matrix m20 (260,000) **633 -> 0**
  (vanity with a glued spelled code digit 423; addresses glued by U+FF04 U+FF05 U+FF06 U+FF07 U+FF0A U+FF0B U+FF0D U+FF1D or
  with U+FF0C / U+FF0E / U+3002 before the house number 193; e-mails D18 10 - 9 glued by U+FF05 / U+FF0B / U+FF1D,
  1 the local part ending in an at-word, item (3) - and D19 / D21 7); boundary b20 (37,663) **70 -> 0** (addresses 69, e-mail 1); glued-vanity probe (1,680) **556 -> 0**;
  composed cp20 (43,680) **4 -> 1** (0.002%; the three e-mail cases were glue; the one left is a vanity number whose
  seven keypad characters hold a number word, `...oone9`, the residual class of round 20's `rv19_cp19` case; worst
  ordered pair 1.25% -> 1.25%, that case, 1 of 80). The builder's suite: c2glue 59 -> 0, vanity_glued 449 -> 0,
  matrix CI draw (N = 5, 7,800) 0, heavy draw (N = 200, 312,000) 0, boundary set 15,752: 0, composed 4,000 and 42,000: 0, the attack corpus 676 + 40,672: 0, and every rv-priya18 / rv-priya19 corpus 0 (cp19 1, as round 20). **LP:** 0 of BASE kept on every personal-data corpus of the registry (BASE = dropped by 644b4901
  or 65f8b92a: 1,478,740 of 1,690,469 cases, 20 corpora). **OQ:** each ordinary corpus labelled by both independent oracles (the suite's,
  updated for the glue set and the one-`@` reading, and rv-priya19's): 0 disagreements on any drop, 0 mandated
  queries kept. The frozen `oq` (24,400): dropped 2,762, mandated 2,683, not mandated 79 (unchanged from round 20); rv-priya18's `ord` (22,600): 1,955 / 1,751 / 204 (unchanged); rv-priya19's `oq19` (24,400): 2,642 / 2,383 / 259 (one UPC fewer, item (4)); rv-priya20's `oq20` (24,400): dropped 1,851, mandated 1,812, **not mandated 39, disclosed**: flights 31 (a flight number, a day/month and a time read together as a NANP phone: the round-15 de-separated phone shape, as oq19's 24) and version numbers 8 (the round-12 homoglyph view), each as at a394a985. Against a394a985 only drops went away (oq19 1, oq20 8: item (4)); the glue reading and the one-`@` reading added no drop to any ordinary corpus. The tables (class, n, dropped, mandated, not mandated - asserted cell for cell - then
  dropped at a394a985, 644b4901, 65f8b92a; round 20's tables above are kept under historical markers):

<!-- priya-oq-table:begin -->
| class | n | dropped | mandated | not mandated | a394a985 dropped | 644b4901 dropped | 65f8b92a dropped |
|---|---|---|---|---|---|---|---|
| plain_text | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| multilingual | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| question | 2000 | 0 | 0 | 0 | 0 | 0 | 0 |
| local | 1500 | 0 | 0 | 0 | 0 | 0 | 0 |
| price | 1200 | 0 | 0 | 0 | 0 | 0 | 0 |
| size | 1200 | 33 | 33 | 0 | 33 | 33 | 0 |
| tire_size | 900 | 394 | 394 | 0 | 394 | 675 | 495 |
| model_number | 1500 | 0 | 0 | 0 | 0 | 0 | 261 |
| date | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| time | 600 | 0 | 0 | 0 | 0 | 0 | 0 |
| year | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip_year | 800 | 714 | 714 | 0 | 714 | 714 | 714 |
| version | 1000 | 248 | 193 | 55 | 248 | 257 | 278 |
| flight | 900 | 127 | 116 | 11 | 127 | 514 | 518 |
| product_code | 900 | 2 | 2 | 0 | 2 | 714 | 714 |
| part_number | 1500 | 13 | 0 | 13 | 13 | 1500 | 1500 |
| business_address | 500 | 500 | 500 | 0 | 500 | 500 | 500 |
| quantity | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| date_time | 900 | 731 | 731 | 0 | 731 | 731 | 0 |
<!-- priya-oq-table:end -->

<!-- priya-oq-table-rv18_ord:begin -->
| class | n | dropped | mandated | not mandated | a394a985 dropped | 644b4901 dropped | 65f8b92a dropped |
|---|---|---|---|---|---|---|---|
| plain_text | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| multilingual | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| question | 2000 | 0 | 0 | 0 | 0 | 0 | 0 |
| local | 1500 | 0 | 0 | 0 | 0 | 0 | 0 |
| price | 1200 | 1 | 1 | 0 | 1 | 1 | 1 |
| size | 1200 | 0 | 0 | 0 | 0 | 0 | 0 |
| tire_size | 900 | 309 | 305 | 4 | 309 | 527 | 337 |
| model_number | 1500 | 0 | 0 | 0 | 0 | 82 | 140 |
| date | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| time | 600 | 0 | 0 | 0 | 0 | 0 | 0 |
| date_time | 900 | 524 | 524 | 0 | 524 | 571 | 47 |
| year | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip_year | 800 | 536 | 536 | 0 | 536 | 536 | 536 |
| version | 1000 | 112 | 112 | 0 | 112 | 111 | 182 |
| flight | 600 | 0 | 0 | 0 | 0 | 0 | 0 |
| product_code | 900 | 289 | 89 | 200 | 289 | 311 | 311 |
| business_address | 500 | 173 | 173 | 0 | 173 | 173 | 173 |
| quantity | 1000 | 11 | 11 | 0 | 11 | 11 | 11 |
<!-- priya-oq-table-rv18_ord:end -->

<!-- priya-oq-table-rv19_oq19:begin -->
| class | n | dropped | mandated | not mandated | a394a985 dropped | 644b4901 dropped | 65f8b92a dropped |
|---|---|---|---|---|---|---|---|
| plain_text | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| multilingual | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| question | 2000 | 0 | 0 | 0 | 0 | 0 | 0 |
| local | 1500 | 0 | 0 | 0 | 0 | 0 | 0 |
| price | 1200 | 1 | 1 | 0 | 1 | 1 | 1 |
| size | 1200 | 0 | 0 | 0 | 0 | 0 | 0 |
| tire_size | 900 | 397 | 181 | 216 | 397 | 648 | 467 |
| model_number | 1500 | 0 | 0 | 0 | 0 | 0 | 390 |
| date | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| time | 600 | 0 | 0 | 0 | 0 | 0 | 0 |
| year | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip_year | 800 | 718 | 718 | 0 | 718 | 718 | 718 |
| version | 1000 | 18 | 0 | 18 | 18 | 21 | 151 |
| flight | 900 | 270 | 245 | 25 | 270 | 317 | 319 |
| product_code | 900 | 1 | 1 | 0 | 2 | 628 | 628 |
| part_number | 1500 | 0 | 0 | 0 | 0 | 1500 | 1500 |
| business_address | 500 | 500 | 500 | 0 | 500 | 500 | 500 |
| quantity | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| date_time | 900 | 737 | 737 | 0 | 737 | 735 | 0 |
<!-- priya-oq-table-rv19_oq19:end -->

<!-- priya-oq-table-rv20_oq20:begin -->
| class | n | dropped | mandated | not mandated | a394a985 dropped | 644b4901 dropped | 65f8b92a dropped |
|---|---|---|---|---|---|---|---|
| plain_text | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| multilingual | 2500 | 0 | 0 | 0 | 0 | 0 | 0 |
| question | 2000 | 0 | 0 | 0 | 0 | 0 | 0 |
| local | 1500 | 0 | 0 | 0 | 0 | 0 | 0 |
| price | 1200 | 0 | 0 | 0 | 0 | 0 | 0 |
| size | 1200 | 0 | 0 | 0 | 6 | 1 | 0 |
| tire_size | 900 | 418 | 418 | 0 | 418 | 663 | 457 |
| model_number | 1500 | 0 | 0 | 0 | 0 | 0 | 0 |
| date | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| time | 600 | 3 | 3 | 0 | 3 | 0 | 0 |
| year | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| zip_year | 800 | 648 | 648 | 0 | 648 | 648 | 648 |
| version | 1000 | 12 | 4 | 8 | 12 | 8 | 8 |
| flight | 900 | 263 | 232 | 31 | 263 | 296 | 297 |
| product_code | 900 | 0 | 0 | 0 | 2 | 601 | 601 |
| part_number | 1500 | 7 | 7 | 0 | 7 | 1500 | 1500 |
| business_address | 500 | 500 | 500 | 0 | 500 | 500 | 500 |
| quantity | 1000 | 0 | 0 | 0 | 0 | 0 | 0 |
| date_time | 900 | 0 | 0 | 0 | 0 | 259 | 259 |
<!-- priya-oq-table-rv20_oq20:end -->

  **Cost (PF, process CPU time; shared machine):** PF-1, the median per-query cost of the whole Priya path on OQ (every 4th query, best of 3, two runs): now 298.8 / 298.0 us, at 644b4901 530.7 / 491.0 us (-43.7% / -39.3%), at 65f8b92a 487.9 / 502.7 us (-38.8% / -40.7%). PF-2, the 30 adversarial families at 64-512 characters: worst at 512 characters 26.18 ms (`to 12` filler runs), then 12.22 ms (triple base64 in chunks of 4); the largest ratio of any doubling 2.26x (`spelled_run`, 64 -> 128). A first form of item (5) that let a window grow past twelve short words cost 2.61x per doubling on `a@b.` runs and was replaced by the merged-run view. Heavy lane: PF-1 and PF-2 pass.
  **Mutants (G-6):** 67 mutants of `devtools/conformance/mutants.py` (round 20's 57, one of them re-anchored to the new window code, plus 10: the review's two behavioural survivors pinned - brackets as address glue, `r01`, and the script view's lower-casing, `r08`, both KILLED by `test_query_privacy_r21` - and eight over round 21's code): **62 killed, 5 shown equivalent, 0 pinned survivors**. Round 21's glue view made three of round 20's mutants survive at first (`n05` the TLD's trailing full stop, `n08` the full stop glued to the next word, both now read by the glue view for U+3002 / U+FF0E) and one of its own (`n37` the one-`@` reading): each killed after this round added the ASCII cases they missed (re-run of those mutants: 3 of 3 killed). Equivalent: `p_key_never`, `w_no_raw`, `n28` (as round 20); `n06` (the TLD after a dot-word: the one-`@` reading leaves that at-word as letters, so the mailbox is found either way - 0 verdicts change on the boundary sets 15,752 + 11,532 + 37,663 and c2glue 8,640); `n38` (a window never counted as cut: the merged short-chunk view reads every value the cut-window rule kept - 0 verdicts change on those sets and rv-priya18's 70,346).
  **Owner item (CP-3, the reading taken):** rv-priya20's r13 probe drops 18 queries whose number word stands five
  characters as written from the nearest digit group (the suite's own r13 probe: 14 of its 85 list-mandated drops,
  `<two years> the one` and `<date> the one`: digits, a space, `the`, a space, `one`). The reading taken: a gap is
  counted on the text with its separators (white space and punctuation, categories P, S, Z, C) removed - round 19's
  joined view, `cinco, a, W, cero` -> `5aW0` - so ` the ` is a gap of three letters and `<years> the one` spells an
  SSN-shaped stream (both oracles name `spelled_stream_ssn`). If the owner reads CP-3's three characters as written
  (separators counted), these 14 / 18 become KEEP and the joined view must count separators; nothing else changes.
  **Residuals:** as round 20, plus `rv20_cp20`'s one vanity case above; outside the spec, none of the review's 8
  encoded-e-mail drops remains.
- a street address: a house number (at the start, or after a space not preceded by `top` / `best`), an optional
  direction, 1-3 street-name tokens (none a stop or quantity word such as `near`, `in`, `best`, `hour`, `bedroom`,
  `lane`, `way`), then a suffix. `old`, `new`, `good` may start a street name (`17 old mill road`, `40 good hope
  rd`); as the whole name (`12 new street`) the suffix must end the query or be followed only by a unit, a state and
  a ZIP, so `3 new dr pepper flavors`, `2 old court cases`, `10 good place to eat` are kept (round 5) and `12 new
  street london` is missed. After a **strong** suffix (abbreviations that are not English words — `st`, `ave`,
  `blvd`, `rd`, `dr`, `ln`, `ct`, `pl`, `ter`, `cir`, `hwy`, `pkwy`, … — and `street`, `avenue`, `boulevard`) anything
  may follow (`123 main st 2nd floor`, `123 main st springfield il 62701 hours`); after an **ordinary-word** suffix
  (`road`, `lane`, `drive`, `court`, `way`, `terrace`, `place`, `loop`, `plaza`) only up to three place words, a state and a
  ZIP (and, after a ZIP, anything) to the end (`12 kings road chelsea`, `42 wallaby way sydney`), and since round 19
  any amount of other text after it (an address inside surrounding text); a unit (`apt` /
  `suite` / `unit` / `#` N) only after such a street;
- a state name or code with a 5-digit ZIP not followed by a unit word (`btu`, `miles`, `jobs`, `sq`, `zip`, ...); the
  codes `in`, `or`, `me`, `ok` are left out because they are English words; since round 19 a state CODE needs a
  space, `.` or `,` before the ZIP, so a product code such as `sku dc56249` is kept;
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
no rule can tell such a word from an ordinary one). **Over-drops by design (round 8, privacy over recall):** every
URL-like query (above); a Luhn-valid run of year-like digit groups; a word whose letters and digits alternate once
its separators are removed. The round-8 reviewer's 61 attack strings and the round's smuggles (percent-encoded,
split, card numbers) are DROP tests in `tests/test_query_privacy_r8.py`. **Known over-drops:** a non-address shaped like `<number> <word> st|ave|dr|ct|pl…`, or `<number> <word>
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

**Hand-off record (round 11, contract `priya.handoff_record/1`, consumer side ADR 0049 / PR #53).** Every completed
run's `keyword_ledger_recorded` record — and the run views — carry `handoff_record` (`priya.handoff_record/1`),
`handoff_sha256` (SHA-256 of the hand-off document exactly as the run returned it, canonical form: JSON, keys
sorted, separators `,` `:`, `ensure_ascii` false, UTF-8; `priya.handoff_sha`) and `handoff_items`
(`len(items)`), next to the run id, tenant and status, so Theo and Naomi can verify a hand-off they are given.
Only the hash and the count are kept; the document (it carries query text) is still never stored.

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
