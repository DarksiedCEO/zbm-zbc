# finance-py — Finance (31)

ZBC's and ZBM's books, ZBC's segregated client deposits, creator payables from V&I certifications, the weekly payout
under maker-checker, clawback netting, tax records, daily reconciliation to zero and the month-end close.
Architecture and every choice the spec left open: `docs/adr/0009-finance-department-architecture.md`.

**Not live.** Rails, bank, tax agent, GL, vault, Clipper Network, Legal, People and push are fail-closed stand-ins;
the V&I and Compliance thin clients are unwired unless configured. On day one nothing accrues, activates, issues,
reconciles or pays. The unlock list is in ADR 0009.

## Run

```bash
cd services/finance-py/src
FIN_SERVICE_TOKEN=... FIN_ANDRE_APPROVAL_TOKEN=... \
FIN_CALLER_TOKENS='{"scheduler":"...","creative_production":"...","onboarding":"...","clipper_network":"...",
                    "compliance_38":"...","verification_integrity":"...","rail_gateway":"...","bank_feed":"..."}' \
LEDGER_SERVICE_URL=http://127.0.0.1:8080 LEDGER_SERVICE_TOKEN=... FIN_DATA_DIR=/var/lib/zbc/finance \
python3 -m api                                   # 127.0.0.1:8410 (FIN_BIND_ADDR, FIN_PORT)
```

Every token ≥ 32 printable ASCII characters and all distinct, or the service refuses to start. Without
`FIN_DATA_DIR` the log is in memory (`/health` says `in_memory: true`) and nothing survives a restart. Options it
cannot honour (a custody model other than `own_deposit`, card prepayments, late fees, a refund fee, a reserve, rail
reversals, direct ACH, any rail/bank/tax/vault/GL/CN/Legal wiring) refuse to start. The full list is `src/config.py`;
every variable it reads is also named here (wave 25: 35 were not, `tests/test_env_documented.py` now fails on any):

- Founder-locked, one value accepted (anything else refuses to start): `FIN_REVENUE_MODEL` (`principal`),
  `FIN_CUSTODY_MODEL` (`own_deposit`), `FIN_STRIPE_LOSSES` (`stripe`); `FIN_UNMATCHED_TIN_POLICY` (`block`, or
  `withhold_24`); `FIN_GL` (`qbo`, or `none`; the QBO adapter is a stand-in).
- Not built, so setting them refuses to start: `FIN_RAIL_STRIPE`, `FIN_RAIL_TROLLEY`, `FIN_BANK_FEED`,
  `FIN_TAX_AGENT`, `FIN_VAULT`, `FIN_CN_URL`, `FIN_LEGAL_URL`, `FIN_PEOPLE_URL`, `FIN_PUSH_URL`,
  `FIN_IDENTITY_HMAC_KEY`; `=1` refuses for `FIN_RAIL_REVERSAL_ENABLED`, `FIN_CARD_PREPAYMENTS`, `FIN_LATE_FEES`;
  `FIN_REFUND_ADMIN_FEE_PCT` and `FIN_RESERVE_PCT` must stay 0. `FIN_RAILS` (`stripe,trolley`) may only name a
  subset of those two.
- Schedule: `FIN_RUN_WEEKDAY` (`FRI`), `FIN_RUN_LOCAL_TIME` (`10:00`), `FIN_RECON_LOCAL_TIME` (`07:00`),
  `FIN_CLOSE_WORKDAYS` (5, 1..10).
- Money limits (money strings): `FIN_LIMIT_PAYEE_RUN` (2500.00), `FIN_LIMIT_PAYEE_30D` (10000.00),
  `FIN_LIMIT_FIRST_PAYOUT` (500.00), `FIN_LIMIT_BATCH_NET` (50000.00), `FIN_LIMIT_BATCH_ITEMS` (500, at most
  500), `FIN_RESTRICTED_BUFFER` (0.00, kept back from what a sweep may move), `FIN_1099NEC_THRESHOLD_2026`
  (2000.00; `FIN_1099NEC_THRESHOLD_2027` … `_2030` optional), `FIN_PAYEE_CHANGE_COOLING_OFF_H` (72, 24..720).
- `FIN_SEED_PATH` (`seed/fin_rules_seed.json`): another file only under the unpinned-seed rule of ADR 0009.
- Server tuning, read once at start by `src/serve.py` (a non-numeric or non-positive value refuses to start): `FIN_REQUEST_HEAD_TIMEOUT_SECONDS` (10), `FIN_KEEP_ALIVE_TIMEOUT_SECONDS` (5), `FIN_LIMIT_CONCURRENCY` (128 open connections, then 503), `FIN_SWITCH_INTERVAL_SECONDS` (0.001; only 0.0001 .. 0.05 starts, checked in force and printed: the check shared by every launcher, `src/launch_guard.py`, fix wave 26b) and `FIN_DRAINS_MAX` (512 concurrent graceful-close drains).

## First steps for Andre

1. `GET /fin/v1/rules` → approve the seed proposal: `POST /fin/v1/rules/decisions` quoting its `content_sha256`.
2. Counsel/CPA memos become rows: amend `FIN-CQ-01` / `FIN-CQ-11` with `status: verified` and
   `parameters.memo_ref`, approve with `acknowledge_weakening: true`.
3. `POST /fin/v1/controls/FC-05/results` — the token-roster access review (every 90 days).
4. Rate cards: `POST /fin/v1/rate-cards/proposals` then `/decisions`; profiles: `PUT /fin/v1/campaigns/{id}/commercial-profile`.

## The payout week (scheduler + Andre)

`POST /fin/v1/reconciliations/run` (daily) → `POST /fin/v1/payout-runs` (maker; a batch `proposed` with a
`content_sha256`) → Andre `POST /fin/v1/payout-batches/{id}/decision` (checker; above `FIN_DUAL_HUMAN_THRESHOLD` the
second approver sends its OWN request, `POST /fin/v1/payout-batches/{id}/second-approval` with only its token) → scheduler
`POST /fin/v1/treasury/funding` + Andre's funding decision (F4a) → after `FIN_RELEASE_DELAY_H` (12 h) the scheduler
`POST /fin/v1/payout-batches/{id}/release` (every gate re-runs per item) → rail webhooks via `rail_gateway`
(`paid` / `failed` / `returned` / `destination_changed`). Andre's token on the release route is refused (403); no
caller token can approve.

## Money that comes back, and sweeps (AEGIS round 17, ADR 0009 amendment)

- A client deposit the bank returns (ACH return): `POST /fin/v1/receipts/{receipt_id}/return` (bank_feed or Andre)
  posts F1r. If creators were already accrued against it, a deposit shortfall opens and blocks runs, releases,
  sweeps and refunds until Andre's `POST /fin/v1/treasury/top-ups` names its `shortfall_id`.
- A margin sweep (and funding, top-ups, refund payments) is posted and anchored BEFORE the bank is asked; a bank
  refusal is undone by a recorded reversal; an unknown bank outcome keeps the posting, opens a break and retries with
  the same key. `sweepable` already subtracts sweeps that are proposed, approved or in flight.
- Treasury liabilities are absolute per sub-ledger; reconciliation L4 compares every sub-ledger, not only totals.
- New settings: `FIN_MAX_RATE_PER_1000` (`"1000.00"`), `FIN_MAX_CERTIFIED_VIEWS` (10^12), `FIN_WITHHOLDING_BASIS`
  (`gross`; `gross_minus_netting` only with CPA row FIN-CQ-16 verified).

## Media buys and client receipts (ADR 0009 amendment, Oct 5 2026)

ZBM buys media as principal, prepaid, ACH or wire only; the vendor is paid only from cleared money (FIN-31).
`POST /fin/v1/media-buys` (Andre: client, media type, vendor ref, description, flight, vendor cost, optional
`markup_pct`, `display` breakout|blended, `legal_ref`) → Finance drafts the prepayment invoice → Andre issues it
(`/fin/v1/invoices/{id}/decision`, F12) → the bank feed matches the payment (F11a; buy `prepaid`) → after the hold,
Andre records what he paid the vendor (`POST /fin/v1/media-buys/{id}/vendor-payments`, F12v) → once the vendor is paid
in full and the flight has run, `POST /fin/v1/media-buys/{id}/delivery` posts revenue and cost together (F12r).
`POST /fin/v1/media-buys/{id}/cancel` voids an UNPAID buy (F12c); `GET /fin/v1/media-buys/{id}` reads one.
Every matched payment makes a client receipt; the scheduler sends it with
`POST /fin/v1/client-receipts/{id}/send` (client-mail stand-in: nothing is sent, it stays `pending_send`).
Settings: `FIN_MEDIA_DEFAULT_MARKUP_PCT` (`"15.00"`), `FIN_MEDIA_MAX_MARKUP_PCT` (`"100.00"`, at most `"500.00"`),
`FIN_MEDIA_RELEASE_HOLD_BD` (5 business days, 2..10: the Nacha window for reversing an erroneous ACH credit). Card: only on a Revenue Recovery-only invoice, and still off
(D11).

## Reconciling the local log with the ledger

Identical to compliance-py / verification-py. If start-up refuses with a VOIDABLE problem (a commit that failed
after its anchor, a truncated tail, a newer lease from another instance, a version event with no local decision),
restart with `FIN_RECONCILE_MODE=1` (only reads and the reconcile route answer), read `GET /fin/v1/reconcile`
(Andre's token), then `POST /fin/v1/reconcile` with exactly the `head_sha256`, `void_lines` and `void_event_ids` the
plan lists; restart without the flag. FATAL problems (an unanchored local line, a missing cited event, foreign
anchors, an empty log against a ledger that anchors one) cannot be reconciled: restore the data directory.

## Tests

```bash
cd services/finance-py && python3 -m pytest -q          # no network (a socket guard fails any attempt)
LEDGER_BIN=/path/to/ledger-rust/target/release/server python3 devtools/live_run.py --ports 19450,19451,19452,19453
```

The run's work directory (ledger and service logs) is removed when the run ends, passed or failed; set
`LIVE_WORK_DIR=<dir>` to keep it inside `<dir>` (the run prints where; fix wave 26b, C6-2). This script read
`FIN_LIVE_WORKDIR` before (C5-7); that name still works, with a deprecation notice.

Test counts: [`docs/test-counts.md`](../../docs/test-counts.md), generated by CI (no hand-written count here: the
one this line carried went stale, wave 25). Fix 18 (Sep 27, 2026, commit `e3088a2`) added the 59 tests of `tests/test_aegis_r17.py`;
its live run was 33/33 with the review-10 ledger binary, on ports 19550-19553 passed with `--ports` (the default
is 19450-19453, as above).

`devtools/live_server.py` runs the service with the test fakes and a settable clock (devtools only, never
production). `devtools/gen_rules_seed.py` regenerates the seed (its SHA-256 is pinned).
