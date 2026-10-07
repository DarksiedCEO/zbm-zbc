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
| Items per request list (orders, subscriptions, events, touchpoints, statuses, terms, findings) | 1000 | 422 `too_long` | Same batch cap as fulfillment-py. Since Oct 6 2026 (E-6) the orchestrator batches: detect calls of at most 500 items, correlation calls of at most 700 findings, never splitting one entity's findings or one order's touchpoints. |
| Every field of every request model (`src/zbm_schema/limits.py`) | ids 1-64 chars of `[A-Za-z0-9._:/@+#-]`, first alphanumeric; client_id 1-64 of `[A-Za-z0-9._:-]`; finding_id `rrf1-` + 40 hex; entity_id 64; period_label 16; agent_id 32; methodology_id 24; methodology 600; labels 1-32 (status and platform normalized slugs); SKU, discount code 64; cause_description 1024; line items per order 50; discounts per order 10; every int bounded | 422 naming the field | See "Body limit sizing" below, and "Revenue Recovery fix wave" for the Oct 6 2026 changes. |
| Request body, per route | orders routes 35 MiB; `/correlation/overlaps` 13 MiB; subscriptions, events, touchpoints, platform statuses, contract terms 1 MiB; any path without a body 64 KiB | 413, before any parsing | The computed worst case of the route's largest legal batch + 25%, rounded up to a MiB (below). Checked from `Content-Length` without reading the body, and as a running total for a chunked body. Oct 6 2026: identifier fields are ASCII-safe, so their worst case is 1 byte per character (orders 36 -> 35 MiB); findings carry more fields (11 -> 13 MiB). |
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
0.0001 and 0.05 — since the wave-25 E-A review it trusts an answer on the port only
after that child has logged its own bind, as a port picked free a moment earlier can be
another process's; the same range and in-force check now also guard creative-py's and
fulfillment-py's launchers, which set no interval before, and onboarding-py's and
compliance-py's, which accepted any positive value); and the numbers, now the same in `serve.py`, `api.py` and here, with
their conditions — measured on this 2-CPU box, Python 3.13.13, the live test
above (16 clients, ~28 MiB worst-case batches, `/health` time to first byte
from a prober in its own process), 5 runs each: 1 ms with no other load —
p50 11–17 ms, max 0.10–0.16 s; 1 ms with three busy loops — p50 6–8 ms, max
0.21–0.27 s; 5 ms with three busy loops — p50 11–14 ms, max 0.23–0.45 s. The
wave-23 notes' 0.09–0.10 s (`serve.py`) and 0.11–0.15 s (`api.py`, here) were
single sessions under unstated load; AEGIS round 23 measured, with three busy
loops, 1 ms max 0.11–0.17 s and 5 ms 0.12–0.24 s. The earlier "max 0.08–0.22 s
(8 runs)" above is the wave-1 measurement at the 5 ms default.

**Fix wave 25 (E-A; scout A D2, D3, D4; FIX_WAVE_23b item 2 "find what the test measures"; R-HYGIENE L1/L2).** The
two `/health` bounds in `tests/test_request_limits_live.py` were measured by a thread of the pytest process that also
ran the 16 sender threads, as plain wall time. The prober is now its own process, and each probe reports its wall time
next to the time the kernel kept the prober, and the server's event-loop thread, runnable but not running
(`/proc/<pid>/task/<tid>/schedstat`, field 2). The bounds (1 s, 0.5 s) are unchanged and apply to the wall time minus
the LARGER of those two waits (the two can overlap; subtracting their sum would credit an overlap twice): the time
the server had the CPU and still had not answered, its GIL waits behind the parse included. Where schedstat does
not exist (macOS) the waits are 0 — plain wall time, as before. The 16-batch test starts measuring once a batch has
been answered, not after a fixed 0.5 s. The live server is accepted only after it has logged its own bind on the port
(an answer on a port picked free a moment earlier can be another process's), and ports are OS-assigned unless
`DETECTION_LIVE_TEST_PORTS` is set (the literal default 19960-19969 is gone). `test_oversized_content_length_is_
refused_before_the_body_is_sent` no longer bounds the wall clock: no body byte is ever sent, so a 413 at all is the
refusal before the body. Settings (scout A D10): `DETECTION_DRAINS_MAX` (default 512) caps how many graceful-close
drains run at once (`src/serve.py`, the shared module of ADR 0003); `DETECTION_LIVE_TEST_PORTS` (`lo-hi`, test-only)
pins the live tests to a port range — unset, the OS assigns ports. Measured (E-A, Oct 2 01:00-01:02Z, head e665109, this 2-CPU box, Python 3.13.13,
exactly 2 busy loops, 5 runs of the two `/health` tests, `w25-reports/E-A/logs/detload-e665109/`): 5/5 passed; beside
large and oversized bodies the max server-side latency was 27-94 ms (wall 29-100 ms; bound 1 s); under 16 concurrent
worst-case batches p50 3-4 ms, max 163-217 ms (wall max 169-241 ms; bound 0.5 s; largest run-queue waits subtracted:
prober 10 ms, server loop 43 ms). The test prints every raw number.

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
| detection-py response body | 8 MiB | its largest response was ~1 MiB (one finding per item, ≤ 1000 items); since fix wave 1 LOW-C it can reach 8.6 MiB for a 1,000-finding correlation call (see detection-py above). Since Oct 6 2026 the orchestrator sends at most 700 findings per correlation call: ~7.1 MiB at the worst case of every field, under the cap (a real finding is ~1.3 KB). |
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

## Revenue Recovery fix wave (Oct 6 2026) — finding identity, tenant, evidence class, scan records

Amends Decision 6 and the sections above. Source: the backend bug sweep of
integration 5d49ee9 (findings E-1 to E-15, probes in `scratchpad/sweep-E`).
Every probe that reproduced is now a regression test that fails on 5d49ee9:
`services/detection-py/tests/test_revrec_fix_wave.py` and
`services/orchestrator-go/cmd/orchestrator/revrec_fix_wave_test.go`. How a
scan is written to the ledger is ADR 0003 section 13.

### Finding identity and the correlation key (E-3, E-10)

- **Tenant.** Every detect request names `client_id`, the ZBM client whose
  data it is (required; 1-64 characters of `[A-Za-z0-9._:-]`, so it can be a
  ledger `subject_id`). Every `Finding` carries it. A contract term or
  platform status whose own `client_id` is another client's is a 422 for
  that item, never a finding under the wrong tenant.
- **finding_id** = `"rrf1-"` + the first 40 hex digits of
  SHA-256(`"rrf1\n" + client_id + "\n" + agent_id + "\n" + entity_type + "\n"
  + entity_id + "\n" + period_label`), period empty when absent. No field's
  charset allows a newline, so the preimage is unambiguous; the old ids were
  concatenations and collided (two minimum-spend terms of one client and
  period; client "a-b" + platform "c" vs client "a" + platform "b-c").
  `Finding` validates that its id is derived; orchestrator-go recomputes it
  for every finding it receives and every record it reads back. `period_label`
  is part of the identity: a contract term's billing period, a missed
  renewal's due date (UTC), absent for one-off entities.
- **entity_type** is one of `order`, `subscription`, `contract_term`,
  `platform`; a platform finding's entity is the platform itself (the client
  is `client_id`).
- **Correlation key** = (client_id, entity_type, entity_id), as
  `"client|entity_type|entity_id"` (`|` is in none of the charsets). An
  overlap needs **more than one distinct agent**: the same agent reporting
  one entity twice is not double-counting (E-10). Duplicate input rows give
  one finding; two different rows sharing an identifier are a 422
  (`duplicate_entity`) rather than one silently winning.
- **Fixture tenant.** The fixture pool is one store, tenant `fixture-pool`
  (`fixtures_loader.FIXTURE_CLIENT_ID`); its tier 2 rows now carry that
  client id. orchestrator-go defaults a scan with no `client_id` to it, and
  only to it, and records `tenant_defaulted`. Any other tenant is refused
  (422) until a live data source exists: fixture data is never attributed to
  a real client.

### Evidence class and methodology (E-4)

Every finding carries `evidence_class` and a `methodology_id` + `methodology`
note. `OBSERVED`: read off recorded transactions/terms with exact arithmetic.
`ESTIMATED`: observed inputs plus a stated assumption. `MODELED`: from a
statistical/attribution model (no agent emits it yet). `UNKNOWN`: no
defensible dollar figure — required exactly when `recoverable_value` is null,
and then the text states no dollar figure either (Hallucination Agent rule).

Every agent was reviewed for the overstatement the sweep found in three of
them. What each one claims now, and what it claimed before (the shared
fixture pool, one scan):

| Agent | Before (5d49ee9) | Now | Evidence | Why |
|---|---|---|---|---|
| discount-misuse | whole stacked discount: ord_1003 $128.00, ord_1007 $54.38 (OBSERVED/VERY_HIGH) | the excess over the most valuable single code: $48.00, $16.88 (OBSERVED/HIGH) | OBSERVED | The best single code was the customer's to use; only what the stack gave beyond it breaches the one-code policy. Each code is assumed individually valid (the customer-favourable reading), so the figure is a floor. Confidence HIGH, not VERY_HIGH: the one-code policy is the agent's assumption, not read from the store's promotion rules. |
| affiliate-coupon-extension | whole order subtotal: ord_1002 $120.00, ord_1007 $150.00 (ATTRIBUTED/HIGH) | subtotal x the affiliate's `commission_rate_percent` when known: ord_1002 $12.00 (10%, ATTRIBUTED/MEDIUM); no figure without a rate (ord_1007) | ESTIMATED / UNKNOWN | What leaks is the commission paid on the order, not the order. It is ESTIMATED because the payout itself is not in the data. A negative click-to-order gap (order before click, E-14) is now an UNCERTAIN finding with no figure; it used to pass as "within the window". |
| server-side-attribution | whole order value: ord_3001 $210.00 (OBSERVED/HIGH) | no figure | UNKNOWN | The order was real and paid; the gap is attribution visibility, not lost revenue (the agent's own docstring said so). |
| abandoned-cart-coverage | whole cart value: ord_1005 $89.99 (INCREMENTAL/MEDIUM) | no figure | UNKNOWN | Only the share a recovery flow wins back is recoverable, and no store-measured recovery rate exists in the data. The cart value is the ceiling of the opportunity, not a recoverable amount. A measured rate is the input that would allow an ESTIMATED figure. |
| renewal-never-triggered | one cycle at plan price: sub_2001 $39.00 (OBSERVED/HIGH) | unchanged amount; labels INCREMENTAL/MEDIUM since Oct 7 2026 (AEGIS M2, below — they stayed OBSERVED/HIGH in 8bdebde) | ESTIMATED | The charge that should have been attempted, not inflated; ESTIMATED because whether it would have succeeded is not in the data. A renewal now counts only if due at or before the scan's `as_of` (E-13). |
| contract-pricing-term-drift | contracted minimum minus billed: $900.00 (OBSERVED/VERY_HIGH) | unchanged | OBSERVED | Exact arithmetic on the contract and the invoice. |
| cross-channel-attribution | no figure | no figure | UNKNOWN | Needs a multi-touch model (not built). |
| platform-integration | no figure | no figure | UNKNOWN | A coverage gap, not a transaction. |

Fixture pool total of claimed dollars: $1,691.37 before, $1,015.88 now; of
which contract drift is $900.00 in both. Without it: $791.37 before, $115.88
now.

### Other input rules (E-11, E-12, E-13)

- `Order.status` and `PlatformConnectionStatus.platform` are normalized
  (trimmed, lowercased, whitespace and `-` runs to `_`; raw text at most 32
  characters): `"Abandoned_Cart"` is `abandoned_cart` instead of a silent
  miss. `Subscription.status` is normalized the same way before its enum.
- Identifiers are never empty and use the safe charset above (an empty
  `order_id` made every finding collide on entity `""`).
- `placed_at`, `last_renewal_at`, `next_renewal_due_at`, `Customer.created_at`
  and the renewal route's `as_of` must be timezone-aware. The renewal route
  requires `as_of` (orchestrator-go passes the scan's instant; `?as_of=` on
  the scan route overrides it, RFC 3339 with an offset), and a lapsed
  subscription counts only if its renewal was due at or before it.

### Scans (E-1, E-2, E-6, E-8)

- A store with no leaks is a 200 with `findings: []`: lists are never sent
  as JSON `null`, and the correlation call is skipped when nothing can
  overlap (it used to make every clean-store scan a 502).
- `POST /revenue-recovery/scan[?client_id=][&as_of=]` — one scan at a time
  per orchestrator process; a concurrent request is 409 with `Retry-After`
  and runs nothing. (One process per deployment, like the ledger: the lock
  is in-process. Two orchestrators would still each record only complete,
  separately identified scans.) Every finding is checked before anything is
  written (tenant, agent, derived id, field bounds); every scan has a
  `scan_id` and is recorded as started -> findings -> completed ledger events
  (ADR 0003 section 13). `GET /revenue-recovery/findings` counts only
  completed scans and lists the others under `excluded_scans` with a reason;
  each row carries `scan_id`, `client_id`, `evidence_class`,
  `present_in_latest_scan`; pre-fix-wave ledger finding entries are counted
  as `legacy_finding_entries_ignored`, never shown.
- Detect calls are batched (500 items) and correlation is batched by key
  (700 findings), so a store with more than 1,000 orders or findings no
  longer fails the whole scan.
- E-8: a scan writes each finding once (retries and concurrent requests no
  longer duplicate). Each completed scan still records every finding it saw,
  by design: that is what lets a reader prove from the ledger alone that a
  scan is complete. `GET /findings` still reads the whole ledger, through one
  function (`Orchestrator.entryPages`); paginated ledger reads
  (`?after_seq=&department=&event_type=`, a separate branch) replace that
  function's body only.

### Timing-sensitive tests (E-15)

`detection-py tests/test_request_limits_live.py`: 90% of `/health` probes
under 16 concurrent worst-case batches must meet the 0.5 s bound outright,
and the single worst may exceed it by the idle server's own worst probe,
measured just before on the same box (one 0.62 s outlier under unrelated
machine load failed the sweep run). The 1 s bound beside large bodies is
likewise relative to the idle worst. Parsing on the event loop — the
regression these guard — held `/health` for seconds on every probe and fails
both. `orchestrator-go internal/client/limits_test.go`: the response-size
tests lift the 10 s call timeout (moving 64 MiB over loopback exceeded it
under load); the timeout itself is covered by its own test.

## AEGIS conditions on the fix wave (Oct 7 2026)

AEGIS approved fix-revrec 8bdebde with conditions. Regressions:
`services/detection-py/tests/test_revrec_aegis_conditions.py`,
`services/orchestrator-go/internal/orchestrator/aegis_conditions_test.go`,
`services/orchestrator-go/cmd/orchestrator/aegis_conditions_test.go`,
`apps/dashboard-ts/tests/finding-view.test.ts` and the "mixed" case of
`apps/dashboard-ts/tests/status.live.test.mjs` (the built page on the wire).
The L2 and M3 tests were mutation-checked: each fails with its guard removed.
How the new ledger event is written: ADR 0003 section 13.

| Id | Finding | Fix |
|---|---|---|
| M1 (Medium) | The dashboard types lacked `present_in_latest_scan`, `evidence_class`, `client_id`, `scan_id` and `period_label`, so a finding the latest scan no longer found looked current, and an ESTIMATED figure looked like an OBSERVED one | `src/types/finding.ts` mirrors `RecordedFinding` in full (plus `value_basis`, `labels_exceed_evidence`, scans, excluded scans, legacy rows). `src/lib/api.ts` refuses (502, "not the recorded-findings contract") a body whose findings lack staleness, evidence class, tenant, scan or period, or whose amount disagrees with its evidence class — a pre-fix-wave orchestrator can never render as current. `src/lib/finding-view.ts` + `page.tsx`: every row carries text badges (STALE — not in latest scan; OBSERVED / ESTIMATED / MODELED / NO FIGURE; LABELS EXCEED EVIDENCE), stale rows are dimmed, struck through and listed after every current one, ESTIMATED/MODELED amounts read "est. $x", and the header counts current/stale and observed/estimated/no-figure. `quotable()` = current, valued, in contract, labels within evidence |
| M2 (Medium) | renewal-never-triggered still labelled its ESTIMATED figure OBSERVED/HIGH | Renewal is INCREMENTAL/MEDIUM (revenue a working trigger would have added; success of the charge is not in the data). General invariant, both languages: only OBSERVED evidence may carry the `observed` or `financially_verified` classification or a `high`/`very_high` confidence (`zbm_schema.labels_exceed_evidence`, enforced by `Finding`; orchestrator-go `labelsExceedEvidence` in `checkFinding`, so an over-claiming finding fails the scan with nothing written). Records already in the ledger (8bdebde's renewal) are shown with `labels_exceed_evidence` set and a banner — never silently relabelled, never quotable. Tested over every evidence x classification x confidence combination and every agent's fixture output. The worst-case ledger summary drops from 272 to 271 characters (ESTIMATED can no longer carry the longest labels) |
| M3 (Medium) | A future `as_of` was accepted, so a subscription due later than the real present was reported as a missed renewal (P11 through `as_of`) | Refused when later than now + 60 s, the stated clock-skew tolerance between orchestrator-go and detection-py: detection-py `api.AS_OF_CLOCK_SKEW` (422 on `as_of`), orchestrator-go `AsOfClockSkew` (422 before any detection call or ledger write). A past `as_of` is unchanged |
| M4 (Medium) | Legacy ledger findings and excluded scans were counted but invisible on the dashboard | orchestrator-go returns `legacy_findings` (each pre-Oct-6 `kind:"finding"` entry, its amount shown only if it is contract money, else `amount_out_of_contract`) and every excluded scan with a status; the page shows "Excluded scans (n) — not counted" and "Legacy ledger findings (n) — LEGACY, not counted" sections below the findings |
| L1 (Low) | The affiliate commission's base was not recorded — only in the prose | `Finding.value_basis` {`base_usd`, `rate_percent`} (exact decimal text of the rate, at most 34 characters so the product stays exact in both languages); the model refuses a basis that does not reproduce the amount (`percent_of`, half-up). orchestrator-go checks it again with exact `big.Rat` arithmetic and records it as an `rr_value_basis` event covered by the scan's completion manifest; `GET /revenue-recovery/findings` serves it and the page shows "10% of $120.00" under the figure |
| L2 (Low) | No test that a failed scan releases the one-scan lock | `TestL2_FailedScanReleasesTheScanLock`: after a ledger failure mid-write, a detection contract failure and a refused request, the next scan is 200, not 409. It held (`defer Unlock`); the test fails when the unlock is skipped on error |
| L3 (Low) | No staleness label for abandoned scans | `ExcludedScan.status`: `running` (this process is writing it), `incomplete` (no completion or abort record, started < 2 min ago or start time unreadable — may be running in another process), `abandoned` (no completion or abort record, started >= 2 min ago: a scan request is cut off at 60 s and its abort attempted within 5 s more), `aborted`, `inconsistent`; plus `started_at` |
| LR-1 (ledger review) | `client/ledger.go` set the entry total to `len(raw)` of orchestrator-go's own read; with a `revenue_recovery`-scoped or filtered read that is the findings count and disagrees with `/ledger/verify` | `ledger_entries_total` now comes from `GET /ledger/head` `entries` (ledger-rust sweep F, `{"entries","head_seq","head_hash"}`, shape-checked); a ledger without that route (this branch's ledger-rust answers 404) falls back to the verify verdict's `entries`, and only an invalid chain (no count) falls back to the read, with `ledger_total_source` saying which. `ledger_entries_read` keeps the read's size. Premise note: on this branch ledger-rust has neither `/ledger/head` nor scoped reads (both are on fix-ledger 747fd1a, not merged), and sweep F's scoped tokens restrict writes only — every read still returns the whole ledger — so today `len(raw)` equals the total; the fix makes the total independent of the read for when filtered/paged reads (E-8) are wired |

### AEGIS re-review of 45bc33c (Oct 7 2026)

Regressions: `TestN1_*` to `TestN4_*` in
`services/orchestrator-go/internal/orchestrator/aegis_conditions_test.go`,
the N-tests in `apps/dashboard-ts/tests/finding-view.test.ts`, and the
"mixed" case of `tests/status.live.test.mjs`.

| Id | Finding | Fix |
|---|---|---|
| N1 (Medium) | Rollback hazard: a pre-6b0f0ad orchestrator ignores `rr_value_basis` events, so a new scan's manifest fails; it excludes that scan and presents the previous scan's findings as current | Verified by running 8bdebde's reader on ledgers written by this code: it excludes the new scan ("the scan's findings do not match its completion record", or with rrc2 "unreadable scan_completed record: not an rrc1 summary") and still marks the older scan's findings `present_in_latest_scan: true` — exclusion is loud, the fallback is silent, and an already-deployed old binary cannot be changed. So: (1) scans are now completed as `rrc2` (rrc1 still read; an rrc1 scan with a value-basis event is inconsistent); (2) this reader refuses any newer format with status `unsupported_format` and a reason that says orchestrator-go cannot be rolled back past the writer; (3) a scan that has a completion record but cannot be counted and completed after a client's latest counted scan makes nothing of that client current or quotable (`latest_scan_uncounted`, red banner on the page) — never a fallback to the older scan. **Operational rule (ADR 0003 section 13): orchestrator-go must not be rolled back past 6b0f0ad once an rrc2 scan exists; roll forward** |
| N2 (Low) | `labelsExceedEvidence` judged only known high labels, so an unknown label ("verified", "certain") passed | Allowlist (observed / attributed / incremental / financially_verified; low / medium / high / very_high): an unknown label fails `checkFinding` (scan fails, nothing written) and is flagged on read (`labels_exceed_evidence`, never quotable). detection-py's enums already refuse them |
| N3 (Low) | Scan `as_of` not shown; a scan as of an earlier instant recorded after a later one was indistinguishable | `ScanSummary.backdated` (as_of earlier than the same client's previous completed scan's, completion order); the page lists completed scans with their as_of and flags "BACKDATED" |
| N4 (Low) | Quotability was decided only in the dashboard | orchestrator-go serves `quotable` per finding (`RecordedFinding.isQuotable`: valid amount, present in the latest scan, OBSERVED, known labels within evidence, value basis reproduces the amount); the page shows the served flag, and a disagreement with its local restatement (`localQuotable`) is shown as "QUOTE CHECK MISMATCH — not quotable"; the API contract check refuses a body without it |

