# ZBM/ZBC — HTTP API surface (go-live reference)

Derived from code on branch `golive-plan` (= `integration-2026-09-24` @ `013caff`): FastAPI route decorators, Go `http.ServeMux` registrations, the hyper `match` in ledger-rust, and Next.js route handlers / proxy. READMEs were used only to confirm launch commands. Where something cannot be determined from code it says **UNKNOWN**. Paths are repo-relative; `file:line` points at the route registration unless stated otherwise.

## Summary

The repository contains **13 HTTP services**: 10 Python FastAPI services (`services/*-py`), `orchestrator-go` (Go `net/http`), `ledger-rust` (Rust, hyper), and `apps/dashboard-ts` (Next.js). Together they expose **364 endpoints** (method + path; `/health` counted once per service). The counts per service are in the table below. **Only one endpoint is consumed by a frontend in this repo:** `apps/dashboard-ts` calls `GET /revenue-recovery/findings` on orchestrator-go, server-side (`apps/dashboard-ts/src/lib/api.ts:78`). The dashboard has two endpoints of its own (`/` and `/healthz`) and no authentication. Every other endpoint is a **service-to-service API**. They use a shared bearer token, and most also use per-caller identity headers whose caller names are departments (`onboarding`, `finance_31`, `scheduler`, …) or gateways (`hub`, `rail_gateway`, `bank_feed`, `esign_gateway`). No service sends CORS headers: there is no `CORSMiddleware` and no `Access-Control-Allow-*` anywhere in `services/` or `apps/`. A browser therefore cannot call any of these APIs cross-origin; a frontend must go through its own server, as the dashboard does. The caller name `hub` (Clipper Network, Legal) looks like a future clipper-facing portal, but no hub client exists in this repo, and `CN_HUB_URL` is listed as not built (`services/clipper-network-py/src/config.py:60`). **State is NOT uniformly in-memory.** What the code shows per service:

| Service | Lang | Default bind | Endpoints | Where state lives (from code) |
|---|---|---|---|---|
| detection-py | Python/FastAPI | 127.0.0.1:8000 | 17 | **Stateless.** Fixture routes read JSON files under repo-root `fixtures/`, read-only (`services/detection-py/src/fixtures_loader.py:27`). |
| fulfillment-py | Python/FastAPI | 127.0.0.1:8091 | 11 | **In-memory, process lifetime.** Dossiers, dedupe maps and outbound-gate history (`services/fulfillment-py/src/api.py:1651-1689`: "process lifetime, lost on restart — no datastore"). Fixtures are read-only files. |
| orchestrator-go | Go | 127.0.0.1:8080 | 3 | **Stateless.** Writes findings to and reads them from ledger-rust (`services/orchestrator-go/internal/orchestrator/orchestrator.go:154`, `recorded.go:52`). |
| ledger-rust | Rust/hyper | 127.0.0.1:8090 | 5 | **Append-only JSONL file**, hash-chained, fsynced, at `LEDGER_LOG_PATH` (default `ledger_data/ledger.jsonl`, `services/ledger-rust/src/bin/server.rs:915`; `src/persistence.rs`). Verified at start; the server refuses to start if the chain is broken. |
| onboarding-py | Python/FastAPI | 127.0.0.1:8200 | 40 | **In-memory** (`OnboardingService.clients` etc., `services/onboarding-py/src/service.py:323`; no file I/O in `src/`) plus events to ledger-rust. Contract storage is a stand-in unless `ONBOARDING_CONTRACT_STORAGE=in_memory` (`api.py:158`). |
| creative-py | Python/FastAPI | 127.0.0.1:8300 | 41 | **In-memory** ("The store is in memory and bounded … like every other piece of state in this service", `services/creative-py/src/api.py:49-50`) plus events to ledger-rust. |
| compliance-py | Python/FastAPI | 127.0.0.1:8380 | 28 | **Hash-chained JSONL** `COMPLIANCE_DATA_DIR/compliance_log.jsonl`. **In-memory if `COMPLIANCE_DATA_DIR` is unset** (`services/compliance-py/src/store.py:4-17,71-75`). Plus ledger-rust. |
| verification-py | Python/FastAPI | 127.0.0.1:8390 | 36 | JSONL `VI_DATA_DIR/vi_log.jsonl` plus `VI_DATA_DIR/vi_platform_data.json` (atomic rewrite). **In-memory if `VI_DATA_DIR` is unset** (`services/verification-py/src/store.py:4-17`, `src/sidestore.py:13-14`). Plus ledger-rust. |
| clipper-network-py | Python/FastAPI | 127.0.0.1:8400 | 43 | JSONL `CN_DATA_DIR/cn_log.jsonl` plus `CN_DATA_DIR/cn_contacts.json`. **In-memory if `CN_DATA_DIR` is unset** (`services/clipper-network-py/src/store.py:5-18,76-80`, `src/contacts.py:29,41-44`). Plus ledger-rust. |
| finance-py | Python/FastAPI | 127.0.0.1:8410 | 67 | JSONL `FIN_DATA_DIR/fin_log.jsonl`. **In-memory if `FIN_DATA_DIR` is unset** (`services/finance-py/src/store.py:4-17,71-75`). Plus ledger-rust. |
| legal-py | Python/FastAPI | 127.0.0.1:8420 | 59 | JSONL `LEGAL_DATA_DIR/legal_log.jsonl` plus a blob directory `LEGAL_DATA_DIR/blobs`. **In-memory if `LEGAL_DATA_DIR` is unset** (`services/legal-py/src/store.py:5-18,72-76,168-183`). Plus ledger-rust. |
| delivery-py | Python/FastAPI | 127.0.0.1:8430 | 12 | JSONL `DLV_DATA_DIR/dlv_log.jsonl`. **In-memory if `DLV_DATA_DIR` is unset** (`services/delivery-py/src/zbm_delivery/store.py:4-16,70-74`); scratch home then goes to a private temp dir (`api.py:449-462`). Plus ledger-rust. |
| dashboard-ts | TypeScript/Next.js 16 | 127.0.0.1:3000 | 2 | **Stateless.** Reads orchestrator-go on every request (`src/proxy.ts`, `src/app/healthz/route.ts`). |

The six JSONL-backed services report `in_memory: true|false` in their `/health` body. Without the data-dir env var, everything they hold is lost on restart.

Endpoint counts: detection-py 17, fulfillment-py 11, orchestrator-go 3, ledger-rust 5, onboarding-py 40, creative-py 41, compliance-py 28, verification-py 36, clipper-network-py 43, finance-py 67, legal-py 59, delivery-py 12, dashboard-ts 2 = **364**.

## How to read this document

* **Auth.** Every API route except `/health` (`/healthz` on the dashboard) requires `Authorization: Bearer <TOKEN>`, compared in constant time. A missing or malformed header gets **401** `{"detail":"missing or malformed Authorization header (expected: Bearer <token>)"}`; a wrong token gets **401** `{"detail":"invalid token"}`. Both carry `WWW-Authenticate: Bearer`. Go uses `{"error":…}` and Rust uses `{"error":…}` instead. Identity headers come on top of the bearer:
  * **Caller headers** (`X-FIN-Caller-Token`, `X-VI-Caller-Token`, `X-Compliance-Caller-Token`, `X-LEGAL-Caller-Token`, `X-CN-Caller-Token`, `X-DLV-Caller-Token`). Each is matched by SHA-256 digest against every configured caller token. Missing or unrecognised → **403** `{"detail":"caller token missing or not recognised"}`. Recognised but not allowed on the route → **403**.
  * **`X-Andre-Approval-Token`** (Andre's "FounderGate"). A refusal is **403** and is recorded as `founder_approval_refused`. A token equal to the service token or to any caller token counts as "not configured", so it is refused.
  * Some services have additional identity headers. They are listed per service.
* **Request body.** The pydantic model is named with `file:line`, followed by its top-level fields exactly as declared (type and default/constraints copied from source). Nested types (e.g. `Id`, `Money`, sub-models) are defined in the same module unless noted. Models with `model_config = ConfigDict(extra="forbid")` (shown when present on the model or its base) reject unknown fields with 422.
* **Response.** The key list comes from static analysis of the service method the route calls. It covers dict literals returned directly, through a local variable, or through `return self.other(...)`. When a method has several `return` statements, each key set is one possible answer. "via stored idempotent answer (replay)" means a retried request returns the stored first answer. **UNKNOWN** marks a shape that is not a literal in code; the method to read is cited.
* **Errors per endpoint.** These are the exception classes found by walking `raise` statements on the code path: the route body plus the service method, following `self.*` calls up to 5 levels. Each is mapped to its HTTP status through the class's `status_code`. The list is **best-effort, not exhaustive.** Errors raised inside collaborators that are not `self.*` methods do not appear in it: the ledger recorder, the founder gate, ports, and the request parser. Those are covered by the service-level "Errors on every route" tables.
* **Idempotency** (finance, verification, compliance, legal, clipper-network, delivery). Every write body carries `request_id` (1–128 chars `[A-Za-z0-9._:-]`). The same `(caller, request_id)` with the same body within **15 minutes** replays the stored answer. A different body → **409** "request_id already used with a different body". Reuse after 15 minutes → **409** (e.g. `services/finance-py/src/service.py:417-428`; `IDEMPOTENCY_WINDOW = timedelta(minutes=15)` in each service.py). Compliance gate routes re-evaluate on replay (`services/compliance-py/src/service.py:687-693`).
* **503 conventions.** `Unavailable` → **503** with `Retry-After: 1` plus `took_effect:false` (finance, delivery) or `issued:false` (verification, compliance, legal, clipper-network). Ledger write failures and reconcile mode both answer 503. Reconcile mode is `<PREFIX>_RECONCILE_MODE=1`: every operation that records is refused with "only Andre's POST …/reconcile is answered" (e.g. `services/finance-py/src/service.py:307-311`).


---

## 1. orchestrator-go (Revenue Recovery scan orchestrator)

* **Language / entry:** Go, `services/orchestrator-go/cmd/orchestrator/main.go` (`main()` at :197). Routes are registered in `newMux` (:167).
* **Bind:** `ORCHESTRATOR_BIND_ADDR` (default `127.0.0.1`, :209) and `ORCHESTRATOR_PORT` (default `8080`, :213). `ORCHESTRATOR_PORT=0` picks a free port. `ORCHESTRATOR_PORT_FILE` (optional) receives the port that was bound (:264).
* **Required env (refuses to start without each):** `ORCHESTRATOR_SERVICE_TOKEN` is the token callers must present (:230). `DETECTION_SERVICE_TOKEN` is sent to detection-py (:222). `LEDGER_SERVICE_TOKEN` is sent to ledger-rust (:244). Upstream URLs: `DETECTION_SERVICE_URL` (default `http://localhost:8000`) and `LEDGER_SERVICE_URL` (default `http://localhost:8090`).
* **Server limits (all routes, before routing and auth, `limitRequests` :122):** head 5 s (`ReadHeaderTimeout`); whole request 15 s; handler budget 60 s; write 75 s; idle 60 s; `MaxHeaderBytes` 16 KiB. Any body over 64 KiB → **413** `{"error":"request body too large"}`. A slow body → **408**; an unreadable body → **400**. No route reads a body; any body sent is discarded.
* **Wrong method:** Go 1.22 method patterns answer **405** before any handler runs. `GET` also matches `HEAD`. Unknown path → **404** (Go default, text/plain).
* **Auth errors:** **401**. `http.Error` sends `Content-Type: text/plain`, but the body is the JSON text `{"error":"missing or malformed Authorization header (expected: Bearer <token>)"}` or `{"error":"invalid token"}` (:31-48).
* **Upstream failures:** **502** `{"error": "<step> failed: <reason>", "correlation_id": "<16 hex>"}` (`writeUpstreamError` :62; `PublicMessage` in `internal/orchestrator/errors.go:42`). A ledger chain that fails verification surfaces as `"LEDGER INTEGRITY FAILURE …"` in `error`.

#### `GET /health` (any method)
- **Source:** `services/orchestrator-go/cmd/orchestrator/main.go:172`
- **Purpose:** Liveness. The pattern has no method qualifier, so any method answers.
- **Auth:** none
- **Response:** 200 `{"status":"ok","service":"orchestrator-go"}`

#### `POST /revenue-recovery/scan`
- **Source:** `services/orchestrator-go/cmd/orchestrator/main.go:176`
- **Purpose:** Runs a full non-live scan. It checks `GET /ledger/verify` first, pulls fixtures from detection-py, runs all 8 detection agents, runs the correlation check, appends **every** finding to the ledger (`POST /ledger/append`), and verifies again (`internal/orchestrator/orchestrator.go:47-179`). **Writes to the evidence ledger every time it runs**, including duplicate entries for findings already recorded.
- **Auth:** `Authorization: Bearer <ORCHESTRATOR_SERVICE_TOKEN>`
- **Request body:** none. Any body is discarded; more than 64 KiB → 413.
- **Response:** 200 `ScanResult` (`internal/orchestrator/orchestrator.go:15-22`):
  `findings: Finding[]`, `overlapping_claims: {entity_id: Finding[]}`, `agents_run: string[]` (8 ids, e.g. `"affiliate-coupon-extension-v1"`), `non_live_data_source: true`, `ledger_entries_written: int`, `ledger_verify: {valid: bool, entries: int, error: string}`.
  `Finding` (`internal/client/types.go:48-59`): `finding_id, agent_id, leak_category, entity_type, entity_id, customer_id, cause_certainty, cause_description, recoverable_value: {amount_usd: "<two-decimal string>", classification, confidence} | null, detected_at`.
- **Errors:** 401; 405 for a non-POST method; 502 with `correlation_id` (ledger unreachable or not verifying *before* the scan, in which case nothing is run or written; a detection failure; a ledger append failure; a failed verify after the scan).

#### `GET /revenue-recovery/findings` (also HEAD)
- **Source:** `services/orchestrator-go/cmd/orchestrator/main.go:185`
- **Purpose:** **Read-only** view of findings already recorded in the ledger, deduplicated by `finding_id` (latest entry wins), with the ledger's verify verdict (`internal/orchestrator/recorded.go:52-103`). Calls only `GET /ledger/entries` and `GET /ledger/verify`. **This is the endpoint the dashboard uses.**
- **Auth:** `Authorization: Bearer <ORCHESTRATOR_SERVICE_TOKEN>`
- **Request body:** none
- **Response:** 200 `RecordedFindingsResult` (`internal/orchestrator/recorded.go:25-35`):
  `findings: RecordedFinding[]`, `overlapping_claims: {entity_id: RecordedFinding[]}` (entities claimed by more than one distinct agent), `ledger_entries_total: int`, `finding_entries_total: int`, `ledger_verify: {valid, entries, error} | null`, `non_live_data_source: true`.
  `RecordedFinding` (`recorded.go:13-21` + `internal/client/ledger.go:135-152`): `seq, finding_id, agent_id, entity_id, leak_category, amount_usd: "<string>"|null, value_classification: string|null, decision_confidence: string|null, recorded_at, prev_hash, hash, amount_out_of_contract: bool, first_seq: int, times_recorded: int, amounts_differ_across_records: bool`.
  **A chain that fails verification is still 200**, with `ledger_verify.valid=false`; the caller must check it (`recorded.go:49-51`).
- **Errors:** 401; 405; 502 `{error, correlation_id}` when the ledger is unreachable, refuses the token, or returns an unexpected shape (e.g. an unknown entry `kind`, `internal/client/ledger.go:165-218`).

---

## 2. ledger-rust (evidence ledger)

* **Language / entry:** Rust, hyper 1 on tokio. `services/ledger-rust/src/bin/server.rs`: `main()` at :911, routing in `handle()` at :390-443.
* **Bind:** `LEDGER_BIND_ADDR` (default `127.0.0.1`) and `LEDGER_PORT` (default `8090`), both at :914-917. `LEDGER_PORT_FILE` is optional (:933). `LEDGER_LOG_PATH` (default `ledger_data/ledger.jsonl`, :915). `LEDGER_MAX_CONNECTIONS` (default 512, :898).
* **Required env:** `LEDGER_SERVICE_TOKEN`; the server refuses to start without it (:883-896).
* **Auth:** every path except exactly `/health` needs `Authorization: Bearer <LEDGER_SERVICE_TOKEN>` (:403-411). The check runs **before** routing, so an unauthenticated request to an unknown path is 401, not 404. The comparison is on path **plus query string**, so `/health?x=1` requires auth and then 404s (:392-396, :413). 401 body: `{"error":"missing, malformed, or invalid Authorization header (expected: Bearer <token>)"}`.
* **Limits:** one request per connection (`Connection: close`). Request head must arrive within 5 s; anything larger than 16 KiB → **431** (hyper). Body cap 64 KiB: **413**, decided from `Content-Length` before the body is read. Body must arrive within 5 s → **408**. Whole connection 15 s. Over `LEDGER_MAX_CONNECTIONS` → immediate **503** `{"error":"ledger-rust is at its connection limit; retry shortly"}` with `Retry-After: 1` (:171-210, :511-522).
* **Other errors:** unknown path → **404** `{"error":"not found"}`. Wrong method on a known path → **404** too: the match is on `(method, path)` with a catch-all (:441). A write that fails to persist → **500** `{"error":"failed to persist …"}`; the caller must treat it as not recorded.
* **All responses:** `Content-Type: application/json`.

#### `GET /health`
- **Source:** `services/ledger-rust/src/bin/server.rs:414`
- **Purpose:** Liveness.
- **Auth:** none (exact path `/health` only)
- **Response:** 200 `{"status":"ok","service":"ledger-rust"}`

#### `GET /ledger/entries`
- **Source:** `services/ledger-rust/src/bin/server.rs:416`
- **Purpose:** Returns the **entire** ledger in sequence order. There is no filtering or paging.
- **Auth:** Bearer `LEDGER_SERVICE_TOKEN`
- **Response:** 200 JSON array of entries tagged by `kind` (`src/lib.rs:261-266`):
  `kind:"finding"` (`FindingEntry`, `src/lib.rs:172-186`): `seq: u64, finding_id, agent_id, entity_id, leak_category, amount_usd: string|null, value_classification: string|null, decision_confidence: string|null, recorded_at: RFC3339, prev_hash, hash`.
  `kind:"event"` (`EventEntry`, `src/lib.rs:190-202`): `seq, event_id, department, event_type, actor, subject_id, payload_sha256, summary, recorded_at, prev_hash, hash`.

#### `GET /ledger/verify`
- **Source:** `services/ledger-rust/src/bin/server.rs:420`
- **Purpose:** Verifies the hash chain.
- **Auth:** Bearer `LEDGER_SERVICE_TOKEN`
- **Response:** 200 `{"valid":true,"entries":N}`. An empty ledger is valid: `entries:0`. **409** `{"valid":false,"error":"<Debug of LedgerError, e.g. ChainBroken { at_seq: 3, reason: … }>"}` when the chain does not verify (:420-428).

#### `POST /ledger/append`
- **Source:** `services/ledger-rust/src/bin/server.rs:436`
- **Purpose:** Appends one **finding** entry. **Not idempotent:** every call adds a new entry.
- **Auth:** Bearer `LEDGER_SERVICE_TOKEN`
- **Request body:** `LedgerRecordInput` (`src/lib.rs:67-77`, `deny_unknown_fields`): `finding_id: string, agent_id: string, entity_id: string, leak_category: string, amount_usd: string|null, value_classification: string|null, decision_confidence: string|null`. Rules (`src/lib.rs:148-163`, `src/money.rs`):
  * `amount_usd` must be a JSON **string** matching `^(0|[1-9][0-9]{0,14})\.[0-9]{2}$`, at most `"999999999999999.99"`, and not `"0.00"`. A JSON number is rejected.
  * No field may contain `|` or control characters.
  * Optional fields may not be the literal string `"null"`.
- **Response:** **201** with the stored entry: `kind:"finding"` plus the `FindingEntry` fields.
- **Errors:** 400 `{"error":"invalid LedgerRecordInput: …"}` (unknown field, a bad money value, `|`, a control character, `"null"`, or non-UTF-8); 401; 408; 413; 500 (persist failed); 503 (connection cap).

#### `POST /ledger/events`
- **Source:** `services/ledger-rust/src/bin/server.rs:431`
- **Purpose:** Records a generic department **event**. Idempotent by `event_id` (`handle_event` :263-290).
- **Auth:** Bearer `LEDGER_SERVICE_TOKEN`
- **Request body:** `EventInput` (`src/event.rs:13-23`, `deny_unknown_fields`; every field is required). Validation at `src/event.rs:67-92`:

  | Field | Type | Rule |
  |---|---|---|
  | `event_id` | string | 1-128 chars of `[A-Za-z0-9._:-]` |
  | `department` | string | 1-64 chars of `[a-z0-9_]` |
  | `event_type` | string | 1-64 chars of `[a-z0-9_]` |
  | `actor` | string | 1-64 chars of `[a-z0-9_]` |
  | `subject_id` | string | 1-128 chars of `[A-Za-z0-9._:-]` |
  | `payload_sha256` | string | exactly 64 lowercase hex |
  | `summary` | string | 1-280 Unicode chars, no control characters |

- **Response:**
  * **201** with the new entry (`kind:"event"` plus the `EventEntry` fields).
  * **200** with the existing entry for an identical retry.
  * **409** `{"error":"event_id … already recorded with different content …","event_id":…}`.
- **Errors:** 400 `{"error":"invalid event: …"}`; 401; 408; 413; 500; 503.

---

## 3. detection-py (Revenue Recovery detection agents)

* **Language / entry:** Python FastAPI. App in `services/detection-py/src/api.py`. Started with `cd services/detection-py/src && python3 serve.py [--host H] [--port P]` (`src/serve.py:2,155-173`).
* **Bind:** CLI flags `--host` (default `127.0.0.1`) and `--port` (default `8000`) (`src/serve.py:157-158`). **No env var for bind or port.**
* **Required env:** `ZBM_SERVICE_TOKEN`; the service refuses to start without it (`api.py:73-87`). orchestrator-go sends the same value as `DETECTION_SERVICE_TOKEN`.
* **Auth:** Bearer `ZBM_SERVICE_TOKEN` on every route except `/health` (`api.py:90-117`).
* **Limits (`_BodyLimitMiddleware` :198-291, before routing and auth):**
  * Head (path + query + headers) over 16 KiB → **431**. `Content-Length` that is not a digit or is repeated → **400**.
  * Per-route body cap → **413**. The cap is computed at import by `request_limits.body_limit_for` from the worst-case legal batch, plus 25%. Evaluated in this environment (pydantic 2.13.5):

    | Route | Body cap (bytes) |
    |---|---|
    | orders routes | 37,748,736 |
    | renewal-never-triggered | 2,097,152 |
    | server-side-attribution | 1,048,576 |
    | cross-channel-attribution | 1,048,576 |
    | platform-integration | 1,048,576 |
    | contract-pricing-term-drift | 2,097,152 |
    | correlation/overlaps | 11,534,336 |
    | any other path | 65,536 |

  * Only one "heavy" request at a time: a body over 256 KiB, or chunked. A second one gets **503** with `Retry-After: 1`.
  * Body read deadline 30 s → **408**.
  * Every list is capped at 1,000 items (`MAX_BATCH_ITEMS` :190) → **422** `too_long`.
* **Body parsing:** `Content-Type` must be `application/json` or `application/*+json`, else **415** (`wire_body` :316-336). Money values must be JSON strings; a JSON number is rejected.
* **422 shape:** `{"detail":[{"type","loc","msg"}]}`, never echoing input (:312-340). An agent that cannot produce a valid finding for an item → **422**, one entry per item, `type:"agent_value_error"`, **and no findings at all** (:496-518).
* `/docs`, `/redoc` and `/openapi.json` are disabled (:133-135).

#### `GET /health`
- **Source:** `services/detection-py/src/api.py:398` · **Auth:** none · **Response:** 200 `{"status":"ok","service":"detection-py","data_source":"non-live"}`

#### Fixture routes (dev/test data, read-only)
All need Bearer `ZBM_SERVICE_TOKEN` and take no body. Each returns 200 with a JSON array read from repo-root `fixtures/` via `src/fixtures_loader.py`.

| Method & path | Source | Returns (model, file:line) |
|---|---|---|
| `GET /fixtures/orders` | `api.py:407` | `Order[]` (`src/zbm_schema/__init__.py:160`) |
| `GET /fixtures/customers` | `api.py:412` | `Customer[]` (`src/zbm_schema/__init__.py:115`); the route has no return annotation |
| `GET /fixtures/subscriptions` | `api.py:417` | `Subscription[]` (`src/zbm_schema/__init__.py:207`) |
| `GET /fixtures/tier2/server-side-events` | `api.py:422` | `ServerSideAttributionEvent[]` (`src/zbm_schema/tier2.py:21`) |
| `GET /fixtures/tier2/channel-touchpoints` | `api.py:427` | `ChannelTouchpoint[]` (`src/zbm_schema/tier2.py:30`) |
| `GET /fixtures/tier2/platform-connections` | `api.py:432` | `PlatformConnectionStatus[]` (`src/zbm_schema/tier2.py:38`) |
| `GET /fixtures/tier2/contract-terms` | `api.py:437` | `ContractTerm[]` (`src/zbm_schema/tier2.py:51`) |

#### Agent routes
All need Bearer `ZBM_SERVICE_TOKEN`. They return 200 `{"findings": Finding[]}` (`FindingsResponse` :374; `Finding` at `src/zbm_schema/__init__.py:234`: `finding_id, agent_id, leak_category, entity_type, entity_id, customer_id, cause_certainty, cause_description, recoverable_value: {amount_usd: "<string>", classification, confidence}|null, detected_at`). Errors: 401, 408, 413, 415, 422, 503 (heavy cap).

| Method & path | Source | Purpose | Request body (model :line → fields) |
|---|---|---|---|
| `POST /agents/affiliate-coupon-extension/detect` | `api.py:521` | Affiliate coupon-extension leak detection | `OrdersRequest` (:346) → `orders: list[Order]` (≤1000) |
| `POST /agents/discount-misuse/detect` | `api.py:526` | Discount misuse detection | `OrdersRequest` (:346) |
| `POST /agents/abandoned-cart-coverage/detect` | `api.py:531` | Abandoned-cart recovery coverage gaps | `OrdersRequest` (:346) |
| `POST /agents/renewal-never-triggered/detect` | `api.py:536` | Subscriptions whose renewal never fired | `SubscriptionsRequest` (:350) → `subscriptions: list[Subscription]` (≤1000) |
| `POST /agents/server-side-attribution/detect` | `api.py:541` | Pixel vs server-side attribution gaps | `ServerSideEventsRequest` (:354) → `events: list[ServerSideAttributionEvent]` |
| `POST /agents/cross-channel-attribution/detect` | `api.py:546` | Cross-channel attribution (grouped per `order_id`) | `ChannelTouchpointsRequest` (:358) → `touchpoints: list[ChannelTouchpoint]` |
| `POST /agents/platform-integration/detect` | `api.py:554` | Platforms the client uses but has not connected | `PlatformConnectionsRequest` (:362) → `statuses: list[PlatformConnectionStatus]` |
| `POST /agents/contract-pricing-term-drift/detect` | `api.py:559` | Billed vs contracted price drift | `ContractTermsRequest` (:366) → `terms: list[ContractTerm]` |

Item models:

* `Order` (`zbm_schema/__init__.py:160`): `order_id, customer_id, placed_at, status, line_items[] (bounded), discounts[], affiliate|null, source_platform, recovery_attempted=false`
* `Subscription` (`:207`): `subscription_id, customer_id, plan_price_usd (positive money string), renewal_interval_days, last_renewal_at|null, next_renewal_due_at, status`
* `ServerSideAttributionEvent` (`tier2.py:21`): `order_id, channel, order_value_usd, pixel_attributed, server_confirmed`
* `ChannelTouchpoint` (`tier2.py:30`): `order_id, channel, touchpoint_sequence (≥1), is_paid_channel, is_credited_conversion_channel`
* `PlatformConnectionStatus` (`tier2.py:38`): `client_id, platform, client_reports_using_it, integration_connected`
* `ContractTerm` (`tier2.py:51`): `term_id, client_id, term_type, contracted_value_usd, actual_billed_value_usd, period_label`

#### `POST /correlation/overlaps`
- **Source:** `services/detection-py/src/api.py:566`
- **Purpose:** Returns the entities claimed by more than one agent (`zbm_schema/correlation.py:19`).
- **Auth:** Bearer `ZBM_SERVICE_TOKEN`
- **Request body:** `FindingsRequest` (:370) → `findings: list[Finding]` (≤1000)
- **Response:** 200 `{ "<entity_id>": Finding[] , … }`
- **Errors:** 401, 408, 413, 415, 422, 503

---

## 4. fulfillment-py (missed-call / follow-up / dossier / completion tracking)

* **Language / entry:** Python FastAPI. Started with `cd services/fulfillment-py/src && python3 -m api` (`main()` at `src/api.py:2096-2118`).
* **Bind:** `FULFILLMENT_BIND_ADDR` (default `127.0.0.1`) and `FULFILLMENT_PORT` (default `8091`) (`api.py:2101-2102`).
* **Required env:** `FULFILLMENT_SERVICE_TOKEN`; the service refuses to start without it (`api.py:97-109`). Optional settings that are validated at start (an invalid value refuses start-up):
  * `FULFILLMENT_CONTACT_WINDOW` (`HH:MM-HH:MM` inside 08:00-21:00)
  * `FULFILLMENT_CONTACT_MAX_ATTEMPTS_PER_24H`
  * `FULFILLMENT_CONTACT_MIN_SPACING_MINUTES`
  * `FULFILLMENT_COUNTRY_ZONES`
  * `FULFILLMENT_BODY_READ_TIMEOUT_S` (≤30)
  * `FULFILLMENT_SIP_DIALER=in_memory` and `FULFILLMENT_SYSTEM_OF_RECORD=in_memory` are demo-only opt-ins. The defaults are the honest "not wired" stand-ins (`api.py:1556-1590`).
* **Auth:** Bearer `FULFILLMENT_SERVICE_TOKEN` on every route except `/health` (`api.py:113-139`).
* **Limits:**
  * Bodies up to **4 MiB** (`_MAX_BODY_BYTES` :176) → **413** `{"detail":"request body exceeds 4194304 bytes; split the batch"}`. Refusals decided from the head alone are delayed 250 ms.
  * Head 16 KiB (431 / 400).
  * Body deadline 30 s, with a minimum rate of 1 KiB/s after 5 s → **408**.
  * At most 1,000 items per list or map. JSON shape pre-scan: more than 32,000 members, more than 4,000 containers, or depth over 32 → **422** (`json_too_many_members` / `json_too_many_containers` / `json_too_deep`).
  * Large bodies (over 64 KiB) are rate-budgeted at 32 MiB/s; a request not admitted within 2 s → **503** with `Retry-After: 1`. The in-flight byte budget is 64 MiB → **503**.
  * `limit_concurrency` 128 → **503** from uvicorn (`src/http_limits.py:133-142`).
* **Body parsing:** `Content-Type` must be JSON, else **415** `"expected application/json"` (`api.py:1335`). Request models use `extra="forbid"` (`_Req` :1690); a stale client still sending `now` gets a 422.
* **422 shape:** `{"detail":[…≤20 errors], "error_count": N, "truncated"?: true}`, kept under 8 KiB (`api.py:1478-1545`).
* **State:** in-memory only. Dossier store cap 100,000 customers and 10,000 history entries per list; over the cap → **503** with `Retry-After: 60`, nothing applied. Dedupe maps have a 24 h TTL and a cap of 100,000 (`api.py:1651-1689`).

#### `GET /health`
- **Source:** `services/fulfillment-py/src/api.py:1783` · **Auth:** none · **Response:** 200 `{"status":"ok","service":"fulfillment-py","data_source":"non-live"}`

#### Fixture routes
All need Bearer `FULFILLMENT_SERVICE_TOKEN` and take no body.

| Method & path | Source | Response 200 |
|---|---|---|
| `GET /fixtures/call-events` | `api.py:1792` | `CallEvent[]` (`src/fulfillment_schema/__init__.py:145`) |
| `GET /fixtures/appointments` | `api.py:1797` | `Appointment[]` (`:241`) |
| `GET /fixtures/dossiers` | `api.py:1802` | `CustomerDossier[]` (`:215`) |

#### `POST /agents/missed-call-detection/detect`
- **Source:** `api.py:1809` · **Purpose:** Turns missed calls into follow-up tasks. · **Auth:** Bearer
- **Request body:** `CallEventsRequest` (`api.py:1732`) → `call_events: list[CallEvent]` (≤1000).
  `CallEvent` (`fulfillment_schema/__init__.py:145`): `call_id, customer_id|null, phone_number (E.164), direction, status, started_at, ended_at|null, duration_seconds (0-86400), voicemail_transcript (≤10000)|null, line_id`
- **Response:** 200 `{"tasks": FollowUpTask[]}` (`TasksResponse` :1740).
  `FollowUpTask` (`:197`): `task_id, purpose, channel (call|sms|email|human_handoff), customer_id, source_call_id, source_appointment_id, created_at, due_at, attempt_number (1-100), status (pending|sent|failed|completed|escalated), reason (1-2000)`
- **Errors:** 401, 408, 413, 415, 422, 503

#### `POST /agents/appointment-tracking/detect`
- **Source:** `api.py:1818` · **Purpose:** Finds overdue or unconfirmed appointments and creates tasks. · **Auth:** Bearer
- **Request body:** `AppointmentsRequest` (:1736) → `appointments: list[Appointment]` (≤1000).
  `Appointment` (`:241`): `appointment_id, customer_id, scheduled_at, service_type, status (scheduled|confirmed|completed|no_show|cancelled), completion_confirmed_at|null, technician_id|null`
- **Response:** 200 `{"tasks": FollowUpTask[]}` · **Errors:** as above

#### `POST /agents/followup-sequencing/escalate`
- **Source:** `api.py:1827` · **Purpose:** Escalates a FAILED follow-up task to the next channel. When the sequence is exhausted, it records a `no_resolution` resolution with a write-back (deduplicated per task). · **Auth:** Bearer
- **Request body:** `EscalateRequest` (:1753) → `task: FollowUpTask`
- **Response:** 200 `{"next_task": FollowUpTask|null, "sequence_exhausted": bool, "resolution": ResolutionRecord|null}` (:1843, :1883)
- **Errors:** **409** if the task status is not `failed` (:1836-1841); **503** with `Retry-After: 60` if the dedupe state is full; 401/408/413/415/422

#### `POST /agents/callback-orchestration/run`
- **Source:** `api.py:1897` · **Purpose:** Dials the pending callback tasks through the outbound gate: quiet hours (server clock), per-number and per-customer limits, recipient time zone. Dials are serialized and deduplicated per `task_id` for 24 h. · **Auth:** Bearer
- **Request body:** `OrchestrateRequest` (:1757):
  * `tasks: list[FollowUpTask]` (≤1000)
  * `phone_by_call_id: {EntityId: PhoneE164}` (≤1000)
  * `line_by_call_id: {EntityId: EntityId}` = {}
  * `timezone_by_call_id: {EntityId: str(1-64)}` = {}. A call with no time zone entry is not dialed.
- **Response:** 200 `{"outcomes":[{task_id, attempted, skip_reason, dial_placed: bool|null, sla_breached, resulting_task_status}], "gate": <gate status object (see /gate/status)>}` (:1902-1971). With the default `NotWiredSipDialer`, no call is actually placed.
- **Errors:** 401/408/413/415/422/503

#### `GET /gate/status`
- **Source:** `api.py:1974` · **Purpose:** Outbound-gate capacity metrics for monitoring. · **Auth:** Bearer · **Body:** none
- **Response:** 200 `{tracked_keys, max_tracked_keys, utilization, near_capacity, at_capacity, new_keys_last_hour, max_new_keys_per_hour, new_key_budget_exhausted, new_key_burst, new_key_tokens_available, new_key_refill_per_minute, new_key_burst_exhausted}` (`src/outbound_gate.py:240-264`)

#### `POST /agents/customer-dossier/update`
- **Source:** `api.py:1983` · **Purpose:** Folds call events and appointments into per-customer dossiers held in memory. Returns only the dossiers that changed. · **Auth:** Bearer
- **Request body:** `DossierUpdateRequest` (:1744) → `call_events: list[CallEvent]` = [], `appointments: list[Appointment]` = [] (each ≤1000)
- **Response:** 200 `{"dossiers": CustomerDossier[]}`. `CustomerDossier` (`:215`): `customer_id, name, phone_numbers[], first_contact_at, last_contact_at, call_history[], appointment_history[], open_task_ids[], lifetime_value|null, tags[], notes[]`
- **Errors:** **503** with `Retry-After: 60` when the store or a history list is full (nothing applied); 401/408/413/415/422

#### `POST /agents/resolution-writeback/resolve`
- **Source:** `api.py:2018` · **Purpose:** Records terminal resolutions and attempts a write-back to the system of record. The default is `NotConfiguredSystemOfRecord`, so `write_back_status` reports that it is not configured. · **Auth:** Bearer
- **Request body:** `ResolveRequest` (:1775) → `events: list[TerminalEventIn]` (≤1000).
  `TerminalEventIn` (:1768): `entity_type: "call"|"appointment"|"task"`, `entity_id: TaskId`, `customer_id: EntityId|null`, `resolution_type: booked|rescheduled|cancelled|escalated_to_human|confirmed_complete|no_resolution`
- **Response:** 200 `{"records": ResolutionRecord[]}`. `ResolutionRecord` (`:281`): `resolution_id, entity_type, entity_id, customer_id, resolution_type, resolved_at, write_back_status, write_back_detail`
- **Errors:** 401/408/413/415/422/503

---

## 5. apps/dashboard-ts (Next.js Revenue Recovery dashboard)

* **Language / entry:** TypeScript, Next.js 16.3.8 (`apps/dashboard-ts/package.json`). Started with `npm start` → `node scripts/serve.mjs start` → `next start -H $DASHBOARD_BIND_ADDR`.
* **Bind:** `DASHBOARD_BIND_ADDR` (default `127.0.0.1`; `-H` in the args is refused) (`scripts/serve.mjs:24-41`). Port: Next's own `-p` / `PORT`, default 3000 (`scripts/serve.mjs:7`).
* **Server-side env:** `ORCHESTRATOR_URL` (default `http://localhost:8080`), `ORCHESTRATOR_SERVICE_TOKEN` (required for a 200), and `ORCHESTRATOR_TIMEOUT_MS` (default 10000, at most 600000) (`src/lib/api.ts:25-30,61-62`). The token is never sent to the browser (no `NEXT_PUBLIC_` prefix).
* **Auth:** **none.** The dashboard has no authentication of its own (`scripts/serve.mjs:52-56` warns when it binds a non-loopback address).
* **Status mapping** (`src/lib/load-outcome.ts:29-38`):
  * **200**: findings were read and the ledger verified, including a verified-empty ledger.
  * **503**: the token is not set, or the orchestrator is unreachable or timed out.
  * **502**: the orchestrator answered 401/403 or another non-2xx, sent a non-contract body, or reported `ledger_verify.valid=false`.

#### `GET /` (also `HEAD`)
- **Source:** `apps/dashboard-ts/src/proxy.ts:21-35` (status decision) and `src/app/page.tsx` (render, `dynamic = "force-dynamic"`)
- **Purpose:** HTML page listing recorded findings. The proxy calls orchestrator `GET /revenue-recovery/findings` once per request and hands the result to the page through an internal request header. Any client-supplied copy of that header is stripped (`src/lib/handoff.ts`).
- **Auth:** none
- **Response:** `text/html` with status 200, 502 or 503 as mapped above.
- **Errors:** **405** with `Allow: GET, HEAD` for any other method on `/` (`proxy.ts:28-29`).

#### `GET /healthz`
- **Source:** `apps/dashboard-ts/src/app/healthz/route.ts:16`
- **Purpose:** Monitoring endpoint. It performs the same read as `/` and returns JSON with `cache-control: no-store`.
- **Auth:** none
- **Response:**

  | Status | Body |
  |---|---|
  | 200 | `{"status":"ok","ledger_entries":N}` |
  | 503 | `{"status":"unavailable","error":"…","correlation_id":"…"}` |
  | 502 | `{"status":"upstream_error","error":"…","correlation_id":"…"}` |
  | 502 | `{"status":"ledger_invalid","ledger_entries":N,"error":"evidence ledger did not verify: …"}` |

  (`route.ts:4-27`)

Other paths: Next.js serves its own build assets under `/_next/*`. That is framework behaviour, not application routes.

---

## 6. onboarding-py (Onboarding department: client lane, ZBC creator lane, ZBC brand lane)

* **Language / entry:** Python FastAPI. Started with `cd services/onboarding-py/src && python3 -m api` (`main()` at `src/api.py:740-749`, which runs the hardened launcher `src/serve.py`).
* **Bind:** `ONBOARDING_BIND_ADDR` (default `127.0.0.1`) and `ONBOARDING_PORT` (default `8200`) (`api.py:747-748`).
* **Required env:** `ONBOARDING_SERVICE_TOKEN`; the service refuses to start without it (`api.py:91-99`).
* **Optional wiring:** each dependency below is wired only when its env vars are set; otherwise it is a fail-closed stand-in (`api.py:143-160`).

  | Dependency | Env vars | Without them |
  |---|---|---|
  | ledger-rust | `LEDGER_SERVICE_URL` + `LEDGER_SERVICE_TOKEN` | every recorded action → **503** `proceeded:false` |
  | detection-py | `DETECTION_SERVICE_URL` + `DETECTION_SERVICE_TOKEN` | audit → **502** |
  | Compliance (38) | `COMPLIANCE_SERVICE_URL` + `COMPLIANCE_SERVICE_TOKEN` + `COMPLIANCE_CALLER_TOKEN` | stand-in |
  | Andre's approval key | `ONBOARDING_ANDRE_APPROVAL_KEY` | nothing attributed to Andre is possible |

* **Auth:** Bearer `ONBOARDING_SERVICE_TOKEN` on every route except `/health`. There are **no caller-identity headers.** Andre-only actions carry an HMAC approval token in the **body** (see the escalation and playbook routes).
* **Request limits (`InputLimits` `api.py:331-398`; `config.py:82-83,141`):**
  * Request target over 8,192 bytes → **414**; head over 16 KiB → **431**; `Content-Length` not a digit → **400**.
  * Body over 1 MiB → **413**. Env overrides: `ONBOARDING_MAX_BODY_BYTES`, `ONBOARDING_MAX_REQUEST_TARGET_BYTES`.
  * Body not received within 30 s → **408**.
  * Every body is scanned for credentials under a CPU budget; a scan over budget → **422** `scan_budget_exceeded`. A full scan queue → **503** with `Retry-After` and `{"detail":…, "proceeded": false}` (`api.py:471-485`).
  * The launcher limits concurrency (beyond it uvicorn answers 503).
* **Request models** extend `Inbound` (`src/onboarding_schema/__init__.py:73`), which sets `extra="forbid"`.
* **Errors on every route** (`api.py:467-556`; classes in `src/service.py:139-165`):

  | Status | Cause / body |
  |---|---|
  | 400 | `OnboardingError` base |
  | 403 | `Refused` |
  | 404 | `NotFound` |
  | 409 | `Conflict`; also `OutboundBlocked` (a guardrail blocked client-facing text), body `{"detail","rule"}` |
  | 422 | `Invalid` and validation errors: `{"detail":[{loc,msg,type}] ≤50}` |
  | 502 | `UpstreamUnavailable` |
  | 503 | ledger write failed: `proceeded:false, ledger_write:"not_recorded"` |
  | 503 | ledger outcome unknown: `proceeded:"unknown"`, `Retry-After: 1`, `retry` hint (retrying the identical request is safe) |
  | 503 | ledger failed after outside effects: `proceeded:true, completed:false, outside_effects_done:[…]` |
  | 500 | `{"detail":"internal error"}` |

  All error and success bodies pass through a credential scrubber.
* **State:** in-memory (see the summary table).

**40 endpoints.**

#### `GET /health`

- **Source:** `services/onboarding-py/src/api.py:561`
- **Purpose:** Liveness.
- **Auth:** none (open)
- **Request body:** none
- **Response:** 200 — `{"status": "ok", "service": "onboarding-py", "data_source": "non-live"}` (services/onboarding-py/src/api.py:563).
- **Errors raised on this code path:** none found statically

#### `GET /intelligences`

- **Source:** `services/onboarding-py/src/api.py:565`
- **Purpose:** List the department's intelligence modules.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Request body:** none
- **Response:** 200 — JSON array from `registry()` (services/onboarding-py/src/intelligences/__init__.py:37): one object per intelligence with keys `number`, `name`, `phase`, `status`.
- **Errors raised on this code path:** none found statically

#### `POST /onboarding/clients`

- **Source:** `services/onboarding-py/src/api.py:571`
- **Purpose:** Start onboarding for a client (client or zbc_brand lane).
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Request body:** JSON `StartClientRequest` (services/onboarding-py/src/onboarding_schema/requests.py:36); `model_config = ConfigDict(extra="forbid")`
    - `client_id: SubjectId`
    - `lane: Literal["client", "zbc_brand"] = "client"`
    - `business_name: Annotated[str, StringConstraints(min_length=1, max_length=200)]`
    - `signer: Person`
    - `login_holder: Optional[Person] = None`
    - `time_zone: Name64`
    - `quiet_hours_start: Optional[Annotated[str, StringConstraints(pattern=r"^\d{2}:\d{2}$")]] = None`
    - `quiet_hours_end: Optional[Annotated[str, StringConstraints(pattern=r"^\d{2}:\d{2}$")]] = None`
    - `preferred_channel: Optional[Channel] = None`
    - `deal_size_usd: Optional[PositiveMoney] = None`
    - `contract: Optional[ContractTerms] = None`
- **Response:** 201 JSON — from `start_client` (services/onboarding-py/src/service.py:573): via _start_response (services/onboarding-py/src/service.py:680), _complete_start (services/onboarding-py/src/service.py:686).
- **Errors raised on this code path:** 409 `Conflict`, 409 `OutboundBlocked`, 422 `Invalid`, 503 `LedgerWriteAfterEffects`

#### `GET /onboarding/clients/{client_id}`

- **Source:** `services/onboarding-py/src/api.py:575`
- **Purpose:** Full view of one client's onboarding state.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** none
- **Response:** 200 JSON — from `view` (services/onboarding-py/src/service.py:1614): object with keys `client_id`, `lane`, `business_name`, `exited`, `profile`, `access`, `permission_receipts_sent`, `baseline`, `plan`, `commitments`, `escalations`, `soft_issues`, `first_win`, `activation`, `memory`.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /onboarding/clients/{client_id}/intake/facts`

- **Source:** `services/onboarding-py/src/api.py:579`
- **Purpose:** Add intake facts to the client profile.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** JSON `FactsRequest` (services/onboarding-py/src/onboarding_schema/requests.py:73); `model_config = ConfigDict(extra="forbid")`
    - `facts: list[FactIn] = Field(max_length=200)`
    - `vertical: Optional[Name64] = None`
- **Response:** 200 JSON — from `add_facts` (services/onboarding-py/src/service.py:1046): object with keys `profile`, `next_question`, `injection_flags`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 422 `ScanBudgetExceeded`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/intake/documents`

- **Source:** `services/onboarding-py/src/api.py:583`
- **Purpose:** Add an intake document (text) to the client.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** JSON `DocumentRequest` (services/onboarding-py/src/onboarding_schema/requests.py:78); `model_config = ConfigDict(extra="forbid")`
    - `name: ShortText`
    - `text: LongText`
- **Response:** 200 JSON — from `add_document` (services/onboarding-py/src/service.py:1103): object with keys `stored`, `injection_flags`, `extraction`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `ScanBudgetExceeded`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/messages`

- **Source:** `services/onboarding-py/src/api.py:587`
- **Purpose:** Send a client message; returns the assistant's reply/intent.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** JSON `MessageRequest` (services/onboarding-py/src/onboarding_schema/requests.py:83); `model_config = ConfigDict(extra="forbid")`
    - `text: Annotated[str, StringConstraints(min_length=1, max_length=5000)]`
- **Response:** 200 JSON — from `message` (services/onboarding-py/src/service.py:1115): object with keys `intent`, `reply`, `next_question`, `injection_flags`, `escalation`, `account_changed`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `OutboundBlocked`, 422 `ScanBudgetExceeded`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/recap`

- **Source:** `services/onboarding-py/src/api.py:591`
- **Purpose:** Produce a recap for the client.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** none
- **Response:** 200 JSON — from `recap` (services/onboarding-py/src/service.py:1158): object with keys `recap`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/access/website-scan`

- **Source:** `services/onboarding-py/src/api.py:595`
- **Purpose:** Scan supplied website HTML to detect platforms and needed access.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** JSON `WebsiteScanRequest` (services/onboarding-py/src/onboarding_schema/requests.py:87); `model_config = ConfigDict(extra="forbid")`
    - `html: Annotated[str, StringConstraints(max_length=500_000)]`
- **Response:** 200 JSON — from `website_scan` (services/onboarding-py/src/service.py:1183): object with keys `detected`, `access_requests`, `injection_flags`, `site_fetch`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/access/grants`

- **Source:** `services/onboarding-py/src/api.py:599`
- **Purpose:** Record a platform access grant from the client.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** JSON `AccessGrantIn` (services/onboarding-py/src/onboarding_schema/__init__.py:244); `model_config = ConfigDict(extra="forbid")`
    - `platform: Platform`
    - `account_id: AccountId`
    - `account_name: Optional[ShortText] = None`
    - `account_type: AccountType`
    - `granted_role: Annotated[str, StringConstraints(min_length=1, max_length=64)]`
    - `granted_by_email: Optional[ShortText] = None`
    - `account_last_activity_at: Optional[AwareDatetime] = None`
    - `scopes: list[Annotated[str, StringConstraints(max_length=128)]] = Field(default_factory=list)`
    - `job: Annotated[str, StringConstraints(min_length=1, max_length=64)] = "audit"`
- **Response:** 200 JSON — from `add_grant` (services/onboarding-py/src/service.py:1210): object with keys `verification`, `client_message`, `permission_receipt`, `permission_receipt_status`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `OutboundBlocked`, 422 `Invalid`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/access/credentials`

- **Source:** `services/onboarding-py/src/api.py:603`
- **Purpose:** Credential offer (vault tier) — body is never read; the vault stand-in refuses.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** none read — the body is deliberately never read or parsed (services/onboarding-py/src/api.py:605).
- **Response:** 200 JSON — from `offer_credential` (services/onboarding-py/src/service.py:1237): .
- **Errors raised on this code path:** 403 `Refused`, 404 `NotFound`, 409 `Conflict`, 409 `OutboundBlocked`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/audit`

- **Source:** `services/onboarding-py/src/api.py:608`
- **Purpose:** Run the Revenue Recovery audit on supplied account data (calls detection-py).
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** JSON `AuditRequest` (services/onboarding-py/src/onboarding_schema/requests.py:94); `model_config = ConfigDict(extra="forbid")`
    - `account_data: dict[Name64, list[dict]]`
    - `observed_monthly_revenue_usd: Optional[Money] = None`
    - `risk_signals: list[Name64] = Field(default_factory=list)`
- **Response:** 200 JSON — from `audit` (services/onboarding-py/src/service.py:1257): object with keys `baseline`, `findings`, `rejected_findings`, `risk`, `soft_issue`, `escalation`, `client_facing_numbers`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `OutboundBlocked`, 502 `UpstreamUnavailable`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/plan`

- **Source:** `services/onboarding-py/src/api.py:612`
- **Purpose:** Build the client's plan from their priorities.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** JSON `PlanRequest` (services/onboarding-py/src/onboarding_schema/requests.py:104); `model_config = ConfigDict(extra="forbid")`
    - `client_priorities: list[Name64] = Field(max_length=20)`
- **Response:** 200 — `MergedPlan` model as JSON (services/onboarding-py/src/onboarding_schema/__init__.py:348): `client_id`, `items` (list[PlanItem]), `disagreements`, `recommended_order`, `summary_text` — from `plan` (services/onboarding-py/src/service.py:1348-1360). No audit baseline yet → 409 `Conflict`; unknown client → 404.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/plan/choices`

- **Source:** `services/onboarding-py/src/api.py:616`
- **Purpose:** Record the client's plan choice for a topic.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** JSON `PlanChoiceRequest` (services/onboarding-py/src/onboarding_schema/requests.py:108); `model_config = ConfigDict(extra="forbid")`
    - `topic: Name64`
    - `choice: Literal["keep_my_order", "accept_recommendation"]`
- **Response:** 200 JSON — from `plan_choice` (services/onboarding-py/src/service.py:1362): object with keys `plan`, `choices`, `plan_agreed`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/setup-plan`

- **Source:** `services/onboarding-py/src/api.py:620`
- **Purpose:** Produce the setup plan.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** none
- **Response:** 200 — object with keys `proposals`, `dashboard`, `reporting`, `executed` (services/onboarding-py/src/intelligences/i05_setup.py:53-70) via `setup_plan` (services/onboarding-py/src/service.py:1380).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/account-changes`

- **Source:** `services/onboarding-py/src/api.py:624`
- **Purpose:** Request an account change (needs client approvals).
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** JSON `AccountChangeRequest` (services/onboarding-py/src/onboarding_schema/requests.py:120); `model_config = ConfigDict(extra="forbid")`
    - `change_id: Name64`
    - `platform: Name64`
    - `description: ShortText`
    - `client_approvals: list[ClientApprovalIn] = Field(default_factory=list)`
- **Response:** 200 JSON — from `account_change` (services/onboarding-py/src/service.py:1388): object with keys `executed`.
- **Errors raised on this code path:** 403 `Refused`, 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/momentum`

- **Source:** `services/onboarding-py/src/api.py:628`
- **Purpose:** Momentum check (next finding / phase).
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** none
- **Response:** 200 JSON — from `momentum` (services/onboarding-py/src/service.py:1406): object with keys `finding_id`, `score`, `reason`, `rejected`, `phase`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/first-win`

- **Source:** `services/onboarding-py/src/api.py:632`
- **Purpose:** Record the client's first win from a finding.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** JSON `FirstWinRequest` (services/onboarding-py/src/onboarding_schema/requests.py:127); `model_config = ConfigDict(extra="forbid")`
    - `finding_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]`
- **Response:** 200 JSON — from `first_win` (services/onboarding-py/src/service.py:1415): object with keys `first_win`, `value`, `recommend_question`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `OutboundBlocked`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/recommend-score`

- **Source:** `services/onboarding-py/src/api.py:636`
- **Purpose:** Submit the client's 0-10 recommend score.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** JSON `RecommendScoreRequest` (services/onboarding-py/src/onboarding_schema/requests.py:131); `model_config = ConfigDict(extra="forbid")`
    - `score: int = Field(ge=0, le=10)`
- **Response:** 200 JSON — from `recommend_score_submit` (services/onboarding-py/src/service.py:1437): object with keys `path`, `client_message`, `soft_issue`, `escalation`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `OutboundBlocked`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/tick`

- **Source:** `services/onboarding-py/src/api.py:640`
- **Purpose:** Advance time-based onboarding checks for the client.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** none
- **Response:** 200 JSON — from `tick` (services/onboarding-py/src/service.py:1456): object with keys `stuck`, `commitment_actions`, `escalation_deliveries`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `OutboundBlocked`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/issues/{issue_id}/outcome`

- **Source:** `services/onboarding-py/src/api.py:644`
- **Purpose:** Record the outcome of a soft issue.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`, `issue_id`
- **Request body:** JSON `IssueOutcomeRequest` (services/onboarding-py/src/onboarding_schema/requests.py:135); `model_config = ConfigDict(extra="forbid")`
    - `resolved: bool`
    - `note: ShortText = ""`
- **Response:** 200 JSON — from `issue_outcome` (services/onboarding-py/src/service.py:914): object with keys `soft_issue`, `escalation`; via _escalate_issue (services/onboarding-py/src/service.py:904).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/escalations/{escalation_id}/acknowledge`

- **Source:** `services/onboarding-py/src/api.py:648`
- **Purpose:** Andre acknowledges an escalation (approval token in body).
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`; plus body `approval_token` = hex HMAC-SHA256(key=ONBOARDING_ANDRE_APPROVAL_KEY, msg=sha256hex(json ["andre_action","escalation_acknowledge",client_id,escalation_id])) (memory.py:151-160, service.py:931-950); missing/wrong/unconfigured → 403 `Refused`
- **Path params:** `client_id`, `escalation_id`
- **Request body:** JSON `EscalationAckRequest` (services/onboarding-py/src/onboarding_schema/requests.py:143) (body optional); `model_config = ConfigDict(extra="forbid")`
    - `approval_token: Optional[ApprovalToken] = None`
- **Response:** 200 — `Escalation` model as JSON (services/onboarding-py/src/onboarding_schema/__init__.py:425): `escalation_id`, `client_id`, `trigger`, `hard`, `reason`, `snag`, `attempted_resolution`, `raised_at`, `briefing`, `push_delivered`, `push_detail`, `push_attempts`, `client_commitment_text`, `client_commitment_due_at`, `client_message_status`, `commitment_id`, `acknowledged_at`, `resolution`, `resolved_at` — from `acknowledge_escalation` (service.py:944).
- **Errors raised on this code path:** 403 `Refused`, 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/escalations/{escalation_id}/resolve`

- **Source:** `services/onboarding-py/src/api.py:655`
- **Purpose:** Resolve an escalation (Andre approval token in body).
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`; plus body `approval_token` = hex HMAC-SHA256(key=ONBOARDING_ANDRE_APPROVAL_KEY, msg=sha256hex(json ["andre_action","escalation_resolve",client_id,escalation_id,resolution,snag_category])) (memory.py:151-160, service.py:961-967); missing/wrong/unconfigured → 403 `Refused`
- **Path params:** `client_id`, `escalation_id`
- **Request body:** JSON `EscalationResolveRequest` (services/onboarding-py/src/onboarding_schema/requests.py:152); `model_config = ConfigDict(extra="forbid")`
    - `resolution: ShortText`
    - `snag_category: Name64`
    - `approval_token: Optional[ApprovalToken] = None`
- **Response:** 200 — `Escalation` model as JSON (services/onboarding-py/src/onboarding_schema/__init__.py:425) — same fields as acknowledge — from `resolve_escalation` (service.py:961).
- **Errors raised on this code path:** 403 `Refused`, 404 `NotFound`, 409 `Conflict`, 422 `ScanBudgetExceeded`, 503 `LedgerWriteAfterEffects`

#### `GET /onboarding/clients/{client_id}/health`

- **Source:** `services/onboarding-py/src/api.py:659`
- **Purpose:** Client health score/band/early-warning.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** none
- **Response:** 200 JSON — from `health` (services/onboarding-py/src/service.py:1599): object with keys `score`, `band`, `early_warning`, `reasons`, `scorecard`, `phase`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `DELETE /onboarding/clients/{client_id}/memory`

- **Source:** `services/onboarding-py/src/api.py:663`
- **Purpose:** Delete the client's memory.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** none
- **Response:** 200 JSON — from `delete_memory` (services/onboarding-py/src/service.py:1636): object with keys `deleted`.
- **Errors raised on this code path:** 404 `NotFound`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/exit`

- **Source:** `services/onboarding-py/src/api.py:667`
- **Purpose:** Exit the client (export-then-destroy or destroy memory).
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** JSON `ExitRequest` (services/onboarding-py/src/onboarding_schema/requests.py:158); `model_config = ConfigDict(extra="forbid")`
    - `memory_choice: Literal["export_then_destroy", "destroy"] = "export_then_destroy"`
- **Response:** 200 JSON — from `exit` (services/onboarding-py/src/service.py:1646): object with keys `exit_plan`, `memory_export`, `vault_secrets_destroyed`, `executed`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `POST /onboarding/clients/{client_id}/activate`

- **Source:** `services/onboarding-py/src/api.py:671`
- **Purpose:** Activate the client (handoff).
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `client_id`
- **Request body:** none
- **Response:** 200 JSON — from `activate_client` (services/onboarding-py/src/service.py:1662): via _finish_activation (services/onboarding-py/src/service.py:1716).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `GET /onboarding/escalations`

- **Source:** `services/onboarding-py/src/api.py:675`
- **Purpose:** Andre's escalation queue, newest first.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Request body:** none
- **Response:** 200 JSON — from `list_escalations` (services/onboarding-py/src/service.py:1007): returns `list[dict]`.
- **Errors raised on this code path:** none found statically

#### `POST /zbc/creators/applications`

- **Source:** `services/onboarding-py/src/api.py:681`
- **Purpose:** ZBC creator (clipper) application.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Request body:** JSON `ClipperApplication` (services/onboarding-py/src/onboarding_schema/__init__.py:503); `model_config = ConfigDict(extra="forbid")`
    - `creator_id: SubjectId`
    - `legal_name: Annotated[str, StringConstraints(min_length=1, max_length=200)]`
    - `date_of_birth: Optional[Annotated[date, AfterValidator(_dob_not_before_1900)]] = None`
    - `time_zone: Annotated[str, StringConstraints(min_length=1, max_length=64)] = "America/Los_Angeles"`
    - `platforms: list[Annotated[str, StringConstraints(max_length=64)]] = Field(default_factory=list)`
    - `follower_count: int = Field(ge=0)`
    - `avg_engagement_rate: float = Field(ge=0, le=1)`
    - `follower_growth_30d_ratio: float = Field(ge=0, default=0.0)`
    - `fake_follower_ratio: Optional[float] = Field(default=None, ge=0, le=1)`
    - `engagement_pod_signal: float = Field(ge=0, le=1, default=0.0)`
    - `brand_safety_flags: list[Annotated[str, StringConstraints(max_length=64)]] = Field(default_factory=list)`
    - `content_history_posts: int = Field(ge=0, default=0)`
    - `bio: Annotated[str, StringConstraints(max_length=5000)] = ""`
    - `network_fit_tags: list[Annotated[str, StringConstraints(max_length=64)]] = Field(default_factory=list)`
    - `w9_received: bool = False`
    - `creator_agreement_signed: bool = False`
    - `disclosure_training_completed: bool = False`
- **Response:** 201 JSON — from `apply_creator` (services/onboarding-py/src/service.py:1752): object with keys `first_message`, `vetting`, `applicant_message`, `andre_referral`, `activation`; via _complete_apply (services/onboarding-py/src/service.py:1823).
- **Errors raised on this code path:** 409 `Conflict`, 409 `OutboundBlocked`, 422 `Invalid`, 422 `ScanBudgetExceeded`, 503 `LedgerWriteAfterEffects`

#### `POST /zbc/creators/{creator_id}/w9`

- **Source:** `services/onboarding-py/src/api.py:685`
- **Purpose:** Record whether the creator's W-9 was received.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `creator_id`
- **Request body:** JSON `CreatorFlagRequest` (services/onboarding-py/src/onboarding_schema/requests.py:162); `model_config = ConfigDict(extra="forbid")`
    - `received: bool`
- **Response:** 200 JSON — from `creator_flag` (services/onboarding-py/src/service.py:1839): object with keys `creator_id`, `w9_on_file`, `disclosure_training`.
- **Errors raised on this code path:** 404 `NotFound`, 503 `LedgerWriteAfterEffects`

#### `POST /zbc/creators/{creator_id}/disclosure-training`

- **Source:** `services/onboarding-py/src/api.py:689`
- **Purpose:** Record whether the creator completed disclosure training.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `creator_id`
- **Request body:** JSON `CreatorFlagRequest` (services/onboarding-py/src/onboarding_schema/requests.py:162); `model_config = ConfigDict(extra="forbid")`
    - `received: bool`
- **Response:** 200 JSON — from `creator_flag` (services/onboarding-py/src/service.py:1839): object with keys `creator_id`, `w9_on_file`, `disclosure_training`.
- **Errors raised on this code path:** 404 `NotFound`, 503 `LedgerWriteAfterEffects`

#### `POST /zbc/creators/{creator_id}/activate`

- **Source:** `services/onboarding-py/src/api.py:693`
- **Purpose:** Activate a creator.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `creator_id`
- **Request body:** none
- **Response:** 200 JSON — from `activate_creator` (services/onboarding-py/src/service.py:1849): via _activate_creator (services/onboarding-py/src/service.py:1853), _finish_activation (services/onboarding-py/src/service.py:1716).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `POST /zbc/creators/{creator_id}/payments`

- **Source:** `services/onboarding-py/src/api.py:697`
- **Purpose:** Request a creator payment.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `creator_id`
- **Request body:** JSON `CreatorPaymentRequest` (services/onboarding-py/src/onboarding_schema/requests.py:166); `model_config = ConfigDict(extra="forbid")`
    - `amount_usd: PositiveMoney`
- **Response:** 200 JSON — from `creator_payment` (services/onboarding-py/src/service.py:1895): shape not statically determinable (returns `creator_tax.form_1099_status(year, paid, self.config.form_1099_thresholds_usd)`) — UNKNOWN beyond the cited method.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `POST /zbc/creators/{creator_id}/posts/check`

- **Source:** `services/onboarding-py/src/api.py:701`
- **Purpose:** Check a creator's post caption against guardrails.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `creator_id`
- **Request body:** JSON `CaptionRequest` (services/onboarding-py/src/onboarding_schema/requests.py:172); `model_config = ConfigDict(extra="forbid")`
    - `caption: Annotated[str, StringConstraints(max_length=5000)]`
- **Response:** 200 JSON — from `creator_post_check` (services/onboarding-py/src/service.py:1924): object with keys `allowed`, `detail`, `injection_flags`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `OutboundBlocked`, 503 `LedgerWriteAfterEffects`

#### `POST /zbc/brands/{brand_id}/campaigns`

- **Source:** `services/onboarding-py/src/api.py:707`
- **Purpose:** Plan a ZBC brand campaign.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `brand_id`
- **Request body:** JSON `CampaignRequest` (services/onboarding-py/src/onboarding_schema/requests.py:176); `model_config = ConfigDict(extra="forbid")`
    - `campaign_id: SubjectId`
    - `regulated: bool`
    - `wants_owned_addon: bool = False`
    - `requested_budget_usd: Optional[PositiveMoney] = None`
- **Response:** 201 JSON — from `plan_campaign` (services/onboarding-py/src/service.py:1940): object with keys `plan`, `plan_digest`, `phase`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `POST /zbc/brands/{brand_id}/campaigns/{campaign_id}/approve`

- **Source:** `services/onboarding-py/src/api.py:711`
- **Purpose:** Brand approves a planned campaign (plan digest must match).
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `brand_id`, `campaign_id`
- **Request body:** JSON `CampaignApproveRequest` (services/onboarding-py/src/onboarding_schema/requests.py:183); `model_config = ConfigDict(extra="forbid")`
    - `brand_yes_campaign_id: SubjectId`
    - `plan_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]`
- **Response:** 200 JSON — from `approve_campaign` (services/onboarding-py/src/service.py:1956): object with keys `approved`, `plan`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `POST /zbc/brands/{brand_id}/campaigns/{campaign_id}/proving-result`

- **Source:** `services/onboarding-py/src/api.py:715`
- **Purpose:** Record a campaign proving result; returns whether it may scale.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Path params:** `brand_id`, `campaign_id`
- **Request body:** JSON `ProvingResultRequest` (services/onboarding-py/src/onboarding_schema/requests.py:188); `model_config = ConfigDict(extra="forbid")`
    - `views_delivered: int = Field(ge=0)`
    - `clicks: int = Field(ge=0)`
    - `evidence: Literal["observed", "estimated"]`
- **Response:** 200 JSON — from `proving_result` (services/onboarding-py/src/service.py:1974): object with keys `may_scale`, `detail`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `LedgerWriteAfterEffects`

#### `GET /playbook`

- **Source:** `services/onboarding-py/src/api.py:721`
- **Purpose:** Playbook history view.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Request body:** none
- **Response:** 200 JSON — from `playbook_view` (services/onboarding-py/src/service.py:2007): object with keys `history`.
- **Errors raised on this code path:** none found statically

#### `POST /playbook/rules`

- **Source:** `services/onboarding-py/src/api.py:725`
- **Purpose:** Change a playbook rule (Andre approval token in body).
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`; plus body `approval_token` = hex HMAC-SHA256(key=ONBOARDING_ANDRE_APPROVAL_KEY, msg=sha256hex(json [rule_id,version,text])) and `version` must be the rule's next version (memory.py:132-140, 201-211); else 403 `Refused`
- **Request body:** JSON `PlaybookRuleRequest` (services/onboarding-py/src/onboarding_schema/requests.py:194); `model_config = ConfigDict(extra="forbid")`
    - `rule_id: Name64`
    - `version: int = Field(ge=1)`
    - `text: Annotated[str, StringConstraints(min_length=1, max_length=2000)]`
    - `approval_token: ApprovalToken`
- **Response:** 200 JSON — from `change_playbook` (services/onboarding-py/src/service.py:1994): object with keys `rule_id`, `version`, `approved_at`.
- **Errors raised on this code path:** 403 `Refused`, 503 `LedgerWriteAfterEffects`

#### `GET /learning/proposals`

- **Source:** `services/onboarding-py/src/api.py:729`
- **Purpose:** Rule proposals learned from history.
- **Auth:** `Authorization: Bearer <ONBOARDING_SERVICE_TOKEN>`
- **Request body:** none
- **Response:** 200 JSON — from `propose_rules` (services/onboarding-py/src/service.py:1990): object with keys `proposals`, `institutional_patterns`.
- **Errors raised on this code path:** none found statically

---

## 7. creative-py (Creative Production: ZBM advertising + ZBC clipping)

* **Language / entry:** Python FastAPI. Started with `cd services/creative-py/src && python3 serve.py` (`src/serve.py:2`; it runs `uvicorn.run("api:app", …)`).
* **Bind:** `CREATIVE_BIND_ADDR` (default `127.0.0.1`) and `CREATIVE_PORT` (default `8300`) (`src/serve.py:149-150`). `CREATIVE_MAX_CONCURRENCY` defaults to 256.
* **Required env:** `CREATIVE_SERVICE_TOKEN`; the service refuses to start without it (`api.py:214-222`).
* **Optional env:**
  * `CREATIVE_ANDRE_APPROVAL_TOKEN`: Andre's approvals.
  * `CREATIVE_ACTOR_TOKENS`: JSON `{"actor_id":"token"}`; each token at least 16 printable ASCII characters.
  * `CREATIVE_SUPERSEDED_GRACE_HOURS`.
  * `LEDGER_SERVICE_URL` + `LEDGER_SERVICE_TOKEN`: without them every decision is refused.
  * `COMPLIANCE_SERVICE_URL` + `COMPLIANCE_SERVICE_TOKEN` + `COMPLIANCE_CALLER_TOKEN`: the Compliance publish/payout gate client.
* **Auth:**
  * Bearer `CREATIVE_SERVICE_TOKEN` on every route except `/health`.
  * Actions attributed to an actor also need **`X-Creative-Actor-Token`**. A missing or unknown token → **401**. No actor tokens configured → **403** `GuardrailViolation`. A body `actor_id` that differs from the authenticated actor → **403** (`api.py:721-742`).
  * Andre's actions read **`X-Andre-Approval-Token`**. The workflow checks it against `CREATIVE_ANDRE_APPROVAL_TOKEN`; a refusal → **403** `FounderApprovalRefused`.
* **Idempotency:** creating POSTs accept **`Idempotency-Key`** (1-128 printable ASCII). A replay returns the original status and body with **`Idempotent-Replayed: true`**; the same key with a different body → **409**. Without a key, the clip, clearance and licence routes key on the body's own id, and the others derive a key from the content (`api.py:31-50,750-819`).
* **Limits:**
  * Body over 1 MiB → **413** (checked from `Content-Length` and while streaming).
  * Head over 16 KiB → **431**. Body not received within 30 s → **408**.
  * A non-JSON body, or none with a declared length → **415**.
  * Per-route JSON member caps derived from each route's model, and depth over 32 → **422**. The launcher has a 10 s head deadline and a concurrency limit (503) (`api.py:52-90`).
* **Errors on every route** (`_STATUS` map `api.py:203-211`, handlers `api.py:836-882`):

  | Status | Cause |
  |---|---|
  | 400 | other `CreativeError` |
  | 403 | `GuardrailViolation`, `FounderApprovalRefused` |
  | 404 | `NotFound` |
  | 409 | `FrozenError`, `PreconditionFailed`, `RegistryRowBlocked`; `LedgerConflict` (`took_effect:"unknown"`) |
  | 422 | `ValidationFailed` (`{"detail","error","issues"[≤20]}`) and validation errors (bounded body) |
  | 503 | `OutcomeNotRecorded` (`took_effect:"partial"`) or another ledger error (`took_effect:false` or `"unknown"`) |
  | 500 | `{"detail":"internal error","error":"<ExceptionType>"}` |

* **Request bodies** are plain FastAPI body parameters, so FastAPI's own JSON parsing applies. The `*In` models live in `src/api.py:611-681`; the workflow models live under `src/zbm/` and `src/zbc/`. Path ids are typed `IdPath` / `CampaignIdPath` / `VersionPath` (constrained `Path` annotations, `api.py:197-199`).
* **State:** in-memory (see the summary table).

**41 endpoints.**

#### `GET /health`

- **Source:** `services/creative-py/src/api.py:889`
- **Purpose:** Liveness plus ledger/founder-token configuration flags.
- **Auth:** none (open)
- **Request body:** none
- **Response:** 200 — `{status, service: "creative-py", department: "creative_production", ledger_configured: bool, founder_token_configured: bool}` (api.py:889-893).
- **Errors raised on this code path:** none found statically

#### `GET /registry/rows`

- **Source:** `services/creative-py/src/api.py:900`
- **Purpose:** List Platform Rules Registry rows with usability.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Request body:** none
- **Response:** 200 — `{rows: [RegistryRow + usable: bool, usability_reason]}` sorted by row_id; `RegistryRow` at services/creative-py/src/shared/registry.py (api.py:896-902).
- **Errors raised on this code path:** none found statically

#### `GET /registry/rows/{row_id}`

- **Source:** `services/creative-py/src/api.py:904`
- **Purpose:** Read one registry row with usability.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `row_id`
- **Request body:** none
- **Response:** 200 — one `RegistryRow` as JSON plus `usable`, `usability_reason` (api.py:896-906). Unknown row → 404 `NotFound` (via registry.get).
- **Errors raised on this code path:** none found statically

#### `PUT /registry/rows/{row_id}`

- **Source:** `services/creative-py/src/api.py:908`
- **Purpose:** Write a registry row (actor must own ZBM placement-spec or ZBC platform-rules rows).
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Creative-Actor-Token` (actor credential from CREATIVE_ACTOR_TOKENS; body `actor_id`, if sent, must match)
- **Path params:** `row_id`
- **Request body:** JSON `RegistryWriteIn` (services/creative-py/src/api.py:619); `model_config = ConfigDict(extra="forbid")`
    - `actor_id: str | None = Field(default=None, min_length=1, max_length=64)`
    - `row: RegistryRow`
- **Response:** 200 — `{row: <row view>, ledger_event_id}` (api.py:923). Path/body row_id mismatch → 422 `ValidationFailed`; actor owning no registry rows → 403 `GuardrailViolation`.
- **Errors raised on this code path:** 403 `GuardrailViolation`, 422 `ValidationFailed`

#### `POST /rights/clearances`

- **Source:** `services/creative-py/src/api.py:926`
- **Purpose:** Record a rights clearance (idempotent).
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Creative-Actor-Token` (actor credential from CREATIVE_ACTOR_TOKENS; body `actor_id`, if sent, must match)
- **Optional headers:** `idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")`
- **Request body:** JSON `ClearanceIn` (services/creative-py/src/api.py:624); `model_config = ConfigDict(extra="forbid")`
    - `actor_id: str | None = None`
    - `record: ClearanceRecord`
- **Response:** 201 — `{record: ClearanceRecord, ledger_event_id}`; replay with same key and body returns the original body with header `Idempotent-Replayed: true`; same key, different body → 409 `PreconditionFailed` (api.py:926-936).
- **Errors raised on this code path:** none found statically

#### `POST /rights/licenses`

- **Source:** `services/creative-py/src/api.py:938`
- **Purpose:** Record a campaign licence (idempotent).
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Creative-Actor-Token` (actor credential from CREATIVE_ACTOR_TOKENS; body `actor_id`, if sent, must match)
- **Optional headers:** `idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")`
- **Request body:** JSON `LicenseIn` (services/creative-py/src/api.py:629); `model_config = ConfigDict(extra="forbid")`
    - `actor_id: str | None = None`
    - `license: CampaignLicense`
- **Response:** 201 — `{license: CampaignLicense, ledger_event_id}`; idempotent like /rights/clearances (api.py:938-948).
- **Errors raised on this code path:** none found statically

#### `POST /zbm/briefs`

- **Source:** `services/creative-py/src/api.py:951`
- **Purpose:** ZBM: draft a creative brief from client requirements (idempotent).
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Creative-Actor-Token` (actor credential from CREATIVE_ACTOR_TOKENS; body `actor_id`, if sent, must match)
- **Optional headers:** `idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")`
- **Request body:** JSON `DraftBriefIn` (services/creative-py/src/api.py:634); `model_config = ConfigDict(extra="forbid")`
    - `actor_id: str | None = None`
    - `requirements: ClientRequirements`
- **Response:** 201 JSON — from `draft_brief` (services/creative-py/src/zbm/workflow.py:285): `BriefRecord` model (services/creative-py/src/zbm/brief.py:177) — fields: `brief_id`, `client_id`, `status`, `drafted_by`, `fields`, `open_questions`, `issues`, `warnings`, `spec_row_ids`, `maker_summary`, `approved_by`, `approved_at`, `review_issues`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `PreconditionFailed`

#### `GET /zbm/briefs/{brief_id}`

- **Source:** `services/creative-py/src/api.py:959`
- **Purpose:** ZBM: read a brief.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `brief_id`
- **Request body:** none
- **Response:** 200 JSON — from `get_brief` (services/creative-py/src/zbm/workflow.py:278): `BriefRecord` model (services/creative-py/src/zbm/brief.py:177) — fields: `brief_id`, `client_id`, `status`, `drafted_by`, `fields`, `open_questions`, `issues`, `warnings`, `spec_row_ids`, `maker_summary`, `approved_by`, `approved_at`, `review_issues`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /zbm/briefs/{brief_id}/review`

- **Source:** `services/creative-py/src/api.py:963`
- **Purpose:** ZBM: review/approve a brief (approver must differ from drafter).
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Creative-Actor-Token` (actor credential from CREATIVE_ACTOR_TOKENS; body `actor_id`, if sent, must match)
- **Path params:** `brief_id`
- **Request body:** JSON `ActorIn` (services/creative-py/src/api.py:615); `model_config = ConfigDict(extra="forbid")`
    - `actor_id: str | None = Field(default=None, min_length=1, max_length=64)`
- **Response:** 200 JSON — from `review_brief` (services/creative-py/src/zbm/workflow.py:301): `BriefRecord` model (services/creative-py/src/zbm/brief.py:177) — fields: `brief_id`, `client_id`, `status`, `drafted_by`, `fields`, `open_questions`, `issues`, `warnings`, `spec_row_ids`, `maker_summary`, `approved_by`, `approved_at`, `review_issues`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `PreconditionFailed`

#### `POST /zbm/briefs/{brief_id}/jobs`

- **Source:** `services/creative-py/src/api.py:967`
- **Purpose:** ZBM: open a production job for an approved brief (idempotent).
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `brief_id`
- **Optional headers:** `idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")`
- **Request body:** none
- **Response:** 201 JSON — from `open_job` (services/creative-py/src/zbm/workflow.py:328): `ProductionJob` model (services/creative-py/src/zbm/workflow.py:147) — fields: `job_id`, `brief_id`, `commissions`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `PreconditionFailed`, 503 `OutcomeNotRecorded`

#### `POST /zbm/jobs/{job_id}/work`

- **Source:** `services/creative-py/src/api.py:977`
- **Purpose:** ZBM: submit a work deliverable to a job (idempotent).
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `job_id`
- **Optional headers:** `idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")`
- **Request body:** JSON `WorkSubmission` (services/creative-py/src/zbm/workflow.py:154); `model_config = ConfigDict(extra="forbid")`
    - `deliverable_id: SafeId`
    - `variant_index: int = 0`
    - `declared: DeclaredExport`
    - `asset_ids: list[SafeId] = Field(max_length=500)`
    - `uses_ai_generative_fill: bool = False`
    - `quality: QualityDeclaration`
- **Response:** 201 JSON — from `submit_work` (services/creative-py/src/zbm/workflow.py:396): `WorkItem` model (services/creative-py/src/zbm/workflow.py:165) — fields: `work_id`, `job_id`, `brief_id`, `submission`, `round`, `stage`, `export_validation`, `rights`, `quality_decision`, `compliance`, `final_approval`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `PreconditionFailed`

#### `GET /zbm/work/{work_id}`

- **Source:** `services/creative-py/src/api.py:983`
- **Purpose:** ZBM: read a work item.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `work_id`
- **Request body:** none
- **Response:** 200 JSON — from `get_work` (services/creative-py/src/zbm/workflow.py:449): `WorkItem` model (services/creative-py/src/zbm/workflow.py:165) — fields: `work_id`, `job_id`, `brief_id`, `submission`, `round`, `stage`, `export_validation`, `rights`, `quality_decision`, `compliance`, `final_approval`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /zbm/work/{work_id}/export-validation`

- **Source:** `services/creative-py/src/api.py:987`
- **Purpose:** ZBM: run export validation on a work item.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `work_id`
- **Request body:** none
- **Response:** 200 JSON — from `validate_export` (services/creative-py/src/zbm/workflow.py:461): `WorkItem` model (services/creative-py/src/zbm/workflow.py:165) — fields: `work_id`, `job_id`, `brief_id`, `submission`, `round`, `stage`, `export_validation`, `rights`, `quality_decision`, `compliance`, `final_approval`, `ledger_event_ids`; via get_work (services/creative-py/src/zbm/workflow.py:449).
- **Errors raised on this code path:** 404 `NotFound`, 409 `PreconditionFailed`

#### `POST /zbm/work/{work_id}/rights`

- **Source:** `services/creative-py/src/api.py:991`
- **Purpose:** ZBM: run the rights check on a work item.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `work_id`
- **Request body:** none
- **Response:** 200 JSON — from `check_rights` (services/creative-py/src/zbm/workflow.py:476): `WorkItem` model (services/creative-py/src/zbm/workflow.py:165) — fields: `work_id`, `job_id`, `brief_id`, `submission`, `round`, `stage`, `export_validation`, `rights`, `quality_decision`, `compliance`, `final_approval`, `ledger_event_ids`; via get_work (services/creative-py/src/zbm/workflow.py:449).
- **Errors raised on this code path:** 404 `NotFound`, 409 `PreconditionFailed`

#### `POST /zbm/work/{work_id}/quality`

- **Source:** `services/creative-py/src/api.py:995`
- **Purpose:** ZBM: quality review of a work item.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Creative-Actor-Token` (actor credential from CREATIVE_ACTOR_TOKENS; body `actor_id`, if sent, must match)
- **Path params:** `work_id`
- **Request body:** JSON `QualityIn` (services/creative-py/src/api.py:639); `model_config = ConfigDict(extra="forbid")`
    - `actor_id: str | None = None`
    - `notes: list[str] = Field(default_factory=list, max_length=100)`
- **Response:** 200 JSON — from `quality_review` (services/creative-py/src/zbm/workflow.py:499): `WorkItem` model (services/creative-py/src/zbm/workflow.py:165) — fields: `work_id`, `job_id`, `brief_id`, `submission`, `round`, `stage`, `export_validation`, `rights`, `quality_decision`, `compliance`, `final_approval`, `ledger_event_ids`; via get_work (services/creative-py/src/zbm/workflow.py:449).
- **Errors raised on this code path:** 404 `NotFound`, 409 `PreconditionFailed`

#### `POST /zbm/work/{work_id}/escalation`

- **Source:** `services/creative-py/src/api.py:999`
- **Purpose:** ZBM: Andre resolves a work-item escalation.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` header passed to the workflow (checked against CREATIVE_ANDRE_APPROVAL_TOKEN; refusal 403)
- **Path params:** `work_id`
- **Request body:** JSON `EscalationIn` (services/creative-py/src/api.py:644); `model_config = ConfigDict(extra="forbid")`
    - `decision: Literal["accept", "kill"]`
- **Response:** 200 JSON — from `resolve_escalation` (services/creative-py/src/zbm/workflow.py:529): `WorkItem` model (services/creative-py/src/zbm/workflow.py:165) — fields: `work_id`, `job_id`, `brief_id`, `submission`, `round`, `stage`, `export_validation`, `rights`, `quality_decision`, `compliance`, `final_approval`, `ledger_event_ids`; via get_work (services/creative-py/src/zbm/workflow.py:449).
- **Errors raised on this code path:** 404 `NotFound`, 409 `PreconditionFailed`

#### `POST /zbm/work/{work_id}/compliance`

- **Source:** `services/creative-py/src/api.py:1004`
- **Purpose:** ZBM: run the Compliance (38) publish gate for a work item.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `work_id`
- **Request body:** none
- **Response:** 200 JSON — from `compliance_gate` (services/creative-py/src/zbm/workflow.py:547): `WorkItem` model (services/creative-py/src/zbm/workflow.py:165) — fields: `work_id`, `job_id`, `brief_id`, `submission`, `round`, `stage`, `export_validation`, `rights`, `quality_decision`, `compliance`, `final_approval`, `ledger_event_ids`; via get_work (services/creative-py/src/zbm/workflow.py:449).
- **Errors raised on this code path:** 404 `NotFound`, 409 `PreconditionFailed`

#### `POST /zbm/work/{work_id}/final-approval`

- **Source:** `services/creative-py/src/api.py:1008`
- **Purpose:** ZBM: Andre's final approval of a work item.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` header passed to the workflow (checked against CREATIVE_ANDRE_APPROVAL_TOKEN; refusal 403)
- **Path params:** `work_id`
- **Request body:** none
- **Response:** 200 JSON — from `final_approval` (services/creative-py/src/zbm/workflow.py:562): `WorkItem` model (services/creative-py/src/zbm/workflow.py:165) — fields: `work_id`, `job_id`, `brief_id`, `submission`, `round`, `stage`, `export_validation`, `rights`, `quality_decision`, `compliance`, `final_approval`, `ledger_event_ids`; via get_work (services/creative-py/src/zbm/workflow.py:449).
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /zbm/hook-advice`

- **Source:** `services/creative-py/src/api.py:1012`
- **Purpose:** ZBM: rank hooks from supplied results for a platform/placement/metric.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Request body:** JSON `HookAdviceIn` (services/creative-py/src/api.py:648); `model_config = ConfigDict(extra="forbid")`
    - `results: list[PerformanceResult] = Field(max_length=500)`
    - `platform: str`
    - `placement: str`
    - `metric: str = hook_retention.DEFAULT_METRIC`
- **Response:** 200 — `{platform, placement, metric, note, ranked: [..], excluded}` (api.py:1012-1016).
- **Errors raised on this code path:** none found statically

#### `POST /zbm/memory/results`

- **Source:** `services/creative-py/src/api.py:1018`
- **Purpose:** ZBM: feed a result into memory (learned or not, with reason).
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Request body:** JSON `ZbmMemoryIn` (services/creative-py/src/api.py:655); `model_config = ConfigDict(extra="forbid")`
    - `brief_id: str`
    - `result: PerformanceResult`
- **Response:** 200 — `{learned: bool, reason}` (api.py:1018-1021).
- **Errors raised on this code path:** 404 `NotFound`, 409 `PreconditionFailed`

#### `POST /zbc/campaigns/{campaign_id}/rulebooks`

- **Source:** `services/creative-py/src/api.py:1043`
- **Purpose:** ZBC: draft a campaign rulebook (idempotent).
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Creative-Actor-Token` (actor credential from CREATIVE_ACTOR_TOKENS; body `actor_id`, if sent, must match)
- **Path params:** `campaign_id`
- **Optional headers:** `idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")`
- **Request body:** JSON `DraftRulebookIn` (services/creative-py/src/api.py:660); `model_config = ConfigDict(extra="forbid")`
    - `actor_id: str | None = None`
    - `goal: CampaignGoal`
- **Response:** 201 JSON — from `draft_rulebook` (services/creative-py/src/zbc/workflow.py:279): `Rulebook` model (services/creative-py/src/zbc/rulebook.py:130) — fields: `campaign_id`, `client_id`, `vertical`, `version`, `status`, `objective`, `source_asset_ids`, `approved_angles`, `platforms`, `rules`, `rule_number_high_water`, `blocking_issues`, `warnings`, `drafted_by`, `supersedes_version`, `approved_by`, `review_issues`, `review_warnings`, `signed_by`, `signed_at`, `live_at`, `superseded_at`, `language`.
- **Errors raised on this code path:** 409 `PreconditionFailed`, 422 `ValidationFailed`

#### `GET /zbc/campaigns/{campaign_id}/rulebooks`

- **Source:** `services/creative-py/src/api.py:1057`
- **Purpose:** ZBC: page of rulebook version summaries.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `campaign_id`
- **Query params:** `offset: int = Query(0, ge=0, le=MAX_RULEBOOK_VERSION)`; `limit: int = Query(RULEBOOK_PAGE, ge=1, le=RULEBOOK_PAGE)`
- **Request body:** none
- **Response:** 200 — `{campaign_id, total, offset, limit, next_offset (null at end), versions: [summary]}`; summary keys: campaign_id, version, status, supersedes_version, rule_count, retired_rule_count, rule_number_high_water, blocking_issue_count, drafted_by, approved_by, signed_by, signed_at, live_at, superseded_at (api.py:1025-1064).
- **Errors raised on this code path:** none found statically

#### `GET /zbc/campaigns/{campaign_id}/rulebooks/{version}/retired-rule-ids`

- **Source:** `services/creative-py/src/api.py:1066`
- **Purpose:** ZBC: page of a version's retired rule ids.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `campaign_id`, `version`
- **Query params:** `offset: int = Query(0, ge=0)`; `limit: int = Query(RETIRED_PAGE, ge=1, le=RETIRED_PAGE)`
- **Request body:** none
- **Response:** 200 — `{campaign_id, version, total, offset, limit, next_offset, ids: [..]}` (api.py:1066-1073).
- **Errors raised on this code path:** none found statically

#### `GET /zbc/campaigns/{campaign_id}/rulebooks/{version}`

- **Source:** `services/creative-py/src/api.py:1075`
- **Purpose:** ZBC: read one rulebook version.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `campaign_id`, `version`
- **Request body:** none
- **Response:** 200 — one rulebook version (`zbc.rulebooks.get(...).model_dump`) (api.py:1035-1036, 1075-1077). Unknown → 404.
- **Errors raised on this code path:** none found statically

#### `PUT /zbc/campaigns/{campaign_id}/rulebooks/{version}`

- **Source:** `services/creative-py/src/api.py:1079`
- **Purpose:** ZBC: edit a draft rulebook version.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Creative-Actor-Token` (actor credential from CREATIVE_ACTOR_TOKENS; body `actor_id`, if sent, must match)
- **Path params:** `campaign_id`, `version`
- **Request body:** JSON `DraftRulebookIn` (services/creative-py/src/api.py:660); `model_config = ConfigDict(extra="forbid")`
    - `actor_id: str | None = None`
    - `goal: CampaignGoal`
- **Response:** 200 JSON — from `edit_rulebook` (services/creative-py/src/zbc/workflow.py:294): `Rulebook` model (services/creative-py/src/zbc/rulebook.py:130) — fields: `campaign_id`, `client_id`, `vertical`, `version`, `status`, `objective`, `source_asset_ids`, `approved_angles`, `platforms`, `rules`, `rule_number_high_water`, `blocking_issues`, `warnings`, `drafted_by`, `supersedes_version`, `approved_by`, `review_issues`, `review_warnings`, `signed_by`, `signed_at`, `live_at`, `superseded_at`, `language`.
- **Errors raised on this code path:** 422 `ValidationFailed`

#### `POST /zbc/campaigns/{campaign_id}/rulebooks/{version}/review`

- **Source:** `services/creative-py/src/api.py:1083`
- **Purpose:** ZBC: review/approve a rulebook version.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Creative-Actor-Token` (actor credential from CREATIVE_ACTOR_TOKENS; body `actor_id`, if sent, must match)
- **Path params:** `campaign_id`, `version`
- **Request body:** JSON `ActorIn` (services/creative-py/src/api.py:615); `model_config = ConfigDict(extra="forbid")`
    - `actor_id: str | None = Field(default=None, min_length=1, max_length=64)`
- **Response:** 200 JSON — from `review_rulebook` (services/creative-py/src/zbc/workflow.py:337): `Rulebook` model (services/creative-py/src/zbc/rulebook.py:130) — fields: `campaign_id`, `client_id`, `vertical`, `version`, `status`, `objective`, `source_asset_ids`, `approved_angles`, `platforms`, `rules`, `rule_number_high_water`, `blocking_issues`, `warnings`, `drafted_by`, `supersedes_version`, `approved_by`, `review_issues`, `review_warnings`, `signed_by`, `signed_at`, `live_at`, `superseded_at`, `language`.
- **Errors raised on this code path:** none found statically

#### `POST /zbc/campaigns/{campaign_id}/rulebooks/{version}/sign`

- **Source:** `services/creative-py/src/api.py:1087`
- **Purpose:** ZBC: Andre signs a rulebook version.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` header passed to the workflow (checked against CREATIVE_ANDRE_APPROVAL_TOKEN; refusal 403)
- **Path params:** `campaign_id`, `version`
- **Request body:** none
- **Response:** 200 JSON — from `sign_rulebook` (services/creative-py/src/zbc/workflow.py:357): `Rulebook` model (services/creative-py/src/zbc/rulebook.py:130) — fields: `campaign_id`, `client_id`, `vertical`, `version`, `status`, `objective`, `source_asset_ids`, `approved_angles`, `platforms`, `rules`, `rule_number_high_water`, `blocking_issues`, `warnings`, `drafted_by`, `supersedes_version`, `approved_by`, `review_issues`, `review_warnings`, `signed_by`, `signed_at`, `live_at`, `superseded_at`, `language`.
- **Errors raised on this code path:** none found statically

#### `POST /zbc/campaigns/{campaign_id}/rights-check`

- **Source:** `services/creative-py/src/api.py:1091`
- **Purpose:** ZBC: rights check for campaign assets.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `campaign_id`
- **Request body:** JSON `RightsCheckIn` (services/creative-py/src/api.py:665); `model_config = ConfigDict(extra="forbid")`
    - `assets: list[DeclaredAsset] = Field(max_length=500)`
    - `uses_ai_generative_fill: bool = False`
- **Response:** 200 JSON — from `check_rights` (services/creative-py/src/zbc/workflow.py:374): `ClearanceResult` model (services/creative-py/src/zbc/rights_clearance.py:51) — fields: `campaign_id`, `cleared`, `license_id`, `blockers`, `flags`, `cleared_asset_ids`, `legal_crossing`.
- **Errors raised on this code path:** none found statically

#### `POST /zbc/campaigns/{campaign_id}/rulebooks/{version}/go-live`

- **Source:** `services/creative-py/src/api.py:1095`
- **Purpose:** ZBC: make a signed rulebook version live (announces to Clipper Network).
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `campaign_id`, `version`
- **Request body:** none
- **Response:** 200 JSON — from `go_live` (services/creative-py/src/zbc/workflow.py:386): `Rulebook` model (services/creative-py/src/zbc/rulebook.py:130) — fields: `campaign_id`, `client_id`, `vertical`, `version`, `status`, `objective`, `source_asset_ids`, `approved_angles`, `platforms`, `rules`, `rule_number_high_water`, `blocking_issues`, `warnings`, `drafted_by`, `supersedes_version`, `approved_by`, `review_issues`, `review_warnings`, `signed_by`, `signed_at`, `live_at`, `superseded_at`, `language`.
- **Errors raised on this code path:** 503 `OutcomeNotRecorded`

#### `POST /zbc/campaigns/{campaign_id}/revisions`

- **Source:** `services/creative-py/src/api.py:1099`
- **Purpose:** ZBC: draft a revision of a live rulebook (idempotent).
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Creative-Actor-Token` (actor credential from CREATIVE_ACTOR_TOKENS; body `actor_id`, if sent, must match)
- **Path params:** `campaign_id`
- **Optional headers:** `idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")`
- **Request body:** JSON `DraftRulebookIn` (services/creative-py/src/api.py:660); `model_config = ConfigDict(extra="forbid")`
    - `actor_id: str | None = None`
    - `goal: CampaignGoal`
- **Response:** 201 JSON — from `revise_rulebook` (services/creative-py/src/zbc/workflow.py:319): `Rulebook` model (services/creative-py/src/zbc/rulebook.py:130) — fields: `campaign_id`, `client_id`, `vertical`, `version`, `status`, `objective`, `source_asset_ids`, `approved_angles`, `platforms`, `rules`, `rule_number_high_water`, `blocking_issues`, `warnings`, `drafted_by`, `supersedes_version`, `approved_by`, `review_issues`, `review_warnings`, `signed_by`, `signed_at`, `live_at`, `superseded_at`, `language`.
- **Errors raised on this code path:** 409 `PreconditionFailed`

#### `POST /zbc/campaigns/{campaign_id}/moment-map`

- **Source:** `services/creative-py/src/api.py:1107`
- **Purpose:** ZBC: build a moment map from source material.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `campaign_id`
- **Request body:** JSON `SourceMaterial` (services/creative-py/src/zbc/source_mining.py:57); `model_config = ConfigDict(extra="forbid", frozen=True)`
    - `source_asset_id: SafeId`
    - `duration_seconds: float = Field(gt=0, le=86400)`
    - `segments: list[SourceSegment] = Field(min_length=1, max_length=2000)`
- **Response:** 200 JSON — from `build_moment_map` (services/creative-py/src/zbc/workflow.py:449): `MomentMap` model (services/creative-py/src/zbc/source_mining.py:84) — fields: `campaign_id`, `rulebook_version`, `source_asset_id`, `moments`, `rejected`.
- **Errors raised on this code path:** 409 `PreconditionFailed`

#### `POST /zbc/campaigns/{campaign_id}/hook-sheets`

- **Source:** `services/creative-py/src/api.py:1111`
- **Purpose:** ZBC: build hook sheets for the campaign.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `campaign_id`
- **Request body:** none
- **Response:** 200 — `{sheets: [..]}` from `zbc.build_hook_sheets` (services/creative-py/src/zbc/workflow.py) (api.py:1111-1113).
- **Errors raised on this code path:** 409 `PreconditionFailed`

#### `POST /zbc/campaigns/{campaign_id}/kit`

- **Source:** `services/creative-py/src/api.py:1115`
- **Purpose:** ZBC: build the clipper kit (idempotent).
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `campaign_id`
- **Optional headers:** `idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")`
- **Request body:** JSON `KitRequest` (services/creative-py/src/zbc/campaign_kit.py:49); `model_config = ConfigDict(extra="forbid", frozen=True)`
    - `seed_count: int = Field(ge=3, le=5)`
    - `caption_styles: list[NonEmptyStr] = Field(min_length=1, max_length=100)`
    - `overlays: list[NonEmptyStr] = Field(default_factory=list, max_length=100)`
    - `templates: list[NonEmptyStr] = Field(default_factory=list, max_length=100)`
    - `brand_asset_ids: list[SafeId] = Field(default_factory=list, max_length=200)`
    - `do_examples: list[NonEmptyStr] = Field(default_factory=list, max_length=100)`
    - `dont_examples: list[NonEmptyStr] = Field(default_factory=list, max_length=100)`
- **Response:** 201 JSON — from `build_kit` (services/creative-py/src/zbc/workflow.py:472): `CampaignKit` model (services/creative-py/src/zbc/campaign_kit.py:77) — fields: `kit_id`, `campaign_id`, `rulebook_version`, `seeds`, `caption_styles`, `overlays`, `templates`, `brand_asset_ids`, `do`, `dont`, `commissions`, `seed_clips_produced`, `status`, `signed_by`.
- **Errors raised on this code path:** 409 `PreconditionFailed`, 503 `OutcomeNotRecorded`

#### `POST /zbc/campaigns/{campaign_id}/kit/sign`

- **Source:** `services/creative-py/src/api.py:1125`
- **Purpose:** ZBC: Andre signs the kit.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` header passed to the workflow (checked against CREATIVE_ANDRE_APPROVAL_TOKEN; refusal 403)
- **Path params:** `campaign_id`
- **Request body:** none
- **Response:** 200 JSON — from `sign_kit` (services/creative-py/src/zbc/workflow.py:508): `CampaignKit` model (services/creative-py/src/zbc/campaign_kit.py:77) — fields: `kit_id`, `campaign_id`, `rulebook_version`, `seeds`, `caption_styles`, `overlays`, `templates`, `brand_asset_ids`, `do`, `dont`, `commissions`, `seed_clips_produced`, `status`, `signed_by`.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /zbc/clips`

- **Source:** `services/creative-py/src/api.py:1129`
- **Purpose:** ZBC: submit a clip for review (idempotent by submission_id).
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Optional headers:** `idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")`
- **Request body:** JSON `ClipSubmission` (services/creative-py/src/zbc/clip_review.py:187); `model_config = ConfigDict(extra="forbid", frozen=True)`
    - `submission_id: SafeId`
    - `campaign_id: CampaignId`
    - `rulebook_version: int = Field(ge=1, le=MAX_RULEBOOK_VERSION)`
    - `clipper_id: SafeId`
    - `posted_at: AwareDatetime`
    - `platform: NonEmptyStr`
    - `placement: NonEmptyStr`
    - `post_ref: NonEmptyStr`
    - `length_seconds: float = Field(gt=0, le=36000)`
    - `resolution_height_px: int | None = Field(default=None, ge=1, le=10000)`
    - `angle_id: NonEmptyStr`
    - `moment_ids: list[str] = Field(default_factory=list, max_length=200)`
    - `caption: str = Field(default="", max_length=5000)`
    - `on_screen_text: str = Field(default="", max_length=5000)`
    - `transcript: str = Field(default="", max_length=50000)`
    - `account_bio: str = Field(default="", max_length=5000)`
    - `transformation_elements: list[str] = Field(default_factory=list, max_length=50)`
    - `is_raw_repost: bool`
    - `has_third_party_watermark: bool`
    - `paid_partnership_label: bool = False`
    - `source_asset_ids: list[SafeId] = Field(default_factory=list, max_length=200)`
    - `added_asset_ids: list[SafeId] = Field(default_factory=list, max_length=200)`
- **Response:** 201 JSON — from `submit_clip` (services/creative-py/src/zbc/workflow.py:587): `ClipReviewDecision` model (services/creative-py/src/zbc/clip_review.py:225) — fields: `submission_id`, `campaign_id`, `rulebook_version`, `outcome`, `broken_rules`, `human_review_reasons`, `checks`, `decided_by`, `decided_at`, `received_at`; via _replay_uncertain (services/creative-py/src/zbc/workflow.py:232).
- **Errors raised on this code path:** 409 `PreconditionFailed`

#### `GET /zbc/clips/{submission_id}`

- **Source:** `services/creative-py/src/api.py:1136`
- **Purpose:** ZBC: read a clip's review decision.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `submission_id`
- **Request body:** none
- **Response:** 200 JSON — from `get_decision` (services/creative-py/src/zbc/workflow.py:640): `ClipReviewDecision` model (services/creative-py/src/zbc/clip_review.py:225) — fields: `submission_id`, `campaign_id`, `rulebook_version`, `outcome`, `broken_rules`, `human_review_reasons`, `checks`, `decided_by`, `decided_at`, `received_at`.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /zbc/clips/{submission_id}/human-review`

- **Source:** `services/creative-py/src/api.py:1140`
- **Purpose:** ZBC: human reviewer verdict on a clip.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Creative-Actor-Token` (actor credential from CREATIVE_ACTOR_TOKENS; body `actor_id`, if sent, must match)
- **Path params:** `submission_id`
- **Request body:** JSON `HumanReviewIn` (services/creative-py/src/api.py:670); `model_config = ConfigDict(extra="forbid")`
    - `actor_id: str | None = None`
    - `outcome: Literal["pass", "reject"]`
    - `broken_rules: list[BrokenRule] = Field(default_factory=list, max_length=100)`
    - `note: str = Field(default="", max_length=2000)`
- **Response:** 200 JSON — from `human_review` (services/creative-py/src/zbc/workflow.py:647): `ClipReviewDecision` model (services/creative-py/src/zbc/clip_review.py:225) — fields: `submission_id`, `campaign_id`, `rulebook_version`, `outcome`, `broken_rules`, `human_review_reasons`, `checks`, `decided_by`, `decided_at`, `received_at`; via _replay_uncertain (services/creative-py/src/zbc/workflow.py:232).
- **Errors raised on this code path:** 404 `NotFound`, 409 `PreconditionFailed`, 422 `ValidationFailed`

#### `POST /zbc/clips/{submission_id}/human-review/withdraw`

- **Source:** `services/creative-py/src/api.py:1145`
- **Purpose:** ZBC: withdraw an UNCERTAIN human verdict (same reviewer, after a minimum age).
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`; plus `X-Creative-Actor-Token` (actor credential from CREATIVE_ACTOR_TOKENS; body `actor_id`, if sent, must match)
- **Path params:** `submission_id`
- **Request body:** JSON `WithdrawVerdictIn` (services/creative-py/src/api.py:677); `model_config = ConfigDict(extra="forbid")`
    - `actor_id: str | None = None`
- **Response:** 200 JSON — from `withdraw_uncertain_verdict` (services/creative-py/src/zbc/workflow.py:688): object with keys `submission_id`, `withdrawn_event_id`, `event_id`, `outcome`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `PreconditionFailed`

#### `POST /zbc/clips/{submission_id}/payout-eligibility`

- **Source:** `services/creative-py/src/api.py:1150`
- **Purpose:** ZBC: payout eligibility of a clip.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Path params:** `submission_id`
- **Request body:** none
- **Response:** 200 JSON — from `payout_eligibility` (services/creative-py/src/zbc/workflow.py:733): shape not statically determinable (returns `res.as_dict()`) — UNKNOWN beyond the cited method.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /zbc/memory/results`

- **Source:** `services/creative-py/src/api.py:1154`
- **Purpose:** ZBC: feed a clip result into memory.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Request body:** JSON `ClipResult` (services/creative-py/src/zbc/creative_memory.py:32); `model_config = ConfigDict(extra="forbid", frozen=True)`
    - `result_id: SafeId`
    - `campaign_id: CampaignId`
    - `submission_id: SafeId`
    - `vertical: NonEmptyStr`
    - `platform: NonEmptyStr`
    - `angle_id: NonEmptyStr`
    - `hook: NonEmptyStr`
    - `source: Literal["self_reported", "platform_export", "tracking_link"]`
    - `reported_views: int = Field(ge=0)`
- **Response:** 200 — `{learned: bool, reason}` (api.py:1154-1157).
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /zbc/memory/winners`

- **Source:** `services/creative-py/src/api.py:1159`
- **Purpose:** ZBC: winning clips for a vertical/platform.
- **Auth:** `Authorization: Bearer <CREATIVE_SERVICE_TOKEN>`
- **Request body:** none
- **Response:** 200 — `{winners: [..]}` (api.py:1159-1161). Query params `vertical` and `platform` are required strings (plain FastAPI query params, no explicit constraints).
- **Errors raised on this code path:** none found statically

---

## 8. compliance-py (Compliance 38)

* **Language / entry:** Python FastAPI, `services/compliance-py/src/api.py`. Started with `cd services/compliance-py/src && python3 -m api` (`main()` `api.py:535-540` → `serve.run`), which uses the hardened launcher `serve.py`.
* **Bind:** `COMPLIANCE_BIND_ADDR` (default `127.0.0.1`), `COMPLIANCE_PORT` (default `8380`).
* **Required env:** `COMPLIANCE_SERVICE_TOKEN` (refuses to start without it, `src/config.py:77-80`).
* **Auth:** Bearer service token on every route except `/health`. Caller identity: `X-Compliance-Caller-Token` → `COMPLIANCE_CALLER_TOKENS` (JSON `{name: token}`, each ≥32 chars; names: onboarding, creative_production, finance_31, verification_integrity, legal_37, cybersecurity_22, people_43, vendor_33, scheduler; `src/config.py:13-15,82-99`). Andre: `X-Andre-Approval-Token` → `COMPLIANCE_ANDRE_APPROVAL_TOKEN`.
* **Other env & rules:** `COMPLIANCE_DATA_DIR` (persistence), `COMPLIANCE_SEED_PATH`, `COMPLIANCE_RECONCILE_MODE`, `LEDGER_SERVICE_URL` + `LEDGER_SERVICE_TOKEN` (without them every recorded action is 503).
* **Request limits (`InputLimits`, outermost middleware, `services/compliance-py/src/api.py`):**
  * Request target (path + query) over 4,096 bytes → **414**; head over 16 KiB → **431**; `Content-Length` not a digit → **400**.
  * Body over the route cap → **413**, checked from `Content-Length` and again on the bytes received. Route caps: gates (`rule`, `review`, `gates/*`) 256 KiB, `register/proposals` 128 KiB, `register/decisions` 64 KiB, `reconcile` 1 MiB, everything else 16 KiB (`api.py:67-73`); service cap 1 MiB.
  * A body whose `Content-Type` is not `application/json` or `application/*+json` → **415**.
  * JSON nested deeper than 32 or with more than 20,000 members → **422**.
  * Body not received within 30 s → **408**.
  * The hardened launcher also caps the head at 16 KiB in the parser, sets a 10 s head deadline and a 5 s keep-alive, and limits concurrency to 128 (beyond it uvicorn answers **503**).
  * Validation errors → **422** `{"detail":[{loc,msg,type}… ≤20], "errors_total": N}` (input never echoed). An unhandled exception → **500** `{"detail":"internal error"}`.
* **Errors on every route:** 401 (bearer); 403 `Forbidden` / `FounderRefused` (caller or Andre identity); 409 `Conflict` (request_id reuse); 503 `Unavailable` with `Retry-After: 1` (ledger write failed, reconcile mode, port down); 500. Domain errors answer `{"detail": <reason>, …extra body}`.
* **State:** see the summary table (JSONL log if the data dir is set, otherwise in-memory).


**28 endpoints.**

#### `GET /health`

- **Source:** `services/compliance-py/src/api.py:305`
- **Purpose:** Liveness/status of the service.
- **Auth:** none (open)
- **Request body:** none
- **Response:** 200 JSON — from `health` (services/compliance-py/src/service.py:1413): object with keys `status`, `service`, `register_version_in_force`, `in_memory`, `seed_pinned`, `production`, `reconcile_mode`, `reconcile_required`.
- **Errors raised on this code path:** none found statically

#### `GET /intelligences`

- **Source:** `services/compliance-py/src/api.py:309`
- **Purpose:** List the department's intelligence modules.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 — JSON array from `registry()` (services/compliance-py/src/intelligences/__init__.py:13): one object per intelligence with keys `number`, `name`, `actor`, `llm` (false).
- **Errors raised on this code path:** none found statically

#### `POST /compliance/v1/rule`

- **Source:** `services/compliance-py/src/api.py:315`
- **Purpose:** Activation gate ruling for a subject in a lane (Onboarding).
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = caller ∈ {onboarding}
- **Request body:** JSON `RuleRequest` (services/compliance-py/src/models.py:43); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `subject_id: Id`
    - `lane: Literal["client", "zbc_creator", "zbc_brand"]`
    - `facts: dict[str, Any]`
- **Response:** 200 JSON — from `gate` (services/compliance-py/src/service.py:684): object with keys `ruling_id`, `gate`, `subject_id`, `allowed`, `unmet`, `unmet_lines`, `register_version`, `evaluated_at`, `ledger_event_id`, `seed_pinned`, `request_id`, `facts_sha256`; via stored idempotent answer (replay), _idem_store_ruling (services/compliance-py/src/service.py:822), ruling_view (services/compliance-py/src/service.py:859).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /compliance/v1/review`

- **Source:** `services/compliance-py/src/api.py:319`
- **Purpose:** Payout gate (subject_kind zbc_clip) or publish gate (zbm_work) ruling (Creative Production).
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = caller ∈ {creative_production}
- **Request body:** JSON `ReviewRequest` (services/compliance-py/src/models.py:50); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `subject_kind: Literal["zbc_clip", "zbm_work"]`
    - `subject_id: Id`
    - `facts: dict[str, Any]`
    - `caller_context: Annotated[Optional[dict[str, Any]], AfterValidator(_bounded_json)] = None`
- **Response:** 200 JSON — from `gate` (services/compliance-py/src/service.py:684): object with keys `ruling_id`, `gate`, `subject_id`, `allowed`, `unmet`, `unmet_lines`, `register_version`, `evaluated_at`, `ledger_event_id`, `seed_pinned`, `request_id`, `facts_sha256`; via stored idempotent answer (replay), _idem_store_ruling (services/compliance-py/src/service.py:822), ruling_view (services/compliance-py/src/service.py:859).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /compliance/v1/gates/activation`

- **Source:** `services/compliance-py/src/api.py:326`
- **Purpose:** Activation gate ruling (same as /compliance/v1/rule).
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = caller ∈ {onboarding}
- **Request body:** JSON `RuleRequest` (services/compliance-py/src/models.py:43); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `subject_id: Id`
    - `lane: Literal["client", "zbc_creator", "zbc_brand"]`
    - `facts: dict[str, Any]`
- **Response:** 200 JSON — from `gate` (services/compliance-py/src/service.py:684): object with keys `ruling_id`, `gate`, `subject_id`, `allowed`, `unmet`, `unmet_lines`, `register_version`, `evaluated_at`, `ledger_event_id`, `seed_pinned`, `request_id`, `facts_sha256`; via stored idempotent answer (replay), _idem_store_ruling (services/compliance-py/src/service.py:822), ruling_view (services/compliance-py/src/service.py:859).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /compliance/v1/rulings/{ruling_id}`

- **Source:** `services/compliance-py/src/api.py:344`
- **Purpose:** Read one ruling.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = any recognised caller
- **Path params:** `ruling_id`
- **Request body:** none
- **Response:** 200 JSON — from `get_ruling` (services/compliance-py/src/service.py:872): object with keys `ruling_id`, `gate`, `subject_id`, `allowed`, `unmet`, `unmet_lines`, `register_version`, `evaluated_at`, `ledger_event_id`, `seed_pinned`, `request_id`, `facts_sha256`; via ruling_view (services/compliance-py/src/service.py:859).
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /compliance/v1/sanctions/screen`

- **Source:** `services/compliance-py/src/api.py:350`
- **Purpose:** Sanctions screening request.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = caller ∈ {onboarding, finance_31}
- **Request body:** JSON `ScreenRequest` (services/compliance-py/src/models.py:58); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `subject_id: Id`
    - `role: Literal["payee", "owner"]`
    - `owner_of: Optional[Id] = None`
    - `legal_name: Name`
    - `aliases: list[Name] = Field(default_factory=list, max_length=10)`
    - `dob: Optional[Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]] = None`
    - `country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]`
    - `region: Optional[Annotated[str, Field(pattern=r"^[A-Z]{2}-[A-Z0-9]{1,3}$")]] = None`
- **Response:** 200 JSON — from `screen` (services/compliance-py/src/service.py:143): returns `Optional[dict]`.
- **Errors raised on this code path:** none found statically

#### `POST /compliance/v1/accessibility/checks`

- **Source:** `services/compliance-py/src/api.py:355`
- **Purpose:** Accessibility check request.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = caller ∈ {creative_production}
- **Request body:** JSON `A11yRequest` (services/compliance-py/src/models.py:79); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `asset_ref: Annotated[str, Field(min_length=1, max_length=512), AfterValidator(_no_control)]`
    - `asset_type: Literal[ASSET_TYPES]`
    - `content_sha256: Sha`
    - `owner_id: Id`
- **Response:** 200 JSON — from `accessibility_check` (services/compliance-py/src/service.py:933): object with keys `**rec`, `status`, `ledger_event_ids`.
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /compliance/v1/jurisdictions/resolve`

- **Source:** `services/compliance-py/src/api.py:359`
- **Purpose:** Resolve applicable jurisdictions.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = any recognised caller
- **Request body:** JSON `ResolveRequest` (services/compliance-py/src/models.py:126); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `person: Optional[dict[str, Any]] = None`
    - `network_country_signal: Optional[Annotated[str, Field(pattern=r"^[A-Z]{2}$")]] = None`
    - `targets: list[Annotated[str, Field(pattern=r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")]] = Field(default_factory=list, max_length=60)`
- **Response:** 200 JSON — from `resolve` (services/compliance-py/src/service.py:986): object with keys `resolution_id`, `register_version`, `answers`, `note`, `ledger_event_ids`.
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /compliance/v1/register`

- **Source:** `services/compliance-py/src/api.py:365`
- **Purpose:** Page through the obligations register (filters: gate, jurisdiction, status, domain).
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = any recognised caller
- **Query params:** `gate: Optional[str] = Query(default=None, pattern=r"^(activation|payout|publish|control)$")`; `jurisdiction: Optional[str] = Query(default=None, pattern=r"^(ALL|EU|[A-Z]{2}(-[A-Z0-9]{1,3})?)$")`; `status_: Optional[str] = Query(default=None, alias="status", pattern=r"^(verified|unverified|expired|superseded)$")`; `domain: Optional[str] = Query(default=None, pattern=r"^[a-z_]{1,40}$")`; `page: int = Query(default=1, ge=1, le=10_000)`
- **Request body:** none
- **Response:** 200 JSON — from `register_rows` (services/compliance-py/src/service.py:502): object with keys `register_version`, `rows_sha256`, `total`, `page`, `page_size`, `rows`; object with keys `register_version`, `rows`, `page`, `total`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** none found statically

#### `GET /compliance/v1/register/versions`

- **Source:** `services/compliance-py/src/api.py:375`
- **Purpose:** List register versions.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `version_list` (services/compliance-py/src/service.py:539): returns `list[dict]`.
- **Errors raised on this code path:** none found statically

#### `GET /compliance/v1/register/{obligation_id}`

- **Source:** `services/compliance-py/src/api.py:379`
- **Purpose:** Read one register obligation row.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = any recognised caller
- **Path params:** `obligation_id`
- **Request body:** none
- **Response:** 200 JSON — from `register_row` (services/compliance-py/src/service.py:524): object with keys `register_version`, `row`, `history`.
- **Errors raised on this code path:** 404 `NotFound`, 422 `Invalid`

#### `POST /compliance/v1/register/proposals`

- **Source:** `services/compliance-py/src/api.py:385`
- **Purpose:** Create a register change proposal (Andre or legal_37).
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre) OR `X-Compliance-Caller-Token` = legal_37 (else 403) (api.py:386-399)
- **Request body:** JSON `ProposalRequest` (services/compliance-py/src/models.py:87); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `kind: Literal["new", "amend", "reverify", "supersede", "retire", "control"]`
    - `target_id: Optional[Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9-]{1,39}$")]] = None`
    - `proposed_row: Optional[dict[str, Any]] = None`
    - `evidence: Optional[dict[str, Any]] = None`
- **Response:** 201 JSON — from `create_proposal` (services/compliance-py/src/service.py:553): object with keys `proposal`, `ledger_event_ids`.
- **Errors raised on this code path:** 403 `Forbidden`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /compliance/v1/inbox`

- **Source:** `services/compliance-py/src/api.py:401`
- **Purpose:** Andre's proposal inbox.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `inbox` (services/compliance-py/src/service.py:543): returns `list[dict]`.
- **Errors raised on this code path:** 503 `Unavailable`

#### `POST /compliance/v1/register/decisions`

- **Source:** `services/compliance-py/src/api.py:405`
- **Purpose:** Andre approves/rejects register proposals (atomic new version).
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `DecisionsRequest` (services/compliance-py/src/models.py:103); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `decisions: list[Decision] = Field(min_length=1, max_length=200)`
- **Response:** 200 JSON — from `decide` (services/compliance-py/src/service.py:578): object with keys `decided`, `approved`, `register_version`, `rows_sha256`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /compliance/v1/controls`

- **Source:** `services/compliance-py/src/api.py:412`
- **Purpose:** List controls.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `controls_view` (services/compliance-py/src/service.py:1017): returns `list[dict]`.
- **Errors raised on this code path:** none found statically

#### `POST /compliance/v1/controls/internal/run`

- **Source:** `services/compliance-py/src/api.py:416`
- **Purpose:** Scheduler runs internal controls.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = caller ∈ {scheduler}
- **Request body:** JSON `RunRequest` (services/compliance-py/src/models.py:133); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
- **Response:** 200 JSON — from `run_internal_controls` (services/compliance-py/src/service.py:1088): object with keys `results`, `reverify_proposals_drafted`, `controls`, `ledger_event_ids`.
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /compliance/v1/controls/{control_id}`

- **Source:** `services/compliance-py/src/api.py:420`
- **Purpose:** Read one control.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = any recognised caller
- **Path params:** `control_id`
- **Request body:** none
- **Response:** 200 JSON — from `control_view` (services/compliance-py/src/service.py:1030): shape not statically determinable (returns `c`) — UNKNOWN beyond the cited method.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /compliance/v1/controls/{control_id}/results`

- **Source:** `services/compliance-py/src/api.py:424`
- **Purpose:** Push a control test result (owner caller, or Andre for Andre-owned controls).
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus if the control's owner is Andre: `X-Andre-Approval-Token`; otherwise any recognised `X-Compliance-Caller-Token` (api.py:425-446); `control_id` must match `C-[0-9]{2,3}`; `tested_at` must be RFC 3339 with offset (else 422)
- **Path params:** `control_id`
- **Request body:** JSON `ControlResultRequest` (services/compliance-py/src/models.py:114); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `result: Literal["pass", "fail"]`
    - `tested_at: Annotated[str, Field(max_length=40)]`
    - `evidence: list[EvidenceItem] = Field(max_length=50)`
- **Response:** 200 JSON — from `push_control_result` (services/compliance-py/src/service.py:1071): object with keys `control`, `ledger_event_ids`.
- **Errors raised on this code path:** 403 `Forbidden`, 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /compliance/v1/trust-center`

- **Source:** `services/compliance-py/src/api.py:448`
- **Purpose:** Trust-center view of controls.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `trust_center` (services/compliance-py/src/service.py:1036): returns `list[dict]`.
- **Errors raised on this code path:** none found statically

#### `GET /compliance/v1/holds`

- **Source:** `services/compliance-py/src/api.py:454`
- **Purpose:** List compliance holds.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `list_holds` (services/compliance-py/src/service.py:964): returns `list[dict]`.
- **Errors raised on this code path:** none found statically

#### `POST /compliance/v1/holds/{hold_id}/release`

- **Source:** `services/compliance-py/src/api.py:458`
- **Purpose:** Andre releases a hold.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `hold_id`
- **Request body:** JSON `HoldReleaseRequest` (services/compliance-py/src/models.py:121); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `reason: Text`
- **Response:** 200 JSON — from `release_hold` (services/compliance-py/src/service.py:968): object with keys `hold_id`, `status`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /compliance/v1/reconcile`

- **Source:** `services/compliance-py/src/api.py:467`
- **Purpose:** Andre previews what a ledger reconcile would void.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** none
- **Response:** 200 JSON — from `reconcile_plan` (services/compliance-py/src/service.py:345): object with keys `epoch`, `head_seq`, `head_sha256`, `register_version`, `fatal`, `problems`, `voidable`, `reconcile_mode`.
- **Errors raised on this code path:** none found statically

#### `POST /compliance/v1/reconcile`

- **Source:** `services/compliance-py/src/api.py:471`
- **Purpose:** Andre voids stray anchors/rulings/leases to reconcile with the ledger.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `ReconcileRequest` (services/compliance-py/src/models.py:137); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `head_sha256: Sha`
    - `void_lines: list[Annotated[int, Field(ge=1, le=10**12)]] = Field(max_length=10_000)`
    - `void_event_ids: list[Id] = Field(max_length=10_000)`
- **Response:** 200 JSON — from `reconcile` (services/compliance-py/src/service.py:355): object with keys `reconcile_event_id`, `voided`, `void_lines`, `remaining_problems`, `restart_required`, `ledger_event_ids`.
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /compliance/v1/watcher/run`

- **Source:** `services/compliance-py/src/api.py:478`
- **Purpose:** Scheduler runs the regulatory-source watcher.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = caller ∈ {scheduler}
- **Request body:** JSON `RunRequest` (services/compliance-py/src/models.py:133); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
- **Response:** 200 JSON — from `watcher_run` (services/compliance-py/src/service.py:1199): object with keys `ran`, `**cyc`, `ledger_event_ids`; object with keys `ran`, `reason`, `proposals`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /compliance/v1/audit/export`

- **Source:** `services/compliance-py/src/api.py:482`
- **Purpose:** Audit export of the local record log (paged; optional since/until).
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = any recognised caller
- **Query params:** `since: Optional[str] = Query(default=None, max_length=40)`; `until: Optional[str] = Query(default=None, max_length=40)`; `cursor: int = Query(default=0, ge=0, le=10**12)`
- **Request body:** none
- **Response:** 200 JSON — from `audit_export` (services/compliance-py/src/service.py:1392): object with keys `records`, `next_cursor`, `ledger_event_id`, `register_version`.
- **Errors raised on this code path:** 422 `Invalid`, 503 `Unavailable`

#### `POST /compliance/v1/gates/payout`

- **Source:** `services/compliance-py/src/api.py:341`
- **Purpose:** Payout gate ruling; subject_kind must be zbc_clip.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = caller ∈ {creative_production}
- **Request body:** JSON `ReviewRequest` (services/compliance-py/src/models.py:50); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `subject_kind: Literal["zbc_clip", "zbm_work"]`
    - `subject_id: Id`
    - `facts: dict[str, Any]`
    - `caller_context: Annotated[Optional[dict[str, Any]], AfterValidator(_bounded_json)] = None`
- **Response:** 200 JSON — from `gate` (services/compliance-py/src/service.py:684): object with keys `ruling_id`, `gate`, `subject_id`, `allowed`, `unmet`, `unmet_lines`, `register_version`, `evaluated_at`, `ledger_event_id`, `seed_pinned`, `request_id`, `facts_sha256`; via stored idempotent answer (replay), _idem_store_ruling (services/compliance-py/src/service.py:822), ruling_view (services/compliance-py/src/service.py:859).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /compliance/v1/gates/publish`

- **Source:** `services/compliance-py/src/api.py:342`
- **Purpose:** Publish gate ruling; subject_kind must be zbm_work.
- **Auth:** `Authorization: Bearer <COMPLIANCE_SERVICE_TOKEN>`; plus `X-Compliance-Caller-Token` = caller ∈ {creative_production}
- **Request body:** JSON `ReviewRequest` (services/compliance-py/src/models.py:50); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `subject_kind: Literal["zbc_clip", "zbm_work"]`
    - `subject_id: Id`
    - `facts: dict[str, Any]`
    - `caller_context: Annotated[Optional[dict[str, Any]], AfterValidator(_bounded_json)] = None`
- **Response:** 200 JSON — from `gate` (services/compliance-py/src/service.py:684): object with keys `ruling_id`, `gate`, `subject_id`, `allowed`, `unmet`, `unmet_lines`, `register_version`, `evaluated_at`, `ledger_event_id`, `seed_pinned`, `request_id`, `facts_sha256`; via stored idempotent answer (replay), _idem_store_ruling (services/compliance-py/src/service.py:822), ruling_view (services/compliance-py/src/service.py:859).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

---

## 9. verification-py (Verification & Integrity)

* **Language / entry:** Python FastAPI, `services/verification-py/src/api.py`. Started with `cd services/verification-py/src && python3 -m api` (`main()` `api.py:564-569`), which uses the hardened launcher `serve.py`.
* **Bind:** `VI_BIND_ADDR` (default `127.0.0.1`), `VI_PORT` (default `8390`).
* **Required env:** `VI_SERVICE_TOKEN` (refuses to start without it, `src/config.py:138-141`).
* **Auth:** Bearer service token on every route except `/health`. Caller identity: `X-VI-Caller-Token` → `VI_CALLER_TOKENS` (names: compliance_38, creative_production, onboarding, clipper_network, finance_31, scheduler; `src/config.py:17`). Andre: `X-Andre-Approval-Token` → `VI_ANDRE_APPROVAL_TOKEN`; reviewer delegates `X-VI-Reviewer-Token` → `VI_REVIEWER_TOKENS` (only count when People-43 confirms; stand-in confirms nobody).
* **Other env & rules:** `VI_DATA_DIR`, `VI_SEED_PATH`, `VI_RECONCILE_MODE`, `VI_COMPLIANCE_URL` + `VI_COMPLIANCE_TOKEN` + `VI_COMPLIANCE_CALLER_TOKEN` (Compliance register thin client), `VI_OEMBED_ENABLED` (TikTok oEmbed, external), `LEDGER_SERVICE_URL` + `LEDGER_SERVICE_TOKEN`. Write answers echo `request_id` (`_echo`, `api.py:244`). Ids: `SUB_ID` `[A-Za-z0-9._:-]{1,128}`, `VI_ID` `vi-[a-z]{2,5}-[0-9A-Z]{26}` (`api.py:242-243`) → 422 on mismatch.
* **Request limits (`InputLimits`, outermost middleware, `services/verification-py/src/api.py`):**
  * Request target (path + query) over 4,096 bytes → **414**; head over 16 KiB → **431**; `Content-Length` not a digit → **400**.
  * Body over the route cap → **413**, checked from `Content-Length` and again on the bytes received. Route caps: `results/attest` 64 KiB, `rules/proposals` 64 KiB, `rules/decisions` 64 KiB, `submissions` 32 KiB, `reconcile` 1 MiB, everything else 16 KiB (`api.py:67-74`); service cap 1 MiB. Bodies naming counts/metrics/amounts/rates/guardian fields → 422 `forbidden_field` (`models.forbidden_keys`).
  * A body whose `Content-Type` is not `application/json` or `application/*+json` → **415**.
  * JSON nested deeper than 32 or with more than 20,000 members → **422**.
  * Body not received within 30 s → **408**.
  * The hardened launcher also caps the head at 16 KiB in the parser, sets a 10 s head deadline and a 5 s keep-alive, and limits concurrency to 128 (beyond it uvicorn answers **503**).
  * Validation errors → **422** `{"detail":[{loc,msg,type}… ≤20], "errors_total": N}` (input never echoed). An unhandled exception → **500** `{"detail":"internal error"}`.
* **Errors on every route:** 401 (bearer); 403 `Forbidden` / `FounderRefused` (caller or Andre identity); 409 `Conflict` (request_id reuse); 503 `Unavailable` with `Retry-After: 1` (ledger write failed, reconcile mode, port down); 500. Domain errors answer `{"detail": <reason>, …extra body}`.
* **State:** see the summary table (JSONL log if the data dir is set, otherwise in-memory).


**36 endpoints.**

#### `GET /health`

- **Source:** `services/verification-py/src/api.py:346`
- **Purpose:** Liveness/status of the service.
- **Auth:** none (open)
- **Request body:** none
- **Response:** 200 JSON — from `health` (services/verification-py/src/service.py:2774): object with keys `status`, `service`, `rules_version`, `in_memory`, `rules_pinned`, `production`, `reconcile_mode`, `reconcile_required`, `platform_data_store_degraded`.
- **Errors raised on this code path:** none found statically

#### `GET /vi/v1/intelligences`

- **Source:** `services/verification-py/src/api.py:350`
- **Purpose:** List the department's intelligence modules.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 — JSON array from `registry()` (services/verification-py/src/intelligences/__init__.py:13): one object per intelligence with keys `number`, `name`, `actor`.
- **Errors raised on this code path:** none found statically

#### `GET /vi/v1/integrity`

- **Source:** `services/verification-py/src/api.py:354`
- **Purpose:** Integrity check of the local log vs the evidence ledger.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `integrity` (services/verification-py/src/service.py:444): object with keys `status`, `problems`, `log_lines`, `rules_version`; object with keys `status`, `problems`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** none found statically

#### `POST /vi/v1/connections/start`

- **Source:** `services/verification-py/src/api.py:360`
- **Purpose:** Start a clipper platform-account connection (OAuth start) for Clipper Network.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {clipper_network}
- **Request body:** JSON `ConnectionStart` (services/verification-py/src/models.py:78); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `clipper_id: Id`
    - `platform: Platform`
    - `redirect_uri: Annotated[str, StringConstraints(pattern=r"^https://[^\s\x00-\x1f\x7f]{3,500}$")]`
- **Response:** 200 JSON — from `connections_start` (services/verification-py/src/service.py:896): object with keys `request_id`, `rules_pinned`, `clipper_id`, `started`, `connection_id`, `authorization_url`, `state_expires_at`, `ledger_event_ids`; object with keys `request_id`, `rules_pinned`, `clipper_id`, `started`, `connection_id`, `reasons`, `reason_lines`, `ledger_event_ids`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /vi/v1/connections/complete`

- **Source:** `services/verification-py/src/api.py:365`
- **Purpose:** Complete a platform-account connection (OAuth state + code).
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {clipper_network}
- **Request body:** JSON `ConnectionComplete` (services/verification-py/src/models.py:85); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `state: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{16,128}$")]`
    - `code: Ref`
- **Response:** 200 JSON — from `connections_complete` (services/verification-py/src/service.py:953): object with keys `request_id`, `rules_pinned`, `connection`, `ledger_event_ids`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /vi/v1/connections/{connection_id}/revoke`

- **Source:** `services/verification-py/src/api.py:370`
- **Purpose:** Revoke a platform-account connection.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {clipper_network}
- **Path params:** `connection_id`
- **Request body:** JSON `RunRequest` (services/verification-py/src/models.py:74); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
- **Response:** 200 JSON — from `revoke_connection` (services/verification-py/src/service.py:1087): object with keys `request_id`, `rules_pinned`, `connection`, `ledger_event_ids`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /vi/v1/connections`

- **Source:** `services/verification-py/src/api.py:375`
- **Purpose:** List a clipper's connections.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {clipper_network, compliance_38}
- **Query params:** `clipper_id: str = Query(max_length=128)`
- **Request body:** none
- **Response:** 200 JSON — from `list_connections` (services/verification-py/src/service.py:1136): object with keys `clipper_id`, `rules_pinned`, `items`.
- **Errors raised on this code path:** none found statically

#### `POST /vi/v1/submissions`

- **Source:** `services/verification-py/src/api.py:382`
- **Purpose:** Creative Production registers a clip submission for verification.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {creative_production}
- **Request body:** JSON `SubmissionRequest` (services/verification-py/src/models.py:91); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `submission_id: Id`
    - `campaign_id: BoundedId`
    - `rulebook_version: int = Field(ge=1, le=9_999_999)`
    - `clipper_id: Id`
    - `platform: Platform`
    - `post_ref: Ref`
    - `posted_at: Timestamp`
    - `min_days_live: Optional[int] = Field(ge=1, le=365)`
    - `collab_permitted: bool`
    - `media_ref: Optional[Annotated[str, StringConstraints(min_length=1, max_length=512), AfterValidator(_printable)]] = None`
    - `seed_media_refs: list[Annotated[str, StringConstraints(min_length=1, max_length=512), AfterValidator(_printable)]] = Field(default_factory=list, max_length=20)`
    - `target_regions: Optional[list[Iso2]] = Field(default=None, max_length=250)`
- **Response:** 201 JSON — from `register_submission` (services/verification-py/src/service.py:1146): object with keys `submission_id`, `certification_id`, `stolen_check`, `holds`, `ledger_event_ids`; object with keys `submission_id`, `certification_id`, `already_registered`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /vi/v1/submissions/{submission_id}/approval`

- **Source:** `services/verification-py/src/api.py:387`
- **Purpose:** Creative Production reports human approval of a submission.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {creative_production}
- **Path params:** `submission_id`
- **Request body:** JSON `ApprovalRequest` (services/verification-py/src/models.py:108); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `review_ref: Optional[Id] = None`
- **Response:** 200 JSON — from `approve_submission` (services/verification-py/src/service.py:1413): object with keys `submission_id`, `fingerprint`, `ledger_event_ids`; object with keys `submission_id`, `fingerprint`, `already_taken`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /vi/v1/clips/attest`

- **Source:** `services/verification-py/src/api.py:392`
- **Purpose:** Creative Production attests clip facts.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {creative_production}
- **Request body:** JSON `CreativeClipAttest` (services/verification-py/src/models.py:121); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `submission_id: Id`
    - `facts: CreativeClipFacts`
- **Response:** 200 JSON — from `attest_creative_clip` (services/verification-py/src/service.py:2219): object with keys `attestation_id`; via _attest (services/verification-py/src/service.py:2105), stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /vi/v1/clips/hr13`

- **Source:** `services/verification-py/src/api.py:397`
- **Purpose:** Compliance submits an HR-13 attestation for a clip.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {compliance_38}
- **Request body:** JSON `Hr13Attest` (services/verification-py/src/models.py:127); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `submission_id: Id`
    - `post_ref: Ref`
    - `platform: Annotated[str, StringConstraints(min_length=1, max_length=32), AfterValidator(_printable)]`
    - `posted_at: Timestamp`
    - `settlement_lag_days: int = Field(ge=0, le=365)`
- **Response:** 200 JSON — from `attest_hr13` (services/verification-py/src/service.py:2139): object with keys `attestation_id`; via _attest (services/verification-py/src/service.py:2105), stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /vi/v1/results/attest`

- **Source:** `services/verification-py/src/api.py:401`
- **Purpose:** Creative Production attests a clip's results (e.g. reported views).
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {creative_production}
- **Request body:** JSON `ResultAttest` (services/verification-py/src/models.py:150); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `result_id: Id`
    - `facts: ClipResultFacts`
- **Response:** 200 JSON — from `attest_result` (services/verification-py/src/service.py:2265): object with keys `attestation_id`; via _attest (services/verification-py/src/service.py:2105), stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /vi/v1/feed/verified-results`

- **Source:** `services/verification-py/src/api.py:406`
- **Purpose:** Cursor feed of verified results (Creative Production).
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {creative_production}
- **Query params:** `cursor: int = Query(default=0, ge=0, le=10**12)`
- **Request body:** none
- **Response:** 200 JSON — from `feed_verified` (services/verification-py/src/service.py:2677): object with keys `items`, `next_cursor`; via _feed (services/verification-py/src/service.py:2665).
- **Errors raised on this code path:** none found statically

#### `GET /vi/v1/certifications/{certification_id}`

- **Source:** `services/verification-py/src/api.py:413`
- **Purpose:** Read one certification.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = any recognised caller
- **Path params:** `certification_id`
- **Request body:** none
- **Response:** 200 JSON — from `get_certification` (services/verification-py/src/service.py:2651): via cert_view (services/verification-py/src/service.py:2644).
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /vi/v1/submissions/{submission_id}/certification`

- **Source:** `services/verification-py/src/api.py:417`
- **Purpose:** Certification for a submission.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = any recognised caller
- **Path params:** `submission_id`
- **Request body:** none
- **Response:** 200 JSON — from `certification_for` (services/verification-py/src/service.py:2658): via cert_view (services/verification-py/src/service.py:2644).
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /vi/v1/certifications`

- **Source:** `services/verification-py/src/api.py:421`
- **Purpose:** Certifications of a clipper.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {clipper_network, compliance_38, finance_31}
- **Query params:** `clipper_id: str = Query(max_length=128)`
- **Request body:** none
- **Response:** 200 JSON — from `certifications_of` (services/verification-py/src/service.py:2714): object with keys `clipper_id`, `certifications`, `rules_pinned`.
- **Errors raised on this code path:** none found statically

#### `GET /vi/v1/clawbacks`

- **Source:** `services/verification-py/src/api.py:426`
- **Purpose:** Cursor feed of clawbacks (Finance).
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {finance_31}
- **Query params:** `cursor: int = Query(default=0, ge=0, le=10**12)`
- **Request body:** none
- **Response:** 200 JSON — from `feed_clawbacks` (services/verification-py/src/service.py:2688): object with keys `items`, `next_cursor`; via _feed (services/verification-py/src/service.py:2665).
- **Errors raised on this code path:** none found statically

#### `POST /vi/v1/age/checks`

- **Source:** `services/verification-py/src/api.py:432`
- **Purpose:** Age check for a subject (filed in the caller's namespace).
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {clipper_network, onboarding}
- **Request body:** JSON `AgeCheck` (services/verification-py/src/models.py:156); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `subject_id: Id`
    - `dob: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]`
    - `dob_field_neutral: bool`
    - `method: Literal[METHODS]`
    - `provider_session_ref: Annotated[str, StringConstraints(min_length=1, max_length=256), AfterValidator(_printable)]`
- **Response:** 200 JSON — from `age_check` (services/verification-py/src/service.py:2314): object with keys `request_id`, `facts_sha256`, `rules_pinned`, `attestation_id`, `subject_id`, `status`, `result`, `method`, `buffer_applied`, `rules_in_force`, `reasons`, `reason_lines`, `ledger_event_ids`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /vi/v1/age/attestations/{attestation_id}`

- **Source:** `services/verification-py/src/api.py:436`
- **Purpose:** Read an age attestation (Compliance).
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {compliance_38}
- **Path params:** `attestation_id`
- **Request body:** none
- **Response:** 200 JSON — from `age_attestation` (services/verification-py/src/service.py:2421): object with keys `attestation_id`, `status`, `rules_pinned`, `reasons`, `reason_lines`, `reason`.
- **Errors raised on this code path:** 404 `NotFound`, 503 `Unavailable`

#### `GET /vi/v1/age/subjects/{subject_id}`

- **Source:** `services/verification-py/src/api.py:440`
- **Purpose:** Age-verified status of a subject in the caller's own namespace.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {onboarding, clipper_network}
- **Path params:** `subject_id`
- **Request body:** none
- **Response:** 200 JSON — from `age_subject` (services/verification-py/src/service.py:2440): object with keys `allowed`, `status`, `attestation_id`, `unmet`, `detail`, `subject_id`, `rules_pinned`.
- **Errors raised on this code path:** 503 `Unavailable`

#### `POST /vi/v1/identity/checks`

- **Source:** `services/verification-py/src/api.py:444`
- **Purpose:** Identity check for a clipper (email).
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {clipper_network}
- **Request body:** JSON `IdentityCheck` (services/verification-py/src/models.py:165); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `clipper_id: Id`
    - `email: Annotated[str, StringConstraints(pattern=r"^[^\s@\x00-\x1f\x7f]{1,64}@[^\s@\x00-\x1f\x7f]{1,189}$")]`
- **Response:** 200 JSON — from `identity_check` (services/verification-py/src/service.py:2459): object with keys `request_id`, `rules_pinned`, `check_id`, `clipper_id`, `status`, `clear`, `why`, `findings`, `ledger_event_ids`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /vi/v1/clippers/{clipper_id}/integrity`

- **Source:** `services/verification-py/src/api.py:448`
- **Purpose:** A clipper's integrity view.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {clipper_network, compliance_38}
- **Path params:** `clipper_id`
- **Request body:** none
- **Response:** 200 JSON — from `clipper_integrity` (services/verification-py/src/service.py:2733): object with keys `clipper_id`, `rules_pinned`, `strikes_active`, `strikes`, `ban_recommended`, `banned`, `bought_engagement_findings`, `duplicate_identity_findings`, `age`, `identity`, `open_holds`, `note`.
- **Errors raised on this code path:** none found statically

#### `GET /vi/v1/strikes`

- **Source:** `services/verification-py/src/api.py:452`
- **Purpose:** Cursor feed of strikes (Clipper Network).
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {clipper_network}
- **Query params:** `cursor: int = Query(default=0, ge=0, le=10**12)`
- **Request body:** none
- **Response:** 200 JSON — from `feed_strikes` (services/verification-py/src/service.py:2698): object with keys `**page`, `rules_pinned`.
- **Errors raised on this code path:** none found statically

#### `GET /vi/v1/holds`

- **Source:** `services/verification-py/src/api.py:458`
- **Purpose:** List holds.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `list_holds` (services/verification-py/src/service.py:2724): returns `list[dict]`.
- **Errors raised on this code path:** none found statically

#### `GET /vi/v1/findings`

- **Source:** `services/verification-py/src/api.py:462`
- **Purpose:** List findings.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `list_findings` (services/verification-py/src/service.py:2728): returns `list[dict]`.
- **Errors raised on this code path:** none found statically

#### `GET /vi/v1/findings/{finding_id}`

- **Source:** `services/verification-py/src/api.py:466`
- **Purpose:** Read one finding.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {clipper_network, compliance_38}
- **Path params:** `finding_id`
- **Request body:** none
- **Response:** 200 JSON — from `finding_view` (services/verification-py/src/service.py:2703): object with keys `**{k: f.get(k) for k in ("finding_id", "ki`, `subject_ref`, `rules_pinned`.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /vi/v1/holds/{hold_id}/decision`

- **Source:** `services/verification-py/src/api.py:470`
- **Purpose:** Decide a hold (Andre or confirmed reviewer).
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre) OR `X-VI-Reviewer-Token` of a reviewer People-43 confirms (stand-in confirms nobody → Andre only)
- **Path params:** `hold_id`
- **Request body:** JSON `DecisionRequest` (services/verification-py/src/models.py:171); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `decision: Literal["release", "uphold", "overturn"]`
    - `reason: Text`
    - `appeal_id: Optional[Id] = None`
- **Response:** 200 JSON — from `decide_hold` (services/verification-py/src/service.py:2519): object with keys `hold`, `ledger_event_ids`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /vi/v1/findings/{finding_id}/decision`

- **Source:** `services/verification-py/src/api.py:475`
- **Purpose:** Decide a finding (Andre or confirmed reviewer).
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre) OR `X-VI-Reviewer-Token` of a reviewer People-43 confirms (stand-in confirms nobody → Andre only)
- **Path params:** `finding_id`
- **Request body:** JSON `DecisionRequest` (services/verification-py/src/models.py:171); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `decision: Literal["release", "uphold", "overturn"]`
    - `reason: Text`
    - `appeal_id: Optional[Id] = None`
- **Response:** 200 JSON — from `decide_finding_route` (services/verification-py/src/service.py:2597): object with keys `finding`, `ledger_event_ids`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /vi/v1/bans`

- **Source:** `services/verification-py/src/api.py:481`
- **Purpose:** Clipper Network reports Andre's ban decision (needs caller AND Andre token).
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {clipper_network}; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `BanRequest` (services/verification-py/src/models.py:178); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `clipper_id: Id`
    - `cn_decision_id: Id`
    - `approved_at: Timestamp`
- **Response:** 200 JSON — from `ban` (services/verification-py/src/service.py:2615): object with keys `request_id`, `clipper_id`, `rules_pinned`, `ban`, `ledger_event_ids`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /vi/v1/rules`

- **Source:** `services/verification-py/src/api.py:488`
- **Purpose:** Current V&I rules register.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `rules_view` (services/verification-py/src/service.py:544): object with keys `rules_version`, `rules_sha256`, `rules_pinned`, `seed_sha256`, `rules`, `versions`, `open_proposals`.
- **Errors raised on this code path:** 503 `Unavailable`

#### `POST /vi/v1/rules/proposals`

- **Source:** `services/verification-py/src/api.py:492`
- **Purpose:** Andre proposes a rule change.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `RuleProposalRequest` (services/verification-py/src/models.py:185); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `kind: Literal["add", "amend", "retire"]`
    - `target_id: Optional[Annotated[str, StringConstraints(pattern=r"^VI-[0-9]{2,3}[a-z]?$")]] = None`
    - `proposed_row: Optional[dict] = None`
- **Response:** 201 JSON — from `create_rule_proposal` (services/verification-py/src/service.py:555): object with keys `proposal`, `ledger_event_ids`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /vi/v1/rules/decisions`

- **Source:** `services/verification-py/src/api.py:497`
- **Purpose:** Andre approves/rejects rule proposals.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `RuleDecisions` (services/verification-py/src/models.py:200); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `decisions: list[RuleDecision] = Field(min_length=1, max_length=200)`
- **Response:** 200 JSON — from `decide_rules` (services/verification-py/src/service.py:573): object with keys `decided`, `approved`, `rules_version`, `rules_sha256`, `ledger_event_ids`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /vi/v1/jobs/{job}/run`

- **Source:** `services/verification-py/src/api.py:503`
- **Purpose:** Scheduler runs a named V&I job (unknown job -> 422).
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = caller ∈ {scheduler}
- **Path params:** `job`
- **Request body:** JSON `RunRequest` (services/verification-py/src/models.py:74); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
- **Response:** 200 JSON — from `run_job` (services/verification-py/src/service.py:1786): object with keys `job`, `day`, `already_ran`, `summary`, `ledger_event_ids`; object with keys `job`, `day`, `already_ran`, `summary`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /vi/v1/reconcile`

- **Source:** `services/verification-py/src/api.py:509`
- **Purpose:** Andre previews what a ledger reconcile would void.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** none
- **Response:** 200 JSON — from `reconcile_plan` (services/verification-py/src/service.py:470): object with keys `epoch`, `head_seq`, `head_sha256`, `rules_version`, `fatal`, `problems`, `voidable`, `reconcile_mode`.
- **Errors raised on this code path:** none found statically

#### `POST /vi/v1/reconcile`

- **Source:** `services/verification-py/src/api.py:513`
- **Purpose:** Andre voids stray lines/events to reconcile with the ledger.
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `ReconcileRequest` (services/verification-py/src/models.py:205); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `head_sha256: Sha256`
    - `void_lines: list[Annotated[int, Field(ge=1, le=10**12)]] = Field(max_length=10_000)`
    - `void_event_ids: list[Id] = Field(max_length=10_000)`
- **Response:** 200 JSON — from `reconcile` (services/verification-py/src/service.py:479): object with keys `reconcile_event_id`, `voided`, `void_lines`, `remaining_problems`, `restart_required`, `ledger_event_ids`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /vi/v1/audit/export`

- **Source:** `services/verification-py/src/api.py:518`
- **Purpose:** Audit export of the local record log (paged; optional since/until).
- **Auth:** `Authorization: Bearer <VI_SERVICE_TOKEN>`; plus `X-VI-Caller-Token` = any recognised caller
- **Query params:** `since: Optional[str] = Query(default=None, max_length=40)`; `until: Optional[str] = Query(default=None, max_length=40)`; `cursor: int = Query(default=0, ge=0, le=10**12)`
- **Request body:** none
- **Response:** 200 JSON — from `audit_export` (services/verification-py/src/service.py:2753): object with keys `records`, `next_cursor`, `ledger_event_id`, `rules_version`.
- **Errors raised on this code path:** 422 `Invalid`, 503 `Unavailable`

---

## 10. clipper-network-py (Clipper Network)

* **Language / entry:** Python FastAPI, `services/clipper-network-py/src/api.py`. Started with `cd services/clipper-network-py/src && python3 -m api` (`main()` `api.py:588-593`), which uses the hardened launcher `serve.py`.
* **Bind:** `CN_BIND_ADDR` (default `127.0.0.1`), `CN_PORT` (default `8400`).
* **Required env:** `CN_SERVICE_TOKEN` (refuses to start without it, `src/config.py:142-145`).
* **Auth:** Bearer service token on every route except `/health`. Caller identity: `X-CN-Caller-Token` → `CN_CALLER_TOKENS` (names: hub, onboarding, creative_production, verification_integrity, finance_31, compliance_38, scheduler; `src/config.py:26-27`). Andre: `X-Andre-Approval-Token` → `CN_ANDRE_APPROVAL_TOKEN`; delegates `X-CN-Delegate-Token` → `CN_DELEGATE_TOKENS` (dispute outcome only; People-43 stand-in confirms nobody).
* **Other env & rules:** `CN_DATA_DIR`, `CN_RULES_SEED_PATH`, `CN_RECONCILE_MODE`, `CN_IDENTITY_HMAC_KEY`, `CN_POSTAL_ADDRESS`, `CN_OPT_OUT_URL`; thin clients `CN_VI_URL`/`CN_VI_SERVICE_TOKEN`/`CN_VI_CALLER_TOKEN`, `CN_COMPLIANCE_URL`/`CN_COMPLIANCE_SERVICE_TOKEN`/`CN_COMPLIANCE_CALLER_TOKEN`, `CN_CREATIVE_URL`/`CN_CREATIVE_SERVICE_TOKEN` (`src/config.py:125-201`); `CN_FINANCE_URL`, `CN_LEGAL_URL`, `CN_PEOPLE_URL`, `CN_HUB_URL`, `CN_PUSH_URL` are not built (setting one refuses start-up, `src/config.py:60`). Path ids must match `[A-Za-z0-9._:-]{1,128}` (`cid`, 422).
* **Request limits (`InputLimits`, outermost middleware, `services/clipper-network-py/src/api.py`):**
  * Request target (path + query) over 4,096 bytes → **414**; head over 16 KiB → **431**; `Content-Length` not a digit → **400**.
  * Body over the route cap → **413**, checked from `Content-Length` and again on the bytes received. Route caps: `recruiting/campaigns` 512 KiB, `rules|templates/proposals` 64 KiB, `rules/decisions` 32 KiB, `disputes` 48 KiB, `reconcile` 1 MiB, everything else 16 KiB (`api.py:73-80`); service cap 1 MiB.
  * A body whose `Content-Type` is not `application/json` or `application/*+json` → **415**.
  * JSON nested deeper than 32 or with more than 20,000 members → **422**.
  * Body not received within 30 s → **408**.
  * The hardened launcher also caps the head at 16 KiB in the parser, sets a 10 s head deadline and a 5 s keep-alive, and limits concurrency to 128 (beyond it uvicorn answers **503**).
  * Validation errors → **422** `{"detail":[{loc,msg,type}… ≤20], "errors_total": N}` (input never echoed). An unhandled exception → **500** `{"detail":"internal error"}`.
* **Errors on every route:** 401 (bearer); 403 `Forbidden` / `FounderRefused` (caller or Andre identity); 409 `Conflict` (request_id reuse); 503 `Unavailable` with `Retry-After: 1` (ledger write failed, reconcile mode, port down); 500. Domain errors answer `{"detail": <reason>, …extra body}`.
* **State:** see the summary table (JSONL log if the data dir is set, otherwise in-memory).


**43 endpoints.**

#### `GET /health`

- **Source:** `services/clipper-network-py/src/api.py:323`
- **Purpose:** Liveness/status of the service.
- **Auth:** none (open)
- **Request body:** none
- **Response:** 200 JSON — from `health` (services/clipper-network-py/src/service.py:2700): object with keys `status`, `service`, `rules_version`, `in_memory`, `rules_pinned`, `reconcile_mode`, `reconcile_required`.
- **Errors raised on this code path:** none found statically

#### `GET /intelligences`

- **Source:** `services/clipper-network-py/src/api.py:327`
- **Purpose:** List the department's intelligence modules.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 — JSON array from `registry()` (services/clipper-network-py/src/intelligences/__init__.py:12): one object per intelligence with keys `number`, `name`, `actor`, `llm` (false).
- **Errors raised on this code path:** none found statically

#### `GET /cn/v1/integrity`

- **Source:** `services/clipper-network-py/src/api.py:331`
- **Purpose:** Integrity check of the local log vs the evidence ledger.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `integrity` (services/clipper-network-py/src/service.py:411): object with keys `ledger_verify`, `local_log_chain`, `anchor_problems`, `contacts_missing`, `ok`.
- **Errors raised on this code path:** none found statically

#### `POST /cn/v1/opt-ins`

- **Source:** `services/clipper-network-py/src/api.py:337`
- **Purpose:** Hub records a recruiting opt-in.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub}
- **Request body:** JSON `OptInRequest` (services/clipper-network-py/src/models.py:96); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `email: Email`
    - `recipient_country: Iso2`
    - `time_zone: Tz`
    - `consent_text_sha256: Sha`
    - `source_form_id: Id`
    - `captured_at: Ts`
    - `age_18_plus_confirmed: StrictBool`
- **Response:** 201 JSON — from `opt_in` (services/clipper-network-py/src/service.py:791): object with keys `opt_in_record_id`, `status`, `ledger_event_ids`.
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /cn/v1/opt-outs`

- **Source:** `services/clipper-network-py/src/api.py:341`
- **Purpose:** Hub records an opt-out (suppressed immediately, contact data deleted).
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub}
- **Request body:** JSON `OptOutRequest` (services/clipper-network-py/src/models.py:115); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `email: Email`
- **Response:** 200 JSON — from `opt_out` (services/clipper-network-py/src/service.py:822): object with keys `email_hmac`, `opt_ins_withdrawn`, `honored_at`, `ledger_event_ids`.
- **Errors raised on this code path:** 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/recruiting/campaigns`

- **Source:** `services/clipper-network-py/src/api.py:345`
- **Purpose:** Andre creates a recruiting campaign.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `RecruitingCampaignRequest` (services/clipper-network-py/src/models.py:120); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `channel: Literal["email_opt_in", "discord_server_post"]`
    - `template_id: Literal["recruiting_invite"]`
    - `recipients: list[Annotated[str, Field(min_length=1, max_length=254), AfterValidator(_no_control)]] = Field(default_factory=list, max_length=1000)`
    - `discord_server_ref: Optional[Id] = None`
- **Response:** 201 JSON — from `create_recruiting` (services/clipper-network-py/src/service.py:847): object with keys `recruit_id`, `recipients`, `ledger_event_ids`.
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /cn/v1/recruiting/campaigns/{recruit_id}/send`

- **Source:** `services/clipper-network-py/src/api.py:350`
- **Purpose:** Scheduler sends a recruiting campaign.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {scheduler}
- **Path params:** `recruit_id`
- **Request body:** JSON `RunRequest` (services/clipper-network-py/src/models.py:90); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
- **Response:** 200 JSON — from `send_recruiting` (services/clipper-network-py/src/service.py:877): object with keys `recruit_id`, `queued`, `sent`, `refused`, `delivery`, `ledger_event_ids`; object with keys `recruit_id`, `sent`, `queued`, `refused`, `ledger_event_ids`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/applications`

- **Source:** `services/clipper-network-py/src/api.py:357`
- **Purpose:** Clipper application intake (hub / Onboarding).
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub, onboarding}
- **Request body:** JSON `ApplicationRequest` (services/clipper-network-py/src/models.py:131); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `display_name: Annotated[str, Field(min_length=1, max_length=80), AfterValidator(_no_control), AfterValidator(_display_name)]`
    - `email: Email`
    - `declared_country: Iso2`
    - `declared_region: Optional[Region] = None`
    - `jurisdiction_attested: StrictBool`
    - `time_zone: Optional[Tz] = None`
    - `channel: Literal[APPLICATION_CHANNELS]`
    - `referrer_clipper_id: Optional[Id] = None`
    - `opt_in_record_id: Optional[Id] = None`
    - `declared_18_plus: StrictBool`
    - `sag_aftra_member: StrictBool`
    - `statement: Optional[Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(_no_control_ml)]] = None`
- **Response:** 201 JSON — from `apply` (services/clipper-network-py/src/service.py:999): object with keys `application_id`, `clipper_id`, `status`, `injection_text_ignored`, `ledger_event_ids`.
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /cn/v1/applications/{application_id}`

- **Source:** `services/clipper-network-py/src/api.py:362`
- **Purpose:** Read one application.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub, onboarding}
- **Path params:** `application_id`
- **Request body:** none
- **Response:** 200 JSON — from `get_application` (services/clipper-network-py/src/service.py:1102): shape not statically determinable (returns `{k: v for k, v in a.items() if k != "principal"}`) — UNKNOWN beyond the cited method.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /cn/v1/clippers/{clipper_id}/connections/start`

- **Source:** `services/clipper-network-py/src/api.py:366`
- **Purpose:** Relay a platform-connection start to V&I.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub}
- **Path params:** `clipper_id`
- **Request body:** JSON `ConnectionStartRequest` (services/clipper-network-py/src/models.py:161); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `platform: Literal[PLATFORMS]`
    - `redirect_uri: Annotated[str, Field(max_length=512, pattern=r"^https://[^\s]{1,500}$")]`
    - `handle: Optional[Annotated[str, Field(min_length=1, max_length=100), AfterValidator(_no_control)]] = None`
- **Response:** 200 JSON — from `connection_start` (services/clipper-network-py/src/service.py:1115): object with keys `available`, `started`, `connection_id`, `authorization_url`, `state_expires_at`, `reasons`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/clippers/{clipper_id}/connections/complete`

- **Source:** `services/clipper-network-py/src/api.py:371`
- **Purpose:** Relay a platform-connection completion (OAuth code) to V&I.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub}
- **Path params:** `clipper_id`
- **Request body:** JSON `ConnectionCompleteRequest` (services/clipper-network-py/src/models.py:168); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `state: Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9._~:-]{1,512}$")]`
    - `code: Annotated[str, Field(min_length=1, max_length=2048, pattern=r"^[\x21-\x7e]{1,2048}$")]`
- **Response:** 200 JSON — from `connection_complete` (services/clipper-network-py/src/service.py:1158): object with keys `available`, `connection_id`, `status`, `reasons`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/clippers/{clipper_id}/age-check`

- **Source:** `services/clipper-network-py/src/api.py:376`
- **Purpose:** Relay an age check to V&I (DOB passed through, never stored).
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub}
- **Path params:** `clipper_id`
- **Request body:** JSON `AgeCheckRequest` (services/clipper-network-py/src/models.py:174); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `dob: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]`
    - `dob_field_neutral: StrictBool`
    - `method: Literal[AGE_METHODS]`
    - `provider_session_ref: Id`
- **Response:** 200 JSON — from `age_check` (services/clipper-network-py/src/service.py:1189): object with keys `available`, `result`, `vi_attestation_id`, `status`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/clippers/{clipper_id}/agreement-acceptances`

- **Source:** `services/clipper-network-py/src/api.py:381`
- **Purpose:** Record the clipper's acceptance of the current agreement version.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub}
- **Path params:** `clipper_id`
- **Request body:** JSON `AgreementAcceptanceRequest` (services/clipper-network-py/src/models.py:182); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `doc_id: Literal["clipper_agreement"]`
    - `version: Id`
    - `doc_sha256: Sha`
    - `presented_sha256: Sha`
    - `method: Literal["clickwrap_unticked_box"]`
    - `box_ticked: StrictBool`
    - `session_ref: Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_no_control)]`
- **Response:** 200 JSON — from `accept_agreement` (services/clipper-network-py/src/service.py:1246): object with keys `accepted`, `acceptance_id`, `unmet`, `unmet_lines`, `ledger_event_ids`; object with keys `accepted`, `unmet`, `unmet_lines`, `ledger_event_ids`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/clippers/{clipper_id}/disclosure-training`

- **Source:** `services/clipper-network-py/src/api.py:386`
- **Purpose:** Record the clipper's disclosure-training attestation.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub}
- **Path params:** `clipper_id`
- **Request body:** JSON `TrainingRequest` (services/clipper-network-py/src/models.py:193); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `training_version: Id`
    - `attested: StrictBool`
- **Response:** 200 JSON — from `attest_training` (services/clipper-network-py/src/service.py:1295): object with keys `**rec`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /cn/v1/clippers/{clipper_id}/admission`

- **Source:** `services/clipper-network-py/src/api.py:391`
- **Purpose:** Run the admission checks for a clipper.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub, onboarding, scheduler}
- **Path params:** `clipper_id`
- **Request body:** JSON `RunRequest` (services/clipper-network-py/src/models.py:90); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
- **Response:** 200 JSON — from `admission` (services/clipper-network-py/src/service.py:1314): object with keys `admission_id`, `clipper_id`, `admitted`, `unmet`, `unmet_lines`, `request_id`, `facts_sha256`, `rules_pinned`, `rules_version`, `ledger_event_id`, `status`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `Unavailable`

#### `GET /cn/v1/clippers/{clipper_id}`

- **Source:** `services/clipper-network-py/src/api.py:396`
- **Purpose:** Read a clipper (contact data only for the hub caller).
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = any recognised caller
- **Path params:** `clipper_id`
- **Request body:** none
- **Response:** 200 JSON — from `clipper_view` (services/clipper-network-py/src/service.py:1477): shape not statically determinable (returns `out`) — UNKNOWN beyond the cited method.
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /cn/v1/clippers/{clipper_id}/messages`

- **Source:** `services/clipper-network-py/src/api.py:400`
- **Purpose:** Messages for a clipper (hub).
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub}
- **Path params:** `clipper_id`
- **Request body:** none
- **Response:** 200 JSON — from `messages_for` (services/clipper-network-py/src/service.py:778): returns `list[dict]`.
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /cn/v1/clippers/{clipper_id}/export`

- **Source:** `services/clipper-network-py/src/api.py:404`
- **Purpose:** Export the clipper's own data (hub).
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub}
- **Path params:** `clipper_id`
- **Request body:** none
- **Response:** 200 JSON — from `export_for` (services/clipper-network-py/src/service.py:2667): object with keys `export`, `export_sha256`, `ledger_event_id`.
- **Errors raised on this code path:** 404 `NotFound`, 503 `Unavailable`

#### `POST /cn/v1/clippers/{clipper_id}/tier-nomination`

- **Source:** `services/clipper-network-py/src/api.py:408`
- **Purpose:** Andre nominates/un-nominates a clipper for a tier.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `clipper_id`
- **Request body:** JSON `TierNominationRequest` (services/clipper-network-py/src/models.py:315); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `nominate: StrictBool`
- **Response:** 200 JSON — from `nominate` (services/clipper-network-py/src/service.py:2319): object with keys `clipper_id`, `nominated`, `note`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `Unavailable`

#### `PUT /cn/v1/campaigns/{campaign_id}/network-config`

- **Source:** `services/clipper-network-py/src/api.py:415`
- **Purpose:** Andre sets a campaign's network config (new version per change).
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `campaign_id`
- **Request body:** JSON `NetworkConfigRequest` (services/clipper-network-py/src/models.py:212); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `min_tier: Tier`
    - `platforms: list[Literal[PLATFORMS]] = Field(min_length=1, max_length=4)`
    - `clipper_jurisdictions: list[Code] = Field(min_length=1, max_length=60)`
    - `max_clippers: StrictInt = Field(ge=1, le=100_000)`
    - `max_submissions_per_clipper: StrictInt = Field(ge=1, le=10_000)`
    - `view_terms: ViewTerms`
    - `rate_card_ref: RateCardRef`
    - `rate_card_effective_at: Ts`
    - `opens_at: Ts`
    - `closes_at: Ts`
- **Response:** 200 JSON — from `put_network_config` (services/clipper-network-py/src/service.py:1517): object with keys `config`, `rate_card_changed`, `notified`, `ledger_event_ids`.
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /cn/v1/campaigns/{campaign_id}/rulebook-announcements`

- **Source:** `services/clipper-network-py/src/api.py:420`
- **Purpose:** Creative Production announces a new rulebook version to enrolments.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {creative_production}
- **Path params:** `campaign_id`
- **Request body:** JSON `AnnouncementRequest` (services/clipper-network-py/src/models.py:239); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `version: StrictInt = Field(ge=1, le=1_000_000)`
    - `facts: Annotated[dict[str, Any], AfterValidator(_bounded_json)] = Field(default_factory=dict)`
- **Response:** 200 JSON — from `announce` (services/clipper-network-py/src/service.py:1577): object with keys `**base`, `allowed`, `reason`, `kit_delivered`, `ledger_event_ids`; object with keys `**base`, `allowed`, `reason`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/campaigns/{campaign_id}/enrolments`

- **Source:** `services/clipper-network-py/src/api.py:425`
- **Purpose:** Hub enrols a clipper in a campaign.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub}
- **Path params:** `campaign_id`
- **Request body:** JSON `EnrolmentRequest` (services/clipper-network-py/src/models.py:245); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `clipper_id: Id`
- **Response:** 200 JSON — from `enrol` (services/clipper-network-py/src/service.py:1648): object with keys `ruling_id`, `campaign_id`, `clipper_id`, `eligible`, `enrolment_id`, `kit_delivery_id`, `unmet`, `unmet_lines`, `request_id`, `facts_sha256`, `rules_pinned`, `rules_version`, `ledger_event_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `Unavailable`

#### `GET /cn/v1/campaigns/{campaign_id}/enrolments`

- **Source:** `services/clipper-network-py/src/api.py:430`
- **Purpose:** List a campaign's enrolments (hub).
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub}
- **Path params:** `campaign_id`
- **Request body:** none
- **Response:** 200 JSON — from `list_enrolments` (services/clipper-network-py/src/service.py:1771): returns `list[dict]`; via _enrolments_of (services/clipper-network-py/src/service.py:1510).
- **Errors raised on this code path:** none found statically

#### `POST /cn/v1/enrolments/{enrolment_id}/kit-acknowledgment`

- **Source:** `services/clipper-network-py/src/api.py:434`
- **Purpose:** Hub records a clipper's kit acknowledgment.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub}
- **Path params:** `enrolment_id`
- **Request body:** JSON `KitAckRequest` (services/clipper-network-py/src/models.py:250); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `kit_delivery_id: Id`
    - `kit_sha256: Sha`
    - `rulebook_version: StrictInt = Field(ge=1, le=1_000_000)`
    - `rate_card_version: Id`
    - `rulebook_received: StrictBool`
    - `disclosure_section_received: StrictBool`
- **Response:** 200 JSON — from `kit_ack` (services/clipper-network-py/src/service.py:1775): object with keys `**new`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /cn/v1/rules`

- **Source:** `services/clipper-network-py/src/api.py:441`
- **Purpose:** Current Clipper Network rules register.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `rules_view` (services/clipper-network-py/src/service.py:478): object with keys `rules_version`, `content_sha256`, `version_sha256`, `rules`, `templates`, `rules_pinned`; object with keys `rules_version`, `rules`, `templates`, `rules_pinned`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** none found statically

#### `GET /cn/v1/inbox`

- **Source:** `services/clipper-network-py/src/api.py:445`
- **Purpose:** Andre's proposal inbox.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `inbox` (services/clipper-network-py/src/service.py:486): returns `list[dict]`.
- **Errors raised on this code path:** 503 `Unavailable`

#### `POST /cn/v1/rules/proposals`

- **Source:** `services/clipper-network-py/src/api.py:449`
- **Purpose:** Andre proposes a rule change.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `RuleProposalRequest` (services/clipper-network-py/src/models.py:262); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `kind: Literal["new", "amend", "retire", "counsel_memo"]`
    - `target_id: Optional[Annotated[str, Field(pattern=r"^CN-[A-Z0-9-]{2,12}$")]] = None`
    - `rule: Optional[dict[str, Any]] = None`
    - `memo: Optional[dict[str, Any]] = None`
- **Response:** 201 JSON — from `create_proposal` (services/clipper-network-py/src/service.py:492): object with keys `proposal`, `ledger_event_ids`.
- **Errors raised on this code path:** 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/templates/proposals`

- **Source:** `services/clipper-network-py/src/api.py:454`
- **Purpose:** Andre proposes a message-template change.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `TemplateProposalRequest` (services/clipper-network-py/src/models.py:270); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `kind: Literal["new", "amend", "retire"]`
    - `target_id: Optional[Annotated[str, Field(pattern=r"^[a-z_]{1,40}$")]] = None`
    - `template: Optional[dict[str, Any]] = None`
- **Response:** 201 JSON — from `create_proposal` (services/clipper-network-py/src/service.py:492): object with keys `proposal`, `ledger_event_ids`.
- **Errors raised on this code path:** 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/rules/decisions`

- **Source:** `services/clipper-network-py/src/api.py:459`
- **Purpose:** Andre approves/rejects rule/template proposals.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `DecisionsRequest` (services/clipper-network-py/src/models.py:285); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `decisions: list[Decision] = Field(min_length=1, max_length=100)`
- **Response:** 200 JSON — from `decide` (services/clipper-network-py/src/service.py:518): object with keys `decided`, `approved`, `rules_version`, `content_sha256`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /cn/v1/disputes`

- **Source:** `services/clipper-network-py/src/api.py:466`
- **Purpose:** Hub files a clipper dispute.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {hub}
- **Request body:** JSON `DisputeRequest` (services/clipper-network-py/src/models.py:292); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `clipper_id: Id`
    - `notice_message_id: Id`
    - `subject_kind: Literal["clip_flag", "vi_finding", "strike", "ban", "suspension", "tier", "enrolment", "admission"]`
    - `subject_ref: Id`
    - `statement: Annotated[str, Field(min_length=1, max_length=4000), AfterValidator(_no_control_ml)]`
    - `evidence_refs: list[Id] = Field(default_factory=list, max_length=20)`
- **Response:** 201 JSON — from `file_dispute` (services/clipper-network-py/src/service.py:1803): object with keys `**self._dispute_view(rec)`, `unmet_lines`, `ledger_event_ids`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `Unavailable`

#### `GET /cn/v1/disputes/{dispute_id}`

- **Source:** `services/clipper-network-py/src/api.py:470`
- **Purpose:** Read one dispute.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = any recognised caller
- **Path params:** `dispute_id`
- **Request body:** none
- **Response:** 200 JSON — from `get_dispute` (services/clipper-network-py/src/service.py:1908): object with keys `**self._dispute_view(d)`, `evidence_packet`.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /cn/v1/disputes/{dispute_id}/outcome`

- **Source:** `services/clipper-network-py/src/api.py:474`
- **Purpose:** Andre (or a People-confirmed delegate) decides a dispute.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre) OR `X-CN-Delegate-Token` of a delegate People (43) confirms — the People stand-in confirms nobody, so Andre only in this build (api.py:475-488)
- **Path params:** `dispute_id`
- **Request body:** JSON `DisputeOutcomeRequest` (services/clipper-network-py/src/models.py:302); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `outcome: Literal["appeal_granted", "appeal_denied"]`
    - `note: Note`
- **Response:** 200 JSON — from `dispute_outcome` (services/clipper-network-py/src/service.py:1920): object with keys `**nd`, `ledger_event_ids`.
- **Errors raised on this code path:** 403 `FounderRefused`, 404 `NotFound`, 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/disputes/sla-run`

- **Source:** `services/clipper-network-py/src/api.py:489`
- **Purpose:** Scheduler runs the dispute SLA pass.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {scheduler}
- **Request body:** JSON `RunRequest` (services/clipper-network-py/src/models.py:90); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
- **Response:** 200 JSON — from `disputes_sla_run` (services/clipper-network-py/src/service.py:1963): object with keys `ran`, `**out`; object with keys `ran`, `unmet`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/discipline/sync`

- **Source:** `services/clipper-network-py/src/api.py:495`
- **Purpose:** Scheduler pulls V&I strikes and applies discipline.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {scheduler}
- **Request body:** JSON `RunRequest` (services/clipper-network-py/src/models.py:90); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
- **Response:** 200 JSON — from `discipline_sync` (services/clipper-network-py/src/service.py:2013): object with keys `ran`, `**result`; object with keys `ran`, `unmet`; object with keys `ran`, `unmet`, `ledger_event_ids`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/clippers/{clipper_id}/ban-decision`

- **Source:** `services/clipper-network-py/src/api.py:499`
- **Purpose:** Andre decides a ban proposal (forwards to V&I /vi/v1/bans).
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate); plus Andre's exact token is forwarded to V&I `POST /vi/v1/bans` (api.py:500-504)
- **Path params:** `clipper_id`
- **Request body:** JSON `BanDecisionRequest` (services/clipper-network-py/src/models.py:308); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `proposal_id: Id`
    - `decision: Literal["approve", "reject"]`
    - `note: Note`
- **Response:** 200 JSON — from `ban_decision` (services/clipper-network-py/src/service.py:2186): object with keys `proposal_id`, `status`, `decision_id`, `clipper_status`, `vi_ban_propagation`, `offboarding_id`, `ledger_event_ids`; object with keys `proposal_id`, `status`, `clipper_status`, `ledger_event_ids`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** 403 `Forbidden`, 404 `NotFound`, 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/tiers/run`

- **Source:** `services/clipper-network-py/src/api.py:506`
- **Purpose:** Scheduler runs tier recalculation.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {scheduler}
- **Request body:** JSON `RunRequest` (services/clipper-network-py/src/models.py:90); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
- **Response:** 200 JSON — from `tiers_run` (services/clipper-network-py/src/service.py:2287): object with keys `ran`, `changed`, `vi_unavailable_for`; object with keys `ran`, `unmet`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/messages/flush`

- **Source:** `services/clipper-network-py/src/api.py:510`
- **Purpose:** Scheduler flushes queued messages.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {scheduler}
- **Request body:** JSON `RunRequest` (services/clipper-network-py/src/models.py:90); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
- **Response:** 200 JSON — from `flush_messages` (services/clipper-network-py/src/service.py:761): object with keys `ran`, `attempted`, `outcomes`; object with keys `ran`, `unmet`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** 409 `Conflict`, 503 `Unavailable`

#### `POST /cn/v1/clippers/{clipper_id}/offboarding`

- **Source:** `services/clipper-network-py/src/api.py:516`
- **Purpose:** Start a clipper's offboarding (Andre decision or clipper request via hub).
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus body `trigger` = `andre_decision` → `X-Andre-Approval-Token`; otherwise `X-CN-Caller-Token` must be hub (else 403) (api.py:517-525)
- **Path params:** `clipper_id`
- **Request body:** JSON `OffboardingRequest` (services/clipper-network-py/src/models.py:320); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `trigger: Literal["clipper_request", "andre_decision"]`
    - `keep_connections_until_settlement: Optional[StrictBool] = None`
- **Response:** 200 JSON — from `offboard` (services/clipper-network-py/src/service.py:2334): object with keys `**o`, `clipper_status`; via offboarding_view (services/clipper-network-py/src/service.py:2500).
- **Errors raised on this code path:** 403 `Forbidden`, 404 `NotFound`, 409 `Conflict`, 503 `Unavailable`

#### `GET /cn/v1/clippers/{clipper_id}/offboarding`

- **Source:** `services/clipper-network-py/src/api.py:526`
- **Purpose:** Read a clipper's offboarding status (hub or Andre).
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre) OR `X-CN-Caller-Token` = hub (else 403) (api.py:527-532)
- **Path params:** `clipper_id`
- **Request body:** none
- **Response:** 200 JSON — from `offboarding_view` (services/clipper-network-py/src/service.py:2500): object with keys `**o`, `clipper_status`.
- **Errors raised on this code path:** 403 `Forbidden`, 404 `NotFound`

#### `POST /cn/v1/offboarding/run`

- **Source:** `services/clipper-network-py/src/api.py:534`
- **Purpose:** Scheduler advances every open offboarding.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = caller ∈ {scheduler}
- **Request body:** JSON `RunRequest` (services/clipper-network-py/src/models.py:90); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
- **Response:** 200 JSON — from `offboarding_run` (services/clipper-network-py/src/service.py:2508): object with keys `ran`, `**out`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 503 `Unavailable`

#### `GET /cn/v1/reconcile`

- **Source:** `services/clipper-network-py/src/api.py:540`
- **Purpose:** Andre previews what a ledger reconcile would void.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** none
- **Response:** 200 JSON — from `reconcile_plan` (services/clipper-network-py/src/service.py:367): object with keys `epoch`, `head_seq`, `head_sha256`, `rules_version`, `fatal`, `problems`, `voidable`, `reconcile_mode`.
- **Errors raised on this code path:** none found statically

#### `POST /cn/v1/reconcile`

- **Source:** `services/clipper-network-py/src/api.py:544`
- **Purpose:** Andre voids stray lines/events to reconcile with the ledger.
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `ReconcileRequest` (services/clipper-network-py/src/models.py:326); `model_config = ConfigDict(extra="forbid")`
    - `request_id: Id`
    - `head_sha256: Sha`
    - `void_lines: list[Annotated[int, Field(ge=1, le=10**12)]] = Field(max_length=10_000)`
    - `void_event_ids: list[Id] = Field(max_length=10_000)`
- **Response:** 200 JSON — from `reconcile` (services/clipper-network-py/src/service.py:376): object with keys `reconcile_event_id`, `voided`, `void_lines`, `remaining_problems`, `restart_required`, `ledger_event_ids`.
- **Errors raised on this code path:** 409 `Conflict`, 503 `Unavailable`

#### `GET /cn/v1/audit/export`

- **Source:** `services/clipper-network-py/src/api.py:548`
- **Purpose:** Audit export of the local record log (paged; optional since/until).
- **Auth:** `Authorization: Bearer <CN_SERVICE_TOKEN>`; plus `X-CN-Caller-Token` = any recognised caller
- **Query params:** `since: Optional[str] = Query(default=None, max_length=40)`; `until: Optional[str] = Query(default=None, max_length=40)`; `cursor: int = Query(default=0, ge=0, le=10**12)`
- **Request body:** none
- **Response:** 200 JSON — from `audit_export` (services/clipper-network-py/src/service.py:2679): object with keys `records`, `next_cursor`, `ledger_event_id`, `rules_version`.
- **Errors raised on this code path:** 422 `Invalid`, 503 `Unavailable`

---

## 11. finance-py (Finance 31)

* **Language / entry:** Python FastAPI, `services/finance-py/src/api.py`. Started with `cd services/finance-py/src && python3 -m api` (`main()` `api.py:736-741`), which uses the hardened launcher `serve.py`.
* **Bind:** `FIN_BIND_ADDR` (default `127.0.0.1`), `FIN_PORT` (default `8410`).
* **Required env:** `FIN_SERVICE_TOKEN` (≥32 printable ASCII; refuses to start without it, `src/config.py:151-156`).
* **Auth:** Bearer service token on every route except `/health`. Caller identity: `X-FIN-Caller-Token` → `FIN_CALLER_TOKENS` (names: compliance_38, clipper_network, verification_integrity, creative_production, onboarding, legal_37, scheduler, rail_gateway, bank_feed; `src/config.py:20-21`). Andre: `X-Andre-Approval-Token` → `FIN_ANDRE_APPROVAL_TOKEN`; second approver `X-FIN-Second-Approver-Token` → `FIN_SECOND_APPROVER_TOKEN` (all tokens must be distinct, `config.py:168`).
* **Other env & rules:** `FIN_DATA_DIR`, `FIN_SEED_PATH`, `FIN_RECONCILE_MODE`, many policy settings validated at start (`src/config.py:149-222`), `FIN_VI_URL`/`FIN_VI_TOKEN`/`FIN_VI_CALLER_TOKEN` and `FIN_COMPLIANCE_URL`/`FIN_COMPLIANCE_TOKEN`/`FIN_COMPLIANCE_CALLER_TOKEN` thin clients (`config.py:141-145,223-224`), `LEDGER_SERVICE_URL` + `LEDGER_SERVICE_TOKEN`. Path ids must match `[A-Za-z0-9._:-]{1,128}` and must not look like bank/card/tax data (`_id`, `api.py:250-253`, 422). Every body is scanned for bank/card/tax/identity data → **422** `SENSITIVE_DATA_REFUSED`; protocol routes refuse any key naming a count/metric/amount/rate → **422** `forbidden_field` (`api.py:307-327`). Out-of-range money → **422** `AMOUNT_OUT_OF_RANGE` (`api.py:344-348`). Extra domain errors: `Refused` 409 and `InvalidReasons` 422 carry `reasons` + `reason_lines`; `IntegrityRefused` 409 (`src/service.py:85-107`).
* **Request limits (`InputLimits`, outermost middleware, `services/finance-py/src/api.py`):**
  * Request target (path + query) over 4,096 bytes → **414**; head over 16 KiB → **431**; `Content-Length` not a digit → **400**.
  * Body over the route cap → **413**, checked from `Content-Length` and again on the bytes received. Route caps: `rules/(proposals|decisions)` 64 KiB, `rate-cards/decisions` 64 KiB, `bank/events` 128 KiB, `rails/*/events` 64 KiB, `invoices` 64 KiB, `payout-handoffs` 32 KiB, `journal/*/corrections` 32 KiB, `reconcile` 1 MiB, everything else 16 KiB (`api.py:67-78`); service cap 1 MiB.
  * A body whose `Content-Type` is not `application/json` or `application/*+json` → **415**.
  * JSON nested deeper than 32 or with more than 20,000 members → **422**.
  * Body not received within 30 s → **408**.
  * The hardened launcher also caps the head at 16 KiB in the parser, sets a 10 s head deadline and a 5 s keep-alive, and limits concurrency to 128 (beyond it uvicorn answers **503**).
  * Validation errors → **422** `{"detail":[{loc,msg,type}… ≤20], "errors_total": N}` (input never echoed). An unhandled exception → **500** `{"detail":"internal error"}`.
* **Errors on every route:** 401 (bearer); 403 `Forbidden` / `FounderRefused` (caller or Andre identity); 409 `Conflict` (request_id reuse); 503 `Unavailable` with `Retry-After: 1` (ledger write failed, reconcile mode, port down); 500. Domain errors answer `{"detail": <reason>, …extra body}`.
* **State:** see the summary table (JSONL log if the data dir is set, otherwise in-memory).


**67 endpoints.**

#### `GET /health`

- **Source:** `services/finance-py/src/api.py:360`
- **Purpose:** Liveness/status of the service (rules version, in-memory flag, reconcile mode).
- **Auth:** none (open)
- **Request body:** none
- **Response:** 200 JSON — from `health` (services/finance-py/src/service.py:880): object with keys `status`, `service`, `rules_version`, `in_memory`, `rules_pinned`, `production`, `entities`, `reconcile_mode`, `reconcile_required`.
- **Errors raised on this code path:** none found statically

#### `GET /fin/v1/intelligences`

- **Source:** `services/finance-py/src/api.py:364`
- **Purpose:** List the department's intelligence modules.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 — JSON array from `registry()` (services/finance-py/src/intelligences/__init__.py:11): one object per intelligence with keys `number`, `name`, `actor`.
- **Errors raised on this code path:** none found statically

#### `GET /fin/v1/integrity`

- **Source:** `services/finance-py/src/api.py:368`
- **Purpose:** Integrity check: local log chain, journal chain, anchoring, and ledger-rust /ledger/verify.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `integrity` (services/finance-py/src/service.py:529): object with keys `status`, `problems`, `log_lines`, `journal_entries`, `rules_version`, `checked_at`, `findings`.
- **Errors raised on this code path:** 503 `Unavailable`

#### `POST /fin/v1/payout-handoffs`

- **Source:** `services/finance-py/src/api.py:374`
- **Purpose:** Creative Production hands off an approved submission for payout.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {creative_production}
- **Request body:** JSON `PayoutHandoff` (services/finance-py/src/models.py:307); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `submission_id: Id`
    - `facts: HandoffFacts`
- **Response:** 200 JSON — from `accept_handoff` (services/finance-py/src/svc_payables.py:210): object with keys `department`, `allowed`, `reason`, `reference`, `reasons`, `request_id`, `facts_sha256`, `submission_id`, `rules_pinned`, `ledger_event_ids`.
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/payees`

- **Source:** `services/finance-py/src/api.py:379`
- **Purpose:** Create a payee (clipper) record.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {onboarding, clipper_network}
- **Request body:** JSON `PayeeCreate` (services/finance-py/src/models.py:313); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `payee_id: Id`
    - `kind: Literal["clipper"]`
    - `declared_country: Optional[Annotated[str, Field(pattern=r"^[A-Z]{2}$")]] = None`
    - `declared_region: Optional[Annotated[str, Field(pattern=r"^[A-Z]{2}-[A-Z0-9]{1,3}$")]] = None`
    - `callback_contact_ref: Optional[Annotated[str, Field(pattern=r"^vault:[A-Za-z0-9._:-]{1,120}$")]] = None`
    - `legal_form: Literal["individual", "entity"] = "individual"`
    - `owner_subject_ids: list[Id] = Field(default_factory=list, max_length=10)`
- **Response:** 200 JSON — from `create_payee` (services/finance-py/src/svc_payees.py:82): object with keys `payee_id`, `allowed`, `unmet`, `reasons`, `detail`, `request_id`, `facts_sha256`, `rules_pinned`, `ledger_event_ids`; object with keys `payee_id`, `allowed`, `unmet`, `reasons`, `detail`, `request_id`, `facts_sha256`, `rules_pinned`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /fin/v1/payees/{payee_id}`

- **Source:** `services/finance-py/src/api.py:384`
- **Purpose:** Read one payee.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Path params:** `payee_id`
- **Request body:** none
- **Response:** 200 JSON — from `payee_view` (services/finance-py/src/svc_payees.py:148): shape not statically determinable (returns `out`) — UNKNOWN beyond the cited method.
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /fin/v1/payees/{payee_id}/tax-status`

- **Source:** `services/finance-py/src/api.py:388`
- **Purpose:** Payee tax-form/TIN/backup-withholding status (for Compliance / Clipper Network).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {compliance_38, clipper_network}
- **Path params:** `payee_id`
- **Request body:** none
- **Response:** 200 JSON — from `tax_status` (services/finance-py/src/svc_payees.py:196): object with keys `payee_id`, `available`, `form_kind`, `form_on_file`, `tin_match`, `w8_current`, `services_outside_us_attested`, `backup_withholding`, `as_of`, `reason`, `rules_pinned`.
- **Errors raised on this code path:** none found statically

#### `GET /fin/v1/payees/{payee_id}/rail-status`

- **Source:** `services/finance-py/src/api.py:392`
- **Purpose:** Payee payout-rail status (for Compliance).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {compliance_38}
- **Path params:** `payee_id`
- **Request body:** none
- **Response:** 200 JSON — from `rail_status` (services/finance-py/src/svc_payees.py:209): object with keys `payee_id`, `available`, `status`, `payouts_enabled`, `rail`, `reason`, `rules_pinned`.
- **Errors raised on this code path:** none found statically

#### `GET /fin/v1/payees/{payee_id}/payout-identity`

- **Source:** `services/finance-py/src/api.py:396`
- **Purpose:** Payee payout identity HMAC (for Verification & Integrity).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {verification_integrity}
- **Path params:** `payee_id`
- **Request body:** none
- **Response:** 200 JSON — from `payout_identity` (services/finance-py/src/svc_payees.py:220): object with keys `payee_id`, `available`, `identity_hmac`, `rules_pinned`.
- **Errors raised on this code path:** none found statically

#### `GET /fin/v1/payees/{payee_id}/open-items`

- **Source:** `services/finance-py/src/api.py:400`
- **Purpose:** Whether the payee still has open financial items (for Clipper Network offboarding).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {clipper_network}
- **Path params:** `payee_id`
- **Request body:** none
- **Response:** 200 JSON — from `open_items` (services/finance-py/src/svc_payees.py:233): object with keys `payee_id`, `state`, `counts`, `rules_pinned`.
- **Errors raised on this code path:** none found statically

#### `POST /fin/v1/payees/{payee_id}/offboarding-notices`

- **Source:** `services/finance-py/src/api.py:404`
- **Purpose:** Clipper Network notifies Finance that a payee is being offboarded.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {clipper_network}
- **Path params:** `payee_id`
- **Request body:** JSON `OffboardingNotice` (services/finance-py/src/models.py:324); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `offboarding_id: Id`
- **Response:** 200 JSON — from `offboarding_notice` (services/finance-py/src/svc_payees.py:252): object with keys `ok`, `reference`, `request_id`, `facts_sha256`, `payee_id`; object with keys `ok`, `reference`, `request_id`, `facts_sha256`, `payee_id`, `reason`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/payees/{payee_id}/callbacks`

- **Source:** `services/finance-py/src/api.py:409`
- **Purpose:** Andre records a payee callback (verification call-back) record.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `payee_id`
- **Request body:** JSON `CallbackRecord` (services/finance-py/src/models.py:329); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `change_event_id: Id`
    - `contact_ref: Annotated[str, Field(pattern=r"^vault:[A-Za-z0-9._:-]{1,120}$")]`
    - `outcome: Literal["confirmed", "denied"]`
    - `notes: Optional[Note] = None`
    - `creator_notified_ref: Optional[Id] = None`
- **Response:** 200 JSON — from `record_callback` (services/finance-py/src/svc_payees.py:281): object with keys `callback`, `hold`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 422 `InvalidReasons`, 503 `Unavailable`

#### `POST /fin/v1/payees/{payee_id}/tax/b-notices`

- **Source:** `services/finance-py/src/api.py:414`
- **Purpose:** Andre records an IRS B-notice for a payee.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `payee_id`
- **Request body:** JSON `BNotice` (services/finance-py/src/models.py:338); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `cp2100_received_on: date`
    - `second_notice_within_3y: bool = False`
    - `first_b_notice_sent_on: Optional[date] = None`
    - `start_withholding: bool = False`
- **Response:** 200 JSON — from `b_notice` (services/finance-py/src/svc_payees.py:350): object with keys `payee_id`, `tax`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /fin/v1/rate-cards/{doc_id}/versions/{version}`

- **Source:** `services/finance-py/src/api.py:420`
- **Purpose:** Read rate-card version metadata.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Path params:** `doc_id`, `version`
- **Request body:** none
- **Response:** 200 JSON — from `rate_card_meta` (services/finance-py/src/svc_books.py:163): object with keys `doc_id`, `version`, `published`, `sha256`, `campaign_id`, `effective_at`, `rules_pinned`.
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /fin/v1/rate-cards/{doc_id}/versions/{version}/document`

- **Source:** `services/finance-py/src/api.py:424`
- **Purpose:** Andre reads the rate-card document itself.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `doc_id`, `version`
- **Request body:** none
- **Response:** 200 JSON — from `rate_card_document` (services/finance-py/src/svc_books.py:172): shape not statically determinable (returns `dict(v)`) — UNKNOWN beyond the cited method.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /fin/v1/rate-cards/proposals`

- **Source:** `services/finance-py/src/api.py:428`
- **Purpose:** Andre proposes a rate-card version.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `RateCardProposal` (services/finance-py/src/models.py:241); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `doc_id: Optional[Id] = None`
    - `campaign_id: Id`
    - `creator_rate_per_1000: TierRates`
    - `max_paid_views_per_clip: int = Field(ge=1, le=10**12)`
    - `effective_at: Timestamp`
- **Response:** 201 JSON — from `propose_rate_card` (services/finance-py/src/svc_books.py:73): object with keys `proposal`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 422 `InvalidReasons`, 503 `Unavailable`

#### `POST /fin/v1/rate-cards/decisions`

- **Source:** `services/finance-py/src/api.py:433`
- **Purpose:** Andre approves/rejects rate-card proposals.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `RuleDecisions` (services/finance-py/src/models.py:227); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `decisions: list[RuleDecision] = Field(min_length=1, max_length=200)`
- **Response:** 200 JSON — from `decide_rate_cards` (services/finance-py/src/svc_books.py:109): object with keys `decided`, `published`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `PUT /fin/v1/campaigns/{campaign_id}/commercial-profile`

- **Source:** `services/finance-py/src/api.py:438`
- **Purpose:** Andre sets a campaign's commercial profile.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `campaign_id`
- **Request body:** JSON `CommercialProfile` (services/finance-py/src/models.py:265); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `client_id: Id`
    - `order_form: DocRef`
    - `budget: PositiveMoney`
    - `client_rate_per_1000: PositiveMoney`
    - `rate_card_doc_id: Id`
    - `account_title: Optional[Short] = None`
- **Response:** 200 JSON — from `put_profile` (services/finance-py/src/svc_books.py:181): object with keys `profile`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 422 `InvalidReasons`, 503 `Unavailable`

#### `GET /fin/v1/campaigns/{campaign_id}/budget`

- **Source:** `services/finance-py/src/api.py:443`
- **Purpose:** Campaign budget view (for Clipper Network / Creative Production).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {clipper_network, creative_production}
- **Path params:** `campaign_id`
- **Request body:** none
- **Response:** 200 JSON — from `budget_view` (services/finance-py/src/svc_books.py:285): object with keys `campaign_id`, `budget_state`, `remaining_views_estimate`, `rules_pinned`.
- **Errors raised on this code path:** 404 `NotFound`

#### `PUT /fin/v1/clients/{client_id}/billing-profile`

- **Source:** `services/finance-py/src/api.py:447`
- **Purpose:** Andre sets a client's billing profile.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `client_id`
- **Request body:** JSON `BillingProfile` (services/finance-py/src/models.py:275); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `entity: Literal["zbc", "zbm"]`
    - `payment_method: Literal["ach", "wire"]`
    - `msa: DocRef`
- **Response:** 200 JSON — from `put_billing_profile` (services/finance-py/src/svc_books.py:216): object with keys `billing_profile`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /fin/v1/clients/{client_id}/billing-readiness`

- **Source:** `services/finance-py/src/api.py:452`
- **Purpose:** Whether a client is ready to be billed (for Onboarding).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {onboarding}
- **Path params:** `client_id`
- **Request body:** none
- **Response:** 200 JSON — from `billing_readiness` (services/finance-py/src/svc_books.py:261): object with keys `client_id`, `allowed`, `unmet`, `reasons`, `detail`, `rules_pinned`, `ledger_event_ids`.
- **Errors raised on this code path:** none found statically

#### `POST /fin/v1/invoices`

- **Source:** `services/finance-py/src/api.py:458`
- **Purpose:** Draft an invoice (Onboarding or scheduler).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {onboarding, scheduler}
- **Request body:** JSON `InvoiceDraft` (services/finance-py/src/models.py:365); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `entity: Literal["zbc", "zbm"]`
    - `client_id: Id`
    - `campaign_id: Optional[Id] = None`
    - `kind: Literal["campaign_deposit", "service", "retainer", "subscription"]`
    - `lines: list[InvoiceLine] = Field(min_length=1, max_length=50)`
    - `payment_methods: list[Literal["ach", "wire", "card"]] = Field(min_length=1, max_length=3)`
    - `due_days: int = Field(default=15, ge=0, le=120)`
    - `legal_ref: DocRef`
    - `recurring: Optional[Recurring] = None`
    - `notes: Optional[Note] = None`
    - `template_vars: Optional[dict[Annotated[str, Field(max_length=40)], Short]] = Field(default=None, max_length=20)`
- **Response:** 201 JSON — from `draft_invoice` (services/finance-py/src/svc_books.py:306): object with keys `invoice`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 422 `InvalidReasons`, 503 `Unavailable`

#### `GET /fin/v1/invoices/{invoice_id}`

- **Source:** `services/finance-py/src/api.py:463`
- **Purpose:** Read one invoice.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Path params:** `invoice_id`
- **Request body:** none
- **Response:** 200 JSON — from `get_invoice` (services/finance-py/src/svc_books.py:410): shape not statically determinable (returns `dict(inv)`) — UNKNOWN beyond the cited method.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /fin/v1/invoices/{invoice_id}/decision`

- **Source:** `services/finance-py/src/api.py:467`
- **Purpose:** Andre approves/rejects a drafted invoice.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `invoice_id`
- **Request body:** JSON `Decision` (services/finance-py/src/models.py:250); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `content_sha256: Sha`
    - `decision: Literal["approve", "reject"]`
    - `note: Optional[Note] = None`
    - `acknowledge_weakening: Optional[bool] = None`
- **Response:** 200 JSON — from `decide_invoice` (services/finance-py/src/svc_books.py:355): object with keys `invoice`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 422 `InvalidReasons`, 503 `Unavailable`

#### `POST /fin/v1/bank/events`

- **Source:** `services/finance-py/src/api.py:472`
- **Purpose:** Bank feed posts bank statement lines.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {bank_feed}
- **Request body:** JSON `BankEvents` (services/finance-py/src/models.py:390); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `lines: list[StatementLine] = Field(min_length=1, max_length=200)`
- **Response:** 200 JSON — from `bank_events` (services/finance-py/src/svc_books.py:419): object with keys `results`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 422 `InvalidReasons`, 503 `Unavailable`

#### `POST /fin/v1/receipts/{receipt_id}/apply`

- **Source:** `services/finance-py/src/api.py:476`
- **Purpose:** Andre applies an unapplied receipt to an issued campaign deposit invoice.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `receipt_id`
- **Request body:** JSON `ApplyReceipt` (services/finance-py/src/models.py:460); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `invoice_id: Id`
- **Response:** 200 JSON — from `apply_receipt` (services/finance-py/src/svc_books.py:486): object with keys `receipt_id`, `entry_id`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 422 `InvalidReasons`, 503 `Unavailable`

#### `POST /fin/v1/receipts/{receipt_id}/return`

- **Source:** `services/finance-py/src/api.py:481`
- **Purpose:** Record that the bank returned a matched client deposit (ACH return).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre) OR `X-FIN-Caller-Token` = caller ∈ {bank_feed}
- **Path params:** `receipt_id`
- **Request body:** JSON `DepositReturn` (services/finance-py/src/models.py:474); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `return_ref_sha256: Sha`
    - `return_code: Annotated[str, Field(pattern=r"^R[0-9]{2}$")]`
    - `value_date: date`
- **Response:** 200 JSON — from `deposit_return` (services/finance-py/src/svc_books.py:531): object with keys `receipt_id`, `entry_id`, `to_2010`, `shortfall`, `shortfall_id`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 422 `InvalidReasons`, 503 `Unavailable`

#### `POST /fin/v1/rails/{rail}/events`

- **Source:** `services/finance-py/src/api.py:486`
- **Purpose:** Payout-rail gateway posts rail events for rail `{rail}`.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {rail_gateway}
- **Path params:** `rail`
- **Request body:** JSON `RailEvents` (services/finance-py/src/models.py:404); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `events: list[RailEvent] = Field(min_length=1, max_length=100)`
- **Response:** 200 JSON — from `rail_events` (services/finance-py/src/svc_payees.py:425): object with keys `results`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 422 `InvalidReasons`, 503 `Unavailable`

#### `POST /fin/v1/disputes`

- **Source:** `services/finance-py/src/api.py:491`
- **Purpose:** Open a payment dispute (Andre or rail gateway).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre) OR `X-FIN-Caller-Token` = caller ∈ {rail_gateway}
- **Request body:** JSON `DisputeOpen` (services/finance-py/src/models.py:409); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `kind: Literal["invoice_dispute", "card_chargeback"]`
    - `invoice_id: Id`
    - `amount: PositiveMoney`
    - `evidence_refs: list[Id] = Field(default_factory=list, max_length=50)`
    - `notes: Optional[Note] = None`
- **Response:** 201 JSON — from `open_dispute` (services/finance-py/src/svc_books.py:622): object with keys `dispute`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/disputes/{dispute_id}/outcome`

- **Source:** `services/finance-py/src/api.py:496`
- **Purpose:** Andre records a dispute's outcome.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `dispute_id`
- **Request body:** JSON `DisputeOutcome` (services/finance-py/src/models.py:418); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `outcome: Literal["won", "lost", "withdrawn"]`
    - `notes: Optional[Note] = None`
- **Response:** 200 JSON — from `dispute_outcome` (services/finance-py/src/svc_books.py:658): object with keys `dispute`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/refunds/{campaign_id}`

- **Source:** `services/finance-py/src/api.py:501`
- **Purpose:** Scheduler proposes a refund for a campaign.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {scheduler}
- **Path params:** `campaign_id`
- **Request body:** JSON `RunRequest` (services/finance-py/src/models.py:206); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
- **Response:** 201 JSON — from `propose_refund` (services/finance-py/src/svc_books.py:687): object with keys `refund`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/refunds/{refund_id}/decision`

- **Source:** `services/finance-py/src/api.py:506`
- **Purpose:** Andre approves/rejects a refund.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `refund_id`
- **Request body:** JSON `Decision` (services/finance-py/src/models.py:250); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `content_sha256: Sha`
    - `decision: Literal["approve", "reject"]`
    - `note: Optional[Note] = None`
    - `acknowledge_weakening: Optional[bool] = None`
- **Response:** 200 JSON — from `decide_refund` (services/finance-py/src/svc_books.py:724): object with keys `refund`, `payment`, `ledger_event_ids`, `request_id`; object with keys `refund`, `ledger_event_ids`, `request_id`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/jobs/{job}/run`

- **Source:** `services/finance-py/src/api.py:513`
- **Purpose:** Scheduler runs a named Finance job.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {scheduler}
- **Path params:** `job`
- **Request body:** JSON `RunRequest` (services/finance-py/src/models.py:206); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
- **Response:** 200 JSON — from `run_job` (services/finance-py/src/svc_recon.py:632): object with keys `job`, `day`, `summary`, `already_ran`, `request_id`.
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/payout-runs`

- **Source:** `services/finance-py/src/api.py:517`
- **Purpose:** Scheduler starts a payout run (builds a payout batch) on a rail.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {scheduler}
- **Request body:** JSON `PayoutRun` (services/finance-py/src/models.py:426); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `rail: Literal["stripe", "trolley"] = "stripe"`
- **Response:** 201 JSON — from `run_payouts` (services/finance-py/src/svc_payouts.py:367): object with keys `batch`, `run_empty`, `exceptions_opened`, `ledger_event_ids`, `request_id`; object with keys `batch`, `run_empty`, `blocked`, `reasons`, `reason_lines`, `excluded`, `exceptions_opened`, `ledger_event_ids`, `request_id`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /fin/v1/payout-batches/{batch_id}`

- **Source:** `services/finance-py/src/api.py:521`
- **Purpose:** Read one payout batch.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Path params:** `batch_id`
- **Request body:** none
- **Response:** 200 JSON — from `get_batch` (services/finance-py/src/svc_payouts.py:514): object with keys `**b`, `items`; via _batch_view (services/finance-py/src/svc_payouts.py:506).
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /fin/v1/payout-batches`

- **Source:** `services/finance-py/src/api.py:525`
- **Purpose:** List payout batches, optionally filtered by status.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Query params:** `status_: Optional[str] = Query(default=None, alias="status", max_length=20)`
- **Request body:** none
- **Response:** 200 JSON — from `list_batches` (services/finance-py/src/svc_payouts.py:518): object with keys `batches`.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /fin/v1/payout-batches/{batch_id}/decision`

- **Source:** `services/finance-py/src/api.py:530`
- **Purpose:** Andre approves/rejects a payout batch.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate); plus a request that ALSO carries `X-FIN-Second-Approver-Token` is refused 403 (api.py:533)
- **Path params:** `batch_id`
- **Request body:** JSON `Decision` (services/finance-py/src/models.py:250); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `content_sha256: Sha`
    - `decision: Literal["approve", "reject"]`
    - `note: Optional[Note] = None`
    - `acknowledge_weakening: Optional[bool] = None`
- **Response:** 200 JSON — from `decide_batch` (services/finance-py/src/svc_payouts.py:553): object with keys `batch_id`, `status`, `approval`, `release_not_before`, `approval_expires_at`, `ledger_event_ids`, `request_id`; object with keys `batch_id`, `status`, `ledger_event_ids`, `request_id`; object with keys `batch_id`, `status`, `andre_approval`, `ledger_event_ids`, `request_id`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay).
- **Errors raised on this code path:** 403 `Forbidden`, 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/payout-batches/{batch_id}/second-approval`

- **Source:** `services/finance-py/src/api.py:540`
- **Purpose:** Second approver's approval of a payout batch (own request, own token).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus ONLY `X-FIN-Second-Approver-Token` (FIN_SECOND_APPROVER_TOKEN); any `X-Andre-Approval-Token` or `X-FIN-Caller-Token` on the request → 403 (api.py:542-549)
- **Path params:** `batch_id`
- **Request body:** JSON `Decision` (services/finance-py/src/models.py:250); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `content_sha256: Sha`
    - `decision: Literal["approve", "reject"]`
    - `note: Optional[Note] = None`
    - `acknowledge_weakening: Optional[bool] = None`
- **Response:** 200 JSON — from `second_approve_batch` (services/finance-py/src/svc_payouts.py:607): object with keys `batch_id`, `status`, `by`, `ledger_event_ids`, `request_id`; object with keys `batch_id`, `status`, `approval`, `release_not_before`, `approval_expires_at`, `ledger_event_ids`, `request_id`; object with keys `batch_id`, `status`, `second_approval`, `ledger_event_ids`, `request_id`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay).
- **Errors raised on this code path:** 403 `Forbidden`, 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/payout-batches/{batch_id}/release`

- **Source:** `services/finance-py/src/api.py:552`
- **Purpose:** Scheduler releases an approved payout batch (approver may not release).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {scheduler}; plus any `X-Andre-Approval-Token` or `X-FIN-Second-Approver-Token` on the request → 403 (separation of duties, api.py:555)
- **Path params:** `batch_id`
- **Request body:** JSON `RunRequest` (services/finance-py/src/models.py:206); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
- **Response:** 200 JSON — from `release_batch` (services/finance-py/src/svc_payouts.py:665): object with keys `batch`, `results`, `request_id`; via _release (services/finance-py/src/svc_payouts.py:675).
- **Errors raised on this code path:** 403 `Forbidden`, 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /fin/v1/payables/{payable_id}`

- **Source:** `services/finance-py/src/api.py:560`
- **Purpose:** Read one payable.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Path params:** `payable_id`
- **Request body:** none
- **Response:** 200 JSON — from `get_payable` (services/finance-py/src/svc_payables.py:254): shape not statically determinable (returns `dict(p)`) — UNKNOWN beyond the cited method.
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /fin/v1/exceptions`

- **Source:** `services/finance-py/src/api.py:564`
- **Purpose:** List Finance exceptions.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `list_exceptions` (services/finance-py/src/svc_payouts.py:974): object with keys `exceptions`.
- **Errors raised on this code path:** none found statically

#### `POST /fin/v1/exceptions/{exception_id}/decision`

- **Source:** `services/finance-py/src/api.py:568`
- **Purpose:** Andre decides a Finance exception.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `exception_id`
- **Request body:** JSON `ExceptionDecision` (services/finance-py/src/models.py:431); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `decision: Literal["approve", "reject"]`
    - `note: Optional[Note] = None`
- **Response:** 200 JSON — from `decide_exception` (services/finance-py/src/svc_payouts.py:978): object with keys `exception`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/reconciliations/run`

- **Source:** `services/finance-py/src/api.py:575`
- **Purpose:** Scheduler runs bank/rail reconciliation.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {scheduler}
- **Request body:** JSON `RunRequest` (services/finance-py/src/models.py:206); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
- **Response:** 200 JSON — from `run_recon` (services/finance-py/src/svc_recon.py:86): object with keys `recon`, `fc01`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /fin/v1/breaks`

- **Source:** `services/finance-py/src/api.py:579`
- **Purpose:** List reconciliation breaks.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `list_breaks` (services/finance-py/src/svc_recon.py:197): object with keys `breaks`.
- **Errors raised on this code path:** none found statically

#### `POST /fin/v1/breaks/{break_id}/resolution`

- **Source:** `services/finance-py/src/api.py:583`
- **Purpose:** Andre resolves a reconciliation break.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `break_id`
- **Request body:** JSON `BreakResolution` (services/finance-py/src/models.py:442); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `explanation_code: Literal["timing_in_transit", "fee_unbooked", "misapplied_receipt", "rail_return", "unknown"]`
    - `entry_id: Optional[Id] = None`
    - `clears_by: Optional[date] = None`
    - `evidence: list[Evidence] = Field(default_factory=list, max_length=20)`
- **Response:** 200 JSON — from `resolve_break` (services/finance-py/src/svc_recon.py:205): object with keys `break`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /fin/v1/treasury`

- **Source:** `services/finance-py/src/api.py:588`
- **Purpose:** Treasury view.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `treasury_view` (services/finance-py/src/svc_recon.py:344): object with keys `journal`, `sweepable`, `independent`, `account_title`, `custody_model`, `open_operations`, `shortfalls`.
- **Errors raised on this code path:** none found statically

#### `POST /fin/v1/treasury/sweeps`

- **Source:** `services/finance-py/src/api.py:592`
- **Purpose:** Scheduler proposes a treasury sweep.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {scheduler}
- **Request body:** JSON `SweepProposal` (services/finance-py/src/models.py:450); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `amount: PositiveMoney`
- **Response:** 201 JSON — from `propose_sweep` (services/finance-py/src/svc_recon.py:378): object with keys `operation`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/treasury/sweeps/{op_id}/decision`

- **Source:** `services/finance-py/src/api.py:596`
- **Purpose:** Andre approves/rejects a treasury sweep.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `op_id`
- **Request body:** JSON `Decision` (services/finance-py/src/models.py:250); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `content_sha256: Sha`
    - `decision: Literal["approve", "reject"]`
    - `note: Optional[Note] = None`
    - `acknowledge_weakening: Optional[bool] = None`
- **Response:** 200 JSON — from `decide_treasury` (services/finance-py/src/svc_recon.py:455): object with keys `operation`, `transfer`, `request_id`; object with keys `operation`, `ledger_event_ids`, `request_id`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/treasury/funding`

- **Source:** `services/finance-py/src/api.py:600`
- **Purpose:** Scheduler proposes funding for a payout batch.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {scheduler}
- **Request body:** JSON `FundingProposal` (services/finance-py/src/models.py:455); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `batch_id: Id`
- **Response:** 201 JSON — from `propose_funding` (services/finance-py/src/svc_recon.py:400): object with keys `operation`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/treasury/funding/{op_id}/decision`

- **Source:** `services/finance-py/src/api.py:604`
- **Purpose:** Andre approves/rejects a funding proposal.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `op_id`
- **Request body:** JSON `Decision` (services/finance-py/src/models.py:250); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `content_sha256: Sha`
    - `decision: Literal["approve", "reject"]`
    - `note: Optional[Note] = None`
    - `acknowledge_weakening: Optional[bool] = None`
- **Response:** 200 JSON — from `decide_treasury` (services/finance-py/src/svc_recon.py:455): object with keys `operation`, `transfer`, `request_id`; object with keys `operation`, `ledger_event_ids`, `request_id`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/treasury/top-ups`

- **Source:** `services/finance-py/src/api.py:609`
- **Purpose:** Andre records a treasury top-up.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `TopUp` (services/finance-py/src/models.py:465); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `amount: PositiveMoney`
    - `reason_code: Literal["clawback_after_release", "over_budget", "dispute_loss", "deposit_return_shortfall", "other"] = "other"`
    - `notes: Optional[Note] = None`
    - `shortfall_id: Optional[Id] = None`
- **Response:** 200 JSON — from `top_up` (services/finance-py/src/svc_recon.py:426): object with keys `operation`, `transfer`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/clawbacks/{payee_id}/write-off`

- **Source:** `services/finance-py/src/api.py:613`
- **Purpose:** Andre writes off a payee's clawback balance.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `payee_id`
- **Request body:** JSON `RunRequest` (services/finance-py/src/models.py:206); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
- **Response:** 200 JSON — from `write_off` (services/finance-py/src/svc_payables.py:370): object with keys `payee_id`, `written_off`, `entry_id`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/close/{entity}/{period}/tasks/{task}`

- **Source:** `services/finance-py/src/api.py:618`
- **Purpose:** Scheduler completes a month-end close task (`period` = YYYY-MM).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = caller ∈ {scheduler}
- **Path params:** `entity`, `period`, `task`
- **Request body:** JSON `RunRequest` (services/finance-py/src/models.py:206); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
- **Response:** 200 JSON — from `close_task` (services/finance-py/src/svc_books.py:818): object with keys `task`, `status`, `reasons`, `reason_lines`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/close/{entity}/{period}/approve`

- **Source:** `services/finance-py/src/api.py:625`
- **Purpose:** Andre approves a month-end close (`period` = YYYY-MM).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `entity`, `period`
- **Request body:** JSON `RunRequest` (services/finance-py/src/models.py:206); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
- **Response:** 200 JSON — from `close_approve` (services/finance-py/src/svc_books.py:861): object with keys `entity`, `period`, `locked`, `statements`, `draft_reasons`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/journal/{entity}/corrections`

- **Source:** `services/finance-py/src/api.py:632`
- **Purpose:** Andre posts a journal correction.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `entity`
- **Request body:** JSON `Correction` (services/finance-py/src/models.py:490); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `effective_date: date`
    - `reverses_entry_id: Optional[Id] = None`
    - `lines: Optional[list[CorrectionLine]] = Field(default=None, max_length=50)`
    - `notes: Optional[Note] = None`
- **Response:** 201 JSON — from `correction` (services/finance-py/src/svc_books.py:780): object with keys `entry`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 422 `InvalidReasons`, 503 `Unavailable`

#### `GET /fin/v1/journal/{entity}/entries`

- **Source:** `services/finance-py/src/api.py:637`
- **Purpose:** Page through an entity's journal entries.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Path params:** `entity`
- **Query params:** `cursor: int = Query(default=0, ge=0, le=10**9)`
- **Request body:** none
- **Response:** 200 JSON — from `journal_entries` (services/finance-py/src/svc_books.py:889): object with keys `entity`, `entries`, `next_cursor`.
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /fin/v1/journal/{entity}/trial-balance`

- **Source:** `services/finance-py/src/api.py:641`
- **Purpose:** Trial balance for an entity (optionally as of a date).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Path params:** `entity`
- **Query params:** `as_of: Optional[str] = Query(default=None, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")`
- **Request body:** none
- **Response:** 200 JSON — from `trial_balance` (services/finance-py/src/svc_books.py:898): object with keys `**J.trial_balance(b, entity)`, `as_of`.
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /fin/v1/controls`

- **Source:** `services/finance-py/src/api.py:648`
- **Purpose:** List Finance controls.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `controls_view` (services/finance-py/src/svc_recon.py:271): object with keys `controls`, `as_of`.
- **Errors raised on this code path:** none found statically

#### `POST /fin/v1/controls/{control_id}/results`

- **Source:** `services/finance-py/src/api.py:652`
- **Purpose:** Record a control test result (Andre or Compliance).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre) OR `X-FIN-Caller-Token` = caller ∈ {compliance_38}
- **Path params:** `control_id`
- **Request body:** JSON `ControlResult` (services/finance-py/src/models.py:498); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `result: Literal["pass", "fail"]`
    - `evidence_ref: Id`
- **Response:** 200 JSON — from `record_control_result` (services/finance-py/src/svc_recon.py:319): object with keys `control`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 403 `Forbidden`, 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /fin/v1/rules`

- **Source:** `services/finance-py/src/api.py:657`
- **Purpose:** Current Finance rules register.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `rules_view` (services/finance-py/src/service.py:710): object with keys `rules_version`, `rules_sha256`, `rules_pinned`, `seed_sha256`, `rules`, `versions`, `open_proposals`.
- **Errors raised on this code path:** 503 `Unavailable`

#### `POST /fin/v1/rules/proposals`

- **Source:** `services/finance-py/src/api.py:661`
- **Purpose:** Andre proposes a rule change.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `RuleProposalRequest` (services/finance-py/src/models.py:212); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `kind: Literal["add", "amend", "retire"]`
    - `target_id: Optional[Annotated[str, Field(max_length=16)]] = None`
    - `proposed_row: Optional[dict] = None`
- **Response:** 201 JSON — from `create_rule_proposal` (services/finance-py/src/service.py:721): object with keys `proposal`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/rules/decisions`

- **Source:** `services/finance-py/src/api.py:666`
- **Purpose:** Andre approves/rejects rule proposals.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `RuleDecisions` (services/finance-py/src/models.py:227); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `decisions: list[RuleDecision] = Field(min_length=1, max_length=200)`
- **Response:** 200 JSON — from `decide_rules` (services/finance-py/src/service.py:740): object with keys `decided`, `approved`, `rules_version`, `rules_sha256`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /fin/v1/tax/readiness`

- **Source:** `services/finance-py/src/api.py:670`
- **Purpose:** Andre records tax-filing readiness facts.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `TaxReadiness` (services/finance-py/src/models.py:504); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `tcc_obtained_at: Optional[date] = None`
    - `iris_test_passed_at: Optional[date] = None`
    - `ftb_swift_ready_at: Optional[date] = None`
- **Response:** 200 JSON — from `tax_readiness` (services/finance-py/src/svc_payees.py:377): object with keys `readiness`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /fin/v1/tax/1099/{tax_year}`

- **Source:** `services/finance-py/src/api.py:674`
- **Purpose:** Andre reads 1099 data for a tax year (2026-2100).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `tax_year`
- **Request body:** none
- **Response:** 200 JSON — from `form_1099` (services/finance-py/src/svc_payees.py:401): object with keys `tax_year`, `threshold`, `records`, `reasons`, `reason_lines`, `ledger_event_ids`.
- **Errors raised on this code path:** 422 `Invalid`

#### `GET /fin/v1/audit/export`

- **Source:** `services/finance-py/src/api.py:680`
- **Purpose:** Audit export of the local record log (paged; optional since/until).
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-FIN-Caller-Token` = any recognised caller
- **Query params:** `since: Optional[str] = Query(default=None, max_length=40)`; `until: Optional[str] = Query(default=None, max_length=40)`; `cursor: int = Query(default=0, ge=0, le=10**12)`
- **Request body:** none
- **Response:** 200 JSON — from `audit_export` (services/finance-py/src/service.py:859): object with keys `records`, `next_cursor`, `ledger_event_id`, `rules_version`.
- **Errors raised on this code path:** 422 `Invalid`, 503 `Unavailable`

#### `GET /fin/v1/reconcile`

- **Source:** `services/finance-py/src/api.py:689`
- **Purpose:** Andre previews what a ledger reconcile would void.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** none
- **Response:** 200 JSON — from `reconcile_plan` (services/finance-py/src/service.py:623): object with keys `epoch`, `head_seq`, `head_sha256`, `rules_version`, `fatal`, `problems`, `voidable`, `reconcile_mode`.
- **Errors raised on this code path:** none found statically

#### `POST /fin/v1/reconcile`

- **Source:** `services/finance-py/src/api.py:693`
- **Purpose:** Andre voids stray lines/events to reconcile the local log with the ledger.
- **Auth:** `Authorization: Bearer <FIN_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `ReconcileRequest` (services/finance-py/src/models.py:511); `model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)`
    - `request_id: Id`
    - `head_sha256: Sha`
    - `void_lines: list[int] = Field(default_factory=list, max_length=10000)`
    - `void_event_ids: list[Annotated[str, Field(max_length=128)]] = Field(default_factory=list, max_length=10000)`
- **Response:** 200 JSON — from `reconcile` (services/finance-py/src/service.py:632): object with keys `reconcile_event_id`, `voided`, `void_lines`, `remaining_problems`, `restart_required`, `ledger_event_ids`, `request_id`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

---

## 12. legal-py (Legal 37)

* **Language / entry:** Python FastAPI, `services/legal-py/src/api.py`. Started with `cd services/legal-py/src && python3 -m api` (`main()` `api.py:689-694`), which uses the hardened launcher `serve.py`.
* **Bind:** `LEGAL_BIND_ADDR` (default `127.0.0.1`), `LEGAL_PORT` (default `8420`).
* **Required env:** `LEGAL_SERVICE_TOKEN` (refuses to start without it, `src/config.py:113-116`).
* **Auth:** Bearer service token on every route except `/health`. Caller identity: `X-LEGAL-Caller-Token` → `LEGAL_CALLER_TOKENS` (names: compliance_38, clipper_network, verification_integrity, creative_production, onboarding, finance_31, hub, esign_gateway, scheduler; `src/config.py:21-22`). Andre: `X-Andre-Approval-Token` → `LEGAL_ANDRE_APPROVAL_TOKEN`. Many read routes accept either any recognised caller **or** Andre's token (`reader`, `api.py:301`).
* **Other env & rules:** `LEGAL_DATA_DIR`, `LEGAL_SEED_DIR`, `LEGAL_RECONCILE_MODE`, `LEGAL_COMPLIANCE_URL` + `LEGAL_COMPLIANCE_TOKEN` + `LEGAL_COMPLIANCE_CALLER_TOKEN` (Compliance thin client), `LEDGER_SERVICE_URL` + `LEDGER_SERVICE_TOKEN`. Write answers echo `request_id`. Id formats (`api.py:245-250`): `doc_id` `^[a-z][a-z0-9_]{1,60}$`, `version` `^[0-9]{1,4}\.[0-9]{1,4}$`, Legal ids `^lg-[a-z]{3}-[0-9A-Z]{26}$`, client/subject ids `[A-Za-z0-9._:-]{1,128}`, register ids `(CQ|VI-CQ|CN-CQ|FIN-CQ)-NN | retention:<x> | signoff:<x>` → 422. Every write body is refused **422** if it carries an IP/user-agent/device/DOB/government-id/payment key (`forbidden_field`) or an IP address in any string (`IP_ADDRESS_REFUSED`) (`api.py:330-340`). Legal returns no free text.
* **Request limits (`InputLimits`, outermost middleware, `services/legal-py/src/api.py`):**
  * Request target (path + query) over 4,096 bytes → **414**; head over 16 KiB → **431**; `Content-Length` not a digit → **400**.
  * Body over the route cap → **413**, checked from `Content-Length` and again on the bytes received. Route caps: blob routes (`documents/*/versions`, `…/counsel-signoff`, `memos`, `esign/events`, `playbooks/*/reviews`, `playbooks/proposals`) 7 MiB, `reconcile` 1 MiB, everything else 256 KiB (`api.py:67-77`); service cap 7 MiB.
  * A body whose `Content-Type` is not `application/json` or `application/*+json` → **415**.
  * JSON nested deeper than 32 or with more than 20,000 members → **422**.
  * Body not received within 30 s → **408**.
  * The hardened launcher also caps the head at 16 KiB in the parser, sets a 10 s head deadline and a 5 s keep-alive, and limits concurrency to 128 (beyond it uvicorn answers **503**).
  * Validation errors → **422** `{"detail":[{loc,msg,type}… ≤20], "errors_total": N}` (input never echoed). An unhandled exception → **500** `{"detail":"internal error"}`.
* **Errors on every route:** 401 (bearer); 403 `Forbidden` / `FounderRefused` (caller or Andre identity); 409 `Conflict` (request_id reuse); 503 `Unavailable` with `Retry-After: 1` (ledger write failed, reconcile mode, port down); 500. Domain errors answer `{"detail": <reason>, …extra body}`.
* **State:** see the summary table (JSONL log if the data dir is set, otherwise in-memory).


**59 endpoints.**

#### `GET /health`

- **Source:** `services/legal-py/src/api.py:347`
- **Purpose:** Liveness/status of the service.
- **Auth:** none (open)
- **Request body:** none
- **Response:** 200 JSON — from `health` (services/legal-py/src/service.py:2678): object with keys `status`, `service`, `rules_version`, `in_memory`, `rules_pinned`, `counsel_channel_wired`, `reconcile_mode`, `reconcile_required`.
- **Errors raised on this code path:** none found statically

#### `GET /legal/v1/intelligences`

- **Source:** `services/legal-py/src/api.py:351`
- **Purpose:** List the department's intelligence modules.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Request body:** none
- **Response:** 200 — JSON array from `registry()` (services/legal-py/src/intelligences/__init__.py:11): one object per intelligence with keys `number`, `name`, `actor`, `module`.
- **Errors raised on this code path:** none found statically

#### `GET /legal/v1/integrity`

- **Source:** `services/legal-py/src/api.py:355`
- **Purpose:** Integrity check of the local log vs the evidence ledger.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Request body:** none
- **Response:** 200 JSON — from `integrity` (services/legal-py/src/service.py:488): object with keys `status`, `problems`, `log_lines`, `rules_version`; object with keys `status`, `problems`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** none found statically

#### `GET /legal/v1/documents/{doc_id}/current`

- **Source:** `services/legal-py/src/api.py:361`
- **Purpose:** Current version answer for a document (protocol answer for Compliance / Clipper Network).
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `doc_id`
- **Request body:** none
- **Response:** 200 JSON — from `current_answer` (services/legal-py/src/service.py:860): object with keys `**base`, `available`, `current_version`, `doc_sha256`, `effective_at`, `review_by`, `reason`; object with keys `**base`, `reason`; object with keys `**base`, `available`, `reason`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** none found statically

#### `GET /legal/v1/documents/{doc_id}`

- **Source:** `services/legal-py/src/api.py:365`
- **Purpose:** Document view (versions and status).
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `doc_id`
- **Request body:** none
- **Response:** 200 JSON — from `document_view` (services/legal-py/src/service.py:838): object with keys `**{k: d[k] for k in ("doc_id", "title", "d`, `current_version`, `versions`, `rules_pinned`.
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /legal/v1/documents/{doc_id}/versions/{version}`

- **Source:** `services/legal-py/src/api.py:369`
- **Purpose:** Read one document version's record.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `doc_id`, `version`
- **Request body:** none
- **Response:** 200 JSON — from `version_get` (services/legal-py/src/service.py:847): via version_view (services/legal-py/src/service.py:831).
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /legal/v1/documents/{doc_id}/versions/{version}/text`

- **Source:** `services/legal-py/src/api.py:373`
- **Purpose:** Andre reads a document version's text.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `doc_id`, `version`
- **Request body:** none
- **Response:** 200 JSON — from `version_text` (services/legal-py/src/service.py:852): object with keys `doc_id`, `version`, `sha256`, `text`.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /legal/v1/documents/{doc_id}/versions`

- **Source:** `services/legal-py/src/api.py:377`
- **Purpose:** Create a new document version (scheduler or Andre).
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {scheduler} — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `doc_id`
- **Request body:** JSON `DocVersionCreate` (services/legal-py/src/models.py:88); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `entity: Entity`
    - `text: Optional[DocText] = None`
    - `template_variables: Optional[dict] = None`
    - `clause_ids: list[ClauseUse] = Field(default_factory=list, max_length=200)`
    - `variables: Optional[dict] = None`
    - `party_ref: Optional[PartyRef] = None`
    - `bump: Literal["major", "minor"] = "minor"`
    - `supersedes: Optional[Version] = None`
- **Response:** 201 JSON — from `create_version` (services/legal-py/src/service.py:877): object with keys `doc_id`, `version`, `sha256`, `status`, `review_label`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 403 `Forbidden`, 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/documents/{doc_id}/versions/{version}/counsel-review`

- **Source:** `services/legal-py/src/api.py:382`
- **Purpose:** Andre records counsel review of a version.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `doc_id`, `version`
- **Request body:** JSON `CounselReview` (services/legal-py/src/models.py:103); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `question_text: Text`
    - `detected_change_sha256: Optional[Sha256] = None`
    - `proposed_edit_text: Optional[ClauseText] = None`
    - `facts: dict = Field(default_factory=dict)`
- **Response:** 200 JSON — from `counsel_review` (services/legal-py/src/service.py:979): object with keys `package_id`, `delivered`, `status`, `reasons`, `reason_lines`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/documents/{doc_id}/versions/{version}/counsel-signoff`

- **Source:** `services/legal-py/src/api.py:388`
- **Purpose:** Andre records counsel sign-off of a version.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `doc_id`, `version`
- **Request body:** JSON `CounselSignoff` (services/legal-py/src/models.py:111); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `counsel_ref: Ref`
    - `signed_on: IsoDate`
    - `doc_sha256: Sha256`
    - `memo_id: Optional[Id] = None`
    - `memo_sha256: Optional[Sha256] = None`
    - `countersignature_b64: Optional[B64] = None`
- **Response:** 200 JSON — from `counsel_signoff` (services/legal-py/src/service.py:1013): object with keys `doc_id`, `version`, `counsel_signoff`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/documents/{doc_id}/versions/{version}/decision`

- **Source:** `services/legal-py/src/api.py:394`
- **Purpose:** Andre decides (approves/rejects) a document version.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `doc_id`, `version`
- **Request body:** JSON `DocDecision` (services/legal-py/src/models.py:121); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `decision: Literal["approve", "retire", "withdraw"]`
    - `version_sha256: Sha256`
    - `effective_at: Optional[Timestamp] = None`
- **Response:** 200 JSON — from `decide_version` (services/legal-py/src/service.py:1058): object with keys `doc_id`, `version`, `**fields`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/acceptances`

- **Source:** `services/legal-py/src/api.py:402`
- **Purpose:** Record a party's acceptance of a document version (hub, Clipper Network, Onboarding).
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {hub, clipper_network, onboarding}
- **Request body:** JSON `AcceptanceCreate` (services/legal-py/src/models.py:142); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `party_ref: PartyRef`
    - `signer_identity_ref: Ref`
    - `doc_id: DocId`
    - `version: Version`
    - `doc_sha256: Sha256`
    - `presented_sha256: Sha256`
    - `method: Literal["clickwrap_unticked_box"]`
    - `presentation: Literal["scroll_to_accept", "link", "inline"]`
    - `affirmative_act: bool`
    - `esign_consent: Optional[EsignConsent] = None`
    - `evidence_ref: Optional[EvidenceRef] = None`
- **Response:** 201 JSON — from `record_acceptance` (services/legal-py/src/service.py:1122): object with keys `**self.acceptance_view(rec)`, `obligation_ids`, `reasons`, `reason_lines`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/acceptances/{acceptance_id}`

- **Source:** `services/legal-py/src/api.py:407`
- **Purpose:** Read one acceptance record.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {clipper_network, finance_31, onboarding} — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `acceptance_id`
- **Request body:** none
- **Response:** 200 JSON — from `get_acceptance` (services/legal-py/src/service.py:1115): via acceptance_view (services/legal-py/src/service.py:1111).
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /legal/v1/envelopes`

- **Source:** `services/legal-py/src/api.py:412`
- **Purpose:** Andre creates an e-sign envelope.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `EnvelopeCreate` (services/legal-py/src/models.py:157); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `doc_id: DocId`
    - `version: Version`
    - `party_ref: PartyRef`
    - `signer_refs: list[Ref] = Field(min_length=1, max_length=10)`
- **Response:** 200 JSON — from `create_envelope` (services/legal-py/src/service.py:1241): object with keys `created`, `envelope_id`; object with keys `created`, `envelope_id`, `reasons`, `reason_lines`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/esign/events`

- **Source:** `services/legal-py/src/api.py:416`
- **Purpose:** E-sign gateway posts an e-sign event.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {esign_gateway}
- **Request body:** JSON `ESignEvent` (services/legal-py/src/models.py:165); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `envelope_id: Ref`
    - `status: Literal["completed", "declined", "voided"]`
    - `signed_document_sha256: Optional[Sha256] = None`
    - `certificate_b64: Optional[B64] = None`
- **Response:** 200 JSON — from `esign_event` (services/legal-py/src/service.py:1278): object with keys `**self.acceptance_view(rec)`, `obligation_ids`, `reasons`, `reason_lines`; object with keys `envelope_id`, `status`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/playbooks/proposals`

- **Source:** `services/legal-py/src/api.py:422`
- **Purpose:** Andre proposes a playbook.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `PlaybookProposal` (services/legal-py/src/models.py:195); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `playbook: PlaybookBody`
    - `counsel_memo_id: Optional[Id] = None`
- **Response:** 201 JSON — from `create_playbook_proposal` (services/legal-py/src/service.py:1333): object with keys `proposal`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/playbooks/decisions`

- **Source:** `services/legal-py/src/api.py:427`
- **Purpose:** Andre decides a playbook proposal.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `PlaybookDecision` (services/legal-py/src/models.py:201); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `proposal_id: Id`
    - `content_sha256: Sha256`
    - `decision: Literal["approve", "reject"]`
    - `acknowledge_weakening: bool = False`
- **Response:** 200 JSON — from `decide_playbook` (services/legal-py/src/service.py:1386): object with keys `doc_type`, `version`, `status`; object with keys `proposal_id`, `status`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/playbooks/{doc_type}`

- **Source:** `services/legal-py/src/api.py:432`
- **Purpose:** Playbook view for a document type.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `doc_type`
- **Request body:** none
- **Response:** 200 JSON — from `playbook_view` (services/legal-py/src/service.py:1429): object with keys `doc_type`, `playbook`, `rules_pinned`.
- **Errors raised on this code path:** none found statically

#### `POST /legal/v1/playbooks/{doc_type}/reviews`

- **Source:** `services/legal-py/src/api.py:436`
- **Purpose:** Review a document against the playbook for a document type.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {onboarding, scheduler} — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `doc_type`
- **Request body:** JSON `PlaybookReview` (services/legal-py/src/models.py:180); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `our_template_version: Optional[Version] = None`
    - `counterparty_positions: list[Position] = Field(default_factory=list, max_length=200)`
    - `facts: dict[Code, Tri] = Field(default_factory=dict, max_length=100)`
    - `counterparty_paper_text: Optional[DocText] = None`
- **Response:** 200 JSON — from `review` (services/legal-py/src/service.py:1434): object with keys `review_id`, `**rec["result"]`, `unreviewed`, `review_label`, `reasons`, `reason_lines`, `injection_text_ignored`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/obligations`

- **Source:** `services/legal-py/src/api.py:443`
- **Purpose:** List contract obligations (filters: party_ref, owner_department, status).
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Query params:** `party_ref: Optional[str] = Query(default=None, max_length=120)`; `owner_department: Optional[str] = Query(default=None, pattern=r"^[a-z0-9_]{1,40}$")`; `status_: Optional[str] = Query(default=None, alias="status", pattern=r"^(open|due_soon|done|missed|waived)$")`
- **Request body:** none
- **Response:** 200 JSON — from `list_obligations` (services/legal-py/src/service.py:1481): object with keys `items`, `rules_pinned`.
- **Errors raised on this code path:** none found statically

#### `POST /legal/v1/obligations`

- **Source:** `services/legal-py/src/api.py:450`
- **Purpose:** Andre enters a counterparty obligation from a counsel memo.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `ObligationEntry` (services/legal-py/src/models.py:226); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `memo_id: Id`
    - `doc_id: DocId`
    - `version: Version`
    - `party: Literal["zbc", "zbm", "counterparty"]`
    - `counterparty_ref: PartyRef`
    - `obligation_code: Code`
    - `due: Optional[IsoDate] = None`
    - `alert_lead_days: int = Field(ge=0, le=365)`
    - `owner_department: Literal["onboarding", "finance_31", "creative_production", "clipper_network", "compliance_38", "legal_37", "andre"]`
- **Response:** 201 JSON — from `obligation_entry` (services/legal-py/src/service.py:1526): object with keys `obligation_id`, `status`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/obligations/{obligation_id}/done`

- **Source:** `services/legal-py/src/api.py:455`
- **Purpose:** Mark an obligation done.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {onboarding, finance_31, creative_production, clipper_network, compliance_38} — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `obligation_id`
- **Request body:** JSON `ObligationDone` (services/legal-py/src/models.py:216); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `evidence: DoneEvidence`
- **Response:** 200 JSON — from `obligation_done` (services/legal-py/src/service.py:1489): object with keys `obligation_id`, `status`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/obligations/{obligation_id}/waive`

- **Source:** `services/legal-py/src/api.py:463`
- **Purpose:** Andre waives an obligation.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `obligation_id`
- **Request body:** JSON `ObligationWaive` (services/legal-py/src/models.py:221); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `memo_id: Optional[Id] = None`
- **Response:** 200 JSON — from `obligation_waive` (services/legal-py/src/service.py:1509): object with keys `obligation_id`, `status`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/contracts/{client_id}/terms`

- **Source:** `services/legal-py/src/api.py:468`
- **Purpose:** Onboarding reads stored contract terms for a client (404 if none).
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {onboarding}
- **Path params:** `client_id`
- **Request body:** none
- **Response:** 200 JSON — from `contract_get` (services/legal-py/src/service.py:1558): returns `Optional[dict]`.
- **Errors raised on this code path:** 404 `NotFound`

#### `PUT /legal/v1/contracts/{client_id}/terms`

- **Source:** `services/legal-py/src/api.py:475`
- **Purpose:** Onboarding stores contract terms for a client.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {onboarding}
- **Path params:** `client_id`
- **Request body:** JSON `ContractTermsPut` (services/legal-py/src/models.py:260); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `terms: ContractTermsModel`
    - `executed: Executed`
- **Response:** 200 JSON — from `contract_put` (services/legal-py/src/service.py:1578): object with keys `client_id`, `stored`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/register`

- **Source:** `services/legal-py/src/api.py:482`
- **Purpose:** Counsel-question register view.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Request body:** none
- **Response:** 200 JSON — from `register_view` (services/legal-py/src/service.py:757): object with keys `rows`, `rules_pinned`.
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /legal/v1/register/{cq_id}`

- **Source:** `services/legal-py/src/api.py:486`
- **Purpose:** Read one counsel-question register row.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `cq_id`
- **Request body:** none
- **Response:** 200 JSON — from `register_row_view` (services/legal-py/src/service.py:746): object with keys `**{k: row[k] for k in ("cq_id", "origin", `, `status`, `memo_ids`, `review_by`, `check`, `compliance`, `rules_pinned`.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /legal/v1/register/{cq_id}/invalidate`

- **Source:** `services/legal-py/src/api.py:491`
- **Purpose:** Compliance marks a register row / retention class / sign-off topic unverified.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {compliance_38}
- **Path params:** `cq_id`
- **Request body:** JSON `Invalidate` (services/legal-py/src/models.py:268); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `source_ref: Ref`
    - `detected_change_sha256: Sha256`
- **Response:** 200 JSON — from `invalidate` (services/legal-py/src/service.py:761): object with keys `target`, `canonical`, `status`, `was`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/memos`

- **Source:** `services/legal-py/src/api.py:497`
- **Purpose:** Andre files a counsel memo.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `MemoIntake` (services/legal-py/src/models.py:305); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `counsel_ref: Ref`
    - `memo_date: IsoDate`
    - `content_b64: B64`
    - `cites: Cites`
    - `answers: list[Answer] = Field(default_factory=list, max_length=100)`
    - `retention_periods: dict[Code, Annotated[str, StringConstraints(pattern=r"^P[0-9]{1,3}[YMD]$")]] = Field(default_factory=dict, max_length=20)`
    - `signoff_scopes: dict[Code, dict] = Field(default_factory=dict, max_length=20)`
- **Response:** 201 JSON — from `file_memo` (services/legal-py/src/service.py:1638): object with keys `**resp_core`, `proposals`, `delivery_attempts`, `ledger_event_ids`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/memos/{memo_id}/compliance-proposals`

- **Source:** `services/legal-py/src/api.py:501`
- **Purpose:** Andre creates Compliance register proposals backed by a filed memo.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `memo_id`
- **Request body:** JSON `MemoProposals` (services/legal-py/src/models.py:300); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `proposals: list[MemoProposal] = Field(min_length=1, max_length=100)`
- **Response:** 201 JSON — from `memo_proposals` (services/legal-py/src/service.py:1751): object with keys `**resp_core`, `proposals`, `delivery_attempts`, `ledger_event_ids`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/memos/{memo_id}`

- **Source:** `services/legal-py/src/api.py:506`
- **Purpose:** Read one memo record.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `memo_id`
- **Request body:** none
- **Response:** 200 JSON — from `memo_view` (services/legal-py/src/service.py:1618): object with keys `**{k: m[k] for k in ("memo_id", "counsel_r`, `answers`, `proposals`, `rules_pinned`.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /legal/v1/requests`

- **Source:** `services/legal-py/src/api.py:512`
- **Purpose:** Open a legal matter (intake request).
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Request body:** JSON `MatterIntake` (services/legal-py/src/models.py:334); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `channel: Literal["email", "portal", "mail", "phone", "hub", "department"]`
    - `requester_ref: Ref`
    - `kind: Literal["agency_letter", "subpoena", "litigation_threat", "demand_letter", "ip_claim", "contract_dispute", "privacy_request", "data_incident", "routine_contract", "question"]`
    - `facts: MatterFacts = Field(default_factory=MatterFacts)`
    - `disputed_amount_usd: Optional[Money] = None`
    - `subject_refs: list[Ref] = Field(default_factory=list, max_length=200)`
    - `custodians: list[Ref] = Field(default_factory=list, max_length=200)`
    - `systems: list[Literal["email", "chat", "drive", "legal_store", "finance_log", "vi_evidence", "cn_records", "creative_store"]] = Field(default_factory=list, max_length=8)`
    - `deadlines: Deadlines = Field(default_factory=Deadlines)`
    - `retention_class: Literal["matters"] = "matters"`
- **Response:** 201 JSON — from `matter_intake` (services/legal-py/src/service.py:2020): object with keys `matter_id`, `severity`, `likelihood`, `route`, `hold_ids`, `deadlines`, `routing_notice`, `status`, `unreviewed`, `review_label`, `reasons`, `reason_lines`; via _matter_answer (services/legal-py/src/service.py:2036), stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/matters/{matter_id}`

- **Source:** `services/legal-py/src/api.py:517`
- **Purpose:** Read one matter.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `matter_id`
- **Request body:** none
- **Response:** 200 JSON — from `matter_view` (services/legal-py/src/service.py:2042): shape not statically determinable (returns `{k: v for k, v in m.items() if k != "intake"} | {"kind": m["intake"]["kind"], "received_at": m["intake"]["received_at"]}`) — UNKNOWN beyond the cited method.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /legal/v1/matters/{matter_id}/close`

- **Source:** `services/legal-py/src/api.py:521`
- **Purpose:** Andre closes a matter.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `matter_id`
- **Request body:** JSON `MatterClose` (services/legal-py/src/models.py:350); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `memo_id: Optional[Id] = None`
- **Response:** 200 JSON — from `close_matter` (services/legal-py/src/service.py:2050): object with keys `matter_id`, `status`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/holds/check`

- **Source:** `services/legal-py/src/api.py:526`
- **Purpose:** Check whether a subject is under legal hold.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Query params:** `subject_ref: str = Query(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/-]{1,200}$")`
- **Request body:** none
- **Response:** 200 JSON — from `hold_check` (services/legal-py/src/service.py:2071): object with keys `subject_ref`, `held`, `hold_ids`, `rules_pinned`.
- **Errors raised on this code path:** none found statically

#### `POST /legal/v1/holds/{hold_id}/acknowledgments`

- **Source:** `services/legal-py/src/api.py:531`
- **Purpose:** Hub records acknowledgment of a legal hold.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {hub}
- **Path params:** `hold_id`
- **Request body:** JSON `HoldAck` (services/legal-py/src/models.py:355); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `custodian: Ref`
- **Response:** 200 JSON — from `hold_ack` (services/legal-py/src/service.py:2082): object with keys `hold_id`, `acknowledged`, `custodians`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/holds/{hold_id}/release`

- **Source:** `services/legal-py/src/api.py:535`
- **Purpose:** Andre releases a legal hold.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `hold_id`
- **Request body:** JSON `HoldRelease` (services/legal-py/src/models.py:360); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `memo_id: Optional[Id] = None`
- **Response:** 200 JSON — from `hold_release` (services/legal-py/src/service.py:2101): object with keys `hold_id`, `status`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/takedowns`

- **Source:** `services/legal-py/src/api.py:544`
- **Purpose:** Record an inbound takedown notice.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {hub, clipper_network} — OR `X-Andre-Approval-Token` (Andre)
- **Request body:** JSON `TakedownIn` (services/legal-py/src/models.py:380); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `target: TakedownTarget`
    - `elements: Elements`
    - `arguable_elements: list[Literal["signature", "work_identified", "material_located", "contact", "good_faith_statement", "perjury_statement"]] = Field(default_factory=list, max_length=6)`
    - `uploader_ref: Optional[Ref] = None`
- **Response:** 201 JSON — from `takedown_in` (services/legal-py/src/service.py:2127): object with keys `notice_id`, `valid`, `status`, `matter_id`, `hold_ids`, `forwarded_to`, `repeat_count_for_uploader`, `reasons`, `reason_lines`; object with keys `notice_id`, `valid`, `status`, `reasons`, `reason_lines`; (several return statements; each listed key set is one possible answer); via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/takedowns/count`

- **Source:** `services/legal-py/src/api.py:548`
- **Purpose:** Count takedowns for a post (by SHA-256 of its ref) (V&I).
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {verification_integrity}
- **Query params:** `post_ref_sha256: str = Query(pattern=r"^[0-9a-f]{64}$")`
- **Request body:** none
- **Response:** 200 JSON — from `takedown_count` (services/legal-py/src/service.py:2288): object with keys `post_ref_sha256`, `available`, `notices`, `rules_pinned`; object with keys `post_ref_sha256`, `available`, `notices`, `rules_pinned`, `reason`; (several return statements; each listed key set is one possible answer).
- **Errors raised on this code path:** none found statically

#### `POST /legal/v1/takedowns/outbound`

- **Source:** `services/legal-py/src/api.py:553`
- **Purpose:** Andre sends an outbound takedown notice.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `OutboundNotice` (services/legal-py/src/models.py:401); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `target: TakedownTarget`
    - `variables: dict = Field(default_factory=dict)`
    - `license_or_fair_use_possible: bool`
    - `counsel_memo_id: Optional[Id] = None`
- **Response:** 201 JSON — from `outbound_notice` (services/legal-py/src/service.py:2303): object with keys `outbound_id`, `status`, `rendered_sha256`, `template`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/takedowns/{notice_id}`

- **Source:** `services/legal-py/src/api.py:558`
- **Purpose:** Read one takedown notice.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `notice_id`
- **Request body:** none
- **Response:** 200 JSON — from `takedown_view` (services/legal-py/src/service.py:2296): shape not statically determinable (returns `dict(n)`) — UNKNOWN beyond the cited method.
- **Errors raised on this code path:** 404 `NotFound`

#### `POST /legal/v1/takedowns/{notice_id}/counter-notice`

- **Source:** `services/legal-py/src/api.py:562`
- **Purpose:** Record a counter-notice on a takedown.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {hub, clipper_network} — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `notice_id`
- **Request body:** JSON `CounterNotice` (services/legal-py/src/models.py:390); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `checklist: dict[Code, bool] = Field(default_factory=dict, max_length=20)`
- **Response:** 200 JSON — from `counter_notice` (services/legal-py/src/service.py:2186): object with keys `notice_id`, `status`, `**cn`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/takedowns/{notice_id}/claimant-action`

- **Source:** `services/legal-py/src/api.py:566`
- **Purpose:** Record a claimant action on a takedown.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {hub, clipper_network} — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `notice_id`
- **Request body:** JSON `ClaimantAction` (services/legal-py/src/models.py:395); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `filed: Literal[True]`
    - `court_ref_sha256: Optional[Sha256] = None`
- **Response:** 200 JSON — from `claimant_action` (services/legal-py/src/service.py:2217): object with keys `notice_id`, `status`, `matter_id`, `hold_ids`, `reasons`, `reason_lines`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/takedowns/{notice_id}/restore`

- **Source:** `services/legal-py/src/api.py:571`
- **Purpose:** Restore content after a takedown.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {hub, clipper_network} — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `notice_id`
- **Request body:** JSON `RunRequest` (services/legal-py/src/models.py:77); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
- **Response:** 200 JSON — from `restore` (services/legal-py/src/service.py:2242): object with keys `notice_id`, `status`, `reasons`, `reason_lines`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/takedowns/{notice_id}/withdraw`

- **Source:** `services/legal-py/src/api.py:575`
- **Purpose:** Withdraw a takedown notice.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {hub, clipper_network} — OR `X-Andre-Approval-Token` (Andre)
- **Path params:** `notice_id`
- **Request body:** JSON `RunRequest` (services/legal-py/src/models.py:77); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
- **Response:** 200 JSON — from `withdraw_notice` (services/legal-py/src/service.py:2272): object with keys `notice_id`, `status`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/filings`

- **Source:** `services/legal-py/src/api.py:581`
- **Purpose:** List regulatory filings.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Request body:** none
- **Response:** 200 JSON — from `list_filings` (services/legal-py/src/service.py:2416): object with keys `items`, `rules_pinned`.
- **Errors raised on this code path:** none found statically

#### `POST /legal/v1/filings`

- **Source:** `services/legal-py/src/api.py:585`
- **Purpose:** Andre creates a filing record.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `FilingCreate` (services/legal-py/src/models.py:411); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `entity: Entity`
    - `kind: Literal["dmca_agent_designation", "tm_application", "tm_statement_of_use", "tm_section_8", "tm_section_9", "tm_section_15", "sos_statement_of_information", "fbn_statement", "insurance_policy_notice"]`
    - `reference: Optional[Ref] = None`
    - `filed_on: Optional[IsoDate] = None`
    - `registration_date: Optional[IsoDate] = None`
    - `formation_month: Optional[int] = Field(default=None, ge=1, le=12)`
    - `due_year: Optional[int] = Field(default=None, ge=2020, le=2100)`
    - `window_opens: Optional[IsoDate] = None`
    - `window_closes: Optional[IsoDate] = None`
    - `expires_on: Optional[IsoDate] = None`
    - `owner: Literal["andre", "legal_37"] = "andre"`
- **Response:** 201 JSON — from `create_filing` (services/legal-py/src/service.py:2349): via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/filings/{filing_id}/ready`

- **Source:** `services/legal-py/src/api.py:589`
- **Purpose:** Andre marks a filing ready.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `filing_id`
- **Request body:** JSON `RunRequest` (services/legal-py/src/models.py:77); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
- **Response:** 200 JSON — from `filing_ready` (services/legal-py/src/service.py:2373): object with keys `filing_id`, `status`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/filings/{filing_id}/filed`

- **Source:** `services/legal-py/src/api.py:594`
- **Purpose:** Andre marks a filing filed.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Path params:** `filing_id`
- **Request body:** JSON `FilingFiled` (services/legal-py/src/models.py:427); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `filed_on: IsoDate`
    - `reference: Ref`
- **Response:** 200 JSON — from `filing_filed` (services/legal-py/src/service.py:2392): object with keys `filing_id`, `**fields`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/signoffs`

- **Source:** `services/legal-py/src/api.py:599`
- **Purpose:** Creative Production requests a legal sign-off ruling.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {creative_production}
- **Request body:** JSON `SignoffRequest` (services/legal-py/src/models.py:433); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `topic: Code`
    - `subject_id: Id`
    - `facts: dict = Field(default_factory=dict)`
- **Response:** 200 JSON — from `signoff` (services/legal-py/src/service.py:2424): object with keys `department`, `allowed`, `reason`, `reference`, `request_id`, `facts_sha256`, `rules_pinned`, `review_label`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/music/rulings`

- **Source:** `services/legal-py/src/api.py:604`
- **Purpose:** Music-use ruling (Creative Production / Compliance).
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {creative_production, compliance_38}
- **Request body:** JSON `MusicRuling` (services/legal-py/src/models.py:446); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `subject_kind: Literal["zbc_clip", "zbm_work"]`
    - `subject_id: Id`
    - `platform: Literal["tiktok", "youtube", "instagram", "x", "facebook", "snapchat", "other"]`
    - `paid: bool`
    - `music: Music`
    - `reposted_or_reedited_by_zbc: bool`
    - `music_changed_since_approval: bool`
- **Response:** 200 JSON — from `music_ruling` (services/legal-py/src/service.py:2467): object with keys `allowed`, `ruling_id`, `reasons`, `reason_lines`, `request_id`, `facts_sha256`, `rules_pinned`, `review_label`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/retention`

- **Source:** `services/legal-py/src/api.py:609`
- **Purpose:** Retention schedule view.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Request body:** none
- **Response:** 200 JSON — from `retention_view` (services/legal-py/src/service.py:2500): object with keys `classes`, `rules_pinned`.
- **Errors raised on this code path:** none found statically

#### `POST /legal/v1/jobs/{job}/run`

- **Source:** `services/legal-py/src/api.py:615`
- **Purpose:** Scheduler runs a named Legal job (unknown job -> 422).
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = caller ∈ {scheduler}
- **Path params:** `job`
- **Request body:** JSON `RunRequest` (services/legal-py/src/models.py:77); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
- **Response:** 200 JSON — from `run_job` (services/legal-py/src/service.py:2516): object with keys `job`, `day`, `already_ran`, `summary`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 409 `Refused`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/rules`

- **Source:** `services/legal-py/src/api.py:621`
- **Purpose:** Current Legal rules register.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Request body:** none
- **Response:** 200 JSON — from `rules_view` (services/legal-py/src/service.py:611): object with keys `rules_version`, `rules_sha256`, `rules_pinned`, `seed_sha256`, `rules`, `versions`, `open_proposals`.
- **Errors raised on this code path:** 503 `Unavailable`

#### `POST /legal/v1/rules/proposals`

- **Source:** `services/legal-py/src/api.py:625`
- **Purpose:** Andre proposes a rule change.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `RuleProposalRequest` (services/legal-py/src/models.py:459); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `kind: Literal["add", "amend", "retire"]`
    - `target_id: Optional[Annotated[str, StringConstraints(pattern=r"^LG-[0-9]{2}[a-z]?$")]] = None`
    - `proposed_row: Optional[dict] = None`
- **Response:** 201 JSON — from `create_rule_proposal` (services/legal-py/src/service.py:622): object with keys `proposal`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /legal/v1/rules/decisions`

- **Source:** `services/legal-py/src/api.py:630`
- **Purpose:** Andre approves/rejects rule proposals.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `RuleDecisions` (services/legal-py/src/models.py:474); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `decisions: list[RuleDecision] = Field(min_length=1, max_length=50)`
- **Response:** 200 JSON — from `decide_rules` (services/legal-py/src/service.py:642): object with keys `decided`, `approved`, `rules_version`, `rules_sha256`, `ledger_event_ids`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/reconcile`

- **Source:** `services/legal-py/src/api.py:635`
- **Purpose:** Andre previews what a ledger reconcile would void.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** none
- **Response:** 200 JSON — from `reconcile_plan` (services/legal-py/src/service.py:516): object with keys `epoch`, `head_seq`, `head_sha256`, `rules_version`, `fatal`, `problems`, `voidable`, `reconcile_mode`.
- **Errors raised on this code path:** none found statically

#### `POST /legal/v1/reconcile`

- **Source:** `services/legal-py/src/api.py:639`
- **Purpose:** Andre voids stray lines/events to reconcile with the ledger.
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `ReconcileRequest` (services/legal-py/src/models.py:479); `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`
    - `request_id: Id`
    - `head_sha256: Sha256`
    - `void_lines: list[Annotated[int, Field(ge=1, le=10**12)]] = Field(max_length=10_000)`
    - `void_event_ids: list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]] = Field(max_length=10_000)`
- **Response:** 200 JSON — from `reconcile` (services/legal-py/src/service.py:525): object with keys `reconcile_event_id`, `voided`, `void_lines`, `remaining_problems`, `restart_required`, `ledger_event_ids`; via stored idempotent answer (replay). Answer also carries `request_id` (added by `_echo`).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /legal/v1/audit/export`

- **Source:** `services/legal-py/src/api.py:644`
- **Purpose:** Audit export of the local record log (paged; optional since/until).
- **Auth:** `Authorization: Bearer <LEGAL_SERVICE_TOKEN>`; plus `X-LEGAL-Caller-Token` = any recognised caller — OR `X-Andre-Approval-Token` (Andre)
- **Query params:** `since: Optional[str] = Query(default=None, max_length=40)`; `until: Optional[str] = Query(default=None, max_length=40)`; `cursor: int = Query(default=0, ge=0, le=10**12)`
- **Request body:** none
- **Response:** 200 JSON — from `audit_export` (services/legal-py/src/service.py:2657): object with keys `records`, `next_cursor`, `ledger_event_id`, `rules_version`.
- **Errors raised on this code path:** 422 `Invalid`, 503 `Unavailable`

---

## 13. delivery-py (Client Delivery & Operations 28 — AEGIS fix engine)

* **Language / entry:** Python FastAPI, `services/delivery-py/src/zbm_delivery/api.py`. Started with `cd services/delivery-py/src && python3 -m zbm_delivery.api` (`main()` `api.py:470-476`; README: `../.venv/bin/python -m zbm_delivery.api`), which uses the hardened launcher `serve.py`.
* **Bind:** `DLV_BIND_ADDR` (default `127.0.0.1`), `DLV_PORT` (default `8430`).
* **Required env:** `DLV_SERVICE_TOKEN` (refuses to start without it, `src/zbm_delivery/config.py:246`); also a start-up gate (`gate.run`) that needs `DLV_SANDBOX_IMAGE`, `DLV_IMAGE_REGISTRY` and other settings (`config.py:227-232`) — exact full list UNKNOWN beyond config.py.
* **Auth:** Bearer service token on every route except `/health`. Caller identity: `X-DLV-Caller-Token` → `DLV_CALLER_TOKENS` (names: aegis, andre_session, scheduler; `config.py:25`). Andre: `X-Andre-Approval-Token` → `DLV_ANDRE_APPROVAL_TOKEN` (reconcile routes only).
* **Other env & rules:** `DLV_DATA_DIR`, `DLV_SEED_DIR`, `DLV_PROMPTS_DIR`, `DLV_REPO_PATH`, `DLV_WORKTREES_DIR`, `DLV_RECONCILE_MODE`, `DLV_LLM_MODEL`, `DLV_LLM_API_KEY_REF`, `DLV_MAX_FINDINGS`, `LEDGER_SERVICE_URL` + `LEDGER_SERVICE_TOKEN`. Every POST answer echoes `request_id` and `facts_sha256`; every answer carries `policy_version` and `prompts_manifest_sha256` (module docstring, `api.py:14-15`). Run ids `^dlv-run-[0-9A-HJKMNP-TV-Z]{26}$` (`api.py:67`) → 422. `Refused` → 503.
* **Request limits (`InputLimits`, outermost middleware, `services/delivery-py/src/zbm_delivery/api.py`):**
  * Request target (path + query) over 4,096 bytes → **414**; head over 16 KiB → **431**; `Content-Length` not a digit → **400**.
  * Body over the route cap → **413**, checked from `Content-Length` and again on the bytes received. Route caps: `fix-runs` 1 MiB, `fix-runs/*/review` 1 MiB, `fix-runs/*/cancel` 8 KiB, `reconcile` 1 MiB, everything else 16 KiB (`api.py:60-66`); service cap 1 MiB.
  * A body whose `Content-Type` is not `application/json` or `application/*+json` → **415**.
  * JSON nested deeper than 32 or with more than 20,000 members → **422**.
  * Body not received within 30 s → **408**.
  * The hardened launcher also caps the head at 16 KiB in the parser, sets a 10 s head deadline and a 5 s keep-alive, and limits concurrency to 128 (beyond it uvicorn answers **503**).
  * Validation errors → **422** `{"detail":[{loc,msg,type}… ≤20], "errors_total": N}` (input never echoed). An unhandled exception → **500** `{"detail":"internal error"}`.
* **Errors on every route:** 401 (bearer); 403 `Forbidden` / `FounderRefused` (caller or Andre identity); 409 `Conflict` (request_id reuse); 503 `Unavailable` with `Retry-After: 1` (ledger write failed, reconcile mode, port down); 500. Domain errors answer `{"detail": <reason>, …extra body}`.
* **State:** see the summary table (JSONL log if the data dir is set, otherwise in-memory).


**12 endpoints.**

#### `GET /health`

- **Source:** `services/delivery-py/src/zbm_delivery/api.py:317`
- **Purpose:** Liveness/status of the service.
- **Auth:** none (open)
- **Request body:** none
- **Response:** 200 JSON — from `health` (services/delivery-py/src/zbm_delivery/service.py:594): object with keys `status`, `service`, `in_memory`, `ledger`, `sandbox`, `llm`, `non_production`, `config_sha256`, `prompts_manifest_sha256`, `policy_version`, `deerflow_commit`, `reconcile_mode`, `reconcile_required`, `runs_live`.
- **Errors raised on this code path:** none found statically

#### `POST /dlv/v1/fix-runs`

- **Source:** `services/delivery-py/src/zbm_delivery/api.py:321`
- **Purpose:** Submit an AEGIS findings document to open a fix run (accepted asynchronously, 202).
- **Auth:** `Authorization: Bearer <DLV_SERVICE_TOKEN>`; plus `X-DLV-Caller-Token` = caller ∈ {aegis, andre_session}
- **Request body:** JSON `FindingsDocument` (services/delivery-py/src/zbm_delivery/models.py:118); `model_config = ConfigDict(extra="forbid", str_max_length=FREE_TEXT_MAX)`
    - `request_id: str = Field(pattern=f"^{ID_RE.pattern}$")`
    - `source: Source`
    - `base_ref: str = Field(pattern=REF_RE.pattern)`
    - `base_sha: str = Field(pattern=COMMIT_RE.pattern)`
    - `service: str = Field(pattern=SERVICE_RE.pattern)`
    - `findings: list[Finding] = Field(min_length=1, max_length=200)`
- **Response:** 202 JSON — from `create_fix_run` (services/delivery-py/src/zbm_delivery/service.py:882): via _red_finish (services/delivery-py/src/zbm_delivery/service.py:830), stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Refused`, 503 `Unavailable`

#### `GET /dlv/v1/fix-runs/{run_id}`

- **Source:** `services/delivery-py/src/zbm_delivery/api.py:328`
- **Purpose:** Read a fix run.
- **Auth:** `Authorization: Bearer <DLV_SERVICE_TOKEN>`; plus `X-DLV-Caller-Token` = any recognised caller
- **Path params:** `run_id`
- **Request body:** none
- **Response:** 200 JSON — from `run_view` (services/delivery-py/src/zbm_delivery/service.py:1011): shape not statically determinable (returns `json.loads(json.dumps(run))`) — UNKNOWN beyond the cited method.
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /dlv/v1/fix-runs/{run_id}/findings`

- **Source:** `services/delivery-py/src/zbm_delivery/api.py:332`
- **Purpose:** Read a fix run's findings.
- **Auth:** `Authorization: Bearer <DLV_SERVICE_TOKEN>`; plus `X-DLV-Caller-Token` = any recognised caller
- **Path params:** `run_id`
- **Request body:** none
- **Response:** 200 JSON — from `findings_view` (services/delivery-py/src/zbm_delivery/service.py:1018): object with keys `run_id`, `findings`.
- **Errors raised on this code path:** 404 `NotFound`

#### `GET /dlv/v1/fix-runs/{run_id}/report`

- **Source:** `services/delivery-py/src/zbm_delivery/api.py:336`
- **Purpose:** Read a fix run's report (Markdown text).
- **Auth:** `Authorization: Bearer <DLV_SERVICE_TOKEN>`; plus `X-DLV-Caller-Token` = any recognised caller
- **Path params:** `run_id`
- **Request body:** none
- **Response:** 200 — `text/markdown; charset=utf-8` body (not JSON) from `report_text`; headers `X-DLV-Policy-Version`, `X-DLV-Prompts-Manifest-SHA256`; > 1 MiB → 422 "fetch it as evidence" (api.py:336-343).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /dlv/v1/fix-runs/{run_id}/evidence/{evidence_id}`

- **Source:** `services/delivery-py/src/zbm_delivery/api.py:345`
- **Purpose:** Read one evidence item of a fix run (raw bytes, max 1 MiB).
- **Auth:** `Authorization: Bearer <DLV_SERVICE_TOKEN>`; plus `X-DLV-Caller-Token` = any recognised caller
- **Path params:** `run_id`, `evidence_id`
- **Request body:** none
- **Response:** 200 — raw bytes (truncated at 1 MiB); Content-Type by evidence kind: brief/report `text/markdown`, diff `text/x-diff`, review `application/json`, else `text/plain`; headers `X-DLV-Evidence-Kind`, `X-DLV-Evidence-SHA256`, `X-DLV-Policy-Version`, `X-DLV-Prompts-Manifest-SHA256` (api.py:345-356). `evidence_id` must match `^dlv-ev-[0-9a-f]{26}$`.
- **Errors raised on this code path:** 404 `NotFound`, 422 `Invalid`

#### `POST /dlv/v1/fix-runs/{run_id}/review`

- **Source:** `services/delivery-py/src/zbm_delivery/api.py:358`
- **Purpose:** AEGIS posts a pass/fail review of a fix run (a fail can open a new run).
- **Auth:** `Authorization: Bearer <DLV_SERVICE_TOKEN>`; plus `X-DLV-Caller-Token` = caller ∈ {aegis}
- **Path params:** `run_id`
- **Request body:** JSON `ReviewRequest` (services/delivery-py/src/zbm_delivery/models.py:184); `model_config = ConfigDict(extra="forbid", str_max_length=FREE_TEXT_MAX)`
    - `request_id: str = Field(pattern=f"^{ID_RE.pattern}$")`
    - `review_ref: str = Field(min_length=1, max_length=128)`
    - `sha256: str = Field(pattern=SHA_RE.pattern)`
    - `verdict: Literal["pass", "fail"]`
    - `reopened: list[str] = Field(default_factory=list, max_length=200)`
    - `new_findings: list[Finding] = Field(default_factory=list, max_length=200)`
    - `finding_verdicts: list[FindingVerdict] = Field(default_factory=list, max_length=200)`
    - `flags_addressed: list[FlagNote] = Field(default_factory=list, max_length=2000)`
    - `src_diff_sha256: Optional[str] = Field(default=None, pattern=SHA_RE.pattern)`
- **Response:** 200 JSON — from `review` (services/delivery-py/src/zbm_delivery/service.py:1370): object with keys `run_id`, `status`, `next_run_id`, `request_id`, `facts_sha256`; via _red_finish (services/delivery-py/src/zbm_delivery/service.py:830), _record_review_locked (services/delivery-py/src/zbm_delivery/service.py:1419), stored idempotent answer (replay).
- **Errors raised on this code path:** 403 `Forbidden`, 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `POST /dlv/v1/fix-runs/{run_id}/cancel`

- **Source:** `services/delivery-py/src/zbm_delivery/api.py:364`
- **Purpose:** Cancel a fix run.
- **Auth:** `Authorization: Bearer <DLV_SERVICE_TOKEN>`; plus `X-DLV-Caller-Token` = caller ∈ {aegis, andre_session}
- **Path params:** `run_id`
- **Request body:** JSON `CancelRequest` (services/delivery-py/src/zbm_delivery/models.py:253); `model_config = ConfigDict(extra="forbid", str_max_length=FREE_TEXT_MAX)`
    - `request_id: str = Field(pattern=f"^{ID_RE.pattern}$")`
    - `reason: str = Field(min_length=1, max_length=400)`
- **Response:** 200 JSON — from `cancel` (services/delivery-py/src/zbm_delivery/service.py:1506): object with keys `run_id`, `status`, `request_id`, `facts_sha256`, `ledger_event_id`; via stored idempotent answer (replay), _cancel_admission_locked (services/delivery-py/src/zbm_delivery/service.py:1532).
- **Errors raised on this code path:** 404 `NotFound`, 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`

#### `GET /dlv/v1/policy`

- **Source:** `services/delivery-py/src/zbm_delivery/api.py:369`
- **Purpose:** Current tool/test policy view.
- **Auth:** `Authorization: Bearer <DLV_SERVICE_TOKEN>`; plus `X-DLV-Caller-Token` = any recognised caller
- **Request body:** none
- **Response:** 200 JSON — from `policy_view` (services/delivery-py/src/zbm_delivery/service.py:604): object with keys `policy_version`, `classes`, `test_commands`, `prompts_manifest_sha256`, `policy_seed_sha256`, `test_commands_sha256`.
- **Errors raised on this code path:** none found statically

#### `GET /dlv/v1/audit/export`

- **Source:** `services/delivery-py/src/zbm_delivery/api.py:373`
- **Purpose:** Audit export of the local record log (paged; optional since/until).
- **Auth:** `Authorization: Bearer <DLV_SERVICE_TOKEN>`; plus `X-DLV-Caller-Token` = any recognised caller
- **Query params:** `since: Optional[str] = Query(default=None, max_length=40)`; `until: Optional[str] = Query(default=None, max_length=40)`; `cursor: int = Query(default=0, ge=0, le=10**12)`
- **Request body:** none
- **Response:** 200 JSON — from `audit_export` (services/delivery-py/src/zbm_delivery/service.py:1558): object with keys `records`, `next_cursor`, `ledger_event_id`, `chain_valid`.
- **Errors raised on this code path:** 422 `Invalid`, 503 `Unavailable`

#### `GET /dlv/v1/reconcile`

- **Source:** `services/delivery-py/src/zbm_delivery/api.py:382`
- **Purpose:** Andre previews what a ledger reconcile would void.
- **Auth:** `Authorization: Bearer <DLV_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** none
- **Response:** 200 JSON — from `reconcile_plan` (services/delivery-py/src/zbm_delivery/service.py:1588): object with keys `epoch`, `head_seq`, `head_sha256`, `fatal`, `voidable`, `void_lines`, `void_event_ids`, `reconcile_mode`.
- **Errors raised on this code path:** 503 `Unavailable`

#### `POST /dlv/v1/reconcile`

- **Source:** `services/delivery-py/src/zbm_delivery/api.py:386`
- **Purpose:** Andre voids stray lines/events to reconcile with the ledger.
- **Auth:** `Authorization: Bearer <DLV_SERVICE_TOKEN>`; plus `X-Andre-Approval-Token` (Andre; FounderGate)
- **Request body:** JSON `ReconcileRequest` (services/delivery-py/src/zbm_delivery/models.py:263); `model_config = ConfigDict(extra="forbid", str_max_length=FREE_TEXT_MAX)`
    - `request_id: str = Field(pattern=f"^{ID_RE.pattern}$")`
    - `head_sha256: str = Field(pattern=SHA_RE.pattern)`
    - `void_lines: list[int] = Field(default_factory=list, max_length=10_000)`
    - `void_event_ids: list[str] = Field(default_factory=list, max_length=10_000)`
- **Response:** 200 JSON — from `reconcile` (services/delivery-py/src/zbm_delivery/service.py:1599): object with keys `reconcile_event_id`, `voided`, `void_lines`, `restart_required`, `request_id`, `remaining_problems`; via stored idempotent answer (replay).
- **Errors raised on this code path:** 409 `Conflict`, 422 `Invalid`, 503 `Unavailable`


---

## Call graph (from client code)

Each outbound edge is wired **only when the caller's URL/token env vars are set**. Otherwise the caller uses a fail-closed stand-in and makes no HTTP call.

```
dashboard-ts ──GET /revenue-recovery/findings──▶ orchestrator-go
orchestrator-go ──GET /fixtures/*, POST /agents/*/detect, POST /correlation/overlaps──▶ detection-py
orchestrator-go ──GET /ledger/verify, POST /ledger/append, GET /ledger/entries──▶ ledger-rust
onboarding-py ──POST /agents/*/detect (8), POST /correlation/overlaps──▶ detection-py
onboarding-py ──POST /compliance/v1/rule──▶ compliance-py
creative-py ──POST /compliance/v1/review──▶ compliance-py
verification-py ──GET /compliance/v1/register/{id}──▶ compliance-py
legal-py ──POST /compliance/v1/register/proposals, GET /compliance/v1/register/{id}──▶ compliance-py
finance-py ──GET /vi/v1/submissions/{id}/certification, GET /vi/v1/clawbacks──▶ verification-py
finance-py ──GET /compliance/v1/rulings/{id}, GET /compliance/v1/holds, GET /compliance/v1/register/{id}──▶ compliance-py
clipper-network-py ──/vi/v1/* (age, identity, connections, integrity, strikes, findings, certifications, bans)──▶ verification-py
clipper-network-py ──POST /compliance/v1/jurisdictions/resolve, POST /compliance/v1/rule, POST /compliance/v1/review, GET /compliance/v1/activations/…──▶ compliance-py
clipper-network-py ──GET /zbc/campaigns/{id}/rulebooks, GET /zbc/campaigns/{id}/kit──▶ creative-py
onboarding, creative, compliance, verification, clipper-network, finance, legal, delivery ──POST /ledger/events──▶ ledger-rust
compliance, verification, clipper-network, finance, legal, delivery ──GET /ledger/verify + GET /ledger/entries──▶ ledger-rust
creative-py ──GET /ledger/entries──▶ ledger-rust
```

| From → To | Client code | Wiring env |
|---|---|---|
| dashboard-ts → orchestrator-go | `apps/dashboard-ts/src/lib/api.ts:78` | `ORCHESTRATOR_URL`, `ORCHESTRATOR_SERVICE_TOKEN` |
| orchestrator-go → detection-py | `services/orchestrator-go/internal/client/client.go:183-227`, `tier2.go:27-73` | `DETECTION_SERVICE_URL`, `DETECTION_SERVICE_TOKEN` |
| orchestrator-go → ledger-rust | `services/orchestrator-go/internal/client/ledger.go:70,87,167` | `LEDGER_SERVICE_URL`, `LEDGER_SERVICE_TOKEN` |
| onboarding-py → detection-py | `services/onboarding-py/src/integrations/revenue_recovery.py:33-43` | `DETECTION_SERVICE_URL`, `DETECTION_SERVICE_TOKEN` |
| onboarding-py → compliance-py | `services/onboarding-py/src/integrations/compliance38.py:43` | `COMPLIANCE_SERVICE_URL`, `COMPLIANCE_SERVICE_TOKEN`, `COMPLIANCE_CALLER_TOKEN` |
| creative-py → compliance-py | `services/creative-py/src/shared/compliance38.py:45` | same three `COMPLIANCE_*` vars |
| verification-py → compliance-py | `services/verification-py/src/compliance_client.py:118` | `VI_COMPLIANCE_URL`, `VI_COMPLIANCE_TOKEN`, `VI_COMPLIANCE_CALLER_TOKEN` |
| legal-py → compliance-py | `services/legal-py/src/compliance_client.py:110,129` | `LEGAL_COMPLIANCE_URL`, `LEGAL_COMPLIANCE_TOKEN`, `LEGAL_COMPLIANCE_CALLER_TOKEN` |
| finance-py → verification-py, compliance-py | `services/finance-py/src/clients.py:127,144,166,176,193` | `FIN_VI_*`, `FIN_COMPLIANCE_*` (URL, TOKEN, CALLER_TOKEN) |
| clipper-network-py → verification-py | `services/clipper-network-py/src/httpclients.py:167-391` | `CN_VI_URL`, `CN_VI_SERVICE_TOKEN`, `CN_VI_CALLER_TOKEN` |
| clipper-network-py → compliance-py | `services/clipper-network-py/src/httpclients.py:394-449` | `CN_COMPLIANCE_URL`, `CN_COMPLIANCE_SERVICE_TOKEN`, `CN_COMPLIANCE_CALLER_TOKEN` |
| clipper-network-py → creative-py | `services/clipper-network-py/src/httpclients.py:452-492` | `CN_CREATIVE_URL`, `CN_CREATIVE_SERVICE_TOKEN` |
| services → ledger-rust | `services/*/src/ledger.py`, `services/creative-py/src/shared/ledger.py`, `services/delivery-py/src/zbm_delivery/ledger.py` | `LEDGER_SERVICE_URL`, `LEDGER_SERVICE_TOKEN` |

Outbound calls to systems outside this repo, when enabled:

* **verification-py → TikTok oEmbed** when `VI_OEMBED_ENABLED=1` (`services/verification-py/src/api.py:537-538`).
* **compliance-py watcher → allow-listed regulatory feeds** when the watcher is enabled (`HttpFeedFetcher`, `services/compliance-py/src/api.py:513-514`).
* **delivery-py → LLM provider** through its egress client, plus a local Docker daemon (`services/delivery-py/src/zbm_delivery/api.py:418-427`).
* **fulfillment-py** makes no HTTP calls; its SIP dialer and system of record are not-wired stand-ins by default.
* **detection-py** makes no outbound calls.

### Mismatches between clients and routes (found while building this map)

1. **clipper-network-py → compliance-py: route does not exist.** `HttpCompliance.latest_activation` calls `GET /compliance/v1/activations/{lane}/{subject_id}/latest` (`services/clipper-network-py/src/httpclients.py:438-441`, used at `src/service.py:1367-1368`). compliance-py registers no `/compliance/v1/activations/…` route. The call would get FastAPI's 404, and the client treats any non-200 as "not allowed".
2. **clipper-network-py → creative-py: wrong method.** `HttpCreative.kit` calls `GET /zbc/campaigns/{campaign_id}/kit` (`services/clipper-network-py/src/httpclients.py:485-492`). creative-py only registers `POST …/kit` (`services/creative-py/src/api.py:1115`) and `POST …/kit/sign`. The GET would get 405, and the client answers "Creative kit unusable".
3. **clipper-network-py has no caller identity at compliance-py.** compliance-py's caller names do not include `clipper_network` (`services/compliance-py/src/config.py:13-14`). CN's calls to `POST /compliance/v1/rule` (which accepts only caller `onboarding`, `services/compliance-py/src/api.py:315-317`) and `POST /compliance/v1/review` (only `creative_production`) can succeed only if CN is configured with another department's caller token in `CN_COMPLIANCE_CALLER_TOKEN`. `POST /compliance/v1/jurisdictions/resolve` accepts any recognised caller. How this is meant to be configured is UNKNOWN from code.

## Services where routes could not be determined

None. Every service's routes were read from code:

* the FastAPI decorators;
* the two non-decorator `app.post(...)(handler)` registrations in compliance-py (`api.py:341-342`);
* the Go mux in orchestrator-go (`main.go:167-195`);
* the explicit `match` in ledger-rust (`server.rs:413-442`);
* the Next.js proxy and route handler in dashboard-ts.

No `APIRouter`, `include_router`, `add_api_route` or `mount` is used anywhere in `services/`.

The remaining **UNKNOWN**s are response shapes that are not dict literals (e.g. `dict(stored_record)`); each one cites the method to read.
