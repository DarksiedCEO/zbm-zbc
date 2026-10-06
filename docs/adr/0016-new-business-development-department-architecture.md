# ADR 0016 — New Business Development (12): big pursuits and partnerships for ZBM and ZBC

Status: accepted for build, Oct 6 2026 (founder Q&A the same day). Not in force: no email or submission provider,
no bid source, and no Onboarding, Finance (31) or Legal (37) client is built, so outreach, submissions and payouts
stay queued, won hand-offs stay `pending_delivery`, and agreements are refused `LEGAL_UNAVAILABLE`.

## Founder decisions (Q&A, Oct 6 2026; binding)

| Question | Answer |
|---|---|
| Scope | **Both.** (a) Big pursuits: enterprise and government bids, RFP / RFQ responses, formal pitches, multi-month pursuits with stages, deadlines, bid / no-bid qualification and win / loss. (b) Partnerships: referral partners, agency alliances, white-label deals. Sales (27) keeps everyday leads and deals. A won pursuit hands off to Onboarding and Finance as `pending_delivery`, as sales-py does. Influencer and co-marketing work is department 11 (built in parallel), not here. |
| Brands | **Both**, ZBM and ZBC. |
| Pitches and RFP responses | The AI drafts; **Andre approves each one** by content hash with his approval token; any change after approval invalidates it. Responses are assembled only from approved boilerplate blocks plus Andre-approved custom text. Submission goes through a NOT_BUILT port, so approved submissions stay queued. Bid deadlines are tracked; a submission past its deadline is refused, judged on stored deadline data and an injected clock. |
| Government bids | Fail closed. Public-sector procurement bids are here (department 14 keeps political work and public affairs). Required certifications and representations are checklist items **Andre must attest**; nothing is ever auto-attested; conflict-of-interest and gift / lobbying-sensitive items are flagged to Andre. No bid-portal fetching code: source fetching is a NOT_BUILT port. |
| Partner commissions | A percentage of the won deal, **paid only after the client has actually paid** (a Finance payment event through a port). Andre approves each partner's rate. Payouts go through a Finance (31) port, never Stripe directly. Partner tax info as references or tokens only; a raw TIN / SSN / EIN is refused 422. Nothing is actually paid (fixtures only). Decimal-exact, strings, floats refused, finance-py's rounding. A refund or chargeback claws back unpaid commission. |
| Legal | Partner agreements, NDAs and white-label contracts go through a Legal (37) hand-off, like service-py's `legal_client`; while Legal is a stand-in, sending is refused `LEGAL_UNAVAILABLE`. |
| Outreach | Email only, from Andre-approved templates matched by hash; CAN-SPAM, suppression shared across both brands, as in sales-py; a reply holds further automatic outreach until Andre decides; no texts or calls. |
| Deal approval | Any pursuit or partnership deal value **over $10,000** goes to Andre, **aggregated** so that splitting a deal does not get around the threshold. Every pitch needs his approval regardless. |

## Decisions

1. **One service, `services/bizdev-py`** (Python; FastAPI, pydantic strict). Ledger department `bizdev`, event ids
   `nb-<abbr>-<40 hex>`, port 8490, `X-NBD-Caller-Token`, env prefix `NBD_`. The house pattern of sales-py (26-27)
   and service-py (29-30) and ADRs 0012-0014, copied and adapted, not reinvented.
2. **Boundaries.** Sales (27) keeps everyday leads, deals and its own outreach; department 11 owns influencer and
   co-marketing; department 14 owns political work. Nothing here calls Sales.
3. **Callers** (`config.KNOWN_CALLERS`): `dashboard` (Andre's console; never Andre by itself), `bizdev_agent` (the
   agent runtime that drafts), `scheduler`, `provider_events` (email webhooks relay), `hub` (unsubscribe link relay),
   `finance_31` (client-money events, payout confirmations), `compliance_38` (audit reads). Each has its own token.
4. **Andre.** His actions arrive through `dashboard` with `X-Andre-Approval-Token` (`NBD_ANDRE_APPROVAL_TOKEN`,
   legal-py's FounderGate; a token equal to the service or any caller token is "not configured"; the header through
   any other caller is refused). Andre alone: the `bid` decision, deadline moves, deal approvals, checklist
   attestations, block / response / template approvals and retirements, winning a pursuit or partner deal,
   withdrawing a pursuit, approving a rate, setting a payee, hold decisions and closing review tasks.
5. **Thirteen deterministic intelligences** (`src/intelligences/`, `GET /intelligences`): i01 bid qualification,
   i02 identity, i03 deadline guard, i04 response assembly, i05 government checklist, i06 sensitivity flags, i07 deal
   threshold, i08 template guard, i09 reply classifier, i10 suppression, i11 commission calculator, i12 tax reference
   guard, i13 audit export. No model calls; none writes state.
6. **Both brands.** Pursuits, responses, templates, contacts and partner deals carry `zbm` or `zbc`; boilerplate may be
   `both`; a partner lists the brands it works with and a partner deal's brand must be one of them.
7. **Pursuits.** Kinds `rfp`, `rfq`, `enterprise_bid`, `government_bid`, `formal_pitch`. Stages `identified ->
   qualifying -> responding -> submitted -> won | lost`; `qualifying -> no_bid`; any open stage -> `withdrawn` (Andre).
   A pursuit records its counterparty (ref, name, domain), value (money string), deadline, notes and source ref.
8. **Qualification and the bid decision.** The agent answers seven fixed criteria (`yes` / `no` / `unknown`); i01
   recommends: a `no` on a must-have (scope fit, capacity, deadline feasible, compliance feasible) is `no_bid`, an
   `unknown` on one is `needs_andre`, otherwise `bid` needs five yeses. **`bid` is always Andre's** and names the
   exact qualification hash he saw (a re-qualification makes it stale); `no_bid` may be recorded by the agent.
9. **Counterparty identity.** Three keys per deal: the caller's ref, the registrable domain (sales-py's rule plus
   common `.gov` two-level suffixes; every domain and email domain is IDNA-encoded first, so `münchen.de` is
   `xn--mnchen-3ya.de`, AEGIS round 2 L4) and the organisation name normalised by i02 (NFKC, accents dropped, Cyrillic /
   Greek lookalikes folded with sales-py's confusables table, legal suffixes such as Inc / LLC / Incorporated / The
   dropped).
10. **Responses and pitches.** Boilerplate blocks and responses are versioned and **immutable**: a change is a new
    version, so any change after an approval is a different content hash that no approval binds. Andre approves a
    block version by its hash; a response version (block parts at approved versions plus custom text) by its hash,
    naming **exactly** the sensitivity flags it raises (i06; no more, no fewer). i04 assembles only from approved,
    unedited blocks of the pursuit's brand (or `both`); a retired block cancels any queued submission citing it.
    A new response version supersedes the old (its queued submission is cancelled). Every pitch is a response and so
    always needs Andre's approval, whatever its value.
11. **Deadlines and submission.** A deadline is required for every kind but a formal pitch, must carry a UTC offset,
    must be in the future when set, and is moved only by Andre. Submission requires the CURRENT version, approved,
    its hash matching what was approved AND what the current blocks re-assemble to, a `bid` decision, a deadline
    not passed, a complete government checklist and the deal gate. The deadline is judged on the STORED deadline and
    the service's injected clock (`i03_deadline.passed(deadline, service.now())`; `now >= deadline` is late), never
    on request data or the wall clock. A submission is queued (typed `submission_queued` event); the
    `submission-queue` job re-checks every gate from current state, cancels on a hard failure (deadline passed,
    superseded, hash mismatch, a block retired, an addendum item not yet attested, closed), holds while only the
    deal gate is open, and — the port not being wired —
    leaves it `queued`. With a port, `submission_sending` is recorded before the call, the outcome after. Only
    `accepted` with a provider reference is `submitted`, and only an explicit `refused` ends the attempt (the response
    may then be submitted again). A timeout, an exception or any other answer is an UNKNOWN outcome: the submission
    stays `sending` — never `failed`, never resent, never resubmittable (it may have been delivered) — and each run
    reconciles it through the port's `submission_status` (the stand-in answers `unknown`). If its deadline passes
    while it is still unknown, the queue job or `deadline-sweep` opens ONE task for Andre
    (`SUBMISSION_OUTCOME_UNKNOWN`); it is never auto-failed. (AEGIS round 1 follow-up.) Every unknown run is counted
    durably; after `NBD_UNKNOWN_TICKS_BEFORE_TASK` of them (default 6, counted in job runs, never wall hours) one
    `SUBMISSION_STUCK` task opens, deadline or not, and Andre settles it at `POST /submissions/{id}/reconcile`, naming
    its exact `state_sha256`: `delivered` makes it `submitted`; `not_delivered` makes the response resubmittable
    (AEGIS round 2 N2). `deadline-sweep` cancels late queued submissions and
    opens one task per pursuit whose deadline passed.
12. **Government bids (fail closed).** Every government bid carries i05's baseline items (SAM registration,
    debarment / suspension, independent price determination, authority to bind, conflict of interest, gifts and
    gratuities, lobbying, contingent fees) plus requested items from a closed catalogue or `custom` items with a
    label; an addendum may ADD items (never remove or edit) until submission. Each item has a hash binding pursuit,
    code and label. **Only Andre attests, one item at a time, by its exact hash**; there is no bulk route, no
    "attested" field and no default. Sensitive items (the conflict, gift, lobbying and contingent-fee codes, and any
    custom item whose label raises an i06 flag) open review tasks for Andre; i06 also flags the pursuit's title and
    notes and every response's custom text, and those flags must be acknowledged exactly at approval. No code fetches
    a bid portal (`NBD_BID_SOURCE_PROVIDER` refuses start; `POST /pursuits/import` is always `SOURCE_NOT_WIRED`).
13. **Win, loss, hand-off.** A pursuit is won only by Andre, only after a submission was delivered (`submitted`), with
    the deal gate re-checked. The win records two hand-offs (Onboarding: create the client; Finance: draft the first
    invoice), ids and refs only, delivered outside the lock through ports that are not wired: they stay
    `pending_delivery`, retried by `handoff-retry` (an adapter must be idempotent on `handoff_id`). Lost: the agent,
    with a reason code, from `responding` — but once a submission was delivered (or is `sending`), marking the pursuit
    lost is Andre's alone (AEGIS round 1 H2).
14. **Bid sourcing** is a NOT_BUILT port (decision 12).
15. **Partners and rates.** Kinds `referral`, `agency_alliance`, `white_label`. The agent proposes a commission rate
    (versioned, a canonical two-decimal percentage, 0.01..50.00); Andre approves exactly that version and binding
    hash. A later proposal does not unseat the approved rate until Andre approves it. A partner deal is registered
    against a counterparty and goes through the deal gate (decision 18). Only Andre marks a partner deal won, and only
    with an approved rate (snapshotted into the deal: later rate changes do not touch it) and an agreement Legal (37)
    shows in force (asked outside the lock; the stand-in answers `unavailable`: `503 LEGAL_UNAVAILABLE`).
16. **Tax information** is a reference only, by structure first and by scanning second (as corrected in AEGIS
    rounds 1 and 2). (1) Partner records carry NO free text (no notes field on a partner or a partner deal; unknown
    fields are 422). (2) Request ids are opaque and of one shape — a UUID with or without hyphens, or 16..64
    lowercase hex — so nothing can be typed into one (round 2 N1; the round-1 digit cap on them over-refused UUIDs).
    Finance's ids (`finance_event_id`, a payout's `finance_ref`) must be exactly finance-py's own generated ids,
    `fin-<prefix>-<26 Crockford base32>` (`service.rid`) or `fin-<prefix>-<40 hex>` (`ledger.derived_id`), as
    `OWN_ID_RE` in `services/finance-py/src/models.py` defines them. (3) Human-entered fields — every key (`partner_key`),
    counterparty `ref` (pursuits and partner deals alike), name and domain — are SCANNED, not capped: NFKC-normalised
    with every separator dropped, a run of exactly nine digits (an SSN / ITIN / EIN) or any other i12 tax-id shape is
    refused 422, while a longer run such as `hubspot:12345678901` is accepted. (4) A tax reference is exactly
    `vault:tax:<16..64>` or `tok:<16..64>` and the Finance payee reference `fin:<4..120>`, each with at most eight
    digits in total. (5) As a second layer, i12 scans every string of a partner body the same way (`422
    TAX_ID_RAW_REFUSED`, never echoed), skipping only money values, versions, hashes, server-issued ids, Finance's
    ids and the opaque request id. Keys named `tin`, `ssn`, `ein`, `itin`, `tax_id`, `taxpayer_id` (and the other
    spellings in `api.FORBIDDEN_KEYS`) are refused 422 in every body. The payee is set by Andre only, never shown back,
    and digested in the audit export.
17. **Commissions, clawbacks, payouts.** Commission accrues only on money Finance (31) reports as actually paid by the
    client (`POST /finance/events`, caller `finance_31`, kinds `payment`, `refund`, `chargeback`, USD), on a won
    deal. The commissionable base is net client money, floored at 0.00 and capped at the won value. Accrued =
    `money.commission_total(base, rate)`: exact `base * rate / 100`, then ONE half-up quantize to cents — finance-py's
    rule, `q()` in `services/finance-py/src/money.py` (ROUND_HALF_UP under the explicit `MONEY_CONTEXT`, copied byte
    for byte into `src/money.py`) — always on the CUMULATIVE base so per-payment rounding never drifts. Money is a
    canonical two-decimal JSON string everywhere; a float, int, exponent or non-canonical string is 422. A refund or
    chargeback claws back: first the unpaid balance, then payout requests Finance has not taken (newest first,
    cancelled at 0.00); the rest is a shortfall on money already with Finance, recorded with a task for Andre and
    never recovered here. A Finance event id is unique: the same id with other facts is `409 FINANCE_EVENT_REUSED`.
    The `payout-request` job records a payout request for each won deal's unpaid balance whose partner has a payee
    (settled at once, so never requested twice), then hands queued requests to Finance through the payouts port,
    marking each `sending` first so a clawback cannot cut an amount being sent. Finance owns payees, tax checks,
    approvals and the Stripe rail; this service never calls Stripe. The port is not wired: requests stay `queued`.
    Only `delivered` with a Finance reference moves a payout to `with_finance`; only an explicit `refused` requeues
    it, after taking from it any outstanding shortfall of its deal. A timeout, an exception or any other answer is an
    UNKNOWN outcome: the payout stays `sending` (never resent, never cut by a clawback) and each `payout-request`
    run reconciles it through the port's `payout_status` (the stand-in answers `unknown`). (AEGIS round 1 M1.) After
    `NBD_UNKNOWN_TICKS_BEFORE_TASK` unknown runs one `PAYOUT_STUCK` task opens; after `NBD_PAYOUT_MAX_REFUSALS`
    refusals (default 3) the payout is `held`, never resent, with one `PAYOUT_REFUSED` task. Andre settles a stuck or
    held payout at `POST /payouts/{id}/reconcile` by its exact `state_sha256`: `paid`, or `not_paid` (requeued after
    the shortfall is applied, its refusal count reset). The shortfall is DERIVED — `max(0, settled - accrued)` — so a
    later payment that restores the accrual absorbs it, and its open task is closed as `absorbed` the moment it
    reaches 0.00 (AEGIS round 2 N2, L3).
18. **Deal approval, aggregated.** The gate of a pursuit or partner deal is the SUM of the values of every deal in
    its counterparty group (pursuits and partner deals, both brands) that still counts: every OPEN deal whatever its
    age, and — opened within `NBD_AGGREGATION_WINDOW_DAYS` — won deals and any pursuit whose submission was delivered
    (or is `sending`) whatever its stage, so a delivered bid marked lost still counts (AEGIS round 1 H2 and Low);
    lost, no-bid and withdrawn deals with nothing delivered do not; two deals are in one group when they share ANY counterparty
    key, transitively. Over `NBD_DEAL_APPROVAL_THRESHOLD` (10000.00; may only be lowered) — strictly greater —
    needs Andre's deal approval, which binds the deal id, its value, the aggregate and the group members
    (`deal_gate.binding_sha256`). The gate is re-computed at every gate (submission, the submission queue, win): a
    sibling opened later, a value change or a member leaving the group makes the old approval stale. An approval
    asked for a deal under the threshold is refused (`DEAL_APPROVAL_NOT_NEEDED`). A deal value is at most
    1,000,000,000.00 (`422 VALUE_INVALID`); a group whose aggregate still does not fit in money (a legacy row) fails
    closed — `needs_andre`, never approvable, never a 500 (AEGIS round 1 M3).
19. **Legal hand-off.** `POST /partners/{id}/agreements` (kinds matching the partner: referral / alliance /
    white-label agreement, or an NDA) and `POST /pursuits/{id}/agreements` (NDA) call the Legal port outside the lock
    with ids and codes only. Anything but `delivered` with a Legal reference is `503 LEGAL_UNAVAILABLE` and nothing
    is recorded; the hand-off id is stable per request, so a retry is the same hand-off. The HTTP client is not built:
    legal-py has no intake kind for these agreements yet (its kinds are disputes, claims, privacy requests and
    questions), so `NBD_LEGAL_URL` refuses start rather than guess a contract.
20. **Outreach (email only).** Contacts hold a brand, an email (in the local log only: the send port needs it), its
    HMAC under `NBD_PII_HASH_KEY_FILE`, a name, and the partner or pursuit they belong to; no phone field exists.
    Templates are versioned and immutable, CAN-SPAM-checked (no deceptive subject, i08; footer with brand, postal
    address and a one-click unsubscribe link with `List-Unsubscribe` / `List-Unsubscribe-Post`); Andre approves a
    version by hash and a queue request must NAME that hash (it must equal the stored, the approved and the
    recomputed hash). Merge values come only from the console (`POST /contacts/{id}/merge-fields`, no URL or domain).
    The suppression list is keyed by the address's HMAC, append-only, shared across both brands (with Sales:
    unlock item 6). Everything is checked at queue time and again from current state at send time; a send is recorded
    (`outreach_send`) before the port is called; the daily cap counts by the service clock's UTC date.
21. **Replies hold.** A reply is ALWAYS recorded: its sender fields never refuse it (AEGIS round 1 H1). ANY reply
    (an out-of-office included) holds further automatic outreach to the contact, both brands, and cancels its queued
    messages, until Andre decides: `resume` lifts the hold and nothing else; `opt_out` suppresses permanently. Opt-out
    wording (i09, sales-py's normaliser) also suppresses at once, through the message it answers (its recipient and
    contact) and the sender's address when it parses (IDN `xn--` addresses parse; i02). A sender address that does
    not parse (quoted or UTF-8 local part, over 64 characters) is held under the keyed hash of its normalised raw form;
    an unknown message id is recorded, not refused. Addresses written in the body, after NFKC (fullwidth forms
    count), are held — never suppressed — whoever sent the reply, and matched against existing contacts. Every reply
    opens a review task, even with nothing resolvable. The reply text is never stored (SHA-256). AEGIS round 2: every
    address in the body that is an existing contact is held (the cap of five applies only to addresses that are not
    contacts, L1); one review task per sender per day, and a new hold only for what is not already held — active
    holds are indexed by address hash and by contact (L2).
22. **Ports** (`src/ports.py`, `src/legal_client.py`): email, submission, bid source, Onboarding and Finance hand-offs,
    Finance payouts, Legal agreements. Each stand-in fails closed and says so in `/status`; a port that raises is
    unavailable; no port is called with the lock held.
23. **Minimum data.** No route takes a phone number, date of birth, government or tax id, card or bank detail, IP or
    device: those keys are refused 422 anywhere in a body before it is parsed. Error bodies carry a reason code
    from `src/reasons.py` and never echo the request.
24. **Audit export** (`GET /audit/export`): the log with emails replaced by their keyed hashes (a raw value without a
    stored hash is dropped) and names, notes, labels and free text by SHA-256 (i13).
25. **Record first.** Every state change is one `_commit`: its typed evidence events are recorded on the ledger
    first, then the exact log line is fsynced aside (pending line), anchored on the ledger, appended (exact-size: the
    file must be exactly the in-memory lines; a blank line is never written and refuses start), and only then
    applied by the same `_apply` that replays the log at start. Typed evidence payloads carry ids, codes and hashes
    only: amounts, rates and values enter as `terms_sha256` (the SHA-256 of the canonical terms); emails as keyed
    hashes; and the ledger itself only ever receives the payload's SHA-256. Integrity is always verified against the
    ledger: every local line must have its anchor and the ledger may hold no anchor this log lacks. The service keeps
    its OWN pending line in memory and rolls it forward; a pending line found on disk at start is appended only if
    its anchor is already on the ledger, otherwise set aside to `pending.discarded` and appended later only if its
    anchor appears. The PII key's fingerprint is bound in the log; another key refuses start.
26. **Data directory and lifecycle** (service-py's, ADR 0014 rounds 5-5e). `NBD_DATA_DIR` (owned by the service user,
    0700) is held by a per-process flock taken by `config.load`; each service instance must `claim()` it (one claim at
    a time, under a mutex), and adopts the claim through a **single-use token** (a second adoption of the same
    token, or a stale or foreign token, refuses start; once adopted the claimer's token can no longer release it). A
    failed start gives the claim back. `close()` takes the service lock and leaves the instance **inert**: its log
    refuses every write (pending, discarded, append), every commit is refused, `verify_integrity` does no ledger I/O,
    `/health` is 503 `closed`, and the integrity and job routes answer `503 SERVICE_CLOSED`. The ledger's chain
    `verify()` (an HTTP call) runs **outside** the service lock; its verdict is reported exactly as returned
    (`ledger_valid` is `True` only for a real `True`), and the integrity result and log length are read fresh after it.
27. **Idempotency.** Every write carries a `request_id` — a UUID (with or without hyphens) or 16..64 lowercase hex,
    nothing else (AEGIS round 2 N1); the request key is `op|target|request_id` per actor, with
    the SHA-256 of the body: the same body answers what the first call did (across restarts), a different body is
    `409 REQUEST_ID_REUSED`. Ledger event ids are derived from the same identity, so a retry records nothing twice.
28. **Jobs** (caller `scheduler`, idempotent per request id, one at a time — `409 JOB_RUNNING`): `send-queue`,
    `submission-queue`, `deadline-sweep`, `handoff-retry`, `payout-request`, `integrity`. No job, check or test
    depends on the wall-clock hour; everything time-based reads the injected clock. In the send, submission and
    payout queues an item that fails with anything but a ledger / store outage is listed under `errors` in the job's
    recorded result and the queue carries on (AEGIS round 1 M3).
29. **Start-up safety.** The shared `graceful_close.py` and `launch_guard.py` (byte-identical, hygiene rule L4), the
    hardened launcher `serve.py`, and every NOT_BUILT provider setting refuses start.

## How the other departments use it (not wired yet)

| Department | Here | Needed there |
|---|---|---|
| Onboarding | won-pursuit hand-off port (`onboarding_create_client`) | an intake route for a won pursuit |
| Finance (31) | invoice-draft hand-off port; payouts port; `POST /nbd/v1/finance/events` and `/finance/payouts/{id}/paid` as caller `finance_31` | a partner payee kind (finance-py's `PayeeCreate.kind` is `clipper` only), a payout intake for partner commissions, and a sender of client-money events |
| Legal (37) | agreement port (`legal_client.py`) | intake kinds for partner agreements, NDAs and white-label contracts, an "agreement in force" read, and this department as a caller |
| Compliance (38) | audit export and integrity reads | — |
| Sales (27) | — | a shared suppression read (unlock item 6) |

## Not built (unlock list)

1. Andre's approvals by passkey through Cybersecurity (22) instead of `X-Andre-Approval-Token`.
2. The email send provider and its webhook relay (signature checks in the gateway).
3. Submission delivery (portal upload or delivery adapter) behind `NBD_SUBMISSION_PROVIDER`.
4. Bid-portal / RFP sourcing behind `NBD_BID_SOURCE_PROVIDER` (and ingestion of what it fetches).
5. Clients for Onboarding (`NBD_ONBOARDING_URL`), Finance (31) (`NBD_FINANCE_URL`: invoice drafts and partner payouts)
   and Legal (37) (`NBD_LEGAL_URL`), each needing the route named in the table above.
6. Suppression shared with Sales (27) (`NBD_SALES_SUPPRESSION_URL`): today the list is shared across both brands
   within this department only.
7. The console pages (pursuit board, approvals, attestations, holds, tasks).
8. Moving bootstrap caller tokens to Cybersecurity (22) minted credentials.

## Known limits (accepted)

- Aggregation catches splits that share a counterparty ref, registrable domain or normalised name. A counterparty
  entered under three new, unrelated identifiers is a new group; splits spread beyond the aggregation window are not
  summed.
- ledger-rust has no filtered read, so every integrity check reads the whole shared ledger (security-py's L7 rate
  limit is kept). `verify_integrity` still reads `entries()` under the lock (service-py's accepted stall).
- The ledger's `department` field is self-declared by any holder of the ledger token.
- A send, submission or payout whose outcome is unknown stays `sending` and is never retried on its own; submissions
  and payouts are reconciled through their ports' status calls (stand-ins answer `unknown`).
- A deal value is what the agent records; the threshold cannot see a price written only into a response's text.
  Andre's approval of every response and pitch (its exact text) and of every win is the control for that.
- The sensitivity flags (i06), the deceptive-subject rules (i08) and the raw-tax-id scan (i12) are word and shape
  rules, so they can be evaded by deliberate obfuscation. None of them is the guarantee: every response, pitch and
  template is approved by Andre on its exact text, and partner tax data is kept out structurally (decision 16).
- A process that stops with an anchor in flight AND commits a new line before that anchor lands leaves two anchors
  for one sequence number; the integrity check reports it and writes stop until an operator reconciles
  (security-py's accepted residual, ADR 0012 round 4).

## Settings

All settings and routes are in `services/bizdev-py/README.md`: `NBD_SERVICE_TOKEN`, `NBD_CALLER_TOKENS`,
`NBD_NON_PRODUCTION`, `NBD_DATA_DIR`, `NBD_PII_HASH_KEY_FILE`, `NBD_ANDRE_APPROVAL_TOKEN`,
`NBD_DEAL_APPROVAL_THRESHOLD`, `NBD_AGGREGATION_WINDOW_DAYS`, the outreach settings, the NOT_BUILT switches and the
launcher tuning.

## Amendment — AEGIS round 1 (Oct 6 2026, on 2d398f8): BLOCKING, every finding fixed

Regression tests: `services/bizdev-py/tests/test_aegis_r1.py` (each fails on 2d398f8).

- **H1** a STOP reply was refused 422 when `from_email` did not parse (IDN TLD, quoted or UTF-8 local part, over 64
  characters), so nothing was held or suppressed. Replies are now always recorded and held, suppressed through the
  message they answer, unparseable senders held by the keyed hash of their raw form, IDN addresses accepted (decision
  21).
- **H2** the agent could mark a delivered bid lost, dropping it from the aggregate. Lost after delivery is Andre's;
  a delivered pursuit counts whatever its stage (decisions 13, 18).
- **M1** a payout whose answer was lost was requeued and then clawed back although Finance might hold it. Unknown
  outcomes stay `sending` and are reconciled through `payout_status`; only `refused` requeues, after the shortfall
  is applied (decision 17).
- **M2** a nine-digit id fit in `partner_key`, `counterparty.ref` and `request_id`; separators beat the scan. Eight-digit
  total cap on every caller-chosen key, ref and id of partner and deal requests; the scan strips every separator
  (decision 16, corrected).
- **M3** an oversized value overflowed the aggregate (500 on views, stalled submission queue). Values capped at
  1,000,000,000.00; overflow fails closed; one failing item never stalls a queue (decisions 18, 28).
- **Lows** body addresses are held whoever sent the reply and after NFKC; `org_key` folds confusables; the window
  applies to closed deals only (decisions 9, 18, 21).
- **Port** 8480 collided with department 11; the default is now 8490 (decision 1).

## Amendment — AEGIS round 1 follow-up (Oct 6 2026, coordinator-approved)

A submission whose submit call timed out or errored was marked `failed` and could be resubmitted, so a bid that had
in fact arrived could be submitted twice. Now an unknown outcome stays `sending` (never failed, never resubmittable),
is reconciled through the new `SubmissionPort.submission_status`, only an explicit refusal makes the response
resubmittable, and a deadline passing while the outcome is unknown opens one task for Andre (decision 11).
Regression tests: `services/bizdev-py/tests/test_aegis_r1b.py` (each fails on 22fe413).

## Amendment — AEGIS round 2 (Oct 6 2026, on 5e16d09): NOT BLOCKING, every item fixed

Regression tests: `services/bizdev-py/tests/test_aegis_r2.py` (each fails on 5e16d09).

- **N1** the eight-digit cap over-refused (UUID request ids, finance-py ids, CRM refs). Request ids now have one
  opaque shape; Finance's ids match finance-py's `OWN_ID_RE`; keys, refs, names and domains are scanned for a run of
  exactly nine digits instead of capped (decisions 16, 27).
- **N2** a submission or payout could stay `sending` with nobody told. A durable unknown-run counter opens one task
  after `NBD_UNKNOWN_TICKS_BEFORE_TASK`; Andre-only reconcile routes bound by state hash settle them (decisions 11, 17).
- **L1** every contact named in a reply body is held. **L2** one task per sender per day; holds indexed by hash.
  **L3** a payout refused `NBD_PAYOUT_MAX_REFUSALS` times is held with a task; the shortfall is derived and its task
  closes when absorbed. **L4** domains are IDNA-encoded before hashing and matching (decisions 9, 17, 21).
- **Gitleaks** the round-1 test literal `west-1234-56789` (a fake tax id) is now built from parts; `.gitleaks.toml`
  carries an exact-string allowlist entry for the copy left in history (22fe413).
