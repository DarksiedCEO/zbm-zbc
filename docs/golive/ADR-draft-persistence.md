# ADR draft: persistence for go-live

Status: **DRAFT, not accepted.** Needs the founder's decision (D6 in `GOLIVE_PLAN.md`). Nothing here is built.
Date: Oct 5, 2026. Scope: the state the first Shopify Revenue Recovery client needs to survive restarts and host failure.

## Context (from the code)

The repo already has a persistence pattern; it is not "everything in memory":

| Component | State today | Source |
|---|---|---|
| ledger-rust | Hash-chained JSONL at `LEDGER_LOG_PATH`, fsync'd before success, torn-tail recovery, whole log loaded into memory at start, one chain for all departments/clients | `services/ledger-rust/src/persistence.rs:15-44,279`; `src/bin/server.rs:915-916` |
| compliance, verification, clipper-network, finance, legal, delivery | Hash-chained JSONL per service under `*_DATA_DIR`, every line anchored on the ledger first, chain verified at start, replayed into memory; **memory-only if the dir is unset** | e.g. `services/finance-py/src/store.py:1-19` |
| onboarding | In-process dicts only; "lost on restart … Persistence isn't built" | `services/onboarding-py/src/service.py:323-360`; README:426-435 |
| fulfillment, creative | In-memory only | `_SURVEY.md` |
| detection, orchestrator, dashboard | Stateless | `_SURVEY.md` |

No service has a database driver. The JSONL pattern was hardened across many AEGIS rounds (tamper evidence, torn writes,
ledger anchoring). Any choice should preserve those properties, not discard them.

New state go-live adds: per-merchant Shopify install records and access tokens (tokens belong in the vault, not here),
ingested store snapshots for each scan, client↔shop mapping, scan runs, report artifacts.

## Options

### Option 1: Extend the existing JSONL event-log pattern (recommended for Track B / first clients)
- Give onboarding the same `store.py` pattern (`ONB_DATA_DIR`), add one for the new Shopify connector service.
- Run every service with its `*_DATA_DIR` on one durable, backed-up volume; nightly + pre-deploy snapshots; offsite copy.
- Ingested Shopify snapshots go to write-once blob files (content-addressed by SHA-256), referenced from the log, the
  same idea legal-py already uses for documents (`services/legal-py/src/store.py:174`).
- **Pros:** smallest change; keeps tamper evidence and ledger anchoring intact; no new dependency in a stdlib-only
  design; the reviewed code paths stay the reviewed code paths.
- **Cons:** single writer per service (one instance; no horizontal scale); start-up replays the whole log (grows with
  history); no ad-hoc queries for reports or a portal; backup must capture every log *and* the ledger consistently, or
  anchoring checks will refuse start after a restore.
- **Limits to watch:** replay time per service (measure at each deploy), log size, restore drill.

### Option 2: PostgreSQL as the system of record
- Each service persists to its own schema in one managed Postgres; ledger stays the tamper-evidence anchor.
- **Pros:** durable, queryable, multi-instance, managed backups/point-in-time restore, standard tooling for a portal.
- **Cons:** rewrites the persistence layer of 6–7 heavily reviewed services; the hash-chain/anchor guarantees must be
  re-implemented on top of tables (append-only tables + per-row hashes) and re-reviewed; adds a driver to each stack
  (psycopg / pgx / sqlx); weeks, not days. Highest regression risk right before first revenue.

### Option 3: Hybrid: logs stay the source of truth, Postgres as a read model (recommended for Track A)
- Keep Option 1 as the authoritative, tamper-evident record. A projector tails each service log and writes query
  tables into Postgres for reports, the client portal and analytics. Postgres can be rebuilt from the logs at any time.
- **Pros:** portal/report queries without touching the reviewed write path; rebuildable; adds Postgres where its
  strengths matter (reads), not where the existing design is strong (evidence).
- **Cons:** two stores to operate; projection lag; the projector itself needs review.

## Recommendation (draft)

- **Now (Track B):** Option 1. Add the store to onboarding, set every `*_DATA_DIR` on a durable volume, back up logs +
  ledger together, and rehearse a restore before the first real merchant's data lands. Est. 5–8 days including a
  restore drill.
- **Track A, when the client portal is built:** Option 3: managed Postgres as a read model fed from the logs.
- **Not recommended now:** Option 2. It trades the strongest-reviewed part of the system for convenience, right
  before first revenue.

## How the Rust ledger fits
- Unchanged role: the cross-department tamper-evidence anchor. Every service log line is anchored there first.
- Needs: its log on the same durable volume, included in the same consistent backup; a documented restore order
  (ledger first, then services) so anchoring checks pass after restore.
- Known limit: one chain for all clients, whole log in memory. Fine for the first clients; revisit (per-tenant chains or
  checkpoints) when the log is large enough that start-up replay time matters. Measure, don't guess.

## Migration path
1. Add `ONB_DATA_DIR` store to onboarding, reusing the finance/legal `store.py` pattern; failing-first tests for restart
   survival (same style as the existing restart tests).
2. Deployment config sets every `*_DATA_DIR` and `LEDGER_LOG_PATH` on one volume.
3. Backup job: quiesce or snapshot the volume atomically; copy offsite; keep N days.
4. Restore drill: restore to a fresh host, start ledger then services, confirm every chain verifies and anchors.
5. Later (Track A): Postgres read model + projector, rebuilt from logs on demand.

## Open questions
- Retention: how long must merchant order snapshots be kept, and when must they be deleted? (Counsel A2/A8; data
  minimization argues for short retention and no customer PII.)
- Encryption at rest for the volume and backups: required by the merchant agreement? (Counsel A2.)
- Single-host risk acceptable for the pilot? (Founder.)
