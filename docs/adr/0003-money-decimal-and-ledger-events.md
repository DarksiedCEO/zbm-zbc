# ADR 0003 — Exact money (Decimal) end to end, and the ledger events endpoint

**Status:** Accepted (Sep 24, 2026)
**Context:** README gap #6 (money was binary `float` across `detection-py`,
`float64` in `orchestrator-go`, `f64` in `ledger-rust`, `number` in the
dashboard). The founder approved fixing it. In the same pass the evidence
ledger gains a generic `POST /ledger/events` endpoint so other departments
(Onboarding, Creative) can record hash-chained facts. Both are fixed
cross-service contracts (build contracts, sections 1 and 2); this ADR
records how they are implemented and why the change is backward compatible.

## 1. Money wire format

A JSON money value is a **string** matching `^(0|[1-9][0-9]*)\.[0-9]{2}$`,
e.g. `"12.30"`. No sign, no exponent, no leading zeros, exactly two
fraction digits — and, since the section 1a amendment, at most 15 integer
digits (amounts < 10^15 dollars). Positive-only fields (`LabeledValue.amount_usd`, prices,
order values, contracted values) additionally reject `"0.00"`.

| Layer | Representation | Where |
|---|---|---|
| Python `detection-py` | `decimal.Decimal`, quantized to `0.01` with `ROUND_HALF_UP` wherever money is created or computed | `src/zbm_schema/money.py` (`Money`, `PositiveMoney`, `to_money`, `quantize_money`, `format_money`, `percent_of`) |
| Go `orchestrator-go` | `client.Money`, an opaque validated string; the orchestrator does no money arithmetic, it passes the string through byte-for-byte | `internal/client/money.go` |
| Rust `ledger-rust` | `Money(String)`, validated; the ledger does no money arithmetic | `src/money.rs` |
| TS `apps/dashboard-ts` | `amount_usd: string`, displayed verbatim (`"$" + amount`); never parsed to a JS number; no totals are computed | `src/lib/money.ts` |

Python input rules (amended by fix wave 1, see section 1a): a `str` is
accepted ONLY in the canonical wire form above — the same verdicts as Go,
the dashboard and the ledger (`fixtures/money_vectors.json`); strings are
never rounded (`"12.3"`, `"12.345"`, `"012.30"`, `"1.00\n"` are rejected).
`Decimal` and `int` are accepted; `float` only through `str(value)` (never
`Decimal(float)`), so a fixture JSON number `49.99` becomes exactly
`Decimal("49.99")`. Decimal/int/float values with more than two fraction
digits are rounded half-up to cents (`Decimal("2.675")` → `2.68`,
`1.005` → `1.01`). When a request body is validated from JSON text (every
detection-py HTTP route), a JSON number for a money field is rejected with
422, as Go and the ledger already do. `bool`, NaN, ±Infinity, exponent
notation, whitespace, thousands separators and negative values (including
`"-0.00"` / `"-0.004"`) are rejected. `quantize_money` never returns a
signed zero.

Computation rules:
- Subtotals: `sum(unit_price * quantity)` in Decimal, exact.
- Percent discounts: `percent_of(amount, pct)` = `amount * Decimal(str(pct)) / 100`,
  quantized half-up **per discount line**, and the running remainder is
  reduced by that recorded cent amount (how a cart engine records stacked
  discounts). `percent_off` itself is a percentage, not money, and stays a
  JSON number.
- Contract drift: exact Decimal subtraction.

Explanation text and the Hallucination Agent agree by construction: every
agent writes dollar figures with `format_money` (always two decimals), and
the check extracts every `$…` figure verbatim (commas removed, full
fraction captured) and requires the exact canonical string of
`recoverable_value.amount_usd` to be among them. `"$12.3"` does not satisfy
a `12.30` claim; there is no float tolerance.

Go rejects a JSON number for `amount_usd` with an explicit error (accepting
one would reintroduce float ambiguity), and decodes loosely typed
pass-through payloads with `json.Decoder.UseNumber()` so no JSON number
passes through `float64` on the way back out. The Rust ledger's
`POST /ledger/append` also rejects a JSON number with 400.

## 1a. Contract amendment: money magnitude bound (fix wave 1, Sep 24 2026)

**Finding F14:** detection-py returned 500 for `unit_price_usd =
"1" + "0"*30 + ".00"`. `Decimal.quantize` needs the result's coefficient to
fit the context precision (28 digits by default); above that it raises
`decimal.InvalidOperation`, which is not a `ValueError`, so pydantic did not
turn it into a 422. The wire regex admitted amounts of any length, so no
finite precision could cover "any value the contract admits".

**Amendment:** every money amount is **less than 10^15 dollars**. The
largest valid amount is `"999999999999999.99"`; the wire pattern becomes

    ^(0|[1-9][0-9]{0,14})\.[0-9]{2}$        (at most 18 characters)

10^15 dollars is far above any real order, subscription, contract or
recoverable value, and keeps every amount at 17 significant digits.

| Layer | Enforcement |
|---|---|
| Python `detection-py` | `to_money` rejects a string outside the pattern and any Decimal/int/float that is ≥ 10^15 after rounding to cents (`ValueError` → 422). An `Order` whose subtotal (computed in exact integer cents) exceeds the maximum is rejected at validation (422), so quantity × price can never leave the range. |
| Go `orchestrator-go` | `ParseMoney` / `Money.UnmarshalJSON` reject it (`ErrInvalidMoney`); a detection response carrying one fails the scan (502), so it is never passed to the ledger. `client.MaxMoney`. |
| TS `apps/dashboard-ts` | `isMoneyString` / `formatUsd` refuse to display it (`MAX_MONEY`, `MONEY_PATTERN`). |
| Rust `ledger-rust` | **Adopted in fix wave 2.** `Money::parse` (and so `parse_positive`, the only gate for a NEW `amount_usd` on `POST /ledger/append`) rejects more than 15 integer digits → 400; `money::MAX_MONEY`. Every `ledger_append_expected` verdict of `fixtures/money_vectors.json` is asserted by `money::tests::ledger_append_verdicts_match_shared_money_vectors` (deserialize + validate, the append route's exact path) and over real HTTP by `server_hardening::money_bound_append_verdicts_match_shared_vectors_over_real_http`. The bound is **not** applied to persisted entries: the fix-wave-1 binary accepted over-bound canonical strings, and a legacy number such as `1e20` renders as `"100000000000000000000.00"`; both are hashed bytes, so `deserialize_persisted_amount` reads strings through `Money::from_persisted_str` (canonical shape, no bound). Proven by `tests/fixtures/ledger_v3_overbound_strings.jsonl`, written by the real fix-wave-1 binary (commit 23a1752), which still loads, verifies and extends. orchestrator-go's read route keeps flagging any such recorded amount as `amount_out_of_contract`. |

**Explicit Decimal context (Python):** every money operation runs under
`zbm_schema.money.MONEY_CONTEXT` (precision 50, `ROUND_HALF_UP`, traps
InvalidOperation / DivisionByZero / Overflow) via `money_context()`, never
the ambient thread context. Precision 50 is enough for exact results:
amounts have ≤ 17 significant digits, sums/differences of in-range amounts
≤ 18, a line product exists only for an order already checked to be ≤ the
maximum, and `percent_of` multiplies ≤ 17 digits by a float repr of ≤ 17
digits (≤ 34) and divides exactly by 100. The only rounding is the
deliberate half-up quantize to cents. Tested with the ambient precision
lowered to 5 (`tests/test_money_bounds.py`).

**Shared vectors:** `fixtures/money_vectors.json` holds string and JSON-value
vectors with the verdict for a zero-allowed money field, a positive-only
field, and the ledger's `POST /ledger/append`. detection-py
(`tests/test_money_vectors.py`, including the real HTTP route: 200 or 422,
never 500), orchestrator-go (`internal/client/money_vectors_test.go`) and the
dashboard (`tests/money.test.ts`, `npm test`) all run against it.

## 2. Ledger backward compatibility (the hash proof)

Before this change a finding's canonical hash string contained
`format!("{:.2}", amount_f64)` (or `null`). It now contains the stored
money string. Two properties make every existing chain still verify:

1. **Two-decimal amounts:** for every amount with exactly two decimals, the
   canonical string is byte-identical to `format!("{:.2}", s.parse::<f64>())`.
   `money::tests::two_decimal_strings_match_old_f64_formatting` checks this
   exhaustively for every cent value from 0.00 to 100,000.00 (10,000,001
   values) plus a geometric spread of large amounts up to about 10^12 dollars.
2. **Legacy persisted entries:** a log line with a JSON number `amount_usd`
   (and no `kind`) is loaded through `deserialize_persisted_amount`, which
   converts the number with exactly the old call, `format!("{:.2}", f64)`.
   The canonical string is therefore the old one by construction, including
   for amounts the old API accepted that were not two-decimal (`12.345`,
   `2.675`, `0.30000000000000004`, `1e20`). The on-disk legacy lines are
   never rewritten.

Proof by test, not just argument:
- `lib::tests::chain_built_with_old_hashing_verifies_with_new_code` —
  reimplements the old `compute_hash` verbatim (from commit 9531fc2), builds
  a 12-entry chain in the old JSON shape, loads and verifies it with the new
  code, then extends it with new findings and an event and re-verifies.
- `lib::tests::new_finding_hash_equals_old_hash_for_two_decimal_amounts`.
- `tests/fixtures/legacy_ledger_v1.jsonl` — an 11-entry log written by the
  **actual pre-change server binary** (built from commit 9531fc2, fed over
  real HTTP with numeric amounts incl. `0.1`, `0.30000000000000004`,
  `1234567.89` and a null). It was then loaded and verified by that same old
  binary (`{"entries":11,"valid":true}`) before being checked in.
  `persistence::tests::legacy_persisted_file_from_old_binary_loads_verifies_and_extends`
  and `tests/server_events.rs::legacy_log_from_old_binary_is_served_and_extended`
  (new binary, real socket) load it, verify it, extend it and re-open it;
  `tampered_legacy_numeric_amount_is_detected` shows a one-cent edit to a
  legacy number still breaks the chain.

**Negative legacy amounts (corrected in fix wave 1, AEGIS F6).** This ADR
originally said a legacy line whose number formats negative (`-5.0`, `-0.0`)
would refuse to load, on the assumption that none existed. That was wrong
in the way that matters: the 9531fc2 binary accepted ANY finite JSON number
(checked against the real old binary: `-0.0`, `-5`, `-0.001` all got `201`
and a valid chain), so such logs can exist, and refusing them bricks the
ledger. `Money::from_legacy_f64` now keeps the exact old rendering for
every finite `f64`, sign included: `-0.0` → `"-0.00"`, `-5` → `"-5.00"`,
`-0.001` → `"-0.00"`, `-12.345` → `"-12.35"`, `-0.005` → `"-0.01"`.
`GET /ledger/entries` shows that string verbatim, so what is shown is
exactly what the old hash covered. Such a string can only come from a
legacy line: a line with `kind` must carry a strict string amount, and
every new append still requires a positive two-decimal string (`"-5.00"`,
`"-0.00"`, `"0.00"` → `400`). Consumers should expect a non-canonical,
possibly negative `amount_usd` only on legacy entries (log lines written
before Sep 24 2026, which have no `kind` on disk; the API reports them as
`"kind":"finding"`).
Proof: `tests/fixtures/legacy_ledger_v2_negatives.jsonl`, 15 entries
written by the real 9531fc2 binary over HTTP (the old binary's own
`/ledger/verify` said `{"entries":15,"valid":true}`), containing `-0.0`,
`-5`, `-0.001`, `-12.345`, `-0.005`, `-1e-9` and the earlier positives
(`120.0`, `54.38`, `0.1`, `0.30000000000000004`, `1234567.89`, `2.675`,
`1e20`, `null`). Loaded, verified, served, extended and re-opened by
`persistence::tests::legacy_negative_amount_log_from_old_binary_loads_verifies_and_extends`
and `tests/server_hardening.rs::f6_legacy_log_with_negative_amounts_from_old_binary_is_served_and_extended`.

## 3. Event records (`POST /ledger/events`)

Request (all fields required, unknown fields rejected via
`deny_unknown_fields`): `event_id` (1–128 `[A-Za-z0-9._:-]`, idempotency
key), `department`, `event_type`, `actor` (1–64 `[a-z0-9_]`), `subject_id`
(1–128 `[A-Za-z0-9._:-]`), `payload_sha256` (64 lowercase hex),
`summary` (1–280 Unicode scalar values, no control characters: C0, DEL, C1).

Responses: `201` new entry; `200` existing entry when the same `event_id`
was recorded with identical content (nothing written); `409` when the same
`event_id` exists with different content (nothing written); `400`
validation / malformed JSON; `401` missing or wrong bearer token; `413`
body over 64 KiB.

Design:
- **One shared chain.** `LedgerEntry` is an enum serialized with a `kind`
  tag (`"finding"` / `"event"`); events and findings take the next `seq`
  and chain to the previous entry's hash regardless of kind. A line with no
  `kind` is a legacy finding. `/ledger/verify` recomputes every entry of
  both kinds, checks `prev_hash` links, and (new) checks `seq` equals the
  entry's position.
- **Domain-separated event hash.** Event canonical string:
  `event|{seq}|{event_id}|{department}|{event_type}|{actor}|{subject_id}|{payload_sha256}|{summary}|{recorded_at_rfc3339}|{prev_hash}`.
  A finding canonical string always starts with its decimal `seq`, an event
  one with the letter `e`, so the two can never collide. Every field before
  `summary` is restricted to a charset without `|`, and the two after it
  cannot contain `|`, so the string parses back unambiguously even though
  `summary` may contain `|`.
- **The ledger never stores the payload**, only its SHA-256 (computed by the
  caller as SHA-256 of `json.dumps(payload, sort_keys=True,
  separators=(",", ":"), default=str)`), plus a short human summary.
- **Idempotency survives restart.** An `event_id → position` index is
  rebuilt from the log on every open. A log containing the same `event_id`
  twice is treated as corrupt (fail closed), since the append path can
  never write one.
- **Durability before visibility**, same as findings: the entry is written
  and fsync'd before it enters memory; a disk failure returns 500 and the
  caller must treat the event as not recorded.
- No Go client method for events: the orchestrator does not record events.
- Reading is not writing (fix wave 1): orchestrator-go's
  `GET /revenue-recovery/findings` reads `GET /ledger/entries` (entries of
  kind `"finding"`; events are counted, not shown) plus `GET /ledger/verify`
  and never calls a ledger write endpoint. The dashboard renders that route.
  A scan — which records every finding it detects — is only
  `POST /revenue-recovery/scan`; any other method is 405. Before this, each
  dashboard page view ran a scan and appended ~10 duplicate findings.
  Python callers use the `LedgerClient.record_event(...)` protocol defined
  in the build contracts (implemented by the calling services, not here).

## 4. Finding canonical form must be unambiguous (fix wave 1, AEGIS F7)

The finding hash covers
`{seq}|{finding_id}|{agent_id}|{entity_id}|{leak_category}|{amount}|{vc}|{dc}|{ts}|{prev}`
with a missing optional value written as `null`. That format cannot change:
every existing chain was hashed with it. But as written it is ambiguous:
`agent_id="agent-A"`, `entity_id="ord_1|ord_999"`, `dc=None` and
`agent_id="agent-A|ord_1"`, `entity_id="ord_999"`, `dc="null"` produce the
same string, so the same hash. AEGIS forged exactly that on disk and the
log opened and verified.

The canonical string maps back to exactly one entry if and only if **no
string field contains `|`** and **no optional field (`value_classification`,
`decision_confidence`) is the literal string `"null"`**. (`amount` is a
validated money string or a legacy `{:.2}` rendering, neither of which can
be `null` or contain `|`; `seq`, the RFC3339 timestamp and the hex hashes
cannot contain `|`.) With no `|` inside fields there are exactly nine
separators, so any re-split must produce the same fields.

Rules:
- **Append** (`POST /ledger/append`, and `PersistentLedger::append` itself
  for library callers): `400` if any of `finding_id`, `agent_id`,
  `entity_id`, `leak_category`, `value_classification`, `decision_confidence`
  contains `|` or a control character (C0, DEL, C1), or if an optional field
  is the string `"null"` (send JSON `null`). Real callers' values
  (`aff-ord_1002`, `discount-misuse-v1`, `observed`, `very_high`, …) are
  unaffected.
- **Load and `verify_chain`**: a finding that breaks the ambiguity rule is
  refused (startup refuses; `/ledger/verify` reports it), **legacy or
  not**. In addition, a line must have the shape one of the two real
  writers produced: no `kind` → numeric or null `amount_usd` (the old
  `Option<f64>`); `kind:"finding"` → string or null `amount_usd` and no
  control characters. Legacy lines may contain control characters (the old
  binary did no validation, and they do not make the string ambiguous).

**Decision on legacy entries that contain `|` or `"null"`.** The old binary
validated nothing, so it could have written such an entry legitimately
(`tests/fixtures/legacy_ambiguous_pipe.jsonl` and
`legacy_ambiguous_null.jsonl` were written by the real 9531fc2 binary and
it verified them). But such an entry is, byte for byte, indistinguishable
from a forged re-split of another entry with the same hash: no rule can
accept one and refuse the other, because they share a hash, and a forger
can write any JSON formatting the old binary could. Requirements (a) never
accept a forged re-split and (b) load every real legacy log therefore
conflict for exactly these entries. **We choose (a): they are refused
(the ledger does not start) with a message naming the field and the word
"ambiguous".** No real caller ever produced one: detection-py ids, agent
ids, categories and the value/confidence enums contain no `|` and never
the string `"null"`. If an operator ever meets this refusal, the entry must
be resolved by a human against an independent record, not by the ledger.

**Same class, swept:** event canonical strings were only unambiguous
because append validates event fields, but a loaded event was never
re-validated — and `summary` may contain `|`, so a forger could shift
`department`…`payload_sha256` one slot right into the summary with the
same hash (reproduced in
`lib::tests::event_resplit_forgery_is_refused_by_verify_and_load`, which
fails on the pre-fix code). Every event entry is now re-validated with the
full `EventInput::validate` rules on load and in `verify_chain`, and
`PersistentLedger::append_event` validates its input itself.

Regression tests: `tests/fixtures/aegis_forged_resplit.jsonl` (the AEGIS
forgery file) refuses to open (`persistence::tests::aegis_forged_resplit_log_refuses_to_open`,
`tests/server_hardening.rs::f7_aegis_forged_resplit_log_refuses_to_start`);
`lib::tests::aegis_resplit_and_null_forgery_is_refused_by_verify_and_load`
builds the colliding pair in memory and shows both are refused.

## 5. Crash safety and a known HTTP limitation (fix wave 1)

**Torn final line (AEGIS F5).** An append returns success only after the
whole line **and its trailing newline** are written and `fsync`'d. So an
unterminated final segment of the log was never acknowledged. On open,
after every complete line has parsed and the whole chain has verified,
such a segment is treated as a torn write: its bytes are copied to
`<log>.torn-<unix_nanos>` (fsync'd), the log is truncated to the end of its
last complete line (fsync'd, directory fsync'd), and a
`WARNING — TORN FINAL LINE` line is logged. If the side file cannot be
written, the log is not truncated and startup fails. This applies whether
or not the unterminated segment happens to parse as JSON: in both cases it
was never acknowledged, and someone able to rewrite the file could delete
the final line outright anyway (truncating the tail of a hash chain is not
detectable without an external anchor; that is unchanged). Everything else
still refuses to start and leaves the file untouched: a bad mid-file line,
a complete newline-terminated line that fails to parse, fails hash
verification, or breaks the ambiguity rules, or invalid UTF-8 in a
complete line. A torn tail never excuses corruption earlier in the file.

**Failed write (AEGIS F5, related).** If `write_all`, `flush` or `fsync`
fails part-way, the file is truncated back to its pre-append length and
`fsync`'d before the `500` is returned, and neither the ledger nor the
event idempotency index advances. If that truncation also fails, the
ledger is poisoned: every further append is refused until restart, because
writing after an unknown partial line would turn a recoverable torn tail
into mid-file corruption; restart then applies the torn-tail rule. Tested
with a real kernel failure (`ulimit -f` with `SIGXFSZ` ignored → `EFBIG`
after a partial write) in `tests/server_hardening.rs`, and with an
injected failing writer (partial write, failed fsync, failed rollback) in
`persistence::tests`.

**Logging never kills the server (found during this fix).** `eprintln!`
panics if stderr cannot be written (full disk under a redirected log,
closed pipe, `RLIMIT_FSIZE`), and the server is a single-threaded loop, so a
failed log line killed the ledger (exit 101, reproduced with stderr on
`/dev/full`). All logging now goes through `ledger_log!`, which drops a
line it cannot write.

**Known limitation: non-ASCII bytes in the request head.** *(Superseded
in fix wave 4 by section 7: the HTTP stack is now hyper, which answers
these requests; the test was changed accordingly. Kept for the record.)*
`tiny_http`
0.12.0 (the latest release; the same code is on its `master` branch)
reads each request-head line in `ClientConnection::read_next_line` and
returns `io::ErrorKind::InvalidInput` ("Header is not in ASCII") if any
byte is non-ASCII; its iterator maps that `ReadIoError` to `return None`,
which closes the connection without writing any response. This happens in
tiny_http's connection thread before a `Request` exists, so this service's
code never sees it and the library offers no hook to change it (verified
by reading `tiny_http-0.12.0/src/client.rs`). Consequences: a request with
any non-ASCII header byte (even on `/health`, even with a valid token) gets
an empty reply instead of `401`/`400`. It is fail-closed: nothing is read,
nothing is written, auth is not bypassed, and the process stays up
(`tests/server_hardening.rs::d4_non_ascii_header_closes_connection_process_stays_up_nothing_bypassed`
pins exactly that). Replacing the HTTP stack was evaluated and rejected
for this pass: the only mature alternatives (hyper-based) bring an async
runtime and a rewrite of the server loop, which is not a small or safe
change for the evidence ledger; forking tiny_http for a one-line change
adds an unmaintained fork. Revisit if a caller can legitimately send
non-ASCII header bytes (no current caller does: tokens and all headers
they send are ASCII).

## 6. Unknown fields and the empty chain (fix wave 3, Sep 24 2026)

- **Unknown fields are refused (AEGIS N8).** A persisted finding or event
  line with a field outside its known set (e.g. an injected
  `"approved_by":"andre"`) used to load: serde dropped the field, the field
  is not in the hash, so the chain still verified — the raw log could carry
  unhashed "evidence" that looked recorded. `FindingEntry`, `EventEntry`
  and `LedgerRecordInput` are now `#[serde(deny_unknown_fields)]`: such a
  log refuses to open (`Corrupt`, the server does not start) and such an
  append is a `400`. The known field sets are the ones the real binaries
  wrote; every real fixture (`legacy_ledger_v1.jsonl`,
  `legacy_ledger_v2_negatives.jsonl`, `ledger_v3_overbound_strings.jsonl`)
  still loads and verifies. AEGIS's injected line is kept as
  `tests/fixtures/aegis_unknown_field_injection.jsonl`.
- **An empty ledger is a valid chain.** `GET /ledger/verify` on an empty
  ledger now answers `200 {"valid":true,"entries":0}` (it was
  `409 {"valid":false,"error":"Empty"}`, which every caller had to
  special-case). `409` now always means the chain failed verification.

## 7. One slow client must not freeze the ledger (fix wave 4, Sep 24 2026)

**Finding (AEGIS, HIGH, no auth needed).** One socket sending a wrong
token, `Content-Length: 5000` and 12 body bytes blocked `/health` for as
long as it stayed open. Every department fails closed on the ledger, so
this halts the company.

**Exact blocking point (evidence).** The server handled requests one at a
time on its main thread (`for request in server.incoming_requests()`).
Auth ran before the body was read and the 401 *was* written, but
`tiny_http::Request::respond(self, ..)` then drops the request, and the
request's body reader is an `EqualReader` whose `Drop` reads and discards
the rest of the declared body with no timeout. `gdb -p <pid>` on the live
server during `slow.py` (wrong token) showed the main thread at:

```
#0  __libc_recv (fd=6, len=4988)
#11 tiny_http::util::equal_reader::{impl#2}::drop   (equal_reader.rs:72)
#17 core::ptr::drop_in_place<tiny_http::request::Request>
#18 tiny_http::request::Request::respond            (request.rs:441)
```

`/health` meanwhile sat parsed in tiny_http's queue (`http=000` after the
probe's 4 s hold; `200` in 3 ms right after the socket closed). Other
blocking points of the same class, same thread: `read_body` on a slow
right-token body (no deadline); the response write to a client that does
not read (no write timeout; a 30 000-entry `/ledger/entries` stalled
`/health`); an absurd `Content-Length` was read up to 64 KiB before the
413. Header reading happened in tiny_http's per-connection threads, which
are unbounded and also have no deadline (a silent connection was held
forever).

**Why tiny_http was replaced.** tiny_http 0.12 never exposes the socket
(`Listener`/`Connection` are closed enums), so no read/write timeout or
total deadline can be set; the drain-on-drop is inside the library; and it
spawns one thread per connection without bound. A worker pool on top of it
only moves the stall: each slow client then holds a worker forever. Socket
options inherited from the listener would give per-read timeouts at best,
which a byte-every-second trickle defeats. So the fix needs the socket.
Options: hand-written HTTP/1.1 on `std::net` (no new crates, but a new
parser for the most important service is its own risk), or **hyper 1.x on
tokio** (chosen): the most widely used Rust HTTP implementation, with
`header_read_timeout`, a bounded read buffer, `keep_alive(false)`, and
cancellation by dropping the connection future. New crates: `tokio`,
`hyper`, `hyper-util` (tokio adapter + timer), `http-body-util`
(`Limited` body). tiny_http is removed. Every existing integration test
passed unchanged except the one that pinned the old non-ASCII limitation.

**Design (`src/bin/server.rs`).**

| Limit | Value | Behavior |
|---|---|---|
| Request head (line + headers) | 5 s (`HEADER_READ_TIMEOUT`); head ≤ 16 KiB | connection closed (hyper); larger head → 431 |
| Body | 5 s total from the start of the read (`BODY_READ_TIMEOUT`) | `408`, closed; a trickle is cut too |
| Declared `Content-Length` > 64 KiB | checked before any body byte | `413`, closed, body never read |
| Body > 64 KiB without a length (chunked) | `Limited` | `413` |
| Whole connection: head, body, handling, response write | 15 s (`REQUEST_DEADLINE`) | socket dropped |
| Concurrent connections | `LEDGER_MAX_CONNECTIONS`, default 512 | extra connections get an immediate `503` + `Retry-After: 1`, closed |
| Listen backlog | 128 | kernel queue |
| Threads | 4 async workers; ≤ 16 blocking threads for ledger work | |

- One request per connection (`Connection: close`). A request answered
  without reading its body (401, 404, 413) is closed with the body unread —
  nothing is drained. Keep-alive is not needed on loopback and would make
  the per-request deadline harder to reason about.
- The total deadline is 15 s, not 10 s, so a request whose head and body
  each arrive just before their 5 s limits still has 5 s for the append and
  the response. If the deadline does expire during an append, the append
  still completes (ledger work is never cancelled halfway); only the
  response is lost, which is the same as any dropped connection: events
  are idempotent by `event_id`, and a caller must treat a finding with no
  response as unknown, as before.
- The ledger `Mutex` is taken only inside `spawn_blocking`, after the body
  has been fully read, parsed and validated. Every append still runs under
  that one lock, so the chain, seq numbering, and event idempotency/conflict
  rules are unchanged (tested: 800 parallel posts over 200 ids → exactly
  200 `201`s with seqs 0..199 and 600 `200`s; 50 conflicting posts on one
  id → one `201`, 49 `409`s; chain valid; restart reloads).
- A panic during ledger work, or a poisoned lock, exits the process (the
  old single-threaded server also died on any panic); restart re-verifies
  the log.
- Non-ASCII header values (obs-text) are now accepted by the HTTP layer;
  an `Authorization` value that is not visible ASCII is a `401`.
  `tests/server_hardening.rs::d4_*` was changed from "connection dropped"
  to "real answers, auth never bypassed".

**Residual limit, stated plainly.** A client able to open 512 connections
at once can make other callers get `503` (fast, not a hang) until its
connections hit their deadlines (5–15 s), and can repeat. There is no
per-client limit: the ledger binds loopback and every caller is local.
Raise `LEDGER_MAX_CONNECTIONS` (with the file-descriptor limit) if needed.

**Tests** (`tests/server_slow_clients.rs`, real binary, real sockets):
slow body with wrong token, slow body with right token, byte trickle,
slow head, idle connection, oversized `Content-Length`, slow reader of a
30 000-entry ledger, 150 concurrent slow clients, over-cap 503 and
recovery; each asserts `/health` and an authenticated append finish in
under 1 s while the bad clients are stalled, and that the server cuts the
bad connection within its deadline. Plus the two concurrency tests above.
Before the fix 9 of the 11 failed (the two concurrency tests passed; they
guard against the new concurrency).

## 8. Graceful close and ephemeral test ports (fix wave 21, Sep 28 2026)

AEGIS round 20, N20-M-1 (E3; low-severity product defect and test defect):
every path that answers without reading the whole request — the 503 load
shed (`shed()`, which never reads the request), a `401`/`404` answered
before the body, the `413` on a declared `Content-Length` over the cap, a
`408`, a refusal hyper produces itself — closed the socket with request
bytes still unread. The kernel then sends RST after the response: Linux
reports it as `EPIPE` in the client's `SO_ERROR` after a clean EOF (the
reviewer's `rst_witness.py`: 10/10 on the shed and 413 paths); macOS XNU
checks `so_error` before `SS_CANTRCVMORE` and returns `ECONNRESET` from the
client's read — the Mac "connection reset by peer" failures, because the
tests read responses to EOF.

Decision (RFC 9112 section 9.6, graceful close): after the response the
server shuts its write side down (FIN), then reads and discards what the
peer still sends — at most `DRAIN_MAX_BYTES` (64 KiB) within
`DRAIN_TIMEOUT` (1 s) — and only then closes. The answer still never
waits for the body (a slow-body client still gets its `401` at once); only
the close is deferred, and it is bounded. hyper serves each connection
without shutting the socket down itself (`poll_without_shutdown`), and the
socket is taken back (`into_parts`) however the connection ended — answer
written, deadline, or a protocol error hyper answered itself — so the
graceful close covers every path with one descriptor per connection (a
`dup(2)` kept outside hyper would have doubled the descriptors and made the
512-connection cap unreachable under the common 1024 limit). An answered
connection gives its connection slot back BEFORE it drains, so the cap
still counts connections being served and a peer slow to close never holds
a serving slot; draining sockets — served and load-shed alike — have their
own bound, `DRAINS_MAX` (512), past which a socket is closed at once (the
old behaviour, RST included). Descriptors are therefore bounded by the cap
plus 512 draining plus the load-shed writes in flight. A peer that keeps
sending past 64 KiB or 1 s still gets the kernel's RST — by design.

Swept to the Python services (fix wave 21, lead ruling L1): uvicorn closes an answered connection with
`transport.close()` at once — its own `limit_concurrency` 503, a 400 it writes itself, an app answer with
`Connection: close`, every deadline — so bytes of a body still arriving after the answer made the kernel send RST
(the client's `SO_ERROR` was `EPIPE`, error 32, on all three paths in every service). Each service's protocol class
(`serve.py` of clipper-network, compliance, creative, delivery, detection, finance, legal, onboarding, verification;
fulfillment's `http_limits.py`) now mixes in `GracefulCloseMixin`: FIN once the answer is flushed (`write_eof`),
then at most 64 KiB / 1 s of the client's bytes read and discarded, then close. The mixin wraps the transport
uvicorn sees, so every close uvicorn or the service makes goes through it. `tests/test_fix21_graceful_close.py`
(delivery: `test_live_graceful_close.py`) in each service drives that service's protocol class under uvicorn.
The bound is the ledger's and it is real: a client that writes a 3.9 MB body with a blocking `sendall` and reads
only afterwards, answered early by uvicorn's `limit_concurrency` 503, still has MBs unsent when the 64 KiB drain
ends and is reset without reading the answer (measured with fulfillment's protocol: 20/20 reset at 64 KiB, 20/20
clean with the bound raised to 8 MiB, 20/20 clean at 64 KiB for a client that reads while it sends). Under uvicorn
the only server-side remedy is reading the whole declared body (up to 4 MiB per connection, times the connection
cap), which is the unbounded drain the ruling excludes; so fulfillment's 128-sender test client
(`test_fix8_n7_2_body_prealloc.py::_send_reading`) now reads while it sends, stops at the answer and reads it to
its `Content-Length` — a reset after a complete answer is the server's documented behaviour.

Tests read every response to its `Content-Length` (`tests/common/mod.rs`),
never to EOF. `server_slow_clients.rs::every_early_answer_closes_gracefully_so_error_is_clean`
asserts, for the 413 (65 KiB body sent whole), the 401 and 404 (body sent),
a 400 hyper answers itself (unparseable `Content-Length`, 30 KiB after the
head) and the load-shed 503, that after the whole response and EOF the
client's `SO_ERROR` is clean. With the old close it failed with `EPIPE` on
the 413, 400 and shed paths (the small 401/404 bodies sat in hyper's read
buffer, so those two were already clean).

N20-M-2 (test race): `over_connection_cap_gets_prompt_503_and_recovers`
opened its four slow-body holders right after the readiness probe, whose
connection slot is released asynchronously; under CPU contention a holder
was itself shed and `/health` got the free slot (200). The test now
confirms every holder is held (nothing came back on it, non-blocking peek)
and replaces a shed one before asserting the 503. The server was correct.

N20-M-3: the tests' `free_port()` picked a port, released it and passed it
to the server — a window in which another process can take it. The server
now accepts `LEDGER_PORT=0` (the kernel picks) and `LEDGER_PORT_FILE`:
after the bind it writes the bound port there atomically (temp file +
rename); a write failure refuses to start. Every integration test starts
the server that way and reads the port back; no test picks a port. The
second Mac `server_events` failure in the relayed E4 summary stays
unidentified (no log received); this removes the two candidate causes the
reviewer named that are in the test harness (the port race; a 5 s startup
wait, now 10 s behind the port file).

## 9. LEDGER_PORT_FILE hardened; the Python drains bounded like the ledger's (fix wave 22, Sep 28 2026)

**LEDGER_PORT_FILE (AEGIS round 21 N21-C-2, lead ruling G7).** Wave 21 wrote the port to `<path>.tmp-<pid>` with
`std::fs::write` — a predictable name, followed if it was a symlink (a link planted there made the server overwrite
the link's target with the port number: `tests/server_port_file.rs`, run on the wave-21 server through
`sh -c 'ln -s … "$LEDGER_PORT_FILE.tmp-$$" && exec server'`, left the victim holding `36667\n`) — with the default
mode (0644), BEFORE the ledger log was opened (a server refusing a corrupt log had already announced a port), and
never removed it. Now (`src/bin/server.rs`, `write_port_file`/`publish_port_file`): a target that is a symlink
(or not a regular file) is refused — the server does not start; the port is written only after the log opened and
verified; to `.<name>.tmp-<16 hex from /dev/urandom>` in the target's directory, created
`O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC` with mode 0600, written and fsynced, then `rename`d over the target (the
symlink check is repeated just before; `rename` never follows a link at the destination); on SIGTERM/SIGINT an
async-signal-safe handler `lstat`s the path and unlinks it only if it is still the regular file the server
renamed there (same device and inode), then re-raises the signal with the default disposition (the process ends
as before, killed by the signal); a `main` that returns removes it the same way. `libc` became a direct
dependency for `O_NOFOLLOW`, `lstat`/`unlink`/`signal`/`raise` — it was already in the build through tokio/mio at
the same version (Cargo.lock gains one dependency edge, no new package); tokio's `signal` feature would have
added `signal-hook-registry`, which this offline build does not carry. Tests (`tests/server_port_file.rs`, 5):
the planted temp-name link is never followed; a symlinked target is refused; nothing is written when the log
fails verification; a stale file is replaced by a 0600 file and SIGTERM removes it; a file that is no longer the
server's own is left alone. Wave-21 server: 4 of the 5 fail (the fifth, "leave someone else's file", passes
trivially — it never removed anything).

**The Python services' graceful close (AEGIS round 21 N21-C-1, N21-C-3; lead rulings G5, G6).** The ten Python
services now share ONE module, byte-identical (`src/graceful_close.py`; delivery `src/zbm_delivery/graceful_close.py`;
`tests/test_live_graceful_close_module.py`, itself byte-identical in every service, pins its sha256 and compares
every copy the checkout holds). Mirroring §8: an answered connection gives its uvicorn concurrency slot back
(`server_state.connections`) BEFORE it drains; at most `drains_max` connections of one server drain at once
(`<PREFIX>_DRAINS_MAX`, default 512: `CN_`, `COMPLIANCE_`, `CREATIVE_`, `DETECTION_`, `DLV_`, `FIN_`,
`FULFILLMENT_`, `LEGAL_`, `ONBOARDING_`, `VI_`; anything but a positive integer refuses startup), past which the
socket is closed at once. Measured on the wave-21 code with the service's own protocol under uvicorn
(`w22/g6_live.py`): a request arriving while one answered connection drains got uvicorn's 503 (limit 2: the
draining one still counted), and three answered connections all drained with a cap of 2 configured nowhere; now
the request is served and the third is closed at once. The body uvicorn buffered for the request a connection
answered is released when its drain starts. Reads go through a `BufferedProtocol` in front of uvicorn's protocol
into ONE 16 KiB buffer per event-loop thread: a read hands the parser at most 16 KiB (the loops read up to
256 KiB, and uvicorn buffers a request's body up to its 64 KiB high-water mark PLUS the read that crossed it);
drained bytes are counted in that buffer and discarded (no bytes object at all). The module holds no float
literal (`DRAIN_TIMEOUT_S = 1`, seconds): it sits in every service's `src/`, and finance-py's G1 guardrail
(`test_g1_no_float_in_money_paths`) refuses a float literal in any `src/` file outside its transport allowlist —
the first 3.13 suite run of this wave caught `1.0` there (1 failed, finance-py); the guardrail was kept as it
was and the module changed. The attribution behind this,
and fulfillment's 96 MiB bound, are in ADR 0002 ("Fix wave 22").

**Evidence, fix wave 22 (all runs 2026-09-29 00:00–00:05Z, against 1391b5c; the round-21 reviewers' close probes,
paths re-pointed, ports 18840–18847).** `g6_live.py`, all ten services: the wave-21 code answers the second request
503 and drains all three sockets; this code answers it and closes the third at once (detection and creative
re-run with their service token, 00:03Z). `rst_probe`: shed-503, 413 and 401 each 50/50 clean EOF; `rst_witness`
40/40; `cap_race` 100/100 under three busy loops; `l1_probe` unchanged (a blocking 3.9 MB `sendall` 20/20
BrokenPipe — the pinned residual — and a reading client 20/20 503); `ledger_drain` (the Rust server, 1500 unclosed
answered connections): fds peak 641 with its DRAINS_MAX 512, back to 8 after 2.5 s. `drain_dos` (Python,
a 12k-connection flood of early-answered requests): fds peak 2284 → 534 (the cap holds), and legitimate `/health`
went from 8×200 + 19×503 to 7×200 with no 503 — BUT its latency rose (p50 0.002 s → 1.4 s, max 1.9 s): the event
loop is saturated by the flood either way, and the wave-21 code turned that into fast 503s where this code queues
the request. That trade is not hidden: neither is a DoS defence; the ingress in front is. `drain_dos_slowhead`
(connections that never finish their head) is UNCHANGED (fds peak 12022 in both): those are never answered, so
the drain cap does not apply — a head timeout is their bound, outside this change.

## 10. LEDGER_PORT_FILE's parent directory, and every stop signal (fix wave 23, Sep 30 2026)

**AEGIS round 22 N22-C-3.** §9 refused a symlink AT the port file path but followed one in its PARENT: with
`LEDGER_PORT_FILE=<dir>/linkdir/p3.port` and `linkdir -> victimdir` holding a regular `p3.port`, the server replaced
`victimdir/p3.port` (0600, its port) and SIGTERM then deleted it; SIGHUP and SIGQUIT left the file behind (the
reviewers' `g7_dirlink.py` / `g7_portfile.py` item 3/3b/9). Now (`src/bin/server.rs`, `open_parent_nofollow`,
`port_file_location`, `write_port_file`): the parent path is walked one component at a time from `/` (absolute) or
the working directory (relative), each component opened `O_DIRECTORY|O_NOFOLLOW` (`O_PATH` on Linux, so a
search-only directory is walkable; `O_RDONLY` elsewhere) and `fstat`-checked to be a directory; a symlink anywhere
in the parent path refuses the start, naming the component (checked before the log opens, and again when the file
is published). The target check (`fstatat … AT_SYMLINK_NOFOLLOW`), the temp file (`openat
O_CREAT|O_EXCL|O_NOFOLLOW`, 0600) and the rename (`renameat`) are all relative to that directory descriptor, which
stays open for the life of the process: the removal (`fstatat` same device and inode, then `unlinkat` — both
async-signal-safe) acts on the directory the file was written in even if a path component is swapped afterwards.
The removal runs on SIGTERM, SIGINT, SIGHUP and SIGQUIT (the handler then restores the default disposition and
re-raises: the process still dies of the signal) and when `main` returns. A caller whose temp directory sits behind
a symlink (macOS `/var` -> `/private/var`) must pass the canonical path: the integration tests' port files now come
from `common::real_temp_dir()` (the canonicalized temp dir). Tests (`tests/server_port_file.rs`, +4): a symlinked
parent directory is refused and the file behind it untouched; a symlink deeper in the parent path is refused; a
relative path in a real directory still works; SIGHUP and SIGQUIT remove the file. On the 540a64e server the first,
second and fourth fail; the third passes (a regression guard). `cargo test --locked` (recorded in a5fb681): 60
unit + 53 integration tests, 0 failed; `cargo clippy --locked --all-targets -- -D warnings` clean (Linux only: the non-Linux branch —
`O_RDONLY` instead of `O_PATH` — is not compiled on this box; a search-only ancestor directory would be refused there).
The reviewers' `g7_portfile.py` against the release binary: 17/18, the 18th being its item 3b ("SIGTERM removes it
through the dir link"), which now CANNOT happen — the file behind the link is never touched; `g7_dirlink.py` 3/3
refused with the victim intact. Evidence: `services/delivery-py/docs/evidence/
dept28/round22/` (ledger logs and the reviewers' probes re-run against the release binary).

## 11. The publish window: stop signals blocked, removal armed before anything is written (fix wave 24, Oct 1 2026)

**AEGIS round 23 N23-S-2.** §9/§10 installed the stop-signal handlers only AFTER the rename. The reviewers'
`g7_publish_window.py` fired SIGTERM the moment the temp file appeared: 298/300 signals landed in the window and 297
left `.<name>.tmp-<hex>` behind (the default disposition killed the process mid-publish); fired the moment the port
file appeared, a signal between the rename and the handlers would leave the port file. Now (`src/bin/server.rs`,
`publish_port_file` / `write_and_rename`): `main` blocks SIGTERM/SIGINT/SIGHUP/SIGQUIT before the tokio runtime starts
its threads (they inherit the mask, so only the main thread ever takes a stop signal) and unblocks them in the main
thread at once; the publish blocks them again in the main thread — so for the whole process — from before the temp
file is created until the port file is in place and armed; the removal is armed BEFORE anything is written (the
directory descriptor, the published name and the temp name recorded, the handlers installed; the temp file's
device/inode recorded before the rename, which keeps the inode); the handler removes the temp name while the publish
is in progress and the published name while it is still this server's file (same device and inode), then restores
the default disposition and re-raises. A signal sent during the publish stays pending and is handled the moment the
mask is restored, with the port file in place and armed. Test (`tests/server_port_file.rs`,
`a_stop_signal_aimed_at_the_publish_window_leaves_neither_the_temp_file_nor_the_port_file`, 100 + 100 aimed
SIGTERMs): before, temp file left 94/100 and port file left 2/100; after, 0/100 and 0/100.

**Residual, stated (fix wave 25, AEGIS round 24 N24-S-8).** SIGKILL — and the kernel's OOM killer, which sends it —
cannot be blocked, caught or handled: a server killed that way leaves what it had written. AEGIS round 24 measured it
with aimed SIGKILLs: fired the moment the temp file appeared, 49/50 left `.<name>.tmp-<hex>`; fired the moment the
port file appeared, 50/50 left the port file (by design: nothing runs after SIGKILL). The same holds for any death
that skips the handlers (an abort, a power loss). So the port file is a HINT, not a promise: a reader connects to the
port it names and checks it (`GET /health`) before trusting it — the port may be dead or already reused by another
process — and treats a `.*.tmp-*` file beside it as debris to remove, never to read. `src/bin/server.rs`'s module
documentation says the same.

## 12. The ledger's own tests measure the server, not the box (fix wave 25, Oct 1 2026)

Scout C (wave 25) found the slow-client tests (`tests/server_slow_clients.rs`) asserting latency bounds on a loaded
machine (`/health` and an fsync'ing append within 1 s while 150 clients stall; a 401 within 3 s; cuts within the
deadline + 2 s) and using fixed sleeps (300 ms, 1.5 s) as the "the bad client is now stalling the server" barrier —
nothing confirmed the server had even accepted the bad connection before "others are served" was measured, so on a
starved box the property could be measured before the stall existed (C2-4, C2-5). Under the wave-25 hygiene rule
(R-HYGIENE, `devtools/hygiene_check.py`, rule L1) a wall-clock upper bound against a literal is not allowed. Now:

- **Barrier, not sleep.** The server runs with a small `LEDGER_MAX_CONNECTIONS`; holders fill the slots the bad
  connections do not hold; `/health` must then be SHED (503) — possible only if every bad connection holds a slot —
  and after the holders are dropped `/health` answers 200 again. A slow reader is confirmed by at least 64 KiB of
  its response already queued in its socket; a wrong-token client by its 401 having arrived.
- **Ordering, not stopwatch.** Others are served (a shed caused by the test's own small cap is retried; hang guard =
  the request deadline), and only THEN is every stalled connection checked to be still held (nothing came back on
  it) — a server that served the others behind the stalled client could only do so after cutting it. For the slow
  reader (whose socket always holds queued response bytes, so "nothing came back" cannot be observed) the ordering is
  the request deadline itself: the only thing that cuts it is that 15 s deadline, which starts after the request was
  sent, so others answered before `since + REQUEST_DEADLINE` were answered before the cut. Proof: the mutant
  `serve_connection` awaited inline in the accept loop (one connection at a time — the wave-4 defect class) fails the
  connection-dependent tests; the run that shows it is in the wave-25 E-C report (logs `proof-ledger_mut`), not
  restated here as a count.
- **Deadlines:** the cut is an event (the read ends because the server closed); a lower bound (not before the
  deadline) stays — load cannot break it. Two upper bounds remain, each against the server's own deadline constant
  (never a literal) and each separating two causes: the wrong-token client's EOF must come before the 5 s body
  deadline (closed at once vs closed by the deadline; read right after the 401, so nothing else is inside the bound),
  and the slow reader's others before the 15 s request deadline (above). Both are allowlisted with these reasons in
  `devtools/hygiene_allowlist.json` (rule L1 flags a bound held in a constant too). The trickle test's "the
  deadline is total, not per read" is an ordering: the server's cut reaches the trickling writer before its 20 s
  of bytes are sent.
- Review of the first wave-25 version of these tests (E-C, same wave): the wrong-token EOF was read only after the
  others were served (their service time counted against the 5 s bound), and the slow-reader check drained the
  socket until it would block — which can consume the whole multi-MB response the test then asserts was truncated.
  Both changed as described above.
- `tests/server_port_file.rs`, aimed-signal test (C2-6): the sample is now 100 signals that landed IN the publish
  window, however many spawns that takes (at most 400); before, fewer than 90 hits in 100 spawns failed a correct
  server whenever the poller missed the window.
- `start_with` (C2-11) owns the child in a `ServerHandle` (kill + wait on drop) before anything that can panic.

Residual, stated: the barrier relies on the connection cap's shed path (a 503 before the request is read), which
these tests therefore also exercise; the integration tests still write fixed-prefix files directly in the temp
directory (`std::env::temp_dir()`, which honours TMPDIR — under the hygiene wrapper that is the run's private TMPDIR,
so a crashed test's leftovers fail rule R3 instead of accumulating in /tmp; the 735 `ledger_test_*` logs in this
machine's /tmp are from earlier waves' code and are not removed by this wave — other sessions' files).

## 13. Bug sweep F: one writer, a durable head, strict tails, scoped callers, bounded reads (Oct 6 2026)

Amendment for the Oct 6 2026 backend bug sweep (findings F-1, F-2, F-4, F-6, F-7, F-12, F-13 against integration
5d49ee9; `claude/bug-sweep-2026-10-06.md` in the project). Every reproduced finding has a test that fails on 5d49ee9
and passes now: `tests/server_sweep_f.rs` (real binary, real TCP) and the `sweep F` unit tests in
`src/persistence.rs`. The hash chain, the canonical forms and the on-disk entry format are unchanged; every log any
earlier binary wrote still loads, verifies and is extended.

**F-1, single writer.** Two servers on one `LEDGER_LOG_PATH` both started and both appended seq 0, forking the
chain on disk; the next start refused. `PersistentLedger::open` now takes an exclusive, non-blocking `flock` on
`<log>.lock` (created mode 0600, never through a symlink) before it reads anything, and holds it for the life of the
process. A second opener — another process or a second handle in the same one — gets `PersistError::Locked` and the
server refuses to start (exit 1, "Another ledger-rust is serving this log"). `LEDGER_ALLOW_RESET` never bypasses it.
The lock file is never deleted (deleting it would let a third opener lock a new inode while the holder still holds
the old one). flock is advisory and local: it does not protect a log on a network filesystem shared by two hosts —
"exactly one ledger replica" stays a deployment rule.

**F-6, deletion and rollback.** A deleted log restarted as an empty, "valid" ledger; dropping whole acknowledged
lines from the end verified. Now, after every append, the head checkpoint
`{"entries":N,"head_seq":N-1,"head_hash":"…"}` (genesis hash and `null` when empty) is written to `<log>.head`:
temp file `<log>.head.tmp` (O_EXCL, mode 0600), fsync, rename, directory fsync. An append is acknowledged only after
both the log line and the checkpoint are durable; if the checkpoint cannot be written before its rename, the log line
is rolled back exactly like a failed log write (and the ledger is poisoned if even that fails). On open, after the
full chain verifies, the log must reach the checkpoint:

| log vs checkpoint | result |
|---|---|
| log file missing, checkpoint present | refused |
| fewer entries than the checkpoint | refused |
| entry `head_seq` does not carry `head_hash` (rewritten / replaced log) | refused |
| checkpoint file unreadable or malformed | refused |
| equal | opens |
| log ahead of the checkpoint (a crash between the log fsync and the rename; every extra entry still verifies) | opens, checkpoint moved up, logged — *since the Oct 7 AEGIS review (M1, below): exactly one entry ahead only* |
| no checkpoint file (a log written by an older binary) | opens, checkpoint created, WARNING logged — *since M2 (below): refused unless the bound `LEDGER_MIGRATE_LEGACY`* |

A refusal changes nothing on disk. `LEDGER_ALLOW_RESET=1` (exactly `1`; any value other than `0`/empty/unset refuses
to start) accepts the verified log as it is and rewrites the checkpoint, with a loud `OPERATOR RESET` line naming the
old checkpoint; it skips nothing else (lock, parsing, chain verification, the F-13 rules). `GET /ledger/head` returns
the in-memory head, which equals the durable checkpoint after every acknowledged append.

*Residual, stated:* the checkpoint sits next to the log, so whoever can rewrite the log can rewrite or delete the
checkpoint too (deleting both reads as a fresh install; deleting the checkpoint and truncating the log reads as an
upgrade from an older binary). It catches accidental deletion, truncation, a restore of an older copy and a swapped
log — not a deliberate forger with write access to the directory. **Future work:** a stronger, external anchor — the
head signed with a key the ledger host does not hold for writing, and published somewhere the host cannot rewrite
(another service's store, a transparency log, a periodic signed export) — so that rollback is detectable off-box.

**F-13, torn tail and blank lines.** The torn-tail recovery (section 5) moved ANY unterminated final segment aside,
so deleting the one final newline of an acknowledged log silently dropped the last acknowledged entry. A crash
mid-append leaves a strict prefix of `<json>\n`, and no strict prefix of a JSON object is valid JSON; so an
unterminated tail that parses as a complete JSON value is now refused (`Corrupt`, file untouched — restore the
newline or move the line aside by hand), as is an unterminated whitespace-only tail (no real line starts with
whitespace). A blank line (empty or whitespace-only) anywhere in the log is refused; the old loader skipped it. A
genuinely partial final line is still preserved to `<log>.torn-<nanos>` and truncated, exactly as before.

**F-7, finding idempotency (opt-in).** `POST /ledger/append` appended every post, so retries made duplicates (the
probe: 60 posts of 5 `finding_id`s, 60 entries). With an `Idempotency-Key` header whose value equals the body's
`finding_id`, the append has `event_id` semantics: `201` new, `200` and the existing entry when an entry with that
`finding_id` and identical content exists (amounts compared as canonical money strings, so a legacy numeric amount
matches), `409` when only different content exists; nothing is written for `200`/`409`. A key that differs from the
body's `finding_id` (or is not visible ASCII) is a `400`. Without the header nothing changes — the orchestrator
re-records findings on every scan today and keeps doing so. The `finding_id → positions` index is rebuilt on open; a
log may legitimately hold one `finding_id` several times (every earlier writer appended unconditionally), so unlike
`event_id` a duplicate is never corruption, and the opt-in compares against every entry with that id.

**F-12, token length.** `LEDGER_SERVICE_TOKEN` must be at least 32 bytes or the server refuses to start (a
1-character token was accepted). Every live run in the repo already used ≥ 32 bytes; two onboarding-py tests that
start the real binary used 23-byte tokens and were padded.

**F-2, scale.** At ~120k entries the dashboard broke and at ~480k every caller's integrity check failed: a full
`GET /ledger/entries` or `/ledger/verify` serialized or re-hashed the whole ledger while holding the one mutex, on a
blocking pool readers could fill, so an append queued behind every reader in front of it (500k entries: an append
behind 6 full reads took 5.2 s; `/ledger/verify` 1.6–2.3 s).

- `GET /ledger/entries?after_seq=&limit=&department=&event_type=`: entries with `seq > after_seq`, in seq order, at
  most `limit` (default 1000, max 10000) that match the filters; `department`/`event_type` match events only (a
  finding has neither). Response: the same JSON array of entries. Fewer than `limit` entries means the end was
  reached; otherwise the next page is `after_seq=<last seq>`. The query is strict: an unknown or repeated parameter,
  an empty value, a non-numeric number, `limit` outside 1–10000 or a filter outside `[a-z0-9_]{1,64}` is a `400` — a
  typo never silently widens a read. With no query string the response is byte-identical to before (checked at
  500k entries: same SHA-256 of the body from both binaries).
- Readers copy their snapshot under the mutex in chunks of 2048 entries and serialize or verify outside it; entries
  are immutable and append-only, so the chunks are exactly the snapshot `0..len` taken at the start.
- Writer priority: an append announces itself before it locks; a reader waits, before each chunk, while an append is
  waiting. An append therefore waits for at most the one chunk being copied, however many readers there are.
- Reads take one of `min(CPUs, 4)` read slots (async — a waiting reader holds no thread) before they use the blocking
  pool (16 threads), so appends always find a thread; appends never wait for a read slot. More concurrent full reads
  than CPUs only slowed every read (6 reads of 500k on 2 CPUs: up to 10.1 s with 4 slots, 5.6 s with 2).
- `GET /ledger/verify` is incremental: the full chain is verified on open, and each verify checks only the entries
  after the last verified head (`len`, hash), using the same per-entry check (`verify_entry`, now shared with
  `Ledger::verify_chain`). `?full=1` re-verifies the whole chain on demand (chunked as above). The responses are
  unchanged (`200 {"valid":true,"entries":N}` / `409 {"valid":false,"error":…}`); `?full=` other than `0`/`1` is a
  `400`. Verification is of the in-memory chain, as before; the log on disk is re-verified at every start.
- `REQUEST_DEADLINE` (15 s per connection) is unchanged and still covers the wait for a read slot.

Measured (release builds, 2-CPU build box shared with other jobs — absolute numbers are noisy, the before/after
direction was the same in every run; 500k-entry / 258 MB log; `probe_scale.py` and this wave's bench scripts, two
runs each, quiet / loaded):

| | 5d49ee9 | after |
|---|---|---|
| one append behind 6 concurrent full reads (`probe_scale.py`) | 5.25 s / 3.24 s | 0.73 s / 0.89 s |
| append latency, 6 readers looping full reads, median (p95) | 5.46 s (6.36 s) / 7.33 s (8.42 s) | 8.8 ms (52 ms) / 11.9 ms (44 ms) |
| `GET /ledger/verify` | 1.6 s / 2.5–5.0 s | 0.001–0.06 s (`?full=1`: 2.1 s / 3.1 s) |
| `GET /ledger/entries?after_seq=…&limit=1000` | — (404) | 3 ms / 14 ms |
| idle append, median | 4.0 ms / 1.1 ms | 12.0 ms / 16.2 ms |

Costs, stated: an idle append now does three fsyncs instead of one (log, checkpoint, directory). A single full read
is ~0.4 s slower at 500k (the copy), and several concurrent full reads take longer in aggregate than before (they ran
one at a time under the lock; now up to one per CPU run side by side): at 500k on this box some of 6 concurrent full
reads reach the 15 s `REQUEST_DEADLINE` with either binary, a few more with this one. The full read is kept only for
backward compatibility; a client that needs the whole ledger repeatedly should page with `after_seq`.

**F-4, per-caller tokens with department scopes (opt-in).** One shared token, and `department` self-declared: any
service could record evidence for any other. New optional `LEDGER_CALLERS_FILE`: a JSON object mapping the lowercase
hex SHA-256 of a caller's bearer token to `{"caller": "<name>", "departments": ["<dept>", …], "scope":
"write"|"read"}` (unknown fields refused; a write caller must list at least one department; departments follow the
event `department` rule; any malformed entry, an empty map, a missing or > 1 MiB file refuses the start). When it is
set:

- a token is looked up by its SHA-256 (the file holds no tokens; lookup timing can reveal nothing about a token);
- `POST /ledger/events` needs a write caller whose `departments` include the event's `department`, else `403`
  (checked after the body is parsed, before the lock; nothing is written);
- `POST /ledger/append` (findings) needs the department `revenue_recovery`;
- a `read` caller may `GET /ledger/entries`, `/ledger/verify` and `/ledger/head` only; a POST is `403` before any
  body byte is read;
- the shared `LEDGER_SERVICE_TOKEN` is accepted only with `LEDGER_ALLOW_SHARED_TOKEN=1` (then with its old, unscoped
  access, and still ≥ 32 bytes); without the flag it may be unset, and if set it is ignored (logged).

When `LEDGER_CALLERS_FILE` is unset nothing changes, but the server logs a startup WARNING that the shared token can
write any department. The startup log names every configured caller with its scope and departments. *Residual:* the
callers file is read once at start (rotation = restart); scoping is per department, not per event type; a caller
token's length cannot be checked (only its hash is configured).

**A failed bind** (port taken, bad address) panicked with exit 101; now it is a `REFUSING TO START … cannot bind`
line and exit 1 (as are a failed runtime start, a bad `LEDGER_ALLOW_RESET` / `LEDGER_ALLOW_SHARED_TOKEN` value and
every startup refusal above).

**AEGIS review of d6b1cd9 (Oct 7 2026, approved with conditions).** Every finding has a test that fails against the
d6b1cd9 binary and passes now: `tests/server_sweep_f.rs` `aegis_*` (real binary, real TCP) and the unit tests named
below in `src/persistence.rs`. The hash chain, the canonical forms and the on-disk entry and checkpoint formats are
unchanged; the HTTP API is unchanged for the shared token and for `read_all` callers. Every live run in CI
(`live-runs`, ten departments) passes against this binary, and finance-py and sales-py also pass with a scoped
per-caller token (`LEDGER_CALLERS_FILE` naming only their own department) in place of the shared one.

| Id | Finding | Fix |
|---|---|---|
| M1 | A log any number of entries ahead of the checkpoint started (the checkpoint moved up), though an append leaves at most one unacknowledged entry. | `check_against_head` (`persistence.rs`): exactly one ahead opens (logged `EVENT checkpoint_moved_up`); two or more ahead is refused like a shorter or rewritten log. Tests: `only_a_log_exactly_one_entry_ahead_of_its_checkpoint_opens`, `aegis_m1_only_one_entry_ahead_of_the_checkpoint_starts`. |
| M2 | A non-empty log with no checkpoint opened and got a new one, so deleting the head file and truncating the log read as an upgrade; a new ledger was created silently. | No checkpoint + non-empty log refuses to start unless `LEDGER_MIGRATE_LEGACY=<entries>:<16 hex of the head hash>` — the value the refusal prints, bound to that log, so it can never match again once an entry is appended (and is reported as not needed when left set). No log + no checkpoint is a new ledger: one structured line `ledger-rust: EVENT {"event":"ledger_created",…}`. Every checkpoint decision is such a line (`ledger_created`, `checkpoint_moved_up`, `legacy_log_migrated`, `operator_reset`). Tests: `a_log_without_a_checkpoint_needs_the_bound_one_shot_migrate`, `aegis_m2_…`. Tests that serve the legacy fixtures pass the migrate value, as an operator would once. |
| M3 | `LEDGER_ALLOW_RESET=1` left in the environment stayed armed for every later start. | The value is bound: `<checkpoint entries>:<16 hex>/<log entries>:<16 hex>` (or `unreadable:<16 hex of the head file's SHA-256>/…`), printed by the refusal; it accepts only that exact (checkpoint, log head) pair. `1` now refuses to start with an explanation; a value matching no refusal does nothing and is logged. `LedgerOpenOptions { reset, migrate }` replaces `allow_reset`. Tests: `a_reset_value_accepts_only_the_state_it_was_printed_for`, `aegis_m3_a_stale_reset_value_does_not_accept_a_later_rollback`, and the sweep F reset tests now use the printed value. |
| M4 | Reads were not scoped: any authenticated caller read every department's evidence. | Enforced, consistent with the write scopes. A write caller reads its `departments` plus optional `read_departments`; a read caller reads its `departments`, or everything with `"read_all": true` (dashboard, compliance, audit); a read caller with neither, `read_all` with `read_departments`, or `read_departments` on a read caller refuses to start. `GET /ledger/entries` (with or without a query) returns only in-scope events, and findings only to `revenue_recovery` readers; `?department=` outside the scope is a 403, not an empty page. The shared token and `read_all` callers get the unchanged, byte-identical full read. `/ledger/verify` and `/ledger/head` (counts and a hash, no department data) stay open to every authenticated caller — every department's integrity check needs them. Callers checked: every Python service's `entries()` feeds an evidence audit that filters on its own `department` (bizdev, clipper-network, compliance, delivery, finance, influencer, legal, sales, security, service, verification); creative-py looks up its own event ids; the orchestrator reads findings only. No caller fixture changes: no service or live run configures `LEDGER_CALLERS_FILE` today. Tests: `aegis_m4_reads_are_scoped_by_department`, `aegis_m4_a_read_scope_must_be_explicit`. *Residual:* scope is per department, not per event type; the orchestrator's `ledger_entries_total` would count only findings under a `revenue_recovery`-only token (give it `read_all` if that total must stay the whole ledger). |
| L1 | The same token hash twice in `LEDGER_CALLERS_FILE` was accepted (the last entry silently won). | `load_callers` reads the map in file order with duplicates kept (`CallersFile`) and refuses the start naming both callers. Test: `aegis_l1_a_duplicate_token_hash_refuses_to_start`. |
| L2 | `LEDGER_CALLERS_FILE=""` was treated as unset (the shared token, unscoped). | Set-but-empty (or whitespace, or not UTF-8) refuses to start. Test: `aegis_l2_an_empty_callers_file_variable_refuses_to_start`. |
| L3 | Token entropy undocumented; caller tokens' length unchecked. | Premise confirmed in part: sweep F already refused a shared token under 32 bytes (`MIN_TOKEN_BYTES`, `load_shared_token`). Now documented (README "Tokens": 32+ bytes from a CSPRNG, e.g. `openssl rand -hex 32`; only length can be checked), and a caller token is checked when presented (`Auth::authenticate`): a configured token under 32 bytes is a 401, logged. Test: `aegis_l3_a_short_caller_token_is_refused_when_presented`. |
| L4 | `GET /ledger/verify?full=1` re-verified memory only, so a log rewritten on disk under a running ledger verified "valid" until the next restart. | `?full=1` also runs `verify_log_file`: the acknowledged bytes of the log (`log_bytes`, tracked per append) are re-read line by line with the open-time rules and re-hashed from the genesis hash, and must end at the in-memory head of the same snapshot; any difference is a 409 and a `CRITICAL` log line. Outside the ledger mutex (one short lock hold takes the snapshot), memory bounded by the longest line (1 MiB cap). The incremental verify is unchanged (memory only). Tests: `the_disk_reverify_catches_any_change_to_the_acknowledged_bytes`, `aegis_l4_full_verify_rereads_the_log_from_disk`. |
| L5 | Filtered paging scanned every entry after `after_seq`: O(n) per page. | `ReadIndex` (`persistence.rs`): seq-ordered position lists of findings, per department, per event type and per (department, event type), rebuilt on open and extended on every append. A page finds its start in each list it needs by binary search and merges them (a heap over k lists): O(k log n + page log k). `entries_selected` (`server.rs`) copies at most `READ_CHUNK` selected entries per lock hold, as before. Guard: `filtered_pages_come_from_the_index_and_match_a_naive_scan` (20 000 entries; a 10-entry page of a department whose entries are spread through the log takes ≤ 16 index steps, where a scan would take ~20 000; and the index equals a naive filter for 400 filter/scope/range combinations). Cost: memory for four position lists (≈ 3 `usize` per event, 1 per finding). |


## Verification

Current test counts: [docs/test-counts.md](../test-counts.md) (generated). The original commands and a live
three-process run are recorded in the README ("Sep 24 2026 — money is exact, ledger records events"). Fix wave 25
(H7, AEGIS N24-S-8) checked that §11's SIGKILL/OOM residual paragraph (commit 27d3440) and the matching module
documentation in `src/bin/server.rs` are present and that its numbers (49/50 temp files, 50/50 port files) are the
ones in the AEGIS round-24 services report (text only; those SIGKILL measurements were not re-run in wave 25).
`FIX_WAVE_23b.md` — the wave-23b ruling record, which lives in the review session's scratchpad, NOT in this
repository — concerns fulfillment, detection and onboarding tests, not the ledger; it is cited so this ADR does not
claim a ruling it was not part of (docs/findings/OPEN.md C4-5 tracks such out-of-repo references).
