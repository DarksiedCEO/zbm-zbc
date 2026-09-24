// Mirrors the REAL JSON contracts served by services/orchestrator-go. Kept
// as hand-written types — no live client yet, so no codegen pipeline is
// justified for contracts this small. Money is always a string (never a
// number) — see src/lib/money.ts.

export type ValueClassification = "observed" | "attributed" | "incremental" | "financially_verified";
export type DecisionConfidence = "low" | "medium" | "high" | "very_high";
export type CauseCertainty = "named" | "uncertain";

// Canonical two-decimal money string, e.g. "54.38" (docs/adr/0003).
// Never parse this into a number for arithmetic.
export type MoneyString = string;

export interface LabeledValue {
  amount_usd: MoneyString;
  classification: ValueClassification;
  confidence: DecisionConfidence;
}

// A detection Finding (detection-py zbm_schema.Finding), as returned by
// POST /revenue-recovery/scan.
export interface Finding {
  finding_id: string;
  agent_id: string;
  leak_category: string;
  entity_type: string;
  entity_id: string;
  customer_id: string;
  cause_certainty: CauseCertainty;
  cause_description: string;
  recoverable_value: LabeledValue | null;
  detected_at: string;
}

export interface LedgerVerifyResult {
  valid: boolean;
  entries: number;
  error: string;
}

// POST /revenue-recovery/scan (runs every agent AND writes every finding to
// the ledger — the dashboard never calls it).
export interface ScanResult {
  findings: Finding[];
  overlapping_claims: Record<string, Finding[]>;
  agents_run: string[];
  non_live_data_source: boolean;
  ledger_entries_written: number;
  ledger_verify: LedgerVerifyResult | null;
}

// One distinct finding as the evidence ledger recorded it
// (orchestrator-go internal/orchestrator/recorded.go RecordedFinding): the
// latest ledger entry for its finding_id. The ledger stores no
// cause_description or customer_id.
export interface RecordedFinding {
  seq: number;
  finding_id: string;
  agent_id: string;
  entity_id: string;
  leak_category: string;
  amount_usd: MoneyString | null;
  value_classification: ValueClassification | null;
  decision_confidence: DecisionConfidence | null;
  recorded_at: string;
  prev_hash: string;
  hash: string;
  amount_out_of_contract: boolean;
  first_seq: number;
  times_recorded: number;
  amounts_differ_across_records: boolean;
}

// GET /revenue-recovery/findings — read-only; viewing never writes.
export interface RecordedFindingsResult {
  findings: RecordedFinding[];
  overlapping_claims: Record<string, RecordedFinding[]>;
  ledger_entries_total: number;
  finding_entries_total: number;
  ledger_verify: LedgerVerifyResult | null;
  non_live_data_source: boolean;
}
