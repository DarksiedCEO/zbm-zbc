# ADR 0025: platform resilience and service isolation (red team Oct 30-31 2026, fix wave A)

Status: accepted (fix wave A, branch `sec/resilience-isolation-2026-10-31`, local). Inputs: the chaos, access and money
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
  `{"alert":"crash_recovered",…}`. Anything voidable the journal does not announce, or anything fatal, is real
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
  so the external record is Andre's heartbeat monitor: a daily job stores the anchor where no service can write
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

## Consequences

- A kill or a blip inside a write costs one rolled-back operation, recorded on the ledger, instead of an outage until
  Andre reconciles. Real divergence still needs Andre, and now shows its reason on /health instead of in a deploy log.
- `/health` of the three services can be 503 while the process is up; anything that treated "process up" as "can
  serve" must read the body. The heartbeat does.
- One fsync more per ruling/anchor write (the intent line), next to an HTTP round trip to the ledger.
