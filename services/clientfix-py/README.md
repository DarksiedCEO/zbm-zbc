# clientfix-py — Client Delivery & Operations (28): the client-fix lane

The second lane of department 28. `services/delivery-py` (ADR 0011) is the AEGIS fix engine for OUR OWN code; this
service fixes the things Revenue Recovery finds in a CLIENT's systems, in four version-1 lanes (founder decision 3):

- **store and website settings** — Shopify product pages and SEO, pages, broken links (redirects), product
  metafields (WooCommerce, checkout settings and site speed are NOT_BUILT);
- **tracking and analytics** — GA4 key events, Google Tag Manager tags (ad pixels are NOT_BUILT);
- **follow-up automations** — missed calls, abandoned carts, unanswered leads, booking flows: ports to our own Service
  (29-30) and Sales (27) departments, both NOT_BUILT (neither has a client-automation route yet); third-party CRMs
  NOT_BUILT;
- **listings and reviews** — Google Business Profile phone, website and address (verified, gated on Google's
  approval); Yelp as a **guided manual fix** (Yelp has no write API we may use).

Agents never get free rein on a client system. The fire teams (Claude, inside delivery-py's runtime) only PROPOSE a
change set — typed operations from a per-connector allowlist with exact before and after values. The client approves
the exact plan by its hash. Deterministic code (`src/executor.py`) then snapshots, dry-runs where the platform can,
applies, reads back, and rolls back from the snapshot on any mismatch. Only a re-detection by Revenue Recovery makes
an item `fixed_proven`; anything not proven is refunded, with Andre's approval.

Architecture, founder decisions, the connector table with doc citations, and the unlock list:
`docs/adr/0017-client-fix-lane-architecture.md`.

**Status:** built and tested; not in force. Nothing is wired: every apply answers `503 CONNECTOR_NOT_WIRED` before
anything is touched, engaging the fire team answers `503 MODEL_NOT_WIRED`, re-detection answers unknown (nothing is
ever counted fixed), and approved refunds stay `queued`. Not in CI and not in `docs/test-counts.md` yet.

## Run

```bash
cd services/clientfix-py && python3 -m pytest -q -p no:cacheprovider     # no network; counts: docs/test-counts.md
export CFX_SERVICE_TOKEN=<secret>                       # required: refuses to start without it
export CFX_CALLER_TOKENS='{"hub": "<>=32 printable chars>", "dashboard": "...", "clientfix_agent": "...", "fire_team": "...", "orchestrator": "...", "scheduler": "...", "finance_31": "...", "compliance_38": "..."}'
export CFX_DATA_DIR=/var/lib/zbm/clientfix               # required unless CFX_NON_PRODUCTION=1; owned by the service user, 0700
export CFX_ANDRE_APPROVAL_TOKEN=<Andre's token>         # unset = nothing Andre-only can happen (cancel, refunds, unfreeze)
export LEDGER_SERVICE_URL=http://127.0.0.1:8090 LEDGER_SERVICE_TOKEN=<ledger secret>
cd src && python3 -m api                                # CFX_BIND_ADDR (127.0.0.1), CFX_PORT (8500)
```

Live run against the real ledger binary and this production entrypoint:
`LEDGER_BIN=<ledger-rust>/target/release/server python3 devtools/live_run.py` (no check depends on the time of day).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `CFX_SERVICE_TOKEN` | — (required) | bearer token for every route but `/health` |
| `CFX_CALLER_TOKENS` | `{}` | JSON `{caller: token}`; callers: `hub`, `dashboard`, `clientfix_agent`, `fire_team`, `orchestrator`, `scheduler`, `finance_31`, `compliance_38`; all distinct |
| `CFX_NON_PRODUCTION` | `0` | `1` allows an in-memory run (tests only) |
| `CFX_DATA_DIR` | — (required in production) | the local log and the flock; 0700, owned by the service user |
| `CFX_ANDRE_APPROVAL_TOKEN` | unset | Andre's token (`X-Andre-Approval-Token`, through `dashboard` only); equal to any caller or the service token = not configured |
| `CFX_CLIENT_SESSION_MINUTES` | `30` (5..240) | how long a hub client session lasts, on the service clock |
| `CFX_UNKNOWN_TICKS_BEFORE_TASK` | `6` (1..1000) | re-detection / manual-verify runs that answer unknown before ONE task for Andre (counted in runs, never wall hours) |
| `CFX_MAX_ITEMS_PER_JOB` | `50` (1..200) | items in one job |
| `CFX_MAX_OPS_PER_ITEM` | `20` (1..50) | operations in one item's change set |
| `CFX_BIND_ADDR` / `CFX_PORT` | `127.0.0.1` / `8500` | listen address |
| `CFX_REQUEST_HEAD_TIMEOUT_SECONDS`, `CFX_KEEP_ALIVE_TIMEOUT_SECONDS`, `CFX_LIMIT_CONCURRENCY`, `CFX_SWITCH_INTERVAL_SECONDS`, `CFX_DRAINS_MAX` | 10 / 5 / 128 / 0.001 / 512 | the hardened launcher (`serve.py`, shared with every Python service) |
| `LEDGER_SERVICE_URL` / `LEDGER_SERVICE_TOKEN` | unset | ledger-rust; unset = nothing can take effect |

**NOT_BUILT switches** — each refuses start if set (`config.NOT_BUILT`; the ADR's unlock list says what each needs):
`CFX_ANTHROPIC_API_KEY_REF`, `CFX_DELIVERY_RUNTIME_URL`, `CFX_CONNECTOR_TRANSPORT`, `CFX_VAULT_URL`,
`CFX_SHOPIFY_APP_CLIENT_REF`, `CFX_GOOGLE_OAUTH_CLIENT_REF`, `CFX_GBP_API_ACCESS`, `CFX_YELP_API_KEY_REF`,
`CFX_DETECTION_URL`, `CFX_FINANCE_URL`, `CFX_SERVICE_AUTOMATIONS_URL`, `CFX_SALES_AUTOMATIONS_URL`,
`CFX_CRM_PROVIDER`, `CFX_WOOCOMMERCE`.

## Contracts

**Request ids.** Every write carries a `request_id`: a UUID (with or without hyphens) or 16..64 hex characters, any
case (lower-cased first). The request key is `op|target|request_id` per caller with the SHA-256 of the body: the same
body is answered as the first time (also after a restart); a different body is `409 REQUEST_ID_REUSED`.

**Ids from other departments, exactly as they make them.** `client_id` is onboarding-py's (`onb-<32 or 64 hex>`);
`finance_event_id` is finance-py's own (`fin-<prefix>-<40 hex>` or 26 Crockford base32); `finding_id` / `agent_id` are
detection-py's `FindingRef` / `AgentId` (≤ 128 / ≤ 64 characters, safe alphabet); `leak_category` is detection-py's
`LeakCategory`. This service's own ids are `cfx-<abbrev>-<40 hex>`.

**Connections hold vault references only.** The hub registers a connection after the client finished an official
OAuth app flow: `connector`, `account_ref` (a `*.myshopify.com` shop, `properties/<n>`, `accounts/<n>/containers/<n>`,
`locations/<n>`, a Yelp business id), the granted `scopes` (each connector's required scopes must be among them), and
`token_ref` = `vault:<owner>.<name>` (security-py's reference shape). The reference is never shown back
(`has_token_ref`). A body naming a password, secret, token or personal-data key anywhere, or carrying a
credential-shaped value anywhere (`shpat_…`, `ya29.…`, `1//…`, `sk-ant-…`, a JWT, a PEM key, `Bearer …`, a URL with
user:password), is refused 422 (`SECRET_REFUSED`). Yelp takes no `token_ref` at all.

**Change sets** are bound to their finding: every op targets the finding's resource and uses only the ops and fields
its check allows (`catalogue.CHECK_OPS`). Rich text is accepted only in the allowlist's canonical form
(`connectors/richtext.py`: `sanitize(value) == value`). GTM targets are `accounts/A/containers/C/tags/T`; GTM runs lease
the whole container. A rollback never overwrites a value it did not write (`conflict`: frozen, Andre alerted).

**Revocation is never refused.** `POST /connections/{id}/revoke` (hub or dashboard) sets the kill switch first: a
running apply for that client stops at its next request (the client's revocation epoch moved), even if the commit
fails (503 with `work_stopped: true`; retry). Planned items on the connection become `cancelled_revoked`.

**Client approvals.** Through the `hub` caller with `X-CFX-Client-Session` — a token the hub got from
`POST /client-sessions` for a client it authenticated (shown once; only its SHA-256 is kept). The quote is accepted by
`quote_sha256`; the plan by `plan_sha256`, which the service recomputes from the stored plan at approval AND at apply
(SHA-256 of canonical JSON of the job id, client id, quote hash, plan version, team and every item's finding, check,
connection, connector, account and exact ops). Any new plan voids the approval.

**Evidence: committed vs attempted.** As bizdev-py: typed evidence events are recorded first; only events named by an
anchored log line with matching `rk` and `seq` are `committed` in `GET /cfx/v1/audit/evidence`; everything else is
`attempted`. Eventually consistent: re-read to settle.

## Routes (prefix `/cfx/v1` except `/health`)

| Route | Caller | What |
|---|---|---|
| `GET /health` | none | `ok` / `degraded` / `closed` (503) |
| `GET /status` | dashboard | counts by state, ports wired, integrity |
| `GET /connectors` | dashboard, clientfix_agent, fire_team, compliance_38 | the connector table: status, allowlisted ops, dry-run mode, doc URLs |
| `POST /connections` | hub | register an OAuth connection by vault reference |
| `GET /connections[?client_id]`, `GET /connections/{id}` | dashboard, clientfix_agent (+ hub for one) | views (no token reference) |
| `POST /connections/{id}/revoke` | hub, dashboard | revoke; never refused |
| `POST /client-sessions` | hub | open a client session (token shown once) |
| `POST /findings` | orchestrator, dashboard | a Revenue Recovery finding mapped to a check and a client resource |
| `POST /jobs` | clientfix_agent, dashboard | a job and its quote from open findings of one client |
| `GET /jobs[?client_id&status]` | dashboard, clientfix_agent, compliance_38 | jobs |
| `GET /jobs/{id}` | dashboard, clientfix_agent, compliance_38; hub inside the client's session | one job: items, quote, plan, results, report |
| `POST /jobs/{id}/quote/accept` | hub + client session | accept the quote by hash; asks Finance for the up-front invoice (not wired) |
| `POST /finance/events` | finance_31 | `payment_confirmed` for exactly the quote's amount and hash |
| `GET /jobs/{id}/brief` | fire_team, dashboard | the fire team's brief with each item's current values (read-only; secret-shaped values withheld; never a credential) — paid jobs only |
| `POST /jobs/{id}/engage` | clientfix_agent, dashboard, scheduler | run the fire team (503 `MODEL_NOT_WIRED` today) |
| `POST /jobs/{id}/plan` | fire_team, dashboard | submit the change sets (validated; paid jobs only) |
| `POST /jobs/{id}/plan/approve` | hub + client session | approve the exact plan hash |
| `POST /jobs/{id}/apply` | scheduler, dashboard | run the deterministic executor (503 `CONNECTOR_NOT_WIRED` today) |
| `POST /jobs/{id}/manual-done` | hub + client session, dashboard | a guided manual fix was made on the platform |
| `POST /jobs/{id}/cancel` | Andre | cancel before apply (paid: refund proposed) |
| `POST /jobs/{id}/items/{item}/close` | Andre | end an unprovable item as unfixed, by its state hash |
| `POST /clients/freeze`, `POST /clients/unfreeze` | Andre | stop / resume all work for a client |
| `GET /frozen`, `GET /leases` | dashboard, compliance_38 | frozen resources (with `freeze_sha256`), leases |
| `POST /frozen/unfreeze` | Andre | unfreeze one resource by its exact `freeze_sha256` |
| `GET /refunds`, `POST /refunds/{id}/approve` | dashboard, compliance_38 / Andre | refund proposals; Andre approves by `refund_sha256` |
| `GET /tasks`, `POST /tasks/{id}/close` | dashboard / Andre | Andre's tasks (rollback failed, interrupted, revoked mid-apply, re-detection disagrees or unknown, refund decision) |
| `POST /ticks/{name}` | scheduler | `apply-queue`, `manual-verify`, `redetect`, `refunds`, `recover`, `integrity` |
| `GET /audit/integrity`, `/audit/export`, `/audit/evidence` | dashboard, compliance_38 | integrity (ledger verdict as returned), the log with client values hashed, committed vs attempted evidence |

## Item states

`open` → `planned` → apply → `applied_verified` | `awaiting_manual` → `manual_reported` → `manual_verified`, then
re-detection → `fixed_proven` (payment kept) | `not_cleared`. Unfixed terminal states (refundable once paid):
`not_cleared`, `drifted`, `snapshot_unknown`, `dry_run_refused`, `dry_run_unknown`, `rolled_back`, `rollback_failed`
(resource frozen, Andre alerted), `halted_revoked` (frozen when a write was sent), `halted_frozen` (a guarded
rollback is attempted; frozen and Andre alerted when a write was sent),
`interrupted` (frozen), `cancelled`, `cancelled_revoked`, `abandoned`.
