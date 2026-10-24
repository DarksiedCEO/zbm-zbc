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

(Filled in below as the build lands.)
