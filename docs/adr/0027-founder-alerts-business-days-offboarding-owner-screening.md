# ADR 0027: Founder alerts, bank-holiday business days, client offboarding, OFAC owner screening

Status: proposed (fix wave C, Oct 31 2026; local branch `feat/ops-alerts-offboarding-2026-10-31`, not pushed)
Deciders: Andre (founder). Inputs: production audit red team reports (ops OPS-H2, OPS-H4, OPS-H5, OPS-H8, OPS-M13;
chaos CH-6..CH-8), MSA v1 sections 1.5 and 15.5, Refund and Cancellation Policy v1, the draft runbooks on local branch
`docs/ops-runbooks-2026-10-30`, ADR 0024 (the compliance gate) and ADR 0026 (finance operations).

## Context

- ALERT log lines and high-severity breaks page nobody. A ledger outage, a dispute, a sanctions hold or a client email
  that failed for good is seen only if Andre reads the logs (OPS-H2, CH-6..CH-8). The OFAC rule row expires on
  2026-12-25 and every client turns REFUSE that day with no warning (OPS-H5).
- The contracts count "business days" as weekdays that are not US federal holidays; Finance skipped weekends only.
- The MSA (15.5) promises deletion within 30 days of the end of the work, keeping only records the law requires. No
  tool existed (OPS-H8), and wave B added a client email address to Finance's store.
- The MSA (1.5(b), (e)) promises ZBM screens each owner a client discloses and never deals with an entity owned 50
  percent or more by sanctioned persons. Compliance screened the client's own name only (OPS-H4, OPS-M13).

## Decisions

### 1. Founder alerts: one dispatcher, in Finance, ticked by a cron service

The simplest design that is robust:

- **Where.** Finance already holds the Resend key, a volume and almost every alert source (breaks, holds, disputes,
  mail failures, unknown sessions, reconciliation). The dispatcher (`founder_alerts.py`) lives there. Compliance's
  open holds and expiring activation rules are read over the existing `finance_31` caller (`GET
  /compliance/v1/holds`, `GET /compliance/v1/readiness`): no new route, no second sender, no second key.
- **When.** A second Railway cron service, `ops-alerts`, runs the fin-cron image with `FIN_CRON_JOBS=alerts` every 5
  minutes (Railway's shortest cron interval) and calls `POST /fin/v1/jobs/founder-alerts/run` with its OWN caller
  token, `ops_alerts`, which reaches nothing else. fin-cron's hourly housekeeping is unchanged.
- **What.** A closed list of categories (ledger_unreachable, finance_high_break, compliance_hold, client_mail_failed,
  reconciliation_mismatch, payment_disputed, unknown_paid_checkout, compliance_unreachable, compliance_rule_expiring).
  The ledger is probed on its unauthenticated `/health`; "unreachable for N minutes" (default 15) and "Compliance not
  answering for N minutes" are tracked in the alert state.
- **Content.** Category, severity, source and ONE identifier per line (a break id, a hold id, an obligation id), plus
  counts. An identifier that does not match an id pattern, or contains `@`, is replaced by a hash. No amount, name,
  email address, card or bank data, hold kind or reason, or note can reach the email (the categories are fixed; the
  break's note and subject are never read).
- **Dedupe, rate limit, retry.** A condition is emailed once, then only as a 24-hour reminder or after it cleared and
  came back. At most one digest per 60 minutes and 30 a day. A digest that fails is kept frozen (same text, same
  Resend idempotency key) and retried after 5, 15, 30, 60, 120, 240, 480 minutes (about 16 hours, inside Resend's
  24-hour key window); then dropped with an ALERT line, its items due again.
- **Fail safe.** The job writes no book, no log line and no ledger event: its state is `founder_alerts.json` beside the
  log (atomic replace, 0600), so it works while the ledger is down and is exempt from the backup quiesce. An alert
  failure never blocks or undoes bookkeeping; every failure is logged (`ALERT finance: founder_alert_*`). A corrupt
  state file is set aside (worst case: an alert twice).
- **Off until configured.** `FOUNDER_ALERT_EMAIL` (set by hand), `FOUNDER_ALERT_FROM` (IaC: alerts@zbestmedia.com;
  only alerts@ or billing@ accepted) and the Resend key. A wrong value leaves alerts off with the reason in the job's
  answer; it never stops Finance from starting (alerts are not in the money path).

Rejected: an alert sender in every service (N keys, N digests, N rate limits); alert state in the hash-chained log
(needs the ledger, the thing most worth alerting about); a stateless cron that sends directly (no dedupe without
state); Resend idempotency as the dedupe (24 hours only, and a changed digest is a 409).

Residual risk: if Finance itself is down, no email can come (the cron runs fail red in Railway). A second,
independent path (for example the heartbeat monitor in railway.md, or a Railway notification) covers that.

### 2. Business days skip the federal legal public holidays as observed under 5 U.S.C. 6103

The MSA (section 1, changelog 45) defines a business day as "a weekday that is not a United States federal legal public
holiday, as observed under 5 U.S.C. 6103 (a holiday falling on a Saturday is observed on the Friday before, and one
falling on a Sunday on the Monday after)". `finance-py/src/bizdays.py` computes exactly that for any year from 1986:
the 11 holidays of 6103(a) (Juneteenth from 2022), a Saturday holiday observed the Friday before (New Year's Day 2028
is observed Friday December 31, 2027), a Sunday holiday the Monday after. Every contract deadline Finance computes
uses it: refunds (10 business days), client acknowledgements (2), billing disputes (15).

(The first version of this decision followed the Federal Reserve schedule, which does not move a Saturday holiday; the
MSA now settles the rule, and Finance matches it.)

Legal keeps its own seeded list (`legal-py/seed/us_federal_holidays.json`, pinned by hash, 2026-2030, the same rule);
a Finance test checks that both give identical dates for every year Legal's list covers. Legal's list ends with 2030
and Legal fails closed past it (`HolidaysUnknown`): it must be extended (or computed like Finance's) before late 2030,
when a 10-14 business-day window starts reaching 2031. Open item.

### 3. Client offboarding: a dry run, then a certified run; personal information never in the append-only stores

- **Redaction by design.** The ledger and every department's record log are append-only and hash-chained: they cannot
  forget. So personal information must never be written there. Finance's billing contact (added in wave B, not yet
  deployed) moves to a **contact vault**, one file per client (`pii/<sha256(client id)>.json`, 0600, atomic replace):
  the log and the ledger hold a `contact_ref`; the idempotency hash covers an HMAC keyed by a per-client salt kept in
  the same file, so deleting the file leaves nothing that links back (the earlier plain SHA-256 of the address is
  gone from the ledger payload). A test reads every log line and every ledger payload for the address, the name and
  their plain hashes. Compliance already stored names as hashes only; Legal stores references.
- **Finance** `POST /fin/v1/clients/{id}/offboarding` (Andre): the dry run lists what Finance holds (billing contact,
  unsent client email, invoices, payment records, credit notes and refunds, journal entries, tax jurisdiction), the
  action for each and a `plan_sha256`, and writes nothing. The real run names that hash (409 when anything changed),
  is blocked by an open Compliance hold or an open Stripe dispute, deletes the vault file first, then in one anchored
  commit withdraws unsent mail, marks the contact erased and records the **deletion certificate**: hash-only (client
  id hash, plan hash, counts deleted / withdrawn / kept with their retention, `pii_left_in_log`, and the peer
  services' certificate hashes) as ledger event `client_offboarded`.
- **Compliance** `POST /compliance/v1/clients/{id}/offboarding` (Andre): same dry run / plan shape; the real run closes
  the activation (the status refuses until a NEW activation ruling after it) and certifies. It deletes nothing: OFAC
  screening records are kept 10 years (MSA 15.5(a), changelog 46; 31 CFR 501.601 as amended effective March 21,
  2025); rulings 7 years. Finance likewise keeps the records of a payment held on sanctions grounds (a blocked or
  rejected transaction) 10 years and every other financial record 7 years; the plan and the certificate name each
  kind's retention.
- **The tool.** `devtools/client_offboarding.py plan` runs the dry runs (and reads SEO's tenants for the client), writes
  one private plan file, and prints the manual steps; `run` runs Compliance, then Finance with Compliance's certificate
  hash, and writes the combined certificate (hashes and counts only).
- **Not covered by a route** (manual, in the runbook): SEO (unbind Search Console, pause schedules; audit records stay
  in seo's append-only log), Revenue Recovery store data (detection has no volume; its finding records on the ledger
  carry Shopify order references: RR SOW 6.3(e) notice path), Legal (acceptances kept 7 years), delivery (not
  deployed), working copies, Gmail, Neon, Resend.

Open item (High, not built here): the department logs can only forget through a **log compaction** that rewrites a
log without a client's lines and re-anchors it on the ledger. Until then, data a department wrote into its log (SEO
domains and audit summaries; Revenue Recovery finding references on the ledger) is retained in that log and its
backups. It is business data, not personal information, but it is Client Data under MSA 15.5.

### 4. OFAC owner screening and the 50 Percent Rule in the client activation gate

- **Owner records (Andre enters them; clients attest).** `PUT /compliance/v1/clients/{id}/ownership` (Andre only):
  `client_kind` (entity or sole_proprietor), owners (id, person or entity, share of the parent in basis points, parent,
  country, SHA-256 of the name searched: never the name), `no_owner_25` for a widely held entity, and the client's
  attestation of the disclosure (method, time, evidence hash). Validation refuses duplicates, shares above 100% per
  parent, a person as parent, an unknown parent, cycles and chains deeper than 6. Recorded on the ledger first
  (`client_ownership_recorded`, counts and the record's hash), then the local log (kind `ownership`).
- **The same list engine.** Each owner is screened the way the client is: Andre's recorded OFAC Sanctions List Search
  (`POST /compliance/v1/sanctions/attestations` with `owner_id`; `subject_id` stays the client). The search must be for
  the owner's listed name (the hashes must match). An owner's search never stands in for the client's own (refused as
  `sanctions_attestation_id`, and by the activation status).
- **The gate (HR-14, seed v3).** Activation is refused unless: an ownership record exists; an entity has its
  attestation and either owners or `no_owner_25`; every entity owner holding 25% or more of the client (effective
  share through the chain) has its owners listed or `no_further_owners_25`; every owner has a no-match search no older
  than 30 days; and the blocked owners hold less than 50% in aggregate. Unmet items are `HR-14 / owner_screening`.
- **Hits.** Any non-no-match owner search opens a permanent hold on the CLIENT (no API release, as for the client's own
  hit; founder rule "never investigated around"). Kind `sanctions_match` (sanctions-grounded: Finance never refunds,
  Andre reports) when the blocked owners then hold 50% or more in aggregate, else `sanctions_owner_match` (refused, not
  blocked property). The activation status also reports sanctions-grounded whenever the current record's blocked share
  reaches 50%.
- **The 50 Percent Rule, computed.** Bottom up: an owner is blocked when its own search hit, or when blocked owners hold
  50% or more of it; the client's blocked share is the sum of its blocked direct owners' shares. So an entity owned 50%
  by a listed person, owning 50% of the client, blocks the client although the entity is on no list (OFAC FAQ 401).
  Shares are integers; effective shares exact fractions (no floats).
- **Re-screen every 90 days with the client.** An ALLOW ruling records the ownership record's hash and each owner's
  search id. The status refuses when the record changed since (re-activate), when any of those searches is older than
  90 days, and `valid_until` is the earliest of the client's and the owners' searches plus 90 days.
- **Seed v3.** HR-14's text names owners and the rule; two new reviewed parameters, `owner_disclosure_bp` (2500) and
  `aggregate_block_bp` (5000). `devtools/make_seed_v3.py` rebuilds it from v2 byte for byte; the pin moves to
  `02b8d0ae...7b2b`. Andre approves the seed as before. Fail-closed rules and the Andre-only release are unchanged.

Runbook: docs/runbooks/ofac-owner-screening.md (from the draft on `docs/ops-runbooks-2026-10-30`, now matching the
routes); railway.md step G points to it.

## Consequences

- New caller name `ops_alerts` at Finance; new Railway service `ops-alerts`; new Finance variables
  `FOUNDER_ALERT_EMAIL`, `FOUNDER_ALERT_FROM`, `FIN_ALERT_WINDOW_MIN`, `FIN_ALERT_REMIND_HOURS`,
  `FIN_ALERT_LEDGER_DOWN_MIN`, `FIN_ALERT_MAX_PER_DAY`. Runbook: docs/runbooks/founder-alerts.md.
- Refund and acknowledgement due dates move later by each bank holiday they span (never earlier).
- New Finance collection `client_offboardings`, new files under `FIN_DATA_DIR/pii/`, new ledger event types
  `client_offboarded` (Finance, Compliance) and `client_ownership_recorded` (Compliance). Runbook:
  docs/runbooks/client-offboarding.md (from the draft on `docs/ops-runbooks-2026-10-30`, now with the tool).

## Verification

finance-py: tests/test_founder_alerts.py, tests/test_bizdays.py; devtools/test_deploy_config.py (OpsAlerts).
compliance-py: tests/test_owner_screening.py, tests/test_client_offboarding.py (and the activation helpers in
test_service_area_gate.py now record a default ownership). finance-py: tests/test_client_offboarding.py.
devtools/test_client_offboarding.py. Nothing
has run on Railway or against the real Resend API.
