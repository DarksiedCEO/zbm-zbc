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
- Reading is not writing (fix wave 1): orchestrator-go's
  `GET /revenue-recovery/findings` reads `GET /ledger/entries` (entries of
  kind `"finding"`; events are counted, not shown) plus `GET /ledger/verify`
  and never calls a ledger write endpoint. The dashboard renders that route.
  A scan — which records every finding it detects — is only
  `POST /revenue-recovery/scan`; any other method is 405. Before this, each
  dashboard page view ran a scan and appended ~10 duplicate findings.
  Python callers use the `LedgerClient.record_event(...)` protocol defined
  in the build contracts (implemented by the calling services, not here).

## Verification

Commands, counts and a live three-process run are recorded in the README
("Sep 24 2026 — money is exact, ledger records events").
