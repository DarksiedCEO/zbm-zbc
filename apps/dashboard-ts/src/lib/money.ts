// Money display for the dashboard (README gap #6, docs/adr/0003).
//
// amount_usd arrives as the canonical two-decimal string from the backend
// ("1234.50"). It is displayed exactly as recorded — the same text the
// finding's explanation and the evidence ledger carry — and is never parsed
// into a JS number, which would reintroduce binary-float rounding.
//
// The dashboard deliberately computes no money totals: overlapping claims
// (Decision 3) must not be summed automatically, and a correct total needs
// a valuation policy that does not exist yet.
//
// Display guard (fix wave 1, ADR 0003 section 1a): amounts are < 10^15
// dollars, so the pattern allows at most 15 integer digits. Anything else —
// out of range, non-canonical, a JSON number — is never shown as a dollar
// figure. The verdicts are pinned by fixtures/money_vectors.json
// (tests/money.test.ts), shared with detection-py and orchestrator-go.

export const MAX_MONEY = "999999999999999.99";

// JS regexes without the "m" flag: "$" matches only at the very end (no
// trailing "\n"), and [0-9] is ASCII-only (no fullwidth digits).
export const MONEY_PATTERN = /^(0|[1-9][0-9]{0,14})\.[0-9]{2}$/;

export function isMoneyString(value: unknown): value is string {
  return typeof value === "string" && value.length <= MAX_MONEY.length && MONEY_PATTERN.test(value);
}

/** Money for a positive-only field (e.g. recoverable_value.amount_usd): rejects "0.00". */
export function isPositiveMoneyString(value: unknown): value is string {
  return isMoneyString(value) && value !== "0.00";
}

/** "54.38" -> "$54.38". Returns null for anything not in the wire format. */
export function formatUsd(amount: unknown): string | null {
  return isMoneyString(amount) ? `$${amount}` : null;
}
