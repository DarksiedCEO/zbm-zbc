package client

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

// LedgerClient calls the Rust evidence ledger service.
type LedgerClient struct {
	base *DetectionClient // reuses the same doJSON/http plumbing — same REST discipline, different service
}

func NewLedgerClient(baseURL string) *LedgerClient {
	return &LedgerClient{base: NewDetectionClient(baseURL)}
}

type LedgerRecordInput struct {
	FindingID           string   `json:"finding_id"`
	AgentID             string   `json:"agent_id"`
	EntityID            string   `json:"entity_id"`
	LeakCategory        string   `json:"leak_category"`
	AmountUSD           *float64 `json:"amount_usd"`
	ValueClassification string   `json:"value_classification"`
	DecisionConfidence  string   `json:"decision_confidence"`
}

type LedgerEntry struct {
	Seq      uint64 `json:"seq"`
	Hash     string `json:"hash"`
	PrevHash string `json:"prev_hash"`
}

// AppendFinding writes one Finding to the ledger. A Finding with no
// RecoverableValue still gets an entry (AmountUSD/classification/
// confidence come through null) — coverage-gap findings (Platform
// Integration, Cross-Channel risk) are audit-worthy even with no dollar
// figure attached.
func (l *LedgerClient) AppendFinding(ctx context.Context, f Finding) (*LedgerEntry, error) {
	record := LedgerRecordInput{
		FindingID:    f.FindingID,
		AgentID:      f.AgentID,
		EntityID:     f.EntityID,
		LeakCategory: f.LeakCategory,
	}
	if f.RecoverableValue != nil {
		amt := f.RecoverableValue.AmountUSD
		record.AmountUSD = &amt
		record.ValueClassification = f.RecoverableValue.Classification
		record.DecisionConfidence = f.RecoverableValue.Confidence
	}

	var entry LedgerEntry
	err := l.base.doJSON(ctx, http.MethodPost, "/ledger/append", record, &entry)
	return &entry, err
}

type LedgerVerifyResult struct {
	Valid   bool   `json:"valid"`
	Entries int    `json:"entries"`
	Error   string `json:"error"`
}

// Verify reads the ledger's own /ledger/verify judgment. Deliberately
// bypasses doJSON's 2xx-only success handling: the Rust service returns
// 409 for BOTH "chain tampered" and "ledger empty" (a legitimate, non-
// error state before the first append), and callers need the parsed
// body in either case, not just a generic transport error.
func (l *LedgerClient) Verify(ctx context.Context) (*LedgerVerifyResult, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, l.base.baseURL+"/ledger/verify", nil)
	if err != nil {
		return nil, fmt.Errorf("build verify request: %w", err)
	}
	resp, err := l.base.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("ledger verify request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read verify response: %w", err)
	}

	var out LedgerVerifyResult
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("unmarshal verify response: %w (body=%s)", err, string(body))
	}
	return &out, nil
}
