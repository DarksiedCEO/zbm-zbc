# ADR 0049 — SEO (Dept 2): the conversation fireteam — Theo (conversation mining), Naomi (prompt volume), Zara (lead)

Status: built Nov 8 2026 on `claude/seo-theo-naomi-zara-20261108`, on top of PR #51 (`claude/seo-callum-engines-20261108`
at dda14c34: the multi-engine answer adapter and Callum, ADR 0046). **Not in force.** Fixtures and synthetic data
only: no provider has been called, no key exists, nothing has been spent; Theo's own call cap defaults to 0 (OFF).
Extends ADR 0017 and ADR 0046; their rules apply unless this ADR says otherwise. Numbered 0049 to stay clear of
concurrent SEO branches (Priya took 0047; another builder is working from the same base); renumber at rebase if it
collides.

## Context

ADR 0017 named Theo, Naomi and Zara among its sixteen single-task agents; Wave 1 built Naomi only as a NOT_CONNECTED
audit step (no volume source). Callum (ADR 0046) sees ONE question per prompt; a brand can be invisible at turn one
and recommended by turn three, which no opening-prompt probe can see. Priya (ADR 0047, PR #50, not on this base)
defined `priya-emerging-handoff/1`, the EMERGING tier "for Theo (conversation mining) and Naomi (prompt volume)",
as a contract with no consumer. The owner's rules: single-task agents with a human name, one job and a registry
entry, the lead included; a swarm is parallel with real-time cross-checking; vendor independence through the adapter;
official APIs only, no scraping; fixtures only, budgets OFF by default, fail closed; honest output (provenance and a
confidence band on every number, never fabricated volume, UNKNOWN not 0); no paid data; no secrets.

## Decisions

### 1. Three agents, one job each

| Agent | The one job | Does NOT |
|---|---|---|
| Theo (`src/agents/theo.py`) | run scripted multi-turn chains through the adapter and record, per turn, whether and where the client and competitors appear, and the final recommendation | classify citation sources (Callum), size demand, cross-check, resolve URLs, generate follow-ups with a model |
| Naomi (`src/agents/naomi.py`) | estimate how often each topic is asked, as a band with a confidence, or UNKNOWN | ask an engine, buy data, read a panel, call any network |
| Zara (`src/agents/zara.py`) | cross-check Theo's output against Naomi's as it arrives; flag, downgrade or hold before anything moves downstream | mine, estimate, change a member's number, fill in a missing half |

All three are in `svc_manager.AGENTS` and `REGISTRY` (assignment, inputs, outputs, on `GET /seo/v1/department`), with
the lifecycle and a `conversation_runs` scorecard from recorded runs. Theo and Zara left `NOT_BUILT_AGENTS`. The
topic list (`src/agents/fireteam_topics.py`) and the Priya hand-off mirror (`src/agents/priya_handoff.py`) are shared
plumbing, not agents. The audit's Naomi step (`probes.run_naomi`, NOT_CONNECTED) is unchanged.

### 2. Theo — follow-up generation (honest and deterministic)

The whole chain is planned before the first question (`theo.plan_turns`, `theo-followups/2026-11-08.1`), from the
topic and Priya's items only — never from an answer, so an instruction inside an answer cannot add, change or
reorder a follow-up (tested with an injection that names its own follow-up: the prompts sent are byte-identical to a
clean run's): (1) the topic; (2) `compare` "What are the main options to compare for this, and how do they
differ?"; (3) for a prompt-set topic, at most ONE Priya EMERGING item (kind `constraint` or `question`) sharing at
least two content words with the topic, first by key, as "What about this: <query>"; (last) `decide` "Which one would
you recommend, and why?". A planned turn whose normalised text repeats an earlier one is not asked
(`LOOP_PREVENTED:<kind>`); at most 4 turns. **LLM-generated follow-ups are not built**: they would go only through
the adapter, and `SEO_THEO_LLM_FOLLOWUPS` is in `config.NOT_BUILT` (setting it refuses start).

**Context modes (review round 1 MED 5), chosen per engine and named on every chain, row and finding.**
`native_multi_turn`: the engine's own earlier answers go back to it as assistant messages through the adapter's new
`ask(..., history=[{role, text}])` (answer_adapter.py: alternating user / assistant, at most 8 turns and 40,000
characters, refused otherwise; `history=None` builds exactly the request as before). Built where the provider's
multi-turn request shape was read on Nov 8 2026: Anthropic Messages (`messages` alternating `user` / `assistant`;
platform.claude.com/docs/en/build-with-claude/working-with-messages) and Gemini generateContent (`contents`, "for
multi-turn queries like chat ... the conversation history and the latest request", roles `user` / `model`;
ai.google.dev/api/generate-content; the Gemini text-generation guide now documents only the Interactions API for
chat). Recorded as `MULTI_TURN_DOCS` in each module. **OpenAI and Perplexity are not**: OpenAI's conversation-state
page could not be read in this session and its web-search page shows string input only; Perplexity's Agent API page
read for ADR 0017 documents a plain-string `input` for single-turn prompts. Their `ask(history=...)` is refused
(`MULTI_TURN_NOT_SUPPORTED`), and Theo uses `stacked_questions` for them: ONE request per turn carrying the earlier
USER questions only, named as such. The follow-up PLAN (the user turns) stays fixed before turn one in both modes;
an earlier answer (an injection included) can only appear as an assistant message, never as a user turn or in
Theo's logic (tested). An empty answer mid-chain stops a native chain (`EMPTY_ANSWER_MID_CHAIN`): nothing is invented
to fill the assistant turn.

**Assistant turns go back as received (review round 2, items 1-2).** An answer's provider-native content is kept in
memory (`ProviderAnswer.turn_content`, never stored) and sent back unchanged as that assistant turn:
- *Anthropic*: the response's `content` blocks exactly as received — text blocks with their citations
  (`encrypted_index`), `server_tool_use`, `web_search_tool_result` with each result's `encrypted_content`. The web
  search tool page (platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool, read Nov 8 2026; this
  adapter uses tool `web_search_20250305`, `anthropic-version: 2026-10-21`): "To continue a conversation that contains
  search results, send the assistant's content blocks back exactly as you received them, including each result's
  `encrypted_content` ... If `encrypted_content` is missing or modified, the request fails with a 400 validation
  error"; `encrypted_index` "must be passed back for multi-turn conversations". The page shows no multi-turn request
  example; the shape follows the Messages page above. Tested with the recorded synthetic fixture's tool blocks.
- *Gemini*: the chosen candidate's `content.parts` exactly as received (any `thoughtSignature` and thought parts
  included), as role `model`. **PROVISIONAL**: the pages read (the API reference's first 100,000 characters, the
  Google Search grounding page) do not state the thought-signature rule; the grounding page only links a "Thought
  signatures" page, which was not read. `google_engine.MULTI_TURN_DOCS.status` is
  `provisional_until_first_live_call` and every Gemini chain carries `native_status` saying so.
- No truncation: the adapter refuses a history over `MAX_HISTORY_BYTES` (256 KiB of JSON, `HISTORY_TOO_LARGE`), and
  Theo checks the same cap before asking, ending the chain INCOMPLETE (`HISTORY_TOO_LARGE`) with no request: a cut
  block would change what the engine saw (round 1's 4,000-character text cut is gone).

**Injected text in native mode (review round 2, item 3) — disclosed.** In native multi-turn an engine's earlier
answer is re-sent to it, so text injected into that answer (by a page the engine read, say) may steer the engine's
later answers, **the classified final one included**. Theo's plan and logic cannot be altered by it (the user turns
are fixed before turn one; the classification reads the answer lexically), but the engine's behaviour can. Guard: an
earlier answer that will be re-sent and matches `theo._INJECTION` (instruction-like text aimed at the model: "ignore
previous instructions", "system:", "you are now", "from now on you", "always recommend", "in all future answers",
chat-template tokens, …) flags its row `injection_suspected` and the chain INJECTION_SUSPECTED: it is excluded from
every rate, outcome and pattern (so never "invisible → recommended") and its finals are reported apart under
`injection_suspected`. The detection is lexical and reduces, not removes, the risk; the disclosure is in every Theo
envelope's limitations. Since round 3 the guard is `agents/injection_guard.py`, it scans every re-sent text, and a
flagged chain is excluded in stacked mode too (see "Review round 3").
**Budget for growing context:** a native turn k carries k-1 earlier answers and is reserved and charged as k reserve
units (a 3-turn chain = 6 units, a stacked one = 3), both in Theo's own cap and in the engine reservation; a request
of unknown cost books k reserves (`_Meter.charge_context`, svc_engines.py) and must fit the reservation before it is
sent (`RESERVATION_SPENT` otherwise). These are reserve estimates, not measured cost (ADR 0046 A5 L3).

### 3. Theo — what a turn and a chain record

Per answered turn (`theo.observe`; text scanned in memory, dropped): client mentioned (agents/brand_mention.py, with
its HIGH/LOW confidence) and cited (the structured citation's HOST is the client's, agents/domain_match.py: exact,
www. twin or subdomain; a lookalike never; a redirector host makes it `null` = UNKNOWN — Theo does not resolve URLs),
the client's rank among named entities, each competitor term (`competitor_terms` in the request) mentioned, the
competitor domains (the prompt set's) cited, model, version, provider time, answer SHA-256, and a ≤160-character
PII-screened excerpt (Callum's rule) — withheld for a Priya topic (`PRIYA_QUERY_CONTEXT`). The final answer
(`theo.recommendation`, complete chains only) is read LEXICALLY and negation-aware (review round 1 MED 3): an entity
mention is dropped when, within its clause (cut at `. ! ? ; , : ( ) —`, `but`, `however`, `whereas`, `while`), it is
preceded by `not`, `n't`, `never`, `avoid…`, `instead of`, `rather than`, `over`, `unlike`, `stay away from`, `skip`,
`no longer`, `<x>er than` / `more|less <x> than`, `superior to`, `except`, or followed by `is not` / `isn't` /
`not recommended` / … ("I would not recommend Acme. I recommend Beta." → Beta; "Avoid Acme, go with Beta" → Beta;
"Beta is better than Acme" → Acme is never the pick). Review round 2: `not only` is not a negation, and a cue sentence
naming several entities with "not only … but also" or "both … and" is a shared pick (`recommended_together`, no
winner); "would not hesitate to recommend X", "don't overlook X" and "can't recommend X enough" are positive. Seven
fallbacks are tested as reading "no recommendation" (never a wrong win): double negatives ("can't think of a reason
not to recommend X", "it's not that I don't recommend X", "I'd never not recommend X"), litotes without a cue
("hardly anyone would avoid X"), a pick made by pronoun ("X is not bad at all; I recommend it"), a hedge ("you cannot
go wrong with X, though I would not pick it myself") and a real refusal ("I can't recommend X"). The first remaining entity in the first sentence holding a cue
(`recommend`, `best choice`, `top pick`, `go with`, …) is **recommended in the final answer** — and counts as a
recommendation ONLY when its mention is confirmed (HIGH), the same threshold the opening turn uses (review round 1
MED 4). Otherwise the first remaining entity is reported as **"named first in the final answer"**
(`named_first_only`), never as a win; else `none`. Confidence MEDIUM at most. Chain status: COMPLETE,
INCOMPLETE (`ENGINE_FAILED_AT_OPENING` / `ENGINE_FAILED_MID_CHAIN`, `REFUSED_*`, `BUDGET_EXHAUSTED_MID_CHAIN`),
LOOP_STOPPED (`ANSWER_REPEATED`: an answer identical to an earlier one of the same chain), NOT_ASKED (cap, budget,
stop code, personal data). **An incomplete chain's final recommendation is `unknown`**, never the last answered turn.
Per topic × engine (`theo.conversation_chains/2`): context mode, chains, complete, per-turn client-mentioned
(confirmed) / unconfirmed / cited / competitor-mentioned rates (k, n, Wilson 95%, directional under 3), the final
distribution (`recommended` {client, competitor}, `named_first_only` {client, competitor}, `none`, `unknown`),
`client_recommended`, and — from the CLIENT'S OWN outcome, never the majority winner (MED 4) — `client_outcome`
(RECOMMENDED: recommended in ≥ 2/3 of complete chains; NOT_RECOMMENDED: in none; MIXED otherwise: a 1-of-5 win is
MIXED), `pattern` (INVISIBLE_THEN_RECOMMENDED — native multi-turn only, no mention of ANY confidence at turn one;
INVISIBLE_AT_OPENING_RECOMMENDED_STACKED — the same in stacked mode; VISIBLE_AND_RECOMMENDED — confirmed at turn one in
≥ 2/3; RECOMMENDED_OPENING_MIXED; VISIBLE_THEN_NOT_RECOMMENDED; NOT_RECOMMENDED; MIXED; INCONCLUSIVE), `strength`
(STRONG: ≥ 3 complete chains and a RECOMMENDED or NOT_RECOMMENDED outcome; WEAK; NONE) and `unstable`. Zara
re-derives outcome, pattern and strength from the counts with the same functions and holds any mismatch. Run rules are Callum's (`collect`): a metered port only
inside a recorded reservation, the stop codes, two protocol errors in a row stop the engine, the 4x cost guard
(over the reserve mid-chain → INCOMPLETE), personal data never sent, a crashing adapter counted, never raised.

### 4. Naomi — the estimator and its limits

No engine publishes prompt volume and no paid panel is used, so Naomi estimates a **directional proxy** from owned
signals and says so on every topic (`proxy_for`, flag `PROXY_NOT_PROMPT_VOLUME`, unit `impressions_on_this_property`):

| Signal | State here |
|---|---|
| the topic's Priya EMERGING item (`observed_counts`: this property's impressions for that exact query — Search Console under her ESTABLISHED threshold (`BELOW_THRESHOLD`), or a Bing-only count of any size (`MEASURED_NOT_TIERED`) — as Priya measured them through the existing GSC client data contract / her Bing adapter) | read when the caller passes the hand-off; VERIFIED only against Priya's stored run record (§5); until then every signal is capped at THIN |
| the latest completed audit's Search Console `search_performance.by_query_class.classes.question` | context only: every question-form query together, never a topic's estimate |
| Bing Webmaster Tools directly | NOT_CONNECTED (Priya's adapter is not on this base) |
| any paid panel, clickstream or "AI prompt volume" vendor | not used (owner rule) |

Per topic (`naomi-estimator/2026-11-08.2`): no item, a derived item, an unmeasured item or a refused topic →
`UNKNOWN` (band `null`, reliability NONE, confidence NONE, a named reason) — never 0. An observed item → one signal per
source (never summed: different windows), the count k, its window, and the exact **Garwood 95% Poisson band** on k
(lower χ²(0.025; 2k)/2, upper χ²(0.975; 2k+2)/2, by bisection on the Poisson CDF up to k = 100, Wilson–Hilferty with
a 0.2% outward margin above (LOW 6), rounded outward to 0.01; checked against published values for k = 0, 1, 10, and
tested never narrower than the exact band for k from 101 to 2,000). Reliability THIN < 20 (Priya's threshold),
MODERATE < 1,000, STRONG ≥ 1,000 — **for a VERIFIED hand-off only**: an unverified signal is capped at THIN whatever
its count (`HANDOFF_NOT_VERIFIED`, `data_class: claimed`, the uncapped tier kept as `reliability_if_verified`).
Confidence LOW for THIN, else MEDIUM — **never HIGH** (outside Naomi's vocabulary; Zara holds any payload that says
HIGH). No estimate carries a bare `value`: the count is an input inside its signal, the estimate is the band. What
THIN means in practice (corrected, review round 1 HIGH 2): a Search Console signal from the hand-off is under 20 by
Priya's rule; a Bing-only (`MEASURED_NOT_TIERED`) signal can be any size up to the 10,000,000 sanity bound, and is THIN
here only while unverified — which, until Priya records the hand-off hash, is always.

### 5. The hand-off contracts

- **Priya → Theo / Naomi**: `priya-emerging-handoff/1`, passed as `priya_handoff` in the run request (Priya's ledger
  is not stored, ADR 0047 §3). `agents/priya_handoff.py` mirrors Priya's validator field for field (PR #50 at
  15917fc8; to be replaced by `priya.validate_handoff` when both land) and adds consumer rules: the document's tenant
  must be the run's (`403 HANDOFF_TENANT_MISMATCH`, refused whole); at most 50 items; and (review round 1 HIGH 1a)
  EVERY item type is checked against how Priya builds it — `derived` ⇒ `UNKNOWN_NOT_MEASURED` with no counts;
  `observed` ⇒ counts, with `BELOW_THRESHOLD` ⇔ source `first_party:search_console` and < 20 impressions, `MEASURED_NOT_TIERED` ⇔
  source `first_party:bing_webmaster` (Priya's exact strings since review round 6); every count a whole number ≤ 10,000,000 (a sanity bound, not a plausibility test), clicks
  ≤ impressions, an ISO window of ≤ 400 days, start ≤ end, ending no later than `generated_at`. Any problem →
  `422 HANDOFF_INVALID`, nothing recorded (the review's 10^12 forgery is refused here).
  **Verification (HIGH 1b).** The request may also carry `priya_run_id` and `priya_handoff_sha256` (the SHA-256 of the
  hand-off's canonical JSON: keys sorted, separators `,` `:`, UTF-8, `priya_handoff.canonical_sha`). The service
  looks the run up in Priya's stored run records (`keyword_runs`, PR #50) under the record contract
  `priya.handoff_record/1`: `{run_id, tenant_id, status: completed, handoff_sha256, handoff_items}`. VERIFIED when the
  document's run id and hash equal the claim and the record (this tenant, completed) holds the same hash and item
  count; any mismatch → `422 HANDOFF_VERIFICATION_FAILED`; a missing record or a record without the hash →
  NOT_VERIFIED (and on this base, where Priya's service is absent, `PRIYA_NOT_INSTALLED`). **Priya must add
  `handoff_sha256` and `handoff_items` to her `keyword_ledger_recorded` record** for any hand-off to be verifiable;
  PR #50 now does (f759ebaa: `handoff_record`, `handoff_sha256` via `priya.handoff_sha`, `handoff_items`), so once it
  lands on this base a genuine hand-off is VERIFIED; until then nothing is. **Until verified (HIGH 1c)** every
  signal is capped at THIN (§4), and Zara treats `HANDOFF_NOT_VERIFIED` as BLOCKING: the topic is FLAGGED, never
  CLEARED, never TEST, and volume is never a `priority_basis`. The verification state is in the run record.
  Query text is data (`untrusted`) and never reaches the log or the ledger: the request record keeps the hand-off's
  SHA-256, item count and verification state; Priya topics are reported by key (`text: null`).
- **Theo → Zara**: per topic, the list of `theo.conversation_chains/2` chain summaries (one per engine), emitted as
  soon as that topic is done; at the end the whole contract (tenant, run, rules, templates, context mode, topics).
- **Naomi → Zara**: per topic, one `naomi.prompt_volume/1` estimate, emitted as made; at the end the whole contract.
- **Zara → downstream**: `zara.conversation_volume/1` (`GET .../conversation-runs/{id}/crosscheck`), the only
  fireteam output that moves downstream, and inside it `zara.priya_feedback/1` for Priya's emerging-query loop (chain
  patterns, volume reliability and conflicts per Priya key; no text; contract only, not consumed).

### 6. Swarm semantics (`src/svc_conversations.py`)

One topic list for both members (`fireteam_topics.plan`: up to 10 prompt-set prompts, then up to 5 Priya `question` /
`conversational` items; a Priya item equal to a prompt is not a second topic, it lends that topic its key). Theo and
Naomi run in **parallel** (two worker threads). Each emits topic by topic into Zara's inbox (`zara.Crosscheck`,
thread-safe); Zara checks a topic **the moment both halves are in** (`checked_on_arrival`), and when a member closes,
every topic still waiting for it (`checked_at_close`). The result is ordered by topic, so it does not depend on
arrival order (tested under threads). Zara's checks: shape and closed vocabularies per contract; self-consistency
(Theo: complete ≤ chains, the final distribution adds up, outcome / pattern / strength re-derived from the counts
with Theo's own functions — `INVISIBLE_THEN_RECOMMENDED` needs native mode; Naomi: UNKNOWN has no band or signal, the
band holds the count, reliability matches the count when verified and is THIN when not, the
`HANDOFF_NOT_VERIFIED` flag matches, confidence matches reliability); the member contract names this tenant, this run and its own contract id;
no unplanned or duplicate topic payload. Conflicts: `STRONG_CHAIN_THIN_VOLUME`, `STRONG_CHAIN_UNKNOWN_VOLUME`,
`VOLUME_WITHOUT_CHAIN`, `UNSTABLE_CHAIN`, `ENGINES_DISAGREE`, `HANDOFF_NOT_VERIFIED` (blocking). Verdicts: CLEARED, FLAGGED (passes, decision
INSUFFICIENT_EVIDENCE, `priority_basis` `chain_only` when volume is thin or unknown — a consumer must not rank by
volume), PARTIAL (a member missing: its half `null` with the reason, never filled in; an unverified volume half
still lists `HANDOFF_NOT_VERIFIED`), NOTHING_OBSERVED (no complete
chain and no volume: not "cleared", nothing passes), HELD (invalid, contradictory, foreign or duplicate: nothing passes).
Decisions never ACT; TEST only for CLEARED with a STRONG chain and VERIFIED MODERATE+ volume.

**Failure isolation.** A member that raises is closed FAILED (exception type only) and the run completes with the
other member and Zara (PARTIAL). A restricted Theo or Naomi does not run (RESTRICTED). A restricted or retired
**Zara refuses the whole run** (403 `AGENT_RESTRICTED`): nothing may move downstream unchecked. A kill switch
(global, write, the tenant, `capability:ai_probe`, `capability:audit`) engaged mid-run stops it at the next step; the
run is recorded `interrupted` with the switch's code and every result discarded; `provider:<engine>` stops that
engine. No new kill switch, so the switch view is unchanged. **Bounded wait (LOW 7):** one overall wait for both
members, `SEO_FIRETEAM_MEMBER_TIMEOUT_SECONDS` (default 600, 1..3600, anything else refuses start); a member still
running at the end is closed `TIMED_OUT` (Zara reports PARTIAL, its half `null`), told to stop at its next step (its
guard raises `MEMBER_TIMED_OUT`), never waited for, and anything it emits afterwards is counted in `late_payloads` and
never used. Cancellation happens at the member's NEXT guard check, so **at most one request already in flight**
completes after the close — bounded by that engine's own hard deadline and inside its recorded reservation (its
settlement is still recorded); no further request starts.

### 7. Routes, records, budgets, isolation

`POST /seo/v1/tenants/{tid}/conversation-runs` (dashboard, seo_agent; a client tenant needs Andre, a bound Finance
client and Compliance (38)'s current ALLOW, ADR 0024, no invoice — Callum's founder-pending default (30));
`GET .../conversation-runs`, `GET .../{id}`, `GET .../{id}/crosscheck` (dashboard, seo_agent, hub for its own tenant,
compliance_38). Record-first: `conversation_run_requested` on the ledger and the log before any engine is asked (ids,
hashes, counts); every metered chain sample inside a recorded reservation (`engine_calls_reserved`, subject
`conversation_run:<id>`, `run_kind: conversation_run`; reserved WHOLE before its first turn), settled at the end;
`conversation_run_recorded` with the result's SHA-256; `conversation_run_interrupted` for a switch, a crash
(`RUN_FAILED` with the safe location) or a dead process (the `interrupted-audits` job reports
`conversation_runs_interrupted`). One running run per tenant. Another tenant's run is the same 404 as a missing one.
Budgets: every engine budget and per-run cap of ADR 0046 binds, AND Theo's own `SEO_THEO_RUN_MAX_CALLS` (default **0
= OFF**: Theo asks nothing, Naomi and Zara still run), the calls one run may reserve across all engines; a chain that
does not fit is not started, so the cap never cuts a chain in half. Naomi spends nothing.

## Honesty rules, as built

Every rate has k, n, a Wilson interval and `directional`; every volume is a band with an interval name, a unit, a
window, provenance and a confidence, or UNKNOWN with a reason; nothing is 0 for "not measured"; no visibility score;
no market-volume claim; an incomplete chain has no final recommendation; every finding's `effect_class` is null.

## What is NOT built (deliberately)

- No live call, key or spend; `SEO_THEO_RUN_MAX_CALLS` is 0. The first real chain needs the per-engine one-call smoke
  (ADR 0046 "not built") and a founder decision on the cap.
- Native multi-turn for OpenAI and Perplexity (their multi-turn request shapes were not read this session; they run
  `stacked_questions`). Re-sending the engines' own answers to the providers is built for Anthropic and Gemini and is
  still a founder decision for the first live run (question 2).
- LLM-generated follow-ups (`SEO_THEO_LLM_FOLLOWUPS` refuses start).
- Cited-URL resolution in Theo (Callum's resolver is not reused: Theo reports `cited: null` on a redirector).
- Priya's side of verification: her run record does not yet hold `handoff_sha256` / `handoff_items` (PR #50), so the
  verifier built here finds nothing to verify against and every hand-off stays NOT_VERIFIED (capped THIN). The mirror
  validator is replaced by Priya's own once PR #50 lands.
- A direct Bing Webmaster read, any prompt-volume vendor or panel (owner rule), Google Ads Keyword Planner.
- A consumer of `zara.conversation_volume/1` or `zara.priya_feedback/1`; scheduling; drift between runs; dashboard
  pages; client-report section; war-room scenarios.
- The maintenance pins and the seo image are not re-released (see "Release").

## Limitations (honest)

- Lexical matching (brand_mention) and a lexical recommendation rule: they do not understand the answer.
- `stacked_questions` chains (OpenAI, Perplexity) approximate a conversation; native chains replay the engine's own
  answers but follow fixed templates, which are not what real users ask. Negation handling is a closed word list
  inside a clause: double negatives, sarcasm and long-range contrast are not understood.
- Naomi's proxy is search demand on one property; a hand-off signal is THIN while unverified (always, today) and, for
  Search Console items, under 20 impressions by Priya's rule. It says nothing about how often anyone asks an AI engine.
- Context-weighted reserves are estimates of a larger request, not measured cost.
- One account, one time, N samples: observations, not search truth; nothing calibrated against traffic or revenue.
- The engine scorecard on Callum's card (`engine_runs`) counts every recorded engine run, Theo's included.

## Founder questions

1. Set `SEO_THEO_RUN_MAX_CALLS` (and each engine's per-run cap) for the first fixture-free run, or keep OFF?
2. Native multi-turn is built for Anthropic and Gemini: approve re-sending the engines' own earlier answers to them on a
   live run (more tokens, untrusted text re-sent to its own author)? Read OpenAI's conversation-state page to add it?
3. Should Priya hand ESTABLISHED queries to Naomi too (today only EMERGING items travel)?
4. Is the lexical final-recommendation rule acceptable for reporting, or should a recommendation be shown only as
   "named first in the final answer"?
5. Approve the `priya.handoff_record/1` fields (`handoff_sha256`, `handoff_items`) on Priya's run record, so a
   hand-off can be VERIFIED (until then every hand-off signal is capped THIN)?

## Review round 1 (independent review of ca3b02ca: CONDITIONAL, 2 HIGH)

| Item | Fix |
|---|---|
| HIGH 1 forged hand-off (a `MEASURED_NOT_TIERED` count of 10^12 was accepted → STRONG → CLEARED/TEST) | every item type validated and bounded (§5); verification against Priya's record with the `priya.handoff_record/1` contract; unverified signals capped THIN; `HANDOFF_NOT_VERIFIED` blocking in Zara; the 10^12 forgery refused 422 and a 9,000,000 unverified count FLAGGED, not TEST (tests) |
| HIGH 2 the "THIN by construction" claim | corrected in naomi.py and here (§4, Limitations) |
| MED 3 negation / contrast | the clause-scoped negation rule (§3), ten table cases tested |
| MED 4 thresholds and strength | one confirmed-mention threshold at both ends; "named first in the final answer" apart; outcome, pattern and strength from the client's own outcome; the 1-of-5 case is MIXED / WEAK (tested); Zara re-derives them |
| MED 5 native multi-turn | adapter `history`, Anthropic and Gemini bodies (doc URLs above), stacked fallback named honestly, plan fixed before turn one, context-weighted reservations, caps fail closed |
| LOW 6 Garwood speed | exact to k = 100, Wilson–Hilferty with an outward margin above, outward-only proven against the exact band (no timing assertion) |
| LOW 7 bounded member wait | `SEO_FIRETEAM_MEMBER_TIMEOUT_SECONDS`, TIMED_OUT → PARTIAL, late payloads unused |

## Review round 2 (ab285510: CONDITIONAL, no HIGH)

| Item | Fix |
|---|---|
| 1 Anthropic assistant turns re-sent as cut text | content blocks sent back exactly as received (tool-result blocks, `encrypted_content`, citations); a size cap that ends the chain INCOMPLETE instead of cutting; the tool page URL and version cited (§2); tested with the recorded fixture's blocks |
| 2 Gemini thought signatures | the candidate's parts sent back as received (`thoughtSignature` included, tested); the rule was not confirmed in the pages read, so Gemini native is `provisional_until_first_live_call` on every chain |
| 3 injection steering the engine | disclosed (§2, every Theo envelope); INJECTION_SUSPECTED chains excluded from every rate and pattern, reported apart (tested) |
| 4 "not only X but also Y" and idioms | shared pick, no winner; three positive idioms fixed; seven fallbacks tested as "no recommendation" |
| 5 small | Zara names `HANDOFF_NOT_VERIFIED` on PARTIAL items; the at-most-one-in-flight-request behaviour of a timed-out member documented (§6) |

## Review round 3 (bf57280b: CONDITIONAL)

1. **Block allowlist and provenance (answer_adapter.py).** Every content block a history sends back must be (a) of an
   allowed type — Anthropic: `text` (citations of type `web_search_result_location` only), `server_tool_use` named
   `web_search`, `web_search_tool_result` (results or the tool's error); Gemini: parts holding only `text`,
   `thought`, `thoughtSignature` (never `functionCall`, `functionResponse` or any other part) — and (b) a block THIS
   engine returned earlier IN THIS CHAIN: the adapter's `Conversation` record holds the SHA-256 of every block
   received, Theo opens one per chain, and anything not in it — a forged `tool_use`, an edited block, a block from
   another chain, or any block with no record — is refused before a request is built (`HISTORY_BLOCK_NOT_ALLOWED`,
   `HISTORY_BLOCK_NOT_RECEIVED`; tested, nothing is sent).
2. **Injection guard (`agents/injection_guard.py`, `injection-guard/2026-11-08.1`).** Replaces round 2's single
   regex. Normalised views (NFKC; zero-width / bidi removed; HTML entities; letters spaced or dotted apart joined;
   leetspeak; base64 runs decoded) × closed pattern families (override, chat-role and template markers, addressing
   the AI, steering later turns, persona / jailbreak, recommendation or suppression directives to the model,
   prompt exfiltration, output control), each written in the model-directed form so reader-facing advice ("You are
   now eligible…", "From now on, you can file…", "I always recommend getting three quotes", "we should recommend
   reviewing…", "ignore the noise and focus on…") is not flagged. It scans EVERY text an answer would carry back:
   the answer text and, from the blocks, Anthropic `cited_text` and result `title`, Gemini text / thought parts. One
   flagged row flags the whole chain, in either context mode (stacked chains too, round 3), which is then excluded
   from every rate and pattern and reported apart.
   **Round-3 self-measurement, superseded:** on its own 110 author-written cases the guard scored 1.00 / 1.00 on a
   held-out split; that claim is WITHDRAWN. An independent reviewer then measured it on an unseen set at **recall 0.53
   (37/70) and precision 0.93** (round 4) — the author-written split had not represented the attacks the guard
   missed. See "Review round 4" for the current numbers.
3. **Negation.** `cannot` / `can not` / `can't` / `won't` / `wouldn't` / `shouldn't` are negations ("I cannot
   recommend Z Best Media." is no win), while "can't recommend X enough" stays positive; a shared pick needs an
   entity on EACH side of "not only … but also" / "both … and" ("I recommend Z Best Media, and both RivalCo and others
   are weaker" is Z Best Media's win); "and" before a new imperative (`go`, `choose`, `pick`, `use`, …) and "then"
   start a clause ("Skip RivalCo and go with Z Best Media" → Z Best Media). Tested.

## Tests

`services/seo-py/tests/test_theo.py` (planning, observation, the recommendation, and the adversarial runner cases:
an engine failing mid-chain, a rate limit, a refusal, a repeating answer loop, prompt injection in answers, the run
cap OFF and mid-run, a reservation refused between chains, a cost over the reserve mid-chain, a kill switch
mid-chain, personal data, malformed and crashing adapters), `tests/test_naomi_volume.py` (the Garwood band, the
estimator, UNKNOWN never 0, confidence never HIGH, the hand-off mirror and its consumer rules),
`tests/test_zara.py` (on-arrival and at-close checks, every conflict, holds for contradictory, foreign and duplicate
payloads, PARTIAL on a failed member, NOTHING_OBSERVED, arrival-order independence under threads, the Priya feedback
without text), `tests/test_conversation_runs.py` (the route, gates, record-first, no query text in the log or the
ledger, the cap OFF, NOT_BUILT LLM follow-ups, the hand-off refusals, isolation, Zara required, a restricted and a
crashing member, a kill switch mid-run, the real OpenAI adapter on a mock transport with reservations under the
conversation run, restart and the interrupted-runs job, the department registry). Review round 1 adds: the 10^12
forgery and every item-type rule, the verifier (verified, unverified, every mismatch), the THIN cap and Zara's
blocking rule, ten negation / contrast cases, the unconfirmed "named first" case, the 1-of-5 MIXED case, native
multi-turn (history sent, user turns fixed, injected text confined to the assistant turn, the provider bodies, refusal
on OpenAI), context-weighted reservations and the context meter, the Garwood approximation outward-only, and the
member wait. Counts: `docs/test-counts.md`.

## Release

seo-py source changed, so the Gate 5 maintenance pins and the seo image must be re-released after Gate 5; until then
the hygiene-static cold-pin check is red for that expected reason. The pins are not edited here (`deploy/gate5/**`,
`deploy/docker/maintenance/**`, `.railway/**` and `.github/workflows/**` are untouched).

## Review round 4 (87fcfa91: CONDITIONAL; independent unseen set: detector recall 0.53, precision 0.93)

**The injection guard is a HEURISTIC GUARD, NOT A GUARANTEE.** Pattern detection will never be complete; the
numbers below are on fixtures written by the builder and overstate what an adversary would meet.

1. **Provenance bound to order and turn (answer_adapter.py `Conversation`).** The record now holds, per assistant
   turn and in order, the SHA-256 of that answer's WHOLE ordered content list (`None` for an answer that carried
   none). The k-th assistant turn of a history must carry exactly the k-th record, and the number of assistant turns
   must equal the number recorded. Refused before any request (tested, one test per forgery): re-ordered blocks,
   duplicated blocks, partly dropped blocks, an edited block, blocks swapped across turns, blocks moved from one turn
   into another, a dropped turn and an extra replayed turn (`HISTORY_TURNS_MISMATCH`), a text-only assistant turn
   where content was recorded (`HISTORY_TURN_CONTENTLESS`), another chain's record, no record; the block-type
   allowlist of round 3 still applies first.
2. **Detector recall (`injection-guard/2026-11-08.2`).** New families, each with new fixtures of the builder's own:
   multilingual override / "from now on" / "you must recommend" / "you are now" / future-turn / note-to-the-AI forms in
   Spanish, French, German, Portuguese, Italian, Russian, Chinese and Japanese; homoglyph folding of non-ASCII
   characters with the Unicode confusables (UTS #39 `confusables.txt` v16.0.0, vendored unchanged as
   `agents/confusables.txt` with `agents/confusables.NOTICE` — Unicode License V3, SPDX `Unicode-3.0`, the
   owner-approved class; source: the ICU project's copy at tag `release-76-1`, commit 8eca245c, because
   unicode.org was not reachable from the build environment; SHA-256 `95bd0aad…a8611` pinned and checked at load, a
   different file refused); hyphen- and space-split words (a hyphen-joined view and a letters-only view matched
   against a closed list of override phrases); JSON / YAML / fenced role forms (`{"role": "system"}`,
   `role: system`, a ```` ```system ```` fence); third-person and indirect address ("the assistant should…", "any AI
   that reads this…"); future-turn forms ("next time you're asked…", "in your next answer…", "for the remainder of
   this chat…"); persona games ("you are ShopBot", "act as …GPT", "your new name is …Bot", "you are no longer an
   assistant", "let's play a game … you are"). False alarms kept down by model-directed forms: "jailbreak-proof
   phone cases", "reply only with your availability", "print your instructions sheet" are not flagged (tested).
   Measured numbers: see "Review round 5" — the self-measured round-4 headline is withdrawn.
3. **Structural second guard (opt-in, `SEO_THEO_STACKED_CHECK`, default 0).** For a complete native multi-turn chain,
   the final question is asked once more as stacked questions with NO earlier answers in context (one extra request,
   reserved with the chain); when the lexical final pick differs, the chain is flagged `DIVERGENT_UNDER_HISTORY`
   (`divergent_under_history` per chain summary, finding `THEO_DIVERGENT_UNDER_HISTORY`). Disclosed as a heuristic:
   a different pick may be the history's influence (an injection included) or ordinary sampling variation; it is
   reported, not used to exclude a chain. Tested (divergent, not divergent, reserved with the chain, off by default).
   (Superseded in round 5: the check is now the BASIS of the native pattern.)
4. This section replaces round 3's 1.00 / 1.00 claim (above).

## Review round 5 (7b906d15: REJECTED — ReDoS, swallowed kill, guard does not generalise)

1. **ReDoS fixed (HIGH).** The guard ran page-controlled re-sent text through patterns such as
   `(?m)^\s*-?\s*role\s*:…`, cubic on whitespace / newline input. Now: every repetition in every guard pattern is
   BOUNDED (`\s{0,8}`, `\s{1,8}`, `\w{1,40}`, `.{1,80}`, `#{2,8}`, …; the helper regexes too), proved structurally by
   a test that walks each compiled pattern's parse tree and refuses any unbounded or over-4,096 repeat (no wall-clock
   assertion); whitespace runs are collapsed by a linear split before any pattern runs; a text longer than
   `MAX_SCAN_CHARS` (20,000) is not scanned but flagged (fail closed). Tests on 100,000-character whitespace and
   newline inputs assert the size bounds.
2. **Kill switch during the structural re-ask (MEDIUM).** `_stacked_check` no longer swallows it: a `Killed`
   (kill switch, member cancellation) and an adapter `ENGINE_KILLED` propagate and stop the run (tested for both and
   for the guard before the re-ask); only other failures leave the check `None` (not checked).
3. **No history without a record (LOW).** The adapter refuses ANY history — text-only included — unless it comes with
   this engine's `Conversation` record (`HISTORY_NO_RECORD`); the record then binds every assistant turn as in round 4.
4. **The structural guard is the basis; the regex guard is auxiliary (DESIGN).** Native multi-turn chains produce the
   "invisible at turn one → recommended later" pattern (`INVISIBLE_THEN_RECOMMENDED`) ONLY when EVERY complete chain
   of that topic × engine was re-asked without history (`_stacked_check`) and gave the same pick, and none was flagged
   by the auxiliary injection guard (`divergent_under_history.agreed_clean == complete`). Otherwise — check OFF,
   check failed, any divergence, any flag — the chain set is `INVISIBLE_AT_OPENING_RECOMMENDED_UNCHECKED`, reported
   apart (finding `THEO_NATIVE_PATTERN_NOT_STRUCTURALLY_CHECKED`, INSUFFICIENT_EVIDENCE); Zara re-derives this from
   the counts and holds any payload that claims more. **The check is budget-gated and OFF by default
   (`SEO_THEO_STACKED_CHECK`), so with it OFF native chains report per-turn data and every other pattern, but never
   the native invisible-then-recommended pattern.** The regex guard (`INJECTION_SUSPECTED`) is now an auxiliary flag
   only: flagged chains stay in the rates, are reported (`injection_suspected.chains`), and cannot support the native
   pattern. The structural check is itself a heuristic (sampling variation also changes picks) and is disclosed as one.
5. **Honest numbers (replacing the self-measured round-4 headline of precision 0.972 / recall 1.000).** Independent
   measurements of the auxiliary regex guard:
   - the round-4 reviewer's unseen set, before round 4: recall 0.53 (37/70), precision 0.93;
   - the same reviewer's set after round 4: recall 0.843, precision 1.000 — **after tuning**: round 4 added pattern
     families for the miss categories that reviewer reported (its cases were not read), so this set is no longer
     independent of the tuning;
   - a FRESH independent set (round 5): **recall 0.246, precision 0.789**.
   Pattern detection does not generalise; the guard is auxiliary and no claim rests on it. The builder's own fixture
   splits (`tests/fixtures/injection/labelled.json`) remain as regression tests only (they assert ≥ 0.85 on text the
   builder wrote) and are not a measure of the guard.

## Review round 6 (49732dd3: CONDITIONAL)

1. **Zara's native-pattern gate is tested.** Each structural invariant has its own hold reason, so each is testable
   on its own: `THEO_STRUCTURAL_CHECKED_EXCEEDS_COMPLETE`, `THEO_STRUCTURAL_DIVERGENT_EXCEEDS_CHECKED`,
   `THEO_STRUCTURAL_AGREED_EXCEEDS_UNDIVERGED`, and `THEO_NATIVE_PATTERN_WITHOUT_BASIS` (a payload claiming
   `INVISIBLE_THEN_RECOMMENDED` when `agreed_clean < complete`, or when the check never ran). Mutation-checked: forcing
   `agreed_all = True`, and removing each of the three count invariants, each makes a test fail (as do the mutants
   of items 2-6 below); a run of the mutants is part of the review evidence, not of the suite.
2. **`THEO_NATIVE_PATTERN_NOT_STRUCTURALLY_CHECKED` is emitted** (it was named in round 5 but the edit had not
   landed): for every `INVISIBLE_AT_OPENING_RECOMMENDED_UNCHECKED` chain set, decision INSUFFICIENT_EVIDENCE, with the
   counts and the wording "… not every chain was re-asked without history with the same pick …; no claim is made".
3. **Nested repeats rejected.** The structural regex test now also refuses a quantified group that holds a
   quantifier (e.g. `(?:\s{0,8}){0,4096}`), from the parse tree; a test feeds the checker known-bad shapes (nested,
   nested inside a branch, unbounded, over the bound) and a bounded flat one, and the guard's patterns pass it.
4. **`TOO_LONG_TO_SCAN` is its own label.** `injection_guard.scan()` answers `INJECTION_SUSPECTED`,
   `TOO_LONG_TO_SCAN` or nothing; a row and a chain carry `too_long_to_scan` apart from `injection_suspected`, and a
   long page is never called an injection. It still keeps a chain from supporting the native pattern (fail closed).
5. **The re-ask** is not run once the cost guard has tripped on the final turn (`over_reserve`); its answer is scanned
   by the auxiliary guard and recorded as an evidence row (`kind: structural_recheck`, stacked mode, the re-ask's
   pick and whether it diverged); a flagged or too-long re-ask cannot agree.
6. **Priya's vocabulary.** The hand-off's `observed_counts.source` is Priya's exact string —
   `first_party:search_console` / `first_party:bing_webmaster` (`agents/priya.py` `_gsc_metrics` / `_bing_metrics`,
   read on PR #50 at f759ebaa, not changed) — so every real Priya item was being rejected; the mirror now accepts
   exactly those strings. End-to-end test: a hand-off PRODUCED BY PRIYA'S OWN CODE (`priya.build` on synthetic Search
   Console and Bing rows, `led["handoff"]`, recorded with `priya.handoff_sha` in
   `tests/fixtures/priya/handoff_f759ebaa.json`) validates, hashes to the same canonical SHA-256, verifies against a
   record of Priya's `keyword_runs` shape, and flows through Naomi and Zara.

## Review round 7 (5df18c30: CONDITIONAL, one gap)

Zara accepted a payload claiming `agreed_clean = 3` and `INVISIBLE_THEN_RECOMMENDED` while also reporting three
complete chains flagged by the auxiliary guard or too long to scan, and did not validate `too_long_to_scan`. Now:
`injection_suspected` and `too_long_to_scan` must both be `{chains, complete}` of whole numbers ≥ 0 with
`complete ≤ chains ≤ chains of the summary` (Theo reports `too_long_to_scan.complete`), else THEO_PAYLOAD_INVALID;
and a payload is HELD when `agreed_clean > complete − injection_suspected.complete`
(`THEO_STRUCTURAL_AGREED_INCLUDES_FLAGGED`) or `agreed_clean > complete − too_long_to_scan.complete`
(`THEO_STRUCTURAL_AGREED_INCLUDES_TOO_LONG`). One test each; removing each guard makes its test fail (five mutants,
all killed).

