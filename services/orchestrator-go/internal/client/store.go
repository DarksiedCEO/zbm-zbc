package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
)

// StoreSnapshot is detection-py's held snapshot of a connected store (ADR
// 0020): the orders, already minimised and in zbm_schema.Order shape, and
// the coverage note (window, counts, which agents the source feeds). The
// orchestrator never talks to a store: detection-py read it earlier, in a
// sync that it recorded on the ledger itself (department rr_store).
type StoreSnapshot struct {
	ClientID   string          `json:"client_id"`
	SnapshotID string          `json:"snapshot_id"`
	DataSource string          `json:"data_source"`
	Orders     []Order         `json:"orders"`
	Coverage   json.RawMessage `json:"coverage"`
	// CoveredAgents: the agent ids the coverage note marks covered (AEGIS
	// golive-rr L5). Parsed from Coverage; never on the wire.
	CoveredAgents map[string]bool `json:"-"`
}

// StoreRefusal is detection-py's answer that no snapshot can be served:
// a closed reason code (detection-py store_connector/reasons.py) and its
// fixed message. Nothing was read from any store to produce it.
type StoreRefusal struct {
	Status  int
	Reason  string
	Message string
}

func (e *StoreRefusal) Error() string {
	return fmt.Sprintf("store snapshot refused: %s (%s)", e.Reason, e.Message)
}

var (
	reasonCodeRE = regexp.MustCompile(`^[A-Z][A-Z_]{1,63}$`)
	snapshotIDRE = regexp.MustCompile(`^snap-[0-9a-f]{24}$`)
	// ErrBadSnapshot: the snapshot is not the contract (wrong tenant,
	// malformed id, missing coverage, too many orders).
	ErrBadSnapshot = errors.New("detection-py sent a store snapshot that is not the contract")
)

// MaxSnapshotOrders is detection-py's hard ceiling on orders per snapshot
// (store_connector/config.py HARD_MAX_ORDERS).
const MaxSnapshotOrders = 1000

// StoreSnapshot fetches the tenant's snapshot. A refusal with a reason code
// (NOT_CONNECTED, NOT_SYNCED, SNAPSHOT_STALE, STORE_READS_DISABLED, ...) is a
// *StoreRefusal; anything else that is not a 200 is an *UpstreamError.
func (c *DetectionClient) StoreSnapshot(ctx context.Context, clientID string) (*StoreSnapshot, error) {
	const path = "/stores/snapshot"
	b, _ := json.Marshal(map[string]string{"client_id": clientID})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(b))
	if err != nil {
		return nil, c.upstreamErr(UpstreamBadRequest, http.MethodPost, path, 0, err.Error())
	}
	req.Header.Set("Content-Type", "application/json")
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, c.upstreamErr(UpstreamUnreachable, http.MethodPost, path, 0, err.Error())
	}
	defer resp.Body.Close()
	body, err := c.readBody(http.MethodPost, path, resp)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		var ref struct {
			Detail struct {
				Reason  string `json:"reason"`
				Message string `json:"message"`
			} `json:"detail"`
		}
		if json.Unmarshal(body, &ref) == nil && reasonCodeRE.MatchString(ref.Detail.Reason) &&
			len(ref.Detail.Message) <= 200 && resp.StatusCode >= 400 && resp.StatusCode < 600 {
			return nil, &StoreRefusal{Status: resp.StatusCode, Reason: ref.Detail.Reason, Message: ref.Detail.Message}
		}
		return nil, c.upstreamErr(UpstreamStatus, http.MethodPost, path, resp.StatusCode, snippet(body))
	}
	var snap StoreSnapshot
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	if err := dec.Decode(&snap); err != nil {
		return nil, c.upstreamErr(UpstreamBadResponse, http.MethodPost, path, resp.StatusCode, "unmarshal snapshot: "+err.Error())
	}
	switch {
	case snap.ClientID != clientID:
		return nil, fmt.Errorf("%w: snapshot is for another tenant", ErrBadSnapshot)
	case !snapshotIDRE.MatchString(snap.SnapshotID):
		return nil, fmt.Errorf("%w: snapshot_id %.40q", ErrBadSnapshot, snap.SnapshotID)
	case snap.DataSource != "shopify":
		return nil, fmt.Errorf("%w: data_source %.20q", ErrBadSnapshot, snap.DataSource)
	case len(snap.Orders) > MaxSnapshotOrders:
		return nil, fmt.Errorf("%w: %d orders", ErrBadSnapshot, len(snap.Orders))
	case len(snap.Coverage) == 0 || snap.Coverage[0] != '{':
		return nil, fmt.Errorf("%w: coverage missing", ErrBadSnapshot)
	}
	var cov struct {
		Agents map[string]struct {
			Covered *bool `json:"covered"`
		} `json:"agents"`
	}
	if err := json.Unmarshal(snap.Coverage, &cov); err != nil || cov.Agents == nil {
		return nil, fmt.Errorf("%w: coverage lists no agents", ErrBadSnapshot)
	}
	snap.CoveredAgents = map[string]bool{}
	for id, a := range cov.Agents {
		if a.Covered == nil {
			return nil, fmt.Errorf("%w: coverage of %.64q has no covered flag", ErrBadSnapshot, id)
		}
		if *a.Covered {
			snap.CoveredAgents[id] = true
		}
	}
	if snap.Orders == nil {
		snap.Orders = []Order{}
	}
	return &snap, nil
}
