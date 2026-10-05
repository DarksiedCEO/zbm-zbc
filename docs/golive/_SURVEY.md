# Go-live capability survey: first paying Shopify Revenue Recovery customer

Tree: `golive-plan` = `integration-2026-09-24` @ `013caff`. Read-only survey made 2026-10-04. Paths are repo-relative and
`file:line` is cited throughout. UNKNOWN means not verified in this pass.

Target path: **detect leaks in a real Shopify store → onboard client → contract signed → fix applied to the store → invoice →
cash collected.**

---

## Per-service

### services/detection-py (Revenue Recovery agents A–H)
1. **Does today:** Runs 8 deterministic leak agents (4 Tier 1, 4 Tier 2), plus correlation (double-count) and safety modules, over
   REST (`src/api.py:521-566`). Money is exact Decimal. The app declares itself non-live: "this service has no connection to a
   real store" (`src/api.py:121-126`).
2. **Inputs:** Either a JSON body POSTed to `/agents/*/detect`, or repo fixtures served at `/fixtures/*` (`src/api.py:405-438`).
   The fixtures come from `fixtures/*.json`, read by `src/fixtures_loader.py:26-66`, which says "explicitly non-live, fixture-only
   data". **There is no Shopify code:** no Admin API client, no OAuth, no webhooks, no GraphQL. The schema is deliberately
   platform-agnostic: "A per-platform translation layer (not yet built) is responsible for converting a real store's native data
   into this shape" (`src/zbm_schema/__init__.py:17-21`). `source_platform` is "informational only" (`src/zbm_schema/__init__.py:169`).
   Core `Order`/`Finding` carry no `client_id`; only Tier 2 models do (`src/zbm_schema/tier2.py:39,53`).
3. **State:** Stateless (no store). No DB driver in `requirements.txt` (pydantic, fastapi, uvicorn, httpx, pytest).
4. **Secrets/config:** `ZBM_SERVICE_TOKEN` is required, and the service fails closed without it (`src/api.py:73-87`). It is a single
   shared bearer, "explicitly not adequate for anything internet-facing" (`src/api.py:60-70`). Serve tuning is
   `DETECTION_SWITCH_INTERVAL_SECONDS` / `DETECTION_DRAINS_MAX` (`src/serve.py:85,104`).
5. **External write:** None. `safety/action_execution.py:1-21` reads "SCOPED, NOT LIVE … no connected client store … Building a fake
   'successful write to Shopify' here would violate…". Only the trust-graduation gate is real.
6. **Relevance: REQUIRED.** This is the product. It needs a Shopify ingestion/translation layer and per-client scoping.

### services/orchestrator-go
1. **Does today:** `POST /revenue-recovery/scan` runs every agent against detection-py fixtures and appends each finding to the
   ledger. `GET /revenue-recovery/findings` reads them back (`cmd/orchestrator/main.go:176-192`). It checks the ledger first and
   fails fast (`internal/orchestrator/orchestrator.go:41-55`).
2. **Inputs:** Only the fixture endpoints: "this method only ever reads from detection-py's /fixtures/* endpoints (no real-store
   data source exists yet)" (`internal/orchestrator/orchestrator.go:35-38`, `internal/client/client.go:186-195`). The scan takes
   no body, so there is no client or store parameter. Responses hard-code `non_live_data_source: true`
   (`orchestrator.go:175`, `recorded.go:101`). No Shopify code (the only "shopify" hit is a test fixture, `client_test.go:120`).
3. **State:** None of its own; the ledger is the store. `go.mod` has no dependencies at all (stdlib only).
4. **Secrets/config:** These env vars are required and fail closed (`cmd/orchestrator/main.go:198-244`): `DETECTION_SERVICE_URL`,
   `LEDGER_SERVICE_URL`, `DETECTION_SERVICE_TOKEN`, `ORCHESTRATOR_SERVICE_TOKEN`, `LEDGER_SERVICE_TOKEN`. Optional:
   `ORCHESTRATOR_BIND_ADDR` (default 127.0.0.1), `ORCHESTRATOR_PORT`, `ORCHESTRATOR_PORT_FILE`.
5. **External write:** Only to ledger-rust.
6. **Relevance: REQUIRED.** It is the scan pipeline. It must take a client/store id and a real data source in place of fixtures.

### services/ledger-rust
1. **Does today:** A hash-chained, append-only evidence ledger holding findings (`/ledger/append`) and department events
   (`/ledger/events`), with `/ledger/verify` and `/ledger/entries` (`src/bin/server.rs:416-436`). Tamper-evident and
   bearer-authenticated.
2. **Inputs:** HTTP payloads from the other services only.
3. **State:** A **real on-disk JSONL file** at `LEDGER_LOG_PATH` (default `ledger_data/ledger.jsonl`, `src/bin/server.rs:915-916`).
   Each append is fsync'd before it reports success, and torn-tail recovery exists (`src/persistence.rs:15-44,279`). The whole log
   loads into memory at start. There is one chain for every department and client, with no tenant partition. No DB crate in
   `Cargo.toml` (sha2, serde, chrono, tokio, hyper, libc).
4. **Secrets/config:** `LEDGER_SERVICE_TOKEN` is required and fails closed (`src/bin/server.rs:884-890`). Also `LEDGER_PORT` (8090),
   `LEDGER_BIND_ADDR` (127.0.0.1), `LEDGER_MAX_CONNECTIONS` (512), `LEDGER_PORT_FILE` (`server.rs:899-933`).
5. **External write:** None.
6. **Relevance: REQUIRED.** Every service's "record first" depends on it. It needs a durable volume and backups.

### services/onboarding-py (15 intelligences, 3 lanes)
1. **Does today:** Runs client, creator, and brand onboarding: intake facts and messages, platform-access guidance, an audit baseline
   pulled from detection-py, a first-win pick, escalations, and an activation gated on contract (i14) plus compliance (i15)
   (`README.md:13-36`; routes `src/api.py:561-729`). The README says it is "NOT certified for any real client" (`README.md:8-13`).
2. **Inputs:** HTTP payloads from an internal caller. Website HTML for the tag scan must be supplied, because `NotWiredSiteFetcher`
   refuses to fetch (`src/integrations/platforms.py:6-12`). Shopify appears only as **text and regex**: tag detection of
   `cdn.shopify.com|myshopify.com` (`src/intelligences/i04_platform_access.py:60`), owner instructions
   (`i04_platform_access.py:148-158`), and an "abandoned-cart flow" proposal (`src/intelligences/i05_setup.py:63-64`). OAuth grant
   **metadata only**, with no token field (`src/onboarding_schema/__init__.py:15,239-245`). Access verification uses
   `NotWiredPlatformProbe`, which "access not yet verified" (`platforms.py:23-25`). It calls detection-py over HTTP when
   `DETECTION_SERVICE_URL/_TOKEN` are set (`src/api.py:150-153`; route map `src/integrations/revenue_recovery.py:1-43`).
3. **State:** **In-process dicts** (`src/service.py:323-360`). "State is in-process memory and is lost on restart … Persistence
   isn't built" (`README.md:426-435`). Contract terms are refused by default (`NotDecidedContractStorage`,
   `src/integrations/departments.py:65-72`). The `ONBOARDING_CONTRACT_STORAGE=in_memory` option keeps them in a dict
   (`src/api.py:158-159`). No DB driver.
4. **Secrets/config:** `ONBOARDING_SERVICE_TOKEN` (`src/api.py:92`) and `ONBOARDING_ANDRE_APPROVAL_KEY` (`api.py:160`). Also
   `LEDGER_SERVICE_*` and `DETECTION_SERVICE_*` (`api.py:146-151`), `COMPLIANCE_SERVICE_URL/_TOKEN/COMPLIANCE_CALLER_TOKEN`
   (`api.py:155-156`), and many `ONBOARDING_*` tunables (`src/config.py:191-231`). The **vault is `RefusingVault`**, which refuses
   to store any credential (`src/integrations/vault.py:31-39`). The credentials route never reads its body (`README.md:101`).
5. **External write:** None. Every outward port has a fail-closed default (`src/integrations/departments.py:192-199`):
   - billing: `NotBuiltBillingDepartment` (`departments.py:113-115`)
   - handoff: `NotWiredHandoff` (`departments.py:151-158`)
   - push to Andre: `NotWiredPushNotifier` (`departments.py:177-179`)
   - Andre's PlatformWriter: refuses (`platforms.py:10-12`)

   "Contract signed" is a caller-supplied boolean `ContractTerms.signed` (`src/onboarding_schema/__init__.py:474-477`); nothing
   verifies a signature.
6. **Relevance: REQUIRED.** The client lane is the intake and activation path. It needs persistence, contract storage, and a
   billing hook.

### services/fulfillment-py (missed-call → callback)
1. **Does today:** Runs six agents for missed-call detection, callback orchestration, follow-up sequencing, dossier, appointments,
   and write-back (`README.md:27-33`). Status is CONDITIONAL (`README.md:3`).
2. **Inputs:** HTTP payloads (call events and so on). No Shopify code. The "webhook" mention is a comment only
   (`src/agents/missed_call_detection.py:52`).
3. **State:** In-memory bounded maps (`src/bounded_state.py:1-14`). Dedupe is "Not persisted — lost on restart"
   (`README.md:83,108`). It does not use the ledger.
4. **Secrets/config:** `FULFILLMENT_SERVICE_TOKEN` (`src/api.py:98`), `FULFILLMENT_SIP_DIALER` and `FULFILLMENT_SYSTEM_OF_RECORD`
   (`api.py:1563-1572`), and contact-window envs (`api.py:1581-1614`).
5. **External write:** None. `NotWiredSipDialer`, `NotWiredMessageSender` and `NotConfiguredSystemOfRecord` raise or refuse
   (`src/integrations/sip_dialer.py:66-80`, `message_sender.py:48-55`, `system_of_record.py:39-55`). The README says "No code path
   in this repo can place a real call or write to a real external system" (`README.md:100`).
6. **Relevance: NOT NEEDED.** It is a phone-callback product, not Shopify revenue recovery.

### services/creative-py (Creative Production, ZBM + ZBC)
1. **Does today:** Runs deterministic brief, approval, rights, quality and compliance workflows for ZBM ads, plus ZBC clip
   rulebook, kit and payout-eligibility (`README.md:10-27`). It is self-tested only (`README.md:3-6`).
2. **Inputs:** HTTP payloads. No media libraries are integrated (`README.md:41-43`). No Shopify code.
3. **State:** In memory ("state is in memory", `README.md:793`). It records to the ledger when `LEDGER_SERVICE_URL/_TOKEN` are set
   (`src/api.py:604`, `src/shared/ledger.py:204-205`).
4. **Secrets/config:** `CREATIVE_SERVICE_TOKEN` (`src/api.py:215`), `CREATIVE_ACTOR_TOKENS` (`api.py:243`),
   `CREATIVE_ANDRE_APPROVAL_TOKEN` (`api.py:1178`), `COMPLIANCE_SERVICE_URL`, and `CREATIVE_BIND_ADDR/PORT` (`src/serve.py:149-150`).
5. **External write:** None. Departments and media are stand-ins (`README.md:36-43`).
6. **Relevance: NOT NEEDED.**

### services/compliance-py (Compliance 38)
1. **Does today:** Acts as the activation, payout and publish gate plus 17 control monitors, over an obligation register that only
   Andre can change (`README.md:1-27`). "On day one almost everything is blocked" (`README.md:9-13`). The client lane requires a
   fact set (`src/intelligences/i02_activation_gate.py:45-63`).
2. **Inputs:** HTTP payloads (facts from onboarding). The Change Watcher can do real HTTPS GETs (`HttpFeedFetcher`), but it is off
   unless `COMPLIANCE_WATCHER_ENABLED=1`, and it is allowlisted with robots-respecting fetches (`src/fetcher.py:1-15`). No Shopify code.
3. **State:** A hash-chained local JSONL at `COMPLIANCE_DATA_DIR/compliance_log.jsonl`, fsync'd, anchored on the ledger, and
   replayed at start. Without a data dir it is in memory and "nothing survives a restart" (`src/store.py:1-17`).
4. **Secrets/config:** Service, caller and Andre tokens plus `LEDGER_SERVICE_URL/_TOKEN` (`src/config.py:139`). Ports are
   fail-closed stand-ins: `NotBuiltVerificationIntegrity`, `NotBuiltFinance31`, `NotBuiltLegal37`, `NotWiredSanctionsProvider`
   (OFAC) and `NotWiredAccessibilityChecker` (`src/ports.py:54,91,114,136,166`).
5. **External write:** None.
6. **Relevance: USEFUL (it becomes a blocker if wired).** Onboarding activation calls it when configured. The register has to be
   approved, and the client-lane facts have to pass, before activation.

### services/verification-py (V&I, ZBC clip view certification)
1. **Does today:** Certifies clip view counts and clipper integrity from official platform APIs through the clipper's OAuth
   connection (`README.md:1-20`). "On day one no connection completes and nothing certifies" (`README.md:8-12`).
2. **Inputs:** Real HTTP adapters for YouTube, TikTok, Instagram and X are "built and mock-tested, NOT wired" (`src/adapters/http_adapters.py:1-3`).
   Pending OAuth state lives in memory only (`src/service.py:200`), and `VI_COUNT_SOURCE` accepts only `owner_oauth_api`
   (`src/config.py:150-153`). No Shopify code.
3. **State:** JSONL at `VI_DATA_DIR/vi_log.jsonl`, the same pattern as compliance (`src/store.py:1-17`).
4. **Secrets/config:** `VI_SERVICE_TOKEN` (`src/config.py:140`). The token vault is a port only (`src/ports.py:26-47`), and unbuilt
   options refuse to start (`config.py:129-169`).
5. **External write:** None (reads only, and not wired).
6. **Relevance: NOT NEEDED** for ZBM Revenue Recovery.

### services/clipper-network-py (ZBC clipper lifecycle)
1. **Does today:** Handles clipper recruiting, admission, tiering, enrolment, messaging, discipline and offboarding (`README.md:1-20`).
   "On day one no clipper can be admitted, enrolled or messaged" (`README.md:8-14`).
2. **Inputs:** HTTP payloads. It relays OAuth codes to V&I and never stores them (`src/service.py:1159`). No Shopify code.
3. **State:** JSONL at `CN_DATA_DIR/cn_log.jsonl` plus a mutable contact store (`src/store.py:1-23`).
4. **Secrets/config:** `CN_SERVICE_TOKEN`, `CN_IDENTITY_HMAC_KEY`, `CN_ANDRE_APPROVAL_TOKEN`, `CN_CALLER_TOKENS`, `CN_*_URL` and
   `CN_MESSAGE_PROVIDER` (`src/config.py`).
5. **External write:** None. The messaging provider is a stand-in (`README.md:8-11`).
6. **Relevance: NOT NEEDED.**

### services/finance-py (Finance 31)
1. **Does today:** Keeps double-entry books for ZBM and ZBC, and runs ZBC creator payables, maker-checker payout runs, clawbacks,
   tax, reconciliation and close (`README.md:1-5`). It also drafts invoices and lets Andre approve or "issue" them
   (`src/api.py:458-470`, `src/svc_books.py:304-400`). README: "**Not live.** … On day one nothing accrues, activates, issues,
   reconciles or pays" (`README.md:7-9`).
2. **Inputs:**
   - Invoice drafts come from the `onboarding` and `scheduler` callers (`src/api.py:458-461`).
   - Bank statement lines come in as a POST from a `bank_feed` caller (`src/api.py:472-474`).
   - Rail webhooks come in as a POST to `/fin/v1/rails/{rail}/events` (`src/api.py:486`).
   - Stripe appears only as a **payout rail (Stripe Connect) port** plus a pinned country list (`src/ports.py:188-262`,
     `src/svc_payees.py:33-47`). There is no Stripe SDK and no collection or checkout code.
3. **State:** JSONL at `FIN_DATA_DIR/fin_log.jsonl`, replayed at start. Without it, it is in memory (`src/store.py:1-17`,
   `src/service.py:264-268`).
4. **Secrets/config:**
   - Required: `FIN_SERVICE_TOKEN`, `FIN_ANDRE_APPROVAL_TOKEN`, `FIN_CALLER_TOKENS` (each ≥32 chars, all distinct) and
     `LEDGER_SERVICE_*` (`README.md:12-23`).
   - Setting any of these **refuses start**, because they are not built (`src/config.py:172-185`, `README.md:28-31`):
     `FIN_RAIL_STRIPE`, `FIN_RAIL_TROLLEY`, `FIN_BANK_FEED`, `FIN_TAX_AGENT`, `FIN_VAULT`, `FIN_LEGAL_URL` and others.
5. **External write:** None.
   - Issuing an invoice is a status change plus a journal posting; nothing is sent to the client (`src/svc_books.py:388-400`).
   - Rails and bank are `NotWiredRail` and `NotWiredBank` (`src/ports.py:241-262,289-296`).
   - **Issue is blocked** until counsel memo `FIN-CQ-11` (sales tax) is verified, and until a Legal contract reference is in force
     (`src/svc_books.py:366-370`).
   - Card is refused (ADR 0009:187, D11), so payment methods are ACH or wire only (`src/intelligences/i02_receivables.py:23`).
   - ZBM line codes are creative, strategy, production, retainer or subscription. **There is no Revenue-Recovery or
     performance-fee code** (`src/models.py:348-350`).
6. **Relevance: REQUIRED** to bill and book revenue. The collection rail, invoice delivery, a matching line code and the
   FIN-CQ-11 memo are all missing.

### services/legal-py (Legal 37)
1. **Does today:** Keeps a document register (SHA-pinned, counsel sign-off, then Andre approval), records clickwrap and e-sign
   acceptance evidence, and runs playbooks, obligations, matters, takedowns and filings. It never gives advice (`README.md:1-13`).
   Contract terms are exposed at `GET|PUT /legal/v1/contracts/{client_id}/terms` (`src/api.py:468-475`). Status: "not in force
   for any real document" (`README.md:11-13`).
2. **Inputs:** HTTP payloads. Acceptance comes in through `POST /legal/v1/acceptances`, envelopes through `POST /legal/v1/envelopes`,
   and provider events through `POST /legal/v1/esign/events` from the `esign_gateway` caller (`src/api.py:402-416`). No
   DocuSign or other e-sign SDK.
3. **State:** JSONL at `LEGAL_DATA_DIR/legal_log.jsonl` plus a write-once blob store (`src/store.py:1-18,174`). Without the data
   dir, it is in memory.
4. **Secrets/config:** `LEGAL_SERVICE_TOKEN`, `LEGAL_ANDRE_APPROVAL_TOKEN`, `LEGAL_CALLER_TOKENS` and `LEDGER_SERVICE_*`
   (`README.md:20-25`). Setting `LEGAL_ESIGN_PROVIDER` or `LEGAL_COUNSEL_CHANNEL` **refuses start** (`src/config.py:124-125`).
5. **External write:** None. `NotWiredESignProvider.create_envelope` returns `available=False` (`src/ports.py:73-91`), and the
   counsel channel and push are not wired (`ports.py:98-131`).
6. **Relevance: REQUIRED** for the contract. Today it can record a clickwrap acceptance against an approved document, but no
   approved document, counsel engagement or e-sign provider exists (ADR 0010:239-263).

### services/delivery-py (Dept 28, "fix engine")
1. **Does today:** **This is a code-fix engine for this monorepo, not a client-store fixer.** It ingests an AEGIS findings document,
   opens a git worktree on `fix<N>-<service>`, and runs an LLM engineer (embedded deer-flow) in a Docker sandbox to write a failing
   test and fix `services/<service>/` source. The engine verifies RED/GREEN and commits locally for AEGIS review (`README.md:7-12`;
   ADR 0011:27-33). File writes are contained to `services/<service>/` and `docs/adr/` (`README.md:48-49`). The network is
   `none`, and push and remotes are denied (`README.md:49-50`).
2. **Inputs:** An AEGIS findings document POSTed by the `aegis` or `andre_session` caller (`README.md` Routes table). Also a local
   repo at `DLV_REPO_PATH`, which **must have no remotes** (`README.md:10,117-118`, `src/zbm_delivery/config.py:340-344`). No Shopify code.
3. **State:** JSONL plus an evidence store under `DLV_DATA_DIR`; unset means in memory (`src/zbm_delivery/config.py:363`).
4. **Secrets/config:**
   - Needs `DLV_SERVICE_TOKEN`, `DLV_CALLER_TOKENS`, `DLV_SANDBOX_IMAGE` (digest-pinned), `DLV_LLM_PROVIDER/MODEL` and
     `DLV_LLM_API_KEY_REF` (`README.md:120-127`).
   - A production key must be `vault:<ref>`, but `DLV_VAULT` refuses because the vault is not built. So a key is only possible as
     `env:DLV_*` with `DLV_NON_PRODUCTION=1` (`config.py:266,277-289`).
   - Day one: no Docker daemon gives 503, and no key gives 503 (`README.md:129-131`).
5. **External write:** Local git commits only. Remotes, push, network and MCP are denied unconditionally (`README.md:49-50,137-141`).
   **It cannot touch a Shopify store or any client system.**
6. **Relevance: NOT NEEDED as built** for the customer path. Nothing in the repo applies a revenue-recovery fix to a store. That
   capability, a Shopify writer plus the trust-graduation "Action Execution" path (`detection-py/src/safety/action_execution.py`),
   does not exist. delivery-py is internal engineering tooling.

### apps/dashboard-ts (Next.js 16)
1. **Does today:** A read-only page that renders the findings recorded by orchestrator-go. It returns 200 only if the findings load
   and the ledger verifies (`README.md` table, `src/proxy.ts:6-34`).
2. **Inputs:** `GET {ORCHESTRATOR_URL}/revenue-recovery/findings` (`src/lib/api.ts:61-81`). No Shopify code.
3. **State:** None.
4. **Secrets/config:** `ORCHESTRATOR_URL`, `ORCHESTRATOR_SERVICE_TOKEN` (server-side only) and `ORCHESTRATOR_TIMEOUT_MS`
   (`src/lib/api.ts:14-26,61-62`). Binds to 127.0.0.1 unless `DASHBOARD_BIND_ADDR` is set (`scripts/serve.mjs:24,39`).
5. **External write:** None. Non-GET requests to `/` return 405 (`src/proxy.ts:28-30`).
6. **Relevance: USEFUL.** It could serve as an internal view, but it has **no authentication** ("it has NO authentication",
   `scripts/serve.mjs:55`), no per-client view, and nothing client-facing.

---

## Cross-cutting

### (a) Deployment
- The only container files are `services/delivery-py/docker/Dockerfile` (which needs a `BASE_DIGEST` build-arg; image tag
  `registry.zbm.internal/...`, not real) and `docker/sandbox.Dockerfile`.
- None of these exist: compose, k8s, Helm, Terraform, Procfile, fly/render/vercel configs, systemd units.
- CI exists (`.github/workflows/ci.yml`, `docs/ci.md`). It runs tests only and has no deploy job.
- Every service binds 127.0.0.1 by default. No TLS termination or reverse proxy is defined.
- Git remote is `github.com/DarksiedCEO/zbm-zbc`. Nothing is deployed anywhere (no hosting config found).

### (b) Service-to-service calls that exist
| Caller | Callee | Where |
|---|---|---|
| dashboard-ts | orchestrator-go `GET /revenue-recovery/findings` | `apps/dashboard-ts/src/lib/api.ts:78` |
| orchestrator-go | detection-py `/fixtures/*`, `/agents/*/detect`, `/correlation/overlaps` | `internal/client/client.go:186-225` |
| orchestrator-go | ledger-rust `/ledger/append`, `/verify`, `/entries` | `internal/client/ledger.go:51-167` |
| onboarding-py | detection-py `/agents/*/detect` (audit baseline) | `src/integrations/revenue_recovery.py:30-43` |
| onboarding-py | compliance-py (activation ruling), ledger-rust | `src/integrations/compliance38.py:51-63`, `src/api.py:146` |
| creative-py | compliance-py, ledger-rust | `src/api.py:604,1177` |
| legal-py | compliance-py (`LEGAL_COMPLIANCE_URL`), ledger-rust | `legal-py/README.md` settings table |
| finance-py | V&I, Compliance thin clients (`FIN_VI_URL`, `FIN_COMPLIANCE_URL`), ledger-rust | `src/clients.py:119-158` |
| clipper-network-py | V&I (`CN_VI_URL`), compliance, creative, ledger-rust | `src/config.py:197-201` |
| verification-py | compliance (`VI_COMPLIANCE_URL`), ledger-rust | `src/compliance_client.py` |
| compliance-py, delivery-py, fulfillment-py | ledger-rust only (fulfillment: none) | config |

**Missing links on the customer path:**
- onboarding → finance (billing is `NotBuiltBillingDepartment`)
- onboarding → legal (contract storage is `NotDecidedContractStorage`; legal's `/contracts/{id}/terms` is not called by onboarding)
- onboarding → "handoff" to an RR owner (`NotWiredHandoff`)
- orchestrator → onboarding or client scoping
- anything → a store writer

### (c) Identity / auth / tenancy
- All auth is **service-to-service**: a shared bearer token per service, plus per-caller tokens and an Andre approval token on the
  newer services. Nothing has end-user or client login, sessions, OAuth for ZBM's own users, or a client portal.
- The dashboard is unauthenticated.
- `client_id` exists in onboarding, finance and legal records. Detection orders and findings, the orchestrator scan, and the ledger
  chain have **no client or tenant dimension** (`detection-py/src/zbm_schema/__init__.py:160-170`; orchestrator scan takes no body).
- Shopify app OAuth (shop install, access token storage) does not exist, and the onboarding and V&I vaults refuse to store tokens.

### (d) Payments
- No Stripe or other payment SDK in any requirements file. No checkout, payment link, card capture, or Stripe Billing/Invoicing.
- finance-py models Stripe Connect and Trolley only as **outbound creator payout rails**, with no adapter: "No rail adapter code
  exists yet" (ADR 0009:255-257). Card acceptance is refused (ADR 0009:187).
- Inbound cash can only be recorded from a `bank_feed` caller posting statement lines (`finance-py/src/api.py:472-474`), and no bank
  feed adapter exists (`FIN_BANK_FEED` refuses).

### (e) Contract / e-signature
- legal-py has the evidence model (clickwrap and envelope sufficiency, `src/intelligences/i03_acceptance.py:1-15`) and a port.
- The provider is `NotWiredESignProvider`, and setting `LEGAL_ESIGN_PROVIDER` refuses start. No DocuSign, Dropbox Sign or
  PandaDoc code.
- Clickwrap evidence is "evidence_sufficient=false until CQ-19 is verified by a counsel memo" (`i03_acceptance.py:7-9`).
- onboarding's gate trusts a caller-supplied `signed: bool`.
- No counsel-approved MSA or order form exists (ADR 0010:239-250; ADR 0009 unlock item 3).

### (f) Founder rulings and open decisions affecting go-live
- **Platform-agnostic schema** (founder correction Sep 21): the Shopify translation layer is intentionally separate and "not yet
  built" (ADR 0001:24-29).
- **Tier 3 (dunning, chargebacks, gateway) is excluded**, because Revenue Recovery is "marketing-leak recovery, not payment/dunning
  recovery" (ADR 0001:38-51).
- **Action execution is gated by trust graduation** (shadow → human-approval → autonomous), and "has not been entered by any client
  yet" (ADR 0001:53-56; `detection-py/src/safety/action_execution.py`). Any store fix needs this path built.
- **R-GATE** (Oct 1 2026): Critical/High findings block merge, and Medium/Low go to `docs/findings/OPEN.md` and are fixed the next
  wave (`docs/findings/OPEN.md:3-7`). One **High is still open: CI7-1** (delivery-py macOS CI leg red; fix committed locally, not
  pushed) (`OPEN.md:48`).
- **Waiting on the founder** (`FIX_WAVE_26B_REPORT.md:222-245`):
  - TG-1, DLV-CLAMP, H9-R6m, W25-EA-9 and W25-EA-2
  - **W25-EA-1 / C4-5**: `BUILD_CONTRACTS.md`, `revenue-recovery-founder-decisions.md` and `revenue-recovery-roadmap.md` are cited
    but **absent from the repo** (`OPEN.md:23,27`).
  - **push of `fix26b`** (nothing is pushed).
- **Finance unlock list** (ADR 0009:245-262):
  - Andre approves the FIN-00 rules seed.
  - Counsel/CPA memos FIN-CQ-01 and **FIN-CQ-11** (sales tax; "no invoice issues without it").
  - Legal order form and MSA.
  - **Bank chosen** and a bank feed adapter written.
  - Stripe Connect adapter.
  - Cybersecurity-22 vault.
  - AEGIS review.
- **Legal unlock list** (ADR 0010:239-263):
  - Andre approves the LG-00 seed.
  - Counsel engaged.
  - A per-document counsel memo and sign-off.
  - A CQ-19 memo for clickwrap sufficiency.
  - **e-sign provider adapter** plus the `esign_gateway` caller.
- **Day-one fail-closed design across departments:**
  - Onboarding: activation needs both gates (README:40).
  - Compliance: "almost everything is blocked" (README:9-13).
  - Finance: "nothing … issues" (README:7-9).
  - Every live connector requires an ADR amendment or unlock, not just config.
- **Vault (Cybersecurity 22) is not built**, and it is referenced as a hard prerequisite by onboarding (`vault.py:4-8`), finance,
  verification and delivery. That makes it the blocker for storing any Shopify access token.
