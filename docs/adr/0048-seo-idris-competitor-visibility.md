# ADR 0048 — SEO (Dept 2): Idris, the Competitor Visibility agent

Status: built Nov 8 2026 on `claude/seo-idris-20261108`, on top of ADR 0046 (the multi-engine answer adapter and
Callum, PR #51 at dda14c34). **Not in force.** Fixtures and synthetic data only: no provider has been called, no key
exists, nothing has been spent. Idris asks no engine itself, so it adds no budget, no key and no switch. Extends ADR
0017 and ADR 0046; their rules apply unless this ADR says otherwise. (Numbered 0048: 0047 is taken by the parallel
Priya branch.)

## Context

ADR 0017 named Idris (competitor visibility) and did not build it. The owner's locked spec: **core** — Semrush-scale AI
share-of-voice benchmarking for up to 20 tracked competitors per client (a configured list), per engine and per
prompt; **differentiator** — continuous auto-detection of previously untracked brands appearing in the same AI answers,
flagged `EMERGING_COMPETITOR` and folded into the benchmark automatically, with an owner / tenant ability to dismiss;
output framed as opportunity ("gap being closed"), not anxiety tables; refresh tier near-real-time. Owner rules (as
for ADR 0046): one job, a human name and a registry entry; vendor independence through the existing adapter; official
APIs only; fixtures only, budgets OFF, fail closed; honest output (inconclusive is not a finding, every number with
provenance and confidence, never 0% for unknown); no paid data.

## Decisions

### D1. One job, and what is not Idris's

Idris measures the client's AI share of voice against competitors and detects emerging competitors (`agents/idris.py`,
rules `idris-rules/2026-11-08.3`, contract `idris.share_of_voice/1`). Registry entry in `svc_manager.REGISTRY`
(assignment, inputs, outputs; `GET /seo/v1/department`); Idris leaves `NOT_BUILT_AGENTS`; it has its own lifecycle
state (a restricted Idris is refused 403 `AGENT_RESTRICTED`, and citation runs then record no observations — Callum
is unaffected). NOT Idris's: asking engines (the adapter and Callum's collection), classifying the client's citations
(Callum: `callum.classify_citation` is reused, unchanged), content or gap advice (Roman, Adaeze), prompt volume
(Naomi, NOT_CONNECTED). The service plumbing (`svc_competitors.py`) is not an agent.

### D2. The same answers as Callum, never a second query

`callum.run` gains one optional parameter, `observer` (default None: behaviour unchanged, every Callum test passes
unchanged). When the tenant has a competitor set and Idris is not restricted, `svc_citations._execute_citation_run`
passes `idris.Observer`, which is called once after Callum's classification with the same collected samples (text in
memory only), the resolver (its cache: resolved redirects are reused, no new fetch) and the run's times. It never
raises: a failure is recorded as `failed` (with the safe location) and Callum's run is recorded regardless. The
service commits `competitor_observations_recorded` right after `citation_run_recorded` (ledger: ids, counts, hashes). Review round 1:
building the observer, calling it (`callum.run` catches anything it raises) and recording its observations are each
guarded, so a failing Idris can never change Callum's response, outcome or HTTP status (tested byte-for-byte against a
run without Idris); a run whose observations were not recorded is simply not reusable.

**Reuse rules** (`svc_competitors._reusable`). `POST .../competitor-visibility-runs` reuses — with **zero** engine
calls — the most recent citation run of the same tenant that: completed (never an interrupted one); has Idris's
observations; used the same domain, the same prompt set id, version and sha256, and the competitor-set version now in
force (same sha256: competitor names are matched while the text exists, so a changed set needs new answers); covered
every requested engine (narrowing is allowed and the observations are narrowed; widening is not reuse); and whose answers were OBSERVED (the observations' `observed_at`, not the run's completion; review round 1)
within `max_age_minutes` (default and maximum 60 = Callum's near-real-time refresh interval; 0 = always fresh).
Otherwise it requests a fresh citation run with the request's own `request_id` and **every one of Callum's rules**
(domain, prompt set, Andre + Finance + Compliance for a client tenant, one running run per tenant, per-run caps and
daily budgets, kill switches): one collection serves both agents, and Callum's rows are recorded as usual. A citation
run Callum starts on its own also records Idris's observations, so it is reusable. A crash between the citation run
and the visibility record is recovered by retrying (the retry finds the completed run and reuses it).

### D3. Brand matching

`agents/brand_mention.py`, unchanged, for the client AND every tracked competitor: whole words, possessives yes,
plurals no, per-competitor `term_rules` (`case_sensitive`, `context_terms`, `negative_terms`, `common_word`), and the
common-word rule — a competitor called `Target`, `Ring` or `Zoom` with no disambiguation is LOW confidence (counted as
`low_confidence_mentions`, never in presence or share; a brand whose only mentions are LOW is UNKNOWN
`MENTIONS_UNCONFIRMED`), and the competitor set warns (`BRAND_TERM_MAY_BE_COMMON_WORD`) on create and read. A
competitor's own domain named in the answer, or a citation of it, confirms its mention (any of its up to 5 domains).
**Negative context**: a lexical cue (`NEGATIVE_CUES`: avoid, scam, complaints, lawsuit, worst, ...) within 40
characters of a brand's name sets `negative_cue` and is counted (`negative_cue_mentions`). It is never subtracted:
share of voice is presence, not endorsement, and Idris measures no sentiment.

### D4. Citations

Each structured citation (`probes.extract_citations`, never a URL in the text) is owned by: `client` / `client_lookalike`
/ `unresolved` / `canonical_to_client` exactly as Callum classifies it (a lookalike host is never the client wherever
it lands; an unresolved redirector is nobody's); else a tracked competitor whose domain it is on (exact, www. twin or
subdomain, IDNA-aware; a lookalike of a competitor's domain is `competitor_lookalike`, never the competitor); else
`publisher` (`KNOWN_PUBLISHERS`, a self-authored table of ~35 hosts that publish or host content about brands: Reddit,
Wikipedia, YouTube, G2, Capterra, Forbes, ...; tracked domains are checked first, so a tenant whose real competitor is
on that table lists it); else `other`.

### D5. Share of voice: two measures, never one score

Per brand × engine × prompt (and pooled per engine and in total, marked directional): **presence** = answers naming
the brand / usable answers; **mention share** = answers naming it / brand-answer pairs over every benchmark brand
(the client and the tracked competitors; emerging ones are reported apart, review round 1); **citation share** = citations on its domains / citations on any tracked brand's
domain (unresolved ones excluded and counted; `incomplete: true` when any were). Each is `{value, k, n, ci95
(Wilson 95%), interval, unknown}` with provenance (data class measured, source `callum_citation_run_answers`, citation
run, engine, prompt id, prompt-set and competitor-set hashes, rules, model versions, observed_at). A share's interval is
approximate (mentions within one answer are not independent): it says so. **UNKNOWN (`value: null`, `ci95: null`,
never 0%)**: fewer than 3 usable answers (`INSUFFICIENT_ANSWERS`); no benchmark brand named (`NO_BRAND_MENTIONED`);
only unconfirmed mentions (`MENTIONS_UNCONFIRMED`); every citation unresolved (`CITATIONS_UNRESOLVED`); no tracked brand
cited (`NO_TRACKED_BRAND_CITED`); an emerging brand's citation share (`NO_VERIFIED_DOMAIN`). A brand simply not named in
3+ usable answers while others are is a measured 0 with its interval. Refusals and failed calls are excluded from every
rate; a failed call is an `INCONCLUSIVE` row with the engine's code; a stopped engine is `ENGINE_RUN_STOPPED`.

**Rows**: every answer × brand named or cited, and every failed call, with engine, model, model version, provider,
prompt id, sample, `observed_at`, `provider_asked_at`, `refresh_tier`, `refresh_due` and an **evidence pointer**
(`citation_run_id`, engine, prompt id, sample, `answer_sha256`) into Callum's citation run, whose row holds the bounded
excerpt. At most 2,000 rows (`rows_truncated`). No whole answer is kept: the observations hold counts and pointers,
and per emerging candidate its extracted name and at most 3 excerpts of at most 160 characters (PII-screened, withheld
when they look like personal data, marked untrusted).

### D6. Emerging competitors: a deterministic extractor, never a model

`idris.candidates`: brand-like runs of 1–4 words (capitalised, mixed case `HubSpot`/`eBay`, or a digit plus capitals;
joined by one space or ` & `; a possessive ends the name; leading / trailing stopwords stripped), in answers to the
client's prompts. Excluded, each counted once per name per answer by reason: `GENERIC` (the bundled common-word list,
stopwords, weekdays, months, common acronyms), `SENTENCE_INITIAL` (a single word that only ever starts a sentence with
no brand signal), `TITLE_RUN` (5+ capitalised words, heading lines, title lines), `ENGINE_SELF` (the answer engines'
names), `CLIENT_LOOKALIKE` (the client's brand terms or the client domain's label after the shared lookalike fold,
spacing and punctuation ignored, or one edit away for 6+ characters: listed apart in `client_lookalikes_seen` with the
`IDRIS_CLIENT_LOOKALIKE_NAMED` finding, a brand-impersonation signal), `CLIENT_VARIANT` (a longer name containing a
client term), `TRACKED`, `PUBLISHER`, `TOPIC_TERM` (every word of the name is in the prompt: "Amazon" for "best amazon
reimbursement agency"), `EXCLUDED_BY_TENANT` (the set's `exclude_terms`). **Brand signals**: `LIST_HEAD` (first words
of a markdown list item), `BOLD`, `MIXED_CASE`, `CORPORATE_SUFFIX` (Inc, LLC, Group, Agency, Labs, Media, ...),
`DOMAIN_HINT` (the name equals the registrable label of an `other` host cited in the same answer: a hint, unverified).

**Rule** (stated in every payload): over the current run and the tenant's earlier observations for the same domain in
the last 30 days, a candidate is `EMERGING_COMPETITOR` when it appeared in ≥ 3 usable answers AND on ≥ 2 engines or in
≥ 2 runs AND carries ≥ 1 brand signal; confidence HIGH with ≥ 6 answers, 2+ engines, 2+ runs and `DOMAIN_HINT`, else
MEDIUM; anything less is `WATCH` (listed with what it is `missing`, never folded in). The top 10 emerging competitors
(by answers) are **listed in the benchmark** (origin `emerging`, `brand_id: emg:<key>`; PRESENCE only from this run's
answers, kept out of every share denominator: review round 1) — beyond, not inside, the 20 tracked. Finding
`IDRIS_EMERGING_COMPETITOR` is data class `inferred` (the identification is lexical), decision WATCH. **Dismissal**:
`POST .../competitor-set/dismissals` by the owner (dashboard, seo_agent) or the tenant itself (hub with its tenant
token, own tenant only — the hub's one write here, like log ingests), for a key seen in the tenant's observations
(else 404 `CANDIDATE_NOT_FOUND`), once (409 `CANDIDATE_ALREADY_DISMISSED`). A dismissed candidate that reappears is
listed in `dismissed_seen` with this run's count — never flagged, never folded, never silently dropped.

### D7. Opportunity framing

`opportunities` lists the prompt × engine scopes (3+ usable answers) where a competitor appears more often than the
client: the leader, the gap in answers and in presence, decision `TEST` when the Wilson intervals do not overlap else
`INSUFFICIENT_EVIDENCE`, and a headline ("Room to grow: Acme appears in 4 of 5 openai answers to this prompt, you in
1."). Against the previous comparable visibility run (same tenant, domain and prompt set version, an earlier citation
run): `GAP_CLOSING`, `GAP_CLOSED`, `GAP_WIDENING`, `UNCHANGED` or `NO_PREVIOUS`, with `within_noise` (the client's
presence intervals overlap); `IDRIS_GAP_CLOSING` is INSUFFICIENT_EVIDENCE when within the noise, never a claim.
`strengths` lists where the client leads. Order rule stated in the payload; no score.

### D8. Configuration, routes, isolation

`POST /seo/v1/tenants/{tid}/competitor-sets` (dashboard, seo_agent; capability switch `prompt_sets`): a new version of
the tenant's one competitor set — up to **20** competitors (a 21st: 422 `COMPETITOR_LIMIT`; the model accepts up to 40
so the refusal names the rule), each a name, up to 5 terms (default the name), term rules and up to 5 domains;
`exclude_terms`; `emerging_detection` (default true: it is deterministic, free and in memory). Refused 422
`COMPETITOR_IS_CLIENT`: a competitor domain that is the client's, a subdomain, a parent or a lookalike of one of the
tenant's domains, or a name / term that is a lookalike of a brand term in the tenant's prompt sets (and a prompt set
created later is caught at run time: `excluded_tracked`, finding `IDRIS_TRACKED_COMPETITOR_EXCLUDED`). Same content as
the current version: 409 `COMPETITOR_SET_EXISTS`. `GET .../competitor-set` (+ hub own tenant, compliance_38).
`POST .../competitor-visibility-runs` (dashboard, seo_agent; kill switches global, write, tenant, `capability:ai_probe`;
a fresh run adds Callum's), `GET` list / one (+ hub own tenant, compliance_38). Another tenant's objects answer the same
404 as missing ones. Idempotent by request id; replay rebuilds every view; the manager's scorecard shows Idris's runs
(reused / fresh / outcomes).

## Review round 1 (independent review of 44098614: CONDITIONAL) — changes

- **HIGH, overlapping terms.** "Jungle Scout" and "Scout" let one span count for two brands (a denominator of 15
  for 10 answers). The competitor set now refuses terms that contain one another, as whole words after the fold, across
  competitors and against the tenant's client brand terms (422 `COMPETITOR_TERMS_OVERLAP`). At run time one span names
  one brand: a brand's matching runs on the answer with every LONGER term of another brand that contains its term
  masked (longest match wins), and a tracked competitor overlapping a client term is excluded
  (`CLIENT_TERM_OVERLAP`). A competitor set is refused while the tenant has no prompt-set brand terms (409
  `CLIENT_BRAND_TERMS_REQUIRED`).
- **MED, dismissal bypass.** A dismissal now covers its key, legal-suffix variants either way (`refundrangersinc` /
  `refundrangers`: inc, llc, ltd, corp, co, group, holdings, labs, gmbh, plc) and, for keys of 4+ characters, any name
  containing it; `dismissed_seen` names the covering key. `POST .../competitor-set/undismissals` (dashboard only:
  the owner) withdraws a dismissal (`emerging_competitor_undismissed`, replayed).
- **MED, non-brands promoted.** New exclusions: `INSTRUCTION_LIKE` (a name holding an instruction word — ignore,
  instructions, prompt, system, override, ... — or an underscore artefact), `PLACE` (the bundled gazetteer
  `agents/idris_places.txt`: ~150 countries and regions, the 50 US states, ~100 major cities; self-authored, place names
  are facts, no licence), `PERSON_NAME` (a 1-3 word name starting with a first name from `agents/idris_first_names.txt`,
  common US first names — provenance corrected in round 2: the US Social Security Administration's popular-names data,
  a US government work, public domain, no licence obligation; a disclosed heuristic that can drop a brand named like a
  person). Publisher and engine names now match as a word or prefix of a name ("Forbes Advisor", "Gemini Advanced",
  "ChatGPT Plus"). `GENERIC` covers names made only of generic business nouns (`GENERIC_NOUNS`: "Revenue Recovery
  Software"), and `CORPORATE_SUFFIX` is a legal form only (Software, Solutions, Media, ... are no longer a signal).
  **Emerging competitors are kept out of every share denominator** (reviewer's recommendation, adopted): they are
  listed in the benchmark with their presence only (`benchmark.*.emerging`, `excluded_from_shares: true`, provenance
  data class `inferred`; `facts.emerging_brands`), never a leader in an opportunity, and never named in a headline;
  `folded_into_benchmark` is renamed `listed_in_benchmark`. This supersedes D5/D6 where they say "folded into the
  benchmark".
- **MED, client lookalikes.** `idris.client_like`: a name whose core (legal suffixes and generic nouns stripped) is a
  client term or domain label, or a lookalike of one (`ZBestMedia Labs`, `Zbestmedia LLC`), is `CLIENT_LOOKALIKE`; one
  containing a client term as whole words, or as a compact substring for terms of 5+ characters (labels of 6+), is
  `CLIENT_VARIANT`. The same rule refuses such a tracked competitor (`COMPETITOR_IS_CLIENT`) and excludes it at run time.
- **LOW.** Reuse age from `observed_at` (above); a visibility record names the real caller (`requested_by`), never
  "andre" because the header was present, and notes `andre_presented`; the README, the registry and this ADR state the
  kept excerpts exactly.

## Review round 2 (independent review of 16d34b41: CONDITIONAL) — changes

- **HIGH, partial overlap.** `idris.overlapping` now refuses any token-sequence overlap: equal, containment, or the end
  of one term equal to the start of another (`Jungle Scout` / `Scout Pro`; `Media Pros` against the client's `Z Best
  Media`), across competitors and against client terms (422 `COMPETITOR_TERMS_OVERLAP`). At run time
  `idris.assign_spans` collects every brand term's matches and selects NON-OVERLAPPING spans, longest first then
  leftmost; each brand is matched on the answer with the spans selected for other brands overwritten, so any overlap
  names one brand (`Jungle Scout Pro` names Jungle Scout only). With no overlap the text is unchanged.
- **Dismissals, word-aligned** (`idris.DismissalIndex`, built once per run — no sort per call): the key; the same
  tokens after the fold (invisible characters removed) with legal suffixes dropped and plurals singular; a name
  containing the dismissed tokens as a contiguous token sequence; or one edit apart for 6+ characters. Never a
  substring inside a word (`acme` does not cover `Pacmed Labs`, `ring` does not cover `Springbot`). A dismissal now
  stores the candidate's display name for token matching.
- **Recall restored, precision kept.** Exclusions match WHOLE names: `GENERIC` only for a single dictionary word or a
  name made only of business-generic words, without a FORM signal (legal suffix, mixed case, digit, host name);
  `INSTRUCTION_LIKE` only for injection PHRASES (`INJECTION_PHRASES`), never ordinary words (Buy, Contact, Run, Mode,
  Output); publisher / engine names only standing alone (NerdWallet, G2, Gemini); `KNOWN_NON_BRANDS` (self-authored:
  Seller Central, Meta AI, Le Chat, DeepSeek, Black Friday, Customer Service, Forbes Advisor, Gemini Advanced, ...);
  `PLACE` only when the whole name is a place, `PERSON_NAME` only for "First Last" with no form signal; a sentence-
  initial verb glued to a name is stripped ("Contact Victoria"); digits and host names stay in names ("Helium 10",
  "eva.guru"). The gazetteer gained Lyon, Leeds, Guadalajara, Kraków and ~40 more cities.
- **Labelled corpus** (`tests/fixtures/idris_candidates_corpus.json`, 291 answers, synthetic and self-authored from
  labelled lists of 60 brands and 60 non-brands including every example of both reviews; IN-SAMPLE, so the numbers are
  optimistic): extractor precision 1.000, recall 0.965; strong-signal-gated precision 1.000, recall 0.705. The test
  prints them and asserts floors of 0.95 / 0.90 (extractor) and 0.95 / 0.60 (gated). The reviewer's own 82-answer set
  is not in the repository; precision on unseen answers is not known.
- **EMERGING needs a STRONG brand signal** (`STRONG_SIGNALS`: its domain cited (`DOMAIN_HINT`) or written as a host
  name, a legal suffix, mixed case, a digit, product / pricing context, or a distinctive multi-word name); a list
  head or bold alone is layout and never enough.
- **Optional classifier** (`SEO_IDRIS_CLASSIFIER_ENGINE`, unset = OFF): one question per visibility run through the
  multi-engine adapter, inside a recorded reservation (`visibility_run:<vid>`) with that engine's budgets (default 0
  = OFF: `BLOCKED`, nothing asked). Injection-safe: at most 30 candidate names go as a JSON array of DATA under a fixed
  instruction; the reply must be exactly `{"not_brand": [ids]}` with ids from that array, or it is `FAILED` and
  filters nothing. It can only REMOVE candidates (`classifier_filtered`), never add, promote or rename one.
- **"Candidate — needs confirmation"** on every emerging and watch item, every emerging presence entry and the
  finding. `POST .../competitor-set/confirmations` (owner: dashboard, seo_agent; tenant: hub, own tenant) confirms a
  candidate: it is promoted INTO the shares (`brand_id: cnf:<key>`, presence and mention share from the run's answers,
  citation share UNKNOWN) and may lead an opportunity headline. A dismissal ends a confirmation; a dismissed candidate
  cannot be confirmed (409). **Spec clarification for the owner:** the spec says emerging competitors are "folded into
  the benchmark automatically"; as built they are listed automatically with their presence only, and enter the shares
  and headlines only once a human confirms them — because an unconfirmed lexical candidate in a share denominator
  would move every brand's number on a guess.

## What is NOT built (deliberately)

- No LLM-based candidate EXTRACTION (the optional classifier only filters; see review round 2).
- No sentiment measure; the negative cue is lexical and reported only.
- No verification of an emerging competitor's domain (domain hints are unverified; citation share stays UNKNOWN);
  no "promote to tracked" action (the tenant adds it to the set, within the 20).
- No scheduler (the near-real-time tier is on demand, `refresh_due` says when), no dashboard pages, no client-report
  section, no war-room scenarios for Idris.
- No paid SEO data, no consumer-app scraping, no prompt volume; no live call, key or spend (Idris adds none).
- The maintenance pins and the seo image are not re-released (see "Release").

## Limitations (honest)

- Lexical: matching and extraction do not understand the answer. The extractor misses lower-case or unusual names and
  can surface a non-brand (a place, a product line) — the brand-signal requirement, WATCH and dismissal bound that.
- Shares are relative to the benchmark brands only; an untracked brand counts only once detected and folded.
- Brand mentions within one answer are not independent: share intervals are approximate (directional).
- `KNOWN_PUBLISHERS` and the generic lists are self-authored and incomplete.
- Narrowing a reused run's engines keeps candidate brand signals that were seen on other engines.
- The same one-account, one-time sampling limits as Callum (ADR 0046); nothing is calibrated against traffic or revenue.

## Release

seo-py source changed, so the Gate 5 maintenance pins and the seo image must be re-released after Gate 5; until then
the hygiene-static cold-pin check is red for that expected reason. The pins are not edited here (`deploy/gate5/**`,
`deploy/docker/maintenance/**`, `.railway/**` and `.github/workflows/**` are untouched).
