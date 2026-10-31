# ADR 0024 — Compliance (38) deployed as the hard gate, with the US service area

Status: **WIP, not merged** (branch `feat/compliance-gate-deploy` from `golive-next-2026-10-30` `8d20e83`). Founder
decisions of Oct 30, 2026, approved by Andre for this build. Amends ADR 0006 (Compliance architecture), ADR 0009
(Finance), ADR 0017 (SEO) and ADR 0020 (Revenue Recovery store connector). Nothing here has been applied to Railway.

## Context

The AEGIS review of Oct 30, 2026 ("is Compliance (38) enforced across the go-live flow?") found compliance-py built
but not enforced: not deployed, no Dockerfile, and no go-live service asking it anything (findings C1, C2). Activation,
invoicing and store reads ran with no jurisdiction or sanctions check; Finance's thin client hard-coded the
jurisdiction and sanctions reads as unavailable (M1); the register's 10 platform rows expired on Oct 26 and 23
guidance rows expire on Dec 25 with nothing scheduled to notice (H2); the suite never ran on today's date (M3).

Andre's decisions (Oct 30, 2026):

1. **Service area.** ZBM and ZBC do business only with clients located in a US jurisdiction: the 50 states, the
   District of Columbia, Puerto Rico, Guam, the US Virgin Islands, American Samoa and the Northern Mariana Islands
   (founder update of 12:51 the same day, which widened the morning's 50 states + DC + PR + GU). The US Minor Outlying
   Islands (uninhabited) are left out: the model does not need them for completeness. Everything else is refused
   because it is not the US (Canada, the UK, France, the EU, every other country). Plus OFAC: refuse anyone on the
   SDN/consolidated lists.
2. Compliance deployed on Railway as a **private** service, with its volume (daily backups), ledger connection and
   tokens declared but never stored; a daily scheduled job; gates closed until C-11 has passed.
3. **Gates, fail closed.** Client activation for SEO paid audits and Revenue Recovery store connections needs a current
   Compliance activation decision (jurisdiction in the area, sanctions screening recorded, legal documents current);
   Finance invoice approval/issuance and billing readiness need it too; payouts stay blocked.
4. The client's jurisdiction is recorded at activation; rule expiry is reported, never silently extended; a
   per-jurisdiction sales report flags 75% of each economic-nexus threshold (memo MEMO-FIN-CQ-11-2026-10-30).
5. **Re-screen every 90 days** (founder decision of Oct 30, 2026, 16:04): a client's activation stays current for 90
   days after its recorded OFAC search (HR-14 `rescreen_after_days`); after that the client is re-screened and
   re-activated before it is served again.

## Decisions

### 1. The allowed set is one reviewed rule parameter (seed v2)

`HR-05 parameters.operate` lists the allowed codes one by one: `US-AL` … `US-WY` (50), `US-DC`, `US-PR`, `US-GU`,
`US-VI`, `US-AS`, `US-MP`, and `PR`, `GU`, `VI`, `AS`, `MP` (a territory may be declared either way; Compliance reports
it by its own two-letter code). `US` itself is not in the list, so a subdivision is never admitted by its country
prefix (`US-UM` is refused although `US-CA` is operate); a bare `US` person still needs a state
(`region_required`). A campaign *target* may be the bare country `US` (`HR-05 parameters.target_countries`), meaning the
allowed states. The service area is the United States only: every place outside it is refused because it is not the
US (`HR-07 parameters.unlisted: refuse`), not by any further choice. As defence in depth, `HR-07 parameters.refuse`
also lists the places under OFAC comprehensive sanctions (US-OFAC-04): Cuba, Iran, North Korea, Crimea and Sevastopol
`UA-43`/`UA-40`, the so-called DNR/LNR `UA-14`/`UA-09`, and Zaporizhzhia `UA-23` and Kherson `UA-65` under
US-OFAC-04's "other occupied regions" reading. HR-07's text names each code and gives that sanctions basis (AEGIS F-13,
r2 N-7; founder decision of Oct 30, 2026, 16:04). The resolver ignores the parameters of a `superseded` row.

Changing the area is a register amendment of HR-05 that only Andre approves (test:
`test_payout_for_foreign_and_entity_payees` adds `GB` by an approved amendment). The area applies to every lane:
clients, ZBC brands, campaign targets **and creators** (clippers). That is the fail-closed reading of "everything else
is refused"; ZBC payouts are blocked anyway. A creator in any of the five territories is not a foreign payee.

**Seed v2 instead of an amendment proposal.** The register's only path to a rule in force is Andre's approval of a
proposal; the seed is the first proposal, and it has never been approved on any deployment (Compliance was never
deployed). So the decisions are written into the seed itself (`seed_version: 2`, generated Oct 30, 2026) and its new
SHA-256 is pinned (`config.PINNED_SEED_SHA256`, `service.SPEC_SEED_SHA256`, test H.29); `source_report` records seed
v1's hash (`4e3821d0…584d`), and v1 stays in git. Andre still approves it, as one decision, before anything is in
force. Built from v1 by `services/compliance-py/devtools/make_seed_v2.py` (row changes only; it reproduces the
pinned file byte for byte):

- **HR-05, HR-07** rewritten as above (`verified_at` 2026-10-30, `effective_date` null: in force from the approval).
- **17 rows superseded, unchanged otherwise, with a note** ("Superseded Oct 30, 2026: outside the US service
  area (HR-05, founder decision). Kept for history."): HR-06 (the EU kit), EU-GDPR-27, EU-AIACT-50, EU-EAA,
  FR-LOI-2023-451, IT-AGCOM, ES-RD444, ES-LABEL, DE-UWG-5A, NL-CVDM, IE-LABEL, UK-HIDDEN-ADS, CA-COMPBUREAU,
  CA-QC-LAW25, CQ-05, CQ-06, CQ-14. They described serving those markets; nothing is deleted. Kept as they were: rows
  about recipients or outreach (HR-08 no cold outbound to Canada, CA-CASL) and CQ-07 (it still blocks a marketplace
  flag).
- **HR-14** (house rule, activation, lanes `client`, `zbc_brand`): Andre's OFAC search recorded, no match, SDN and
  Consolidated, no older than `max_age_days` (30); an activation stays current for `rescreen_after_days` (90, Andre's
  decision 5 above). Like
  every house rule it has no `source_url` (the OFAC pages are `additional_sources`), so Andre can amend it.
- **HR-15** (house rule, activation, lane `client`): Legal (37) confirms each `required_doc_ids` (`client_msa`)
  version the client accepted is Legal's current version with the same hash, and the acceptance is this client's
  (`party_ref client:<id>`, the reference Finance uses) with sufficient evidence.
- **US-SALESTAX-NEXUS** (guidance, `control` gate only, **unverified**: the threshold table is a vendor's): the
  thresholds as parameters with their sources — default `$100,000.00`; California `$500,000.00` (CDTFA); Hawaii,
  Nevada, Puerto Rico also 200 transactions; alert ratio `0.75`. Other states' own thresholds are not individually
  verified (stated in the row). Finance's report has three windows: the trailing 12 months, the calendar year and the
  previous calendar year (AEGIS F-11), and flags on any of them. Guam, the US Virgin Islands, American Samoa and the Northern Mariana Islands get **no
  number**: no verifiable threshold source was found for them, so `parameters.check_before_first_client` records
  "local gross receipts/business tax — check before first client" and Finance's report shows that check (and flags the
  territory) instead of a ratio.

HR-14 and HR-15 take effect on 2026-10-30 (`effective_date`), the decision date: the suite's older scenarios, pinned to
Sep 26, keep testing the engine as it stood, and every new test runs on or after Oct 30.

### 2. Sanctions: a recorded search, not an API

No vetted offline OFAC list exists in the repository and an unvetted external API is not called. Andre searches OFAC's
Sanctions List Search and records it: `POST /compliance/v1/sanctions/attestations` (his token only; body: subject,
lane, `searched_at`, `list_version`, `lists`, `tool: ofac_sanctions_list_search`, `result`, `evidence_sha256` of the
saved screenshot/export, `searched_name_sha256` — never the name). It is recorded on the ledger first
(`sanctions_attestation_recorded`). A `potential_match` or `match` opens a hold that the API cannot release (refuse on
any hit; never investigate around it). The activation fact `sanctions_attestation_id` names the record; HR-14's check
(`client_sanctions_attested`) refuses a missing, foreign, stale, future-dated, partial-list or non-clean record, and a
record made for another lane (a ZBC brand search does not activate a ZBM client; AEGIS F-7).

The record is Andre's assertion (AEGIS F-8): `evidence_sha256` is the hash of a file the service never receives. Keep
the saved export or screenshot itself where it can be checked against the hash later (the founder's evidence folder or
Legal's document store); a `searched_at` may be up to `max_age_days` old, which only shortens the activation.

### 3. Legal wired to Compliance

`legal_client.HttpLegal37` (Compliance → Legal, caller `compliance_38`) reads `GET /legal/v1/documents/{id}/current`
(Legal's section E.1 answer) and `GET /legal/v1/acceptances/{id}` (now open to `compliance_38` in legal-py). Wired only
when the three `COMPLIANCE_LEGAL_*` settings are set (a partial set refuses the start); otherwise `NotBuiltLegal37`
answers unavailable and every client activation is refused. Bounded (10 s total, one retry on a transport error or
5xx, 256 KiB cap, no redirects); a Legal on an unpinned seed is unavailable.

### 4. The client activation status (what the other departments read)

`GET /compliance/v1/clients/{client_id}/activation[?lane=client]` (callers `finance_31`, `seo_02`, `revenue_recovery` —
detection-py — and `onboarding`; any other caller 403, AEGIS F-9; read-only) answers
`{decision: ALLOW|REFUSE, current, ruling_id, jurisdiction, valid_until, reasons, seed_pinned, ...}`. **ALLOW only when
all hold now**: the latest activation ruling for the client allowed it; every rule it evaluated is still `verified` in
the register in force (an expired rule is never extended: on Dec 25, US-OFAC-04 turns every client to REFUSE until it
is re-verified) **and says what it said then**: the ruling records a fingerprint of each rule it relied on (id,
jurisdiction, obligation, gates, applies_when, check, parameters, effective_date; for HR-05 and HR-07 everything
but `parameters`, which the live area check below covers, AEGIS r2 N-1) and any approved amendment of one of
them refuses until the client is re-activated, while a re-verification (`verified_at`/`expires_at` only) does not; the
client's recorded jurisdiction and campaign targets are `operate` under the service area **in force now** (HR-05
`operate`, HR-07 `refuse`), checked on every call independently of the fingerprints (AEGIS F-1: a client in a state
removed from HR-05, or in a region added to HR-07, is refused at once); the register gained no activation rule since
the ruling; no control blocking activation is red (C-11;
C-04 blocks creators only, ADR 0006 choice 8); no hold is open on the client; its OFAC search is within
`rescreen_after_days`, for this lane. `jurisdiction` is the recorded code (`US-CA`, `US-DC`; `PR`, `GU`, `VI`, `AS`, `MP` however declared).

HR-15 (the client's documents) is checked at activation, not on every status call (AEGIS F-6): the status is a
read-only path that writes nothing to the ledger, while every call to Legal is a recorded crossing (record first), and
asking Legal on each status read would make Legal's availability a condition of every invoice, SEO run and store read.
A superseded MSA or an acceptance found insufficient therefore takes effect when the client is next activated (at the
latest at the 90-day re-screen); when Legal publishes a new MSA version that must bind existing clients, re-activate
them (the gate then asks Legal).

Callers treat anything but `decision == ALLOW`, `current == true`, `seed_pinned == true` with a ruling id and a
jurisdiction as a refusal, and an unreachable or malformed answer as unavailable (refused too):

| Caller | Where | Refusal |
|---|---|---|
| finance-py (`finance_31`) | invoice approval (`decide_invoice`, approve only), billing readiness, every Stripe checkout request, before the idempotent replay and before an open page is handed back; on a REFUSE every open page of the client is expired at Stripe, and the hourly `stripe-sessions` job closes the open pages of any client now refused (an outage closes nothing) (AEGIS F-5); Compliance is read again after a page is made, and anything but ALLOW (an outage included) expires that page and refuses (AEGIS r3 N-9); a Stripe payment is booked after a fresh read: when the client is refused, or Compliance is unreachable, it is booked as received but the invoice is `held_compliance` (`refused` / `unverified`): no client receipt, no media vendor payment, `held: true` in the invoice status seo reads (`INVOICE_HELD`), a `compliance_hold` / `compliance_unverified` break for Andre (AEGIS r3 N-9, r4 R4-1). The hourly job re-asks Compliance for unverified holds (ALLOW releases, REFUSE converts to refused). A hold is released only by `POST /fin/v1/invoices/{id}/compliance-hold/release`: `refund` with an F7r refund entry of that payment's receipt, after the payment entry in the journal, the refunds covering the held amount; or `compliance_allow` with a current ALLOW read at that moment (a receipt only if nothing was refunded); the generic break resolution refuses these breaks (R4-2, r5 R5-1). **A hold grounded in sanctions** (Compliance's `sanctions_grounded`: an open sanctions_match hold, or the client's place on HR-07's sanctions list) is never released by a refund or an ALLOW: OFAC's blocking rules apply (report within 10 business days, 31 CFR 501.603 / 501.604), and only `ofac_authorization` releases it, with the SHA-256 and kind of the OFAC document: a license, an unblocking report filed under 501.603(b)(3) for a false positive (OFAC FAQ 1196), or a Compliance Release (501.806) (r5 C1, r6 N5). The marker is computed even when the latest ruling refused (every activation ruling records the place and targets), an absent or malformed flag counts as sanctions, the hourly job upgrades any held client's hold when Compliance now says sanctions (never downgrades), a refund release re-reads Compliance, a later hold inherits the marker, and no checkout is made on a held invoice (r6 N1-N4). A blocked payment leaves with `ofac_authorization` and a license, an unblocking report, a Compliance Release or a `delisting` notice; every sanctions hold is BLOCKED by default. Compliance names the ground (`sanctions_ground`: `match`, an OFAC list match, or `place`, a sanctioned place or target; match wins) and Finance keeps it on the hold, sticky; an unknown ground counts as `match`. Only a `place` hold, with OFAC's written guidance that the funds be rejected (hashed, kind `ofac_guidance`), may leave with `ofac_rejected_return`: refunded to the sender (proven like a refund) and the 501.604 rejected-transaction report hashed; never a `match` (r7, r8). Each program's regulations decide blocked vs rejected (e.g. Cuba, 31 CFR 515.201 and 515.205: blocked). Only Andre's token sees the marker, the reason, the notes and the OFAC details, on the invoice and breaks reads and on `/fin/v1/audit/export` and `/fin/v1/audit/evidence`; other callers see `held`, a generic break, a generic `compliance_hold_event` with neutral event ids, and Compliance refusals as "compliance hold" in billing readiness and checkout (r6, r7 V1, r8). The job re-reads Compliance for each hold just before releasing it (R5-3); a second hold on the same invoice closes the first one's break (R5-2). These breaks are outside FC-02: the hold is enforced on the client's own invoice, so one client's hold does not freeze every payout (INFO-3) | `COMPLIANCE_NOT_ACTIVATED` / `DEPENDENCY_UNAVAILABLE:compliance_38`, both rule FIN-08 ("an absent, stale or stand-in input fails its gate"; no Finance seed change) |
| seo-py (`seo_02`) | a client tenant's paid audit, its schedule, every scheduled slot, every client report; before the invoice check; **no override** | `COMPLIANCE_NOT_ACTIVATED` 409; `COMPLIANCE_NOT_CONFIGURED` / `_UNAVAILABLE` / `_AUTH_REFUSED` / `_RESPONSE_INVALID` 503 (a slot waits) |
| detection-py (`revenue_recovery`) | every store sync and every snapshot handed to the scan path | `COMPLIANCE_NOT_ACTIVATED` 409 / `COMPLIANCE_UNAVAILABLE` 503, no store call made |

The client id is the one Finance, Legal and Compliance share (SEO: the tenant's `finance_client_id`, which must now be
bound for every client run in either invoice mode; detection: the connection's `client_id`). Finance records the
jurisdiction and ruling id on every invoice it issues. Payouts stay blocked (their V&I, OFAC provider and rail gates are
unchanged). SEO's own-properties tenants (`kind` not `client`) run without the activation check: only Andre registers
tenants and their domains, and a domain (its `www.` twin, its subdomains and parents) belongs to one tenant only
(`DOMAIN_TAKEN`, AEGIS M2/N1), so a domain already on a client tenant cannot be put on an own tenant; registering a
client's site only on an own tenant would be Andre's own act (AEGIS F-10). Finance's `sanctions_status` and `jurisdiction` reads now call `GET /compliance/v1/sanctions/status` and
`POST /compliance/v1/jurisdictions/resolve` instead of a hard-coded "unavailable" (finding M1).

### 5. Readiness: which rules are expired and what they block

`GET /compliance/v1/readiness` (any caller) and the lines printed at every start and after every scheduled run list:
the register version and whether the seed is pinned; what closes every gate (`register_not_in_force`, a red control);
each expired row with its expiry date and gates; and the set of rows a plain US web client's activation runs through
(lane `client`, platform `web`, a US state, no flag), with what blocks it now and when each of its rows expires. On the
seed v2 approved on Oct 30: 10 expired (all platform rows plus US-STRIPE-KYC), none blocks a US web client; that set is
HR-03, HR-05, HR-07, HR-08, HR-14, HR-15, US-FTC-465-07 (expires 2027-03-25) and US-OFAC-04 (expires 2026-12-25).
A TikTok or X client is refused by its expired platform rows, as before.

### 6. The scheduler is in process

`scheduler.Scheduler` runs inside compliance-py when `COMPLIANCE_SCHEDULER_ENABLED=1`: internal controls at start and
every `COMPLIANCE_SCHEDULER_INTERVAL_SECONDS` (default 6 h, inside C-11's 24 h SLA), the Change Watcher every 24 h when
`COMPLIANCE_WATCHER_ENABLED=1`. Same service calls as the `scheduler` caller's routes, recorded first; a failed run is
printed and retried on the next 60 s tick (not at the next interval). Any other error in a tick is caught, recorded
and printed: the thread does not die of an exception (AEGIS F-4). `/health` carries the scheduler's liveness (`alive`,
last successful controls and watcher runs, `last_error` with the error class only, AEGIS r2 N-8) and answers **503** when the thread is dead or the last
successful controls run (or the start) is older than two intervals. Two failure kinds, two responses:

- **The thread is dead** (it ended without `stop()`; only something `except Exception` does not catch can end it): it
  prints `compliance scheduler: scheduler thread stopped unexpectedly (...)` and `api.main`'s hook ends the process with
  exit code 1 (`os._exit`), so Railway's ON_FAILURE policy restarts compliance (at most 10 times), and the new
  process's first tick re-runs the controls.
- **Controls fail or are stale** (ledger down, a 409 during a watcher cycle) while the thread is alive: no exit, since
  a restart would not fix the cause and would loop. The thread retries every 60 s, C-11 closes every gate after 24 h
  without a successful run (fail closed), and `/health` answers 503.

Railway runs the healthcheck only when a deployment starts, so the 503 needs a monitor (runbook: a private check on
the internal network that pings an outside heartbeat monitor only while `/health` is 200). Why not a Railway cron service: a second service would need its own image,
healthcheck exemption and a `scheduler` token held somewhere; in process there is no token to leak. C-11 still blocks
every gate until it has passed once (it is `never_run` until the first run) and whenever it is older than 24 h.

### 7. Deployment

`services/compliance-py/Dockerfile` (the legal-py pattern: pinned digest, hash-locked wheels, uid 10001,
`zbm-entrypoint --port-var COMPLIANCE_PORT --state-dir-var COMPLIANCE_DATA_DIR`). `.railway/railway.ts`: service
`compliance` (no domain), volume `compliance-data` (`VOLUME_AS_DEPLOYED`, daily backups, `requiredMountPath /data`),
every secret `preserve()` or a reference, `COMPLIANCE_CALLER_TOKENS` interpolated from each caller's own variable
(`onboarding` is Andre-held; no `scheduler`), the Legal wiring, and the callers' `*_COMPLIANCE_*` variables on finance,
seo, detection (token files through the entrypoint) and legal. The ledger caller `compliance-py` writes `compliance`
(`devtools/ledger_callers.py`); its C-11 calls `GET /ledger/verify`, open to every caller, and `GET /ledger/entries`,
scoped to its department (ledger-rust M4), which is all `assess_log` reads.

Evaluated with the pinned SDK (railway 3.13.1) against the base commit: 2 resources added (`service.compliance`,
`volume.compliance-data`), 0 removed; on existing services only variables added, and one literal changed
(`LEGAL_CALLER_TOKENS` gains `compliance_38`). The plan Andre runs must show the same: no destroy.

## Consequences

- Day one: every client activation, invoice approval, Stripe page, SEO client run and store read is refused until
  Andre approves seed v2, the scheduler has run C-11, and the client is activated. That is the intent.
- A register change that adds an activation rule, or amends one a client's ruling relied on, turns that client's status
  to REFUSE until re-activated; re-verifying an existing rule does not. Every client's activation relies on HR-03,
  HR-05, HR-07, HR-08, HR-14, HR-15, US-FTC-465-07 and US-OFAC-04, so such an amendment refuses **every** client at once
  (re-activation needs a new OFAC search for any client whose last one is over 30 days old), and the hourly job then
  closes their open Stripe pages. The exception: an amendment of only HR-05's or HR-07's `parameters` (the service
  area) refuses exactly the clients it takes out; a widening or a re-ordering refuses nobody.
- Finance's sweep is bounded (AEGIS r2 N-4): a refused checkout closes at most 25 of the client's pages per request
  (the rest are reported "not attempted (cap)", AEGIS r3 N-13), and the job reads Compliance for at most 25 clients per
  run (rotating) outside the Stripe turn. The rotation cursor is
  in memory, so after a restart it starts again from the first client.
- On 2026-12-25 every client turns REFUSE unless US-OFAC-04 has been re-verified and approved (runbook checklist).
- The EU-kit and local-label mechanics stay in the engine but no seed v2 row reaches them; their tests now exercise the
  label functions directly and assert the refusal at the gate.

## Not done / open

- No accessibility provider and no sanctions provider: site publish (HR-09) and creator payouts stay refused (H3).
- Guam's business privilege tax and the gross receipts/business taxes of the US Virgin Islands, American Samoa and the
  Northern Mariana Islands are not researched; `US-SALESTAX-NEXUS` is unverified.
- After a refund of a held payment the invoice is `issued` again with the amount owed on paper: finance-py has no
  void or credit note for an issued invoice. Adding one is an accounting decision (a new journal flow), left open.
- Finance has no email path for Andre (its client mail sends client receipts only), so an open hold is signalled
  through finance's `/health` (`compliance_holds_open`, a count) and the private heartbeat check of the runbook, which
  stops pinging while any hold is open; a daily manual check is the fallback (AEGIS r5 C2).
- Deploy seo-py before or with finance-py: the older seo parser refuses the new `held` field (AEGIS r5 C3).
- No monitor polls compliance's `/health` yet (compliance has no public domain; the runbook's private check plus
  heartbeat is Andre's step). Railway does not restart a running service on a 503; only the dead-thread exit restarts.
- The images were not built and the smoke was not run here (no Docker daemon); `railway config plan` was not run (no
  Railway access). Both are in Andre's steps.
