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

**Context mode `user_turns_only`.** The adapter is single-question and stateless (ADR 0046 A1). Turn k is ONE request
carrying the chain's earlier USER questions as a numbered list, then the new one. The engine's own earlier answers are
NOT sent back. A consumer chat app keeps them in context, so these chains approximate a conversation; they are not a
replay of one (stated in every envelope's limitations). Native multi-turn (each engine's message array) is not built.

### 3. Theo — what a turn and a chain record

Per answered turn (`theo.observe`; text scanned in memory, dropped): client mentioned (agents/brand_mention.py, with
its HIGH/LOW confidence) and cited (the structured citation's HOST is the client's, agents/domain_match.py: exact,
www. twin or subdomain; a lookalike never; a redirector host makes it `null` = UNKNOWN — Theo does not resolve URLs),
the client's rank among named entities, each competitor term (`competitor_terms` in the request) mentioned, the
competitor domains (the prompt set's) cited, model, version, provider time, answer SHA-256, and a ≤160-character
PII-screened excerpt (Callum's rule) — withheld for a Priya topic (`PRIYA_QUERY_CONTEXT`). The final recommendation
(`theo.recommendation`, complete chains only) is LEXICAL: the entity named first in the first sentence of the last
answer holding a cue (`recommend`, `best choice`, `top pick`, `go with`, …), else the entity named first, else
`none`; confidence MEDIUM at most (a cue sentence with a confirmed mention), LOW otherwise. Chain status: COMPLETE,
INCOMPLETE (`ENGINE_FAILED_AT_OPENING` / `ENGINE_FAILED_MID_CHAIN`, `REFUSED_*`, `BUDGET_EXHAUSTED_MID_CHAIN`),
LOOP_STOPPED (`ANSWER_REPEATED`: an answer identical to an earlier one of the same chain), NOT_ASKED (cap, budget,
stop code, personal data). **An incomplete chain's final recommendation is `unknown`**, never the last answered turn.
Per topic × engine (`theo.conversation_chains/1`): chains, complete, per-turn client-mentioned / cited /
competitor-mentioned rates (k, n, Wilson 95%, directional under 3), the final-recommendation distribution with
`unknown`, `client_recommended`, `unstable` (complete chains disagree), `pattern` (INVISIBLE_THEN_RECOMMENDED,
VISIBLE_AND_RECOMMENDED, VISIBLE_THEN_NOT_RECOMMENDED, NOT_RECOMMENDED, INCONCLUSIVE) and `strength` (STRONG: ≥3
complete chains and a modal winner in ≥2/3; WEAK; NONE). Run rules are Callum's (`collect`): a metered port only
inside a recorded reservation, the stop codes, two protocol errors in a row stop the engine, the 4x cost guard
(over the reserve mid-chain → INCOMPLETE), personal data never sent, a crashing adapter counted, never raised.

### 4. Naomi — the estimator and its limits

No engine publishes prompt volume and no paid panel is used, so Naomi estimates a **directional proxy** from owned
signals and says so on every topic (`proxy_for`, flag `PROXY_NOT_PROMPT_VOLUME`, unit `impressions_on_this_property`):

| Signal | State here |
|---|---|
| the topic's Priya EMERGING item (`observed_counts`: this property's impressions for that exact query, Search Console or Bing, as Priya measured them through the existing GSC client data contract / her Bing adapter) | read when the caller passes the hand-off; **not verified against a Priya record** (Priya stores only a hash) |
| the latest completed audit's Search Console `search_performance.by_query_class.classes.question` | context only: every question-form query together, never a topic's estimate |
| Bing Webmaster Tools directly | NOT_CONNECTED (Priya's adapter is not on this base) |
| any paid panel, clickstream or "AI prompt volume" vendor | not used (owner rule) |

Per topic (`naomi-estimator/2026-11-08.1`): no item, a derived item, an unmeasured item or a refused topic →
`UNKNOWN` (band `null`, reliability NONE, confidence NONE, a named reason) — never 0. An observed item → one signal per
source (never summed: different windows), the count k, its window, and the exact **Garwood 95% Poisson band** on k
(lower χ²(0.025; 2k)/2, upper χ²(0.975; 2k+2)/2, by bisection on the Poisson CDF up to k = 1,000, Wilson–Hilferty with
an outward margin above, rounded outward to 0.01; checked against published values for k = 0, 1, 10). Reliability
THIN < 20 (Priya's threshold), MODERATE < 1,000, STRONG ≥ 1,000; confidence LOW for THIN, else MEDIUM — **never HIGH**
(outside Naomi's vocabulary; Zara holds any payload that says HIGH). No estimate carries a bare `value`: the count is a
measured input inside its signal, the estimate is the band. Because the hand-off carries only EMERGING items, a signal
from it is THIN by construction.

### 5. The hand-off contracts

- **Priya → Theo / Naomi**: `priya-emerging-handoff/1`, passed as `priya_handoff` in the run request (Priya's ledger
  is not stored, ADR 0047 §3). `agents/priya_handoff.py` mirrors Priya's validator field for field (PR #50 at
  15917fc8; to be replaced by `priya.validate_handoff` when both land) and adds consumer rules: the document's tenant
  must be the run's (`403 HANDOFF_TENANT_MISMATCH`, refused whole); at most 50 items; an observed item must carry
  counts; a `BELOW_THRESHOLD` item with ≥ 20 impressions or clicks above impressions contradicts itself; any problem
  → `422 HANDOFF_INVALID`, nothing recorded. Query text is data (`untrusted`) and never reaches the log or the ledger:
  the request record keeps the hand-off's SHA-256 and item count; Priya topics are reported by key (`text: null`).
- **Theo → Zara**: per topic, the list of `theo.conversation_chains/1` chain summaries (one per engine), emitted as
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
(Theo: complete ≤ chains, the final distribution adds up, the pattern agrees with turn one and the winners, STRONG
needs ≥3 complete chains; Naomi: UNKNOWN has no band or signal, the band holds the count, reliability matches the
count, confidence matches reliability); the member contract names this tenant, this run and its own contract id;
no unplanned or duplicate topic payload. Conflicts: `STRONG_CHAIN_THIN_VOLUME`, `STRONG_CHAIN_UNKNOWN_VOLUME`,
`VOLUME_WITHOUT_CHAIN`, `UNSTABLE_CHAIN`, `ENGINES_DISAGREE`. Verdicts: CLEARED, FLAGGED (passes, decision
INSUFFICIENT_EVIDENCE, `priority_basis` `chain_only` when volume is thin or unknown — a consumer must not rank by
volume), PARTIAL (a member missing: its half `null` with the reason, never filled in), NOTHING_OBSERVED (no complete
chain and no volume: not "cleared", nothing passes), HELD (invalid, contradictory, foreign or duplicate: nothing passes).
Decisions never ACT; TEST only for CLEARED with a STRONG chain and MODERATE+ volume.

**Failure isolation.** A member that raises is closed FAILED (exception type only) and the run completes with the
other member and Zara (PARTIAL). A restricted Theo or Naomi does not run (RESTRICTED). A restricted or retired
**Zara refuses the whole run** (403 `AGENT_RESTRICTED`): nothing may move downstream unchecked. A kill switch
(global, write, the tenant, `capability:ai_probe`, `capability:audit`) engaged mid-run stops it at the next step; the
run is recorded `interrupted` with the switch's code and every result discarded; `provider:<engine>` stops that
engine. No new kill switch, so the switch view is unchanged.

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
- Native multi-turn context (the engine's own answers sent back) — needs each engine's message-array support in the
  adapter and a decision on re-sending untrusted answer text to providers.
- LLM-generated follow-ups (`SEO_THEO_LLM_FOLLOWUPS` refuses start).
- Cited-URL resolution in Theo (Callum's resolver is not reused: Theo reports `cited: null` on a redirector).
- Verification of the Priya hand-off against a Priya record (Priya stores only a ledger hash); the mirror validator is
  replaced by Priya's own once PR #50 lands.
- A direct Bing Webmaster read, any prompt-volume vendor or panel (owner rule), Google Ads Keyword Planner.
- A consumer of `zara.conversation_volume/1` or `zara.priya_feedback/1`; scheduling; drift between runs; dashboard
  pages; client-report section; war-room scenarios.
- The maintenance pins and the seo image are not re-released (see "Release").

## Limitations (honest)

- Lexical matching (brand_mention) and a lexical recommendation rule: they do not understand the answer.
- `user_turns_only` chains are an approximation of a conversation; templates are not what real users ask.
- Naomi's proxy is search demand on one property, thin by construction from the hand-off; it says nothing about how
  often anyone asks an AI engine.
- One account, one time, N samples: observations, not search truth; nothing calibrated against traffic or revenue.
- The engine scorecard on Callum's card (`engine_runs`) counts every recorded engine run, Theo's included.

## Founder questions

1. Set `SEO_THEO_RUN_MAX_CALLS` (and each engine's per-run cap) for the first fixture-free run, or keep OFF?
2. Native multi-turn: may the engines' own earlier answers be sent back to the providers (closer to real chats; more
   tokens; untrusted text re-sent)?
3. Should Priya hand ESTABLISHED queries to Naomi too (today only EMERGING items travel, so every signal is THIN)?
4. Is the lexical final-recommendation rule acceptable for reporting, or should a recommendation be shown only as
   "named first in the final answer"?
5. Should Priya store the hand-off's SHA-256 so Naomi can verify what the caller passes?

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
conversation run, restart and the interrupted-runs job, the department registry). Counts: `docs/test-counts.md`.

## Release

seo-py source changed, so the Gate 5 maintenance pins and the seo image must be re-released after Gate 5; until then
the hygiene-static cold-pin check is red for that expected reason. The pins are not edited here (`deploy/gate5/**`,
`deploy/docker/maintenance/**`, `.railway/**` and `.github/workflows/**` are untouched).
