# ADR 0052 - SEO: Farrah, monthly revenue attribution with a confidence band

- Status: Accepted (built to frozen spec `farrah-spec/1`, approved by the owner 2026-11-13, all owner questions at
  their defaults; CLARIFICATIONS v1.1 GF-1..GF-5 approved by the owner 2026-11-14 (OD-4); round-3 owner decisions
  OD-1..OD-5 of 2026-11-14 recorded as v1.1 clarifications GF-6..GF-8, see "Owner decisions (round 3)")
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
3. **The band.** F(c) = the net revenue of channel c. L = F(c) + the NEGATIVE part of the unknown-channel rows
   (the sum of min(0, row) over unknown rows: an unattributed refund may be c's; OD-5, GF-8). U = F(c) + the
   POSITIVE part of the unknown-channel rows (the sum of max(0, row); OD-2, GF-7) + the documented `(other)` row (or
   the documented share x the source's total) + revenue of rows dropped at Osei's caps + |D| x F(c) / total for an
   RC-4 difference D of this month. `totals.unknown_revenue` stays the net unknown sum. Both are exact Decimal sums. Sampled data: GO-2's sampling metadata carries no bounds, so the label is LOW
   with `incomplete:sampled`; dropped rows of unknown revenue: LOW.
4. **Money** (ADR 0003, GF-4): only decimal strings (or ints) are taken; a float, NaN / Infinity, more than 40
   significant digits or an exponent beyond +-40 refuses the document. Every sum, every product (share x total,
   |D| x F) and every addition into the band is exact: one context of precision 1,000 with `Inexact` trapped (the
   bounds keep every value far below it; the precision is a ceiling, not a cost). The absolute value of D is
   `Decimal.copy_abs()` (round 2, F-1: the built-in `abs` rounded to the thread's 28 digits). The ONE rounding on the
   path is the division |D| x F / total, precision 40, half-even; cents only in the display. USD only.
   *Measured (round 2):* the review's 30-digit difference gives U exactly (difference 0 against an exact rational
   oracle); the review's 40-digit-by-127-digit case (10^40 with a 1.2...e-40 row and a 40-digit D) is exact and no
   longer fails the run; `test_B_FA_24_A_FA_01_reconciliation_difference_exact_at_every_magnitude` (5 magnitudes
   from 10^-36 to 10^39) is within half a unit in the 40th digit of the division, i.e. exact elsewhere;
   `test_B_FA_24_no_builtin_on_the_money_path` pins that no `abs` / `sum` / `round` / `float` is on it.
5. **Inputs as consumer** (CB-19). A frozen copy of Osei's key sets (`agents/farrah.py`, equal to
   `tests/conformance/data/osei_dataset_v1.json`). Before anything recurses into a consumed document (validation,
   the HMAC's canonical JSON, a copy), an iterative walk bounds it: nesting deeper than 16, more than 2,000,000
   values, a non-string key or a non-JSON value refuses it whole (round 2, F-3; a valid document nests 5 deep).
   There is no separate label-count cap (round 3, J-2: the spec sets none; MAX_NODES bounds the whole document;
   50, 51 and 200 labels are used, `test_AC_11_J_2_*`).
   Any other exception while reading one document refuses that document (`DATASET_INVALID` etc.), never the run.
   A dataset is used only when its producer, Osei, is ACTIVE in the department manager (round 2, F-2: restricted or
   retired through the lifecycle route -> every Osei document is refused whole with CB-2's code `AGENT_RESTRICTED`,
   counted, and nothing of it is recorded; restricted mid-run -> the run is refused at commit), it is this tenant's,
   VALID, strict in every key and value, recorded by a `source_read_recorded` / `crm_import_recorded` record whose
   keyed HMAC (vault field `dataset`, Osei's W-OS-2 seal) matches, not of a quarantined read, and its window covers
   the whole month in its own timezone (a property timezone other than the window's: not covered). Anything else is
   refused whole and counted by code. Osei's store is `self.osei_source`, absent in production until Osei lands:
   every run is NOT_CONNECTED (GF-2). It is not a port; Farrah calls no port and spends nothing.
   *Measured (round 2):* depths 3,000 / 10,000 / 100,000 in `surface.parts` answer 201 with the dataset refused;
   120 fuzzed pathological shapes (deep rows, labels, records, reconciliation, freshness, totals, completeness;
   cycles; int keys; objects; bytes; sets; tuples; 5,000-key parts; 100,000 labels; NaN floats) answer 201, 0 raises;
   restricted and restricted-then-retired Osei through `POST /agents/osei/lifecycle` give `inputs: []`.
6. **Channels** (closed): organic medium or the source's organic group; an exact AI-referrer host (versioned list,
   "not complete"); `(not set)` / unassigned / direct with no landing data -> unknown; else other_known. CRM: utm
   first, then the tenant's declared lead-source map; unmapped -> unknown. Matching is by exact value after case and
   surrounding whitespace only (no confusable fold): lookalikes are `other_known`.
7. **Write paths.** `attribution_run_requested` (ids, month as its first day, basis, input references, the
   reference of what the run reads) before anything is computed; the result passes `guard_record` (every field but
   this service's own numbers and ids, which are shape-checked instead), is sealed whole in the tenant's vault
   (`revenue_attribution`), and `attribution_run_recorded` carries ids, labels, row counts, codes and the result's
   keyed HMAC: NO amount in the log or the ledger (Q-FA-5). Owners (dashboard, seo_agent, the hub for its own tenant)
   read the result through `owner_view` and see the dataset ids; Compliance (38) gets labels, counts, codes and the
   references only; Finance (31) nothing.
   **Recorded forms (round 2, F-4).** The sink guard refuses a hex value made only of decimal digits (its digit rule)
   but takes a service id `seo-<listed prefix>-<40 hex>` whatever its digits (`sink_guard._is_service_id`: the class
   every run id already uses, QB-FA-9). So Farrah never records a value whose acceptance depends on its own digits:
   a dataset id is recorded as its reference `ledger.derived_id("att", "dataset", id)` (accepted by the owner as
   the recorded form of W-FA-1's "input dataset ids" and of B-FA-28's non-owner `inputs`: OD-1, GF-6) (about 1 valid dataset id in
   1,850 has an all-digit 16-hex part and was refused, 503 SINK_REFUSED on every retry); what a run reads is recorded
   as `derived_id("att", "inputs", keyed HMAC)`; a keyed HMAC (result, channel map) is recorded as itself when it
   holds a letter and as `derived_id("att", "hmac", HMAC)` otherwise (all 64 hex characters digits, about 1 in
   10^13), and the reader accepts either form (`svc_attribution.mac_ok`, constant-time). No sink rule changed.
   *Measured:* every id-bearing field forced to digits only (run id, dataset references, inputs reference, result
   and channel-map HMACs) commits, verifies on read and passes the sink scan.
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

## Owner decisions (round 3, 2026-11-14)

| Decision | Recorded as | What it settles |
|---|---|---|
| OD-1 | v1.1 GF-6 | Dataset ids are RECORDED (log, ledger, non-owner views) as references `seo-att-<40 hex>` = `ledger.derived_id("att", "dataset", id)`; owner views keep the real ids (decision 7) |
| OD-2 | v1.1 GF-7 | QB-FA-6 fixed in the formula: U adds only the positive part of the unknown-channel rows, so U is always a true ceiling (decision 3) |
| OD-5 | v1.1 GF-8 | True floor too: L = F + the negative part of the unknown-channel rows (decision 3); the band holds the truth in 100% of cases, with or without refunds (O-FA-1 / B-FA-21) |
| OD-3 | owner acceptance of R-5 | U < 0 states the negative figure with the label LOW |
| OD-4 | owner approval | CLARIFICATIONS v1.1 GF-1..GF-5 for the new-7 specs approved |

## Disclosures (round 3): residuals in this ADR's own numbering, mapped to the review's

The R-numbers below are THIS ADR's (the builder's, following the probe order of the ported review suite
`tests/conformance/farrah_rv/`); they are not the review's. The review of record for round 1
(`claude/review-2026-11-14-farrah-3b99c9d5.md`) numbers its residuals RR-1..RR-8 here, and the round-2 review's
observations are O-1, O-2. The mapping table says where each one is disclosed. Probes report these residuals and do
not assert them, unless a test is named.

| ADR R | Residual | Size (measured) |
|---|---|---|
| R-1 | CLOSED (round 3, OD-2 / OD-5): refunds in the unknown channel used to put U below the truth (QB-FA-6, a strict xfail at round 2) | 0 truths outside [L, U] in the review oracle (RV_N 2,000: 3,196 checked, 410 with a hidden refund, 32 below F, all inside); QB-FA-6 case now L 70.00, F 100.00, U 140.00 |
| R-2 | A documented share with no Osei totals is applied to the basis total (the rows), not to the true total | the review's case: U = 150 against a true 200 (short by 25% of the truth); 5 of 2,000 datasets on the review's seeds 50000.. (`share_nototal`); 0 on seeds 1000.. |
| R-3 | A negative `(other)` row or negative dropped revenue is clamped to 0, not added literally (U never narrows) | the clamped term itself: 30.00 in the probe |
| R-4 | A net-negative F(c) with a reconciliation difference: the term \|D\| x F / total is negative, is not added, and RECONCILE_DIFFERENCE_WIDENED is not listed | \|D\| x \|F\| / total: 25.00 in the probe |
| R-5 | U < 0 (net refunds): the label is LOW and the amounts are stated; the spec makes the figure null only for U = 0. Accepted by the owner (OD-3) | the probe's U = -50.00 is stated |
| R-6 | A revenue row is a row whose revenue is not zero: zero-valued rows do not count toward the 10 / 30 row thresholds | the probe: 1 row counted of 41 (40 zeros), label LOW |
| R-7 | The one rounding on the path, the division \|D\| x F / total, at precision 40 half-even (GF-4) | at most half a unit in the 40th significant digit of the term (5 magnitudes); 0 elsewhere (30-digit D: difference 0) |
| R-8 | Thresholded data with no documented share: nothing quantifies the withheld rows, so U does not include them (the label cannot be HIGH) | the withheld revenue (unknown to the service by definition) |
| R-9 | A-FA-01's 10^6-row money sum sits near its 5 s bound under load; only the lead lane asserts the bound (worst of the 200/2,000 money cases <= 5.0 s), the 10^6-row exactness test does not time itself | round-1 review: 4.5-5.5 s under load; round 3: `exact_sum` of 10^6 rows best 2.689 s, worst 3.191 s of 3 at load 1.6-2.0 on 2 cores |
| R-10 | CLOSED (round 3, J-5): "won deals only" (B-FA-13) was not pinned: the old test's open / lost rows had no closed_date, so the month check dropped them with or without the stage rule | with the stage rule removed, the old test passed and the new `test_B_FA_13_crm_open_and_lost_deals_with_a_closed_date_in_the_month_are_not_counted` failed (1 of 3 B-FA-13 tests) |
| R-11 | Round 1 changed core files outside G's Farrah scope: `sink_guard.py` (the one token `"att"` added to `SERVICE_ID_PREFIXES`; no rule changed) and the wiring files `api.py`, `config.py`, `models.py`, `reasons.py`, `service.py`, `svc_manager.py`, `svc_tenants.py` | 8 files, +89 / -10 lines in 3b99c9d5; none changed in rounds 2-3 |
| R-12 | The OD-5 floor widens the band by the unattributed refunds, so U - L grows and a label can drop (HIGH -> MEDIUM -> LOW) | the width grows by \|sum of the negative unknown rows\| exactly; 0 when no unknown row is negative |
| R-13 | Osei's records feed is bounded as ONE document: one malformed record entry (depth > 16, a non-string key) refuses the whole feed (RECORDS_INVALID), every dataset is then DATASET_NOT_RECORDED and the tenant-month is INSUFFICIENT_EVIDENCE. Fails closed; the spec does not say whether records are one document or many | one bad entry in N: all N records discarded (review RS-1 probe) |
| R-14 | Refused-document counts are not part of the inputs signature: a later invalid document with nothing new selected answers the earlier run (B-FA-29) and is not counted in a new run's `inputs_refused` | the new refusal is not shown until the inputs change |

Mapping, review number -> this ADR:

| Review | Review's residual | This ADR |
|---|---|---|
| RR-1 | extreme amounts and a 40-digit share -> RUN_FAILED | fixed in round 2 (F-1, decision 4); the one rounding left is R-7 |
| RR-2 | share with no Osei totals -> U can sit below the truth | R-2 |
| RR-3 | negative widening terms clamped to 0 | R-3 |
| RR-4 | zero-revenue rows are not revenue rows | R-6 |
| RR-5 | U < 0 -> LOW with the negative figure | R-5 (OD-3) |
| RR-6 | 10^6-row sum 4.5-5.5 s against the 5 s bound under load | R-9 |
| RR-7 | "only won deals count" not pinned | R-10 (closed by J-5) |
| RR-8 | sink_guard token and wiring files outside G scope | R-11 |
| O-1 (round-2 review) | one malformed record discards every record | R-13 |
| O-2 (round-2 review) | a replay on an unchanged signature hides new refusal counts | R-14 |
| H-1 (round-2 review) | the 50-label cap was not in the spec | removed (J-2, decision 5) |
| H-2 (round-2 review) | W-FA-1's expected value used the code's own `dataset_ref` | the test now uses `ledger.derived_id("att", "dataset", id)` and checks two datasets give two refs (J-3) |
| (none) | builder-only | R-1 (closed), R-4, R-8, R-12 |

Review measurements at round 2 (for the record): 0 oracle mismatches over 2,000 generated datasets on the service
path (3,196 truths checked); 0 planted-amount hits in the ledger, Python logging, the data directory, the
Compliance / other-tenant / tenant / department / export views; AC-1 + AC-2 1,360 runs with 0 readable; depths 900,
3,000 and 100,000 answer 201; an all-digit dataset id answers 201.
