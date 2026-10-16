# ADR 0019 — Out-of-Home & DOOH (15): department constitution (OOH-0) and operating spine (OOH-1)

Status: accepted for build, Oct 16 2026, from the founder decisions of Oct 16 2026 and the OOH/DOOH master blueprint
v1 (`claude/ooh-dooh-master-blueprint-v1-2026-10-07.md`) with its hardening directive
(`claude/ooh-hardening-directive-2026-10-07.md`), both in the ZBM/ZBC Backend project. Built on branch
`ooh-dept-15-wave0` from `b759578`. **Not in force.** Waves OOH-0 and OOH-1 only. No agent is built. Nothing here
buys media: there is no vendor connection, and every order is placed by Andre outside the system and recorded here.

**Source caveat.** The blueprint and the directive were not available to this build as files. The sections used
(3, 4, 9, 10, 11, 12, 15, 19, 42, 48, 50, 51, 52, 54, 56) are the ones restated in the build brief. Three things the
brief did not restate were RECONSTRUCTED and are marked `provisional` in `src/policy.py`: the campaign state names
(section 4), the names of the 23 capabilities (the capability map), and which of them form the initial operating
cell (section 42). Reconciling them with the blueprint is open item 1 below; the code keeps them as data so the
reconciliation is an edit to one table, not a redesign.

## Context

Department 15 sells and buys out-of-home media (billboards, transit, street furniture, digital screens). The money
risk is specific: a vendor commitment is binding and usually not cancellable, so a purchase made before the client's
money has cleared — or against a stale quote, the wrong faces, the wrong vendor, or twice after a timeout — is a loss
ZBM carries. OOH-0 writes the department's rules down as enforced data; OOH-1 builds the spine every later wave (the
full inventory schema, agents, proof of performance, reconciliation) will stand on.

## Founder decisions (Oct 16 2026; binding)

1. **Authority.** Andre alone holds the OOH Director / P&L, Buyer and contract-signer roles at launch. Agents prepare
   work and never commit.
2. **Funding.** Clients are 100% prepaid before any purchase. A hold may be requested without funds. Purchase
   authorization is blocked until client funds are verified cleared by Finance (31). Until a Finance client exists in
   this service, funds can be marked verified only through Andre's approval gate, with the evidence reference
   recorded, and the code fails closed.
3. **Pricing.** A disclosed markup, set per deal from evidence. 15% is a suggested starting value only, never applied
   automatically. There is no universal markup.
4. **Agents.** The 23 agents are rebuilt from the blueprint's 23-capability map. No agent is built in these waves; the
   roster is registered as data, `REGISTERED_INACTIVE_READY`, except the initial operating cell.

## Decisions

1. **One service, `services/ooh-py`** (Python; FastAPI, pydantic strict; the locked stack). Ledger department `ooh`,
   event ids `ooh-<abbr>-<40 hex>`, port 8510, env prefix `OOH_`, `X-OOH-Caller-Token`, `X-OOH-Tenant-Token`. The
   record-first plumbing (`store.py` with `DataDirLock` and `StoreWriteError`; `_commit`, integrity, roll-forward,
   `close()`, the R6-M1 `GET /ooh/v1/audit/evidence`), the API limits, the founder gate, `graceful_close.py` and
   `launch_guard.py` are seo-py's (ADR 0017), copied, not reinvented; the two shared modules and the shared
   graceful-close tests are byte-identical (hygiene rule L4).
2. **Fail closed everywhere.** No ledger → no write. Integrity unverified → no write. Every port (Finance 31, vendor
   orders, e-signature, inventory data) is `NOT_CONNECTED`; selecting one by environment (`OOH_FINANCE_URL`,
   `OOH_VENDOR_PORT_URL`, `OOH_ESIGN_URL`, `OOH_INVENTORY_PROVIDER`) refuses start.
3. **Callers** (`config.KNOWN_CALLERS`): `dashboard` (Andre's console; never Andre by itself), `ooh_agent` (the agent
   runtime: prepares campaigns, inventory, estimates, quotes, hold requests), `scheduler` (the integrity job), `hub`
   (a client's read-only view of its own campaign, always with a tenant token; never vendor cost or terms),
   `compliance_38` (evidence, export, integrity; may engage a kill switch).
4. **Andre's gate** (`X-Andre-Approval-Token` through the dashboard, `hmac.compare_digest`; a token equal to any
   other token counts as not configured). Andre-only: creating a tenant; the commercial model; the client sell price
   and the approved buy limit (P&L decisions); sending the proposal; recording the client's approval; verifying
   client funds; authorizing a purchase; submitting (signing) the order and recording the vendor's answer; backing a
   campaign out of approval; leaving a dispute; releasing any kill switch. Moving his approvals to Cybersecurity (22)
   passkeys is on the unlock list.
5. **Principal / agent state (section 10):** `PRINCIPAL | AGENT | UNDETERMINED`, default `UNDETERMINED`, set per deal
   in the commercial model by Andre. A purchase is refused while it is `UNDETERMINED`
   (`PRINCIPAL_AGENT_UNDETERMINED`): who owes the vendor must be settled before ZBM commits to one (open item 3).
6. **Cash exposure (section 11).** Policy `CLIENT_PREPAY_REQUIRED`, the only one. `GET …/exposure` computes, from the
   records (never stored, so it cannot drift): `client_funds_received`, `client_funds_available`,
   `approved_client_commitment`, `vendor_commitment` (every order SUBMITTED-UNKNOWN or ACCEPTED: an order whose answer
   is unknown counts as committed), `authorized_not_submitted`, `production_commitment` (0, not tracked in OOH-1, and
   says so), `cancellation_liability` (null, not tracked, and says so), `unsecured_exposure`, and
   `authorized_exposure_limit` (= cleared client funds: no credit is extended). "No binding vendor media commitment
   beyond authorized financial exposure."
7. **Tenants.** Created by Andre. Every campaign, unit, price object, funds record and authorization names one
   tenant; a tenant-scoped reader asking for another tenant's object gets the same 404 as for a missing one, and an
   id of another tenant's object inside a body is refused exactly like an id that does not exist.
8. **Kill switches**, each with its own reason code and test: `global`, `write`, `tenant:<id>`,
   `capability:<campaigns|inventory|prices|funds|purchase|submission>`. The dashboard, the agent runtime or
   Compliance may engage; only Andre releases. An engage the ledger cannot record takes effect at once in memory and
   is shown as unrecorded. `OOH_KILL_GLOBAL` and `OOH_KILLED_CAPABILITIES` cannot be released at runtime.
9. **Campaign states (sections 3-4; names provisional).** `DRAFT → BRIEF_CONFIRMED → PLANNING → QUOTING →
   PROPOSAL_READY → PROPOSAL_SENT → CLIENT_APPROVED → FUNDS_VERIFIED → PURCHASE_AUTHORIZED → ORDER_SUBMITTED → BOOKED
   → IN_PRODUCTION → LIVE → COMPLETED → RECONCILED → CLOSED`; exception states `ON_HOLD`, `CHANGE_REQUESTED`,
   `DISPUTED`, `CANCELLED`. The allowed transitions are one explicit table (`policy.CAMPAIGN_ALLOWED`); anything
   else is `TRANSITION_NOT_ALLOWED`. Stale callers are refused (`STATE_STALE`, `VERSION_STALE`: every change bumps
   the campaign's version).
10. **No state is silently promoted.** `CLIENT_APPROVED`, `FUNDS_VERIFIED`, `PURCHASE_AUTHORIZED`, `ORDER_SUBMITTED`
    and `BOOKED` are reached only through their own gate (`TRANSITION_GATED` on the generic route, even for Andre).
    Every state change of a campaign happens in one function (`_move`), called only from a log-replay handler, i.e.
    after the typed evidence and the anchored log line. Guards: `QUOTING` needs a unit; `PROPOSAL_READY` needs the
    commercial model and a current, unexpired sell price. `IN_PRODUCTION` and later are `TRANSITION_NOT_IN_WAVE`.
11. **Exception states.** Entering `ON_HOLD`, `DISPUTED`, `CHANGE_REQUESTED` or `CANCELLED` voids every unsubmitted
    authorization. Leaving `ON_HOLD` or `DISPUTED` goes back only to the state it came from (`RESUME_TARGET_INVALID`
    otherwise), and a campaign held at `PURCHASE_AUTHORIZED` resumes at `FUNDS_VERIFIED`: an authorization is never
    restored. `CHANGE_REQUESTED` clears the client approval. An order in flight cannot be held, changed or cancelled
    (its outcome is unknown); it can be disputed.
12. **Availability (section 15).** `IDENTIFIED, VERIFIED, AVAILABILITY_REQUESTED, QUOTED, HOLD_REQUESTED,
    HOLD_CONFIRMED, BOOKING_PENDING, BOOKING_CONFIRMED, UNAVAILABLE, EXPIRED, WITHDRAWN, DISPUTED` with an explicit
    table. Known is not available (`QUOTED` needs a vendor quote naming the unit); a hold requested is not a hold
    confirmed (`HOLD_CONFIRMED` needs the vendor's reference and an expiry in the future, and is recorded
    `VENDOR_ASSERTED`); booking states are reached only through the order flow (`AVAILABILITY_GATED`). The full
    inventory schema is OOH-2.
13. **Price objects (section 19)** are separate records that never stand in for each other: `VENDOR_COST_ESTIMATE`,
    `VENDOR_QUOTE` (vendor, units, `valid_until` required), `NEGOTIATION_TARGET`, `APPROVED_BUY_LIMIT` (Andre),
    `CLIENT_SELL_PRICE` (Andre), `ACTUAL_RECONCILED_COST` (`PRICE_TYPE_NOT_IN_WAVE`). An estimate, a target or a sell
    price named where a quote is required is `PRICE_TYPE_MISMATCH`.
14. **Commercial model (section 9).** Per campaign, Andre: `principal_agent_state`, `pricing_method`
    (`DISCLOSED_MARKUP`), `markup_pct` (required: no default exists), `markup_disclosed` (must be true),
    `markup_basis_ref` (the evidence it was set from), `presentation` (`COST_AND_FEE_BROKEN_OUT` or
    `BLENDED_WITH_MARKUP_DISCLOSED`), `cancellation_terms_ref`; its `terms_id` binds the sell price, the client
    approval and the authorization. A client sell price must equal the quotes it names marked up by that markup,
    to the cent (`MARKUP_MISMATCH`): no hidden margin, and the 15% suggestion is never applied. The model is locked
    once the proposal is out (`COMMERCIAL_MODEL_LOCKED`); a new model unseats a sell price made under the old one.
15. **Client approval** (Andre, at `PROPOSAL_SENT`): binds the current sell price, its quotes, its terms and the
    client's document reference.
16. **Funds** (Andre's gate, at `CLIENT_APPROVED`): amount, currency (must match), method (`ach` or `check`; card is
    refused: vendor money is ACH, founder Oct 5), the bank's transfer reference (never an account number: those keys
    are refused anywhere in a body), and `cleared: true`. One evidence reference funds one campaign
    (`FUNDS_EVIDENCE_REUSED`, across tenants too). Recorded with source `ANDRE_GATE` and evidence class `OBSERVED` —
    never `FINANCIALLY_VERIFIED`, which is reserved for Finance (31). The campaign reaches `FUNDS_VERIFIED` only when
    cleared funds cover the approved client price in full.
17. **Purchase authorization (section 12)**, Andre only: `campaign_id, inventory_ids[], vendor, exact_quote_refs[],
    authorized_amount, authorized_currency, funding_state (computed, never supplied), client_approval_ref,
    commercial_terms_ref, cancellation_terms_ref, signer ("andre"), authority_scope ("OOH_PURCHASE"), expires_at`
    (at most `OOH_AUTHORITY_MAX_DAYS`). Refused, in this order, with one code each: a kill switch; the wrong state
    (`FUNDS_NOT_VERIFIED` while only approved); principal/agent undetermined; signer or scope; authority expired or too
    long; currency; a quote missing, of another type, of another vendor or currency, or expired; the units not exactly
    the quoted units, of another vendor, not `HOLD_CONFIRMED`, or with an expired hold; the amount not exactly the sum
    of the quotes (`QUOTE_ORDER_MISMATCH`), above the approved buy limit (`AMOUNT_EXCEEDS_APPROVAL`) or above
    `OOH_MAX_PURCHASE_AMOUNT` (`SPEND_LIMIT_EXCEEDED`); a client approval that is not the current one or does not
    cover these quotes; changed material terms (`TERMS_CHANGED`); funds not cleared in full, or not covering every
    open vendor commitment plus this one (`FUNDS_INSUFFICIENT`). The authorization binds tenant, campaign, units (with
    their versions), quotes, amount, authority, the campaign version and the expiry (section 50). A new authorization
    supersedes an unsubmitted one. A refusal changes nothing and is recorded on the ledger
    (`purchase_gate_refused`, best effort).
18. **Submission and the retry law (sections 50-51).** `NOT_SUBMITTED → SUBMISSION_UNKNOWN → ACCEPTED | REJECTED`,
    and `DUPLICATE_BLOCKED` for a refused duplicate. Submission is Andre's (channel `MANUAL_BY_ANDRE`: he places the
    signed order with the vendor outside the system), so its result is always `SUBMISSION_UNKNOWN` until he records
    the vendor's answer with its reference (`VENDOR_ASSERTED`). A timeout is never an outcome. The body must restate
    vendor, amount, currency, quotes and campaign version exactly (`VENDOR_MISMATCH`, `AMOUNT_MISMATCH`,
    `QUOTE_ORDER_MISMATCH`, `STALE_VERSION`); anything recorded on the campaign or its units since the authorization
    makes it stale (`STALE_VERSION`, `INVENTORY_CHANGED`); the full gate re-runs. The idempotency key is the SHA-256
    of tenant, campaign, vendor, the sorted quotes, amount, currency and the campaign version; a second submission of
    an open order — the same authorization with a new request id, the same key, or any open order sharing a quote — is
    `DUPLICATE_ORDER_BLOCKED`. The same request id again returns the first answer and never places a second order;
    after a lost ledger answer the pending line is rolled forward by the integrity check, not re-sent. A rejected
    order voids its authorization, returns its units to their pre-order state and the campaign to `FUNDS_VERIFIED`.
19. **External documents are untrusted (section 52).** Vendor text (on a unit or a price) is stored bounded, with
    control and bidi characters removed, marked `untrusted`, and read by nothing; only its SHA-256 reaches the ledger.
    Tested with injected instructions to verify funds, zero the markup, authorize and submit.
20. **Evidence classes (section 54):** `OBSERVED, VENDOR_ASSERTED, ESTIMATED, MODELED, ATTRIBUTED, INCREMENTAL,
    CAUSALLY_SUPPORTED, FINANCIALLY_VERIFIED, UNKNOWN`. The class a record may be created with is fixed per kind
    (`policy.EVIDENCE_ALLOWED`), anything else is `EVIDENCE_CLASS_NOT_ALLOWED`, and no route changes the class of an
    existing record: never silently upgraded.
21. **Decision outputs (section 48):** `RECOMMEND_BUY, NEGOTIATE, REQUEST_HOLD, WAIT, DO_NOT_BUY,
    INSUFFICIENT_EVIDENCE` — what an agent may propose. None is an authorization; no model self-authorizes BUY. (No
    agent exists yet to propose one.)
22. **Agent roster (section 42).** `GET /ooh/v1/roster`: the 23 capabilities as data, every one `built: false`,
    `authority: PREPARE_ONLY`, `can_commit: false`. The initial operating cell (provisional: C01 campaign intake, C04
    inventory discovery, C06 availability and holds, C07 vendor quotes, C12 cash exposure and funds readiness) is
    `INITIAL_CELL_NOT_BUILT`; the other eighteen are `REGISTERED_INACTIVE_READY`.
23. **Evidence and replay.** Typed evidence first, then the anchored log line, then memory, through the same handlers
    the log replays at start, so live and replayed state cannot diverge (tested: restart and double replay give an
    identical snapshot). `GET /ooh/v1/audit/evidence` marks each event `committed` or `attempted` (R6-M1).

## Configuration

Read once at start (`src/config.py`; every variable is also listed in `services/ooh-py/README.md`):
`OOH_SERVICE_TOKEN`, `OOH_CALLER_TOKENS`, `OOH_TENANT_TOKENS`, `OOH_ANDRE_APPROVAL_TOKEN`, `OOH_DATA_DIR`,
`OOH_NON_PRODUCTION` (tests only: an in-memory run), `OOH_KILL_GLOBAL`, `OOH_KILLED_CAPABILITIES`,
`OOH_MAX_PURCHASE_AMOUNT` (default 10000.00), `OOH_AUTHORITY_MAX_DAYS` (default 7), `OOH_INVENTORY_MAX_PER_CAMPAIGN`
(default 200), `OOH_BIND_ADDR`, `OOH_PORT` (8510), the launcher's `OOH_REQUEST_HEAD_TIMEOUT_SECONDS`,
`OOH_KEEP_ALIVE_TIMEOUT_SECONDS`, `OOH_LIMIT_CONCURRENCY`, `OOH_SWITCH_INTERVAL_SECONDS`, `OOH_DRAINS_MAX`, and the
NOT_BUILT names `OOH_FINANCE_URL`, `OOH_VENDOR_PORT_URL`, `OOH_ESIGN_URL`, `OOH_INVENTORY_PROVIDER` (setting any
refuses start).

## What OOH-0 and OOH-1 do NOT do (honest)

- **No agent.** The roster is data. Nothing proposes, researches, negotiates or submits on its own.
- **No Finance (31) client.** Funds are Andre's attestation with a reference, labelled `OBSERVED`. Nothing checks a
  bank. When Finance's client is built, it becomes the `FINANCE_31` source and the only one allowed to produce
  `FINANCIALLY_VERIFIED`.
- **No vendor port, no e-signature.** Orders and signatures happen outside the system; the service records them and
  enforces the gate before Andre is told the order may go out. It cannot stop Andre from ordering by phone without
  it.
- **No full inventory schema** (faces, dimensions, impressions, geometry, DOOH loops and share of voice): OOH-2.
- **No production, installation, go-live, proof of performance, measurement, reconciliation, cancellation
  liability or production commitments.** The states exist; the transitions are `TRANSITION_NOT_IN_WAVE`; the exposure
  view reports those two amounts as not tracked.
- **No refunds.** A cancelled campaign keeps its funds records; returning money is Finance's.
- **One currency (USD).** Anything else is refused.
- **Holds do not lapse by themselves.** An expired hold is caught by the gate at authorization and at submission; no
  job moves it to `EXPIRED`.
- **Re-funding after a change request.** A campaign sent back through `CHANGE_REQUESTED` and re-approved needs a new
  funds verification with a new evidence reference (a bank statement reference will do; the amount may be 0.00 when
  the money already received covers the new price).
- **War room:** see the build report for this branch; not part of this ADR's claims.

## Open founder decisions

1. Reconcile the provisional names with the blueprint: the section 4 campaign states, the 23 capabilities, the
   section 42 initial cell.
2. `OOH_MAX_PURCHASE_AMOUNT` default (10000.00) and `OOH_AUTHORITY_MAX_DAYS` (7) are build defaults, not his numbers.
3. Whether a purchase may proceed while principal/agent is `UNDETERMINED` (built: no). His Oct 5 statement was
   "ZBM is principal" for media; the blueprint's default is `UNDETERMINED` until counsel confirms.
4. Whether `QUOTED` without a confirmed hold may ever be bought (built: no; every unit must be `HOLD_CONFIRMED`).
5. Whether a check is acceptable client money for a vendor buy (built: `ach` or `check`; card refused).
6. Whether the client's hub view should ever show cost and fee broken out (built: never cost; the presentation
   choice is recorded for the proposal only).

## Unlock list

Finance (31) funds client; vendor ordering port; e-signature; Cybersecurity (22) passkeys for Andre's approvals;
OOH-2 inventory schema and data source; the agents, starting with the initial cell; production and proof of
performance; reconciliation (`ACTUAL_RECONCILED_COST`).
