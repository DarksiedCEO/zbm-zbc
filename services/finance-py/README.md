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
reversals, direct ACH, any rail/bank/tax/vault/GL/CN/Legal wiring) refuse to start. The full list is `src/config.py`.

## First steps for Andre

1. `GET /fin/v1/rules` → approve the seed proposal: `POST /fin/v1/rules/decisions` quoting its `content_sha256`.
2. Counsel/CPA memos become rows: amend `FIN-CQ-01` / `FIN-CQ-11` with `status: verified` and
   `parameters.memo_ref`, approve with `acknowledge_weakening: true`.
3. `POST /fin/v1/controls/FC-05/results` — the token-roster access review (every 90 days).
4. Rate cards: `POST /fin/v1/rate-cards/proposals` then `/decisions`; profiles: `PUT /fin/v1/campaigns/{id}/commercial-profile`.

## The payout week (scheduler + Andre)

`POST /fin/v1/reconciliations/run` (daily) → `POST /fin/v1/payout-runs` (maker; a batch `proposed` with a
`content_sha256`) → Andre `POST /fin/v1/payout-batches/{id}/decision` (checker) → scheduler
`POST /fin/v1/treasury/funding` + Andre's funding decision (F4a) → after `FIN_RELEASE_DELAY_H` (12 h) the scheduler
`POST /fin/v1/payout-batches/{id}/release` (every gate re-runs per item) → rail webhooks via `rail_gateway`
(`paid` / `failed` / `returned` / `destination_changed`). Andre's token on the release route is refused (403); no
caller token can approve.

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

`devtools/live_server.py` runs the service with the test fakes and a settable clock (devtools only, never
production). `devtools/gen_rules_seed.py` regenerates the seed (its SHA-256 is pinned).
