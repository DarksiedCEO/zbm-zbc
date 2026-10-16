# ADR 0019 — Out-of-Home & DOOH (15): department constitution (OOH-0) and operating spine (OOH-1)

Status: accepted for build, Oct 16 2026, from the founder decisions of Oct 16 2026 and the OOH/DOOH master blueprint
v1 (`claude/ooh-dooh-master-blueprint-v1-2026-10-07.md`) with its hardening directive
(`claude/ooh-hardening-directive-2026-10-07.md`), both in the ZBM/ZBC Backend project. Built on branch
`ooh-dept-15-wave0` from `b759578`. **Not in force.** Waves OOH-0 and OOH-1 only. No agent is built. Nothing here
buys media: there is no vendor connection, and every order is placed by Andre outside the system and recorded here.

**Source caveat.** The blueprint and the directive were not available to this build as files. The sections used
(3, 4, 9, 10, 11, 12, 15, 19, 40, 42, 48, 50, 51, 52, 54, 56) are the ones restated in the build brief and in the
AEGIS round 1 direction, which supplied the section 40 capability names and the section 42 initial cell, and in the
coordinator's message of Oct 16 with the complete section 4 list (main and exception states). The roster and the
state vocabulary now carry the blueprint's names exactly; what this service adds beyond them is marked, with reasons
(decision 9).

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
9. **Campaign states (sections 3-4)**, the complete section 4 vocabulary (`policy.SECTION_4_MAIN`,
   `policy.SECTION_4_EXCEPTION`; `GET /ooh/v1/constitution` lists both):
   - Main, built: `INTAKE → BRIEF_VALIDATED → PLAN_DRAFTED → PLAN_APPROVED (Andre) → AVAILS_REQUESTED →
     QUOTES_VALIDATED → [PROPOSAL_SENT (Andre)] → CLIENT_APPROVED → [FUNDING_REQUIRED →] FUNDS_VERIFIED →
     PURCHASE_AUTHORIZED → [ORDER_SUBMITTED] → BOOKED`. `FUNDING_REQUIRED` is the state a client-approved campaign
     enters when cleared funds cover part of the approved price (the exposure view's `PARTIAL`).
   - Main, defined but NOT_BUILT: `CREATIVE_PENDING → CREATIVE_ACCEPTED → PRODUCTION_PENDING → LIVE →
     DELIVERY_REVIEWED → RECONCILIATION_PENDING → RECONCILED → CLOSED` (the table records the order after BOOKED).
     The OOH-1 placeholders `IN_PRODUCTION` and `COMPLETED` are gone.
   - Main, derived: `HOLD_REQUESTED` and `HOLD_CONFIRMED` are section 4 campaign-level states, but holds are per face
     here (section 15, and a hold may be requested before any money), so they are not stored as the campaign's
     `state`. The campaign view's `hold_state` derives them: `HOLD_CONFIRMED` when every live unit (not withdrawn) is
     `HOLD_CONFIRMED` with an unexpired hold; `HOLD_REQUESTED` when at least one live unit is `HOLD_REQUESTED` or
     `HOLD_CONFIRMED` but not all are confirmed; null otherwise. The purchase gate never reads the derived value: it
     checks every unit of the order itself. Storing them would have put a hold step between FUNDS_VERIFIED and
     PURCHASE_AUTHORIZED, contradicting "a hold may be requested without funds".
   - Exceptions, built: `CLIENT_HOLD`, `VENDOR_HOLD`, `DISPUTED`, `CANCELLED` (and `CHANGE_REQUESTED`, below).
   - Exceptions, defined but NOT_BUILT: `EXPIRED, WITHDRAWN, UNAVAILABLE, SUBSTITUTION_PROPOSED,
     SUBSTITUTION_REQUIRES_APPROVAL, PARTIAL_DELIVERY, UNDERDELIVERY, OUTAGE, MAKEGOOD_PENDING, CREDIT_PENDING,
     REFUND_PENDING, FAILED, BLOCKED`. `EXPIRED`, `WITHDRAWN` and `UNAVAILABLE` also exist as per-unit availability
     states (decision 12), which are built; at campaign level they are not.
   - Every NOT_BUILT state is unreachable: any route asking for one is refused `STATE_NOT_BUILT`, from any state.
   - Added beyond section 4, each for a reason (`policy.BEYOND_SECTION_4`): `PROPOSAL_SENT` (the moment the price
     leaves ZBM: the client's approval binds that exact sell price), `ORDER_SUBMITTED` (the order is with the vendor
     and its answer is unknown, section 51: nothing may be re-sent), `CHANGE_REQUESTED` (the client changes the brief
     after a proposal: approvals and authorizations are voided).
   Ledger event types do not carry state names (`campaign_transition` carries from / to in its payload), so the
   rename changes no event type. The allowed transitions are one explicit table (`policy.CAMPAIGN_ALLOWED`); anything
   else is `TRANSITION_NOT_ALLOWED`. Stale callers are refused (`STATE_STALE`, `VERSION_STALE`: every change bumps
   the campaign's version).
10. **No state is silently promoted.** `CLIENT_APPROVED`, `FUNDING_REQUIRED`, `FUNDS_VERIFIED`,
    `PURCHASE_AUTHORIZED`, `ORDER_SUBMITTED` and `BOOKED` are reached only through their own gate (`TRANSITION_GATED` on the generic route, even for Andre).
    Every state change of a campaign happens in one function (`_move`), called only from a log-replay handler, i.e.
    after the typed evidence and the anchored log line. Guards: `AVAILS_REQUESTED` needs a unit; `QUOTES_VALIDATED`
    needs the commercial model and a current, unexpired sell price; `PROPOSAL_SENT` needs a current sell price.
    `CREATIVE_PENDING` and later, and the unbuilt exception states, are `STATE_NOT_BUILT`.
11. **Exception states.** Entering `CLIENT_HOLD`, `VENDOR_HOLD`, `DISPUTED`, `CHANGE_REQUESTED` or `CANCELLED` voids
    every unsubmitted authorization. Leaving a hold or `DISPUTED` goes back only to the state it came from (`RESUME_TARGET_INVALID`
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
22. **Agent roster (sections 40, 42).** `GET /ooh/v1/roster`: the blueprint's 23 capabilities, names and order
    exactly: 1 Department Coordination, 2 Client Fit, 3 Market/Geography Intelligence, 4 Inventory/Operator
    Intelligence, 5 Avails/Availability, 6 Audience/Mobility Intelligence, 7 Pricing/Transaction Evidence, 8 Planning,
    9 Proposal/Estimation, 10 Procurement Liaison, 11 Contract/Terms Control, 12 Purchase Authorization Control,
    13 Creative Suitability/Specs, 14 Production/Trafficking, 15 Regulatory/Permit Intelligence, 16 Programmatic DOOH,
    17 Proof-of-Posting/Delivery Verification, 18 Outage/Makegood Control, 19 Measurement/Attribution,
    20 Billing/Reconciliation, 21 Vendor Relationship Intelligence, 22 Quality/Error/Agent Performance,
    23 Knowledge/Documentation/Recovery. Every one `built: false`, `build_state: NOT_BUILT_YET`,
    `authority: PREPARE_ONLY`, `can_commit: false`. The section 42 initial cell — 4, 5, 13, 17 and 20 (20 covering
    reconciliation and makegood control, with 18 noted) — is `INITIAL_CELL`; the other eighteen are
    `REGISTERED_INACTIVE_READY`. Mapping from the OOH-1 placeholders: intake → 2, inventory discovery → 4,
    availability and holds → 5, vendor quotes → 7, cash exposure and funds readiness → 12 (no longer in the cell).
23. **Evidence and replay.** Typed evidence first, then the anchored log line, then memory, through the same handlers
    the log replays at start, so live and replayed state cannot diverge (tested: restart and double replay give an
    identical snapshot). `GET /ooh/v1/audit/evidence` marks each event `committed` or `attempted` (R6-M1).

## AEGIS round 1 (Oct 16 2026): fixes

- **H1** One bank transfer, one funds record: the reference is keyed by `funds_ref_key` (NFKC, case-folded,
  separators and whitespace removed, method words `ach, wire, check, cheque, chk, eft, transfer, xfer, txn, trace,
  ref, reference, conf, confirmation` dropped and stripped as a prefix). The key is stored on the record and in the
  log, so replay rebuilds the same registry. A collision only ever refuses; a reference that is nothing but method
  words is refused as invalid.
- **H2** A unit named by more than one quote is refused in a sell price and in an authorization
  (`UNIT_IN_MULTIPLE_QUOTES`).
- **M1** `(tenant, vendor, unit_key)` is unique among live unit records (not withdrawn, campaign not cancelled):
  `UNIT_ALREADY_REGISTERED`. The gate also refuses a face that is on any open order in any campaign of the tenant
  (`DUPLICATE_ORDER_BLOCKED`).
- **M2** The vendor's answer recorded while the campaign is `DISPUTED` updates the order, the units and `resume_to`
  (BOOKED or FUNDS_VERIFIED); the campaign stays `DISPUTED` until Andre resumes it. The outcome route refuses any
  other campaign state.
- **M3** A funds record must move money (amount > 0). After a change request and a new client approval, Andre
  re-confirms the cleared funds already recorded (`POST …/funds-reconfirmations`, the approved amount restated, no
  new reference): only when they cover the new approval, net of open vendor commitments (`FUNDS_INSUFFICIENT`).
- **M4** A rejected order returns its units to `QUOTED` with the hold's vendor reference and expiry cleared: the
  quote can only be bought again after a new hold is requested and confirmed.
- **L1** The personal-data refusal runs before the model and answers `forbidden_field` (now asserted per key and
  per route; the previous assertion was only the status). The check was already effective on the reviewed code;
  the stronger test passes there too.
- **L2** Every 503 carries `maybe`: true when the call may still take effect (a lost ledger answer whose pending line
  the integrity check will roll forward). Never retry such a call with a new request id.
- **L3** service.py's docstring now says ledger writes are made with the service lock held; the CI header counts
  are corrected.
- **L5** `PROPOSAL_SENT` needs a current sell price (a model reset at the ready stage unseats it).

## AEGIS round 2 (Oct 16 2026): fixes

- **R2-H1** Zero padding: every digit run of a funds reference loses its leading zeros (an all-zero run keeps one
  "0"), after one-character runs joined by one same separator are read as one spelled-out token: `000012345`,
  `0-0-0-012345`, `ach:0000012345` and `12345` are one transfer.
- **R2-L1** A method word is removed only as a whole token (or a spelled-out one, `a.c.h`) or as a token's leading
  prefix directly followed by a digit (`ACH12345`); never from inside a word, so `REFUND77`/`UND77`, `CHECKOUT1`/`OUT1`,
  `TRANSFERWISE-889`/`WISE-889`, `WIRELESS5`/`LESS5` and `TRACEY01`/`Y01` are different transfers. The key is
  `<rail>:<identity>`, the rail taken from the structured `method` (ach, eft → ach; check, cheque, chk → check;
  wire → wire). A reference naming another rail than its method is refused `FUNDS_METHOD_CONFLICT`, so
  `BOA-ACH-12345` and `BOA-WIRE-12345` are never merged (and the second cannot be recorded as ACH). The H1 merges
  hold: `BOA-ACH-12345` / `boa-ach-12345`, `12345` / `ach:12345`. Residual: a reference spelled out character by
  character with ONE separator throughout (`b-o-a-a-c-h-1-2-3-4-5`) is read as one token and keeps its method word;
  the normalisation is a guard, not proof — Finance (31) matching against the bank feed is the real control.
- **R2-M1** Andre's vendor registry (`POST /ooh/v1/vendors`, Andre; `GET`, readers including the agent runtime):
  global, not per tenant (ZBM is the only buyer). A vendor id or legal name whose identity key (spelled-out runs
  joined, leetspeak digits and look-alike letters folded, separators and corporate-form words removed) equals any
  registered id or name is refused `VENDOR_EXISTS`: `la-mar`, `lamar-co`, `l4mar`, `lamar-1nc`,
  "Lamar Advertising Co." cannot become a second Lamar. Units, prices and authorizations must name a registered
  vendor id (`VENDOR_UNKNOWN`).
- **R2-M2** Every unit has a flight (`flight_start`, `flight_end`, ISO dates, start ≤ end; `FLIGHT_INVALID`), given at
  registration or set later (`POST …/inventory/{uid}/flight`); it is required before a vendor quote and an
  authorization (`FLIGHT_REQUIRED`). The authorization binds each unit's flight; a change is `INVENTORY_CHANGED`.
  Registration is unique per tenant per face (vendor id, normalised face reference) only among live records whose
  flights overlap (a unit without a flight overlaps everything): a booked face is no longer locked out forever.
- **R2-M3** The face clash moved to the purchase gate and became cross-tenant: an authorization or submission is
  refused `FACE_ALREADY_ORDERED` when the same (vendor id, face) is on any open order (SUBMISSION_UNKNOWN or
  ACCEPTED/BOOKED) of any tenant with an overlapping flight. The body is that code alone: no tenant, campaign,
  order or flight of the other buyer. Proven through the API (two tenants; overlapping refused, adjacent allowed,
  re-checked at submission, freed by a rejection).
- **R2-L3** Every log line carries `fmt` (log format 2). A log of another format refuses start with a reason
  (`StoreCorrupt`: "log line N is log format …; this build reads format 2 only and has no migration"). There is no
  migration: the service is unreleased, so an older data directory is moved aside, not converted.
- **R2-L4** An outcome recorded during a dispute adds a history entry (DISPUTED → DISPUTED with the answer as the
  reason and the new `resume_to`).

## AEGIS round 3 (Oct 16 2026): fixes

- **R3-H1** A unit's flight is bound to what was agreed for it: every vendor quote records the flight of each unit it
  prices (`flights`), and a confirmed hold records the flight it was confirmed for (`hold_flight`).
  - From `PROPOSAL_SENT` on, `POST …/inventory/{uid}/flight` is refused `FLIGHT_LOCKED` (the client approved, and the
    funds cover, that flight); a change goes through `CHANGE_REQUESTED`.
  - In planning, a new flight on a unit that is `AVAILABILITY_REQUESTED`, `QUOTED`, `HOLD_REQUESTED` or
    `HOLD_CONFIRMED` sends it back to `VERIFIED` (vendor reference, hold expiry and hold flight cleared) and retires
    every quote naming it (`retired`, `FLIGHT_CHANGED`). A sell price built on a retired quote stops being current; a
    retired quote cannot price a sell price, satisfy `QUOTED` or be authorized (`QUOTE_RETIRED`).
  - The gate compares, at authorization and again at submission, each quote's recorded flight and the unit's hold
    flight with the unit's current flight (`QUOTE_FLIGHT_MISMATCH`, `HOLD_FLIGHT_MISMATCH`).
- **R3-L flights** A flight that has begun (start before the injected clock's UTC date) is neither quoted nor bought
  (`FLIGHT_IN_PAST`). A flight is at most `policy.FLIGHT_MAX_DAYS` = 366 days (`FLIGHT_INVALID`), so no flight can
  hold a face forever.
- **R3-L vendor over-merge** Andre may register a vendor whose identity key collides with others' when it is a
  genuinely different vendor ("Model Media" / "Mode1 Media"): `distinct_from_ack` names EVERY vendor it collides with
  and a reason from a closed set (`SEPARATE_LEGAL_ENTITY`, `SEPARATE_OPERATOR_VERIFIED`, `NAME_COINCIDENCE_VERIFIED`).
  The registration is recorded as `vendor_registered_distinct_by_andre` with the vendors and the reason. A vendor id
  is never shared, acknowledged or not; an acknowledgement with nothing to acknowledge is refused.
- **R3-L rail** Different rails never merge (R2-L1). When the same reference identity is already recorded on another
  rail, the funds record is allowed and carries a warning `SAME_REFERENCE_OTHER_RAIL` (with the other rails), in the
  response, on the record and in the ledger evidence, so Andre takes a second look.
- **Test gaps** Vendor registration without Andre's token is refused (403); a new flight that clashes with the same
  face's live record is refused (`UNIT_ALREADY_REGISTERED`). Both tests fail if their check is removed.
- **Cosmetic** The old-log refusal message is whole ("log line N has no log format (written before format 2); …").

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
- **War room (ADR 0018):** `devtools/warroom/scenarios/ooh-py.json` with `drivers/ooh_py.py` covers thirteen money and
  authority scenarios, all MUST: vendor-text injection, an amount off the quote, vendor substitution, someone other
  than Andre authorizing, a duplicate submission, and (AEGIS round 1) a respelt funds reference, one face in two
  quotes, a respelt face reference, a vendor answer during a dispute, a funds record of 0.00, and (AEGIS round 2)
  a zero-padded transfer number, a face clash across tenants, vendor spoofing. It does not cover the availability machine, the commercial model
  or the exception states; those are covered by the service's own tests only.

## Open founder decisions

1. (Resolved Oct 16: the complete section 4 list was supplied; decision 9 maps every name.) Whether
   `HOLD_REQUESTED` / `HOLD_CONFIRMED` should become stored campaign states (built: derived from the units).
2. `OOH_MAX_PURCHASE_AMOUNT` default (10000.00) and `OOH_AUTHORITY_MAX_DAYS` (7) are build defaults, not his numbers.
3. Whether a purchase may proceed while principal/agent is `UNDETERMINED` (built: no). His Oct 5 statement was
   "ZBM is principal" for media; the blueprint's default is `UNDETERMINED` until counsel confirms.
4. Whether `QUOTED` without a confirmed hold may ever be bought (built: no; every unit must be `HOLD_CONFIRMED`).
5. Whether a check is acceptable client money for a vendor buy (built: `ach` or `check`; card refused).
6. Whether the client's hub view should ever show cost and fee broken out (built: never cost; the presentation
   choice is recorded for the proposal only).
7. (AEGIS L4) The agent runtime may record `HOLD_CONFIRMED` with a free-text vendor reference and an expiry; nothing
   checks that reference against the vendor. Built as is: a confirmed hold is `VENDOR_ASSERTED`, the gate still needs
   Andre's authorization, and the reference is on the record. Whether a hold confirmation should need Andre (or a
   vendor port) is his decision.
8. (AEGIS M1, superseded by R2-M2) Registration is now unique per tenant per face per FLIGHT; a booked face is free
   again for a flight that does not overlap.
9. (AEGIS R2, founder-confirm) The section 42 cell mapping judgment calls: C04 Inventory/Operator Intelligence is in
   the cell as the owner of the face records and the vendor registry it reads; C13 Creative Suitability/Specs was
   chosen over C14 Production/Trafficking as the cell's production / spec-verification seat (C14 does the
   trafficking itself, later); C20 Billing/Reconciliation carries reconciliation and makegood control in the cell,
   with C18 Outage/Makegood Control noted as its dependency rather than a cell member.
10. (AEGIS R2, founder-confirm) A campaign whose client pays the approved price in ONE cleared payment goes
    CLIENT_APPROVED → FUNDS_VERIFIED directly, never visiting FUNDING_REQUIRED (which is entered only on a partial
    payment). Built that way; the alternative is to always pass through FUNDING_REQUIRED.
11. (AEGIS R2-L1) Wire is not an accepted method for client money (`ach` or `check`, founder Oct 5: vendor money is
    ACH). A wire reference is therefore refused, not recorded under ACH (`FUNDS_METHOD_CONFLICT`). Whether wires
    should be accepted is his decision.

## Unlock list

Finance (31) funds client; vendor ordering port; e-signature; Cybersecurity (22) passkeys for Andre's approvals;
OOH-2 inventory schema and data source; the agents, starting with the initial cell; production and proof of
performance; reconciliation (`ACTUAL_RECONCILED_COST`).
