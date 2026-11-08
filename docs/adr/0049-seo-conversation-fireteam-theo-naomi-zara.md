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
Theo's logic (tested). An earlier answer longer than 4,000 characters is cut and the row flagged; an empty answer
mid-chain stops a native chain (`EMPTY_ANSWER_MID_CHAIN`): nothing is invented to fill the assistant turn.
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
"Beta is better than Acme" → Acme is never the pick). The first remaining entity in the first sentence holding a cue
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
  `observed` ⇒ counts, with `BELOW_THRESHOLD` ⇔ source `search_console` and < 20 impressions, `MEASURED_NOT_TIERED` ⇔
  source `bing_webmaster`; every count a whole number ≤ 10,000,000 (a sanity bound, not a plausibility test), clicks
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
  PR #50 records `ledger_sha256` and counts only, so today nothing is VERIFIED. **Until verified (HIGH 1c)** every
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
volume), PARTIAL (a member missing: its half `null` with the reason, never filled in), NOTHING_OBSERVED (no complete
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
never used.

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
| HIGH 1 forged hand-off (a `MEASURED_NOT_TIERED` 10^12 passed → STRONG → CLEARED/TEST) | every item type validated and bounded (§5); verification against Priya's record with the `priya.handoff_record/1` contract; unverified signals capped THIN; `HANDOFF_NOT_VERIFIED` blocking in Zara; the 10^12 forgery refused 422 and a 9,000,000 unverified count FLAGGED, not TEST (tests) |
| HIGH 2 the "THIN by construction" claim | corrected in naomi.py and here (§4, Limitations) |
| MED 3 negation / contrast | the clause-scoped negation rule (§3), ten table cases tested |
| MED 4 thresholds and strength | one confirmed-mention threshold at both ends; "named first in the final answer" apart; outcome, pattern and strength from the client's own outcome; the 1-of-5 case is MIXED / WEAK (tested); Zara re-derives them |
| MED 5 native multi-turn | adapter `history`, Anthropic and Gemini bodies (doc URLs above), stacked fallback named honestly, plan fixed before turn one, context-weighted reservations, caps fail closed |
| LOW 6 Garwood speed | exact to k = 100, Wilson–Hilferty with an outward margin above, outward-only proven against the exact band (no timing assertion) |
| LOW 7 bounded member wait | `SEO_FIRETEAM_MEMBER_TIMEOUT_SECONDS`, TIMED_OUT → PARTIAL, late payloads unused |

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
