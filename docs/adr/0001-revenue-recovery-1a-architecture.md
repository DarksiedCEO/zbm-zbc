# ADR 0001 — Revenue Recovery 1A Architecture

**Status:** Accepted (Sep 21, 2026)
**Context:** First real build in the `zbm-zbc` monorepo. Founder decisions
locked in `revenue-recovery-founder-decisions.md`; this ADR records the
concrete engineering choices made to implement them.

## Decisions

1. **Monorepo**, not per-service repos. Founder-confirmed: simplest to
   manage solo, one version history, no cross-repo sync overhead.
2. **REST/JSON** between services, not gRPC. Founder-confirmed: pragmatic
   for where the build is today; a specific connection can move to a
   stricter contract later if it proves it needs one — not decided
   up front for a problem that doesn't exist yet.
3. **Language stack**: Python (statistical/detection logic), Go
   (orchestration/APIs), Rust (evidence ledger + future low-latency
   decision path), TypeScript (dashboard — not yet built). See founder
   decisions doc, Decision 6 (Revised) for full per-component reasoning.
4. **Shared fixture pool**, not per-agent fixtures. Founder-confirmed:
   matches real-world overlap (one order can have more than one leak),
   and is required to honestly test the Decision 3 anti-double-counting
   safeguard — that safeguard cannot be tested against isolated fixtures.
5. **Platform-agnostic schema.** `zbm_schema` has no Shopify/Amazon/TikTok-
   specific fields (founder correction, voice session Sep 21 2026: ZBM is
   not a Shopify-only business). A per-platform translation layer,
   converting a real store's native data into this generic shape, is not
   yet built — it's the boundary where real-store integration attaches
   later without touching agent logic.
6. **Confidence labeling and double-count correlation are architecturally
   enforced, not conventions.** `LabeledValue` cannot be constructed
   without both a `ValueClassification` and a `DecisionConfidence`
   (pydantic validation). Every `Finding` carries a mandatory `entity_id`
   correlation key; `zbm_schema.correlation.find_overlapping_entities`
   is the safeguard's real implementation, tested against a fixture
   order (`ord_1007`) deliberately built to trigger two agents at once.

## Scope boundary — Tier 3 excluded

The original roadmap (`revenue-recovery-roadmap.md`) lists agents I–L
(Failed-Payment/Dunning, Chargeback & Dispute, Gateway-Level Technical-
Failure, Post-Purchase Consolidation) under Tier 3, and gates them behind
"a separate Definition-of-Ready pass, own registry entry, evidence of
real client demand — before scoping." Noted inconsistency: I/J/K are
payment-processor/dunning-adjacent, the same category the roadmap's own
scope correction says Revenue Recovery explicitly is NOT ("marketing-leak
recovery, not payment/dunning recovery"). Tier 3 is excluded from "1A
complete" on this basis — not silently built around, and not silently
dropped either.

"1A complete" = Tier 1 (A–D) + Tier 2 (E–H) + the trust/safety net
(Trust Graduation Agent, Calibration Drift Agent, Hallucination Agent) +
orchestrator + ledger + a minimal dashboard. Action Execution Agent is
scoped but does not execute anything live — Decision 1's graduation path
(shadow mode → human-approval → autonomous) has not been entered by any
client yet, since there is no live client.

## Verified so far (Sep 21–22, 2026 build session)

- `services/detection-py` — schema, fixtures, agents A–D, correlation
  utility, REST API. 23/23 pytest passing, including live HTTP round-trip.
- `services/ledger-rust` — hash-chained append-only ledger core. 6/6
  `cargo test` passing, including two simulated-tampering detection tests.
- `services/orchestrator-go` — REST client + orchestration pipeline.
  4/4 `go test` passing against the real captured detection-py contract,
  plus a live end-to-end smoke test: real Python process + real Go
  process, real network calls, correctly surfaced the `ord_1007` overlap.
- `apps/dashboard-ts` — not yet built.

## Zero-value findings and agent errors (fix wave 3, Sep 24 2026 — AEGIS N6)

**Rule.** When the dollar value a finding would claim computes to `0.00`
after cent rounding, the agent emits **no finding**. Examples: two stacked
0% codes, two 10% codes on a $0.01 item (each rounds to 0.00), or a valid
order with no line items (subtotal 0.00) in the abandoned-cart, affiliate
or discount agents.

Why "no finding" rather than an UNCERTAIN finding without a value: each of
these agents' single job is a revenue leak, and a zero-dollar outcome is
not a leak — nothing was given away, nothing is at risk. Failure Mode #1
forbids a dollar figure without both labels, and `LabeledValue` is
positive-only, so `0.00` cannot be labeled; Failure Mode #3's UNCERTAIN is
for doubt about the *cause*, and there is no doubt here. Agents whose
findings never carry a value (cross-channel, platform integration) are
unaffected. Before this rule, the agent built a `0.00` `LabeledValue`,
pydantic raised inside the agent, and the whole request died with 500.

**Agent errors never become a 500.** `api.py` runs every agent through
`_run_agent`: each item (for cross-channel, each order's touchpoints) is
run on its own; a `ValueError` raised by the agent for an item (pydantic
`ValidationError` and `MoneyRangeError` are both `ValueError`s) is recorded
against that item and the rest still run. If any item failed, the answer is
`422` with one `{"type": "agent_value_error", "loc": ["body", <field>,
<index>], "msg": ...}` entry per failed item and **no findings** — never a
partial `200`, because the orchestrator records every returned finding in
the evidence ledger and a silently shortened list would be recorded as a
complete scan. `tests/test_zero_value_no_500.py` fuzzes all eight agents
over generated valid input (0%, 100%, stacked codes, $0.01 items,
maximum-bound amounts, empty collections) and asserts nothing raises.
