# ADR 0020 — Revenue Recovery on a real Shopify store: read-only store connector

Status: **WIP, not merged** (branch `golive-rr` from integration `84a5377`, go-live plan Days 6-9). Amends ADR 0001
decision 5 (the per-platform translation layer "where real-store integration attaches later"). The connector ships
**NOT_CONNECTED**: no client token exists, and nothing in this repo makes an outbound call to Shopify until Andre
registers a connection (section 9).

## Shopify facts this design rests on (verified Oct 24 2026)

Each fact was read on the page named, on Oct 24 2026. Quotes are the page's words. A fact marked UNVERIFIED was not
found on shopify.dev and the design does not depend on it being true.

| Id | Fact | Source |
|---|---|---|
| S1 | Admin-created ("legacy") custom apps can no longer be created: "You can no longer create new admin-created custom apps." "Existing apps are unaffected and continue to work." "For new apps, use Dev Dashboard or Shopify CLI." A legacy app's token "is shown only once, when it's generated". | https://shopify.dev/docs/apps/build/authentication-authorization/legacy/admin-custom-apps |
| S2 | Dev Dashboard client credentials grant: "only works when the app and the store belong to the same Shopify organization"; "Client credentials can't reach a store outside your organization, including a client's store." Token endpoint `POST https://{shop}.myshopify.com/admin/oauth/access_token`, form-encoded `grant_type=client_credentials`, `client_id`, `client_secret`; "Tokens expire after 24 hours" (`expires_in` "Always 86399"); "The token request doesn't ask for scopes" — scopes are set on the app version in the Dev Dashboard. For stores outside the organization the page points to custom distribution with token exchange or the authorization code grant. | https://shopify.dev/docs/apps/build/dev-dashboard/get-api-access-tokens?lang=curl |
| S3 | Shopify staff (community forum, Jan 2026, "the January 1st deprecation"): "The merchant creates the app in the Dev Dashboard and provides you with the Client ID and Client Secret" (client credentials, 24 h tokens); or the authorization code grant, which yields a "non-expiring offline token". **Forum, not shopify.dev documentation.** | https://community.shopify.dev/t/is-it-still-possible-to-create-a-custom-app-for-a-single-store-admin-storefront-access-without-partner-dashboard-cli/28387 |
| S4 | Versioning: "Shopify releases a new API version every three months"; names are date-based (`2026-04`); "Each stable version is supported for a minimum of 12 months"; "If your app targets an inaccessible version, Shopify falls forward and responds using the oldest accessible stable version"; responses carry `X-Shopify-API-Version`. The version table labels **`2026-10` "Latest stable"** and `2027-01` a release candidate. | https://shopify.dev/docs/api/usage/versioning/index |
| S5 | Endpoint `POST https://{store_name}.myshopify.com/admin/api/2026-10/graphql.json`; token in the `X-Shopify-Access-Token` header. The API "can return HTTP 200 for errors"; codes `THROTTLED` (like 429), `ACCESS_DENIED` (like 401), `SHOP_INACTIVE`, `INTERNAL_SERVER_ERROR`; HTTP 402 (shop frozen), 403 (shop forbidden), 404, 423 (shop unavailable), 5xx. | https://shopify.dev/docs/api/admin-graphql/2026-10.md |
| S6 | Cost-based throttling: Standard 100 points/second, Advanced 200, Plus 1000, enterprise 2000; "A single query may not exceed a cost of 1,000 points"; `extensions.cost` = `{requestedQueryCost, actualQueryCost, throttleStatus: {maximumAvailable, currentlyAvailable, restoreRate}}`; bulk operations "don't have the max cost limits or rate limits that single queries have". | https://shopify.dev/docs/apps/build/apis/graphql-admin/rate-limits.md |
| S7 | "The recommended backoff time is one second"; array inputs are capped at 250. | https://shopify.dev/api/usage/rate-limits |
| S8 | Cursor pagination: "up to a maximum of 250 resources" per page; `pageInfo { hasNextPage endCursor }`; next page with `after:`. Cursor lifetime is not stated. | https://shopify.dev/docs/api/usage/pagination-graphql |
| S9 | Bulk queries: started by the `bulkOperationRunQuery` **mutation**; results are a JSONL file at a signed URL (the example host is `storage.googleapis.com`) that expires after a week; up to five concurrent bulk queries per shop from 2026-01; must complete within 10 days; polling or a webhook. | https://shopify.dev/docs/apps/build/apis/graphql-admin/bulk-operations/queries.md |
| S10 | Scopes: `read_orders` covers "orders created within the last 60 days"; `read_all_orders` adds older orders, "doesn't grant access on its own", needs Shopify approval for Partner-organization apps but apps created in the Dev Dashboard under a merchant organization "don't need to request Shopify approval"; "Any scope that writes a resource also grants read access to it"; an installation's granted scopes are read from `currentAppInstallation { accessScopes { handle } }`. | https://shopify.dev/docs/api/usage/access-scopes |
| S11 | Protected customer data includes "Orders, draft orders, abandoned checkouts, refunds, transactions" relating to a customer. Level 1 = customer data excluding name, address, phone and email; Level 2 adds those fields. Level 1 requirements include processing "only the minimum personal data required", stated purposes, retention periods, encryption at rest and in transit, merchant data-protection agreements. Level 1 is "Always available" to custom apps; public apps need review. | https://shopify.dev/docs/apps/launch/protected-customer-data |
| S13 | (Oct 25 2026, H-1) `DiscountAllocation` (2026-10, "latest"): `allocatedAmountSet: MoneyBag!` "The money amount that's allocated to a line based on the associated discount application in shop and presentment currencies"; `discountApplication: DiscountApplication!` "The discount application that the allocated amount originated from" (`allocatedAmount` is deprecated). | https://shopify.dev/docs/api/admin-graphql/latest/objects/DiscountAllocation |
| S14 | (Oct 25 2026, H-1) `DiscountApplication` interface (2026-10): `index: Int!` "An ordered index that can be used to identify the discount application and indicate the precedence of the discount application for calculations"; also `allocationMethod`, `targetSelection`, `targetType`, `value`; implemented by `AutomaticDiscountApplication`, `DiscountCodeApplication`, `ManualDiscountApplication`, `ScriptDiscountApplication`. | https://shopify.dev/docs/api/admin-graphql/latest/interfaces/DiscountApplication |
| S15 | (Oct 25 2026, H-1) `LineItem` (front matter `api_version: 2026-10`): `discountAllocations: [DiscountAllocation!]!` "The discounts that have been allocated to the line item by discount applications" — a plain list, and it **includes discounts on refunded and removed quantities**; `quantity: Int!` "The number of units ordered, including refunded and removed units"; `currentQuantity: Int!` "The number of units ordered, excluding refunded and removed units". | https://shopify.dev/docs/api/admin-graphql/latest/objects/LineItem.md |
| S16 | (Oct 25 2026, H-1) `Order` (2026-10): `totalRefundedSet: MoneyBag!` "The total amount that was refunded, in shop and presentment currencies"; `discountApplications` "A list of discounts that are applied to the order, excluding order edits and refunds". Cross-checked in Shopify's Admin schema tool (S12's tool) for `DiscountAllocation`, `LineItem.currentQuantity`, `Order.totalRefundedSet`. | https://shopify.dev/docs/api/admin-graphql/latest/objects/Order |
| S17 | (Oct 25 2026, H-1) Combining discounts: "Multiple discounts are applied to an order only when the setting on each discount allows the combination"; with two or more percentage order discounts "both percentages are calculated on the original subtotal"; "All order discounts are applied on the order subtotal after product discounts have been applied"; a percentage order discount applies before a fixed-amount one; at most 5 product or order discount codes per order. **help.shopify.com, not shopify.dev**; used only to explain why a recomputation is wrong and why the one-code policy is an assumption, never as an input to a figure. | https://help.shopify.com/manual/discounts/discount-combinations |
| S12 | Schema (Shopify's Admin schema tool, pinned version not stated by the tool): `Order` has `legacyResourceId`, `createdAt`, `cancelledAt`, `test`, `currencyCode`, `displayFinancialStatus` (`AUTHORIZED EXPIRED PAID PARTIALLY_PAID PARTIALLY_REFUNDED PENDING REFUNDED VOIDED`), `discountCodes`, `discountApplications` (connection); `DiscountCodeApplication {code allocationMethod(ACROSS EACH) targetSelection(ALL ENTITLED EXPLICIT) targetType(LINE_ITEM SHIPPING_LINE) value: PricingValue = MoneyV2 | PricingPercentageValue}`; `LineItem {sku quantity originalUnitPriceSet}`; `Shop.currencyCode`; `AppInstallation.accessScopes`. | Shopify Admin GraphQL schema (MCP `graphql_schema`), cross-checked against S5 |

### U2 — can a merchant hand over an Admin API token without App Store review?

**Status: PARTLY VERIFIED; the "merchant creates a Dev Dashboard app" half is UNVERIFIED on shopify.dev.**

- VERIFIED (S1): a merchant can **no longer create** an admin-created custom app, so a new client cannot generate the
  old static `shpat_` token. A client that already has one keeps a working token.
- VERIFIED (S2, S10): a Dev Dashboard app under the merchant's own organization gets 24-hour tokens by the client
  credentials grant, with scopes fixed on the app version, and needs no Shopify approval even for `read_all_orders`.
  The client credentials grant cannot reach a store outside the app's organization.
- UNVERIFIED (S3, forum only): that a merchant with no Partner account can create that Dev Dashboard app themselves
  and give us its client id and secret. No shopify.dev page found says so or says otherwise; none mentions App Store
  review for it.
- The documented route for an app ZBM owns is custom distribution with OAuth (authorization code grant, offline
  token). Not built here: it needs a hosted callback.

So the connector accepts **two credential kinds**, all registered the same way (section 3): `client_credentials`
(the client's own Dev Dashboard app; ZBM exchanges id + secret for a 24 h token per sync), `static_token` (a client's
existing legacy custom-app token, or an offline token from a future OAuth flow), and nothing else.

## Decisions

### 1. Where it lives

Revenue Recovery is two services: **detection-py** hosts the agents (and now the store connector), **orchestrator-go**
hosts the scan (agents, correlation, ledger record; ADR 0001, ADR 0003 section 13). The connector is in detection-py
(`src/store_connector/`) because the repo's outbound-call conventions (seo-py `finance_client.py`) are Python and the
translation target (`zbm_schema.Order`) is there. The orchestrator never talks to a store.

```
Andre/ops ──POST /stores/sync──▶ detection-py ──(record-first: rr_store_read_requested)──▶ ledger-rust
                                     │  token exchange* / preflight / orders pages  ──▶ https://<shop>.myshopify.com
                                     │  translate -> minimised snapshot (memory)    ──▶ rr_store_read_completed
orchestrator-go ──POST /stores/snapshot──▶ detection-py (no outbound) ──▶ agents ──▶ rr_scan_started/…/completed
                                                                                         (src=shopify:<snapshot_id>)
```
\* client credentials only.

### 2. Routes (detection-py, service bearer token)

| Route | Outbound call? | Answers |
|---|---|---|
| `GET /stores/{client_id}/status` | never | registration, kill switch, budget, held snapshot id; never the credential or its path |
| `POST /stores/sync` `{"client_id"}` | only this route, only after every gate in section 5 | 200 `{sync_id, snapshot_id, orders_read, orders_analysed, orders_excluded, truncated_by, pages, cost_points, throttle_waits}` or a reason code |
| `POST /stores/snapshot` `{"client_id"}` | never | the held snapshot (orders + coverage) or a reason code |

orchestrator-go `POST /revenue-recovery/scan?client_id=<tenant>`: the fixture tenant (also the default when `client_id`
is omitted) is refused with 403 `FIXTURE_SCAN_DISABLED`, before the ledger or detection is called, unless
`ORCHESTRATOR_ALLOW_FIXTURE_SCANS=1` (dev, tests and live runs; never on Railway; AEGIS final go-live L-2), and is
otherwise unchanged; any other tenant
asks `/stores/snapshot` FIRST and, on a refusal, answers 409 (`NOT_CONNECTED`, `NOT_SYNCED`, `SNAPSHOT_STALE`, ...),
429 (budget) or 503 (kill switch, config) with `{"error", "reason"}`, having run no agent and written nothing (it
used to answer 422 "no live data source"). With a snapshot, every agent runs (the store feeds the order agents; the
rest get empty batches), the started record says `src=shopify:<snapshot_id> fixture=0` (the rrs1/rrc2 formats are
unchanged, so no rollback hazard; the snapshot id is the link to detection-py's `rr_store_read_completed`), and the
started record's `agents` (the hashed payload's list and the summary's `agents=<n>`) name only the agents the
snapshot's coverage note marks covered, plus any other agent that returned a finding anyway — not all eight that
ran on empty batches (AEGIS golive-rr L5; a fixture scan still names all eight; the record format is unchanged, only
which ids a store scan lists). A snapshot whose coverage note has no `agents` map, or an agent without a boolean
`covered`, fails the scan before anything is written. The
result carries `store_snapshot_id` and `store_coverage`, with `non_live_data_source: false`. The findings view's
`non_live_data_source` is now false only when every counted scan read a store.

### 3. Credentials (no prior pattern existed)

Revenue Recovery held no client credentials before this. The pattern is the repo's `*_FILE` rule:
`RR_STORE_CONNECTIONS_FILE` is a JSON file **Andre writes when he approves a connection** (his approval gate: each
entry must carry `approval: {approved_by: "andre", approved_on, approval_ref}`), holding secret **references** only:
`{"kind": "static_token", "token_file": "/abs/path"}` or `{"kind": "client_credentials", "client_id_file",
"client_secret_file"}`. An inline secret is refused (the model forbids unknown fields). Secret files: absolute path,
regular file, no symlink (`O_NOFOLLOW`), owned by the service user, mode 0600/0400, at most 4 KiB, read at use time
(rotation needs no restart). The connections file is re-read on every call (registering or removing a client needs no
restart), must not be group/other-writable, and refuses one shop under two clients or one client twice. Unset: every
client is NOT_CONNECTED.

The credential lives in a `Secret` (repr `Secret(<redacted>)`, not picklable) and goes only into the
`X-Shopify-Access-Token` header or the token-exchange form. Errors carry fixed messages chosen in `reasons.py`, never
upstream text; the ledger payloads and the snapshot hold counts, codes, the shop handle and digests.

**Limitation (on record):** "Andre's approval gate" here is the deployment file itself: whoever can write
`RR_STORE_CONNECTIONS_FILE` can register a store. `approved_by` is a declaration in that file, not a signature. A
signed approval (as in ooh-py's gate) is the upgrade path.

### 4. The connector (`shopify_client.py`, `net.py`, `shop_domain.py`, `shopify_models.py`)

- **GraphQL Admin API only, read-only.** Two query constants (`RrPreflight`, `RrOrders`) and the client-credentials
  token exchange; variables only, nothing interpolated. API version pinned to **`2026-10`** (S4); an answer whose
  `X-Shopify-API-Version` differs (fall-forward) is `STORE_API_VERSION_MISMATCH`.
- **Minimum scopes:** `read_orders` (required) and `read_all_orders` (optional, for a lookback over 60 days, S10).
  The preflight reads `currentAppInstallation.accessScopes` (S10) and refuses a credential with any `write_*`
  scope (`STORE_TOKEN_HAS_WRITE_SCOPE`) or any other scope (`STORE_TOKEN_EXCESS_SCOPE`, e.g. `read_customers`); for
  client credentials the token response's `scope` is checked first, before any GraphQL call. A lookback over 60
  days without `read_all_orders` is clamped to 60 and the report says so. The preflight also checks
  `shop.myshopifyDomain` equals the registered shop and `shop.currencyCode` is USD.
- **Host and SSRF:** the URL is built only from a validated handle: 3-60 of `[a-z0-9-]`, ASCII checked before
  lowercasing (the Kelvin sign cannot fold to `k`), no ACE/punycode prefix, optional `.myshopify.com` suffix — no
  scheme, port, path, userinfo, dot, IP literal or other domain. Resolution happens inside the deadline on at most
  two resolver threads (`DnsGate`), and every resolved address must be public unicast (`is_global`, not multicast):
  loopback, private, link-local (169.254.169.254), CGNAT, reserved and mapped-loopback answers are refused before a
  socket opens. No proxy from the environment, no redirects followed.
- **Hard deadline** per sync (default 120 s, `RR_STORE_SYNC_DEADLINE_SECONDS`, 10-600) covering the exchange, every
  page, every wait, every name lookup and every socket operation (`DeadlineBackend`, the seo-py pattern); the body
  loop stops at the deadline (a slow drip is `STORE_DEADLINE_EXCEEDED`). Per-request timeout 30 s within it.
- **Bounded response:** 2 MiB per answer, `Accept-Encoding: identity` (a compressed answer is refused, so the cap
  counts received bytes), JSON content type required.
- **Throttling (S5, S6, S7):** `THROTTLED` (HTTP 200 + `errors[].extensions.code`) waits
  `(requestedQueryCost - currentlyAvailable) / restoreRate`, clamped to 1-10 s; HTTP 429 waits 1 s; at most 8 waits
  per request (`STORE_THROTTLED`); between pages the client paces when the bucket cannot cover the next page. 5xx
  and `INTERNAL_SERVER_ERROR` retry at most twice. A wait that would cross the deadline is not slept.
- **Pagination (S8):** `orders(first, after, query: "created_at:>='<start>'", sortKey: CREATED_AT, reverse: true)`,
  newest first; 10 orders per page, halved down to 1 on `MAX_COST_EXCEEDED` (S6: 1,000-point single-query cap); a
  cursor that does not advance is refused; caps: `max_orders` (≤ 1,000), `max_cost_points` per sync, 1,000 pages.
  Hitting a cap is recorded as `truncated_by` and the window actually read is in the coverage note.
- **Bulk operations: not used.** They need the `bulkOperationRunQuery` mutation, deliver results from a signed URL on
  another host (S9: `storage.googleapis.com` in the example), and complete asynchronously over minutes to days —
  against the read-only, `*.myshopify.com`-only, hard-deadline constraints. Capped pagination covers a 1,000-order
  snapshot.
- **Strict parsing:** every node model forbids unknown fields and is parsed in strict JSON mode; a node carrying a
  field the query never asked for (an email, a customer) is `STORE_RESPONSE_INVALID` and nothing is kept; partial
  answers (`data` + `errors`) are refused.
- **Unverified details, failing closed:** the GraphQL error code `MAX_COST_EXCEEDED` (not on the pages read; an
  unrecognised code is `STORE_RESPONSE_INVALID`, never a retry loop), and `>=` with a quoted timestamp in the
  `created_at` filter (the orders query documents `created_at:<=2024` and quoted values; checked on the first live
  sync).

### 5. Gates before the first outbound byte; reason codes

`sync`: kill switch → registration (`NOT_CONNECTED`) → one sync at a time for this client (`STORE_SYNC_IN_PROGRESS`)
→ at most `RR_STORE_MAX_CONCURRENT_SYNCS` syncs across all clients (default 2, clamped 1-16; `STORE_SYNC_CAPACITY`)
→ the client's daily budget (`STORE_BUDGET_EXHAUSTED`) → the credential files (`STORE_CREDENTIAL_UNREADABLE`) → the
ledger record (`LEDGER_UNAVAILABLE`). Closed codes: `store_connector/reasons.py` (each with its HTTP status and fixed
message; `NO_OUTBOUND` lists the ones decided before any outbound call).

After the read (AEGIS golive-rr): if the client's registration changed while it ran — another shop, credential
reference or `approval_ref`, or deregistered — the read is discarded (`STORE_REGISTRATION_CHANGED`, recorded as
failed; checked before the completion record and again when the snapshot is stored). If the completion record
itself fails, the answer is `LEDGER_COMPLETION_UNRECORDED` (the store WAS called; not in `NO_OUTBOUND`), the read is
discarded and the failure is recorded best effort. A held snapshot carries the shop, credential reference and
`approval_ref` it was read under; `snapshot` and `status` drop it (`NOT_SYNCED`) as soon as the current registration
differs in any of them, so a client never gets a shop it no longer owns.

**Kill switch:** `RR_STORE_READS_DISABLED=1` (environment) or the existence of the file named by
`RR_STORE_KILL_SWITCH_FILE` (`touch` it: every sync stops, a running sync stops at its next page, and held
snapshots are not served — no restart). **Per-tenant budget** (in the connection entry, with hard ceilings):
`syncs_per_day` (default 4, ≤ 24; counted per UTC day, in process memory — a restart resets it), `max_orders`
(1,000), `max_cost_points` (50,000, ≤ 200,000), `lookback_days` (60, ≤ 365), `snapshot_max_age_hours` (24, ≤ 168).

### 6. Protected customer data and minimisation (S11)

The queries request no customer, name, email, phone, address, note, IP or custom attribute (pinned by a test over
the query text); so ZBM needs Shopify's Level 1 only, which S11 says is "Always available" to custom apps. Every
order gets `customer_id: "not-collected"` — the agents fed by this source never need to link orders to a person, so
nothing is hashed because nothing is collected. Kept per order: `shopify-order-<id>`, created time, financial status,
line items (SKU, unit price, quantity), discount codes and values. Snapshots are in process memory only, replaced
by the next sync, dropped when older than `snapshot_max_age_hours`, when the client is deregistered, and on restart
(retention). The ledger holds only payload hashes and summaries with counts. The Level 1 duties that are not code —
telling the merchant what is processed and why, the data-protection agreement — are in section 9.

Translation is conservative: an order whose discounts the discount agent's model would misstate is **excluded and
counted**, never reshaped: test, cancelled, not (partially) paid, more line items or discount applications than a
page holds, shipping codes, codes on selected items only (`ENTITLED`/`EXPLICIT`), fixed codes applied per item
(`EACH`), zero-value codes, non-USD or over-precise money, codes the schema cannot hold. Automatic/manual/script
discounts are not codes and are counted, not modelled.

**H-1 (final go-live AEGIS, Oct 25 2026).** The query now also asks for each discount application's `index` (S14),
each line's `currentQuantity` and `discountAllocations { allocatedAmountSet { shopMoney } discountApplication {
index } }` (S13, S15), and the order's `totalRefundedSet` (S16). Each code's dollar amount on an order is what
Shopify **recorded** for it — the sum, over every line, of the allocations whose `discountApplication.index` is that
code's `index` — carried as `DiscountApplication.recorded_usd`. It is never recomputed from the code's nominal value
against the full price: S17 says Shopify computes two percentage order codes on the same subtotal (a sequential
recompute said $9.00 on the AEGIS $100 / 10% + 10% order where Shopify records $10 + $10), and applies order codes
after product and automatic discounts. Exact per-code attribution does not exist, so the order is **excluded and
counted**, for:

| Reason | Why no exact figure exists |
|---|---|
| `non_code_discount_present` | any automatic, manual or script discount application: it changed the base the codes applied to (the AEGIS $100 / two 10% codes / 50% automatic case used to read $9.00 OBSERVED). It used to be counted and ignored while the order was still analysed. |
| `partially_refunded` | `displayFinancialStatus` `PARTIALLY_REFUNDED`: S15's allocations still include the refunded units' discount. It used to be analysed at original prices. |
| `refund_recorded` | `totalRefundedSet` above zero on any analysed status. |
| `line_items_refunded_or_removed` | a line whose `currentQuantity` differs from `quantity` (refund or order edit, S15). |
| `discount_allocation_unmatched` | an allocation pointing at an index the order's applications do not have, or two applications sharing one index. |
| `recorded_discount_exceeds_subtotal` | the recorded code amounts add up to more than the analysed lines' subtotal. |

A refunded or partly refunded order could in principle be attributed from `refunds { refundLineItems }`; that is not
built, so those orders are left out, and the report lists them under "Left out because". Leaving an order out can
only lower a figure, never raise it.

### 7. Ledger evidence (record-first)

detection-py now writes to ledger-rust (department `rr_store`, actor `detection_py`; config
`DETECTION_LEDGER_URL` + `DETECTION_LEDGER_TOKEN_FILE`): `rrs.<sync_id>.requested` BEFORE the first outbound byte
(none without it), then `rrs.<sync_id>.completed` (snapshot id and SHA-256, orders read/analysed, requests, pages,
cost points, throttle waits, granted scopes, truncation) — the snapshot is kept only after this record — or
`rrs.<sync_id>.failed` with the reason code (best effort). With `LEDGER_CALLERS_FILE`, give detection-py a write
caller scoped to `rr_store` only.

### 8. The scan's findings and the client report

Agent coverage of this source: **discount-misuse** is fed: the excess of stacked codes over the best single code,
exact cents. From Shopify the stack's total is the recorded allocations (section 6), minus the most the single
most valuable code could have given alone on the full subtotal, never less than what was recorded for it (so the
excess is a floor); methodology `disc_excess_recorded`. Sources that record no per-code amounts keep the old
recompute (`disc_excess_best_code`); an order where only some codes carry a recorded amount gives no finding.

**Evidence class: ESTIMATED, labels attributed/medium (H-1 decision).** The figure was labelled OBSERVED/high and so
"quotable". Two honest options existed: keep OBSERVED and print the assumption next to the figure, or demote. We
demote, because OBSERVED here means "read off recorded transactions/terms, exact arithmetic only" (ADR 0001,
`EvidenceClass`), and the claim that the excess is *revenue lost* rests on a rule the data does not contain: that
the store intends one code per order. S17 says Shopify only lets codes combine when the setting on each discount
allows it, so every stack on a Shopify order is one the store's own configuration permitted; it is not an observed
violation. A figure that is true only under a stated assumption is exactly `ESTIMATED` ("observed inputs combined
with a stated assumption"), and zbm_schema's M2 rule then caps its labels at attributed/medium. OBSERVED with a
caveat would still have been marked quotable and summed into the OBSERVED column, which the report's own
"the only figures we quote as facts" line would contradict. The finding's cause now states the assumption next to
the figure ("Assumption, not an observed breach: this counts as a leak only if the store intends at most one code
per order. Shopify combines codes only when each discount's combination setting allows it, so the store's own
settings permitted this stack."), and "against single-code policy" / "beyond any one authorized code" are gone.
Reading each discount's `combinesWith` would tell whether the combination was configured on purpose, but not what
the merchant intended; it needs a discounts scope and is not built.

**Projection.** One projection per check and evidence class, never adding an OBSERVED base to an ESTIMATED one. The
base is the corrected findings in the window (so excluded orders are not in it, and the stated assumption says so).
A projection is always ESTIMATED, confidence low; one built on ESTIMATED findings says `est.` in its basis and adds
their assumption (for this check: that the store intends at most one discount code per order). Affiliate, abandoned-cart, renewal, server-side,
cross-channel, platform and contract agents get no data from Shopify orders and are listed **NOT COVERED** with the
reason (abandoned checkouts are deliberately not read: they carry contact details, and whether a recovery message
went out is not in the order data).

`services/detection-py/src/rr_report.py` (+ `devtools/rr_report.py SCAN.json --md OUT.md [--json OUT.json]`) turns
one orchestrator scan result into the client report: what was read (window, counts, every exclusion reason), the
checks run with covered/NOT COVERED, each finding with its amount, evidence class, labels, cause and method (OBSERVED
= quotable; ESTIMATED = "est."; UNKNOWN = no figure), overlaps flagged "do not add", per-check OBSERVED and ESTIMATED
subtotals in separate columns, and **projections** — ESTIMATED, confidence low, `base × 365 / window days` per
evidence class with the assumption written out, only for a window of 14 days or more, never added to anything. The window (AEGIS golive-rr M2): when the read
was not truncated, every order in the requested lookback was read, so the window is the lookback (`requested_start`
to the snapshot time, else `lookback_days`) however sparse the orders; only a truncated read is measured back to the
oldest order read. The report's `window_basis` and the projection's stated basis say which. Store-supplied text
(discount codes in a finding's cause) is escaped in the Markdown. No headline score, no grand total. A fixture
scan, or a scan whose labels and figures disagree, is refused.

### 9. Connecting a store — exact steps

**The client (merchant), in their own Shopify:**

1. *If you already have a custom app made in your Shopify admin (legacy; S1: none can be created any more)* and
   want to use it: in that app's configuration in your Shopify admin (menu names are not verified here), set the
   Admin API access scopes to **only** `read_orders` (and `read_all_orders` if we are to look back more than 60
   days) — no other scope, above all no `write_` scope — save, then generate or reveal the Admin API access token
   (S1: it is shown only once). Send it to Andre through the secure channel he gives you — never by email or chat.
2. *Otherwise (the documented path for new apps, S2/S3):* in the Shopify **Dev Dashboard**, under your own
   organization, create an app; on its version set Admin API access scopes to **only** `read_orders` (+
   `read_all_orders` if needed); release the version and install it on your store, approving the scopes. Send Andre
   the app's **Client ID** and **Client secret** through the secure channel. (UNVERIFIED on shopify.dev that a
   merchant without a Partner account can do this; S3 is a Shopify staff forum answer. If you cannot, tell Andre:
   the alternative is an app ZBM owns installed by OAuth, not built yet.)
3. Agree to ZBM's data-processing terms (Level 1 duties, S11): ZBM reads orders, line items and discount codes to find
   discount-stacking leakage; no customer names, emails, phones or addresses are read; snapshots are kept at most
   the agreed hours in memory; you can stop it at any time by uninstalling the app or removing its scopes.

**Andre, on the detection-py host:**

1. Put each secret in its own file, owned by the service user, `chmod 600`, outside the repo (e.g.
   `/etc/zbm/secrets/rr/<client>.shopify_token`, or `<client>.client_id` + `<client>.client_secret`). Never commit
   them (the repo is public).
2. Add the client to the connections file (mode 600, path in `RR_STORE_CONNECTIONS_FILE`) — this is the approval:
   ```json
   {"version": 1, "connections": [{"client_id": "<zbm client id>", "platform": "shopify",
     "shop": "<handle>.myshopify.com",
     "credential": {"kind": "static_token", "token_file": "/etc/zbm/secrets/rr/<client>.shopify_token"},
     "approval": {"approved_by": "andre", "approved_on": "2026-10-24", "approval_ref": "<contract or ticket ref>"},
     "budget": {"syncs_per_day": 4, "max_orders": 1000, "lookback_days": 60, "snapshot_max_age_hours": 24}}]}
   ```
   (client credentials: `"credential": {"kind": "client_credentials", "client_id_file": "…", "client_secret_file": "…"}`).
3. Give detection-py a ledger: `DETECTION_LEDGER_URL` and `DETECTION_LEDGER_TOKEN_FILE` (a write caller scoped to
   `rr_store` in `LEDGER_CALLERS_FILE`). Without it every sync is `LEDGER_UNAVAILABLE` and nothing is read.
4. Optionally set `RR_STORE_KILL_SWITCH_FILE=/etc/zbm/rr_store.KILL` (touch the file to stop all store reads).
5. Check: `GET /stores/<client>/status` → `connected: true`. Sync: `POST /stores/sync {"client_id": "<client>"}`
   → 200 with `snapshot_id`; a 422 `STORE_TOKEN_HAS_WRITE_SCOPE` / `STORE_TOKEN_EXCESS_SCOPE` means the client must
   untick scopes; 502 `STORE_API_VERSION_MISMATCH` means `2026-10` is no longer served — bump `API_VERSION` after
   reading the release notes.
6. Scan: `POST /revenue-recovery/scan?client_id=<client>` on orchestrator-go; save the JSON; render
   `python3 services/detection-py/devtools/rr_report.py scan.json --md report.md`; review it before sending.
7. To disconnect: remove the entry (the held snapshot is dropped on the next call), delete the secret files, and ask
   the client to uninstall the app or revoke the token.

## Tests and runs

- `services/detection-py/tests/test_store_connector.py`, `tests/test_store_api_report.py` (fake GraphQL over
  `httpx.MockTransport`, injected clock; no network, no wall clock, no /proc): success and cursor pagination,
  truncation, non-advancing cursor, lookback/scope clamp, translation exclusions; THROTTLED with `extensions.cost`
  waits, capped waits and retries, 429, pacing, deadline across waits, MAX_COST_EXCEEDED halving; 401/403/402/423/
  404/302/400, GraphQL codes, bounded 5xx and connect retries; malformed, partial, compressed, wrong-type, deeply
  nested and huge bodies; version fall-forward; unrequested PII fields; shop and currency checks; slow drip; SSRF
  handles and non-public resolution (also through the real transport); scope policy and write-scope refusal before
  any order read; client-credentials exchange; NOT_CONNECTED with no outbound call; kill switch at once and mid-read;
  per-tenant daily budgets; record-first; snapshot kept only after its completion record; failure records;
  snapshot expiry; credential file rules; strict connections file; cross-tenant isolation; token and client secret
  never in logs, reprs, errors, ledger, snapshot, status, API answers or reports; no raw PII stored; the scan path
  through the real routes and the client report. H-1 (Oct 25 2026): the $100 / two 10% codes / 50% automatic case
  (excluded, no figure), partially refunded / refund recorded / reduced-quantity orders (excluded), recorded-allocation
  arithmetic (split across lines, below and above nominal, zero recorded), inconsistent allocations, partly recorded
  orders, ten 64-character codes within the cause limit, ESTIMATED labels and the stated assumption, projections per
  evidence class, and refusal of a scan whose ledger did not verify (L-3). Every new guard was mutation-checked
  (disabled one at a time; each made the suite fail).
- `services/orchestrator-go/cmd/orchestrator/store_scan_test.go` (+ updated E-3 and L2 tests): NOT_CONNECTED and
  other refusals with nothing written, a connected snapshot scanned and recorded with `src=shopify:<id> fixture=0`,
  bad snapshots fail the scan, the fixture scan unchanged; the started record names only covered agents (L5).
- `services/detection-py/devtools/live_run.py`: real ledger-rust, detection-py `serve.py` under a socket audit hook,
  real orchestrator-go — NOT_CONNECTED end to end, record-first refusal, kill switch, fixture scan, ledger valid,
  and no outbound attempt from detection-py. Counts: docs/test-counts.md.

## Limitations

- H-1: orders with an automatic/manual/script discount or any refund are left out rather than attributed, so a store
  that runs automatic discounts alongside codes gets few or no analysed orders. Shopify's exact combination
  arithmetic (S17) was read on help.shopify.com and has not been observed on a store; the figures do not depend on
  it, because they use the recorded allocations.
- No live Shopify call has been made: no token exists. The query shapes were checked against Shopify's Admin schema
  tool (S12), not against a store; the first real sync is the first end-to-end check.
- U2's merchant-created Dev Dashboard path is UNVERIFIED on shopify.dev; OAuth (ZBM-owned app, custom distribution)
  is not built.
- Budgets and snapshots are per process and in memory (a restart resets the day's sync count and drops snapshots;
  two detection-py processes would each have their own). One sync at a time per client and at most
  `RR_STORE_MAX_CONCURRENT_SYNCS` per process.
- Only the discount-misuse agent is fed by this source; seven agents are NOT COVERED by it.
- The approval is a file entry, not a signature (section 3).
