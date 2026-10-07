package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"
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
	return &LedgerClient{base: newServiceClient("ledger-rust", baseURL, token, maxLedgerResponseBytes)}
}

// EventInput is the body of POST /ledger/events (ADR 0003 section 3). Every
// field is required; ledger-rust validates the charsets and lengths.
type EventInput struct {
	EventID       string `json:"event_id"`
	Department    string `json:"department"`
	EventType     string `json:"event_type"`
	Actor         string `json:"actor"`
	SubjectID     string `json:"subject_id"`
	PayloadSHA256 string `json:"payload_sha256"`
	Summary       string `json:"summary"`
}

// ErrEventConflict: the ledger already holds this event_id with DIFFERENT
// content (409). An event id describes exactly one event, so this is never
// retried — it means two different things were given one id.
var ErrEventConflict = errors.New("ledger already holds this event_id with different content")

// Event append retry policy (Revenue Recovery fix wave, Oct 6 2026, E-2).
// POST /ledger/events is idempotent on event_id: an identical retry of an
// event the ledger already committed answers 200 with the existing entry and
// appends nothing. So a call whose answer was lost — a timeout after the
// ledger committed (sweep live probe L3), a connection reset, a 5xx from a
// proxy — is retried with the SAME body, and can never record the event
// twice.
const (
	eventAppendAttempts = 4
	eventRetryBaseDelay = 250 * time.Millisecond
)

// EventAppendResult says whether the ledger created the entry (201) or
// already had it (200), and on which attempt it answered.
type EventAppendResult struct {
	Seq      uint64
	Created  bool
	Attempts int
}

// AppendEvent records ev, retrying a lost or failed answer (no response,
// timeout, 5xx) up to eventAppendAttempts times with the identical body.
// 409 is ErrEventConflict (wrapped in the UpstreamError); any other 4xx is
// returned at once.
func (l *LedgerClient) AppendEvent(ctx context.Context, ev EventInput) (*EventAppendResult, error) {
	body, err := json.Marshal(ev)
	if err != nil {
		return nil, fmt.Errorf("marshal ledger event %s: %w", ev.EventID, err)
	}
	var lastErr error
	for attempt := 1; attempt <= eventAppendAttempts; attempt++ {
		if attempt > 1 {
			delay := eventRetryBaseDelay << (attempt - 2)
			select {
			case <-ctx.Done():
				return nil, errors.Join(lastErr, ctx.Err())
			case <-time.After(delay):
			}
		}
		res, retry, err := l.appendEventOnce(ctx, body)
		if err == nil {
			res.Attempts = attempt
			return res, nil
		}
		lastErr = err
		if !retry || ctx.Err() != nil {
			return nil, err
		}
	}
	return nil, lastErr
}

func (l *LedgerClient) appendEventOnce(ctx context.Context, body []byte) (*EventAppendResult, bool, error) {
	const path = "/ledger/events"
	c := l.base
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return nil, false, c.upstreamErr(UpstreamBadRequest, http.MethodPost, path, 0, err.Error())
	}
	req.Header.Set("Content-Type", "application/json")
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, true, c.upstreamErr(UpstreamUnreachable, http.MethodPost, path, 0, err.Error())
	}
	defer resp.Body.Close()
	respBody, err := c.readBody(http.MethodPost, path, resp)
	if err != nil {
		return nil, true, err // the answer was cut off: the event may or may not be in; retrying is safe
	}
	switch {
	case resp.StatusCode == http.StatusCreated || resp.StatusCode == http.StatusOK:
		var entry struct {
			Seq *uint64 `json:"seq"`
		}
		if err := json.Unmarshal(respBody, &entry); err != nil || entry.Seq == nil {
			return nil, true, c.upstreamErr(UpstreamBadResponse, http.MethodPost, path, resp.StatusCode,
				"event response has no seq (body="+snippet(respBody)+")")
		}
		return &EventAppendResult{Seq: *entry.Seq, Created: resp.StatusCode == http.StatusCreated}, false, nil
	case resp.StatusCode == http.StatusConflict:
		return nil, false, &wrappedUpstreamError{
			UpstreamError: c.upstreamErr(UpstreamStatus, http.MethodPost, path, resp.StatusCode, snippet(respBody)),
			cause:         ErrEventConflict,
		}
	case resp.StatusCode >= 500:
		return nil, true, c.upstreamErr(UpstreamStatus, http.MethodPost, path, resp.StatusCode, snippet(respBody))
	default:
		return nil, false, c.upstreamErr(UpstreamStatus, http.MethodPost, path, resp.StatusCode, snippet(respBody))
	}
}

type LedgerVerifyResult struct {
	Valid   bool   `json:"valid"`
	Entries int    `json:"entries"`
	Error   string `json:"error"`
}

// Verify reads the ledger's own /ledger/verify judgment. Deliberately
// bypasses doJSON's 2xx-only success handling: the Rust service answers 409
// for a chain that fails verification, and callers need the parsed verdict,
// not just a generic transport error. An empty ledger is a valid chain:
// 200 {"valid":true,"entries":0} (fix wave 3; it used to be a 409 "Empty"
// verdict that every caller had to special-case).
func (l *LedgerClient) Verify(ctx context.Context) (*LedgerVerifyResult, error) {
	const path = "/ledger/verify"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, l.base.baseURL+path, nil)
	if err != nil {
		return nil, l.base.upstreamErr(UpstreamBadRequest, http.MethodGet, path, 0, err.Error())
	}
	// Caught live, Sep 22 2026, by an actual 3-process end-to-end run (not
	// just unit tests): Verify() builds its own request instead of going
	// through doJSON (deliberately, for the 409-is-a-verdict handling
	// below) and had no Authorization header at all, so it 401'd against
	// the newly-authenticated ledger even after AppendFinding's fix.
	if l.base.token != "" {
		req.Header.Set("Authorization", "Bearer "+l.base.token)
	}
	resp, err := l.base.httpClient.Do(req)
	if err != nil {
		return nil, l.base.upstreamErr(UpstreamUnreachable, http.MethodGet, path, 0, err.Error())
	}
	defer resp.Body.Close()

	body, err := l.base.readBody(http.MethodGet, path, resp)
	if err != nil {
		return nil, err
	}

	// Only 200 (valid) and 409 (chain failed verification) carry a verdict.
	// Anything else (401 wrong token, 404, 500 disk error, ...) is a failed
	// request, not a verdict — before this check a 401 body parsed as
	// {"valid":false} and was reported as a chain-integrity failure.
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusConflict {
		return nil, l.base.upstreamErr(UpstreamStatus, http.MethodGet, path, resp.StatusCode, snippet(body))
	}
	var out LedgerVerifyResult
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, l.base.upstreamErr(UpstreamBadResponse, http.MethodGet, path, resp.StatusCode,
			fmt.Sprintf("unmarshal verify response: %v (body=%s)", err, snippet(body)))
	}
	if out.Valid != (resp.StatusCode == http.StatusOK) {
		return nil, l.base.upstreamErr(UpstreamBadResponse, http.MethodGet, path, resp.StatusCode,
			fmt.Sprintf("verify verdict valid=%v contradicts HTTP status (body=%s)", out.Valid, snippet(body)))
	}
	return &out, nil
}

// LedgerEvent is one event entry exactly as the ledger recorded it (GET
// /ledger/entries, "kind":"event").
type LedgerEvent struct {
	Seq           uint64 `json:"seq"`
	EventID       string `json:"event_id"`
	Department    string `json:"department"`
	EventType     string `json:"event_type"`
	Actor         string `json:"actor"`
	SubjectID     string `json:"subject_id"`
	PayloadSHA256 string `json:"payload_sha256"`
	Summary       string `json:"summary"`
	RecordedAt    string `json:"recorded_at"`
	PrevHash      string `json:"prev_hash"`
	Hash          string `json:"hash"`
}

// LegacyFindingEntry is a pre-Oct-6 "kind":"finding" ledger entry (POST
// /ledger/append), as recorded. AmountUSD is nil when the entry has none OR
// when what it holds is not contract money (old logs persisted negative and
// over-bound amounts); AmountOutOfContract says which.
type LegacyFindingEntry struct {
	Seq                 uint64  `json:"seq"`
	FindingID           string  `json:"finding_id"`
	AgentID             string  `json:"agent_id"`
	EntityID            string  `json:"entity_id"`
	LeakCategory        string  `json:"leak_category"`
	AmountUSD           *Money  `json:"amount_usd"`
	AmountOutOfContract bool    `json:"amount_out_of_contract"`
	ValueClassification *string `json:"value_classification"`
	DecisionConfidence  *string `json:"decision_confidence"`
	RecordedAt          string  `json:"recorded_at"`
	Hash                string  `json:"hash"`
}

// LedgerEntriesPage is a run of ledger entries in seq order.
type LedgerEntriesPage struct {
	// Entries counts the entries this read returned, findings and events.
	// It is NOT the ledger's size: a filtered or caller-scoped read returns
	// a subset. The ledger's size comes from Head (or the verify verdict).
	Entries int
	// Events: the event entries of the page, in seq order.
	Events []LedgerEvent
	// LegacyFindings counts "kind":"finding" entries — what orchestrator-go
	// wrote through POST /ledger/append before the Oct 6 2026 fix wave. They
	// carry no scan id and no tenant, collided across clients (E-3) and were
	// written before the E-4 amount corrections, so they are counted and
	// shown as ignored, never presented as findings.
	LegacyFindings int
	// LegacyFindingRows: those entries themselves, so they can be shown
	// (labelled as legacy and never counted) instead of only counted.
	LegacyFindingRows []LegacyFindingEntry
	// LastSeq is the seq of the page's last entry (valid when Entries > 0).
	LastSeq uint64
}

// Entries reads the whole ledger (GET /ledger/entries) as one page.
// Read-only. Fails closed on an entry that is not an object with a known
// kind, or an event entry that does not decode — an unexpected shape in the
// evidence ledger is an error to surface, not something to skip.
//
// E-8: this is the ONE place a ledger read happens. ledger-rust has no
// pagination today; a paginated read (?after_seq=&department=&event_type=,
// being added on a separate branch) replaces this body and returns pages
// with more=true until the end — orchestrator.RecordedFindings already
// consumes pages in a loop (see entryPages there).
func (l *LedgerClient) Entries(ctx context.Context) (*LedgerEntriesPage, error) {
	var raw []json.RawMessage
	if err := l.base.doJSON(ctx, http.MethodGet, "/ledger/entries", nil, &raw); err != nil {
		return nil, err
	}
	out := &LedgerEntriesPage{Entries: len(raw), Events: []LedgerEvent{}, LegacyFindingRows: []LegacyFindingEntry{}}
	for i, entry := range raw {
		var head struct {
			Kind *string `json:"kind"`
			Seq  *uint64 `json:"seq"`
		}
		if err := json.Unmarshal(entry, &head); err != nil {
			return nil, fmt.Errorf("ledger entry %d: %w", i, err)
		}
		if head.Kind == nil {
			return nil, fmt.Errorf("ledger entry %d has no kind", i)
		}
		if head.Seq == nil {
			return nil, fmt.Errorf("ledger entry %d has no seq", i)
		}
		out.LastSeq = *head.Seq
		switch *head.Kind {
		case "finding":
			out.LegacyFindings++
			row, err := decodeLegacyFinding(entry)
			if err != nil {
				return nil, fmt.Errorf("ledger finding entry %d: %w", i, err)
			}
			out.LegacyFindingRows = append(out.LegacyFindingRows, *row)
		case "event":
			var ev LedgerEvent
			if err := json.Unmarshal(entry, &ev); err != nil {
				return nil, fmt.Errorf("ledger event entry %d: %w", i, err)
			}
			out.Events = append(out.Events, ev)
		default:
			return nil, fmt.Errorf("ledger entry %d has unknown kind %q", i, snippet([]byte(*head.Kind)))
		}
	}
	return out, nil
}

// decodeLegacyFinding reads a "kind":"finding" entry. Its amount is kept
// only if it is contract money; anything else (a JSON number, a negative or
// over-bound string — ledger-rust still loads those from old logs) is
// reported as out of contract, never shown as dollars.
func decodeLegacyFinding(entry json.RawMessage) (*LegacyFindingEntry, error) {
	var f struct {
		Seq                 uint64          `json:"seq"`
		FindingID           string          `json:"finding_id"`
		AgentID             string          `json:"agent_id"`
		EntityID            string          `json:"entity_id"`
		LeakCategory        string          `json:"leak_category"`
		AmountUSD           json.RawMessage `json:"amount_usd"`
		ValueClassification *string         `json:"value_classification"`
		DecisionConfidence  *string         `json:"decision_confidence"`
		RecordedAt          string          `json:"recorded_at"`
		Hash                string          `json:"hash"`
	}
	if err := json.Unmarshal(entry, &f); err != nil {
		return nil, err
	}
	row := &LegacyFindingEntry{
		Seq: f.Seq, FindingID: f.FindingID, AgentID: f.AgentID, EntityID: f.EntityID, LeakCategory: f.LeakCategory,
		ValueClassification: f.ValueClassification, DecisionConfidence: f.DecisionConfidence,
		RecordedAt: f.RecordedAt, Hash: f.Hash,
	}
	if len(f.AmountUSD) > 0 && string(f.AmountUSD) != "null" {
		var m Money
		if err := json.Unmarshal(f.AmountUSD, &m); err == nil && m.IsPositive() {
			row.AmountUSD = &m
		} else {
			row.AmountOutOfContract = true
		}
	}
	return row, nil
}

// LedgerHead is GET /ledger/head (ledger-rust sweep F-6): the number of
// entries in the whole ledger, the last seq and its hash — independent of
// what any one read returned.
type LedgerHead struct {
	Entries  uint64  `json:"entries"`
	HeadSeq  *uint64 `json:"head_seq"`
	HeadHash string  `json:"head_hash"`
}

// ErrHeadUnsupported: the ledger has no GET /ledger/head (a ledger-rust from
// before sweep F answers 404). Callers fall back to the verify verdict's
// entry count.
var ErrHeadUnsupported = errors.New("ledger-rust has no GET /ledger/head")

// Head reads GET /ledger/head and checks its shape: head_seq is null exactly
// when the ledger is empty and entries-1 otherwise, head_hash 64 lowercase
// hex. A 404 is ErrHeadUnsupported (wrapped in the UpstreamError).
func (l *LedgerClient) Head(ctx context.Context) (*LedgerHead, error) {
	const path = "/ledger/head"
	var h LedgerHead
	if err := l.base.doJSON(ctx, http.MethodGet, path, nil, &h); err != nil {
		var ue *UpstreamError
		if errors.As(err, &ue) && ue.StatusCode == http.StatusNotFound {
			return nil, &wrappedUpstreamError{UpstreamError: ue, cause: ErrHeadUnsupported}
		}
		return nil, err
	}
	hexOK := len(h.HeadHash) == 64
	for _, c := range h.HeadHash {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			hexOK = false
		}
	}
	seqOK := (h.HeadSeq == nil && h.Entries == 0) || (h.HeadSeq != nil && h.Entries > 0 && *h.HeadSeq == h.Entries-1)
	if !hexOK || !seqOK {
		return nil, l.base.upstreamErr(UpstreamBadResponse, http.MethodGet, path, http.StatusOK,
			fmt.Sprintf("head response is not {entries, head_seq, head_hash} (entries=%d head_seq=%v)", h.Entries, h.HeadSeq))
	}
	return &h, nil
}
