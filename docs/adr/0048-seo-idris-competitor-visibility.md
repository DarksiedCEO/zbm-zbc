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
rules `idris-rules/2026-11-08.6`, contract `idris.share_of_voice/1`). Registry entry in `svc_manager.REGISTRY`
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

## Review round 3 (independent review of bf15c0eb on an unseen 126-answer harness: CONDITIONAL) — changes

Owner policy: permissive open source, made our own, rather than paying for data.

- **Self-hosted NER (optional signal).** `agents/ner.py`: spaCy 3.8.7 (MIT) with `en_core_web_sm` 3.8.0 (package
  licence MIT; its meta lists OntoNotes 5 as training data, "commercial (licensed by Explosion)", and WordNet 3.0:
  Explosion holds the commercial OntoNotes licence and releases the trained weights under MIT — see spaCy GitHub
  discussion #13698).
  Pinned in `services/seo-py/requirements-ner.txt` — every distribution `==`-pinned with a sha256 (CPython 3.13
  manylinux x86_64 / py3-none-any wheels), the model wheel by URL and hash; `pip install --require-hashes -r
  requirements-ner.txt` was run on a fresh environment after `requirements.txt`. **Licence gate:**
  `services/delivery-py/devtools/licence_gate.py --site-packages <that env>` -> "63 distributions ...; ok=True" (the
  NER set alone: 44 distributions, ok=True). It is NOT in `requirements.txt` or the image (about 300 MB with numpy;
  the small model is weak on brand names: in spot checks it labelled Recurly PERSON and Getida GPE), so it is OFF
  unless `SEO_IDRIS_NER=1`; `1` without the packages refuses start; the model is loaded from the installed package (no
  network). Its output can only LABEL names the extractor found: ORG / PRODUCT is evidence, PERSON / GPE / LOC / NORP
  / EVENT / DATE ... counter-evidence; it never adds a candidate; a failure adds nothing. Alternative if the size is
  unacceptable for the image: keep NER in an offline batch worker, or train a small in-repo classifier on our own
  labelled fixtures (not built).
- **Domain-backed evidence (strongest).** `DOMAIN_BACKED` when a cited URL's registrable label (or, for a non-generic
  TLD, the whole registrable name: `eva.guru` -> Eva Guru) equals the compact name or its core, in the same answer or
  anywhere in the run; a host written in the text is `DOMAIN_NAME` and keyed by its label (`getida.com` -> Getida). A
  single dictionary word ("Stripe", "Notion") is a candidate ONLY with its domain (or an NER ORG label) — otherwise it
  is `GENERIC`, so names with no domain anywhere are demoted.
- **"Distinctive" is gone.** Evidence (`STRONG_SIGNALS`) is now brand SHAPE only: `DOMAIN_BACKED`, `DOMAIN_NAME`, a
  legal suffix, CamelCase, digits in a token, `INDUSTRY_NAME` (a name ending in a trade noun: "Benjamin Franklin
  Plumbing"), `PRODUCT_CUES` (TWO independent product / pricing cues in the name's own sentence — the 60-character
  any-cue window is gone), `CATEGORY_CONTEXT` (apposition "Recurly, a billing platform" or an introduction "agencies
  such as Kalungi", in the same clause), `LIST_PEER` (enumerated in the same sentence or markdown list as a name with
  evidence), `NER_ORG`.
- **Exclusions extended** (`agents/idris_non_brands.txt`, self-authored, provenance in the header): platform
  features, AI products (GPT-4o, Claude Sonnet, AI Overviews, ...), events (Prosper Show, Prime Big Deal Days,
  Dreamforce, ...), job titles and teams, generic business phrases, acronyms (MRR, B2B, DTC, ...), nationalities,
  honorifics; more review / trade-press publishers (Search Engine Journal / Land, Marketplace Pulse, PCMag, TechRadar,
  TrustRadius, GetApp, UpCity, DesignRush, ...); a bare first name is `PERSON_NAME`; "-based" / "-powered" adjectives
  are `GENERIC`; the gazetteer gained ~250 cities and regions.
- **Recall shapes.** A sentence-initial name that opens an enumeration ("Chargebee, Recurly ...") or has category
  context is kept; "Mr. Rooter" is not split at the honorific period; names ending in a trade noun may run to 7
  words ("One Hour Heating & Air Conditioning"); "First Last + trade noun" is a business, not a person; a capitalised
  "The" mid-sentence stays in the name.
- **Injection proximity.** A name on the same line as, and within 80 characters of, an injection phrase carries
  `INJECTION_PROXIMITY` and is never `EMERGING_COMPETITOR` (WATCH, `missing: no_injection_nearby`).
- **Perf.** `publisher()` is a dictionary lookup per label suffix (no table scan); per-sentence product cues are
  computed once per answer; the list-peer pass no longer copies sets per name; a backtracking regex introduced while
  building `CATEGORY_CONTEXT` was replaced by a bounded scan before commit. 20,000 hosts in one answer: about 5 s on
  the development machine (was 31 s at bf15c0eb per the review), growing linearly.

### Measured quality (honest)

Own fixtures (self-authored; in-sample for the rules they were written with):

| Set | Stage | Before (bf15c0eb) | After |
|---|---|---|---|
| `tests/fixtures/idris_candidates_corpus.json` (291 answers) | extractor P / R | 1.000 / 0.965 | 1.000 / 0.965 |
| same | evidence-gated P / R | 1.000 / 0.705 | 1.000 / 0.548 (evidence is stricter) |
| `tests/fixtures/idris_emerging_train.json` (90 answers, 3 runs, NEW) | emerging P / R, no NER | — | 1.000 / 0.974 |
| same | emerging P / R, NER on | — | 0.973 / 0.923 |

The reviewer's harness (126 answers, 4 tenants; 1 answer has citations, so DOMAIN_BACKED can barely help there):

| Metric | Before (bf15c0eb) | After, NER off | After, NER on |
|---|---|---|---|
| candidate P / R | 0.697 / 0.802 | 0.877 / 0.874 | — |
| realistic 3-run emerging P / R | 0.632 / 0.592 | 0.829 / 0.528 | 0.863 / 0.543 |
| 2-run emerging P / R | 0.971 / 0.642 | 1.000 / 0.604 | — |
| recurring adversarial non-brands made EMERGING (two harness sets of 51) | 46 and 30 | 8 and 2 | — |

**The target (held-out emerging precision ≥ 0.85 at recall ≥ 0.60) is NOT met**: precision reaches 0.86 only with
NER, at recall 0.54. **The held-out set is not clean**: the review named its categories and examples, and I ran the
harness about ten times during this round and read its false-positive / false-negative names, so the "after" numbers
are optimistic. A fresh held-out set is needed for a trustworthy figure.

### The human-confirm gate (stated)

No extracted name ever enters a share, a denominator, an opportunity or a headline until the owner (dashboard,
seo_agent) or the tenant (hub) confirms it (`POST .../competitor-set/confirmations`). Until then every emerging and
watch item is labelled "candidate — needs confirmation" and reported with its presence only. This is the safety net
for the precision above: a wrong candidate costs a reviewer a click, never a wrong number.

## Review round 4 (independent review of 7c501fae on a FRESH 216-answer corpus: REJECTED) — redesign

**Baseline being improved on (the reviewer's fresh corpus, which I do not have):** 6 US tenants x 3 prompts x 4 engines
x 3 runs, about 80% of answers citing 2-6 URLs. At 7c501fae, rules only: emerging precision **0.362** (95% CI
0.298-0.432), recall **0.861**; NER on: precision 0.389. The round-3 harness numbers above were overfit to that harness.

**Statement of scope.** Rules-only Idris is a CANDIDATE GENERATOR for human confirmation, NOT a competitor list. Every
emerging item is "candidate — needs confirmation"; nothing enters a share, a denominator, an opportunity or a
headline until the owner or the tenant confirms it (the gate below, unchanged).

- **Domain-first detection.** The primary candidate is a cited registrable domain (PSL eTLD+1, the vendored list in
  `domain_match.py`) that is not the client or a lookalike of it (domain fold or name fold), not a tracked
  competitor's, and not a platform / publisher / directory / marketplace / social / review / government / education
  site (`idris.non_competitor_domain`: the bundled, self-authored `agents/idris_platforms.txt` — search, social,
  marketplaces, news and trade press, software review sites, local-service, legal and health directories, platform
  vendors such as HubSpot and HVAC equipment makers such as Trane, which a tenant can still track explicitly — plus
  `KNOWN_PUBLISHERS`, any `gov` / `edu` / `mil` / `ac` label, and publisher-like labels such as `news`, `blog`,
  `herald`). The brand name in the answer is attached to its own domain (`keys_match`: equal, equal without legal
  suffixes, or a 5+ character prefix; `name_keys` spells `&` as "and"), across the run, so a brand and its domain are
  ONE candidate. A cited domain becomes `EMERGING_COMPETITOR` only when the answers NAME it as a provider — not mostly
  as a source (`PUBLISHER_CONTEXT`: "according to X", "X reports", "a X survey", "X, a trade publication"). Text-only
  names (no domain anywhere in the look-back) need evidence from TWO independent families (shape / context / NER /
  host name in text) and recurrence; a dictionary word never; `LIST_PEER` is removed entirely.
- **Failure classes removed.** `DATE` (a month or a year in the name, "Since 2021"); `FEATURE` (the bundled,
  self-authored `agents/idris_features.txt`: SOC 2, SEER2, Board Certified, All-on-4, ...); sentence-opening verbs and
  adjectives are stripped ("Hire", "Trusted", "Local", ...); full name spans: `&`, `+`, "and" before Associates / Sons /
  Partners / Company, initials ("J. Walsh Heating") and honorifics join a name; markdown headings are read ("### 2.
  Name").
- **Injection.** The whole answer is scanned with the injection guard's pattern families (`agents/injection_guard.py`,
  a byte-identical copy of Theo's module from `claude/seo-theo-naomi-zara-20261108`, with its vendored
  `confusables.txt` and `confusables.NOTICE`, Unicode License V3; since round 5 the copy is Theo's 49732dd3 version,
  which bounds every repetition (the 7b906d15 copy had a cubic ReDoS in the YAML role pattern) and flags a text over
  `MAX_SCAN_CHARS` unscanned — so such an answer counts for no Idris candidate (fail closed); Theo's structural
  pattern tests run on this copy in `tests/test_idris.py`. MERGE INTO ONE MODULE when both branches land), so line
  breaks, spacing, homoglyphs and distance do not hide an injection. An answer that holds one counts for NONE of its
  candidates (each is marked `injection_answers`); a candidate seen only in such answers is never emerging. The
  per-name proximity loop is gone (it was O(names x injections)).
- **Classifier (optional, default OFF, unchanged guarantees).** It now labels each candidate {brand, platform,
  publisher, person, place, feature, other} from its name, cited domain and the stored PII-screened excerpt (data, not
  instructions); the reply must label every id once from that closed set or it is `FAILED` and filters nothing; only
  `brand` survives; it never adds. **Not measured:** no live engine may be called here; with it on, precision is
  expected to be higher, and nothing more is claimed.
- **NER fail-closed.** `SEO_IDRIS_NER=1` now LOADS the pipeline and runs one inference in `config.load`; any failure
  (a broken install, missing model files) refuses start. The loaded port is reused; nothing loads lazily.

### Measured on my own NEW fixtures (self-authored; honest limits)

`tests/fixtures/idris_r4_train.json` and `idris_r4_test.json`: 6 US tenants (subscription billing SaaS, DTC agency,
Denver HVAC, Houston personal-injury law, Austin dental, Amazon seller services) x 3 prompts x 4 engines x 3 runs = 216
answers each; every answer cites 2-6 URLs (own domains, review sites, directories, publishers, platforms, government);
45% name a non-competitor SOURCE that is also cited; 8% carry an injection promoting a fake brand. Train and test use
DISJOINT brand, person, place, source and template pools. Rules were tuned on train only.

| Split | Mode | Emerging precision (95% Wilson) | Recall |
|---|---|---|---|
| train | rules only | 1.000 (0.930-1.000) | 0.981 |
| test | rules only | 1.000 (0.929-1.000) | 0.980 |
| test | NER on | 0.980 (0.897-0.997) | 0.980 |
| round-3 harness (few citations) | rules only | 0.938 | 0.056 |

These numbers are NOT a claim about real answers: the test split comes from the same generator family as train (the
same answer structure, different names), so it measures generalisation across names, not across writing styles. The
round-3 harness row shows the cost of domain-first on answers without citations: recall collapses by design. The only
independent number is the reviewer's, at the baseline above; a re-run of the reviewer's fresh corpus is the
acceptance test for this round.

## Review round 5 (independent review of 659a5e8e on the reviewer's fresh corpus) — changes

**The reviewer's fresh-corpus numbers (the only independent ones):**

| Version | Mode | Emerging precision (95% CI) | Recall |
|---|---|---|---|
| 7c501fae (baseline) | rules only | 0.362 (0.298-0.432) | 0.861 |
| 659a5e8e (domain-first) | rules only | **0.928 (0.841-0.969)** | **0.785** |
| 659a5e8e | NER on | 0.678 | 0.772 |

The precision bar was met on the numbers; the round was rejected for the two blockers below. **NER stays OFF by
default** (`SEO_IDRIS_NER` unset): on the fresh corpus it made precision WORSE (0.678 against 0.928), so it is an
experimental option, not a recommended setting.

- **BLOCKER fixed — double counting.** `compute()` could raise (`wilson` math domain error) because an emerging
  presence k exceeded the usable answers n: `_merge_names_into_domains` SUMMED a text-only name's answers and
  per-scope counts onto its domain candidate, so an answer that named the brand two ways, or named it and cited its
  domain, counted twice (and inflated the eligibility count). Every candidate count is now derived from SETS of answer
  ids (`_new_candidate` / `_finalise`); a merge is a union. Invariant k <= n is enforced in `_rate`: in tests
  (`STRICT_INVARIANTS`, set by `tests/test_idris.py`) a violation raises; in production the rate is marked
  `INCONSISTENT` (no interval, never a crash) and so stays out of every view. Regression tests cover a name and its
  domain in the same answers, and two spellings in one answer.
- **BLOCKER fixed — injection guard.** The copy is Theo's 49732dd3 version byte-for-byte (separate commit), with Theo's
  structural per-pattern ReDoS test and size test run on this copy.
- **Shorteners and link-in-bio redirectors** (`SHORTENER_HOSTS`: bit.ly, t.co, tinyurl.com, ow.ly, rb.gy, lnkd.in,
  goo.gl, linktr.ee, amzn.to, ...) are UNRESOLVED citations — never a candidate — unless Callum's resolver followed
  them (`SEO_CALLUM_RESOLVE_URLS=1`), when the landing domain decides.
- **News aggregators and communities** (news.ycombinator.com, Techmeme, Flipboard, Google News, MSN, ...) are on the
  platform list. **PSL private suffixes** keep the whole registrable name as the key (acme.github.io, acme.myshopify.com
  and acme.com are three candidates). **`idris_platforms.txt` is domain-only** (the one path entry, junglescout.com/blog,
  is gone; path entries in `KNOWN_PUBLISHERS` too) and validated at load: any entry that is not a bare host name refuses
  to load.
- **Products, decided consistently:** a product name keys to its maker's domain. When the maker is on the platform
  list, the platform's name alone or followed only by product words (`PRODUCT_WORDS`: "HubSpot", "HubSpot Sales Hub")
  is `PLATFORM_PRODUCT` — never a candidate unless the tenant tracks the platform; word-aligned, so "Apple Valley Dental"
  is not Apple's. When the maker is not a platform, the product name merges into the maker's cited-domain candidate and
  is listed in its `names` ("Stripe Billing" -> key `stripe`, domain stripe.com, names ["Stripe Billing"]).

My own cited fixtures after this round (self-authored, same caveats as round 4): train 1.000 / 0.981, test 1.000 /
0.980, rules only.

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
