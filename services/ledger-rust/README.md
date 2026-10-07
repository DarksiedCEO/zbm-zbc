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
| `LEDGER_CALLERS_FILE` | unset | Per-caller tokens with department scopes (below). Unset: the shared token can do everything, and a startup WARNING says so. |
| `LEDGER_ALLOW_SHARED_TOKEN` | unset | `1`: with `LEDGER_CALLERS_FILE` set, still accept the shared token (unscoped). |
| `LEDGER_LOG_PATH` | `ledger_data/ledger.jsonl` | The log. `<log>.lock` and `<log>.head` live next to it. |
| `LEDGER_ALLOW_RESET` | unset | `1`: operator override — accept a log that does not reach its head checkpoint (logged loudly). Unset it again afterwards. |
| `LEDGER_PORT` / `LEDGER_BIND_ADDR` | `8090` / `127.0.0.1` | Listen address. `LEDGER_PORT=0` picks a free port. |
| `LEDGER_PORT_FILE` | unset | Receives the bound port (a hint: check `GET /health`; ADR 0003 sections 9-11). |
| `LEDGER_MAX_CONNECTIONS` | `512` | Concurrent connections; beyond it an immediate 503. |

Switches (`LEDGER_ALLOW_RESET`, `LEDGER_ALLOW_SHARED_TOKEN`) accept only `1` (on) or `0`/empty/unset (off); any
other value refuses to start. Every startup refusal (and a failed bind) is one `REFUSING TO START` line and exit 1.

## Routes

Every route except `GET /health` needs `Authorization: Bearer <token>` (401 otherwise).

| Route | Answers |
|---|---|
| `GET /health` | 200 `{"status":"ok","service":"ledger-rust"}` |
| `POST /ledger/append` | A finding. 201 entry; 400 invalid; 413 body over 64 KiB; 500 not recorded. With `Idempotency-Key: <finding_id>` (must equal the body's `finding_id`, else 400): 201 new, 200 identical retry (existing entry), 409 same `finding_id` with different content. Without the header every post is appended (unchanged). |
| `POST /ledger/events` | A department event, idempotent on `event_id`: 201 new, 200 identical retry, 409 conflicting content, 400 invalid. |
| `GET /ledger/entries` | No query: the whole ledger as a JSON array (unchanged). `?after_seq=&limit=&department=&event_type=`: entries with `seq > after_seq`, at most `limit` (default 1000, max 10000), filters match events only; the next page is `after_seq=<last seq>`, and fewer than `limit` entries means the end. Unknown/repeated/malformed parameters: 400. |
| `GET /ledger/verify` | 200 `{"valid":true,"entries":N}` / 409 `{"valid":false,"error":…}`. Incremental (verifies only what is new since the last verified head; the whole chain is verified at every start); `?full=1` re-verifies everything. |
| `GET /ledger/head` | `{"entries":N,"head_seq":N-1,"head_hash":"…"}` — the durable head checkpoint. |

With `LEDGER_CALLERS_FILE` set: 403 when a caller writes an event for a department it is not scoped to, writes a
finding without the `revenue_recovery` department, or (read-only caller) calls any POST.

## Per-caller tokens (`LEDGER_CALLERS_FILE`)

A JSON object keyed by the lowercase hex SHA-256 of each caller's token (the file never holds a token):

```json
{
  "<sha256 hex of the sales-py token>":   {"caller": "sales-py",        "departments": ["sales"],            "scope": "write"},
  "<sha256 hex of the orchestrator token>": {"caller": "orchestrator-go", "departments": ["revenue_recovery"], "scope": "write"},
  "<sha256 hex of the dashboard token>":  {"caller": "dashboard",       "scope": "read"}
}
```

`printf %s "$TOKEN" | sha256sum` gives a key. A write caller must list at least one department (`[a-z0-9_]{1,64}`);
unknown fields, an empty map or a malformed entry refuse the start. The file is read once at start (rotate = edit +
restart). Migration: list every caller, start with `LEDGER_ALLOW_SHARED_TOKEN=1` while services move to their own
tokens, then drop the flag.

## Files next to the log, and what an operator does

- `<log>.lock` — the single-writer lock (flock). A second server on the same log refuses to start. Never delete it
  while a server runs; it is harmless to leave.
- `<log>.head` — the head checkpoint, rewritten (atomically) after every append. On start the log must reach it:
  a missing log, a shorter log or a different entry at the checkpoint refuses to start. If the log was restored on
  purpose, restart ONCE with `LEDGER_ALLOW_RESET=1`. A log written by a binary older than Oct 6 2026 has no head file;
  one is created on first start (logged). The head file catches deletion, truncation and stale restores — not a
  forger who can rewrite both files; a signed, externally published head is future work (ADR 0003 section 13).
- `<log>.torn-<nanos>` — the bytes of a genuinely torn final line (a crash mid-append), moved aside on start. An
  unterminated final line that is a complete entry, or any blank line, is NOT moved aside: the start is refused and
  the file left untouched for inspection.

## Test

```bash
cargo test --locked && cargo clippy --locked --all-targets -- -D warnings
```

Integration tests spawn the real binary over real TCP (`tests/server_*.rs`); `tests/server_sweep_f.rs` holds one
test per Oct 6 2026 sweep finding, each failing against the 5d49ee9 binary.
