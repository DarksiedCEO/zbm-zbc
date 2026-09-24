package client

import (
	"bytes"
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
	FindingID           string `json:"finding_id"`
	AgentID             string `json:"agent_id"`
	EntityID            string `json:"entity_id"`
	LeakCategory        string `json:"leak_category"`
	AmountUSD           *Money `json:"amount_usd"` // canonical two-decimal string, or null
	ValueClassification string `json:"value_classification"`
	DecisionConfidence  string `json:"decision_confidence"`
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
		// Exact pass-through of detection-py's string; no conversion.
		amt := f.RecoverableValue.AmountUSD
		if !amt.IsValid() {
			return nil, fmt.Errorf("finding %s: %w: recoverable_value.amount_usd is unset", f.FindingID, ErrInvalidMoney)
		}
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

	// Only 200 (valid) and 409 (tampered, or empty) carry a verify verdict.
	// Anything else (401 wrong token, 404, 500 disk error, ...) is a failed
	// request, not a verdict — before this check a 401 body parsed as
	// {"valid":false} and was reported as a chain-integrity failure.
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusConflict {
		return nil, fmt.Errorf("ledger verify returned %d: %s", resp.StatusCode, string(body))
	}
	var out LedgerVerifyResult
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("unmarshal verify response: %w (body=%s)", err, string(body))
	}
	return &out, nil
}

// LedgerFindingRecord is one finding entry exactly as the ledger recorded it
// (GET /ledger/entries, "kind":"finding"). The ledger stores less than a
// detection Finding: no cause_description or customer_id, and the labels
// are value_classification / decision_confidence (null when the finding
// carried no recoverable value).
type LedgerFindingRecord struct {
	Seq                 uint64  `json:"seq"`
	FindingID           string  `json:"finding_id"`
	AgentID             string  `json:"agent_id"`
	EntityID            string  `json:"entity_id"`
	LeakCategory        string  `json:"leak_category"`
	AmountUSD           *Money  `json:"amount_usd"`
	ValueClassification *string `json:"value_classification"`
	DecisionConfidence  *string `json:"decision_confidence"`
	RecordedAt          string  `json:"recorded_at"`
	PrevHash            string  `json:"prev_hash"`
	Hash                string  `json:"hash"`
	// AmountOutOfContract is true when the ledger holds an amount string
	// that is not valid money under the current contract (e.g. a legacy
	// entry above the ADR 0003 section 1a bound). AmountUSD is then null:
	// the value is flagged, never passed on as if it were valid money.
	AmountOutOfContract bool `json:"amount_out_of_contract"`
}

// LedgerEntriesResult is the finding view of GET /ledger/entries.
type LedgerEntriesResult struct {
	TotalEntries int                   // every entry, findings and events
	Findings     []LedgerFindingRecord // finding entries, in seq order
}

// Entries reads the whole ledger (GET /ledger/entries) and returns its
// finding entries. Read-only: it never calls a ledger write endpoint.
// Fails closed on any entry whose kind it does not know, or a finding whose
// amount_usd is neither a string nor null — an unexpected shape in the
// evidence ledger is an error to surface, not something to skip.
func (l *LedgerClient) Entries(ctx context.Context) (*LedgerEntriesResult, error) {
	var raw []json.RawMessage
	if err := l.base.doJSON(ctx, http.MethodGet, "/ledger/entries", nil, &raw); err != nil {
		return nil, err
	}
	out := &LedgerEntriesResult{TotalEntries: len(raw), Findings: []LedgerFindingRecord{}}
	for i, entry := range raw {
		var head struct {
			Kind *string `json:"kind"`
		}
		if err := json.Unmarshal(entry, &head); err != nil {
			return nil, fmt.Errorf("ledger entry %d: %w", i, err)
		}
		if head.Kind == nil {
			return nil, fmt.Errorf("ledger entry %d has no kind", i)
		}
		switch *head.Kind {
		case "event":
			continue
		case "finding":
		default:
			return nil, fmt.Errorf("ledger entry %d has unknown kind %q", i, *head.Kind)
		}
		// amount_usd is decoded separately: an out-of-contract string must
		// be flagged, not make the whole read fail.
		var wire struct {
			LedgerFindingRecord
			AmountUSD json.RawMessage `json:"amount_usd"`
		}
		if err := json.Unmarshal(entry, &wire); err != nil {
			return nil, fmt.Errorf("ledger finding entry %d: %w", i, err)
		}
		rec := wire.LedgerFindingRecord
		rec.AmountUSD = nil
		amt := bytes.TrimSpace(wire.AmountUSD)
		switch {
		case len(amt) == 0 || bytes.Equal(amt, []byte("null")):
		case amt[0] == '"':
			var s string
			if err := json.Unmarshal(amt, &s); err != nil {
				return nil, fmt.Errorf("ledger finding entry %d amount_usd: %w", i, err)
			}
			if m, err := ParseMoney(s); err == nil {
				rec.AmountUSD = &m
			} else {
				rec.AmountOutOfContract = true
			}
		default:
			return nil, fmt.Errorf("ledger finding entry %d: %w: amount_usd must be a string or null, got %s", i, ErrInvalidMoney, string(amt))
		}
		out.Findings = append(out.Findings, rec)
	}
	return out, nil
}
