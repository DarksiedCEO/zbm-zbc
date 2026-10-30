# ADR 0024 — Compliance (38) deployed as the hard gate, with the US + PR + GU service area

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

1. **Service area.** ZBM and ZBC do business only with clients located in the 50 US states, the District of Columbia,
   Puerto Rico and Guam. Everything else is refused, including the other US territories (USVI, American Samoa,
   Northern Mariana Islands) until Andre adds them. Plus OFAC: refuse anyone on the SDN/consolidated lists.
2. Compliance deployed on Railway as a **private** service, with its volume (daily backups), ledger connection and
   tokens declared but never stored; a daily scheduled job; gates closed until C-11 has passed.
3. **Gates, fail closed.** Client activation for SEO paid audits and Revenue Recovery store connections needs a current
   Compliance activation decision (jurisdiction in the area, sanctions screening recorded, legal documents current);
   Finance invoice approval/issuance and billing readiness need it too; payouts stay blocked.
4. The client's jurisdiction is recorded at activation; rule expiry is reported, never silently extended; a
   per-jurisdiction sales report flags 75% of each economic-nexus threshold (memo MEMO-FIN-CQ-11-2026-10-30).

## Decisions

### 1. The allowed set is one reviewed rule parameter (seed v2)

`HR-05 parameters.operate` lists the allowed codes one by one: `US-AL` … `US-WY` (50), `US-DC`, `US-PR`, `US-GU`, and
`PR`, `GU` (Puerto Rico and Guam may be declared either way). `US` itself is not in the list, so a subdivision is never
admitted by its country prefix (`US-VI` is refused although `US-CA` is operate); a bare `US` person still needs a state
(`region_required`). A campaign *target* may be the bare country `US` (`HR-05 parameters.target_countries`), meaning the
allowed states. `HR-07 parameters.refuse` names the OFAC comprehensively sanctioned jurisdictions (cited with
US-OFAC-04) and `unlisted: refuse` refuses everything else. The resolver ignores the parameters of a `superseded` row.

Changing the area is a register amendment of HR-05 that only Andre approves (test:
`test_payout_for_foreign_and_entity_payees` adds `GB` by an approved amendment). The area applies to every lane:
clients, ZBC brands, campaign targets **and creators** (clippers). That is the fail-closed reading of "everything else
is refused"; ZBC payouts are blocked anyway. A Puerto Rico or Guam creator is not a foreign payee.

**Seed v2 instead of an amendment proposal.** The register's only path to a rule in force is Andre's approval of a
proposal; the seed is the first proposal, and it has never been approved on any deployment (Compliance was never
deployed). So the decisions are written into the seed itself (`seed_version: 2`, generated Oct 30, 2026) and its new
SHA-256 is pinned (`config.PINNED_SEED_SHA256`, `service.SPEC_SEED_SHA256`, test H.29); `source_report` records seed
v1's hash (`4e3821d0…584d`), and v1 stays in git. Andre still approves it, as one decision, before anything is in
force. Built from v1 by a script (row changes only):

- **HR-05, HR-07** rewritten as above (`verified_at` 2026-10-30, `effective_date` null: in force from the approval).
- **17 rows superseded, unchanged otherwise, with a note** ("Superseded Oct 30, 2026: outside the US + PR + GU service
  area (HR-05, founder decision). Kept for history."): HR-06 (the EU kit), EU-GDPR-27, EU-AIACT-50, EU-EAA,
  FR-LOI-2023-451, IT-AGCOM, ES-RD444, ES-LABEL, DE-UWG-5A, NL-CVDM, IE-LABEL, UK-HIDDEN-ADS, CA-COMPBUREAU,
  CA-QC-LAW25, CQ-05, CQ-06, CQ-14. They described serving those markets; nothing is deleted. Kept as they were: rows
  about recipients or outreach (HR-08 no cold outbound to Canada, CA-CASL) and CQ-07 (it still blocks a marketplace
  flag).
- **HR-14** (house rule, activation, lanes `client`, `zbc_brand`): Andre's OFAC search recorded, no match, SDN and
  Consolidated, no older than `max_age_days` (30); an activation stays current for `rescreen_after_days` (90).
- **HR-15** (house rule, activation, lane `client`): Legal (37) confirms each `required_doc_ids` (`client_msa`)
  version the client accepted is Legal's current version with the same hash, and the acceptance is this client's
  (`party_ref client:<id>`, the reference Finance uses) with sufficient evidence.
- **US-SALESTAX-NEXUS** (guidance, `control` gate only, **unverified**: the threshold table is a vendor's): the
  thresholds as parameters with their sources — default `$100,000.00`; California `$500,000.00` (CDTFA); Hawaii,
  Nevada, Puerto Rico also 200 transactions; alert ratio `0.75`. Other states' own thresholds are not individually
  verified (stated in the row).

HR-14 and HR-15 take effect on 2026-10-30 (`effective_date`), the decision date: the suite's older scenarios, pinned to
Sep 26, keep testing the engine as it stood, and every new test runs on or after Oct 30.

### 2. Sanctions: a recorded search, not an API

No vetted offline OFAC list exists in the repository and an unvetted external API is not called. Andre searches OFAC's
Sanctions List Search and records it: `POST /compliance/v1/sanctions/attestations` (his token only; body: subject,
lane, `searched_at`, `list_version`, `lists`, `tool: ofac_sanctions_list_search`, `result`, `evidence_sha256` of the
saved screenshot/export, `searched_name_sha256` — never the name). It is recorded on the ledger first
(`sanctions_attestation_recorded`). A `potential_match` or `match` opens a hold that the API cannot release (refuse on
any hit; never investigate around it). The activation fact `sanctions_attestation_id` names the record; HR-14's check
(`client_sanctions_attested`) refuses a missing, foreign, stale, future-dated, partial-list or non-clean record.

### 3. Legal wired to Compliance

`legal_client.HttpLegal37` (Compliance → Legal, caller `compliance_38`) reads `GET /legal/v1/documents/{id}/current`
(Legal's section E.1 answer) and `GET /legal/v1/acceptances/{id}` (now open to `compliance_38` in legal-py). Wired only
when the three `COMPLIANCE_LEGAL_*` settings are set (a partial set refuses the start); otherwise `NotBuiltLegal37`
answers unavailable and every client activation is refused. Bounded (10 s total, one retry on a transport error or
5xx, 256 KiB cap, no redirects); a Legal on an unpinned seed is unavailable.

### 4. The client activation status (what the other departments read)

`GET /compliance/v1/clients/{client_id}/activation[?lane=client]` (any caller; read-only) answers
`{decision: ALLOW|REFUSE, current, ruling_id, jurisdiction, valid_until, reasons, seed_pinned, ...}`. **ALLOW only when
all hold now**: the latest activation ruling for the client allowed it; every rule it evaluated is still `verified` in
the register in force (an expired rule is never extended: on Dec 25, US-OFAC-04 turns every client to REFUSE until it
is re-verified); the register gained no activation rule since the ruling; no control blocking activation is red (C-11;
C-04 blocks creators only, ADR 0006 choice 8); no hold is open on the client; its OFAC search is within
`rescreen_after_days`. `jurisdiction` is the recorded code (`US-CA`, `US-DC`; `PR`, `GU` however declared).

Callers treat anything but `decision == ALLOW`, `current == true`, `seed_pinned == true` with a ruling id and a
jurisdiction as a refusal, and an unreachable or malformed answer as unavailable (refused too):

| Caller | Where | Refusal |
|---|---|---|
| finance-py (`finance_31`) | invoice approval (`decide_invoice`, approve only), billing readiness, every Stripe page (before one is made or handed back) | `COMPLIANCE_NOT_ACTIVATED` / `DEPENDENCY_UNAVAILABLE:compliance_38`, both rule FIN-08 ("an absent, stale or stand-in input fails its gate"; no Finance seed change) |
| seo-py (`seo_02`) | a client tenant's paid audit, its schedule, every scheduled slot, every client report; before the invoice check; **no override** | `COMPLIANCE_NOT_ACTIVATED` 409; `COMPLIANCE_NOT_CONFIGURED` / `_UNAVAILABLE` / `_AUTH_REFUSED` / `_RESPONSE_INVALID` 503 (a slot waits) |
| detection-py (`revenue_recovery`) | every store sync and every snapshot handed to the scan path | `COMPLIANCE_NOT_ACTIVATED` 409 / `COMPLIANCE_UNAVAILABLE` 503, no store call made |

The client id is the one Finance, Legal and Compliance share (SEO: the tenant's `finance_client_id`, which must now be
bound for every client run in either invoice mode; detection: the connection's `client_id`). Finance records the
jurisdiction and ruling id on every invoice it issues. Payouts stay blocked (their V&I, OFAC provider and rail gates are
unchanged); Finance's `sanctions_status` and `jurisdiction` reads now call `GET /compliance/v1/sanctions/status` and
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
printed and retried at the next tick. Why not a Railway cron service: a second service would need its own image,
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
- A register change that adds an activation rule turns every client's status to REFUSE until re-activated; re-verifying
  an existing rule does not.
- On 2026-12-25 every client turns REFUSE unless US-OFAC-04 has been re-verified and approved (runbook checklist).
- The EU-kit and local-label mechanics stay in the engine but no seed v2 row reaches them; their tests now exercise the
  label functions directly and assert the refusal at the gate.

## Not done / open

- No accessibility provider and no sanctions provider: site publish (HR-09) and creator payouts stay refused (H3).
- Guam's business privilege tax is not researched (memo section 4); `US-SALESTAX-NEXUS` is unverified.
- The images were not built and the smoke was not run here (no Docker daemon); `railway config plan` was not run (no
  Railway access). Both are in Andre's steps.
