# ledger-rust — the evidence ledger

The tamper-evident, hash-chained, append-only ledger every ZBM/ZBC service anchors its evidence to. Findings
(Revenue Recovery) and department events share one SHA-256 chain; each entry's hash covers its own fields and the
previous entry's hash. Design and every amendment: [ADR 0003](../../docs/adr/0003-money-decimal-and-ledger-events.md)
(section 13 is the Oct 6 2026 bug-sweep amendment). Test counts: [docs/test-counts.md](../../docs/test-counts.md).

## Run

```bash
cd services/ledger-rust
export LEDGER_SERVICE_TOKEN=<shared secret, at least 32 bytes>
cargo run --release --bin server
```

| Variable | Default | Meaning |
|---|---|---|
| `LEDGER_SERVICE_TOKEN` | — (required) | Shared bearer token, **at least 32 bytes** or the server refuses to start. Optional only when `LEDGER_CALLERS_FILE` is set and `LEDGER_ALLOW_SHARED_TOKEN` is not `1`. |
| `LEDGER_CALLERS_FILE` | unset | Per-caller tokens with department scopes (below). Unset: the shared token can do everything, and a startup WARNING says so. Set but empty: refuses to start. |
| `LEDGER_ALLOW_SHARED_TOKEN` | unset | `1`: with `LEDGER_CALLERS_FILE` set, still accept the shared token (unscoped). |
| `LEDGER_LOG_PATH` | `ledger_data/ledger.jsonl` | The log. `<log>.lock` and `<log>.head` live next to it. |
| `LEDGER_ALLOW_RESET` | unset | One-shot operator override, bound to one exact state: `<checkpoint>/<log head>` as printed by the refusal it answers (e.g. `3:1f0c…/1:9a2e…`). Accepts a log that does not reach its head checkpoint, records `reset_from` in the checkpoint, logs it and **exits without serving**; start again with it unset. Set when not needed (or `1`): refuses to start. |
| `LEDGER_MIGRATE_LEGACY` | unset | One-shot, bound to one log: `<entries>:<16 hex of the head hash>` as printed by the refusal. Creates the head checkpoint (with `migrated_from`) for a non-empty log that has none (written by a binary older than Oct 6 2026, or a deleted head file), logs it and **exits without serving**. Set when not needed: refuses to start. |
| `LEDGER_PORT` / `LEDGER_BIND_ADDR` | `8090` / `127.0.0.1` | Listen address. `LEDGER_PORT=0` picks a free port. |
| `LEDGER_PORT_FILE` | unset | Receives the bound port (a hint: check `GET /health`; ADR 0003 sections 9-11). |
| `LEDGER_MAX_CONNECTIONS` | `512` | Concurrent connections; beyond it an immediate 503. |

`LEDGER_ALLOW_SHARED_TOKEN` accepts only `1` (on) or `0`/empty/unset (off); `LEDGER_ALLOW_RESET` and
`LEDGER_MIGRATE_LEGACY` accept only `0`/empty/unset or a value of their printed shape; any other value refuses to
start. Every startup refusal (and a failed bind) is one `REFUSING TO START` line and exit 1.

## Routes

Every route except `GET /health` needs `Authorization: Bearer <token>` (401 otherwise).

| Route | Answers |
|---|---|
| `GET /health` | 200 `{"status":"ok","service":"ledger-rust"}` |
| `POST /ledger/append` | A finding. 201 entry; 400 invalid; 413 body over 64 KiB; 500 not recorded. With `Idempotency-Key: <finding_id>` (must equal the body's `finding_id`, else 400): 201 new, 200 identical retry (existing entry), 409 same `finding_id` with different content. Without the header every post is appended (unchanged). |
| `POST /ledger/events` | A department event, idempotent on `event_id`: 201 new, 200 identical retry, 409 conflicting content, 400 invalid. |
| `GET /ledger/entries` | No query: every entry the caller may read, as a JSON array (the whole ledger for the shared token and `read_all` callers, byte-identical to before). `?after_seq=&limit=&department=&event_type=`: entries with `seq > after_seq`, at most `limit` (default 1000, max 10000), filters match events only; the next page is `after_seq=<last seq>`, and fewer than `limit` entries means the end. Served from a per-department / per-event-type index (a page costs O(page + log n), not a scan). Unknown/repeated/malformed parameters: 400; a `department` outside the caller's read scope: 403. |
| `GET /ledger/verify` | 200 `{"valid":true,"entries":N}` / 409 `{"valid":false,"error":…}`. Incremental (verifies only what is new since the last verified head; the whole chain is verified at every start); `?full=1` re-verifies the whole in-memory chain AND re-reads and re-hashes the log from disk, which must end at the in-memory head. |
| `GET /ledger/head` | `{"entries":N,"head_seq":N-1,"head_hash":"…"}` — the durable head checkpoint (plus `migrated_from` / `reset_from` when an operator override was ever applied). |

With `LEDGER_CALLERS_FILE` set: 403 when a caller writes an event for a department it is not scoped to, writes a
finding without the `revenue_recovery` department, (read-only caller) calls any POST, or filters
`GET /ledger/entries` by a department it may not read. Reads are scoped: a caller's `GET /ledger/entries` returns
only events of the departments it may read, and findings only if those include `revenue_recovery`.
`/ledger/verify` and `/ledger/head` carry no department data and stay open to every authenticated caller.

## Per-caller tokens (`LEDGER_CALLERS_FILE`)

A JSON object keyed by the lowercase hex SHA-256 of each caller's token (the file never holds a token):

```json
{
  "<sha256 hex of the sales-py token>":     {"caller": "sales-py",        "departments": ["sales"],            "scope": "write"},
  "<sha256 hex of the orchestrator token>": {"caller": "orchestrator-go", "departments": ["revenue_recovery"], "scope": "write"},
  "<sha256 hex of a reporting token>":      {"caller": "sales-reporting", "departments": ["sales"],            "scope": "write",
                                             "read_departments": ["finance"]},
  "<sha256 hex of the dashboard token>":    {"caller": "dashboard",       "scope": "read", "read_all": true},
  "<sha256 hex of an auditor token>":       {"caller": "compliance-audit", "scope": "read", "departments": ["sales", "finance"]}
}
```

`printf %s "$TOKEN" | sha256sum` gives a key. A write caller must list at least one department (`[a-z0-9_]{1,64}`);
it writes and reads those, and may read `read_departments` too. A read caller reads its `departments`, or
everything with `"read_all": true` (dashboard, compliance, audit; read-only callers only — a write caller with
`read_all` refuses the start); one with neither refuses the start, as do
`read_all` together with `read_departments`, `read_departments` on a read caller, the same token hash listed twice,
unknown fields, an empty map or a malformed entry. The file is read once at start (rotate = edit + restart); the
startup log names every caller with what it writes and reads. Migration: list every caller, start with
`LEDGER_ALLOW_SHARED_TOKEN=1` while services move to their own tokens, then drop the flag. Every department service
in this repo reads only its own department's entries (its evidence audit filters on its `department`), so each needs
only its own department; the orchestrator reads findings (`revenue_recovery`).

**Tokens.** Every token — the shared one and each caller's — must be at least 32 bytes of a cryptographically random
source (e.g. `openssl rand -hex 32`, 256 bits): the ledger cannot rate-limit guesses, so the token's entropy is the
only defence against an online guess. The shared token's length is checked at start; a caller token's length is
checked when it is presented (only its hash is configured), and a configured token under 32 bytes is refused (401,
logged). Length is all the server can check — a long but guessable token (a word, a date) is not detected.

## Files next to the log, and what an operator does

- `<log>.lock` — the single-writer lock (flock). A second server on the same log refuses to start. Never delete it
  while a server runs; it is harmless to leave.
- `<log>.head` — the head checkpoint, rewritten (atomically) after every append. On start the log must reach it
  exactly, or be exactly ONE entry past it (a crash between the log fsync and the checkpoint rename; the checkpoint
  is moved up, logged). A missing log, a shorter log, a different entry at the checkpoint or a log two or more
  entries past it refuses to start. If the log was restored on purpose, restart ONCE with the
  `LEDGER_ALLOW_RESET=<value>` the refusal prints (bound to that checkpoint and that log head). A non-empty log with
  no head file (written by a binary older than Oct 6 2026, or a deleted head file — indistinguishable) refuses to
  start; restart ONCE with the `LEDGER_MIGRATE_LEGACY=<value>` the refusal prints. No log and no head file is a new
  ledger, logged as `EVENT {"event":"ledger_created",…}`. Every such decision is one `ledger-rust: EVENT {json}`
  line (`ledger_created`, `checkpoint_moved_up`, `legacy_log_migrated`, `operator_reset`) to alert on. The head
  file catches deletion, truncation and stale restores — not a forger who can rewrite both files; a signed,
  externally published head is future work (ADR 0003 section 13).
- `<log>.torn-<nanos>` — the bytes of a genuinely torn final line (a crash mid-append), moved aside on start. An
  unterminated final line that is a complete entry, or any blank line, is NOT moved aside: the start is refused and
  the file left untouched for inspection.

## Test

```bash
cargo test --locked && cargo clippy --locked --all-targets -- -D warnings
```

Integration tests spawn the real binary over real TCP (`tests/server_*.rs`); `tests/server_sweep_f.rs` holds one
test per Oct 6 2026 sweep finding, each failing against the 5d49ee9 binary, and one per Oct 7 2026 AEGIS finding
(`aegis_*`), each failing against the d6b1cd9 binary.
