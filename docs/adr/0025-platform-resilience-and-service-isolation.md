# ADR 0025: platform resilience and service isolation (red team Oct 30-31 2026, fix wave A)

Status: accepted (fix wave A, branch `sec/resilience-isolation-2026-10-31`, local), amended after the AEGIS review
of b7108c52 (section 7; `scratchpad/night/aegis-resilience-waveA.md`, F-1..F-15). Inputs: the chaos, access and money
red-team reports of Oct 30-31 2026 (`redteam-chaos.md` CH-1, CH-2, CH-5, CH-6, CH-11, CH-12; `redteam-access.md`
RTA-1..RTA-6, RTA-8; `redteam-money.md` RTM-07).

## 1. Crash recovery without Andre (CH-1, CH-2)

**Problem.** Finance, Legal and Compliance record a write's rulings, then its `local_log_appended` anchor, on the
evidence ledger before they fsync the local line (record-first). A kill in that window (3 of 16 finance kills, 3 of 9
legal, 3 of 11 compliance) or a ledger answer lost mid-write without a kill (7 of 16 blips) left ledger evidence the
local log lacks. The start-up assessment calls that VOIDABLE and the service refused to start until Andre ran the
manual reconcile. The same assessment cannot tell that residue from another instance on the same ledger, a restored
older volume, a truncated or edited log, or a forged event, which is why it never voided anything itself.

**Decision.** An intent journal, `<log>.intent` (`src/crash_recovery.py`, byte-identical in the three services and
checked by hygiene L4). Before the ledger call for a ruling, an anchor, a rules/register version or a lease, the
event id is appended and fsynced; the committed line takes its ids out again; an id the ledger certainly did not
record (`took_effect: false`) is taken out at once. So the journal holds exactly the writes THIS log started and
never finished, and none of them was acknowledged (the 2xx is sent only after the local append).

- **At start:** when every voidable item is in the journal and nothing is fatal, the service rolls the residue back
  itself: the same recorded `reconcile` (ledger event + local line) Andre's route writes, by the evidence intelligence,
  marked `"automatic": true`, then re-assesses against the ledger, which must now be clean, and logs
  `{"alert":"crash_recovered",…}`. Section 7 (AEGIS F-2) narrows this to residue that never reached its
  anchor. Anything voidable the journal does not announce, or anything fatal, is real
  divergence: start-up still refuses (`StartupHeld`), with `{"alert":"log_divergence",…}` and the reason on /health
  (section 3). The manual reconcile is unchanged for that case.
- **While running (CH-2):** `LedgerWatch` (a daemon thread `main()` starts) probes the ledger every 15 s; while the
  journal holds a write or the ledger is unreachable it calls `settle_pending()` with backoff (2 s doubling to 60 s):
  residue is rolled back the same way, ids the ledger shows as committed or explained (an earlier attempt) are
  forgotten, ids absent from the ledger for 60 s are forgotten. A later restart therefore never trips over a blip.
- **Roll back, not roll forward.** Completing the half-written line would apply an operation whose preconditions were
  checked before the crash; voiding it is always safe because nobody was told it happened. Callers retry: a new log
  position gives new evidence ids (Finance, Legal); Stripe redelivers. Compliance's ruling ids are request-derived, so
  a voided id is never issued again (CH-11): the retry takes `…|retry<n>`, the first id no reconcile voided.

Rejected: a sidecar copy of the prepared line with roll-forward (the red team's option 1) — more state to verify and
an effect applied after its checks; voiding every unacknowledged ruling with no journal (option 2) — that would also
void another instance's or a restored volume's real, acknowledged rulings (CH-3 R1 voids paid invoices).

## 2. "Can it write?" is on /health (CH-6, CH-2)

`/health` of Finance, Legal and Compliance answers **503** `"status":"degraded"` with `degraded_reasons` while the
instance cannot write now: `ledger_unreachable` (last probe failed), `ledger_write_failing:<type>` (the last ledger
write failed for a reason other than a refusal of that one event), `pending_unreconciled_writes:<n>`,
`reconcile_required`, `log_write_fault`; `write_ok` is false. Reconcile mode (Andre's deliberate state) keeps its own
flag. The bodies carry codes and counts only (no ids: /health needs no token). The ledger's own `/health` answers 503
`last_append_failed` (cleared by the next append that persists) or `disk_low` (below `LEDGER_MIN_FREE_BYTES`, default
64 MiB). The heartbeat check (`docs/deploy/railway.md`, Compliance (38), the private heartbeat check) uses `curl -f`, so any 503 stops the pings.

## 3. Ledger unreachable at start: hold and retry, never exit (CH-5)

**Decision:** `src/startup_hold.py`. The API module builds the real app once; on `StartupTransient` (the ledger cannot
be read or written now) or `StartupHeld` (real divergence) the process does NOT exit. It serves a holding ASGI app —
`GET /health` 503 `{"status":"starting"|"refused","degraded_reasons":[<reason>],…}`, every other request 503 with
`Retry-After` and `took_effect:false` — and retries the build in a thread: 1 s doubling to 60 s for a transient cause,
every 5 min for divergence. When the build succeeds the real app takes every request and the readiness print, the
scheduler and the ledger watch start. Any other start-up error (configuration, seed hash, data directory busy) still
raises and exits: those never fix themselves.

**Why this and not a degraded read-only service:** a read-only mode would answer from a local log that has not been
verified against the ledger — the very check that refused it. Holding serves nothing unverified (fail closed), uses
no restart budget (Railway's ON_FAILURE gives up after 10 restarts and leaves the service Crashed), keeps the reason
visible to the heartbeat, and recovers by itself when the ledger returns. A deploy made while the ledger is down fails
Railway's deploy healthcheck and the old deployment keeps serving, which is the safe outcome.

## 4. Service isolation (RTA-1..RTA-5)

- **RTA-1 ledger ids per department.** ledger-rust's idempotency index is keyed by `(department, event_id)`. A write
  caller of one department can no longer pre-claim (and permanently 409) another department's deterministic id.
  Within a department the 200/409 rule is unchanged. Migration: none needed — every existing log has globally unique
  ids, hence unique per department; the index is rebuilt at open and the chain's canonical form does not involve it.
  Consumers already filter ledger reads by their own department (ADR 0003 M4 "Callers checked").
- **RTA-2 Compliance** `NAMED_ONLY = {seo_02, revenue_recovery}`: only `GET /compliance/v1/clients/{id}/activation`
  and `/health`, checked in middleware before routing (finance-py's seo_02 pattern), and again in `caller()`.
- **RTA-3 Legal** `NAMED_ROUTES`: `finance_31` and `compliance_38` reach only what their deployed clients call
  (document versions/current, acceptances, `register/{cq}` for Finance) plus routes that name them; neither can open
  a matter or read the register list, obligations, filings, memos, matters or takedowns. Compliance's tighten-only
  invalidation refuses `FIN-*` rows (FIN-CQ-11 is Andre's determination), and every invalidation logs
  `{"alert":"register_row_invalidated",…}`.
- **RTA-4** `src/access_redact.py` in every Python launcher: uvicorn's access line drops the query string and replaces
  every path segment that is not a plain route word with `:id`. No header is ever logged.
- **RTA-5** `src/token_rotation.py`: `<P>_SERVICE_TOKEN_NEXT`, `<P>_CALLER_TOKENS_NEXT` (accepted beside the current
  values during a rotation) and `<P>_ANDRE_APPROVAL_TOKEN_SHA256` / `_NEXT_SHA256` (Andre's token held as a hash; the
  plaintext variable can then be removed). Runbook: `docs/runbooks/token-rotation.md`.

## 5. Ledger time and external anchor (CH-12, RTM-07)

- `recorded_at = max(previous recorded_at, now)` with a `clock_skew` warning line. A forward clock jump cannot be told
  from time passing; entries after it carry that time until the wall clock catches up (the chain order, `seq`, was
  always the authority). The wall-clock value of a clamped entry is in the warning line, not in the entry, so the
  stored format and every existing hash are unchanged.
- `GET /ledger/anchor` returns `<entries>:<head_hash>` and, with `LEDGER_HEAD_SIGNING_KEY`, an HMAC-SHA256 signature.
  No Resend adapter exists on the ledger (Rust, no outbound client) and pushes from this repository are not allowed,
  so the external record was Andre's heartbeat monitor (superseded by section 7, F-5: Compliance stores it): a daily job stores the anchor where no service can write
  (`docs/deploy/railway.md`, Compliance (38), step 3 of the heartbeat check). `LEDGER_ANCHORED_HEAD` makes the ledger refuse to start behind or off the last
  stored anchor.

## 6. Supply chain (RTA-6, RTA-8)

- `devtools/check_docker_pins.py` fails CI (hygiene-static) on any `FROM` not pinned by digest. The golang and rust
  builder digests could not be looked up from the sandbox (registry unreachable), so the check is red until they are
  pasted in from the railway-images job's "base images actually used" step. `.github/dependabot.yml` (docker) then
  proposes digest bumps weekly.
- `devtools/osv_git_audit.py` asks OSV for advisories affecting delivery-py's git-pinned deer-flow commit (pip-audit
  cannot). `langchain-openai` is NOT unused: deer-flow's `deerflow.models.factory` imports it at module level.
  `langchain-deepseek` is reachable only through a model class path delivery's config never allows; dropping it needs
  `uv lock` with the CI-pinned uv 0.8.17 and a networked resolve, so it is left as a documented follow-up (docs/ci.md).

## 7. Amendments after the AEGIS review (F-1..F-15)

- **F-1, deployability.** `/health` answering 503 in reconcile mode made Andre's recovery deploy unactivatable
  (Railway activates a deployment only on a 2xx healthcheck). Now: `GET /livez` (200 whenever the process serves,
  holding app included) is Railway's `healthcheckPath` for finance, legal, compliance and the ledger
  (`.railway/railway.ts`, `devtools/test_deploy_config.py`); `/health` stays truthful for the heartbeat, and in
  reconcile mode it is 200 with `reconcile_mode`/`reconcile_required` (the voidable residue Andre is there to fix is
  not a reason for 503). The two tests loosened in wave A are reverted.
- **F-2, automatic rollback only when provably safe.** Section 1 claimed a restored older volume is held; a snapshot
  taken mid-write after which the write was ACKNOWLEDGED was in fact voided. Rule now (`crash_recovery.classify`):
  roll back automatically only residue that never reached its anchor (no `local_log_appended` and no version event of
  this log in the void set, no stray anchored line). Without its anchor a line can never have been appended, so nothing
  in the set was acknowledged. An interrupted write that reached its anchor is held (`interrupted_write_anchored`):
  a crash after the anchor and a restore look identical here, so Andre decides (Finance's restore lockdown is fix wave
  B's). The **volume generation marker** is the instance lease every boot writes to the volume's log and the ledger:
  a ledger lease of this log the volume does not hold (a restored or cloned volume after another boot) is
  `volume_generation_mismatch`, held. The `crash_recovered` alert names the voided events and their types.
- **F-3.** The ledger watch reads `/ledger/entries` WITHOUT the service lock, then takes the lock only to check the
  log and the journal did not move, classify and apply (`crash_recovery.Settler`); a read slower than 20 s is not acted
  on. A plain request is no longer queued behind a slow ledger read.
- **F-4.** The pin check covers `COPY --from=<image>` and `RUN --mount=…from=<image>` (stage names and indices exempt);
  `ghcr.io/astral-sh/uv:0.8.17` (delivery) is flagged with the three builders. CI prints the digests to paste
  (`check_docker_pins.py --print-digests`, railway-images job); docs/ci.md lists the four replacements. The job stays
  red until they are pasted.
- **F-5, the anchor is live, in the IaC.** Compliance's in-process scheduler stores the ledger's anchor daily (and at
  start) on Compliance's own volume (`ledger_anchor.py`, `ledger_anchors.jsonl`), each record HMAC'd with
  `COMPLIANCE_LEDGER_ANCHOR_KEY` (a key the ledger never holds) and chained to the previous one. Before storing, it
  checks with `GET /ledger/anchor?at=<n>` that the ledger still extends the last stored anchor (same hash at entry n,
  at least n entries); a rollback, a rewrite or a broken store is `ledger_anchor_mismatch` on Compliance's /health
  (503: the heartbeat stops) with an ALERT. So no anchor is ever copied by hand at a ledger redeploy;
  `LEDGER_ANCHORED_HEAD` remains an optional start-time floor. `LEDGER_HEAD_SIGNING_KEY` and
  `COMPLIANCE_LEDGER_ANCHOR_KEY` are `preserve()` in the IaC and in the runbook's Secrets table. Residual: someone with
  write access to BOTH the ledger's and Compliance's volumes and Compliance's key can still rewrite both.
- **F-6, time.** `recorded_at` is the wall clock as observed, corrected only where implausible: a forward jump of more
  than 5 minutes ahead of (previous stamp + real elapsed time, monotonic) is stamped at the plausible time; a previous
  entry more than 5 minutes in the future (stamped under a bad clock, e.g. before a restart) no longer pins later
  entries — they take the observed time and ordering is `seq`; a small backward step (≤ 5 min) stays monotonic. Any
  clock fault logs both readings and shows `"warnings":["clock_skew"]` on the ledger's /health for an hour. Tested for
  a 100-years-ahead entry, in-process and after a restart.
- **F-7.** Unauthenticated /health bodies carry fixed codes and counts only: the holding app shows
  `ledger_unavailable_at_start` / `log_divergence` / `interrupted_write_anchored` / `volume_generation_mismatch`;
  running services add `divergence:<code>`. The full reason, which can name event ids, is in the log. The ledger's
  `disk_low` is a fixed code (bytes in the log).
- **F-8.** Legal refuses Compliance's invalidation of a `FIN-*` target, of a `FIN-*` canonical row, and of any row a
  `FIN-*` row aliases (FIN-CQ-07/08/15 alias CQ-01/04/07). Rationale corrected: Finance does not read Legal's FIN rows
  (it has its own rules); the guard protects Legal's record of Andre's Finance determinations.
- **F-10, one-way ledger change.** With RTA-1 two departments may hold the same `event_id`. The previous ledger
  binary treats that as corruption and refuses to start, so once such a pair exists **the ledger cannot be rolled
  back to a pre-wave-A binary** (services/ledger-rust/README.md, "Rollback"). Roll forward instead.
- **F-11, rotation windows end.** Any `<P>_*_NEXT` value requires `<P>_ROTATION_ENDS_AT` (ISO 8601 with a zone, in the
  future, at most 14 days ahead); next values are refused after it, and a start with the variable past refuses (the
  `_NEXT` variable must be removed). `/health` shows `rotation_in_progress` while a window is open. A plaintext Andre
  token beside a hash of a different token refuses to start, so an old plaintext stops working once a hash is set.
- **F-12.** Guard tests for mutants M9, M10, M18 in legal-py and compliance-py; ledger mutant R5 (`last_append_failed`
  ignored) is killed by `health_tests` in `src/bin/server.rs`; only a 201 (a persisted append) clears the flag.
- **F-13/F-14.** c03's legal half is replaced by `c03L2_kill_legal_approval.py` (a real SIGKILL inside Andre's
  approval write, deterministic via the fault proxy). c01/c02 run against finance-sim, which has no start-up hold or
  ledger watch; those are covered in-process and on the production leg (c05).

## Consequences

- A kill or a blip inside a write costs one rolled-back operation, recorded on the ledger, instead of an outage until
  Andre reconciles. Real divergence still needs Andre, and now shows its reason on /health instead of in a deploy log.
- `/health` of the three services can be 503 while the process is up; anything that treated "process up" as "can
  serve" must read the body. The heartbeat does.
- One fsync more per ruling/anchor write (the intent line), next to an HTTP round trip to the ledger.
