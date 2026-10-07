// Mirrors the REAL JSON contracts served by services/orchestrator-go. Kept
// as hand-written types — no live client yet, so no codegen pipeline is
// justified for contracts this small. Money is always a string (never a
// number) — see src/lib/money.ts.

export type ValueClassification = "observed" | "attributed" | "incremental" | "financially_verified";
export type DecisionConfidence = "low" | "medium" | "high" | "very_high";
export type CauseCertainty = "named" | "uncertain";
// How a dollar figure was obtained (ADR 0001 "Evidence class and
// methodology"). UNKNOWN exactly when there is no figure.
export type EvidenceClass = "OBSERVED" | "ESTIMATED" | "MODELED" | "UNKNOWN";

// Canonical two-decimal money string, e.g. "54.38" (docs/adr/0003).
// Never parse this into a number for arithmetic.
export type MoneyString = string;

export interface LabeledValue {
  amount_usd: MoneyString;
  classification: ValueClassification;
  confidence: DecisionConfidence;
}

// The base and rate a rate-derived figure was computed from (AEGIS L1, Oct 7
// 2026): amount = base_usd x rate_percent / 100, cent-rounded half-up.
export interface ValueBasis {
  base_usd: MoneyString;
  rate_percent: string;
}

// A detection Finding (detection-py zbm_schema.Finding), as returned by
// POST /revenue-recovery/scan.
export interface Finding {
  finding_id: string;
  client_id: string;
  agent_id: string;
  leak_category: string;
  entity_type: string;
  entity_id: string;
  period_label: string | null;
  customer_id: string;
  cause_certainty: CauseCertainty;
  cause_description: string;
  recoverable_value: LabeledValue | null;
  evidence_class: EvidenceClass;
  methodology_id: string;
  methodology: string;
  value_basis: ValueBasis | null;
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
  scan_id: string;
  client_id: string;
  findings: Finding[];
  overlapping_claims: Record<string, Finding[]>;
  agents_run: string[];
  non_live_data_source: boolean;
  ledger_entries_written: number;
  ledger_verify: LedgerVerifyResult | null;
}

// One distinct finding as the evidence ledger recorded it
// (orchestrator-go internal/orchestrator/recorded.go RecordedFinding): the
// record from the latest COMPLETED scan that contains its finding_id. The
// ledger stores no cause_description or customer_id.
export interface RecordedFinding {
  seq: number;
  finding_id: string;
  client_id: string;
  agent_id: string;
  leak_category: string;
  entity_type: string;
  entity_id: string;
  period_label: string | null;
  amount_usd: MoneyString | null;
  value_classification: ValueClassification | null;
  decision_confidence: DecisionConfidence | null;
  evidence_class: EvidenceClass;
  methodology_id: string;
  scan_id: string;
  payload_sha256: string;
  recorded_at: string;
  prev_hash: string;
  hash: string;
  amount_out_of_contract: boolean;
  first_seq: number;
  times_recorded: number;
  amounts_differ_across_records: boolean;
  // false: the client's latest completed scan no longer found it (stale).
  // A quote uses only findings that are present.
  present_in_latest_scan: boolean;
  value_basis: ValueBasis | null;
  // Set when the recorded labels claim more than the recorded evidence class
  // supports (a record from before the Oct 7 2026 invariant): the reason.
  labels_exceed_evidence: string | null;
}

// One completed, consistent scan.
export interface ScanSummary {
  scan_id: string;
  client_id: string;
  data_source: string;
  fixture: boolean;
  tenant_defaulted: boolean;
  as_of: string;
  findings: number;
  started_seq: number;
  completed_seq: number;
}

export type ExcludedScanStatus = "running" | "incomplete" | "abandoned" | "aborted" | "inconsistent";

// A scan in the ledger that does not count (AEGIS M4/L3).
export interface ExcludedScan {
  scan_id: string;
  client_id: string;
  finding_events: number;
  reason: string;
  status: ExcludedScanStatus;
  started_at: string;
}

// A pre-Oct-6-2026 "kind":"finding" ledger entry: no scan, no tenant,
// pre-correction amounts. Shown labelled as legacy, never counted (AEGIS M4).
export interface LegacyFinding {
  seq: number;
  finding_id: string;
  agent_id: string;
  entity_id: string;
  leak_category: string;
  amount_usd: MoneyString | null;
  amount_out_of_contract: boolean;
  value_classification: string | null;
  decision_confidence: string | null;
  recorded_at: string;
  hash: string;
}

// GET /revenue-recovery/findings — read-only; viewing never writes.
export interface RecordedFindingsResult {
  findings: RecordedFinding[];
  overlapping_claims: Record<string, RecordedFinding[]>;
  scans: ScanSummary[];
  excluded_scans: ExcludedScan[];
  // Every entry in the whole ledger (from the ledger's head or verify), not
  // the size of the orchestrator's own read.
  ledger_entries_total: number;
  ledger_total_source: "head" | "verify" | "entries_read";
  ledger_entries_read: number;
  finding_entries_total: number;
  legacy_finding_entries_ignored: number;
  legacy_findings: LegacyFinding[];
  ledger_verify: LedgerVerifyResult | null;
  non_live_data_source: boolean;
}
