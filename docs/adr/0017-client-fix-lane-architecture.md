# ADR 0017 — Client Delivery & Operations (28): the client-fix lane (`services/clientfix-py`)

Status: accepted for build, Oct 6 2026 (founder Q&A the same day). Built and tested on branch `clientfix-dept-28`
(from integration `5d49ee9`); **not in force**: no connector transport, vault client, OAuth app, model key,
re-detection client or Finance refund contract exists, so every apply answers `CONNECTOR_NOT_WIRED` before anything
is touched, the fire teams answer `MODEL_NOT_WIRED`, nothing is ever counted fixed, and approved refunds stay
`queued`. Wired into CI, `PY_SERVICES` and `docs/test-counts.md` on branch `wire-clientfix-28` at f0a9fc7. AEGIS: four rounds (R1-R2 BLOCKING, R3-R4 NOT BLOCKING); cleared for wiring at b75a77b, R4 Lows closed at fb9de63.

Context: department 28 already has `services/delivery-py` (ADR 0011), the AEGIS fix engine for OUR code. This ADR adds
the lane that fixes CLIENT systems. Inputs: the founder Q&A below; the Sep 27 2026 AEGIS verdict on deer-flow v2.1.0 /
Superpowers v6.4.2 (claude.ai project doc `claude/dept28-thirdparty-audit-verdict-2026-09-27.md`: deer-flow adopted
behind adapters, Superpowers skill texts forked as prompts only, the seam map); the house pattern of bizdev-py and
influencer-py (ADRs 0015 / 0016 and their AEGIS amendments); and the real APIs of detection-py, orchestrator-go,
finance-py, onboarding-py, security-py, sales-py and service-py.

## Founder decisions (Andre, Oct 6 2026 Q&A; binding)

| # | Question | Answer |
|---|---|---|
| 1 | Runtime | deer-flow behind our adapters; the Elastic-2.0 LangGraph "Studio" piece is cut now. Versions pinned; every bump is a re-audit. |
| 2 | Fire teams | Two teams — one of 3 lane specialists, one of 4 — on Claude (Anthropic API). The model key is a NOT_BUILT setting until Andre adds it: the engineer port refuses `MODEL_NOT_WIRED`. |
| 3 | Version-1 scope | All four: store and website settings; tracking and analytics; follow-up automations; listings and reviews. |
| 4 | Access | Official app connections only (OAuth). Never store client passwords: refuse password-shaped fields. Clients can revoke at any time; a revoked connection stops all work for that client immediately. |
| 5 | Sign-off | The client approves the exact fix plan, bound by hash. Andre sees everything. Agents test the fix first (dry run, or a staging / preview copy where the platform offers one); anything that breaks is rolled back automatically. |
| 6 | Proof | After the fix, detection is re-run (detection-py port) and a dated before / after report is produced with evidence. |
| 7 | Refunds | The client pays up front through Stripe (Finance port). Items not fixed, or failed and rolled back, are refunded through the Finance port **with Andre's approval** (Andre token, hash-bound). Payment is kept for items fixed and proven. |

## The deer-flow Studio path (founder decision 1): already cut, recorded here

Checked on this branch, Oct 6 2026:

- `services/delivery-py/pyproject.toml` overrides `langgraph-api`, `langgraph-runtime-inmem` and `langgraph-cli` to
  `sys_platform == 'never'`, so `uv` never installs them (`uv.lock` carries them only under that marker);
- delivery-py's start-up gate refuses to start when `langgraph_api` is importable (`gate.py` `FORBIDDEN_MODULES`), and
  its licence gate lists both packages as forbidden distributions and Elastic-2.0 as a forbidden licence
  (`seed/licence_allowlist.json`);
- delivery-py embeds the deer-flow HARNESS in process (`deerflow.client.DeerFlowClient`, ADR 0011 D1) and never
  installs the gateway (`app/`), which is where the audit found the Studio imports (`app/gateway/app.py`, `deps.py`,
  `health.py`). In the pinned harness itself (`deerflow_harness` 2.1.0, commit `345f08be`) the only mentions of
  `langgraph_runtime` are two comments and one docstring (`runtime/runs/manager.py:2120`,
  `config/reload_boundary.py:47,50,86`) — no import.

So the Elastic-licensed path is on no path we use, and the audit's open item "whether `langgraph-api` is truly
Studio-only on the default path" is moot for us: the harness does not import it and the gateway is not installed.
Nothing had to be removed. This service adds a guard: `tests/test_runtime_boundary.py` fails if anything under
`clientfix-py/src` imports deer-flow, LangGraph, LangChain or a model SDK, or if delivery-py's three Studio guards are
weakened.

## Decisions

1. **One service, `services/clientfix-py`** (Python; FastAPI, pydantic strict). Ledger department `clientfix`, event
   ids `cfx-<abbr>-<40 hex>`, port 8500, `X-CFX-Caller-Token`, env prefix `CFX_`. The house pattern of bizdev-py,
   copied and adapted: `store.py`, `ledger.py`, `clock.py`, `errors.py`, `founder.py`, `money.py`, `serve.py` and the
   commit / integrity / evidence plumbing of `service.py` are bizdev-py's; `graceful_close.py` and `launch_guard.py`
   are byte-identical (hygiene L4). No PII hash key: this department keeps no emails, phones of people or tax data.
2. **Boundaries.** delivery-py keeps the fix engine for our code and owns the agent runtime. Revenue Recovery
   (detection-py via orchestrator-go) finds; this service fixes and asks it to re-detect; Finance (31) takes payment
   and pays refunds; the hub owns the client's login and the OAuth flows; Cybersecurity (22) holds the tokens.
3. **Callers** (`config.KNOWN_CALLERS`): `hub`, `dashboard` (never Andre by itself), `clientfix_agent`, `fire_team`,
   `orchestrator`, `scheduler`, `finance_31`, `compliance_38`. Each has its own token. Andre's token counts only through
   `dashboard`; through any other caller it is refused.
4. **Fire teams run in delivery-py, never here.** The engineers are deer-flow agents inside delivery-py's runtime —
   its `ZbmDockerSandboxProvider` (non-root, default seccomp, digest-pinned image, egress allowlist),
   `ZbmGuardrailProvider` (every tool call a ledger-recorded decision) and `EgressChatModel` with the hand-written
   Anthropic Messages backend. Those adapters are reused, not duplicated: nothing under `clientfix-py/src` imports
   deer-flow or a model SDK (tested). This service gives the team a BRIEF — findings, checks, lanes, connector,
   account, target, the allowlisted op names, never a token or a vault reference — and receives CHANGE SETS, which are
   untrusted data: the same secrets scan, strict model and allowlist validation as a hand-submitted plan. The engineers
   never hold client credentials and never call a client platform. (Amended by AEGIS round 2 R2-4.) A change set
   carries only `{op, target, field, after}`: `before` is never accepted from a model (or anyone) — the service reads
   it from the platform at plan submission, so the model can neither echo nor steer it. The client's own content
   reaches the model only for the checks that cannot be fixed without it (`CONTENT_CHECKS`: a product description, a
   page body), read-only, wrapped as `untrusted_client_content` and labelled UNTRUSTED CLIENT DATA (never
   instructions), secret-shaped values withheld, and read live at most once per item per
   `CFX_BRIEF_READ_INTERVAL_SECONDS` on the service clock. Every link host an op's `after` adds that its `before` did
   not hold is listed in `new_external_hosts` on the item and the job — the client's and Andre's approval screen data
   — and bound into the plan hash.
5. **Connections** (founder decision 4). The hub registers a connection after the client finished an official OAuth
   flow: connector, platform account, granted scopes (the connector's required scopes must all be granted) and a
   `vault:<owner>.<name>` reference (security-py's shape) to the token the hub stored in the vault. No model has a
   field that can hold a token; every body is scanned for password / secret / token keys and credential-shaped values
   (`secrets_guard.py`) and refused 422; the reference is never shown back. Yelp connections carry no reference at
   all (we never hold a Yelp credential of the client's).
6. **Revocation** is never refused for being late, repeated or ill-timed. The kill switch (`revoked_now`) is set before
   the commit; the client's revocation epoch moves, and every running apply of that client stops at its next request
   (founder decision 4 says "stops all work for that client"; ADR reading: all IN-FLIGHT work for the client halts, and
   every not-yet-applied item planned on the revoked connection is cancelled; other connections' future jobs may
   proceed — decision D-6 below). A revoked connection's account can be connected again later.
7. **Tenant isolation.** A connection belongs to one client; an account is bound to at most one ACTIVE connection
   (`ACCOUNT_BOUND_ELSEWHERE`); a vault reference is bound to one connection forever (`TOKEN_REF_IN_USE`). A finding, a
   job, a plan item and an apply each check that the connection is the job's client's (`TENANT_MISMATCH`), with real
   ids on both sides (no `None == None`). Targets that encode their account (GA4 property, GTM container, GBP location,
   Yelp business) must equal the connection's account. The transport is told the connection's account and reference
   and nothing else; a request it carries for one connection can reach only that account.
8. **Flow** (every step one committed, ledger-anchored line): finding (orchestrator) → job + quote (clientfix_agent:
   one client, open findings, money strings) → the client accepts the quote by `quote_sha256` in a hub session →
   Finance's `payment_confirmed` for exactly the quote's amount and hash → fire-team plan → the client approves the
   exact `plan_sha256` in a hub session → apply → verify → re-detection → report → refund decision.
9. **Fire-team assignment** (founder decision 2). `alpha` = 3 specialists (store, tracking, listings); `bravo` = 4
   (store, tracking, automations, listings). A job goes to the smallest team covering all its lanes (D-2).
10. **No work before payment.** Engage, brief, plan and apply are refused `PAYMENT_REQUIRED` until Finance's event is
    on record. Only `payment_confirmed` for exactly the quote's amount and hash on an accepted job pays it; every other
    payment is recorded and never refused bare — see "Every Finance payment path" in the round 3 amendment.
11. **Change sets.** A list of operations, each `{op, target, field, before, after}`: `op` from the connector's
    allowlist, `target` matching the op's target shape, `field` from the op's field list, `before` / `after` the exact
    values (None = absent; only where the op allows a create or a removal), validated by the field's value rule. No
    operation outside the allowlist exists in code. A (connector, account, target, field) key appears at most once in a
    plan. Rich text is refused when it carries a script, frame, form, style, base / meta / link tag, an event handler,
    a `javascript:` / `vbscript:` / `data:text/html` URL or `srcdoc` (D-4).
12. **Connectors only against officially documented APIs** — table below. Anything not verified is NOT_BUILT and has an
    empty allowlist. No connector makes a network call in tests: `tests/platforms.py` fakes answer only the documented
    request shapes (the exact Shopify documents the connector sends were validated against Shopify's live 2026-10
    Admin schema) with documented answer shapes.
13. **Outcomes.** A write is `applied` only on the documented success shape; `refused` on a documented refusal
    (GraphQL `userErrors`, HTTP 4xx except 408); everything else — a timeout, an exception, a 5xx, a 200 with an
    unexpected body, top-level GraphQL `errors` — is `unknown`, never success, and is rolled back as possibly applied.
    A read that is not the documented shape is unknown and stops the item before anything is written.
14. **Ports** (`ports.py`): `Transport` (vault-resolving, egress-allowlisted HTTP to the platforms), `Detection`
    (re-scan), `Finance` (invoice, refund, refund status), `Engineers` (the fire teams). Each stand-in fails closed and
    says so in `/status`; no port is called with the lock held; a port that raises is unavailable / unknown.
15. **The executor** (`executor.py`, founder decision 5) runs ONE item, outside the lock, deterministically: snapshot
    (read every key) → drift (every `before` must equal the snapshot, else `drifted`, nothing written) → guided manual
    (instructions only) → dry run (GBP `validateOnly`; Shopify and GA4 have none: offline validation) → apply each op →
    stage check (GTM `quick_preview` must compile) → finalize (GTM create version + publish) → verify (read back; must
    equal every `after`) → on any failure after the first write: rollback in reverse order from the snapshot (current
    state re-read first, only differing keys written back; GTM re-publishes the previous live version), then the
    rollback is READ BACK against the snapshot. Unproven rollback = `rollback_failed`. Every request that may change
    the platform is recorded on the ledger before it leaves and its answer after; `live()` is asked before every
    request and halts on revocation, a frozen resource or client, or a closed service; a step that cannot be recorded
    halts the item (`interrupted`) — no unrecorded request ever leaves.
16. **Leases and concurrency.** `apply_started` records one lease per client resource (connector | account | target)
    on the ledger; another run touching any leased resource is refused `RESOURCE_LEASED`; the same job twice is
    `APPLY_IN_PROGRESS` / `JOB_STATE`. Leases are released together when the run ends (`apply_finished`), so two items
    of one run touching one resource stay covered throughout (D-7). No lease expires on the wall clock: a run left
    `applying` by a stop is settled by the `recover` tick (`interrupted`, frozen, leases released, task).
17. **Freeze and alert.** `rollback_failed`, `interrupted`, and `halted_revoked` after a write freeze the item's
    resources and open a task for Andre (`ROLLBACK_FAILED`, `APPLY_INTERRUPTED`, `REVOKED_MID_APPLY`). Nothing touches
    a frozen resource (refused before any request) until Andre unfreezes it by its exact `freeze_sha256`. Andre can
    freeze a whole client at any time; a running apply then halts (`halted_frozen`).
18. **Proof** (founder decision 6; delivery-py's rule): `applied_verified` is necessary, not sufficient. Only the
    Detection port answering `cleared` makes an item `fixed_proven`; `present` makes it `not_cleared` with a task
    (`REDETECTION_DISAGREES`); anything else is unknown and counted (in runs, never wall hours) with ONE task after
    `CFX_UNKNOWN_TICKS_BEFORE_TASK` (counting stops there). Andre may end an unprovable item as unfixed by its state
    hash. A guided manual fix counts only when the platform's read shows the required value.
19. **Report.** When every item is terminal, a dated report (service clock) is committed: per item the finding, check,
    lane, status, `fixed`, price, the ops, `before` (the snapshot), `after` (the read-back), failure, dry run,
    rollback, re-detection, and the evidence (ledger event id and log seq of every executor step); the quote, plan and
    payment references; its SHA-256 on the ledger.
20. **Refunds** (founder decision 7). Payment is kept for `fixed_proven` items; the exact sum of every other item's
    price is proposed as a refund (terms bound by `refund_sha256`: refund id, job, client, items, amount, currency,
    Finance's payment event id, report hash) with a task for Andre. Only Andre approves, through the dashboard, with his
    token and the exact hash. The `refunds` tick hands an approved refund to Finance: `refund_sending` is recorded
    first; only `delivered` with a reference is `with_finance`; only `refused` requeues; anything else leaves it
    `sending`, never resent, reconciled through `refund_status`. Not wired: it stays `queued`.
21. **Record first** — bizdev-py's decision 25 and its AEGIS rounds 5-7 unchanged: typed evidence (ids, codes and
    hashes only: no client value, shop, vault reference, session token or amount reaches the ledger) → pending line →
    anchor → append → apply; evidence carries `rk` and `seq` and the line names it; `GET /audit/evidence` marks
    unanchored evidence `attempted`. The data directory flock with a single-use adopt token; `close()` makes the
    instance inert; the ledger's `verify()` runs outside the lock and its verdict is reported as returned.
22. **Client sessions.** The hub opens a session for a client it authenticated; the token is returned once and only its
    SHA-256 is kept; it expires on the service clock. Quote acceptance, plan approval, the hub's job view and a hub
    manual-done report need a live session of exactly that client.
23. **Minimum data.** Business data of the client's (product text, a store's public phone and address) lives in the
    local log only (0700); the audit export replaces every client value by its SHA-256.
24. **Jobs** (`POST /ticks/{name}`, scheduler, one at a time): `apply-queue` (recover first, then approved jobs; with no
    transport each is counted `not_wired`), `manual-verify`, `redetect`, `refunds`, `recover`, `integrity`. Nothing
    depends on the wall-clock hour; everything time-based reads the injected clock.

## Connectors (founder decision 4 and the build brief: verified against official docs, Oct 6 2026)

Shopify was verified with Shopify's own documentation tool (shopify.dev search and live-schema validation of every
GraphQL document the connector sends — all VALID, with the scopes each needs); the others by fetching the official
reference pages.

| Connector | Status | Operations (allowlist) | Dry run | Doc citations |
|---|---|---|---|---|
| `shopify` | **verified** | `shopify.product.update` (title, descriptionHtml, seo.title, seo.description — the two seo fields always travel together); `shopify.page.update` (title, body); `shopify.redirect.set` (create / update a redirect to a same-store relative path); `shopify.metafield.set` (product metafield, simple types, compare-and-swap via `compareDigest`) | none (offline validation) | https://shopify.dev/docs/api/admin-graphql/2026-10 ; …/mutations/productUpdate ; …/mutations/pageUpdate ; …/objects/UrlRedirect ; …/queries/urlRedirects ; …/mutations/metafieldsSet ; https://shopify.dev/docs/apps/build/authentication-authorization/authenticate-standalone-apps ; https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens |
| `ga4` | **verified** | `ga4.key_event.set` (mark / unmark an event as a key event; create or delete only; delete only when `deletable`) | none (offline) | https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.keyEvents (+ `/list`, `/create`, `/delete`) ; https://developers.google.com/identity/protocols/oauth2/web-server |
| `gtm` | **verified** | `gtm.tag.update` (`paused`, `firingTriggerId` only — never a tag's code, type or parameters) in a dedicated run workspace; fingerprint compare-and-swap; `getStatus` must show only the plan's changes; publish; container lease (round 1 H3) | `quick_preview` must compile | https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/accounts.containers.workspaces.tags (`/get`, `/create`, `/update`) ; …/accounts.containers.workspaces/quick_preview ; …/accounts.containers.workspaces/create_version ; …/accounts.containers.versions (`/live`, `/publish`) |
| `gbp` | **verified, gated** (Google must approve the Cloud project: 0 QPM until approved) | `gbp.location.patch` (`phoneNumbers.primaryPhone`, `websiteUri` https only, `storefrontAddress` listed subfields) | `PATCH …?validateOnly=true` | https://developers.google.com/my-business/reference/businessinformation/rest/v1/locations/patch ; …/locations/get ; …/accounts.locations ; https://developers.google.com/my-business/content/prereqs |
| `yelp` | **guided manual** — no usable write API: Fusion Business Details is read-only; the Data Ingestion API that can write phone / address "is reserved for contracted Yelp partners" and "disabled by default" | `yelp.business.set` (`phone` E.164, `location` all seven fields): exact instructions, never a write; verified by a Fusion read with OUR app key | n/a | https://docs.developer.yelp.com/reference/v3_business_info ; https://docs.developer.yelp.com/docs/data-ingestion-api |
| `woocommerce` | NOT_BUILT | — | — | its app flow (`/wc-auth/v1/authorize`) issues long-lived REST API keys, not OAuth tokens (https://woocommerce.github.io/woocommerce-rest-api-docs/#authentication-endpoint); decision 4 says OAuth only |
| `shopify_checkout` | NOT_BUILT | — | — | checkout settings: checkout-extensibility APIs not verified |
| `shopify_theme` | NOT_BUILT | — | — | site speed means theme file writes: too broad to allowlist safely |
| `ad_pixels` | NOT_BUILT | — | — | Meta / TikTok / other pixels: not verified |
| `service_automations` | NOT_BUILT | — | — | service-py has no route that configures a client's follow-up automations (its routes serve our own customers) |
| `sales_automations` | NOT_BUILT | — | — | sales-py has no such route either |
| `crm` | NOT_BUILT | — | — | third-party CRMs: none built |

Every connector is unwired by default: the transport that would carry its requests is NOT_BUILT, so it stays unwired
until Andre adds the OAuth app credentials, the vault client and the transport (unlock list).

## Defaulted decisions (the brief or the founder was silent; the safest option taken)

- **D-1 Quote prices** are set by `clientfix_agent` / the dashboard and accepted by the client by hash; Andre does not
  approve quotes (not in the Q&A). A partial payment never pays the job: exactly the quote or nothing (a mismatched
  payment is recorded and refunded in full on Andre's approval — AEGIS round 3 L3).
- **D-2 Fire-team composition:** `alpha` = store, tracking, listings; `bravo` = all four lanes. Automations go to
  `bravo` (its only home). The founder named the sizes, not the lanes.
- **D-3 Version-1 allowlist is narrow:** no product create / delete, no price or inventory, no theme files, no
  checkout, no GTM tag code or parameters, no GA4 counting-method patch (its update mask was not verified), no
  redirect deletion and no off-store redirect target. Each is a later, separately verified addition.
- **D-4 Rich-text denylist** for product descriptions, page bodies and metafield values (decision 11). The client also
  approves the exact text; the denylist is defence in depth, not the guarantee.
- **D-5 A business listing's phone number is business data**, not personal data: the house rule refusing `phone` keys
  is not applied here (it is in sales-py / bizdev-py, which hold people's numbers). Personal-data keys are refused.
- **D-6 Revocation scope:** "stops all work for that client immediately" is read as: every in-flight apply of that
  client halts at once (any connection), and every unapplied item on the revoked connection is cancelled
  (refundable); later jobs on the client's OTHER connections may run. Andre's client freeze is the stronger stop.
- **D-7 Leases are per run** (released together at the end), not per item.
- **D-8 No rollback after a revocation.** A revoked connection may not be used even to undo; the item is
  `halted_revoked`, the resource frozen when something was written, and Andre gets the exact list of writes.
- **D-9 A re-detection that disagrees** (`present`) does not trigger an automatic rollback of a change that verified:
  it opens a task for Andre and the item is refundable.
- **D-10 Manual-verify mismatch** is counted like an unknown re-detection (the client may not have made the change yet);
  Andre ends it if it never matches.
- **D-11 Read-back mismatch on GBP** (an edit Google holds for review) is treated as a mismatch and rolled back.
- **D-12 Refund amount** is the exact sum of unfixed items' prices (no fees, no proration).
- **D-13 Unpaid cancelled jobs** reopen their findings; paid ones settle into a report and a full refund proposal.
- **D-14 Invoices.** Accepting a quote asks the Finance port for the up-front invoice outside the lock; the answer is
  returned, never stored (Finance's record is the truth). The payment itself is only ever Finance's event.

## How the other departments use it (not wired yet)

| Department | Here | Needed there |
|---|---|---|
| Revenue Recovery (detection-py, orchestrator-go) | `POST /cfx/v1/findings` as `orchestrator`; Detection port `rescan` | a per-client re-scan route returning, per finding, cleared / present |
| Finance (31) | `POST /cfx/v1/finance/events` as `finance_31`; Finance port (invoice, refund, refund status) | an invoice intake for a client-fix quote (Stripe checkout exists: `POST /fin/v1/invoices/{id}/stripe-checkout`); a refund intake for an Andre-approved client-fix refund (today refunds are per campaign: `POST /fin/v1/refunds/{campaign_id}`); a sender of `payment_confirmed` |
| Cybersecurity (22) | connection `token_ref` = `vault:<owner>.<name>` | the hub storing OAuth tokens under owner `delivery_28` (a known security-py caller); the transport reading them through `POST /sec/v1/secrets/{ref}/use` |
| delivery-py (28) | Engineers port | a change-set route that runs a fire team on a brief inside its sandbox / guardrail / egress adapters |
| Hub | connections, revocations, client sessions, approvals | the OAuth app flows, the client login behind a session, relaying platform uninstall webhooks as revocations |
| Service (29-30), Sales (27) | automation connectors (NOT_BUILT) | routes that configure a client's follow-up automations |
| Compliance (38) | audit export, evidence, integrity | — |

## Unlock list (what Andre or another department must provide; each NOT_BUILT switch names its item)

1. **Anthropic API key** for the fire teams (`CFX_ANTHROPIC_API_KEY_REF`), stored in the Cybersecurity (22) vault and
   read by delivery-py's `EgressChatModel` — never by this service.
2. **delivery-py change-set route** for the fire teams (`CFX_DELIVERY_RUNTIME_URL`).
3. **Shopify Partner app** (public app, client id / secret as vault references; `CFX_SHOPIFY_APP_CLIENT_REF`), with the
   eight scopes in the connector, expiring offline tokens.
4. **Google Cloud project** with an OAuth client for GA4 Admin, Tag Manager and Business Profile
   (`CFX_GOOGLE_OAUTH_CLIENT_REF`), and **Business Profile API approval** from Google (`CFX_GBP_API_ACCESS`).
5. **Yelp choice:** a Yelp Fusion plan and app key for verifying guided fixes (`CFX_YELP_API_KEY_REF`) — or a Yelp
   partner contract for the Data Ingestion API (a separate connector and ADR).
6. **Finance refund contract:** finance-py intakes for a client-fix invoice and an Andre-approved client-fix refund, and
   a `payment_confirmed` sender (`CFX_FINANCE_URL`).
7. **Hub client session:** the hub's client login behind `POST /client-sessions`, the OAuth flows that end in
   `POST /connections`, and the revocation relay.
8. **Vault client and connector transport** (`CFX_VAULT_URL`, `CFX_CONNECTOR_TRANSPORT`): token resolution at call
   time, an egress allowlist of exactly the documented API hosts, timeouts.
9. **Re-detection route** in orchestrator-go / detection-py (`CFX_DETECTION_URL`).
10. **Automation routes** in service-py and sales-py (`CFX_SERVICE_AUTOMATIONS_URL`, `CFX_SALES_AUTOMATIONS_URL`);
    third-party CRMs (`CFX_CRM_PROVIDER`); WooCommerce if Andre accepts key-based app connections (`CFX_WOOCOMMERCE`).
11. Andre's approvals by passkey through Cybersecurity (22) instead of `X-Andre-Approval-Token`; caller tokens minted by
    Cybersecurity (22).
12. ~~CI wiring (`ci.yml`, `PY_SERVICES`, `docs/test-counts.md`)~~ (**done** on branch `wire-clientfix-28`, commit
    f0a9fc7) and the console pages (jobs, approvals, frozen resources, refunds, tasks).

## Known limits (accepted)

- Shopify products, pages and redirects have no server-side compare-and-swap: between the snapshot and the write
  another actor can change a field; the read-back then mismatches and the item is rolled back to the snapshot, which
  can undo that actor's change too. Metafields (compareDigest) and GTM tags (fingerprint) are compare-and-swap.
- Rollback is only as good as the platform's read API: a rollback is `proven` only by a read-back equal to the
  snapshot; otherwise the resource is frozen for Andre. GTM rollback re-publishes the previous live version, which
  also undoes anything else published in between.
- The secrets scan and the rich-text denylist are shape rules and can be evaded by deliberate obfuscation; the
  structural guarantees are that no field can hold a token and that the client approves the exact text.
- A plan's values are agent-written; the threshold of harm is bounded by the allowlist and the client's approval of
  the exact before / after values, not by any judgment of the text.
- Leases do not expire on the wall clock; a stuck `applying` job needs the `recover` tick.
- ledger-rust has no filtered read: integrity and the evidence view read the whole shared ledger (as every
  department).
- A process that stops with an anchor in flight AND commits a new line before that anchor lands leaves two anchors for
  one sequence number (security-py's accepted residual, ADR 0012 round 4).

## Settings

All settings, routes and contracts are in `services/clientfix-py/README.md`: `CFX_SERVICE_TOKEN`,
`CFX_CALLER_TOKENS`, `CFX_NON_PRODUCTION`, `CFX_DATA_DIR`, `CFX_ANDRE_APPROVAL_TOKEN`, `CFX_CLIENT_SESSION_MINUTES`,
`CFX_UNKNOWN_TICKS_BEFORE_TASK`, `CFX_MAX_ITEMS_PER_JOB`, `CFX_MAX_OPS_PER_ITEM`, `CFX_BIND_ADDR`, `CFX_PORT`, the
launcher tuning and the NOT_BUILT switches.

## Amendment — AEGIS round 1 (Oct 6 2026, on f9e3a3f): BLOCKING (three Highs), every finding fixed

Regression tests: `services/clientfix-py/tests/test_aegis_r1.py` (every finding-specific test fails on f9e3a3f; the
corpus and safe-text cases are coverage — the vectors round 0's denylist already caught pass there too).

- **H1 stored XSS through the rich-text denylist** (`<svg/onload>`, `<img src=x/onerror>`, entity- or
  whitespace-encoded `javascript:`). Replaced by an ALLOWLIST rebuild (`src/connectors/richtext.py`, standard library
  `html.parser`; `nh3` was not added: it is not installed for the interpreters this repo tests on and the canonical
  rebuild is stricter): fixed tags and attributes, href / src only `https://` or a same-site relative path (or a
  fragment for href), no character reference but the serialiser's own, a strict content model, and a value is accepted
  only when `sanitize(value) == value`. Only AFTER values are held to it; a BEFORE value is the client's own current
  content (bounded string), so a store whose markup is outside the allowlist can still be fixed. Tested against an
  OWASP cheat-sheet cut, mXSS misnesting, SVG / MathML, `data:` URLs and obfuscated schemes. Metafield values are now
  typed (plain text, canonical integer / boolean, safe URL).
- **H2 an `seo.title` fix nulled `seo.description`** (Shopify's `seo` is one SEOInput object). Connectors now declare
  COMPANION keys (`Connector.companions`): a write of one SEO field snapshots both, sends the untouched one with its
  snapshot value, verifies it unchanged and reports it. Rollback re-reads companions and is `conflict` (nothing
  written) when one changed. The audit of every other op: Shopify product title / description, page and redirect
  inputs are partial updates of scalars (safe); metafieldsSet carries the whole value (safe); GA4 delete-then-restore
  dropped `defaultValue` — the whole key-event snapshot is now kept and restored; GTM PUTs the whole tag read from
  the run workspace (safe); GBP `storefrontAddress` replaced the whole address — M3.
- **H3 GTM publish shipped unapproved workspace edits.** Each run creates its OWN workspace (`workspaces.create`) and
  deletes it after; it refuses unless the latest version IS the live one (the base of a new workspace is not
  documented), checks `workspaces.getStatus` before `create_version` and refuses unless the only changes are the
  plan's own tags as `updated` with no merge conflict, re-checks the latest version, and verifies after publishing that
  every OTHER entity of the container equals the snapshot's live version (else the previous live version is
  re-published). The lease is the whole CONTAINER (`Connector.lease_key`). A refused run deletes its workspace through
  the recorded, anchored request path. Target shape is now `accounts/A/containers/C/tags/T`; the connection needs the
  `tagmanager.delete.containers` scope too. Calls verified (Oct 6 2026): workspaces/create, workspaces/getStatus and
  the Entity change statuses (none / added / deleted / updated), workspaces/delete, version_headers/latest and
  ContainerVersionHeader, versions/live, create_version (syncStatus), all under
  https://developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/.
- **M1** a change set is bound to its finding: every op's target must be the finding's resource
  (`RESOURCE_MISMATCH`) and each check has an allowlist of ops and fields (`catalogue.CHECK_OPS`, `OP_NOT_FOR_CHECK`).
- **M2** redirects: no backslash, no `%2f` / `%5c` / control escape in any case, nothing a browser resolves off the
  store, no redirect to itself; chains and loops across the plan are refused (`REDIRECT_CHAIN`), and at apply time a
  chain with the store's existing redirects (read through `urlRedirects(query: "path:…" / "target:…")`) refuses the
  item before any write.
- **M3** GBP address: the subfields outside the allowlist are a companion snapshot, merged into every address write
  and verified unchanged. The other masks (`phoneNumbers.primaryPhone`, `websiteUri`) name one scalar each.
- **M4** a per-client kill switch (`revoked_clients_now`) is set BEFORE the revocation commit and checked by `live()`
  for every connection of that client and by apply's preflight; it is cleared once the revocation is committed (from
  then on the revocation epoch stops runs that started earlier).
- **M5** Shopify (every connector on the default rollback): a key is written back only when it still holds OUR value;
  any other value is left alone and the rollback is `conflict` — not proven, resource frozen, Andre alerted.
- **M6** proto3 JSON: a missing `compilerError`, `syncError`, `deleted` or `paused` is false and a missing list empty
  (https://protobuf.dev/programming-guides/json/).
- **Lows.** A freeze in the middle of a write now triggers a guarded rollback (revocation still stops it), freezes the
  resource and opens `FROZEN_MID_APPLY`; a halt freezes only when a write was actually sent. A payment for a job
  already closed unpaid is recorded and becomes a full refund proposal with an `ORPHANED_PAYMENT` task for Andre. The
  fire-team brief carries each item's current values (read-only through the connector; any value the secrets scan
  flags is withheld). Unsalted hashes stay (house pattern).

## Amendment — AEGIS round 2 (Oct 6 2026, on 05cb5e6): BLOCKING (one High), every item fixed

Regression tests: `services/clientfix-py/tests/test_aegis_r2.py` (17 of 18 fail on 05cb5e6; the 18th checks the test
transport's own delete guard).

- **R2-1 (High) a GTM rollback after publishing left our version as the container's latest**, so the next workspace
  (based on the latest version; workspaces/sync "syncs a workspace to the latest container version") carried the
  rolled-back change and later runs were refused `base_not_live`. `create_version` "sets the base container version to
  the newly created version" (workspaces/create_version) and the latest header stays ours after re-publishing the old
  version (version_headers/latest). The rollback now builds a REVERT version through a fresh run workspace (the planned
  fields back to their snapshot values, `getStatus` showing only those, `create_version`, `publish`) and proves
  latest == live and the live content == the snapshot's (versions/live, version_headers/latest). A lost
  `create_version` answer is resolved by reading the latest version (versions/get: ours only if it is exactly the
  snapshot plus the planned values). If the end state cannot be reached the item is `rollback_failed`, the container
  is frozen and a `GTM_VERSION_POISONED` task names our version (`ref`).
- **R2-2** the rollback re-publishes the snapshot version only when the live version is OUR created version; anyone
  else's release (or newer draft) is a `conflict` — `rollback_failed`, container frozen, task.
- **R2-3** the per-client kill switch is a SET of pending revocation ids per client (`pending_revocations`); it clears
  for a connection only when that revocation is committed (live, replayed or rolled forward) and for the client only
  when the set is empty; the repeat and replay paths never clear another revocation's entry.
- **R2-4** prompt injection through `before` values: decision 4 above. Plans with a `before` are refused 422 (from the
  hub, the dashboard and an engineer proposal alike); with no transport a plan cannot be submitted at all
  (`CONNECTOR_NOT_WIRED`); an `after` equal to the store's current value is `OP_NO_CHANGE`; unreadable state is
  `503 STATE_UNREADABLE`.
- **R2-5** run workspaces have unique names (`zbm-clientfix-run-<role>-<item>-<random>`); a lost `workspaces.create`
  answer lists the container's workspaces (workspaces/list) and deletes ours by exact name; the `recover` tick (run first
  inside every `apply-queue`; the scheduler also runs it once after each start — start-up itself does no platform I/O)
  reaps orphaned run workspaces of every unleased, unfrozen GTM container, every DELETE recorded on the ledger before
  it leaves. *Narrowed by round 3 M3 (below):* the reaper deletes ONLY a workspace whose exact generated name was
  recorded on the ledger BEFORE the create request, of a run that has settled, and only when `getStatus` shows no
  change; the name prefix alone selects nothing.
- **R2-6** every payment a job cannot take (closed unpaid, or already paid) is recorded and gets its own refund proposal
  (`kind: orphaned_payment`, one per Finance event) and an `ORPHANED_PAYMENT` task; such a refund never closes the job.
- **R2-7** checks whose fields are named (a GA4 key event, a Shopify metafield) bind the finding to ONE exact field
  (`resource.field`, required for them and refused for the others); a plan may only touch that field.
- **Accepted risk: the `tagmanager.delete.containers` scope.** The client's vault token for GTM holds it (Google
  grants no narrower scope for deleting a workspace). The code sends exactly one kind of DELETE: a workspace of the
  connected container that a run created (`TagManagerConnector._delete` refuses any other path; the reaper selects
  only names recorded on the ledger before their create request — round 3 M3). The test transport refuses any GTM DELETE that is not a run workspace,
  so every test that sent one would fail; a direct test proves the guard fires.
- **`/brief` rate limit:** decision 4 above (per item, configurable, on the injected clock; memory only).

## Amendment — AEGIS round 3 (Oct 6 2026, on 407d2db): not blocking; GTM stays unwired until M1–M3 were fixed

Regression tests: `services/clientfix-py/tests/test_aegis_r3.py` (16 of 17 fail on 407d2db; the 17th is the positive
half of M2: our own exact version is still recognised). Docs re-fetched Oct 6 2026:
developers.google.com/tag-platform/tag-manager/api/reference/rest/v2/ accounts.containers.workspaces/create_version,
accounts.containers.versions/publish, accounts.containers.versions/get, accounts.containers.workspaces/getStatus.

- **M1 `create_version` syncs to the latest container version.** Its `syncStatus` is "Whether version creation failed
  when syncing the workspace to the latest container version": a version the client created after our last check is
  merged into ours. Before publishing, the returned `containerVersion` ("The container version created.") is compared
  with the EXACT expected content — the snapshot plus the planned values going forward, the snapshot alone for a
  revert — and our exact name. On any difference nothing is published, the outcome is UNKNOWN and
  `poisoned_version` names the version (`rollback_failed`, container frozen, `GTM_VERSION_POISONED` task with `ref`).
  A revert is never built on a version that is not known clean (content-checked, or identified by M2).
- **M2 a version is ours only by exact name AND exact values.** Each `create_version` carries a unique name
  (`zbm-clientfix <item> <random>`); a lost answer is resolved through versions.get, and the latest version is ours
  only when its `name` equals the name we sent AND every planned field equals our `after` AND nothing else differs
  from the snapshot. Anything else is someone else's: `conflict`, left alone (never un-published, never reverted).
- **M3 the reaper.** The generated name of every run workspace is recorded on the ledger BEFORE the create request
  (`HttpRequest.note` on the recorded `request_sending` step; indexed as `run_workspaces` on replay). The reaper only
  considers a workspace whose exact name is on that index, for the same client, of a run that has SETTLED (its item is
  no longer `planned` and its job is not running). Before deleting it asks `getStatus`
  (accounts.containers.workspaces/getStatus): any change or merge conflict means someone worked in it — it is HELD,
  never deleted, and a `GTM_RUN_WORKSPACE_CHANGED` task (`ref` = the workspace path) is opened for Andre once.
- **L1** the reaper holds the container while it works (an apply is refused `RESOURCE_LEASED` meanwhile) and re-checks
  lease, freeze, revocation and shutdown under the lock immediately before each DELETE (and before every request).
- **L2** a halt (revocation, shutdown) in the middle of a revert still copies `poisoned_version` into the result, so
  the `GTM_VERSION_POISONED` task names it.
- **L3** a payment whose amount or quote hash does not match goes through the orphaned-payment path: recorded, the job
  not paid by it, a full refund proposed (`reason: payment_mismatch`) and an `ORPHANED_PAYMENT` task.
- **L4 accepted risk: there is no compare-and-swap on the GTM live version.** versions.publish takes only the
  fingerprint of the version being published ("must match the fingerprint of the container version in storage"); it
  has no precondition on the version currently live. Between the rollback reading "live is ours" and re-publishing the
  snapshot, a release the client publishes can be un-published by us. It cannot be prevented through the API; it is
  DETECTED only in part: after the re-publish the latest version is read, and a version newer than ours makes the
  rollback a `conflict` (`rollback_failed`, container frozen) with a `GTM_RELEASE_MAY_BE_UNPUBLISHED` task naming
  that version (`ref`) so Andre re-publishes it if it was live. *Corrected in round 4 (Info 4):* this detection covers
  only a client release that CREATES a new version. A client who re-publishes an existing older version inside the
  window creates no new version, leaves the latest header unchanged, and is NOT detected — our re-publish of the
  snapshot silently replaces theirs. The window is two API calls long (read live, publish) and only exists during the
  rollback of a failed run; this part is accepted as is.
- **L5** `/brief` checks the kill switch (`revoked_now`, `revoked_clients_now`) and the connection's status before
  using its cache; a revocation (even one whose commit failed) drops every cached read of that client's items.
- **Every Finance payment path (round 3 follow-up).** `POST /finance/events` (caller `finance_31`, the only route that
  receives a Finance payment event; `PaymentEvent`, kind `payment_confirmed`) is the one entry point — Finance's refund
  answers come back through the port's `request_refund` / `refund_status`, never as a payment. Every outcome:
  | Case | Answer | Record |
  |---|---|---|
  | accepted job, exact amount and quote hash, USD | 200, job `paid` | `payment_confirmed` |
  | job still `quoted` (quote not accepted) | 200, job unpaid | `payment_orphaned` + full refund proposal (`reason: quote_not_accepted`) + `ORPHANED_PAYMENT` task |
  | amount or quote hash differs | 200, job unpaid | same, `reason: payment_mismatch` |
  | currency other than USD (three capital letters, ISO 4217 shape) | 200, job unpaid | same, `reason: currency_not_supported` |
  | job already paid, or closed unpaid | 200 | same, `reason: job_cannot_take_payment` |
  | job id unknown | 404 `JOB_NOT_FOUND` with `recorded: true` and the `refund_id` | same, `reason: job_not_found` (`client_id` null, no items) |
  | the same Finance event id again, same facts | the first answer | none needed (already recorded) |
  | the same Finance event id with OTHER facts | 409 `FINANCE_EVENT_REUSED` with `recorded: true` | `finance_event_conflict` (both fact hashes) + `FINANCE_EVENT_CONFLICT` task, once per distinct fact set; no automatic refund — which event is the money is ambiguous, Andre decides |
  | body fails the schema (a float, a missing field, a secret-shaped value, a currency that is not `^[A-Z]{3}$`) | 422 | `finance_event_malformed` with ONLY the keyed HMAC of the canonical body (round 4 Info 1) + `FINANCE_EVENT_MALFORMED` task, once per body; beyond `CFX_MALFORMED_TASKS_MAX` open ones (default 5) further bodies roll into one `FINANCE_EVENT_MALFORMED_DIGEST` task with a count (round 4 L3) |
  | service closed, integrity unverified, or the ledger cannot take the line | 503 | nothing could be written; nothing was accepted either: Finance retries the same event id (idempotent) |
  | caller is not `finance_31` | 403 | not a Finance payment event (unauthenticated input is never written to the ledger) |
  `REQUEST_ID_REUSED` cannot occur on this route: the request key is scoped by the Finance event id, and an event id
  already recorded is answered by the replay / conflict rows above before the request key is consulted.
- **Info** every punycode host in `new_external_hosts` is shown next to its Unicode form in
  `new_external_hosts_detail` (job and item views) with a `confusable` flag (mixed scripts, a Latin-lookalike script,
  or undecodable punycode), so the client approving the plan sees where the link really points.

## Amendment — AEGIS round 4 (Oct 6 2026, on b75a77b): not blocking, cleared for wiring; Lows and Infos closed

Regression tests: `services/clientfix-py/tests/test_aegis_r4.py` (18 of 19 fail on b75a77b; the 19th is the control
that a well-formed non-USD payment is still recorded and refunded).

- **L1** the live run selects the refund it checks by `kind == "unfixed"` and its job id, never by list position (the
  orphaned refund of the mismatched test payment sorts anywhere: refund ids are hashes). Run five times in a row.
- **L2** `PaymentEvent.currency` must match `^[A-Z]{3}$`. Anything else is a schema failure: recorded only as a keyed
  digest (never the text), so free text can no longer reach the log, the refund terms or Finance.
- **L3** at most `CFX_MALFORMED_TASKS_MAX` (default 5, 1..1000) open `FINANCE_EVENT_MALFORMED` tasks; every further
  distinct body is still one ledger line but rolls into ONE open `FINANCE_EVENT_MALFORMED_DIGEST` task whose `count`
  grows; once Andre closes it, the next overflow opens a new one.
- **Info 1** the malformed-body digest is HMAC-SHA256 under a key derived (domain-separated,
  `clientfix-py finance-malformed-body v1`) from the service's own secret `CFX_SERVICE_TOKEN`: this department has no
  separate hash-key file, and the token never leaves the process. A low-entropy secret inside a known-shape body can
  no longer be brute-forced from the ledger. Rotating the token changes future digests only.
- **Info 2** a `ROLLBACK_FAILED` (or other freeze) task names the foreign version that made the rollback a
  `conflict` (`ref`) and every run workspace this run created that may still exist (`run_workspaces`: not deleted
  and not consumed, or handed to a `create_version` whose outcome is unknown). The reaper still treats it under M3.
- **Info 3** `new_external_hosts_detail` flags a punycode host by UTS #46 / IDNA2008 (the `idna` library, BSD-3,
  already httpx's dependency, now pinned): `idna_invalid` (`check_label`: fullwidth and mathematical letters are
  disallowed code points), `not_uts46_mapped` (`uts46_remap`, non-transitional, STD3, changes the label),
  `ascii_lookalike` (the NFKC case-folded skeleton is ASCII), `mixed_script`, `lookalike_script` (Cyrillic, Greek,
  Armenian, Cherokee, Coptic letters), `undecodable`. `ｇｏｏｇｌｅ`, `𝐠𝐨𝐨𝐠𝐥𝐞`, `аpple`, `аррӏе` are flagged; `bücher`,
  `café`, `中国` are not.
- **Info 4** the L4 text above is corrected (an old version re-published by the client inside the window is not
  detected). **Accepted risk: the reaper's getStatus→DELETE window.** workspaces.delete has no precondition (no
  fingerprint), so between getStatus and the DELETE there is a one-request window in which someone could start
  editing the orphaned run workspace; that edit would be deleted with it. The reaper holds the container and re-checks
  lease, freeze and revocation under the lock right before the DELETE (round 3 L1), which closes the window for this
  service's own runs, but not for a person working in the GTM UI. It is accepted: the workspace is one this service
  created, recorded and abandoned, with no change in it a request earlier.
