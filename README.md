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
pass (below) against six real, independently-flagged gaps, five of them
fixed and confirmed live, one (per-store leak thresholds/currency) still
open by design decision, not oversight.**

| Service | Language | Tests | Status |
|---|---|---|---|
| `services/detection-py` | Python (FastAPI, pydantic) | 72/72 passing | Real, REST-exposed, hardened |
| `services/orchestrator-go` | Go | 8/8 passing | Real, live-tested against detection-py + ledger-rust, hardened |
| `services/ledger-rust` | Rust | 19/19 passing | Real, hash-chained, tamper-evidence proven by test, now authenticated |
| `apps/dashboard-ts` | TypeScript (Next.js 16) | build + typecheck clean, 0 npm audit vulnerabilities | Real, server-rendered against a live orchestrator |

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
| 6 | Money (`amount_usd`, `order_value_usd`, `unit_price_usd`, etc.) is `float` throughout `zbm_schema`, used in real arithmetic across 7 agent files — unlike `fulfillment-py`'s single unused `Decimal`-converted field, this is live, load-bearing math. | **Not fixed — flagged, not silently converted** | Confirmed real (grep-verified 39 arithmetic/comparison sites across 7 files). Deliberately NOT converted in this pass: swapping `float` → `Decimal` here touches every agent's math and every test's literals, a materially different blast radius than fulfillment-py's isolated stub field, and doing that silently risked introducing regressions across the whole Tier 1 detection layer without the founder having signed off on that scope. Tracked below as an open gap, not fixed quietly and not ignored. |
| 7 | `orchestrator-go` called `http.ListenAndServe(":"+port, mux)` with no host — same class of bug as `ledger-rust`'s #3, binding every network interface instead of just localhost. Found after the initial review pass, in a later session. | **Fixed** | `ORCHESTRATOR_BIND_ADDR` env var, defaults to `127.0.0.1`. Confirmed live by reading the actual bound socket from `/proc/net/tcp` (`0100007F:...`, not `00000000:...`), same method as #3. `cmd/orchestrator` had zero test coverage before this — 2 new tests build and spawn the real compiled binary and connect over a real socket, the same gap in coverage that let #3/#4/#7 all ship with green test suites. |

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

**All three backend services now fail closed and refuse to start without
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
ORCHESTRATOR_URL=http://localhost:8080 npm run dev   # http://localhost:3000
```

Or just run the full pipeline once without the dashboard:
```bash
curl -s -X POST -H "Authorization: Bearer $ORCHESTRATOR_SERVICE_TOKEN" \
  http://localhost:8080/revenue-recovery/scan | python3 -m json.tool
```

## Testing

```bash
# Python — 72 tests. Use `python3 -m pytest`, not the bare `pytest`
# binary, if pytest was installed as a standalone tool (e.g. via uv) —
# it can silently run against a different interpreter than the one you
# `pip install`ed into, and report a module-not-found collection error
# that looks like a broken test suite rather than an environment mismatch.
cd services/detection-py && PYTHONPATH=src python3 -m pytest tests/ -v

# Go — 8 tests
cd services/orchestrator-go && go test ./... -v

# Rust — 19 tests (11 unit + 8 integration; the integration tests spawn
# the real compiled binary and talk to it over a real TCP socket — see
# tests/server_auth.rs)
cd services/ledger-rust && cargo test
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
- **Money is `float`, not `Decimal`, throughout `zbm_schema`** — confirmed
  real by review (finding #6 above), live arithmetic in 7 agent files,
  deliberately not converted this pass given the blast radius. Real risk
  once real currency flows through this, not yet fixed.
- Dashboard has no write actions, no auth, no multi-client view — it
  renders one scan result, nothing more.
- Tier 2's `escalator`/`overage_rate` contract-drift directionality
  rules are not implemented (only `minimum_spend` shortfall detection is
  real) — flagged in code rather than guessed at with no fixture behind it.
- Single shared-secret bearer tokens across all three services, not a
  real auth system (no per-caller identity, no rotation, no scoping) —
  adequate for a private network, not for anything internet-facing.
- This is one review pass (Sep 22 2026). A second, independent reviewer
  looking at the same code might find different things — see
  `fulfillment-py`'s README for the same caveat stated about that build.

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
Andre's final approval and no ZBC clip is payout-eligible.

```bash
cd services/creative-py && python3 -m pytest -q      # 205 tests
export CREATIVE_SERVICE_TOKEN=<secret> CREATIVE_ANDRE_APPROVAL_TOKEN=<other secret>
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret>
cd src && python3 serve.py   # CREATIVE_BIND_ADDR (default 127.0.0.1), CREATIVE_PORT (default 8300)
```

Details, live-run evidence and gaps: `services/creative-py/README.md`;
decisions: `docs/adr/0005-creative-production-architecture.md`.
