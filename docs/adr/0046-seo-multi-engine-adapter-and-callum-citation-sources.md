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
    keeps exactly its four budgets (no per-run cap was added to it: behaviour-preserving). None of the three reports
    a dollar cost, so every request is charged at its reserve (defaults $0.10 / $0.10 / $0.05, above the documented
    search charges: Anthropic $10 per 1,000 searches plus tokens, Google $14 per 1,000 queries after a free tier;
    OpenAI's per-call price was not on the page read). Per-call timeout: the hard deadline per question
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
    rows, and the `callum.mnc_priority/1` list). The registry now lists all seven agents; shared plumbing (the
    adapter, the engines, the primitives, the ports) is not an agent. Callum keeps one lifecycle state: a restricted
    Callum is refused 403 `AGENT_RESTRICTED` for citation runs as for audits.
B2. **Classes** (`agents/callum.py`, rules `callum-rules/2026-11-08.1`), per cited URL: `STRENGTH_URL`,
    `OPPORTUNITY_URL` (competitor pages flagged from the prompt set), `LOOKALIKE_URL` (folds to the client's domain on
    the shared lookalike table but is not it: never the client, a high-severity WATCH finding), `INCONCLUSIVE` (a
    redirector URL not resolved, an unreadable URL); per answer: `MENTIONED_NOT_CITED` (a brand term in the text and
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
    the run's kill switches before every hop). The landing URL decides; a third-party page whose single valid
    canonical is the client's page is a STRENGTH_URL `via: canonical` (`THIRD_PARTY_COPY_CANONICAL_TO_CLIENT`); a
    client URL that redirects off-site is classified where it lands (`REDIRECTED_OFF_CLIENT`); a lookalike anywhere
    in the chain is flagged. A failed resolution falls back to the cited host (`RESOLUTION_FAILED`), except for a
    redirector, which stays INCONCLUSIVE. Bodies are parsed in memory and dropped.
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
B6. **Metrics with provenance and confidence.** Per engine × prompt, per engine and in total: usable answers,
    refusals, inconclusive calls, URL counts per class, zero-citation answers, and the Mentioned-Not-Cited and
    cited-strength rates as `{value, k, n, ci95 (Wilson), interval, directional}` — `directional` under 3 usable
    answers — each with `provenance` (data class measured, source, run, engine, prompt id, prompt-set hash, rules,
    model versions). Pooled rates say they are directional. No visibility score. Findings:
    `CALLUM_MENTIONED_NOT_CITED` (TEST, or INSUFFICIENT_EVIDENCE when directional), `CALLUM_OPPORTUNITY_URLS`,
    `CALLUM_LOOKALIKE_DOMAIN_CITED` (high), `CALLUM_STRENGTH_URLS` (info), `ENGINE_RUN_STOPPED`; effect class null.
B7. **The Mentioned-Not-Cited output contract `callum.mnc_priority/1`** (`GET .../citation-runs/{cid}/mnc-priority`),
    for Roman and Adaeze (neither consumer is built): `{contract, producer: "callum", consumers: {roman:
    "content_structure", adaeze: "content_gap"}, run_id, domain, generated_at, rules, prompt_set {id, version,
    sha256}, order_rule, items[], excluded, data_class: "measured", effect_class: null}`; each item `{rank,
    prompt_id, prompt_index, prompt (untrusted, null when it looks like personal data), mnc_answers, usable_answers,
    mentioned_not_cited (rate as B6), engines[{engine, mnc_answers, usable_answers, mentioned_not_cited,
    model_versions}], cited_instead[{url, host, answers, competitor}] (top 10), competitor_cited_instead, evidence[≤3
    {engine, sample, observed_at, basis, excerpt, withheld}], for_roman {task, question}, for_adaeze {task, gap,
    hosts}}`. Order rule (stated in the payload, not a score): MNC answers descending, competitor cited instead
    first, more engines first, prompt index. INCONCLUSIVE answers are never listed. A change to these fields is a new
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
- (34) a third-party page whose single canonical names the client's page counts as a STRENGTH_URL (`via:
  canonical`, flagged). A canonical is the page's own claim, so a third party can declare one; the flag lets the
  dashboard show it apart, and counting it as OPPORTUNITY instead is one rule change.

## What is NOT built (deliberately)

- No live call to any provider; no key; no spend. The first real call of each new engine needs a one-call smoke like
  Perplexity's (ADR 0017 W4-6 step 5a); there is no `smoke_one_call.py` mode for the new engines yet.
- Roman and Adaeze do not read `callum.mnc_priority/1`; Adaeze does not exist (registry: `not_built_agents`).
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
  do not say so). With resolution OFF every Gemini citation is INCONCLUSIVE and no Gemini answer can be MNC.
- Resolution sees one network location at one time; a site may answer the crawler differently; robots.txt may block
  it (then the cited host decides).
- Query strings are dropped: two pages that differ only by query string count as one.
- One account, one time, N samples: observations, not search truth. Rates are uncalibrated against traffic or revenue.

## Release

seo-py source changed, so the Gate 5 maintenance pins and the seo image must be re-released after Gate 5; until then
the hygiene-static cold-pin check is red for that expected reason. The pins are not edited here (`deploy/gate5/**`,
`deploy/docker/maintenance/**`, `.railway/**` and `.github/workflows/**` are untouched).
