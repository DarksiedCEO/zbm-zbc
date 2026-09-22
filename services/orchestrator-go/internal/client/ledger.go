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

// NewLedgerClient wraps the same REST plumbing as DetectionClient.
//
// Independent review, Sep 22 2026 (CONFIRMED): ledger-rust had zero
// authentication on any route, including POST /ledger/append — the
// single most important service to protect, unauthenticated. Fixed
// server-side (LEDGER_SERVICE_TOKEN, fail-closed). This constructor now
// takes and sends a token, exactly as this comment previously said
// whoever closed that gap should do — token must match ledger-rust's own
// LEDGER_SERVICE_TOKEN, or every call fails with 401, loudly, not
// silently, same failure mode as NewDetectionClient with a wrong token.
func NewLedgerClient(baseURL, token string) *LedgerClient {
	return &LedgerClient{base: NewDetectionClient(baseURL, token)}
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
	// Caught live, Sep 22 2026, by an actual 3-process end-to-end run (not
	// just unit tests): Verify() builds its own request instead of going
	// through doJSON (deliberately, for the 409-is-not-an-error handling
	// below) and had no Authorization header at all, so it 401'd against
	// the newly-authenticated ledger even after AppendFinding's fix. A
	// full pipeline scan surfaced this as "LEDGER INTEGRITY FAILURE after
	// scan: missing ... Authorization header" — doJSON's fix alone did
	// not cover this method, since it bypasses doJSON.
	if l.base.token != "" {
		req.Header.Set("Authorization", "Bearer "+l.base.token)
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
