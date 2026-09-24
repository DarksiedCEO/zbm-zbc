import type { RecordedFindingsResult } from "@/types/finding";

export type LedgerState = "invalid" | "empty" | "verified";

// The verdict comes first (fix wave 3): an empty ledger is a valid chain
// (ledger-rust answers {"valid":true,"entries":0}), so "empty" is only ever
// shown for a ledger that verified. Any invalid or missing verdict is an
// integrity failure, whatever the entry count.
export function ledgerState(result: RecordedFindingsResult): LedgerState {
  const v = result.ledger_verify;
  if (!v || !v.valid) return "invalid";
  return result.ledger_entries_total === 0 ? "empty" : "verified";
}
