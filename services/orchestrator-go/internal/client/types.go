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

type Finding struct {
	FindingID        string        `json:"finding_id"`
	AgentID          string        `json:"agent_id"`
	LeakCategory     string        `json:"leak_category"`
	EntityType       string        `json:"entity_type"`
	EntityID         string        `json:"entity_id"`
	CustomerID       string        `json:"customer_id"`
	CauseCertainty   string        `json:"cause_certainty"`
	CauseDescription string        `json:"cause_description"`
	RecoverableValue *LabeledValue `json:"recoverable_value"`
	DetectedAt       string        `json:"detected_at"`
}

type findingsResponse struct {
	Findings []Finding `json:"findings"`
}

type ordersRequest struct {
	Orders []Order `json:"orders"`
}

type subscriptionsRequest struct {
	Subscriptions []Subscription `json:"subscriptions"`
}

type findingsRequest struct {
	Findings []Finding `json:"findings"`
}
