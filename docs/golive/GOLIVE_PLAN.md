# Go-live plan: first paying Shopify Revenue Recovery client

Status: **DRAFT for founder review.** Written Oct 4–5, 2026 from a read-only survey of `integration-2026-09-24` @ 013caff.
Sources: `_SURVEY.md` (per-service facts, file:line cited), `API_SURFACE.md`, `COUNSEL_PACKET.md`, `REPO_CHECK.md`.
Every size below is an **estimate** in focused build days (agent-assisted build plus AEGIS review), not a commitment.

---

## 0. The three facts that change the plan

1. **Nothing in the repo can apply a fix to a client's store.** `delivery-py` (Dept 28) is a *code-fix engine for this
   monorepo*: it fixes `services/<service>/` source in a Docker sandbox with network off and push denied
   (`services/delivery-py/README.md:7-12,48-50`; ADR 0011:27-33). The piece that would change a store is detection-py's
   Action Execution path, which is "SCOPED, NOT LIVE" and refuses everything
   (`services/detection-py/src/safety/action_execution.py:1-21`). The founder's Sep 26 condition, "won't launch until the
   fix piece works end to end", therefore refers to a component that **does not exist yet**, not one that needs finishing.
2. **There is no Shopify connection of any kind:** no OAuth install, no Admin API client, no translation of store data into
   the detection schema (`services/detection-py/src/zbm_schema/__init__.py:17-21`). Every scan runs on repo fixtures and
   is stamped `non_live_data_source: true` (`services/orchestrator-go/internal/orchestrator/orchestrator.go:35-38,175`).
3. **There is no way to collect money and no Revenue Recovery price.** No payment SDK anywhere; card is refused by
   design (ADR 0009 D11); "issue invoice" only changes a status (`services/finance-py/src/svc_books.py:388-400`); and
   ZBM's invoice line codes have no Revenue Recovery or performance-fee code; the closest are `retainer_fee` and
   `strategy_services` (`services/finance-py/src/models.py:348-350`).

Correction to earlier chat estimates: "1–3 weeks" for go-live assumed a store writer and Shopify connector existed in
part. They don't. The fully automated path below is **~9–14 weeks** of build, with counsel in parallel.

---

## 1. Two ways to the first dollar

### Track A: full platform, as specified (the founder's stated standard)
Client installs → platform scans the real store → client signs → platform applies fixes (trust-graduated) →
finance invoices → cash collected. Nothing manual (founder, Sep 26).

### Track B: pilot (proposal; **conflicts with two founder rules**, so needs a ruling)
Merchant installs a **read-only** Shopify app → platform scans the real store → ZBM delivers the findings report →
the merchant (or ZBM by hand, inside the merchant's admin) applies the fixes → flat audit fee paid via an
off-the-shelf invoice. Conflicts with: "nothing manual in Revenue Recovery" and "won't launch until the fix engine works
end to end" (Sep 26). Benefit: first revenue and the first real-store test of detection in **~3–5 weeks**, and every
piece it needs is also step one of Track A, so nothing is thrown away.

**Recommendation:** run Track B as the first milestone of Track A. It proves detection on live data (it has only ever
seen fixtures) before investing in the riskiest piece, the store writer. The decision is the founder's.

---

## 2. Work items

Legend: **A** = needed for Track A, **B** = needed for Track B. Order is the build order.

| # | Item | Exists today (cite) | Missing | Est. | Track | Depends on |
|---|---|---|---|---|---|---|
| 1 | **Repo visibility** | `zbm-zbc` is **public** (REPO_CHECK.md) | Founder decision to make private | 0 | A,B | — |
| 2 | **Deployment base**: one cloud host, TLS reverse proxy, process supervision, durable volume, backups, logs | Services bind 127.0.0.1; no compose/IaC; only delivery-py has Dockerfiles (`_SURVEY.md` §a) | Everything | 4–6 | A,B | Hosting choice (D3) |
| 3 | **Secrets store**: minimal vault for service tokens + per-merchant Shopify tokens | All vaults are `RefusingVault`/ports (`onboarding-py/src/integrations/vault.py:31-39`, finance `FIN_VAULT` refuses) | A real backend (cloud secret manager or KMS-encrypted table) behind the existing vault ports | 3–5 | A,B | 2 |
| 4 | **Persistence** (see ADR draft) | Ledger: real fsync'd JSONL. 6 services: JSONL when `*_DATA_DIR` set. Onboarding, fulfillment, creative: memory only | Durable onboarding state; `*_DATA_DIR` on a backed-up volume; client dimension | 5–8 | A (B: 2) | 2, ADR decision |
| 5 | **Client/tenant dimension** in the scan path | `client_id` in onboarding/finance/legal only; detection orders, orchestrator scan, ledger chain have none (`_SURVEY.md` §c) | `client_id`/`shop_id` through orchestrator → detection → ledger entries | 3–5 | A,B | 4 |
| 6 | **Shopify app + install** (Dev Dashboard app, OAuth, scopes, token to vault) | Nothing; onboarding stores OAuth *metadata only* (`onboarding-py/src/onboarding_schema/__init__.py:239-245`) | App registration, OAuth callback service, scope set, uninstall/GDPR webhooks | 4–6 | A,B | 2, 3 |
| 7 | **Shopify ingestion + translation layer** → `zbm_schema` (orders, abandoned checkouts, discounts, products) | Schema is ready and platform-agnostic (`detection-py/src/zbm_schema/__init__.py:17-21`) | Admin GraphQL client, pagination/rate-limit handling, mapping, Decimal money, fixtures from a dev store | 6–10 | A,B | 6 |
| 8 | **Orchestrator live data source** | Fixture-only, `non_live_data_source: true` hard-coded (`orchestrator.go:35-38,175`) | Scan takes `client_id`, reads from 7, keeps fixture mode for tests | 2–3 | A,B | 5, 7 |
| 9 | **Findings report the client sees** | Dashboard: internal, unauthenticated, read-only (`apps/dashboard-ts/scripts/serve.mjs:55`) | Per-client report (PDF or authenticated page) | 3–5 | A,B | 8 |
| 10 | **Price + RR line code** | Line codes are `campaign_deposit`, `creative/strategy/production_services`, `retainer_fee`, `subscription_fee`; no RR-specific or performance-fee code (`finance-py/src/models.py:348-350`). A flat fee could ride on `retainer_fee`/`strategy_services` if the CPA agrees | Founder sets the fee model (D2); add a line code only if the model needs one (e.g. % of recovered revenue) | 0–2 | A,B | D2, CPA (A9) |
| 11 | **Collect money** | No payment SDK; card refused (ADR 0009 D11); `FIN_BANK_FEED` refuses | B: off-the-shelf invoice (e.g. Stripe Invoicing/ACH) used by hand, recorded in finance. A: inbound rail adapter + bank feed | B: 0.5 · A: 5–8 | A,B | D4, counsel A5 |
| 12 | **Contract + signature** | legal-py has the evidence model; e-sign `NotWiredESignProvider`, setting the provider refuses start; no approved MSA (ADR 0010:239-263); onboarding trusts a caller `signed: bool` (`onboarding_schema/__init__.py:474-477`) | Counsel-drafted pilot agreement/MSA; B: signed via an off-the-shelf e-sign tool, recorded in legal-py; A: e-sign adapter + onboarding→legal link | B: 1 · A: 4–6 | A,B | Counsel A1, A2, A4, A7 |
| 13 | **Onboarding wiring** (→ legal, → finance, handoff) | All three are fail-closed stand-ins (`onboarding-py/src/integrations/departments.py:65-158`) | Real calls to legal `/contracts/{id}/terms` and finance invoice draft | 4–6 | A | 4, 12 |
| 14 | **Compliance + rules approval** | Register is founder-approved only; "almost everything is blocked" (`compliance-py/README.md:9-13`) | Founder reviews and approves the FIN-00, LG-00 and compliance seeds for the client lane | founder time | A (B: client-lane subset) | — |
| 15 | **Store writer + Action Execution** (trust graduation: shadow → approval → autonomous) | Gate logic only; execution refuses (`action_execution.py:1-21`; ADR 0001:53-56) | Shopify write client, per-fix playbooks (start with 2–3 fix types), approval flow, rollback, audit to ledger, write scopes | 15–25 | A | 6, 7, counsel A7 |
| 16 | **Client login + portal** | None; all auth is service tokens (`_SURVEY.md` §c) | Client auth, per-tenant views | 5–8 | A | 2, 5 |
| 17 | **Independent review** | All AEGIS verdicts CONDITIONAL (same-session reviewer) | Outside review of the live path before real data/money | 3–5 | A,B | items done |

**Rough totals (estimates):**
- **Track B (pilot):** items 1, 2, 3, 4 (minimal), 5, 6, 7, 8, 9, 10, 11B, 12B, 17 ≈ **35–60 build days → ~3–5 weeks** with
  parallel agent work, plus counsel turnaround.
- **Track A (full):** adds 4 (full), 11A, 12A, 13, 14, 15, 16 ≈ **+45–70 days → ~9–14 weeks total.**
- **Counsel runs in parallel and can be the critical path.** No estimate is possible for it from here.

### Shopify facts the plan relies on (verified Oct 4, 2026)
- New custom apps can no longer be created in the Shopify admin since Jan 1, 2026; they are built in the **Dev
  Dashboard** and installed on the store. Existing custom apps keep working.
  ([Shopify changelog](https://changelog.shopify.com/posts/legacy-custom-apps-can-t-be-created-after-january-1-2026))
- `read_orders` covers orders **and abandoned checkouts**, but only the **last 60 days**; older orders need
  `read_all_orders`, requested separately. Apps get **no protected customer data** by default, and the API won't return
  it from non-development stores until the app is approved for it.
  ([Shopify access scopes](https://shopify.dev/docs/api/usage/access-scopes))
- Implication: the detection agents must work on a 60-day window unless `read_all_orders` is granted, and the pilot
  should avoid customer PII scopes unless an agent truly needs them (also shrinks counsel question A8).

---

## 3. Decisions needed (founder unless marked)

| ID | Decision | Why it blocks | Recommendation |
|---|---|---|---|
| **D1** | Track B pilot allowed, despite "nothing manual" and "fix engine first"? | Determines whether revenue is ~1 month or ~3 months out | Yes, as milestone 1 of Track A |
| **D2** | Revenue Recovery fee model: flat audit fee, monthly retainer, % of recovered revenue, or hybrid | Item 10; also counsel/CPA A9 | Flat fee for the pilot; % of recovered revenue needs attribution the platform can't yet prove |
| **D3** | Hosting: which cloud/provider | Items 2–4 | One small managed setup (single VM or container host + managed Postgres) |
| **D4** | Payment collection: allow card for clients (reverses ADR 0009 D11) or ACH/wire only | Item 11 | ACH via an invoicing tool for the pilot |
| **D5** | Make `zbm-zbc` private | Item 1; repo exposes security posture and legal strategy | Yes (check Actions minutes first) |
| **D6** | Persistence choice (ADR draft) | Item 4 | See `ADR-draft-persistence.md` |
| **D7** | Which 2–3 fix types the store writer starts with | Item 15 scope | Pick from the agents with highest-confidence findings after the pilot |
| COUNSEL | A1 engagement + AI clause, A2 CCPA terms, A4 clickwrap, A7 store-change liability, A8 merchant PII | Item 12; nothing signs without them | Send `COUNSEL_PACKET.md` this week |
| CPA | A5 sales tax, A9 revenue recognition | Item 10–11 | Same week |

---

## 4. Suggested sequence

1. **This week (founder):** D1, D2, D5; send the counsel packet; choose hosting (D3).
2. **Weeks 1–2 (build):** deployment base, secrets, minimal persistence, client dimension, Shopify Dev Dashboard app +
   OAuth on a **development store** (no real merchant data yet).
3. **Weeks 2–4:** ingestion + translation, orchestrator live source, findings report; run against the dev store, then a
   friendly real store with read-only scopes.
4. **Week 4–5:** independent review of the live read path; pilot agreement signed; first invoice. **First dollar (Track B).**
5. **Weeks 5–14:** store writer with 2–3 fix types under trust graduation, e-sign + inbound payment adapters, onboarding
   wiring, client portal. **Full Track A.**

---

## 5. Out of scope for customer #1 (explicitly)
- ZBC clipping money flows (verification, clipper-network, payouts). Separate track; counsel group B.
- Fulfillment (phone callbacks) and creative production.
- Departments 11+ of the 45-department roster.
- The parked macOS timing-advisory CI change (branch `macos-timing-advisory`, local).

## 6. Housekeeping noticed during the survey
- `integration-2026-09-24`'s `docs/findings/OPEN.md` still lists CI7-1 as open; the closure record is on `fix26b`
  (bfb042c), two docs-only commits ahead. Merge with the next real change.
- Three client/route mismatches in clipper-network-py (see `API_SURFACE.md` summary). Not on the Shopify path.
- `BUILD_CONTRACTS.md`, `revenue-recovery-founder-decisions.md`, `revenue-recovery-roadmap.md` are cited but absent from the
  repo (`docs/findings/OPEN.md:23,27`). Detection's scope rules live there; they should be committed before the live build.
