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
fraction digits. Positive-only fields (`LabeledValue.amount_usd`, prices,
order values, contracted values) additionally reject `"0.00"`.

| Layer | Representation | Where |
|---|---|---|
| Python `detection-py` | `decimal.Decimal`, quantized to `0.01` with `ROUND_HALF_UP` wherever money is created or computed | `src/zbm_schema/money.py` (`Money`, `PositiveMoney`, `to_money`, `quantize_money`, `format_money`, `percent_of`) |
| Go `orchestrator-go` | `client.Money`, an opaque validated string; the orchestrator does no money arithmetic, it passes the string through byte-for-byte | `internal/client/money.go` |
| Rust `ledger-rust` | `Money(String)`, validated; the ledger does no money arithmetic | `src/money.rs` |
| TS `apps/dashboard-ts` | `amount_usd: string`, displayed verbatim (`"$" + amount`); never parsed to a JS number; no totals are computed | `src/lib/money.ts` |

Python input rules: `Decimal`, `int` and plain decimal strings are
accepted; `float` only through `str(value)` (never `Decimal(float)`), so a
fixture JSON number `49.99` becomes exactly `Decimal("49.99")`. `bool`,
NaN, ±Infinity, exponent notation, whitespace, thousands separators and
negative values (including `"-0.00"` / `"-0.004"`) are rejected. Values
with more than two fraction digits are rounded half-up to cents at the
boundary (`"2.675"` → `2.68`, `"1.005"` → `1.01`). `quantize_money` never
returns a signed zero.

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

Known limit: a legacy line whose number formats to a negative value
(e.g. `-5.0`, or `-0.0` → `"-0.00"`) fails to load with an explicit error
(fail closed). The old `detection-py` schema never produced a non-positive
amount, so no such line is expected; if one exists the ledger refuses to
start and says why instead of silently rewriting it.

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
  Python callers use the `LedgerClient.record_event(...)` protocol defined
  in the build contracts (implemented by the calling services, not here).

## Verification

Commands, counts and a live three-process run are recorded in the README
("Sep 24 2026 — money is exact, ledger records events").
