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

const MONEY_PATTERN = /^(0|[1-9][0-9]*)\.[0-9]{2}$/;

export function isMoneyString(value: unknown): value is string {
  return typeof value === "string" && MONEY_PATTERN.test(value);
}

/** "54.38" -> "$54.38". Returns null for anything not in the wire format. */
export function formatUsd(amount: unknown): string | null {
  return isMoneyString(amount) ? `$${amount}` : null;
}
