# ADR 0021 — Out-of-Home & DOOH (15): planning and pricing (OOH-3)

Status: accepted for build, Oct 16 2026. Built on branch `ooh-dept-15-wave2` on top of OOH-2 (ADR 0020). **Not in
force.** No agent is built; nothing here buys media.

**Source caveat.** The master blueprint and the hardening directive were not available to this build as files; the
brief named OOH-3 only as "planning/pricing" and said to follow the blueprint. The scope below is therefore this
build's reading, built on what ADRs 0019 and 0020 already enforce, and every choice beyond them is listed under open
questions. Locked decisions that bind it: markup disclosed and set per deal from evidence (15% a suggestion, never
automatic); the hub never shows vendor cost; never invent rate or inventory data.

## Decisions

1. **The client's brief** (`POST …/campaigns/{cid}/brief`, preparers): budget cap (all-in, USD), the formats the client
   wants, a flight window (at most `policy.BRIEF_WINDOW_MAX_DAYS` = 731 days; each flight in it still at most 366), and
   the brief document's reference. Written while planning (`INTAKE`, `BRIEF_VALIDATED`, `PLAN_DRAFTED`,
   `CHANGE_REQUESTED`); after that `BRIEF_LOCKED` (a change goes through `CHANGE_REQUESTED`). Ledger event
   `campaign_brief_set`.
2. **Units fit the brief.** No unit before a brief (`BRIEF_REQUIRED`); the unit's format must be in the brief
   (`FORMAT_NOT_IN_BRIEF`); its flight, at registration and on every flight change, must lie in the window
   (`FLIGHT_OUTSIDE_BRIEF`). A new brief is refused while any live unit would fall outside it (AEGIS OOH-3/4 source
   F-1: withdraw or move the unit first), and the plan approval guard re-checks every line
   (`PLAN_UNIT_OUTSIDE_BRIEF`).
3. **Rate-card estimates** (`POST …/inventory/{uid}/rate-card-estimates`, preparers, method
   `RATE_CARD_PRORATED_DAILY`): a `VENDOR_COST_ESTIMATE` computed from the face's CURRENT sourced
   `rate_card_amount` and `rate_card_period_days` (ADR 0020), prorated by calendar day to the unit's flight,
   rounded half up to the cent: `amount × flight_days / period_days`. Class `ESTIMATED`; the record keeps the rate
   card values, both source ids, their date and the flight. No sourced rate card (or only half of one) is
   `RATE_CARD_UNKNOWN`: nothing is estimated from nothing. A flight is required. Ledger event
   `rate_card_estimate_recorded`. A manual `VENDOR_COST_ESTIMATE` (ADR 0019) stays possible but never counts in the
   plan (it is not bound to a rate card and a flight).
4. **The plan view** (`GET …/campaigns/{cid}/plan`, dashboard, agent runtime, Compliance; never the hub — it carries
   vendor cost). One line per live unit (not withdrawn, not unavailable) with its face, format, market, flight,
   proof requirement and cost basis, best first (`policy.COST_BASES`): its current vendor quote (not retired, for its
   flight, unexpired; a quote naming several units is split evenly and says so), else its latest rate-card estimate
   for its flight, else `UNKNOWN`. Totals: quoted, estimated, known, and the count of unknown lines. `budget_fit`:
   `WITHIN`, `OVER`, or `UNKNOWN` while any line is unknown (known cost is compared with the all-in budget, a necessary
   condition: the client's price adds the markup). A warning when one vendor's known total exceeds the single-purchase
   limit (`VENDOR_TOTAL_ABOVE_SINGLE_PURCHASE_LIMIT`: it cannot be bought in one authorization). The markup block
   shows the 15% suggestion as `suggested_applied: false` and the deal's markup once set; no price is computed from
   the suggestion.
5. **Plan approval guards** (`PLAN_DRAFTED → PLAN_APPROVED`, Andre, after ADR 0020's planning coverage): a brief
   (`NO_BRIEF`), no line of unknown cost (`PLAN_COST_UNKNOWN`), and known cost within the budget cap
   (`PLAN_OVER_BUDGET`).
6. **Markup from evidence** (`POST …/campaigns/{cid}/markup-evidence`, preparers): a record of what the deal's markup
   is set from — `COMPARABLE_DEALS`, `MARKET_RATE_SURVEY`, `COST_TO_SERVE` or `CLIENT_NEGOTIATION` — with its document
   reference. The commercial model's `markup_basis_ref` must name one of this campaign's records
   (`MARKUP_BASIS_UNKNOWN`; another campaign's record does not count). The markup percentage itself is still Andre's
   (the commercial model is his), disclosed, and checked to the cent against the quotes (ADR 0019).
7. Everything commits through `_commit` and replays through its `_a_*` handler (tested: identical snapshot and
   identical plan after restart).

## What OOH-3 does NOT do

- No audience, impressions, reach or frequency (no measurement provider is chosen).
- No optimisation or recommendation: the plan shows the faces preparers chose; it does not pick them.
- No client-facing proposal document; the sell price and proposal remain ADR 0019's (Andre's).
- No currency other than USD.

## Open questions for the founder

1. Is the brief's budget cap the client's all-in price (built) or the media cost?
2. Should a plan be approvable with rate-card estimates only (built: yes; quotes are required later, at
   `QUOTES_VALIDATED` and the purchase gate), or only once every line is quoted?
3. The markup-evidence kinds (`policy.MARKUP_EVIDENCE_KINDS`): are these the right four?
4. Proration by calendar day (built) versus the vendor's own period rules (4-week cycles, minimum terms).
