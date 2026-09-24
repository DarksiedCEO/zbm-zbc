# ZBM/ZBC — Revenue Recovery 1A

First real code in the `zbm-zbc` monorepo. Built Sep 21–22, 2026, then
hardened Sep 22, 2026 against a real independent review. Everything below
has been actually run and verified in this build session — not just
written. See `docs/adr/0001-revenue-recovery-1a-architecture.md` for the
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
| `services/detection-py` | Python (FastAPI, pydantic) | 404/404 passing | Real, REST-exposed, hardened, money is exact `Decimal` |
| `services/orchestrator-go` | Go | 48/48 passing | Real, live-tested against detection-py + ledger-rust, hardened, string-backed `Money` |
| `services/ledger-rust` | Rust | 91/91 passing (60 unit + 31 real-binary integration), clippy `-D warnings` clean | Real, hash-chained, tamper-evidence proven by test, authenticated, findings + events on one chain |
| `apps/dashboard-ts` | TypeScript (Next.js 16) | 14/14 `npm test` (money vectors, loopback bind, error sanitizing, ledger status), build + typecheck clean, 0 npm audit vulnerabilities | Real, rendered per request (`ƒ /`), binds 127.0.0.1 by default, reads recorded findings — viewing never writes |

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
  server. Known limitation: a non-ASCII byte in any request header gets an
  empty reply (tiny_http drops it before our code runs; fail-closed, pinned
  by test). Fix wave 2: new appends enforce the money bound (ADR 0003
  section 1a; over `"999999999999999.99"` is a 400), checked against every
  `ledger_append_expected` verdict in `fixtures/money_vectors.json`;
  already-persisted amounts (legacy `1e20`, over-bound strings the
  fix-wave-1 binary accepted) still load and verify. `cargo test`: 84
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

**Verified Sep 24 2026:**
- `python3 -m pytest -q` (detection-py): 138 passed.
- `go vet ./...` clean; `go test ./...` (orchestrator-go): 21 passed.
- `cargo test` (ledger-rust): 55 passed (39 unit, 8 `server_auth`, 8
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

Requires: Python 3.11+, Go 1.24+, Rust/cargo, Node 22+.

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
cd src && python3 -m uvicorn api:app --port 8000

# 3. Orchestrator (Go) — separate terminal. Needs the SAME token values
#    as the two services it calls, plus its own token for callers of it.
cd services/orchestrator-go
export DETECTION_SERVICE_TOKEN=<same value as ZBM_SERVICE_TOKEN above>
export LEDGER_SERVICE_TOKEN=<same value as LEDGER_SERVICE_TOKEN above>
export ORCHESTRATOR_SERVICE_TOKEN=<your-shared-secret, for callers of THIS service>
go run ./cmd/orchestrator  # DETECTION_SERVICE_URL, LEDGER_SERVICE_URL, ORCHESTRATOR_PORT

# 4. Dashboard (TypeScript) — separate terminal
cd apps/dashboard-ts
npm install
ORCHESTRATOR_URL=http://localhost:8080 ORCHESTRATOR_SERVICE_TOKEN=<same as above> npm run dev   # http://127.0.0.1:3000
# `npm run build && npm start` for production. Both bind 127.0.0.1 (the
# dashboard has no auth); DASHBOARD_BIND_ADDR overrides, PORT sets the port.
# The page shows findings already recorded in the ledger (read-only);
# run a scan first with the POST below. `npm test` runs the money vectors.
```

Or just run the full pipeline once without the dashboard:
```bash
curl -s -X POST -H "Authorization: Bearer $ORCHESTRATOR_SERVICE_TOKEN" \
  http://localhost:8080/revenue-recovery/scan | python3 -m json.tool
```

## Testing

```bash
# Python — 404 tests. Use `python3 -m pytest`, not the bare `pytest`
# binary, if pytest was installed as a standalone tool (e.g. via uv) —
# it can silently run against a different interpreter than the one you
# `pip install`ed into, and report a module-not-found collection error
# that looks like a broken test suite rather than an environment mismatch.
cd services/detection-py && python3 -m pytest -q

# Go — 48 tests
cd services/orchestrator-go && go vet ./... && go test -count=1 ./...

# Rust — 91 tests (60 unit + 31 integration; the integration tests spawn
# the real compiled binary and talk to it over a real TCP socket — see
# tests/server_auth.rs, tests/server_events.rs, tests/server_hardening.rs)
cd services/ledger-rust && cargo test && cargo clippy --all-targets -- -D warnings

# Dashboard — 14 tests
cd apps/dashboard-ts && npm ci && npx tsc --noEmit && npm run build && npm test && npm audit
```

## Data

Everything runs against `fixtures/*.json` — a shared, hand-built pool of
orders, customers, subscriptions, and Tier 2 domain data with deliberate
leak cases AND control cases (so every agent proves it doesn't just flag
everything). **Explicitly non-live.** No real store is connected. See
Decision 6 in `revenue-recovery-founder-decisions.md` for why, and the
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

- **Status:** built and tested (274 tests, `python3 -m pytest -q`). **Not
  certified for any real client, clipper or brand.** Scenario, attack and
  guardrail tests exist. The AEGIS review findings were fixed in fix wave 1
  (Sep 24); see ADR 0004.
- **Fails closed.**
  - Activation needs both the Contract (14) and Compliance (15) gates, and a
    blocked activation returns the exact unmet list.
  - Every decision is written to the ledger (`POST /ledger/events`, department
    `onboarding`) before any outside effect, such as a payout or handoff. If the
    write fails, the action doesn't happen and the API says so (503). If it
    fails after an effect, the API names the effect. Retries are idempotent.
  - Only the server's clock decides. Clipper 18+ is checked on the server's
    date at UTC−12. Credentials are refused at intake. Escalation decisions
    need Andre's own key.
  - Compliance 38, the vault, platform APIs, push to Andre and contract
    storage are stand-ins that answer "not allowed yet" / "not wired".
- **Reuses Revenue Recovery** over HTTP (detection-py's real routes). It never
  re-implements a detection agent.
- **Run:** `cd services/onboarding-py/src && ONBOARDING_SERVICE_TOKEN=... python3 -m api`
  - Binds 127.0.0.1:8200 by default.
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

```bash
cd services/creative-py && python3 -m pytest -q      # 310 tests
export CREATIVE_SERVICE_TOKEN=<secret> CREATIVE_ANDRE_APPROVAL_TOKEN=<other secret>
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret>
cd src && python3 serve.py   # CREATIVE_BIND_ADDR (default 127.0.0.1), CREATIVE_PORT (default 8300)
```

Details, live-run evidence and gaps: `services/creative-py/README.md`;
decisions: `docs/adr/0005-creative-production-architecture.md`.
