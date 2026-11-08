# Revenue Recovery: first real store connection (Pussycat Alley)

**Who this is for:** Andre. **Time:** about 30 minutes in Shopify, then the Railway steps.
**What it does:** lets Revenue Recovery **read** Pussycat Alley's (PCA's) recent orders, once, so it can look for
discount-code leaks. It can never change anything in the store.

This page is the short version. The full engineering procedure (ledger caller, tunnel, scan, report) is
[docs/deploy/railway.md, "Revenue Recovery: connecting a real store"](../deploy/railway.md#revenue-recovery-connecting-a-real-store).
Where the two differ, railway.md wins for the Railway side; this page wins for the Shopify side (it was checked
against shopify.dev on Nov 8 2026, sources at the bottom).

> **Not part of the Gate 5 launch.** `deploy/gate5/provisioning_inventory.md` keeps the store connector out of that
> launch. Do this only after Gate 5 is done, as its own approved step. Setting `RR_STORE_CONNECTIONS` is your approval.

---

## The rules that never change

- **Read-only.** The app gets one permission: `read_orders`. Detection refuses to read anything if the credential
  has **any** other permission, and refuses any `write_` permission outright.
- **No customer data.** Detection never asks Shopify for names, emails, phones, addresses or IPs.
- **Secrets never go in chat, email, a ticket or a file in the repo.** They go from Shopify straight into your
  password manager, then into a **Sealed** Railway variable. This page only names the variables, never their values.
- **One store, one client.** PCA is registered as client `pca` and nothing else can read it.

---

## Part 1 — In Shopify (create the read-only app)

Shopify no longer lets anyone create the old "custom app made in the admin" (S1). The current way for your own store
is a **Dev Dashboard app**, opened **from PCA's own admin**, so the app lands in PCA's organization (S2, S3).

1. Log in to **PCA's Shopify admin** as the store owner.
2. Go to **Settings → Apps → Develop apps → Build apps in Dev Dashboard**.
   (Same place: click the store name at the top, then **View Dev Dashboard**.)
3. **Create app.** Name it something you will recognise later, e.g. `ZBM Revenue Recovery (read-only)`.
4. Open the app's **Versions** page → **Create version**:
   - **Scopes: `read_orders` — and nothing else.** Do not tick any other box. No `write_` anything.
   - **App URL:** leave the default (this app has no screens).
   - Select **Release**.
5. **Install it on PCA:** on the app, **Install app** (top of the Installs card) → pick the PCA store → **Install**,
   and approve `read_orders`.
6. **Check it is in the same organization as the store.** In the Dev Dashboard, the PCA store should be listed under
   your organization. Shopify: "Owning a store or having it installed doesn't automatically place it in your org"
   (S2). Opening the Dev Dashboard from PCA's admin (step 2) is Shopify's documented route for a merchant's own
   store (S3); whether PCA then shows in the list is **to be confirmed on the day**. If it does not, stop and see
   "If Shopify refuses the app" below.
7. Open the app's **Settings → Credentials**. Copy the **Client ID** and the **Client secret** into your password
   manager. Close the page.

**Why only `read_orders`:** it covers orders from the **last 60 days** (S4), which is exactly what the first run
reads (`lookback_days: 60`). Do **not** add `read_all_orders` for the first run. (Later, for older orders, apps made
in your own organization can add it without Shopify approval (S4); detection accepts it, nothing else.)

**Customer data:** orders count as "protected customer data" at Shopify. For a custom app, level 1 (order data
without name, address, phone, email) is "Always available" (S5). Detection only reads level-1 fields, so there is
nothing to request.

---

## Part 2 — In Railway (where the credentials go)

All on the **detection** service. Variable **names** only:

| Variable | What goes in it | Sealed? |
|---|---|---|
| `RR_STORE_SECRET_1` | the app's **Client ID** | Sealed |
| `RR_STORE_SECRET_2` | the app's **Client secret** | Sealed |
| `RR_STORE_KILL_SWITCH_FILE` | `/tmp/rr-store.KILL` (a path, not a secret) — **required**, or the instant stop below does nothing | no |
| `RR_STORE_CONNECTIONS` | the approval JSON below (file paths only, no secret) — **setting this is your approval** | no secret in it; Sealed is fine |
| `DETECTION_LEDGER_TOKEN` | detection's own ledger token (railway.md steps 6-7) | Sealed |
| `DETECTION_COMPLIANCE_TOKEN`, `DETECTION_COMPLIANCE_CALLER_TOKEN` | the Compliance gate (ADR 0024); client `pca` must also be **activated** at Compliance (railway.md, "Compliance (38): the hard gate") | Sealed |
| `RR_STORE_READS_DISABLED` | leave **unset** to allow reads; `1` = off | no |

The entrypoint turns each of these into a private file at start (`RR_STORE_SECRET_1` →
`/run/zbm-secrets/rr-store-secret-1-file`, and so on); the service reads the files, never the variables.

The approval JSON for `RR_STORE_CONNECTIONS` (replace the two `<...>`; `shop` is PCA's `*.myshopify.com` name,
**not** its custom domain — you see it in the admin URL or under Settings → Domains):

```json
{"version": 1, "connections": [{"client_id": "pca", "platform": "shopify",
  "shop": "<pca-handle>.myshopify.com",
  "credential": {"kind": "client_credentials",
                 "client_id_file": "/run/zbm-secrets/rr-store-secret-1-file",
                 "client_secret_file": "/run/zbm-secrets/rr-store-secret-2-file"},
  "approval": {"approved_by": "andre", "approved_on": "<YYYY-MM-DD>", "approval_ref": "pca-rr-test-1"},
  "budget": {"syncs_per_day": 4, "max_orders": 1000, "lookback_days": 60, "snapshot_max_age_hours": 24}}]}
```

Detection refuses anything that is not a `*.myshopify.com` name (no custom domains, no IPs, no other hosts), so a
typo fails safe with `STORE_CONFIG_INVALID`.

---

## Part 3 — Turn reads on, run the first sync

**Reads are on when all three are true:** `RR_STORE_CONNECTIONS` is set, `RR_STORE_READS_DISABLED` is unset (or
anything but `1`), and the kill file `/tmp/rr-store.KILL` does not exist (it never survives a restart).

Then follow railway.md steps 10-12 (tunnel, status, sync). In short:

1. **Status** (never calls Shopify): `GET /stores/pca/status` → `"connected": true`, `"kill_switch": false`.
2. **Sync** (the only call that reaches Shopify): `POST /stores/sync` with `{"client_id": "pca"}`.

### What a good first sync looks like

HTTP **200** with these fields (numbers here are made up; yours will differ):

```json
{"client_id": "pca", "sync_id": "…", "snapshot_id": "snap-…",
 "orders_read": 37, "orders_analysed": 29,
 "orders_excluded": {"test_order": 3, "non_code_discount_present": 5},
 "truncated_by": null, "pages": 4, "cost_points": 1180.0, "throttle_waits": 0}
```

- `snapshot_id` is present → the read worked and was recorded on the ledger (`rr_store_read_requested`, then
  `rr_store_read_completed`).
- `orders_excluded` is **normal**: test orders, cancelled, refunded or edited orders, and orders with automatic
  discounts are left out on purpose, so no number is overstated.
- `truncated_by: null` → every order from the last 60 days was read. A value (`max_orders`, `cost_budget`,
  `deadline`, `max_pages`) means it stopped early on purpose; the report says how far back it got, and gives no
  yearly projection if that is under 14 days or under a quarter of the 60 days (measured figures only).
- **Few or zero orders is still a pass**: it proves the path. Only the discount-code check is fed by Shopify orders;
  the other seven checks show as NOT COVERED.

Then the scan and the report (railway.md steps 13-14). Review the report yourself before anyone else sees it.

### If it fails

Every failure answers with one `reason` and, since Nov 8 2026, an `upstream` block that says what Shopify answered —
the step (`token`, `preflight`, `orders`), Shopify's HTTP status and Shopify's error codes. Never a secret, never
text from Shopify.

| `reason` | Most likely | Fix |
|---|---|---|
| `STORE_AUTH_REFUSED` with `upstream.stage: "token"` | the app and PCA are not in the same organization, or the Client ID/secret are wrong | redo Part 1 steps 2 and 6-7; then see "If Shopify refuses the app" |
| `STORE_AUTH_REFUSED` at `preflight`/`orders` | the app was uninstalled or the secret revoked | reinstall (Part 1 step 5) |
| `STORE_TOKEN_EXCESS_SCOPE` / `STORE_TOKEN_HAS_WRITE_SCOPE` | the app has more than `read_orders` | new version with `read_orders` only, re-approve it in PCA's admin |
| `STORE_SCOPE_MISSING` | `read_orders` not granted | Part 1 steps 4-5 |
| `STORE_SHOP_UNAVAILABLE` | PCA is frozen (unpaid bill) or locked | fix the store's Shopify account |
| `STORE_RESPONSE_INVALID` | Shopify answered something unexpected; `upstream.graphql_codes` names it if it is a known code (else `OTHER`) | send Claude the `reason` + `upstream` block only |
| `STORE_CLOCK_INVALID` | a bug in detection itself (nothing was sent to Shopify) | send Claude the answer |
| `STORE_API_VERSION_MISMATCH` | the pinned API version (`2026-10`) is no longer served | engineering bumps it (see "Calendar" below) |
| `STORE_CONFIG_INVALID` | the approval JSON is wrong | recheck Part 2 |
| `STORE_CREDENTIAL_UNREADABLE` | a secret variable is missing or empty | recheck Part 2 |
| `LEDGER_UNAVAILABLE` | detection's ledger token not set up | railway.md steps 6-7 |
| `COMPLIANCE_NOT_ACTIVATED` / `COMPLIANCE_UNAVAILABLE` | client `pca` not activated, or Compliance tokens missing | railway.md, "Compliance (38): the hard gate" |
| `STORE_READS_DISABLED` | the kill switch is on | see below |

### If Shopify refuses the app

If PCA does not appear in the app's organization (Part 1 step 6) or the sync says `STORE_AUTH_REFUSED` at the
`token` step, Shopify's answer is that the client credentials method only works for stores in the app's
organization (S2). Do not try to force it. The fallback (custom distribution + a permanent read-only token) is in
railway.md, "If step 4 fails". It needs someone who has done that token exchange before — book it, don't improvise.

---

## Stop it (kill switch)

- **Instantly, no restart:** `railway ssh` into detection, then `touch /tmp/rr-store.KILL`. Every read stops at once,
  including one in the middle of running, and the snapshot can no longer be handed to the scan. It is gone after a
  restart.
- **Durably:** set `RR_STORE_READS_DISABLED` = `1` on detection (this redeploys it). Stays off until you remove it.
- Either way, `GET /stores/pca/status` shows `"kill_switch": true`.

To turn reads back on: delete the file (or redeploy), and/or remove `RR_STORE_READS_DISABLED`.

## Revoke it (disconnect for good)

Do all three, in this order:

1. **Railway:** delete `RR_STORE_CONNECTIONS`, `RR_STORE_SECRET_1`, `RR_STORE_SECRET_2` on detection (it redeploys;
   held snapshots are dropped).
2. **Shopify, PCA admin:** **Settings → Apps** (may be labelled "Apps and sales channels") → the ZBM app →
   **Uninstall**. After that the next sync is refused (`STORE_AUTH_REFUSED`).
3. **Dev Dashboard:** app → **Settings → Credentials** → rotate the client secret and **revoke** the old one
   (S6: once revoked, tokens tied to it "stop working"). If the secret ever leaked, do this step **first**.

Detection never keeps a Shopify access token: it asks for a fresh 24-hour token at each sync (S2) and drops it when
the sync ends, so there is nothing else to clean up.

---

## Calendar

- **API version `2026-10` is accessible until Oct 16, 2027, 15:00 UTC** (S7). Engineering must move detection to a
  newer version well before then. If it is missed, Shopify answers with a newer version and detection refuses it
  (`STORE_API_VERSION_MISMATCH`) — safe, but reads stop.
- Rotate the client secret at least once a year, and whenever someone with access leaves (S6).

---

## Sources (shopify.dev, read Nov 8 2026 through Shopify's docs search and Admin GraphQL schema tools)

- S1 "Generate access tokens for admin-created custom apps": "You can no longer create new admin-created custom apps.
  Existing apps are unaffected and continue to work. For new apps, use Dev Dashboard or Shopify CLI."
  https://shopify.dev/docs/apps/build/authentication-authorization/legacy/admin-custom-apps
- S2 "Authenticate an app for stores in your organization" (client credentials): only works when "the app and the
  store belong to the same Shopify organization"; `shop_not_permitted` otherwise; "Owning a store or having it
  installed doesn't automatically place it in your org"; `expires_in` "Always 86399 (24 hours)". And "Access tokens":
  "Shopify ignores a scope parameter in this request. The token carries all the access scopes that the store granted
  to your app installation" — why detection checks the granted scopes itself before reading.
  https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant ,
  https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens
- S3 "Dev Dashboard": "If you're a merchant, or a developer building for your company's own stores, then open it
  from the Shopify admin ... Settings > Apps, click Develop apps, and then click Build apps in Dev Dashboard. Apps
  that you create there are custom apps for your organization's stores." And "Create apps using the Dev Dashboard"
  (Versions → Scopes → Release; Install app). https://shopify.dev/docs/apps/build/dev-dashboard ,
  https://shopify.dev/docs/apps/build/dev-dashboard/create-apps-using-dev-dashboard
- S4 "Shopify API access scopes": `read_orders` reads "orders created within the last 60 days"; `read_all_orders`
  "doesn't grant access on its own"; Dev Dashboard apps under a merchant organization "don't need to request Shopify
  approval for read_all_orders". https://shopify.dev/docs/api/usage/access-scopes
- S5 "Work with protected customer data": level 1 is "Always available" to custom apps.
  https://shopify.dev/docs/apps/launch/protected-customer-data
- S6 "Manage your app credentials": rotate the client secret (Settings → Credentials → Rotate); after revoking the
  old secret, "Any access tokens still pinned to the old secret stop working."
  https://shopify.dev/docs/apps/build/authentication-authorization/manage-credentials
- S7 "About Shopify API versioning": `2026-10` "Latest stable", accessible until October 16, 2027, 15:00 UTC;
  "Each stable version is supported for a minimum of 12 months"; Shopify "falls forward" from an inaccessible
  version. https://shopify.dev/docs/api/usage/versioning
- Not affecting PCA: the April 1, 2027 cut-off for non-expiring tokens applies to public apps; "Custom apps and apps
  created by merchants are excluded" (shopify.dev changelog, Oct 27 2026).
  https://shopify.dev/docs/apps/build/authentication-authorization/migrate-to-expiring-offline-access-tokens

**Not verified here (needs the live store):** that PCA appears in the app's organization after step 2; the exact
HTTP status Shopify gives for `shop_not_permitted` (detection treats 400/401/403 at the token step as
`STORE_AUTH_REFUSED`); that the `created_at:>='…'` filter returns exactly the 60-day window. Nothing on this page
was run against a real store.
