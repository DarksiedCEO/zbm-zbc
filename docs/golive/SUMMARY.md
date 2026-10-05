# Go-live overnight summary (Oct 4–5, 2026)

Branch `golive-plan` (from `integration-2026-09-24` @ 013caff). **Docs only, no code changed.** Not merged anywhere.

## The short version

1. **You can't fix a client's store today, and nothing is close to it.** The "fix engine" (Dept 28, delivery-py) fixes
   *this repo's own code*, not client stores. The part that would change a Shopify store exists only as a safety gate
   that refuses everything. This is the biggest gap, and it's new information versus what we've been assuming.
2. **No Shopify connection, no way to collect money, no Revenue Recovery price.** Detection has only ever run on test
   fixtures.
3. **The fully automated path is ~9–14 weeks** of build (estimate), not the 1–3 weeks I said before. I said that before
   anyone had surveyed the code. This survey replaces it.
4. **A pilot path gets a first dollar in ~3–5 weeks** (estimate): a read-only Shopify app, a real scan, a findings
   report, fixes applied by the merchant or by hand, flat fee. It breaks two of your Sep 26 rules ("nothing manual",
   "fix engine first"), so it's your call. Everything it builds is also step one of the full path.
5. **`zbm-zbc` is a public repo.** Anyone can read all the source, the security findings, the AEGIS reports and your
   counsel questions. One click to make private (check Actions minutes first).

## Files

| File | What it is |
|---|---|
| `GOLIVE_PLAN.md` | 17 work items with what exists (cited), what's missing, estimates, order; two tracks; decisions |
| `ADR-draft-persistence.md` | Storage decision draft: extend the existing hash-chained logs now, add Postgres as a read model later |
| `COUNSEL_PACKET.md` | 51 counsel/CPA questions, deduplicated, grouped by what blocks the first payment; hand to a lawyer cold |
| `API_SURFACE.md` | All 364 endpoints across 13 services with request/response shapes, for the frontend |
| `REPO_CHECK.md` | `zbm-zbc` vs `zbestmedia` (old Aug TypeScript brand platform, unrelated code); live site is in a third repo, `zbestmedia-ui` |
| `_SURVEY.md` | Raw per-service fact survey behind the plan |

## Top 3 decisions, in order

1. **Pilot or not (D1).** Allow the Track B pilot (manual fix application, flat fee) as the first milestone? Decides
   whether revenue is ~1 month or ~3 months away.
2. **Send the counsel packet this week.** Counsel is likely the critical path. Item A1 (engagement letter with an AI-use
   clause) blocks every other answer in the software, so engage counsel first. The 4 questions the repo never raised
   (store-change liability, merchant customer data, RR fee basis, ADA) are in group A/C.
3. **Price (D2) and repo visibility (D5).** Set the Revenue Recovery fee model (flat audit fee recommended for the
   pilot) and make the repo private.

Then: hosting choice (D3), payment method (D4: ACH via an invoicing tool recommended), persistence (D6).

## What I verified vs. what I didn't

- **Verified by reading code (cited in the files):** every claim in "The short version" items 1, 2 and 5. Spot-checked
  directly: `action_execution.py` header, delivery-py README, finance line codes, absence of Shopify/Stripe code, repo
  visibility via GitHub API.
- **Verified on the web (Oct 4):** Shopify custom apps must be built in the Dev Dashboard since Jan 1, 2026; `read_orders`
  covers orders + abandoned checkouts for 60 days only; protected customer data needs approval. Links in the plan.
- **Not verified:** all time estimates (judgment); anything requiring a running system (nothing was run); the contents of
  the private `zbestmedia-ui` repo; the founder-decision docs that are cited in the repo but missing from it.
- **Housekeeping:** `integration-2026-09-24` still shows CI7-1 open in `OPEN.md`; the closure record is on `fix26b`
  (bfb042c), two docs-only commits ahead. Merge it with the next real change.
