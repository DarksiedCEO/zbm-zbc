# ADR 0052 - SEO: Farrah, monthly revenue attribution with a confidence band

- Status: Accepted (built to frozen spec `farrah-spec/1`, approved by the owner 2026-11-13, all owner questions at
  their defaults; CLARIFICATIONS v1.1 GF-1..GF-5 applied provisionally, pending the owner's morning approval)
- Service: `services/seo-py` (Search & Answer Intelligence, department 2)
- Code: `src/agents/farrah.py` (pure), `src/svc_attribution.py` (service mixin), tenant fields `plan` and
  `revenue_basis` (`PUT /tenants/{tid}/plan`), the lead-source channel map (`PUT /tenants/{tid}/channel-map`)
- Suite: `tests/conformance/farrah/` (one test per spec item, the item id in the test name)
- Number: 0052 was the next free number on every branch at build time; renumber at merge if another round took it

## Context

The owner roster names Farrah "Revenue Attribution with confidence score"; the dashboard contract asks for Farrah's
revenue figure with a confidence band; the refresh tiers put Farrah monthly; the pricing ruling keeps revenue
attribution out of Entry and in from Growth up. ADR 0017 decision 5 separates effect classes ("credit is not
causation") and wave1 B-UL-03 forbids scores. Osei full (the only producer Farrah reads) is not on the base.

## Decision

1. **One job.** For one tenant on Growth or Full (or ZBM's own tenant) and one CLOSED month (its last day plus a
   3-day data lag before now, in the dataset's timezone), state the revenue the first-party records attribute to
   `organic_search` and `ai_referral` as a figure F, a band [L, U] and a closed label, with every assumption listed.
   `effect_class: attributed`, never incremental or causal (Q-FA-6: a later version, only with a client's written
   agreement to a holdout).
2. **A label, not a score** (X-FA-1, Q-FA-1): `HIGH` / `MEDIUM` / `LOW` / `INSUFFICIENT_EVIDENCE`; HIGH when
   U - L <= 10% of U, no completeness flag, the basis input FRESH on Osei's revenue panel and >= 30 revenue rows;
   MEDIUM when <= 35% and >= 10 rows; LOW otherwise; INSUFFICIENT_EVIDENCE when no VALID, recorded basis dataset
   covers the whole month, the basis input is STALE / FAILED_LAST / NOT_CONNECTED, or U = 0 (then every amount is
   `null`, never 0). The thresholds are `heuristic: true, calibrated: false`.
3. **The band.** L = F(c); U = F(c) + the basis source's unknown-channel revenue + the documented `(other)` row (or
   the documented share x the source's total) + revenue of rows dropped at Osei's caps + |D| x F(c) / total for an
   RC-4 difference D of this month. Sampled data: GO-2's sampling metadata carries no bounds, so the label is LOW
   with `incomplete:sampled`; dropped rows of unknown revenue: LOW.
4. **Money** (ADR 0003, GF-4): only decimal strings (or ints) are taken; a float, NaN / Infinity, more than 40
   significant digits or an exponent beyond +-40 refuses the document. Sums are exact (a context wide enough for
   every bounded amount, `Inexact` trapped); the one division uses precision 40, half-even; cents only in the
   display. USD only.
5. **Inputs as consumer** (CB-19). A frozen copy of Osei's key sets (`agents/farrah.py`, equal to
   `tests/conformance/data/osei_dataset_v1.json`). A dataset is used only when it is this tenant's, VALID, strict in
   every key and value, recorded by a `source_read_recorded` / `crm_import_recorded` record whose keyed HMAC (vault
   field `dataset`, Osei's W-OS-2 seal) matches, not of a quarantined read, and its window covers the whole month in
   its own timezone (a property timezone other than the window's: not covered). Anything else is refused whole and
   counted by code. Osei's store is `self.osei_source`, absent in production until Osei lands: every run is
   NOT_CONNECTED (GF-2). It is not a port; Farrah calls no port and spends nothing.
6. **Channels** (closed): organic medium or the source's organic group; an exact AI-referrer host (versioned list,
   "not complete"); `(not set)` / unassigned / direct with no landing data -> unknown; else other_known. CRM: utm
   first, then the tenant's declared lead-source map; unmapped -> unknown. Matching is by exact value after case and
   surrounding whitespace only (no confusable fold): lookalikes are `other_known`.
7. **Write paths.** `attribution_run_requested` (ids, month as its first day, basis, input dataset ids as their hex
   part, the keyed HMAC of what the run reads) before anything is computed; the result passes `guard_record` (every
   field but this service's own numbers and ids, which are shape-checked instead), is sealed whole in the tenant's
   vault (`revenue_attribution`), and `attribution_run_recorded` carries ids, labels, row counts, codes and the
   result's keyed HMAC: NO amount in the log or the ledger (Q-FA-5). Owners (dashboard, seo_agent, the hub for its
   own tenant) read the result through `owner_view`; Compliance (38) gets labels, counts and codes only; Finance
   (31) nothing.
8. **Runs.** One running run per tenant (409 RUN_IN_PROGRESS); a request key replays its first answer; a month
   whose inputs are unchanged answers its current run; changed inputs (another basis dataset, freshness,
   reconciliation, channel map) give a new run that `supersedes` the old one, which stays readable, marked
   `superseded_by`. Kill switches global, write, tenant, `capability:audit` and the new `capability:attribution`
   are checked at the request, between steps and at the commit; the plan is re-read at the commit (a downgrade
   mid-run refuses the run). A dead process's run is closed by the `interrupted-audits` job.

## Consequences

- Revenue attribution is visible to the dashboard as soon as Osei lands; until then it says NOT_CONNECTED, never a
  number.
- The core's sweep (`secret_guard.guard_record`) and the owner masker withhold digit runs of 9+ characters, so a
  result's amounts and ids are kept out of them and checked by shape instead (open question QB-FA-9 to the core).
- The war-room library gains `money-edge-cases`, `month-boundaries`, `plan-escalation`, `attribution-gaming` and
  `attribution-gaming-hosts` (seo-py driver actions `farrah_*`).
- Open questions (QB-FA-1..12) are listed in the build report; residuals R-FA-1..5 stand as the spec accepts them.
