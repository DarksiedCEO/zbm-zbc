# ZBM/ZBC — Revenue Recovery 1A

First real code in the `zbm-zbc` monorepo. Built Sep 21–22, 2026.
Everything below has been actually run and verified in this build session —
not just written. See `docs/adr/0001-revenue-recovery-1a-architecture.md`
for the full architecture rationale and scope boundary (Tier 3 excluded,
see that doc for why).

## Status

**8/8 detection agents (Tier 1 A–D, Tier 2 E–H) + full trust/safety net +
3-service pipeline (Python → Go → Rust) + TypeScript dashboard, all real,
all tested, all verified running together end-to-end.**

| Service | Language | Tests | Status |
|---|---|---|---|
| `services/detection-py` | Python (FastAPI, pydantic) | 61/61 passing | Real, REST-exposed |
| `services/orchestrator-go` | Go | 4/4 passing | Real, live-tested against detection-py + ledger-rust |
| `services/ledger-rust` | Rust | 6/6 passing | Real, hash-chained, tamper-evidence proven by test |
| `apps/dashboard-ts` | TypeScript (Next.js 16) | build + typecheck clean, 0 npm audit vulnerabilities | Real, server-rendered against a live orchestrator |

**Verified live, full-stack run** (Python + Go + Rust + TypeScript, real
processes, real HTTP, no mocks): 8 agents run, 10 findings produced, all
10 written to the tamper-evident ledger, ledger integrity verified valid,
dashboard server-rendered every one of them including the Decision 3
double-claim warning banner on `ord_1007`.

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

```bash
# 1. Detection service (Python)
cd services/detection-py
pip install -r requirements.txt
cd src && python3 -m uvicorn api:app --port 8000

# 2. Evidence ledger (Rust) — separate terminal
cd services/ledger-rust
cargo run --bin server   # LEDGER_PORT env var, default 8090

# 3. Orchestrator (Go) — separate terminal
cd services/orchestrator-go
go run ./cmd/orchestrator  # DETECTION_SERVICE_URL, LEDGER_SERVICE_URL, ORCHESTRATOR_PORT

# 4. Dashboard (TypeScript) — separate terminal
cd apps/dashboard-ts
npm install
ORCHESTRATOR_URL=http://localhost:8080 npm run dev   # http://localhost:3000
```

Or just run the full pipeline once without the dashboard:
```bash
curl -s http://localhost:8080/revenue-recovery/scan | python3 -m json.tool
```

## Testing

```bash
# Python — 61 tests
cd services/detection-py && python3 -m pytest tests/ -v

# Go — 4 tests
cd services/orchestrator-go && go test ./... -v

# Rust — 6 tests
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
- Ledger server (`ledger-rust`) is single-process, in-memory — restarting
  it loses the chain. No persistence layer yet.
- Dashboard has no write actions, no auth, no multi-client view — it
  renders one scan result, nothing more.
- Tier 2's `escalator`/`overage_rate` contract-drift directionality
  rules are not implemented (only `minimum_spend` shortfall detection is
  real) — flagged in code rather than guessed at with no fixture behind it.
