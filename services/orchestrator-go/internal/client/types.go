// Package client is the Go orchestrator's REST client for the Python
// detection-py service. Field names/casing here are matched against the
// ACTUAL captured JSON output of the real FastAPI service (see
// docs/adr/0001-revenue-recovery-1a-architecture.md), not guessed.
package client

// Order mirrors zbm_schema.Order closely enough for what the orchestrator
// needs to pass through — it does not re-validate business rules, that is
// the Python detection layer's job. The orchestrator's job is routing and
// correlation, not re-implementing detection logic in a second language.
type Order map[string]any

type Subscription map[string]any

type LabeledValue struct {
	AmountUSD      float64 `json:"amount_usd"`
	Classification string  `json:"classification"`
	Confidence     string  `json:"confidence"`
}

type Finding struct {
	FindingID       string        `json:"finding_id"`
	AgentID         string        `json:"agent_id"`
	LeakCategory    string        `json:"leak_category"`
	EntityType      string        `json:"entity_type"`
	EntityID        string        `json:"entity_id"`
	CustomerID      string        `json:"customer_id"`
	CauseCertainty  string        `json:"cause_certainty"`
	CauseDescription string       `json:"cause_description"`
	RecoverableValue *LabeledValue `json:"recoverable_value"`
	DetectedAt      string        `json:"detected_at"`
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
