# ZBM/ZBC — Revenue Recovery 1A

First real code in the `zbm-zbc` monorepo. Built Sep 21–22, 2026, then
hardened Sep 22, 2026 against a real independent review. The dated sections
below record what was run on their date; they are history, not the current
state. Current test counts are generated: **[docs/test-counts.md](docs/test-counts.md)**
(fix wave 25 — every hand-written count in this file had drifted; the
hygiene check now fails a hand-written count, see "Testing"). See `docs/adr/0001-revenue-recovery-1a-architecture.md` for the
full architecture rationale and scope boundary (Tier 3 excluded, see that
doc for why).

## Status

**8/8 detection agents (Tier 1 A–D, Tier 2 E–H) + full trust/safety net +
3-service pipeline (Python → Go → Rust) + TypeScript dashboard, all real,
all tested, all verified running together end-to-end — plus a hardening
pass (below) against real, independently-flagged gaps — all now fixed and
confirmed live, including gap #6 (money was `float`), fixed Sep 24 2026 by
converting money to exact `Decimal` end to end. The evidence ledger also
now records generic department events (`POST /ledger/events`) on the same
hash chain as findings.**

| Service | Language | Tests | Status |
|---|---|---|---|
| `services/detection-py` | Python (FastAPI, pydantic) | [counts](docs/test-counts.md) | Real, REST-exposed, hardened, money is exact `Decimal`; request limits (1000 items; every field bounded; per-route body limit = computed worst-case legal batch + 25%, 1–36 MiB; 1 large request at a time, else 503 + Retry-After; async `/health`; head size/deadline — ADR 0001 "Request limits"); run with `src/serve.py` |
| `services/orchestrator-go` | Go | [counts](docs/test-counts.md) | Real, live-tested against detection-py + ledger-rust, hardened, string-backed `Money`; server timeouts, body/header caps, bounded upstream responses (ADR 0001 "Request limits") |
| `services/ledger-rust` | Rust | [counts](docs/test-counts.md) (unit + real-binary integration), clippy `-D warnings` clean | Real, hash-chained, tamper-evidence proven by test, authenticated, findings + events on one chain |
| `apps/dashboard-ts` | TypeScript (Next.js 16) | [counts](docs/test-counts.md); `npm test` after `npm run build` (money vectors, loopback bind, error sanitizing, ledger status, load outcome → HTTP status, live status codes on the wire), build + typecheck clean; `npm audit` clean since fix wave 25 moved Next.js to 16.3.8 (GHSA-vcvr-r3jv-pc5j, critical, affected >=16.2.0 <16.3.6) | Real, rendered per request (`ƒ /`), binds 127.0.0.1 by default, reads recorded findings — viewing never writes; `/` and `/healthz` answer 503 (orchestrator unreachable/timeout, token unset) or 502 (token rejected, orchestrator error, ledger does not verify), 200 only when findings loaded and the ledger verified |

**Verified live, full-stack run** (Python + Go + Rust, real processes,
real HTTP, no mocks, all three services requiring and presenting real
bearer tokens): 8 agents run, 10 findings produced, all 10 written to the
now-authenticated tamper-evident ledger, `ledger_verify: valid: true`
returned over real HTTP after a real scan.

## Independent review, Sep 22 2026 — findings and fixes

An independent review (the same pass that hardened `services/fulfillment-py`
the same day) flagged six real, specific gaps in this build. Five are
fixed here and re-verified live, not just in unit tests; the sixth is a
real, still-open, explicitly deferred gap.

| # | Finding | Fixed? | How |
|---|---|---|---|
| 1 | `detection-py`'s `require_auth` called `hmac.compare_digest` unguarded — a non-ASCII bearer token raised an unhandled `TypeError`, turning an attacker-reachable invalid-token request into an unauthenticated 500 instead of a 401. | **Fixed** | Same fix as `fulfillment-py`: wrapped in `try/except TypeError`, treated as invalid (401). Verified over real HTTP with a live `uvicorn` process, not just `TestClient`. |
| 2 | `detection-py`'s `/docs`, `/redoc`, `/openapi.json` were reachable with no auth, exposing the full route/schema map. | **Fixed** | Disabled outright (`docs_url=None`, etc.), confirmed 404 over real HTTP. |
| 3 | `ledger-rust` hardcoded `0.0.0.0:{port}` — bound every network interface with no loopback default or override. | **Fixed** | `LEDGER_BIND_ADDR` env var, defaults to `127.0.0.1`. Confirmed by reading the actual bound socket from `/proc/net/tcp` while the process was running (`0100007F:...`, not `00000000:...`), not just by asserting the code changed. |
| 4 | `ledger-rust` had **zero authentication on any route**, including `POST /ledger/append` (anyone reaching the port could forge entries in the evidence ledger) and `GET /ledger/entries` (read the whole ledger). Confirmed the single most important service to protect, and the one with the least protection. | **Fixed** | `LEDGER_SERVICE_TOKEN` env var, fail-closed at startup if unset (same pattern as the other two services), required as `Authorization: Bearer <token>` on every route except `/health`, checked with a hand-rolled constant-time comparison (no `hmac`-equivalent crate was already a dependency, so one wasn't added for a single comparison). 8 new integration tests spawn the real compiled binary and hit it over a real TCP socket — the layer the original unit tests never touched, which is exactly how both bugs (#3 and #4) shipped with a fully green test suite. |
| 5 | Fixing #4 broke the actual calling code: `orchestrator-go`'s `NewLedgerClient` was built to send **no** Authorization header at all (there was a test explicitly asserting that, written when it was correct). A live 3-process pipeline run caught this — `AppendFinding` was fixed via the shared `doJSON` path, but `Verify()` builds its own request by hand (for its own documented reason — 409 is a valid non-error response it needs to parse, not just a transport error) and was missed on the first pass, 401ing every real scan's ledger-integrity check. | **Fixed** | `NewLedgerClient` now takes and sends a token; `Verify()` gets the header explicitly since it doesn't go through `doJSON`. Both `orchestrator-go`'s `main.go` (fail-closed on `LEDGER_SERVICE_TOKEN`, matching the other two required tokens) and the stale test comment referencing ledger-rust's old no-auth state were updated. Two new regression tests (`TestLedgerClient_SendsBearerTokenOnAppend`, `TestLedgerClient_SendsBearerTokenOnVerify`) — the second one specifically because the first alone would not have caught this. Re-verified with a second full live 3-process run after the fix: `ledger_verify: valid: true` over real HTTP, not just green `go test`. |
| 6 | Money (`amount_usd`, `order_value_usd`, `unit_price_usd`, etc.) is `float` throughout `zbm_schema`, used in real arithmetic across 7 agent files — unlike `fulfillment-py`'s single unused `Decimal`-converted field, this is live, load-bearing math. | **Fixed Sep 24 2026** (founder-approved scope) | Deferred on Sep 22 for blast radius, then fixed end to end: Python `Decimal` quantized to cents `ROUND_HALF_UP` at every creation/computation point, JSON money is a two-decimal string (`"12.30"`), Go carries a validated string `Money`, the Rust ledger stores the string (legacy numeric entries still load and old chains still verify), the dashboard displays the string without parsing it. See ADR 0003 and the Sep 24 section below for how it was verified. |
| 7 | `orchestrator-go` called `http.ListenAndServe(":"+port, mux)` with no host — same class of bug as `ledger-rust`'s #3, binding every network interface instead of just localhost. Found after the initial review pass, in a later session. | **Fixed** | `ORCHESTRATOR_BIND_ADDR` env var, defaults to `127.0.0.1`. Confirmed live by reading the actual bound socket from `/proc/net/tcp` (`0100007F:...`, not `00000000:...`), same method as #3. `cmd/orchestrator` had zero test coverage before this — 2 new tests build and spawn the real compiled binary and connect over a real socket, the same gap in coverage that let #3/#4/#7 all ship with green test suites. |

## Sep 24 2026 — money is exact, ledger records events

**Gap #6 fixed (money `float` → `Decimal` end to end).** Full design and the
backward-compatibility proof are in
`docs/adr/0003-money-decimal-and-ledger-events.md`. In short:

- `detection-py`: every money field is a `Decimal` quantized to cents
  `ROUND_HALF_UP` (`zbm_schema/money.py`); subtotals, stacked percent
  discounts (quantized per discount line), contract drift and all Tier 2
  values are exact; floats are accepted only through `str()`, so fixture
  `49.99` stays exactly `49.99`; NaN/Infinity/negative/`-0.00` are rejected.
  JSON money is a two-decimal string. Explanation text uses the same
  formatter the Hallucination Agent compares against, exactly (no tolerance;
  `$12.3` does not match `12.30`). Fixture results are unchanged in value
  (e.g. the stacked discount on `ord_1007` is `54.38` = 22.50 + 31.88,
  per-line half-up; the old float path also rounded to 54.38 here), but are
  now exact `Decimal`s and two-decimal strings rather than floats.
- `orchestrator-go`: `client.Money` is a validated string; JSON numbers for
  money are rejected; the amount detection-py emits is the exact string the
  ledger receives.
- `ledger-rust`: `amount_usd` is the canonical string; a JSON number on
  `POST /ledger/append` is a 400. Legacy log lines with numeric amounts
  still load and verify — proven against a log written by the actual
  pre-change server binary (commit 9531fc2), checked in as
  `tests/fixtures/legacy_ledger_v1.jsonl`, and (fix wave 1) a second log
  from that binary with negative, `-0.0` and sub-cent negative amounts,
  which are served verbatim as their old hashed rendering (`"-5.00"`,
  `"-0.00"`).
- `ledger-rust` fix wave 1 (ADR 0003 sections 4-5): a torn final log line
  from a crash is preserved to `<log>.torn-<nanos>` and truncated on
  startup instead of bricking the ledger (other corruption still refuses
  to start); a failed write is truncated back and never advances memory;
  `POST /ledger/append` rejects `|`, control characters and the string
  `"null"` in optional fields, and ambiguous (re-splittable) findings or
  invalid events on disk refuse to load; logging can no longer crash the
  server. (The fix-wave-1 non-ASCII-header limitation is gone since fix
  wave 4.) Fix wave 4 (ADR 0003 section 7): one slow client can no longer
  freeze the ledger. The server moved from tiny_http to hyper/tokio; each
  connection has deadlines (head 5 s, body 5 s, whole connection 15 s),
  a declared body over 64 KiB is a 413 before it is read, unauthenticated
  requests are answered and closed without reading their body, and over
  `LEDGER_MAX_CONNECTIONS` (default 512) open connections a caller gets an
  immediate 503. Appends stay strictly serialized. Fix wave 2: new appends enforce the money bound (ADR 0003
  section 1a; over `"999999999999999.99"` is a 400), checked against every
  `ledger_append_expected` verdict in `fixtures/money_vectors.json`;
  already-persisted amounts (legacy `1e20`, over-bound strings the
  fix-wave-1 binary accepted) still load and verify. At 19f7320, `cargo test`: 84
  passed (56 unit, 8 `server_auth`, 8 `server_events`, 12
  `server_hardening`).
- `apps/dashboard-ts`: `amount_usd: string`, displayed verbatim, never
  parsed to a JS number, no totals computed.

Tests added that fail under float include `0.10 + 0.20`, `0.67 × 3 = 2.01`,
a 1,583-line subtotal, half-up at `.005` boundaries (`2.675`, `1.005`,
`0.125`, `31.875`), identical-string JSON round trips, and `49.99` staying
exact.

**New: `POST /ledger/events`** (bearer auth, `LEDGER_SERVICE_TOKEN`). Records
a department event — `event_id` (idempotency key), `department`,
`event_type`, `actor`, `subject_id`, `payload_sha256`, `summary` — on the
SAME hash chain as findings. `201` new, `200` identical retry, `409` same
`event_id` with different content, `400` invalid, `401` bad token. Every
entry in `GET /ledger/entries` now carries `"kind": "finding"` or
`"kind": "event"`; `/ledger/verify` covers both; the event hash is
domain-separated (`event|` prefix); events and the idempotency index
survive restart. Field rules are in ADR 0003.

**Verified Sep 24 2026** (commit 2dbce4d; historical counts, superseded by docs/test-counts.md):
- `python3 -m pytest -q` (detection-py) at 2dbce4d: 138 passed.
- `go vet ./...` clean; `go test ./...` (orchestrator-go) at 2dbce4d: 21 passed.
- `cargo test` (ledger-rust) at 2dbce4d: 55 passed (39 unit, 8 `server_auth`, 8
  `server_events`); `cargo clippy --all-targets`: no warnings.
- `npm install` / `npx tsc --noEmit` / `npm run build` (dashboard-ts):
  clean; `npm audit`: 0 vulnerabilities.
- Live three-process run (real processes, real HTTP, real tokens): a scan
  through the orchestrator ran 8 agents and wrote 10 findings to the ledger
  with string amounts (`"120.00"`, `"54.38"`, `"89.99"`, …, `null` for the
  two no-value findings); one event was posted (`201`), retried (`200`),
  conflicted (`409`); `/ledger/verify` returned `{"valid":true,"entries":11}`;
  after restarting the ledger process it reloaded 11 entries, still
  answered the retry with `200`, and a second scan brought it to
  `{"valid":true,"entries":21}`.

## Sep 24 2026 — fix wave 1 (Revenue Recovery)

- **F14 (huge money → 500):** money is now bounded to < 10^15 dollars (max
  `"999999999999999.99"`, ADR 0003 section 1a) in detection-py (422),
  orchestrator-go (rejected on decode) and the dashboard (not displayed).
  Every Python money operation runs under an explicit `MONEY_CONTEXT`; an
  order whose subtotal would exceed the bound is a 422. The ledger adopted
  the same bound for new appends in fix wave 2 (400).
- **F15:** `fixtures/money_vectors.json` — one shared vector file run by all
  three test suites (and carrying the ledger's expected verdict). It found
  Python accepting `"1.00\n"`, `"012.30"`, `"12.3"`, `"12.345"` and JSON
  numbers; all now rejected, same as Go and the dashboard.
- **Viewing no longer writes:** new read-only `GET /revenue-recovery/findings`
  (recorded ledger findings + ledger verify); the dashboard uses it and is
  always rendered per request. `/revenue-recovery/scan` is POST-only (405
  otherwise). Live: a scan wrote 10 entries; 5 reads and 3 page views left
  the ledger at 10.
- **Other 4xx-not-500 fixes in detection-py:** timezone-less affiliate
  timestamps, NaN/Infinity in a body, and huge quantities now return 422.

## Sep 24 2026 — fix wave 3 (Revenue Recovery + ledger)

- **N6:** stacked codes that give away 0.00 (0% codes, 10% of $0.01) and
  orders with no line items no longer 500: a finding whose value rounds to
  0.00 is not emitted (ADR 0001 "Zero-value findings"); an agent error for
  one item is a 422 naming that item, never a 500 or a partial list. All 8
  agents are fuzzed over generated valid input.
- **D2:** `npm start` / `npm run dev` bind `127.0.0.1` (`DASHBOARD_BIND_ADDR`
  overrides); `tests/bind.test.mjs` fails if the default would not be loopback.
- **D3:** orchestrator errors name the service that failed; callers get a
  generic message plus a `correlation_id` (details only in the server log,
  no internal URLs). A scan checks `GET /ledger/verify` first: ledger down
  or chain invalid → 502 with zero detection calls.
- **N8:** the ledger refuses unknown fields in persisted entries (startup
  fails) and in `POST /ledger/append` (400). An empty ledger now verifies:
  `200 {"valid":true,"entries":0}` (was `409 "Empty"`).
- Live (ports 19640–19643, real processes): N6 bodies → 200; dashboard
  socket `0100007F` (127.0.0.1); ledger down → scan 502 in <1 ms with 0
  detection calls and no address in the body; empty ledger verify → 200.

## What's built

**Tier 1 (rule-based, low-hanging fruit):**
- A. Affiliate & Coupon-Extension Leak Agent
- B. Discount & Coupon Misuse Agent
- C. Abandoned-Cart Coverage Agent
- D. Renewal-Never-Triggered Agent

**Tier 2 (mid-reach, no processor integration required):**
- E. Server-Side Attribution Agent
- F. Cross-Channel Attribution Modeling Agent (deliberately conservative —
  flags the pattern, does not fabricate a dollar figure without a real
  multi-touch model, which isn't built yet)
- G. Platform Integration Agent
- H. Contract & Pricing-Term Drift Agent

**Trust & safety net (`services/detection-py/src/safety/`):**
- Trust Graduation Agent — Decision 1's exact numeric thresholds (20
  shadow decisions @ ≥95% agreement → 30 clean human-approved executions
  over ≥30 days), pure decision function, no side effects.
- Calibration Drift Agent — Failure Mode #4 safeguard, flags a confidence
  band whose observed accuracy falls below what it promises.
- Hallucination Agent — Failure Mode #1 safeguard, checks every finding's
  human-readable explanation against its own backing dollar figure.
  Regression-tested against every real agent's actual fixture output.
- Action Execution Agent — the only agent permitted to execute a fix.
  Gated on the Trust Graduation Agent's real evaluation logic (not a
  duplicated copy of the thresholds). Currently refuses everything
  because no client has graduated and no live platform connector exists
  — that refusal is itself the tested behavior, not a stub.
- Correlation safeguard (`zbm_schema/correlation.py`) — Decision 3 /
  Failure Mode #2, catches two agents claiming the same order before
  either value is presented. Proven against a fixture order
  (`ord_1007`) deliberately built to trigger two agents at once.

**Not yet built:** Tier 3 (I–L) — see the ADR for why that's a deliberate
exclusion, not an oversight.

## Running it

Requires: Python 3.12+ (CI tests 3.12 and 3.13; 3.11 is not tested — fix wave 25), Go 1.24+, Rust/cargo, Node 22+.

**All three backend services fail closed and refuse to start without
their auth token set** (post-hardening, Sep 22 2026). Pick your own real
shared secrets for local/private-network use — the values below are
examples only, not defaults baked into the code.

```bash
# 1. Evidence ledger (Rust) — separate terminal. Start this first: the
#    other two call it.
cd services/ledger-rust
export LEDGER_SERVICE_TOKEN=<your-shared-secret>
cargo run --bin server   # LEDGER_PORT (default 8090), LEDGER_BIND_ADDR (default 127.0.0.1)

# 2. Detection service (Python) — separate terminal
cd services/detection-py
pip install -r requirements.txt
export ZBM_SERVICE_TOKEN=<your-shared-secret>
cd src && python3 serve.py --port 8000   # not `uvicorn api:app`: serve.py adds the request-head limits (ADR 0001)

# 3. Orchestrator (Go) — separate terminal. Needs the SAME token values
#    as the two services it calls, plus its own token for callers of it.
cd services/orchestrator-go
export DETECTION_SERVICE_TOKEN=<same value as ZBM_SERVICE_TOKEN above>
export LEDGER_SERVICE_TOKEN=<same value as LEDGER_SERVICE_TOKEN above>
export ORCHESTRATOR_SERVICE_TOKEN=<your-shared-secret, for callers of THIS service>
go run ./cmd/orchestrator  # DETECTION_SERVICE_URL, LEDGER_SERVICE_URL, ORCHESTRATOR_PORT
#   (ORCHESTRATOR_PORT=0 + ORCHESTRATOR_PORT_FILE=<path>: the kernel picks the port and the
#   orchestrator writes it there, atomically; removed on SIGINT/SIGTERM, not on SIGKILL — a hint,
#   check GET /health before trusting it, as for ledger-rust's LEDGER_PORT_FILE, ADR 0003 §11)

# 4. Dashboard (TypeScript) — separate terminal
cd apps/dashboard-ts
npm install
ORCHESTRATOR_URL=http://localhost:8080 ORCHESTRATOR_SERVICE_TOKEN=<same as above> npm run dev   # http://127.0.0.1:3000
# `npm run build && npm start` for production. Both bind 127.0.0.1 (the
# dashboard has no auth); DASHBOARD_BIND_ADDR overrides, PORT sets the port.
# The page shows findings already recorded in the ledger (read-only);
# run a scan first with the POST below. `npm test` runs every dashboard test
# (money vectors, bind, error mapping, and the live tests against the build).
# Monitoring: GET /healthz (JSON) and GET / answer 200 only when findings
# loaded and the ledger verified; 503/502 otherwise (ORCHESTRATOR_TIMEOUT_MS,
# default 10000, bounds the orchestrator call).
```

Or just run the full pipeline once without the dashboard:
```bash
curl -s -X POST -H "Authorization: Bearer $ORCHESTRATOR_SERVICE_TOKEN" \
  http://localhost:8080/revenue-recovery/scan | python3 -m json.tool
```

## Testing

```bash
# Test counts for every suite: docs/test-counts.md (generated; do not
# write counts here). Python: use `python3 -m pytest`, not the bare `pytest`
# binary, if pytest was installed as a standalone tool (e.g. via uv) —
# it can silently run against a different interpreter than the one you
# `pip install`ed into, and report a module-not-found collection error
# that looks like a broken test suite rather than an environment mismatch.
cd services/detection-py && python3 -m pytest -q

# Go (the binary tests start the real orchestrator with ORCHESTRATOR_PORT=0
# and read the bound port from ORCHESTRATOR_PORT_FILE — no fixed port)
cd services/orchestrator-go && go vet ./... && go test -count=1 ./...

# Rust (the integration tests spawn the real compiled binary and talk to it
# over a real TCP socket — tests/server_auth.rs, server_events.rs,
# server_hardening.rs, server_port_file.rs, server_slow_clients.rs)
cd services/ledger-rust && cargo test && cargo clippy --all-targets -- -D warnings

# Dashboard (the live tests start the built server on a port it picks:
# run `npm run build` first; without a build they SKIP, and CI fails a skip)
cd apps/dashboard-ts && npm ci && npm run lint && npm run build && npm test && npm audit
```

### Hygiene check (fix wave 25, founder ruling R-HYGIENE)

Every suite in CI runs under `devtools/hygiene_check.py run` (standard-library
Python; `--help` lists the rules), which fails the run when the suite:
changes a tracked file (R1); leaves a new git-ignored file in the checkout
(R2; a venv or a build directory the job itself makes is allowlisted by
path); leaves anything in its private TMPDIR or creates a new entry in /tmp
(R3); leaves a process running (R4 — found by session/process group, an
inherited environment marker (read on macOS too since fix wave 26b), and on
Linux by being the suite's child subreaper, so a double-forked env-scrubbed
grandchild is found too; `docs/ci.md` states what macOS cannot see); skips a
test for a reason not on the suite's list in `devtools/hygiene_allowlist.json`
(R5); or runs a different number of tests than `docs/test-counts.md` says (R6).
`devtools/hygiene_check.py lint` (CI job `hygiene-static`) fails a test that
asserts an upper bound on wall-clock time against a literal (L1), a test that
binds a hard-coded port (L2), a hand-written test count in the docs (L3), and
a shared file that differs between services (L4). Python suites load
`devtools/pytest_plugin/zbm_pytest_hygiene.py` through the wrapper (it can be
used on its own: see its docstring). Run one suite locally exactly as CI does:

```bash
python3 devtools/hygiene_check.py run --suite python:finance-py --kind pytest \
  --cwd services/finance-py -- python3 -m pytest -q -rs -p no:cacheprovider
python3 devtools/hygiene_check.py lint
```

After adding or removing tests, regenerate the suite's row with
`--counts write` (same command) and commit `docs/test-counts.md`. Open
Medium/Low findings that a wave could not close are tracked one line each in
[docs/findings/OPEN.md](docs/findings/OPEN.md) (founder ruling R-GATE: the next
wave fixes them).

## Data

Everything runs against `fixtures/*.json` — a shared, hand-built pool of
orders, customers, subscriptions, and Tier 2 domain data with deliberate
leak cases AND control cases (so every agent proves it doesn't just flag
everything). **Explicitly non-live.** No real store is connected. See
Decision 6 in `revenue-recovery-founder-decisions.md` (a founder document that is NOT in this
repository — a dangling reference, scout C4-5) for why, and the
platform-agnostic design note in the ADR for how a real store's data
attaches later without touching agent logic.

## Known gaps, stated plainly

- No per-platform translation layer yet (Shopify/Amazon/TikTok →
  the generic `zbm_schema.Order` shape) — the seam is designed for it,
  nothing is built.
- Money is exact and USD-only: there is still no multi-currency support
  and no per-store leak thresholds (by design decision, not oversight).
- The events endpoint stores only a payload hash and a summary; the ledger
  cannot show what an event's payload was, only prove it has not changed.
  Callers must keep the payload themselves.
- Dashboard has no write actions, no auth, no multi-client view — it
  renders recorded findings, nothing more. It binds 127.0.0.1 by default
  for that reason; do not set DASHBOARD_BIND_ADDR to a public address.
- Tier 2's `escalator`/`overage_rate` contract-drift directionality
  rules are not implemented (only `minimum_spend` shortfall detection is
  real) — flagged in code rather than guessed at with no fixture behind it.
- Single shared-secret bearer tokens across all three services, not a
  real auth system (no per-caller identity, no rotation, no scoping) —
  adequate for a private network, not for anything internet-facing.
- This is one review pass (Sep 22 2026). A second, independent reviewer
  looking at the same code might find different things — see
  `fulfillment-py`'s README for the same caveat stated about that build.

## Onboarding department (`services/onboarding-py`) — Sep 24, 2026

Client onboarding from signed contract to a running account, across three
lanes: client, ZBC creator and ZBC brand. It is built from the founder-locked
spec, with 15 deterministic single-task intelligences (no model calls).
Architecture: `docs/adr/0004-onboarding-department-architecture.md`. Details
and routes: `services/onboarding-py/README.md`.

- **Status:** built and tested (current test count: docs/test-counts.md; `python3 -m pytest -q`; the live
  ledger-rust tests build the binary with cargo and run by default). **Not
  certified for any real client, clipper or brand.** Scenario, attack and
  guardrail tests exist. The AEGIS review findings were fixed in fix waves 1–7
  (Sep 24); see ADR 0004.
- **Hostile input can't stall it** (fix wave 4). Every regex is linear-time.
  Input is capped before it is scanned: field lengths are checked first, a body
  over 1 MiB is a 413, a request target over 8 KiB is a 414, and log lines are
  cut. Scanning runs off the event loop. Each request's budget counts its own
  CPU time, not time spent waiting on other requests, and scanning is admitted
  through a weighted budget with measured defaults of one large scan at a time
  (busy returns 503 with Retry-After; fix wave 6); bodies up to 16 KiB have a
  lane of their own, so a client's small messages are not queued behind other
  clients' large uploads (p50 12–25 ms beside 1–12 uploaders; fix wave 7).
  Intake facts are capped per client (409 past 2,000, counted after per-field
  trimming). `/health` stays responsive under attack.
- **Fails closed.**
  - Activation needs both the Contract (14) and Compliance (15) gates, and a
    blocked activation returns the exact unmet list.
  - Every decision is written to the ledger (`POST /ledger/events`, department
    `onboarding`) before any outside effect, such as a payout or handoff. If the
    write fails, the action doesn't happen and the API says so (503). If it
    fails after an effect, the API names the effect. The next call writes the
    missing record, and a retry finishes the job. Retries are idempotent. If
    the ledger reply was lost, the API says `proceeded: "unknown"` and asks for
    the identical retry. Only ledger-rust's exact load-shed answer counts as
    "not recorded" (the same rule as creative-py).
  - Only the server's clock decides. Clipper 18+ is checked on the server's
    date at UTC−12. Credentials are refused at intake, and client free text is
    stored only redacted. Money is the contract string only. Escalation
    decisions need Andre's own key.
  - Compliance 38, the vault, platform APIs, push to Andre and contract
    storage are stand-ins that answer "not allowed yet" / "not wired".
- **Reuses Revenue Recovery** over HTTP (detection-py's real routes). It never
  re-implements a detection agent.
- **Run:** `cd services/onboarding-py/src && ONBOARDING_SERVICE_TOKEN=... python3 -m api`
  - Binds 127.0.0.1:8200 by default.
  - Uses the hardened launcher (`src/serve.py`): 16 KiB head cap, head and
    idle timeouts, and a concurrency limit. Don't start it with plain
    `uvicorn api:app`.
  - Needs `LEDGER_SERVICE_URL`/`LEDGER_SERVICE_TOKEN` to act, and
    `DETECTION_SERVICE_URL`/`DETECTION_SERVICE_TOKEN` for the audit.
- **Open items:**
  - Deal-size threshold: unset, so every deal escalates.
  - Stuck window: 48h suggested.
  - Intake channel.
  - Noon cutoff: to be revisited.
  - Contract storage location.
  - Compliance department: not built.
  - Counsel: P1 wording and the P23 clause.
  - Accountant: 1099 threshold from 2027.

## Creative Production (services/creative-py)

Added Sep 24, 2026 from the founder-locked Creative Production spec (rev 1).
One department with two structurally separate intelligence layers: **ZBM**
(advertising agency: 8 intelligences, brief → Creative Lead approval →
export validation → rights → quality with a 2-round cap → Compliance 38
gate → Andre) and **ZBC** (clipping agency, per campaign: 9 intelligences,
rulebook draft → approval by a different actor → Andre signs → frozen
when live → Moment Map → hooks → kit → clip review citing rule ids →
payout *eligibility* only). They share the Platform Rules Registry, rights
records and the evidence ledger, never decision-makers (enforced by an
import test). Every approval, rejection, signature and crossing goes
through the ledger's `POST /ledger/events` first; if that fails the
decision does not take effect (503). Compliance 38, Verification and
Integrity, Legal 37, Finance 31, Clipper Network and the Enigma/Phantom
Canvas contract are fail-closed stand-ins, so today no ZBM work reaches
Andre's final approval and no ZBC clip is payout-eligible. Fix wave 1
(Sep 24): no outside department is called before its request is on the
ledger; deterministic ledger event ids; review cap per brief deliverable
across jobs; confusable/split-letter text evasion caught; bounded ids
(422, never a fake 503); verified live against the real ledger-rust.
Fix wave 2: per-actor credentials (`CREATIVE_ACTOR_TOKENS`,
`X-Creative-Actor-Token`); review cap per client deliverable; retry
receipt time bound to content (15 min); never-say survives invisible and
lookalike characters. Fix wave 4: never-say near misses with symbols/digits
("return$", "G€t", "6et") and unknown Latin letters go to a human; a lost
ledger response no longer wedges a clip (`took_effect: "unknown"`, exact
replay); `Idempotency-Key` on every creating POST; escalation clones matched
with length tolerance and strict client ids; 1 MiB body limit. Fix wave 5:
never-say is a similarity gate (visual skeleton + bounded edit distance;
"make rnoney" → reject, "miracle kure" → human; 2.5% measured false
positives); `serve.py` caps request heads at 16 KiB with head/idle
deadlines and bounded concurrency; ledger-rust's own load-shed 503 is "not
recorded"; an uncertain human verdict can be withdrawn by its reviewer.
Fix wave 6: never-say runs on the caption's letter stream (splits,
stretched and doubled letters, fillers within two words; 4-letter entries
exact-only unless opted in; 1.7% / 2.7% measured false positives on two
corpora); 422 bodies are bounded and never echo the request, JSON bodies
capped at 4,096 members; a certainly-unrecorded verdict holds nothing.
Fix wave 7: JSON member caps are per route, computed from the request
model (a 2,000-segment Moment Map is accepted; the 4,096 cap refused it);
vowel-drop ("mk mny") and phonetic ("phree money") respellings go to a
human (consonant-skeleton and phonetic-key signals; 0.0% / 2.7% / 2.1%
false positives on three corpora); the per-word share rule is a 60%
letter share, never a hard cap; every text field, the bio included, is
scanned by every prohibiting rule. Fix wave 8: the JSON shape gate covers
every `application/*+json` content type and anything else is 415 unread;
rule ids have room for the model (`NS-100`, `NS-1000`, ...; two-digit ids
unchanged); a symbol standing for a never-say word ("make 💰", "make $$$
fast") and a respelled phrase split across any two fields go to a human;
stacked respellings ("grnteed retunrs", "lose vvait fst") are caught (0/30
auto-pass, was 9/30; false positives unchanged); a 99-phrase review of a
50 KB transcript is ≤ 1.2 s CPU (was 5.9 s) — not bounded for the
1,000-phrase lists the goal model admits (9 s; open decision). Fix wave 9:
never-say phrases in any letter-like style (🅼🅰🅺🅴, 🅜🅐🅚🅔, 🇲🇦🇰🇪, math,
fullwidth, small caps, super/subscript; a map generated from Unicode names)
are rejected, styled text and text canonicalisation mostly strips are a
human's call; any symbol in a never-say word's place (across line breaks
and fields) goes to a human; Clip Review at 100 phrases ≤ 1.3 s CPU on the
AEGIS generator (was 4.7 s) and runs off the workflow lock; retired rule
ids are derived, not stored, and the rulebook list is paginated; 401
before 415.

```bash
cd services/creative-py && python3 -m pytest -q      # count: docs/test-counts.md
export CREATIVE_SERVICE_TOKEN=<secret> CREATIVE_ANDRE_APPROVAL_TOKEN=<other secret>
export CREATIVE_ACTOR_TOKENS='{"<actor_id>": "<token>", ...}'   # per-actor credentials
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret>
cd src && python3 serve.py   # CREATIVE_BIND_ADDR (default 127.0.0.1), CREATIVE_PORT (default 8300)
```

Details, live-run evidence and gaps: `services/creative-py/README.md`;
decisions: `docs/adr/0005-creative-production-architecture.md`.

## Compliance (38) (`services/compliance-py`) — Sep 26, 2026

The hard gate before **activation** (Onboarding), **payout** (Creative's
payout eligibility) and **publish** (Creative's ZBM gate), plus Vanta-style
**control monitoring** (17 controls, trust-center view), over an
**obligation register** of 118 rows seeded from the verified compliance
research (59 verified, 59 unverified; the seed file is checked against its
SHA-256 at every start). Built from the locked Compliance spec (rev 1), with
11 deterministic single-task intelligences and no model calls. Architecture
and every choice made where the spec was silent:
`docs/adr/0006-compliance-department-architecture.md`. Routes and settings:
`services/compliance-py/README.md`.

- **Status:** built and tested (current test count: docs/test-counts.md; `python3 -m pytest -q`; no network —
  a socket guard fails any test that tries). **Not certified for any real
  client, clipper, payout or publish.** Verification and Integrity, Finance
  (31), Legal (37), the OFAC screening provider and the accessibility checker
  are stand-ins that answer "not allowed yet", so on day one every payout is
  blocked (counsel rows CQ-01/03/11 stay red until memos are approved), Meta
  and Twitch clips are blocked, and every site/video waits for an
  accessibility provider and Legal (37) — the spec's stated day-one effect.
- **Fails closed.** Nothing is in force until Andre approves the seed with
  his own token (`COMPLIANCE_ANDRE_APPROVAL_TOKEN`; the service or a caller
  token never counts as his). Expired or unverified rows block every gate
  they feed; an unknown jurisdiction is refused; a missing fact is named.
  Every block cites an obligation id and its source URL. Every ruling,
  decision, screen, check, control result and hold is written to the ledger
  (`department: compliance`) and to a hash-chained local log **before** it
  takes effect; otherwise 503 and nothing issued.
- **Callers:** onboarding-py and creative-py gained thin HTTP clients
  (`HttpComplianceDepartment`, `HttpCompliance38`) used only when
  `COMPLIANCE_SERVICE_URL`, `COMPLIANCE_SERVICE_TOKEN` and
  `COMPLIANCE_CALLER_TOKEN` are set; any failure means "not allowed". Those
  callers still send today's facts (spec §F.2 changes not made in this
  build), so Compliance answers blocked with every missing fact named.
- **Live run** (real ledger-rust binary + compliance-py over real HTTP):
  `devtools/live_run.py` — all gates, two register approvals, a Change
  Watcher proposal approved by Andre, both thin clients, and
  `GET /ledger/verify` → `{"entries":148,"valid":true}`.

```bash
cd services/compliance-py && python3 -m pytest -q      # count: docs/test-counts.md
export COMPLIANCE_SERVICE_TOKEN=<secret> COMPLIANCE_ANDRE_APPROVAL_TOKEN=<Andre's secret>
export COMPLIANCE_CALLER_TOKENS='{"onboarding": "<>=32 chars>", "creative_production": "...", "scheduler": "..."}'
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret> COMPLIANCE_DATA_DIR=<dir>
cd src && python3 -m api   # COMPLIANCE_BIND_ADDR (default 127.0.0.1), COMPLIANCE_PORT (default 8380)
```

## Verification and Integrity (`services/verification-py`) — Sep 26, 2026

The "bean counter making sure clippers play fairly": certifies **view counts** per clip at settlement
from the platform's official API, through the clipper's own OAuth connection. It also answers Compliance's
HR-13 and age ports, Creative's `attest_clip` / `attest_result` and Onboarding's `age_verified_18_plus`.
It checks clip integrity (same clip, caption unchanged, live through the minimum live period) and clipper
integrity (strikes, bought engagement, stolen clips, duplicate identities, 18+). Anomaly holds go to a human.
It is built from the locked V&I spec (rev 1) with 10 deterministic single-task intelligences and no model
calls. Money never touches it: no amount, rate or currency anywhere. Architecture and the 35 numbered choices
made where the spec was silent are in `docs/adr/0007-verification-integrity-architecture.md`. Routes and
settings are in `services/verification-py/README.md`.

- **Status:** built and tested (current test count: docs/test-counts.md; `python3 -m pytest -q`; no network). **Not certified for any
  real clipper, clip or payout.** The token vault, platform adapters, perceptual hasher, media intake,
  age provider, Finance (31), Legal (37), People (43) and Clipper Network are fail-closed stand-ins, so on
  day one no connection completes and nothing certifies. Even with every stand-in replaced, the spec's own
  rule table makes certification wait for a counsel memo on Compliance row CQ-11 (VI-05/VI-06 cite it). The
  full unlock list is in ADR 0007.
- **Fails closed.** Nothing is in force until Andre approves the 28-rule seed (pinned SHA-256). Every
  rejection, hold or strike cites a rule id and a reason code from the closed catalog. Bans take effect only
  with Clipper Network's call **and** Andre's token. Every ruling is recorded on the ledger
  (`department: verification_integrity`) and in a hash-chained, ledger-anchored local log before it is
  answered; otherwise 503 and nothing is issued. Raw platform ids live only in a purgeable side store under
  the platforms' retention rules. The log keeps hashes and HMACs only; no token, code or DOB is ever stored.
- **Other services unchanged:** the spec's "changes other services must make" (thin clients in
  creative-py, compliance-py and onboarding-py) are reported, not made. Until they are, no service calls V&I.
- **Live run** (`devtools/live_run.py`, real ledger-rust binaries). Leg A runs compliance-py and V&I through
  their production entrypoints: V&I reads HR-13 live from compliance-py, and nothing connects or certifies.
  Leg B uses test fakes and a settable clock over 32 simulated days. It exercises every ruling type, a
  revision down (clawback −300 views), holds released and upheld, S1/S2/S3 strikes, an Andre-approved ban
  and a restart with the anchors verified. It ends with `GET /ledger/verify` → `{"entries":57,"valid":true}`
  and `{"entries":6802,"valid":true}`. 28/28 checks passed.

```bash
cd services/verification-py && python3 -m pytest -q      # count: docs/test-counts.md
export VI_SERVICE_TOKEN=<secret> VI_ANDRE_APPROVAL_TOKEN=<Andre's secret>
export VI_CALLER_TOKENS='{"compliance_38": "<>=32 chars>", "creative_production": "...", "scheduler": "..."}'
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret> VI_DATA_DIR=<dir>
cd src && python3 -m api   # VI_BIND_ADDR (default 127.0.0.1), VI_PORT (default 8390)
```

## Clipper Network (`services/clipper-network-py`) — Sep 26, 2026

The system of record for a ZBC clipper from application to exit: recruiting
(opt-in only), admission, tiers (T0–T3 from V&I-verified outcomes only),
campaign enrolment and delivery of Creative's signed kit, versioned-template
messaging inside the recipient's quiet hours, disputes, discipline from V&I
strikes (a ban only on Andre's approval) and the P4 clean exit. Built from
the locked Clipper Network spec (rev 1): 10 deterministic single-task
intelligences, no model calls, no money handled or stored. Architecture and
every choice made where the spec was silent:
`docs/adr/0008-clipper-network-architecture.md`. Routes and settings:
`services/clipper-network-py/README.md`.

- **Status:** built and tested (current test count: docs/test-counts.md; `python3 -m pytest -q`; no
  network — a socket guard fails any test that tries). **Not certified for
  any real clipper.** V&I, Compliance (38), Creative, Finance (31), Legal
  (37), People (43), the messaging provider, the hub and push are stand-ins
  that answer "not allowed yet", so on day one admission is blocked with one
  `DEPENDENCY_UNAVAILABLE:<port>` per stand-in, nothing is enrolled and no
  message is delivered. ADR 0008 ends with the unlock list.
- **Fails closed.** Nothing is in force until Andre approves the pinned
  rules seed (27 rules, 8 counsel holds, 17 templates); only Andre changes a
  rule or template, weakening changes need his explicit acknowledgment, and
  the §H numbers are rule parameters the environment cannot change. CN never
  trusts a caller's boolean (`age_verified` is a 422; a tick box never
  counts); it asks V&I, Compliance, Finance, Legal and Creative. Every
  adverse item cites a rule id in force. Every decision is written to the
  ledger (`department: clipper_network`) and to a hash-chained, ledger-anchored
  local log before it takes effect, otherwise 503 and nothing changed; the
  compliance-py reconcile procedure and instance lease apply. Contact data
  lives only in a separate contact store so exit deletion is real; DOBs and
  OAuth codes are relayed to V&I and stored nowhere.
- **Callers:** the thin clients to V&I, Compliance and Creative are wired only
  when fully configured; the changes those services (and Onboarding) must make
  are listed in ADR 0008, not made here.
- **Live run** (real ledger-rust binary, production entrypoint on stand-ins +
  `devtools/live_server.py` with the test fakes): admission blocked on
  stand-ins → admitted → T0→T1 → enrolled → kit delivered and acknowledged →
  S3 mirrored → suspended + ban proposal → Andre's ban → offboarding →
  restart (anchors verified) → exit closed; `GET /ledger/verify` →
  `{"entries":30,"valid":true}` and `{"entries":134,"valid":true}`.

```bash
cd services/clipper-network-py && python3 -m pytest -q      # count: docs/test-counts.md
export CN_SERVICE_TOKEN=<secret> CN_IDENTITY_HMAC_KEY=<secret> CN_ANDRE_APPROVAL_TOKEN=<Andre's secret>
export CN_CALLER_TOKENS='{"hub": "<>=32 chars>", "onboarding": "...", "creative_production": "...", "scheduler": "..."}'
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret> CN_DATA_DIR=<dir>
cd src && python3 -m api   # CN_BIND_ADDR (default 127.0.0.1), CN_PORT (default 8400)
```

## Finance (31) (`services/finance-py`) — Sep 26, 2026

ZBC's and ZBM's books (two entities, two charts, never mixed), ZBC's client
deposits held in a ZBC-owned restricted account titled `ZBC Client Campaign
Deposits` (never "escrow", "trust" or "FBO" — those words are refused),
creator payables accrued only from V&I certifications, the weekly payout
under maker-checker (the system proposes, Andre approves, the system
releases), clawbacks by netting only, tax records, daily reconciliation to
zero and the month-end close. Built from the locked Finance spec (rev 1): 10
deterministic single-task intelligences, no model calls, money as `Decimal`
two-decimal strings with one half-up rounding per payable. Architecture,
every choice made where the spec was silent, and the unlock list:
`docs/adr/0009-finance-department-architecture.md`. Routes and settings:
`services/finance-py/README.md`.

- **Status:** built and tested (current test count: docs/test-counts.md; `python3 -m pytest -q`; no
  network; AEGIS round 17 fixed Sep 27 -- payable identity, record-first
  sweeps, strict adapter answers, deposit returns (F1r); ADR 0009 amendment). **Not certified for any real dollar.** Rails (Stripe/Trolley),
  bank feed and transfers, tax agent, GL, vault, Clipper Network, Legal,
  People and push are fail-closed stand-ins (no rail or bank adapter code
  exists yet — only the ports); the V&I and Compliance thin clients exist
  but are unwired by default. On day one nothing accrues, activates,
  issues, reconciles or pays.
- **Controls:** double-entry journal that must balance to zero, append-only,
  hash-chained and anchored on the ledger; restricted pool ≥ creator and
  client liabilities checked before every posting; twelve release gates
  (certification, Compliance, tax status, OFAC ≤ 1 day, rail, payee
  hold/callback, minimum, velocity limits, reconciliation, treasury,
  controls, rules) re-run at release — property-tested over all 2,047
  combinations of absent inputs; release idempotency keyed per
  batch/payee/period and a payable in at most one live item; zero-tolerance
  reconciliation whose open breaks block the next run; separation of duties
  by caller identity. **With one human approver this is compensating dual
  control, not dual control** (FR's definition) until a second approver is
  named.
- **Live run** (real ledger-rust binary): production entrypoint on
  stand-ins refuses everything; then with the test fakes: prepayment →
  certification → payable 29.01 → batch proposed → Andre approves → funding
  → release after the 12 h delay → paid → clawback after payment → next
  week's earnings net it → reconciliation green → restart with anchor check;
  `GET /ledger/verify` → `{"entries":34,"valid":true}` and
  `{"entries":232,"valid":true}`.

```bash
cd services/finance-py && python3 -m pytest -q      # count: docs/test-counts.md
export FIN_SERVICE_TOKEN=<secret> FIN_ANDRE_APPROVAL_TOKEN=<Andre's secret>
export FIN_CALLER_TOKENS='{"scheduler": "<>=32 chars>", "creative_production": "...", "onboarding": "...", "clipper_network": "..."}'
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret> FIN_DATA_DIR=<dir>
cd src && python3 -m api   # FIN_BIND_ADDR (default 127.0.0.1), FIN_PORT (default 8410)
```

## Legal (37) (`services/legal-py`) — Sep 26, 2026

Keeps the document register and acceptance evidence, runs contract playbooks, tracks obligations and filings,
triages matters and litigation holds, runs the DMCA takedown desk, applies the music policy and records counsel
answers. **It never gives legal advice**: to a third party it emits only counsel-approved documents (pinned by
SHA-256), a routing notice, or dates and statuses of that party's own records; to Andre and other departments,
codes labelled `unreviewed` until a counsel memo or template id is attached. A deterministic advice-text guard
refuses and records any rendered text or template variable that reads as advice. Built from the locked Legal spec
(rev 1): 10 deterministic single-task intelligences, no model calls. Architecture and the 40 choices made where
the spec was silent: `docs/adr/0010-legal-department-architecture.md`. Routes and settings:
`services/legal-py/README.md`.

- **Status:** built and tested (current test count: docs/test-counts.md; `python3 -m pytest -q`; no network; AEGIS round 17 fixed Sep 27 --
  party-bound acceptances, server-assigned versions, IP scan, SOW counsel gate, urn memo proposals; ADR 0010
  amendment). **Not in force for any real
  document.** No counsel is engaged and the counsel channel, e-sign provider, Cybersecurity 22 and People 43 are
  fail-closed stand-ins, so on day one no document is current, no acceptance is evidence-sufficient, every
  music-bearing clip is blocked, every Creative sign-off is refused and Legal deletes nothing. ADR 0010 ends with
  the unlock list.
- **The counsel gate.** Only Andre approves (rules, playbooks, documents), and a `counsel_required` document only
  after a sign-off record whose memo cites that exact version and hash. A filed counsel memo is the only path to
  "verified", and only for what the memo cites; Compliance may only tighten. Acceptance records carry doc id,
  version, SHA-256, Legal's timestamp, method and a signer identity ref — never an IP address, never the text.
- **Fails closed.** Nothing is in force until Andre approves the pinned 20-rule seed; every seed file is SHA-256
  pinned. Every refusal cites a rule id. Every decision is recorded on the ledger (`department: legal`) and in a
  hash-chained, ledger-anchored local log before it takes effect, otherwise 503 and nothing changed; document,
  memo and paper texts live only in a content-addressed blob store, never in the log or on the ledger.
- **Other services unchanged.** The spec's "changes other services must make" are reported in ADR 0010, not made.
  Memo evidence is `urn:legal37:memos:<id>` (compliance-py refused the spec's `legal37://` form); the path is
  proven against the real compliance-py app (`tests/contract_compliance_real.py`).
- **Live run** (`devtools/live_run.py`, real ledger-rust binary, production entrypoint, a Compliance stub behind the
  real thin client): rules approved → engagement letter countersigned → memo → playbook → MSA draft → approval
  refused without a counsel record → sign-off → approval → template fill → clickwrap acceptance → obligations
  bound → memo → Compliance proposal delivered → takedown → counter-notice window → hold → restart (anchors
  verified, a tampered copy refuses to start) → `GET /ledger/verify` → `{"entries":87,"valid":true}`. 34/34 checks
  passed.

```bash
cd services/legal-py && python3 -m pytest -q      # count: docs/test-counts.md
export LEGAL_SERVICE_TOKEN=<secret> LEGAL_ANDRE_APPROVAL_TOKEN=<Andre's secret>
export LEGAL_CALLER_TOKENS='{"compliance_38": "<>=32 chars>", "hub": "...", "scheduler": "..."}'
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret> LEGAL_DATA_DIR=<dir>
cd src && python3 -m api   # LEGAL_BIND_ADDR (default 127.0.0.1), LEGAL_PORT (default 8420)
```

## Cybersecurity (22) (`services/security-py`) — Oct 5, 2026

The vault for every department's secrets, Andre's passkey approvals, short-lived service credentials, the freeze
switch (a caller, a secret, or everything), Legal's preservation holds, incidents with text/email/push alerts, and
vulnerability findings tracked to their deadlines. Founder decisions (Oct 5 Q&A), architecture and the unlock list:
`docs/adr/0012-cybersecurity-department-architecture.md`. Routes and settings: `services/security-py/README.md`.

- **Status:** built and tested (count: docs/test-counts.md). **Not in force.** No production key service is wired
  (hosting not chosen: `SEC_KMS=aws|gcp` refuses to start), no alert provider is chosen (`SEC_ALERT_*` refuses to
  start), and no department calls it yet; each consumer's port is listed in ADR 0012 with what it maps to.
- **Andre approves with a passkey or security key, nothing else.** Each approval is a WebAuthn assertion over a
  single-use challenge bound to exactly one action and body; clone detection by signature counter; first passkey
  by a one-time enroll token, every later one approved by an existing one; a recovery procedure for lost keys.
- **Fails closed.** Envelope encryption (AES-256-GCM per secret, data key wrapped by the key service, context-bound);
  every log line anchored on the ledger before it is written, every release recorded before it is returned; a
  truncated, rolled-back or replaced log stops all releases. No value ever reaches the log, the ledger, an error,
  an alert or the audit export.
- **Live run** (`devtools/live_run.py`: real ledger-rust binary, production entrypoint, software passkeys): enrol
  two passkeys → store, use, rotate → refusals → service credential verified → freeze and lift → Legal hold →
  restart → truncated log detected → `GET /ledger/verify` valid.

```bash
cd services/security-py && python3 -m pytest -q   # count: docs/test-counts.md
LEDGER_BIN=services/ledger-rust/target/release/server python3 services/security-py/devtools/live_run.py
```

## Lead Generation (26) + Sales (27) (`services/sales-py`) — Oct 5, 2026

One service for both brands, ZBM and ZBC: leads from four sources (inbound, referral / partner, public data, paid
provider) deduped, scored and routed; the pipeline (accounts, contacts, leads, opportunities, activities, tasks) on
the ledger with no external CRM; cold email under CAN-SPAM from a separate outreach domain; texts and calls only with
recorded consent inside TCPA quiet hours; the two price books; proposals; won deals handed to Onboarding and Finance.
Recruiting clippers is not here (Clipper Network). Founder decisions (Oct 5 Q&A), architecture and the unlock list:
`docs/adr/0013-sales-leadgen-department-architecture.md`. Routes and settings: `services/sales-py/README.md`.

- **Status:** built and tested (count: docs/test-counts.md). **Not in force.** No send provider, lead source or
  department client is wired: email, SMS and voice stay queued, imports answer `SOURCE_NOT_WIRED`, proposals cannot
  be sent (`LEGAL_UNAVAILABLE`) and won hand-offs stay `pending_delivery`. Setting any unbuilt provider or client
  switch refuses start.
- **Andre approves** template versions and proposals by content hash, and every price (each price-book line exists
  with no price until he approves one). Agents send a proposal on their own only at approved list prices, with no
  media buy, discount or custom term, and only while it and the opportunity's other live proposals stay within
  $10,000; everything else waits for him.
- **Outreach fails closed.** Cold email only from an Andre-approved template whose hash still matches, with the
  postal address and a one-click unsubscribe added by the service, at a warm-up pace per outreach domain. One
  suppression list across both brands with no removal path. Texts and calls only to +1 numbers with express consent
  for that phone, channel and brand, 08:00-21:00 in the recorded zone and every zone of the area code; any inbound
  reply (except an exact machine auto-reply) holds texts and calls to the contact until Andre decides it. Emails and phones are keyed hashes in dedupe,
  suppression and the audit export.
- **Record-first log** (security-py's design as fixed in ADR 0012): every send, consent change, suppression and
  approval is a typed ledger event before its log line, and each line is anchored before it takes effect; ledger
  down = nothing happens; a truncated, replaced or edited log stops all writes.
- **Live run** (`devtools/live_run.py`: real ledger-rust binary, production entrypoint): second process on the same data
  directory refused → inbound, referral and duplicate leads → imports refused → template approval → cold email queued, not sent → SMS refused without consent
  and in quiet hours → opt-out suppressing across both brands → quote and approval rules → send refused while Legal
  is a stand-in → restart → forged pending line inert → truncated log detected →
  `GET /ledger/verify` valid.

```bash
cd services/sales-py && python3 -m pytest -q   # count: docs/test-counts.md
LEDGER_BIN=services/ledger-rust/target/release/server python3 services/sales-py/devtools/live_run.py
```

## Customer Service (30) + Client Success (29) (`services/service-py`) — Oct 5, 2026

One desk for both brands, ZBM and ZBC: one conversation per client across email, chat, SMS and phone; routine
questions answered right away, but only with answers Andre approved; money, contracts and complaints sent to Andre
(and Legal, Finance, Cybersecurity, Compliance where they apply); the consent registry; SLA timers; a health score per
account and a save plan for an account at risk. Founder decisions (Oct 5 Q&A), architecture and the unlock list:
`docs/adr/0014-customer-service-success-department-architecture.md`. Routes and settings:
`services/service-py/README.md`.

- **Status:** built and tested (count: docs/test-counts.md). **Not in force.** No email, SMS, chat-push, voice or
  alert provider is chosen, so outbound messages stay queued (visibly) and alerts are recorded, not sent
  (`SVC_VOICE_PROVIDER` refuses start). Only the Legal (37) handoff has a client; Finance (31), Cybersecurity (22),
  Compliance (38), the results-trend source and contract end dates are ports with fail-closed stand-ins.
- **Nothing generated.** No model calls: deterministic single-task intelligences (triage, approved answers, health
  score). The bot sends only a knowledge-base article Andre approved, byte for byte, re-hashed at match time; an
  ambiguous match goes to Andre. Privacy, security, contract, money and complaint messages (one keyword is enough)
  are never answered by the bot. Save-plan offers come only from Andre's approved catalogue, by id.
- **Consent and channels.** SMS outbound only with recorded express consent, 08:00-21:00 in the recipient's zone; a
  STOP revokes at once and START does not restore it. Proactive email needs email consent. Phone is never an
  outbound channel; no bot answers a call.
- **Record-first log** (security-py's design as fixed in ADR 0012): each answer, escalation, consent change,
  approval, alert and SLA breach is a typed ledger event (ids, codes and hashes only) before its log line; message
  bodies are stored once by content hash and never reach the log line, the ledger, an alert or an error; a
  truncated, replaced or edited log stops every write.
- **Live run** (`devtools/live_run.py`: real ledger-rust binary, production entrypoint): an approved answer sent and
  an unapproved one never → a refund, a lawyer and a privacy request escalated → SMS refused without consent, STOP
  revoking it → an at-risk account alerted with a save plan that accepts only an approved offer → restart →
  truncated log detected → nothing personal on the ledger → `GET /ledger/verify` valid.

```bash
cd services/service-py && python3 -m pytest -q   # count: docs/test-counts.md
LEDGER_BIN=services/ledger-rust/target/release/server python3 services/service-py/devtools/live_run.py
```

## Influencer & Partnership Marketing (11) (`services/influencer-py`) — Oct 6, 2026

One service for both brands, ZBM and ZBC: finds creators (their own applications, profiles a person researched, and
later a paid influencer database and public-profile data through ports that are not built; no scraping code); reaches
them by outreach email from a separate domain under CAN-SPAM, and by platform DMs only as Andre approved each one;
runs influencer campaigns and campaign-level co-marketing with brands and agencies; issues sponsored-content briefs
that always carry an FTC disclosure; makes deals; sends contracts through Legal (37); and asks Finance (31) to pay
verified creators. Referral and alliance partner commissions are department 12's. Founder decisions (Oct 6 Q&A),
architecture and the unlock list: `docs/adr/0015-influencer-partnership-department-architecture.md`. Routes, settings
and the hub contract: `services/influencer-py/README.md`. Env prefix `INF_`, default port 8480.

- **Status:** built and tested (count: docs/test-counts.md). **Not in force.** No send provider, DM provider,
  discovery source or department client is wired: email and approved DMs stay queued, imports answer
  `SOURCE_NOT_WIRED`, contracts `LEGAL_UNAVAILABLE`, payee verification `FINANCE_UNAVAILABLE`, and nothing is ever
  paid. Setting any unbuilt provider or client switch refuses start.
- **Andre approves** template versions, every DM draft, briefs, deals over the limit and final content, each by its
  content SHA-256, and decides reply holds and minor reviews. A deal goes to him when it, or the same person's
  lifetime total across both brands and every campaign, or the campaign's total, is over $5,000; the rule is applied
  again at payout time, keyed on the payee.
- **Fails closed.** Creators attest to being 18 or older (no date of birth is ever accepted; a declared minor naming a
  record we hold freezes it for Andre's review). The mailbox is confirmed before an application is taken. A raw TIN,
  SSN or EIN is refused 422 by key and by shape; only a provider reference is kept. Any reply on any channel holds
  every further outreach to that creator until Andre decides; one suppression list covers both brands, email and
  DMs. Content without the exact disclosure up front is refused, and counts live only once Andre approved it and the
  contract is in force.
- **Record-first log** (security-py's design as fixed in ADR 0012, with service-py's closed-instance and claim rules
  from ADR 0014): every change is a typed ledger event and an anchored log line before it takes effect; emails and
  handles reach the ledger and the export only as keyed hashes; a truncated, replaced or edited log stops all writes.
- **Open unlock items** (ADR 0015): the email provider, its webhooks and DNS, and the hub's unsubscribe page; DM
  providers; the discovery sources; the Legal (37) contract client (legal-py needs an influencer-agreement document
  type); the Finance (31) payee and payout client, which must confirm a `stripe:` / `vault:` tax reference exists and
  belongs to the creator before any payee is registered, and must return its `person_key` (an opaque keyed hash of
  the matched TIN) on `register_payee` and `payee_status`; the hub contract (per-IP and per-session rate limits and a
  CAPTCHA on the public forms, `requester_key`, the `/u/<token>` and `/c/<token>` pages); passkey approvals through
  Cybersecurity (22); an un-block path for a confirmed minor who later turns 18; a CCPA erasure design; a route to
  change a creator's email.
- **Live run** (`devtools/live_run.py`: real ledger-rust binary, production entrypoint): start-up integrity → second
  process on the same data directory refused → address-first application, link and session → minor and missing
  attestation refused → date of birth and raw TIN refused → discovery not wired → template approval → outreach email
  queued, not sent → DM approved by hash and still queued → any reply holds, Andre lifts it → opt-out suppressing
  both brands and DMs → brief with its FTC section → the $5,000 rule with a split caught → deal and content approval
  by hash → contract refused while Legal is a stand-in → tax reference inside a session only → Finance refusing →
  restart → forged pending line inert → truncated log detected → nothing personal on the ledger →
  `GET /ledger/verify` valid.

```bash
cd services/influencer-py && python3 -m pytest -q   # count: docs/test-counts.md
LEDGER_BIN=services/ledger-rust/target/release/server python3 services/influencer-py/devtools/live_run.py
```

## New Business Development (12) (`services/bizdev-py`) — Oct 6, 2026

One service for both brands, ZBM and ZBC, for two jobs: big pursuits (enterprise and government bids, RFP / RFQ
responses and formal pitches, as multi-month pursuits with stages, deadlines, bid / no-bid qualification and win /
loss) and partnerships (referral partners, agency alliances and white-label deals, with commissions paid through
Finance). Everyday leads and deals stay with Sales (27); influencer and co-marketing work is department 11; political
work and public affairs are department 14. Founder decisions (Oct 6 Q&A), architecture and the unlock list:
`docs/adr/0016-new-business-development-department-architecture.md`. Routes and settings:
`services/bizdev-py/README.md`. Env prefix `NBD_`, default port 8490.

- **Status:** built and tested (count: docs/test-counts.md). **Not in force.** No provider or department client is
  wired: outreach email, submissions and partner payouts stay `queued`, won pursuits' hand-offs stay
  `pending_delivery`, bid-portal import answers `SOURCE_NOT_WIRED`, and every Legal (37) agreement hand-off is refused
  `LEGAL_UNAVAILABLE`, so no partner deal can be marked won yet. Nothing is ever paid. Setting any unbuilt provider or
  client switch refuses start.
- **Andre decides.** The AI drafts; every response and pitch is assembled only from approved boilerplate plus
  approved custom text and needs Andre's approval of its exact hash. The `bid` decision, deadline moves, wins,
  partner rates, payees and hold decisions are his alone. Any pursuit or partner deal over $10,000 goes to him,
  aggregated over every deal sharing a counterparty ref, registrable domain or normalised name, so a split deal
  cannot get under the threshold.
- **Fails closed.** A submission past its stored deadline is refused (judged on the service's injected clock, never
  the wall clock); an outcome that is not known stays `sending` and is never resent. Government bids carry required
  certifications that only Andre attests, one item at a time by hash; conflict-of-interest, gift, lobbying and
  contingent-fee items are flagged to him. Commission accrues only on money Finance reports the client actually paid,
  with clawback on refunds and chargebacks; partner tax information is a reference only, and a raw TIN / SSN / EIN is
  refused 422. Outreach is email only, and any reply holds further outreach until Andre decides.
- **Record-first log** (security-py's design as fixed in ADR 0012, with service-py's lifecycle from ADR 0014): typed
  evidence on the ledger first, then the anchored log line; no email, name, amount, rate or reply text reaches the
  ledger; a truncated, replaced or edited log stops all writes.
- **Open unlock items** (ADR 0016): passkey approvals through Cybersecurity (22); the email provider and its webhook
  relay; submission delivery; bid-portal sourcing; the Onboarding, Finance (31) and Legal (37) clients (Finance needs
  a partner payee kind and a payout intake for partner commissions; legal-py needs intake kinds for partner
  agreements, NDAs and white-label contracts); a suppression list shared with Sales (27); the console pages; minted
  caller credentials.
- **Live run** (`devtools/live_run.py`: real ledger-rust binary, production entrypoint): an unbuilt provider setting
  refuses start → start-up integrity → second process refused → the bid decision is Andre's → response approval by
  hash, submission still queued → a submission past its deadline refused → a government bid refused until every item
  is attested, sensitive items flagged → a split deal aggregated over $10,000 → a raw SSN refused → partner rate
  Andre's → agreements and partner wins refused while Legal is a stand-in → no commission without a won deal →
  outreach queued, a reply holds → restart → truncated log detected → nothing personal on the ledger →
  `GET /ledger/verify` valid.

```bash
cd services/bizdev-py && python3 -m pytest -q   # count: docs/test-counts.md
LEDGER_BIN=services/ledger-rust/target/release/server python3 services/bizdev-py/devtools/live_run.py
```

## Client Delivery & Operations (28) (`services/delivery-py`) — Sep 27, 2026, fix waves 20-26b applied

The AEGIS fix engine and the agent runtime adapters around the pinned deer-flow harness (`345f08be`, v2.1.0;
Superpowers `8ca22dba` prompt texts forked as ours). A findings document goes in; the ENGINE opens a
`fix<N>-<service>` worktree, runs one agent engineer per finding inside our Docker sandbox (`--network none`, no
`.git`, no `curl`) under our guardrail, and computes every verdict in a FRESH container the agent never had a
process in (fix wave 20): the failing test, the passing test, a split-diff verification (base + the agent's
source changes + its RED test file alone must pass; base + the RED test alone must fail; the whole fix minus the
finding's file must fail; the finding's own reproduction must pass with the fix and fail without it) and the whole
suite on the exact tree it commits, with its own configuration, an engine-owned pytest plugin and a per-ecosystem
result it cross-checks (pytest junit + collect-only + the plugin record; `go test -json` + `-list`; `cargo test`
lines + `-- --list`; `node --test` junit + TAP); a test that was passed/failed at baseline and is skipped or
missing afterwards fails the run. It commits, writes the report from its records and hands the run to AEGIS
re-review — the engine never claims a finding is fixed (wave 23: its end state is `candidate_passed_checks`,
checks passed, necessary not sufficient; only an AEGIS review with a verdict per finding makes one `accepted`, and
every added source line that can observe the execution context is a review flag at the top of the report), nothing
the agent's process prints is ever a count, and `git
push`, merges, network, deletion outside the service directory, ACP/MCP and self-modification are denied
unconditionally. Architecture, pins, the 28 choices, the round-18 and round-19 amendments and the spec defects:
`docs/adr/0011-delivery-department-architecture.md`. Routes and settings: `services/delivery-py/README.md`.

- **Status:** built and tested (fix wave 23, c71e363: 651 tests — 647 passed, 4 skipped with the printed
  reason — on Python 3.13.13 and on 3.12.3; at that commit a run left four `dlv-git-*` isolation dirs and one
  `go-build*` dir in `/tmp` itself, from processes started with a scrubbed environment and ended by a signal.
  Since then: wave 24 gives every child the suite's `TMPDIR` (on the env allowlist), so such dirs land in the
  session's temp root, which the suite removes; wave 25 creates `dlv-git-*` at first use, not at import (C6-3);
  a process killed by a signal after its first git command still leaves its `dlv-git-*` dir in its `TMPDIR`
  (OPEN.md C6-3-res); the CI jobs run every suite under the hygiene check, which fails any leftover. Wave 21:
  every finding must name a runnable reproduction — an existing test or a reviewer-authored
  `reproduction_test` that fails on the starting tree — or is refused 422, and since wave 22 that RED check is
  part of admission (wave 23: its containers run outside the service lock, the service's run slot reserved), and
  the reproduction must also hold OUTSIDE the test runner — a reproduction whose TEST needs pytest ends
  `needs_review_runner_dependent`, flagged for the reviewer). The loop is
  proven end to end with a deterministic model against the `fixtures/dlv/toy-py`, `toy-rs`, `toy-go` and `toy-ts`
  fixtures (the real `cargo`, `go` and `node`) through the REAL harness and guardrail; every round-18 attack (conftest monkeypatch, forged summary, neutered `pytest.ini`, deleted test,
  trivial `DISPROOF:`, hung or flooded suite) and every round-19 route (a fix in a new module the test imports, a
  pytest plugin registered from inside the test, a reproduction skipped under `CHANGED_TEST:`, a cancel after the
  FIXED turn, multi-line bash, file-tool writes through symlinks) now ends the run `failed` with the defect intact
  and uncommitted. **Docker live: not provable on the build box** (no daemon; `tests/test_live_docker.py` lists the
  properties only a daemon can prove); the CI job `delivery-docker-live` builds the sandbox image and runs them
  (fix wave 26a; its image build failed on CI #3 and was fixed in 26b — not yet proven green). **Not
  certified for a fix run against `main`.** pip-audit not run (tool absent); image digests are required build
  arguments. Go, Rust (stable, no nightly/nextest) and Node 22 services now have engine-owned verdicts (Node has no
  collect-only mechanism: stated in the ADR); a service outside `services/<name>/` (`apps/dashboard-ts`) is still
  outside the findings document's path grammar.
- **Fails closed.** Refuse-to-start on any pinned hash (no unpinned mode exists), any tampered prompt/skill/seed,
  any forbidden module, a deer-flow commit other than the pin, any env name outside the allowlist, a tag-only image,
  a sandbox network other than `none`, subagents turned on, a licence outside the allowlist (incl. `*.egg-info` and
  vendored packages without a record), a repository with remotes. No Docker daemon or no provider key → every run
  503, nothing queued. Every transition is a ledger event (department `delivery`) and a hash-chained,
  ledger-anchored local log line before it takes effect; an unverifiable test result is `unknown`, never green.

```bash
cd services/delivery-py && uv sync --frozen && .venv/bin/python -m pytest -q     # count: docs/test-counts.md (cargo, go, node on PATH)
```
