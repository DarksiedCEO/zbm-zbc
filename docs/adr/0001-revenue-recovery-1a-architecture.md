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
| Every field of every request model (`src/zbm_schema/limits.py`) | ids 64 chars; finding_id/entity_id 128; labels 32; agent_id, SKU, discount code 64; cause_description 1024; line items per order 50; discounts per order 10; every int bounded | 422 naming the field | See "Body limit sizing" below. |
| Request body, per route | orders routes 36 MiB; `/correlation/overlaps` 11 MiB; subscriptions, contract terms 2 MiB; events, touchpoints, platform statuses 1 MiB; any path without a body 64 KiB | 413, before any parsing | The computed worst case of the route's largest legal batch + 25%, rounded up to a MiB (below). Checked from `Content-Length` without reading the body, and as a running total for a chunked body. |
| Large requests in progress | 1 (a body declared > 256 KiB, or chunked) | 503 + `Retry-After: 1`, before the body is read | Parsing, agents and serialization hold the GIL; a second large batch only delays the event loop. Smaller requests (every orchestrator scan) are never capped. The slot is held from the request head to the end of the response. A request without a valid token is answered 401 before its body is read, so it holds the slot only for that (checked on a real socket); a caller with a valid token that sends its body slowly holds it for at most the 30 s body deadline. |
| Body delivery | 30 s | 408 | A 36 MiB body on a 100 Mbit/s link takes ~3 s. |
| Request line + headers | 16 KiB | 400 (h11 parser, while reading); 431 (middleware, any launcher) | Callers send a few short headers. |
| Request head delivery | 10 s | connection closed | uvicorn has none; `serve.py` adds it (`_HeadDeadlineH11Protocol`). |

**Body limit sizing (fix wave 1, LOW-C).** The flat 2 MiB limit was sized
from a typical order, so 1,000 orders × 30 line items (2.12 MiB) — a batch
the API advertised — got 413. It could not have been sized from the worst
case: no field had a limit, so a legal batch had no maximum size. Of the two
ways out (a body limit from the true worst case, or a per-order line-item
cap small enough that 1,000 orders fit 2 MiB), only the first is consistent
with this ADR's rule that the limit is the largest accepted batch plus
headroom: at the worst-case encoding below, 2 MiB holds 1,000 orders only
with no line items at all. So every field got a limit and the body limit is
computed from them: `src/request_limits.py` walks each route's request model
and bounds its compact JSON size with every field at its limit — a string of
N characters as 2 + 6N bytes (the most one character takes in minimal JSON
escaping, `\u00XX`, and in Go's encoder, which also escapes `<>&`), money
20, datetime 37 (RFC 3339 with 9 fractional digits and an offset), float 24,
ints by their bound, every list full, every optional present. Results:
1,000 worst-case orders 28.3 MiB (29,713 bytes per order, 50 line items of
~460 bytes), 1,000 findings 8.6 MiB, 1,000 contract terms 1.07 MiB,
subscriptions 0.98 MiB, events/touchpoints/statuses ~0.66 MiB. The limit is
that × 1.25, rounded up to a MiB (the headroom covers encoders that are not
compact or escape astral characters as 12-byte surrogate pairs). Not
counted, because no finite limit could admit them: insignificant whitespace,
zero-padded numbers, over-long fractional seconds. A realistic ASCII batch is
~5× smaller than the worst case (1,000 orders × 30 line items ≈ 2.6 MiB).
`tests/test_body_limits.py` builds the worst-case legal batch of every route
(max-length strings of characters JSON must escape), checks it is within 5%
of the computed bound, is accepted (200) and that one byte over the limit is
413; the limits are not hardcoded but recomputed at import, and a request
model with an unbounded field fails to load.

JSON parsing runs in the threadpool (`run_in_threadpool`), every agent
route handler is a plain `def` (FastAPI runs those in the threadpool), and
the response JSON is built in that thread too (FastAPI serialized returned
models on the event loop), so neither parsing, agent execution nor response
serialization runs on the event loop. `/health` is `async def`: answered on
the event loop, never queued behind the threadpool. **Bound:** with 16
clients sending ~28 MiB worst-case batches at once, `/health` answered in
p50 6–17 ms, max 0.08–0.22 s (8 runs, 2-CPU host, the 16 clients on the same
host); `tests/test_request_limits_live.py` asserts max < 0.5 s. Before this
fix, 16 concurrent 1,000-order batches (0.61 MiB each) held `/health` at p50
0.89 s, max 1.08 s; after it, the same load gives p50 6 ms, max 47 ms, with
the same batch throughput (one run each). **Fix wave 23:** on the w23
2-CPU box that bound failed (max 0.50–0.63 s at 5ba1eb6; 4/6 at 540a64e).
It measured the server (time to first byte, prober in its own process) and
the event loop was never blocked for more than 0.13 s: the time was the GIL
convoy — `/health` re-acquires the GIL after every syscall and waited up to
a 5 ms switch slice each time behind the ~0.5 s parse. `serve.py` now sets
`sys.setswitchinterval(0.001)` (`DETECTION_SWITCH_INTERVAL_SECONDS`), as the
other `serve.py` launchers have since fix wave 7 (NEW-5). The bound is
unchanged. **Fix wave 24 (AEGIS round 23 N23-S-3/S-4):** the override is
accepted only in 0.0001–0.05 s and `serve.py` refuses to start unless the
interval in force (`sys.getswitchinterval()` after setting it) is the one
set (**fix wave 25, AEGIS round 24 N24-S-6:** compared in whole microseconds,
`round(getswitchinterval() × 1e6)` in [100, 50 000] and within the microsecond
CPython truncates — CPython keeps the interval as an integer number of
microseconds and read 0.0001 back as 9.999999999999999e-05, so the float check
refused the range's own lower end; `serve.py` now prints the interval in force
at start, and `tests/test_fix23_switch_interval.py` starts the real launcher at
0.0001 and 0.05); and the numbers, now the same in `serve.py`, `api.py` and here, with
their conditions — measured on this 2-CPU box, Python 3.13.13, the live test
above (16 clients, ~28 MiB worst-case batches, `/health` time to first byte
from a prober in its own process), 5 runs each: 1 ms with no other load —
p50 11–17 ms, max 0.10–0.16 s; 1 ms with three busy loops — p50 6–8 ms, max
0.21–0.27 s; 5 ms with three busy loops — p50 11–14 ms, max 0.23–0.45 s. The
wave-23 notes' 0.09–0.10 s (`serve.py`) and 0.11–0.15 s (`api.py`, here) were
single sessions under unstated load; AEGIS round 23 measured, with three busy
loops, 1 ms max 0.11–0.17 s and 5 ms 0.12–0.24 s. The earlier "max 0.08–0.22 s
(8 runs)" above is the wave-1 measurement at the 5 ms default.

Largest detection-py *responses* at these limits: 5.3 MiB (discount-misuse on
1,000 worst-case orders) and 8.6 MiB (`/correlation/overlaps` echoing 1,000
worst-case findings). The latter is above orchestrator-go's 8 MiB cap on
detection-py responses below; the orchestrator sends ≤ ~10 findings, so this
is not reachable from a scan today, but the two limits disagree. The supported launcher
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
| detection-py response body | 8 MiB | its largest response was ~1 MiB (one finding per item, ≤ 1000 items); since fix wave 1 LOW-C it can reach 8.6 MiB for a 1,000-finding correlation call (see detection-py above) |
| ledger-rust response body | 64 MiB | `GET /ledger/entries` returns the whole ledger (no pagination); a finding entry is ~470–500 bytes, so this is ~130,000 entries (~13,000 fixture scans). Beyond that, reads fail closed (502; the log says "response body exceeds"). **Known limit until the ledger paginates.** |
| Upstream text in errors/logs | 2 KiB | |

**dashboard-ts** (fix wave 1, LOW-A). The page rendered an orchestrator
failure with HTTP 200, so a monitor saw a healthy dashboard, and the
orchestrator call had no timeout. Now the call is bounded by
`ORCHESTRATOR_TIMEOUT_MS` (default 10 s) and the status on the wire is
503 when the dashboard cannot get an answer (orchestrator unreachable,
timed out, `ORCHESTRATOR_SERVICE_TOKEN` unset), 502 when the orchestrator
answered but not with usable findings (token rejected, any other non-2xx,
a body that is not the contract, or a ledger that does not verify), and
200 only when findings loaded and the ledger verified. Mechanism: an App
Router page component cannot set a 5xx status (only `notFound()`,
`forbidden()`, `unauthorized()`, `redirect()`, or a throw, which is a 500
with the message replaced by a digest), so `src/proxy.ts` reads the
findings once per `GET /`, decides the status, and answers
`NextResponse.next({ status })` while handing the very same outcome to the
page through an overridden request header (`src/lib/handoff.ts`; a
client-supplied copy of that header is dropped on every request). The
page renders exactly that outcome, so the status and the message can never
disagree. `GET /healthz` returns the same verdict as JSON for monitoring.
Verified with curl against the built server; `tests/status.live.test.mjs`
checks every case on the wire, `tests/load-outcome.test.ts` the mapping.
The handoff header lives in process memory only (a 50,000-finding ledger,
19 MB of JSON, passed through it in a manual run); the page itself, which
renders the whole ledger, is the practical size limit.

Tests: `services/detection-py/tests/test_request_limits.py`,
`tests/test_request_limits_live.py` (real uvicorn on a real socket: `/health`
stays under 1 s while a 1000-order batch and ~44 MB bodies are in flight, and
under 0.5 s with 16 concurrent worst-case batches; a stalled large request
without a valid token is answered 401 and frees the heavy slot at once;
413 from `Content-Length` and while streaming chunked data; head cap; head
deadline); `services/orchestrator-go/cmd/orchestrator/server_limits_test.go`
(the real binary on a real socket: slow-header and slow-body clients are
cut off at 5 s / 15 s while `/health` keeps answering and no scan starts;
413 and 431), `internal/client/limits_test.go`.

**Clients that send a whole oversized body before reading the reply** may
see a connection reset instead of the 413: both servers answer and close
without reading the rest of the body, which is the point. `curl` (which
reads while sending) sees the 413.
