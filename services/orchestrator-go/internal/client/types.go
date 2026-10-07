// Package client is the Go orchestrator's REST client for the Python
// detection-py service. Field names/casing here are matched against the
// ACTUAL captured JSON output of the real FastAPI service (see
// docs/adr/0001-revenue-recovery-1a-architecture.md), not guessed.
package client

import (
	"encoding/json"
	"fmt"
)

// Order mirrors zbm_schema.Order closely enough for what the orchestrator
// needs to pass through — it does not re-validate business rules, that is
// the Python detection layer's job. The orchestrator's job is routing and
// correlation, not re-implementing detection logic in a second language.
type Order map[string]any

type Subscription map[string]any

// LabeledValue mirrors zbm_schema.LabeledValue. AmountUSD is a validated,
// string-backed Money (README gap #6) — never a float64.
type LabeledValue struct {
	AmountUSD      Money  `json:"amount_usd"`
	Classification string `json:"classification"`
	Confidence     string `json:"confidence"`
}

// UnmarshalJSON enforces that a present recoverable_value carries a
// positive amount_usd (contract section 1: positive-only fields reject
// "0.00"), in addition to Money's own string/format validation. A missing
// amount_usd is rejected too rather than silently left as the zero Money.
func (lv *LabeledValue) UnmarshalJSON(data []byte) error {
	type plain LabeledValue // no methods — avoids recursion
	var p plain
	if err := json.Unmarshal(data, &p); err != nil {
		return fmt.Errorf("recoverable_value: %w", err)
	}
	if !p.AmountUSD.IsValid() {
		return fmt.Errorf("recoverable_value: %w: amount_usd is missing", ErrInvalidMoney)
	}
	if !p.AmountUSD.IsPositive() {
		return fmt.Errorf("recoverable_value: %w: amount_usd must be positive, got %q", ErrInvalidMoney, p.AmountUSD.String())
	}
	*lv = LabeledValue(p)
	return nil
}

// Finding mirrors zbm_schema.Finding field for field (Revenue Recovery fix
// wave, Oct 6 2026: client_id, period_label, evidence_class, methodology_id
// and methodology added). The orchestrator sends findings back to
// detection-py (/correlation/overlaps), which re-validates every field —
// including that finding_id is the hash of (client_id, agent_id,
// entity_type, entity_id, period_label) — so nothing may be dropped here.
type Finding struct {
	FindingID        string        `json:"finding_id"`
	ClientID         string        `json:"client_id"`
	AgentID          string        `json:"agent_id"`
	LeakCategory     string        `json:"leak_category"`
	EntityType       string        `json:"entity_type"`
	EntityID         string        `json:"entity_id"`
	PeriodLabel      *string       `json:"period_label"`
	CustomerID       string        `json:"customer_id"`
	CauseCertainty   string        `json:"cause_certainty"`
	CauseDescription string        `json:"cause_description"`
	RecoverableValue *LabeledValue `json:"recoverable_value"`
	// EvidenceClass: OBSERVED, ESTIMATED, MODELED or UNKNOWN (UNKNOWN exactly
	// when RecoverableValue is nil). MethodologyID/Methodology say how the
	// figure (or its absence) was arrived at. ADR 0001 "Evidence class and
	// methodology".
	EvidenceClass string `json:"evidence_class"`
	MethodologyID string `json:"methodology_id"`
	Methodology   string `json:"methodology"`
	// ValueBasis (AEGIS L1, Oct 7 2026): the base and rate a rate-derived
	// figure was computed from (the affiliate commission: order subtotal x
	// commission rate). Nil for figures not derived from a rate. Recorded in
	// the ledger as its own scan event (orchestrator ledger_record.go).
	ValueBasis *ValueBasis `json:"value_basis"`
	DetectedAt string      `json:"detected_at"`
}

// ValueBasis mirrors zbm_schema.ValueBasis. RatePercent is the exact decimal
// text of the rate (no exponent); the orchestrator checks it and recomputes
// the amount before recording.
type ValueBasis struct {
	BaseUSD     Money  `json:"base_usd"`
	RatePercent string `json:"rate_percent"`
}

type findingsResponse struct {
	Findings []Finding `json:"findings"`
}

// Every detect request names the tenant (E-3). The list fields are never
// nil when marshaled: nonNil (client.go) turns a nil slice into [] so the
// wire never carries `null` for a list (E-1: detection-py answers 422 to
// {"findings":null}, which made every clean-store scan fail with 502).
type ordersRequest struct {
	ClientID string  `json:"client_id"`
	Orders   []Order `json:"orders"`
}

type subscriptionsRequest struct {
	ClientID      string         `json:"client_id"`
	AsOf          string         `json:"as_of"`
	Subscriptions []Subscription `json:"subscriptions"`
}

type findingsRequest struct {
	Findings []Finding `json:"findings"`
}
