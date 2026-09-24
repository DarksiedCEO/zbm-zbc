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
| Rust `ledger-rust` | **Not yet changed** (owned separately). `Money::parse` should apply the same bound; the expected verdicts are the `ledger_append_expected` column of `fixtures/money_vectors.json`. Until then the ledger accepts over-bound canonical strings; orchestrator-go's read route flags any such recorded amount as `amount_out_of_contract` instead of displaying it. |

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

**Known limitation: non-ASCII bytes in the request head.** `tiny_http`
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

## Verification

Commands, counts and a live three-process run are recorded in the README
("Sep 24 2026 — money is exact, ledger records events").
