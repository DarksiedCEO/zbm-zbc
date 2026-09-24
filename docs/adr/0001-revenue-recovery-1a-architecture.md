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

## Request limits (fix wave 1 round 4, Sep 24 2026)

Findings: detection-py had no body-size limit and parsed JSON on the event
loop (a 33 MB body held `/health` for 3.2 s and was then run as a 33 MB
batch); orchestrator-go served with `http.ListenAndServe`, i.e. no
timeouts. The sweep found more of the same class: uvicorn's default
httptools parser accepted a 20 MB request header; uvicorn has no
request-head timeout (a slowloris connection stayed open indefinitely);
orchestrator-go read every upstream response with an unbounded
`io.ReadAll`, accepted 10 MB of upstream response headers, and copied
whole upstream bodies into its log lines.

**detection-py** (`src/api.py`, `src/serve.py`):

| Limit | Value | Answer | Why this size |
|---|---|---|---|
| Items per request list (orders, subscriptions, events, touchpoints, statuses, terms, findings) | 1000 | 422 `too_long` | Same batch cap as fulfillment-py. The orchestrator sends the fixture pools (≤ 7 items) and ≤ ~10 findings. |
| Request body | 2 MiB | 413, before any parsing | The largest accepted batch: 1000 orders × ~0.5–1 KiB (the fixture pool's largest order is 558 bytes of JSON; a 1000-order batch of it is 495 KB) or 1000 findings × ~650 bytes. 2 MiB is that with ≥ 2× headroom. Checked from `Content-Length` without reading the body, and as a running total for a chunked body. |
| Body delivery | 30 s | 408 | A 2 MiB body on any working link takes well under a second. |
| Request line + headers | 16 KiB | 400 (h11 parser, while reading); 431 (middleware, any launcher) | Callers send a few short headers. |
| Request head delivery | 10 s | connection closed | uvicorn has none; `serve.py` adds it (`_HeadDeadlineH11Protocol`). |

JSON parsing runs in the threadpool (`run_in_threadpool`) and every route
handler is a plain `def` (FastAPI runs those in the threadpool), so neither
parsing nor agent execution runs on the event loop. The supported launcher
is now `cd src && python3 serve.py --port 8000`: it pins uvicorn's h11
parser with the head-size cap and adds the head deadline. Running
`python3 -m uvicorn api:app` still enforces every body limit, but the head
size and head deadline then depend on uvicorn's defaults (unbounded with
httptools).

**orchestrator-go** (`cmd/orchestrator/main.go`, `internal/client/client.go`):

| Limit | Value | Why |
|---|---|---|
| `ReadHeaderTimeout` | 5 s | |
| `ReadTimeout` | 15 s | whole request, body included |
| Handler budget (context deadline) | 60 s | A full fixture scan (15 detection calls, 10 ledger appends, 2 verifies) measured 25–100 ms on the live 3-process stack. |
| `WriteTimeout` | 75 s | must exceed the handler budget, so a scan that used it all can still send its 502 |
| `IdleTimeout` | 60 s | |
| `MaxHeaderBytes` | 16 KiB (431 above it; Go adds 4 KiB slack) | |
| Request body | 64 KiB (`MaxBytesReader`; 413) | No route takes a body. Any body is read and discarded **before** routing, so a scan never starts until its whole request has arrived (a slow body gets 408 at `ReadTimeout`). |
| Upstream call | 10 s total (unchanged), dial 5 s, response headers 10 s | |
| Upstream response headers | 64 KiB | |
| detection-py response body | 8 MiB | its largest response is ~1 MiB (one finding per item, ≤ 1000 items) |
| ledger-rust response body | 64 MiB | `GET /ledger/entries` returns the whole ledger (no pagination); a finding entry is ~470–500 bytes, so this is ~130,000 entries (~13,000 fixture scans). Beyond that, reads fail closed (502; the log says "response body exceeds"). **Known limit until the ledger paginates.** |
| Upstream text in errors/logs | 2 KiB | |

Tests: `services/detection-py/tests/test_request_limits.py`,
`tests/test_request_limits_live.py` (real uvicorn on a real socket: `/health`
stays under 1 s while a 1000-order batch and ~33 MB bodies are in flight;
413 from `Content-Length` and while streaming chunked data; head cap; head
deadline); `services/orchestrator-go/cmd/orchestrator/server_limits_test.go`
(the real binary on a real socket: slow-header and slow-body clients are
cut off at 5 s / 15 s while `/health` keeps answering and no scan starts;
413 and 431), `internal/client/limits_test.go`.

**Clients that send a whole oversized body before reading the reply** may
see a connection reset instead of the 413: both servers answer and close
without reading the rest of the body, which is the point. `curl` (which
reads while sending) sees the 413.
