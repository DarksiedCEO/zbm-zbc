# ADR 0046 — SEO (Dept 2): the multi-engine answer adapter and Callum, the Citation Source agent

Status: built Nov 8 2026 on `claude/seo-callum-engines-20261108` (from `codex/gate5-release-20261107`). **Not in
force.** Fixtures and synthetic data only: no provider has been called, no key exists, nothing has been spent. Every
new engine is OFF by default. Extends ADR 0017 (Search & Answer Intelligence); ADR 0017's rules apply unless this ADR
says otherwise.

## Context

ADR 0017 named Callum (citation sources) and built one answer engine, Perplexity (W4). The owner's rules for this
build: single-task agents with a human name, one job and a registry entry; vendor independence (every provider
behind one adapter, switching = configuration, official APIs only, no scraping of consumer AI apps, no paid SEO data
vendors); fixtures only, live wiring OFF by default and fail-closed; honest output (inconclusive is not a finding,
every number carries provenance, directional data carries confidence, the client is never blamed for our failures).
Callum's spec (locked): for each tracked prompt × engine answer, trace the exact cited URLs, classify each as a
Strength URL (the client's own page), an Opportunity URL (a third-party page cited where the client could be) or
Mentioned-Not-Cited (the brand named with no link to the client) — the last its own metric, feeding Roman (content
structure) and Adaeze (content gap) as a priority list; near-real-time refresh; robust brand / domain matching
(subdomains, redirects and canonicals of cited URLs, look-alike domains never counted as the client); every row with
engine, model, prompt id, timestamp and evidence; INCONCLUSIVE when the engine call failed, never "not cited".

## Decisions

### A. The multi-engine answer adapter (plumbing, not an agent)

A1. **One interface** (`src/answer_adapter.py`, `AnswerEngine`): `ask(prompt, guard=, meter=)` returns a
    `ports.ProviderAnswer` — answer text, the URLs the engine attributed to the answer (structured fields only, in
    the engine's order), model, model version, usage, `answer_sha256`, and (new, stamped by the adapter) `provider`
    and `asked_at` — or raises `EngineError` with one of the closed codes; `describe()` for `/status`. The probe
    framework (the audit's Callum) and the citation runs call only this.
A2. **Perplexity refactored onto it, behaviour-preserving.** The network layer (`_GuardedDeadlineBackend`, its
    stream, the guard wrapper, the retry signal), the ask loop, `EngineError` and `ApiKey` moved from `perplexity.py`
    into `answer_adapter.py` unchanged and are re-exported, so every existing name, test and war-room driver works
    as before (`perplexity.EngineError is answer_adapter.EngineError`). `PerplexityEngine` now supplies only what is
    Perplexity's (base URL, allow-listed host, path, Bearer header, body, parser). The existing Perplexity tests pass
    unchanged. The adapter adds one control for every engine, Perplexity included: the request URL must be on the
    engine's `ALLOWED_HOSTS` (https, port 443, no credentials, no fragment) before it is sent, and the network
    backend refuses to connect to any other host or port.
A3. **Three new adapters**, each from its official documentation (URL, read date and version recorded in the
    module's docstring and `DOCS`, shown on `/status`):

    | Engine | Module | Call | Cited = |
    |---|---|---|---|
    | `openai` | `openai_engine.py` | `POST https://api.openai.com/v1/responses`, tool `web_search`, `include: ["web_search_call.action.sources"]`, `store: false` (docs: developers.openai.com/api/docs/guides/tools-web-search.md, read Nov 8 2026) | a `url_citation` annotation whose URL the call's own search returned (`action.sources`); without sources, the annotations, and the answer says `annotations_only` |
    | `anthropic` | `anthropic_engine.py` | `POST https://api.anthropic.com/v1/messages`, `anthropic-version: 2026-10-21` (the page's examples), tool `web_search_20250305` with `max_uses` (docs: platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool, read Nov 8 2026) | a `web_search_result_location` citation whose URL its own search returned |
    | `google` | `google_engine.py` | `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`, header `x-goog-api-key`, tool `google_search` (docs: ai.google.dev/gemini-api/docs/generate-content/google-search and ai.google.dev/api/generate-content, read Nov 8 2026) | a grounding chunk's `web.uri` (a redirect: see C3) |

    The documentation was read through a web-fetch tool that returns a model's reading of each page, not the raw
    page. Fields the pages did not show (OpenAI's and Anthropic's response-object reference pages were not re-read;
    Gemini's FinishReason values were cut) are strict: any other shape is `ENGINE_PROTOCOL_ERROR`, never a guessed
    answer. Refusals (Gemini SAFETY-class finish reasons and block reasons, Anthropic `refusal`, OpenAI `refusal`
    content) are REFUSAL answers, excluded from every rate. Anthropic search-tool errors inside a 200 are
    `ENGINE_RATE_LIMITED` (`too_many_requests`) or `ENGINE_UNAVAILABLE` `SEARCH_TOOL_ERROR` (every search failed);
    `pause_turn` is not resumed (`RESPONSE_PAUSE_TURN`, never counted). Closed model allowlists: `gpt-6-astra`;
    `claude-sonnet-5-5` (default), `claude-opus-5-5`, `claude-haiku-5-5`; `gemini-3.8-flash`. A reported dated
    snapshot of the asked model is accepted and recorded as the model version.
A4. **Default OFF, fail closed.** `SEO_<ENGINE>_ENABLED=1` and a key (`SEO_<ENGINE>_API_KEY_FILE` under the *_FILE
    rules, or `SEO_<ENGINE>_API_KEY` for Railway) are both required; one without the other refuses start, both key
    forms refuse start, a key equal to any token or another engine's key refuses start, and no error echoes a value.
    The three key settings left `config.NOT_BUILT` (they used to refuse start as "not built").
A5. **Budgets** (svc_engines.py, unchanged mechanism: record-first reservations, settlement, unknown cost charged at
    the reserve, the 4x cost guard). Per engine: the four daily budgets (global and per tenant, calls and dollars),
    plus two new **per-run caps** — `SEO_<ENGINE>_RUN_MAX_CALLS` and `SEO_<ENGINE>_RUN_MAX_SPEND_USD`, the calls and
    dollars ONE audit or ONE citation run may reserve on that engine (`RUN_CALL_CAP`, `RUN_SPEND_CAP`) — and the
    reserve per call. **Every one defaults to 0 = OFF**; while any is 0 nothing is asked (`BUDGET_OFF`). Perplexity
    keeps its four budgets and gains the per-run caps OPT-IN (review round 1 L4): `SEO_PERPLEXITY_RUN_MAX_CALLS` and
    `SEO_PERPLEXITY_RUN_MAX_SPEND_USD` unset = no per-run cap (exactly the behaviour before, so the Perplexity suite
    is unchanged); both set = they bind as for the other engines; one alone refuses start. None of the three new
    engines reports a dollar cost, so every request is charged at its reserve (defaults $0.10 / $0.10 / $0.05, above
    the documented search charges: Anthropic $10 per 1,000 searches plus tokens, Google $14 per 1,000 queries after a
    free tier; OpenAI's per-call price was not on the page read). **These are reserve ESTIMATES, not spend
    tracking** (review round 1 L3): the dollar budgets and caps bound estimates, a request that really costs more is
    still booked at the reserve, and the 4x cost guard (which acts only on a reported cost) cannot detect an overrun
    on these engines. The real ceiling must be a hard spend limit set at each provider; nothing here claims to track
    real spend. Per-call timeout: the hard deadline per question
    (`SEO_<ENGINE>_TIMEOUT_SECONDS`, default 60).
A6. **Egress / SSRF.** One fixed base URL per engine; `ALLOWED_HOSTS` checked before every request and by the
    network backend before every connect (host and port 443); every resolved address must be public; no redirect
    followed; no proxy from the environment; the request carries only the prompt and one fixed instruction (no
    tenant, brand, domain or user identifier). Kill switches `provider:<engine>` and `capability:ai_probe` before
    every request and every socket operation.

### B. Callum — the Citation Source agent

B1. **One job**: classify where and how AI engines cite the client. Registry entry (`svc_manager.REGISTRY`, shown on
    `GET /seo/v1/department`): assignment, inputs (prompt set, registered domain, adapter answers, the fetcher for
    resolution) and outputs (the audit's per-prompt summary — `agents/probes.py`, unchanged — the citation-source
    rows, and the `callum.mnc_priority/2` list; /1 until review round 1). The registry now lists all seven agents; shared plumbing (the
    adapter, the engines, the primitives, the ports) is not an agent. Callum keeps one lifecycle state: a restricted
    Callum is refused 403 `AGENT_RESTRICTED` for citation runs as for audits.
B2. **Classes** (`agents/callum.py`, rules `callum-rules/2026-11-08.2`), per cited URL: `STRENGTH_URL`,
    `CANONICAL_TO_CLIENT` (a third-party page whose canonical names the client: B4), `OPPORTUNITY_URL` (competitor
    pages flagged from the prompt set), `LOOKALIKE_URL` (the cited host, or where it lands, folds to the client's
    domain on the shared lookalike table but is not it: never the client, wherever it redirects, a high-severity
    WATCH finding), `INCONCLUSIVE` (a URL on a redirector host not resolved, an unreadable URL); per answer: `MENTIONED_NOT_CITED` (a brand term in the text and
    no STRENGTH_URL citation in that answer), its own metric with its own rate and interval. An answer that names the
    brand but has an INCONCLUSIVE citation is INCONCLUSIVE (`MENTION_WITH_UNRESOLVED_CITATIONS`), never MNC. A failed
    engine call is an INCONCLUSIVE row with the engine's code. Citations come only from the adapter's structured
    fields (`probes.extract_citations`: http/https only; backslash, control and whitespace URLs refused); a URL in the
    answer text is never a citation.
B3. **Domain matching reuses `agents/domain_match.py`** unchanged: exact host, the www. twin or a subdomain, IDNA
    A-labels, never by substring; the IDNA2003 round-trip rule; public-suffix tenant domains match nothing; lookalikes
    by the shared fold.
B4. **Exact-URL resolution** (`callum.Resolver`, OFF unless `SEO_CALLUM_RESOLVE_URLS=1`, at most
    `SEO_CALLUM_RESOLVE_MAX` distinct URLs per run, each fetched once): through the existing SSRF-safe fetcher
    (public addresses only, ports 80/443, redirects by hand with the cap, robots.txt honoured for `ZBM-SEO-Audit`,
    the run's kill switches before every hop). The landing URL decides, with two exceptions (review round 1):
    **M1** a citation on a LOOKALIKE host — or, since review round 2, one whose redirect chain passes through a
    lookalike host at ANY hop (the resolver records every hop's host, `chain.hop_hosts`; flag `LOOKALIKE_HOP`), e.g.
    `t.co` or the Gemini redirector → lookalike → client — is `LOOKALIKE_URL` wherever it lands (when resolution
    fails after a lookalike hop the row keeps its class and adds `LOOKALIKE_HOP`, round 3)
    (`CITED_HOST_LOOKALIKE`); one that
    redirects ONTO the client is flagged `REDIRECTED_ONTO_CLIENT` and raises `CALLUM_LOOKALIKE_REDIRECTS_ONTO_CLIENT`
    (high, WATCH) — a lookalike forwarding to the client is suspicious, never a credit. **M2** a third-party page whose
    single valid canonical is the client's page is `CANONICAL_TO_CLIENT` `via: canonical` (flag `THIRD_PARTY_PAGE`):
    the third party controls that tag, so it is excluded from `cited_strength`, counted as
    `canonical_to_client_urls`, reported by `CALLUM_CANONICAL_TO_CLIENT` (info), and listed in the MNC contract's
    `cited_instead` with `canonical_to_client: true` (an answer citing only such a copy, naming the brand, is
    Mentioned-Not-Cited). A client URL that redirects off-site is classified where it lands
    (`REDIRECTED_OFF_CLIENT`). **M3** the WHOLE host `vertexaisearch.cloud.google.com` is a redirector, whatever the
    path: Gemini's grounding-URI format is UNVERIFIED until a first live call (the docs show truncated examples), so
    an unresolved URL there is INCONCLUSIVE, never OPPORTUNITY; one that resolves onto the redirector itself is
    INCONCLUSIVE (`REDIRECTOR_DID_NOT_REDIRECT`). A failed resolution falls back to the cited host
    (`RESOLUTION_FAILED`), except for a redirector, which stays INCONCLUSIVE. Bodies are parsed in memory and
    dropped.
B5. **Rows and evidence.** Every row: engine, model, model version, provider, `prompt_id`
    (`<prompt_set_id>:v<version>:<index>`), sample, `observed_at` (service clock), `provider_asked_at` (adapter),
    `refresh_tier` `near_real_time` and `refresh_due` (observed + 1 hour), `run_id`, position, the URL chain (cited,
    final, canonical, redirects, resolution state), flags, the citation basis, and evidence: the URL and an excerpt of
    at most 160 characters around the first brand mention (else the opening), marked untrusted. **This changes ADR
    0017 W4-3 for citation runs only** ("answer text scanned in memory and dropped"): a bounded excerpt is kept
    because the spec requires drill-down evidence; an excerpt that looks like personal data (the prompt guard's
    rules) is withheld (`personal_data:<kind>`). URLs are kept as scheme, host and path — query and fragment dropped
    (`query_dropped`) — and a path that looks like personal data is withheld. At most 2,000 rows a run
    (`rows_truncated`). The ledger receives ids, counts and hashes only.
B5a. **Brand matching** (review round 1 L1, `agents/brand_mention.py`, shared with the audit summary in
    `agents/probes.py`): whole words; case-insensitive unless the tenant's rule says `case_sensitive`; possessives
    match deliberately (`Acme's`, `Acme’s`), plurals and other inflections deliberately do not (`Acmes`, `apples`: a
    tenant lists another form as its own term); per-tenant rules on the prompt set (`brand_term_rules`: `term`,
    `case_sensitive`, `context_terms` — one must appear in the answer — `negative_terms` — a match is discarded
    when a whole-word occurrence of one, found on the full text (never on a slice: round 2), lies within 40
    characters of the brand span — and `common_word`). Whitespace (NBSP, tabs, runs of spaces) is normalised to one
    space in the answer and in every term before matching (negatives looked up by `bisect` in their sorted positions:
    linear overall, never quadratic). Round 6: a markdown emphasis underscore is a word boundary (`_Acme_` names
    Acme) unless the character beyond it is a letter or digit (`foo_acme_bar` does not, as at a4af277); this is the
    only intended difference from a4af277 for non-dictionary brands (a randomized parity check: every a4af277 match
    still matches; the extra matches are all underscore emphasis). **Confirmed by default (review rounds 3-4, the a4af277 behaviour for
    ordinary brands):** every term that is not a common word — multi-word terms and single words not on the bundled
    list (`Acme`, `HubSpot`, `eBay`, `iPhone`, `Ünïcorn`) — matches case-insensitively, whole word, and every match
    is confirmed (`try acme`, `the iphone 15`, `buy it on ebay`); a match inside a URL or host written in the answer
    text (`acme.io/pricing`, `github.com/acme`) is a mention as at a4af277, never a citation of the client. Only a
    COMMON-WORD term — on the bundled list (`agents/common_words.txt`, self-authored for this repository, ~325 words
    incl. ford, zoom, stripe, oracle, visa, subway, ring, monday; no third-party licence) or declared
    `common_word: true`, with no disambiguation rule — needs confirmation: a match capitalised as configured that
    does not start a sentence (round 5: a capital is grammar at the start of the text or a line, a markdown list
    item `-`/`*`/`+`/`•`/`1.`/`1)`/`a)`, a heading `#`, a blockquote `>`, after `.`/`!`/`?`/`:`/`;`, or right after an
    opening quote — `He said "Apple is good."` is a quoted sentence; round 6: also a markdown table cell `| Target |`
    and a task box `- [ ] Zoom`; detected by two linear scanners over a 32-character look-back, never a
    backtracking regex — the round-5 regex was a ReDoS; line structure is read with line breaks kept;
    so `Zoom in on the map`, `- Target the right audience`, `## Zoom in`, `1) Target buyers`, `Steps: Zoom in` do
    not confirm; a lower-case match never does), the client's domain named in the answer, a
    citation of the client, or another brand term of the set that is not itself a common word (two common words
    never confirm each other: `the notion of apple pie` with Apple and Notion stays unconfirmed). A mention made only by unconfirmed terms is `MENTIONED_NOT_CITED`
    `confidence: LOW_CONFIDENCE`
    (`BRAND_TERM_MAY_BE_COMMON_WORD`): its own `mentioned_not_cited_low_confidence` rate (`headline: false`), the
    `CALLUM_MENTIONED_NOT_CITED_LOW_CONFIDENCE` finding, never in the headline rate or the MNC list. The audit summary
    (review round 2) applies the same rule: a LOW-confidence mention is not counted in `mentioned` / `mention_rate`;
    a prompt not cited whose only mentions are LOW is class `MENTION_UNCONFIRMED` (finding
    `CITATION_MENTION_UNCONFIRMED`, INSUFFICIENT_EVIDENCE), shown in the client report as "Possible mention, not
    confirmed" with no page recommendation, never "Mentioned but not cited"; `mentioned_unconfirmed` is added to the
    prompt's result only when non-zero. When a prompt's ONLY mentions are unconfirmed its mention rate is UNKNOWN
    (`mention_rate: null`, `mention_rate_unknown`; the client report prints "UNKNOWN", never 0%), and Callum's
    Mentioned-Not-Cited rate likewise (`value: null`, `ci95: null`, `unknown: MENTIONS_UNCONFIRMED`). An answer that cites the
    client confirms its own mention, so a partly cited prompt keeps a real mention rate. A not-cited verdict needs
    certainty in the audit as in Callum: when every not-cited mention (either confidence) had a citation on a
    redirector host, the prompt is `INCONCLUSIVE` ("Could not tell whether you were cited"). **Never silent:** a
    prompt set whose term is on the list (or declared common) with no rule carries `warnings`
    (`BRAND_TERM_MAY_BE_COMMON_WORD`) in every create and read response, and the audit and citation-run methodology
    name those terms. The audit methodology's class list names MENTION_UNCONFIRMED and INCONCLUSIVE. A prompt set
    without rules keeps its content and hash exactly as before.
B6. **Metrics with provenance and confidence.** Per engine × prompt, per engine and in total: usable answers,
    refusals, inconclusive calls, URL counts per class, zero-citation answers, and the Mentioned-Not-Cited and
    cited-strength rates as `{value, k, n, ci95 (Wilson), interval, directional}` — `directional` under 3 usable
    answers — each with `provenance` (data class measured, source, run, engine, prompt id, prompt-set hash, rules,
    model versions). Pooled rates say they are directional. No visibility score. Findings:
    `CALLUM_MENTIONED_NOT_CITED` (TEST, or INSUFFICIENT_EVIDENCE when directional), `CALLUM_OPPORTUNITY_URLS`,
    `CALLUM_LOOKALIKE_DOMAIN_CITED` (high), `CALLUM_STRENGTH_URLS` (info), `ENGINE_RUN_STOPPED`; effect class null.
B7. **The Mentioned-Not-Cited output contract `callum.mnc_priority/2`** (/2 since review round 1: `cited_instead[]`
    gains `canonical_to_client`, and LOW_CONFIDENCE mentions are excluded) (`GET .../citation-runs/{cid}/mnc-priority`),
    for Roman and Adaeze (neither consumer is built): `{contract, producer: "callum", consumers: {roman:
    "content_structure", adaeze: "content_gap"}, run_id, domain, generated_at, rules, prompt_set {id, version,
    sha256}, order_rule, items[], excluded, data_class: "measured", effect_class: null}`; each item `{rank,
    prompt_id, prompt_index, prompt (untrusted, null when it looks like personal data), mnc_answers, usable_answers,
    mentioned_not_cited (rate as B6), engines[{engine, mnc_answers, usable_answers, mentioned_not_cited,
    model_versions}], cited_instead[{url, host, answers, competitor, canonical_to_client}] (top 10), competitor_cited_instead, evidence[≤3
    {engine, sample, observed_at, basis, excerpt, withheld}], for_roman {task, question}, for_adaeze {task, gap,
    hosts}}`. Order rule (stated in the payload, not a score): MNC answers descending, competitor cited instead
    first, more engines first, prompt index. INCONCLUSIVE answers and LOW_CONFIDENCE mentions are never listed. A change to these fields is a new
    contract version.
B8. **Citation runs: the near-real-time tier** (`svc_citations.py`, separate from the audit, which keeps Callum's
    summary unchanged): `POST /seo/v1/tenants/{tid}/citation-runs` (dashboard, seo_agent) with a registered domain and
    a tenant prompt set (`engines` may narrow, never widen, the set's engines); `GET` list / one run / the MNC list for
    dashboard, seo_agent, hub (its own tenant only) and compliance_38. Record-first `citation_run_requested` →
    reservations (ledger subject `citation_run:<cid>`, `run_kind` in the payload; audits unchanged) → settlements →
    `citation_run_recorded` with the result's SHA-256; `citation_run_interrupted` for a switch engaged mid-run (the
    result discarded), a crash (`RUN_FAILED` with the safe location, as for audits) or a dead process (the
    `interrupted-audits` job, which now also reports `citation_runs_interrupted`). One running citation run per
    tenant. Kill switches: global, write, the tenant, `capability:ai_probe` and `capability:audit` (no new capability
    switch, so the switch view is unchanged), and `provider:<engine>` (that engine's part is KILLED, as in the audit).
    Tenant isolation: another tenant's run is the same 404 as a missing one. A client tenant's run needs Andre's
    approval, a bound Finance client and Compliance (38)'s current ALLOW (ADR 0024), no override.

B9. **War room fixes (PR #55 findings WR-F011..WR-F014; replays `seo-py-callum/R0001..R0050`).**
    - **WR-F011** every adapter builds the answer with `answer_adapter.join_text`: adjacent text blocks of one
      run are concatenated (one space added only where a sentence end would touch the next block), runs separated by
      anything else (a tool call, a search result, another message item) are joined with a paragraph break, which
      brand matching reads as a line start. Anthropic: runs split at non-text blocks; OpenAI and Perplexity's Agent
      API: one run per message item (a Perplexity answer with two message items now reads `one\n\ntwo`, not
      `onetwo`: its hash changes for such answers); Google: the candidate's parts are one run. A single run is
      unchanged.
    - **WR-F012** `agents/secret_guard.py` (documented key prefixes, credential names, JWTs, 16+ hex digits, and
      high-entropy tokens; standard library) redacts every excerpt BEFORE its span is chosen (`[redacted]`,
      `secret_redacted: true`) and the engine's chunk-title hints. Rows, the run response and the log carry only
      redacted text. To unify later with Priya's search-query secret classifier (PR #50), not on this base.
    - **WR-F013** a cited URL path segment that looks like a secret is stored as `{secret}` (`withheld:
      SECRET_REDACTED`, row flag `SECRET_REDACTED`); query and fragment were already dropped. A URL whose path,
      query, fragment or user part carries a secret is NEVER fetched: with resolution on it is INCONCLUSIVE
      (`CITED_URL_NOT_FETCHED`), with resolution off classified by its host as any URL. Cost: a path segment of 16+
      hex digits (a commit SHA, a content hash) is redacted too; the host still classifies. The host itself is kept.
    - **WR-F014** the fetcher records a redirect whose `Location` httpx refuses to parse or encode (a raw-Unicode or
      invalid IDNA host) as `refused_redirect` {status, location} on the FetchResult (never fetched; `redirects`,
      `final_url` and `summary()` unchanged for every other reader). Callum adds every followed hop, the host where a
      failed chain stopped and a refused target to `hop_hosts`, and a client URL whose chain stopped is never a
      credit: through a lookalike → `LOOKALIKE_URL` (`LOOKALIKE_HOP`, `REDIRECT_TARGET_REFUSED`,
      `REDIRECTED_OFF_CLIENT`); stopped off the client or refused by URL rules → INCONCLUSIVE
      (`REDIRECT_NOT_RESOLVED`); a client landing page answering 4xx/5xx → INCONCLUSIVE (`LANDING_PAGE_ERROR`). A
      failure with no redirect seen (timeout, DNS) still falls back to the cited host.
    - **Redaction precision (re-certification review).** The hex rule needs letters AND digits (a tweet's status id
      or a 16-digit item id is not a secret); a credential label (`password`, `token`, `secret`, `api-key`,
      `authorization`, `bearer`) counts only when followed by `:`, `=` or whitespace and a token-shaped value (letters
      and digits), so `/password-reset-guide` and "password protection" stay; allowed URL shapes: commit / tree / blob
      SHAs on github.com, gitlab.com, bitbucket.org, codeberg.org, hashed asset names (`main.8f14e45fceea167a.css`),
      any path on Google Docs / Drive / Sheets / Slides and on known link shorteners; known tracking parameters
      (`gclid`, `fbclid`, `msclkid`, `utm_*`, ...) are never secrets and are dropped before a fetch (the stored URL
      drops the whole query anyway). Under-redaction fixed: text and URL parts are also checked percent-decoded
      (`Bearer%20<token>`), and the 40-character AWS secret-key format is a rule of its own (both cases, a digit / `/`
      / `+`, entropy ≥ 4 bits, no run of 6+ lower-case letters; checked across path segments, since such a key holds
      `/`). Tests: a table of 43 ordinary URLs that are kept and fetchable, and the documented fake example values
      (AWS's `AKIAIOSFODNN7EXAMPLE` and its example secret key, built at run time) dropped from paths, queries,
      fragments and text. A long digit run in a path may still be withheld as possible personal data (the prompt
      guard's pre-existing rule): the stored path is shortened, the URL is still fetched.
    - The war room's fake web now encodes chains without hex digits (the reviewer's path fix for PR #55), so its
      redirect cases are fetched and exercise the resolution logic.

## Founder-pending defaults (added by this ADR)

- (27) every new engine OFF; every budget, including the per-run caps, 0;
- (28) default models `gpt-6-astra`, `claude-sonnet-5-5`, `gemini-3.8-flash` (the documentation's example or
  supported models; which model best represents what consumers see is a founder decision);
- (29) reserves $0.10 / $0.10 / $0.05 per request;
- (30) a client tenant's citation run needs Andre and Compliance but no invoice (monitoring under the managed tier;
  per-run billing is a founder and Finance decision);
- (31) cited-URL resolution OFF (it fetches third-party pages the engines cited, robots-honouring and SSRF-safe);
- (32) bounded answer excerpts (≤ 160 characters, PII-screened) are kept for citation runs (B5);
- (33) `anthropic-version: 2026-10-21` and `web_search_20250305` (basic search, no dynamic filtering or code
  execution) for Anthropic;
- (34) SUPERSEDED by review round 1 M2: a third-party page whose single canonical names the client's page is its
  own class, `CANONICAL_TO_CLIENT`, never a STRENGTH_URL (the third party controls the tag);
- (35) a brand term is confirmed unless it is on the bundled common-word list (or declared common) and the answer
  does not confirm it (review rounds 3-4; round 1's "every single word" default and round 3's lower-case downgrade
  for non-dictionary brands were regressions);
- (36) a Google answer without `modelVersion` is refused, not counted under the asked model (review round 1 L2).

## What is NOT built (deliberately)

- No live call to any provider; no key; no spend. The first real call of each new engine needs a one-call smoke like
  Perplexity's (ADR 0017 W4-6 step 5a); there is no `smoke_one_call.py` mode for the new engines yet.
- Roman and Adaeze do not read `callum.mnc_priority/2`; Adaeze does not exist (registry: `not_built_agents`).
- No scheduler for citation runs (the near-real-time tier is on demand; `refresh_due` says when one is due); no drift
  comparison between citation runs; no dashboard pages.
- No consumer-app scraping, no paid SEO data vendor, no prompt-volume source (Naomi stays NOT_CONNECTED).
- No continuation of an Anthropic `pause_turn`; no Anthropic dynamic filtering; no OpenAI `open_page` handling beyond
  counting what the response reports; no Gemini `groundingSupports` segment mapping (every grounding chunk counts).
- No scoped or short-lived credentials for the three providers (not researched); the Perplexity compensating
  controls (ADR 0017 W4-5) apply.
- No war-room scenarios for the new engines or for citation runs (the war room is not changed here).
- The maintenance pins and the seo image are not re-released (see "Release").

## Limitations (honest)

- None of the three new adapters has met its real API; the parsers follow the documented shapes as read through a
  web-fetch tool, and the fixtures are synthetic. A relied-on field with another shape is ENGINE_PROTOCOL_ERROR and
  two in a row stop the engine.
- Removing `utm_source=openai` from OpenAI citation URLs is an assumption (the reference was not read); removing an
  absent pair changes nothing.
- Gemini grounding URIs are treated as redirects (the page's examples show `vertexaisearch.cloud.google.com` URIs but
  do not say so); the URI format is unverified until a first live call, so every path on that host counts. With
  resolution OFF every Gemini citation is INCONCLUSIVE and no Gemini answer can be MNC.
- Google model attribution (review round 1 L2) is exact: `modelVersion` must equal the allow-listed model or an
  alias listed in `google_engine.MODEL_ALIASES` (none yet: what the live API reports is unknown); anything else is
  `MODEL_NOT_ALLOWED`, and a missing `modelVersion` is `MODEL_UNVERIFIED` — fail closed: the answer is an
  ENGINE_PROTOCOL_ERROR (INCONCLUSIVE in Callum, two in a row stop the engine), never attributed to a model it may
  not be. If the live API omits `modelVersion`, Google stays unusable until that is decided. SIDE EFFECT (accepted,
  review round 2): a prompt Gemini blocks (`promptFeedback.blockReason`, no candidate) usually carries no answer and
  may carry no `modelVersion`; it is then `MODEL_UNVERIFIED`, an ENGINE_PROTOCOL_ERROR instead of a REFUSAL, and two
  in a row stop the engine for the run. Chosen because the alternative — accepting a response that names no model —
  is exactly the unattributed answer L2 forbids, and a refusal is excluded from every rate anyway, so the cost is a
  stopped engine (visible as `ENGINE_RUN_STOPPED`), never a wrong number. Revisit after the first live call.
- The Anthropic model allowlist has not been checked against the Messages API models list (the Models overview or
  `GET /v1/models`); the web search tool page names models the tool works with, not the list (review round 1 L5).
- Brand matching is lexical (B5a): it does not understand the answer; context and negative terms reduce, not remove,
  false positives.
- Spend on OpenAI, Anthropic and Google is a reserve estimate (A5, L3); only a provider-side hard limit bounds the
  real bill.
- Resolution sees one network location at one time; a site may answer the crawler differently; robots.txt may block
  it (then the cited host decides).
- Query strings are dropped: two pages that differ only by query string count as one.
- One account, one time, N samples: observations, not search truth. Rates are uncalibrated against traffic or revenue.

## Release

seo-py source changed, so the Gate 5 maintenance pins and the seo image must be re-released after Gate 5; until then
the hygiene-static cold-pin check is red for that expected reason. The pins are not edited here (`deploy/gate5/**`,
`deploy/docker/maintenance/**`, `.railway/**` and `.github/workflows/**` are untouched).
