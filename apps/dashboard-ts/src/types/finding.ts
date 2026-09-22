// Mirrors the REAL JSON contract served by services/detection-py's
// Finding model (see zbm_schema/__init__.py) and the orchestrator's
// ScanResult (see services/orchestrator-go/internal/orchestrator). Kept
// as a hand-written type here — no live client yet, so no codegen
// pipeline is justified for a contract this small.

export type ValueClassification = "observed" | "attributed" | "incremental" | "financially_verified";
export type DecisionConfidence = "low" | "medium" | "high" | "very_high";
export type CauseCertainty = "named" | "uncertain";

export interface LabeledValue {
  amount_usd: number;
  classification: ValueClassification;
  confidence: DecisionConfidence;
}

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

export interface ScanResult {
  findings: Finding[];
  overlapping_claims: Record<string, Finding[]>;
  agents_run: string[];
  non_live_data_source: boolean;
}
